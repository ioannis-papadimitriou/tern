"""Plan 08b step 2: ``IngestService.event_lag_s``, the age of the oldest TB event not yet applied
(bridge ``index.status.lag_s``), over FakeBridge.

Events reach the service as in ternd (``FakeBridge.on_event`` → ``submit_event``); a gate in front
of FakeBridge holds the event's bridge requests so the test controls when an event is applied.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import test_fixture_manifest as tfm
from pydantic import BaseModel

from tern.config import TernConfig
from tern.index.service import IngestService, JobOutcome
from tern.index.source import IngestSource
from tern.index.store import Store
from tern.testing.fake_bridge import FakeBridge

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
FOLDER_IDS = [f"{a['key']}:/{f['path']}" for a in MANIFEST["accounts"] for f in a["folders"]]
DEADLINE_S = 10.0


class Injected(RuntimeError):
    """A failure injected by the test (carries no content)."""


@dataclass
class GatedRequester:
    """FakeBridge behind a gate: while ``closed``, every request waits for ``open()`` (and counts
    in ``waiting``); ``fail`` makes every request raise :class:`Injected` instead."""

    fb: FakeBridge
    closed: bool = False
    fail: bool = False
    waiting: int = 0
    _gate: asyncio.Event = field(default_factory=asyncio.Event)

    def close(self) -> None:
        self.closed = True
        self._gate.clear()

    def open(self) -> None:
        self.closed = False
        self._gate.set()

    async def request(self, method: str, params: BaseModel) -> Any:
        if self.closed:
            self.waiting += 1
            try:
                await self._gate.wait()
            finally:
                self.waiting -= 1
        if self.fail:
            raise Injected()
        return await self.fb.request(method, params)

    async def notify(self, method: str, params: BaseModel) -> None:
        await self.fb.notify(method, params)


@dataclass
class Running:
    service: IngestService
    fb: FakeBridge
    req: GatedRequester
    task: asyncio.Task[None]

    async def until(self, cond: Callable[[], bool], what: str) -> None:
        deadline = time.perf_counter() + DEADLINE_S
        while not cond():
            if self.task.done():
                self.task.result()
                raise AssertionError(f"service stopped before {what}")
            assert time.perf_counter() < deadline, f"timed out waiting for {what}"
            await asyncio.sleep(0.005)

    async def backfill(self) -> None:
        self.service.on_attach()
        await self.until(lambda: len(self.service.outcomes) >= 1, "the backfill")
        assert self.service.outcomes[0] == JobOutcome("backfill", "ok")

    def lag(self) -> float:
        return self.service.event_lag_s(time.monotonic())


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "data" / "index.db")
    yield s
    s.close()


@pytest_asyncio.fixture
async def running(store: Store) -> AsyncIterator[Running]:
    fb = FakeBridge(FIXTURE)
    req = GatedRequester(fb)
    service = IngestService(store, IngestSource(req), TernConfig())

    async def to_service(method: str, params: BaseModel) -> None:
        service.submit_event(method, params)

    fb.on_event = to_service
    stop = asyncio.Event()
    task = asyncio.create_task(service.run_until(stop))
    yield Running(service, fb, req, task)
    req.open()
    stop.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_lag_is_zero_with_no_event_pending(running: Running) -> None:
    assert running.lag() == 0.0
    await running.backfill()
    assert running.lag() == 0.0
    # an event that was applied leaves no lag behind
    await running.fb.folder_changed(FOLDER_IDS[0])
    await running.until(lambda: not running.service._event_tasks, "the event to be applied")
    assert running.lag() == 0.0


@pytest.mark.asyncio
async def test_lag_grows_while_an_event_is_blocked_and_returns_to_zero_once_applied(
    running: Running,
) -> None:
    await running.backfill()
    running.req.close()
    before = time.monotonic()
    await running.fb.folder_changed(FOLDER_IDS[0])
    after = time.monotonic()
    await running.until(lambda: running.req.waiting > 0, "the event to wait at the gate")
    now = time.monotonic()
    lag = running.service.event_lag_s(now)
    # the age of the event, measured from when submit_event received it
    assert now - after <= lag <= now - before
    assert running.service.event_lag_s(now + 2.5) == pytest.approx(lag + 2.5)
    await asyncio.sleep(0.05)
    assert running.lag() > lag
    running.req.open()
    await running.until(lambda: not running.service._event_tasks, "the event to be applied")
    assert running.lag() == 0.0


@pytest.mark.asyncio
async def test_lag_follows_the_oldest_pending_event(running: Running) -> None:
    await running.backfill()
    running.req.close()
    t0 = time.monotonic()
    await running.fb.folder_changed(FOLDER_IDS[0])
    await running.until(lambda: running.req.waiting > 0, "the first event to wait at the gate")
    await asyncio.sleep(0.05)
    t1 = time.monotonic()
    await running.fb.folder_changed(FOLDER_IDS[1])
    assert len(running.service._event_tasks) == 2
    now = time.monotonic()
    # both pending: the first event's age (received at least 0.05 s before t1, not the second's)
    assert running.service.event_lag_s(now) >= now - t1 + 0.05
    assert running.service.event_lag_s(now) <= now - t0
    running.req.open()
    await running.until(lambda: not running.service._event_tasks, "both events to be applied")
    assert running.lag() == 0.0


@pytest.mark.asyncio
async def test_a_failed_event_no_longer_counts(running: Running) -> None:
    await running.backfill()
    running.req.close()
    await running.fb.folder_changed(FOLDER_IDS[0])
    await running.until(lambda: running.req.waiting > 0, "the event to wait at the gate")
    assert running.lag() > 0.0
    running.req.fail = True
    running.req.open()
    await running.until(lambda: not running.service._event_tasks, "the event to fail")
    assert running.service.outcomes[-1] == JobOutcome("event", "error:Injected", ("*",))
    assert running.lag() == 0.0


@pytest.mark.asyncio
async def test_events_cancelled_at_shutdown_no_longer_count(running: Running) -> None:
    await running.backfill()
    running.req.close()
    await running.fb.folder_changed(FOLDER_IDS[0])
    await running.until(lambda: running.req.waiting > 0, "the event to wait at the gate")
    assert running.lag() > 0.0
    running.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await running.task
    assert not running.service._event_tasks
    assert running.lag() == 0.0
