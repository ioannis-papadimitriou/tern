"""``RegistryBackend``: the MCP server's in-process backend over a ``Registry`` (plan 07).

Used by tests and the smoke run only; ``tern mcp`` (plan 07 part B) never imports it: its
backend reaches ternd's registry over the bridge. Every call goes through ``Registry.call``
(permission engine + audit); this module has no other path to a tool.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from pydantic_core import PydanticSerializationError

from tern.mcp.server import ToolFailure, ToolOutcome
from tern.perms.audit import CALLER_RE
from tern.tools.contract import ToolError, ToolMeta
from tern.tools.registry import Registry

log = logging.getLogger(__name__)


class RegistryBackend:
    """A ``ToolBackend`` running every call as ``Registry.call(caller, tool, args,
    caller_label=client_name)``. ``caller`` must be an ``mcp:<id>`` caller id."""

    def __init__(self, registry: Registry, caller: str) -> None:
        if not isinstance(registry, Registry):
            raise TypeError("registry must be a Registry")
        if not isinstance(caller, str) or not caller.startswith("mcp:"):
            raise ValueError("an MCP backend's caller must be mcp:<id>")
        if not CALLER_RE.fullmatch(caller):
            raise ValueError("invalid caller id (mcp:<id>)")
        self._registry = registry
        self._caller = caller

    @property
    def caller(self) -> str:
        return self._caller

    def describe(self) -> list[ToolMeta]:
        return self._registry.describe()

    async def call(
        self, tool: str, args: Mapping[str, Any], *, client_name: str | None
    ) -> ToolOutcome:
        try:
            result = await self._registry.call(self._caller, tool, args, caller_label=client_name)
        except ToolError as exc:
            return ToolOutcome(error=ToolFailure(exc.code, exc.message, exc.reason))
        try:
            output: dict[str, Any] = json.loads(result.output.model_dump_json(by_alias=True))
        except PydanticSerializationError:
            # A tool-authored serializer failed after the call ran: an explicit error for the
            # client, never a partial output. The audit row keeps ``decision=auto`` with no
            # event (the registry saw a successful call); the failure is visible only to the
            # client and in this WARNING. The exception message may quote content, so it is
            # neither logged nor returned. A write's undo token is still passed on.
            log.warning("tool output not serializable (call %s)", result.call_id)
            failure = ToolFailure("internal", "cannot serialize the output", "serialize")
            return ToolOutcome(error=failure, call_id=result.call_id, undo_token=result.undo_token)
        return ToolOutcome(
            output=output,
            untrusted_output=result.untrusted_output,
            call_id=result.call_id,
            undo_token=result.undo_token,
        )
