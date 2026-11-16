"""Plan 05 step 9: latency sanity bounds for search and the embed queue's pending scan.

Marked ``slow`` (wall-clock budgets; ``make test-slow`` only, excluded from ``make check``, L13).
These are sanity bounds on synthetic data, not reportable numbers: the real-size measurement is
plan 08b step 7 and plan 09 against #17's p90 > 300 ms trigger.

- Hybrid search (``SearchService`` over the smoke index, ``StubEmbedder``; the query embedding is
  inside the timed call): p90 < 300 ms over 50 runs of each of the 3 smoke queries.
- ``vec.pending(limit=32)`` on a 50k-chunk synthetic index with a dim-1024 vec table: every one of
  20 calls < 250 ms (the plan's bound is unqualified, so it holds per call, not as a p90), with no
  vector yet and with the newest half embedded (the embed queue's mid-run state).

The module-scoped fixtures build their indexes before the function-scoped autouse ``tern_home``
fixture runs, so each sets ``TERN_HOME`` to its own tmp root for the build (no helper reads the
developer's environment even if one starts to). They keep SQLite's default fsync (the autouse
no-fsync fixture is function-scoped too); that only costs setup time, never a timed call.

Mail here is synthetic: the committed synthetic mailbox (through the smoke helpers) and
generated records that carry no addresses at all.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest

from tern.index.store import LocationState, MessageRecord, Store
from tern.search import vec
from tern.search.service import SearchService
from tern.testing import smoke

pytestmark = pytest.mark.slow

MAILBOX = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_mailbox"

#: Plan 05 step 9 bounds (sanity, not reportable).
SEARCH_P90_S = 0.300
PENDING_MAX_S = 0.250
SEARCH_RUNS = 50
PENDING_RUNS = 20
#: ``tern search``'s default ``--limit``.
SEARCH_LIMIT = 10
PENDING_LIMIT = 32

#: 25k messages x (one subject chunk + one body chunk) = 50k chunks.
N_MESSAGES = 25_000
N_CHUNKS = 2 * N_MESSAGES
DIM = 1024
T0 = 1_700_000_000
FOLDER = "acct1:/INBOX"
WRITE_BATCH = 1_000


def p90(samples: list[float]) -> float:
    """Nearest-rank 90th percentile."""
    if not samples:
        raise ValueError("no samples")
    ordered = sorted(samples)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def ms(seconds: float) -> str:
    return f"{seconds * 1000:.1f} ms"


# ---- search ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def smoke_index(tmp_path_factory: pytest.TempPathFactory) -> tuple[smoke.SmokeConfig, Path]:
    """The smoke index (real Backfill + full stub embedding) built by the smoke helpers."""
    root = tmp_path_factory.mktemp("latency-smoke")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TERN_HOME", str(root))
        cfg = smoke.smoke_config(MAILBOX, root)
        ds = smoke.build_dataset(cfg)
        out = root / "ckpt"
        out.mkdir()
        smoke.train(cfg, ds, out)
    return cfg, out


async def timed[T](call: Callable[[], Awaitable[T]]) -> tuple[float, T]:
    start = time.perf_counter()
    result = await call()
    return time.perf_counter() - start, result


@pytest.mark.asyncio
async def test_hybrid_search_p90_is_under_300_ms(
    smoke_index: tuple[smoke.SmokeConfig, Path],
) -> None:
    cfg, checkpoint = smoke_index
    store = Store.open(checkpoint / "index.db", readonly=True)
    try:
        service = SearchService(store, cfg.embedder, mode="hybrid")
        samples: dict[str, list[float]] = {q: [] for q in smoke.SMOKE_QUERIES}
        for _ in range(SEARCH_RUNS):
            for query in smoke.SMOKE_QUERIES:
                elapsed, result = await timed(lambda q=query: service.search(q, limit=SEARCH_LIMIT))
                # The bound is about the full hybrid path over a fully embedded index.
                assert result.mode == "hybrid"
                assert result.embed_pending == 0
                assert len(result.hits) == SEARCH_LIMIT
                samples[query].append(elapsed)
    finally:
        store.close()
    p90s = {q: p90(s) for q, s in samples.items()}
    report = ", ".join(f"q{i}={ms(v)}" for i, v in enumerate(p90s.values(), 1))
    assert all(len(s) == SEARCH_RUNS for s in samples.values())
    assert max(p90s.values()) < SEARCH_P90_S, f"search p90 over {SEARCH_RUNS} runs: {report}"


# ---- vec.pending ----------------------------------------------------------------------------


def _mid(i: int) -> str:
    return f"{i:064x}"


def _record(i: int) -> MessageRecord:
    return MessageRecord(
        mid=_mid(i),
        content_hash=f"{i:064x}"[::-1],
        parse_status="ok",
        subject=f"synthetic subject {i}",
        body_text=" ".join(f"word{(i * 7 + j) % 997}" for j in range(40)),
        date_utc=T0 + i,
    )


def _vector(chunk_id: int) -> list[float]:
    v = [0.0] * DIM
    v[chunk_id % DIM] = 1.0
    v[(chunk_id * 7 + 3) % DIM] += 0.5
    return v


@pytest.fixture(scope="module")
def big_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Store]:
    """50k chunks through Store's own write methods, a registered dim-1024 model, no vector."""
    root = tmp_path_factory.mktemp("latency-vec")
    mp = pytest.MonkeyPatch()
    mp.setenv("TERN_HOME", str(root))
    store = Store.open(root / "data" / "index.db")
    try:
        for start in range(0, N_MESSAGES, WRITE_BATCH):
            with store.transaction():
                for i in range(start, min(start + WRITE_BATCH, N_MESSAGES)):
                    record = _record(i)
                    store.upsert_message(record)
                    store.add_location(
                        record.mid,
                        FOLDER,
                        record.content_hash,
                        LocationState(),
                        T0,
                        source="backfill",
                    )
        conn = store.connection
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == N_CHUNKS
        vec.register(store, "fake-1024", DIM)
        yield store
    finally:
        store.close()
        mp.undo()


def _time_pending(store: Store) -> tuple[list[float], list[vec.PendingChunk]]:
    samples: list[float] = []
    first: list[vec.PendingChunk] | None = None
    for _ in range(PENDING_RUNS):
        start = time.perf_counter()
        rows = vec.pending(store.connection, PENDING_LIMIT)
        samples.append(time.perf_counter() - start)
        if first is None:
            first = rows
        assert rows == first
    assert first is not None
    return samples, first


def _newest_mid_order(rows: list[vec.PendingChunk]) -> list[str]:
    seen: list[str] = []
    for row in rows:
        if row.mid not in seen:
            seen.append(row.mid)
    return seen


def test_pending_is_under_250_ms_per_call_on_50k_chunks(big_store: Store) -> None:
    conn = big_store.connection
    model = vec.current(conn)
    assert model is not None and model.dim == DIM
    assert vec.pending_count(conn) == N_CHUNKS

    # No vector yet: every chunk qualifies; the 32 newest come back (16 messages x 2 chunks).
    cold, rows = _time_pending(big_store)
    assert len(rows) == PENDING_LIMIT
    assert _newest_mid_order(rows) == [_mid(i) for i in range(N_MESSAGES - 1, N_MESSAGES - 17, -1)]

    # Newest half embedded, as the queue (newest first) leaves it mid-run.
    half = N_MESSAGES // 2
    newest = conn.execute(
        "SELECT c.chunk_id, c.text FROM chunks c JOIN messages m ON m.mid = c.mid "
        "WHERE m.date_utc >= ? ORDER BY c.chunk_id",
        (T0 + half,),
    ).fetchall()
    assert len(newest) == N_CHUNKS // 2
    written = 0
    for start in range(0, len(newest), WRITE_BATCH):
        batch = newest[start : start + WRITE_BATCH]
        written += vec.insert(big_store, model, [(cid, text, _vector(cid)) for cid, text in batch])
    assert written == N_CHUNKS // 2
    assert vec.pending_count(conn) == N_CHUNKS // 2

    mid_run, rows = _time_pending(big_store)
    assert len(rows) == PENDING_LIMIT
    assert _newest_mid_order(rows) == [_mid(i) for i in range(half - 1, half - 17, -1)]

    report = ", ".join(
        f"{state}: p90 {ms(p90(s))}, max {ms(max(s))}"
        for state, s in (("no vectors", cold), ("newest half embedded", mid_run))
    )
    assert len(cold) == len(mid_run) == PENDING_RUNS
    assert max(cold + mid_run) < PENDING_MAX_S, (
        f"vec.pending(limit=32) over {PENDING_RUNS} calls: {report}"
    )
