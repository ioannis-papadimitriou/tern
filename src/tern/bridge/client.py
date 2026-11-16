"""JSON-RPC endpoints over a framed stream, and the daemon's link to the extension.

``Peer`` is one connection (either side). ``ExtensionLink`` is how daemon code sends
daemon→extension requests: it waits up to 30 s for an extension to (re)connect, then fails
with ``unavailable`` (decisions #3, round-2 amendment); a request in flight on a connection
that drops fails with ``unavailable`` at once. Nothing is retried silently.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from tern.bridge import framing
from tern.bridge.protocol import (
    METHODS,
    BridgeError,
    ErrorResponse,
    Hello,
    Notification,
    Request,
    Response,
    Role,
    dump,
    method_spec,
    parse_message,
)

log = logging.getLogger(__name__)

RequestHandler = Callable[["Peer", Request], Awaitable[Any]]
NotificationHandler = Callable[["Peer", Notification], Awaitable[None]]

WAIT_FOR_EXTENSION_S = 30.0
REQUEST_TIMEOUT_S = 120.0


class ProtocolViolation(RuntimeError):
    """The other side sent something the protocol does not allow (a bug, not a user error)."""


class Peer:
    """One JSON-RPC connection. Call ``run()`` to read; it returns when the connection ends."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        hello: Hello | None = None,
        minor: int = 0,
        on_request: RequestHandler | None = None,
        on_notification: NotificationHandler | None = None,
        max_inbound: int = framing.INBOUND_MAX,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.hello = hello
        self.minor = minor
        #: Set by the daemon at an ``mcp-proxy`` hello (``uuid4().hex``, plan 07 step 8): the
        #: ``<id>`` of the connection's tool caller ``mcp:<id>``. None for every other peer.
        self.connection_id: str | None = None
        self._on_request = on_request
        self._on_notification = on_notification
        self._max_inbound = max_inbound
        self._ids = itertools.count(1)
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.closed = asyncio.Event()

    @property
    def role(self) -> Role | None:
        return None if self.hello is None else self.hello.role

    async def _send(self, obj: dict[str, Any]) -> None:
        if self.closed.is_set():
            raise BridgeError("unavailable", "connection closed")
        self._writer.write(framing.frame(framing.encode(obj)))
        try:
            await self._writer.drain()
        except ConnectionError:
            await self.close()
            raise BridgeError("unavailable", "connection closed while sending") from None

    async def request(
        self, method: str, params: BaseModel | dict[str, Any], *, timeout: float | None = None
    ) -> Any:
        """Send a request and return the raw ``result``; a JSON-RPC error raises ``BridgeError``."""
        rid = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        body = dump(params) if isinstance(params, BaseModel) else params
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": body})
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise BridgeError("unavailable", f"no reply to {method} within {timeout} s") from None
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: BaseModel | dict[str, Any]) -> None:
        body = dump(params) if isinstance(params, BaseModel) else params
        await self._send({"jsonrpc": "2.0", "method": method, "params": body})

    async def respond(self, rid: int | str, result: Any) -> None:
        await self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    async def respond_error(self, rid: int | str | None, err: BridgeError) -> None:
        await self._send({"jsonrpc": "2.0", "id": rid, "error": dump(err.to_rpc())})

    async def run(self) -> None:
        try:
            while True:
                payload = await framing.read_frame(self._reader, self._max_inbound)
                if payload is None:
                    break
                await self._dispatch(framing.decode(payload))
        except (framing.FrameError, ValueError, ConnectionError) as exc:
            log.warning("bridge connection dropped", extra={"reason": type(exc).__name__})
        finally:
            await self.close()

    async def _dispatch(self, obj: object) -> None:
        msg = parse_message(obj)
        if isinstance(msg, Response | ErrorResponse):
            fut = self._pending.get(msg.id) if msg.id is not None else None
            if fut is None or fut.done():
                log.warning("bridge response for an unknown request id")
                return
            if isinstance(msg, Response):
                fut.set_result(msg.result)
            else:
                fut.set_exception(BridgeError.from_rpc(msg.error))
            return
        if isinstance(msg, Request):
            task = asyncio.create_task(self._serve(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        if self._on_notification is None:
            log.warning("bridge notification dropped: no handler", extra={"method": msg.method})
            return
        await self._on_notification(self, msg)

    async def _serve(self, req: Request) -> None:
        try:
            if self._on_request is None:
                raise BridgeError("method_not_available", f"{req.method} is not served here")
            result = await self._on_request(self, req)
        except BridgeError as err:
            await self._try_send_error(req.id, err)
            return
        except Exception as exc:
            # A handler bug: report it to the caller as an error and log it. The exception text
            # and traceback can carry mail content (a handler's locals, a parser's message), so
            # ERROR gets the type only and the traceback goes to DEBUG (plan 02 review, plan 03).
            # The connection stays usable; nothing is swallowed.
            log.error(
                "bridge handler failed",
                extra={"method": req.method, "error": type(exc).__name__},
            )
            log.debug("bridge handler traceback", exc_info=True, extra={"method": req.method})
            await self._try_send_error(req.id, BridgeError("unavailable", "internal error"))
            return
        try:
            await self.respond(req.id, result)
        except BridgeError:
            log.info("bridge reply not delivered: connection closed", extra={"method": req.method})

    async def _try_send_error(self, rid: int | str, err: BridgeError) -> None:
        try:
            await self.respond_error(rid, err)
        except BridgeError:
            log.info("bridge error reply not delivered: connection closed")

    async def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(BridgeError("unavailable", "connection dropped"))
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except ConnectionError as exc:
            # Already reset by the other side; the connection is closed either way.
            log.debug("bridge close after reset", extra={"reason": type(exc).__name__})


class ExtensionRequester(Protocol):
    """What daemon code needs from the extension: ``ExtensionLink`` in production,
    ``tern.testing.fake_bridge.FakeBridge`` in tests (plan 03 step 4)."""

    async def request(self, method: str, params: BaseModel) -> Any: ...

    async def notify(self, method: str, params: BaseModel) -> None: ...


class ExtensionLink:
    """The daemon's single extension connection (architecture §3)."""

    def __init__(
        self,
        *,
        wait_s: float = WAIT_FOR_EXTENSION_S,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
    ) -> None:
        self.wait_s = wait_s
        self.request_timeout_s = request_timeout_s
        self._active: Peer | None = None
        self._changed = asyncio.Condition()
        #: Called (synchronously, in order) after every attach; e.g. the ingest service starts
        #: or resumes its backfill there (plan 04 step 9). They run in the hello handler after
        #: the success reply went out, so they must be quick and must not raise. One that raises
        #: is a bug: the exception propagates out of the hello handler, the server's connection
        #: handler closes the peer (so ``active`` is None), and the rest are not called.
        self.on_attach: list[Callable[[], None]] = []

    @property
    def active(self) -> Peer | None:
        if self._active is not None and self._active.closed.is_set():
            return None
        return self._active

    async def attach(self, peer: Peer) -> None:
        async with self._changed:
            self._active = peer
            self._changed.notify_all()
        for callback in list(self.on_attach):
            callback()

    async def detach(self, peer: Peer) -> None:
        async with self._changed:
            if self._active is peer:
                self._active = None
            self._changed.notify_all()

    async def _wait(self) -> Peer:
        async def ready() -> Peer:
            async with self._changed:
                await self._changed.wait_for(lambda: self.active is not None)
                peer = self.active
                assert peer is not None
                return peer

        try:
            return await asyncio.wait_for(ready(), self.wait_s)
        except TimeoutError:
            raise BridgeError(
                "unavailable", f"no extension connected within {self.wait_s} s"
            ) from None

    async def request(self, method: str, params: BaseModel) -> Any:
        """A daemon→extension request; returns the validated result model."""
        spec = METHODS.get(method)
        if spec is None or spec.kind != "request" or spec.direction not in ("d2e", "any"):
            raise ValueError(f"{method} is not a daemon→extension request")
        peer = await self._wait()
        method_spec(method, peer.minor)
        raw = await peer.request(method, params, timeout=self.request_timeout_s)
        assert spec.result is not None
        try:
            return spec.result.model_validate(raw)
        except ValidationError as exc:
            raise ProtocolViolation(f"extension returned a malformed {method} result") from exc

    async def notify(self, method: str, params: BaseModel) -> None:
        spec = METHODS.get(method)
        if spec is None or spec.kind != "notification" or spec.direction != "d2e":
            raise ValueError(f"{method} is not a daemon→extension notification")
        peer = await self._wait()
        method_spec(method, peer.minor)
        await peer.notify(method, params)
