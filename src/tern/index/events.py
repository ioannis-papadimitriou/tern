"""``EventIngest``: TB change events applied to the index (plan 04 step 8; architecture §2 step 3,
#16, #30).

``on_event(method, params)`` takes the 1.0 notifications as FakeBridge and the bridge deliver
them:

- ``event.mail.new`` → each item is ingested through the shared per-item path
  (``tern.index.ingest.ItemIngester``, source ``'event'``): a downloaded item is always fetched
  and written, even when its mid is already located in the folder (the Store collapses a
  byte-identical copy and records a differing one in ``same_folder_copies``, so a same-folder
  spoof is conflict-checked); headers-only items get a ``headers_only_items`` row, also when the
  mid is already located there (none when a row of that mid at that tb_date exists), so a
  differing copy that TB downloads later, without an event, is found by reconcile; items over
  RAW_MAX without a Message-ID, and items TB fails to read (``tb_error`` from ``raw``/``full``,
  plan 04 merge review M5), a ``skipped_items`` row;
- ``event.mail.moved`` → the items are ingested into the destination first, then the source
  location is resynced (removed when TB no longer holds a copy there), so a message never loses
  its last location half-way through a move; an item with no location in the source drops its
  source ``headers_only_items`` row (by mid at its tb_date; for ``md5:`` one NULL-mid row at its
  tb_date, only when the item is headers-only). A downloaded ``md5:`` item whose mid cannot be
  learned (its copy went stale) is unresolved: no row is guessed at, the source's hourly check
  repairs it;
- ``event.mail.deleted`` → the location is resynced (removed when TB holds no copy there); a
  Message-ID tracked only in ``headers_only_items`` loses that row;
- ``event.mail.updated`` → ``Store.set_location_state`` (observers fire only on a change); a
  headers-only item has no location state: nothing to do;
- ``event.folder.changed`` → accounts and folders are re-listed (:meth:`EventIngest.sync_folders`:
  new folder ids are backfilled first, then vanished ids (left out of the listing and paged as
  ``folder_not_found``) lose their locations with hook ``None``; with a backfill job
  attached, both are handed to it while any indexed folder is not ``done``), then that
  folder's hourly check runs. A listing with no accounts, or a listed account with no folders,
  while the index holds folders for it raises ``IngestError`` before anything is written
  (mass-removal guard, plan 04 merge review M4).

Events for excluded folders (trash, junk, …: ``tern.index.ingest.is_indexed``) are ignored and
counted at DEBUG; a move into one removes the source location. An event naming a folder the
index has never listed re-lists the folders first.

**Resolution (no stored TB ids, #5).** A non-``md5:`` headerMessageId resolves to
``mid_of(hmid)``, looked up by (mid, folder_id) in ``locations``, then in
``headers_only_items``. Headers-only items are never fetched (Decision 8: ``getRaw`` would hit
the network). Otherwise:

- a moved item with an ``md5:`` id is resolved by its own ``tb_id`` (``raw``; its destination
  copy, already fetched when the destination is indexed), unless it is headers-only;
- an ``md5:`` update is resolved by finding its PageItem at the folder's ID-less tb_dates
  (:meth:`CopyFetcher.find_id_less`; ``UpdatedItem`` has no ``headers_only``): headers-only →
  nothing to do; downloaded → ``raw`` → mid;
- a delete is never resolved by ``find`` + ``raw``: a deleted message cannot be found, and any
  match is another copy that still exists (possibly headers-only);
- whatever stays unresolved (an ``md5:`` delete, an ``md5:`` update not found, a downloaded
  ``md5:`` move not located in its source, a Message-ID the index does not hold in the folder)
  counts in ``unresolved_events`` and makes that folder's
  hourly check run now, after the event (only once its backfill is done).

Resync fetches raw for every TB copy of a folder that has ``same_folder_copies`` rows of the mid
(to drop the copies that went away), and for the ``find`` matches of an ``md5:`` move.

**Remaining headers-only copies.** A Message-ID copy deleted (or moved away) from a folder
where ``find`` still lists copies of it, and no ``same_folder_copies`` rows exist, keeps its
location only when one of those copies is downloaded. ``find`` reports neither
``headers_only`` nor a date, so their PageItems are paged (the location's second first, then
the folder's windows until all are seen; nothing is fetched, Decision 8). When every one is
headers-only, the location's own copy has a ``headers_only_items`` row (at the location's
tb_date: reconcile records one for an evicted copy) and the folder's rows of the Message-ID
(before the event) outnumber them, a recorded headers-only copy is the one that left: the
location stood on headers-only copies alone (evicted content kept by reconcile, below) and
stays, its rows following TB's remaining copies; unless a move (it carries the item's date)
took away a headers-only item at the location's tb_date that no remaining copy shares: the
evicted copy itself left. Otherwise the copy that left was the folder's last downloaded
one and the indexed text is its own (every copy written there had the location's hash), so the
location goes (a spoof delivered with the Message-ID of a headers-only genuine copy, then
deleted, leaves no text) and each remaining copy gets a ``headers_only_items`` row
(:meth:`EventIngest._drop_to_headers_only`).

**Evicted bodies (#30: keep, user decision 2026-11-15).** TB evicting a message's offline body
(its copies in a folder turn headers-only) fires no event; reconcile sees it and keeps the
location with its indexed content and last known state, for Message-ID and x- locations alike
(:meth:`EventIngest.record_headers_only` records a ``headers_only_items`` row per Message-ID
copy, as a re-page does; an x- location gets no row: reconcile's per-second ID-less count
matches one x- location to one headers-only ``md5:`` item, and an ``md5:`` update of a
headers-only item does nothing). A fresh backfill would hold only the rows (Decision 8: a
headers-only item is never fetched), so a live index and a rebuilt one differ there, and a
spoof delete that the event path did not apply (event lost, or unresolved) cannot be told
from an eviction by reconcile: its text stays (see Known limitations).

**Removals and refills.** Every location removal goes through :meth:`EventIngest.remove_location`:
when the removal leaves no copy holding the stored content, one of ``Store.refill_candidates`` is
fetched (:meth:`CopyFetcher.fetch_copy`) and passed as the refill, so a deleted spoof's text
never stays indexed; if none can be fetched, the content is cleared (``refill_pending``) and
reconcile re-fills it later.

**Buffering (#16 S1).** While a reconcile pass runs over a folder (:meth:`EventIngest.paused`),
events touching that folder are queued, and applied in arrival order after the pass (only when
it ends normally: a pass that raises, or is cancelled, leaves them queued for the next pass that
ends normally, so the pass's exception is never replaced by an event's). The pass
asks :attr:`EventIngest.buffered` whether a change it finds is covered by a queued event (then
it leaves the change to the event and does not count it as missed). One ``asyncio.Lock``
serializes event application, so events keep their order on each folder; hourly checks an event
asks for run after the lock is released. Events left queued by a pass that raised are applied
before the next event (:meth:`EventIngest.on_event`); when one of them fails there, it is
dropped (reconcile repairs its change), the arriving event is still applied unless an older
queued event touches one of its folders (then it is queued behind it, keeping the order), and
the queued event's failure is raised afterwards, with its folders in
:data:`failed_event_folders` for the caller's report.

Each applied event writes one ``ingest_runs`` row (``kind='event'``; counters of the per-item
path plus ``unresolved_events``). Coverage of applied events is kept only for folders whose
backfill is done (no pass runs before that, and stale coverage would hide genuine missed
changes from the first pass after it). Logs carry mids, counts and #28 hashes only.

Known limitations:

- a Message-ID the index holds under another mid (``hmid_mismatch``) cannot be resolved without
  fetching, so its updates and deletes count as ``unresolved_events`` and run the hourly check
  (the plan's Risk: those counts inflate where TB's hmid differs from the raw header);
- an ``md5:`` delete of a headers-only item is unresolved (its NULL-mid row is dropped by the
  hourly check it triggers);
- **Remaining headers-only copies**: a ``find`` match not seen in any page leaves the event
  unresolved; the hourly check it runs keeps the location (it sees an eviction, see the last
  item). A match counts as a downloaded copy by its TB hmid alone: a downloaded item whose TB
  hmid equals the removed copy's but whose raw Message-ID differs (``hmid_mismatch``, rare)
  keeps the location and its text (confirming would fetch every remaining downloaded copy on
  every such delete). Paging starts at the location's second but may page every window of the
  folder, under the event lock, when the match sits elsewhere (one ``page`` per window, no
  fetch): a cost for step 9 to weigh on large IMAP folders;
- resync of a mid with ``same_folder_copies`` rows, and of an ``md5:`` move's source copy,
  fetches every ``find`` match in the folder, and ``find`` reports no ``headers_only``: a
  headers-only extra copy there would be fetched (copy fetches exclude them,
  :meth:`CopyFetcher.fetch_copy`);
- a new folder found by the hourly/daily re-list (in the job task, so never beside the backfill
  job) is backfilled inline, while the event lock is held: every event (and every pass's
  :meth:`EventIngest.paused`) waits for it. One found by an event is handed to the backfill job
  when :attr:`EventIngest.backfill_job` is set (``IngestService``); a standalone
  ``EventIngest`` (no job to race) still backfills it inline in the event's task. A renamed
  folder whose new id cannot be backfilled (a failure other than ``unavailable``) keeps its old
  id's locations only until the hourly re-list (``"inline"`` mode), which removes them, as
  before;
- this module uses private names of ``tern.index.ingest`` (``Backfill._backfill_folder``,
  ``_Progress``), and ``reconcile.py`` uses ``_WindowRows`` and ``Backfill._replace_rows``:
  making them public needs ``ingest.py``, which step 8 does not list (a later step that lists it
  should rename them, recorded as a deviation);
- a folder whose id stays the same but which becomes excluded (its ``special_use`` changes)
  loses its locations only through :meth:`EventIngest.sync_folders` (``folder.changed`` or the
  hourly re-list), not through mail events;
- ``md5:`` items of a move: the source copy is matched by the raw-derived mid of the destination
  copy; an ID-less copy whose headers equal another's but whose body differs is resolved by
  fetching every same-folder ``find`` match;
- refill fetches are not throttled (``ingest_max_msgs_per_s``); they are rare (one per removed
  content copy);
- a headers-only item gets no second row when its mid already has one at its tb_date (so a
  replayed event adds none): two headers-only copies of one Message-ID at one second share a row
  until a re-page of their window rebuilds the rows; a headers-only copy of a located
  Message-ID that is deleted or moved away keeps its row (the location is resynced, the row is
  not), and a replayed or stale new-mail event that still says headers-only for an item the
  index holds downloaded adds one (it cannot be told from a header-first spoof without TB):
  both are counted in ``index status`` until the folder's next reconcile pass drops rows TB does
  not back (``tern.index.reconcile``, "Stale headers-only rows");
- evicted content is kept (#30, user decision 2026-11-15): a location whose items in the folder
  all turned headers-only with no event keeps its content and its last known state
  (tags/flagged/read change only by events; the daily pass skips headers-only items), while a
  fresh backfill would hold only ``headers_only_items`` rows. Reconcile cannot tell an eviction
  from a delete of the folder's last downloaded copy whose event the event path did not apply
  (lost, or unresolved: a ``find`` match seen in no page) while headers-only copies remain (e.g.
  a deleted spoof of a headers-only genuine copy): that copy's text stays indexed, and the index
  differs from the one the event would have left. Telling them apart would need the
  headers-only copy's body (Decision 8). Pinned by
  ``tests/index/test_reconcile.py::test_eviction_keeps_message_id_and_x_locations`` and
  ``tests/index/test_events.py::test_hourly_cannot_tell_a_dropped_spoof_delete_from_an_eviction``;
- a delete or move of one copy of a kept evicted location ("Remaining headers-only copies") is
  told from a spoof's by the ``headers_only_items`` row at the location's own tb_date and by
  counting the folder's rows of the Message-ID, which reconcile records for an eviction: before
  the folder's next pass has seen the eviction, a delete of a headers-only duplicate drops the
  location as a spoof delete would; and a delete event carries no date, so a delete of the
  evicted copy itself while a recorded headers-only duplicate remains keeps the location and
  its text (a move is told apart by the moved item's date, unless a remaining copy shares the
  location's second). Pinned by
  ``test_deleting_a_headers_only_duplicate_of_an_evicted_location_keeps_it`` and
  ``test_moving_the_evicted_copy_away_from_its_headers_only_duplicate_drops_its_text``;
- an evicted, kept message moved to another folder loses its kept content: its source location
  goes (``find`` lists no copy there) and the destination copy is headers-only, so it gets only
  a ``headers_only_items`` row and is never fetched (Decision 8). Carrying the content to the
  destination is tracked in ``plans/90-post-mvp.md`` (plan 04 M2 review). Pinned by
  ``test_moving_an_evicted_message_drops_its_kept_content``;
- a moved message TB cannot read in its destination (``tb_error``, plan 04 M5) gets a
  ``tb_error`` row there and its source location is still resynced: TB no longer lists it in
  the source, so that location goes, and when it was the message's last location the message
  is dropped (removal observers fire) until a pass of the destination reads it and adds it back
  (a retry, not missed). Keeping the source location instead would only defer the same removal
  to the source's hourly check, which knows nothing of the destination. Pinned by
  ``test_a_moved_message_unreadable_in_its_destination_drops_until_the_retry_reads_it``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from tern.bridge.protocol import (
    RAW_MAX,
    BridgeError,
    Folder,
    FolderChanged,
    MailDeleted,
    MailMoved,
    MailNew,
    MailUpdated,
    PageItem,
)
from tern.config import TernConfig
from tern.index.ingest import (
    DAY_S,
    SKIP_REASONS,
    STATUS_DONE,
    Backfill,
    Counters,
    FolderInfo,
    IngestError,
    ItemIngester,
    _Progress,
    coalesce_dates,
    is_indexed,
    record_from_full,
    record_from_raw,
    tb_date_of,
)
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
from tern.index.shorthash import assign, folder_name
from tern.index.source import IngestSource
from tern.index.store import (
    IngestRun,
    LocationState,
    MessageRecord,
    Source,
    Store,
    StoreError,
)

if TYPE_CHECKING:
    from tern.index.reconcile import Reconcile

log = logging.getLogger(__name__)

#: The one instant :meth:`EventIngest._confirmed_gone` pages a folder over.
_PROBE_DATE = datetime(1970, 1, 1, tzinfo=UTC)

#: How :meth:`EventIngest.sync_folders` treats new folders and removals (see its docstring).
SyncMode = Literal["inline", "defer", "job"]

#: Set (in the calling task's context) when :meth:`EventIngest.on_event` raises the failure of
#: an older queued event it drained before the arriving one: that event's folder ids, so the
#: caller's failure report names them rather than the arriving event's. Left unset when the
#: failure raised is the arriving event's own (or that of an hourly check it asked for).
failed_event_folders: ContextVar[frozenset[str] | None] = ContextVar(
    "tern_failed_event_folders", default=None
)

HEADERS_ONLY = "headers_only_items"
SKIPPED = "skipped_items"

EventParams = MailNew | MailMoved | MailDeleted | MailUpdated | FolderChanged
_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "event.mail.new": MailNew,
    "event.mail.moved": MailMoved,
    "event.mail.deleted": MailDeleted,
    "event.mail.updated": MailUpdated,
    "event.folder.changed": FolderChanged,
}


def hmid_mid(header_message_id: str) -> str | None:
    """``mid_of`` a TB headerMessageId; None for TB's ``md5:`` ids (and ids normalizing to
    nothing): those items are told apart only by fetching them."""
    if header_message_id.startswith(TB_MD5_PREFIX):
        return None
    return mid_of(header_message_id)


def state_of(tags: Iterable[str], read: bool, flagged: bool) -> LocationState:
    return LocationState(tags=tuple(tags), flagged=flagged, read=read)


# ---- coverage ----------------------------------------------------------------------------------


@dataclass
class Coverage:
    """The location changes some events account for. A change a reconcile pass finds that is
    covered is not a missed change. ``mids`` covers (folder_id, mid); ``dates`` covers the
    ID-less items (x- mids) of a folder at one tb_date; ``folders`` covers every ID-less item of
    a folder (an ``md5:`` delete or update has no date)."""

    mids: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    dates: set[tuple[str, int]] = field(default_factory=set[tuple[str, int]])
    folders: set[str] = field(default_factory=set[str])

    def add(self, other: Coverage) -> None:
        self.mids |= other.mids
        self.dates |= other.dates
        self.folders |= other.folders

    def covers(self, folder_id: str, mid: str | None, tb_date: int | None) -> bool:
        """``mid`` None: an ID-less item whose mid is not known (yet)."""
        if mid is not None and (folder_id, mid) in self.mids:
            return True
        if mid is not None and not mid.startswith(X_PREFIX):
            return False
        return folder_id in self.folders or (
            tb_date is not None and (folder_id, tb_date) in self.dates
        )

    def take(self, folder_id: str) -> Coverage:
        """Remove and return the entries of ``folder_id``."""
        out = Coverage(
            mids={k for k in self.mids if k[0] == folder_id},
            dates={k for k in self.dates if k[0] == folder_id},
            folders={folder_id} & self.folders,
        )
        self.mids -= out.mids
        self.dates -= out.dates
        self.folders -= out.folders
        return out

    def note_item(self, folder_id: str, header_message_id: str, tb_date: int | None) -> None:
        m = hmid_mid(header_message_id)
        if m is not None:
            self.mids.add((folder_id, m))
        elif tb_date is not None:
            self.dates.add((folder_id, tb_date))
        else:
            self.folders.add(folder_id)


def _typed(method: str, params: BaseModel) -> EventParams:
    model = _EVENT_MODELS.get(method)
    if model is None:
        raise ValueError(f"{method} is not an index event")
    if not isinstance(params, model):
        raise TypeError(f"{method} params are {type(params).__name__}, not {model.__name__}")
    assert isinstance(params, MailNew | MailMoved | MailDeleted | MailUpdated | FolderChanged)
    return params


def event_folders(params: EventParams) -> set[str]:
    match params:
        case MailNew() | MailDeleted() | FolderChanged():
            return {params.folder_id}
        case MailMoved():
            return {params.src_folder_id, params.dst_folder_id}
        case MailUpdated():
            return {i.folder_id for i in params.items}


def event_coverage(params: EventParams) -> Coverage:
    """What a queued event will change, as far as it is known without the network."""
    cov = Coverage()
    match params:
        case MailNew():
            for i in params.items:
                cov.note_item(params.folder_id, i.header_message_id, tb_date_of(i.date))
        case MailMoved():
            for i in params.items:
                for f in (params.src_folder_id, params.dst_folder_id):
                    cov.note_item(f, i.header_message_id, tb_date_of(i.date))
        case MailDeleted():
            for h in params.header_message_ids:
                cov.note_item(params.folder_id, h, None)
        case MailUpdated():
            for u in params.items:
                cov.note_item(u.folder_id, u.header_message_id, None)
        case FolderChanged():
            pass
    return cov


# ---- folders -------------------------------------------------------------------------------------


def folder_infos(store: Store) -> tuple[dict[str, FolderInfo], set[str]]:
    """(indexed folders by id, every listed folder id) as the index knows them; hashes per #28
    over every listed folder. ``total`` is the last reconciled ``tb_total``, 0 before the first
    pass (it only sizes progress reports, never a count that is stored or compared)."""
    accounts = {
        str(r[0]): (str(r[1]), str(r[2]))
        for r in store.connection.execute("SELECT account_id, name, type FROM accounts")
    }
    folders = store.folders()
    missing = {f.account_id for f in folders} - set(accounts)
    if missing:
        raise StoreError(f"{len(missing)} folder account(s) missing from the index")
    names = {f.folder_id: folder_name(accounts[f.account_id][0], f.path) for f in folders}
    hashes = assign(names.values(), "fold")
    indexed = {
        f.folder_id: FolderInfo(
            folder_id=f.folder_id,
            account_id=f.account_id,
            account_type=accounts[f.account_id][1],
            special_use=f.special_use,
            hash=hashes[names[f.folder_id]],
            total=0 if f.tb_total is None else f.tb_total,
        )
        for f in folders
        if is_indexed(f.special_use)
    }
    return indexed, {f.folder_id for f in folders}


def folder_windows(
    store: Store, folder_id: str, now: int, window_s: int
) -> list[tuple[int | None, int | None]]:
    """A folder's windows ``(lo, hi)`` (``lo <= tb_date < hi``, None = open), newest first:
    ``[now - W, open)``, then ``[lo - W, lo)`` until past the oldest tb_date the index holds for
    the folder, then the open-ended tail."""
    row = store.connection.execute(
        "SELECT min(d) FROM (SELECT min(tb_date) AS d FROM locations WHERE folder_id = ? "
        "UNION ALL SELECT min(tb_date) FROM headers_only_items WHERE folder_id = ? "
        "UNION ALL SELECT min(tb_date) FROM skipped_items WHERE folder_id = ?)",
        (folder_id, folder_id, folder_id),
    ).fetchone()
    oldest = None if row[0] is None else int(row[0])
    lo = now - window_s
    out: list[tuple[int | None, int | None]] = [(lo, None)]
    while oldest is not None and lo > oldest:
        out.append((lo - window_s, lo))
        lo -= window_s
    out.append((None, lo))
    return out


def backfill_status(store: Store, folder_id: str) -> str | None:
    row = store.connection.execute(
        "SELECT status FROM ingest_state WHERE folder_id = ?", (folder_id,)
    ).fetchone()
    return None if row is None else str(row[0])


def is_located(store: Store, mid: str, folder_id: str) -> bool:
    return (
        store.connection.execute(
            "SELECT 1 FROM locations WHERE mid = ? AND folder_id = ?", (mid, folder_id)
        ).fetchone()
        is not None
    )


def has_copies(store: Store, mid: str, folder_id: str) -> bool:
    return (
        store.connection.execute(
            "SELECT 1 FROM same_folder_copies WHERE mid = ? AND folder_id = ? LIMIT 1",
            (mid, folder_id),
        ).fetchone()
        is not None
    )


def has_headers_only_row(
    store: Store, folder_id: str, mid: str, tb_date: int | None = None
) -> bool:
    """A ``headers_only_items`` row of ``mid`` in the folder (at ``tb_date`` when given)."""
    cond, args = "folder_id = ? AND mid = ?", (folder_id, mid)
    if tb_date is not None:
        cond, args = cond + " AND tb_date = ?", (*args, tb_date)
    return (
        store.connection.execute(
            f"SELECT 1 FROM headers_only_items WHERE {cond} LIMIT 1", args
        ).fetchone()
        is not None
    )


def drop_headers_only_row(
    store: Store, folder_id: str, mid: str | None, tb_date: int | None
) -> bool:
    """Delete one ``headers_only_items`` row of a headers-only item that left the folder: a row
    of ``mid`` (a non-``md5:`` id; at ``tb_date`` when given, else its oldest: a delete event
    carries no date, and several headers-only copies of one Message-ID cannot be told apart
    then), else one NULL-mid row at ``tb_date`` (an ``md5:`` id; same-second ID-less items
    cannot be told apart, as in ``ItemIngester``). Ordinals may keep a gap (as after a step-7
    drop); a re-page renumbers. Returns True when a row went."""
    if mid is None and tb_date is None:
        return False
    if mid is not None and tb_date is not None:
        cond, args = "mid = ? AND tb_date = ?", (folder_id, mid, tb_date)
    elif mid is not None:
        cond, args = "mid = ?", (folder_id, mid)
    else:
        cond, args = "tb_date = ? AND mid IS NULL", (folder_id, tb_date)
    with store.transaction():
        cur = store.connection.execute(
            "DELETE FROM headers_only_items WHERE rowid = (SELECT rowid FROM headers_only_items "
            f"WHERE folder_id = ? AND {cond} ORDER BY tb_date, ordinal LIMIT 1)",
            args,
        )
    return cur.rowcount > 0


def add_aux_row(
    store: Store,
    table: str,
    folder_id: str,
    tb_date: int,
    mid: str | None,
    *,
    reason: str | None = None,
) -> None:
    """Append one ``headers_only_items`` (``mid``) or ``skipped_items`` (``reason``, required
    there) row, ordinal = next at that tb_date (an event's single item; a reconcile re-page
    replaces a window's rows)."""
    if (table == SKIPPED) != (reason is not None):
        raise ValueError("a reason is given for skipped_items rows only")
    with store.transaction():
        conn = store.connection
        if table == HEADERS_ONLY:
            n = conn.execute(
                "SELECT coalesce(max(ordinal) + 1, 0) FROM headers_only_items "
                "WHERE folder_id = ? AND tb_date = ?",
                (folder_id, tb_date),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO headers_only_items(folder_id, tb_date, ordinal, mid) "
                "VALUES (?, ?, ?, ?)",
                (folder_id, tb_date, n, mid),
            )
        elif table == SKIPPED:
            n = conn.execute(
                "SELECT coalesce(max(ordinal) + 1, 0) FROM skipped_items "
                "WHERE folder_id = ? AND tb_date = ?",
                (folder_id, tb_date),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO skipped_items(folder_id, tb_date, ordinal, reason) "
                "VALUES (?, ?, ?, ?)",
                (folder_id, tb_date, n, reason),
            )
        else:
            raise ValueError("not an auxiliary item table")


# ---- copy fetching -------------------------------------------------------------------------------


class CopyFetcher:
    """Fetch one known copy of a message again (no TB id is stored, #5): for a refill or a
    pending refill. The copy is looked for among the folder's downloaded items at its location's
    tb_date, then (Message-ID mids, when none of those hashed to it: a same-folder copy has no
    tb_date of its own) by ``find``; it must hash to the wanted content_hash. Headers-only items
    are never fetched (Decision 8): ``find`` reports no ``headers_only``, so the tb_ids of the
    mid's headers-only items in the folder are learned by paging their recorded tb_dates."""

    def __init__(self, store: Store, source: IngestSource, backfill: Backfill) -> None:
        self.store = store
        self.source = source
        self.backfill = backfill

    async def fetch_copy(self, mid: str, folder_id: str, wanted: str) -> MessageRecord | None:
        tried: set[int] = set()
        row = self.store.connection.execute(
            "SELECT tb_date FROM locations WHERE mid = ? AND folder_id = ?", (mid, folder_id)
        ).fetchone()
        info = folder_infos(self.store)[0].get(folder_id)
        if row is not None and info is not None:
            d = int(row[0])
            # [d, d + 1] inclusive: an item dated d plus a fraction of a second is floored to d
            for it in await self.backfill.page_all(info, d, d + 1):
                if tb_date_of(it.date) != d or it.headers_only:
                    continue
                m = hmid_mid(it.header_message_id)
                if m == mid or (m is None and mid.startswith(X_PREFIX)):
                    tried.add(it.tb_id)
                    rec = await self._record(it.tb_id, it.size, mid, wanted)
                    if rec is not None:
                        return rec
        msg = self.store.get_message(mid)
        if msg is not None and msg.header_message_id is not None:
            held = set() if info is None else await self._headers_only_tb_ids(info, mid)
            for f in await self.source.find(msg.header_message_id):
                if f.folder_id != folder_id or f.tb_id in tried or f.tb_id in held:
                    continue
                tried.add(f.tb_id)
                rec = await self._record(f.tb_id, None, mid, wanted)
                if rec is not None:
                    return rec
        log.info("copy not fetched", extra={"mid": mid, "candidates": len(tried)})
        return None

    async def _headers_only_tb_ids(self, info: FolderInfo, mid: str) -> set[int]:
        """tb_ids of the folder's headers-only items whose hmid gives ``mid``, at the tb_dates
        ``headers_only_items`` records for it."""
        dates = [
            int(r[0])
            for r in self.store.connection.execute(
                "SELECT DISTINCT tb_date FROM headers_only_items WHERE folder_id = ? AND mid = ?",
                (info.folder_id, mid),
            )
        ]
        out: set[int] = set()
        for lo, hi in coalesce_dates(dates, DAY_S):
            for it in await self.backfill.page_all(info, lo, hi + 1):
                if it.headers_only and hmid_mid(it.header_message_id) == mid:
                    out.add(it.tb_id)
        return out

    async def page_items(
        self, info: FolderInfo, tb_ids: set[int], first: Iterable[int]
    ) -> dict[int, PageItem] | None:
        """The PageItems of ``tb_ids`` (``find`` matches in ``info``: ``find`` reports neither
        ``headers_only`` nor a date). Looked for at the seconds ``first`` (each paged
        ``[d, d + 1]``), then window by window, newest first (:func:`folder_windows`), stopping
        once every one is seen; nothing is fetched. None when one is never seen (it went away
        between ``find`` and the pages)."""
        seen: dict[int, PageItem] = {}

        def take(items: Iterable[PageItem]) -> bool:
            for it in items:
                if it.tb_id in tb_ids:
                    seen[it.tb_id] = it
            return len(seen) == len(tb_ids)

        for lo, hi in coalesce_dates(first, DAY_S):
            if take(await self.backfill.page_all(info, lo, hi + 1)):
                return seen
        now = int(self.backfill.clock())
        for lo, hi in folder_windows(self.store, info.folder_id, now, self.backfill.window_s):
            if take(await self.backfill.page_all(info, lo, hi)):
                return seen
        log.info("find match not seen in any page", extra={"folder": info.hash})
        return None

    async def find_id_less(self, info: FolderInfo, tb_id: int) -> PageItem | None:
        """The PageItem of an ID-less (``md5:``) TB item of ``info``, looked for at the tb_dates
        where the index holds ID-less rows of the folder (x- locations, NULL-mid headers-only
        rows, skipped rows); None when it is not among them (unknown to the index, or stale)."""
        fid = info.folder_id
        dates = [
            int(r[0])
            for r in self.store.connection.execute(
                "SELECT tb_date FROM locations WHERE folder_id = ? AND mid LIKE 'x-%' UNION "
                "SELECT tb_date FROM headers_only_items WHERE folder_id = ? AND mid IS NULL UNION "
                "SELECT tb_date FROM skipped_items WHERE folder_id = ?",
                (fid, fid, fid),
            )
        ]
        for lo, hi in coalesce_dates(dates, DAY_S):
            for it in await self.backfill.page_all(info, lo, hi + 1):
                if it.tb_id == tb_id and it.header_message_id.startswith(TB_MD5_PREFIX):
                    return it
        return None

    async def _record(
        self, tb_id: int, size: int | None, mid: str, wanted: str
    ) -> MessageRecord | None:
        try:
            if not wanted.startswith(FULL_PREFIX):
                if size is not None and size > RAW_MAX:
                    return None
                raw = await self.source.raw(tb_id)
                if content_hash(raw) == wanted and mid_from_raw(raw) == mid:
                    # Parsing is CPU work (bounded, T13): off the event loop.
                    return await asyncio.to_thread(record_from_raw, raw)
                return None
            if size is None:  # the full path needs the PageItem size (never invented)
                return None
            full = await self.source.full(tb_id)
        except BridgeError as exc:
            if exc.name not in ("stale_id", "too_large", "tb_error"):
                raise
            if exc.name == "tb_error":  # this candidate is unreadable: try the next one
                log.info("copy candidate not read", extra={"mid": mid, "tb_code": exc.tb_code})
            return None
        if content_hash_full(full) == wanted and mid_from_full(full) == mid:
            return await asyncio.to_thread(record_from_full, mid, full, size)
        return None

    async def mid_of_tb(self, tb_id: int, size: int | None = None) -> str | None:
        """The raw-derived mid of a TB message (``full`` above RAW_MAX); None when it went
        stale, TB cannot read it (``tb_error``, plan 04 M5) or it has no mid on the full path.
        Every caller takes None as "unknown" (the event is unresolved, the daily pass leaves
        that item's state), never as "gone"."""
        try:
            if size is None or size <= RAW_MAX:
                try:
                    return mid_from_raw(await self.source.raw(tb_id))
                except BridgeError as exc:
                    if exc.name != "too_large":
                        raise
            return mid_from_full(await self.source.full(tb_id))
        except BridgeError as exc:
            if exc.name not in ("stale_id", "tb_error"):
                raise
            if exc.name == "tb_error":
                log.info("item not read: its mid is unknown", extra={"tb_code": exc.tb_code})
            return None

    async def hash_of_tb(self, tb_id: int) -> tuple[str, str] | None:
        """(mid, content_hash) of a TB message; None when it went stale or has no mid. A
        ``tb_error`` raises: a caller takes None as "not this copy" and may remove a location."""
        try:
            try:
                raw = await self.source.raw(tb_id)
            except BridgeError as exc:
                if exc.name != "too_large":
                    raise
            else:
                return mid_from_raw(raw), content_hash(raw)
            full = await self.source.full(tb_id)
        except BridgeError as exc:
            if exc.name != "stale_id":
                raise
            return None
        m = mid_from_full(full)
        return None if m is None else (m, content_hash_full(full))


# ---- events ------------------------------------------------------------------------------------


@dataclass
class _Outcome:
    folder: FolderInfo | None = None
    unresolved: int = 0
    checks: set[str] = field(default_factory=set[str])
    ignored: int = 0


class EventIngest:
    """Apply TB change events to the index; see the module docstring."""

    def __init__(
        self,
        store: Store,
        source: IngestSource,
        cfg: TernConfig,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.source = source
        self.cfg = cfg
        self.clock = clock
        self.backfill = Backfill(store, source, cfg, clock=clock)
        self.items: ItemIngester = self.backfill.items
        self.fetcher = CopyFetcher(store, source, self.backfill)
        #: Set by ``Reconcile``: runs the hourly check an event asks for.
        self.reconcile: Reconcile | None = None
        #: Set by ``IngestService``: asks its job task to backfill the folders not ``done`` and
        #: then re-list (:meth:`sync_folders` in ``"job"`` mode); called when an event's re-list
        #: finds folder ids new to the index. When set, an event's re-list never backfills and,
        #: while a folder is not ``done``, never removes (``"defer"``), so no event task
        #: backfills beside the backfill job.
        self.backfill_job: Callable[[], None] | None = None
        #: Changes covered by events queued during a pass (read by the pass).
        self.buffered = Coverage()
        #: Events ignored (excluded or vanished folders), for DEBUG counts and tests.
        self.ignored = 0
        self._applied = Coverage()
        self._lock = asyncio.Lock()
        self._paused: dict[str, int] = {}
        self._queue: list[EventParams] = []
        #: The queued event whose application failed in the last :meth:`_drain_ready`.
        self._drain_failed: EventParams | None = None

    # -- entry points ----------------------------------------------------------------------------

    async def on_event(self, method: str, params: BaseModel) -> None:
        """One 1.0 notification (``event.mail.*``, ``event.folder.changed``).

        Events left queued by a pass that ended with an exception are applied first, in order,
        so no newer event on their folder overtakes them (e.g. a stale flag update applied
        last). If one of them fails (an ``Exception``), it is dropped (reconcile repairs it) and:

        - when an event still queued touches one of this event's folders, this event is queued
          behind it (per-folder order), applied by the next drain (the next event, or the end of
          the next pass), and the failure is raised at once;
        - otherwise nothing older is left on its folders, so this event is applied now and the
          hourly checks asked for run (those of the events drained before the failure too), and
          then the queued event's failure is raised.

        Either way :data:`failed_event_folders` names the failed queued event's folders. If this
        event, or a check, then fails as well, that failure is raised instead (the queued event's
        as its ``__cause__``) and :data:`failed_event_folders` is cleared. A cancellation (or any
        other ``BaseException``) during the drain applies nothing more: this event is queued
        behind what is left and the cancellation propagates."""
        event = _typed(method, params)
        checks: set[str] = set()
        failure: Exception | None = None
        try:
            async with self._lock:
                if event_folders(event) & set(self._paused):
                    self._queue.append(event)
                    self.buffered.add(event_coverage(event))
                    log.debug(
                        "event queued during a reconcile pass", extra={"queued": len(self._queue)}
                    )
                    return
                try:
                    n = await self._drain_ready(checks)
                except Exception as exc:
                    failed = self._drain_failed
                    if failed is not None:
                        failed_event_folders.set(frozenset(event_folders(failed)))
                    if failed is None or self._queued_on(event_folders(event)):
                        self._queue.append(event)
                        self._rebuffer()
                        raise
                    failure, n = exc, 0
                    log.debug("a queued event failed: applying the arriving event first")
                except BaseException:
                    self._queue.append(event)
                    self._rebuffer()
                    raise
                if n:
                    log.debug("queued events applied before an event", extra={"n": n})
                checks |= await self._apply(event)
            await self._run_checks(checks)
        except Exception as exc:
            if failure is None or exc is failure:
                raise
            failed_event_folders.set(None)
            raise exc from failure
        if failure is not None:
            raise failure

    @asynccontextmanager
    async def paused(self, folder_id: str) -> AsyncIterator[Coverage]:
        """A reconcile pass over ``folder_id``: its events are queued until the pass ends, then
        applied in order. A pass that ends with an exception (incl. a cancellation) applies
        nothing: the exception propagates unchanged and the events stay queued, applied before
        the next event (:meth:`on_event`) or when the next pass ends normally. Yields the coverage
        of the events applied to the folder since its last pass (an unresolved event's
        folder-wide coverage among them)."""
        async with self._lock:
            self._paused[folder_id] = self._paused.get(folder_id, 0) + 1
            applied = self._applied.take(folder_id)
        try:
            yield applied
        except BaseException:
            # The pass ended with an exception (a cancellation above all): unpause without
            # applying anything, so the exception propagates as it is. Applying queued events
            # here would await the bridge and could replace it with an event's own failure (a
            # cancelled job task would then go on running). No await, so no lock needed: other
            # tasks see the unpause at once. Queued events stay queued, applied before the next
            # event or when a later pass ends normally (the next start's reconcile repairs them
            # if neither comes).
            self._unpause(folder_id)
            log.debug("pass ended early: queued events left", extra={"queued": len(self._queue)})
            raise
        try:
            # Unpaused under the lock, so an event already waiting for it is queued behind the
            # older ones instead of overtaking them; but a cancellation while waiting for the
            # lock (another task applying an event) must still unpause, or the folder's events
            # would stay queued until a restart.
            await self._lock.acquire()
        except BaseException:
            self._unpause(folder_id)
            raise
        checks: set[str] = set()
        try:
            self._unpause(folder_id)
            n = await self._drain_ready(checks)
        finally:
            self._lock.release()
        if n:
            log.debug("queued events applied", extra={"n": n})
        await self._run_checks(checks)

    def _unpause(self, folder_id: str) -> None:
        left = self._paused[folder_id] - 1
        if left:
            self._paused[folder_id] = left
        else:
            del self._paused[folder_id]

    async def _drain_ready(self, checks: set[str]) -> int:
        """Apply the queued events that touch no paused folder (call under the lock): one at a
        time, in arrival order; the hourly checks they ask for are added to ``checks`` (also
        those of the events applied before a failure). If one fails, it is dropped and recorded
        in ``_drain_failed``, the rest stay queued (applied by the next drain: the next event,
        or the end of the next pass) and the failure propagates. Returns how many were
        applied."""
        n = 0
        self._drain_failed = None
        try:
            while (i := self._next_ready()) is not None:
                event = self._queue.pop(i)
                try:
                    checks |= await self._apply(event)
                except BaseException:
                    self._drain_failed = event
                    raise
                n += 1
        finally:
            self._rebuffer()
        return n

    def _rebuffer(self) -> None:
        """``buffered`` = the coverage of the events still queued."""
        self.buffered = Coverage()
        for e in self._queue:
            self.buffered.add(event_coverage(e))

    def _queued_on(self, folder_ids: set[str]) -> bool:
        """A queued event touches one of ``folder_ids``."""
        return any(event_folders(e) & folder_ids for e in self._queue)

    def _next_ready(self) -> int | None:
        """Index of the first queued event touching no paused folder."""
        for i, e in enumerate(self._queue):
            if not event_folders(e) & set(self._paused):
                return i
        return None

    async def sync_folders(
        self, source: Source, *, mode: SyncMode = "inline"
    ) -> list[tuple[FolderInfo, int]]:
        """Re-list accounts and folders (see :meth:`_sync_folders`)."""
        async with self._lock:
            return await self._sync_folders(source, mode=mode)

    # -- helpers shared with Reconcile ----------------------------------------------------------

    async def remove_location(
        self, mid: str, folder_id: str, *, source: Source, content_hash: str | None = None
    ) -> None:
        """Remove a location (or one copy in it), passing a refill fetched from a remaining
        copy when the removal leaves none holding the stored content."""
        refill = await self._refill_for(mid, folder_id, content_hash)
        self.store.remove_location(
            mid, folder_id, content_hash=content_hash, source=source, refill=refill
        )

    async def _refill_for(
        self, mid: str, folder_id: str, content_hash: str | None
    ) -> MessageRecord | None:
        for fid, h in self.store.refill_candidates(mid, folder_id, content_hash):
            refill = await self.fetcher.fetch_copy(mid, fid, h)
            if refill is not None:
                return refill
        return None

    # -- internals -------------------------------------------------------------------------------

    async def _run_checks(self, folder_ids: set[str]) -> None:
        if not folder_ids:
            return
        if self.reconcile is None:
            log.info("hourly check deferred: no reconcile attached", extra={"n": len(folder_ids)})
            return
        for folder_id in sorted(folder_ids):
            await self.reconcile.hourly_folder(folder_id)

    async def _info(self, folder_id: str) -> FolderInfo | None:
        indexed, listed = folder_infos(self.store)
        if folder_id not in listed:
            log.debug("event names a folder not listed yet: re-listing")
            await self._sync_folders("event", mode=self._event_sync_mode())
            indexed, listed = folder_infos(self.store)
        return indexed.get(folder_id)

    def _unresolved(
        self, folder_id: str, header_message_id: str, tb_date: int | None, out: _Outcome
    ) -> None:
        """An item an event could not apply: counted in ``unresolved_events`` and the folder's
        hourly check asked for, once its backfill is done (no pass runs before that)."""
        if backfill_status(self.store, folder_id) == STATUS_DONE:
            out.unresolved += 1
            out.checks.add(folder_id)
            self._applied.note_item(folder_id, header_message_id, tb_date)

    def _ignore(self, n: int, out: _Outcome) -> None:
        self.ignored += n
        out.ignored += n

    async def _apply(self, event: EventParams) -> set[str]:
        t0 = time.monotonic()
        counters = Counters()
        out = _Outcome()
        match event:
            case MailNew():
                await self._new(event, counters, out)
            case MailMoved():
                await self._moved(event, counters, out)
            case MailDeleted():
                await self._deleted(event, counters, out)
            case MailUpdated():
                await self._updated(event, counters, out)
            case FolderChanged():
                await self._folder_changed(event, out)
        for folder_id in event_folders(event):
            if backfill_status(self.store, folder_id) != STATUS_DONE:
                # no pass runs over the folder before its backfill is done, so coverage kept
                # until then would hide genuine missed changes from the first pass after it
                self._applied.take(folder_id)
        if out.ignored:
            log.debug("events for excluded or vanished folders ignored", extra={"n": out.ignored})
        if out.folder is not None:
            self.store.record_ingest_run(
                IngestRun(
                    ts=int(self.clock()),
                    kind="event",
                    folder_id=out.folder.folder_id,
                    account_type=out.folder.account_type,
                    n_msgs=counters.n_msgs,
                    secs=time.monotonic() - t0,
                    stale_retries=counters.stale_retries,
                    skipped_large_noid=counters.skipped_large_noid,
                    md5_ids=counters.md5_ids,
                    x_mid_churn=counters.x_mid_churn,
                    hmid_mismatch=counters.hmid_mismatch,
                    unresolved_events=out.unresolved,
                )
            )
            log.debug(
                "event applied",
                extra={
                    "folder": out.folder.hash,
                    "n": counters.n_msgs,
                    "unresolved": out.unresolved,
                },
            )
        if out.unresolved:
            log.info("unresolved events: hourly check scheduled", extra={"n": out.unresolved})
        return out.checks

    async def _ingest_item(self, info: FolderInfo, item: PageItem, counters: Counters) -> set[str]:
        """Ingest one item of ``info`` (new or moved in); returns the mids it holds there.
        A downloaded item is always fetched and written, even when its mid is already located
        in the folder: the Store collapses a byte-identical copy (refreshing its state) and
        records a differing one in ``same_folder_copies`` (conflict check, step 6 case i)."""
        folder_id = info.folder_id
        d = tb_date_of(item.date)
        m = hmid_mid(item.header_message_id)
        fetched = await self.items.fetch(info, item, counters)
        self._applied.note_item(folder_id, item.header_message_id, d)
        match fetched.kind:
            case "headers_only":
                # A row for every headers-only item, as Backfill and a re-page record it, also
                # when the mid is located here: once TB downloads the copy (no event),
                # reconcile's per-mid copy count fetches it and drops this row (by the write of
                # a differing copy, or by the pass's stale-row trim for an identical one, #30;
                # the trim also drops the row of a stale or replayed event for an item already
                # downloaded, Known limitations). One row per (mid, tb_date) from an event, so
                # a replayed event adds none (two headers-only copies of one Message-ID at one
                # second share a row until a re-page of their window rebuilds the rows; Known
                # limitations).
                if m is None or not has_headers_only_row(self.store, folder_id, m, d):
                    add_aux_row(self.store, HEADERS_ONLY, folder_id, d, m)
                return set()
            case "skipped_large_noid" | "skipped_tb_error":
                reason = SKIP_REASONS[fetched.kind]
                add_aux_row(self.store, SKIPPED, folder_id, d, None, reason=reason)
                return set()
            case "stale":
                return set()
            case "copies":
                mids = await self.items.write(info, fetched, source="event", counters=counters)
                for written in mids:
                    self._applied.mids.add((folder_id, written))
                return set(mids)

    async def _new(self, event: MailNew, counters: Counters, out: _Outcome) -> None:
        info = await self._info(event.folder_id)
        if info is None:
            self._ignore(len(event.items), out)
            return
        out.folder = info
        for item in event.items:
            await self._ingest_item(info, item, counters)

    async def _moved(self, event: MailMoved, counters: Counters, out: _Outcome) -> None:
        dst = await self._info(event.dst_folder_id)
        src = await self._info(event.src_folder_id)
        out.folder = dst or src
        if dst is None and src is None:
            self._ignore(len(event.items), out)
            return
        for item in event.items:
            mids: set[str] = set()
            if dst is not None:
                mids = await self._ingest_item(dst, item, counters)
            if src is None:
                continue
            d = tb_date_of(item.date)
            m = hmid_mid(item.header_message_id)
            if m is not None:
                mids.add(m)
            elif not mids and not item.headers_only:  # a headers-only item is never fetched
                got = await self.fetcher.mid_of_tb(item.tb_id, item.size)
                mids = set() if got is None else {got}
            located = [x for x in sorted(mids) if is_located(self.store, x, src.folder_id)]
            left = d if item.headers_only else None  # a move carries the item's date
            for x in located:
                if not await self._resync_removal(src, x, item.header_message_id, left):
                    self._unresolved(src.folder_id, item.header_message_id, d, out)
            if located:
                continue
            if m is None and not item.headers_only:
                # a downloaded md5: item not located in the source (its mid unknown: the copy
                # went stale; or known but not held there): a NULL-mid headers-only row at its
                # tb_date would belong to another item, so none is dropped; hourly check
                self._unresolved(src.folder_id, item.header_message_id, d, out)
                continue
            # not located in the source: it was headers-only there (its row goes), or unknown
            if drop_headers_only_row(self.store, src.folder_id, m, d):
                self._applied.note_item(src.folder_id, item.header_message_id, d)
            else:
                log.debug("moved item had no location in its source folder")

    async def _deleted(self, event: MailDeleted, counters: Counters, out: _Outcome) -> None:
        info = await self._info(event.folder_id)
        if info is None:
            self._ignore(len(event.header_message_ids), out)
            return
        out.folder = info
        folder_id = info.folder_id
        for h in event.header_message_ids:
            m = hmid_mid(h)
            if m is not None and is_located(self.store, m, folder_id):
                if await self._resync_removal(info, m, h):
                    self._applied.mids.add((folder_id, m))
                else:
                    self._unresolved(folder_id, h, None, out)
                continue
            if m is not None and drop_headers_only_row(self.store, folder_id, m, None):
                self._applied.mids.add((folder_id, m))  # a headers-only item: its row went
                continue
            # Nothing to fetch: a deleted message cannot be found, and any `find` match is
            # another copy that still exists (possibly headers-only, Decision 8). An md5: id,
            # or a Message-ID the index does not hold here, goes to the hourly check.
            self._unresolved(folder_id, h, None, out)

    async def _updated(self, event: MailUpdated, counters: Counters, out: _Outcome) -> None:
        for u in event.items:
            info = await self._info(u.folder_id)
            if info is None:
                self._ignore(1, out)
                continue
            out.folder = out.folder or info
            m = hmid_mid(u.header_message_id)
            if m is not None and not is_located(self.store, m, u.folder_id):
                if has_headers_only_row(self.store, u.folder_id, m):
                    # a headers-only item: no location state is kept for it, nothing to fetch
                    self._applied.mids.add((u.folder_id, m))
                    continue
                m = None  # not held here (hmid_mismatch, or a lost new event): hourly check
            elif m is None:
                # md5: the item is found by paging the folder's ID-less tb_dates (UpdatedItem
                # carries no headers_only); only a downloaded item is fetched to learn its mid
                it = await self.fetcher.find_id_less(info, u.tb_id)
                if it is not None and it.headers_only:
                    self._applied.dates.add((u.folder_id, tb_date_of(it.date)))
                    continue
                if it is not None:
                    got = await self.fetcher.mid_of_tb(it.tb_id, it.size)
                    if got is not None and is_located(self.store, got, u.folder_id):
                        m = got
            if m is None:
                self._unresolved(u.folder_id, u.header_message_id, None, out)
                continue
            state = state_of(u.tags, u.flags.read, u.flags.flagged)
            self.store.set_location_state(m, u.folder_id, state, source="event")
            self._applied.mids.add((u.folder_id, m))

    async def _folder_changed(self, event: FolderChanged, out: _Outcome) -> None:
        await self._sync_folders("event", mode=self._event_sync_mode())
        info = folder_infos(self.store)[0].get(event.folder_id)
        if info is None:
            self._ignore(1, out)
            return
        out.folder = info
        out.checks.add(info.folder_id)

    async def _resync_removal(
        self, info: FolderInfo, mid: str, hmid: str, left_date: int | None = None
    ) -> bool:
        """TB's copies of ``mid`` in ``info`` after a move or delete: none left → the location
        goes; some left in a folder with same-folder copies → the copies TB no longer holds go;
        only headers-only ones left → the location goes and they get ``headers_only_items``
        rows, unless it stands on recorded headers-only copies (:meth:`_keep_if_downloaded`;
        ``left_date``: the tb_date of a headers-only item a move took away, None for a delete,
        which carries no date). False when that cannot be decided (unresolved)."""
        folder_id = info.folder_id
        found = [f for f in await self.source.find(hmid) if f.folder_id == folder_id]
        md5 = hmid.startswith(TB_MD5_PREFIX)
        copies = has_copies(self.store, mid, folder_id)
        held: set[str] = set()
        if found and (md5 or copies):
            for f in found:
                got = await self.fetcher.hash_of_tb(f.tb_id)
                if got is not None and got[0] == mid:
                    held.add(got[1])
            if md5 and not held:
                found = []
        if not found:
            await self.remove_location(mid, folder_id, source="event")
            return True
        if copies and held:
            for h in sorted(self.store.location_hashes(mid, folder_id) - held):
                await self.remove_location(mid, folder_id, source="event", content_hash=h)
        if not md5 and not copies:
            return await self._keep_if_downloaded(info, mid, {f.tb_id for f in found}, left_date)
        return True

    async def _keep_if_downloaded(
        self, info: FolderInfo, mid: str, tb_ids: set[int], left_date: int | None = None
    ) -> bool:
        """A Message-ID copy left ``info`` while ``find`` still lists ``tb_ids`` there: the
        location stays if one of them is downloaded. If all are headers-only:

        - the location's own copy is recorded headers-only (a ``headers_only_items`` row of
          ``mid`` at the location's tb_date, which reconcile writes for an evicted copy) and the
          folder's rows of ``mid`` (as they stood before the event) outnumber them: a recorded
          headers-only copy is the one that left, and the location stood on headers-only copies
          alone (reconcile kept its evicted content, #30), so it stays and the rows follow TB's
          remaining copies (:meth:`record_headers_only`). A spoof's location has no row at its
          own tb_date (it was downloaded when written), so a stale row (e.g. of a headers-only
          copy deleted while a downloaded one remained) never keeps a spoof's text. Not when a
          move took away a headers-only item at the location's tb_date (``left_date``) and none
          of the remaining copies has that date: the evicted copy itself left, so its text goes
          as below;
        - otherwise the copy that left was the folder's last downloaded one (no
          ``same_folder_copies`` rows: every copy written there had the location's hash), so
          the indexed text is that copy's and goes with it (:meth:`_drop_to_headers_only`; e.g.
          a spoof that took a headers-only genuine copy's Message-ID was deleted). This is a
          delete or move, not an eviction: TB evicting a body fires no event.

        Their PageItems are paged (never fetched, Decision 8); False when one is not seen
        (unresolved)."""
        folder_id = info.folder_id
        row = self.store.connection.execute(
            "SELECT tb_date FROM locations WHERE mid = ? AND folder_id = ?", (mid, folder_id)
        ).fetchone()
        loc_date = None if row is None else int(row[0])
        first = [] if loc_date is None else [loc_date]
        items = await self.fetcher.page_items(info, tb_ids, first)
        if items is None:
            return False
        if any(not it.headers_only for it in items.values()):
            return True
        dates = [tb_date_of(it.date) for it in items.values()]
        (recorded,) = self.store.connection.execute(
            "SELECT count(*) FROM headers_only_items WHERE folder_id = ? AND mid = ?",
            (folder_id, mid),
        ).fetchone()
        evicted_left = left_date is not None and left_date == loc_date and loc_date not in dates
        if (
            int(recorded) > len(dates)
            and loc_date is not None
            and has_headers_only_row(self.store, folder_id, mid, loc_date)
            and not evicted_left
        ):
            # The location's own copy is recorded headers-only (evicted, content kept: #30) and
            # the folder held more recorded headers-only copies of ``mid`` than TB lists now:
            # one of them is the copy that left, not a downloaded one, and not the evicted copy
            # itself (as far as the event says). The location stays; the rows follow TB's
            # remaining copies (the departed copy's row goes).
            self.record_headers_only(mid, folder_id, dates, source="event")
            return True
        await self._drop_to_headers_only(mid, folder_id, dates)
        return True

    async def _drop_to_headers_only(self, mid: str, folder_id: str, tb_dates: list[int]) -> None:
        """The event path after the folder's last downloaded copy of ``mid`` left while
        headers-only copies (one per date in ``tb_dates``) remain: the location goes (its
        content with it, unless refilled from a copy in another folder) and each remaining copy
        gets a ``headers_only_items`` row, replacing the folder's earlier rows of ``mid``, as a
        fresh backfill would record."""
        refill = await self._refill_for(mid, folder_id, None)
        with self.store.transaction():
            self.store.remove_location(mid, folder_id, source="event", refill=refill)
            self.store.connection.execute(
                "DELETE FROM headers_only_items WHERE folder_id = ? AND mid = ?", (folder_id, mid)
            )
            for d in sorted(tb_dates):
                add_aux_row(self.store, HEADERS_ONLY, folder_id, d, mid)
        log.info(
            "last downloaded copy left, only headers-only copies remain: location dropped",
            extra={"mid": mid},
        )

    def record_headers_only(
        self, mid: str, folder_id: str, tb_dates: Iterable[int], *, source: Source
    ) -> bool:
        """Record a folder whose TB copies of the located Message-ID ``mid`` are all
        headers-only (one per date in ``tb_dates``), with no event saying a copy left (TB
        evicted the offline body), or after an event removed one of its recorded headers-only
        copies (:meth:`_keep_if_downloaded`). The location stays, with its indexed content and
        last known state (#30, user decision 2026-11-15: keep evicted content), and each copy
        gets a ``headers_only_items`` row, replacing the folder's earlier rows of ``mid``, as a
        re-page of its windows records them (one row per headers-only item, also of a located
        mid). Nothing is written when the rows already match. True when rows were written."""
        conn = self.store.connection
        want = sorted(tb_dates)
        have = [
            int(r[0])
            for r in conn.execute(
                "SELECT tb_date FROM headers_only_items WHERE folder_id = ? AND mid = ? "
                "ORDER BY tb_date",
                (folder_id, mid),
            )
        ]
        if have == want:
            return False
        with self.store.transaction():
            conn.execute(
                "DELETE FROM headers_only_items WHERE folder_id = ? AND mid = ?", (folder_id, mid)
            )
            for d in want:
                add_aux_row(self.store, HEADERS_ONLY, folder_id, d, mid)
        log.info(
            "location kept with only headers-only copies: rows recorded",
            extra={"mid": mid, "source": source, "n": len(want)},
        )
        return True

    async def _sync_folders(
        self, source: Source, *, mode: SyncMode = "inline"
    ) -> list[tuple[FolderInfo, int]]:
        """Re-list accounts and folders into the index. New indexed folder ids (never listed
        before, or listed but never backfilled: no ``ingest_state`` row) are backfilled first;
        then folder ids TB no longer lists, and folders that became excluded, lose their
        locations (hook ``None``, refills fetched from remaining copies). A folder id TB no
        longer lists in an account it still lists is removed only when a page of it says
        ``folder_not_found`` (:meth:`_confirmed_gone`): one the extension left out because TB
        could not count it keeps its rows and is logged as a count. The folders of an account
        TB no longer lists go with it. Returns the indexed
        folders with TB's message counts, in TB's order.

        ``mode`` (see :data:`SyncMode`):

        - ``"inline"`` (the hourly/daily re-list in the job task; an event of a standalone
          ``EventIngest``): as above;
        - ``"defer"`` (an event, with :attr:`backfill_job` set): nothing is backfilled here.
          The folder rows are upserted; when a listed indexed folder id was not in the index
          before (a new or renamed folder, or any folder on a fresh index while the backfill job
          is still listing), :attr:`backfill_job` is called, whose job task backfills and then
          re-lists in ``"job"`` mode. While any listed indexed folder is not ``done`` (that new
          id, one the job is still on, or one whose backfill failed or was not reached), nothing
          is removed either; the job's re-list or the next hourly/daily ``"inline"`` re-list
          removes. Backfilling in the event's task would run beside the backfill job (each new
          folder backfilled twice at once), and removing would make a renamed folder's messages
          lose their last location before the new id is backfilled (also when a second event
          comes before the job: the first one has already upserted the new id). Only new ids
          wake the job: ``folder.changed`` comes on every unread or total count change, seconds
          apart, so waking it for a failed folder would retry that backfill every few seconds
          (it is retried before the next scheduled pass, as without events);
        - ``"job"`` (that re-list, in the job task): as ``"inline"``, but removals wait while
          any listed indexed folder is still not ``done`` after it (a failed backfill of a
          renamed folder's new id keeps the old id's locations; the hourly re-list, in
          ``"inline"`` mode, removes them)."""
        known_account = {f.folder_id: f.account_id for f in self.store.folders()}
        known = set(known_account)
        accounts = await self.source.accounts()
        per_account = [(a.account_id, await self.source.folders(a.account_id)) for a in accounts]
        self._check_listing(per_account)
        listed = [(account_id, f) for account_id, fs in per_account for f in fs]
        with self.store.transaction():
            for a in accounts:
                self.store.upsert_account(a.account_id, a.name, a.type)
            for account_id, f in listed:
                self.store.upsert_folder(f.folder_id, account_id, f.path, f.special_use)
        indexed, _ = folder_infos(self.store)
        tb_ids = {f.folder_id for _, f in listed}
        out = [(indexed[f.folder_id], f.total) for _, f in listed if f.folder_id in indexed]
        if mode == "defer":
            if self.backfill_job is None:
                raise RuntimeError("defer mode needs a backfill job")
            if fresh := sum(i.folder_id not in known for i, _ in out):
                log.info("new folders left to the backfill job", extra={"new": fresh})
                self.backfill_job()
            if pending := self._not_done(out):
                # DEBUG: an event re-lists on every folder.changed (every count change, seconds
                # apart while mail moves), so this repeats for the whole of a first backfill.
                log.debug("folder removals left: folders not done", extra={"pending": pending})
                return out
        new = [
            (i, total)
            for i, total in out
            if i.folder_id not in known or backfill_status(self.store, i.folder_id) is None
        ]
        for info, total in new:
            progress = _Progress({info.folder_id: total}, self.backfill.batch_size, None, 1, 0)
            await self.backfill._backfill_folder(info, progress)
        if mode == "job" and (pending := self._not_done(out)):
            log.info("folder removals left: folders not backfilled", extra={"pending": pending})
            return out
        # A folder of an account TB no longer lists goes with its account (the M4 guard has
        # passed); one left out of a listed account's folders goes only if TB cannot find it.
        listed_accounts = {a.account_id for a in accounts}
        unlisted = sorted(known - tb_ids)
        gone = [
            f
            for f in unlisted
            if known_account[f] not in listed_accounts or await self._confirmed_gone(f)
        ]
        if kept := len(unlisted) - len(gone):
            log.info("unlisted folders kept: TB still has them", extra={"kept": kept})
        excluded = sorted(
            f.folder_id
            for _, f in listed
            if f.folder_id not in indexed and self._has_index_rows(f.folder_id)
        )
        for folder_id in gone + excluded:
            refills = await self._refills_for_folder(folder_id)
            with self.store.transaction():
                self.store.remove_folder(folder_id, source=source, refills=refills)
                for account_id, f in listed:
                    if f.folder_id == folder_id:
                        self.store.upsert_folder(f.folder_id, account_id, f.path, f.special_use)
        log.info(
            "folders synced",
            extra={
                "listed": len(listed),
                "indexed": len(out),
                "new": len(new),
                "gone": len(gone),
                "excluded_now": len(excluded),
            },
        )
        return out

    def _check_listing(self, per_account: list[tuple[str, list[Folder]]]) -> None:
        """Mass-removal guard (plan 04 merge review M4): raise ``IngestError`` before anything
        is written when TB lists no accounts, or a listed account with no folders, while the
        index holds folders (with locations or a backfill state) for them: an empty listing is
        more likely a bridge or TB glitch than every folder gone, and the re-list would remove
        every location there. An account TB no longer lists while others are listed loses its
        folders as usual. Only fully empty listings are stopped here; a partial one (an IMAP
        account listing only INBOX while TB is still discovering its folders) removes a held
        folder it leaves out only when TB says ``folder_not_found`` for it
        (:meth:`_confirmed_gone`)."""
        held = {
            str(r[0])
            for r in self.store.connection.execute(
                "SELECT DISTINCT account_id FROM folders WHERE folder_id IN "
                "(SELECT folder_id FROM locations UNION SELECT folder_id FROM ingest_state)"
            )
        }
        if not held:
            return
        if not per_account:
            log.info("folder sync stopped: no accounts listed", extra={"held": len(held)})
            raise IngestError(
                "TB listed no accounts while the index holds folders: nothing removed"
            )
        empty = sum(not fs and account_id in held for account_id, fs in per_account)
        if empty:
            log.info("folder sync stopped: accounts listed no folders", extra={"accounts": empty})
            raise IngestError(
                "TB listed no folders for an account the index holds folders of: nothing removed"
            )

    async def _confirmed_gone(self, folder_id: str) -> bool:
        """Whether TB says a folder the listing left out no longer exists: a page of it is
        ``tb_error``/``folder_not_found``. The extension leaves a folder out of
        ``mail.folders.list`` when TB cannot give its counts (a local folder whose summary is
        not built, see ``extension/src/background/handlers.ts``); such a folder still pages, or
        fails with another ``tb_code``, and keeps its index rows. Any other bridge error
        (``unavailable``…) raises: nothing is removed on a guess. The probe asks for one instant
        (the epoch) so TB lists next to nothing."""
        try:
            await self.source.page(folder_id, from_date=_PROBE_DATE, to_date=_PROBE_DATE)
        except BridgeError as exc:
            if exc.name != "tb_error":
                raise
            return exc.tb_code == "folder_not_found"
        return False

    def _event_sync_mode(self) -> SyncMode:
        return "inline" if self.backfill_job is None else "defer"

    def _not_done(self, folders: list[tuple[FolderInfo, int]]) -> int:
        """How many of ``folders`` have a backfill that is not ``done``."""
        return sum(backfill_status(self.store, i.folder_id) != STATUS_DONE for i, _ in folders)

    def _has_index_rows(self, folder_id: str) -> bool:
        return (
            self.store.connection.execute(
                "SELECT 1 FROM locations WHERE folder_id = ? UNION ALL "
                "SELECT 1 FROM ingest_state WHERE folder_id = ? LIMIT 1",
                (folder_id, folder_id),
            ).fetchone()
            is not None
        )

    async def _refills_for_folder(self, folder_id: str) -> dict[str, MessageRecord]:
        refills: dict[str, MessageRecord] = {}
        for loc in self._folder_mids(folder_id):
            for fid, h in self.store.refill_candidates(loc, folder_id):
                rec = await self.fetcher.fetch_copy(loc, fid, h)
                if rec is not None:
                    refills[loc] = rec
                    break
        return refills

    def _folder_mids(self, folder_id: str) -> Sequence[str]:
        return [
            r[0]
            for r in self.store.connection.execute(
                "SELECT mid FROM locations WHERE folder_id = ? ORDER BY mid", (folder_id,)
            )
        ]
