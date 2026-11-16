"""Read-only queries over the Thunderbird GLODA snapshot.

GLODA stores its full-text index with a custom ``mozporter`` tokenizer that is not
available in a standalone Python ``sqlite3`` build, so we cannot use the FTS table
directly. Instead we run ``LIKE`` queries against ``messagesText_content``, which
GLODA keeps populated with precomputed columns:

    c0body, c1subject, c2attachmentNames (newline-separated), c3author, c4recipients

Relevant schema:
    messages(id, folderID, conversationID, date, headerMessageID, deleted, jsonAttributes)
    messagesText_content(docid, c0body, c1subject, c2attachmentNames, c3author, c4recipients)
    folderLocations(id, folderURI, name)

``jsonAttributes`` holds per-message attributes keyed by ``attributeDefinitions``
ids: 54 fromMe, 55 toMe, 56 mailing-list, 58 star, 59 read, 60 repliedTo.

The same message can be indexed in several folders (Gmail's "All Mail" holds a
second copy of everything), so results are de-duplicated by Message-ID (tern: except
the Message-IDs ``set_split`` names, see ``_split``).

Every query is limited by the guardrails in ``guard.py``: allowed accounts and
folders, the history limit, and text cleaning/redaction.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

from tern.tbmcp import accounts, guard, snapshot, textutil

# Bounds applied to every caller-supplied size, whatever the tool docs say.
MAX_LIMIT = 100
MAX_BODY_CHARS = 200_000
PREVIEW_CHARS = 600

# Folders whose messages were never sent or received.
UNSENT_FOLDERS = {"drafts", "templates", "unsent messages", "outbox"}

ATTR_FROM_ME, ATTR_TO_ME, ATTR_LIST = "54", "55", "56"
ATTR_STAR, ATTR_READ, ATTR_REPLIED = "58", "59", "60"

_BASE_COLS = """
    m.id              AS message_id,
    m.conversationID  AS conversation_id,
    m.date            AS date_us,
    m.headerMessageID AS header_message_id,
    m.jsonAttributes  AS json_attrs,
    fl.name           AS folder,
    fl.folderURI      AS folder_uri,
    mt.c1subject      AS subject,
    mt.c3author       AS author,
    mt.c4recipients   AS recipients,
    mt.c2attachmentNames AS attachments,
    mt.c0body         AS body
"""

_FROM = """
    FROM messages m
    JOIN messagesText_content mt ON mt.docid = m.id
    LEFT JOIN folderLocations fl ON fl.id = m.folderID
"""

# One row per Message-ID (the bare columns come from the MIN(m.id) row). tern change: except
# for a Message-ID ``tern_split`` names (``set_split``), whose rows are listed one by one.
_DEDUP = (
    "GROUP BY CASE WHEN tern_split(m.headerMessageID) THEN 'id:' || m.id"
    " ELSE COALESCE(m.headerMessageID, 'id:' || m.id) END"
)
_PICK = ", MIN(m.id) AS _pick"

# tern change (plan 08c): GLODA keeps one row per folder copy, and the de-duplication above
# would show only one of several DIFFERENT messages sharing a Message-ID (a spoofed copy can
# hide the genuine one). tern knows which Message-IDs those are (its index marks them
# conflicted, #5) and installs a predicate over GLODA's headerMessageID; their rows are not
# collapsed. None: every Message-ID collapses (upstream's behaviour).
_split: Callable[[str], bool] | None = None


def set_split(predicate: Callable[[str], bool] | None) -> None:
    """Install the predicate naming the Message-IDs whose GLODA rows are listed one by one."""
    global _split
    _split = predicate


def _tern_split(header: str | None) -> int:
    """SQL ``tern_split(headerMessageID)``: 1 when the installed predicate names it."""
    return int(header is not None and _split is not None and _split(header))


def _now_us() -> int:
    return int(time.time() * 1_000_000)


def _con() -> sqlite3.Connection:
    snapshot.ensure_fresh()
    con = sqlite3.connect(f"file:{snapshot.snapshot_path()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.create_function("tern_split", 1, _tern_split)
    return con


def _fetchall(sql: str, params: list[Any] | tuple = ()) -> list[sqlite3.Row]:
    with closing(_con()) as con:
        return con.execute(sql, params).fetchall()


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def _like(s: str) -> str:
    """``%s%`` with LIKE wildcards in ``s`` escaped (use with ``ESCAPE '\\'``)."""
    s = s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{s}%"


def _us_to_iso(date_us: int | None) -> str | None:
    """GLODA stores dates as microseconds since the Unix epoch; shown in your timezone."""
    if not date_us:
        return None
    return (
        datetime.fromtimestamp(date_us / 1_000_000, tz=UTC)
        .astimezone(guard.user_timezone())
        .isoformat()
    )


def _parse_date(s: str) -> tuple[datetime, bool]:
    """Parse ``YYYY-MM-DD`` (midnight in your timezone) or a full ISO timestamp.

    Returns the datetime and whether the input was a bare date. A timestamp
    without an offset is taken in your timezone too.
    """
    s = s.strip()
    date_only = len(s) == 10
    dt = datetime.fromisoformat(s if not date_only else s + "T00:00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=guard.user_timezone())
    return dt, date_only


def _to_us(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000)


def _date_bounds(where: list[str], params: list[Any], since: str | None, until: str | None) -> None:
    if since:
        where.append("m.date >= ?")
        params.append(_to_us(_parse_date(since)[0]))
    if until:
        dt, date_only = _parse_date(until)
        if date_only:
            where.append("m.date < ?")
            params.append(_to_us(dt + timedelta(days=1)))
        else:
            where.append("m.date <= ?")
            params.append(_to_us(dt))


# --- folder URI -> account -------------------------------------------------------


def _uri_user_host(uri: str | None) -> tuple[str, str] | None:
    if not uri:
        return None
    parsed = urllib.parse.urlparse(uri)
    if parsed.username is None:
        return None
    user = urllib.parse.unquote(parsed.username)
    host = urllib.parse.unquote(parsed.hostname or "")
    return user, host


def _folder_identities(uri: str | None, lookup: dict) -> tuple[str | None, set[str]]:
    """Return (display account, every name the account answers to) for a folder.

    The display account is the account's first identity email when prefs.js
    links one, otherwise the server username from the URI.
    """
    uh = _uri_user_host(uri)
    if uh is None:
        return None, set()
    user, host = uh
    emails = lookup.get((user.lower(), host.lower()), [])
    names = {user.lower(), *(e.lower() for e in emails)}
    return (emails[0] if emails else user), names


def _account_from_uri(uri: str | None) -> str | None:
    """Account for a folder URI, e.g. ``imap://alice%40example.com@host/INBOX``."""
    return _folder_identities(uri, accounts.server_emails())[0]


def _allowed_folders() -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """(allowed folder rows, all folder rows) under the account/folder guardrails."""
    lookup = accounts.server_emails()
    rows = _fetchall("SELECT id, name, folderURI FROM folderLocations")
    allowed = [
        r
        for r in rows
        if guard.folder_allowed(r["name"], r["folderURI"])
        and guard.account_allowed(_folder_identities(r["folderURI"], lookup)[1])
    ]
    return allowed, rows


def _scope(
    where: list[str], params: list[Any], account: str | None = None, exclude_unsent: bool = False
) -> bool:
    """Add guardrail (and optional account) conditions. False = nothing can match."""
    allowed, rows = _allowed_folders()
    if account:
        lookup = accounts.server_emails()
        want = account.strip().lower()
        allowed = [r for r in allowed if want in _folder_identities(r["folderURI"], lookup)[1]]
    if exclude_unsent:
        allowed = [r for r in allowed if (r["name"] or "").lower() not in UNSENT_FOLDERS]
    if len(allowed) < len(rows) or account or exclude_unsent:
        if not allowed:
            return False
        where.append(f"m.folderID IN ({','.join('?' * len(allowed))})")
        params += [r["id"] for r in allowed]
    cutoff = guard.max_age_cutoff_us()
    if cutoff is not None:
        where.append("m.date >= ?")
        params.append(cutoff)
    return True


# --- rows ------------------------------------------------------------------------


def _attrs(raw: str | None) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _flags(r: sqlite3.Row, mine: set[str]) -> dict[str, Any]:
    a = _attrs(r["json_attrs"])
    author = textutil.emails_in(r["author"])
    rcpts = textutil.emails_in(r["recipients"])
    return {
        "read": bool(a[ATTR_READ]) if ATTR_READ in a else None,
        "starred": bool(a.get(ATTR_STAR, False)),
        "replied": bool(a.get(ATTR_REPLIED, False)),
        "from_me": ATTR_FROM_ME in a or bool(author and author[0] in mine),
        "to_me": ATTR_TO_ME in a or any(x in mine for x in rcpts),
        "mailing_list": ATTR_LIST in a,
    }


def _row_to_dict(
    r: sqlite3.Row, lookup: dict, mine: set[str], *, body_chars: int, strip: bool = False
) -> dict[str, Any]:
    body = r["body"] or ""
    item: dict[str, Any] = {
        "message_id": r["message_id"],
        "conversation_id": r["conversation_id"],
        "date": _us_to_iso(r["date_us"]),
        "header_message_id": r["header_message_id"],
        "folder": r["folder"],
        "account": _folder_identities(r["folder_uri"], lookup)[0],
        "subject": r["subject"] or "",
        "author": r["author"] or "",
        "recipients": r["recipients"] or "",
        "attachments": r["attachments"] or "",
        **_flags(r, mine),
    }
    if strip:
        body, removed = textutil.strip_quotes(body)
        item["quoted_text_removed"] = removed
    lengths = guard.protect(
        item,
        ("subject", "author", "recipients", "attachments", "body_preview"),
        full_texts={"body_preview": body},
        limits={"body_preview": body_chars},
    )
    item.setdefault("body_preview", "")
    item["attachments"] = [a for a in item["attachments"].split("\n") if a.strip()]
    item["body_length"] = lengths["body_preview"]
    item["truncated"] = lengths["body_preview"] > body_chars
    return item


def _rows(
    sql: str, params: list[Any], body_chars: int = PREVIEW_CHARS, strip: bool = False
) -> list[dict]:
    lookup, mine = accounts.server_emails(), accounts.my_addresses()
    return [
        _row_to_dict(r, lookup, mine, body_chars=body_chars, strip=strip)
        for r in _fetchall(sql, params)
    ]


def _page(items: list[dict], limit: int, offset: int, key: str = "messages") -> dict:
    more = len(items) > limit
    return {key: items[:limit], "next_offset": offset + limit if more else None}


# --- queries ---------------------------------------------------------------------


def search(
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    from_addr: str | None = None,
    to_addr: str | None = None,
    account: str | None = None,
    folder: str | None = None,
    unread: bool | None = None,
    starred: bool | None = None,
    has_attachment: bool | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Search messages by content, headers, date, account, folder or status.

    ``query`` matches subject + body + attachment names as a literal substring.
    ``since``/``until`` are inclusive; a bare ``YYYY-MM-DD`` is a day in your
    timezone. Results are newest-first, one per Message-ID, paged with
    ``offset``/``next_offset``.
    """
    limit, offset = _clamp(limit, 1, MAX_LIMIT), max(0, int(offset))
    where = ["m.deleted = 0"]
    params: list[Any] = []
    if not _scope(where, params, account=account):
        return _page([], limit, offset)

    if query:
        like = _like(query)
        where.append(
            "(mt.c0body LIKE ? ESCAPE '\\' OR mt.c1subject LIKE ? ESCAPE '\\'"
            " OR mt.c2attachmentNames LIKE ? ESCAPE '\\')"
        )
        params += [like, like, like]
    if from_addr:
        where.append("mt.c3author LIKE ? ESCAPE '\\'")
        params.append(_like(from_addr))
    if to_addr:
        where.append("mt.c4recipients LIKE ? ESCAPE '\\'")
        params.append(_like(to_addr))
    if folder:
        where.append("fl.name LIKE ? ESCAPE '\\'")
        params.append(_like(folder))
    if unread is not None:
        where.append(f"COALESCE(json_extract(m.jsonAttributes, '$.\"{ATTR_READ}\"'), 0) = ?")
        params.append(0 if unread else 1)
    if starred is not None:
        where.append(f"COALESCE(json_extract(m.jsonAttributes, '$.\"{ATTR_STAR}\"'), 0) = ?")
        params.append(1 if starred else 0)
    if has_attachment is not None:
        where.append(
            "COALESCE(mt.c2attachmentNames, '') " + ("!= ''" if has_attachment else "= ''")
        )
    _date_bounds(where, params, since, until)

    sql = (
        f"SELECT {_BASE_COLS} {_PICK} {_FROM} WHERE {' AND '.join(where)} {_DEDUP} "
        f"ORDER BY m.date DESC LIMIT ? OFFSET ?"
    )
    return _page(_rows(sql, [*params, limit + 1, offset]), limit, offset)


def recent_by_address(email_or_domain: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Messages (incoming and outgoing) involving an address or domain."""
    limit, offset = _clamp(limit, 1, MAX_LIMIT), max(0, int(offset))
    where = [
        "m.deleted = 0",
        "(mt.c3author LIKE ? ESCAPE '\\' OR mt.c4recipients LIKE ? ESCAPE '\\')",
    ]
    like = _like(email_or_domain)
    params: list[Any] = [like, like]
    if not _scope(where, params):
        return _page([], limit, offset)
    sql = (
        f"SELECT {_BASE_COLS} {_PICK} {_FROM} WHERE {' AND '.join(where)} {_DEDUP} "
        f"ORDER BY m.date DESC LIMIT ? OFFSET ?"
    )
    return _page(_rows(sql, [*params, limit + 1, offset]), limit, offset)


def get_message(
    message_id: int, body_chars: int = 8_000, strip_quotes: bool = False
) -> dict | None:
    """One non-deleted message by id, with its body up to ``body_chars``."""
    where, params = ["m.id = ?", "m.deleted = 0"], [int(message_id)]
    if not _scope(where, params):
        return None
    sql = f"SELECT {_BASE_COLS} {_FROM} WHERE {' AND '.join(where)}"
    rows = _rows(sql, params, body_chars=_clamp(body_chars, 0, MAX_BODY_CHARS), strip=strip_quotes)
    return rows[0] if rows else None


def thread(conversation_id: int, body_chars: int = 1_500, strip_quotes: bool = True) -> list[dict]:
    """All messages in a conversation, oldest first, quoted text stripped by default."""
    where, params = ["m.deleted = 0", "m.conversationID = ?"], [int(conversation_id)]
    if not _scope(where, params):
        return []
    sql = (
        f"SELECT {_BASE_COLS} {_PICK} {_FROM} WHERE {' AND '.join(where)} {_DEDUP} "
        f"ORDER BY m.date ASC"
    )
    return _rows(sql, params, body_chars=_clamp(body_chars, 0, MAX_BODY_CHARS), strip=strip_quotes)


def folders() -> list[dict]:
    """Folders you allow, with inferred account and message count."""
    allowed, _ = _allowed_folders()
    if not allowed:
        return []
    ids = [r["id"] for r in allowed]
    where, params = [f"fl.id IN ({','.join('?' * len(ids))})"], list(ids)
    cutoff = guard.max_age_cutoff_us()
    age = " AND m.date >= ?" if cutoff is not None else ""
    sql = f"""
    SELECT fl.id, fl.name, fl.folderURI, COUNT(m.id) AS count
    FROM folderLocations fl
    LEFT JOIN messages m ON m.folderID = fl.id AND m.deleted = 0{age}
    WHERE {" AND ".join(where)}
    GROUP BY fl.id
    ORDER BY count DESC
    """
    lookup = accounts.server_emails()
    rows = _fetchall(sql, ([cutoff] if cutoff is not None else []) + params)
    return [
        {
            "name": r["name"],
            "uri": r["folderURI"],
            "account": _folder_identities(r["folderURI"], lookup)[0],
            "count": r["count"],
        }
        for r in rows
    ]


def stats() -> dict:
    """Message count (one per Message-ID) and date range within your scope."""
    where, params = ["m.deleted = 0"], []
    if not _scope(where, params):
        return {"messages": 0, "oldest": None, "newest": None}
    (row,) = _fetchall(
        "SELECT COUNT(DISTINCT COALESCE(m.headerMessageID, 'id:' || m.id)), MIN(m.date),"
        f" MAX(m.date) FROM messages m WHERE {' AND '.join(where)}",
        params,
    )
    total, oldest, newest = row
    return {"messages": total, "oldest": _us_to_iso(oldest), "newest": _us_to_iso(newest)}


# --- attachments -----------------------------------------------------------------


def attachments(
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    from_addr: str | None = None,
    account: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Attachments by file name, newest first; ``limit`` counts messages."""
    limit, offset = _clamp(limit, 1, MAX_LIMIT), max(0, int(offset))
    where = ["m.deleted = 0", "COALESCE(mt.c2attachmentNames, '') != ''"]
    params: list[Any] = []
    if not _scope(where, params, account=account):
        return _page([], limit, offset, key="attachments")
    if query:
        where.append("mt.c2attachmentNames LIKE ? ESCAPE '\\'")
        params.append(_like(query))
    if from_addr:
        where.append("mt.c3author LIKE ? ESCAPE '\\'")
        params.append(_like(from_addr))
    _date_bounds(where, params, since, until)
    sql = (
        f"SELECT {_BASE_COLS} {_PICK} {_FROM} WHERE {' AND '.join(where)} {_DEDUP} "
        f"ORDER BY m.date DESC LIMIT ? OFFSET ?"
    )
    msgs = _rows(sql, [*params, limit + 1, offset], body_chars=0)
    page = _page(msgs, limit, offset, key="attachments")
    want = query.lower() if query else None
    page["attachments"] = [
        {
            "name": name,
            "type": name.rsplit(".", 1)[-1].lower() if "." in name else None,
            "message_id": m["message_id"],
            "header_message_id": m["header_message_id"],
            "conversation_id": m["conversation_id"],
            "date": m["date"],
            "from": m["author"],
            "subject": m["subject"],
            "folder": m["folder"],
            "account": m["account"],
        }
        for m in page["attachments"]
        for name in m["attachments"]
        if want is None or want in name.lower()
    ]
    return page


# --- follow-ups ------------------------------------------------------------------


def _conversations(within_days: int) -> dict[int, list[dict]]:
    """Recent sent/received messages grouped by conversation, oldest first."""
    where = ["m.deleted = 0", "m.date >= ?"]
    params: list[Any] = [_now_us() - int(within_days) * 86_400_000_000]
    if not _scope(where, params, exclude_unsent=True):
        return {}
    sql = f"""
    SELECT m.id AS message_id, m.conversationID AS conversation_id, m.date AS date_us,
           m.headerMessageID AS header_message_id, m.jsonAttributes AS json_attrs,
           fl.name AS folder, mt.c1subject AS subject, mt.c3author AS author,
           mt.c4recipients AS recipients, substr(mt.c0body, 1, 2000) AS body {_PICK}
    {_FROM} WHERE {" AND ".join(where)} {_DEDUP} ORDER BY m.date ASC
    """
    mine = accounts.my_addresses()
    convs: dict[int, list[dict]] = {}
    for r in _fetchall(sql, params):
        convs.setdefault(r["conversation_id"], []).append({**dict(r), **_flags(r, mine)})
    return convs


def _summary(m: dict, now_us: int, extra: dict) -> dict:
    body, _ = textutil.strip_quotes(m["body"] or "")
    item = {
        "conversation_id": m["conversation_id"],
        "message_id": m["message_id"],
        "header_message_id": m["header_message_id"],
        "subject": m["subject"] or "",
        **extra,
        "date": _us_to_iso(m["date_us"]),
        "days_waiting": round((now_us - m["date_us"]) / 86_400_000_000, 1),
        "preview": body,
    }
    guard.protect(item, ("subject", "preview"), limits={"preview": 300})
    return item


def awaiting_reply(days: int = 3, within_days: int = 60, limit: int = 20) -> list[dict]:
    """Threads where you sent the last message at least ``days`` ago and nobody replied."""
    now, limit = _now_us(), _clamp(limit, 1, MAX_LIMIT)
    mine = accounts.my_addresses()
    cutoff = now - int(days) * 86_400_000_000
    out = []
    for msgs in _conversations(within_days).values():
        last = msgs[-1]
        if not last["from_me"] or last["date_us"] > cutoff:
            continue
        to = [e for e in textutil.emails_in(last["recipients"]) if e not in mine]
        if not to:
            continue  # a note to yourself
        out.append(_summary(last, now, {"to": to}))
    out.sort(key=lambda x: -x["days_waiting"])
    return out[:limit]


def unanswered(
    days: int = 1, within_days: int = 30, limit: int = 20, include_automated: bool = False
) -> list[dict]:
    """Messages to you from people, at least ``days`` old, that you haven't answered.

    Skips mailing lists and no-reply/role senders unless ``include_automated``.
    A thread counts as answered when you sent a later message in it or the
    message carries Thunderbird's "replied" flag.
    """
    now, limit = _now_us(), _clamp(limit, 1, MAX_LIMIT)
    cutoff = now - int(days) * 86_400_000_000
    out = []
    for msgs in _conversations(within_days).values():
        last = msgs[-1]
        if last["from_me"] or last["replied"] or not last["to_me"]:
            continue
        if last["date_us"] > cutoff:
            continue
        if not include_automated and textutil.is_automated(last["author"], last["mailing_list"]):
            continue
        out.append(_summary(last, now, {"from": last["author"] or "", "read": last["read"]}))
        guard.protect(out[-1], ("from",))
    out.sort(key=lambda x: x["days_waiting"])
    return out[:limit]
