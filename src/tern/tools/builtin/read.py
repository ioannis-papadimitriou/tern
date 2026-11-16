"""The R0/R1 built-in tools (plan 06a step 5, architecture §5).

Names and inputs follow the S3 stubs the tool-calling model was measured on
(``spikes/s3/stubs.py``; 08a's ``toolcalls-v1`` mirrors them): ``search_messages``,
``get_message``, ``get_thread``, ``list_folders``, ``find_contacts``, ``index_status`` (R0, index
only, works with Thunderbird closed), ``open_in_thunderbird`` and ``read_attachment`` (R1, the
two that talk to the extension: ``read_attachment`` fetches the message's raw bytes and extracts
an attachment's text with :mod:`tern.tools.attachments`). Every output is an object model.

Policies (plan 06a step 5 and amendments 4-6):

- **Search** goes through ``ctx.search`` only (never FTS or vector SQL here), with
  ``include_conflicted=False``. ``EmptyQuery`` and a ``SearchFilters`` the service refuses are
  ``invalid_params``; ``NoEmbeddingModel``, ``ModelMismatch``, ``EmbedderUnavailable`` and
  ``EmbedderError`` are ``unavailable`` (never a silent FTS-only answer); ``SearchStateError``,
  ``VecStateError`` and ``sqlite3.DatabaseError`` are ``internal``. No bare ``ValueError`` is
  mapped to ``invalid_params``. The output carries ``mode`` and ``embed_pending``.
- **Conflicted mids** (#5, T14): dropped from search and thread results; ``get_message`` and
  ``open_in_thunderbird`` refuse them (``mid_conflicted``), as does ``get_thread`` for its anchor
  (the stubs' ``_visible``); ``find_contacts`` counts no conflicted message. A mid that is not
  indexed is ``unknown_mid``.
- **NULLs stay explicit**: ``date``, ``from`` and ``subject`` are nullable; nothing is invented.
  A body whose content was cleared (refill pending) is ``body: null, body_status: unavailable``;
  one that failed to parse is ``body: null, body_status: unparseable``.
- **Threads** compare ``thread_id`` by equality only (``Store.thread``); a thread_id is never
  looked up as a message (it may be the mid of a message that is not indexed).
- **Reads**: a tool that reads more than one statement does it in one read transaction with no
  await inside; no tool holds a transaction across an await.
- ``open_in_thunderbird``'s ``tb_id`` exists only inside the call (#5): never returned, logged
  or audited. It stays R1 until the display check (``docs/integration-checklist.md``, plan 06a)
  shows no side effect; otherwise it becomes W2 (tool metadata, not a contract change).

Folder and account names may appear in outputs (tool output is for the model) but are never
logged; these tools log nothing at INFO.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import ConfigDict, Field

from tern._model import BoundaryModel
from tern.bridge.protocol import (
    BridgeError,
    FindParams,
    FoundList,
    FoundLocation,
    Mid,
    Raw,
    TbIdParams,
)
from tern.index.chunks import fold
from tern.index.store import MessageRow, Store
from tern.search import vec
from tern.search.embedder import EmbedderError, EmbedderUnavailable
from tern.search.service import (
    EmptyQuery,
    Hit,
    ModelMismatch,
    NoEmbeddingModel,
    SearchFilters,
    SearchStateError,
)
from tern.tools import attachments
from tern.tools.context import ToolContext
from tern.tools.contract import Effect, Tier, ToolError, ToolSpec, tool
from tern.tools.ids import normalize_ref, prefix_range

#: Characters of body text a ``get_thread`` member's snippet keeps (the stubs' value).
THREAD_SNIPPET_CHARS = 400
#: The ``ingest_state.status`` prefix of a failed job (``tern.index.service.ERROR_PREFIX``).
_JOB_ERROR_PREFIX = "error:"

BodyStatus = Literal["ok", "unavailable", "unparseable"]


# ---- shared helpers -----------------------------------------------------------------------------


class _Out(BoundaryModel):
    """Output models: ``from_`` is ``from`` on the wire (schema and dump) and set by name here."""

    model_config = ConfigDict(
        extra="forbid", hide_input_in_errors=True, validate_by_name=True, serialize_by_alias=True
    )


def _from_field() -> Any:
    return Field(
        validation_alias="from",
        serialization_alias="from",
        description='"Name <addr>", else whichever is known, else null',
    )


def sender(name: str | None, addr: str | None) -> str | None:
    """``"Name <addr>"``, else the name or the address, else None (``cli._sender``'s rule)."""
    if name and addr:
        return f"{name} <{addr}>"
    return name or addr or None


def _iso(epoch: int | None) -> str | None:
    """``date_utc`` as ``YYYY-MM-DDTHH:MM:SSZ``; None stays None (no invented date)."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, ValueError, OSError):
        # ingest stores NULL for such dates (mime.utc_epoch): an index-state error, not a
        # caller mistake
        raise ToolError("internal", "a stored date is out of range", reason="index_state") from None


def _iso_dt(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def _snapshot(store: Store) -> Iterator[sqlite3.Connection]:
    """One read transaction on the (read-only) index connection; no await may run inside."""
    conn = store.connection
    if conn.in_transaction:
        raise RuntimeError("tool read started inside an open transaction")
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        # read-only: ROLLBACK, and only on this path, so a failing COMMIT cannot mask the error
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if conn.in_transaction:
        conn.execute("COMMIT")


@contextmanager
def _index_errors() -> Iterator[None]:
    """Index-state and database errors as ``internal`` (reason code only, never the text)."""
    try:
        yield
    except (SearchStateError, vec.VecStateError) as exc:
        raise ToolError(
            "internal",
            f"the index is in an unexpected state ({type(exc).__name__})",
            reason="index_state",
        ) from None
    except sqlite3.DatabaseError as exc:
        raise ToolError(
            "internal", f"index read failed ({type(exc).__name__})", reason="index_error"
        ) from None


def _visible(row: MessageRow | None) -> MessageRow:
    """``row``, or ``unknown_mid`` / ``mid_conflicted``."""
    if row is None:
        raise ToolError("unknown_mid", "no indexed message has this mid", reason="not_indexed")
    if row.conflicted:
        raise ToolError(
            "mid_conflicted",
            "the copies of this message disagree; it cannot be used",
            reason="conflicted",
        )
    return row


def _body(row: MessageRow) -> tuple[str | None, BodyStatus]:
    """The stored main text and its status: cleared content (refill pending) or a row without
    body text is ``unavailable``, a failed parse ``unparseable``."""
    if row.content_hash is None:
        return None, "unavailable"
    if row.parse_status == "failed":
        return None, "unparseable"
    if row.body_text is None:
        return None, "unavailable"
    return row.body_text, "ok"


# ---- search_messages ----------------------------------------------------------------------------


class SearchIn(BoundaryModel):
    """Search the user's mail. Returns the best matches: mid, date, sender, subject, snippet."""

    # As the S3 stubs: ``from`` on the wire, ``from_`` accepted too.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, validate_by_name=True)

    query: str = Field(min_length=1, description="words to look for")
    folder: str | None = Field(default=None, description="folder_id from list_folders")
    from_: str | None = Field(default=None, alias="from", description="sender name or address")
    after: str | None = Field(default=None, description="ISO date, inclusive")
    before: str | None = Field(default=None, description="ISO date, exclusive")
    has_attachment: bool | None = None
    limit: int = Field(default=10, ge=1, le=50)


class SearchHitOut(_Out):
    mid: str
    date: str | None
    from_: str | None = _from_field()
    subject: str | None
    snippet: str
    score: float
    #: folder_ids of the message's locations.
    folders: list[str]


class SearchOut(_Out):
    hits: list[SearchHitOut]
    #: ``hybrid`` or an explicit ``fts`` service (never switched silently).
    mode: Literal["hybrid", "fts"]
    #: Chunks without a vector: vector recall is partial while this is > 0.
    embed_pending: int


def _when(value: str | None, field: str) -> datetime | None:
    """An ISO date or datetime; naive values are UTC, offsets are honoured (the stubs' rule)."""
    if value is None:
        return None
    try:
        d = datetime.fromisoformat(value)
        # inside the try: an offset that pushes the date past year 1 or 9999 is the caller's
        # mistake too (OverflowError), not a tool failure
        return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ToolError(
            "invalid_params", f"{field}: not an ISO date (e.g. 2026-04-01)", reason="bad_date"
        ) from None


def _hit(h: Hit) -> SearchHitOut:
    return SearchHitOut(
        mid=h.mid,
        date=_iso_dt(h.date),
        from_=sender(h.from_name, h.from_addr),
        subject=h.subject,
        snippet=h.snippet,
        score=h.score,
        folders=list(h.folder_ids),
    )


@tool(name="search_messages", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=True)
async def search_messages(ctx: ToolContext, inp: SearchIn) -> SearchOut:
    """Search the user's mail. Returns the best matches: mid, date, sender, subject, snippet."""
    after, before = _when(inp.after, "after"), _when(inp.before, "before")
    try:
        filters = SearchFilters(
            folder_id=inp.folder,
            from_=inp.from_,
            after=after,
            before=before,
            has_attachment=inp.has_attachment,
            include_conflicted=False,
        )
    except ValueError:
        # only the constructor is covered: its checks name fields and rules, no values
        raise ToolError(
            "invalid_params", "a filter is empty or blank (folder, from)", reason="bad_filter"
        ) from None
    try:
        with _index_errors():
            result = await ctx.search.search(inp.query, filters, inp.limit)
    except EmptyQuery:
        raise ToolError(
            "invalid_params", "query: no word to search for", reason="empty_query"
        ) from None
    except (NoEmbeddingModel, ModelMismatch) as exc:
        reason = "no_embedding_model" if isinstance(exc, NoEmbeddingModel) else "model_mismatch"
        raise ToolError(
            "unavailable", f"hybrid search cannot run ({reason})", reason=reason
        ) from None
    except (EmbedderUnavailable, EmbedderError) as exc:
        reason = (
            "embedder_unavailable" if isinstance(exc, EmbedderUnavailable) else "embedder_error"
        )
        raise ToolError(
            "unavailable", f"the embedding model failed ({reason})", reason=reason
        ) from None
    return SearchOut(
        hits=[_hit(h) for h in result.hits],
        mode=result.mode,
        embed_pending=result.embed_pending,
    )


# ---- get_message --------------------------------------------------------------------------------


class GetMessageIn(BoundaryModel):
    """Read one message: headers, body text and attachment names."""

    mid: Mid
    max_chars: int = Field(default=4000, ge=1, le=20000)


class AttachmentOut(_Out):
    name: str | None
    mime: str | None
    size: int | None


class MessageOut(_Out):
    mid: str
    date: str | None
    from_: str | None = _from_field()
    to: list[str]
    cc: list[str]
    subject: str | None
    #: The main text (quotes and signature removed at ingest), cut to ``max_chars``; null when
    #: ``body_status`` is not ``ok``.
    body: str | None
    body_status: BodyStatus
    #: ``max_chars`` cut the body.
    truncated: bool
    attachments: list[AttachmentOut]
    #: folder_ids of the message's locations.
    folders: list[str]
    thread_id: str | None


@tool(name="get_message", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=True)
async def get_message(ctx: ToolContext, inp: GetMessageIn) -> MessageOut:
    """Read one message: headers, body text and attachment names."""
    with _index_errors(), _snapshot(ctx.index):
        row = _visible(ctx.index.get_message(inp.mid))
        folders = [loc.folder_id for loc in ctx.index.locations(inp.mid)]
    body, status = _body(row)
    truncated = body is not None and len(body) > inp.max_chars
    return MessageOut(
        mid=row.mid,
        date=_iso(row.date_utc),
        from_=sender(row.from_name, row.from_addr),
        to=list(row.to_addrs),
        cc=list(row.cc_addrs),
        subject=row.subject,
        body=None if body is None else body[: inp.max_chars],
        body_status=status,
        truncated=truncated,
        attachments=[
            AttachmentOut(name=a.filename, mime=a.mime, size=a.size) for a in row.attachments
        ],
        folders=folders,
        thread_id=row.thread_id,
    )


# ---- get_thread ---------------------------------------------------------------------------------


class GetThreadIn(BoundaryModel):
    """List the messages of the thread that contains this message, oldest first."""

    mid: Mid


class ThreadMessageOut(_Out):
    mid: str
    date: str | None
    from_: str | None = _from_field()
    subject: str | None
    #: The start of the body text; null when ``body_status`` is not ``ok``.
    snippet: str | None
    body_status: BodyStatus


class ThreadOut(_Out):
    #: Oldest first, undated last; conflicted members are left out.
    messages: list[ThreadMessageOut]


def _thread_member(row: MessageRow) -> ThreadMessageOut:
    body, status = _body(row)
    text = None if body is None else " ".join(body.split())[:THREAD_SNIPPET_CHARS]
    return ThreadMessageOut(
        mid=row.mid,
        date=_iso(row.date_utc),
        from_=sender(row.from_name, row.from_addr),
        subject=row.subject,
        snippet=text,
        body_status=status,
    )


@tool(name="get_thread", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=True)
async def get_thread(ctx: ToolContext, inp: GetThreadIn) -> ThreadOut:
    """List the messages of the thread that contains this message, oldest first."""
    with _index_errors(), _snapshot(ctx.index):
        _visible(ctx.index.get_message(inp.mid))
        rows: list[MessageRow] = []
        members = ctx.index.thread(inp.mid)
        if inp.mid not in members:
            # a NULL thread_id matches nothing: an empty list would hide the anchor itself
            raise ToolError("internal", "the message has no thread", reason="index_state")
        for mid in members:
            row = ctx.index.get_message(mid)
            if row is None:  # same snapshot: Store.thread only lists stored messages
                raise ToolError("internal", "a thread member has no row", reason="index_state")
            if not row.conflicted:
                rows.append(row)
    return ThreadOut(messages=[_thread_member(r) for r in rows])


# ---- list_folders -------------------------------------------------------------------------------


class ListFoldersIn(BoundaryModel):
    """List all folders with their folder_id, account, path, unread and total counts."""


class FolderOut(_Out):
    folder_id: str
    account_id: str
    #: The account's name; null when the index has none.
    account: str | None
    path: str
    special_use: list[str]
    #: Indexed locations in this folder not marked read.
    unread: int
    #: Indexed locations in this folder.
    total: int


class FoldersOut(_Out):
    folders: list[FolderOut]


@tool(name="list_folders", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def list_folders(ctx: ToolContext, inp: ListFoldersIn) -> FoldersOut:
    """List all folders with their folder_id, account, path, unread and total counts."""
    with _index_errors(), _snapshot(ctx.index) as conn:
        folders = ctx.index.folders()
        names: dict[str, str | None] = {
            r[0]: r[1] for r in conn.execute("SELECT account_id, name FROM accounts")
        }
        counts: dict[str, tuple[int, int]] = {
            r[0]: (int(r[1]), int(r[2]))
            for r in conn.execute(
                "SELECT folder_id, count(*), sum(CASE WHEN coalesce(read, 0) = 0 THEN 1 ELSE 0 "
                "END) FROM locations GROUP BY folder_id"
            )
        }
    return FoldersOut(
        folders=[
            FolderOut(
                folder_id=f.folder_id,
                account_id=f.account_id,
                account=names.get(f.account_id),
                path=f.path,
                special_use=list(f.special_use),
                unread=counts.get(f.folder_id, (0, 0))[1],
                total=counts.get(f.folder_id, (0, 0))[0],
            )
            for f in folders
        ]
    )


# ---- find_contacts ------------------------------------------------------------------------------


class FindContactsIn(BoundaryModel):
    """Find addresses seen in the user's mail matching a name or address fragment."""

    query: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=50)


class ContactOut(_Out):
    address: str
    #: The display name most often seen with this address in From (null when none).
    name: str | None
    #: Non-conflicted messages that carry this address in From, To or Cc.
    count: int


class ContactsOut(_Out):
    contacts: list[ContactOut]


def _folded(text: str) -> str:
    return fold(text).casefold()


@tool(name="find_contacts", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=True)
async def find_contacts(ctx: ToolContext, inp: FindContactsIn) -> ContactsOut:
    """Find addresses seen in the user's mail matching a name or address fragment."""
    needles = _folded(inp.query).split()
    if not needles:
        raise ToolError("invalid_params", "query: nothing to match", reason="empty_query")
    counts: Counter[str] = Counter()
    names: defaultdict[str, Counter[str]] = defaultdict(Counter)
    with _index_errors(), _snapshot(ctx.index) as conn:
        rows = conn.execute(
            "SELECT from_addr, from_name, to_addrs, cc_addrs FROM messages WHERE conflicted = 0"
        ).fetchall()
    for from_addr, from_name, to_json, cc_json in rows:
        seen: set[str] = set()
        if from_addr:
            addr = cast("str", from_addr).lower()
            seen.add(addr)
            if from_name:
                names[addr][cast("str", from_name)] += 1
        for text in (to_json, cc_json):
            if text is None:
                continue
            for a in cast("list[str]", json.loads(text)):
                if a:
                    seen.add(a.lower())
        counts.update(seen)

    def matches(addr: str) -> bool:
        hay = [_folded(addr), *(_folded(n) for n in names.get(addr, ()))]
        return all(any(n in h for h in hay) for n in needles)

    def best_name(addr: str) -> str | None:
        seen = names.get(addr)
        if not seen:
            return None
        return min(seen.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    found = sorted((a for a in counts if matches(a)), key=lambda a: (-counts[a], a))
    return ContactsOut(
        contacts=[
            ContactOut(address=a, name=best_name(a), count=counts[a]) for a in found[: inp.limit]
        ]
    )


# ---- index_status -------------------------------------------------------------------------------


class IndexStatusIn(BoundaryModel):
    """Report the index's state: message counts, embedding progress and failed jobs."""


class IndexStatusOut(_Out):
    schema_version: int
    messages: int
    locations: int
    same_folder_copies: int
    conflicted: int
    conflicts_unclassified: int
    x_mids: int
    parse_ok: int
    parse_partial: int
    parse_failed: int
    headers_only: int
    skipped_large_noid: int
    skipped_tb_error: int
    refill_pending: int
    #: The registered embedding model; null when none is (search is then FTS-only or refused).
    embed_model: str | None
    #: Chunks without a vector under the registered model (all chunks when none is).
    embed_pending: int
    #: The latest folder reconcile (``YYYY-MM-DDTHH:MM:SSZ``); null when none ran yet.
    last_reconcile: str | None
    #: Ingest scopes whose last run failed (``ingest_state`` status ``error:...``).
    jobs_failed: int


@tool(name="index_status", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def index_status(ctx: ToolContext, inp: IndexStatusIn) -> IndexStatusOut:
    """Report the index's state: message counts, embedding progress and failed jobs."""
    with _index_errors(), _snapshot(ctx.index) as conn:
        st = ctx.index.status()
        model = vec.current(conn)
        pending = vec.pending_count(conn)
        last = conn.execute("SELECT max(last_reconciled_at) FROM folders").fetchone()[0]
        failed = int(
            conn.execute(
                "SELECT count(*) FROM ingest_state WHERE substr(status, 1, ?) = ?",
                (len(_JOB_ERROR_PREFIX), _JOB_ERROR_PREFIX),
            ).fetchone()[0]
        )
    return IndexStatusOut(
        schema_version=st.schema_version,
        messages=st.messages,
        locations=st.locations,
        same_folder_copies=st.same_folder_copies,
        conflicted=st.conflicted,
        conflicts_unclassified=st.conflicts_unclassified,
        x_mids=st.x_mids,
        parse_ok=st.parse_ok,
        parse_partial=st.parse_partial,
        parse_failed=st.parse_failed,
        headers_only=st.headers_only,
        skipped_large_noid=st.skipped_large_noid,
        skipped_tb_error=st.skipped_tb_error,
        refill_pending=st.refill_pending,
        embed_model=None if model is None else model.model_id,
        embed_pending=pending,
        last_reconcile=_iso(last),
        jobs_failed=failed,
    )


# ---- open_in_thunderbird ------------------------------------------------------------------------


class OpenIn(BoundaryModel):
    """Show this message in Thunderbird (opens it in the user's mail window)."""

    mid: Mid


class OpenOut(_Out):
    opened: bool
    #: The folder_id of the copy that was opened.
    folder_id: str


def _pick(found: FoundList, indexed: set[str]) -> FoundLocation:
    """The found copy in the lowest folder_id the index also lists for the message."""
    candidates = [loc for loc in found.root if loc.folder_id in indexed]
    if not candidates:
        raise ToolError(
            "unknown_mid",
            "Thunderbird does not hold this message in an indexed folder",
            reason="not_in_thunderbird",
        )
    return min(candidates, key=lambda loc: (loc.folder_id, loc.tb_id))


async def _find(ctx: ToolContext, find: FindParams, indexed: set[str]) -> FoundLocation:
    found = await ctx.bridge.request("mail.messages.find", find)
    if not isinstance(found, FoundList):
        raise ToolError("internal", "unexpected bridge result", reason="bad_bridge_result")
    return _pick(found, indexed)


@tool(name="open_in_thunderbird", tier=Tier.R1, effects={Effect.READ_LIVE}, untrusted_output=False)
async def open_in_thunderbird(ctx: ToolContext, inp: OpenIn) -> OpenOut:
    """Show this message in Thunderbird (opens it in the user's mail window)."""
    with _index_errors(), _snapshot(ctx.index):
        row = _visible(ctx.index.get_message(inp.mid))
        indexed = {loc.folder_id for loc in ctx.index.locations(inp.mid)}
    if row.header_message_id is None:
        raise ToolError(
            "unknown_mid", "this message has no Message-ID to find it by", reason="no_message_id"
        )
    find = FindParams(header_message_id=row.header_message_id)
    loc = await _find(ctx, find, indexed)
    try:
        await ctx.bridge.request("mail.open", TbIdParams(tb_id=loc.tb_id))
    except BridgeError as exc:
        # A tb_id from find went stale (TB restarted, the message moved): find once more.
        if exc.name != "stale_id":
            raise
        stale = True
    else:
        stale = False
    if stale:
        loc = await _find(ctx, find, indexed)
        try:
            await ctx.bridge.request("mail.open", TbIdParams(tb_id=loc.tb_id))
        except BridgeError as exc:
            if exc.name != "stale_id":
                raise
            raise ToolError(
                "unavailable", "the message moved in Thunderbird again", reason="stale_id"
            ) from None
    return OpenOut(opened=True, folder_id=loc.folder_id)


# ---- read_attachment ----------------------------------------------------------------------------

#: Default and largest ``max_chars`` of ``read_attachment``.
ATTACHMENT_CHARS_DEFAULT = 12_000
ATTACHMENT_CHARS_MAX = 50_000


class ReadAttachmentIn(BoundaryModel):
    """Read the text of a message's attachment (PDF, Word .docx, Excel .xlsx, text, CSV, HTML or
    an attached e-mail)."""

    mid: str = Field(
        min_length=8,
        max_length=66,
        description="the message's mid as a tool showed it (short id or full mid)",
    )
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="the attachment's file name (or a unique part of it); omit when the "
        "message has only one attachment",
    )
    max_chars: int = Field(default=ATTACHMENT_CHARS_DEFAULT, ge=100, le=ATTACHMENT_CHARS_MAX)


class AttachmentInfo(_Out):
    name: str | None
    mime: str
    size: int


class ReadAttachmentOut(_Out):
    #: Every attachment of the message (so a wrong or missing name can be corrected).
    attachments: list[AttachmentInfo]
    #: The attachment read, or None when ``name`` matched none or several.
    name: str | None
    status: Literal["ok", "empty", "unsupported", "encrypted", "unreadable", "not_found"]
    text: str | None
    truncated: bool
    detail: str | None = None


def _resolve_mid(ctx: ToolContext, ref: str) -> str:
    """The one indexed mid a short id or full mid names; ``invalid_params`` when it names none
    or several (never a guess)."""
    norm = normalize_ref(ref)
    if norm is None:
        raise ToolError("invalid_params", "not a message id", reason="bad_mid")
    low, high = prefix_range(norm)
    rows = ctx.index.connection.execute(
        "SELECT mid FROM messages WHERE mid >= ? AND mid < ? ORDER BY mid LIMIT 2", (low, high)
    ).fetchall()
    if not rows:
        raise ToolError("unknown_mid", "no indexed message has this mid", reason="not_indexed")
    if len(rows) > 1:
        raise ToolError(
            "invalid_params", "this short id names more than one message", reason="ambiguous"
        )
    return str(rows[0][0])


async def _raw(ctx: ToolContext, find: FindParams, indexed: set[str]) -> bytes:
    """The raw bytes of the message, from one of its copies in Thunderbird (one retry when the
    tb_id went stale, as ``open_in_thunderbird``)."""
    for attempt in range(2):
        loc = await _find(ctx, find, indexed)
        try:
            raw = await ctx.bridge.request("mail.messages.raw", TbIdParams(tb_id=loc.tb_id))
        except BridgeError as exc:
            if exc.name != "stale_id" or attempt:
                if exc.name == "stale_id":
                    raise ToolError(
                        "unavailable", "the message moved in Thunderbird again", reason="stale_id"
                    ) from None
                raise
            continue
        if not isinstance(raw, Raw):
            raise ToolError("internal", "unexpected bridge result", reason="bad_bridge_result")
        return base64.b64decode(raw.raw_b64, validate=True)
    raise AssertionError("unreachable")  # the loop returns or raises


@tool(name="read_attachment", tier=Tier.R1, effects={Effect.READ_LIVE}, untrusted_output=True)
async def read_attachment(ctx: ToolContext, inp: ReadAttachmentIn) -> ReadAttachmentOut:
    """Read the text of a message's attachment (PDF, Word .docx, Excel .xlsx, text, CSV, HTML or
    an attached e-mail). Give the message's mid as a tool showed it and the attachment's file
    name; the output lists all of the message's attachments."""
    with _index_errors(), _snapshot(ctx.index):
        mid = _resolve_mid(ctx, inp.mid)
        row = _visible(ctx.index.get_message(mid))
        indexed = {loc.folder_id for loc in ctx.index.locations(mid)}
    if row.header_message_id is None:
        raise ToolError(
            "unknown_mid", "this message has no Message-ID to find it by", reason="no_message_id"
        )
    raw = await _raw(ctx, FindParams(header_message_id=row.header_message_id), indexed)
    parts = attachments.attachments(raw)
    listed = [AttachmentInfo(name=p.name, mime=p.mime, size=len(p.data)) for p in parts]
    part = attachments.pick(parts, inp.name)
    if part is None:
        why = (
            "the message has no attachments"
            if not parts
            else (
                "no attachment has this name" if inp.name else "the message has several attachments"
            )
        )
        return ReadAttachmentOut(
            attachments=listed,
            name=None,
            status="not_found",
            text=None,
            truncated=False,
            detail=f"{why}; give one of the listed names",
        )
    got = attachments.extract_text(part, inp.max_chars)
    return ReadAttachmentOut(
        attachments=listed,
        name=part.name,
        status=got.status,
        text=got.text,
        truncated=got.truncated,
        detail=got.detail,
    )


#: The built-in read tools, in architecture §5's order.
READ_TOOLS: tuple[ToolSpec, ...] = (
    search_messages,
    get_message,
    get_thread,
    list_folders,
    find_contacts,
    index_status,
    open_in_thunderbird,
    read_attachment,
)
