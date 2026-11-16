"""``EmbedQueue``: embeds pending chunks in the background of ``ternd`` (plan 05 step 4).

The queue runs as a task on the event loop that owns the ingest's :class:`~tern.index.store.Store`
and writes through that Store (no second writer connection; sqlite3's same-thread check enforces
the loop thread). :meth:`EmbedQueue.run`:

1. registers the configured model: one fixed probe string is embedded to learn the dim
   (``/v1/models`` reports none), then :func:`tern.search.vec.register` (a new model id or dim
   drops and recreates ``chunks_vec``, so every chunk is pending again);
2. loops: waits while any :meth:`EmbedQueue.hold` is active; takes the next
   ``embed_batch_size`` chunks from a window of pending chunks (see below); cuts each text to
   ``embed_max_input_chars`` (cuts are counted, never silent); awaits ``embed_documents``; writes
   the vectors with ``vec.insert`` in one short synchronous transaction (no await inside), which
   skips a chunk whose text changed or whose id was reused while the embedder was awaited; the
   next read picks the new chunk up. When no chunk is pending it records
   ``meta.embed_complete_at`` the first time (``vec.register`` clears it) and sleeps
   ``embed_idle_poll_s``.

One ``vec.pending`` scan (a full pass over ``chunks`` plus a sort) reads a window of up to
:data:`PENDING_WINDOW` pending chunks, newest message first; the window is consumed in
``embed_batch_size`` slices, and each slice is re-checked with ``vec.still_pending`` (point
lookups) before it is sent, so a chunk replaced, deleted or embedded since the scan is dropped
without an embedder call. The window is read again when it is used up, after a hold made the
queue wait, and after a (re-)registration; a slice that fails at the embedder is read again by
the next scan. A full embed of N chunks therefore costs about N / PENDING_WINDOW scans instead
of N / ``embed_batch_size`` (plan 05 merge review). Mail ingested while a window is being
consumed waits for the next scan (at most one window, i.e. PENDING_WINDOW chunks, later).

Idle polls are cheap: the queue remembers that ``embed_complete_at`` is set (it reads it once
per registration), and it skips the ``vec.pending`` scan while the Store connection's
``total_changes`` has not moved since the last empty read. The daemon holds the only writer
connection (``tern index rebuild`` takes the daemon lock), and every chunk insert, by the ingest
or anything else in ``ternd``, goes through it, so an unchanged counter means no new chunk.

A batch that writes no vector although chunks were pending (every chunk changed while it was
at the embedder) is logged at WARNING and followed by an idle wait in :meth:`EmbedQueue.run`;
:data:`ZERO_PROGRESS_LIMIT` such batches in a row raise ``VecStateError`` (a queue that cannot
make progress must not spin).

Failures:

- ``EmbedderUnavailable`` (server down, model not loaded, busy): status ``unavailable:<Type>``,
  retried with exponential back-off (1 s doubling to a 300 s cap, reset by the next success).
  Chunks are never marked failed; they stay pending.
- ``EmbedderError`` (a malformed response, including a vector count or dim that differs from the
  registered model's, or a NaN / zero vector): status ``error:<Type>``, logged at ERROR, and
  :meth:`EmbedQueue.run` returns (the queue stops; ``ternd`` keeps ingesting).
- Anything else (a ``VecStateError``, a database error) sets ``error:<Type>`` and is raised.

:meth:`EmbedQueue.hold` is a refcounted async context manager for model calls that must not run
beside an embedding batch (A-8: plans 08a/08b/11 wrap chat and triage calls in it). Entering it
does not wait: a batch already in flight finishes (and is written), and no new batch starts until
every hold has exited.

Logs carry counts, the model's dim and exception type names only: never chunk text or mids'
content.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass

from tern.config import TernConfig
from tern.index.store import Store
from tern.search import vec
from tern.search.embedder import (
    Embedder,
    EmbedderError,
    EmbedderUnavailable,
    InvalidVector,
    l2_norm,
)

log = logging.getLogger(__name__)

#: The text embedded once at registration to learn the model's dim.
PROBE_TEXT = "tern embedding dimension probe"
BACKOFF_START_S = 1.0
BACKOFF_CAP_S = 300.0
#: Consecutive batches that wrote no vector before the queue gives up (``VecStateError``).
ZERO_PROGRESS_LIMIT = 5
#: Pending chunks read by one ``vec.pending`` scan (at least ``embed_batch_size``).
PENDING_WINDOW = 4096

STATUS_OK = "ok"
STATUS_PAUSED = "paused"
STATUS_DISABLED = "disabled"
UNAVAILABLE_PREFIX = "unavailable:"
ERROR_PREFIX = "error:"


@dataclass(frozen=True)
class EmbedState:
    """What status reports show about the queue."""

    #: The configured model id (None when embedding is disabled).
    model: str | None
    #: Chunks without a vector under the configured model. Before the queue has registered,
    #: the index's count when its current model has the configured id, else all chunks (None
    #: when disabled).
    pending: int | None
    #: ``ok``, ``paused`` (a hold is active), ``unavailable:<Type>``, ``error:<Type>`` or
    #: ``disabled``.
    status: str
    #: Chunk texts cut to ``embed_max_input_chars`` before embedding, since the queue started.
    truncated: int


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class EmbedQueue:
    """Embeds the index's pending chunks; see the module docstring.

    ``sleep`` (back-off and idle waits) and ``clock`` (``meta.embed_complete_at``, unix seconds)
    are injectable for tests."""

    def __init__(
        self,
        store: Store,
        embedder: Embedder,
        cfg: TernConfig,
        *,
        sleep: Callable[[float], Awaitable[None]] = _sleep,
        clock: Callable[[], float] = time.time,
        pending_window: int = PENDING_WINDOW,
    ) -> None:
        if cfg.embed_model is not None and embedder.model_id != cfg.embed_model:
            raise ValueError("the embedder's model id differs from embed_model in the config")
        if isinstance(pending_window, bool) or not isinstance(pending_window, int):
            raise ValueError("pending_window must be an int")
        if pending_window < 1:
            raise ValueError("pending_window must be >= 1")
        self.store = store
        self.embedder = embedder
        self.cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self._model: vec.ModelInfo | None = None
        self._status = STATUS_DISABLED if cfg.embed_model is None else STATUS_OK
        self._truncated = 0
        self._holds = 0
        self._released = asyncio.Event()
        self._released.set()
        self._active = False
        #: ``meta.embed_complete_at`` is known to be set under ``self._model``.
        self._complete_known = False
        #: ``conn.total_changes`` after the last read that found no pending chunk (None: scan).
        self._idle_changes: int | None = None
        self._zero_batches = 0
        #: Pending chunks read by the last scan and not yet taken (newest message first).
        self._window: collections.deque[vec.PendingChunk] = collections.deque()
        self._window_size = max(pending_window, cfg.embed_batch_size)

    # -- status ----------------------------------------------------------------------------------

    @property
    def disabled(self) -> bool:
        return self.cfg.embed_model is None

    @property
    def state(self) -> EmbedState:
        """The current state; ``pending`` is counted from the index on each read."""
        if self.disabled:
            return EmbedState(None, None, STATUS_DISABLED, self._truncated)
        conn = self.store.connection
        registered = self._model if self._model is not None else vec.current(conn)
        if registered is None or registered.model_id != self.cfg.embed_model:
            pending = int(conn.execute("SELECT count(*) FROM chunks").fetchone()[0])
        else:
            # Before registration a same-id model's vectors count: registration keeps them unless
            # the probe finds another dim (then every chunk is pending again).
            # Two counts (exact, see vec.pending_count), not vec.status's per-chunk probe:
            # bridge index.status reads this on every call (plan 08b).
            pending = vec.pending_count(conn)
        return EmbedState(self.cfg.embed_model, pending, self._status, self._truncated)

    # -- holds -----------------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def hold(self, reason: str) -> AsyncIterator[None]:
        """While any hold is active no new batch starts; an in-flight batch finishes. Holds nest
        and overlap (refcounted). ``reason`` (e.g. ``"chat"``) is logged at DEBUG only."""
        self._holds += 1
        self._released.clear()
        log.debug("embed hold entered", extra={"reason": reason, "holds": self._holds})
        try:
            yield
        finally:
            self._holds -= 1
            if self._holds == 0:
                self._released.set()
            log.debug("embed hold exited", extra={"reason": reason, "holds": self._holds})

    async def _wait_holds(self) -> bool:
        """Wait until no hold is active; True when it had to wait."""
        waited = False
        while self._holds:
            waited = True
            self._status = STATUS_PAUSED
            await self._released.wait()
        if self._status == STATUS_PAUSED:
            self._status = STATUS_OK
        return waited

    # -- running ---------------------------------------------------------------------------------

    def _enter(self) -> None:
        if self.disabled:
            raise ValueError("embedding is disabled (embed_model is not configured)")
        if self._active:
            raise RuntimeError("the embed queue is already running")
        self._active = True

    async def run(self) -> None:
        """Embed until cancelled; returns when the queue stops on an ``EmbedderError`` (and at
        once when embedding is disabled). See the module docstring."""
        if self.disabled:
            log.info("embed disabled")
            return
        self._enter()
        try:
            backoff = BACKOFF_START_S
            while True:
                try:
                    if self._model is None:
                        await self._register()
                    written = await self._batch()
                except EmbedderUnavailable as exc:
                    self._status = UNAVAILABLE_PREFIX + type(exc).__name__
                    log.warning(
                        "embedder unavailable; retrying",
                        extra={"error": type(exc).__name__, "retry_in_s": backoff},
                    )
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_CAP_S)
                    continue
                except EmbedderError as exc:
                    self._status = ERROR_PREFIX + type(exc).__name__
                    log.error(
                        "embedder error; embed queue stopped", extra={"error": type(exc).__name__}
                    )
                    return
                backoff = BACKOFF_START_S
                self._status = STATUS_OK
                if not written:  # idle (None) or a batch that wrote nothing (0)
                    await self._sleep(self.cfg.embed_idle_poll_s)
        except Exception as exc:
            self._status = ERROR_PREFIX + type(exc).__name__
            raise
        finally:
            self._active = False

    async def drain(self) -> int:
        """Embed until no chunk is pending and return how many vectors were written. Holds are
        respected; ``EmbedderUnavailable`` and ``EmbedderError`` are raised, not retried (smoke
        and CLI tests)."""
        self._enter()
        try:
            if self._model is None:
                await self._register()
            total = 0
            while (written := await self._batch()) is not None:
                total += written
            self._status = STATUS_OK
            return total
        except (EmbedderUnavailable, EmbedderError) as exc:
            prefix = UNAVAILABLE_PREFIX if isinstance(exc, EmbedderUnavailable) else ERROR_PREFIX
            self._status = prefix + type(exc).__name__
            raise
        except Exception as exc:
            self._status = ERROR_PREFIX + type(exc).__name__
            raise
        finally:
            self._active = False

    async def _register(self) -> None:
        await self._wait_holds()  # the probe is a model call too
        vectors = await self.embedder.embed_documents([PROBE_TEXT])
        if len(vectors) != 1 or not vectors[0]:
            raise EmbedderError("probe returned no vector")
        _check_vectors(vectors, 1, len(vectors[0]))
        self._model = vec.register(self.store, self.embedder.model_id, len(vectors[0]))
        self._complete_known = False
        self._idle_changes = None
        self._window.clear()

    async def _batch(self) -> int | None:
        """One batch: the number of vectors written (a changed chunk is skipped), or None when
        no chunk was pending (``meta.embed_complete_at`` is then set if it is not yet). The
        pending scan is skipped while nothing was written since the last empty read."""
        model = self._model
        if model is None:
            raise RuntimeError("no model registered")
        # Yield once per batch: an embedder that never suspends (the stub, a CPU model) must not
        # starve the ingest on the same loop, and a hold taken meanwhile is seen below.
        await asyncio.sleep(0)
        if await self._wait_holds():
            self._window.clear()  # new mail may have arrived meanwhile: read newest first again
        conn = self.store.connection
        batch = self._next_slice(conn)
        if not batch:
            if not self._complete_known:
                status = vec.status(conn, max_input_chars=self.cfg.embed_max_input_chars)
                if status.embed_complete_at is None:
                    vec.set_complete(self.store, int(self._clock()))
                    log.info("embedding complete", extra={"dim": model.dim})
                self._complete_known = True
            self._idle_changes = conn.total_changes  # after set_complete's own write
            return None
        self._idle_changes = None
        cap = self.cfg.embed_max_input_chars
        texts = [p.text[:cap] for p in batch]
        cut = sum(len(p.text) > cap for p in batch)
        # The rest of the window is set aside during the await and kept only on success: after
        # a failure the next batch scans again (and so re-reads this slice's chunks first).
        rest, self._window = self._window, collections.deque()
        vectors = await self.embedder.embed_documents(texts)
        self._window = rest
        _check_vectors(vectors, len(batch), model.dim)
        if conn.in_transaction:
            raise RuntimeError("the store connection is inside a transaction")
        # The re-check in vec.insert compares the full chunk text, not the cut one.
        written = vec.insert(
            self.store,
            model,
            [(p.chunk_id, p.text, v) for p, v in zip(batch, vectors, strict=True)],
        )
        self._truncated += cut
        log.debug(
            "embedded batch",
            extra={"chunks": len(batch), "written": written, "truncated": cut},
        )
        if written:
            self._zero_batches = 0
            return written
        self._zero_batches += 1
        log.warning(
            "embed batch wrote no vector",
            extra={"chunks": len(batch), "zero_batches": self._zero_batches},
        )
        if self._zero_batches >= ZERO_PROGRESS_LIMIT:
            raise vec.VecStateError(f"{self._zero_batches} embed batches in a row wrote no vector")
        return 0

    def _next_slice(self, conn: sqlite3.Connection) -> list[vec.PendingChunk]:
        """The next up to ``embed_batch_size`` chunks to embed, or [] when none is pending (or
        the idle shortcut applies). A slice from a window read by an earlier scan is re-checked
        with ``vec.still_pending``; when every chunk in it is stale, the next slice is taken."""
        size = self.cfg.embed_batch_size
        while True:
            fresh = not self._window
            if fresh:
                if self._idle_changes is not None and conn.total_changes == self._idle_changes:
                    return []
                self._window.extend(vec.pending(conn, self._window_size))
                if not self._window:
                    return []
            batch = [self._window.popleft() for _ in range(min(size, len(self._window)))]
            if fresh:
                return batch  # read just now
            batch = vec.still_pending(conn, batch)
            if batch:
                return batch


def _check_vectors(vectors: Sequence[Sequence[float]], count: int, dim: int) -> None:
    """``EmbedderError`` unless there are ``count`` vectors of length ``dim``, each finite with
    a nonzero norm (an embedder other than ``EmbeddingClient`` may not check this itself)."""
    if len(vectors) != count:
        raise EmbedderError("vector count differs from the input count")
    for vector in vectors:
        if len(vector) != dim:
            raise EmbedderError("vector dim differs from the registered model's")
        try:
            l2_norm(vector)
        except InvalidVector as exc:
            raise EmbedderError(f"invalid vector: {exc.reason}") from exc
