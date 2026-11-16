"""Architecture §5/§6/§7 and decisions #32/#33 describe what plans 06a and 06b built (plan 06a
step 8, plan 06b step 12).

Each check reads the document itself and compares it with the code, so a later change to
``ToolContext``, the error codes, ``ToolMeta``, the scoped bridge, the audit tables, the approval
outcome rows, the engine's limits or the undo entry point that forgets the docs fails here.
Each helper is also run over a planted gap (a missing name, section, bullet, table line or
threat row, and text past the end of the section or bullet it should stop at).
"""

from __future__ import annotations

import contextlib
import dataclasses
import re
import sqlite3
import typing
from collections.abc import Iterable
from pathlib import Path

import pytest

from tern.perms.approvals import OUTCOME_ROWS
from tern.perms.audit import AuditLog
from tern.perms.engine import COUNT_ESCALATION_THRESHOLD, FORBIDDEN_EFFECTS, Limits
from tern.perms.write_status import WriteStatus
from tern.tools import ToolContext, ToolErrorCode, ToolMeta
from tern.tools.context import EFFECT_METHODS
from tern.tools.registry import Registry, ToolResult

REPO = Path(__file__).resolve().parents[2]

DOCS = Path(__file__).resolve().parents[2] / "docs"
ARCHITECTURE = DOCS / "architecture.md"


def section(text: str, heading: str) -> str:
    """The body of the ``## `` section whose heading starts with ``heading``."""
    start = text.index(f"\n{heading}")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def bullet(sec: str, label: str) -> str:
    """The top-level bullet ``- **<label>**`` of a section, with its nested lines."""
    lines = sec.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"- **{label}"):
            body = [line]
            for nxt in lines[i + 1 :]:
                if nxt.startswith("- ") or nxt.startswith("#") or not nxt.strip():
                    break
                body.append(nxt)
            return "\n".join(body)
    raise AssertionError(f"no bullet {label!r}")


def table_line(text: str, table: str) -> str:
    """The nested ``  - `<table>`: ...`` line of a bullet (one audit table's own column list)."""
    for line in text.splitlines():
        if line.lstrip().startswith(f"- `{table}`:"):
            return line
    raise AssertionError(f"no table line {table!r}")


def missing(text: str, names: Iterable[str]) -> list[str]:
    """``names`` that do not appear backticked (alone, or as a call/assignment) in ``text``."""
    found = set(re.findall(r"`([^`]+)`", text))
    heads = {re.split(r"[ (=:]", f, maxsplit=1)[0] for f in found}
    return sorted(n for n in names if n not in found and n not in heads)


def table_columns(path: Path, table: str) -> list[str]:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


ARCH = ARCHITECTURE.read_text(encoding="utf-8")
SEC5 = section(ARCH, "## 5. Tool and plugin contract")
SEC6 = section(ARCH, "## 6. Permission model")
SEC7 = section(ARCH, "## 7. Threat model")


def test_missing_finds_a_planted_gap() -> None:
    assert missing("`index`, `search` (x), `clock=1`", ["index", "search", "clock"]) == []
    assert missing("`index` and search", ["index", "search"]) == ["search"]


def test_section_stops_at_the_next_section_and_refuses_a_missing_one() -> None:
    doc = "intro\n## 1. One\nbody one\n## 2. Two\nbody two\n"
    assert "body one" in section(doc, "## 1.")
    assert "body two" not in section(doc, "## 1.")
    assert "body two" in section(doc, "## 2.")
    with pytest.raises(ValueError):
        section(doc, "## 3.")


def test_bullet_stops_at_the_next_bullet_and_refuses_a_missing_one() -> None:
    sec = "- **A** first\n  - nested a\n- **B** second\n  - nested b\n"
    assert bullet(sec, "A") == "- **A** first\n  - nested a"
    assert "nested b" not in bullet(sec, "A")
    with pytest.raises(AssertionError, match="no bullet"):
        bullet(sec, "C")


def test_table_line_keeps_columns_to_their_own_table() -> None:
    text = "- **Audit log**\n  - `calls`: `call_id`, `ts`;\n  - `events`: `event_id`, `kind`;\n"
    assert missing(table_line(text, "calls"), ["calls", "call_id", "ts"]) == []
    # `call_id`/`ts` appear on the calls line only, so the events line misses them.
    assert missing(table_line(text, "events"), ["events", "call_id", "ts"]) == ["call_id", "ts"]
    with pytest.raises(AssertionError, match="no table line"):
        table_line(text, "undo_tokens")


def test_tool_context_fields_are_listed() -> None:
    fields = [f.name for f in dataclasses.fields(ToolContext)]
    assert fields
    assert missing(bullet(SEC5, "What a tool can reach"), fields) == []


def test_error_codes_are_listed() -> None:
    codes = typing.get_args(ToolErrorCode)
    assert len(codes) == 13  # 06a's seven + the six engine codes of plan 06b step 0
    assert missing(bullet(SEC5, "Errors"), codes) == []


def test_frozen_schema_fields_are_listed() -> None:
    assert missing(bullet(SEC5, "Frozen schema"), ToolMeta.model_fields) == []


def test_scoped_bridge_methods_are_listed() -> None:
    methods = {m for ms in EFFECT_METHODS.values() for m in ms}
    effects = {e.value for e in EFFECT_METHODS}
    text = bullet(SEC5, "What a tool can reach")
    assert missing(text, methods | effects) == []


def test_describe_scope_is_stated() -> None:
    text = bullet(SEC5, "`tern tools describe`")
    for phrase in ("never reads `config.toml`", "never loads config-path plugins", "audit DB"):
        assert phrase in text


def test_audit_tables_are_listed(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    AuditLog.open(db).close()
    text = bullet(SEC6, "Audit log")
    for table in ("calls", "events", "undo_tokens"):
        columns = table_columns(db, table)
        assert columns, table
        assert missing(table_line(text, table), [table, *columns]) == [], table
    assert "**Write-ahead.**" in text


def test_derived_status_is_stated() -> None:
    text = bullet(SEC6, "Write status")
    assert "initial status" in text
    assert "`undone` is a separate derived flag" in text


# -- plan 06b step 12 ---------------------------------------------------------------------------


def threat_row(sec: str, threat: str) -> list[str]:
    """The cells of the §7 table row ``| <threat> | ... |``."""
    for line in sec.splitlines():
        if line.startswith(f"| {threat} |"):
            return [c.strip() for c in line.strip().strip("|").split("|")]
    raise AssertionError(f"no threat row {threat!r}")


def cited_tests(cell: str) -> list[str]:
    """Backticked ``tests/...py`` paths in a table cell."""
    return re.findall(r"`(tests/[^`]+\.py)`", cell)


def test_threat_row_and_cited_tests_on_a_planted_table() -> None:
    sec = "| T1 | a | b | `tests/x/test_a.py`; plan 12 |\n| T10 | c | d | none |\n"
    assert threat_row(sec, "T1")[3] == "`tests/x/test_a.py`; plan 12"
    assert threat_row(sec, "T10")[0] == "T10"  # "T1" does not match the T10 row
    assert cited_tests(threat_row(sec, "T1")[3]) == ["tests/x/test_a.py"]
    assert cited_tests(threat_row(sec, "T10")[3]) == []
    with pytest.raises(AssertionError, match="no threat row"):
        threat_row(sec, "T2")


def test_section_5_write_tools_return_no_undo_token() -> None:
    assert "undo_token" in {f.name for f in dataclasses.fields(ToolResult)}
    example = SEC5[: SEC5.index("```", SEC5.index("```python") + 3)]
    assert "@tool(" in example
    assert not re.search(r"^\s+undo_token\s*:", example, re.MULTILINE)
    table = SEC5[SEC5.index("### Default toolset") :]
    rows = [line for line in table.splitlines() if line.startswith("| `")]
    assert rows
    assert [r for r in rows if "undo_token" in r] == []
    assert "`ToolResult.undo_token`" in SEC5


def test_section_6_states_the_engine_order_and_failed_writes() -> None:
    text = bullet(SEC6, "Engine order")
    codes = ["forbidden_effect", "filters_write", "no_approval_ui", "pending_cap"]
    assert missing(text, codes) == []
    assert missing(text, ["ctx.targets", "ToolResult.undo_token", "targets_changed"]) == []
    assert all(e.value in text for e in FORBIDDEN_EFFECTS)
    assert "A failed write may have partly applied" in text


def test_section_6_lists_every_approval_outcome_row_and_the_limits() -> None:
    text = bullet(SEC6, "Approvals (")
    decisions = {d for d, _ in OUTCOME_ROWS.values()}
    reasons = {r for _, r in OUTCOME_ROWS.values()}
    assert missing(text, decisions | reasons) == []
    assert "No `call` row exists while an approval is pending" in text
    limits = Limits()
    assert f"`Limits.approval_ttl_s = {limits.approval_ttl_s}`" in bullet(SEC6, "Approval binding")
    pending_cap = f"at most {limits.max_pending_per_caller} open per caller"
    assert pending_cap in bullet(SEC6, "Engine order")
    tiers = bullet(SEC6, "Tiers")
    assert f"at most {limits.w1_per_caller} W1 targets per caller" in tiers
    assert f"at most {limits.w1_global} W1 targets per sliding hour" in tiers
    assert f"> {COUNT_ESCALATION_THRESHOLD} targets" in bullet(SEC6, "Count escalation")


def test_section_6_names_the_recheck_the_observer_and_registry_undo() -> None:
    assert "`targets_changed`" in bullet(SEC6, "Execution re-check")
    status = bullet(SEC6, "Write status")
    assert callable(WriteStatus.observe)
    assert missing(status, ["WriteStatus.observe", "Store.location_observers"]) == []
    assert "write_status.stale_writes" in status
    undo = bullet(SEC6, "Undo")
    assert callable(Registry.undo)
    assert missing(undo, ["Registry.undo", "state_changed", "undone", "role_rejected"]) == []
    for phrase in ("**Only caller `ui` may undo**", "**redo token**", "TB is not transactional"):
        assert phrase in undo
    assert "its executed W1 rows (an undo with no move) count in `ui`'s window" in undo
    assert "a W2 undo row does not" in undo
    row = [line for line in ARCH.splitlines() if line.startswith("| e→d | `audit.undo` |")]
    assert len(row) == 1 and row[0].rstrip().endswith("tern's undo row tier: §6 Undo |")


def test_section_7_rows_cite_existing_perms_tests() -> None:
    attacker = "tests/perms/test_attacker_cases.py"
    for threat in ("T1", "T6", "T7", "T10", "T15", "T18", "T21"):
        cited = cited_tests(threat_row(SEC7, threat)[3])
        assert cited, threat
        assert [c for c in cited if not (REPO / c).is_file()] == [], threat
    for threat in ("T1", "T6", "T10", "T15", "T21"):
        assert attacker in cited_tests(threat_row(SEC7, threat)[3]), threat
