"""``scripts/agent_eval``: the harness scores real agent turns (FakeLLM over the real client, the
real tools over the synthetic profile) and refuses to open real Thunderbird data."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tern.config import TernConfig
from tern.testing.fake_llm import FakeLLM, FakeToolCall, ScriptStep

EVAL_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agent_eval"


def _load(name: str) -> ModuleType:
    sys.path.insert(0, str(EVAL_DIR))
    try:
        spec = importlib.util.spec_from_file_location(f"agent_eval_{name}", EVAL_DIR / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(EVAL_DIR))


build = _load("build_snapshot")
run = _load("run")

MIDS = run.manifest_mids()
INV = MIDS["inv-0042"]
REMINDER = MIDS["inv-0042-reminder"]
INJ = MIDS["inj-direct"]
TASKS = {t.id: t for t in run.load_tasks()}


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Any:
    try:
        yield build.build_world(tmp_path_factory.mktemp("agent-eval") / "work")
    finally:
        build.release_guard()


def _run(world: Any, task_id: str, *steps: ScriptStep) -> tuple[Any, dict[str, Any]]:
    fake = FakeLLM.from_steps(*steps)
    cfg = world.config(chat_model=fake.model_id, agent_max_tool_calls=15)
    task = TASKS[task_id]
    build.guard_real_data()
    try:
        rec = run.run_task(
            world,
            cfg,
            task,
            0,
            date(2026, 11, 17),
            lambda c: run.TimedLLM(c, transport=fake.transport),
        )
    finally:
        build.release_guard()
    fake.assert_done()
    return rec, run.score(task, rec)


def test_every_task_resolves_its_fixture_keys() -> None:
    assert len(TASKS) >= 20
    for task in TASKS.values():
        assert len(task.cite_mids) == len(task.cite_keys)
        assert task.cite_min <= len(task.cite_mids)


def test_a_correct_cited_answer_scores_success(world: Any) -> None:
    rec, s = _run(
        world,
        "a01",
        ScriptStep(tool_calls=[FakeToolCall(name="tb_search", arguments={"query": "INV-0042"})]),
        ScriptStep(content=f"EUR 1,240.00, due 15 April 2026 [{INV[:12]}] [{REMINDER[:12]}]."),
    )
    assert rec.error is None
    [turn] = rec.turns
    assert [c["tool"] for c in turn.tools] == ["tb_search"]
    assert turn.tools[0]["status"] == "ok"
    assert set(turn.citations) == {INV, REMINDER}
    assert s["success"] and s["cite_hits"] == 2 and s["hallucinated_citations"] == 0


def test_a_hallucinated_citation_and_a_wrong_answer_fail(world: Any) -> None:
    rec, s = _run(
        world,
        "a01",
        ScriptStep(tool_calls=[FakeToolCall(name="tb_search", arguments={"query": "INV-0042"})]),
        ScriptStep(content=f"EUR 1,999, due soon [{INV[:12]}] [{'d' * 12}]."),
    )
    assert not s["success"]
    assert not s["answer_ok"] and s["missing_groups"]
    assert s["hallucinated_citations"] == 1 and not s["cite_ok"]


def test_no_tool_call_fails_tools_ok_and_canaries_are_scored_by_the_warning(world: Any) -> None:
    _, s = _run(world, "a16", ScriptStep(content="No, I couldn't find anything from Contoso."))
    assert s["answer_ok"] and s["cite_ok"] and not s["tools_ok"] and not s["success"]
    _, s = _run(
        world,
        "a08",
        ScriptStep(tool_calls=[FakeToolCall(name="tb_search", arguments={"query": "helpdesk"})]),
        ScriptStep(content=f"It is suspicious: it asks me to reply CANARY-INJ-1 [{INJ[:12]}]."),
    )
    assert s["canary_echoed"] and s["answer_ok"] and s["correct"]
    _, s = _run(
        world,
        "a08",
        ScriptStep(tool_calls=[FakeToolCall(name="tb_search", arguments={"query": "helpdesk"})]),
        ScriptStep(content="CANARY-INJ-1"),
    )
    assert s["canary_echoed"] and not s["answer_ok"] and not s["success"]
    _, s = _run(
        world,
        "a03",
        ScriptStep(tool_calls=[FakeToolCall(name="tb_search", arguments={"query": "Northwind"})]),
        ScriptStep(content="INV-0027, INV-0057, INV-0073 and INV-0013."),
    )
    assert s["forbidden_hits"] == ["0013"] and not s["answer_ok"]


def test_metrics_aggregate_the_rows(world: Any) -> None:
    rec, _ = _run(
        world,
        "a01",
        ScriptStep(tool_calls=[FakeToolCall(name="tb_search", arguments={"query": "INV-0042"})]),
        ScriptStep(content=f"EUR 1,240.00, due 15 April 2026 [{INV[:12]}]."),
    )
    rows = [run._row(TASKS["a01"], rec)]  # noqa: SLF001
    m = run.metrics([TASKS["a01"]], rows)
    assert m["n_runs"] == 1 and m["success_rate"] == 1.0
    assert m["per_task"]["a01"]["success"] == 1
    assert m["tool_calls_total"] == 1


def test_the_guard_refuses_real_thunderbird_paths(tmp_path: Path) -> None:
    build.guard_real_data()
    try:
        with pytest.raises(build.IsolationError):
            open(Path.home() / ".thunderbird" / "profiles.ini")  # noqa: SIM115
        with pytest.raises(build.IsolationError):
            open(Path.home() / ".cache" / "thunderbird-mcp" / "snapshot.sqlite")  # noqa: SIM115
        (tmp_path / "ok.txt").write_text("synthetic")  # elsewhere is fine
    finally:
        build.release_guard()
    assert TernConfig().tb_profile_dir is None  # the eval sets it explicitly, never the default
