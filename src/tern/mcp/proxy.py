"""``tern mcp``: the stdio MCP server process for local MCP clients (plan 07 step 9, #34/#37).

It connects to the running ``ternd`` over its bridge socket (``paths.resolve().socket``, so
``TERN_HOME`` relocates it) with role ``mcp-proxy`` and serves
:func:`tern.mcp.server.run_stdio` over :class:`BridgeBackend`. Every tool call is ternd's
``tools.call``: ternd runs it through ``Registry.call`` (permission engine + audit) as
``mcp:<connection id>``, an id ternd assigns at this process's hello; the MCP client's name is a
display label only. This process holds no registry, no index and no audit log, and it never starts
a ternd: no ternd, no proxy (:class:`ProxyError`).

- The tool list is fetched once at start (``tools.list``, each entry validated as ``ToolMeta``);
  restart the client to see plugins ternd loaded later.
- A tool's refusal or failure arrives as data and becomes the MCP ``is_error`` result. A bridge
  error on ``tools.call`` (ternd going away, a role or params refusal) is returned the same way as
  ``unavailable`` with the bridge error's name as reason; nothing is retried.
- An MCP-side cancel of one request is not forwarded (tern-bridge 1.1 has no cancel method, #37):
  the call keeps running in ternd, and a W2/W3 call stays pending until its card is decided or
  expires. Closing this process (the client closing stdin) closes the connection, and ternd
  cancels every call still running on it.
- When ternd closes the connection, this process exits 1 at once (the client sees the server
  exit), rather than serving tools that can only fail.
- A ``tools.call`` reply that takes longer than :data:`CALL_TIMEOUT_S` (the approval lifetime
  plus a margin) is an ``unavailable`` outcome.

stdout carries only the MCP protocol; logs go to stderr.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

from pydantic import ValidationError

import tern
from tern.bridge.client import Peer
from tern.bridge.protocol import (
    MINOR,
    PROTOCOL,
    BridgeError,
    Hello,
    ToolCallResult,
    ToolList,
    negotiate_minor,
)
from tern.mcp.server import ToolFailure, ToolOutcome, run_stdio
from tern.perms.engine import Limits
from tern.tools.contract import ToolMeta

log = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 10.0
LIST_TIMEOUT_S = 30.0
#: The minor that has ``tools.list``/``tools.call``.
TOOLS_MINOR = 1
#: The longest wait for a ``tools.call`` reply: a W2/W3 call waits for the user's decision, at most
#: the engine's approval lifetime, plus a margin. A ternd that never answers ends the request with
#: an ``unavailable`` outcome instead of hanging the client.
CALL_TIMEOUT_S = float(Limits().approval_ttl_s + 60)


class ProxyError(Exception):
    """``tern mcp`` cannot serve: ternd is missing, too old, or went away. The message names no
    mail content."""


class NoTernd(ProxyError):
    """Nothing listens on the bridge socket (connecting failed): ``tern mcp`` never starts one."""


def proxy_hello() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "minor": MINOR,
        "role": "mcp-proxy",
        "version": tern.__version__,
        "capabilities": [],
    }


class BridgeBackend:
    """A ``ToolBackend`` over one ``mcp-proxy`` bridge connection to ternd. Build it with
    :meth:`connect` (it fetches the tool list)."""

    def __init__(self, peer: Peer, tools: list[ToolMeta]) -> None:
        self._peer = peer
        self._tools = list(tools)

    @classmethod
    async def connect(cls, peer: Peer) -> BridgeBackend:
        """Fetch ``tools.list`` over ``peer`` (hello already exchanged at minor >= 1)."""
        raw = await peer.request("tools.list", {}, timeout=LIST_TIMEOUT_S)
        try:
            listing = ToolList.model_validate(raw)
            tools = [ToolMeta.model_validate(doc) for doc in listing.tools]
        except ValidationError as exc:
            # Field locations and error types only, never values.
            where = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['type']}"
                for e in exc.errors(include_input=False, include_url=False)
            )
            raise ProxyError(f"ternd sent a malformed tools.list ({where})") from None
        return cls(peer, tools)

    def describe(self) -> list[ToolMeta]:
        return list(self._tools)

    async def call(
        self, tool: str, args: Mapping[str, Any], *, client_name: str | None
    ) -> ToolOutcome:
        params: dict[str, Any] = {"tool": tool, "arguments": dict(args)}
        if client_name is not None:
            params["client_name"] = client_name
        try:
            raw = await self._peer.request("tools.call", params, timeout=CALL_TIMEOUT_S)
        except BridgeError as err:
            log.warning("tools.call bridge error", extra={"error": err.name})
            return ToolOutcome(error=ToolFailure("unavailable", err.message, err.name))
        # A malformed reply is a ternd bug: ValidationError propagates (the SDK answers the MCP
        # request with a JSON-RPC error), never a guessed outcome.
        res = ToolCallResult.model_validate(raw)
        if res.error is not None:
            failure = ToolFailure(res.error.code, res.error.message, res.error.reason)
            return ToolOutcome(error=failure, call_id=res.call_id)
        return ToolOutcome(
            output=res.output, untrusted_output=res.untrusted_output, call_id=res.call_id
        )


async def open_peer(sock: Path) -> tuple[Peer, asyncio.Task[None]]:
    """Connect to ternd and say hello as ``mcp-proxy``; refuse a ternd without the tools minor.
    :class:`NoTernd` when nothing listens at ``sock`` (only the connect is mapped; any later
    ``OSError`` is not "no ternd")."""
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
    except (FileNotFoundError, ConnectionRefusedError):
        raise NoTernd(f"no ternd listening at {sock}") from None
    peer = Peer(reader, writer)
    task = asyncio.create_task(peer.run())
    admitted = False
    try:
        raw = await peer.request("hello", proxy_hello(), timeout=HELLO_TIMEOUT_S)
        try:
            daemon = Hello.model_validate(raw)
        except ValidationError:
            raise ProxyError("ternd sent a malformed hello") from None
        minor = negotiate_minor(MINOR, daemon.minor)
        if minor < TOOLS_MINOR:
            raise ProxyError(
                f"ternd speaks tern-bridge 1.{daemon.minor}; tern mcp needs 1.{TOOLS_MINOR}"
            )
        peer.minor = minor
        admitted = True
    finally:
        if not admitted:  # refused, failed or cancelled: close before the error propagates
            await peer.close()
            await task
    return peer, task


#: What ``tern mcp`` writes to stderr before it exits 1 because ternd went away.
TERND_GONE = "mcp: ternd closed the connection"


def _exit_ternd_gone() -> NoReturn:
    """End the process at once (exit 1) after ternd dropped the connection. Not a cancel: the
    SDK reads stdin in a worker thread that a cancel cannot interrupt, so the process would linger
    until the client's next line (step 9 review). stdout carries only the protocol; the reason
    goes to stderr."""
    sys.stderr.write(TERND_GONE + "\n")
    sys.stderr.flush()
    os._exit(1)


async def serve_proxy(
    sock: Path, *, on_ternd_gone: Callable[[], NoReturn] = _exit_ternd_gone
) -> None:
    """Serve MCP on stdin/stdout over ternd at ``sock`` until the client closes stdin (returns).
    If ternd closes the connection first, ``on_ternd_gone`` ends the process (exit 1; tests pass
    their own)."""
    peer, reader = await open_peer(sock)
    try:
        backend = await BridgeBackend.connect(peer)
        log.info("tern mcp serving", extra={"tools": len(backend.describe())})
        serving = asyncio.create_task(run_stdio(backend))
        closed = asyncio.create_task(peer.closed.wait())
        try:
            await asyncio.wait({serving, closed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            closed.cancel()
        if not serving.done():
            on_ternd_gone()
        serving.result()  # the client closed stdin (or run_stdio raised)
    finally:
        await peer.close()
        await reader
