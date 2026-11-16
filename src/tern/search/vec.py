"""The per-model vector table ``chunks_vec`` (sqlite-vec int8; M9, decisions #17, plan 05 step 3).

- One vec0 table, ``chunks_vec(chunk_id INTEGER PRIMARY KEY, embedding int8[<dim>])``, with the
  dim of the registered embedding model (never a constant: the 64-d stub and a 1024-d model use
  the same code). The metric is vec0's default, L2; with unit vectors quantised alike, L2 order
  matches cosine order up to the quantisation error.
- The current model is the ``embedding_model`` row with the largest rowid; older rows stay as
  history. :func:`register` with a different (model_id, dim, quant) drops the table and its
  triggers and recreates them, in one transaction, so every chunk is pending again (the FTS rows
  are untouched) and ``meta.embed_complete_at`` is cleared.
- Vectors follow chunks inside the writing transaction: per-model triggers ``chunks_vec_ad``
  (``AFTER DELETE ON chunks``) and ``chunks_vec_au`` (``AFTER UPDATE OF chunk_id, text ON
  chunks``) delete the chunk's vec row, so all of the Store's chunk-delete paths (a replace
  with changed chunks, the last location removed, a content clear) drop vectors without the
  Store knowing about them. This is why every ``Store`` connection loads sqlite-vec.
- ``chunks.chunk_id`` can be reused after a delete, and the embedder is awaited between reading
  a chunk and writing its vector, so :func:`insert` re-checks inside its transaction that the
  chunk still has that id *and* that text, and skips it otherwise (counted, not written). It
  never UPDATEs a vec row (an int8 UPDATE fails in sqlite-vec 0.1.9) and never replaces one.
- Quantisation (:data:`QUANT`, recorded in ``embedding_model.quant``): Python checks each
  vector (dim, finite, nonzero norm) and L2-normalises it, then ``vec_quantize_int8(v, 'unit')``
  maps [-1, 1] to int8. A NaN would silently become 127 in SQL, so nothing unchecked reaches it.

All writes go through ``Store.transaction()`` on the caller's Store (the ingest's connection in
ternd), so no second writer connection exists. Logs carry counts only.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass

from tern.index.store import Store
from tern.search.embedder import l2_normalize

log = logging.getLogger(__name__)

#: The quantisation method, recorded in ``embedding_model.quant`` (#17, B-L8).
QUANT = "l2norm+vec_quantize_int8:unit"
#: sqlite-vec's ``k`` limit for a KNN query.
MAX_K = 4096
#: vec0's largest vector dimension (SQLITE_VEC_VEC0_MAX_DIMENSIONS in 0.1.9).
MAX_DIM = 8192

TABLE = "chunks_vec"
TRIGGERS = ("chunks_vec_ad", "chunks_vec_au")
COMPLETE_KEY = "embed_complete_at"

_DECLARED_DIM = re.compile(r"\bembedding\s+int8\[(\d+)\]")


class VecStateError(RuntimeError):
    """The vector tables are not in the state the code needs (missing, inconsistent, or the
    model changed under the caller)."""


@dataclass(frozen=True)
class ModelInfo:
    """One ``embedding_model`` row."""

    model_id: str
    dim: int
    quant: str
    created_at: int

    @property
    def key(self) -> tuple[str, int, str]:
        """What identifies the vector space: vectors are interchangeable only under equal keys."""
        return (self.model_id, self.dim, self.quant)


@dataclass(frozen=True)
class PendingChunk:
    """A chunk without a vector, as :func:`pending` returns it."""

    chunk_id: int
    mid: str
    text: str


@dataclass(frozen=True)
class VecStatus:
    """Embedding progress, derived from the tables (nothing here is a stored counter)."""

    model: ModelInfo | None
    #: Chunks with a vector.
    done: int
    #: Chunks without one (all chunks when no model is registered).
    pending: int
    #: Chunks longer than the embed input cap (their vector is of a truncated text).
    truncated: int
    #: When the queue first saw no pending chunk under the current model (unix seconds).
    embed_complete_at: int | None


def _check_dim(dim: int) -> None:
    if isinstance(dim, bool) or not isinstance(dim, int) or not 1 <= dim <= MAX_DIM:
        raise ValueError(f"dim must be an int in 1..{MAX_DIM}")


def _check_model_id(model_id: str) -> None:
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id must be a non-empty string")


def _latest(conn: sqlite3.Connection) -> ModelInfo | None:
    row = conn.execute(
        "SELECT model_id, dim, quant, created_at FROM embedding_model ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    model_id, dim, quant, created_at = row
    if not (
        isinstance(model_id, str)
        and isinstance(dim, int)
        and isinstance(quant, str)
        and isinstance(created_at, int)
    ):
        raise VecStateError("embedding_model row has unexpected types")
    return ModelInfo(model_id, dim, quant, created_at)


def _declared_dim(conn: sqlite3.Connection) -> int | None:
    """The dim in ``chunks_vec``'s CREATE statement, or None when the table does not exist."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    if row is None:
        return None
    m = _DECLARED_DIM.search(row[0] or "")
    if m is None:
        raise VecStateError("chunks_vec has an unexpected definition")
    return int(m.group(1))


def _triggers(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name IN (?, ?)", TRIGGERS
        )
    }


def current(conn: sqlite3.Connection) -> ModelInfo | None:
    """The registered model, or None when none is. Raises VecStateError when the tables
    disagree: a model row without ``chunks_vec`` (or the reverse), a declared dim that differs
    from the row's, a missing trigger, or a quantisation this code does not implement."""
    latest = _latest(conn)
    declared = _declared_dim(conn)
    triggers = _triggers(conn)
    if latest is None:
        if declared is not None or triggers:
            raise VecStateError("chunks_vec exists without an embedding_model row")
        return None
    if declared is None:
        raise VecStateError("embedding_model row without chunks_vec")
    if declared != latest.dim:
        raise VecStateError("chunks_vec dim differs from the registered model's")
    if triggers != set(TRIGGERS):
        raise VecStateError("chunks_vec triggers are missing")
    if latest.quant != QUANT:
        raise VecStateError("embedding_model has an unsupported quantisation")
    return latest


def _require_current(conn: sqlite3.Connection) -> ModelInfo:
    model = current(conn)
    if model is None:
        raise VecStateError("no embedding model registered")
    return model


def register(store: Store, model_id: str, dim: int, quant: str = QUANT) -> ModelInfo:
    """Make (model_id, dim, quant) the current model. Equal to the current row (and the tables
    consistent with it): nothing happens. Otherwise, in one transaction: drop both triggers and
    ``chunks_vec``, create them for ``dim``, insert an ``embedding_model`` row and clear
    ``meta.embed_complete_at``; every chunk is then pending. Tables left inconsistent (see
    :func:`current`) are recreated the same way, never patched."""
    _check_model_id(model_id)
    _check_dim(dim)
    if quant != QUANT:
        raise ValueError("unsupported quantisation")
    conn = store.connection
    with store.transaction():
        latest = _latest(conn)
        if latest is not None and latest.key == (model_id, dim, quant):
            try:
                if current(conn) == latest:
                    return latest
            except VecStateError:
                log.warning("vector tables inconsistent with the registered model: recreating")
        previous_dim = None if latest is None else latest.dim
        for trigger in TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")  # before the table they reference
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        # dim is a checked int; L2 (vec0's default metric) is the distance.
        conn.execute(
            f"CREATE VIRTUAL TABLE {TABLE} USING "
            f"vec0(chunk_id INTEGER PRIMARY KEY, embedding int8[{dim}])"
        )
        conn.execute(
            f"CREATE TRIGGER {TRIGGERS[0]} AFTER DELETE ON chunks BEGIN "
            f"DELETE FROM {TABLE} WHERE chunk_id = old.chunk_id; END"
        )
        conn.execute(
            f"CREATE TRIGGER {TRIGGERS[1]} AFTER UPDATE OF chunk_id, text ON chunks BEGIN "
            f"DELETE FROM {TABLE} WHERE chunk_id = old.chunk_id; END"
        )
        created_at = int(time.time())
        conn.execute(
            "INSERT INTO embedding_model(model_id, dim, quant, created_at) VALUES (?, ?, ?, ?)",
            (model_id, dim, quant, created_at),
        )
        conn.execute("DELETE FROM meta WHERE key = ?", (COMPLETE_KEY,))
        chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    log.info(
        "embedding model registered",
        extra={"dim": dim, "previous_dim": previous_dim, "chunks_pending": chunks},
    )
    return ModelInfo(model_id, dim, quant, created_at)


def _blob(vector: Sequence[float], dim: int) -> bytes:
    """``vector`` checked (length, finite, nonzero norm), L2-normalised, as a float32 blob."""
    if isinstance(vector, str | bytes) or len(vector) != dim:
        raise ValueError("vector length differs from the model's dim")
    return struct.pack(f"<{dim}f", *l2_normalize(vector))


def insert(
    store: Store, model: ModelInfo, items: Sequence[tuple[int, str, Sequence[float]]]
) -> int:
    """Write the vectors of ``items`` (chunk_id, the chunk text that was embedded, vector) and
    return how many were written. Every vector is checked before any SQL (wrong length, NaN or
    infinite components, zero norm raise ValueError / ``InvalidVector``). Inside one
    transaction, ``model`` must still be the current model (VecStateError otherwise; "no
    embedding model registered" when none is), and an item is skipped when no chunk has that id
    with that text any more (replaced, or the id reused) or the chunk already has a vector."""
    seen: set[int] = set()
    rows: list[tuple[int, str, bytes]] = []
    for chunk_id, text, vector in items:
        if isinstance(chunk_id, bool) or not isinstance(chunk_id, int):
            raise ValueError("chunk_id must be an int")
        if not isinstance(text, str):
            raise ValueError("text must be a str")
        if chunk_id in seen:
            raise ValueError("duplicate chunk_id in one insert")
        seen.add(chunk_id)
        rows.append((chunk_id, text, _blob(vector, model.dim)))
    if not rows:
        return 0
    conn = store.connection
    written = 0
    with store.transaction():
        if _require_current(conn).key != model.key:
            raise VecStateError("the embedding model changed; vectors not written")
        for chunk_id, text, blob in rows:
            # Two checks then a VALUES insert: in INSERT … SELECT … WHERE, vec0 loses the int8
            # subtype of vec_quantize_int8's result (probed, 0.1.9). One transaction, no await.
            if (
                conn.execute(
                    "SELECT 1 FROM chunks WHERE chunk_id = ? AND text = ?", (chunk_id, text)
                ).fetchone()
                is None
            ):
                continue
            if (
                conn.execute(f"SELECT 1 FROM {TABLE} WHERE chunk_id = ?", (chunk_id,)).fetchone()
                is not None
            ):
                continue
            conn.execute(
                f"INSERT INTO {TABLE}(chunk_id, embedding) "
                "VALUES (?, vec_quantize_int8(?, 'unit'))",
                (chunk_id, blob),
            )
            written += 1
    log.debug("vectors written=%d skipped=%d", written, len(rows) - written)
    return written


def pending(conn: sqlite3.Connection, limit: int) -> list[PendingChunk]:
    """Up to ``limit`` chunks without a vector, newest message first (``messages.date_utc``
    descending, undated last), then by chunk_id. Requires a registered model."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive int")
    _require_current(conn)
    rows = conn.execute(
        f"SELECT c.chunk_id, c.mid, c.text FROM chunks c LEFT JOIN messages m ON m.mid = c.mid "
        f"WHERE NOT EXISTS (SELECT 1 FROM {TABLE} v WHERE v.chunk_id = c.chunk_id) "
        "ORDER BY m.date_utc DESC NULLS LAST, c.chunk_id LIMIT ?",
        (limit,),
    ).fetchall()
    out: list[PendingChunk] = []
    for chunk_id, mid, text in rows:
        if not isinstance(text, str):
            raise VecStateError("a chunk has no text")
        out.append(PendingChunk(chunk_id, mid, text))
    return out


def still_pending(conn: sqlite3.Connection, chunks: Sequence[PendingChunk]) -> list[PendingChunk]:
    """The chunks of ``chunks`` (order kept) that still exist with the same mid and text and
    have no vector. Requires a registered model. The embed queue reads a window of pending
    chunks with one :func:`pending` scan and re-checks each slice with this (indexed point
    lookups, no scan) before sending it to the embedder, so a chunk replaced, deleted or
    embedded since the scan is never sent."""
    _require_current(conn)
    out: list[PendingChunk] = []
    for chunk in chunks:
        row = conn.execute(
            f"SELECT c.mid, c.text FROM chunks c WHERE c.chunk_id = ? "
            f"AND NOT EXISTS (SELECT 1 FROM {TABLE} v WHERE v.chunk_id = c.chunk_id)",
            (chunk.chunk_id,),
        ).fetchone()
        if row is not None and row[0] == chunk.mid and row[1] == chunk.text:
            out.append(chunk)
    return out


def pending_count(conn: sqlite3.Connection) -> int:
    """How many chunks have no vector: every chunk when no model is registered, else
    ``count(chunks) - count(chunks_vec)``. That difference is exact because every vec row has
    its chunk (:func:`insert` writes only for an existing chunk with that text, and the
    triggers delete a vec row with its chunk) and it costs two counts instead of a per-chunk
    probe of vec0 (search reports it on every query). A negative difference means that
    invariant broke: VecStateError."""
    total = int(conn.execute("SELECT count(*) FROM chunks").fetchone()[0])
    if current(conn) is None:
        return total
    done = int(conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])
    if done > total:
        raise VecStateError("chunks_vec has more rows than chunks")
    return total - done


def set_complete(store: Store, at: int) -> None:
    """Record ``meta.embed_complete_at`` (the embed queue calls it when pending first reaches
    0 under the current model; :func:`register` clears it)."""
    if isinstance(at, bool) or not isinstance(at, int) or at < 0:
        raise ValueError("at must be a non-negative int")
    conn = store.connection
    with store.transaction():
        _require_current(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (COMPLETE_KEY, str(at)),
        )


def status(conn: sqlite3.Connection, *, max_input_chars: int) -> VecStatus:
    """Model, done/pending/truncated chunk counts and ``embed_complete_at``. ``truncated``
    counts chunks longer than ``max_input_chars`` (``TernConfig.embed_max_input_chars``)."""
    if isinstance(max_input_chars, bool) or not isinstance(max_input_chars, int):
        raise ValueError("max_input_chars must be an int")
    if max_input_chars < 1:
        raise ValueError("max_input_chars must be >= 1")
    model = current(conn)
    total, truncated = conn.execute(
        "SELECT count(*), count(CASE WHEN length(text) > ? THEN 1 END) FROM chunks",
        (max_input_chars,),
    ).fetchone()
    if model is None:
        n_pending = total
    else:
        n_pending = conn.execute(
            f"SELECT count(*) FROM chunks c "
            f"WHERE NOT EXISTS (SELECT 1 FROM {TABLE} v WHERE v.chunk_id = c.chunk_id)"
        ).fetchone()[0]
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (COMPLETE_KEY,)).fetchone()
    complete_at: int | None = None
    if row is not None:
        # isascii(): str.isdigit() also accepts '²' (int() then raises) and '١٢' (read as 12)
        if not isinstance(row[0], str) or not (row[0].isascii() and row[0].isdigit()):
            raise VecStateError("meta.embed_complete_at is not a timestamp")
        complete_at = int(row[0])
    return VecStatus(
        model=model,
        done=total - n_pending,
        pending=n_pending,
        truncated=truncated,
        embed_complete_at=complete_at,
    )


def knn(
    conn: sqlite3.Connection,
    qvec: Sequence[float],
    k: int,
    chunk_filter_sql: str | None = None,
    params: Sequence[object] = (),
) -> list[tuple[int, float]]:
    """The ``k`` nearest chunks to ``qvec`` as (chunk_id, distance), sorted by (distance,
    chunk_id). ``qvec`` is checked and normalised like a stored vector.

    ``chunk_filter_sql`` is a constant ``SELECT`` of chunk ids written by tern code (values go
    in ``params``, never into the SQL); it is applied as ``chunk_id IN (…)``, which vec0 uses
    as a pre-filter, so a filter still fills ``k`` when enough chunks match. Which of several
    chunks at an equal distance make the cut depends on vec0's physical layout; callers that
    need a deterministic cut over-fetch (``SearchService``)."""
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= MAX_K:
        raise ValueError(f"k must be an int in 1..{MAX_K}")
    model = _require_current(conn)
    blob = _blob(qvec, model.dim)
    sql = (
        f"SELECT chunk_id, distance FROM {TABLE} "
        "WHERE embedding MATCH vec_quantize_int8(?, 'unit') AND k = ?"
    )
    if chunk_filter_sql is not None:
        sql += f" AND chunk_id IN ({chunk_filter_sql})"
    elif params:
        raise ValueError("params given without chunk_filter_sql")
    rows = conn.execute(sql, (blob, k, *params)).fetchall()
    return sorted(((int(cid), float(dist)) for cid, dist in rows), key=lambda r: (r[1], r[0]))
