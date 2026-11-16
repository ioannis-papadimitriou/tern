"""``FakeBridge``: the extension side of tern-bridge/1.0, served from a synthetic mailbox.

Daemon code talks to the extension through ``ExtensionRequester`` (``request``/``notify``);
``ExtensionLink`` is the real one, ``FakeBridge`` the test one (plan 03 step 4). It behaves like
the extension's handlers (``extension/src/background/handlers.ts``) over TB 153, as measured in
S1 and read from the comm-esr153 sources:

- **Ids are session-scoped.** ``tb_id``s belong to one TB session, cursors to one bridge
  session. ``restart()`` (a TB restart) renumbers every message and voids every cursor; an old
  id → ``stale_id``. ``end_session()`` (the bridge session ends: port lost or
  ``bridge.reconnected``) voids every cursor only; ``tb_id``s stay valid, as in the extension. A
  move gives the message a new ``tb_id`` (TB does), so the old one → ``stale_id`` too.
- **Paging.** ``mail.messages.page`` filters one folder by an inclusive ``[from_date, to_date]``
  (S1 ``paging-pre``), returns ``page_size`` items per page in folder (file) order, **not** date
  order (``messages.query`` cannot sort, S1 NOTES F3), and an opaque single-use cursor while
  more remain. With ``slow_first_page=True`` every query's first page is empty but continues,
  as TB's ``autoPaginationTimeout`` can produce. At most ``max_open_cursors`` (the extension's
  ``MAX_OPEN_CURSORS``) stay open; a new one evicts the oldest (insertion order; a used cursor is
  removed, its continuation gets a fresh one at the end). A cursor belongs to the folder it was
  opened on.
- **Errors.** ``too_large`` above ``RAW_MAX``; ``invalid_params`` for an unknown account, a date
  without a UTC offset, or a cursor used with another ``folder_id`` (the cursor stays valid);
  ``tb_error``/``folder_not_found`` for an unknown folder.
- **Events.** ``deliver``, ``move``, ``delete``, ``update`` and ``folder_changed`` change the
  mailbox and emit the 1.0 notification to ``on_event``; ``drop_events(n)`` swallows the next
  ``n`` notifications while the state still changes (for reconcile tests).
- **Write requests** (plan 06b step 3): ``mail.update`` (returns each message's ``prev_state``;
  remove_tags, then add_tags, then flagged/read), ``mail.tags.create``, ``mail.move``,
  ``mail.archive`` (to ``dest_folder_id``, else the account's ``archives`` folder) and
  ``mail.delete`` (to the account's ``trash`` folder; a message already in Trash is deleted
  permanently, as TB does). Each checks every ``tb_id`` before it changes anything (an old one →
  ``stale_id``) and emits the 1.0 event TB would emit: ``event.mail.updated`` per folder,
  ``event.mail.moved`` per (source, destination) pair (a move to Trash is a move, as the
  extension's ``onMoved``), ``event.mail.deleted`` for a permanent delete. A missing Archives or
  Trash folder is created first (``<account>:/Archives`` / ``<account>:/Trash``, no event), as TB
  creates them on first use. With ``apply_writes = False`` a write request is validated and
  answered as if it succeeded (``prev_state`` as now, ``new_tb_id`` None) but changes nothing
  and emits nothing: a write TB accepts without applying (plan 06b write-status tests).
  ``mail.draft.save`` and ``mail.compose.open`` are not served (``method_not_available``).
- **Planted ids:** ``first_tb_id`` starts each session's numbering elsewhere than 1 (``restart``
  keeps it), so a test can plant distinctive ``tb_id`` values and look for them in output.
- **Test-only changes without an event** (plan 04 step 7): ``add_mbox(..., special_use=)`` adds a
  folder; ``set_headers_only(tb_id, value)`` marks a message as held headers-only by TB (its
  PageItem says so; ``raw``/``full`` still serve it, as TB would fetch it from the server);
  ``set_date(tb_id, date)`` gives a message another TB date (its raw unchanged: TB dates mail
  whose Date header it cannot read by arrival);
  ``rename_folder(folder_id, new_path)`` gives the folder a new id and path (only that folder,
  not folders below it); its messages get new ``tb_id``s (the old ones go stale) and its open
  cursors are voided. Call ``folder_changed`` to emit the event, as TB would.

Every call is recorded in ``calls``; every params and result object goes through the 1.0 models
in ``protocol.METHODS`` (a fake that drifts from the contract fails the test using it).

Messages without a Message-ID: TB generates an id from the headers; here it is
``md5:<hex md5 of the header block>`` (the real format is confirmed by plan 04's real run).
Accounts: ``account_id`` is the MANIFEST key; one identity per account, its address the account
name when that is an address, else ``<key>@example.invalid``.
"""

from __future__ import annotations

import base64
import email
import hashlib
import inspect
import itertools
import json
import mailbox
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from tern.bridge.protocol import (
    METHODS,
    RAW_MAX,
    Account,
    AccountList,
    ArchiveParams,
    AttachmentInfo,
    BridgeError,
    DeleteParams,
    DeleteResult,
    Empty,
    FindParams,
    Flags,
    Folder,
    FolderChanged,
    FolderList,
    FoldersListParams,
    FoundList,
    FoundLocation,
    FullMessage,
    Identity,
    MailDeleted,
    MailMoved,
    MailNew,
    MailUpdated,
    Moved,
    MoveParams,
    MoveResult,
    Page,
    PageItem,
    PageParams,
    PrevState,
    Raw,
    Tag,
    TagCreateParams,
    TagKey,
    TagList,
    TbIdParams,
    TextPart,
    UpdatedItem,
    UpdateParams,
    UpdateResult,
    dump,
)

PAGE_SIZE = 100  # TB's messagesPerPage default, as the extension sets it (api.ts PAGE_SIZE)
MAX_OPEN_CURSORS = 64  # the extension's MAX_OPEN_CURSORS (handlers.ts)
SESSION_STRIDE = 1_000_000  # tb_ids of session n start at n * SESSION_STRIDE + 1

# TB's default tags (a fresh profile).
DEFAULT_TAGS = (
    Tag(key="$label1", tag="Important", color="#FF0000"),
    Tag(key="$label2", tag="Work", color="#FF9900"),
    Tag(key="$label3", tag="Personal", color="#009900"),
    Tag(key="$label4", tag="To Do", color="#3333FF"),
    Tag(key="$label5", tag="Later", color="#993399"),
)

EventSink = Callable[[str, BaseModel], Awaitable[None]]
Handler = Callable[[Any], BaseModel | Awaitable[BaseModel]]

# What a derived tag key may not contain (TB's legacy tag service replaces such characters).
_TAG_KEY_BAD = re.compile(r"[^a-z0-9_$.-]")


@dataclass
class _Msg:
    raw: bytes
    folder_id: str
    header_message_id: str
    date: datetime
    read: bool = False
    flagged: bool = False
    junk: bool = False
    tags: list[str] = field(default_factory=list[str])
    headers_only: bool = False


@dataclass
class _Cursor:
    folder_id: str
    rest: list[_Msg]


@dataclass
class _Folder:
    folder_id: str
    account_id: str
    path: str
    special_use: list[str]


def header_message_id(raw: bytes) -> str:
    """TB's ``headerMessageId``: the Message-ID without angle brackets, or a generated id."""
    headers = BytesHeaderParser(policy=policy.compat32).parsebytes(raw)
    mid = headers.get("Message-ID")
    if mid is not None and str(mid).strip():
        return str(mid).strip().removeprefix("<").removesuffix(">")
    block = raw.split(b"\n\n", 1)[0].split(b"\r\n\r\n", 1)[0]
    return "md5:" + hashlib.md5(block).hexdigest()


def _date(raw: bytes) -> datetime:
    value = BytesHeaderParser(policy=policy.compat32).parsebytes(raw).get("Date")
    if value is None:
        raise ValueError("synthetic message without a Date header")
    d = parsedate_to_datetime(str(value))
    if d.tzinfo is None:
        raise ValueError("synthetic message with a naive Date header")
    return d


def _leaf_bytes(part: EmailMessage) -> bytes:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        raise TypeError("a non-multipart part without a decodable payload")
    return payload


def _open(path: Path, fmt: str) -> mailbox.Mailbox[Any]:
    if fmt == "mbox":
        return mailbox.mbox(path, create=False)
    if fmt == "maildir":
        return mailbox.Maildir(path, factory=None, create=False)
    raise ValueError(f"unknown folder format {fmt!r}")


class FakeBridge:
    """A synthetic Thunderbird behind the extension; see the module docstring."""

    def __init__(
        self,
        mailbox_dir: Path,
        *,
        page_size: int = PAGE_SIZE,
        slow_first_page: bool = False,
        max_open_cursors: int = MAX_OPEN_CURSORS,
        first_tb_id: int = 1,
    ) -> None:
        if max_open_cursors < 1:
            raise ValueError("max_open_cursors must be at least 1")
        if not 1 <= first_tb_id < SESSION_STRIDE:
            raise ValueError(f"first_tb_id must be in 1..{SESSION_STRIDE - 1}")
        self.page_size = page_size
        self.max_open_cursors = max_open_cursors
        self.slow_first_page = slow_first_page
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.on_event: EventSink | None = None
        self.dropped_events = 0
        #: False: write requests are answered as if applied but change nothing (module docstring).
        self.apply_writes = True
        self.session = 0
        self._first_tb_id = first_tb_id
        self._tags: list[Tag] = list(DEFAULT_TAGS)
        self._drop_next = 0
        self._accounts: list[Account] = []
        self._folders: dict[str, _Folder] = {}  # folder_id → folder, in MANIFEST order
        self._msgs: list[_Msg] = []  # every message, folder (file) order, appended on deliver
        self._ids: dict[int, _Msg] = {}  # tb_id → message (this session)
        self._tb_id: dict[int, int] = {}  # id(message) → its tb_id (this session)
        self._cursors: dict[str, _Cursor] = {}  # token → cursor, insertion-ordered (oldest first)
        self._next_tb_id = itertools.count(first_tb_id)
        self._cursor_seq = itertools.count(1)
        self._load(mailbox_dir)

    # -- setup -----------------------------------------------------------------------------------

    def _load(self, root: Path) -> None:
        manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
        for a in manifest["accounts"]:
            email_addr = a["name"] if "@" in a["name"] else f"{a['key']}@example.invalid"
            self._accounts.append(
                Account(
                    account_id=a["key"],
                    name=a["name"],
                    type=a["type"],
                    identities=[
                        Identity(
                            identity_id=f"{a['key']}-id1",
                            email=email_addr,
                            name=a["name"],
                            is_default=True,
                        )
                    ],
                )
            )
            for f in a["folders"]:
                folder_id = f"{a['key']}:/{f['path']}"
                special = [f["special_use"]] if f["special_use"] else []
                self._folders[folder_id] = _Folder(folder_id, a["key"], f["path"], special)
                box = _open(root / f["file"], f["format"])
                try:
                    for key in sorted(box.keys()) if f["format"] == "maildir" else box.keys():
                        self._add(box.get_bytes(key), folder_id)
                finally:
                    box.close()

    def add_mbox(
        self,
        account_id: str,
        path: str,
        mbox_file: Path,
        special_use: list[str] | None = None,
    ) -> str:
        """Add a folder from an mbox file (e.g. ``gen_synthetic_mailbox.py --large`` output),
        with TB's ``specialUse`` values (e.g. ``["sent"]``, ``["trash"]``)."""
        if account_id not in {a.account_id for a in self._accounts}:
            raise ValueError(f"unknown account {account_id!r}")
        folder_id = f"{account_id}:/{path}"
        if folder_id in self._folders:
            raise ValueError("folder already exists")
        self._folders[folder_id] = _Folder(folder_id, account_id, path, list(special_use or []))
        box = _open(mbox_file, "mbox")
        try:
            for key in box.keys():
                self._add(box.get_bytes(key), folder_id)
        finally:
            box.close()
        return folder_id

    def _add(self, raw: bytes, folder_id: str) -> _Msg:
        m = _Msg(raw, folder_id, header_message_id(raw), _date(raw))
        self._msgs.append(m)
        self._assign(m)
        return m

    def _assign(self, m: _Msg) -> int:
        tb_id = self.session * SESSION_STRIDE + next(self._next_tb_id)
        self._ids[tb_id] = m
        self._tb_id[id(m)] = tb_id
        return tb_id

    # -- session ---------------------------------------------------------------------------------

    def restart(self) -> None:
        """A TB restart: every tb_id and every cursor of the old session becomes stale."""
        self.session += 1
        self._next_tb_id = itertools.count(self._first_tb_id)
        self._ids.clear()
        self._tb_id.clear()
        self.end_session()
        for m in self._msgs:
            self._assign(m)

    def end_session(self) -> None:
        """The bridge session ends (port lost or ``bridge.reconnected``): every cursor becomes
        stale; tb_ids stay valid (they belong to TB's session, not the bridge's)."""
        self._cursors.clear()

    def set_headers_only(self, tb_id: int, value: bool) -> None:
        """Mark a message as held headers-only by TB (or as downloaded, ``value=False``)."""
        self._msg(tb_id).headers_only = value

    def set_date(self, tb_id: int, date: datetime) -> None:
        """Give a message another TB date (no event), as TB dates a message whose Date header it
        cannot read by its arrival; ``date`` must carry a UTC offset. The raw is unchanged, so
        byte-identical copies can sit at different dates (plan 04 M3)."""
        if date.tzinfo is None:
            raise ValueError("set_date needs a date with a UTC offset")
        self._msg(tb_id).date = date

    def rename_folder(self, folder_id: str, new_path: str) -> str:
        """Rename one folder (no event; see the module docstring). Returns the new folder_id."""
        old = self._folder(folder_id)
        new_id = f"{old.account_id}:/{new_path}"
        if new_id in self._folders:
            raise ValueError("a folder with that path exists")
        self._folders = {
            (new_id if fid == folder_id else fid): (
                _Folder(new_id, f.account_id, new_path, list(f.special_use))
                if fid == folder_id
                else f
            )
            for fid, f in self._folders.items()
        }
        for m in self._msgs:
            if m.folder_id == folder_id:
                del self._ids[self._tb_id.pop(id(m))]
                m.folder_id = new_id
                self._assign(m)
        for token in [t for t, c in self._cursors.items() if c.folder_id == folder_id]:
            del self._cursors[token]
        return new_id

    def tb_ids(self, folder_id: str) -> list[int]:
        """Current tb_ids of a folder, in folder order (test convenience, not a bridge method)."""
        return [self._tb_id[id(m)] for m in self._msgs if m.folder_id == folder_id]

    # -- ExtensionRequester ------------------------------------------------------------------------

    async def request(self, method: str, params: BaseModel) -> Any:
        spec = METHODS.get(method)
        if spec is None or spec.kind != "request" or spec.direction not in ("d2e", "any"):
            raise ValueError(f"{method} is not a daemon→extension request")
        wire = dump(params)
        validated = spec.params.model_validate(wire)
        self.calls.append((method, wire))
        handler = self._handlers().get(method)
        if handler is None:
            raise BridgeError("method_not_available", f"{method} not served by the extension yet")
        result = handler(validated)
        if inspect.isawaitable(result):
            result = await result
        assert spec.result is not None
        return spec.result.model_validate(dump(result))  # what ExtensionLink would return

    async def notify(self, method: str, params: BaseModel) -> None:
        spec = METHODS.get(method)
        if spec is None or spec.kind != "notification" or spec.direction != "d2e":
            raise ValueError(f"{method} is not a daemon→extension notification")
        self.notifications.append((method, dump(spec.params.model_validate(dump(params)))))

    def _handlers(self) -> dict[str, Handler]:
        return {
            "bridge.ping": lambda _p: Empty(),
            "mail.accounts.list": lambda _p: AccountList(list(self._accounts)),
            "mail.folders.list": self._folders_list,
            "mail.messages.page": self._page,
            "mail.messages.raw": self._raw,
            "mail.messages.full": self._full,
            "mail.messages.find": self._find,
            "mail.open": self._open_msg,
            "mail.tags.list": lambda _p: TagList(list(self._tags)),
            "mail.tags.create": self._tags_create,
            "mail.update": self._update_req,
            "mail.move": self._move_req,
            "mail.archive": self._archive_req,
            "mail.delete": self._delete_req,
        }

    # -- R1 methods ------------------------------------------------------------------------------

    def _msg(self, tb_id: int) -> _Msg:
        m = self._ids.get(tb_id)
        if m is None:
            raise BridgeError("stale_id", "tb_id is not valid in this session")
        return m

    def _folder(self, folder_id: str) -> _Folder:
        f = self._folders.get(folder_id)
        if f is None:
            raise BridgeError("tb_error", "Thunderbird call failed", tb_code="folder_not_found")
        return f

    def _flags(self, m: _Msg) -> Flags:
        return Flags(read=m.read, flagged=m.flagged, junk=m.junk)

    def _item(self, m: _Msg) -> PageItem:
        return PageItem(
            tb_id=self._tb_id[id(m)],
            header_message_id=m.header_message_id,
            date=m.date,
            size=len(m.raw),
            flags=self._flags(m),
            tags=list(m.tags),
            headers_only=m.headers_only,
        )

    def _folders_list(self, p: FoldersListParams) -> FolderList:
        if p.account_id not in {a.account_id for a in self._accounts}:
            raise BridgeError("invalid_params", "account_id: unknown account")
        out: list[Folder] = []
        for f in self._folders.values():
            if f.account_id != p.account_id:
                continue
            msgs = [m for m in self._msgs if m.folder_id == f.folder_id]
            out.append(
                Folder(
                    folder_id=f.folder_id,
                    path=f.path,
                    name=f.path.rsplit("/", 1)[-1],
                    special_use=list(f.special_use),
                    total=len(msgs),
                    unread=sum(1 for m in msgs if not m.read),
                )
            )
        return FolderList(out)

    def _page(self, p: PageParams) -> Page:
        for name, value in (("from_date", p.from_date), ("to_date", p.to_date)):
            if value is not None and value.utcoffset() is None:
                raise BridgeError("invalid_params", f"{name}: date needs a UTC offset")
        if p.cursor is not None:
            cursor = self._cursors.get(p.cursor)
            if cursor is None:
                raise BridgeError("stale_id", "cursor is not valid")
            if cursor.folder_id != p.folder_id:
                raise BridgeError("invalid_params", "cursor belongs to another folder")
            del self._cursors[p.cursor]
            return self._paginate(p.folder_id, cursor.rest)
        self._folder(p.folder_id)
        hits = [
            m
            for m in self._msgs
            if m.folder_id == p.folder_id
            and (p.from_date is None or m.date >= p.from_date)
            and (p.to_date is None or m.date <= p.to_date)
        ]
        if self.slow_first_page and hits:
            return Page(items=[], next_cursor=self._remember(p.folder_id, hits))
        return self._paginate(p.folder_id, hits)

    def _paginate(self, folder_id: str, msgs: list[_Msg]) -> Page:
        first, rest = msgs[: self.page_size], msgs[self.page_size :]
        items = [self._item(m) for m in first]
        return Page(items=items, next_cursor=self._remember(folder_id, rest) if rest else None)

    def _remember(self, folder_id: str, rest: list[_Msg]) -> str:
        if len(self._cursors) >= self.max_open_cursors:
            del self._cursors[next(iter(self._cursors))]  # the oldest, as the extension aborts it
        token = f"cur-{self.session}-{next(self._cursor_seq)}"
        self._cursors[token] = _Cursor(folder_id, rest)
        return token

    def _raw(self, p: TbIdParams) -> Raw:
        m = self._msg(p.tb_id)
        if len(m.raw) > RAW_MAX:
            raise BridgeError("too_large", f"message over {RAW_MAX} bytes")
        return Raw(raw_b64=base64.b64encode(m.raw).decode("ascii"))

    def _full(self, p: TbIdParams) -> FullMessage:
        m = self._msg(p.tb_id)
        msg = email.message_from_bytes(m.raw, policy=policy.default)
        assert isinstance(msg, EmailMessage)
        headers: dict[str, list[str]] = {}
        for name, value in msg.items():
            headers.setdefault(name.lower(), []).append(str(value))
        texts: list[TextPart] = []
        attachments: list[AttachmentInfo] = []
        for part in msg.walk():
            if part.is_multipart():
                continue
            if part.is_attachment():
                payload = _leaf_bytes(part)
                name = part.get_filename() or ""
                attachments.append(
                    AttachmentInfo(name=name, mime=part.get_content_type(), size=len(payload))
                )
            elif part.get_content_maintype() == "text":
                charset = part.get_content_charset() or "utf-8"
                # TB shows undecodable bytes as replacement characters; so does the fake.
                text = _leaf_bytes(part).decode(charset, errors="replace")
                texts.append(TextPart(mime=part.get_content_type(), text=text))
        return FullMessage(headers=headers, text_parts=texts, attachments=attachments)

    def _find(self, p: FindParams) -> FoundList:
        return FoundList(
            [
                FoundLocation(
                    tb_id=self._tb_id[id(m)],
                    folder_id=m.folder_id,
                    flags=self._flags(m),
                    tags=list(m.tags),
                )
                for m in self._msgs
                if m.header_message_id == p.header_message_id
            ]
        )

    def _open_msg(self, p: TbIdParams) -> Empty:
        self._msg(p.tb_id)
        return Empty()

    # -- write requests (plan 06b step 3) --------------------------------------------------------

    def _checked(self, tb_ids: list[int]) -> list[tuple[int, _Msg]]:
        """Each listed tb_id once with its message; any old id → ``stale_id`` before a change."""
        return [(t, self._msg(t)) for t in dict.fromkeys(tb_ids)]

    def _special_folder(self, account_id: str, use: str, path: str) -> str:
        """The account's folder with ``special_use`` ``use``, created at ``path`` if missing."""
        for f in self._folders.values():
            if f.account_id == account_id and use in f.special_use:
                return f.folder_id
        folder_id = f"{account_id}:/{path}"
        if folder_id in self._folders:
            raise ValueError(f"{folder_id} exists without special_use {use}")
        self._folders[folder_id] = _Folder(folder_id, account_id, path, [use])
        return folder_id

    def _tags_create(self, p: TagCreateParams) -> TagKey:
        if any(t.tag.lower() == p.tag.lower() for t in self._tags):
            raise BridgeError("invalid_params", "tag: a tag with this name exists")
        base = _TAG_KEY_BAD.sub("_", p.tag.lower())
        keys = {t.key for t in self._tags}
        key = base
        for n in itertools.count(2):
            if key not in keys:
                break
            key = f"{base}_{n}"
        if self.apply_writes:
            self._tags.append(Tag(key=key, tag=p.tag, color=p.color or ""))
        return TagKey(key=key)

    async def _update_req(self, p: UpdateParams) -> UpdateResult:
        msgs = self._checked(p.tb_ids)
        prev = [
            PrevState(tb_id=t, tags=list(m.tags), flagged=m.flagged, read=m.read) for t, m in msgs
        ]
        if not self.apply_writes:
            return UpdateResult(prev_state=prev)
        remove = set(p.remove_tags or [])
        by_folder: dict[str, list[UpdatedItem]] = {}
        for t, m in msgs:
            tags = [k for k in m.tags if k not in remove]
            tags += [k for k in dict.fromkeys(p.add_tags or []) if k not in tags]
            m.tags = tags
            if p.flagged is not None:
                m.flagged = p.flagged
            if p.read is not None:
                m.read = p.read
            by_folder.setdefault(m.folder_id, []).append(
                UpdatedItem(
                    tb_id=t,
                    folder_id=m.folder_id,
                    header_message_id=m.header_message_id,
                    flags=self._flags(m),
                    tags=list(m.tags),
                )
            )
        for items in by_folder.values():  # one event per folder, as the extension batches them
            await self._emit("event.mail.updated", MailUpdated(items=items))
        return UpdateResult(prev_state=prev)

    async def _move_to(self, dests: list[tuple[int, str]]) -> MoveResult:
        """Move each (tb_id, destination); already checked. One ``move`` per destination."""
        if not self.apply_writes:
            return MoveResult(moved=[Moved(tb_id=t, new_tb_id=None) for t, _ in dests])
        by_dest: dict[str, list[int]] = {}
        for t, dst in dests:
            by_dest.setdefault(dst, []).append(t)
        new: dict[int, int] = {}
        for dst, ids in by_dest.items():
            new.update(zip(ids, await self.move(ids, dst), strict=True))
        return MoveResult(moved=[Moved(tb_id=t, new_tb_id=new[t]) for t, _ in dests])

    async def _move_req(self, p: MoveParams) -> MoveResult:
        self._folder(p.dest_folder_id)
        return await self._move_to([(t, p.dest_folder_id) for t, _ in self._checked(p.tb_ids)])

    async def _archive_req(self, p: ArchiveParams) -> MoveResult:
        if p.dest_folder_id is not None:
            self._folder(p.dest_folder_id)
        msgs = self._checked(p.tb_ids)
        if not self.apply_writes:
            return MoveResult(moved=[Moved(tb_id=t, new_tb_id=None) for t, _ in msgs])
        dests = [
            (
                t,
                p.dest_folder_id
                or self._special_folder(
                    self._folders[m.folder_id].account_id, "archives", "Archives"
                ),
            )
            for t, m in msgs
        ]
        return await self._move_to(dests)

    async def _delete_req(self, p: DeleteParams) -> DeleteResult:
        msgs = self._checked(p.tb_ids)
        if not self.apply_writes:
            return DeleteResult(trashed=[t for t, _ in msgs])
        to_trash: list[tuple[int, str]] = []
        permanent: list[int] = []
        for t, m in msgs:
            folder = self._folders[m.folder_id]
            if "trash" in folder.special_use:
                permanent.append(t)
            else:
                to_trash.append((t, self._special_folder(folder.account_id, "trash", "Trash")))
        await self._move_to(to_trash)
        if permanent:
            await self.delete(permanent)
        return DeleteResult(trashed=[t for t, _ in msgs])

    # -- events (TB-side changes) ----------------------------------------------------------------

    def drop_events(self, n: int = 1) -> None:
        """The next ``n`` notifications are lost (the mailbox still changes)."""
        self._drop_next += n

    async def _emit(self, method: str, params: BaseModel) -> None:
        spec = METHODS[method]
        assert spec.kind == "notification" and spec.direction == "e2d", method
        spec.params.model_validate(dump(params))
        if self._drop_next > 0:
            self._drop_next -= 1
            self.dropped_events += 1
            return
        if self.on_event is not None:
            await self.on_event(method, params)

    async def deliver(self, folder_id: str, raw: bytes) -> int:
        """New mail arrives in a folder → ``event.mail.new``. Returns its tb_id."""
        self._folder(folder_id)
        m = self._add(raw, folder_id)
        await self._emit("event.mail.new", MailNew(folder_id=folder_id, items=[self._item(m)]))
        return self._tb_id[id(m)]

    async def move(self, tb_ids: list[int], dst_folder_id: str) -> list[int]:
        """Move messages (new tb_ids, the old ones go stale) → ``event.mail.moved`` per source.
        Returns the new tb_ids in the order of ``tb_ids`` (each listed once)."""
        self._folder(dst_folder_id)
        tb_ids = list(dict.fromkeys(tb_ids))
        by_src: dict[str, list[tuple[int, _Msg]]] = {}
        for tb_id in tb_ids:
            m = self._msg(tb_id)
            by_src.setdefault(m.folder_id, []).append((tb_id, m))
        new_id: dict[int, int] = {}
        for src, msgs in by_src.items():
            moved: list[_Msg] = []
            for old, m in msgs:
                del self._ids[self._tb_id.pop(id(m))]
                self._msgs.remove(m)
                n = replace(m, folder_id=dst_folder_id, tags=list(m.tags))
                self._msgs.append(n)
                new_id[old] = self._assign(n)
                moved.append(n)
            await self._emit(
                "event.mail.moved",
                MailMoved(
                    src_folder_id=src,
                    dst_folder_id=dst_folder_id,
                    items=[self._item(n) for n in moved],
                ),
            )
        return [new_id[t] for t in tb_ids]

    async def delete(self, tb_ids: list[int]) -> None:
        """Permanent delete → ``event.mail.deleted`` per folder."""
        tb_ids = list(dict.fromkeys(tb_ids))
        for tb_id in tb_ids:
            self._msg(tb_id)  # every id checked before anything changes
        by_folder: dict[str, list[str]] = {}
        for tb_id in tb_ids:
            m = self._msg(tb_id)
            del self._ids[self._tb_id.pop(id(m))]
            self._msgs.remove(m)
            by_folder.setdefault(m.folder_id, []).append(m.header_message_id)
        for folder_id, hmids in by_folder.items():
            await self._emit(
                "event.mail.deleted", MailDeleted(folder_id=folder_id, header_message_ids=hmids)
            )

    async def update(
        self,
        tb_id: int,
        *,
        read: bool | None = None,
        flagged: bool | None = None,
        junk: bool | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Flag/tag/read change → ``event.mail.updated``."""
        m = self._msg(tb_id)
        if read is not None:
            m.read = read
        if flagged is not None:
            m.flagged = flagged
        if junk is not None:
            m.junk = junk
        if tags is not None:
            m.tags = list(tags)
        await self._emit(
            "event.mail.updated",
            MailUpdated(
                items=[
                    UpdatedItem(
                        tb_id=tb_id,
                        folder_id=m.folder_id,
                        header_message_id=m.header_message_id,
                        flags=self._flags(m),
                        tags=list(m.tags),
                    )
                ]
            ),
        )

    async def folder_changed(self, folder_id: str) -> None:
        await self._emit("event.folder.changed", FolderChanged(folder_id=folder_id))
