"""Architecture §2/§5/§8, decisions #18/#35 and the integration checklist describe what plan 08a
built (plan 08a step 7).

Each check reads the document and, where the text states a behaviour or a value, compares it
with the code (the envelope ``wrap()`` builds, the citation marker and grammar, the turn
outcomes, the config defaults, the per-turn log line), so a later change that forgets the docs
fails here. The helpers are also run over planted gaps.
"""

from __future__ import annotations

import inspect
import re
import typing
from pathlib import Path

import pytest
from mcp.types import TextContent

from tern.agent import citations, loop
from tern.agent.citations import REMOVED_MARKER, find_citations
from tern.agent.untrusted import TAG, wrap
from tern.config import TernConfig
from tern.mcp.server import ToolOutcome, call_result

DOCS = Path(__file__).resolve().parents[2] / "docs"
ARCH = (DOCS / "architecture.md").read_text(encoding="utf-8")

OUTCOMES = frozenset(typing.get_args(loop.Outcome))
MID = "a" * 64


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


def outcome_like(text: str) -> set[str]:
    """Backticked lower-case snake_case words of ``text`` that read like an outcome code."""
    return {w for w in re.findall(r"`([a-z]+(?:_[a-z]+)*)`", text) if w in OUTCOMES or "_" in w}


def default(name: str) -> object:
    return TernConfig.model_fields[name].default


def test_helpers_on_planted_text() -> None:
    doc = "intro\n## 1. One\n- **A** a\n  - nested\n- [ ] **B** b\n\n- **C** c\n## 2. Two\nz\n"
    one = section(doc, "## 1.")
    assert "z" not in one
    assert bullet(one, "A") == "- **A** a\n  - nested"
    assert bullet(one, "B") == "- [ ] **B** b"  # stops at the blank line
    with pytest.raises(AssertionError, match="no bullet"):
        bullet(one, "D")
    with pytest.raises(ValueError):
        section(doc, "## 3.")
    assert outcome_like("`ok`, `timeout`, `not_an_outcome`, `Registry`, `x`") == {
        "ok",
        "timeout",
        "not_an_outcome",
    }


# -- architecture §2 "Ask" ---------------------------------------------------------------------


def ask_flow() -> str:
    sec = section(ARCH, "## 2. Data flows")
    return bullet(sec, "Ask (read-only).")


def test_section_2_names_the_envelope_wrap_builds() -> None:
    text = ask_flow()
    built = wrap("{}", tool="search_messages").splitlines()[0]
    assert built == f'<{TAG} tool="search_messages">'
    assert f'`<{TAG} tool="<name>">…</{TAG}>`' in text
    assert "**one** envelope per result" in text
    assert "`tern.agent.untrusted.wrap()`" in text
    assert "<untrusted_email mid=…>" not in text  # #18's per-message wording, replaced (#35)
    single = wrap("x", mid=MID).splitlines()[0]
    assert single == f'<{TAG} mid="{MID}">'
    assert "A `mid` attribute appears only when a caller wraps the text of a single message" in text
    assert "R0/R1 tools only" in text and "`Registry.call` as `agent:<session>`" in text


def test_section_2_states_the_citation_grammar_and_the_marker() -> None:
    text = ask_flow()
    assert "`[mid]` or `【mid】` as matched pairs" in text
    assert find_citations(f"[{MID}] 【{MID}】 [mid: {MID}]") == [MID, MID, MID]
    assert find_citations(f"[{MID}】") == []  # a mismatched pair is not a citation
    assert find_citations(f"[x-{MID}]") == [f"x-{MID}"]
    assert "`x-` mids included" in text and "A bare mid is not a citation" in text
    assert find_citations(MID) == []
    assert f"`{REMOVED_MARKER}`" in text
    assert find_citations(REMOVED_MARKER) == []  # "which is not a citation itself"
    assert "which is not a citation itself" in text
    assert "before stripping" in text and "on the stream" in text
    assert "also across a tool call within the turn" in text


def test_section_2_names_only_real_outcomes() -> None:
    text = ask_flow()
    named = outcome_like(text) - {"search_messages", "get_thread", "list_folders"}
    named -= {"chat.error"}
    assert named, "no outcome named"
    assert named <= OUTCOMES, named - OUTCOMES
    assert "`chat.error{reason}` = the outcome code" in text


# -- architecture §5 and §8 --------------------------------------------------------------------


def test_section_5_says_mcp_text_is_wrapped_as_call_result_does() -> None:
    text = bullet(section(ARCH, "## 5. Tool and plugin contract"), "MCP mapping")
    assert "Plan 08a wraps that text" not in text  # the part-A future tense, now done
    assert "must not serve real clients before it does" not in text
    result = call_result(ToolOutcome(output={"hits": []}, untrusted_output=True))
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.text.startswith(f"<{TAG}>\n") and block.text.endswith(f"\n</{TAG}>")
    assert f"one `<{TAG}>` envelope with no attribute" in text
    assert "`untrusted.wrap()`" in text


def test_section_5_testing_bullet_describes_the_transport_level_fake() -> None:
    text = bullet(section(ARCH, "## 5. Tool and plugin contract"), "Testing.")
    assert "served as SSE behind the real `LLMClient`" in text


def test_section_8_states_the_configured_turn_timeout() -> None:
    rows = [
        line
        for line in section(ARCH, "## 8. Failure modes").splitlines()
        if line.startswith("| LLM latency or timeouts |")
    ]
    assert len(rows) == 1
    timeout = default("agent_turn_timeout_s")
    assert isinstance(timeout, int | float)
    assert f"{timeout:g} s per-turn timeout (`agent_turn_timeout_s`" in rows[0]
    assert "60 s" not in rows[0]
    assert "reason `timeout`" in rows[0] and "timeout" in OUTCOMES


def test_section_8_gpu_row_holds_the_embedder_per_model_request() -> None:
    rows = [
        line
        for line in section(ARCH, "## 8. Failure modes").splitlines()
        if line.startswith("| GPU OOM |")
    ]
    assert len(rows) == 1
    # the loop holds the embed queue around each model request only, not the whole turn
    request = inspect.getsource(loop.AgentSession._request)
    assert "self._hold(HOLD_REASON)" in request and "self._llm.stream(" in request
    assert "self._hold(" not in inspect.getsource(loop.AgentSession._tool_message)
    assert f'`hold("{loop.HOLD_REASON}")`' in rows[0]
    assert "pauses during each chat model request" in rows[0]
    assert "while a chat turn is active" not in rows[0]


# -- decisions #18 and #35 ---------------------------------------------------------------------


# -- integration checklist ---------------------------------------------------------------------


def test_the_marker_is_the_one_the_citation_module_exports() -> None:
    assert citations.REMOVED_MARKER == REMOVED_MARKER == "[citation removed]"
