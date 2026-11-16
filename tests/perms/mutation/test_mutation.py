"""The permission-engine mutation harness (M31, plan 06b step 9).

The full run (baseline + 13 mutants on a copy under ``$TMPDIR``, about a minute) is marked
``slow`` (``make test-slow``). The harness's own rules (exact anchors, the import-path check,
outcome classification) are pinned by fast tests, so a source change that leaves a mutant's
anchor stale fails ``make check-fast`` at once instead of only under ``make test-slow``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from . import run
from .run import (
    MUTANTS,
    REPO,
    Edit,
    HarnessError,
    Mutant,
    Report,
    RunResult,
    StaleAnchor,
)

#: Plan 06b step 9's list, in its order (M31's eight, then the AFK review's three), then step
#: 11's forbidden_effect refusal (step 11 review round 1), then the merge review's execution
#: re-check of a conflicted mid.
EXPECTED = [
    "engine_check",
    "count_escalation",
    "recipient_escalation",
    "caller_window",
    "global_ceiling",
    "binding_recheck",
    "execution_recheck",
    "undo_state_check",
    "conflicted_refusal",
    "approval_expiry",
    "subset_check",
    "forbidden_effect",
    "recheck_conflicted",
]


# -- fast: the mutant list and its anchors -------------------------------------------------------


def test_the_mutants_are_the_plans_list() -> None:
    assert [m.name for m in MUTANTS] == EXPECTED


@pytest.mark.parametrize("mutant", MUTANTS, ids=lambda m: m.name)
def test_every_anchor_occurs_exactly_once_in_the_working_tree(mutant: Mutant) -> None:
    new = run.check_anchors(REPO, mutant)
    for path, text in new.items():
        assert text != path.read_text(encoding="utf-8")
        assert path.resolve().is_relative_to((REPO / "src").resolve())


@pytest.mark.parametrize("mutant", MUTANTS, ids=lambda m: m.name)
def test_every_mutant_touches_the_engine_or_the_registry(mutant: Mutant) -> None:
    for edit in mutant.edits:
        assert edit.path.startswith(("src/tern/perms/", "src/tern/tools/registry.py"))
        assert edit.old != edit.new


# -- fast: exact anchors ------------------------------------------------------------------------


def _tree(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "src" / "tern" / "perms" / "m.py"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def _mutant(*edits: Edit) -> Mutant:
    return Mutant("m", "test mutant", edits)


def test_apply_and_restore(tmp_path: Path) -> None:
    path = _tree(tmp_path, "a\ncheck()\nb\n")
    originals = run.apply(tmp_path, _mutant(Edit("src/tern/perms/m.py", "check()\n", "pass\n")))
    assert path.read_text(encoding="utf-8") == "a\npass\nb\n"
    run.restore(originals)
    assert path.read_text(encoding="utf-8") == "a\ncheck()\nb\n"


@pytest.mark.parametrize("text", ["a\nb\n", "check()\ncheck()\n"], ids=["missing", "twice"])
def test_an_anchor_that_does_not_occur_exactly_once_is_stale(tmp_path: Path, text: str) -> None:
    path = _tree(tmp_path, text)
    with pytest.raises(StaleAnchor, match="occurs [02] times, expected 1"):
        run.apply(tmp_path, _mutant(Edit("src/tern/perms/m.py", "check()", "pass")))
    assert path.read_text(encoding="utf-8") == text


def test_a_stale_second_anchor_writes_nothing(tmp_path: Path) -> None:
    path = _tree(tmp_path, "one()\ntwo()\n")
    good = Edit("src/tern/perms/m.py", "one()", "pass")
    stale = Edit("src/tern/perms/m.py", "three()", "pass")
    with pytest.raises(StaleAnchor):
        run.apply(tmp_path, _mutant(good, stale))
    assert path.read_text(encoding="utf-8") == "one()\ntwo()\n"


def test_an_edit_that_changes_nothing_is_refused(tmp_path: Path) -> None:
    _tree(tmp_path, "check()\n")
    with pytest.raises(StaleAnchor, match="changes nothing"):
        run.apply(tmp_path, _mutant(Edit("src/tern/perms/m.py", "check()", "check()")))


def test_run_all_checks_every_anchor_before_copying(tmp_path: Path) -> None:
    stale = _mutant(Edit("src/tern/perms/engine.py", "no such line in the engine\n", "pass\n"))
    with pytest.raises(StaleAnchor):
        run.run_all([stale], work=tmp_path)
    assert list(tmp_path.iterdir()) == []


def _toy_repo(tmp_path: Path) -> Path:
    """A repo with the files ``make_copy`` copies and one module the toy mutant edits."""
    repo = tmp_path / "repo"
    _tree(repo, "a\ncheck()\nb\n")
    (repo / "tests").mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    return repo


def test_a_file_left_mutated_after_a_run_is_a_harness_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _toy_repo(tmp_path)
    work = tmp_path / "work"
    work.mkdir()

    def passing(root: Path, modules: Sequence[str], *, stop_first: bool, tag: str) -> RunResult:
        return RunResult(exit_code=0, tests=3, failed=())

    def no_restore(originals: dict[Path, str]) -> None:
        return None

    monkeypatch.setattr(run, "run_perms", passing)
    monkeypatch.setattr(run, "restore", no_restore)
    mutant = _mutant(Edit("src/tern/perms/m.py", "check()\n", "pass\n"))
    with pytest.raises(HarnessError, match=r"src/tern/perms/m.py was not restored after m"):
        run.run_all([mutant], repo=repo, work=work)
    assert (repo / "src/tern/perms/m.py").read_text(encoding="utf-8") == "a\ncheck()\nb\n"
    assert list(work.iterdir()) == []


def test_module_name() -> None:
    assert run.module_name("src/tern/tools/registry.py") == "tern.tools.registry"
    with pytest.raises(ValueError):
        run.module_name("tests/perms/test_gate.py")


# -- fast: the import-path check ----------------------------------------------------------------


def test_modules_inside_the_copy_pass(tmp_path: Path) -> None:
    src = tmp_path / "src" / "tern" / "perms"
    mods = {"tern.perms.engine": str(src / "engine.py")}
    run.check_module_paths(mods, tmp_path, ["tern.perms.engine"])


def test_a_module_from_the_working_tree_is_refused(tmp_path: Path) -> None:
    mods = {
        "tern.perms.engine": str(tmp_path / "src" / "tern" / "perms" / "engine.py"),
        "tern.tools.registry": str(REPO / "src" / "tern" / "tools" / "registry.py"),
    }
    with pytest.raises(HarnessError, match="tern.tools.registry from outside the copy"):
        run.check_module_paths(mods, tmp_path, ["tern.perms.engine"])


def test_a_module_the_run_did_not_report_is_refused(tmp_path: Path) -> None:
    mods = {"tern.perms.engine": str(tmp_path / "src" / "tern" / "perms" / "engine.py")}
    with pytest.raises(HarnessError, match="did not report these modules: tern.tools.registry"):
        run.check_module_paths(mods, tmp_path, ["tern.tools.registry"])


def test_parse_modules_reads_only_the_driver_lines() -> None:
    out = "....\nMUTATION-MODULE\ttern.perms.undo\t/x/src/tern/perms/undo.py\n1 passed\n"
    assert run.parse_modules(out) == {"tern.perms.undo": "/x/src/tern/perms/undo.py"}


def test_the_run_puts_only_the_copy_on_the_path(tmp_path: Path) -> None:
    env = run.env_for(tmp_path)
    assert env["PYTHONPATH"] == str(tmp_path / "src")
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


# -- fast: diagnostics --------------------------------------------------------------------------


def test_output_tail_keeps_the_last_lines_of_both_streams() -> None:
    stdout = "MUTATION-MODULE\ttern.perms.undo\t/x.py\n" + "".join(f"o{i}\n" for i in range(9))
    stderr = "\n".join(f"e{i}" for i in range(9)) + "\n\n"
    tail = run.output_tail(stdout, stderr)
    assert tail == "stderr: e3 | e4 | e5 | e6 | e7 | e8; stdout: o3 | o4 | o5 | o6 | o7 | o8"
    assert run.output_tail("", "") == ""
    assert len(run.output_tail("", "x" * 1000)) == len("stderr: ") + run.TAIL_WIDTH


def test_the_driver_reports_an_import_failure() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", run._DRIVER, "tern.perms.no_such_module", "--co"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == run.IMPORT_FAILED_EXIT
    assert "MUTATION-IMPORT-FAILED\ttern.perms.no_such_module\tModuleNotFoundError" in proc.stderr


def test_a_mutant_that_breaks_an_import_is_an_error_with_its_cause(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    shutil.copytree(REPO / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    broken = _mutant(
        Edit("src/tern/perms/engine.py", "    return _COUNT_STEP[base]\n", "    return (\n")
    )
    run.apply(root, broken)
    result = run.run_perms(root, ["tern.perms.engine"], stop_first=True, tag="broken")
    assert result.exit_code == run.IMPORT_FAILED_EXIT
    status, detail = run.classify(result)
    assert status == "error"
    assert "failed to import" in detail
    assert "MUTATION-IMPORT-FAILED\ttern.perms" in detail and "SyntaxError" in detail


def _fake_subprocess(monkeypatch: pytest.MonkeyPatch, code: int, stdout: str, stderr: str) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr=stderr)

    fake = SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(run, "subprocess", fake)


def test_a_failed_import_path_check_carries_the_runs_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_subprocess(monkeypatch, 3, "", "Traceback (most recent call last):\nboom: planted\n")
    with pytest.raises(HarnessError, match="did not report these modules.*boom: planted"):
        run.run_perms(tmp_path / "tree", ["tern.perms.engine"], stop_first=True, tag="t")


def test_an_error_outcome_carries_the_runs_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    line = f"MUTATION-MODULE\ttern.perms.engine\t{root / 'src/tern/perms/engine.py'}\n"
    _fake_subprocess(monkeypatch, 4, line + "ERROR: usage planted\n", "warn planted\n")
    result = run.run_perms(root, ["tern.perms.engine"], stop_first=True, tag="t")
    status, detail = run.classify(result)
    assert status == "error"
    assert detail == (
        "no JUnit report (pytest exit 4) [stderr: warn planted; stdout: ERROR: usage planted]"
    )


# -- fast: outcomes -----------------------------------------------------------------------------


def _result(code: int | None, tests: int = 3, failed: tuple[str, ...] = ()) -> RunResult:
    return RunResult(exit_code=code, tests=tests, failed=failed, report=code is not None)


@pytest.mark.parametrize(
    ("result", "status"),
    [
        (_result(1, failed=("tests.perms.test_gate::test_x",)), "killed"),
        (_result(0), "survived"),
        (_result(1, failed=("tests.tools.test_call::test_x",)), "error"),
        (_result(1), "error"),
        (_result(2, failed=("tests.perms.test_gate::test_x",)), "error"),
        (_result(5, tests=0), "error"),
        (_result(None), "error"),
        (RunResult(exit_code=1, tests=0, failed=(), report=False), "error"),
        (_result(0, tests=0), "error"),
    ],
    ids=[
        "perms-failure",
        "clean-pass",
        "failure-outside-perms",
        "exit1-no-failure",
        "collection-error",
        "no-tests",
        "timeout",
        "no-report",
        "pass-without-tests",
    ],
)
def test_only_a_failing_perms_test_kills(result: RunResult, status: str) -> None:
    assert run.classify(result)[0] == status


def test_parse_junit(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<?xml version="1.0"?><testsuites><testsuite tests="3">'
        '<testcase classname="tests.perms.test_a" name="test_ok"/>'
        '<testcase classname="tests.perms.test_a" name="test_bad[x]"><failure/></testcase>'
        '<testcase classname="tests.perms.test_b" name="test_err"><error/></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert run.parse_junit(junit) == (
        3,
        ("tests.perms.test_a::test_bad[x]", "tests.perms.test_b::test_err"),
    )


# -- slow: the full run -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> Report:
    return run.run_all(work=tmp_path_factory.mktemp("mutation"))


@pytest.mark.slow  # ~1 min: baseline + 12 runs of tests/perms on a copy
def test_the_unmutated_copy_passes_from_the_copy(report: Report) -> None:
    assert report.baseline.exit_code == 0
    assert report.baseline.tests > 0 and report.baseline.failed == ()
    src = report.baseline.modules["tern.perms.engine"]
    assert "tern-mutation-" in src and not Path(src).is_relative_to(REPO / "src")


@pytest.mark.slow  # shares the module-scoped run above
@pytest.mark.parametrize("name", EXPECTED)
def test_every_mutant_is_killed(report: Report, name: str) -> None:
    (outcome,) = [o for o in report.outcomes if o.mutant.name == name]
    assert outcome.status == "killed", f"{name} ({outcome.mutant.what}): {outcome.detail}"
