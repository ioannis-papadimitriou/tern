"""Plan 05 step 4: ``tern.search.embed_queue.EmbedQueue``.

Mail here is synthetic: inline records of made-up words, no addresses. Expected orders
and counts come from the records each test writes, never from a run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import sqlite3
import struct
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from tern.config import TernConfig
from tern.index.store import LocationState, MessageRecord, Store
from tern.search import vec
from tern.search.embed_queue import (
    BACKOFF_CAP_S,
    PROBE_TEXT,
    ZERO_PROGRESS_LIMIT,
    EmbedQueue,
    EmbedState,
)
from tern.search.embedder import EmbedderError, EmbedderUnavailable, EmbeddingClient
from tern.testing.stub_embedder import StubEmbedder

M1, M2, M3, M4 = "a" * 64, "b" * 64, "c" * 64, "d" * 64
H1, H2 = "1" * 64, "2" * 64
INBOX = "acct1:/INBOX"
S0 = LocationState()
T0 = 1_753_783_200
NOW = 1_800_000_000
IDLE_S = 0.5  # distinct from every back-off delay (whole seconds)


def rec(mid: str, body: str, *, date_utc: int | None = T0, subject: str = "subj") -> MessageRecord:
    return MessageRecord(
        mid=mid,
        content_hash=H1,
        parse_status="ok",
        subject=f"{subject} {mid[0]}",
        body_text=body,
        date_utc=date_utc,
    )


def add(store: Store, record: MessageRecord) -> None:
    with store.transaction():
        store.upsert_message(record)
        store.add_location(record.mid, INBOX, record.content_hash, S0, T0, source="backfill")


def one(conn: sqlite3.Connection, sql: str, *args: Any) -> Any:
    return conn.execute(sql, args).fetchone()[0]


def chunks(store: Store) -> list[tuple[int, str, str]]:
    """(chunk_id, mid, text) of every chunk."""
    return [
        (r[0], r[1], r[2])
        for r in store.connection.execute("SELECT chunk_id, mid, text FROM chunks ORDER BY 1")
    ]


def stored_matches_text(store: Store, emb: StubEmbedder) -> bool:
    """Every chunk has a vector, and it is the quantised stub vector of the chunk's current text."""
    conn = store.connection
    for chunk_id, _, text in chunks(store):
        row = conn.execute(
            "SELECT vec_to_json(embedding) FROM chunks_vec WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        if row is None:
            return False
        v = emb.vector(text)
        norm = math.hypot(*v)
        blob = struct.pack(f"<{emb.dim}f", *(x / norm for x in v))
        if row[0] != one(conn, "SELECT vec_to_json(vec_quantize_int8(?, 'unit'))", blob):
            return False
    return True


class Recorder:
    """An ``Embedder`` over ``StubEmbedder`` that records document batches (the probe apart) and
    runs test hooks: ``fail`` (exceptions raised by the next calls, in order), ``before_return``
    (called with the batch number before the vectors are returned), ``gate`` (awaited inside
    each document batch)."""

    def __init__(self, stub: StubEmbedder) -> None:
        self.stub = stub
        self.model_id = stub.model_id
        self.batches: list[list[str]] = []
        self.probes = 0
        self.fail: list[BaseException] = []
        self.before_return: Callable[[int], None] | None = None
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.override: Callable[[list[list[float]]], list[list[float]]] | None = None

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise self.fail.pop(0)
        if list(texts) == [PROBE_TEXT]:
            self.probes += 1
            return await self.stub.embed_documents(texts)
        self.batches.append(list(texts))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        vectors = await self.stub.embed_documents(texts)
        if self.before_return is not None:
            self.before_return(len(self.batches))
        if self.override is not None:
            vectors = self.override(vectors)
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return await self.stub.embed_query(text)


class Sleeper:
    """Injected ``sleep``: records delays; an idle sleep sets ``idle`` and then blocks."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.idle = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if seconds == IDLE_S:
            self.idle.set()
            await asyncio.Event().wait()
        await asyncio.sleep(0)


def cfg_for(emb: Recorder | StubEmbedder, **kw: Any) -> TernConfig:
    return TernConfig(embed_model=emb.model_id, embed_idle_poll_s=IDLE_S, **kw)


def queue(store: Store, emb: Recorder, sleeper: Sleeper | None = None, **kw: Any) -> EmbedQueue:
    return EmbedQueue(
        store, emb, cfg_for(emb, **kw), sleep=sleeper or Sleeper(), clock=lambda: NOW + 0.7
    )


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.001)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "data" / "index.db")
    yield s
    s.close()


@pytest.fixture
def filled(store: Store) -> Store:
    """Four messages (subject + body chunk each), dated so that newest-first is M2, M4, M1, M3."""
    add(store, rec(M1, "alpha beta gamma", date_utc=T0))
    add(store, rec(M2, "delta epsilon zeta", date_utc=T0 + 100))
    add(store, rec(M3, "eta theta iota", date_utc=None))
    add(store, rec(M4, "kappa lambda mu", date_utc=T0 + 50))
    return store


# ---- drain -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_embeds_every_chunk_newest_first(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    q = queue(filled, emb, embed_batch_size=3)
    assert q.state.pending == 8 and q.state.status == "ok"
    assert await q.drain() == 8
    by_mid: dict[str, list[str]] = {}
    for _, mid, text in chunks(filled):
        by_mid.setdefault(mid, []).append(text)  # chunk_id order within a message
    expected = [t for mid in (M2, M4, M1, M3) for t in by_mid[mid]]
    assert [t for batch in emb.batches for t in batch] == expected
    assert [len(b) for b in emb.batches] == [3, 3, 2]
    assert emb.probes == 1
    model = vec.current(filled.connection)
    assert model is not None and (model.model_id, model.dim) == (emb.model_id, 64)
    assert stored_matches_text(filled, emb.stub)
    st = vec.status(filled.connection, max_input_chars=6000)
    assert (st.done, st.pending, st.embed_complete_at) == (8, 0, NOW)
    assert q.state == EmbedState(emb.model_id, 0, "ok", 0)


@pytest.mark.asyncio
async def test_completion_time_is_kept_once_set(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    q = queue(filled, emb)
    await q.drain()
    add(filled, rec("e" * 64, "nu xi omicron", date_utc=T0 + 500))
    q2 = EmbedQueue(filled, emb, cfg_for(emb), clock=lambda: NOW + 99)
    assert await q2.drain() == 2
    assert vec.status(filled.connection, max_input_chars=6000).embed_complete_at == NOW


@pytest.mark.asyncio
async def test_a_replace_between_read_and_write_is_embedded_on_the_next_batch(
    filled: Store,
) -> None:
    emb = Recorder(StubEmbedder(dim=64))

    def replace(batch_no: int) -> None:
        if batch_no == 1:  # while the first batch is "at the embedder"
            with filled.transaction():
                assert filled.upsert_message(rec(M1, "replaced meanwhile")) == "replaced"

    emb.before_return = replace
    q = queue(filled, emb, embed_batch_size=32)
    assert await q.drain() == 8
    assert len(emb.batches) == 2
    new_m1 = [text for _, mid, text in chunks(filled) if mid == M1]
    assert "replaced meanwhile" in new_m1
    assert "replaced meanwhile" not in emb.batches[0]
    assert emb.batches[1] == new_m1  # skipped in batch 1 (text re-check), embedded in batch 2
    assert stored_matches_text(filled, emb.stub)


# ---- holds -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_overlapping_holds_block_new_batches_until_both_exit(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    sleeper = Sleeper()
    q = queue(filled, emb, sleeper, embed_batch_size=32)
    first = q.hold("chat")
    second = q.hold("triage")
    await first.__aenter__()
    task = asyncio.create_task(q.run())
    try:
        await until(lambda: q.state.status == "paused")
        await second.__aenter__()
        await first.__aexit__(None, None, None)
        for _ in range(20):
            await asyncio.sleep(0)
        assert emb.batches == [] and emb.probes == 0 and q.state.status == "paused"
        await second.__aexit__(None, None, None)
        await asyncio.wait_for(sleeper.idle.wait(), 5)
        assert len(emb.batches) == 1 and q.state.pending == 0 and q.state.status == "ok"
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_an_in_flight_batch_finishes_under_a_hold(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    emb.gate = asyncio.Event()
    q = queue(filled, emb, embed_batch_size=3)
    task = asyncio.create_task(q.run())
    try:
        await asyncio.wait_for(emb.entered.wait(), 5)
        async with q.hold("chat"):
            emb.gate.set()  # the batch at the embedder returns while the hold is active
            await until(lambda: q.state.status == "paused")
            assert len(emb.batches) == 1
            assert q.state.pending == 5  # its 3 vectors were written; no new batch started
        await until(lambda: q.state.pending == 0)
        assert len(emb.batches) == 3
    finally:
        await stop(task)


# ---- failures ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unavailable_backs_off_and_marks_nothing_failed(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    emb.fail = [EmbedderUnavailable("connect_error (model=x)") for _ in range(11)]
    sleeper = Sleeper()
    q = queue(filled, emb, sleeper)
    seen: list[str] = []
    real_sleep = sleeper.__call__

    async def watching(seconds: float) -> None:
        seen.append(q.state.status)
        await real_sleep(seconds)

    q._sleep = watching  # observe the status during each back-off wait
    task = asyncio.create_task(q.run())
    try:
        await asyncio.wait_for(sleeper.idle.wait(), 5)
    finally:
        await stop(task)
    assert sleeper.delays == [
        1,
        2,
        4,
        8,
        16,
        32,
        64,
        128,
        256,
        BACKOFF_CAP_S,
        BACKOFF_CAP_S,
        IDLE_S,
    ]
    assert seen[:11] == ["unavailable:EmbedderUnavailable"] * 11
    assert q.state.status == "ok" and q.state.pending == 0


@pytest.mark.asyncio
async def test_lm_studio_no_model_loaded_backs_off_and_resumes_after_a_reload(
    filled: Store, caplog: pytest.LogCaptureFixture
) -> None:
    # The real EmbeddingClient against LM Studio's answer while the model is unloaded (JIT
    # loading off): HTTP 400 "No models loaded". The queue retries with back-off and embeds
    # everything once the model is back, instead of stopping for good.
    stub = StubEmbedder(dim=64)
    unloaded = [3]  # requests answered before the model is "reloaded"

    async def handle(request: httpx.Request) -> httpx.Response:
        if unloaded[0] > 0:
            unloaded[0] -= 1
            return httpx.Response(
                400,
                json={
                    "error": "No models loaded. Please load a model in the developer page or "
                    "use the 'lms load' command."
                },
            )
        texts = json.loads(request.content)["input"]
        vectors = await stub.embed_documents(texts)
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
        )

    cfg = cfg_for(stub)
    client = EmbeddingClient(cfg, transport=httpx.MockTransport(handle))
    sleeper = Sleeper()
    q = EmbedQueue(filled, client, cfg, sleep=sleeper, clock=lambda: NOW + 0.7)
    caplog.set_level(logging.INFO, logger="tern.search.embed_queue")
    task = asyncio.create_task(q.run())
    try:
        await asyncio.wait_for(sleeper.idle.wait(), 5)
    finally:
        await stop(task)
        await client.aclose()
    assert sleeper.delays == [1, 2, 4, IDLE_S]
    retrying = [r for r in caplog.records if r.getMessage() == "embedder unavailable; retrying"]
    assert len(retrying) == 3
    assert not [r for r in caplog.records if "stopped" in r.getMessage()]
    assert q.state.status == "ok" and q.state.pending == 0
    assert vec.current(filled.connection) is not None


@pytest.mark.asyncio
async def test_back_off_resets_after_a_success(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    emb.fail = [EmbedderUnavailable("connect_error (model=x)")] * 2  # the probe: 1 s, 2 s
    sleeper = Sleeper()
    q = queue(filled, emb, sleeper, embed_batch_size=4)

    def outage_after_first(batch_no: int) -> None:
        if batch_no == 1:  # batch 1 succeeds, then a new outage of two failures
            emb.fail = [EmbedderUnavailable("timeout (model=x)")] * 2

    emb.before_return = outage_after_first
    task = asyncio.create_task(q.run())
    try:
        await asyncio.wait_for(sleeper.idle.wait(), 5)
    finally:
        await stop(task)
    # without the reset the second outage would wait 4 s and 8 s
    assert sleeper.delays == [1, 2, 1, 2, IDLE_S]
    assert q.state.pending == 0 and q.state.status == "ok"


@pytest.mark.asyncio
async def test_a_probe_failure_is_retried_like_a_batch_failure(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    emb.fail = [EmbedderUnavailable("connect_error (model=x)")]
    sleeper = Sleeper()
    q = queue(filled, emb, sleeper)
    assert q.state.pending == 8
    task = asyncio.create_task(q.run())
    try:
        await asyncio.wait_for(sleeper.idle.wait(), 5)
    finally:
        await stop(task)
    assert sleeper.delays == [1, IDLE_S] and emb.probes == 1
    assert vec.current(filled.connection) is not None and q.state.pending == 0


@pytest.mark.parametrize(
    "override",
    [
        None,  # the embedder raises EmbedderError itself
        lambda vs: vs[:-1],  # one vector short
        lambda vs: [v[:-1] for v in vs],  # dim changed under the same model id
        lambda vs: [[math.nan] * len(v) for v in vs],
        lambda vs: [[0.0] * len(v) for v in vs],
    ],
    ids=["raised", "count", "dim", "nan", "zero"],
)
@pytest.mark.asyncio
async def test_a_malformed_response_stops_the_queue(
    filled: Store,
    caplog: pytest.LogCaptureFixture,
    override: Callable[[list[list[float]]], list[list[float]]] | None,
) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    if override is None:
        emb.before_return = lambda _: emb.fail.append(EmbedderError("bad_shape (model=x)"))
        emb.fail = []
        q = queue(filled, emb, embed_batch_size=2)
    else:
        emb.override = override
        q = queue(filled, emb, embed_batch_size=2)
    caplog.set_level(logging.INFO, logger="tern.search.embed_queue")
    await asyncio.wait_for(q.run(), 5)  # returns: the queue stopped
    assert q.state.status == "error:EmbedderError"
    written = 2 if override is None else 0
    assert vec.status(filled.connection, max_input_chars=6000).done == written
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "stopped" in errors[0].getMessage()
    for r in caplog.records:  # counts and type names only: no chunk text
        assert "alpha" not in r.getMessage() and "delta" not in r.getMessage()
    if override is None:
        emb.fail = [EmbedderError("bad_shape (model=x)")]
    with pytest.raises(EmbedderError):  # drain raises instead of stopping quietly
        await queue(filled, emb).drain()


@pytest.mark.asyncio
async def test_drain_raises_when_unavailable(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    emb.fail = [EmbedderUnavailable("connect_error (model=x)")]
    q = queue(filled, emb)
    with pytest.raises(EmbedderUnavailable):
        await q.drain()
    assert q.state.status == "unavailable:EmbedderUnavailable"
    assert await q.drain() == 8 and q.state.status == "ok"


# ---- model changes, truncation, configuration --------------------------------------------------


@pytest.mark.asyncio
async def test_a_config_model_change_re_registers(filled: Store) -> None:
    first = Recorder(StubEmbedder(dim=64))
    await queue(filled, first).drain()
    second = Recorder(StubEmbedder(dim=32, seed=5))
    q = queue(filled, second)
    assert await q.drain() == 8
    model = vec.current(filled.connection)
    assert model is not None and (model.model_id, model.dim) == (second.model_id, 32)
    assert one(filled.connection, "SELECT count(*) FROM embedding_model") == 2
    assert stored_matches_text(filled, second.stub)
    # the same model again: no new registration, nothing re-embedded
    third = Recorder(StubEmbedder(dim=32, seed=5))
    assert await queue(filled, third).drain() == 0
    assert third.batches == [] and third.probes == 1
    assert one(filled.connection, "SELECT count(*) FROM embedding_model") == 2


@pytest.mark.asyncio
async def test_truncation_is_counted_and_the_full_text_is_rechecked(store: Store) -> None:
    add(store, rec(M1, "x" * 40, subject="s"))  # subject "s a" (3 chars), body 40 chars
    add(store, rec(M2, "short", subject="t"))
    emb = Recorder(StubEmbedder(dim=64))
    q = queue(store, emb, embed_max_input_chars=10)
    assert await asyncio.wait_for(q.drain(), 5) == 4  # a cut text in the re-check never ends
    sent = [t for b in emb.batches for t in b]
    assert max(len(t) for t in sent) == 10 and "x" * 10 in sent
    assert q.state.truncated == 1
    assert vec.status(store.connection, max_input_chars=10).truncated == 1


def test_the_embedder_must_match_the_configured_model(store: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    with pytest.raises(ValueError, match="model id"):
        EmbedQueue(store, emb, TernConfig(embed_model="some-other-model"))


@pytest.mark.asyncio
async def test_disabled_when_no_model_is_configured(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    q = EmbedQueue(filled, emb, TernConfig(embed_model=None))
    assert q.state == EmbedState(None, None, "disabled", 0)
    await asyncio.wait_for(q.run(), 5)
    assert emb.probes == 0 and vec.current(filled.connection) is None
    with pytest.raises(ValueError, match="disabled"):
        await q.drain()


@pytest.mark.asyncio
async def test_run_and_drain_cannot_overlap(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    emb.gate = asyncio.Event()
    q = queue(filled, emb)
    task = asyncio.create_task(q.run())
    try:
        await asyncio.wait_for(emb.entered.wait(), 5)
        with pytest.raises(RuntimeError, match="already running"):
            await q.drain()
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_an_unexpected_error_is_raised_with_its_type_in_the_status(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    q = queue(filled, emb)

    def change_model(batch_no: int) -> None:
        vec.register(filled, "another-model", 16)  # the model changes under the queue

    emb.before_return = change_model
    with pytest.raises(vec.VecStateError):
        await asyncio.wait_for(q.run(), 5)
    assert q.state.status == "error:VecStateError"


@pytest.mark.asyncio
async def test_each_batch_yields_to_the_event_loop(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))  # never suspends, like a CPU embedder
    q = queue(filled, emb, embed_batch_size=1)
    seen: list[int] = []

    async def ingest_stand_in() -> None:
        while True:
            seen.append(len(emb.batches))
            await asyncio.sleep(0)

    other = asyncio.create_task(ingest_stand_in())
    try:
        assert await asyncio.wait_for(q.drain(), 5) == 8
    finally:
        await stop(other)
    assert set(range(1, 8)) <= set(seen)  # the other task ran between batches


# ---- review round 1: idle cost, transaction guard, pre-registration state, no spinning ---------


class IdleSteps:
    """Injected ``sleep`` for idle waits only: each wait records ``snapshot()`` and then runs the
    next step; when no step is left it sets ``done`` and blocks."""

    def __init__(self, snapshot: Callable[[], Any], steps: list[Callable[[], None]]) -> None:
        self.snapshot = snapshot
        self.steps = steps
        self.seen: list[Any] = []
        self.done = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        assert seconds == IDLE_S
        self.seen.append(self.snapshot())
        if not self.steps:
            self.done.set()
            await asyncio.Event().wait()
        self.steps.pop(0)()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_idle_polls_do_not_rescan_an_unchanged_index(
    filled: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"pending": 0, "status": 0}
    real_pending, real_status = vec.pending, vec.status

    def counting_pending(*a: Any, **kw: Any) -> list[vec.PendingChunk]:
        calls["pending"] += 1
        return real_pending(*a, **kw)

    def counting_status(*a: Any, **kw: Any) -> vec.VecStatus:
        calls["status"] += 1
        return real_status(*a, **kw)

    monkeypatch.setattr(vec, "pending", counting_pending)
    monkeypatch.setattr(vec, "status", counting_status)
    emb = Recorder(StubEmbedder(dim=64))

    def noop() -> None:
        return None

    steps = IdleSteps(
        lambda: (calls["pending"], calls["status"]),
        [noop, lambda: add(filled, rec("e" * 64, "nu xi omicron", date_utc=T0 + 500)), noop],
    )
    q = EmbedQueue(filled, emb, cfg_for(emb), sleep=steps, clock=lambda: NOW)
    task = asyncio.create_task(q.run())
    try:
        await asyncio.wait_for(steps.done.wait(), 5)
    finally:
        await stop(task)
    # idle 1: one batch scan + one empty scan + one status read (embed_complete_at);
    # idle 2: nothing read (no write since); then a message is added;
    # idle 3: its batch scan + one empty scan, no status read; idle 4: nothing read.
    assert steps.seen == [(2, 1), (2, 1), (4, 1), (4, 1)]
    assert [len(b) for b in emb.batches] == [8, 2]
    assert stored_matches_text(filled, emb.stub)
    assert real_status(filled.connection, max_input_chars=6000).embed_complete_at == NOW


@pytest.mark.asyncio
async def test_a_transaction_left_open_on_the_store_is_refused(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))

    def open_transaction(_: int) -> None:
        filled.connection.execute("BEGIN")

    emb.before_return = open_transaction
    q = queue(filled, emb)
    try:
        with pytest.raises(RuntimeError, match="inside a transaction"):
            await asyncio.wait_for(q.drain(), 5)
    finally:
        if filled.connection.in_transaction:  # a failure before BEGIN must not be masked
            filled.connection.execute("ROLLBACK")
    assert q.state.status == "error:RuntimeError"
    assert one(filled.connection, "SELECT count(*) FROM chunks_vec") == 0


@pytest.mark.asyncio
async def test_pending_before_registration_counts_a_same_id_model(filled: Store) -> None:
    await queue(filled, Recorder(StubEmbedder(dim=64))).drain()
    add(filled, rec("e" * 64, "nu xi omicron", date_utc=T0 + 500))  # two new chunks
    down = Recorder(StubEmbedder(dim=64))
    down.fail = [EmbedderUnavailable("connect_error (model=x)")]
    q = queue(filled, down)
    assert q.state.pending == 2
    with pytest.raises(EmbedderUnavailable):  # the probe fails: still not registered
        await q.drain()
    assert q.state.pending == 2
    # another configured model id: every chunk is pending for it
    assert queue(filled, Recorder(StubEmbedder(dim=64, seed=9))).state.pending == 10


@pytest.mark.asyncio
async def test_batches_that_write_nothing_do_not_spin(
    filled: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(vec, "insert", lambda *a, **kw: 0)
    caplog.set_level(logging.WARNING, logger="tern.search.embed_queue")
    emb = Recorder(StubEmbedder(dim=64))
    with pytest.raises(vec.VecStateError, match="wrote no vector"):
        await asyncio.wait_for(queue(filled, emb).drain(), 5)
    assert len(emb.batches) == ZERO_PROGRESS_LIMIT
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == ZERO_PROGRESS_LIMIT

    delays: list[float] = []

    async def recording(seconds: float) -> None:
        delays.append(seconds)
        await asyncio.sleep(0)

    emb = Recorder(StubEmbedder(dim=64))
    q = EmbedQueue(filled, emb, cfg_for(emb), sleep=recording, clock=lambda: NOW)
    with pytest.raises(vec.VecStateError):
        await asyncio.wait_for(q.run(), 5)
    assert delays == [IDLE_S] * (ZERO_PROGRESS_LIMIT - 1)  # an idle wait after each
    assert q.state.status == "error:VecStateError"


@pytest.mark.asyncio
async def test_a_write_resets_the_zero_progress_count(
    filled: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review lows for steps 3-4: ``ZERO_PROGRESS_LIMIT - 1`` empty batches, a write, then as
    many empty batches again never reach the limit (without the reset the second run would)."""
    real_insert = vec.insert
    calls = {"n": 0}
    empty = ZERO_PROGRESS_LIMIT - 1

    def flaky_insert(*a: Any, **kw: Any) -> int:
        calls["n"] += 1
        n = calls["n"]
        if n <= empty or empty + 1 < n <= 2 * empty + 1:
            return 0  # nothing written: the chunk stays pending
        return real_insert(*a, **kw)

    monkeypatch.setattr(vec, "insert", flaky_insert)
    emb = Recorder(StubEmbedder(dim=64))
    q = queue(filled, emb, embed_batch_size=1)
    assert await asyncio.wait_for(q.drain(), 5) == 8
    assert calls["n"] == 2 * empty + 8 and q.state.status == "ok"
    assert stored_matches_text(filled, emb.stub)


# ---- merge review: one pending scan per window, not per batch; state without vec.status ---------


def many(store: Store, n: int) -> list[str]:
    """``n`` messages with distinct dates (newest = the last), subjects and bodies; their mids."""
    mids = [f"{i + 1:064x}" for i in range(n)]
    for i, mid in enumerate(mids):
        add(store, rec(mid, f"body word{i} other{i}", date_utc=T0 + i, subject=f"s{i}"))
    return mids


def counting(monkeypatch: pytest.MonkeyPatch, name: str) -> dict[str, int]:
    calls = {"n": 0}
    real = getattr(vec, name)

    def wrapper(*a: Any, **kw: Any) -> Any:
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(vec, name, wrapper)
    return calls


@pytest.mark.asyncio
async def test_one_pending_scan_serves_a_window_of_batches(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mids = many(store, 10)  # 20 chunks
    expected = [t for mid in reversed(mids) for _, m, t in chunks(store) if m == mid]
    scans = counting(monkeypatch, "pending")
    emb = Recorder(StubEmbedder(dim=64))
    q = EmbedQueue(
        store, emb, cfg_for(emb, embed_batch_size=2), clock=lambda: NOW, pending_window=8
    )
    assert await asyncio.wait_for(q.drain(), 5) == 20
    # windows of 8, 8 and 4 chunks, then one empty scan (one scan per batch would be 11)
    assert scans["n"] == 4
    assert [len(b) for b in emb.batches] == [2] * 10
    assert [t for b in emb.batches for t in b] == expected  # still newest message first
    assert stored_matches_text(store, emb.stub)


@pytest.mark.asyncio
async def test_window_chunks_replaced_after_the_scan_are_never_sent(store: Store) -> None:
    """Without the re-check each stale slice would reach the embedder and write nothing, and
    ``ZERO_PROGRESS_LIMIT`` of them in a row would stop the queue."""
    mids = many(store, 12)
    newest = mids[-1]
    emb = Recorder(StubEmbedder(dim=64))
    old: set[str] = set()

    def replace_the_rest(batch_no: int) -> None:
        if batch_no != 1:  # batch 1 holds the newest message's first chunk only
            return
        old.update(t for _, m, t in chunks(store) if m != newest)
        with store.transaction():
            for i, mid in enumerate(mids[:-1]):
                new = rec(mid, f"new body {i}", date_utc=T0 + i, subject=f"s{i} new")
                assert store.upsert_message(new) == "replaced"

    emb.before_return = replace_the_rest
    q = queue(store, emb, embed_batch_size=1)
    assert await asyncio.wait_for(q.drain(), 5) == 24
    assert len(old) == 22
    sent_after_first = {t for b in emb.batches[1:] for t in b}
    assert not sent_after_first & old
    assert len(emb.batches) == 24  # one per chunk: no stale slice was sent
    assert stored_matches_text(store, emb.stub)
    assert q.state.status == "ok"


@pytest.mark.asyncio
async def test_a_hold_makes_the_queue_read_the_window_again(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    emb.gate = asyncio.Event()
    sleeper = Sleeper()
    q = queue(filled, emb, sleeper, embed_batch_size=1)
    task = asyncio.create_task(q.run())
    try:
        await asyncio.wait_for(emb.entered.wait(), 5)  # batch 1 is at the embedder
        async with q.hold("chat"):
            add(filled, rec("e" * 64, "nu xi omicron", date_utc=T0 + 500))  # the newest now
            emb.gate.set()
            await asyncio.wait_for(until(lambda: q.state.status == "paused"), 5)
        await asyncio.wait_for(sleeper.idle.wait(), 5)
    finally:
        await stop(task)
    new = [t for _, mid, t in chunks(filled) if mid == "e" * 64]
    assert len(new) == 2
    assert emb.batches[1:3] == [[new[0]], [new[1]]]  # read again after the hold: newest first
    assert len(emb.batches) == 10
    assert stored_matches_text(filled, emb.stub)


@pytest.mark.asyncio
async def test_a_failed_batch_is_read_again_first(filled: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    q = queue(filled, emb, embed_batch_size=3)
    first_three: list[str] = []

    def outage(batch_no: int) -> None:
        if batch_no == 1:
            first_three.extend(emb.batches[0])
            raise EmbedderUnavailable("connect_error (model=x)")

    emb.before_return = outage
    with pytest.raises(EmbedderUnavailable):
        await asyncio.wait_for(q.drain(), 5)
    emb.before_return = None
    assert await asyncio.wait_for(q.drain(), 5) == 8
    assert emb.batches[1] == first_three
    assert stored_matches_text(filled, emb.stub)


def test_the_pending_window_is_validated(store: Store) -> None:
    emb = Recorder(StubEmbedder(dim=64))
    for bad in (0, -1, True):
        with pytest.raises(ValueError, match="pending_window"):
            EmbedQueue(store, emb, cfg_for(emb), pending_window=bad)


@pytest.mark.asyncio
async def test_state_counts_pending_without_the_per_chunk_probe(
    filled: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merge review: ``state`` uses ``vec.pending_count`` (two counts), never ``vec.status``,
    and both give the same number."""
    emb = Recorder(StubEmbedder(dim=64))
    q = queue(filled, emb, embed_batch_size=3)
    q_other = queue(filled, Recorder(StubEmbedder(dim=64)))  # not registered yet
    stages: list[tuple[int | None, int | None, int]] = []

    def snapshot() -> None:
        real = vec.status(filled.connection, max_input_chars=6000).pending
        with monkeypatch.context() as m:
            m.setattr(vec, "status", lambda *a, **kw: pytest.fail("vec.status called"))
            stages.append((q.state.pending, q_other.state.pending, real))

    emb.before_return = lambda _: snapshot()
    await asyncio.wait_for(q.drain(), 5)
    snapshot()
    add(filled, rec("e" * 64, "nu xi omicron", date_utc=T0 + 500))
    snapshot()
    assert stages == [(8, 8, 8), (5, 5, 5), (2, 2, 2), (0, 0, 0), (2, 2, 2)]
