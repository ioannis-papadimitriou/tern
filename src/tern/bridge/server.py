"""The daemon side of the bridge: ``hello``, role checks, the single extension slot, dispatch.

Transport-agnostic: ``handle_connection`` takes a stream pair plus the peer's pid (from
``SO_PEERCRED``); ``tern.daemon.uds`` owns the socket.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

import tern
from tern.bridge import framing
from tern.bridge.client import ExtensionLink, Peer
from tern.bridge.peercred import ProcessInfo, check_extension_peer
from tern.bridge.protocol import (
    MINOR,
    PROTOCOL,
    BridgeError,
    Empty,
    Hello,
    Notification,
    PingExtensionParams,
    PingStats,
    Request,
    dump,
    method_spec,
    negotiate_minor,
    parse_message,
    validate_params,
)

log = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 10.0

#: Everything an ``mcp-proxy`` may ask the daemon (plan 07 step 8 review): its tools and counts.
#: An allowlist, because the frozen 1.0 table puts no roles on ``approvals.pending``,
#: ``audit.list``, ``search.query``, ``chat.*`` or ``index.status``; anything else is
#: ``role_rejected`` before params or handlers, whether or not a handler is wired (fail closed).
MCP_PROXY_METHODS: frozenset[str] = frozenset(
    {"bridge.ping", "tools.list", "tools.call", "audit.status"}
)
LIVENESS_PING_S = 2.0  # architecture §3: the old extension connection must answer within 2 s

Handler = Callable[[Peer, Any], Awaitable[BaseModel]]
NotifHandler = Callable[[Peer, Any], Awaitable[None]]


def _percentile(sorted_ms: list[float], q: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    return sorted_ms[max(0, math.ceil(q * len(sorted_ms)) - 1)]


class BridgeServer:
    def __init__(
        self,
        *,
        process_info: ProcessInfo,
        thunderbird_bin: Path,
        nmhost_path: Path,
        link: ExtensionLink | None = None,
        liveness_ping_s: float = LIVENESS_PING_S,
        hello_timeout_s: float = HELLO_TIMEOUT_S,
        bind_profile: Callable[[UUID], None] | None = None,
        minor: int = MINOR,
    ) -> None:
        if not 0 <= minor <= MINOR:
            raise ValueError(f"minor must be 0..{MINOR}")
        self._info = process_info
        self._minor = minor
        # Binds the index to the extension's TB profile (``Store.bind_profile``); raises
        # ``BridgeError("profile_mismatch")`` for another profile (architecture §3, plan 04 step 9).
        self._bind_profile = bind_profile
        self._thunderbird_bin = thunderbird_bin
        self._nmhost_path = nmhost_path
        self.link = link if link is not None else ExtensionLink()
        self._liveness_ping_s = liveness_ping_s
        self._hello_timeout_s = hello_timeout_s
        self._slot = asyncio.Lock()  # serializes extension hellos
        self.handlers: dict[str, Handler] = {
            "bridge.ping": self._ping,
            "bridge.ping_extension": self._ping_extension,
            "approvals.decide": self._approvals_decide_placeholder,
        }
        self.notification_handlers: dict[str, NotifHandler] = {}
        self._peers: set[Peer] = set()
        self._shut_down = False

    @property
    def minor(self) -> int:
        """The newest minor this server speaks (its hello's ``minor``): :data:`MINOR`, or lower to
        replay the frozen 1.0 golden transcripts (``tests/bridge/test_golden.py``). Fixed at
        construction, so hello and dispatch never disagree."""
        return self._minor

    @property
    def peers(self) -> frozenset[Peer]:
        """The connections open now (a snapshot)."""
        return frozenset(self._peers)

    async def close_all(self) -> None:
        """Close every open connection (shutdown; asyncio's ``wait_closed`` waits for them)."""
        for peer in list(self._peers):
            await peer.close()

    async def shutdown(self) -> None:
        """Close every open connection, and from now on every connection handed to
        :meth:`handle_connection` at once (``ternd``'s shutdown, after the listener is closed).

        A connection the listener accepted before it was closed may reach
        :meth:`handle_connection` only after :meth:`close_all` ran (the accept takes several
        loop iterations); asyncio's ``wait_closed`` waits for it, which without this would last
        until its hello timeout. :meth:`close_all` alone keeps taking new connections (tests drop
        connections and reconnect)."""
        self._shut_down = True
        await self.close_all()

    # -- connection lifecycle -----------------------------------------------------------------

    async def handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer_pid: int
    ) -> None:
        peer = Peer(reader, writer, on_request=self._on_request, on_notification=self._on_notif)
        if self._shut_down:
            log.debug("bridge connection closed: the server is shut down")
            await peer.close()
            return
        self._peers.add(peer)
        try:
            await self._handle(peer, reader, peer_pid)
        finally:
            self._peers.discard(peer)
            await peer.close()

    async def _handle(self, peer: Peer, reader: asyncio.StreamReader, peer_pid: int) -> None:
        try:
            first = await asyncio.wait_for(framing.read_frame(reader), self._hello_timeout_s)
            if first is None:
                await peer.close()
                return
            req = parse_message(framing.decode(first))
        except (TimeoutError, framing.FrameError, ValueError) as exc:
            log.warning(
                "bridge peer sent no valid first frame", extra={"reason": type(exc).__name__}
            )
            await peer.close()
            return
        if not isinstance(req, Request) or req.method != "hello":
            await self._reject(
                peer,
                None,
                BridgeError("invalid_params", "the first message must be a hello request"),
            )
            return
        try:
            hello = validate_params(Hello, req.params)
            if hello.role == "daemon":
                raise BridgeError("role_rejected", "a client cannot take role=daemon")
            if hello.role == "extension":
                await self._admit_extension(peer, hello, peer_pid, req.id)
            else:
                peer.hello = hello
                peer.minor = negotiate_minor(self.minor, hello.minor)
                if hello.role == "mcp-proxy":
                    # Its tools.call caller is mcp:<this id>: one per connection, never reused
                    # (also across restarts), so two clients' windows and audit trails never merge.
                    peer.connection_id = uuid4().hex
                await peer.respond(req.id, dump(self._daemon_hello()))
        except BridgeError as err:
            await self._reject(peer, req.id, err)
            return
        log.info(
            "bridge peer connected",
            extra={"role": hello.role, "minor": peer.minor, "connection_id": peer.connection_id},
        )
        try:
            await peer.run()
        finally:
            if hello.role == "extension":
                await self.link.detach(peer)
            log.info("bridge peer disconnected", extra={"role": hello.role})

    def _daemon_hello(self) -> Hello:
        return Hello(
            protocol=PROTOCOL,
            minor=self.minor,
            role="daemon",
            version=tern.__version__,
            capabilities=[],
        )

    async def _reject(self, peer: Peer, rid: int | str | None, err: BridgeError) -> None:
        log.info("bridge peer rejected", extra={"error": err.name, "detail": err.message})
        try:
            await peer.respond_error(rid, err)
        except BridgeError:
            log.info("bridge rejection not delivered: connection closed")
        await peer.close()

    async def _admit_extension(
        self, peer: Peer, hello: Hello, peer_pid: int, rid: int | str
    ) -> None:
        """Peer check, profile binding, then the one-connection rule; on success reply and take
        the slot."""
        reason = check_extension_peer(
            peer_pid,
            self._info,
            thunderbird_bin=self._thunderbird_bin,
            nmhost_path=self._nmhost_path,
        )
        if reason is not None:
            raise BridgeError("role_rejected", f"extension peer check failed: {reason}")
        if self._bind_profile is not None:
            # Before the liveness ping and the attach: an extension of another TB profile is
            # refused (-32004) and never replaces the live connection.
            # Hello's validator requires profile_id for role=extension; checked again here (not
            # an assert, which ``python -O`` strips) so a None never reaches the binding.
            if hello.profile_id is None:
                raise BridgeError("invalid_params", "extension hello without profile_id")
            self._bind_profile(hello.profile_id)
        async with self._slot:
            old = self.link.active
            if old is not None:
                try:
                    await old.request("bridge.ping", Empty(), timeout=self._liveness_ping_s)
                except BridgeError:
                    log.info("stale extension connection replaced")
                    await old.close()
                else:
                    raise BridgeError(
                        "extension_already_connected", "another extension connection is live"
                    )
            peer.hello = hello
            peer.minor = negotiate_minor(self.minor, hello.minor)
            # Reply before attaching, so the extension sees its hello result before any
            # daemon→extension request.
            await peer.respond(rid, dump(self._daemon_hello()))
            await self.link.attach(peer)

    # -- dispatch -----------------------------------------------------------------------------

    async def _on_request(self, peer: Peer, req: Request) -> Any:
        spec = method_spec(req.method, peer.minor)
        if req.method == "hello":
            raise BridgeError("invalid_params", "hello was already exchanged on this connection")
        if spec.kind != "request" or spec.direction not in ("e2d", "cli2d", "mcp2d", "any"):
            raise BridgeError("method_not_available", f"{req.method} is not served by the daemon")
        if spec.roles is not None and peer.role not in spec.roles:
            raise BridgeError("role_rejected", f"{req.method} is not allowed for role {peer.role}")
        if peer.role == "mcp-proxy" and req.method not in MCP_PROXY_METHODS:
            raise BridgeError("role_rejected", f"{req.method} is not allowed for role mcp-proxy")
        params = validate_params(spec.params, req.params)
        handler = self.handlers.get(req.method)
        if handler is None:
            raise BridgeError("unavailable", f"{req.method} is not wired yet")
        result = await handler(peer, params)
        assert spec.result is not None
        if not isinstance(result, spec.result):
            raise TypeError(f"handler for {req.method} returned {type(result).__name__}")
        return dump(result)

    async def _on_notif(self, peer: Peer, msg: Notification) -> None:
        try:
            spec = method_spec(msg.method, peer.minor)
            if spec.kind != "notification" or spec.direction != "e2d":
                raise BridgeError("method_not_available", f"{msg.method} is not accepted")
            # e2d notifications are Thunderbird events: only the peer-checked extension sends them.
            if peer.role != "extension":
                raise BridgeError("role_rejected", f"{msg.method} is not allowed for {peer.role}")
            params = validate_params(spec.params, msg.params)
        except BridgeError as err:
            log.warning(
                "bridge notification rejected", extra={"error": err.name, "detail": err.message}
            )
            return
        handler = self.notification_handlers.get(msg.method)
        if handler is None:
            log.info("bridge notification not wired yet", extra={"method": msg.method})
            return
        await handler(peer, params)

    # -- built-in handlers --------------------------------------------------------------------

    async def _ping(self, peer: Peer, params: Empty) -> Empty:
        return Empty()

    async def _ping_extension(self, peer: Peer, params: PingExtensionParams) -> PingStats:
        times: list[float] = []
        for _ in range(params.n):
            t0 = perf_counter()
            await self.link.request("bridge.ping", Empty())
            times.append((perf_counter() - t0) * 1000.0)
        times.sort()
        return PingStats(
            n=len(times),
            p50_ms=_percentile(times, 0.50),
            p90_ms=_percentile(times, 0.90),
            p99_ms=_percentile(times, 0.99),
            max_ms=times[-1],
        )

    async def _approvals_decide_placeholder(self, peer: Peer, params: BaseModel) -> BaseModel:
        raise BridgeError("unavailable", "approvals arrive in plan 06b")
