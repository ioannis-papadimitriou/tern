"""A synthetic Thunderbird profile (GLODA index + ``prefs.js``) from a ``FakeBridge`` mailbox.

The vendored thunderbird-mcp tools (``tern.tbmcp``, ``tb_*``, #38) read a Thunderbird PROFILE
directory: ``global-messages-db.sqlite`` (GLODA) and ``prefs.js``. :func:`build_profile` writes
one from the same synthetic mailbox a tern index was built from, so tool results and tern's
citation check agree: every GLODA row's ``headerMessageID`` maps to a ``mid`` in that index (the
tb tools add tern's short ``mid`` to each result, ``tern.tools.builtin.tb.with_mids``).

Source (synthetic data only): the ``FakeBridge`` the index was ingested from, read through its own
bridge requests (``mail.accounts.list``, ``mail.folders.list``, ``mail.messages.page``,
``mail.messages.raw``), so a read/flagged state planted on it before the ingest reaches GLODA as
well; and the tern index (``Store``), for each copy's ``thread_id``.

What GLODA gets, per message COPY (GLODA has one row per folder copy and thunderbird-mcp
de-duplicates by Message-ID, keeping the lowest row id; rows are numbered in bridge order:
accounts, then folders, then file order, as TB's indexing sweep would):

- ``messages``: ``folderID``; ``conversationID`` from the tern index's ``thread_id`` of the copy's
  mid (one integer per distinct thread, in first-seen order); ``date`` in microseconds since the
  epoch (the PageItem date, TB's); ``headerMessageID`` as TB gives it (the PageItem's
  ``header_message_id``: brackets stripped, or TB's invented ``md5:`` id when the message has no
  Message-ID); ``deleted`` 0; ``jsonAttributes`` (attribute ids as ``tern.tbmcp.db``): 58 starred
  = the copy's flagged state, 59 read, 60 replied (some message from one of the user's
  identities has ``In-Reply-To`` naming this Message-ID), 54 fromMe / 55 toMe when the author /
  a To/Cc address is one of the account's identities, 56 mailing list (the ``List-Id``) when the
  message has one.
- ``messagesText_content`` (a plain table, as thunderbird-mcp queries it; the FTS table itself
  needs TB's ``mozporter`` tokenizer and is not created): ``c0body`` tern's parsed main text (quoted
  text split off, as GLODA whittles quotes), ``c1subject``, ``c2attachmentNames``
  (newline-separated), ``c3author`` (``Name <addr>``), ``c4recipients`` (To and Cc addresses,
  comma-separated).
- ``folderLocations``: TB's folder URI (``imap://`` or, for POP3, ``mailbox://``,
  ``<user>@<host>/<path>``) and name (``INBOX`` shows as ``Inbox``, as TB names it; any other
  folder by its last path component).

``prefs.js``: one ``mail.account.accountN`` per bridge account with its server (type from the
bridge; user name = the first identity; host ``imap.<domain>`` / ``pop.<domain>``) and identities.
An account's identities are derived from the mail itself: the account name when it is an address,
plus the senders of its Sent folders' messages; when that gives none, the most frequent To/Cc
address of its messages (the synthetic fixture's ``acct2`` is named by a sentinel, not an
address).

Not built: calendar (``calendar-data/``) and address book (``abook.sqlite``) databases, so the
calendar tools see no calendars and ``tb_contact`` has no card.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser
from pathlib import Path

from tern.bridge.protocol import (
    AccountList,
    Empty,
    FolderList,
    FoldersListParams,
    Page,
    PageItem,
    PageParams,
    Raw,
    TbIdParams,
)
from tern.index.mid import mid_from_raw
from tern.index.mime import ParsedMessage, parse_raw
from tern.index.store import Store
from tern.testing.fake_bridge import FakeBridge

GLODA_DB = "global-messages-db.sqlite"
PREFS_JS = "prefs.js"

# GLODA attribute ids (``tern.tbmcp.db``): fromMe, toMe, mailing list, star, read, repliedTo.
ATTR_FROM_ME, ATTR_TO_ME, ATTR_LIST = "54", "55", "56"
ATTR_STAR, ATTR_READ, ATTR_REPLIED = "58", "59", "60"

_SCHEMA = """
CREATE TABLE folderLocations (id INTEGER PRIMARY KEY, folderURI TEXT, name TEXT);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, folderID INTEGER, conversationID INTEGER,
    date INTEGER, headerMessageID TEXT, deleted INTEGER, jsonAttributes TEXT
);
CREATE TABLE messagesText_content (
    docid INTEGER PRIMARY KEY, c0body TEXT, c1subject TEXT,
    c2attachmentNames TEXT, c3author TEXT, c4recipients TEXT
);
"""


class GlodaBuildError(RuntimeError):
    """The profile cannot be built consistently with the index (e.g. a copy's mid is missing)."""


@dataclass(frozen=True)
class GlodaAccount:
    account_id: str  # the bridge's account_id
    type: str  # "imap" | "pop3" | ...
    username: str
    hostname: str
    identities: tuple[str, ...]


@dataclass(frozen=True)
class GlodaFolder:
    id: int  # folderLocations.id
    folder_id: str  # the bridge's folder_id
    account_id: str
    uri: str
    name: str


@dataclass(frozen=True)
class GlodaMessage:
    id: int  # messages.id (= messagesText_content.docid)
    folder_id: str  # the bridge's folder_id of this copy
    mid: str  # tern's mid of this copy (``mid_from_raw``), present in the index
    header_message_id: str  # GLODA's headerMessageID
    conversation_id: int
    date_us: int
    subject: str
    author: str
    recipients: str
    attachments: tuple[str, ...]
    read: bool
    starred: bool
    replied: bool


@dataclass(frozen=True)
class GlodaProfile:
    """The profile directory and what was written into it."""

    path: Path
    accounts: tuple[GlodaAccount, ...]
    folders: tuple[GlodaFolder, ...]
    messages: tuple[GlodaMessage, ...]

    @property
    def gloda_path(self) -> Path:
        return self.path / GLODA_DB

    @property
    def my_addresses(self) -> frozenset[str]:
        return frozenset(a for acc in self.accounts for a in acc.identities)


@dataclass(frozen=True)
class _Copy:
    account_id: str
    folder_id: str
    sent_folder: bool
    item: PageItem
    parsed: ParsedMessage
    mid: str
    list_id: str | None


# ---- reading the bridge -------------------------------------------------------------------------


async def _copies(bridge: FakeBridge) -> tuple[AccountList, dict[str, FolderList], list[_Copy]]:
    accounts: AccountList = await bridge.request("mail.accounts.list", Empty())
    folders: dict[str, FolderList] = {}
    copies: list[_Copy] = []
    for account in accounts.root:
        listed: FolderList = await bridge.request(
            "mail.folders.list", FoldersListParams(account_id=account.account_id)
        )
        folders[account.account_id] = listed
        for folder in listed.root:
            cursor: str | None = None
            while True:
                page: Page = await bridge.request(
                    "mail.messages.page", PageParams(folder_id=folder.folder_id, cursor=cursor)
                )
                for item in page.items:
                    raw_res: Raw = await bridge.request(
                        "mail.messages.raw", TbIdParams(tb_id=item.tb_id)
                    )
                    raw = base64.b64decode(raw_res.raw_b64)
                    list_id = BytesHeaderParser(policy=policy.compat32).parsebytes(raw)["List-Id"]
                    copies.append(
                        _Copy(
                            account_id=account.account_id,
                            folder_id=folder.folder_id,
                            sent_folder="sent" in folder.special_use,
                            item=item,
                            parsed=parse_raw(raw),
                            mid=mid_from_raw(raw),
                            list_id=None if list_id is None else str(list_id).strip().strip("<>"),
                        )
                    )
                cursor = page.next_cursor
                if cursor is None:
                    break
    return accounts, folders, copies


def _identities(name: str, copies: list[_Copy]) -> tuple[str, ...]:
    """The account's own addresses (module docstring)."""
    out: dict[str, None] = {}
    if "@" in name:
        out[name.lower()] = None
    for c in copies:
        if c.sent_folder and c.parsed.from_addr:
            out[c.parsed.from_addr.lower()] = None
    if not out:
        counts = Counter(
            a.lower() for c in copies for a in (*c.parsed.to_addrs, *c.parsed.cc_addrs)
        )
        if not counts:
            raise GlodaBuildError("an account has no address to derive an identity from")
        out[counts.most_common(1)[0][0]] = None
    return tuple(out)


def _account(account_id: str, type_: str, identities: tuple[str, ...]) -> GlodaAccount:
    user = identities[0]
    domain = user.rpartition("@")[2]
    host = f"{'pop' if type_ == 'pop3' else 'imap'}.{domain}"
    return GlodaAccount(account_id, type_, user, host, identities)


def _folder_name(path: str) -> str:
    return "Inbox" if path.upper() == "INBOX" else path.rsplit("/", 1)[-1]


def _uri(account: GlodaAccount, path: str) -> str:
    scheme = "mailbox" if account.type == "pop3" else account.type
    user = urllib.parse.quote(account.username, safe="")
    return f"{scheme}://{user}@{account.hostname}/{urllib.parse.quote(path)}"


def _thread_ids(store: Store, mids: set[str]) -> dict[str, str | None]:
    conn = store.connection
    found: dict[str, str | None] = {}
    for mid in mids:
        row = conn.execute("SELECT thread_id FROM messages WHERE mid = ?", (mid,)).fetchone()
        if row is None:
            raise GlodaBuildError(f"mid {mid[:12]} of a bridge copy is not in the tern index")
        found[mid] = row[0]
    return found


# ---- writing ------------------------------------------------------------------------------------


def _attrs(
    *, read: bool, starred: bool, replied: bool, from_me: bool, to_me: bool, lst: str | None
) -> str:
    a: dict[str, object] = {
        "43": 1,
        "57": [],
        ATTR_STAR: starred,
        ATTR_READ: read,
        ATTR_REPLIED: replied,
        "61": False,
    }
    if from_me:
        a[ATTR_FROM_ME] = [[1, 1]]
    if to_me:
        a[ATTR_TO_ME] = [[1, 1]]
    if lst is not None:
        a[ATTR_LIST] = [lst]
    return json.dumps(a)


def _author(p: ParsedMessage) -> str:
    if not p.from_addr:
        return p.from_name or ""
    return f"{p.from_name} <{p.from_addr}>" if p.from_name else p.from_addr


def _prefs(accounts: tuple[GlodaAccount, ...]) -> str:
    lines: list[str] = ["// Synthetic prefs.js (tern.testing.gloda): mail accounts only."]
    ident = 0
    for n, acc in enumerate(accounts, 1):
        ids: list[str] = []
        for email in acc.identities:
            ident += 1
            ids.append(f"id{ident}")
            lines.append(f'user_pref("mail.identity.id{ident}.useremail", "{email}");')
        lines += [
            f'user_pref("mail.account.account{n}.server", "server{n}");',
            f'user_pref("mail.account.account{n}.identities", "{",".join(ids)}");',
            f'user_pref("mail.server.server{n}.hostname", "{acc.hostname}");',
            f'user_pref("mail.server.server{n}.userName", "{acc.username}");',
            f'user_pref("mail.server.server{n}.type", "{acc.type}");',
        ]
    lines.append(f'user_pref("mail.accountmanager.accounts", "{_account_list(len(accounts))}");')
    return "\n".join(lines) + "\n"


def _account_list(n: int) -> str:
    return ",".join(f"account{i}" for i in range(1, n + 1))


async def build_profile(bridge: FakeBridge, store: Store, profile_dir: Path) -> GlodaProfile:
    """Write a synthetic profile (``global-messages-db.sqlite`` + ``prefs.js``) of ``bridge``'s
    mailbox into ``profile_dir`` (created; must not hold a GLODA database yet), consistent with
    the tern index ``store`` built from the same bridge (module docstring). Raises
    :class:`GlodaBuildError` when a copy's mid is not in the index."""
    accounts_list, folder_lists, copies = await _copies(bridge)
    gloda = profile_dir / GLODA_DB
    if gloda.exists():
        raise GlodaBuildError(f"{gloda} exists already")

    accounts: list[GlodaAccount] = []
    for acc in accounts_list.root:
        own = [c for c in copies if c.account_id == acc.account_id]
        accounts.append(_account(acc.account_id, acc.type, _identities(acc.name, own)))
    by_account = {a.account_id: a for a in accounts}

    folders: list[GlodaFolder] = []
    for account_id, listed in folder_lists.items():
        for f in listed.root:
            account = by_account[account_id]
            folders.append(
                GlodaFolder(
                    len(folders) + 1,
                    f.folder_id,
                    account_id,
                    _uri(account, f.path),
                    _folder_name(f.path),
                )
            )
    folder_row = {f.folder_id: f for f in folders}

    threads = _thread_ids(store, {c.mid for c in copies})
    conversations: dict[str, int] = {}
    mine_from: set[str] = set()  # Message-IDs some message from an identity replies to
    everyone_mine = {a for acc in accounts for a in acc.identities}
    for c in copies:
        if (c.parsed.from_addr or "").lower() in everyone_mine and c.parsed.in_reply_to:
            mine_from.add(c.parsed.in_reply_to.strip().strip("<>").lower())

    messages: list[GlodaMessage] = []
    rows: list[tuple[object, ...]] = []
    texts: list[tuple[object, ...]] = []
    for n, c in enumerate(copies, 1):
        mine = set(by_account[c.account_id].identities)
        thread = threads[c.mid] or c.mid
        conv = conversations.setdefault(thread, len(conversations) + 1)
        p = c.parsed
        rcpts = [*p.to_addrs, *p.cc_addrs]
        from_me = (p.from_addr or "").lower() in mine
        to_me = any(a.lower() in mine for a in rcpts)
        replied = c.item.header_message_id.lower() in mine_from
        attachments = tuple(a.filename for a in p.attachments if a.filename)
        msg = GlodaMessage(
            id=n,
            folder_id=c.folder_id,
            mid=c.mid,
            header_message_id=c.item.header_message_id,
            conversation_id=conv,
            date_us=round(c.item.date.timestamp() * 1_000_000),
            subject=p.subject or "",
            author=_author(p),
            recipients=", ".join(rcpts),
            attachments=attachments,
            read=c.item.flags.read,
            starred=c.item.flags.flagged,
            replied=replied,
        )
        messages.append(msg)
        attrs = _attrs(
            read=msg.read,
            starred=msg.starred,
            replied=replied,
            from_me=from_me,
            to_me=to_me,
            lst=c.list_id,
        )
        rows.append(
            (n, folder_row[c.folder_id].id, conv, msg.date_us, msg.header_message_id, 0, attrs)
        )
        texts.append(
            (n, p.main_text, msg.subject, "\n".join(attachments), msg.author, msg.recipients)
        )

    profile_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(gloda)
    try:
        con.executescript(_SCHEMA)
        con.executemany(
            "INSERT INTO folderLocations VALUES (?, ?, ?)",
            [(f.id, f.uri, f.name) for f in folders],
        )
        con.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        con.executemany("INSERT INTO messagesText_content VALUES (?, ?, ?, ?, ?, ?)", texts)
        con.commit()
    finally:
        con.close()
    accounts_t = tuple(accounts)
    (profile_dir / PREFS_JS).write_text(_prefs(accounts_t), encoding="utf-8")
    return GlodaProfile(profile_dir, accounts_t, tuple(folders), tuple(messages))
