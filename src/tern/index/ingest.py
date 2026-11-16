"""Backfill and the per-item ingest path (plan 04 step 7; architecture §2, #16 S1, M8, #30).

:class:`Backfill` walks every indexed folder, one at a time, newest mail first, in date windows
of ``backfill_window_days`` (W), and writes each message copy in its own Store transaction.

Windows (TB endpoints are inclusive, #16 S1; pages come in folder order, not date order, A3):

- the first window is open-ended: ``from_date = now - W``, no ``to_date``; every item with
  ``tb_date >= from`` is kept, future-dated mail included (A4);
- each next window queries ``[from - W, from]`` and keeps ``from - W <= tb_date < from``
  (half-open, so an item exactly on a boundary belongs to the newer window only);
- a window is paged to its end (``next_cursor`` followed until absent, also across empty pages)
  before any of its items is fetched; a cursor that turns ``stale_id`` restarts the window's
  paging (at most :data:`MAX_WINDOW_RESTARTS` times, then :class:`IngestError`);
- the watermark (``ingest_state.watermark_date`` = the window's from, ``watermark_mids`` = the
  mids stored at exactly that second) advances only after the window's last item is committed,
  so a killed run resumes by re-paging the whole window it was in; TB cursors and ``tb_id``s
  are never persisted (#5). Re-ingesting a copy is idempotent;
- an empty window starts an open-ended probe (``to_date = from``, no ``from_date``) paged to
  completion: no item older than ``from`` → the folder is done (``status = 'done'``); otherwise
  the next window ends just above the newest probe item over **all** pages (max tb_date + 1 s);
- the ``headers_only_items`` and ``skipped_items`` rows of a window are written in the window's
  commit, replacing that window's rows (DELETE by folder_id and tb_date range, then INSERT), with
  ``ordinal`` = the 0-based index in page order among that table's rows at the same tb_date.

``tb_date`` is the PageItem date in whole epoch seconds (floored).

Per item (:class:`ItemIngester`, shared with EventIngest and Reconcile in step 8):

- ``headers_only`` → never fetched (getRaw would hit the network); a ``headers_only_items`` row
  (``mid = mid_of(hmid)``, NULL for ``md5:`` ids);
- ``size > RAW_MAX`` or ``raw`` → ``too_large`` → ``full``; no Message-ID there →
  ``skipped_items`` + ``skipped_large_noid``; otherwise ``raw``;
- ``tb_error`` on ``raw``/``full`` (TB failed to read this one message; plan 04 merge review M5)
  → a ``skipped_items`` row (``reason='tb_error'``), counted in ``skipped_tb_error`` (status:
  ``StatusCounts.skipped_tb_error``) and logged at INFO with TB's ``tb_code`` only, so one
  unreadable message never stops its folder; any other error (``unavailable``,
  ``invalid_params``, a ``tb_error`` of ``find``/``page``…) still raises;
- ``stale_id`` on ``raw``/``full`` → ``find(hmid)``; every match in the same folder is fetched
  once more; none, or all stale again → skipped, counted in ``stale_retries``, left to reconcile
  (B1). A match in another folder is never ingested here (its own folder's pass does that);
- ``md5:`` ids are counted in ``md5_ids``; for any other id, ``mid_of(hmid)`` differing from the
  raw-derived mid counts ``hmid_mismatch`` (the message is stored under the raw-derived mid),
  once per (message, folder): only when that write gives the mid its first location in the
  folder, so re-fetches of a located copy (hourly re-pages, event replays) never add to it; a
  copy moved to another folder counts again there;
- a copy whose content differs from the stored content of its mid (a new raw-derived conflict)
  makes the path fetch the content copy's raw (``Store.content_copy`` + ``find`` + ``raw``) so the
  Store can classify the pair (``conflict_class``); when that copy cannot be fetched the conflict
  stays unclassified (``conflicts_unclassified``) and the reason is logged;
- in a folder whose ``special_use`` holds ``sent``, the copy's To/Cc are counted once per mid
  (``tern.index.recipients``);
- ``x_mid_churn`` counts x- mids first inserted after the folder's initial backfill completed,
  except a late download (a write that dropped a ``headers_only_items`` row) and an x- mid the
  same reconcile run removed from a folder it passed earlier (an unevented move, see
  ``tern.index.reconcile``): A5's detector is for TB rewriting the headers of mail it already
  had;
- a written copy drops its ``headers_only_items`` row: one row of its mid at its tb_date for a
  non-``md5:`` id; for an ``md5:`` id one NULL-mid row at its tb_date, and only when the mid had
  no location in the folder yet (see ``ItemIngester._drop_headers_only_row`` for the ambiguity
  this accepts).

A ``tb_error`` item is retried by reconcile: every hourly/daily pass re-fetches each tb_date
holding a ``tb_error`` row (and a Message-ID item not indexed mismatches its window anyway) and
rebuilds that second's rows, so the row goes once TB reads the item (``tern.index.reconcile``,
"Unreadable items").

Up to ``ingest_concurrency`` fetches are in flight (writes stay in page order); with
``ingest_max_msgs_per_s > 0`` every fetch first takes a slot from a :class:`TokenBucket`. The cap
is on TB's load, not per job: ``Backfill(throttle=...)`` takes a shared bucket, and
``IngestService`` gives its backfill, events and reconcile jobs one bucket (``Reconcile`` takes
its ``EventIngest``'s). ``find`` and refill fetches (``CopyFetcher``) are not throttled.

A folder already done when ``run`` starts is not paged again, except where it holds
``headers_only_items`` rows: those seconds, coalesced into ranges no wider than one window, are
paged once per range, and only the items that may be those rows are handled, so an item TB has
since downloaded gets ingested and its row dropped (``Backfill._revisit_headers_only``). This
does early what plan step 8 gives reconcile ("any pass that pages a window ingests items whose
headers_only turned false"); its counters go to a ``kind='hourly'`` ``ingest_runs`` row, never
to a ``backfill`` row, so the backfill rate is not mixed with it.

Indexed folders: every folder whose ``special_use`` holds none of trash, junk, outbox, templates
or drafts (#30). Every listed folder (excluded ones too) is recorded in ``folders``.

Each completed folder writes one ``ingest_runs`` row and one INFO line with its #28 hash, the
account type and counts, except a resumed backfill that found nothing left below its last
committed window (every counter zero): it writes no row, only a DEBUG line, so such a near-empty
row (paging and probe seconds, no messages) never skews the backfill ``rate``. Logs never
carry names, paths, subjects or addresses (CLAUDE.md).

Bookkeeping writes: ``ingest_state``, ``headers_only_items`` and ``skipped_items`` (and
``sent_counted``/``sent_recipients`` in ``tern.index.recipients``) go through ``Store.connection``
inside ``Store.transaction()``, as the ``Store.connection`` docstring allows since step 8;
``ingest_runs`` rows go through ``Store.record_ingest_run`` only.

Known limitations (inputs for step 10 and the post-MVP backlog):

- ``ingest_runs`` counters are per run and written only when a folder completes: a killed run's
  counts are lost, so messages ingested by killed runs are never counted in any run row and the
  backfill ``rate`` covers completed runs only. A folder whose messages were all ingested by
  killed runs gets no ``backfill`` row at all (the resume that finds nothing left writes none), so
  if every folder of an account type ends that way, ``index status`` shows no rate or
  throttle/concurrency settings for it. The window a killed run was in is paged again on
  resume, so ``md5_ids`` (also
  counted by a headers-only revisit) is neither a per-folder total nor safe to sum over runs.
  Step 10 should take ``md5_ids`` per folder from the current state (x- locations + NULL-mid
  ``headers_only_items`` + ``skipped_items`` of the folder) or from the latest ``backfill`` row;
- the headers-only revisit re-fetches every downloaded ``md5:`` item at a second that holds a
  NULL-mid row (ID-less items cannot be matched to their rows without a TB id, #5), on every
  run while such a row stays (an ID-less item there still headers-only), including over-RAW_MAX
  items through ``full`` (up to 25 MB each; costly on IMAP). Accepted: it takes downloaded
  ID-less mail at the same second as undownloaded ID-less mail in one folder. A cheaper gate
  (compare the second's item count with its stored locations and rows before fetching) is
  left for the post-MVP backlog (``plans/90-post-mvp.md`` B5, "Headers-only revisit count gate");
- a skipped row says nothing about whether its item had a Message-ID, so ``index status`` leaves
  ``tb_error`` rows out of ``md5_ids`` (an ID-less item TB cannot read is not counted there).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Literal

from tern.bridge.protocol import RAW_MAX, BridgeError, FullMessage, PageItem
from tern.config import TernConfig
from tern.index.html_text import html_to_text_limited, strip_invisible
from tern.index.mid import (
    FULL_PREFIX,
    TB_MD5_PREFIX,
    X_PREFIX,
    content_hash,
    content_hash_full,
    mid_from_full,
    mid_from_raw,
    mid_of,
)
from tern.index.mime import TEXT_CAP_BYTES, parse_raw, split_quotes, utc_epoch
from tern.index.recipients import count_sent
from tern.index.shorthash import assign, folder_name
from tern.index.source import IngestSource
from tern.index.store import (
    AttachmentRecord,
    IngestRun,
    LocationState,
    MessageRecord,
    Source,
    Store,
)

log = logging.getLogger(__name__)

DAY_S = 86_400
#: ``skipped_items.reason`` per skipped fetch kind.
SKIP_REASONS: dict[str, str] = {"skipped_large_noid": "large_noid", "skipped_tb_error": "tb_error"}
#: A window whose cursor goes stale this many times in a row fails the run (IngestError).
MAX_WINDOW_RESTARTS = 5
#: ``special_use`` values whose folders are not indexed (#30).
EXCLUDED_SPECIAL_USE = frozenset({"trash", "junk", "outbox", "templates", "drafts"})
SENT = "sent"
STATUS_BACKFILL = "backfill"
STATUS_DONE = "done"

_MSG_ID = re.compile(r"<([^<>]*)>")


class IngestError(RuntimeError):
    """An ingest job cannot go on (the message names no mail content)."""


class _Unreadable(Exception):
    """``raw``/``full`` of one tb_id gave ``tb_error``: TB failed to read that message."""

    def __init__(self, tb_code: str | None) -> None:
        super().__init__("tb_error")
        self.tb_code = tb_code


def is_indexed(special_use: Sequence[str]) -> bool:
    return not {s.lower() for s in special_use} & EXCLUDED_SPECIAL_USE


def tb_date_of(item_date: datetime) -> int:
    """A PageItem date as whole epoch seconds (floored); a date without UTC offset is refused."""
    if item_date.utcoffset() is None:
        raise IngestError("PageItem date without a UTC offset")
    return math.floor(item_date.timestamp())


def _dt(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def coalesce_dates(dates: Iterable[int], width: int) -> list[tuple[int, int]]:
    """Group epoch seconds into inclusive ``(lo, hi)`` ranges, newest first, each with
    ``hi - lo < width``: one page query per range instead of one per second."""
    out: list[tuple[int, int]] = []
    for d in sorted(set(dates), reverse=True):
        if out and out[-1][1] - d < width:
            out[-1] = (d, out[-1][1])
        else:
            out.append((d, d))
    return out


# ---- records ---------------------------------------------------------------------------------


def record_from_raw(raw: bytes) -> MessageRecord:
    """One copy's record from its getRaw bytes: identity from ``tern.index.mid``, fields from
    ``tern.index.mime.parse_raw``, the main text (quotes and signature removed) as body."""
    mid = mid_from_raw(raw)
    p = parse_raw(raw)
    return MessageRecord(
        mid=mid,
        content_hash=content_hash(raw),
        parse_status=p.parse_status,
        header_message_id=None if mid.startswith(X_PREFIX) else p.header_message_id,
        date_utc=p.date_utc,
        from_addr=p.from_addr,
        from_name=p.from_name,
        to_addrs=p.to_addrs,
        cc_addrs=p.cc_addrs,
        subject=p.subject,
        in_reply_to=p.in_reply_to,
        refs=p.refs,
        size=len(raw),
        body_text=p.main_text,
        attachments=tuple(
            AttachmentRecord(filename=a.filename, mime=a.mime, size=a.size) for a in p.attachments
        ),
    )


def _full_header(full: FullMessage, name: str) -> str | None:
    for key, values in full.headers.items():
        if key.lower() == name and values:
            return values[0]
    return None


def _full_addrs(full: FullMessage, name: str) -> tuple[str, ...]:
    values = [v for key, vs in full.headers.items() if key.lower() == name for v in vs]
    out: dict[str, None] = {}
    for _, addr in getaddresses(values):
        local, at, domain = addr.rpartition("@")
        if at and local and domain:
            out.setdefault(addr.lower())
    return tuple(out)


def _full_text(full: FullMessage) -> tuple[str, bool]:
    """(main text, truncated): the text/plain parts, else the HTML parts as text, each split by
    ``mime.split_quotes``; capped at ``TEXT_CAP_BYTES`` (T13).

    The HTML budget is kept in UTF-8 bytes (the cap's unit) and passed to
    ``html_to_text_limited`` as its character limit: a character is at least one byte, so that
    limit only bounds the work, and the byte cap at the end bounds the text. Once the budget is
    spent, later HTML parts are not converted at all (a limit <= 0 is never passed)."""
    plain = [tp.text for tp in full.text_parts if tp.mime.lower() == "text/plain"]
    budget = TEXT_CAP_BYTES
    truncated = False
    texts: list[str] = []
    if plain:
        texts = plain
    else:
        for tp in full.text_parts:
            if tp.mime.lower() == "text/html":
                if budget <= 0:
                    truncated = True
                    break
                text, cut = html_to_text_limited(tp.text, budget)
                truncated |= cut
                budget -= len(text.encode("utf-8", "surrogatepass"))
                texts.append(text)
    lines: list[str] = []
    for text in texts:
        main, _quoted = split_quotes(text)
        lines += [*main, ""]
    out: list[str] = []
    for line in lines:
        line = line.rstrip(" \t")
        if line or (out and out[-1]):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    joined = "\n".join(out)
    data = joined.encode("utf-8", "surrogatepass")
    if len(data) > TEXT_CAP_BYTES:
        joined = data[:TEXT_CAP_BYTES].decode("utf-8", "ignore")
        truncated = True
    return joined, truncated


def _date_epoch(value: str) -> int | None:
    """UTC epoch of a Date value; None when it cannot be read (the same narrow error set as
    ``tern.index.mid``; the record is ``partial`` anyway) or its epoch is outside Python's
    datetime range (``tern.index.mime.utc_epoch``). A zone-less Date is UTC."""
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return utc_epoch(dt)


def record_from_full(mid: str, full: FullMessage, size: int) -> MessageRecord:
    """One copy's record from TB's ``full`` form (over RAW_MAX there are no raw bytes). Always
    ``parse_status='partial'``: it is TB's rendering of the message, not its bytes, so the MIME
    rules of ``tern.index.mime`` (charsets, hidden HTML in multipart, attachments' sizes) could
    not be applied to it."""
    subject = _full_header(full, "subject")
    from_name, from_addr = parseaddr(_full_header(full, "from") or "")
    local, at, domain = from_addr.rpartition("@")
    hmid = _full_header(full, "message-id")
    date = _full_header(full, "date")
    epoch = None if date is None else _date_epoch(date)
    in_reply_to = _MSG_ID.findall(_full_header(full, "in-reply-to") or "")
    body, _truncated = _full_text(full)
    return MessageRecord(
        mid=mid,
        content_hash=content_hash_full(full),
        parse_status="partial",
        header_message_id=None if hmid is None else (hmid.strip().strip("<>") or None),
        date_utc=epoch,
        from_addr=from_addr.lower() if at and local and domain else None,
        from_name=strip_invisible(from_name).strip() or None,
        to_addrs=_full_addrs(full, "to"),
        cc_addrs=_full_addrs(full, "cc"),
        subject=None if subject is None else strip_invisible(subject).strip(),
        in_reply_to=in_reply_to[0].strip() if in_reply_to else None,
        refs=tuple(i.strip() for i in _MSG_ID.findall(_full_header(full, "references") or "")),
        size=size,
        body_text=body,
        attachments=tuple(
            AttachmentRecord(filename=strip_invisible(a.name) or None, mime=a.mime, size=a.size)
            for a in full.attachments
        ),
    )


# ---- throttle --------------------------------------------------------------------------------


class TokenBucket:
    """At most ``rate`` acquisitions per second, evenly spaced (a bucket of one token: no burst).
    ``clock`` and ``sleep`` are injectable for tests."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next: float | None = None

    async def acquire(self) -> None:
        now = self._clock()
        slot = now if self._next is None else max(now, self._next)
        self._next = slot + self.interval
        if slot > now:
            await self._sleep(slot - now)


def throttle_for(cfg: TernConfig) -> TokenBucket | None:
    """A new bucket for ``cfg.ingest_max_msgs_per_s``, or None when it is 0 (throttle off)."""
    return TokenBucket(cfg.ingest_max_msgs_per_s) if cfg.ingest_max_msgs_per_s > 0 else None


# ---- per-item path ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FolderInfo:
    """An indexed folder as one job run sees it."""

    folder_id: str
    account_id: str
    account_type: str
    special_use: tuple[str, ...]
    #: The #28 short hash (``fold-xxxx``): the only folder name a log line may carry.
    hash: str
    #: TB's message count when the folder was listed.
    total: int

    @property
    def is_sent(self) -> bool:
        return SENT in {s.lower() for s in self.special_use}


@dataclass
class Counters:
    """Per-run counts, written to ``ingest_runs`` (``headers_only`` only to the report)."""

    n_msgs: int = 0
    headers_only: int = 0
    skipped_large_noid: int = 0
    #: items whose ``raw``/``full`` gave ``tb_error`` (report only, like ``headers_only``)
    skipped_tb_error: int = 0
    stale_retries: int = 0
    md5_ids: int = 0
    x_mid_churn: int = 0
    hmid_mismatch: int = 0

    def add(self, other: Counters) -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))


@dataclass(frozen=True)
class FetchedCopy:
    record: MessageRecord
    raw: bytes | None  # None on the full path
    state: LocationState


FetchKind = Literal["copies", "headers_only", "skipped_large_noid", "skipped_tb_error", "stale"]


@dataclass(frozen=True)
class Fetched:
    """What one PageItem yielded."""

    kind: FetchKind
    tb_date: int
    header_message_id: str
    md5: bool
    #: ``mid_of(header_message_id)`` for a non-``md5:`` id, else None.
    hmid_mid: str | None
    copies: tuple[FetchedCopy, ...] = ()
    #: the item's tb_id went stale and ``copies`` are those of every match ``find`` gave in
    #: the folder: copies of its hmid, not necessarily of this item
    refetched: bool = False
    #: TB's error code of a ``skipped_tb_error`` item (an identifier, never mail content)
    tb_code: str | None = None


def _state_of(tags: Iterable[str], flagged: bool, read: bool) -> LocationState:
    return LocationState(tags=tuple(tags), flagged=flagged, read=read)


class ItemIngester:
    """Fetch one PageItem's copy (network only) and write it (one Store transaction). Shared by
    Backfill, EventIngest and Reconcile so every path counts alike."""

    def __init__(
        self, store: Store, source: IngestSource, *, throttle: TokenBucket | None = None
    ) -> None:
        self.store = store
        self.source = source
        self.throttle = throttle

    async def _slot(self) -> None:
        if self.throttle is not None:
            await self.throttle.acquire()

    async def _fetch_one(self, tb_id: int, size: int) -> tuple[MessageRecord, bytes | None] | None:
        """(record, raw) of one tb_id; raw None on the full path; None when over RAW_MAX
        without a Message-ID. Raises ``BridgeError('stale_id')`` for the caller to handle, and
        :class:`_Unreadable` when ``raw``/``full`` gives ``tb_error``."""
        try:
            if size <= RAW_MAX:
                await self._slot()
                try:
                    raw = await self.source.raw(tb_id)
                except BridgeError as exc:
                    if exc.name != "too_large":
                        raise
                else:
                    # Parsing is CPU work (bounded, T13): off the event loop.
                    return await asyncio.to_thread(record_from_raw, raw), raw
            await self._slot()
            full = await self.source.full(tb_id)
        except BridgeError as exc:
            if exc.name != "tb_error":
                raise
            raise _Unreadable(exc.tb_code) from None
        mid = mid_from_full(full)
        if mid is None:
            return None
        return await asyncio.to_thread(record_from_full, mid, full, size), None

    async def fetch(self, folder: FolderInfo, item: PageItem, counters: Counters) -> Fetched:
        tb_date = tb_date_of(item.date)
        hmid = item.header_message_id
        md5 = hmid.startswith(TB_MD5_PREFIX)
        hmid_mid = None if md5 else mid_of(hmid)

        refetched = False

        def result(
            kind: FetchKind, copies: tuple[FetchedCopy, ...] = (), tb_code: str | None = None
        ) -> Fetched:
            return Fetched(kind, tb_date, hmid, md5, hmid_mid, copies, refetched, tb_code)

        if md5:
            counters.md5_ids += 1
        if item.headers_only:
            counters.headers_only += 1
            return result("headers_only")
        got: list[tuple[tuple[MessageRecord, bytes | None] | None, LocationState]] = []
        try:
            try:
                one = await self._fetch_one(item.tb_id, item.size)
                got.append((one, _state_of(item.tags, item.flags.flagged, item.flags.read)))
            except BridgeError as exc:
                if exc.name != "stale_id":
                    raise
                refetched = True
                got = await self._refetch(folder, item)
                if not got:
                    counters.stale_retries += 1
                    log.info("stale item left to reconcile", extra={"folder": folder.hash})
                    return result("stale")
        except _Unreadable as exc:
            counters.skipped_tb_error += 1
            log.info(
                "item skipped: TB could not read it",
                extra={"folder": folder.hash, "tb_code": exc.tb_code},
            )
            return result("skipped_tb_error", tb_code=exc.tb_code)
        copies = tuple(
            FetchedCopy(record=r[0], raw=r[1], state=state) for r, state in got if r is not None
        )
        if not copies:
            counters.skipped_large_noid += 1
            return result("skipped_large_noid")
        return result("copies", copies)

    async def _refetch(
        self, folder: FolderInfo, item: PageItem
    ) -> list[tuple[tuple[MessageRecord, bytes | None] | None, LocationState]]:
        """``stale_id``: re-resolve the item by its hmid in the same folder and fetch every match
        once; matches still stale, or unreadable (``tb_error``), are dropped. When every match is
        dropped and one was unreadable, raises :class:`_Unreadable` (the item is skipped, not
        left stale). An unreadable match beside one that was read is logged at INFO (``tb_code``
        only) and gets no ``skipped_items`` row and no ``skipped_tb_error`` count: rows and that
        count are per page item, and this item gave copies (not skipped). The match is either
        another item of the folder, listed (and so recorded) by its own page entry, or this
        item under its new tb_id with another copy of its Message-ID read in its place;
        reconcile's per-mid copy count fetches it again when the folder holds fewer copies than
        TB lists."""
        found = [
            f
            for f in await self.source.find(item.header_message_id)
            if f.folder_id == folder.folder_id
        ]
        out: list[tuple[tuple[MessageRecord, bytes | None] | None, LocationState]] = []
        unreadable: list[_Unreadable] = []
        for f in found:
            try:
                one = await self._fetch_one(f.tb_id, item.size)
            except BridgeError as exc:
                if exc.name != "stale_id":
                    raise
                continue
            except _Unreadable as exc:
                unreadable.append(exc)
                continue
            out.append((one, _state_of(f.tags, f.flags.flagged, f.flags.read)))
        log.debug("stale item re-resolved", extra={"found": len(found), "fetched": len(out)})
        if not out and unreadable:
            raise unreadable[0]
        for exc in unreadable:
            log.info(
                "re-resolved match not read",
                extra={"folder": folder.hash, "tb_code": exc.tb_code},
            )
        return out

    async def _content_raw(self, copy: FetchedCopy, hmid: str) -> bytes | None:
        """The raw of the copy holding the stored content, when ``copy`` makes a new raw-derived
        conflict (so the Store can classify the pair); None when there is none to classify or it
        cannot be fetched."""
        mid, h = copy.record.mid, copy.record.content_hash
        if copy.raw is None or h.startswith(FULL_PREFIX):
            return None
        row = self.store.connection.execute(
            "SELECT content_hash FROM messages WHERE mid = ?", (mid,)
        ).fetchone()
        stored = None if row is None else row[0]
        if stored is None or stored == h or stored.startswith(FULL_PREFIX):
            return None
        held = {loc.content_hash for loc in self.store.locations(mid)}
        held |= {c for _, c in self.store.same_folder_copies(mid)}
        if h in held:
            return None  # not a new conflict: this content is already one of the copies
        target = self.store.content_copy(mid)
        if target is None:
            return None
        for f in await self.source.find(hmid):
            if f.folder_id != target[0]:
                continue
            await self._slot()
            try:
                raw = await self.source.raw(f.tb_id)
            except BridgeError as exc:
                if exc.name not in ("stale_id", "too_large", "tb_error"):
                    raise
                if exc.name == "tb_error":
                    log.info("content copy not read", extra={"mid": mid, "tb_code": exc.tb_code})
                continue
            if content_hash(raw) == stored:
                return raw
        log.info("conflict left unclassified: content copy not fetched", extra={"mid": mid})
        return None

    def _backfill_done(self, folder_id: str) -> bool:
        row = self.store.connection.execute(
            "SELECT status FROM ingest_state WHERE folder_id = ?", (folder_id,)
        ).fetchone()
        return row is not None and row[0] == STATUS_DONE

    def _drop_headers_only_row(self, folder_id: str, fetched: Fetched, mid: str) -> bool:
        """The item may have been headers-only: drop its row. A non-``md5:`` id drops one row of
        its mid at its tb_date, never the rows of other copies of that Message-ID (e.g. a
        headers-only spoof at another second); copies of one Message-ID at one second cannot be
        told apart, so re-writing a downloaded one there can take a headers-only sibling's row (a
        Backfill window replace or a reconcile re-page restores it). An ``md5:`` id drops one
        NULL-mid row at its tb_date, but only when ``mid`` has no location in this folder yet: a
        copy already indexed here was never headers-only, so re-writing it must not take another
        item's row. Two ID-less items at one second cannot be told apart without a TB id (#5): a
        new one arriving there while the other is still headers-only takes that row (a Backfill
        window replace or a reconcile re-page restores it). Returns True when a row was
        dropped."""
        conn = self.store.connection
        if fetched.hmid_mid is not None:
            cur = conn.execute(
                "DELETE FROM headers_only_items WHERE rowid = (SELECT rowid FROM "
                "headers_only_items WHERE folder_id = ? AND mid = ? AND tb_date = ? "
                "ORDER BY ordinal LIMIT 1)",
                (folder_id, fetched.hmid_mid, fetched.tb_date),
            )
            return cur.rowcount > 0
        if not fetched.md5 or folder_id in {loc.folder_id for loc in self.store.locations(mid)}:
            return False
        cur = conn.execute(
            "DELETE FROM headers_only_items WHERE rowid = (SELECT rowid FROM "
            "headers_only_items WHERE folder_id = ? AND tb_date = ? AND mid IS NULL "
            "ORDER BY ordinal LIMIT 1)",
            (folder_id, fetched.tb_date),
        )
        return cur.rowcount > 0

    async def write(
        self,
        folder: FolderInfo,
        fetched: Fetched,
        *,
        source: Source,
        counters: Counters,
        removed_in_run: Collection[str] = (),
    ) -> list[str]:
        """Write every fetched copy (one transaction each); returns the mids written.

        ``hmid_mismatch`` is counted here, at most once per call, and only when a copy whose
        raw-derived mid differs from ``mid_of(TB hmid)`` gets its first location in the folder:
        a re-fetch of a copy already located there (every hourly re-page of its never-matching
        window, an event replay, a resumed backfill window) does not count it again.
        ``x_mid_churn`` skips the mids in ``removed_in_run`` (x- mids a reconcile run removed
        from an earlier folder: an unevented move, not churn)."""
        mids: list[str] = []
        mismatch = False
        for copy in fetched.copies:
            content_raw = await self._content_raw(copy, fetched.header_message_id)
            rec = copy.record
            churn = False
            with self.store.transaction():
                located = {loc.folder_id for loc in self.store.locations(rec.mid)}
                if not fetched.md5 and rec.mid != fetched.hmid_mid:
                    mismatch |= folder.folder_id not in located
                outcome = self.store.upsert_message(rec)
                late = self._drop_headers_only_row(folder.folder_id, fetched, rec.mid)
                self.store.add_location(
                    rec.mid,
                    folder.folder_id,
                    rec.content_hash,
                    copy.state,
                    fetched.tb_date,
                    source=source,
                    raw=None if content_raw is None else copy.raw,
                    content_raw=content_raw,
                )
                if (
                    outcome == "inserted"
                    and rec.mid.startswith(X_PREFIX)
                    and not late
                    and rec.mid not in removed_in_run
                ):
                    # A late download (it had a headers_only row) is not churn: A5's detector
                    # is for TB rewriting the headers of mail it already had.
                    churn = self._backfill_done(folder.folder_id)
                if folder.is_sent:
                    count_sent(self.store, rec.mid, (*rec.to_addrs, *rec.cc_addrs), rec.date_utc)
            counters.n_msgs += 1
            counters.x_mid_churn += int(churn)
            mids.append(rec.mid)
        if mismatch:
            counters.hmid_mismatch += 1
            log.info(
                "TB headerMessageId does not match the raw Message-ID",
                extra={"mid": fetched.copies[0].record.mid, "folder": folder.hash},
            )
        return mids


# ---- backfill --------------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchReport:
    """Progress after every ``batch_size`` items handled, and once at the end of a run."""

    #: Items handled so far in this run (stored, headers-only, skipped or stale).
    items: int
    #: Items TB listed in the folders not yet done when the run started.
    total: int
    #: Of ``total``, items of folders not yet done that this run has not handled (per folder
    #: TB's count minus the items handled, at least 0; 0 once the folder is done).
    remaining: int
    folders_done: int
    folders_total: int


@dataclass(frozen=True)
class IngestReport:
    folders_total: int
    #: Folders done at the end of the run (including those done before it).
    folders_done: int
    items: int
    batches: int
    secs: float
    counters: Counters = field(default_factory=Counters)


class _Progress:
    def __init__(
        self,
        totals: dict[str, int],
        batch_size: int,
        on_batch: Callable[[BatchReport], None] | None,
        folders_total: int,
        folders_done: int,
    ) -> None:
        self.totals = totals
        self.handled = dict.fromkeys(totals, 0)
        self.done: set[str] = set()
        self.batch_size = batch_size
        self.on_batch = on_batch
        self.folders_total = folders_total
        self.folders_done = folders_done
        self.items = 0
        self.in_batch = 0
        self.batches = 0
        self.last: BatchReport | None = None

    def item(self, folder_id: str) -> None:
        self.items += 1
        if folder_id in self.handled:
            self.handled[folder_id] += 1
        self.in_batch += 1
        if self.in_batch >= self.batch_size:
            self.emit()

    def folder_done(self, folder_id: str) -> None:
        self.done.add(folder_id)
        self.folders_done += 1

    def report(self) -> BatchReport:
        remaining = sum(
            max(self.totals[f] - self.handled[f], 0) for f in self.totals if f not in self.done
        )
        return BatchReport(
            items=self.items,
            total=sum(self.totals.values()),
            remaining=remaining,
            folders_done=self.folders_done,
            folders_total=self.folders_total,
        )

    def emit(self) -> None:
        self.last = self.report()
        self.in_batch = 0
        self.batches += 1
        if self.on_batch is not None:
            self.on_batch(self.last)

    def finish(self) -> None:
        """The final report: emitted unless the last one already says the same (a run whose
        last item fell on a batch boundary still reports its last ``folder_done``)."""
        if self.last is None or self.report() != self.last:
            self.emit()


@dataclass
class _WindowRows:
    headers_only: list[tuple[int, str | None]] = field(default_factory=list[tuple[int, str | None]])
    #: (tb_date, ``skipped_items.reason``)
    skipped: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])
    boundary_mids: set[str] = field(default_factory=set[str])


class Backfill:
    """The initial (and resumed) ingest of every indexed folder; see the module docstring.
    ``tern.index.ingest.Backfill`` as named by ``tests/smoke/adapter.py``."""

    def __init__(
        self,
        store: Store,
        source: IngestSource,
        cfg: TernConfig,
        *,
        batch_size: int = 50,
        clock: Callable[[], float] = time.time,
        throttle: TokenBucket | None | Literal["cfg"] = "cfg",
    ) -> None:
        """``throttle``: the fetch bucket to share with other jobs (None = no throttle); the
        default ``"cfg"`` makes a new one from ``cfg.ingest_max_msgs_per_s`` (none when 0)."""
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.store = store
        self.source = source
        self.cfg = cfg
        self.batch_size = batch_size
        self.clock = clock
        if throttle == "cfg":
            throttle = throttle_for(cfg)
        self.items = ItemIngester(store, source, throttle=throttle)
        self.window_s = cfg.backfill_window_days * DAY_S

    # -- listing ---------------------------------------------------------------------------------

    async def list_folders(self) -> list[FolderInfo]:
        """Re-list accounts and folders into the index; the indexed folders, in TB's order."""
        accounts = await self.source.accounts()
        listed = [(a, await self.source.folders(a.account_id)) for a in accounts]
        hashes = assign((folder_name(a.name, f.path) for a, fs in listed for f in fs), "fold")
        out: list[FolderInfo] = []
        with self.store.transaction():
            for a, folders in listed:
                self.store.upsert_account(a.account_id, a.name, a.type)
                for f in folders:
                    self.store.upsert_folder(f.folder_id, a.account_id, f.path, f.special_use)
                    if is_indexed(f.special_use):
                        out.append(
                            FolderInfo(
                                folder_id=f.folder_id,
                                account_id=a.account_id,
                                account_type=a.type,
                                special_use=tuple(f.special_use),
                                hash=hashes[folder_name(a.name, f.path)],
                                total=f.total,
                            )
                        )
        log.debug("folders listed", extra={"accounts": len(accounts), "indexed": len(out)})
        return out

    # -- ingest_state ------------------------------------------------------------------------------

    def _state(self, folder_id: str) -> tuple[int | None, str | None]:
        row = self.store.connection.execute(
            "SELECT watermark_date, status FROM ingest_state WHERE folder_id = ?", (folder_id,)
        ).fetchone()
        return (None, None) if row is None else (row[0], row[1])

    def _set_state(self, folder_id: str, watermark: int, mids: Iterable[str], status: str) -> None:
        self.store.connection.execute(
            "INSERT INTO ingest_state(folder_id, watermark_date, watermark_mids, status, "
            "updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(folder_id) DO UPDATE SET "
            "watermark_date = excluded.watermark_date, watermark_mids = excluded.watermark_mids, "
            "status = excluded.status, updated_at = excluded.updated_at",
            (folder_id, watermark, json.dumps(sorted(mids)), status, int(self.clock())),
        )

    def _replace_rows(
        self,
        folder_id: str,
        lo: int | None,
        hi: int | None,
        rows: _WindowRows,
        *,
        skipped: bool = True,
    ) -> None:
        """Replace the window's ``headers_only_items``/``skipped_items`` rows (``lo <= tb_date <
        hi``, None = unbounded) by ``rows``, with ordinals per tb_date in page order.
        ``skipped=False`` keeps the window's ``skipped_items`` rows and appends ``rows.skipped``
        after them (the headers-only revisit at a second whose ID-less items it did not all
        handle: a downloaded Message-ID item there that was skipped)."""
        conn = self.store.connection
        cond = "folder_id = ?"
        args: list[object] = [folder_id]
        if lo is not None:
            cond += " AND tb_date >= ?"
            args.append(lo)
        if hi is not None:
            cond += " AND tb_date < ?"
            args.append(hi)
        tables = ("headers_only_items", "skipped_items") if skipped else ("headers_only_items",)
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE {cond}", args)
        ordinal: dict[int, int] = {}
        for tb_date, mid in rows.headers_only:
            n = ordinal.get(tb_date, 0)
            ordinal[tb_date] = n + 1
            conn.execute(
                "INSERT INTO headers_only_items(folder_id, tb_date, ordinal, mid) "
                "VALUES (?, ?, ?, ?)",
                (folder_id, tb_date, n, mid),
            )
        ordinal.clear()
        for tb_date, reason in rows.skipped:
            if tb_date not in ordinal:
                ordinal[tb_date] = (
                    0
                    if skipped
                    else conn.execute(
                        "SELECT coalesce(max(ordinal) + 1, 0) FROM skipped_items "
                        "WHERE folder_id = ? AND tb_date = ?",
                        (folder_id, tb_date),
                    ).fetchone()[0]
                )
            n = ordinal[tb_date]
            ordinal[tb_date] = n + 1
            conn.execute(
                "INSERT INTO skipped_items(folder_id, tb_date, ordinal, reason) "
                "VALUES (?, ?, ?, ?)",
                (folder_id, tb_date, n, reason),
            )

    def _write_run(
        self,
        folder: FolderInfo,
        counters: Counters,
        secs: float,
        kind: Literal["backfill", "hourly"] = "backfill",
    ) -> None:
        self.store.record_ingest_run(
            IngestRun(
                ts=int(self.clock()),
                kind=kind,
                folder_id=folder.folder_id,
                account_type=folder.account_type,
                n_msgs=counters.n_msgs,
                secs=secs,
                concurrency=self.cfg.ingest_concurrency,
                throttle=self.cfg.ingest_max_msgs_per_s,
                stale_retries=counters.stale_retries,
                skipped_large_noid=counters.skipped_large_noid,
                md5_ids=counters.md5_ids,
                x_mid_churn=counters.x_mid_churn,
                hmid_mismatch=counters.hmid_mismatch,
            )
        )
        log.info(
            "backfill folder done" if kind == "backfill" else "headers-only revisit done",
            extra={
                "folder": folder.hash,
                "account_type": folder.account_type,
                "n": counters.n_msgs,
                "secs": round(secs, 3),
                "msg_per_s": round(counters.n_msgs / secs, 2) if secs > 0 else None,
                "concurrency": self.cfg.ingest_concurrency,
            },
        )

    # -- paging ------------------------------------------------------------------------------------

    async def page_all(
        self, folder: FolderInfo, from_date: int | None, to_date: int | None
    ) -> list[PageItem]:
        """Every item of the inclusive ``[from_date, to_date]`` query (None = open), following
        ``next_cursor`` to the end; a ``stale_id`` cursor restarts the query."""
        lo = None if from_date is None else _dt(from_date)
        hi = None if to_date is None else _dt(to_date)
        for attempt in range(MAX_WINDOW_RESTARTS + 1):
            page = await self.source.page(folder.folder_id, lo, hi)
            items = list(page.items)
            try:
                while page.next_cursor is not None:
                    page = await self.source.page(folder.folder_id, cursor=page.next_cursor)
                    items += page.items
            except BridgeError as exc:
                if exc.name != "stale_id":
                    raise
                log.info(
                    "cursor went stale: window restarted",
                    extra={"folder": folder.hash, "attempt": attempt + 1},
                )
                continue
            return items
        raise IngestError(f"cursor went stale {MAX_WINDOW_RESTARTS + 1} times in one window")

    async def process(
        self,
        folder: FolderInfo,
        items: Sequence[PageItem],
        counters: Counters,
        progress: _Progress | None,
        *,
        source: Source = "backfill",
        boundary: int | None = None,
    ) -> _WindowRows:
        """Fetch (up to ``ingest_concurrency`` in flight) and write ``items`` in order; returns
        the window's headers-only and skipped rows and the mids stored at ``boundary``."""
        rows = _WindowRows()
        limit = self.cfg.ingest_concurrency
        queue: deque[asyncio.Task[Fetched]] = deque()
        nxt = 0
        try:
            for _ in items:
                while nxt < len(items) and len(queue) < limit:
                    queue.append(
                        asyncio.create_task(self.items.fetch(folder, items[nxt], counters))
                    )
                    nxt += 1
                fetched = await queue.popleft()
                if fetched.kind == "headers_only":
                    rows.headers_only.append((fetched.tb_date, fetched.hmid_mid))
                elif fetched.kind in SKIP_REASONS:
                    rows.skipped.append((fetched.tb_date, SKIP_REASONS[fetched.kind]))
                elif fetched.kind == "copies":
                    mids = await self.items.write(folder, fetched, source=source, counters=counters)
                    if fetched.tb_date == boundary:
                        rows.boundary_mids.update(mids)
                if progress is not None:
                    progress.item(folder.folder_id)
        finally:
            # Only reached with tasks left when an exception is already propagating: cancel the
            # prefetches and wait for them so none outlives the run. Their own outcomes are
            # dropped on purpose; the exception that stopped the loop is the one re-raised.
            for task in queue:
                task.cancel()
            if queue:
                await asyncio.gather(*queue, return_exceptions=True)
        return rows

    async def _window(
        self,
        folder: FolderInfo,
        lo: int,
        hi: int | None,
        counters: Counters,
        progress: _Progress,
    ) -> int:
        """Page, ingest and commit one window (``lo <= tb_date < hi``; the query is
        ``[lo, hi]``); the watermark becomes ``lo``. Returns the number of items kept."""
        items = await self.page_all(folder, lo, hi)
        kept = [
            i for i in items if lo <= tb_date_of(i.date) and (hi is None or tb_date_of(i.date) < hi)
        ]
        if len(kept) != len(items):
            log.debug("items outside the window dropped", extra={"n": len(items) - len(kept)})
        rows = await self.process(folder, kept, counters, progress, boundary=lo)
        with self.store.transaction():
            self._replace_rows(folder.folder_id, lo, hi, rows)
            self._set_state(folder.folder_id, lo, rows.boundary_mids, STATUS_BACKFILL)
        return len(kept)

    async def _probe(self, folder: FolderInfo, below: int) -> int | None:
        """The newest tb_date older than ``below`` over every page of the open-ended
        ``to_date = below`` query, or None when there is none."""
        dates = [
            d
            for d in (tb_date_of(i.date) for i in await self.page_all(folder, None, below))
            if d < below
        ]
        return max(dates) if dates else None

    async def _backfill_folder(self, folder: FolderInfo, progress: _Progress) -> Counters:
        counters = Counters()
        t0 = time.monotonic()
        watermark, _status = self._state(folder.folder_id)
        if watermark is None:
            watermark = int(self.clock()) - self.window_s
            kept = await self._window(folder, watermark, None, counters, progress)
            resumed = False
        else:
            kept = 1  # resume below the last committed window
            resumed = True
        while True:
            if not kept:
                top = await self._probe(folder, watermark)
                if top is None:
                    with self.store.transaction():
                        self._replace_rows(folder.folder_id, None, watermark, _WindowRows())
                        self._set_state(folder.folder_id, watermark, (), STATUS_DONE)
                        if resumed and counters == Counters():
                            # Nothing was left below the last committed window: a row would
                            # carry only paging/probe seconds and skew the backfill rate (L6).
                            log.debug(
                                "resumed backfill found nothing left: no run row",
                                extra={"folder": folder.hash},
                            )
                        else:
                            self._write_run(folder, counters, time.monotonic() - t0)
                    progress.folder_done(folder.folder_id)
                    return counters
                jump = top + 1
                with self.store.transaction():
                    self._replace_rows(folder.folder_id, jump, watermark, _WindowRows())
                    self._set_state(folder.folder_id, jump, (), STATUS_BACKFILL)
                watermark = jump
            lo = watermark - self.window_s
            kept = await self._window(folder, lo, watermark, counters, progress)
            watermark = lo

    async def _revisit_headers_only(
        self, folder: FolderInfo, progress: _Progress
    ) -> Counters | None:
        """A done folder: ingest the items of its ``headers_only_items`` rows that TB has since
        downloaded (module docstring). The seconds holding rows are coalesced into ranges no
        wider than one window, each paged once (``[lo, hi + 1]``). Of a range's items only those
        with ``lo <= tb_date <= hi`` (a second above ``hi`` is the next newer range's) and of
        these only the following are handled: items still headers-only at such a second (their
        rows are re-recorded); a non-``md5:`` item whose ``mid_of(hmid)`` has a row at its
        second; an ``md5:`` item at a second holding a NULL-mid row (ID-less items cannot be
        matched to their row, so every downloaded ``md5:`` item at that second is fetched again;
        re-writing one is idempotent).
        Rows are then replaced per second; ``skipped_items`` only at seconds whose ``md5:`` items
        were all handled (elsewhere the rows stay and an item skipped here gets one appended).
        Writes one ``kind='hourly'`` ``ingest_runs`` row (the job is reconcile's, plan step 8),
        so ``backfill`` rows keep the backfill rate clean."""
        by_date: dict[int, set[str | None]] = {}
        for d, mid in self.store.connection.execute(
            "SELECT tb_date, mid FROM headers_only_items WHERE folder_id = ?",
            (folder.folder_id,),
        ):
            by_date.setdefault(int(d), set()).add(mid)
        if not by_date:
            return None

        def wanted(item: PageItem) -> bool:
            mids = by_date.get(tb_date_of(item.date))
            if mids is None:
                return False
            if item.headers_only:
                return True
            if item.header_message_id.startswith(TB_MD5_PREFIX):
                return None in mids
            return mid_of(item.header_message_id) in mids

        counters = Counters()
        t0 = time.monotonic()
        for lo, hi in coalesce_dates(by_date, self.window_s):
            items = await self.page_all(folder, lo, hi + 1)
            # The query reaches hi + 1 (whole seconds; a TB date may carry a fraction), but a
            # second above hi belongs to the next newer range: handled there, never twice.
            mine = [i for i in items if lo <= tb_date_of(i.date) <= hi and wanted(i)]
            rows = await self.process(folder, mine, counters, progress)
            with self.store.transaction():
                for d in sorted(d for d in by_date if lo <= d <= hi):
                    all_md5 = None in by_date[d]
                    self._replace_rows(
                        folder.folder_id,
                        d,
                        d + 1,
                        _WindowRows(
                            headers_only=[r for r in rows.headers_only if r[0] == d],
                            skipped=[s for s in rows.skipped if s[0] == d],
                        ),
                        skipped=all_md5,
                    )
        with self.store.transaction():
            self._write_run(folder, counters, time.monotonic() - t0, "hourly")
        return counters

    async def run(self, on_batch: Callable[[BatchReport], None] | None = None) -> IngestReport:
        """Backfill (or resume) every indexed folder, one at a time; ``on_batch`` gets a
        :class:`BatchReport` after every ``batch_size`` items and one at the end."""
        t0 = time.monotonic()
        folders = await self.list_folders()
        done = {f.folder_id for f in folders if self._state(f.folder_id)[1] == STATUS_DONE}
        progress = _Progress(
            {f.folder_id: f.total for f in folders if f.folder_id not in done},
            self.batch_size,
            on_batch,
            folders_total=len(folders),
            folders_done=len(done),
        )
        totals = Counters()
        for folder in folders:
            if folder.folder_id not in done:
                totals.add(await self._backfill_folder(folder, progress))
        for folder in folders:
            if folder.folder_id in done:
                revisit = await self._revisit_headers_only(folder, progress)
                if revisit is not None:
                    totals.add(revisit)
        progress.finish()
        secs = time.monotonic() - t0
        log.info(
            "backfill run done",
            extra={
                "folders": len(folders),
                "folders_done": progress.folders_done,
                "items": progress.items,
                "n": totals.n_msgs,
                "secs": round(secs, 3),
            },
        )
        return IngestReport(
            folders_total=len(folders),
            folders_done=progress.folders_done,
            items=progress.items,
            batches=progress.batches,
            secs=secs,
            counters=totals,
        )
