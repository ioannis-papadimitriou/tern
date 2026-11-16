"""Plan 04 step 9: `IngestService`, the ingest jobs of ternd, over FakeBridge.

A fake clock drives the hourly and daily schedule; faults are injected at the requester (what
the extension link would raise). Expected numbers come from MANIFEST.json.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import test_fixture_manifest as tfm
from pydantic import BaseModel

from tern.bridge.protocol import BridgeError, FoldersListParams, PageParams
from tern.config import TernConfig
from tern.index.events import folder_infos
from tern.index.ingest import Backfill
from tern.index.mid import mid_of
from tern.index.service import ALL_FOLDERS, IngestService, JobOutcome, _check_overrides
from tern.index.source import IngestSource
from tern.index.store import Store
from tern.testing.fake_bridge import FakeBridge

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
COPIES = MANIFEST["message_count"]
DISTINCT = MANIFEST["distinct_messages"]
FOLDER_IDS = [f"{a['key']}:/{f['path']}" for a in MANIFEST["accounts"] for f in a["folders"]]
FAULTY = "acct2:/Projects"
HOUR = TernConfig().reconcile_interval_s
DAY = TernConfig().reconcile_daily_s
DEADLINE_S = 10.0


class Injected(RuntimeError):
    """A failure injected by the test (carries no content)."""


Fault = Callable[[str, BaseModel], BaseException | None]
Hook = Callable[[str, BaseModel], Awaitable[None]]


class FaultyRequester:
    """FakeBridge behind a switchable fault: ``fault(method, params)`` returns the exception
    to raise instead of answering, or None. ``hook(method, params)``, when set, is awaited
    before each request (the request waits for it)."""

    def __init__(self, fb: FakeBridge) -> None:
        self.fb = fb
        self.fault: Fault | None = None
        self.hook: Hook | None = None

    async def request(self, method: str, params: BaseModel) -> Any:
        if self.hook is not None:
            await self.hook(method, params)
        if self.fault is not None and (exc := self.fault(method, params)) is not None:
            raise exc
        return await self.fb.request(method, params)

    async def notify(self, method: str, params: BaseModel) -> None:
        await self.fb.notify(method, params)


class FakeClock:
    """Time moves only when the test calls ``advance``; ``sleep_until`` waits for it."""

    def __init__(self, now: float) -> None:
        self.now = now
        self._cond = asyncio.Condition()

    def time(self) -> float:
        return self.now

    async def sleep_until(self, when: float) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: self.now >= when)

    async def advance(self, seconds: float) -> None:
        async with self._cond:
            self.now += seconds
            self._cond.notify_all()


@dataclass
class Running:
    service: IngestService
    store: Store
    fb: FakeBridge
    req: FaultyRequester
    clock: FakeClock
    task: asyncio.Task[None]

    async def until(self, cond: Callable[[], bool], what: str) -> None:
        deadline = time.perf_counter() + DEADLINE_S
        while not cond():
            if self.task.done():
                self.task.result()  # re-raise what killed the service
                raise AssertionError(f"service stopped before {what}")
            assert time.perf_counter() < deadline, f"timed out waiting for {what}"
            await asyncio.sleep(0.005)

    async def outcome(self, n: int) -> JobOutcome:
        """The ``n``-th job outcome (1-based), once it exists."""
        await self.until(lambda: len(self.service.outcomes) >= n, f"job outcome {n}")
        return self.service.outcomes[n - 1]

    async def attach_and_backfill(self) -> None:
        n = len(self.service.outcomes)
        self.service.on_attach()
        assert await self.outcome(n + 1) == JobOutcome("backfill", "ok")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "data" / "index.db")
    yield s
    s.close()


@pytest_asyncio.fixture
async def running(store: Store) -> AsyncIterator[Running]:
    fb = FakeBridge(FIXTURE)
    req = FaultyRequester(fb)
    clock = FakeClock(float(int(time.time())))  # whole seconds: exact float sums
    service = IngestService(store, IngestSource(req), TernConfig(), clock)

    async def to_service(method: str, params: BaseModel) -> None:
        service.submit_event(method, params)

    fb.on_event = to_service
    stop = asyncio.Event()
    task = asyncio.create_task(service.run_until(stop))
    yield Running(service, store, fb, req, clock, task)
    stop.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def statuses(store: Store) -> dict[str, str]:
    return {
        str(f): str(s)
        for f, s in store.connection.execute("SELECT folder_id, status FROM ingest_state")
    }


def run_kinds(store: Store) -> dict[str, int]:
    return dict(store.connection.execute("SELECT kind, count(*) FROM ingest_runs GROUP BY kind"))


def counts(store: Store) -> tuple[int, int]:
    st = store.status()
    return st.messages, st.locations


def page_of(folder_id: str) -> Fault:
    def fault(method: str, params: BaseModel) -> BaseException | None:
        if (
            method == "mail.messages.page"
            and isinstance(params, PageParams)
            and params.folder_id == folder_id
        ):
            return Injected()
        return None

    return fault


# ---- schedule ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_runs_before_an_extension_attaches(running: Running) -> None:
    await running.clock.advance(DAY + HOUR)
    await asyncio.sleep(0.05)
    assert list(running.service.outcomes) == []
    assert counts(running.store) == (0, 0)


@pytest.mark.asyncio
async def test_attach_starts_the_backfill(running: Running) -> None:
    await running.attach_and_backfill()
    assert counts(running.store) == (DISTINCT, COPIES)
    assert set(statuses(running.store).values()) == {"done"}


@pytest.mark.asyncio
async def test_fake_clock_drives_one_hourly_and_one_daily_pass(running: Running) -> None:
    await running.attach_and_backfill()
    n_folders = len(statuses(running.store))
    await asyncio.sleep(0.05)
    assert run_kinds(running.store).get("hourly", 0) == 0  # not due yet

    await running.clock.advance(HOUR)
    assert await running.outcome(2) == JobOutcome("hourly", "ok")
    assert run_kinds(running.store).get("hourly") == n_folders
    assert run_kinds(running.store).get("daily", 0) == 0

    await running.clock.advance(DAY - HOUR)
    assert await running.outcome(3) == JobOutcome("daily", "ok")
    kinds = run_kinds(running.store)
    assert (kinds.get("hourly"), kinds.get("daily")) == (n_folders, n_folders)
    assert len(running.service.outcomes) == 3
    assert counts(running.store) == (DISTINCT, COPIES)


# ---- failures (B3) -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injected_exception_marks_its_folder_and_the_service_keeps_running(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    running.req.fault = page_of(FAULTY)
    caplog.set_level(logging.DEBUG)
    running.service.on_attach()
    assert await running.outcome(1) == JobOutcome("backfill", "error:Injected", (FAULTY,))
    assert statuses(running.store)[FAULTY] == "error:Injected"
    assert not running.task.done()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == ["ingest job failed"]
    assert errors[0].__dict__["error"] == "Injected"
    assert all("Projects" not in str(r.__dict__) for r in caplog.records)

    # the next attach resumes the backfill; the folder's status is done again
    running.req.fault = None
    await running.attach_and_backfill()
    assert statuses(running.store)[FAULTY] == "done"
    assert counts(running.store) == (DISTINCT, COPIES)


@pytest.mark.asyncio
async def test_a_failed_folder_is_resumed_before_the_next_pass(running: Running) -> None:
    running.req.fault = page_of(FAULTY)
    running.service.on_attach()
    assert (await running.outcome(1)).status == "error:Injected"
    running.req.fault = None
    await running.clock.advance(HOUR)  # no attach: the pass's turn resumes the backfill first
    assert await running.outcome(2) == JobOutcome("backfill", "ok")
    assert await running.outcome(3) == JobOutcome("hourly", "ok")
    assert set(statuses(running.store).values()) == {"done"}
    assert counts(running.store) == (DISTINCT, COPIES)


@pytest.mark.asyncio
async def test_hourly_account_listing_failure_goes_to_the_star_row(running: Running) -> None:
    await running.attach_and_backfill()
    running.req.fault = lambda m, p: Injected() if m == "mail.accounts.list" else None
    await running.clock.advance(HOUR)
    assert await running.outcome(2) == JobOutcome("hourly", "error:Injected", (ALL_FOLDERS,))
    st = statuses(running.store)
    assert st[ALL_FOLDERS] == "error:Injected"
    assert {s for f, s in st.items() if f != ALL_FOLDERS} == {"done"}
    assert not running.task.done()

    # the next hourly run that completes clears the '*' row
    running.req.fault = None
    await running.clock.advance(HOUR)
    assert await running.outcome(3) == JobOutcome("hourly", "ok")
    assert ALL_FOLDERS not in statuses(running.store)


@pytest.mark.asyncio
async def test_daily_folder_listing_failure_goes_to_the_star_row(running: Running) -> None:
    await running.attach_and_backfill()

    def fault(method: str, params: BaseModel) -> BaseException | None:
        if method == "mail.folders.list" and isinstance(params, FoldersListParams):
            return Injected()
        return None

    running.req.fault = fault
    await running.clock.advance(DAY)
    assert await running.outcome(2) == JobOutcome("daily", "error:Injected", (ALL_FOLDERS,))
    assert not running.task.done()


@pytest.mark.asyncio
async def test_a_failure_the_index_cannot_record_claims_no_written_row(
    running: Running, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The status write itself fails (the index is broken): the outcome names no row, and both
    the job failure and the unrecorded write are logged at ERROR; the service keeps running."""
    await running.attach_and_backfill()

    def broken() -> None:
        raise sqlite3.OperationalError("test: disk I/O error")

    def fault(method: str, params: BaseModel) -> BaseException | None:
        if method == "mail.accounts.list":
            monkeypatch.setattr(running.store, "transaction", broken)
            return Injected()
        return None

    running.req.fault = fault
    caplog.set_level(logging.INFO)
    await running.clock.advance(HOUR)
    assert await running.outcome(2) == JobOutcome("hourly", "error:Injected", ())
    monkeypatch.undo()
    assert ALL_FOLDERS not in statuses(running.store)
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == ["ingest failure not recorded", "ingest job failed"]
    assert not running.task.done()


@pytest.mark.asyncio
async def test_unavailable_pauses_the_jobs_until_the_next_attach(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    running.req.fault = lambda m, p: BridgeError("unavailable", "test: no extension")
    caplog.set_level(logging.INFO)
    running.service.on_attach()
    assert await running.outcome(1) == JobOutcome("backfill", "paused")
    assert not any(s.startswith("error:") for s in statuses(running.store).values())
    assert "ingest paused until the extension attaches again" in caplog.messages
    await running.clock.advance(DAY + HOUR)
    await asyncio.sleep(0.05)
    assert len(running.service.outcomes) == 1  # paused: no pass, no retry

    running.req.fault = None
    await running.attach_and_backfill()
    assert counts(running.store) == (DISTINCT, COPIES)


EVENT_FOLDER = "acct1:/Newsletters"
EVENT_RAW = (
    b"Message-ID: <svc-event@example.test>\nDate: Mon, 02 Nov 2026 10:00:00 +0000\n"
    b"From: a@example.test\nSubject: synthetic\n\nbody\n"
)


def raw_fails(method: str, params: BaseModel) -> BaseException | None:
    return Injected() if method == "mail.messages.raw" else None


@pytest.mark.asyncio
async def test_a_bridge_failure_logs_its_error_name_and_tb_code(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    """A real-profile failure must explain itself: the log names the §3 error and TB's code
    (identifiers only), never the error's message text."""
    await running.attach_and_backfill()

    # A tb_error of raw/full is per item since plan 04 merge review M5 (next test); one of
    # find (the re-resolve of a stale tb_id) still fails the job.
    def tb_error(method: str, params: BaseModel) -> BaseException | None:
        if method == "mail.messages.raw":
            return BridgeError("stale_id", "synthetic: id went stale")
        if method == "mail.messages.find":
            return BridgeError("tb_error", "SENTINEL-msg-text", tb_code="NS_ERROR_FAILURE")
        return None

    running.req.fault = tb_error
    caplog.set_level(logging.INFO)
    await running.fb.deliver(EVENT_FOLDER, EVENT_RAW)
    assert await running.outcome(2) == JobOutcome("event", "error:BridgeError", (ALL_FOLDERS,))
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == ["ingest job failed"]
    assert errors[0].__dict__["bridge_error"] == "tb_error"
    assert errors[0].__dict__["tb_code"] == "NS_ERROR_FAILURE"
    assert all("SENTINEL-msg-text" not in str(r.__dict__) for r in errors)


@pytest.mark.asyncio
async def test_a_tb_error_reading_one_event_item_skips_it_and_hourly_retries_it(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    """Plan 04 merge review M5: TB failing to read one message (raw gives tb_error) skips that
    item (a ``tb_error`` skipped row, INFO log with TB's code only) instead of failing the job;
    the hourly pass fetches the Message-ID item again and indexes it once TB reads it."""
    await running.attach_and_backfill()
    n_folders = len(statuses(running.store))

    def tb_error(method: str, params: BaseModel) -> BaseException | None:
        if method == "mail.messages.raw":
            return BridgeError("tb_error", "SENTINEL-msg-text", tb_code="NS_ERROR_FAILURE")
        return None

    def skipped() -> list[tuple[str, str]]:
        return [
            (str(f), str(r))
            for f, r in running.store.connection.execute(
                "SELECT folder_id, reason FROM skipped_items"
            )
        ]

    running.req.fault = tb_error
    caplog.set_level(logging.INFO)
    await running.fb.deliver(EVENT_FOLDER, EVENT_RAW)
    await running.until(lambda: bool(skipped()), "the skipped row")
    assert skipped() == [(EVENT_FOLDER, "tb_error")]
    assert running.store.status().skipped_tb_error == 1
    assert counts(running.store) == (DISTINCT, COPIES)
    assert len(running.service.outcomes) == 1  # no failed job
    assert ALL_FOLDERS not in statuses(running.store)
    [rec] = [r for r in caplog.records if r.getMessage() == "item skipped: TB could not read it"]
    assert rec.levelno == logging.INFO and rec.__dict__["tb_code"] == "NS_ERROR_FAILURE"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert all("SENTINEL-msg-text" not in str(r.__dict__) for r in caplog.records)

    running.req.fault = None
    await running.clock.advance(HOUR)
    assert await running.outcome(2) == JobOutcome("hourly", "ok")
    assert run_kinds(running.store).get("hourly") == n_folders
    assert counts(running.store) == (DISTINCT + 1, COPIES + 1)
    assert skipped() == []
    assert running.store.status().skipped_tb_error == 0


@pytest.mark.asyncio
async def test_a_failed_event_goes_to_the_star_row_and_keeps_its_folder_done(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    """A folder's status is its backfill state: an event failure must not turn ``done`` into
    ``error:``, which would make the next pass re-run its backfill instead of reconciling it."""
    await running.attach_and_backfill()
    n_folders = len(statuses(running.store))
    running.req.fault = raw_fails
    caplog.set_level(logging.INFO)
    await running.fb.deliver(EVENT_FOLDER, EVENT_RAW)
    assert await running.outcome(2) == JobOutcome("event", "error:Injected", (ALL_FOLDERS,))
    st = statuses(running.store)
    assert st[ALL_FOLDERS] == "error:Injected"
    assert {s for f, s in st.items() if f != ALL_FOLDERS} == {"done"}
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == ["ingest job failed"]
    logged = errors[0].__dict__["folders"]
    assert len(logged) == 1 and logged != [ALL_FOLDERS]  # the event's folder, by #28 hash
    assert all("Newsletters" not in str(r.__dict__) for r in caplog.records)
    assert not running.task.done()

    # the next due pass is the hourly one (no backfill re-run): it covers every folder, repairs
    # the message the event missed and clears the '*' row
    running.req.fault = None
    await running.clock.advance(HOUR)
    assert await running.outcome(3) == JobOutcome("hourly", "ok")
    assert run_kinds(running.store).get("hourly") == n_folders
    assert counts(running.store) == (DISTINCT + 1, COPIES + 1)
    assert ALL_FOLDERS not in statuses(running.store)


@pytest.mark.asyncio
async def test_an_event_failure_during_a_pass_outlives_that_pass(running: Running) -> None:
    """The pass may already be past the event's folder: its completion does not clear a ``'*'``
    error written while it ran; the next pass does."""
    await running.attach_and_backfill()
    fired = False

    async def fail_an_event_mid_pass(method: str, params: BaseModel) -> None:
        nonlocal fired
        if fired or not (isinstance(params, PageParams) and params.folder_id == FAULTY):
            return
        fired = True
        running.req.fault = raw_fails
        await running.fb.deliver(EVENT_FOLDER, EVENT_RAW)
        await running.until(lambda: len(running.service.outcomes) >= 2, "the event failure")
        running.req.fault = None

    running.req.hook = fail_an_event_mid_pass
    await running.clock.advance(HOUR)
    assert await running.outcome(3) == JobOutcome("hourly", "ok")
    assert fired
    assert running.service.outcomes[1] == JobOutcome("event", "error:Injected", (ALL_FOLDERS,))
    assert statuses(running.store)[ALL_FOLDERS] == "error:Injected"

    running.req.hook = None
    await running.clock.advance(HOUR)
    assert await running.outcome(4) == JobOutcome("hourly", "ok")
    assert ALL_FOLDERS not in statuses(running.store)


@pytest.mark.asyncio
async def test_a_failed_queued_event_marks_the_pass_folder(running: Running) -> None:
    """Pins a documented limitation (service.py "Limitations"): an event queued during a pass is
    applied at the pass's exit, in the job's task, so its failure is the hourly job's and lands
    on the pass's folder, not on the ``'*'`` row. The resumed backfill and the next pass repair
    the folder and the message."""
    await running.attach_and_backfill()
    queued_mid = mid_of("svc-event@example.test")
    fired = False

    async def queue_a_failing_event(method: str, params: BaseModel) -> None:
        nonlocal fired
        if fired or not isinstance(params, PageParams):
            return
        if params.from_date is not None or params.folder_id != EVENT_FOLDER:
            return
        fired = True  # the pass over EVENT_FOLDER pages its open-ended tail: it is paused
        await running.fb.deliver(EVENT_FOLDER, EVENT_RAW)
        await running.until(
            lambda: (EVENT_FOLDER, queued_mid) in running.service.events.buffered.mids,
            "the event to be queued",
        )
        running.req.fault = raw_fails  # the pass fetches nothing more; the queued event does

    running.req.hook = queue_a_failing_event
    await running.clock.advance(HOUR)
    assert await running.outcome(2) == JobOutcome("hourly", "error:Injected", (EVENT_FOLDER,))
    assert fired
    st = statuses(running.store)
    assert st[EVENT_FOLDER] == "error:Injected" and ALL_FOLDERS not in st
    assert not running.task.done()

    running.req.hook = None
    running.req.fault = None
    await running.clock.advance(HOUR)
    assert await running.outcome(3) == JobOutcome("backfill", "ok")
    assert await running.outcome(4) == JobOutcome("hourly", "ok")
    assert set(statuses(running.store).values()) == {"done"}
    assert counts(running.store) == (DISTINCT + 1, COPIES + 1)


def backfill_rows(store: Store) -> dict[str, int]:
    return dict(
        store.connection.execute(
            "SELECT folder_id, count(*) FROM ingest_runs WHERE kind = 'backfill' GROUP BY folder_id"
        )
    )


@pytest.mark.asyncio
async def test_an_event_during_the_first_listing_leaves_one_backfill_per_folder(
    running: Running,
) -> None:
    """An event that arrives while the backfill job lists folders on a fresh index names a
    folder the index has never listed: its re-list leaves the new folders to the job (one
    backfill row per folder) instead of backfilling them a second time in the event's task."""
    fired = False

    async def event_during_listing(method: str, params: BaseModel) -> None:
        nonlocal fired
        if fired or method != "mail.accounts.list":
            return
        fired = True
        await running.fb.update(running.fb.tb_ids(FOLDER_IDS[0])[0], read=True)
        # the event's own re-list has written the folder rows before the job's listing goes on
        await running.until(lambda: bool(running.store.folders()), "the event's re-list")

    running.req.hook = event_during_listing
    running.service.on_attach()
    assert await running.outcome(1) == JobOutcome("backfill", "ok")
    await running.until(lambda: not running.service._event_tasks, "the event task to end")
    assert fired
    indexed = set(statuses(running.store))
    assert backfill_rows(running.store) == dict.fromkeys(indexed, 1)
    assert counts(running.store) == (DISTINCT, COPIES)
    assert set(statuses(running.store).values()) == {"done"}
    # the job task then finishes the re-list the event left to it (nothing more to backfill)
    assert await running.outcome(2) == JobOutcome("folders", "ok")
    assert len(running.service.outcomes) == 2


@pytest.mark.asyncio
async def test_a_renamed_folder_is_backfilled_by_the_job_before_the_old_id_goes(
    running: Running,
) -> None:
    """``folder.changed`` for a renamed folder: the job task backfills the new id, then its
    re-list drops the old one, so no message loses its last location on the way."""
    await running.attach_and_backfill()
    removed: list[str] = []
    running.store.message_removed_observers.append(removed.append)
    old = FAULTY
    n_old = dict(
        running.store.connection.execute(
            "SELECT folder_id, count(*) FROM locations GROUP BY folder_id"
        )
    )[old]
    new = running.fb.rename_folder(old, "Projects-2026")
    await running.fb.folder_changed(new)
    await running.until(
        lambda: old not in {f.folder_id for f in running.store.folders()}, "the old id to go"
    )
    assert removed == []
    assert await running.outcome(2) == JobOutcome("backfill", "ok")
    assert await running.outcome(3) == JobOutcome("folders", "ok")
    await running.until(lambda: not running.service._event_tasks, "the event task to end")
    st = statuses(running.store)
    assert st[new] == "done" and old not in st
    assert old not in {f.folder_id for f in running.store.folders()}
    assert counts(running.store) == (DISTINCT, COPIES)
    assert backfill_rows(running.store)[new] == 1
    assert (
        running.store.connection.execute(
            "SELECT count(*) FROM locations WHERE folder_id = ?", (new,)
        ).fetchone()[0]
        == n_old
    )


@pytest.mark.asyncio
async def test_a_cancelled_pass_with_a_failing_queued_event_ends_cancelled(
    running: Running,
) -> None:
    """``run_until`` cancelled (``stop`` not set) while a pass is paused on a folder with a
    queued event: the pass's exit applies nothing, so the event's failure cannot replace the
    cancellation and the task ends cancelled. The event stays queued."""
    await running.attach_and_backfill()
    blocked = asyncio.Event()
    queued_mid = mid_of("svc-event@example.test")

    async def block_the_pass(method: str, params: BaseModel) -> None:
        if not isinstance(params, PageParams) or params.folder_id != EVENT_FOLDER:
            return
        if blocked.is_set():
            return
        await running.fb.deliver(EVENT_FOLDER, EVENT_RAW)
        await running.until(
            lambda: (EVENT_FOLDER, queued_mid) in running.service.events.buffered.mids,
            "the event to be queued",
        )
        running.req.fault = raw_fails  # the queued event fails if it is applied
        blocked.set()
        await wait_forever()

    running.req.hook = block_the_pass
    await running.clock.advance(HOUR)
    await asyncio.wait_for(blocked.wait(), DEADLINE_S)
    running.task.cancel()
    done, _ = await asyncio.wait([running.task], timeout=2.0)
    assert done, "the cancelled job task is still running"
    assert running.task.cancelled()
    assert [o.job for o in running.service.outcomes] == ["backfill"]
    assert len(running.service.events._queue) == 1


def locations_in(store: Store, folder_id: str) -> int:
    return int(
        store.connection.execute(
            "SELECT count(*) FROM locations WHERE folder_id = ?", (folder_id,)
        ).fetchone()[0]
    )


def folder_ids(store: Store) -> set[str]:
    return {f.folder_id for f in store.folders()}


@pytest.mark.asyncio
async def test_a_second_folder_changed_before_the_job_removes_nothing(running: Running) -> None:
    """Two ``folder.changed`` events for a renamed folder, back to back: the first upserts the
    new id, so the second no longer sees it as new; it must still leave the old id to the job
    (the new id is not ``done``), or the old id's messages lose their last location."""
    await running.attach_and_backfill()
    removed: list[str] = []
    running.store.message_removed_observers.append(removed.append)
    old = FAULTY
    n_old = locations_in(running.store, old)
    new = running.fb.rename_folder(old, "Projects-2026")
    await running.fb.folder_changed(new)
    await running.fb.folder_changed(new)
    await running.until(lambda: old not in folder_ids(running.store), "the old id to go")
    await running.until(lambda: not running.service._event_tasks, "the event tasks to end")
    assert removed == []
    assert [o.status for o in running.service.outcomes] == ["ok"] * len(running.service.outcomes)
    assert [o.job for o in running.service.outcomes][-1] == "folders"
    assert statuses(running.store)[new] == "done"
    assert backfill_rows(running.store)[new] == 1  # by the job only, never in an event task
    assert locations_in(running.store, new) == n_old
    assert counts(running.store) == (DISTINCT, COPIES)


@pytest.mark.asyncio
async def test_a_hand_off_cut_short_by_a_pause_finishes_after_the_next_attach(
    running: Running,
) -> None:
    """The extension goes away during the backfill the job task runs for an event's re-list:
    the request is kept, so after the next attach the job task still re-lists and drops the
    renamed folder's old id (not only at the next hourly re-list)."""
    await running.attach_and_backfill()
    removed: list[str] = []
    running.store.message_removed_observers.append(removed.append)
    old = FAULTY
    new = running.fb.rename_folder(old, "Projects-2026")

    def gone(method: str, params: BaseModel) -> BaseException | None:
        if isinstance(params, PageParams) and params.folder_id == new:
            return BridgeError("unavailable", "test: no extension")
        return None

    running.req.fault = gone
    await running.fb.folder_changed(new)
    assert await running.outcome(2) == JobOutcome("backfill", "paused")
    await running.until(lambda: not running.service._event_tasks, "the event task to end")
    assert old in folder_ids(running.store)
    running.req.fault = None
    running.service.on_attach()
    assert await running.outcome(3) == JobOutcome("backfill", "ok")
    assert await running.outcome(4) == JobOutcome("folders", "ok")
    assert old not in folder_ids(running.store)
    assert removed == []
    assert statuses(running.store)[new] == "done"
    assert counts(running.store) == (DISTINCT, COPIES)


@pytest.mark.asyncio
async def test_a_failed_backfill_of_a_renamed_folder_keeps_the_old_id(running: Running) -> None:
    """The job task's backfill of a renamed folder's new id fails: its re-list then removes
    nothing (a folder is not ``done``), so the old id keeps the messages' last locations."""
    await running.attach_and_backfill()
    removed: list[str] = []
    running.store.message_removed_observers.append(removed.append)
    old = FAULTY
    n_old = locations_in(running.store, old)
    new = running.fb.rename_folder(old, "Projects-2026")
    running.req.fault = page_of(new)
    await running.fb.folder_changed(new)
    assert await running.outcome(2) == JobOutcome("backfill", "error:Injected", (new,))
    assert await running.outcome(3) == JobOutcome("folders", "ok")
    await running.until(lambda: not running.service._event_tasks, "the event task to end")
    assert removed == []
    assert old in folder_ids(running.store)
    assert locations_in(running.store, old) == n_old
    assert statuses(running.store)[new] == "error:Injected"


@pytest.mark.asyncio
async def test_the_hourly_re_list_backfills_a_folder_an_event_left_to_the_job(
    running: Running,
) -> None:
    """An event upserted a renamed folder's new id and left it to the job, but the hourly
    re-list runs first (the event came between the hourly job's backfill and its re-list): the
    new id is known but has no backfill row, so the re-list backfills it before the old id goes."""
    await running.attach_and_backfill()
    removed: list[str] = []
    running.store.message_removed_observers.append(removed.append)
    asked: list[None] = []
    running.service.events.backfill_job = lambda: asked.append(None)  # the job is not woken
    old = FAULTY
    n_old = locations_in(running.store, old)
    new = running.fb.rename_folder(old, "Projects-2026")
    await running.fb.folder_changed(new)
    await running.until(lambda: not running.service._event_tasks, "the event task to end")
    assert asked and new in folder_ids(running.store) and new not in statuses(running.store)
    assert old in folder_ids(running.store)
    await running.service.events.sync_folders("reconcile")
    assert removed == []
    assert old not in folder_ids(running.store)
    assert statuses(running.store)[new] == "done"
    assert locations_in(running.store, new) == n_old


@pytest.mark.asyncio
async def test_a_pass_cancelled_while_its_exit_waits_for_the_lock_unpauses_its_folder(
    running: Running,
) -> None:
    """A pass ends normally while an event on another folder holds the event lock (awaiting the
    bridge); the job task is cancelled while the pass's exit waits for the lock. The folder must
    not stay paused, or every later event on it would be queued until a restart."""
    await running.attach_and_backfill()
    pass_gate = asyncio.Event()
    event_gate = asyncio.Event()
    in_pass = asyncio.Event()
    in_event = asyncio.Event()
    events = running.service.events

    async def hook(method: str, params: BaseModel) -> None:
        if (
            not in_pass.is_set()
            and isinstance(params, PageParams)
            and params.folder_id == EVENT_FOLDER
        ):
            in_pass.set()
            await pass_gate.wait()
        elif method == "mail.messages.raw" and not in_event.is_set():
            in_event.set()
            await event_gate.wait()

    running.req.hook = hook
    await running.clock.advance(HOUR)
    await asyncio.wait_for(in_pass.wait(), DEADLINE_S)
    assert EVENT_FOLDER in events._paused
    await running.fb.deliver(FOLDER_IDS[0], EVENT_RAW)  # an event on another folder
    await asyncio.wait_for(in_event.wait(), DEADLINE_S)
    assert events._lock.locked()
    pass_gate.set()
    # private asyncio.Lock._waiters (CPython): the pass's exit is queued on the lock
    await running.until(lambda: bool(events._lock._waiters), "the pass's exit to wait")
    running.task.cancel()
    done, _ = await asyncio.wait([running.task], timeout=2.0)
    assert done, "the cancelled job task is still running"
    assert running.task.cancelled()
    assert events._paused == {}
    event_gate.set()


@pytest.mark.asyncio
async def test_folder_changed_does_not_retry_a_failed_backfill(running: Running) -> None:
    """A folder whose backfill failed does not make every ``folder.changed`` (TB sends one on
    each unread or total count change) wake the job task to retry it: the retry waits for the
    next scheduled pass, as before any event."""
    running.req.fault = page_of(FAULTY)
    running.service.on_attach()
    assert await running.outcome(1) == JobOutcome("backfill", "error:Injected", (FAULTY,))
    other = FOLDER_IDS[0]
    assert other != FAULTY and statuses(running.store)[other] == "done"
    # Count the wake-ups at their source (called in the event's task, before it ends), so the
    # check does not depend on how fast the job task would have reacted to one.
    wake = running.service.events.backfill_job
    assert wake is not None
    wakes: list[None] = []

    def counted_wake() -> None:
        wakes.append(None)
        wake()

    running.service.events.backfill_job = counted_wake
    start = len(running.fb.calls)
    for _ in range(5):
        await running.fb.folder_changed(other)
    await running.until(lambda: not running.service._event_tasks, "the event tasks to end")
    # every event re-listed the folders (one account listing each) and none woke the job
    assert sum(m == "mail.accounts.list" for m, _p in running.fb.calls[start:]) == 5
    assert wakes == []
    assert not running.service._folders_wanted.is_set()
    assert list(running.service.outcomes) == [JobOutcome("backfill", "error:Injected", (FAULTY,))]
    # the scheduled pass still retries it first
    running.req.fault = None
    await running.clock.advance(HOUR)
    assert await running.outcome(2) == JobOutcome("backfill", "ok")
    assert await running.outcome(3) == JobOutcome("hourly", "ok")
    assert set(statuses(running.store).values()) == {"done"}


def named_tb_id(fb: FakeBridge, folder_id: str) -> tuple[int, str]:
    """A message in ``folder_id`` with a Message-ID: its tb_id and mid."""
    for tb in fb.tb_ids(folder_id):
        mid = mid_of(fb._msg(tb).header_message_id)
        if mid is not None:
            return tb, mid
    raise AssertionError("no message with a Message-ID in the folder")


def read_flag(store: Store, mid: str, folder_id: str) -> bool:
    row = store.connection.execute(
        "SELECT read FROM locations WHERE mid = ? AND folder_id = ?", (mid, folder_id)
    ).fetchone()
    assert row is not None
    return bool(row[0])


async def raise_a_pass_with_a_queued_event(
    running: Running, queue_it: Callable[[], Awaitable[None]], fault: Fault, queued: int = 1
) -> None:
    """Run the hourly job; while its pass over EVENT_FOLDER is paused, ``queue_it`` emits
    ``queued`` events there (queued), then ``fault`` is switched on and the pass raises
    ``Injected``."""
    fired = False

    async def hook(method: str, params: BaseModel) -> None:
        nonlocal fired
        if fired or not isinstance(params, PageParams):
            return
        if params.from_date is not None or params.folder_id != EVENT_FOLDER:
            return
        fired = True  # the pass over EVENT_FOLDER pages its open-ended tail: it is paused
        await queue_it()
        await running.until(
            lambda: len(running.service.events._queue) == queued, "the queued events"
        )
        running.req.fault = fault  # this very page request raises: the pass ends early

    n = len(running.service.outcomes)
    running.req.hook = hook
    await running.clock.advance(HOUR)
    assert await running.outcome(n + 1) == JobOutcome("hourly", "error:Injected", (EVENT_FOLDER,))
    assert fired
    running.req.hook = None
    assert len(running.service.events._queue) == queued


@pytest.mark.asyncio
async def test_an_event_left_queued_by_a_failed_pass_is_applied_before_a_newer_one(
    running: Running,
) -> None:
    """A pass raises with a flag update queued; a newer update of the same message comes next.
    The queued one is applied first, so the newer state wins and nothing stale stays queued."""
    await running.attach_and_backfill()
    tb, mid = named_tb_id(running.fb, EVENT_FOLDER)
    before = read_flag(running.store, mid, EVENT_FOLDER)

    async def flip() -> None:
        await running.fb.update(tb, read=not before)

    await raise_a_pass_with_a_queued_event(running, flip, page_of(EVENT_FOLDER))
    running.req.fault = None
    await running.fb.update(tb, read=before)  # the newer state
    await running.until(lambda: not running.service._event_tasks, "the event task to end")
    assert running.service.events._queue == []
    assert read_flag(running.store, mid, EVENT_FOLDER) == before


@pytest.mark.asyncio
async def test_an_event_after_a_failing_queued_event_is_applied_and_the_failure_names_its_folder(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    """The queued event left by a failed pass fails again when the next event drains it. No
    queued event is left on the next event's folder, so that event is applied at once (not left
    queued until another event or the end of a pass, up to an hour); the failure is reported as
    the queued event's: row '*', and the ERROR line names its folder, not the arriving one's."""
    await running.attach_and_backfill()
    other = FOLDER_IDS[0]
    assert other != EVENT_FOLDER
    tb, mid = named_tb_id(running.fb, other)
    before = read_flag(running.store, mid, other)

    async def deliver() -> None:
        await running.fb.deliver(EVENT_FOLDER, EVENT_RAW)

    def page_and_raw(method: str, params: BaseModel) -> BaseException | None:
        return page_of(EVENT_FOLDER)(method, params) or raw_fails(method, params)

    await raise_a_pass_with_a_queued_event(running, deliver, page_and_raw)
    running.req.fault = raw_fails
    caplog.set_level(logging.ERROR)
    caplog.clear()
    n = len(running.service.outcomes)
    await running.fb.update(tb, read=not before)
    assert await running.outcome(n + 1) == JobOutcome("event", "error:Injected", (ALL_FOLDERS,))
    await running.until(lambda: not running.service._event_tasks, "the event task to end")
    assert running.service.events._queue == []
    assert read_flag(running.store, mid, other) == (not before)
    indexed, _ = folder_infos(running.store)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == ["ingest job failed"]
    assert errors[0].__dict__["folders"] == [indexed[EVENT_FOLDER].hash]
    assert statuses(running.store)[ALL_FOLDERS] == "error:Injected"


@pytest.mark.asyncio
async def test_an_event_behind_a_failing_queued_event_on_its_folder_stays_queued(
    running: Running,
) -> None:
    """A pass raises with two events queued on EVENT_FOLDER: a delivery and a flag update. The
    next event there drains them: the delivery fails again, the update stays queued, so the
    next event (a newer flag state of the same message) is queued behind it, not applied before
    it; the drain after that applies both in order and the newer state wins."""
    await running.attach_and_backfill()
    tb, mid = named_tb_id(running.fb, EVENT_FOLDER)
    before = read_flag(running.store, mid, EVENT_FOLDER)

    async def deliver_and_flip() -> None:
        await running.fb.deliver(EVENT_FOLDER, EVENT_RAW)
        await running.until(lambda: len(running.service.events._queue) == 1, "the delivery")
        await running.fb.update(tb, read=not before)

    def page_and_raw(method: str, params: BaseModel) -> BaseException | None:
        return page_of(EVENT_FOLDER)(method, params) or raw_fails(method, params)

    await raise_a_pass_with_a_queued_event(running, deliver_and_flip, page_and_raw, queued=2)
    running.req.fault = raw_fails
    n = len(running.service.outcomes)
    await running.fb.update(tb, read=before)  # the newer state
    assert await running.outcome(n + 1) == JobOutcome("event", "error:Injected", (ALL_FOLDERS,))
    await running.until(lambda: not running.service._event_tasks, "the event task to end")
    queue = running.service.events._queue
    assert [type(e).__name__ for e in queue] == ["MailUpdated", "MailUpdated"]
    assert read_flag(running.store, mid, EVENT_FOLDER) == before  # neither applied yet
    running.req.fault = None
    await running.fb.folder_changed(FOLDER_IDS[0])  # the next event drains them first
    await running.until(lambda: not running.service._event_tasks, "the event task to end")
    assert running.service.events._queue == []
    assert read_flag(running.store, mid, EVENT_FOLDER) == before


@pytest.mark.asyncio
async def test_a_failed_reconcile_pass_marks_its_folder(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    await running.attach_and_backfill()
    running.req.fault = page_of(FAULTY)
    await running.clock.advance(HOUR)
    assert await running.outcome(2) == JobOutcome("hourly", "error:Injected", (FAULTY,))
    st = statuses(running.store)
    assert st[FAULTY] == "error:Injected" and ALL_FOLDERS not in st
    assert not running.task.done()

    # recovery: the next due pass resumes the folder's backfill from its bottom watermark (no
    # re-ingest: with nothing left it writes no new backfill row, L6; a re-ingest would count
    # messages and write one) and the folder is done again
    running.req.fault = None
    caplog.set_level(logging.DEBUG, logger="tern.index.ingest")
    caplog.clear()
    await running.clock.advance(HOUR)
    assert await running.outcome(3) == JobOutcome("backfill", "ok")
    assert await running.outcome(4) == JobOutcome("hourly", "ok")
    assert set(statuses(running.store).values()) == {"done"}
    assert counts(running.store) == (DISTINCT, COPIES)
    backfill_rows = running.store.connection.execute(
        "SELECT n_msgs FROM ingest_runs WHERE kind = 'backfill' AND folder_id = ? ORDER BY rowid",
        (FAULTY,),
    ).fetchall()
    assert len(backfill_rows) == 1
    # the resume ran for FAULTY and took the nothing-left branch (not merely left untouched)
    indexed, _ = folder_infos(running.store)
    resumed = [
        r.__dict__["folder"]
        for r in caplog.records
        if r.getMessage() == "resumed backfill found nothing left: no run row"
    ]
    assert resumed == [indexed[FAULTY].hash]


@pytest.mark.asyncio
async def test_a_failed_headers_only_revisit_marks_its_folder(running: Running) -> None:
    """A done folder holding headers-only rows is paged again by the next backfill run."""
    running.fb.set_headers_only(running.fb.tb_ids(FAULTY)[0], True)
    await running.attach_and_backfill()
    assert running.store.status().headers_only == 1
    running.req.fault = page_of(FAULTY)
    running.service.on_attach()
    assert await running.outcome(2) == JobOutcome("backfill", "error:Injected", (FAULTY,))
    assert not running.task.done()


# ---- failure attribution: private overrides ------------------------------------------------------


# Subclasses built with type(): a static override check would (rightly) flag them.


async def _unit(self: Backfill, folder: object) -> None: ...


def test_an_override_of_a_renamed_base_method_is_refused() -> None:
    renamed = type("Renamed", (Backfill,), {"_backfill_folder_renamed": _unit})
    with pytest.raises(TypeError, match="overrides nothing"):
        _check_overrides(renamed, Backfill)


def test_an_override_with_other_parameters_is_refused() -> None:
    changed = type("Changed", (Backfill,), {"_backfill_folder": _unit})
    with pytest.raises(TypeError, match="parameters differ"):
        _check_overrides(changed, Backfill)


# ---- events: input checks and shutdown -----------------------------------------------------------


class NotAnEvent(BaseModel):
    folder_id: str


@pytest.mark.asyncio
async def test_submit_event_refuses_params_that_are_not_an_event_model(running: Running) -> None:
    await running.attach_and_backfill()
    with pytest.raises(TypeError):
        running.service.submit_event("event.mail.new", NotAnEvent(folder_id="acct1:/INBOX"))
    await asyncio.sleep(0.05)
    assert len(running.service.outcomes) == 1  # no event task, no '*' row
    assert ALL_FOLDERS not in statuses(running.store)


@pytest.mark.asyncio
async def test_events_after_the_service_stopped_are_not_applied(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    fb = FakeBridge(FIXTURE)
    service = IngestService(store, IngestSource(fb), TernConfig(), FakeClock(time.time()))
    stop = asyncio.Event()
    stop.set()
    await service.run_until(stop)
    caplog.set_level(logging.INFO)
    raw = (
        b"Message-ID: <late@example.test>\nDate: Mon, 02 Nov 2026 10:00:00 +0000\n"
        b"From: a@example.test\nSubject: synthetic\n\nbody\n"
    )

    async def to_service(method: str, params: BaseModel) -> None:
        service.submit_event(method, params)

    fb.on_event = to_service
    await fb.deliver("acct1:/Newsletters", raw)
    await asyncio.sleep(0.05)
    assert "event not applied: ingest service stopped" in caplog.messages
    assert list(service.outcomes) == []
    assert counts(store) == (0, 0)


async def wait_forever() -> None:
    await asyncio.Event().wait()


async def slow_to_stop(entered: asyncio.Event, stopping: asyncio.Event) -> None:
    """A child task that, once cancelled, ends only when cancelled a second time."""
    entered.set()
    try:
        await wait_forever()
    except asyncio.CancelledError:
        stopping.set()
        await wait_forever()
        raise


async def ends_cancelled(task: asyncio.Task[None]) -> bool:
    done, _ = await asyncio.wait([task], timeout=DEADLINE_S)
    assert done, "the task did not end"
    return task.cancelled()


@pytest.mark.asyncio
async def test_a_cancelled_run_until_ends_cancelled_while_its_event_tasks_stop(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_until``'s own cancellation, arriving while it waits for its event tasks to stop,
    is passed on, not swallowed with theirs."""
    fb = FakeBridge(FIXTURE)
    service = IngestService(store, IngestSource(fb), TernConfig(), FakeClock(time.time()))
    entered, stopping = asyncio.Event(), asyncio.Event()

    async def on_event(method: str, params: BaseModel) -> None:
        await slow_to_stop(entered, stopping)

    monkeypatch.setattr(service.events, "on_event", on_event)

    async def to_service(method: str, params: BaseModel) -> None:
        service.submit_event(method, params)

    fb.on_event = to_service
    stop = asyncio.Event()
    task = asyncio.create_task(service.run_until(stop))
    await fb.deliver(EVENT_FOLDER, EVENT_RAW)
    await asyncio.wait_for(entered.wait(), DEADLINE_S)
    stop.set()
    await asyncio.wait_for(stopping.wait(), DEADLINE_S)
    task.cancel()
    assert await ends_cancelled(task)


@pytest.mark.asyncio
async def test_a_cancelled_wait_ends_cancelled_while_its_waiters_stop(store: Store) -> None:
    """The same for the schedule's wait (else ``run_until`` would go on to its next step)."""
    entered, stopping = asyncio.Event(), asyncio.Event()

    class SlowClock(FakeClock):
        async def sleep_until(self, when: float) -> None:
            await slow_to_stop(entered, stopping)

    clock = SlowClock(time.time())
    service = IngestService(store, IngestSource(FakeBridge(FIXTURE)), TernConfig(), clock)
    stop = asyncio.Event()
    task = asyncio.create_task(service._wait(stop, clock.now + HOUR))
    await asyncio.wait_for(entered.wait(), DEADLINE_S)
    stop.set()
    await asyncio.wait_for(stopping.wait(), DEADLINE_S)
    task.cancel()
    assert await ends_cancelled(task)


@pytest.mark.parametrize("rate", [5.0, 0.0])
def test_the_three_jobs_share_one_fetch_throttle(store: Store, rate: float) -> None:
    """``ingest_max_msgs_per_s`` caps TB's load, not one job's (L3): backfill, events and
    reconcile take their fetch slots from one bucket (none when the throttle is off)."""
    cfg = TernConfig(ingest_max_msgs_per_s=rate)
    service = IngestService(store, IngestSource(FakeBridge(FIXTURE)), cfg, FakeClock(time.time()))
    buckets = [j.items.throttle for j in (service.backfill, service.events, service.reconcile)]
    if rate:
        assert buckets[0] is not None and buckets[0].interval == 1 / rate
        assert all(b is buckets[0] for b in buckets)
    else:
        assert buckets == [None, None, None]
