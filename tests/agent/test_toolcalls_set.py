"""The frozen tool-calling set ``toolcalls-v1`` (plan 08a step 6, M25, A12, A13).

``evals/synthetic/toolcalls-v1.jsonl`` is a byte-for-byte copy of S3's ``spikes/s3/tasks.jsonl``
(``s3-tasks`` v1). These tests check that the set still fits the real tools and the synthetic
mailbox: every tool a step names is a built-in (with every argument key a property of its input
schema) or a write tool listed in ``PLANNED`` for a later plan; every expected mid and destination
folder is in the fixture's MANIFEST; ``configs/eval/toolcalls.toml`` pins the set's sha256 and the
measured settings. The set is never edited to pass them (research integrity): a failure here means
a tool or the fixture drifted from the set, and the fix is a new set version in a plan.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from tern.bridge.protocol import Mid
from tern.tools.registry import builtin_tools

ROOT = Path(__file__).resolve().parents[2]
SET_PATH = ROOT / "evals" / "synthetic" / "toolcalls-v1.jsonl"
CONFIG_PATH = ROOT / "configs" / "eval" / "toolcalls.toml"
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "synthetic_mailbox" / "MANIFEST.json"
#: The ``s3-tasks`` v1 sha256 recorded by every S3 run (``spikes/s3/REPORT.md``, details block).
S3_TASKS_V1_SHA256 = "847128a8bbffe8689a1558493fc7e59d21588e077bad46c338dfd69cd0498b28"

#: Write tools the set measures before they exist (A13): tool -> its input keys as architecture §5's
#: tool table lists them. Once a tool is among the built-ins, its real input schema is checked
#: instead and its entry here is no longer used. The plans that add them (D11; informational, not
#: checked): save_draft and set_tags -> plan 10, move_messages -> plan 12.
PLANNED: dict[str, frozenset[str]] = {
    "save_draft": frozenset({"to", "cc", "subject", "body", "reply_to_mid", "identity_id"}),
    "set_tags": frozenset({"mids", "add", "remove"}),
    "move_messages": frozenset({"mids", "dest_folder_id", "source_folder_id"}),
}
#: Argument keys whose values are mids (S3 records their fixture keys under ``keys``).
MID_KEYS = frozenset({"mid", "mids", "reply_to_mid"})
MID: TypeAdapter[str] = TypeAdapter(Mid)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ArgRule(_M):
    key: str
    op: Literal["contains_any", "in", "equals", "exact_set", "superset"]
    values: Any
    keys: list[str | None] | None = None


class CallSpec(_M):
    tool: str
    args: list[ArgRule] = []


class Step(_M):
    any: list[CallSpec] = Field(min_length=1)


class MidRef(_M):
    key: str | None
    mid: str


class Citations(_M):
    min: int = Field(ge=1)
    mids: list[MidRef] = Field(min_length=1)


class Task(_M):
    """S3's task shape (``spikes/s3/score.py``), strict: an unknown field fails."""

    id: str
    kind: str
    lang: Literal["en", "el"]
    prompt: str = Field(min_length=1)
    order: Literal["listed", "any"]
    steps: list[Step] = Field(min_length=1)
    citations: Citations | None
    answer: list[list[str]]
    max_calls: int = Field(ge=1, le=12)


def load_tasks(path: Path) -> list[Task]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert all(line.strip() for line in lines), "blank line in the set"
    return [Task.model_validate(json.loads(line)) for line in lines]


@pytest.fixture(scope="module")
def tasks() -> list[Task]:
    """The set, loaded per test module rather than at import, so a missing or malformed set fails
    each check that needs it by name instead of as a collection error."""
    return load_tasks(SET_PATH)


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def builtin_keys() -> dict[str, frozenset[str]]:
    """Each built-in tool's input properties (by alias, as the model sees them)."""
    return {
        t.spec.name: frozenset(t.meta().input_schema.get("properties", {})) for t in builtin_tools()
    }


def calls(tasks: list[Task]) -> list[tuple[str, CallSpec]]:
    return [(t.id, c) for t in tasks for s in t.steps for c in s.any]


def tool_problems(
    tasks: list[Task],
    builtins: dict[str, frozenset[str]],
    planned: dict[str, frozenset[str]],
) -> list[str]:
    """Every call naming a tool that is neither built in nor planned, or an argument key its tool
    does not take. A built-in is always checked against its own schema, planned or not."""
    problems: list[str] = []
    for task_id, call in calls(tasks):
        if call.tool in builtins:
            allowed = builtins[call.tool]
        elif call.tool in planned:
            allowed = planned[call.tool]
        else:
            problems.append(f"{task_id}: unknown tool {call.tool}")
            continue
        problems += [
            f"{task_id}: {call.tool} has no argument {r.key}"
            for r in call.args
            if r.key not in allowed
        ]
    return problems


def as_list(values: Any) -> list[Any]:
    return list(values) if isinstance(values, list) else [values]


def manifest_folder_hashes(manifest: dict[str, Any]) -> set[str]:
    return {f["hash"] for a in manifest["accounts"] for f in a["folders"]}


def test_the_set_is_the_s3_tasks_v1_file_byte_for_byte() -> None:
    assert hashlib.sha256(SET_PATH.read_bytes()).hexdigest() == S3_TASKS_V1_SHA256


def test_twenty_tasks_parse_with_unique_ids(tasks: list[Task]) -> None:
    ids = [t.id for t in tasks]
    assert len(ids) == 20
    assert len(set(ids)) == len(ids)
    assert {t.lang for t in tasks} == {"en", "el"}


def test_every_tool_and_argument_key_is_built_in_or_planned(tasks: list[Task]) -> None:
    assert tool_problems(tasks, builtin_keys(), PLANNED) == []


def test_from_is_checked_by_its_alias(tasks: list[Task]) -> None:
    """``search_messages``' ``from_`` field is offered as ``from``; t04 and t18 use it."""
    assert "from" in builtin_keys()["search_messages"]
    assert any(r.key == "from" for _, c in calls(tasks) for r in c.args)


def test_every_planned_tool_is_used_by_the_set(tasks: list[Task]) -> None:
    used = {c.tool for _, c in calls(tasks)}
    assert set(PLANNED) <= used


@pytest.mark.parametrize(
    ("call", "builtins", "problem"),
    [
        ({"tool": "send_mail", "args": []}, {}, "t: unknown tool send_mail"),
        (
            {"tool": "search_messages", "args": [{"key": "sender", "op": "equals", "values": "x"}]},
            {"search_messages": frozenset({"query", "from"})},
            "t: search_messages has no argument sender",
        ),
        (
            {"tool": "set_tags", "args": [{"key": "tag", "op": "equals", "values": "x"}]},
            {},
            "t: set_tags has no argument tag",
        ),
        # A planned tool that became a built-in is checked against the built-in's schema only.
        (
            {"tool": "save_draft", "args": [{"key": "body", "op": "equals", "values": "x"}]},
            {"save_draft": frozenset({"to", "text"})},
            "t: save_draft has no argument body",
        ),
    ],
)
def test_the_tool_check_can_fail(
    call: dict[str, Any], builtins: dict[str, frozenset[str]], problem: str
) -> None:
    task = Task.model_validate(
        {
            "id": "t",
            "kind": "probe",
            "lang": "en",
            "prompt": "synthetic",
            "order": "listed",
            "steps": [{"any": [call]}],
            "citations": None,
            "answer": [],
            "max_calls": 8,
        }
    )
    assert tool_problems([task], builtins, PLANNED) == [problem]


def test_every_expected_mid_is_a_manifest_message_under_its_key(
    tasks: list[Task], manifest: dict[str, Any]
) -> None:
    by_mid = {m["mid"]: m["key"] for m in manifest["messages"] if m["mid"] is not None}
    refs: list[tuple[str | None, str]] = []
    for t in tasks:
        if t.citations is not None:
            refs += [(r.key, r.mid) for r in t.citations.mids]
    for _, call in calls(tasks):
        for r in call.args:
            if r.key not in MID_KEYS:
                assert r.keys is None, f"{r.key}: fixture keys on a non-mid argument"
                continue
            values = as_list(r.values)
            assert r.keys is not None and len(r.keys) == len(values), r.key
            refs += list(zip(r.keys, values, strict=True))
    assert refs, "the set names no mid"
    for key, mid in refs:
        MID.validate_python(mid)
        assert mid in by_mid, f"{key}: mid not in MANIFEST messages"
        if key is not None:
            assert by_mid[mid] == key, f"{key}: MANIFEST names this mid {by_mid[mid]}"


def test_every_dest_folder_id_is_a_manifest_folder_hash(
    tasks: list[Task], manifest: dict[str, Any]
) -> None:
    dests = [
        v
        for _, call in calls(tasks)
        for r in call.args
        if r.key == "dest_folder_id"
        for v in as_list(r.values)
    ]
    assert dests, "the set moves nothing"
    assert not set(dests) - manifest_folder_hashes(manifest), (
        "dest_folder_id not a MANIFEST folder hash"
    )


def test_the_config_pins_the_set_and_the_measured_settings() -> None:
    cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    es = cfg["eval_set"]
    assert (es["name"], es["version"]) == ("toolcalls", "1")
    assert ROOT / es["path"] == SET_PATH
    assert es["sha256"] == hashlib.sha256(SET_PATH.read_bytes()).hexdigest()
    run = cfg["run"]
    assert {k: run[k] for k in ("temperature", "seed", "repeats", "timeout_s", "max_tokens")} == {
        "temperature": 0.0,
        "seed": 0,
        "repeats": 3,
        "timeout_s": 120,
        "max_tokens": 4096,
    }
    assert run["system_prompt"] == "src/tern/agent/prompts/system.md"
    assert (ROOT / run["system_prompt"]).is_file()
    assert cfg["model"] == {"id": "ternary-bonsai-2-27b", "context_length": 32768}
