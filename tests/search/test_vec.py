"""Plan 05 step 3: ``tern.search.vec`` (per-model ``chunks_vec``) and sqlite-vec on every Store.

Mail here is synthetic: inline ``example.test`` records, and the committed synthetic mailbox for
the plan 04 compatibility case. Expected counts come from the records the tests write or from
MANIFEST.json, never from a run.
"""

from __future__ import annotations

import math
import sqlite3
import struct
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm

from tern.bridge.protocol import FindParams
from tern.config import TernConfig
from tern.index import store as store_mod
from tern.index.events import EventIngest
from tern.index.ingest import Backfill
from tern.index.mid import mid_of
from tern.index.reconcile import Reconcile
from tern.index.source import IngestSource
from tern.index.store import LocationState, MessageRecord, Store, StoreError
from tern.search import vec
from tern.search.embedder import InvalidVector
from tern.testing.fake_bridge import FakeBridge
from tern.testing.stub_embedder import StubEmbedder

M1, M2, M3 = "a" * 64, "b" * 64, "c" * 64
H1, H2 = "1" * 64, "2" * 64
INBOX, ARCHIVE = "acct1:/INBOX", "acct1:/Archive"
S0 = LocationState()
T0 = 1_753_783_200

STUB = StubEmbedder(dim=64)
#: A stand-in for a real 1024-d model (Qwen3-Embedding-0.6B's dim), same code path.
FAKE_1024 = StubEmbedder(dim=1024, seed=7)


def rec(
    mid: str,
    h: str,
    body: str,
    *,
    subject: str | None = "synthetic subject",
    date_utc: int | None = T0,
) -> MessageRecord:
    return MessageRecord(
        mid=mid,
        content_hash=h,
        parse_status="ok",
        subject=subject,
        body_text=body,
        date_utc=date_utc,
    )


def add(store: Store, record: MessageRecord, folder: str = INBOX) -> None:
    with store.transaction():
        store.upsert_message(record)
        store.add_location(record.mid, folder, record.content_hash, S0, T0, source="backfill")


def one(conn: sqlite3.Connection, sql: str, *args: Any) -> Any:
    return conn.execute(sql, args).fetchone()[0]


def chunk_ids(store: Store, mid: str) -> list[int]:
    return [
        r[0]
        for r in store.connection.execute(
            "SELECT chunk_id FROM chunks WHERE mid = ? ORDER BY chunk_id", (mid,)
        )
    ]


def vec_rows(store: Store, ids: Sequence[int] | None = None) -> int:
    conn = store.connection
    if ids is None:
        return one(conn, "SELECT count(*) FROM chunks_vec")
    marks = ",".join("?" * len(ids))
    return one(conn, f"SELECT count(*) FROM chunks_vec WHERE chunk_id IN ({marks})", *ids)


def orphans(store: Store) -> int:
    """Vec rows whose chunk is gone."""
    return one(
        store.connection,
        "SELECT count(*) FROM chunks_vec v "
        "WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.chunk_id = v.chunk_id)",
    )


def embed_all(store: Store, emb: StubEmbedder) -> int:
    model = vec.current(store.connection)
    assert model is not None and model.model_id == emb.model_id
    total = 0
    while batch := vec.pending(store.connection, 100):
        n = vec.insert(store, model, [(p.chunk_id, p.text, emb.vector(p.text)) for p in batch])
        assert n == len(batch)
        total += n
    return total


def register(store: Store, emb: StubEmbedder) -> vec.ModelInfo:
    return vec.register(store, emb.model_id, emb.dim)


def status(store: Store) -> vec.VecStatus:
    return vec.status(store.connection, max_input_chars=6000)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "data" / "index.db")
    yield s
    s.close()


@pytest.fixture
def filled(store: Store) -> Store:
    """Three messages (two chunks each: subject + body), registered under the stub."""
    add(store, rec(M1, H1, "alpha beta gamma", date_utc=T0))
    add(store, rec(M2, H1, "delta epsilon zeta", date_utc=T0 + 100))
    add(store, rec(M3, H1, "eta theta iota", date_utc=None))
    register(store, STUB)
    return store


# ---- loading ---------------------------------------------------------------------------------


def test_every_store_connection_loads_the_pinned_sqlite_vec(tmp_path: Path) -> None:
    path = tmp_path / "data" / "index.db"
    with Store.open(path) as rw:
        assert one(rw.connection, "SELECT vec_version()") == "v0.1.9"
    with Store.open(path, readonly=True) as ro:
        assert one(ro.connection, "SELECT vec_version()") == "v0.1.9"
    assert store_mod.SQLITE_VEC_VERSION == "v0.1.9"


@pytest.mark.parametrize("readonly", [False, True])
def test_another_sqlite_vec_build_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, readonly: bool
) -> None:
    path = tmp_path / "data" / "index.db"
    Store.open(path).close()
    monkeypatch.setattr(store_mod, "SQLITE_VEC_VERSION", "v0.0.0")
    with pytest.raises(StoreError, match="sqlite-vec"):
        Store.open(path, readonly=readonly)


@pytest.mark.parametrize("readonly", [False, True])
def test_extension_loading_is_switched_off_after_the_load(tmp_path: Path, readonly: bool) -> None:
    path = tmp_path / "data" / "index.db"
    Store.open(path).close()
    with Store.open(path, readonly=readonly) as s:
        # With loading still enabled the error would be "cannot open shared object file".
        with pytest.raises(sqlite3.OperationalError, match="^not authorized$"):
            s.connection.execute("SELECT load_extension('/nonexistent/x')")


# ---- registration: the dim comes from the model ------------------------------------------------


@pytest.mark.parametrize("emb", [STUB, FAKE_1024], ids=["stub-64", "fake-1024"])
def test_dim_comes_from_the_registered_model(store: Store, emb: StubEmbedder) -> None:
    add(store, rec(M1, H1, "alpha beta"))
    model = register(store, emb)
    assert (model.model_id, model.dim, model.quant) == (emb.model_id, emb.dim, vec.QUANT)
    sql = one(store.connection, "SELECT sql FROM sqlite_master WHERE name = 'chunks_vec'")
    assert f"embedding int8[{emb.dim}]" in sql
    assert vec.current(store.connection) == model
    assert embed_all(store, emb) == 2
    st = status(store)
    assert (st.model, st.done, st.pending) == (model, 2, 0)
    with pytest.raises(ValueError, match="dim"):
        vec.insert(store, model, [(chunk_ids(store, M1)[0], "x", [1.0] * (emb.dim + 1))])


def test_no_model_registered(store: Store) -> None:
    add(store, rec(M1, H1, "alpha"))
    assert vec.current(store.connection) is None
    st = status(store)
    assert (st.model, st.done, st.pending, st.embed_complete_at) == (None, 0, 2, None)
    with pytest.raises(vec.VecStateError, match="no embedding model"):
        vec.pending(store.connection, 10)
    with pytest.raises(vec.VecStateError, match="no embedding model"):
        vec.knn(store.connection, [1.0] * 64, 5)


def test_registering_the_same_model_again_changes_nothing(filled: Store) -> None:
    embed_all(filled, STUB)
    vec.set_complete(filled, T0)
    before = vec.current(filled.connection)
    assert register(filled, STUB) == before
    assert one(filled.connection, "SELECT count(*) FROM embedding_model") == 1
    st = status(filled)
    assert (st.done, st.pending, st.embed_complete_at) == (6, 0, T0)


@pytest.mark.parametrize(
    "new", [StubEmbedder(dim=32), StubEmbedder(dim=64, seed=1)], ids=["new-dim", "new-model-id"]
)
def test_a_new_model_recreates_the_table_and_leaves_fts_alone(
    filled: Store, new: StubEmbedder
) -> None:
    conn = filled.connection
    embed_all(filled, STUB)
    vec.set_complete(filled, T0)
    fts_before = one(conn, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'epsilon'")
    chunks_before = one(conn, "SELECT count(*) FROM chunks")
    assert fts_before == 1 and status(filled).pending == 0

    model = register(filled, new)
    assert vec.current(conn) == model and model.dim == new.dim
    assert f"int8[{new.dim}]" in one(conn, "SELECT sql FROM sqlite_master WHERE name='chunks_vec'")
    st = status(filled)
    assert (st.done, st.pending, st.embed_complete_at) == (0, chunks_before, None)
    assert one(conn, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'epsilon'") == 1
    assert one(conn, "SELECT count(*) FROM chunks") == chunks_before
    # history kept; the current model is the last row
    assert [r[0] for r in conn.execute("SELECT model_id FROM embedding_model ORDER BY rowid")] == [
        STUB.model_id,
        new.model_id,
    ]
    assert embed_all(filled, new) == chunks_before


def test_insert_refuses_a_model_that_is_no_longer_current(filled: Store) -> None:
    old = vec.current(filled.connection)
    assert old is not None
    register(filled, StubEmbedder(dim=64, seed=1))
    (p,) = vec.pending(filled.connection, 1)
    with pytest.raises(vec.VecStateError, match="changed"):
        vec.insert(filled, old, [(p.chunk_id, p.text, STUB.vector(p.text))])
    assert vec_rows(filled) == 0


@pytest.mark.parametrize(
    "args",
    [("", 64, vec.QUANT), ("m", 0, vec.QUANT), ("m", 8193, vec.QUANT), ("m", 64, "other")],
    ids=["empty-id", "dim-0", "dim-too-big", "quant"],
)
def test_register_validates_its_arguments(store: Store, args: tuple[str, int, str]) -> None:
    with pytest.raises(ValueError):
        vec.register(store, *args)
    assert vec.current(store.connection) is None


def test_inconsistent_tables_raise_and_register_recreates_them(filled: Store) -> None:
    conn = filled.connection
    with filled.transaction():
        for trigger in vec.TRIGGERS:
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute("DROP TABLE chunks_vec")
    with pytest.raises(vec.VecStateError, match="without chunks_vec"):
        vec.current(conn)
    model = register(filled, STUB)
    assert vec.current(conn) == model
    assert status(filled).pending == 6


def test_a_missing_trigger_is_an_error(filled: Store) -> None:
    with filled.transaction():
        filled.connection.execute("DROP TRIGGER chunks_vec_au")
    with pytest.raises(vec.VecStateError, match="triggers"):
        vec.current(filled.connection)


def test_a_declared_dim_that_differs_from_the_row_is_an_error(filled: Store) -> None:
    with filled.transaction():
        filled.connection.execute("UPDATE embedding_model SET dim = 65")
    with pytest.raises(vec.VecStateError, match="dim differs"):
        vec.current(filled.connection)


# ---- review lows for steps 3-4: the remaining VecStateError branches ---------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE embedding_model SET dim = 'sixty-four'",
        "UPDATE embedding_model SET created_at = NULL",
        "UPDATE embedding_model SET model_id = x'6d'",  # TEXT affinity would coerce an int
        "UPDATE embedding_model SET quant = x'00'",
    ],
    ids=["dim-text", "created-null", "model-id-blob", "quant-blob"],
)
def test_a_model_row_with_unexpected_types_is_an_error(filled: Store, sql: str) -> None:
    with filled.transaction():
        filled.connection.execute(sql)
    with pytest.raises(vec.VecStateError, match="unexpected types"):
        vec.current(filled.connection)


def test_a_chunks_vec_with_another_definition_is_an_error(filled: Store) -> None:
    conn = filled.connection
    with filled.transaction():
        for trigger in vec.TRIGGERS:
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute("DROP TABLE chunks_vec")
        conn.execute("CREATE TABLE chunks_vec(chunk_id INTEGER PRIMARY KEY, embedding BLOB)")
    with pytest.raises(vec.VecStateError, match="unexpected definition"):
        vec.current(conn)


@pytest.mark.parametrize("keep", ["table-and-triggers", "triggers-only"])
def test_vector_tables_without_a_model_row_are_an_error(filled: Store, keep: str) -> None:
    conn = filled.connection
    with filled.transaction():
        conn.execute("DELETE FROM embedding_model")
        if keep == "triggers-only":
            conn.execute("DROP TABLE chunks_vec")  # the triggers stay
    assert (
        one(
            conn,
            "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' AND name "
            "LIKE 'chunks_vec_%'",
        )
        == 2
    )
    with pytest.raises(vec.VecStateError, match="without an embedding_model row"):
        vec.current(conn)


def test_an_unsupported_quantisation_is_an_error(filled: Store) -> None:
    with filled.transaction():
        filled.connection.execute("UPDATE embedding_model SET quant = 'float32'")
    with pytest.raises(vec.VecStateError, match="unsupported quantisation"):
        vec.current(filled.connection)


def test_a_chunk_without_text_cannot_be_pending(filled: Store) -> None:
    (first, _) = chunk_ids(filled, M2)
    with filled.transaction():
        filled.connection.execute("UPDATE chunks SET text = NULL WHERE chunk_id = ?", (first,))
    with pytest.raises(vec.VecStateError, match="no text"):
        vec.pending(filled.connection, 10)


@pytest.mark.parametrize("value", ["soon", "-5", "", "1.5", "\u00b2", "\u0661\u0662"])
def test_a_completion_time_that_is_not_a_timestamp_is_an_error(filled: Store, value: str) -> None:
    with filled.transaction():
        filled.connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)", (vec.COMPLETE_KEY, value)
        )
    with pytest.raises(vec.VecStateError, match="not a timestamp"):
        status(filled)


def test_set_complete_and_insert_need_a_registered_model(store: Store) -> None:
    add(store, rec(M1, H1, "alpha"))
    with pytest.raises(vec.VecStateError, match="no embedding model"):
        vec.set_complete(store, T0)
    assert one(store.connection, "SELECT count(*) FROM meta WHERE key = ?", vec.COMPLETE_KEY) == 0
    # a ModelInfo for a model that was never registered here
    ghost = vec.ModelInfo(STUB.model_id, STUB.dim, vec.QUANT, T0)
    cid = chunk_ids(store, M1)[0]
    with pytest.raises(vec.VecStateError, match="no embedding model"):
        vec.insert(store, ghost, [(cid, "synthetic subject", STUB.vector("x"))])


# ---- vectors are checked before any SQL ----------------------------------------------------------


@pytest.mark.parametrize(
    ("vector", "error"),
    [
        ([1.0] * 63, ValueError),
        ([math.nan] + [1.0] * 63, InvalidVector),
        ([math.inf] + [1.0] * 63, InvalidVector),
        ([0.0] * 64, InvalidVector),
    ],
    ids=["wrong-dim", "nan", "inf", "zero"],
)
def test_bad_vectors_raise_before_sql(
    filled: Store, vector: list[float], error: type[Exception]
) -> None:
    model = vec.current(filled.connection)
    assert model is not None
    (p,) = vec.pending(filled.connection, 1)
    statements: list[str] = []
    filled.connection.set_trace_callback(statements.append)
    try:
        with pytest.raises(error):
            vec.insert(filled, model, [(p.chunk_id, p.text, STUB.vector(p.text)), (0, "", vector)])
        with pytest.raises(error):
            vec.knn(filled.connection, vector, 5)
    finally:
        filled.connection.set_trace_callback(None)
    # knn reads the model before checking the vector; nothing touched chunks_vec or wrote
    assert not [s for s in statements if "chunks_vec" in s and "sqlite_master" not in s]
    assert not [s for s in statements if s.startswith(("BEGIN", "INSERT"))]
    assert vec_rows(filled) == 0


def test_insert_rejects_duplicate_chunk_ids(filled: Store) -> None:
    model = vec.current(filled.connection)
    assert model is not None
    (p,) = vec.pending(filled.connection, 1)
    item = (p.chunk_id, p.text, STUB.vector(p.text))
    with pytest.raises(ValueError, match="duplicate"):
        vec.insert(filled, model, [item, item])
    assert vec.insert(filled, model, [item]) == 1
    assert vec.insert(filled, model, [item]) == 0  # already has a vector: skipped, never replaced
    assert vec.insert(filled, model, []) == 0


def test_stored_vectors_are_normalised_then_quantised(filled: Store) -> None:
    model = vec.current(filled.connection)
    assert model is not None
    (p,) = vec.pending(filled.connection, 1)
    vector = [0.0] * 64
    vector[3], vector[5] = 3.0, -4.0  # norm 5: unnormalised, 'unit' would clamp both
    assert vec.insert(filled, model, [(p.chunk_id, p.text, vector)]) == 1
    unit = [0.0] * 64
    unit[3], unit[5] = 0.6, -0.8
    stored = one(filled.connection, "SELECT vec_to_json(embedding) FROM chunks_vec")
    expected = one(
        filled.connection,
        "SELECT vec_to_json(vec_quantize_int8(?, 'unit'))",
        struct.pack("<64f", *unit),
    )
    assert stored == expected
    values = [int(x) for x in stored.strip("[]").split(",")]
    assert values[3] not in (127, -128) and values[5] not in (127, -128)


# ---- vec rows follow chunks ----------------------------------------------------------------------


def test_a_replace_with_changed_chunks_drops_the_old_vectors(filled: Store) -> None:
    embed_all(filled, STUB)
    old = chunk_ids(filled, M1)
    assert vec_rows(filled, old) == 2
    with filled.transaction():
        assert filled.upsert_message(rec(M1, H1, "a different body")) == "replaced"
    assert vec_rows(filled, old) == 0 and orphans(filled) == 0
    st = status(filled)
    assert (st.done, st.pending) == (4, 2)  # M1's new chunks are pending, M2/M3 keep theirs
    assert {p.mid for p in vec.pending(filled.connection, 10)} == {M1}


def test_a_replace_with_identical_chunks_keeps_chunk_ids_and_vectors(filled: Store) -> None:
    embed_all(filled, STUB)

    def m1_vectors() -> list[tuple[int, str]]:
        return [
            (r[0], r[1])
            for r in filled.connection.execute(
                "SELECT v.chunk_id, vec_to_json(v.embedding) FROM chunks_vec v "
                "JOIN chunks c ON c.chunk_id = v.chunk_id WHERE c.mid = ? ORDER BY 1",
                (M1,),
            )
        ]

    ids, before = chunk_ids(filled, M1), m1_vectors()
    assert [cid for cid, _ in before] == ids and len(ids) == 2
    with filled.transaction():  # a re-parse with another date, same subject and body
        assert filled.upsert_message(rec(M1, H1, "alpha beta gamma", date_utc=T0 + 1)) == (
            "replaced"
        )
    assert chunk_ids(filled, M1) == ids
    assert m1_vectors() == before
    st = status(filled)
    assert (st.done, st.pending) == (6, 0)


def test_removing_the_last_location_drops_the_vectors(filled: Store) -> None:
    embed_all(filled, STUB)
    old = chunk_ids(filled, M1)
    filled.remove_location(M1, INBOX, source="event")
    assert filled.get_message(M1) is None
    assert vec_rows(filled, old) == 0 and orphans(filled) == 0
    assert (status(filled).done, status(filled).pending) == (4, 0)


def test_a_content_clear_drops_the_vectors(filled: Store) -> None:
    filled.add_location(M1, ARCHIVE, H2, S0, T0, source="backfill")  # a differing copy
    embed_all(filled, STUB)
    old = chunk_ids(filled, M1)
    filled.remove_location(M1, INBOX, source="event")  # no refill: H1 no longer held → clear
    row = filled.get_message(M1)
    assert row is not None and row.content_hash is None
    assert chunk_ids(filled, M1) == []
    assert vec_rows(filled, old) == 0 and orphans(filled) == 0


def test_updating_a_chunk_text_drops_its_vector(filled: Store) -> None:
    embed_all(filled, STUB)
    first, second = chunk_ids(filled, M1)
    with filled.transaction():
        filled.connection.execute("UPDATE chunks SET text = 'changed' WHERE chunk_id = ?", (first,))
    assert vec_rows(filled, [first]) == 0 and vec_rows(filled, [second]) == 1
    assert [p.chunk_id for p in vec.pending(filled.connection, 10)] == [first]


def test_a_reused_chunk_id_never_inherits_an_old_vector(store: Store) -> None:
    add(store, rec(M1, H1, "alpha beta", subject="first subject"))
    register(store, STUB)
    model = vec.current(store.connection)
    assert model is not None
    # read and "embedded" now, written only after the id has been reused
    stale_items = [
        (p.chunk_id, p.text, STUB.vector(p.text)) for p in vec.pending(store.connection, 10)
    ]
    embed_all(store, STUB)
    old_ids = chunk_ids(store, M1)
    store.remove_location(M1, INBOX, source="event")
    add(store, rec(M2, H1, "omega psi", subject="second subject"))
    new_ids = chunk_ids(store, M2)
    assert new_ids == old_ids  # SQLite reused the freed max rowids (no AUTOINCREMENT)
    assert vec_rows(store, new_ids) == 0
    assert [p.chunk_id for p in vec.pending(store.connection, 10)] == new_ids
    # a vector computed for the old chunk's text, written after the id was reused: skipped
    assert vec.insert(store, model, stale_items) == 0
    assert vec_rows(store) == 0


def test_a_chunk_replaced_while_embedding_is_skipped(filled: Store) -> None:
    model = vec.current(filled.connection)
    assert model is not None
    batch = vec.pending(filled.connection, 10)
    vectors = [STUB.vector(p.text) for p in batch]  # "the embed call"
    with filled.transaction():
        filled.upsert_message(rec(M1, H1, "replaced meanwhile"))
    items = [(p.chunk_id, p.text, v) for p, v in zip(batch, vectors, strict=True)]
    written = vec.insert(filled, model, items)
    assert written == 4  # M2's and M3's chunks; M1's are gone or carry other text
    assert orphans(filled) == 0
    assert {p.mid for p in vec.pending(filled.connection, 10)} == {M1}


# ---- pending, status, completion ---------------------------------------------------------------


def test_pending_is_newest_first_then_by_chunk_id(filled: Store) -> None:
    got = [(p.mid, p.chunk_id) for p in vec.pending(filled.connection, 10)]
    expected = [(M2, i) for i in chunk_ids(filled, M2)]
    expected += [(M1, i) for i in chunk_ids(filled, M1)]
    expected += [(M3, i) for i in chunk_ids(filled, M3)]  # undated last
    assert got == expected
    assert [p.chunk_id for p in vec.pending(filled.connection, 3)] == [e[1] for e in expected[:3]]
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            vec.pending(filled.connection, bad)


def test_still_pending_drops_chunks_changed_since_the_scan(filled: Store) -> None:
    """Merge review (pending window): a chunk embedded, deleted or replaced since a
    ``vec.pending`` scan is dropped; the others keep their order."""
    conn = filled.connection
    window = vec.pending(conn, 10)
    assert vec.still_pending(conn, window) == window
    model = vec.current(conn)
    assert model is not None
    m2 = [p for p in window if p.mid == M2]
    assert vec.insert(filled, model, [(m2[0].chunk_id, m2[0].text, STUB.vector(m2[0].text))]) == 1
    with filled.transaction():  # M1's body changes: its old body chunk is gone
        assert filled.upsert_message(rec(M1, H1, "replaced body", date_utc=T0)) == "replaced"
    m1_body = [p for p in window if p.mid == M1 and p.text == "alpha beta gamma"]
    assert len(m1_body) == 1
    expected = [p for p in window if p != m2[0] and p != m1_body[0]]
    current = {(r[0], r[1], r[2]) for r in conn.execute("SELECT chunk_id, mid, text FROM chunks")}
    # an M1 subject chunk whose (id, mid, text) is unchanged is still the same pending work
    expected = [p for p in expected if (p.chunk_id, p.mid, p.text) in current]
    assert vec.still_pending(conn, window) == expected
    got = vec.still_pending(conn, window)
    assert m2[0] not in got and m1_body[0] not in got
    assert m2[1] in got and [p for p in window if p.mid == M3] == [p for p in got if p.mid == M3]
    assert vec.still_pending(conn, []) == []


def test_status_counts_truncated_chunks_and_completion(filled: Store) -> None:
    add(filled, rec("d" * 64, H1, "x" * 50))
    conn = filled.connection
    assert vec.status(conn, max_input_chars=50).truncated == 0
    assert vec.status(conn, max_input_chars=49).truncated == 1
    # > 17 chars: "delta epsilon zeta" (18) and the new body (50); subjects are 17
    assert vec.status(conn, max_input_chars=17).truncated == 2
    embed_all(filled, STUB)
    assert status(filled).embed_complete_at is None
    vec.set_complete(filled, T0)
    assert status(filled).embed_complete_at == T0
    register(filled, StubEmbedder(dim=16))
    assert status(filled).embed_complete_at is None


# ---- knn -------------------------------------------------------------------------------------


def test_a_read_only_store_runs_knn(filled: Store) -> None:
    embed_all(filled, STUB)
    path = filled.path
    query = STUB.vector("delta epsilon zeta")
    expected = chunk_ids(filled, M2)[1]  # M2's body chunk
    with Store.open(path, readonly=True) as ro:
        hits = vec.knn(ro.connection, query, 3)
        assert hits[0] == (expected, 0.0)
        assert len(hits) == 3
        assert [h[1] for h in hits] == sorted(h[1] for h in hits)
        st = vec.status(ro.connection, max_input_chars=6000)
        assert (st.done, st.pending) == (6, 0)


def test_knn_pre_filter_fills_k(filled: Store) -> None:
    embed_all(filled, STUB)
    query = STUB.vector("delta epsilon zeta")
    hits = vec.knn(filled.connection, query, 2, "SELECT chunk_id FROM chunks WHERE mid = ?", (M3,))
    assert sorted(h[0] for h in hits) == chunk_ids(filled, M3)
    with pytest.raises(ValueError, match="params"):
        vec.knn(filled.connection, query, 2, None, (M3,))


@pytest.mark.parametrize("k", [0, 4097, True, 2.0])
def test_knn_validates_k(filled: Store, k: Any) -> None:
    with pytest.raises(ValueError, match="k must"):
        vec.knn(filled.connection, STUB.vector("x"), k)


# ---- plan 04's ingest and reconcile on an index with chunks_vec ----------------------------------


FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
MESSAGES = tfm.messages(MANIFEST)


def _single_copy_keys(folder: str) -> list[str]:
    return [
        m["key"]
        for m in MANIFEST["messages"]
        if m["mid"] and [c["folder"] for c in m["copies"]] == [folder]
    ]


async def _tb_id(fb: FakeBridge, key: str, folder_id: str) -> int:
    hmid = MESSAGES[key]["message_id"].strip("<>")
    found = await fb.request("mail.messages.find", FindParams(header_message_id=hmid))
    (tb_id,) = [f.tb_id for f in found.root if f.folder_id == folder_id]
    return tb_id


@pytest.mark.asyncio
async def test_backfill_and_reconcile_work_with_chunks_vec(store: Store) -> None:
    """chunks_vec and its triggers exist before the first message; vectors follow every delete
    the ingest and reconcile paths make."""
    register(store, STUB)
    fb = FakeBridge(FIXTURE)
    await Backfill(store, IngestSource(fb), TernConfig()).run()
    st = store.status()
    expected = (MANIFEST["distinct_messages"], MANIFEST["message_count"])
    assert (st.messages, st.locations) == expected
    n_chunks = one(store.connection, "SELECT count(*) FROM chunks")
    assert embed_all(store, STUB) == n_chunks

    events = EventIngest(store, IngestSource(fb), TernConfig())
    rec_ = Reconcile(store, IngestSource(fb), TernConfig(), events=events)
    fb.on_event = events.on_event
    deleted, *_ = _single_copy_keys("acct1/Newsletters")
    deleted_mid = MESSAGES[deleted]["mid"]
    deleted_chunks = chunk_ids(store, deleted_mid)
    assert deleted_chunks and vec_rows(store, deleted_chunks) == len(deleted_chunks)
    fb.drop_events(2)  # reconcile, not the event path, has to find both changes
    await fb.delete([await _tb_id(fb, deleted, "acct1:/Newsletters")])
    raw = (
        b"Message-ID: <vec-compat@example.test>\nDate: Fri, 13 Nov 2026 10:00:00 +0000\n"
        b"From: a@example.test\nTo: b@example.test\nSubject: vec compat\n\nbody words\n"
    )
    await fb.deliver("acct1:/Newsletters", raw)
    report = await rec_.hourly()
    assert (report.added, report.removed) == (1, 1)

    assert store.get_message(deleted_mid) is None
    assert vec_rows(store, deleted_chunks) == 0 and orphans(store) == 0
    new_mid = mid_of("vec-compat@example.test")
    assert new_mid is not None
    assert {p.mid for p in vec.pending(store.connection, 100)} == {new_mid}
    st = store.status()
    assert (st.messages, st.locations) == expected
    assert embed_all(store, STUB) == len(chunk_ids(store, new_mid))
    assert status(store).pending == 0
