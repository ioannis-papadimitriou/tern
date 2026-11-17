"""``ternd``: the long-running tern daemon (decisions #3).

It listens only on the bridge Unix socket and runs the ingest jobs over the extension link
(``tern.index.service``, plan 04 step 9), and the embed queue on the same index
(``tern.search.embed_queue``, plan 05 step 7). It holds the audit log and the permission engine
(plan 06b step 7): ternd is the only writer of ``audit.db``. It builds the one tool registry
(``Daemon.registry``, plan 07 step 3) over that engine. ``tern-nmhost`` starts it detached
when the socket is absent, so it outlives Thunderbird. ``--foreground`` logs to stderr
(``make serve``); otherwise logs go to ``<state dir>/ternd.log``.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tern import config, paths
from tern import log as tern_log
from tern.bridge.client import Peer
from tern.bridge.peercred import ProcessInfo, ProcFs, installed_nmhost_path
from tern.bridge.protocol import BridgeError, ErrorName, Role, UndoParams, UndoResult
from tern.bridge.server import BridgeServer, Handler, NotifHandler
from tern.daemon import uds
from tern.daemon.chat import ChatHost
from tern.daemon.handlers import (
    ToolCalls,
    audit_status_handler,
    daemon_info_handler,
    tools_list_handler,
    ui_open_mid_handler,
)
from tern.daemon.status import index_status_handler
from tern.index.service import EVENT_METHODS, IngestService, cancel_and_wait
from tern.index.source import IngestSource
from tern.index.store import Store, index_path
from tern.llm import models as llm_models
from tern.llm.client import LLMClient
from tern.perms.audit import AuditLog, audit_path
from tern.perms.engine import Engine
from tern.perms.recipients import DenyAllRecipients
from tern.search.embed_queue import EmbedQueue
from tern.search.embedder import Embedder, EmbeddingClient
from tern.search.service import SearchService
from tern.tools.contract import ToolError
from tern.tools.registry import PluginLoadError, RegisteredTool, Registry, ToolRuntime, discover

log = logging.getLogger("tern.daemon")

#: Builds the embed queue's embedder from the config (only called when ``embed_model`` is set).
EmbedderFactory = Callable[[config.TernConfig], Embedder]


def make_embedder(cfg: config.TernConfig) -> Embedder:
    """The production embedder: an ``EmbeddingClient`` (loopback unless the config allows a
    remote URL; ``EgressDenied`` otherwise)."""
    return EmbeddingClient(cfg)


#: Builds the chat model client from the config (plan 08b step 1).
LLMFactory = Callable[[config.TernConfig], LLMClient]


def make_llm(cfg: config.TernConfig) -> LLMClient:
    """The production chat client: an ``LLMClient`` over ``cfg.chat_base_url`` (loopback unless
    the config allows a remote URL; ``EgressDenied`` otherwise, raised at construction)."""
    return LLMClient(cfg)


def _picked_llm(cfg: config.TernConfig, base_url: str, model: str) -> LLMClient:
    """A chat client for a model picked in the chat tab (``chat.select_model``)."""
    return LLMClient(cfg, base_url=base_url, model=model)


@dataclass
class Daemon:
    """What a running ``ternd`` holds, for the wiring of later plans: 06a/08b's handlers and the
    model calls of 08a/08b/11, which wrap themselves in ``embed.hold(...)`` (A-8)."""

    store: Store
    server: BridgeServer
    service: IngestService
    #: None when embedding is disabled (``embed_model`` not configured).
    embed: EmbedQueue | None
    #: The audit log (``audit_path(tp)``); ternd is its only writer (plan 06b).
    audit: AuditLog
    #: The permission engine (plan 06b step 7): deny-all recipients (plan 10 injects the real
    #: provider), no approval UI (plan 12 attaches one), a read-only Store over the index.
    engine: Engine
    #: The one tool registry (plan 07 step 3, :func:`tool_registry`): built-ins, entry points and
    #: ``config.plugins.paths``, its ``ToolRuntime`` over ``engine`` (its ``audit``, read-only
    #: ``index``, ``bridge`` and ``clock``). ``audit.undo``, ``tools.*`` and ``ui.open_mid`` run
    #: on it (``tern.daemon.handlers``). Every tool call goes through its :meth:`Registry.call`.
    registry: Registry
    #: The chat glue (plan 08b step 1): ``chat.send``/``chat.cancel`` over agent sessions on
    #: ``registry``, the daemon's ``LLMClient``, the engine's read-only index and ``embed.hold``.
    chat: ChatHost
    #: The ``tools.call`` handler and its running MCP calls (plan 07 step 8).
    tool_calls: ToolCalls


async def serve(
    tp: paths.TernPaths,
    cfg: config.TernConfig,
    *,
    process_info: ProcessInfo | None = None,
    nmhost_path: Path | None = None,
    stop: asyncio.Event | None = None,
    embedder_factory: EmbedderFactory | None = None,
    llm_factory: LLMFactory | None = None,
    on_started: Callable[[Daemon], None] | None = None,
) -> None:
    """Listen on the bridge socket and run the ingest service until SIGTERM/SIGINT (or ``stop``).

    The index (``index_path(tp)``) is opened here and bound to the extension's TB profile at its
    hello (``profile_mismatch`` otherwise); TB events go to the ingest service, which applies
    each in its own task (never the bridge reader task). ``process_info``, ``nmhost_path``,
    ``stop``, ``embedder_factory`` and ``on_started`` are for tests (defaults: ``/proc``, the
    installed host, the signals, :func:`make_embedder`, nothing; ``on_started`` receives the
    :class:`Daemon` once its tasks run).

    When ``cfg.embed_model`` is set, an :class:`EmbedQueue` over the same ``Store`` (the only
    writer connection) runs as a task beside the ingest service; otherwise ``embed disabled`` is
    logged and there is none. The embedder is built before the index is opened: a URL the egress
    guard refuses (``EgressDenied``) stops ternd from starting, like an invalid config. An
    unreachable embedder never stops ternd (the queue backs off and retries); a queue that stops
    on a malformed response, or dies of any other exception (logged at ERROR by type), leaves
    ternd ingesting, its status in ``Daemon.embed.state``. The embedder is closed after the queue
    has stopped.

    If the ingest service dies (an exception that is not a recorded job failure), ternd stops:
    it is logged at ERROR by type and re-raised from here once the socket and the index are
    closed. Shutdown order: stop listening and close every connection (no new events), then stop
    the jobs and their event tasks, then remove the socket and close the index. Cancelling the
    task running ``serve()`` shuts down the same way and ends it cancelled (also when the
    cancellation arrives while the jobs are stopping). The embed queue is stopped with the jobs,
    before the socket is removed and the index closed.

    Permissions (plan 06b step 7): the audit log (``audit_path(tp)``, clock ``time.time``) and a
    read-only Store over the index are opened after the index; the :class:`Engine` uses both,
    the extension link and the same clock. Its write status is loaded from the audit log (writes
    of the last 7 days still ``tb_accepted``) and its observer is added to the index Store's
    ``location_observers`` before the socket opens. At shutdown, after the jobs stopped, every
    open approval is resolved ``shutdown`` (its row written) before the audit log closes; the
    read-only Store and the audit log are closed before the index.

    The tool registry (plan 07 step 3, :func:`tool_registry`) is built after the engine and
    before the socket opens: built-ins, entry-point plugins and ``cfg.plugins.paths``. A plugin
    that cannot be loaded (or a duplicate tool name) is a :class:`PluginLoadError`: logged at
    ERROR by type and re-raised, so ternd does not start (no socket; ``main()`` exits non-zero),
    like an invalid config. ``audit.undo`` (plan 06b step 10) is served by
    :func:`audit_undo_handler` over that registry (one registry).

    Chat (plan 08b step 1): the ``LLMClient`` is built by ``llm_factory`` (default
    :func:`make_llm`) after the embedder and before the index is opened, so a chat URL the egress
    guard refuses (``EgressDenied``) stops ternd before its socket exists. :class:`ChatHost` serves
    ``chat.send``/``chat.cancel``. At shutdown, once no connection is left and before the jobs
    stop, every running turn is cancelled and awaited (no ``Registry.call`` after the audit log
    closes); the client is closed after the index, before the embedder.

    ``index.status`` (plan 08b step 2, :func:`tern.daemon.status.index_status_handler`) reads the
    engine's read-only Store directly (a status read, not a tool call: no audit row), with the
    ingest service's event lag and the embed queue's pending count.

    ``tern-bridge/1.1`` (decisions #37): ``tools.list``/``tools.call`` (the ``tern mcp`` proxy) and
    ``ui.open_mid`` on the registry, ``audit.status`` on the engine, ``daemon.info`` from the
    config (:mod:`tern.daemon.handlers`)."""
    embedder: Embedder | None = None
    if cfg.embed_model is None:
        log.info("embed disabled")
    else:
        embedder = (embedder_factory if embedder_factory is not None else make_embedder)(cfg)
    llm: LLMClient | None = None
    try:
        llm = (llm_factory if llm_factory is not None else make_llm)(cfg)
        # The caller holds the instance lock (main()), so no other ternd has the index open.
        store = Store.open(index_path(tp))
        try:
            reader = Store.open(index_path(tp), readonly=True)
            try:
                with AuditLog.open(audit_path(tp), clock=time.time) as audit:
                    await _serve(
                        tp,
                        cfg,
                        _Stores(store, reader, audit),
                        embedder,
                        llm,
                        process_info,
                        nmhost_path,
                        stop,
                        on_started,
                    )
            finally:
                reader.close()
        finally:
            store.close()
    finally:
        try:
            if llm is not None:
                await llm.aclose()
        finally:
            try:
                if isinstance(embedder, EmbeddingClient):
                    await embedder.aclose()
            finally:
                log.info("ternd stopped")


@dataclass(frozen=True)
class _Stores:
    """What :func:`serve` opened for :func:`_serve`: the index (the only writer connection), a
    read-only connection to it (the engine's) and the audit log."""

    index: Store
    reader: Store
    audit: AuditLog


def _engine(server: BridgeServer, stores: _Stores) -> Engine:
    """The permission engine, with its write status loaded and observing the index."""
    engine = Engine(
        audit=stores.audit,
        index=stores.reader,
        bridge=server.link,
        clock=time.time,
        known_recipients=DenyAllRecipients(),
        notifier=None,
    )
    engine.write_status.load()
    stores.index.location_observers.append(engine.write_status.observe)
    return engine


#: The bridge roles whose ``audit.undo`` is the user's: the extension's audit view and ``tern
#: undo`` (role ``cli``). Both undo as caller ``ui`` (plan 06b step 10); ``mcp-proxy`` never may
#: (bridge 1.0 ``audit.undo`` has no ``roles`` restriction, so the handler enforces it).
UNDO_ROLES: frozenset[Role] = frozenset({"extension", "cli"})

#: ``Registry.undo``'s ToolError codes -> bridge 1.0 error names. ``mid_conflicted`` (the message
#: became conflicted since the write) is a changed state for the user; ``internal`` (an audit
#: failure, an unexpected bridge error) has no 1.0 name and goes out as ``unavailable``, its
#: tern code kept in the message. Any other code is a tern bug (KeyError: the bridge reports a
#: handler failure).
UNDO_ERRORS: Mapping[str, ErrorName] = {
    "invalid_params": "invalid_params",
    "state_changed": "state_changed",
    "mid_conflicted": "state_changed",
    "unavailable": "unavailable",
    "internal": "unavailable",
    "denied": "role_rejected",
}


def tool_registry(
    cfg: config.TernConfig,
    tools: Sequence[RegisteredTool],
    server: BridgeServer,
    engine: Engine,
    embedder: Embedder | None,
    state_dir: Path,
) -> Registry:
    """``Daemon.registry``: ``tools`` over a ``ToolRuntime`` on the engine's audit log, read-only
    index, extension link (``server.link``) and clock. Its ``SearchService`` searches that
    read-only Store in ``hybrid`` mode with ``embedder`` (the embed queue's) when there is one
    (``cfg.embed_model`` set), else explicitly in ``fts`` mode (never switched silently). Tools
    get ``state_dir`` (ternd's state dir) as ``ctx.state_dir``. A duplicate tool name is a
    :class:`PluginLoadError`."""
    search = (
        SearchService(engine.index, None, mode="fts")
        if embedder is None
        else SearchService(engine.index, embedder, mode="hybrid")
    )
    runtime = ToolRuntime(
        audit=engine.audit,
        index=engine.index,
        search=search,
        bridge=server.link,
        config=cfg,
        log=logging.getLogger("tern.tools"),
        clock=engine.clock,
        engine=engine,
        state_dir=state_dir,
    )
    return Registry(tools, runtime=runtime)


def audit_undo_handler(registry: Registry) -> Handler:
    """The ``audit.undo`` handler: roles in :data:`UNDO_ROLES` undo as caller ``ui``; any other
    role is ``role_rejected`` before the token is looked at (no audit row: the caller never
    reaches the registry). A refusal is the :data:`UNDO_ERRORS` name, its message the
    ToolError's (content-free) message plus its code and reason."""

    async def handle(peer: Peer, params: UndoParams) -> UndoResult:
        if peer.role not in UNDO_ROLES:
            log.warning("audit.undo refused for role", extra={"role": peer.role})
            raise BridgeError("role_rejected", f"audit.undo is not allowed for role {peer.role}")
        try:
            await registry.undo("ui", params.undo_token)
        except ToolError as err:
            detail = err.code if err.reason is None else f"{err.code}/{err.reason}"
            raise BridgeError(UNDO_ERRORS[err.code], f"{err.message} [{detail}]") from None
        return UndoResult(undone=True)

    return handle


async def _serve(
    tp: paths.TernPaths,
    cfg: config.TernConfig,
    stores: _Stores,
    embedder: Embedder | None,
    llm: LLMClient,
    process_info: ProcessInfo | None,
    nmhost_path: Path | None,
    stop: asyncio.Event | None,
    on_started: Callable[[Daemon], None] | None,
) -> None:
    store = stores.index
    server = BridgeServer(
        process_info=process_info if process_info is not None else ProcFs(),
        thunderbird_bin=cfg.thunderbird_bin,
        nmhost_path=nmhost_path if nmhost_path is not None else installed_nmhost_path(),
        bind_profile=store.bind_profile,
    )
    service = IngestService(store, IngestSource(server.link), cfg)
    server.link.on_attach.append(service.on_attach)
    for method in EVENT_METHODS:
        server.notification_handlers[method] = _event_handler(service, method)
    embed = EmbedQueue(store, embedder, cfg) if embedder is not None else None
    engine = _engine(server, stores)
    try:
        # Plugin code runs here (import), before the socket exists: a failure stops ternd.
        try:
            tools = discover(cfg.plugins.paths)
        except SystemExit:
            # sys.exit() at plugin import is a BaseException the loader's `except Exception` does
            # not convert: without this ternd would stop with the plugin's exit code (0 included)
            # and no ERROR line. KeyboardInterrupt still propagates. Type only, never the code.
            raise PluginLoadError("a tool plugin raised SystemExit at import") from None
        registry = tool_registry(cfg, tools, server, engine, embedder, tp.state)
    except PluginLoadError as exc:
        # By type only; the message (entry point or path, never file content) is re-raised.
        log.error("tool plugins failed to load; ternd stops", extra={"error": type(exc).__name__})
        raise
    server.handlers["audit.undo"] = audit_undo_handler(registry)
    chat = ChatHost(
        registry,
        llm,
        engine.index,
        cfg,
        hold=embed.hold if embed is not None else None,
        approvals=engine.approvals,
        discover=functools.partial(llm_models.discover, cfg),
        make_llm=functools.partial(_picked_llm, cfg),
    )
    server.handlers["chat.send"] = chat.send
    server.handlers["chat.cancel"] = chat.cancel
    # tern-bridge/1.2, staged (decisions #39): reached only on a connection at minor >= 2.
    server.handlers["chat.start"] = chat.start
    server.handlers["chat.approve"] = chat.approve
    # tern-bridge/1.3: the chat tab's model picker.
    server.handlers["chat.models"] = chat.models
    server.handlers["chat.select_model"] = chat.select_model
    server.handlers["index.status"] = index_status_handler(
        engine.index, service, embed, cfg, chat_model=lambda: chat.chat_model
    )
    # tern-bridge/1.1 (decisions #37; tern.daemon.handlers).
    server.handlers["tools.list"] = tools_list_handler(registry)
    tool_calls = ToolCalls(registry)
    server.handlers["tools.call"] = tool_calls.handle
    server.handlers["audit.status"] = audit_status_handler(engine)
    server.handlers["ui.open_mid"] = ui_open_mid_handler(registry)
    server.handlers["daemon.info"] = daemon_info_handler(
        cfg, chat.info_read, lambda: chat.chat_model
    )
    daemon = Daemon(store, server, service, embed, stores.audit, engine, registry, chat, tool_calls)
    stop = stop if stop is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    srv = await uds.start(server, tp.socket)
    # From here on the socket file exists: everything after this line, signal registration and
    # job creation included, runs inside the try, so the cleanup below always runs.
    registered: list[signal.Signals] = []
    jobs: asyncio.Task[None] | None = None
    embedding: asyncio.Task[None] | None = None
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
            registered.append(sig)
        jobs = asyncio.create_task(service.run_until(stop))
        jobs.add_done_callback(functools.partial(_jobs_ended, stop))
        if embed is not None:
            embedding = asyncio.create_task(embed.run())
            embedding.add_done_callback(_embed_ended)
        if on_started is not None:
            on_started(daemon)
        log.info("ternd listening")
        await stop.wait()
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)
        try:
            # First no new connections and no new events, so no event task starts after the
            # jobs (and with them the pending event tasks) are cancelled below.
            srv.close()
            await server.shutdown()
            await srv.wait_closed()
        finally:
            try:
                # No chat.send or tools.call can arrive any more: every running turn and MCP
                # call is cancelled and awaited, so no Registry.call runs after the audit log
                # closes.
                await _shutdown_calls(chat, tool_calls)
            finally:
                try:
                    # Swallows only the jobs' own cancellation; re-raises what killed the
                    # service, and a cancellation of serve() itself.
                    if jobs is not None:
                        await cancel_and_wait(jobs)
                finally:
                    try:
                        # A queue that already died was logged by _embed_ended and keeps ternd
                        # up, so its exception is not raised here; a running one is cancelled.
                        if embedding is not None and not embedding.done():
                            await cancel_and_wait(embedding)
                    finally:
                        try:
                            # Every open approval gets its denied/shutdown row before the audit
                            # log closes (serve() closes it after this returns).
                            denied = engine.approvals.shutdown()
                            if denied:
                                log.info("open approvals denied at shutdown", extra={"n": denied})
                        finally:
                            tp.socket.unlink(missing_ok=True)


async def _shutdown_calls(chat: ChatHost, tool_calls: ToolCalls) -> None:
    """Chat turns, then MCP tool calls (each cancelled and awaited); the second runs even if the
    first raised."""
    try:
        try:
            await chat.shutdown()
        finally:
            await chat.aclose()
    finally:
        await tool_calls.shutdown()


def _jobs_ended(stop: asyncio.Event, jobs: asyncio.Task[None]) -> None:
    """The ingest service's task ended: if an exception killed it, ternd stops (loudly)."""
    if jobs.cancelled():
        return
    exc = jobs.exception()
    if exc is not None:
        log.error("ingest service died; ternd stops", extra={"error": type(exc).__name__})
        stop.set()


def _embed_ended(embedding: asyncio.Task[None]) -> None:
    """The embed queue's task ended. Unless cancelled (shutdown), ternd keeps ingesting: a
    queue that stopped on a malformed response has logged it itself; any other exception is
    logged here at ERROR by type (its status stays in ``EmbedQueue.state``)."""
    if embedding.cancelled():
        return
    exc = embedding.exception()
    if exc is not None:
        log.error("embed queue died; ternd keeps ingesting", extra={"error": type(exc).__name__})


def _event_handler(service: IngestService, method: str) -> NotifHandler:
    """Hand a TB event to the service; returns at once (the reader task must keep reading)."""

    async def handle(peer: Peer, params: Any) -> None:
        service.submit_event(method, params)

    return handle


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ternd", description="tern daemon")
    ap.add_argument("--foreground", action="store_true", help="log to stderr")
    args = ap.parse_args(argv)

    tp = paths.resolve()
    cfg = config.load(tp.config_file)
    uds.prepare_runtime_dir(tp.run)
    tp.state.mkdir(parents=True, exist_ok=True)
    if args.foreground:
        tern_log.setup(sys.stderr)
    else:
        tern_log.setup(open(tp.state / "ternd.log", "a", encoding="utf-8"))  # noqa: SIM115  # process-lifetime log stream

    lock = uds.InstanceLock(tp.lock)
    if not lock.acquire():
        log.info("another ternd holds the lock; exiting")
        return 0
    try:
        asyncio.run(serve(tp, cfg))
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
