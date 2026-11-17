"""The daemon's ``index.status`` handler (plan 08b step 2; A6, P6, P7).

A status read, not a tool call (P6): it reads the engine's read-only ``Store`` directly, in one
synchronous read transaction (no await inside, as the read-only tools do), and writes no audit
row. Bridge 1.0 puts no ``roles`` on ``index.status``, so the handler enforces
:data:`STATUS_ROLES` (P8): ``mcp-proxy`` gets ``role_rejected`` before anything is read.

Fields of the frozen 1.0 ``IndexStatus`` and how each is filled (P7; architecture §3, #36):

- ``messages``, ``locations``, ``conflicted``, ``parse_failed``, ``headers_only``:
  ``Store.status()``.
- ``last_reconcile``: the latest ``folders.last_reconciled_at``; null when no folder was
  reconciled yet.
- ``accounts``: every ``accounts`` row, by ``account_id``; ``name`` null → ``""`` (#32's null
  mapping); ``last_sync`` = the latest ``last_reconciled_at`` of its folders, null when none.
- ``embed_pending``: ``EmbedQueue.state.pending``. Embedding disabled (no queue) → ``0``, and
  ``models.embed`` → ``""`` (the config's own encoding of "disabled", #31 note); otherwise
  ``models.embed`` is ``cfg.embed_model``.
- ``models.chat``: the model new chat turns use (``ChatHost.chat_model``: ``cfg.chat_model``
  until the chat tab's model picker selects another). ``models.runtime_version``: ``""`` = not
  reported (ternd does not probe the model runtime; ``LMStudioShim`` is eval/doctor only, #35 P9).
- ``lag_s``: :meth:`tern.index.service.IngestService.event_lag_s`, the age of the oldest TB
  event received and not yet applied (``0.0`` when caught up).

Not representable in 1.0 (plan 08b "Skipped"): ``jobs_failed`` and in-memory job outcomes
(#30), 06b's ``stale_writes``, the embed queue's ``paused`` state. Each nullable or missing field
above becomes explicit in ``tern-bridge/2`` or a further 1.N method.

Wire form: the bridge's ``dump`` omits ``None`` fields (``exclude_none``), but the frozen 1.0
schema lists ``last_reconcile`` and ``AccountSync.last_sync`` as required (nullable). The
handler therefore returns :class:`WireIndexStatus`, an ``IndexStatus`` whose serializer writes
those two as an explicit ``null`` (the only required nullable fields of a 1.0 result); the
bridge itself is not changed here (plan 08b P16).

Logging: nothing on success; a refused role at WARNING (role only). Account names are returned
to the peer but never logged.

Account names and the ``cli`` role: the frozen 1.0 ``AccountSync`` requires ``name``, so a
``cli`` peer receives real account names too. A ``tern`` CLI consumer of ``index.status`` must
print each account as its #28 short hash (``tern.index.shorthash``, ``acct-…``), never ``name``
(CLAUDE.md: accounts appear as short hashes in CLI status). No CLI consumer exists yet; the plan
that adds one adds a test that a planted account name never appears in its output (#36).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from pydantic import SerializerFunctionWrapHandler, model_serializer

from tern.bridge.client import Peer
from tern.bridge.protocol import (
    AccountSync,
    BridgeError,
    Empty,
    IndexStatus,
    ModelIds,
    Role,
)
from tern.bridge.server import Handler
from tern.config import TernConfig
from tern.index.service import IngestService
from tern.index.store import Store
from tern.search.embed_queue import EmbedQueue

log = logging.getLogger(__name__)

#: The bridge roles that may read the index status: the extension's tab and ``tern`` CLI clients.
STATUS_ROLES: frozenset[Role] = frozenset({"extension", "cli"})

#: ``models.embed`` when embedding is disabled (the config's ``embed_model = ""``).
EMBED_DISABLED = ""

#: ``models.runtime_version``: ternd does not probe the model runtime ("not reported").
RUNTIME_NOT_REPORTED = ""


class WireIndexStatus(IndexStatus):
    """``IndexStatus`` that keeps its required nullable fields on the wire: ``last_reconcile``
    and each account's ``last_sync`` are written as ``null`` when None, also under the bridge's
    ``exclude_none`` dump (the frozen 1.0 schema requires both keys)."""

    @model_serializer(mode="wrap")
    def _keep_required_nulls(self, handler: SerializerFunctionWrapHandler) -> Any:
        out = handler(self)
        if self.last_reconcile is None:
            out["last_reconcile"] = None
        for account, wire in zip(self.accounts, out["accounts"], strict=True):
            if account.last_sync is None:
                wire["last_sync"] = None
        return out


@contextmanager
def _snapshot(store: Store) -> Iterator[sqlite3.Connection]:
    """One read transaction on the read-only index connection; no await may run inside."""
    conn = store.connection
    if conn.in_transaction:
        raise RuntimeError("status read started inside an open transaction")
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


def _utc(epoch: int | None) -> datetime | None:
    """A stored unix time as an aware UTC datetime; None stays None (nothing invented)."""
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC)


def read_status(
    index: Store,
    service: IngestService,
    embed: EmbedQueue | None,
    cfg: TernConfig,
    *,
    now: float,
    chat_model: str | None = None,
) -> WireIndexStatus:
    """The 1.0 ``IndexStatus`` (see the module docstring); ``now`` is a ``time.monotonic()``
    reading for ``lag_s``. Synchronous: nothing else runs on the loop while it reads."""
    with _snapshot(index) as conn:
        counts = index.status()
        last = conn.execute("SELECT max(last_reconciled_at) FROM folders").fetchone()[0]
        accounts = [
            AccountSync(
                account_id=str(account_id),
                name="" if name is None else str(name),
                last_sync=_utc(last_sync),
            )
            for account_id, name, last_sync in conn.execute(
                "SELECT a.account_id, a.name, max(f.last_reconciled_at) FROM accounts a "
                "LEFT JOIN folders f ON f.account_id = a.account_id "
                "GROUP BY a.account_id ORDER BY a.account_id"
            )
        ]
    if embed is None:
        embed_pending, embed_model = 0, EMBED_DISABLED
    else:
        state = embed.state
        if state.pending is None or state.model is None:
            # A queue exists only when embed_model is set; its state then always has both.
            raise RuntimeError("embed queue reports no model or pending count while enabled")
        embed_pending, embed_model = state.pending, state.model
    return WireIndexStatus(
        messages=counts.messages,
        locations=counts.locations,
        conflicted=counts.conflicted,
        parse_failed=counts.parse_failed,
        headers_only=counts.headers_only,
        embed_pending=embed_pending,
        lag_s=service.event_lag_s(now),
        last_reconcile=_utc(last),
        models=ModelIds(
            chat=cfg.chat_model if chat_model is None else chat_model,
            embed=embed_model,
            runtime_version=RUNTIME_NOT_REPORTED,
        ),
        accounts=accounts,
    )


def index_status_handler(
    index: Store,
    service: IngestService,
    embed: EmbedQueue | None,
    cfg: TernConfig,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    chat_model: Callable[[], str] | None = None,
) -> Handler:
    """The ``index.status`` handler over the engine's read-only ``index``, the ingest service
    (``lag_s``) and the embed queue (None when embedding is disabled). A role outside
    :data:`STATUS_ROLES` is ``role_rejected`` before anything is read."""

    async def handle(peer: Peer, params: Empty) -> IndexStatus:
        if peer.role not in STATUS_ROLES:
            log.warning("index.status refused for role", extra={"role": peer.role})
            raise BridgeError("role_rejected", f"index.status is not allowed for role {peer.role}")
        model = None if chat_model is None else chat_model()
        return read_status(index, service, embed, cfg, now=monotonic(), chat_model=model)

    return handle
