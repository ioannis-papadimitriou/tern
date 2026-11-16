"""Architecture §1/§3/§5, decisions #34/#37, `docs/mcp-clients.md` and the integration checklist
describe what plan 07 built (part A: step 6; part B: step 10).

Each check reads the document and, where the text states a behaviour, compares it with the
code (`tern.mcp.server`), so a later change to the MCP mapping that forgets the docs fails here.
The helpers are also run over planted gaps.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from mcp.server import Server
from mcp.types import TextContent

from tern.mcp.server import (
    UNTRUSTED_META_KEY,
    ToolFailure,
    ToolOutcome,
    build_server,
    call_result,
    tool_annotations,
)
from tern.perms.engine import FORBIDDEN_EFFECTS
from tern.tools.contract import Tier, ToolMeta

DOCS = Path(__file__).resolve().parents[2] / "docs"
ARCH = (DOCS / "architecture.md").read_text(encoding="utf-8")
CLIENTS = (DOCS / "mcp-clients.md").read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """The body of the ``## `` section whose heading starts with ``heading``."""
    start = text.index(f"\n{heading}")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def bullet(sec: str, label: str) -> str:
    """The top-level bullet ``- **<label>`` (or checklist item ``- [ ] **<label>``) of a
    section, with its nested lines."""
    lines = sec.splitlines()
    for i, line in enumerate(lines):
        if line.startswith((f"- **{label}", f"- [ ] **{label}")):
            body = [line]
            for nxt in lines[i + 1 :]:
                if nxt.startswith("- ") or nxt.startswith("#") or not nxt.strip():
                    break
                body.append(nxt)
            return "\n".join(body)
    raise AssertionError(f"no bullet {label!r}")


def table_rows(sec: str, needle: str) -> list[str]:
    """The table lines (``| ... |``) of a section that contain ``needle``."""
    return [line for line in sec.splitlines() if line.startswith("| ") and needle in line]


def test_helpers_on_planted_text() -> None:
    doc = "intro\n## 1. One\n- **A** a\n  - nested\n- [ ] **B** b\n| x | plan 7 |\n## 2. Two\nz\n"
    one = section(doc, "## 1.")
    assert "z" not in one
    assert bullet(one, "A") == "- **A** a\n  - nested"
    assert bullet(one, "B") == "- [ ] **B** b\n| x | plan 7 |"
    assert table_rows(one, "plan 7") == ["| x | plan 7 |"]
    assert table_rows(one, "plan 8") == []
    with pytest.raises(AssertionError, match="no bullet"):
        bullet(one, "C")
    with pytest.raises(ValueError):
        section(doc, "## 3.")


# -- architecture §1 ---------------------------------------------------------------------------


class _NoTools:
    def describe(self) -> list[ToolMeta]:
        return []

    async def call(
        self, tool: str, args: Mapping[str, Any], *, client_name: str | None
    ) -> ToolOutcome:
        raise AssertionError("not called")


def test_section_1_names_the_low_level_server_and_why() -> None:
    assert type(build_server(_NoTools())) is Server  # the low-level class, as §1 says
    rows = table_rows(section(ARCH, "## 1. Components"), "| `tern mcp` |")
    assert len(rows) == 1
    row = rows[0]
    for phrase in (
        "low-level `mcp.server.Server`, not `MCPServer`",
        "never reach `Registry.call`",
        "#10 write-ahead",
        "stdio only",
        "role `mcp-proxy`",
        "caller `mcp:<proxy connection id>`",
        "listen on a network transport",
        "`tests/mcp/test_transport.py`",
        "#34",
    ):
        assert phrase in row, phrase
    assert "SDK 2.x `MCPServer`)" not in row  # the old wording that named the wrong class


# -- architecture §3 ---------------------------------------------------------------------------


def test_section_3_has_the_frozen_1_1_table_matching_the_method_table() -> None:
    """Part B froze 1.1 (#37): §3's table lists exactly the 1.1 methods, with the directions and
    roles of ``tern.bridge.protocol.METHODS``; the planned rows moved one minor up."""
    from tern.bridge.protocol import METHODS
    from tern.bridge.server import MCP_PROXY_METHODS

    sec = section(ARCH, "## 3. Bridge protocol")
    assert "**`tern-bridge/1.1` (frozen: `contracts/tern-bridge-1.1.schema.json`" in sec
    minor_1 = {n for n, spec in METHODS.items() if spec.minor == 1}
    for name in minor_1:
        [row] = table_rows(sec, f"| `{name}` |")
        spec = METHODS[name]
        direction = {"mcp2d": "mcp→d", "e2d": "e→d"}[spec.direction]
        assert row.startswith(f"| {direction} |"), name
        if spec.roles is None:
            assert "any role" in row, name
        elif len(spec.roles) == 1:
            assert f"role `{next(iter(spec.roles))}` only" in row, name
        else:
            assert all(f"`{r}`" in row for r in spec.roles), name
    [call_row] = table_rows(sec, "| `tools.call` |")
    assert "undo_token" not in call_row and "no undo token" in call_row
    assert "**Minors are numbered in freeze order**" in sec
    assert "plan 07 part B froze 1.1 first" in sec
    assert "`contacts.lookup` is 1.2, triage 1.3, contacts 1.4" in sec
    assert len(table_rows(sec, "| 1.2 | plan 10 | d→e `contacts.lookup`")) == 1
    for method in sorted(MCP_PROXY_METHODS):
        assert f"`{method}`" in bullet(sec, "`mcp-proxy` peers"), method


# -- architecture §5 ---------------------------------------------------------------------------


def test_section_5_annotations_match_the_server() -> None:
    text = bullet(section(ARCH, "## 5. Tool and plugin contract"), "MCP mapping")
    for tier in (Tier.R0, Tier.R1):
        assert tool_annotations(tier).read_only_hint is True
    assert "R0/R1 → `readOnlyHint=True`" in text
    w1 = tool_annotations(Tier.W1)
    assert (w1.read_only_hint, w1.destructive_hint) == (False, False)
    assert "W1 → `readOnlyHint=False`, `destructiveHint=False`" in text
    for tier in (Tier.W2, Tier.W3):
        assert tool_annotations(tier).destructive_hint is True
    assert "W2/W3 → `destructiveHint=True`" in text
    assert all(tool_annotations(t).open_world_hint is False for t in Tier)
    assert "`openWorldHint=False` for every tool" in text


def test_section_5_untrusted_errors_and_filter_match_the_server() -> None:
    text = bullet(section(ARCH, "## 5. Tool and plugin contract"), "MCP mapping")
    assert f'`_meta {{"{UNTRUSTED_META_KEY}": true}}`' in text
    untrusted = call_result(ToolOutcome(output={"hits": []}, untrusted_output=True))
    assert untrusted.structured_content is None and untrusted.meta == {UNTRUSTED_META_KEY: True}
    assert "text only (no `structured_content`)" in text
    assert "`untrusted.wrap()`" in text

    refused = call_result(ToolOutcome(error=ToolFailure("denied", "refused")))
    assert refused.is_error and refused.structured_content is None
    block = refused.content[0]
    assert isinstance(block, TextContent)
    assert list(json.loads(block.text)["error"]) == ["code", "reason", "message"]
    assert '`{"error": {"code", "reason", "message"}}`' in text
    assert "`is_error=True`" in text

    assert FORBIDDEN_EFFECTS
    assert all(f"`{e.value}`" in text for e in FORBIDDEN_EFFECTS)
    assert "`FORBIDDEN_EFFECTS`" in text and "left out of the MCP tool list" in text


def test_section_5_scopes_the_cancel_to_the_in_process_backend() -> None:
    # Over the bridge a per-request cancel does not reach ternd (#37: no 1.1 cancel method), so
    # §5 must not claim a client-side cancel cancels the call in general.
    from tern.bridge.protocol import METHODS

    text = bullet(section(ARCH, "## 5. Tool and plugin contract"), "MCP mapping")
    assert "A client-side cancel cancels the in-process call" not in text  # old, unscoped
    assert "In process (`RegistryBackend`), a client-side cancel cancels the call" in text
    assert "only closing the proxy connection cancels its in-flight calls" in text
    assert "a per-request MCP cancel is not forwarded" in text
    assert "runs if the user later approves its card" in text
    assert "1.1 has no cancel method, #37" in text
    assert not any("cancel" in name and spec.minor == 1 for name, spec in METHODS.items())


# -- decisions #34 -----------------------------------------------------------------------------


# -- integration checklist ---------------------------------------------------------------------


# -- decisions #37, docs/mcp-clients.md (part B, step 10) -----------------------------------------


def test_mcp_clients_doc_matches_the_proxy() -> None:
    """`docs/mcp-clients.md` states what `tern.mcp.proxy` and `tern mcp` do."""
    import tern.mcp.proxy as proxy

    assert '"args": ["mcp"]' in CLIENTS
    assert '"env": {"TERN_HOME": ' in CLIENTS
    assert "A-9" in CLIENTS and "remote model" in CLIENTS
    assert "stdio only" in CLIENTS.lower()
    assert proxy.TERND_GONE in CLIENTS
    assert "mcp: unavailable: no ternd listening" in CLIENTS
    assert "does **not** forward" in CLIENTS
    assert f'"{UNTRUSTED_META_KEY}": true' in CLIENTS
    assert '`{"error": {"code", "reason", "message"}}`' in CLIENTS
    # the long-wait sentence names the expiry plus a minute: CALL_TIMEOUT_S
    assert proxy.CALL_TIMEOUT_S == 600 + 60
    assert "expires (10 minutes" in CLIENTS and "plus a minute" in CLIENTS
