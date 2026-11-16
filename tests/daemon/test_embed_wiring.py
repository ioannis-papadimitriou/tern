"""Plan 05 step 7: ``ternd`` runs the embed queue beside the ingest service, on the same Store.

The extension is a scripted client answered by FakeBridge (the synthetic mailbox), as in
``test_ingest_wiring.py``. The embedder comes from an injected factory: the StubEmbedder, or an
``EmbeddingClient`` over ``httpx.MockTransport`` (no socket). The conftest's autouse fixture makes
the default factory an unreachable client, so no test reaches for a real embeddings server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import pytest_asyncio
import test_fixture_manifest as tfm

from tern import paths
from tern.bridge.client import Peer
from tern.bridge.protocol import METHODS, Request, dump
from tern.config import TernConfig
from tern.daemon.main import Daemon, EmbedderFactory, serve
from tern.daemon.main import make_embedder as real_make_embedder  # before the conftest patch
from tern.index.store import Store, index_path
from tern.llm.egress import EgressDenied
from tern.search import vec
from tern.search.embed_queue import EmbedQueue
from tern.search.embedder import Embedder, EmbeddingClient
from tern.testing.bridge import NMHOST, Client, FakeProcessInfo, connect, hello_params
from tern.testing.fake_bridge import FakeBridge
from tern.testing.procs import wait_for_socket
from tern.testing.stub_embedder import StubEmbedder

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
COPIES = MANIFEST["message_count"]
DEADLINE_S = 10.0
STUB_ID = StubEmbedder().model_id
#: Embedding on, with the stub's model id and a short idle poll (chunks arrive after start-up).
STUB_CFG = TernConfig(embed_model=STUB_ID, embed_idle_poll_s=0.02)


def fb_responder(fb: FakeBridge) -> Callable[[Peer, Request], Any]:
    async def respond(peer: Peer, req: Request) -> Any:
        if req.method == "bridge.ping":
            return {}
        spec = METHODS[req.method]
        return dump(await fb.request(req.method, spec.params.model_validate(req.params)))

    return respond


@dataclass
class Run:
    tp: paths.TernPaths
    task: asyncio.Task[None]
    stop: asyncio.Event
    started: list[Daemon] = field(default_factory=list)
    reader: Store | None = None

    @property
    def daemon(self) -> Daemon:
        assert len(self.started) == 1, "on_started was not called exactly once"
        return self.started[0]

    @property
    def conn(self) -> Any:
        if self.reader is None:
            self.reader = Store.open(index_path(self.tp), readonly=True)
        return self.reader.connection

    async def until(self, cond: Callable[[], bool], what: str) -> None:
        deadline = time.perf_counter() + DEADLINE_S
        while not cond():
            if self.task.done():
                self.task.result()
                raise AssertionError(f"serve() returned before {what}")
            assert time.perf_counter() < deadline, f"timed out waiting for {what}"
            await asyncio.sleep(0.01)

    def chunks(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0])

    def backfill_done(self) -> bool:
        rows = self.conn.execute("SELECT status FROM ingest_state").fetchall()
        locations = self.conn.execute("SELECT count(*) FROM locations").fetchone()[0]
        return bool(rows) and {r[0] for r in rows} == {"done"} and locations == COPIES

    async def attach(self) -> Client:
        fb = FakeBridge(FIXTURE)
        c = await connect(
            self.tp.socket,
            "extension",
            serve=fb_responder(fb),
            hello=hello_params("extension", profile_id=str(uuid.uuid4())),
        )
        fb.on_event = c.peer.notify
        return c

    async def shutdown(self) -> None:
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        self.stop.set()
        await asyncio.wait_for(self.task, DEADLINE_S)


def start(cfg: TernConfig, factory: EmbedderFactory | None = None) -> Run:
    tp = paths.resolve()
    tp.run.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = asyncio.Event()
    started: list[Daemon] = []
    task = asyncio.create_task(
        serve(
            tp,
            cfg,
            process_info=FakeProcessInfo(),
            nmhost_path=NMHOST,
            stop=stop,
            embedder_factory=factory,
            on_started=started.append,
        )
    )
    return Run(tp, task, stop, started)


@pytest_asyncio.fixture
async def runs() -> AsyncIterator[list[Run]]:
    """Runs a test started; any still going at the end are stopped (a failed test's cleanup)."""
    started: list[Run] = []
    yield started
    for run in started:
        if run.reader is not None:
            run.reader.close()
        run.stop.set()
        # Exception only: a timeout or serve()'s own error after a failed test, never a
        # KeyboardInterrupt or a cancellation of the test run.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(run.task, DEADLINE_S)


async def started(runs: list[Run], cfg: TernConfig, factory: EmbedderFactory | None) -> Run:
    run = start(cfg, factory)
    runs.append(run)
    await asyncio.to_thread(wait_for_socket, run.tp.socket)
    return run


def track_store_close(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    real_close = Store.close

    def close(self: Store) -> None:
        # The engine's read-only Store (plan 06b step 7) is closed before the index.
        order.append("reader closed" if self.readonly else "index closed")
        real_close(self)

    monkeypatch.setattr(Store, "close", close)


@pytest.mark.asyncio
async def test_chunks_get_embedded_after_a_backfill_through_the_ingest_store(
    runs: list[Run],
) -> None:
    built: list[TernConfig] = []

    def factory(cfg: TernConfig) -> Embedder:
        built.append(cfg)
        return StubEmbedder()

    run = await started(runs, STUB_CFG, factory)
    assert built == [STUB_CFG]
    d = run.daemon
    assert isinstance(d.embed, EmbedQueue)
    # One writer connection: the queue writes through the ingest service's Store.
    assert d.embed.store is d.store and d.service.store is d.store
    c = await run.attach()
    await run.until(run.backfill_done, "the backfill")
    await run.until(lambda: vec.pending_count(run.conn) == 0, "every chunk embedded")
    n = run.chunks()
    assert n > 0
    assert run.conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == n
    model = vec.current(run.conn)
    assert model is not None and (model.model_id, model.dim) == (STUB_ID, 64)
    await run.until(
        lambda: vec.status(run.conn, max_input_chars=6000).embed_complete_at is not None,
        "embed_complete_at",
    )
    state = d.embed.state
    assert (state.model, state.pending, state.status) == (STUB_ID, 0, "ok")
    await c.close()
    await run.shutdown()


@pytest.mark.asyncio
async def test_the_queue_on_the_daemon_object_takes_holds(runs: list[Run]) -> None:
    """A hold taken through ``Daemon.embed`` (08a/08b/11's model calls) pauses the running queue:
    chunks backfilled meanwhile stay pending until it exits."""
    run = await started(runs, STUB_CFG, lambda cfg: StubEmbedder())
    queue = run.daemon.embed
    assert queue is not None
    async with queue.hold("chat"):
        c = await run.attach()
        await run.until(run.backfill_done, "the backfill")
        await run.until(lambda: queue.state.status == "paused", "the queue to pause")
        assert vec.pending_count(run.conn) == run.chunks() > 0
    await run.until(lambda: vec.pending_count(run.conn) == 0, "every chunk embedded")
    await c.close()
    await run.shutdown()


@pytest.mark.asyncio
async def test_embedding_disabled_runs_no_queue(
    runs: list[Run], caplog: pytest.LogCaptureFixture
) -> None:
    def factory(cfg: TernConfig) -> Embedder:
        raise AssertionError("the embedder factory was called with embedding disabled")

    caplog.set_level(logging.INFO, logger="tern.daemon")
    run = await started(runs, TernConfig(embed_model=None), factory)
    assert run.daemon.embed is None
    assert "embed disabled" in [r.getMessage() for r in caplog.records]
    c = await run.attach()
    await run.until(run.backfill_done, "the backfill")
    assert run.chunks() > 0
    assert vec.current(run.conn) is None
    assert run.conn.execute("SELECT count(*) FROM embedding_model").fetchone()[0] == 0
    await c.close()
    await run.shutdown()


@pytest.mark.asyncio
async def test_an_unreachable_embedder_leaves_ingest_running(runs: list[Run]) -> None:
    """The default config's embedder (the conftest's unreachable client): the queue backs off,
    the backfill completes and ternd keeps running."""
    run = await started(runs, TernConfig(embed_model="text-embedding-qwen3-embedding-0.6b"), None)
    queue = run.daemon.embed
    assert queue is not None
    await run.until(
        lambda: queue.state.status == "unavailable:EmbedderUnavailable", "the queue to back off"
    )
    c = await run.attach()
    await run.until(run.backfill_done, "the backfill")
    assert not run.task.done()
    assert queue.state.status == "unavailable:EmbedderUnavailable"
    assert queue.state.pending == run.chunks() > 0
    assert vec.current(run.conn) is None  # the probe never succeeded: nothing registered
    await c.close()
    await run.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_the_queue_before_it_closes_the_index_and_the_embedder(
    runs: list[Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The embedder's request never completes: stopping ternd cancels it, then closes the
    index, then the ``EmbeddingClient``; ``serve()`` returns normally and removes the socket."""
    order: list[str] = []
    requested = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        requested.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            order.append("request cancelled")
            raise
        raise AssertionError("unreachable")

    real_aclose = EmbeddingClient.aclose

    async def aclose(self: EmbeddingClient) -> None:
        order.append("embedder closed")
        await real_aclose(self)

    monkeypatch.setattr(EmbeddingClient, "aclose", aclose)
    track_store_close(monkeypatch, order)

    def factory(cfg: TernConfig) -> Embedder:
        return EmbeddingClient(cfg, transport=httpx.MockTransport(hang))

    run = await started(
        runs, TernConfig(embed_model="text-embedding-qwen3-embedding-0.6b"), factory
    )
    await asyncio.wait_for(requested.wait(), DEADLINE_S)
    run.stop.set()
    assert await asyncio.wait_for(run.task, DEADLINE_S) is None
    assert order == ["request cancelled", "reader closed", "index closed", "embedder closed"]
    assert not run.tp.socket.exists()


class Died(RuntimeError):
    """An unexpected failure of the embed queue (injected; carries no content)."""


@pytest.mark.asyncio
async def test_a_dead_embed_queue_is_logged_and_ternd_keeps_ingesting(
    runs: list[Run], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def die(self: EmbedQueue) -> None:
        await asyncio.sleep(0)
        raise Died()

    monkeypatch.setattr(EmbedQueue, "run", die)
    caplog.set_level(logging.INFO)
    run = await started(runs, STUB_CFG, lambda cfg: StubEmbedder())
    c = await run.attach()
    await run.until(run.backfill_done, "the backfill")
    assert not run.task.done()
    await c.close()
    await run.shutdown()  # returns normally: the queue's exception is not raised at shutdown
    errors = [
        (r.getMessage(), r.__dict__.get("error"))
        for r in caplog.records
        if r.levelno >= logging.ERROR
    ]
    assert errors == [("embed queue died; ternd keeps ingesting", "Died")]


@pytest.mark.asyncio
async def test_a_queue_stopped_by_a_malformed_response_leaves_ternd_ingesting(
    runs: list[Run], caplog: pytest.LogCaptureFixture
) -> None:
    """The queue returns normally on an ``EmbedderError`` (it logs that itself): ternd keeps
    ingesting, the status stays ``error:EmbedderError``, and ``_embed_ended`` adds no record."""

    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    def factory(cfg: TernConfig) -> Embedder:
        return EmbeddingClient(cfg, transport=httpx.MockTransport(malformed))

    caplog.set_level(logging.INFO)
    run = await started(
        runs, TernConfig(embed_model="text-embedding-qwen3-embedding-0.6b"), factory
    )
    queue = run.daemon.embed
    assert queue is not None
    await run.until(lambda: queue.state.status == "error:EmbedderError", "the queue to stop")
    c = await run.attach()
    await run.until(run.backfill_done, "the backfill")
    assert not run.task.done()
    assert queue.state.status == "error:EmbedderError"
    assert vec.current(run.conn) is None
    await c.close()
    await run.shutdown()
    errors = [
        (r.getMessage(), r.__dict__.get("error"))
        for r in caplog.records
        if r.levelno >= logging.ERROR
    ]
    assert errors == [("embedder error; embed queue stopped", "EmbedderError")]


@pytest.mark.asyncio
async def test_an_embedder_url_the_egress_guard_refuses_stops_ternd_before_it_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    track_store_close(monkeypatch, order)
    cfg = TernConfig(
        embed_base_url="http://192.0.2.1/v1", embed_model="text-embedding-qwen3-embedding-0.6b"
    )
    run = start(cfg, real_make_embedder)
    with pytest.raises(EgressDenied):
        await asyncio.wait_for(run.task, DEADLINE_S)
    assert not run.tp.socket.exists()
    assert not index_path(run.tp).exists(), "the index was opened"
    assert order == []
    assert run.started == []


@pytest.mark.asyncio
async def test_an_embedder_for_another_model_is_refused_and_cleaned_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mis-wiring (the factory's model id differs from ``embed_model``) is loud: ``serve()``
    raises before the socket is bound, and the index is closed."""
    order: list[str] = []
    track_store_close(monkeypatch, order)
    run = start(
        TernConfig(embed_model="text-embedding-qwen3-embedding-0.6b"), lambda cfg: StubEmbedder()
    )
    with pytest.raises(ValueError, match="differs from embed_model"):
        await asyncio.wait_for(run.task, DEADLINE_S)
    assert not run.tp.socket.exists()
    assert order == ["reader closed", "index closed"]


@pytest.mark.asyncio
async def test_the_default_factory_builds_an_embedding_client() -> None:
    embedder = real_make_embedder(TernConfig(embed_model="text-embedding-qwen3-embedding-0.6b"))
    assert isinstance(embedder, EmbeddingClient)
    assert embedder.model_id == "text-embedding-qwen3-embedding-0.6b"
    await embedder.aclose()
