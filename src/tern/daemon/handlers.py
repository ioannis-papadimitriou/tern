"""ternd's ``tern-bridge/1.1`` handlers (decisions #37; plan 07 part B step 8, plan 08b's prepared
methods). ``tern.daemon.main`` wires them onto the bridge server.

- ``tools.list`` (role ``mcp-proxy``): ``Daemon.registry.describe()`` as ``tern-tool/1``
  ``ToolMeta`` documents, minus tools that declare a forbidden effect (the call-time refusal stays
  the backstop; the same filter as ``tern.mcp.server.listed``, restated here because ternd never
  imports the MCP SDK).
- ``tools.call`` (role ``mcp-proxy``; :class:`ToolCalls`):
  ``Registry.call(f"mcp:{peer.connection_id}", tool, args, caller_label=client_name)``; the
  connection id is ternd's (assigned at the proxy's hello), the client name a display label only.
  A tool's refusal or failure is result data (``ok: false``, the ToolError's content-free code,
  reason and message), never a bridge error. When the proxy's
  connection closes while a call runs, the call is cancelled (fail closed: nobody can read its
  outcome), which leaves the cancel on its audit row as ``Registry.call`` records it; at
  shutdown ternd cancels and awaits every call still running before the audit log closes.
- ``audit.status`` (any role): the stale-write count (``tern.perms.write_status.stale_writes``)
  and the number of open approval cards; counts only.
- ``ui.open_mid`` (role ``extension``): a citation chip opens its message, as
  ``Registry.call("ui", "open_in_thunderbird", {mid})`` (an audited R1 call); a ToolError becomes
  the :data:`OPEN_ERRORS` bridge error (``unavailable`` for any other code), its tern code and
  reason kept in the message.
- ``daemon.info`` (roles ``extension``, ``cli``): ``{remote_llm, chat_model}`` from the config, so
  the chat tab can show #9's remote-model banner; the connection is marked as having read it
  (``ChatHost.info_read``: remote-model chat needs that mark).

``mcp-proxy`` reaches only ``bridge.ping``, ``tools.list``, ``tools.call`` and ``audit.status``
(``tern.bridge.server.MCP_PROXY_METHODS``, enforced before params and handlers): never approvals,
undo, the audit list, search, chat or the ``ui.*`` methods (06b, step 8 review).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic_core import PydanticSerializationError

from tern import config
from tern.bridge.client import Peer
from tern.bridge.protocol import (
    AuditStatus,
    BridgeError,
    DaemonInfo,
    Empty,
    ErrorName,
    OpenMidParams,
    OpenMidResult,
    ToolCallError,
    ToolCallParams,
    ToolCallResult,
    ToolList,
)
from tern.bridge.server import Handler
from tern.perms.engine import FORBIDDEN_EFFECTS, Engine
from tern.perms.write_status import stale_writes
from tern.tools.contract import ToolError, ToolMeta
from tern.tools.registry import Registry, ToolResult

log = logging.getLogger(__name__)

#: ``open_in_thunderbird``'s ToolError codes -> bridge error names (#36). Any other code
#: (``internal``, ``rate_limited``, ...) is ``unavailable``; the message keeps the tern code.
OPEN_ERRORS: Mapping[str, ErrorName] = {
    "invalid_params": "invalid_params",
    "unknown_mid": "invalid_params",
    "mid_conflicted": "state_changed",
    "unavailable": "unavailable",
    "denied": "role_rejected",
}


def listed(meta: ToolMeta) -> bool:
    """Whether ``tools.list`` offers ``meta``'s tool: not when it declares a forbidden effect."""
    return not (set(meta.effects) & FORBIDDEN_EFFECTS)


def tools_list_handler(registry: Registry) -> Handler:
    async def handle(peer: Peer, params: Empty) -> ToolList:
        return ToolList(tools=[m.model_dump(mode="json") for m in registry.describe() if listed(m)])

    return handle


def tool_call_result(result: ToolResult) -> ToolCallResult:
    """A successful call as ``tools.call`` data. The output is the JSON the MCP server lists in
    ``output_schema`` (``model_dump_json(by_alias=True)``); a tool-authored serializer failing
    after the call ran is an explicit ``internal``/``serialize`` failure (never a partial output;
    the exception message may quote content, so it is neither logged nor returned), still carrying
    the ``call_id``. A write's undo token is never sent to the proxy (undo is the user's, #33)."""
    try:
        output: dict[str, Any] = json.loads(result.output.model_dump_json(by_alias=True))
    except PydanticSerializationError:
        log.warning("tool output not serializable (call %s)", result.call_id)
        return ToolCallResult(
            ok=False,
            untrusted_output=False,
            call_id=result.call_id,
            error=ToolCallError(
                code="internal", reason="serialize", message="cannot serialize the output"
            ),
        )
    return ToolCallResult(
        ok=True,
        output=output,
        untrusted_output=result.untrusted_output,
        call_id=result.call_id,
    )


async def _unless_closed(
    peer: Peer, call: Awaitable[ToolResult], running: set[asyncio.Task[ToolResult]]
) -> ToolResult:
    """Await ``call`` while ``peer`` is open. If the connection closes first, the call is
    cancelled, awaited (so its audit bookkeeping finishes) and ``unavailable`` is raised (no reply
    can be delivered). If this handler is cancelled, so is the call. A call that finished on its
    own (result or exception) is returned or raised as is. The call's task is in ``running``
    until it ends (ternd awaits those at shutdown)."""
    task = asyncio.ensure_future(call)
    running.add(task)
    task.add_done_callback(running.discard)
    watch = asyncio.ensure_future(peer.closed.wait())
    try:
        await asyncio.wait({task, watch}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        watch.cancel()
        if not task.done():
            task.cancel()
            # Not ``await task``: that would turn this handler's own cancellation into the task's.
            await asyncio.wait({task})
    if task.cancelled():
        log.info(
            "tools.call cancelled: its connection closed",
            extra={"connection_id": peer.connection_id},
        )
        raise BridgeError("unavailable", "the connection closed during the call")
    return task.result()


class ToolCalls:
    """The ``tools.call`` handler (:meth:`handle`) and its running calls. :meth:`shutdown`
    cancels and awaits every call still running, so no ``Registry.call`` outlives the audit log
    (ternd calls it once no connection is left, as ``ChatHost.shutdown``)."""

    def __init__(self, registry: Registry) -> None:
        self._registry = registry
        self._running: set[asyncio.Task[ToolResult]] = set()

    @property
    def running(self) -> int:
        return len(self._running)

    async def handle(self, peer: Peer, params: ToolCallParams) -> ToolCallResult:
        # The method table allows only mcp-proxy; a proxy connection always has its id.
        if peer.role != "mcp-proxy" or peer.connection_id is None:
            raise BridgeError("role_rejected", f"tools.call is not allowed for role {peer.role}")
        call = self._registry.call(
            f"mcp:{peer.connection_id}",
            params.tool,
            params.arguments,
            caller_label=params.client_name,
        )
        try:
            result = await _unless_closed(peer, call, self._running)
        except ToolError as exc:
            return ToolCallResult(
                ok=False,
                untrusted_output=False,
                error=ToolCallError(code=exc.code, reason=exc.reason, message=exc.message),
            )
        return tool_call_result(result)

    async def shutdown(self) -> None:
        """Cancel every running call and wait until each has ended (its audit row finished)."""
        tasks = set(self._running)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
            log.info("tools.call calls cancelled at shutdown", extra={"n": len(tasks)})


def audit_status_handler(engine: Engine) -> Handler:
    async def handle(peer: Peer, params: Empty) -> AuditStatus:
        return AuditStatus(
            stale_writes=stale_writes(engine.audit, engine.clock()),
            pending_approvals=len(engine.approvals.pending()),
        )

    return handle


def ui_open_mid_handler(registry: Registry) -> Handler:
    async def handle(peer: Peer, params: OpenMidParams) -> OpenMidResult:
        try:
            result = await registry.call("ui", "open_in_thunderbird", {"mid": params.mid})
        except ToolError as err:
            detail = err.code if err.reason is None else f"{err.code}/{err.reason}"
            log.info("ui.open_mid refused", extra={"code": err.code, "reason": err.reason})
            raise BridgeError(
                OPEN_ERRORS.get(err.code, "unavailable"), f"{err.message} [{detail}]"
            ) from None
        out = result.output.model_dump()
        return OpenMidResult(opened=out["opened"], folder_id=out["folder_id"])

    return handle


def daemon_info_handler(cfg: config.TernConfig, on_read: Callable[[Peer], None]) -> Handler:
    """``on_read(peer)`` runs before the reply: ``ChatHost.info_read``, so remote-model chat is
    allowed only on a connection that has read ``remote_llm`` (its tab shows the banner)."""

    async def handle(peer: Peer, params: Empty) -> DaemonInfo:
        on_read(peer)
        return DaemonInfo(remote_llm=cfg.allow_remote_llm, chat_model=cfg.chat_model)

    return handle
