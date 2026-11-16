"""Plan 05 step 5: ``tern.search.fts.build_match`` and ``tern.search.service.SearchService``.

The index is built by the real ingest (``Backfill`` over ``FakeBridge``) from the committed
synthetic mailbox; expected mids come from MANIFEST.json keys or from the index's own tables
read in Python, never from a search run. One inline record (made-up words, no address) covers
the NULL date / sender case the fixture lacks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import shutil
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm

import tern.search.service as service_mod
from tern.config import TernConfig
from tern.index.chunks import fold
from tern.index.ingest import Backfill
from tern.index.source import IngestSource
from tern.index.store import LocationState, MessageRecord, Store
from tern.search import vec
from tern.search.embedder import EmbedderError, EmbedderUnavailable
from tern.search.fts import build_match
from tern.search.service import (
    EmptyQuery,
    Hit,
    ModelMismatch,
    NoEmbeddingModel,
    SearchFilters,
    SearchResult,
    SearchService,
    SearchStateError,
    candidates,
    snippet,
)
from tern.testing.fake_bridge import FakeBridge
from tern.testing.stub_embedder import StubEmbedder

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
MID = {m["key"]: m["mid"] for m in MANIFEST["messages"]}
STUB = StubEmbedder(dim=64)
SMOKE_QUERIES = ("Acme invoice March", "Rechnung Hausverwaltung", "συνάντηση Πέμπτη")
#: Strings that FTS5 rejects as a raw MATCH expression.
PROBES = (
    "INV-0042",
    "bob@example.com",
    "due?",
    "who's",
    "NOT x",
    "subject: hi",
    '"unbalanced',
    "(x",
    "Acme AND",
)
NO_FTS_HIT = "Rechnung Hausverwaltung"  # no German fixture mail (A-22): vector-only hits


# ---- index fixtures -------------------------------------------------------------------------


async def build_index(path: Path, *, embed: bool) -> None:
    """A fresh index of the synthetic mailbox at ``path`` (closed afterwards), with every chunk
    embedded by the stub when ``embed``."""
    store = Store.open(path)
    try:
        await Backfill(store, IngestSource(FakeBridge(FIXTURE)), TernConfig()).run()
        if embed:
            embed_all(store, STUB)
    finally:
        store.close()


def embed_all(store: Store, emb: StubEmbedder) -> None:
    model = vec.register(store, emb.model_id, emb.dim)
    while batch := vec.pending(store.connection, 100):
        n = vec.insert(store, model, [(p.chunk_id, p.text, emb.vector(p.text)) for p in batch])
        assert n == len(batch)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("svc")
    base = root / "base.db"
    asyncio.run(build_index(base, embed=False))
    embedded = root / "embedded.db"
    shutil.copyfile(base, embedded)
    store = Store.open(embedded)
    try:
        embed_all(store, STUB)
    finally:
        store.close()
    return {"base": base, "embedded": embedded}


def _open_copy(src: Path, dst: Path) -> Store:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return Store.open(dst)


@pytest.fixture
def store(built: dict[str, Path], tmp_path: Path) -> Iterator[Store]:
    """A writable copy of the fully embedded index."""
    s = _open_copy(built["embedded"], tmp_path / "data" / "index.db")
    yield s
    s.close()


@pytest.fixture
def bare(built: dict[str, Path], tmp_path: Path) -> Iterator[Store]:
    """A writable copy of the index without any embedding model."""
    s = _open_copy(built["base"], tmp_path / "bare" / "index.db")
    yield s
    s.close()


def hybrid(store: Store, emb: Any = STUB) -> SearchService:
    return SearchService(store, emb, mode="hybrid")


def fts(store: Store) -> SearchService:
    return SearchService(store, None, mode="fts")


def mids(result: SearchResult) -> list[str]:
    return [h.mid for h in result.hits]


def column(conn: sqlite3.Connection, sql: str, *args: Any) -> list[Any]:
    return [r[0] for r in conn.execute(sql, args)]


class SpyEmbedder:
    """Counts calls; raises ``exc`` (if set) from both methods, else answers like the stub."""

    def __init__(self, model_id: str = STUB.model_id, exc: BaseException | None = None) -> None:
        self.model_id = model_id
        self.exc = exc
        self.calls = 0

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return [STUB.vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return STUB.vector(text)


# ---- build_match ----------------------------------------------------------------------------


def test_build_match_quotes_each_run_and_or_joins_them() -> None:
    assert build_match("Acme invoice") == '"Acme" OR "invoice"'
    assert build_match('say "hi" now') == '"say" OR """hi""" OR "now"'
    assert build_match("  INV-0042\tdue?\n") == '"INV-0042" OR "due?"'


def test_build_match_folds_like_the_index() -> None:
    assert build_match("συνάντηση Πέμπτη") == '"συναντηση" OR "Πεμπτη"'
    assert build_match("café") == '"cafe"'


def test_build_match_drops_runs_without_a_word_character() -> None:
    assert build_match("-- ?! x") == '"x"'
    for empty in ("", "   ", "?!", "-- ...", '"'):
        assert build_match(empty) is None


def test_build_match_turns_control_characters_into_separators() -> None:
    assert build_match("a\x00b") == '"a" OR "b"'
    assert build_match("\x1b[2J") == '"[2J"'


def test_build_match_rejects_a_non_str() -> None:
    with pytest.raises(TypeError):
        build_match(b"x")  # pyright: ignore[reportArgumentType]  # the runtime check under test


# ---- the FTS arm on the real index ----------------------------------------------------------


@pytest.mark.parametrize("probe", PROBES)
def test_probe_breaks_a_raw_match(store: Store, probe: str) -> None:
    """Each probe is a real hazard: handed to MATCH as written, FTS5 refuses it."""
    with pytest.raises(sqlite3.OperationalError):
        store.connection.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (probe,)
        ).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", (*PROBES, "a\x00b"))
async def test_probe_runs_in_both_modes(store: Store, probe: str) -> None:
    for service in (fts(store), hybrid(store)):
        result = await service.search(probe)
        assert len(result.hits) <= 10


@pytest.mark.asyncio
async def test_not_x_finds_documents_with_not_or_x(store: Store) -> None:
    result = await fts(store).search("NOT x", limit=50)
    assert result.hits
    word = re.compile(r"\b(not|x)\b", re.IGNORECASE)
    conn = store.connection
    for mid in mids(result):
        texts = column(conn, "SELECT fts_text FROM chunks WHERE mid = ?", mid)
        assert any(word.search(t) for t in texts)


@pytest.mark.asyncio
async def test_greek_with_tonos_matches_folded_text_and_the_snippet_keeps_it(
    store: Store,
) -> None:
    expected = {MID["meeting-thursday-el"], MID["meeting-thursday-el-re"]}
    with_tonos = await fts(store).search("συνάντηση Πέμπτη")
    without = await fts(store).search("ΣΥΝΑΝΤΗΣΗ πεμπτη")
    assert set(mids(with_tonos)) == expected
    assert mids(without) == mids(with_tonos)
    by_mid = {h.mid: h for h in with_tonos.hits}
    assert "Συνάντηση" in by_mid[MID["meeting-thursday-el"]].snippet  # tonos kept


@pytest.mark.asyncio
async def test_acme_invoice_march_fts_hits_the_invoice_thread(store: Store) -> None:
    result = await fts(store).search("Acme invoice March", limit=2)
    assert set(mids(result)) == {MID["inv-0042"], MID["inv-0042-reminder"]}
    assert result.mode == "fts" and result.model_id is None


# ---- snippets and hit fields ----------------------------------------------------------------


def test_snippet_collapses_whitespace_and_cuts_at_200() -> None:
    assert snippet("  a\n\n b\t c  ") == "a b c"
    long = "word " * 100
    assert snippet(long) == ("word " * 40)[:200]
    assert len(snippet(long)) == 200


@pytest.mark.asyncio
async def test_snippet_is_the_original_text_of_one_of_the_mids_chunks(store: Store) -> None:
    result = await hybrid(store).search("numbers section", limit=20)
    conn = store.connection
    assert any("\n" in t for t in column(conn, "SELECT text FROM chunks"))  # bodies have breaks
    for hit in result.hits:
        texts = column(conn, "SELECT text FROM chunks WHERE mid = ?", hit.mid)
        assert hit.snippet in {" ".join(t.split())[:200] for t in texts}
        assert "\n" not in hit.snippet and len(hit.snippet) <= 200


def _chunk_text(conn: sqlite3.Connection, mid: str, kind: str) -> str:
    (text,) = column(conn, "SELECT text FROM chunks WHERE mid = ? AND kind = ?", mid, kind)
    return text


@pytest.mark.asyncio
async def test_the_snippet_is_the_chunk_the_fts_arm_ranked_for_the_mid(store: Store) -> None:
    """Only inv-0042's body chunk has "services": the snippet is that chunk, not the subject
    (the mid's first chunk) nor the attachment name."""
    conn = store.connection
    mid = MID["inv-0042"]
    matching = "SELECT c.mid FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.rowid"
    assert column(conn, f"{matching} WHERE chunks_fts MATCH 'services'") == [mid]
    body, subject = _chunk_text(conn, mid, "body"), _chunk_text(conn, mid, "subject")
    assert snippet(body) != snippet(subject)
    (hit,) = (await fts(store).search("services", limit=1)).hits
    assert (hit.mid, hit.snippet) == (mid, snippet(body))


@pytest.mark.asyncio
async def test_on_equal_ranks_the_fts_arms_chunk_is_the_snippet(bare: Store) -> None:
    """inv-0042 is rank 1 in both arms through different chunks: FTS through its body (the
    only "services" chunk), the vector arm through its subject, which gets the query's own
    vector (distance 0, every other chunk its stub vector). The FTS chunk wins the tie."""
    conn = bare.connection
    mid = MID["inv-0042"]
    body, subject = _chunk_text(conn, mid, "body"), _chunk_text(conn, mid, "subject")
    (subject_id,) = column(
        conn, "SELECT chunk_id FROM chunks WHERE mid = ? AND kind = ?", mid, "subject"
    )
    q = STUB.vector("services")
    model = vec.register(bare, STUB.model_id, STUB.dim)
    rows = conn.execute("SELECT chunk_id, text FROM chunks").fetchall()
    items = [(cid, text, q if cid == subject_id else STUB.vector(text)) for cid, text in rows]
    assert vec.insert(bare, model, items) == len(rows)
    (first, d1), (_, d2) = vec.knn(conn, q, 2)
    assert first == subject_id and d1 < d2  # the vector arm's rank 1 is the subject alone
    (hit,) = (await hybrid(bare).search("services", limit=1)).hits
    assert hit.mid == mid
    assert hit.snippet == snippet(body) != snippet(subject)


@pytest.mark.asyncio
async def test_hit_fields_come_from_the_index(store: Store) -> None:
    result = await fts(store).search("INV-0042 March", limit=1)
    (hit,) = result.hits
    row = store.connection.execute(
        "SELECT date_utc, from_addr, from_name, subject FROM messages WHERE mid = ?", (hit.mid,)
    ).fetchone()
    folders = column(store.connection, "SELECT folder_id FROM locations WHERE mid = ?", hit.mid)
    assert hit.date == datetime.fromtimestamp(row[0], UTC)
    assert (hit.from_addr, hit.from_name, hit.subject) == row[1:]
    assert list(hit.folder_ids) == sorted(folders)
    assert hit.score > 0 and not hit.conflicted


@pytest.mark.asyncio
async def test_null_date_and_sender_stay_none_and_fail_date_filters(store: Store) -> None:
    mid = "e" * 64
    with store.transaction():
        store.upsert_message(
            MessageRecord(
                mid=mid,
                content_hash="3" * 64,
                parse_status="ok",
                subject=None,
                body_text="zorblat quindle",
                date_utc=None,
            )
        )
        store.add_location(mid, "acct1:/INBOX", "3" * 64, LocationState(), 0, source="backfill")
    embed_all(store, STUB)
    (hit,) = (await hybrid(store).search("zorblat", limit=1)).hits
    assert hit.mid == mid
    assert (hit.date, hit.from_addr, hit.from_name, hit.subject) == (None, None, None, None)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    future = datetime(2999, 1, 1, tzinfo=UTC)
    for filters in (SearchFilters(after=epoch), SearchFilters(before=future)):
        result = await hybrid(store).search("zorblat", filters, limit=50)
        assert mid not in mids(result)


def _insert_inline(store: Store, mid: str, **fields: Any) -> None:
    """One made-up message (body "zorblat quindle") in acct1:/INBOX."""
    with store.transaction():
        store.upsert_message(
            MessageRecord(
                mid=mid,
                content_hash="3" * 64,
                parse_status="ok",
                body_text="zorblat quindle",
                **fields,
            )
        )
        store.add_location(mid, "acct1:/INBOX", "3" * 64, LocationState(), 0, source="backfill")


@pytest.mark.asyncio
async def test_a_stored_date_outside_the_datetime_range_is_a_search_state_error(
    store: Store,
) -> None:
    """31 Dec 9999 23:59:59 -2359 as an epoch (ingest now stores NULL for it, an older index
    may hold it): not a bare ValueError, which a caller would report as a bad query."""
    _insert_inline(store, "e" * 64, date_utc=253402387139)
    with pytest.raises(SearchStateError, match="date_utc out of range") as info:
        await fts(store).search("zorblat", limit=1)
    assert not isinstance(info.value, ValueError)
    assert store.connection.in_transaction is False


# ---- determinism ----------------------------------------------------------------------------


def _same_vector_for_every_chunk(store: Store, order_seed: int) -> None:
    """Register the stub and give every chunk one identical vector, inserted in a shuffled
    order: every KNN distance ties, and vec0's tie order follows the physical layout."""
    model = vec.register(store, STUB.model_id, STUB.dim)
    ids = column(store.connection, "SELECT chunk_id FROM chunks")
    texts = dict(store.connection.execute("SELECT chunk_id, text FROM chunks"))
    random.Random(order_seed).shuffle(ids)
    same = STUB.vector("one shared vector")
    for i in range(0, len(ids), 7):
        batch = ids[i : i + 7]
        assert vec.insert(store, model, [(cid, texts[cid], same) for cid in batch]) == len(batch)


@pytest.mark.asyncio
async def test_identical_vectors_in_two_shuffled_orders_give_identical_hits(
    built: dict[str, Path], tmp_path: Path
) -> None:
    results: list[SearchResult] = []
    for seed in (1, 2):
        s = _open_copy(built["base"], tmp_path / str(seed) / "index.db")
        try:
            n_chunks = len(column(s.connection, "SELECT chunk_id FROM chunks"))
            assert n_chunks > candidates(10) + 1  # the tie spans the arm's cut
            _same_vector_for_every_chunk(s, seed)
            results.append(await hybrid(s).search(NO_FTS_HIT, limit=10))
            by_chunk = column(s.connection, "SELECT mid FROM chunks ORDER BY chunk_id")
        finally:
            s.close()
        # all distances tie, so the vector arm is chunk_id order, collapsed to mids
        expected = list(dict.fromkeys(by_chunk))[:10]
        assert mids(results[-1]) == expected
    assert results[0] == results[1]


@pytest.mark.asyncio
async def test_results_are_identical_across_two_fresh_stores(
    built: dict[str, Path], tmp_path: Path
) -> None:
    fresh = tmp_path / "fresh" / "index.db"
    await build_index(fresh, embed=True)
    out: list[list[SearchResult]] = []
    for path in (built["embedded"], fresh):
        s = Store.open(path, readonly=True)
        try:
            out.append([await hybrid(s).search(q) for q in (*SMOKE_QUERIES, "INV-0077")])
        finally:
            s.close()
    assert out[0] == out[1]
    assert all(r.hits for r in out[0])


# ---- filters --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_filter_that_excludes_the_nearest_neighbours_still_fills_limit(
    bare: Store,
) -> None:
    """The folder's chunks get the vector opposite the query's (the farthest possible), every
    other chunk its stub vector, so the arm's C+1 global nearest chunks hold none of the
    folder's: only a pre-filter (``chunk_id IN (…)`` inside the KNN) can fill ``limit``. The
    query has no FTS hit, so the hits come from the vector arm alone."""
    folder = "acct3:/INBOX"
    conn = bare.connection
    in_folder = set(column(conn, "SELECT mid FROM locations WHERE folder_id = ?", folder))
    assert len(in_folder) >= 10
    q = STUB.vector(NO_FTS_HIT)
    model = vec.register(bare, STUB.model_id, STUB.dim)
    rows = conn.execute("SELECT chunk_id, mid, text FROM chunks").fetchall()
    items = [
        (cid, text, [-x for x in q] if mid in in_folder else STUB.vector(text))
        for cid, mid, text in rows
    ]
    assert vec.insert(bare, model, items) == len(rows)
    nearest = vec.knn(conn, q, candidates(10) + 1)
    near_ids = [cid for cid, _ in nearest]
    marks = ",".join("?" * len(near_ids))
    near_mids = set(column(conn, f"SELECT mid FROM chunks WHERE chunk_id IN ({marks})", *near_ids))
    assert not near_mids & in_folder  # the filter excludes every candidate an unfiltered arm sees
    result = await hybrid(bare).search(NO_FTS_HIT, SearchFilters(folder_id=folder), limit=10)
    assert len(result.hits) == 10
    assert set(mids(result)) <= in_folder
    assert all(folder in h.folder_ids for h in result.hits)


@pytest.mark.asyncio
async def test_folder_filter_selects_exactly_the_folders_mids(store: Store) -> None:
    folder = "acct1:/Invoices"
    expected = set(
        column(store.connection, "SELECT mid FROM locations WHERE folder_id = ?", folder)
    )
    assert 0 < len(expected) <= 50
    # every chunk is a vector candidate (C >= chunk count), so hybrid returns every match
    result = await hybrid(store).search("invoice", SearchFilters(folder_id=folder), limit=50)
    assert set(mids(result)) == expected


@pytest.mark.asyncio
async def test_from_filter_is_a_folded_casefolded_substring_of_name_or_address(
    store: Store,
) -> None:
    conn = store.connection
    addr, name = conn.execute(
        "SELECT from_addr, from_name FROM messages WHERE mid = ?", (MID["meeting-thursday-el"],)
    ).fetchone()
    assert fold(name) != name  # a Greek name with tonos
    rows = conn.execute("SELECT mid, from_name FROM messages WHERE from_addr = ?", (addr,))
    expected = {mid for mid, from_name in rows if from_name == name}
    assert expected and len(expected) <= 50
    assert not set(
        column(conn, "SELECT mid FROM messages WHERE from_addr = ? AND from_name != ?", addr, name)
    )
    needles = (fold(name).upper()[:8], name.lower(), addr.upper()[:7], f" {addr.title()} ")
    for needle in needles:
        result = await hybrid(store).search("hello", SearchFilters(from_=needle), limit=50)
        assert set(mids(result)) == expected, needle


@pytest.mark.asyncio
async def test_date_filters_after_inclusive_before_exclusive_naive_is_utc(store: Store) -> None:
    t = store.connection.execute(
        "SELECT date_utc FROM messages WHERE mid = ?", (MID["inv-0042"],)
    ).fetchone()[0]
    same_t = set(column(store.connection, "SELECT mid FROM messages WHERE date_utc = ?", t))
    assert MID["inv-0042"] in same_t
    at = datetime.fromtimestamp(t, UTC)
    second = timedelta(seconds=1)
    cases = [
        (SearchFilters(after=at, before=at + second), same_t),
        (
            SearchFilters(after=at.replace(tzinfo=None), before=(at + second).replace(tzinfo=None)),
            same_t,
        ),
        (
            SearchFilters(after=at.astimezone(timezone(timedelta(hours=2))), before=at + second),
            same_t,
        ),
        (SearchFilters(after=at, before=at), set()),
        (SearchFilters(after=at + timedelta(milliseconds=500), before=at + second), set()),
        (SearchFilters(after=at - second, before=at), None),
    ]
    for filters, expected in cases:
        result = await hybrid(store).search("invoice", filters, limit=50)
        if expected is None:
            assert MID["inv-0042"] not in mids(result)
        else:
            assert set(mids(result)) == expected


@pytest.mark.asyncio
async def test_has_attachment_filter(store: Store) -> None:
    conn = store.connection
    with_att = set(column(conn, "SELECT mid FROM messages WHERE has_attachments = 1"))
    assert 0 < len(with_att) <= 50
    result = await hybrid(store).search("invoice", SearchFilters(has_attachment=True), limit=50)
    assert set(mids(result)) == with_att
    result = await hybrid(store).search("invoice", SearchFilters(has_attachment=False), limit=50)
    assert len(result.hits) == 50 and not set(mids(result)) & with_att


@pytest.mark.asyncio
async def test_null_has_attachments_fails_both_attachment_filters(store: Store) -> None:
    mid = "e" * 64
    _insert_inline(store, mid)
    no_att = SearchFilters(has_attachment=False)
    assert mid in mids(await fts(store).search("zorblat", no_att))  # stored as 0
    with store.transaction():
        store.connection.execute("UPDATE messages SET has_attachments = NULL WHERE mid = ?", (mid,))
    for value in (True, False):
        result = await fts(store).search("zorblat", SearchFilters(has_attachment=value))
        assert mid not in mids(result), value


@pytest.mark.parametrize(
    "kwargs",
    [
        {"folder_id": ""},
        {"from_": "  "},
        {"from_": "́"},  # folds to nothing
        {"from_": "\u0301 \u0301"},  # folds to a lone space
        {"after": "2026-01-01"},
        {"has_attachment": 1},
        {"include_conflicted": None},
    ],
)
def test_bad_filters_raise(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        SearchFilters(**kwargs)


# ---- conflicted mids ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conflicted_mid_is_flagged_and_can_be_excluded(store: Store) -> None:
    conflicted = MID["inv-0077"]
    for service in (fts(store), hybrid(store)):
        result = await service.search("INV-0077")
        by_mid = {h.mid: h for h in result.hits}
        assert by_mid[conflicted].conflicted is True
        assert sum(h.conflicted for h in result.hits) == 1
        excluded = await service.search("INV-0077", SearchFilters(include_conflicted=False))
        assert conflicted not in mids(excluded)
        assert not any(h.conflicted for h in excluded.hits)


# ---- modes and errors -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_embedding_model_raises_in_hybrid(bare: Store) -> None:
    spy = SpyEmbedder()
    with pytest.raises(NoEmbeddingModel):
        await hybrid(bare, spy).search("invoice")
    assert spy.calls == 0


@pytest.mark.asyncio
async def test_model_mismatch_raises(store: Store) -> None:
    spy = SpyEmbedder(model_id=StubEmbedder(dim=64, seed=1).model_id)
    with pytest.raises(ModelMismatch):
        await hybrid(store, spy).search("invoice")
    assert spy.calls == 0


@pytest.mark.asyncio
async def test_a_model_change_during_the_embed_call_raises_mismatch(store: Store) -> None:
    other = StubEmbedder(dim=32, seed=3)

    class Reregistering(SpyEmbedder):
        async def embed_query(self, text: str) -> list[float]:
            vec.register(store, other.model_id, other.dim)
            return await super().embed_query(text)

    with pytest.raises(ModelMismatch):
        await hybrid(store, Reregistering()).search("invoice")
    assert store.connection.in_transaction is False  # raised inside the snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [EmbedderUnavailable("connect_error"), EmbedderError("x")])
async def test_embedder_failures_propagate(store: Store, exc: Exception) -> None:
    with pytest.raises(type(exc)):
        await hybrid(store, SpyEmbedder(exc=exc)).search("invoice")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [[1.0] * 63, [0.0] * 64, [float("nan")] + [1.0] * 63])
async def test_a_bad_query_vector_is_an_embedder_error(store: Store, bad: list[float]) -> None:
    class Bad(SpyEmbedder):
        async def embed_query(self, text: str) -> list[float]:
            return bad

    with pytest.raises(EmbedderError):
        await hybrid(store, Bad()).search("invoice")
    assert store.connection.in_transaction is False  # raised inside the snapshot


@pytest.mark.asyncio
async def test_fts_mode_never_calls_the_embedder(bare: Store) -> None:
    spy = SpyEmbedder(exc=AssertionError("embedder called in fts mode"))
    service = SearchService(bare, spy, mode="fts")
    result = await service.search("Acme invoice March")
    assert spy.calls == 0
    assert result.hits and result.mode == "fts" and result.model_id is None
    assert result.embed_pending == len(column(bare.connection, "SELECT chunk_id FROM chunks"))


def test_hybrid_without_an_embedder_and_unknown_modes_raise(store: Store) -> None:
    with pytest.raises(ValueError):
        SearchService(store, None, mode="hybrid")
    with pytest.raises(ValueError):
        SearchService(store, STUB, mode="vector")  # pyright: ignore[reportArgumentType]  # runtime check


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "   ", "?!", "-- ...", "\x00"])
async def test_a_zero_term_query_raises_empty_query(store: Store, query: str) -> None:
    spy = SpyEmbedder()
    for service in (fts(store), hybrid(store, spy)):
        with pytest.raises(EmptyQuery):
            await service.search(query)
    assert spy.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 51, -1, True, 2.0])
async def test_limit_outside_1_to_50_raises(store: Store, limit: Any) -> None:
    with pytest.raises(ValueError):
        await fts(store).search("invoice", limit=limit)


@pytest.mark.asyncio
async def test_limit_bounds_and_distinct_mids(store: Store) -> None:
    for limit in (1, 50):
        result = await hybrid(store).search("invoice reminder", limit=limit)
        assert len(result.hits) == limit
        assert len(set(mids(result))) == limit
        scores = [(-h.score, h.mid) for h in result.hits]
        assert scores == sorted(scores)


def test_candidates_per_arm() -> None:
    assert [candidates(n) for n in (1, 10, 11, 50)] == [100, 100, 110, 500]


@pytest.mark.asyncio
async def test_a_tie_wider_than_max_k_warns_and_still_fills_limit(
    bare: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Every vector equal, C=5 and the cap 12: k goes 6 → 12, the tie is still open at the cap,
    so the arm warns and cuts by vec0 layout instead of failing."""
    _same_vector_for_every_chunk(bare, 1)
    monkeypatch.setattr(vec, "MAX_K", 12)
    monkeypatch.setattr(service_mod, "candidates", lambda limit: 5)
    ks: list[int] = []
    real_knn = vec.knn

    def spy(conn: sqlite3.Connection, qvec: Any, k: int, *args: Any) -> list[tuple[int, float]]:
        ks.append(k)
        return real_knn(conn, qvec, k, *args)

    monkeypatch.setattr(vec, "knn", spy)
    caplog.set_level(logging.WARNING, logger="tern.search.service")
    result = await hybrid(bare).search(NO_FTS_HIT, limit=1)
    assert ks == [6, 12]
    assert len(result.hits) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["vector arm: distance tie larger than k=12; cut by vec0 layout"]


def _add_chunk(store: Store, mid: str, text: str | None, fts_text: str) -> None:
    with store.transaction():
        store.connection.execute(
            "INSERT INTO chunks(mid, ord, kind, text, fts_text) VALUES (?, 99, 'body', ?, ?)",
            (mid, text, fts_text),
        )


@pytest.mark.asyncio
async def test_a_chunk_without_a_message_row_is_a_search_state_error(store: Store) -> None:
    _add_chunk(store, "f" * 64, "qwzzyx", "qwzzyx")  # chunks.mid has no foreign key
    with pytest.raises(SearchStateError, match="no message row"):
        await fts(store).search("qwzzyx")
    assert store.connection.in_transaction is False


@pytest.mark.asyncio
async def test_a_chunk_without_text_is_a_search_state_error(store: Store) -> None:
    _add_chunk(store, MID["inv-0042"], None, "qwzzyx")
    with pytest.raises(SearchStateError, match="no text"):
        await fts(store).search("qwzzyx")


def _orphan_vector(store: Store, vector: list[float]) -> None:
    """A chunks_vec row whose chunk does not exist (written past ``vec.insert``'s check)."""
    conn = store.connection
    orphan = int(conn.execute("SELECT max(chunk_id) FROM chunks").fetchone()[0]) + 1000
    with store.transaction():
        conn.execute(
            f"INSERT INTO {vec.TABLE}(chunk_id, embedding) "
            "VALUES (?, vec_quantize_int8(?, 'unit'))",
            (orphan, json.dumps(vector)),
        )


@pytest.mark.asyncio
async def test_a_vector_without_a_chunk_is_a_search_state_error(store: Store) -> None:
    _orphan_vector(store, STUB.vector(NO_FTS_HIT))  # the query's nearest vector
    with pytest.raises(SearchStateError, match="a vector has no chunk"):
        await hybrid(store).search(NO_FTS_HIT)
    assert store.connection.in_transaction is False


def test_pending_count_with_more_vectors_than_chunks_raises(store: Store) -> None:
    assert vec.pending_count(store.connection) == 0
    _orphan_vector(store, STUB.vector("x"))
    with pytest.raises(vec.VecStateError, match="more rows than chunks"):
        vec.pending_count(store.connection)


# ---- embed_pending and read-only use --------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_pending_counts_chunks_without_a_vector(store: Store) -> None:
    conn = store.connection
    assert (await hybrid(store).search("invoice")).embed_pending == 0
    model = vec.register(store, "stub-fh-64-9", 64)
    other = SpyEmbedder(model_id=model.model_id)
    total = len(column(conn, "SELECT chunk_id FROM chunks"))
    assert vec.pending_count(conn) == total
    batch = vec.pending(conn, 10)
    vec.insert(store, model, [(p.chunk_id, p.text, STUB.vector(p.text)) for p in batch])
    result = await hybrid(store, other).search("invoice")
    assert result.embed_pending == total - 10 == vec.status(conn, max_input_chars=6000).pending
    assert result.model_id == model.model_id


def test_pending_count_without_a_model_is_every_chunk(bare: Store) -> None:
    conn = bare.connection
    assert vec.pending_count(conn) == len(column(conn, "SELECT chunk_id FROM chunks"))


@pytest.mark.asyncio
async def test_a_read_only_store_runs_hybrid_search(built: dict[str, Path]) -> None:
    s = Store.open(built["embedded"], readonly=True)
    try:
        result = await hybrid(s).search("Acme invoice March", limit=3)
    finally:
        s.close()
    assert result.mode == "hybrid" and result.model_id == STUB.model_id
    assert result.embed_pending == 0
    assert {MID["inv-0042"], MID["inv-0042-reminder"]} <= set(mids(result))


@pytest.mark.asyncio
async def test_search_inside_an_open_transaction_is_refused(store: Store) -> None:
    store.connection.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError, match="open transaction"):
            await fts(store).search("invoice")
    finally:
        if store.connection.in_transaction:
            store.connection.execute("ROLLBACK")


def test_hit_is_frozen() -> None:
    hit = Hit("m", None, None, None, None, "", 0.0, (), False)
    with pytest.raises(AttributeError):
        hit.mid = "x"  # pyright: ignore[reportAttributeAccessIssue]  # frozen check under test


def test_fold_is_the_index_fold() -> None:
    """The FTS query fold and ``chunks.fts_text`` use the same function (no second copy)."""
    assert build_match("Πέμπτη") == f'"{fold("Πέμπτη")}"'


# ---- plan 06b step 0: the Store accessor ToolContext checks ----------------------------------


def test_store_is_the_store_the_service_searches(store: Store) -> None:
    svc = fts(store)
    assert svc.store is store
    assert hybrid(store).store is store


def test_store_is_read_only(store: Store) -> None:
    svc = fts(store)
    with pytest.raises(AttributeError):
        svc.store = store  # type: ignore[misc]  # the point: there is no setter
    assert svc.store is store
