"""``IngestService``: the ingest jobs inside ``ternd`` (plan 04 step 9; architecture §2, #16, B3).

Jobs:

- **backfill** (:class:`tern.index.ingest.Backfill`) starts, or resumes from its watermarks, on
  every extension attach (:meth:`IngestService.on_attach`, hooked to ``ExtensionLink.on_attach``),
  and before a scheduled pass while an indexed folder's backfill is not ``done`` (a folder whose
  job failed is resumed there);
- **hourly** reconcile every ``reconcile_interval_s`` and **daily** reconcile every
  ``reconcile_daily_s`` (:class:`tern.index.reconcile.Reconcile`), counted from the end of the
  first backfill attempt after the service started, on the injected :class:`Clock`. A due daily
  pass also counts as the hourly one (it applies the same adds and removes);
- **events**: :meth:`IngestService.submit_event` applies each notification in its own task
  (``EventIngest.on_event``), never in the bridge's reader task: applying an event awaits bridge
  replies (``raw``, ``find``) that only the reader task can deliver, and may wait for a running
  pass (step 8's note). Tasks start in arrival order and ``EventIngest`` serializes them with one
  FIFO lock, so events keep their order. Events run beside the scheduled jobs, not after them,
  so a pass's ``EventIngest.paused`` queues (buffers) the events of the folder it is on;
- **folders**: no event backfills in its own task (``EventIngest.backfill_job``). An event whose
  re-list finds a folder id new to the index (a new or renamed folder, or any folder on a fresh
  index while the first backfill is still listing) upserts the folder rows and wakes the job
  task, which runs the backfill (only while a folder is not ``done``) and then a re-list
  (``EventIngest.sync_folders``, ``"job"`` mode) that removes the folder ids TB no longer lists
  once every indexed folder is ``done``, so a renamed folder's messages do not lose their last
  location (when the new id's backfill fails, the hourly re-list removes the old id). While an
  indexed folder is not ``done``, an event's re-list removes nothing. Only new ids wake the
  job: ``folder.changed`` comes on every count change, so a failed folder is retried before the
  next scheduled pass, as above, not on every event. A request cut short by a pause runs after
  the next attach. Its failures go to the ``'*'`` row.

Backfill, hourly, daily and folders run one at a time, in :meth:`IngestService.run_until`'s task
(step 8: Reconcile and Backfill must not run at the same time; no backfill runs in an event
task).

**Failures (B3).** ``BridgeError("unavailable")`` (no extension, or it went away) pauses the jobs
until the next attach, logged at INFO; nothing is recorded as failed. Any other exception stops
that job's run: ``ingest_state.status = 'error:<ExceptionType>'`` (type name only, never the
message, which can carry mail content) on the folder the run was working on, logged at ERROR by
type and #28 hash (traceback at DEBUG), and the service keeps running. The folder is the one of
the innermost per-folder unit that raised (a backfill folder, a headers-only revisit, a reconcile
pass). A failure outside every folder (account and folder listing, including the inline backfill
of a folder a re-list found, and pending refills, of an hourly or daily run; the folder listing
of a backfill run) goes to the reserved row ``folder_id = '*'`` (#30).

A failed **event** applied in its own task (:meth:`IngestService.submit_event`) also goes to the
``'*'`` row, never to a folder row: a folder's status is its backfill state, and ``error:`` over
``done`` would switch off the done-only logic (reconcile passes, ``x_mid_churn``) and re-run its
backfill until it is ``done`` again, for a miss reconcile repairs anyway. The ERROR log line
names the folder (#28 hash): the innermost per-folder unit that raised (an hourly check the
event asked for), else the event's indexed folders; when the failure is that of an older
queued event the event drained first (``EventIngest.on_event``, which sets
``tern.index.events.failed_event_folders``), that queued event's indexed folders (the arriving
event is still applied when no queued event is left on its folders). An event that was queued
during a reconcile pass and is applied at the pass's exit is not covered by this: see
Limitations.

An error status is cleared by the next successful run of the same scope: a folder's by its
resumed backfill (``done``), the ``'*'`` row by the next hourly or daily run that completes and
started after the row was written (``updated_at`` < the run's start second; those runs repeat
every unscoped step and cover what a failed event missed). ``tern index status`` counts both in
``jobs_failed`` (plan step 10).

A call to :meth:`IngestService.submit_event` with params that are not a 1.0 event model is a
programming error: ``TypeError``, raised to the caller. Once :meth:`IngestService.run_until` has
returned, events are no longer applied (logged at INFO; the next start's reconcile repairs them).

Failure attribution relies on overriding private per-folder methods of ``Backfill`` and
``Reconcile`` (:class:`_Backfill`, :class:`_Reconcile`; step 9 does not list ``ingest.py`` or
``reconcile.py``). :func:`_check_overrides` runs at import: if a base method is renamed or its
parameters change, importing this module raises ``TypeError`` (ternd does not start) instead of
failures silently moving to the ``'*'`` row. Follow-up: public per-folder hooks.

Shutdown: child tasks (event tasks, the waits of :meth:`IngestService._wait`) are stopped with
:func:`cancel_and_wait`, which swallows only the children's own cancellation; a cancellation of
the calling task is passed on and re-raised.

Limitations:

- Events run beside a backfill (only reconcile passes pause a folder's events), so the two
  step 8 races stay open (plan 04 "Limitation (steps 8-9)"; follow-up for step 10 / plan 05):
  an event that clears or refills a message between ``IngestWrite._content_raw``'s read of its
  content copy and the write transaction makes the backfill raise ``ValueError``; an event that
  removes a message's last location with a refill candidate a concurrent backfill write made
  stale raises ``StoreError``. Both are loud (``error:ValueError`` / ``error:StoreError`` on the
  folder, resumed by the next backfill or repaired by reconcile), never silent. Not fixed here:
  pausing a folder's events for its whole backfill does not cover events on other folders that
  touch the same message, and pausing every folder would hold events for hours on a first
  backfill; the fix belongs in ``ingest.py``/``events.py`` (re-check under the write).
- An event queued during a reconcile pass (``EventIngest.paused``) is applied at the pass's
  normal exit (a pass that raises or is cancelled leaves it queued, so its exception or
  cancellation propagates unchanged),
  in :meth:`IngestService.run_until`'s task, and its failure propagates into that hourly/daily
  job: the job stops (later folders wait for the next run) and the failure is recorded as
  ``error:<Type>`` on the folder of the pass (``_Reconcile._pass`` set it), not on the ``'*'``
  row, so a ``done`` folder turns ``error:`` and the next scheduled run resumes its backfill from
  its watermarks before reconciling again. The failed event is not retried (the next run repairs
  its change); queued events behind it stay queued and are applied, in order, before the next
  event (``EventIngest.on_event`` drains them first, so a newer event on their folder never
  overtakes them) or when the next pass ends. This matches the plan's "that job stops, error on
  the folder" rule, not the event rule above. Routing these
  failures to ``'*'`` needs a per-event catch at the ``paused`` exit in ``events.py``, outside
  step 9's files: follow-up. Pinned by ``test_a_failed_queued_event_marks_the_pass_folder``.
- Pending event tasks are not capped: a burst of notifications during a long pass queues one
  task each on the ``EventIngest`` lock. Dropping the overflow to reconcile would count those
  changes as missed (#16's event-reliability counter), so no cap is applied here.
- A failure the index cannot record (``sqlite3.Error`` in :meth:`IngestService._write_status`,
  e.g. disk full) is only in the ERROR log and in :attr:`IngestService.outcomes` (a
  :class:`JobOutcome` with no ``folder_ids``): ``tern index status`` reads the index, so its
  ``jobs_failed`` can understate failures then. A status assembled inside ``ternd`` (plan 08b's
  ``index.status``) should count those outcomes (#30).
- :meth:`IngestService.event_lag_s` (bridge ``index.status.lag_s``, plan 08b step 2) sees the
  events whose task has not ended. An event queued during a reconcile pass
  (``EventIngest.paused``) ends its task once queued, so it does not count while it waits for
  the pass to end (that folder's pass covers it); counting it needs a hook in ``events.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
import time
from collections import deque
from collections.abc import Collection
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from tern.bridge.protocol import (
    BridgeError,
    FolderChanged,
    MailDeleted,
    MailMoved,
    MailNew,
    MailUpdated,
)
from tern.config import TernConfig
from tern.index.events import (
    EventIngest,
    EventParams,
    event_folders,
    failed_event_folders,
    folder_infos,
)
from tern.index.ingest import STATUS_DONE, Backfill, Counters, FolderInfo, _Progress
from tern.index.reconcile import PassKind, PassResult, Reconcile
from tern.index.source import IngestSource
from tern.index.store import Store, StoreError

log = logging.getLogger(__name__)

#: The 1.0 notifications ``ternd`` hands to :meth:`IngestService.submit_event`.
EVENT_METHODS = (
    "event.mail.new",
    "event.mail.moved",
    "event.mail.deleted",
    "event.mail.updated",
    "event.folder.changed",
)
#: ``ingest_state`` row for failures not tied to one folder (#30).
ALL_FOLDERS = "*"
ERROR_PREFIX = "error:"
#: How many :class:`JobOutcome`\ s the service keeps (one per job run or failed event).
OUTCOMES_KEPT = 256

JobName = Literal["backfill", "hourly", "daily", "event", "folders"]


class Clock(Protocol):
    """Wall-clock time and sleeping; injectable so tests can drive the schedule. Sleeping takes
    an absolute time, so a clock that moves before the sleep starts cannot push it later."""

    def time(self) -> float: ...

    async def sleep_until(self, when: float) -> None: ...


class SystemClock:
    def time(self) -> float:
        return time.time()

    async def sleep_until(self, when: float) -> None:
        await asyncio.sleep(max(0.0, when - time.time()))


@dataclass(frozen=True)
class JobOutcome:
    """One finished job run: ``status`` is ``ok``, ``paused`` (jobs wait for the next attach),
    ``unavailable`` (an event not applied: no extension) or ``error:<Type>``; ``folder_ids`` are
    the ``ingest_state`` rows an error was written to (empty when that write itself failed: the
    error is then only in the ERROR log)."""

    job: JobName
    status: str
    folder_ids: tuple[str, ...] = ()


# The folder a job is working on, per asyncio task. Set by the per-folder units below and left
# set when one raises, so the job's handler (same task) knows where the failure belongs.
_FOLDER: ContextVar[FolderInfo | None] = ContextVar("tern_ingest_folder", default=None)


class _Backfill(Backfill):
    """``Backfill`` that marks its per-folder units for failure attribution. Overrides private
    methods of ``tern.index.ingest`` (step 9 does not list ``ingest.py``; recorded). The mark is
    not reset when a unit raises: the job's handler (same task) reads the failing folder."""

    async def _backfill_folder(self, folder: FolderInfo, progress: _Progress) -> Counters:
        token = _FOLDER.set(folder)
        counters = await super()._backfill_folder(folder, progress)
        _FOLDER.reset(token)
        return counters

    async def _revisit_headers_only(
        self, folder: FolderInfo, progress: _Progress
    ) -> Counters | None:
        token = _FOLDER.set(folder)
        counters = await super()._revisit_headers_only(folder, progress)
        _FOLDER.reset(token)
        return counters


class _Reconcile(Reconcile):
    """``Reconcile`` that marks each folder pass for failure attribution (private override, as
    :class:`_Backfill`)."""

    async def _pass(self, info: FolderInfo, kind: PassKind, tb_total: int) -> PassResult | None:
        token = _FOLDER.set(info)
        result = await super()._pass(info, kind, tb_total)
        _FOLDER.reset(token)
        return result


def _check_overrides(cls: type, base: type) -> None:
    """``TypeError`` unless every method ``cls`` defines overrides a method of ``base`` with the
    same parameter names: an override whose base method was renamed would never run (failure
    attribution would silently stop)."""
    for name, attr in vars(cls).items():
        if name.startswith("__") or not callable(attr):
            continue
        original = getattr(base, name, None)
        if not callable(original):
            raise TypeError(f"{cls.__name__}.{name} overrides nothing in {base.__name__}")
        if list(inspect.signature(attr).parameters) != list(inspect.signature(original).parameters):
            raise TypeError(f"{cls.__name__}.{name} parameters differ from {base.__name__}'s")


_check_overrides(_Backfill, Backfill)
_check_overrides(_Reconcile, Reconcile)


async def cancel_and_wait(*tasks: asyncio.Future[Any]) -> None:
    """Cancel ``tasks`` and wait until every one has finished. Their own cancellation is expected
    and not raised; another exception of theirs is re-raised (the first, in argument order, once
    all have finished). A cancellation of the calling task while it waits is passed on to the
    tasks and re-raised once they have finished (``asyncio.gather`` semantics); one already
    propagating (a caller's ``finally``) goes on after this returns. Unlike ``await task`` under
    ``contextlib.suppress(CancelledError)``, the caller's own cancellation is never swallowed."""
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result


class IngestService:
    """The ingest jobs of ``ternd``; see the module docstring."""

    def __init__(
        self,
        store: Store,
        source: IngestSource,
        cfg: TernConfig,
        clock: Clock | None = None,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.clock: Clock = clock if clock is not None else SystemClock()
        now = self.clock.time
        self.events = EventIngest(store, source, cfg, clock=now)
        self.reconcile = _Reconcile(store, source, cfg, events=self.events, clock=now)
        # One fetch throttle for all three jobs (ingest_max_msgs_per_s caps TB's load).
        self.backfill = _Backfill(
            store, source, cfg, clock=now, throttle=self.events.items.throttle
        )
        self._folders_wanted = asyncio.Event()  # an event found new folder ids
        self.events.backfill_job = self._folders_wanted.set
        #: The latest finished job runs, oldest first (type names only; bounded).
        self.outcomes: deque[JobOutcome] = deque(maxlen=OUTCOMES_KEPT)
        self._attached = asyncio.Event()
        self._paused = True  # no job runs before the first attach
        self._next_hourly: float | None = None
        self._next_daily: float | None = None
        #: Each running event task and when its event was received (``time.monotonic()``).
        self._event_tasks: dict[asyncio.Task[None], float] = {}
        self._stopped = False  # run_until returned: events are no longer applied

    # -- inputs ----------------------------------------------------------------------------------

    def on_attach(self) -> None:
        """An extension (re)attached: backfill starts or resumes, paused jobs go on."""
        self._attached.set()

    def submit_event(self, method: str, params: BaseModel) -> None:
        """Apply one notification in its own task (see the module docstring); returns at once.
        ``TypeError`` for params that are not a 1.0 event model (a caller bug, not a TB event)."""
        if not isinstance(params, MailNew | MailMoved | MailDeleted | MailUpdated | FolderChanged):
            raise TypeError(f"{method} params are {type(params).__name__}, not an event model")
        if self._stopped:
            log.info("event not applied: ingest service stopped", extra={"method": method})
            return
        received = time.monotonic()
        task = asyncio.create_task(self._apply_event(method, params))
        self._event_tasks[task] = received
        task.add_done_callback(self._event_task_done)

    def _event_task_done(self, task: asyncio.Task[None]) -> None:
        del self._event_tasks[task]

    # -- status ----------------------------------------------------------------------------------

    def event_lag_s(self, now: float) -> float:
        """The age in seconds, at ``now`` (a ``time.monotonic()`` reading), of the oldest event
        received by :meth:`submit_event` whose task has not ended (applied, failed or
        cancelled); ``0.0`` when there is none. Read-only (bridge ``index.status.lag_s``, plan
        08b step 2); see Limitations for events queued during a reconcile pass."""
        if not self._event_tasks:
            return 0.0
        return now - min(self._event_tasks.values())

    # -- the job loop ----------------------------------------------------------------------------

    async def run_until(self, stop: asyncio.Event) -> None:
        """Run the jobs until ``stop`` is set (checked between job runs; cancel the task to stop
        within one). Pending event tasks are cancelled on the way out."""
        try:
            while not stop.is_set():
                await self._step(stop)
        finally:
            self._stopped = True
            await cancel_and_wait(*self._event_tasks)

    async def _step(self, stop: asyncio.Event) -> None:
        if self._attached.is_set():
            self._attached.clear()
            self._paused = False
            await self._job("backfill")
            if not self._paused and (self._next_hourly is None or self._next_daily is None):
                now = self.clock.time()
                self._next_hourly = now + self.cfg.reconcile_interval_s
                self._next_daily = now + self.cfg.reconcile_daily_s
            return
        if self._folders_wanted.is_set() and not self._paused:
            # An event's re-list found new folder ids and left them to this task: backfill
            # them, then re-list (removes vanished ids once every folder is done). A request
            # cut short by a pause is kept for the step after the next attach.
            self._folders_wanted.clear()
            await self._job("backfill", only_if_incomplete=True)
            if not self._paused:
                await self._job("folders")
            if self._paused:
                self._folders_wanted.set()
            return
        if self._paused or self._next_hourly is None or self._next_daily is None:
            await self._wait(stop, None)
            return
        now = self.clock.time()
        due = min(self._next_hourly, self._next_daily)
        if now < due:
            await self._wait(stop, due)
            return
        kind: JobName = "daily" if now >= self._next_daily else "hourly"
        await self._job("backfill", only_if_incomplete=True)
        if self._paused:
            return  # the due pass runs after the next attach
        await self._job(kind)
        if self._paused:
            return
        done = self.clock.time()
        self._next_hourly = done + self.cfg.reconcile_interval_s
        if kind == "daily":
            self._next_daily = done + self.cfg.reconcile_daily_s

    async def _wait(self, stop: asyncio.Event, until: float | None) -> None:
        """Until ``stop``, an attach, the clock reaching ``until``, or (unless paused) an event
        finding new folder ids."""
        waiters: list[asyncio.Task[object]] = [
            asyncio.create_task(stop.wait()),
            asyncio.create_task(self._attached.wait()),
        ]
        if not self._paused:
            waiters.append(asyncio.create_task(self._folders_wanted.wait()))
        if until is not None:
            waiters.append(asyncio.create_task(self.clock.sleep_until(until)))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await cancel_and_wait(*waiters)

    def _backfill_incomplete(self) -> bool:
        indexed, _ = folder_infos(self.store)
        done = {
            str(r[0])
            for r in self.store.connection.execute(
                "SELECT folder_id FROM ingest_state WHERE status = ?", (STATUS_DONE,)
            )
        }
        return bool(set(indexed) - done)

    async def _job(self, name: JobName, *, only_if_incomplete: bool = False) -> None:
        """Run one job and record its outcome; with ``only_if_incomplete`` the backfill runs
        only while an indexed folder is not ``done`` (no outcome otherwise)."""
        token = _FOLDER.set(None)
        started = int(self.clock.time())
        try:
            if name == "backfill":
                if only_if_incomplete and not self._backfill_incomplete():
                    return
                await self.backfill.run()
            elif name == "folders":
                await self.events.sync_folders("event", mode="job")
            elif name == "hourly":
                await self.reconcile.hourly()
                self._clear_all_folders_error(started)
            else:
                await self.reconcile.daily()
                self._clear_all_folders_error(started)
        # B3 boundary: a failed job is recorded (ingest_state + ERROR log) and the service goes
        # on; nothing is swallowed or replaced by a default result.
        except Exception as exc:
            if isinstance(exc, BridgeError) and exc.name == "unavailable":
                self._paused = True
                self.outcomes.append(JobOutcome(name, "paused"))
                log.info("ingest paused until the extension attaches again", extra={"job": name})
            else:
                folder = _FOLDER.get()
                self._failed(name, exc, [] if folder is None else [folder])
        else:
            self.outcomes.append(JobOutcome(name, "ok"))
        finally:
            _FOLDER.reset(token)

    # -- events ----------------------------------------------------------------------------------

    async def _apply_event(self, method: str, params: EventParams) -> None:
        token = _FOLDER.set(None)
        failed_token = failed_event_folders.set(None)
        try:
            await self.events.on_event(method, params)
        # B3 boundary, as in _job.
        except Exception as exc:
            if isinstance(exc, BridgeError) and exc.name == "unavailable":
                # The extension went away mid-event; reconcile repairs what it would have done.
                self.outcomes.append(JobOutcome("event", "unavailable"))
                log.info("event not applied: extension unavailable", extra={"method": method})
            else:
                folder = _FOLDER.get()
                if folder is not None:
                    folders = [folder]
                else:
                    # an older queued event that failed when this one drained it: its folders
                    queued = failed_event_folders.get()
                    folders = self._indexed(event_folders(params) if queued is None else queued)
                # The '*' row, never the folders' rows: those hold the backfill state (module
                # docstring); the log line names the folders.
                self._failed("event", exc, folders, row=ALL_FOLDERS)
        finally:
            failed_event_folders.reset(failed_token)
            _FOLDER.reset(token)

    def _indexed(self, ids: Collection[str]) -> list[FolderInfo]:
        """The indexed folders among ``ids`` ([] → the ``'*'`` row)."""
        try:
            indexed, _ = folder_infos(self.store)
        except (StoreError, sqlite3.Error) as exc:
            # The index cannot say which folders are indexed: the failure goes to '*', and why.
            log.error("event folders not resolved", extra={"error": type(exc).__name__})
            return []
        return [indexed[f] for f in sorted(ids) if f in indexed]

    # -- failure bookkeeping ---------------------------------------------------------------------

    def _failed(
        self,
        job: JobName,
        exc: Exception,
        folders: list[FolderInfo],
        *,
        row: str | None = None,
    ) -> None:
        """Record a failed job: ``error:<Type>`` on ``row`` when given, else on ``folders``'
        rows (``'*'`` when there are none); the ERROR log names ``folders``."""
        status = ERROR_PREFIX + type(exc).__name__
        if row is not None:
            ids = [row]
        else:
            ids = [f.folder_id for f in folders] or [ALL_FOLDERS]
        written = self._write_status(ids, status)
        self.outcomes.append(JobOutcome(job, status, tuple(ids) if written else ()))
        extra: dict[str, Any] = {
            "job": job,
            "error": type(exc).__name__,
            "folders": [f.hash for f in folders] or [ALL_FOLDERS],
        }
        if isinstance(exc, BridgeError):
            # §3 error name and TB's error code: identifiers, never mail content (BridgeError
            # contract), so a real-profile failure explains itself in the log (2026-11-15).
            extra["bridge_error"] = exc.name
            extra["tb_code"] = exc.tb_code
        log.error("ingest job failed", extra=extra)
        log.debug("ingest job traceback", exc_info=exc, extra={"job": job})

    def _write_status(self, folder_ids: list[str], status: str) -> bool:
        """Set ``ingest_state.status`` (watermarks kept: a failed folder resumes from them);
        False when the index could not record it."""
        now = int(self.clock.time())
        try:
            with self.store.transaction():
                for folder_id in folder_ids:
                    self.store.connection.execute(
                        "INSERT INTO ingest_state(folder_id, status, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(folder_id) DO UPDATE SET status = excluded.status, "
                        "updated_at = excluded.updated_at",
                        (folder_id, status, now),
                    )
        except sqlite3.Error as exc:
            # The index itself failed (disk full, I/O error): the job failure above is still
            # logged at ERROR; this says it could not be recorded. The service goes on (B3).
            log.error("ingest failure not recorded", extra={"error": type(exc).__name__})
            return False
        return True

    def _clear_all_folders_error(self, started: int) -> None:
        """A run that started at second ``started`` completed: clear a ``'*'`` error written
        before that second. One written during the run (e.g. an event that failed while the
        run was past its folder) stays until the next run."""
        with self.store.transaction():
            n = self.store.connection.execute(
                "DELETE FROM ingest_state WHERE folder_id = ? AND status LIKE ? AND updated_at < ?",
                (ALL_FOLDERS, ERROR_PREFIX + "%", started),
            ).rowcount
        if n:
            log.info("job failure cleared", extra={"folder": ALL_FOLDERS})
