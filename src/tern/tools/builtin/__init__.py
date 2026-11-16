"""Built-in tools: the R0/R1 read tools in ``read.py`` (plan 06a step 5) and the thunderbird-mcp
snapshot tools in ``tb.py`` (vendored ``tern.tbmcp``; decision of 2026-11-16).

Built-ins are registered explicitly from :data:`BUILTINS`, not as ``pyproject.toml`` entry points
(plan 06a amendment 14): a stale editable install must never silently drop a built-in.
"""

from tern.tools.builtin.read import READ_TOOLS
from tern.tools.builtin.tb import CHAT_TOOL_NAMES, TB_TOOL_NAMES, TBMCP_TOOLS
from tern.tools.contract import ToolSpec

#: Every built-in tool, registered by ``tern.tools.registry`` with origin ``builtin``.
BUILTINS: tuple[ToolSpec, ...] = (*READ_TOOLS, *TBMCP_TOOLS)

__all__ = ["BUILTINS", "CHAT_TOOL_NAMES", "TB_TOOL_NAMES"]
