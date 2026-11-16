"""Hybrid search over the index: FTS5 BM25 + sqlite-vec KNN, fused with RRF over mids (plan 05
step 5, decisions #17).

``SearchService(store, embedder, mode=...)`` is built once and shared (plan 06a's
``ToolContext.search``, the CLI, the smoke adapter). Two modes, never switched silently:

- ``hybrid`` needs an embedder whose ``model_id`` is the index's registered model (checked on
  every search, since a re-registration can happen while the service lives): ``NoEmbeddingModel``
  when none is registered, ``ModelMismatch`` when another one is. Embedder failures
  (``EmbedderUnavailable``, ``EmbedderError``) propagate: there is no FTS-only fallback.
- ``fts`` is explicit; the embedder is never called, and ``SearchResult.mode`` says so.

One search:

1. the query becomes an FTS5 phrase disjunction (:func:`tern.search.fts.build_match`; no word
   character → ``EmptyQuery``); in hybrid mode the query is embedded (the only await);
2. all reads then run in one read transaction with no await inside (one snapshot, also against
   a concurrent writer on another connection);
3. filters (:class:`SearchFilters`) become one constant SQL ``SELECT c.chunk_id FROM chunks c
   JOIN messages m …`` with bound values, applied in both arms as ``chunk_id IN (…)``, a
   pre-filter (so a filter that excludes the global nearest neighbours still fills the arm);
4. each arm takes its top C = min(4096, max(100, 10·limit)) chunks. FTS: ``ORDER BY
   bm25(chunks_fts), chunk_id``. Vector: sorted by (distance, chunk_id); vec0 breaks distance
   ties by physical layout, so while the C-th distance equals the last fetched one and the
   fetch was full, k is doubled (cap 4096) until the whole tie group is in hand; the cut is
   then the same whatever order the vectors were inserted in;
5. each arm collapses to a mid ranking (a mid keeps its best chunk's rank), RRF k=60 fuses them,
   sorted by (−score, mid), and the top ``limit`` mids become :class:`Hit` s.

Hit fields keep NULLs as None (mapping onto bridge ``SearchHit`` is plan 06a/07's job). The
snippet is the mid's best-ranked chunk's original ``chunks.text`` (never FTS5 ``snippet()``,
which would return folded text), whitespace-collapsed, at most 200 characters from its start.
Conflicted mids (D1) come back with ``conflicted=True`` (never eligible as citations);
``include_conflicted=False`` drops them in both arms. Logs carry counts only.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from tern.index.chunks import fold
from tern.index.store import Store
from tern.search import vec
from tern.search.embedder import Embedder, EmbedderError, InvalidVector, l2_norm
from tern.search.fts import build_match
from tern.search.rrf import DEFAULT_K, collapse, rrf

log = logging.getLogger(__name__)

Mode = Literal["hybrid", "fts"]

MIN_LIMIT = 1
MAX_LIMIT = 50
#: Candidate chunks per arm: min(MAX_CANDIDATES, max(MIN_CANDIDATES, CANDIDATES_PER_HIT·limit)).
MIN_CANDIDATES = 100
CANDIDATES_PER_HIT = 10
MAX_CANDIDATES = vec.MAX_K
SNIPPET_CHARS = 200

#: SQL function (registered on the Store's connection) for the ``from_`` filter.
_FOLD_FN = "tern_fold_casefold"


class SearchError(RuntimeError):
    """Search cannot run in the requested mode."""


class NoEmbeddingModel(SearchError):
    """Hybrid search was asked for, but the index has no registered embedding model."""


class ModelMismatch(SearchError):
    """The embedder's model is not the index's registered model (vectors not comparable)."""


class SearchStateError(SearchError):
    """The index is not in a state search can read (e.g. a chunk without a message row)."""


class EmptyQuery(ValueError):
    """The query has no word character, so there is nothing to search for."""


def _fold_casefold(text: str | None) -> str | None:
    return None if text is None else fold(text).casefold()


def _epoch(value: datetime) -> int:
    """``value`` as unix seconds rounded up (naive = UTC). With integer ``date_utc``,
    ``date_utc >= ceil(t)`` ⇔ ``date_utc >= t`` and ``date_utc < ceil(t)`` ⇔ ``date_utc < t``."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return math.ceil(value.timestamp())


@dataclass(frozen=True)
class SearchFilters:
    """Restrictions on the hits, mirroring bridge 1.0 ``SearchFilters`` plus
    ``include_conflicted``. None means no restriction.

    - ``folder_id``: the mid has a location in this folder;
    - ``from_``: a substring of ``from_name`` or ``from_addr``, both sides folded and casefolded;
    - ``after`` (inclusive) / ``before`` (exclusive) on ``date_utc``; naive datetimes are UTC; a
      NULL date fails any date filter;
    - ``has_attachment``: ``messages.has_attachments`` is true / false (NULL fails both);
    - ``include_conflicted``: False drops conflicted mids.
    """

    folder_id: str | None = None
    from_: str | None = None
    after: datetime | None = None
    before: datetime | None = None
    has_attachment: bool | None = None
    include_conflicted: bool = True

    def __post_init__(self) -> None:
        if self.folder_id is not None and not (isinstance(self.folder_id, str) and self.folder_id):
            raise ValueError("folder_id must be a non-empty str")
        if self.from_ is not None:
            if not isinstance(self.from_, str):
                raise ValueError("from_ must be a str")
            if not self.from_needle():
                raise ValueError("from_ must not be blank after folding")
        for name in ("after", "before"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, datetime):
                raise ValueError(f"{name} must be a datetime")
        if self.has_attachment is not None and not isinstance(self.has_attachment, bool):
            raise ValueError("has_attachment must be a bool")
        if not isinstance(self.include_conflicted, bool):
            raise ValueError("include_conflicted must be a bool")

    def from_needle(self) -> str | None:
        """``from_`` folded and casefolded, then stripped (folding can leave only spaces, e.g.
        combining marks around a space), as the ``from_`` filter matches it."""
        folded = _fold_casefold(self.from_)
        return None if folded is None else folded.strip()

    def chunk_filter(self) -> tuple[str | None, tuple[object, ...]]:
        """The pre-filter as (constant SQL selecting chunk ids, bound values), or (None, ())
        when nothing is restricted."""
        conds: list[str] = []
        params: list[object] = []
        if self.folder_id is not None:
            conds.append(
                "EXISTS (SELECT 1 FROM locations l WHERE l.mid = m.mid AND l.folder_id = ?)"
            )
            params.append(self.folder_id)
        if self.from_ is not None:
            needle = self.from_needle()
            conds.append(
                f"(instr({_FOLD_FN}(m.from_name), ?) > 0 OR instr({_FOLD_FN}(m.from_addr), ?) > 0)"
            )
            params.extend((needle, needle))
        if self.after is not None:
            conds.append("m.date_utc >= ?")
            params.append(_epoch(self.after))
        if self.before is not None:
            conds.append("m.date_utc < ?")
            params.append(_epoch(self.before))
        if self.has_attachment is not None:
            conds.append("m.has_attachments = ?")
            params.append(1 if self.has_attachment else 0)
        if not self.include_conflicted:
            conds.append("m.conflicted = 0")
        if not conds:
            return None, ()
        sql = "SELECT c.chunk_id FROM chunks c JOIN messages m ON m.mid = c.mid WHERE " + (
            " AND ".join(conds)
        )
        return sql, tuple(params)


#: No restriction (the default; ``SearchFilters`` is frozen, so one shared instance is safe).
NO_FILTERS = SearchFilters()


@dataclass(frozen=True)
class Hit:
    """One message in a search result. NULL columns stay None."""

    mid: str
    date: datetime | None
    from_addr: str | None
    from_name: str | None
    subject: str | None
    snippet: str
    score: float
    folder_ids: tuple[str, ...]
    conflicted: bool


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[Hit, ...]
    mode: Mode
    #: The registered model the vector arm used (None in ``fts`` mode).
    model_id: str | None
    #: Chunks without a vector under the current model (all chunks when none is registered):
    #: vector recall is partial while this is > 0.
    embed_pending: int


def candidates(limit: int) -> int:
    """C, the chunks each arm contributes for ``limit`` hits."""
    return min(MAX_CANDIDATES, max(MIN_CANDIDATES, CANDIDATES_PER_HIT * limit))


def snippet(text: str) -> str:
    """``text`` whitespace-collapsed, at most :data:`SNIPPET_CHARS` characters from its start."""
    return " ".join(text.split())[:SNIPPET_CHARS]


def _check_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not MIN_LIMIT <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an int in {MIN_LIMIT}..{MAX_LIMIT}")


class SearchService:
    """Hybrid or explicit FTS-only search over one Store (module docstring)."""

    def __init__(self, store: Store, embedder: Embedder | None, *, mode: Mode) -> None:
        if mode not in ("hybrid", "fts"):
            raise ValueError("mode must be 'hybrid' or 'fts'")
        if mode == "hybrid" and embedder is None:
            raise ValueError("hybrid mode needs an embedder")
        self._store = store
        self._embedder = embedder if mode == "hybrid" else None
        self.mode: Mode = mode
        store.connection.create_function(_FOLD_FN, 1, _fold_casefold, deterministic=True)

    @property
    def store(self) -> Store:
        """The Store this service searches (read-only: there is no setter). ``ToolContext``
        checks it is the context's own read-only index (#32 cross-plan note, plan 06b step 0)."""
        return self._store

    def _require_model(self, conn: sqlite3.Connection) -> vec.ModelInfo:
        assert self._embedder is not None
        model = vec.current(conn)
        if model is None:
            raise NoEmbeddingModel("no embedding model is registered in the index")
        if model.model_id != self._embedder.model_id:
            raise ModelMismatch(
                f"embedder model {self._embedder.model_id!r} is not the registered model "
                f"{model.model_id!r}"
            )
        return model

    @contextmanager
    def _snapshot(self) -> Iterator[sqlite3.Connection]:
        """One read transaction on the Store's connection (no await may run inside)."""
        conn = self._store.connection
        if conn.in_transaction:
            raise RuntimeError("search started inside an open transaction")
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            # read-only: ROLLBACK, and only on this path, so a failing COMMIT cannot mask the error
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        if conn.in_transaction:
            conn.execute("COMMIT")

    async def search(
        self, query: str, filters: SearchFilters = NO_FILTERS, limit: int = 10
    ) -> SearchResult:
        """The top ``limit`` (1..50) distinct mids for ``query`` under ``filters``."""
        _check_limit(limit)
        if not isinstance(filters, SearchFilters):
            raise TypeError("filters must be a SearchFilters")
        match = build_match(query)
        if match is None:
            raise EmptyQuery("the query has no word character")
        qvec: list[float] | None = None
        if self._embedder is not None:
            self._require_model(self._store.connection)  # fail before the embed call
            qvec = await self._embedder.embed_query(query)
        filter_sql, params = filters.chunk_filter()
        c = candidates(limit)
        with self._snapshot() as conn:
            rankings: list[list[str]] = []
            best: dict[str, tuple[int, int, int]] = {}  # mid -> (rank, arm, chunk_id)
            fts_rows = self._fts(conn, match, c, filter_sql, params)
            rankings.append(self._ranking(fts_rows, 0, best))
            model_id: str | None = None
            vec_count = 0
            if qvec is not None:
                model = self._require_model(conn)  # again: the await may have spanned a change
                model_id = model.model_id
                vec_rows = self._vector(conn, model, qvec, c, filter_sql, params)
                vec_count = len(vec_rows)
                rankings.append(self._ranking(vec_rows, 1, best))
            fused = rrf(rankings, DEFAULT_K)[:limit]
            hits = tuple(self._hit(conn, mid, score, best[mid][2]) for mid, score in fused)
            pending = vec.pending_count(conn)
        log.debug(
            "search mode=%s fts_chunks=%d vec_chunks=%d hits=%d",
            self.mode,
            len(fts_rows),
            vec_count,
            len(hits),
        )
        return SearchResult(hits=hits, mode=self.mode, model_id=model_id, embed_pending=pending)

    @staticmethod
    def _ranking(
        rows: Sequence[tuple[int, str]], arm: int, best: dict[str, tuple[int, int, int]]
    ) -> list[str]:
        """The arm's mid ranking; records each mid's best chunk as (rank, arm, chunk_id), the
        lower tuple winning (on equal ranks the FTS arm's chunk, arm 0)."""
        ranking = collapse(mid for _, mid in rows)
        first: dict[str, int] = {}
        for chunk_id, mid in rows:
            first.setdefault(mid, chunk_id)
        for rank, mid in enumerate(ranking, start=1):
            entry = (rank, arm, first[mid])
            if mid not in best or entry < best[mid]:
                best[mid] = entry
        return ranking

    @staticmethod
    def _fts(
        conn: sqlite3.Connection,
        match: str,
        c: int,
        filter_sql: str | None,
        params: tuple[object, ...],
    ) -> list[tuple[int, str]]:
        sql = (
            "SELECT c.chunk_id, c.mid FROM chunks_fts "
            "JOIN chunks c ON c.chunk_id = chunks_fts.rowid WHERE chunks_fts MATCH ?"
        )
        if filter_sql is not None:
            sql += f" AND chunks_fts.rowid IN ({filter_sql})"
        sql += " ORDER BY bm25(chunks_fts), c.chunk_id LIMIT ?"
        return [(int(cid), str(mid)) for cid, mid in conn.execute(sql, (match, *params, c))]

    @staticmethod
    def _vector(
        conn: sqlite3.Connection,
        model: vec.ModelInfo,
        qvec: Sequence[float],
        c: int,
        filter_sql: str | None,
        params: tuple[object, ...],
    ) -> list[tuple[int, str]]:
        if isinstance(qvec, str | bytes) or len(qvec) != model.dim:
            raise EmbedderError(f"query_dim_mismatch (model={model.model_id})")
        try:
            l2_norm(qvec)
        except InvalidVector as exc:
            raise EmbedderError(f"query_{exc.reason} (model={model.model_id})") from None
        k = min(c + 1, vec.MAX_K)
        while True:
            rows = vec.knn(conn, qvec, k, filter_sql, params)  # sorted by (distance, chunk_id)
            tie_open = len(rows) == k and len(rows) > c and rows[c - 1][1] == rows[-1][1]
            if not tie_open:
                break
            if k == vec.MAX_K:
                log.warning("vector arm: distance tie larger than k=%d; cut by vec0 layout", k)
                break
            k = min(2 * k, vec.MAX_K)
        top = rows[:c]
        if not top:
            return []
        ids = [cid for cid, _ in top]
        marks = ",".join("?" * len(ids))
        mids = dict(
            conn.execute(f"SELECT chunk_id, mid FROM chunks WHERE chunk_id IN ({marks})", ids)
        )
        if len(mids) != len(ids):
            raise SearchStateError("a vector has no chunk")
        return [(cid, str(mids[cid])) for cid in ids]

    @staticmethod
    def _hit(conn: sqlite3.Connection, mid: str, score: float, chunk_id: int) -> Hit:
        row = conn.execute(
            "SELECT date_utc, from_addr, from_name, subject, conflicted FROM messages "
            "WHERE mid = ?",
            (mid,),
        ).fetchone()
        if row is None:
            raise SearchStateError("a chunk has no message row")
        date_utc, from_addr, from_name, subject, conflicted = row
        text_row = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        if text_row is None or not isinstance(text_row[0], str):
            raise SearchStateError("a chunk has no text")
        date: datetime | None = None
        if date_utc is not None:
            try:
                date = datetime.fromtimestamp(date_utc, UTC)
            except (OverflowError, ValueError, OSError):
                # ingest stores NULL for such dates (mime.utc_epoch); not a bare ValueError,
                # which callers would take for a bad query (EmptyQuery is one)
                raise SearchStateError("date_utc out of range") from None
        folders = tuple(
            r[0]
            for r in conn.execute(
                "SELECT folder_id FROM locations WHERE mid = ? ORDER BY folder_id", (mid,)
            )
        )
        return Hit(
            mid=mid,
            date=date,
            from_addr=from_addr,
            from_name=from_name,
            subject=subject,
            snippet=snippet(text_row[0]),
            score=score,
            folder_ids=folders,
            conflicted=bool(conflicted),
        )
