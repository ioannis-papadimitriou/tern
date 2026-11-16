"""Plan 04 step 9: ``ternd``'s ``serve()`` wires the index, the profile binding, the TB events
and the ingest service to a real bridge server over a Unix socket.

The extension is a scripted client whose requests are answered by FakeBridge (the synthetic
mailbox); its TB events go over the wire as 1.0 notifications. Expected numbers come from
MANIFEST.json.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import pytest
import pytest_asyncio
import test_fixture_manifest as tfm

from tern import paths
from tern.bridge.client import Peer
from tern.bridge.protocol import METHODS, BridgeError, Request, dump
from tern.bridge.server import BridgeServer
from tern.config import TernConfig
from tern.daemon import uds
from tern.daemon.main import serve
from tern.index.mid import mid_of
from tern.index.service import IngestService
from tern.index.store import Store, index_path
from tern.testing.bridge import NMHOST, Client, FakeProcessInfo, connect, hello_params
from tern.testing.fake_bridge import FakeBridge
from tern.testing.procs import wait_for_socket

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
COPIES = MANIFEST["message_count"]
DISTINCT = MANIFEST["distinct_messages"]
DEADLINE_S = 10.0


def fb_responder(fb: FakeBridge) -> Callable[[Peer, Request], Any]:
    """The extension's request handler: FakeBridge answers every daemon→extension request."""

    async def respond(peer: Peer, req: Request) -> Any:
        if req.method == "bridge.ping":
            return {}
        spec = METHODS[req.method]
        return dump(await fb.request(req.method, spec.params.model_validate(req.params)))

    return respond


@dataclass
class Daemon:
    tp: paths.TernPaths
    task: asyncio.Task[None]
    reader: Store

    async def until(self, cond: Callable[[], bool], what: str) -> None:
        deadline = time.perf_counter() + DEADLINE_S
        while not cond():
            if self.task.done():
                self.task.result()
                raise AssertionError(f"serve() returned before {what}")
            assert time.perf_counter() < deadline, f"timed out waiting for {what}"
            await asyncio.sleep(0.01)

    def counts(self) -> tuple[int, int]:
        st = self.reader.status()
        return st.messages, st.locations

    def backfill_done(self) -> bool:
        rows = self.reader.connection.execute("SELECT status FROM ingest_state").fetchall()
        return bool(rows) and {r[0] for r in rows} == {"done"} and self.counts()[1] == COPIES


@pytest_asyncio.fixture
async def daemon() -> AsyncIterator[Daemon]:
    tp = paths.resolve()
    tp.run.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(tp, TernConfig(), process_info=FakeProcessInfo(), nmhost_path=NMHOST, stop=stop)
    )
    await asyncio.to_thread(wait_for_socket, tp.socket)
    reader = Store.open(index_path(tp), readonly=True)
    yield Daemon(tp, task, reader)
    reader.close()
    stop.set()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, DEADLINE_S)


async def extension(d: Daemon, fb: FakeBridge, profile_id: str) -> Client:
    c = await connect(
        d.tp.socket,
        "extension",
        serve=fb_responder(fb),
        hello=hello_params("extension", profile_id=profile_id),
    )
    fb.on_event = c.peer.notify  # TB events go to ternd as 1.0 notifications
    return c


@pytest.mark.asyncio
async def test_extension_attach_backfills_the_mailbox(daemon: Daemon) -> None:
    fb = FakeBridge(FIXTURE)
    profile = str(uuid.uuid4())
    c = await extension(daemon, fb, profile)
    await daemon.until(daemon.backfill_done, "the backfill")
    assert daemon.counts() == (DISTINCT, COPIES)
    assert str(daemon.reader.profile_id()) == profile
    await c.close()


@pytest.mark.asyncio
async def test_an_event_over_the_wire_is_applied(daemon: Daemon) -> None:
    """``event.mail.new`` makes ternd fetch the message over the same connection: applied in the
    bridge's reader task, that fetch could never be answered."""
    fb = FakeBridge(FIXTURE)
    c = await extension(daemon, fb, str(uuid.uuid4()))
    await daemon.until(daemon.backfill_done, "the backfill")
    folder = "acct1:/Newsletters"
    raw = (
        f"Message-ID: <wire-event@example.test>\n"
        f"Date: {format_datetime(datetime.now(UTC))}\n"
        "From: a@example.test\nTo: b@example.test\nSubject: synthetic\n\nbody\n"
    ).encode()
    await fb.deliver(folder, raw)
    mid = mid_of("wire-event@example.test")
    assert mid is not None

    def located() -> bool:
        return [loc.folder_id for loc in daemon.reader.locations(mid)] == [folder]

    await daemon.until(located, "the event")
    assert daemon.counts() == (DISTINCT + 1, COPIES + 1)
    await c.close()


@pytest.mark.asyncio
async def test_serve_refuses_another_profile(daemon: Daemon) -> None:
    fb = FakeBridge(FIXTURE)
    c = await extension(daemon, fb, str(uuid.uuid4()))
    await daemon.until(daemon.backfill_done, "the backfill")
    await c.close()
    with pytest.raises(BridgeError) as exc:
        await connect(
            daemon.tp.socket,
            "extension",
            serve=fb_responder(fb),
            hello=hello_params("extension", profile_id=str(uuid.uuid4())),
        )
    assert exc.value.name == "profile_mismatch"


# ---- lifecycle -----------------------------------------------------------------------------------


def track_store_close(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """One entry per ``Store.close`` call: ``closed`` for the index, ``closed readonly`` for
    the engine's read-only Store (plan 06b step 7)."""
    closed: list[str] = []
    real_close = Store.close

    def close(self: Store) -> None:
        closed.append("closed readonly" if self.readonly else "closed")
        real_close(self)

    monkeypatch.setattr(Store, "close", close)
    return closed


def start_serve(tp: paths.TernPaths, stop: asyncio.Event) -> asyncio.Task[None]:
    tp.run.mkdir(mode=0o700, parents=True, exist_ok=True)
    return asyncio.create_task(
        serve(tp, TernConfig(), process_info=FakeProcessInfo(), nmhost_path=NMHOST, stop=stop)
    )


class Died(RuntimeError):
    """An unexpected failure of the ingest service (injected; carries no content)."""


@pytest.mark.asyncio
async def test_a_dead_ingest_service_stops_ternd_loudly_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def die(self: IngestService, stop: asyncio.Event) -> None:
        await asyncio.sleep(0.05)
        raise Died()

    monkeypatch.setattr(IngestService, "run_until", die)
    closed = track_store_close(monkeypatch)
    caplog.set_level(logging.INFO)
    tp = paths.resolve()
    t0 = time.perf_counter()
    task = start_serve(tp, asyncio.Event())
    with pytest.raises(Died):
        await asyncio.wait_for(task, DEADLINE_S)  # stop is never set: the dead service stops it
    assert time.perf_counter() - t0 < DEADLINE_S / 2, "ternd kept running without its jobs"
    assert not tp.socket.exists()
    assert closed == ["closed readonly", "closed"]
    errors = [
        (r.getMessage(), r.__dict__.get("error"))
        for r in caplog.records
        if r.levelno >= logging.ERROR
    ]
    assert errors == [("ingest service died; ternd stops", "Died")]


@pytest.mark.asyncio
async def test_shutdown_closes_the_connections_before_it_stops_the_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise a notification arriving in between starts an event task nobody cancels, which
    may run after the index is closed. The jobs stand-in ignores ``stop`` and ends only when
    ``serve()`` cancels it, so the order checked is the one ``serve()`` imposes."""
    order: list[str] = []
    real_close_all = BridgeServer.close_all

    async def close_all(self: BridgeServer) -> None:
        order.append("connections closed")
        await real_close_all(self)

    async def run_until(self: IngestService, stop: asyncio.Event) -> None:
        try:
            await asyncio.Event().wait()  # never set: only the task's cancellation ends it
        finally:
            order.append("jobs stopped")

    monkeypatch.setattr(BridgeServer, "close_all", close_all)
    monkeypatch.setattr(IngestService, "run_until", run_until)
    tp = paths.resolve()
    stop = asyncio.Event()
    task = start_serve(tp, stop)
    await asyncio.to_thread(wait_for_socket, tp.socket)
    stop.set()
    await asyncio.wait_for(task, DEADLINE_S)
    assert order == ["connections closed", "jobs stopped"]


@pytest.mark.asyncio
async def test_a_cancelled_serve_ends_cancelled_while_its_jobs_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancellation of ``serve()``'s task arrives while ``serve()`` waits for the jobs to
    stop: it is passed on to them and ``serve()`` ends cancelled (it does not return normally),
    with the socket removed and the index closed."""
    stopping = asyncio.Event()

    async def run_until(self: IngestService, stop: asyncio.Event) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopping.set()
            await asyncio.Event().wait()  # slow to stop: only a second cancellation ends it
            raise

    monkeypatch.setattr(IngestService, "run_until", run_until)
    closed = track_store_close(monkeypatch)
    tp = paths.resolve()
    stop = asyncio.Event()
    task = start_serve(tp, stop)
    await asyncio.to_thread(wait_for_socket, tp.socket)
    stop.set()
    await asyncio.wait_for(stopping.wait(), DEADLINE_S)
    task.cancel()
    done, _ = await asyncio.wait([task], timeout=DEADLINE_S)
    assert done, "serve() did not end"
    assert task.cancelled()
    assert not tp.socket.exists()
    assert closed == ["closed readonly", "closed"]


@pytest.mark.asyncio
async def test_a_failed_socket_start_closes_the_index(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(server: BridgeServer, sock_path: object) -> None:
        raise OSError("test: socket refused")

    monkeypatch.setattr(uds, "start", refuse)
    closed = track_store_close(monkeypatch)
    with pytest.raises(OSError, match="test: socket refused"):
        await asyncio.wait_for(start_serve(paths.resolve(), asyncio.Event()), DEADLINE_S)
    assert closed == ["closed readonly", "closed"]


@pytest.mark.asyncio
async def test_a_failure_after_the_socket_start_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signal registration fails half-way, after the socket is bound: the socket file is
    removed, the handler already registered is removed again and the index is closed."""
    loop = asyncio.get_running_loop()
    real_add = loop.add_signal_handler
    added: list[int] = []

    def add_signal_handler(sig: int, callback: Callable[..., object], *args: Any) -> None:
        if added:
            raise RuntimeError("test: second signal refused")
        real_add(sig, callback, *args)
        added.append(sig)

    monkeypatch.setattr(loop, "add_signal_handler", add_signal_handler)
    closed = track_store_close(monkeypatch)
    tp = paths.resolve()
    with pytest.raises(RuntimeError, match="test: second signal refused"):
        await asyncio.wait_for(start_serve(tp, asyncio.Event()), DEADLINE_S)
    assert len(added) == 1
    assert not tp.socket.exists()
    assert loop.remove_signal_handler(added[0]) is False, "signal handler left installed"
    assert closed == ["closed readonly", "closed"]
