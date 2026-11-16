"""Agent loop and untrusted-text wrapping (plan 08a).

Kept import-light (stdlib only): ``tern.agent.untrusted`` is a leaf module that
``tern.mcp.server`` imports, and importing it runs this file first (#34).
"""

from __future__ import annotations

from importlib import resources

#: The system prompt's resource, relative to this package (plan 08a step 2a; plan 09's
#: ``configs/eval/toolcalls.toml`` names the same file).
SYSTEM_PROMPT_RESOURCE = "prompts/system.md"


def system_prompt() -> str:
    """The chat system prompt (plan 08a step 2a; rewritten in plan 08c step 6: short-id
    citations, fewer tool calls, triage hints), read from the installed package."""
    return resources.files(__name__).joinpath(SYSTEM_PROMPT_RESOURCE).read_text(encoding="utf-8")
