"""``Reconcile``: the hourly and daily passes that repair what events missed (plan 04 step 8;
architecture §2 step 3, #16 S1, A7, #30).

**Hourly** (:meth:`Reconcile.hourly`): accounts and folders are re-listed
(``EventIngest.sync_folders``: new folder ids backfilled, vanished ones dropped), pending
refills are fetched (``Store.pending_refills``), then every indexed folder whose backfill is
done gets a pass. A pass pages the folder window by window (no raw), newest first, in windows of
``backfill_window_days`` anchored at the pass's clock: ``[now - W, open)``, then
``[lo - W, lo)`` down past the oldest tb_date the index holds for the folder, then one
open-ended tail below that (catching anything TB holds below the index). Per window it compares

- TB's ``{mid_of(hmid)}`` of non-``md5:`` items with the index's Message-ID mids in
  ``locations`` ∪ ``headers_only_items`` (and every downloaded item must have a location), and
- per tb_date, TB's count of ``md5:`` items with the index's count of x- locations + NULL-mid
  ``headers_only_items`` + ``skipped_items`` (and TB's downloaded ``md5:`` items must not
  outnumber the x- locations + skipped rows).

A mismatched window is re-paged (:meth:`Reconcile.repage_window`): items not indexed are
fetched and written (a TB item whose ``headers_only`` turned false is a late download, not a
missed change); index locations TB no longer lists become removal candidates (removed, with a
refill when they held the content, unless the pass fetches a held copy elsewhere in the folder:
"Removals are decided per pass" below); at a mismatched tb_date every downloaded ``md5:`` item is
fetched to learn its x- mid, and x- locations that are not among them become candidates too;
the window's ``headers_only_items`` and
``skipped_items`` rows are replaced from TB's items. **Hourly applies location adds and removes
only**: a copy the index already holds is never re-written, so tag/flag/read differences stay
for the daily pass (A7). Each pass updates ``folders.tb_total`` and ``last_reconciled_at``. The
folder-level (count, newest tb_date) comparison is logged, not acted on: equal counts can hide a
delete plus an add in one folder, which only the windows show.

**Daily** (:meth:`Reconcile.daily`): the same, and then tags, flagged and read of every location
are set to TB's (``md5:`` items are matched to x- locations by tb_date; several at one second are
fetched to tell them apart).

**Buffering** (#16 S1): a pass runs inside ``EventIngest.paused(folder)``, so that folder's events
are queued and applied after the pass. A change the pass finds that a queued event covers is left
to that event (not applied by the pass, not counted).

**Missed change** = one (mid, folder_id) location added, removed, or with changed tags/flagged/read,
found by a pass and covered by no queued event and by no event applied to the folder since its
last pass (an unresolved delete covers the folder's ID-less items: its hourly check is the
repair). Written to ``ingest_runs.missed`` (one row per folder per pass, ``kind`` hourly/daily)
and logged at INFO as a count.

**Hook**: every location change goes through the Store, whose ``location_observers`` fire after
commit with source ``'reconcile'`` (``None`` for a removal).

**Headers-only Message-IDs (#30: keep evicted content, user decision 2026-11-15).** Over a
whole pass (all windows), a located Message-ID whose TB items in the folder are all headers-only
(no downloaded item with that hmid, nor a fetched one with that raw mid; e.g. TB evicted the
offline body) keeps its location, with its indexed content and last known state, and each item
gets a ``headers_only_items`` row (``EventIngest.record_headers_only``; nothing is written when
the rows already match, as after a re-page of its window). Not a location change: not counted
as removed or missed. Mids with ``same_folder_copies`` rows, or covered by a queued event, are
left alone. The window comparison alone would accept such a folder (the Message-ID sets match),
so without this step a re-page would add the rows and a pass without one would not. The event
path drops the location instead when a delete or move event says the folder's last downloaded
copy left (``EventIngest._keep_if_downloaded``); with no event, an eviction and such a removal
look the same here, and the content is kept (see Known limitations). An x- location whose
item turned headers-only is kept too, with no row: the per-tb_date ID-less comparison counts
TB's ``md5:`` items, downloaded or not, against x- locations + NULL-mid rows + skipped rows (the
plan's hourly rule), and one held x- location matches one headers-only item. The daily pass skips
headers-only items, so a kept location's tags/flagged/read change only by events (see Known
limitations).

**Per-mid copy count.** Over a whole pass, a located Message-ID whose downloaded TB items in the
folder (by TB's hmid) outnumber the index's copies there (the location row plus its
``same_folder_copies``) has every downloaded item fetched, and a copy whose hash the folder
does not hold is written (``same_folder_copies``, ``conflicted`` recomputed; counted in
``PassResult.copies``). This finds a differing copy whose new-mail event was applied while it
was headers-only (the usual IMAP order: header first, body later, no event for the body: a late
download, its write drops the event's ``headers_only_items`` row, not missed) and one that
arrived downloaded without any event (its new-mail event was lost: also counted as missed,
unless an event applied since the folder's last pass covers the mid), although the Message-ID
sets compare equal. Re-pages record a ``headers_only_items`` row for every headers-only item,
also of a located mid, as Backfill and the event path do.

**Removals are decided per pass** (plan 04 M3: a location is per folder, not per window).
A location's ``tb_date`` names one of its copies; byte-identical copies collapse into it (#30),
and their TB dates may lie in different windows (TB dates mail whose Date header it cannot
read by arrival; the content hash then reads the Date as missing). A re-page therefore only
collects candidates, decided after all windows of the pass (:meth:`Reconcile._resolve_removals`):

- a Message-ID located in the window that TB no longer lists there, or an x- location at a
  re-fetched second that no fetched item there gave: kept when a copy the folder holds was
  fetched anywhere in the pass (its ``locations.tb_date`` moves to the date of a fetched copy of
  the row's content, the newest when several; ``Store.set_location_tb_date``); left to the next
  pass when TB lists a downloaded item of the Message-ID that was not fetched to its own content
  (its fetch went stale, or its stale tb_id was re-resolved by hmid, which gives every match's
  copies) or, for an x- mid, when any fetch of the pass went stale; else removed (counted
  removed and, unless covered by an applied event, missed);
- a kept location's copies (``same_folder_copies``, the row's own hash) are pruned to the
  hashes fetched in the pass only when every downloaded TB item of the Message-ID in the folder
  was fetched to its own content in the pass (also for a located Message-ID with
  ``same_folder_copies`` rows whose window was re-paged); the row's own copy gone, the Store puts
  a remaining copy in its place. Not a location change: not counted.

:meth:`Reconcile.repage_window` decides removals at the end of its one window, from that
window's items, and never prunes copies.

**Mass-removal guard (plan 04 merge review M4).** A window TB lists no items for while the
index holds locations in it, and TB's folder count is above 0, could be a bridge or TB glitch
(an empty page) that would remove every location there. Such windows are left until every window
of the pass is paged: when TB's items over all windows are fewer than its folder count (TB did
not list everything it counts), the pass raises ``IngestError`` before deciding any removal
(the job records ``error:IngestError``, B3; nothing is removed, the queued events stay
queued); otherwise TB's pages account for its count, the window is really empty, and it is
re-paged as any mismatched window. A folder TB counts as empty (count 0) is re-paged as usual.
:meth:`Reconcile.repage_window` sees one window only, so it raises whenever its window is empty,
holds locations, and TB lists the folder with a count above 0 or does not list it. (Folder
listings: ``EventIngest.sync_folders``.)

**Unreadable items (plan 04 merge review M5).** An item whose ``raw``/``full`` gives
``tb_error`` (TB failed to read it) gets a ``tb_error`` ``skipped_items`` row
(``tern.index.ingest``). Every pass re-fetches each tb_date holding such a row (the retry), and
the rows of a re-fetched tb_date are rebuilt from what was fetched, so a re-page never adds a
second row for an item still unreadable and drops the row once TB reads it. An unread item is
not a fetched copy as far as removals go: its Message-ID is not counted as fetched (a candidate
of that mid waits, as for a stale fetch), its copies are not pruned, and the x- locations at its
tb_date are kept (it may be their copy). An x- location elsewhere in the folder is not held back
by it (unlike a stale fetch, which may stand for any tb_id): an unreadable message may stay so,
and must not stop the folder's ID-less removals for good. The flip side: every x- location at
that second is held back, not just one per unread item (which one stands for it cannot be told
without its content), so an ID-less message at that second deleted without an event stays
indexed for as long as its neighbour stays unreadable (see Known limitations). An add at a
re-fetched second whose ``tb_error`` rows outnumber the items still unreadable there is the
retry reading an item at last: counted in ``PassResult.retried`` (at most one per such row),
never in ``missed`` (TB failed a read; no event was lost).

**Stale headers-only rows.** At the end of each pass, every Message-ID's ``headers_only_items``
rows at a second beyond the headers-only items TB listed there under that hmid over the pass
are dropped (mids covered by a queued event excepted): a copy recorded headers-only by its event
whose fetched hash the folder already holds (a byte-identical copy, which collapses, #30, so no
write drops its row), a replayed or stale new-mail event that still says headers-only for an
item the index holds downloaded, a headers-only copy of a located Message-ID deleted or moved
away (the event resyncs the location only). Rows TB still backs are never dropped; the window
comparison alone would accept all three (the Message-ID sets match).

**Serialized passes.** Passes (a whole :meth:`Reconcile.hourly`/:meth:`Reconcile.daily` run,
an :meth:`Reconcile.hourly_folder` check from its folder listing on, and
:meth:`Reconcile.repage_window`) hold one lock, so TB's folder counts are read inside it, and a
pass an event asks for (``on_event`` → ``hourly_folder``) waits for a scheduled pass and never
overlaps it: two passes over one folder would split the applied-event coverage (the second
counting covered changes as missed) and remove the same location twice. The lock is re-entrant
within one task only: a pass's ``EventIngest.paused`` exit applies the queued events and runs the
checks they ask for in the pass's own task, after the pass is done with its folder. Step-9 note:
an event task that asks for a check waits for the running pass, so ``on_event`` must not run in
the bridge's reader task (it already awaits bridge replies under the event lock).

Reconcile and Backfill must not run at the same time (plan step 9 serializes the jobs).

Known limitations:

- equal-count churn among ID-less messages at one tb_date is invisible (C3 consequence);
- a location whose items TB turned headers-only (evicted body) is kept with its content (#30),
  and the daily pass, which skips headers-only items, no longer refreshes its
  tags/flagged/read: a fresh backfill would hold only ``headers_only_items`` rows (a NULL-mid
  one for an x- mid). The same rule keeps the text of a deleted copy whose folder still lists
  only headers-only copies of its Message-ID when the event path did not apply the delete (event
  lost or unresolved; e.g. a deleted spoof of a headers-only genuine copy, which the event path
  drops): it cannot be told from eviction without fetching a headers-only copy (Decision 8). An
  x- location whose item turned headers-only is removed when its tb_date is re-fetched for
  another reason (another ID-less item there changed): only downloaded items can be fetched to
  learn their x- mids, and ID-less items have no identity to match it by (#5, C3); its item
  then gets a NULL-mid row, as in a fresh backfill;
- identical copies of a mid in one folder collapse into one location (#30), so an ID-less pair of
  identical copies makes its tb_date mismatch, and be re-paged, on every pass; a Message-ID pair
  of identical copies trips the per-mid copy count on every pass (each of its seconds paged and
  every downloaded item of the mid there fetched; nothing is written). Storing the number of
  collapsed copies would end it (a two-way schema addition, not made here);
- a TB headerMessageId that differs from the raw Message-ID (``hmid_mismatch``) makes its window
  mismatch on every pass (the item is fetched each time; nothing is removed or duplicated). A
  re-fetched tb_date rebuilds its x- locations only from every downloaded item there: Message-ID
  items already located are fetched too (one may be held under an x- mid when its raw has none),
  and a second where an item is left to a queued event, or a fetch goes stale, keeps its x-
  locations and rows until the next pass;
- **Headers-only Message-IDs**: the location is never dropped by this rule; a downloaded item
  whose TB hmid names the mid but whose raw does not (``hmid_mismatch``) means no rows are
  recorded for the mid's headers-only items by this step (a re-page of their window records
  them);
- same-folder copies whose content changes without an event are seen only when their window is
  re-paged for another reason, or when the per-mid copy count trips (a re-page alone does not
  fetch a located mid's items unless it has ``same_folder_copies`` rows or its tb_date is
  re-fetched). The count compares numbers only: a differing copy that replaces another copy of
  the mid in the folder without events (one gone, one new) leaves the count equal and is not
  seen. A TB hmid that differs from the raw Message-ID (``hmid_mismatch``) can make the count
  trip on every pass (the copy fetched is held under its raw mid, so nothing is written);
- a Message-ID item over RAW_MAX whose full headers carry no Message-ID (``skipped_items`` has no
  mid) makes its window mismatch, and is re-fetched, on every pass; its row is ID-less, so the
  ID-less items at its tb_date are re-fetched with it and that second's rows are rebuilt from
  what was fetched. Where that cannot happen (a queued event covers the tb_date or an item
  there, or a fetch there went stale) the old rows are kept plus that item's row, which may count
  it twice for one pass (the next pass re-fetches the tb_date and rebuilds it). Rows carry no
  item identity (#5, C3), so which of them already stands for that item cannot be told without
  that re-fetch;
- passes run folder by folder: a message moved, without an event, from a folder passed earlier to
  one passed later is removed first (last location → message deleted, its observers fire) and
  added back by the later pass. For an x- mid the run remembers the removal, so the re-insert
  does not count as ``x_mid_churn`` (the mid is not new); ``missed`` counts both the removal
  and the add (two location changes). Cross-plan note for plan 05: its ``message_removed_observers``
  then drop and rebuild that message's vec rows; a two-phase pass (adds in every folder, then
  removals) would avoid it and is a two-way change for a later step;
- **Unreadable items** (plan 04 M5): an unread item holds back every x- location at its
  tb_date, not one per unread item, since which location stands for it cannot be told without
  its content. An ID-less message at that second deleted without an event therefore stays
  indexed and searchable for as long as the neighbour stays unreadable (for good, if TB never
  reads it); once TB reads it, that second's re-fetch removes the deleted one. Removing the
  surplus when the unseen x- locations outnumber the unread items is not decidable either
  (which ones are gone is unknown).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Literal

from tern.bridge.protocol import PageItem
from tern.config import TernConfig
from tern.index.events import (
    CopyFetcher,
    Coverage,
    EventIngest,
    backfill_status,
    folder_infos,
    folder_windows,
    hmid_mid,
    is_located,
    state_of,
)
from tern.index.ingest import (
    DAY_S,
    SKIP_REASONS,
    STATUS_DONE,
    Backfill,
    Counters,
    FolderInfo,
    IngestError,
    _WindowRows,
    tb_date_of,
)
from tern.index.mid import X_PREFIX
from tern.index.source import IngestSource
from tern.index.store import IngestRun, LocationState, Store

log = logging.getLogger(__name__)

PassKind = Literal["hourly", "daily"]


@dataclass
class PassResult:
    """One folder's pass."""

    folder_id: str
    windows: int = 0
    repaged: int = 0
    added: int = 0
    removed: int = 0
    state_changes: int = 0
    #: Adds that were late downloads (headers-only items TB has since fetched): never missed.
    late: int = 0
    #: Adds of items TB failed to read before (a ``tb_error`` row at their second that the
    #: pass's re-fetch read at last; plan 04 M5 review): never missed.
    retried: int = 0
    #: Differing same-folder copies of a located mid found by the per-mid copy count (those not
    #: late downloads and not covered by an applied event are counted in ``missed`` too).
    copies: int = 0
    missed: int = 0


@dataclass(frozen=True)
class ReconcileReport:
    kind: PassKind
    folders: int
    windows: int
    repaged: int
    added: int
    removed: int
    state_changes: int
    missed: int
    refilled: int
    #: Differing same-folder copies found by the per-mid copy count (``PassResult.copies``).
    copies: int = 0
    #: Adds of items unreadable before (``PassResult.retried``).
    retried: int = 0


@dataclass
class _IndexWindow:
    """The index's rows of one folder window."""

    named: dict[str, LocationState]  # Message-ID mid → state
    anon: dict[int, dict[str, LocationState]]  # tb_date → x- mid → state
    ho_named: set[str]
    ho_null: Counter[int]
    skipped: Counter[int]
    copies: set[str]  # mids with same_folder_copies rows in the folder
    #: tb_date → the ``skipped_items`` reasons there, in ordinal order
    skipped_reasons: dict[int, list[str]] = field(default_factory=dict[int, list[str]])


@dataclass
class _TbWindow:
    named_dl: set[str]
    named_ho: set[str]
    anon_dl: Counter[int]
    anon_ho: Counter[int]


def _tb_window(items: list[PageItem]) -> _TbWindow:
    tb = _TbWindow(set(), set(), Counter(), Counter())
    for it in items:
        m = hmid_mid(it.header_message_id)
        d = tb_date_of(it.date)
        if m is None:
            (tb.anon_ho if it.headers_only else tb.anon_dl)[d] += 1
        else:
            (tb.named_ho if it.headers_only else tb.named_dl).add(m)
    return tb


@dataclass
class _PassSeen:
    """What one pass saw of a folder's Message-ID items, over all its windows."""

    #: Message-IDs with a downloaded item: TB's (``mid_of(hmid)``) plus the raw-derived mids of
    #: the items the pass fetched (a TB hmid that differs from the raw one, ``hmid_mismatch``)
    downloaded: set[str] = field(default_factory=set)
    #: Message-ID → tb_dates of its headers-only items
    headers_only: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    #: Message-ID (TB's hmid) → tb_dates of its downloaded items, one entry per item
    downloaded_at: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))

    def note(self, items: list[PageItem]) -> None:
        for it in items:
            m = hmid_mid(it.header_message_id)
            if m is None:
                continue
            if it.headers_only:
                self.headers_only[m].append(tb_date_of(it.date))
            else:
                self.downloaded.add(m)
                self.downloaded_at[m].append(tb_date_of(it.date))


@dataclass
class _PassRemovals:
    """Removal candidates of one pass and the copies it fetched, decided after all its windows
    (see "Removals are decided per pass" in the module docstring)."""

    #: Message-ID mids located in a re-paged window that TB no longer lists there
    named: set[str] = field(default_factory=set)
    #: x- mid → the re-fetched tb_date (its location's) that did not give it
    anon: dict[str, int] = field(default_factory=dict)
    #: located Message-IDs with ``same_folder_copies`` rows fetched in their own window
    prune: set[str] = field(default_factory=set)
    #: record mid → content_hash → tb_dates of the copies fetched in the pass
    fetched: dict[str, dict[str, list[int]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list))
    )
    #: TB hmid mid → downloaded items of it fetched to their own content (not stale, nor
    #: re-resolved by hmid after a stale tb_id: those give every match's copies, not
    #: necessarily the item's); fewer than the pass listed (``_PassSeen.downloaded_at``) means
    #: one was not (each item is fetched at most once)
    fetched_items: Counter[str] = field(default_factory=Counter)
    #: any fetch of the pass went stale
    stale: bool = False


def _holds_locations(ix: _IndexWindow) -> bool:
    return bool(ix.named) or any(ix.anon.values())


def _named_match(ix: _IndexWindow, tb: _TbWindow) -> bool:
    return (tb.named_dl | tb.named_ho) == (set(ix.named) | ix.ho_named) and tb.named_dl <= set(
        ix.named
    )


def _anon_mismatch(ix: _IndexWindow, tb: _TbWindow) -> set[int]:
    """tb_dates whose ID-less items do not match, and every tb_date holding a ``tb_error``
    skipped row (re-fetched on every pass until TB reads its item: the retry, plan 04 M5)."""
    out: set[int] = set()
    for d in set(tb.anon_dl) | set(tb.anon_ho) | set(ix.anon) | set(ix.ho_null) | set(ix.skipped):
        held = len(ix.anon.get(d, {})) + ix.skipped[d]
        if (
            tb.anon_dl[d] + tb.anon_ho[d] != held + ix.ho_null[d]
            or tb.anon_dl[d] > held
            or "tb_error" in ix.skipped_reasons.get(d, ())
        ):
            out.add(d)
    return out


class _PassLock:
    """One lock for every pass; re-entrant within the task that holds it (see "Serialized
    passes" in the module docstring)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("a reconcile pass must run in an asyncio task")
        if self._owner is task:
            yield
            return
        async with self._lock:
            self._owner = task
            try:
                yield
            finally:
                self._owner = None


class Reconcile:
    """Hourly and daily reconcile; see the module docstring. Creates its own ``EventIngest``
    when none is given (then nothing is ever queued)."""

    def __init__(
        self,
        store: Store,
        source: IngestSource,
        cfg: TernConfig,
        *,
        events: EventIngest | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.source = source
        self.cfg = cfg
        self.clock = clock
        self.events = events if events is not None else EventIngest(store, source, cfg, clock=clock)
        self.events.reconcile = self
        # Reconcile pages and fetches through its own source (it may differ from the events'),
        # under the events' fetch throttle: ingest_max_msgs_per_s caps TB's load, not one job's.
        self.backfill = Backfill(
            store, source, cfg, clock=clock, throttle=self.events.items.throttle
        )
        self.items = self.backfill.items
        self.fetcher = CopyFetcher(store, source, self.backfill)
        self.window_s = cfg.backfill_window_days * DAY_S
        self._passes = _PassLock()
        #: x- mids removed by the hourly/daily run in progress (None outside a run): a later
        #: folder's pass re-inserting one is an unevented move, not ``x_mid_churn``.
        self._removed_in_run: set[str] | None = None

    # -- passes ------------------------------------------------------------------------------------

    async def hourly(self) -> ReconcileReport:
        return await self._run("hourly")

    async def daily(self) -> ReconcileReport:
        return await self._run("daily")

    async def hourly_folder(self, folder_id: str) -> PassResult | None:
        """One folder's hourly check now (asked for by an unresolved event or
        ``folder.changed``); None when the folder is not indexed or its backfill is not done.
        TB's folder count is read under the pass lock, so a check that waited for another pass
        never records a count from before it."""
        async with self._passes.hold():
            info = folder_infos(self.store)[0].get(folder_id)
            if info is None:
                return None
            total = None
            for f in await self.source.folders(info.account_id):
                if f.folder_id == folder_id:
                    total = f.total
            if total is None:
                log.info(
                    "hourly check skipped: folder no longer listed", extra={"folder": info.hash}
                )
                return None
            return await self._pass(info, "hourly", total)

    async def _run(self, kind: PassKind) -> ReconcileReport:
        # The whole run holds the pass lock: TB's folder counts listed here are not taken
        # before another pass that runs in between.
        async with self._passes.hold():
            return await self._run_held(kind)

    async def _run_held(self, kind: PassKind) -> ReconcileReport:
        t0 = time.monotonic()
        listed = await self.events.sync_folders("reconcile")
        refilled = await self.refill_pending()
        results: list[PassResult] = []
        self._removed_in_run = set()
        try:
            for info, total in listed:
                r = await self._pass(info, kind, total)
                if r is not None:
                    results.append(r)
        finally:
            self._removed_in_run = None
        report = ReconcileReport(
            kind=kind,
            folders=len(results),
            windows=sum(r.windows for r in results),
            repaged=sum(r.repaged for r in results),
            added=sum(r.added for r in results),
            removed=sum(r.removed for r in results),
            state_changes=sum(r.state_changes for r in results),
            missed=sum(r.missed for r in results),
            refilled=refilled,
            copies=sum(r.copies for r in results),
            retried=sum(r.retried for r in results),
        )
        log.info(
            f"{kind} reconcile done",
            extra={
                "folders": report.folders,
                "windows": report.windows,
                "repaged": report.repaged,
                "missed": report.missed,
                "copies": report.copies,
                "retried": report.retried,
                "refilled": refilled,
                "secs": round(time.monotonic() - t0, 3),
            },
        )
        return report

    async def refill_pending(self) -> int:
        """Re-fill the content of messages left without it (``refill_pending``) from one of
        their remaining copies; returns how many were filled."""
        n = 0
        for mid, copies in self.store.pending_refills().items():
            for folder_id, h in copies:
                rec = await self.fetcher.fetch_copy(mid, folder_id, h)
                if rec is not None:
                    with self.store.transaction():
                        self.store.upsert_message(rec)
                    n += 1
                    break
        if n:
            log.info("pending refills filled", extra={"n": n})
        return n

    def _windows(self, folder_id: str, now: int) -> list[tuple[int | None, int | None]]:
        return folder_windows(self.store, folder_id, now, self.window_s)

    async def _page_window(
        self, info: FolderInfo, lo: int | None, hi: int | None
    ) -> list[PageItem]:
        items = await self.backfill.page_all(info, lo, hi)
        return [
            i
            for i in items
            if (lo is None or tb_date_of(i.date) >= lo) and (hi is None or tb_date_of(i.date) < hi)
        ]

    async def _pass(self, info: FolderInfo, kind: PassKind, tb_total: int) -> PassResult | None:
        async with self._passes.hold():
            return await self._pass_held(info, kind, tb_total)

    async def _pass_held(
        self, info: FolderInfo, kind: PassKind, tb_total: int
    ) -> PassResult | None:
        folder_id = info.folder_id
        if backfill_status(self.store, folder_id) != STATUS_DONE:
            log.debug("pass skipped: backfill not done", extra={"folder": info.hash})
            return None
        t0 = time.monotonic()
        res = PassResult(folder_id)
        counters = Counters()
        newest: int | None = None
        seen = _PassSeen()
        pending = _PassRemovals()
        async with self.events.paused(folder_id) as applied:
            now = int(self.clock())
            paged = 0
            #: empty windows where the index holds locations (mass-removal guard, M4)
            empty: list[tuple[int | None, int | None]] = []
            for lo, hi in self._windows(folder_id, now):
                items = await self._page_window(info, lo, hi)
                res.windows += 1
                paged += len(items)
                seen.note(items)
                for i in items:
                    d = tb_date_of(i.date)
                    newest = d if newest is None else max(newest, d)
                ix = self._index_window(folder_id, lo, hi)
                if not items and tb_total > 0 and _holds_locations(ix):
                    empty.append((lo, hi))  # decided once every window is paged
                    continue
                tb = _tb_window(items)
                if not _named_match(ix, tb) or _anon_mismatch(ix, tb):
                    res.repaged += 1
                    await self._repage(info, lo, hi, items, counters, res, applied, seen, pending)
                if kind == "daily":
                    await self._refresh_states(info, lo, hi, items, res, applied)
            if empty:
                if paged < tb_total:
                    log.info(
                        "pass stopped: empty windows hold locations and TB listed fewer items "
                        "than its folder count",
                        extra={
                            "folder": info.hash,
                            "empty": len(empty),
                            "paged": paged,
                            "tb_total": tb_total,
                        },
                    )
                    raise IngestError(
                        "TB listed no items in a window holding indexed locations and fewer "
                        "items than its folder count: nothing removed"
                    )
                for lo, hi in empty:
                    # TB's pages account for its folder count: the window is really empty
                    res.repaged += 1
                    await self._repage(info, lo, hi, [], counters, res, applied, seen, pending)
            await self._resolve_removals(info, seen, pending, res, applied)
            self._record_headers_only_held(info, seen)
            await self._fetch_uncounted_copies(info, seen, counters, res, applied)
            self._trim_headers_only_rows(info, seen)
            self._log_folder_totals(info, tb_total, newest)
            self.store.set_folder_reconciled(folder_id, tb_total, now)
            self.store.record_ingest_run(
                IngestRun(
                    ts=now,
                    kind=kind,
                    folder_id=folder_id,
                    account_type=info.account_type,
                    n_msgs=counters.n_msgs,
                    secs=time.monotonic() - t0,
                    missed=res.missed,
                    stale_retries=counters.stale_retries,
                    skipped_large_noid=counters.skipped_large_noid,
                    md5_ids=counters.md5_ids,
                    x_mid_churn=counters.x_mid_churn,
                    hmid_mismatch=counters.hmid_mismatch,
                )
            )
        log.info(
            "reconcile pass done",
            extra={
                "folder": info.hash,
                "kind": kind,
                "windows": res.windows,
                "repaged": res.repaged,
                "added": res.added,
                "removed": res.removed,
                "state_changes": res.state_changes,
                "copies": res.copies,
                "retried": res.retried,
                "missed": res.missed,
            },
        )
        return res

    def _log_folder_totals(self, info: FolderInfo, tb_total: int, tb_newest: int | None) -> None:
        conn = self.store.connection
        fid = info.folder_id
        count = sum(
            int(conn.execute(f"SELECT count(*) FROM {t} WHERE folder_id = ?", (fid,)).fetchone()[0])
            for t in ("locations", "same_folder_copies", "headers_only_items", "skipped_items")
        )
        newest = conn.execute(
            "SELECT max(d) FROM (SELECT max(tb_date) AS d FROM locations WHERE folder_id = ? "
            "UNION ALL SELECT max(tb_date) FROM headers_only_items WHERE folder_id = ? "
            "UNION ALL SELECT max(tb_date) FROM skipped_items WHERE folder_id = ?)",
            (fid, fid, fid),
        ).fetchone()[0]
        log.debug(
            "folder totals compared",
            extra={
                "folder": info.hash,
                "count_match": count == tb_total,
                "newest_match": newest == tb_newest,
            },
        )

    # -- windows -----------------------------------------------------------------------------------

    def _index_window(self, folder_id: str, lo: int | None, hi: int | None) -> _IndexWindow:
        ix = _IndexWindow({}, defaultdict(dict), set(), Counter(), Counter(), set())
        for loc in self.store.folder_locations(folder_id, lo, hi):
            if loc.mid.startswith(X_PREFIX):
                ix.anon[loc.tb_date][loc.mid] = loc.state
            else:
                ix.named[loc.mid] = loc.state
        cond = "folder_id = ?"
        params: list[object] = [folder_id]
        if lo is not None:
            cond += " AND tb_date >= ?"
            params.append(lo)
        if hi is not None:
            cond += " AND tb_date < ?"
            params.append(hi)
        conn = self.store.connection
        for d, mid in conn.execute(
            f"SELECT tb_date, mid FROM headers_only_items WHERE {cond}", params
        ):
            if mid is None:
                ix.ho_null[int(d)] += 1
            else:
                ix.ho_named.add(str(mid))
        for d, reason in conn.execute(
            f"SELECT tb_date, reason FROM skipped_items WHERE {cond} ORDER BY tb_date, ordinal",
            params,
        ):
            ix.skipped[int(d)] += 1
            ix.skipped_reasons.setdefault(int(d), []).append(str(reason))
        ix.copies = {
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT mid FROM same_folder_copies WHERE folder_id = ?", (folder_id,)
            )
        }
        return ix

    async def repage_window(self, folder_id: str, lo: int | None, hi: int | None) -> PassResult:
        """Page ``lo <= tb_date < hi`` of a folder (None = open) and apply it (see the module
        docstring); the window's ``headers_only_items``/``skipped_items`` rows are replaced, so a
        re-page is idempotent. Removals are decided from this window's items alone (a pass
        decides them over all its windows), and ``same_folder_copies`` are never pruned (a copy
        may sit in another window). Writes no ``ingest_runs`` row (a pass does)."""
        info = folder_infos(self.store)[0].get(folder_id)
        if info is None:
            raise IngestError("repage_window on a folder that is not indexed")
        res = PassResult(folder_id, windows=1, repaged=1)
        async with self._passes.hold():
            items = await self._page_window(info, lo, hi)
            if not items and _holds_locations(self._index_window(folder_id, lo, hi)):
                total = None
                for f in await self.source.folders(info.account_id):
                    if f.folder_id == folder_id:
                        total = f.total
                if total is None or total > 0:
                    # one window cannot be checked against TB's folder count (M4)
                    raise IngestError(
                        "TB listed no items in a window holding indexed locations while the "
                        "folder is listed with messages or not listed: nothing removed"
                    )
            seen, pending, applied = _PassSeen(), _PassRemovals(), Coverage()
            seen.note(items)
            await self._repage(info, lo, hi, items, Counters(), res, applied, seen, pending)
            # copies of a mid held in other windows are not seen here: never prune to this one
            pending.prune.clear()
            await self._resolve_removals(info, seen, pending, res, applied)
        return res

    def _missed(self, applied: Coverage, folder_id: str, mid: str, d: int | None) -> int:
        return 0 if applied.covers(folder_id, mid, d) else 1

    async def _repage(
        self,
        info: FolderInfo,
        lo: int | None,
        hi: int | None,
        items: list[PageItem],
        counters: Counters,
        res: PassResult,
        applied: Coverage,
        seen: _PassSeen,
        pending: _PassRemovals,
    ) -> None:
        """``seen.downloaded`` gets the raw-derived Message-ID mids of the items fetched here;
        ``pending`` the removal candidates and fetched copies (decided by
        :meth:`_resolve_removals` after the pass's windows)."""
        folder_id = info.folder_id
        ix = self._index_window(folder_id, lo, hi)
        tb = _tb_window(items)
        queued = self.events.buffered
        refetch = {d for d in _anon_mismatch(ix, tb) if not queued.covers(folder_id, None, d)}
        present: set[str] = set()
        ho_rows: list[tuple[int, str | None]] = []
        fetch: list[PageItem] = []
        #: downloaded Message-ID items left unfetched as already located here: fetched after
        #: all only at a re-fetched tb_date (one may be held under an x- mid, when its raw has
        #: no Message-ID, and that second's x- locations are rebuilt from what was fetched)
        located_skip: list[PageItem] = []
        #: tb_dates of downloaded items left to a queued event: their x- locations and rows stay
        queued_dates: set[int] = set()
        for it in items:
            d = tb_date_of(it.date)
            m = hmid_mid(it.header_message_id)
            if m is None:
                if it.headers_only:
                    ho_rows.append((d, None))
                elif d in refetch:
                    fetch.append(it)
                continue
            present.add(m)
            located = m in ix.named
            if it.headers_only:
                ho_rows.append((d, m))  # also for a located mid, as Backfill records it
                continue
            if located and m not in ix.copies:
                located_skip.append(it)
                continue
            if not located and queued.covers(folder_id, m, d):
                queued_dates.add(d)
                continue  # the queued event applies it
            fetch.append(it)

        seen_x: dict[int, set[str]] = defaultdict(set)
        seen_hashes: dict[str, set[str]] = defaultdict(set)
        stale_dates: set[int] = set()
        stale_named: set[str] = set()
        #: tb_date → reasons of the items found skipped there
        skipped_at: dict[int, list[str]] = defaultdict(list)
        #: skipped items fetched for their (non-``md5:``) Message-ID: their rows are ID-less
        skipped_named: dict[int, list[str]] = defaultdict(list)
        #: tb_dates of items TB failed to read (``tb_error``): their rows are rebuilt, but the
        #: unread item may be the copy an x- location there stands for, so none is removed
        unread_dates: set[int] = set()
        #: (mid, tb_date, late download) of the locations added here, counted once every item
        #: is fetched (a retried add needs the second's unread count)
        added_at: list[tuple[str, int, bool]] = []

        async def fetch_one(it: PageItem) -> None:
            fetched = await self.items.fetch(info, it, counters)
            d = fetched.tb_date
            if (
                fetched.hmid_mid is not None
                and fetched.kind not in ("stale", "skipped_tb_error")
                and not fetched.refetched
            ):
                # a skipped item (over RAW_MAX, no id in its full headers) is not a copy of
                # its hmid's mid: fetched as far as removals go; a stale one is unknown, an
                # unread one (tb_error) may be a held copy, and a re-resolved one may have
                # given only other copies of its hmid
                pending.fetched_items[fetched.hmid_mid] += 1
            if fetched.kind == "stale":
                pending.stale = True
                # any stale item at a re-fetched tb_date leaves that second unhandled: a
                # Message-ID item may be the one held under an x- mid there
                stale_dates.add(d)
                if fetched.hmid_mid is not None:
                    stale_named.add(fetched.hmid_mid)
                return
            if fetched.kind in SKIP_REASONS:
                reason = SKIP_REASONS[fetched.kind]
                skipped_at[d].append(reason)
                if fetched.hmid_mid is not None:
                    skipped_named[d].append(reason)
                if fetched.kind == "skipped_tb_error":
                    unread_dates.add(d)
                    if fetched.hmid_mid is not None:
                        stale_named.add(fetched.hmid_mid)  # never prune its copies to this pass
                return
            if fetched.kind != "copies":
                raise IngestError("a headers-only item reached the re-page fetch")
            new = []
            adds: list[tuple[str, bool]] = []
            for c in fetched.copies:
                rm = c.record.mid
                if rm.startswith(X_PREFIX):
                    seen_x[d].add(rm)
                else:
                    present.add(rm)
                    seen.downloaded.add(rm)
                seen_hashes[rm].add(c.record.content_hash)
                pending.fetched[rm][c.record.content_hash].append(d)
                if c.record.content_hash in self.store.location_hashes(rm, folder_id):
                    continue  # already held here: hourly never re-applies a copy's state
                new.append(c)
                if not is_located(self.store, rm, folder_id) and rm not in {a for a, _ in adds}:
                    adds.append((rm, self._was_headers_only(folder_id, fetched.hmid_mid, d, rm)))
            if new:
                await self.items.write(
                    info,
                    replace(fetched, copies=tuple(new)),
                    source="reconcile",
                    counters=counters,
                    removed_in_run=self._removed_in_run or (),
                )
            added_at.extend((rm, d, late) for rm, late in adds)

        for it in fetch:
            await fetch_one(it)
        # A Message-ID item over RAW_MAX whose full headers carry none got an ID-less skipped
        # row at its tb_date. The rows of a tb_date say nothing about which item they stand for,
        # so that second's rows are rebuilt only from all of its ID-less items: fetch them too.
        extra = {
            d
            for d in skipped_named
            if d not in refetch | queued_dates and not queued.covers(folder_id, None, d)
        }
        for it in items:
            if (
                tb_date_of(it.date) in extra
                and not it.headers_only
                and hmid_mid(it.header_message_id) is None
            ):
                await fetch_one(it)
        refetch |= extra
        for it in located_skip:
            if tb_date_of(it.date) in refetch - queued_dates:
                await fetch_one(it)

        # removal candidates (decided over the whole pass, _resolve_removals): Message-ID mids
        # TB no longer lists here; located ones with same-folder copies fetched here
        for m in sorted(ix.named):
            if m in present:
                if m in ix.copies and m in seen_hashes and m not in stale_named:
                    pending.prune.add(m)
                continue
            pending.named.add(m)
        # x- locations at a re-fetched tb_date that no fetched item there gave (none at a
        # tb_date with an unread item: it may be their copy)
        rows_rebuilt = refetch - stale_dates - queued_dates
        handled = rows_rebuilt - unread_dates
        # Adds: an add at a rebuilt second whose tb_error rows outnumber the items still
        # unreadable there is the retry reading one at last (not missed: no event was lost)
        retry_left = {
            d: ix.skipped_reasons.get(d, []).count("tb_error") - skipped_at[d].count("tb_error")
            for d in rows_rebuilt
        }
        for rm, d, late in added_at:
            res.added += 1
            if late:
                res.late += 1
            elif retry_left.get(d, 0) > 0:
                retry_left[d] -= 1
                res.retried += 1
            else:
                res.missed += self._missed(applied, folder_id, rm, d)
        for d in sorted(handled):
            for xm in sorted(ix.anon.get(d, {})):
                if xm not in seen_x[d]:
                    pending.anon[xm] = d

        # A re-fetched tb_date takes its rows from what was fetched. Any other (a queued event
        # covers it or an item there, or a fetch there went stale) keeps its rows plus the
        # Message-ID items found skipped there, which may count one twice for this pass; such a
        # tb_date mismatches on the next pass, which re-fetches and rebuilds it (module
        # docstring, Known limitations).
        # The skipped rows of a re-fetched tb_date with an unread item are rebuilt too (every
        # item there was fetched), so a re-page never adds a second row for it.
        skipped = [(d, r) for d in sorted(rows_rebuilt) for r in skipped_at.get(d, ())]
        skipped += [
            (d, r)
            for d in sorted((set(ix.skipped) | set(skipped_named)) - rows_rebuilt)
            for r in (*ix.skipped_reasons.get(d, ()), *skipped_named.get(d, ()))
        ]
        skipped.sort(key=lambda row: row[0])
        # NULL-mid rows: a tb_date whose ID-less counts matched, and that was not rebuilt, keeps
        # the index's rows. There an x- location whose item TB turned headers-only (evicted
        # body, kept: #30) stands for that item; a NULL-mid row for it too would make the second
        # mismatch on the next pass and its re-fetch drop the x- location. A mismatched or
        # rebuilt tb_date (``handled``: also a second re-fetched for a skipped Message-ID item,
        # where an unseen x- location was just removed) takes them from TB's items.
        rebuilt = _anon_mismatch(ix, tb) | handled
        ho_rows = [r for r in ho_rows if r[1] is not None or r[0] in rebuilt]
        ho_rows += [
            (d, None) for d in sorted(set(ix.ho_null) - rebuilt) for _ in range(ix.ho_null[d])
        ]
        with self.store.transaction():
            self.backfill._replace_rows(
                folder_id, lo, hi, _WindowRows(headers_only=ho_rows, skipped=skipped)
            )

    async def _resolve_removals(
        self,
        info: FolderInfo,
        seen: _PassSeen,
        pending: _PassRemovals,
        res: PassResult,
        applied: Coverage,
    ) -> None:
        """Decide the pass's removal candidates (module docstring, "Removals are decided per
        pass")."""
        folder_id = info.folder_id
        queued = self.events.buffered
        kept = 0

        def complete(m: str) -> bool:
            """Every downloaded TB item of ``m`` (by TB's hmid) in the folder was fetched."""
            return pending.fetched_items[m] >= len(seen.downloaded_at.get(m, ()))

        for m in sorted(pending.named | pending.prune):
            if queued.covers(folder_id, m, None) or not is_located(self.store, m, folder_id):
                continue  # left to the queued event / already gone
            held = self.store.location_hashes(m, folder_id)
            if held & set(pending.fetched.get(m, {})):
                if complete(m):
                    for h in sorted(held - set(pending.fetched[m])):
                        await self.events.remove_location(
                            m, folder_id, source="reconcile", content_hash=h
                        )
                self._follow_held_copy(m, folder_id, pending)
                if m in pending.named:
                    kept += 1
                continue
            if m in pending.prune:
                continue  # only a held copy can be pruned to (none fetched: nothing to decide)
            if m in seen.downloaded and not complete(m):
                continue  # a listed item was not fetched: the next pass decides
            await self.events.remove_location(m, folder_id, source="reconcile")
            res.removed += 1
            res.missed += self._missed(applied, folder_id, m, None)
        for xm, d in sorted(pending.anon.items()):
            if not is_located(self.store, xm, folder_id):
                continue
            if self.store.location_hashes(xm, folder_id) & set(pending.fetched.get(xm, {})):
                self._follow_held_copy(xm, folder_id, pending)
                kept += 1
                continue
            if pending.stale:
                continue  # a stale item may be its copy: the next pass decides
            await self.events.remove_location(xm, folder_id, source="reconcile")
            if self._removed_in_run is not None:
                self._removed_in_run.add(xm)
            res.removed += 1
            res.missed += self._missed(applied, folder_id, xm, d)
        if kept:
            log.info(
                "locations kept: a held copy was seen in another window",
                extra={"folder": info.hash, "n": kept},
            )

    def _follow_held_copy(self, mid: str, folder_id: str, pending: _PassRemovals) -> None:
        """Move the location's ``tb_date`` to a fetched copy of its row's content when no
        fetched copy of it is at the row's date (the newest such date)."""
        (loc,) = [x for x in self.store.locations(mid) if x.folder_id == folder_id]
        dates = pending.fetched.get(mid, {}).get(loc.content_hash)
        if dates and loc.tb_date not in dates:
            self.store.set_location_tb_date(mid, folder_id, max(dates))

    def _record_headers_only_held(self, info: FolderInfo, seen: _PassSeen) -> None:
        """A Message-ID located here whose TB items in the folder are all headers-only (no
        downloaded one anywhere in the pass; e.g. TB evicted its offline body): the location
        keeps its indexed content and last known state (#30, user decision 2026-11-15), and
        its rows are recorded by the event path's rule (``EventIngest.record_headers_only``,
        one row per headers-only item, as a re-page records them), so the index ends the same
        whether or not a re-page or an event ran. Not a location change: never counted as
        removed or missed. Mids with ``same_folder_copies`` rows, or covered by a queued event,
        are left alone."""
        folder_id = info.folder_id
        queued = self.events.buffered
        copies = {
            str(r[0])
            for r in self.store.connection.execute(
                "SELECT DISTINCT mid FROM same_folder_copies WHERE folder_id = ?", (folder_id,)
            )
        }
        for m in sorted(set(seen.headers_only) - seen.downloaded - copies):
            if not is_located(self.store, m, folder_id) or queued.covers(folder_id, m, None):
                continue
            self.events.record_headers_only(m, folder_id, seen.headers_only[m], source="reconcile")

    async def _fetch_uncounted_copies(
        self,
        info: FolderInfo,
        seen: _PassSeen,
        counters: Counters,
        res: PassResult,
        applied: Coverage,
    ) -> None:
        """A located Message-ID whose downloaded TB items in the folder (over the whole pass, by
        TB's hmid) outnumber the index's copies there (the location row plus its
        ``same_folder_copies``): a downloaded copy was never fetched, e.g. a differing copy whose
        new-mail event was applied while it was headers-only, then downloaded with no event, or
        one delivered downloaded whose event was lost (the Message-ID sets still compare equal).
        Every downloaded item of that mid in the folder is fetched (paging each of its seconds,
        ``[d, d + 1]``) and any copy whose hash the folder does not hold is written, so the Store
        records it in ``same_folder_copies`` and recomputes ``conflicted``. Each copy written is
        counted in ``copies`` and, unless it is a late download (its write drops a
        ``headers_only_items`` row: its event was applied while it was headers-only,
        :meth:`_was_headers_only`) or an event applied since the folder's last pass covers the
        mid, in ``missed`` too (its new-mail event never reached the index). A copy whose hash
        the folder already holds is not written; its stale row goes in
        :meth:`_trim_headers_only_rows`. Mids covered by a queued event are left to it.
        Byte-identical copies collapse into one location (#30), so a folder holding such a pair
        re-fetches it on every pass (Known limitations)."""
        folder_id = info.folder_id
        queued = self.events.buffered
        conn = self.store.connection
        for m in sorted(seen.downloaded_at):
            if not is_located(self.store, m, folder_id) or queued.covers(folder_id, m, None):
                continue
            held = 1 + int(
                conn.execute(
                    "SELECT count(*) FROM same_folder_copies WHERE mid = ? AND folder_id = ?",
                    (m, folder_id),
                ).fetchone()[0]
            )
            dates = seen.downloaded_at[m]
            if len(dates) <= held:
                continue
            for d in sorted(set(dates)):
                page = await self._page_window(info, d, d + 1)
                for it in page:
                    if it.headers_only or hmid_mid(it.header_message_id) != m:
                        continue
                    fetched = await self.items.fetch(info, it, counters)
                    if fetched.kind != "copies":
                        continue
                    new = tuple(
                        c
                        for c in fetched.copies
                        if c.record.mid == m
                        and c.record.content_hash not in self.store.location_hashes(m, folder_id)
                    )
                    if not new:
                        continue
                    # before the write, which drops the row that makes it a late download
                    late = self._was_headers_only(folder_id, fetched.hmid_mid, fetched.tb_date, m)
                    await self.items.write(
                        info,
                        replace(fetched, copies=new),
                        source="reconcile",
                        counters=counters,
                    )
                    res.copies += len(new)
                    if not late:
                        res.missed += len(new) * self._missed(applied, folder_id, m, d)
        if res.copies:
            log.info(
                "same-folder copies found by count", extra={"folder": info.hash, "n": res.copies}
            )

    def _trim_headers_only_rows(self, info: FolderInfo, seen: _PassSeen) -> int:
        """Drop each Message-ID's ``headers_only_items`` rows at a second beyond the number of
        headers-only items TB listed there under that hmid over the whole pass
        (``seen.headers_only``; the pass's windows cover every tb_date of the folder), highest
        ordinals first; rows TB still backs are never taken. A row TB does not back is stale: a
        copy recorded headers-only by its event and downloaded later whose write is skipped (its
        hash is already held: a byte-identical copy, which collapses, #30); a replayed or stale
        new-mail event that still says headers-only for an item the index holds downloaded; a
        headers-only copy of a located Message-ID deleted or moved away (the event resyncs the
        location only). A fresh backfill holds none of them. Run after the pass's other steps
        (their writes drop rows themselves); mids covered by a queued event are left to it.
        Returns the number of rows dropped."""
        folder_id = info.folder_id
        queued = self.events.buffered
        conn = self.store.connection
        backed: dict[str, Counter[int]] = {}
        dropped = 0
        with self.store.transaction():
            groups = conn.execute(
                "SELECT mid, tb_date, count(*) FROM headers_only_items "
                "WHERE folder_id = ? AND mid IS NOT NULL GROUP BY mid, tb_date",
                (folder_id,),
            ).fetchall()
            for mid, d, n in groups:
                m, day = str(mid), int(d)
                if m not in backed:
                    backed[m] = Counter(seen.headers_only.get(m, ()))
                keep = backed[m][day]
                if int(n) <= keep or queued.covers(folder_id, m, None):
                    continue
                rows = [
                    int(r[0])
                    for r in conn.execute(
                        "SELECT rowid FROM headers_only_items WHERE folder_id = ? AND mid = ? "
                        "AND tb_date = ? ORDER BY ordinal",
                        (folder_id, m, day),
                    )
                ]
                extra = rows[keep:]
                conn.executemany(
                    "DELETE FROM headers_only_items WHERE rowid = ?", [(r,) for r in extra]
                )
                dropped += len(extra)
        if dropped:
            log.info("stale headers-only rows dropped", extra={"folder": info.hash, "n": dropped})
        return dropped

    def _was_headers_only(self, folder_id: str, hmid_mid: str | None, d: int, mid: str) -> bool:
        """The rule of ``ItemIngester._drop_headers_only_row``: a write of ``mid`` at ``d`` is a
        late download when it will drop a headers-only row (``hmid_mid``: the item's
        ``mid_of(hmid)``, None for an ``md5:`` id)."""
        conn = self.store.connection
        if hmid_mid is not None:
            return (
                conn.execute(
                    "SELECT 1 FROM headers_only_items WHERE folder_id = ? AND mid = ? "
                    "AND tb_date = ?",
                    (folder_id, hmid_mid, d),
                ).fetchone()
                is not None
            )
        if not mid.startswith(X_PREFIX):
            return False
        return (
            conn.execute(
                "SELECT 1 FROM headers_only_items WHERE folder_id = ? AND tb_date = ? "
                "AND mid IS NULL",
                (folder_id, d),
            ).fetchone()
            is not None
        )

    async def _refresh_states(
        self,
        info: FolderInfo,
        lo: int | None,
        hi: int | None,
        items: list[PageItem],
        res: PassResult,
        applied: Coverage,
    ) -> None:
        """Daily: tags/flagged/read of every location of the window set to TB's."""
        folder_id = info.folder_id
        ix = self._index_window(folder_id, lo, hi)
        wanted: dict[str, tuple[LocationState, LocationState, int]] = {}
        anon: dict[int, list[PageItem]] = defaultdict(list)
        for it in items:
            if it.headers_only:
                continue
            d = tb_date_of(it.date)
            st = state_of(it.tags, it.flags.read, it.flags.flagged)
            m = hmid_mid(it.header_message_id)
            if m is None:
                anon[d].append(it)
            elif m in ix.named and m not in wanted:
                wanted[m] = (ix.named[m], st, d)
        for d, its in sorted(anon.items()):
            xs = ix.anon.get(d, {})
            if not xs:
                continue
            if len(its) == 1 and len(xs) == 1:
                (xm,) = xs
                wanted.setdefault(
                    xm, (xs[xm], state_of(its[0].tags, its[0].flags.read, its[0].flags.flagged), d)
                )
                continue
            for it in its:
                xm = await self.fetcher.mid_of_tb(it.tb_id, it.size)
                if xm is not None and xm in xs:
                    st = state_of(it.tags, it.flags.read, it.flags.flagged)
                    wanted.setdefault(xm, (xs[xm], st, d))
        queued = self.events.buffered
        for m, (old, new, d) in sorted(wanted.items()):
            if old == new or queued.covers(folder_id, m, d):
                continue
            self.store.set_location_state(m, folder_id, new, source="reconcile")
            res.state_changes += 1
            res.missed += self._missed(applied, folder_id, m, d)
