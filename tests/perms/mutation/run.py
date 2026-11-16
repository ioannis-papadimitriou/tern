"""Mutation harness for the permission engine (M31, plan 06b step 9).

Each mutant removes one security check from the engine or the registry. The harness applies the
mutants one at a time to a **copy** of ``src/`` + ``tests/`` (+ ``pyproject.toml``, the pytest
config) under ``$TMPDIR`` and runs ``tests/perms`` there; a mutant counts as **killed** only when
at least one ``tests/perms`` test fails. The working tree is never changed.

Hardening (AFK review amendment 13), so a pass can never be vacuous:

- **Exact anchors.** A mutant is a list of exact source replacements. Each anchor must occur
  exactly once in its file (``str.count(old) == 1``); otherwise :class:`StaleAnchor` is raised
  before anything is written. A mutant whose anchor no longer matches never counts as killed.
- **Import-path check.** The run sets ``PYTHONPATH=<copy>/src`` and runs pytest through a driver
  in the same interpreter that first imports every ``tern.perms.*`` module and every mutated
  module and prints its ``__file__``. Every printed path must lie inside ``<copy>/src``
  (:func:`check_module_paths`), so the tests provably ran against the mutated copy and not the
  editable install of the working tree.
- **Baseline.** The unmutated copy must pass ``tests/perms`` (at least one test, no failure)
  before any mutant runs.
- **Outcomes.** ``killed`` needs pytest's "tests failed" exit (1) *and* a failing test id under
  ``tests/perms`` in the JUnit report; a clean pass is ``survived``; anything else (collection
  error, usage error, timeout, missing report) is ``error``. Neither ``survived`` nor ``error``
  counts as killed. A module that fails to import (a mutant that breaks the source) ends the run
  before pytest with exit :data:`IMPORT_FAILED_EXIT` and is ``error``, with the import error.
- Diagnostics. The last lines of the child's stderr and stdout are kept on :class:`RunResult` and
  added to every ``error`` detail and to the :class:`HarnessError` of a failed import-path check.
- Bytecode is never written in the copy (``PYTHONDONTWRITEBYTECODE=1``), so a restored file can
  never be shadowed by a mutant's stale ``.pyc``; after each mutant the copy's files are restored
  and compared with the working tree.

Usage: ``uv run python tests/perms/mutation/run.py [--only NAME ...] [--list] [--keep]``; exit 0
when the baseline passes and every mutant selected is killed, 1 otherwise. ``test_mutation.py``
runs it under ``make test-slow``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

#: The repository root (``tests/perms/mutation/run.py`` -> 3 levels up).
REPO = Path(__file__).resolve().parents[3]

#: What is copied: the package, the tests and the pytest config they need.
COPIED_DIRS = ("src", "tests")
COPIED_FILES = ("pyproject.toml",)

#: The suite every mutant must fail (the harness's own directory excluded).
PERMS_TESTS = "tests/perms"
PERMS_CLASS_PREFIX = "tests.perms."

#: Seconds per pytest run before it counts as ``error`` (never as killed).
RUN_TIMEOUT_S = 600

#: The prefix of the driver's module lines on stdout.
MODULE_LINE = "MUTATION-MODULE\t"

#: The driver's exit code when a module fails to import (pytest's own codes are 0-5).
IMPORT_FAILED_EXIT = 97

#: The prefix of the driver's import-failure line on stderr.
IMPORT_FAILED_LINE = "MUTATION-IMPORT-FAILED\t"

#: How many trailing lines of each stream a :class:`RunResult` keeps, and their maximum width.
TAIL_LINES = 6
TAIL_WIDTH = 300

Status = Literal["killed", "survived", "error"]


class StaleAnchor(Exception):
    """A mutant's anchor does not occur exactly once in its file: the source moved on and the
    mutant must be updated (never counted as killed)."""


class HarnessError(Exception):
    """The harness cannot vouch for a result: the copy is not what the run imports, the baseline
    fails, or a restore did not put the original back."""


@dataclass(frozen=True, slots=True)
class Edit:
    """One exact replacement: ``old`` (occurring exactly once in ``path``) becomes ``new``."""

    path: str
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class Mutant:
    name: str
    what: str
    edits: tuple[Edit, ...]


_REGISTRY = "src/tern/tools/registry.py"
_ENGINE = "src/tern/perms/engine.py"
_RECIPIENTS = "src/tern/perms/recipients.py"
_RATELIMIT = "src/tern/perms/ratelimit.py"
_APPROVALS = "src/tern/perms/approvals.py"
_TARGETS = "src/tern/perms/targets.py"
_UNDO = "src/tern/perms/undo.py"

#: The mutants of plan 06b step 9 (8 from M31, 3 added by the AFK review), in the plan's order,
#: then step 11's ``forbidden_effect`` (its review round 1).
MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        "engine_check",
        "Registry.call skips the engine's approval gate: a W2/W3 call runs as auto",
        (
            Edit(
                _REGISTRY,
                "        if engine is not None and tier.rank >= Tier.W2.rank:\n"
                "            inp, live = await self._approve(",
                "        if False:  # MUTANT engine_check\n"
                "            inp, live = await self._approve(",
            ),
        ),
    ),
    Mutant(
        "count_escalation",
        "count_tier never steps a write tier up (more than 20 targets stays at its tier)",
        (Edit(_ENGINE, "    return _COUNT_STEP[base]\n", "    return base  # MUTANT\n"),),
    ),
    Mutant(
        "recipient_escalation",
        "the stub provider (DenyAllRecipients) answers every address as known",
        (
            Edit(
                _RECIPIENTS,
                "    async def is_known(self, addr: str) -> bool:\n        return False\n",
                "    async def is_known(self, addr: str) -> bool:\n        return True  # MUTANT\n",
            ),
        ),
    ),
    Mutant(
        "caller_window",
        "the per-caller W1 window is never checked",
        (
            Edit(
                _RATELIMIT,
                "        if self.used(caller, since=since) + self.reserved(caller) + need"
                " > self._per_caller:\n",
                "        if False:  # MUTANT caller_window\n",
            ),
        ),
    ),
    Mutant(
        "global_ceiling",
        "the global W1 ceiling is never checked",
        (
            Edit(
                _RATELIMIT,
                "        if self.used(since=since) + self._total + need > self._global:\n",
                "        if False:  # MUTANT global_ceiling\n",
            ),
        ),
    ),
    Mutant(
        "binding_recheck",
        "the args hash is not re-checked at execution (binding not re-checked)",
        (
            Edit(
                _REGISTRY,
                "        if exec_args is None or approval_sha256(spec.name, exec_args)"
                " != approval.args_sha256:\n",
                "        if exec_args is None:  # MUTANT binding_recheck\n",
            ),
        ),
    ),
    Mutant(
        "execution_recheck",
        "an approved call executes with the request-time locations (no execution re-check)",
        (
            Edit(
                _REGISTRY,
                "            return await engine.recheck(approval.live)\n",
                "            return approval.live  # MUTANT execution_recheck\n",
            ),
        ),
    ),
    Mutant(
        "undo_state_check",
        "undo takes any live location of a target, whatever its state",
        (
            Edit(
                _UNDO,
                "if f.tb_id not in used and f.state == post)",
                "if f.tb_id not in used)",
            ),
        ),
    ),
    Mutant(
        "conflicted_refusal",
        "target resolution no longer refuses a conflicted mid",
        (Edit(_TARGETS, "        if msg.conflicted:\n", "        if False:  # MUTANT\n"),),
    ),
    Mutant(
        "approval_expiry",
        "a decide after expires_at is no longer expired (decide-time expiry check)",
        (
            Edit(
                _APPROVALS,
                "        if self._clock() >= approval.card.expires_at:\n",
                "        if False:  # MUTANT approval_expiry\n",
            ),
        ),
    ),
    Mutant(
        "subset_check",
        "a W2 subset is no longer checked to be a subset of the original targets",
        (
            Edit(
                _APPROVALS,
                "        if not keep <= original:\n",
                "        if False:  # MUTANT subset_check\n",
            ),
        ),
    ),
    Mutant(
        "forbidden_effect",
        "Registry.call no longer refuses a tool declaring a forbidden effect (filters_write, T6)",
        (
            Edit(
                _REGISTRY,
                "        if rt.engine is not None and rt.engine.forbidden_effects(spec):\n",
                "        if rt.engine is not None and frozenset():  # MUTANT forbidden_effect\n",
            ),
        ),
    ),
    Mutant(
        "recheck_conflicted",
        "the execution re-check no longer refuses a mid that became conflicted while pending",
        (
            Edit(
                _TARGETS,
                "        if current.conflicted:\n",
                "        if False:  # MUTANT recheck_conflicted\n",
            ),
        ),
    ),
)


# -- applying ------------------------------------------------------------------------------------


def mutated_text(text: str, edits: Iterable[Edit], *, where: str) -> str:
    """``text`` with every edit applied in order; :class:`StaleAnchor` unless each ``old``
    occurs exactly once when its turn comes."""
    for edit in edits:
        n = text.count(edit.old)
        if n != 1:
            raise StaleAnchor(f"{where}: anchor in {edit.path} occurs {n} times, expected 1")
        if edit.new == edit.old:
            raise StaleAnchor(f"{where}: edit in {edit.path} changes nothing")
        text = text.replace(edit.old, edit.new, 1)
    return text


def check_anchors(root: Path, mutant: Mutant) -> dict[Path, str]:
    """The mutated text of each file ``mutant`` touches under ``root``, computed without writing
    anything; :class:`StaleAnchor` for an anchor that does not occur exactly once."""
    by_file: dict[str, list[Edit]] = {}
    for edit in mutant.edits:
        by_file.setdefault(edit.path, []).append(edit)
    out: dict[Path, str] = {}
    for rel, edits in by_file.items():
        path = root / rel
        out[path] = mutated_text(path.read_text(encoding="utf-8"), edits, where=mutant.name)
    return out


def apply(root: Path, mutant: Mutant) -> dict[Path, str]:
    """Write ``mutant`` into the tree at ``root``; returns each touched file's original text
    (for :func:`restore`). Every anchor is checked before the first write, so a stale anchor
    leaves the tree unchanged."""
    new = check_anchors(root, mutant)
    originals = {path: path.read_text(encoding="utf-8") for path in new}
    for path, text in new.items():
        path.write_text(text, encoding="utf-8")
    return originals


def restore(originals: dict[Path, str]) -> None:
    for path, text in originals.items():
        path.write_text(text, encoding="utf-8")


def module_name(rel: str) -> str:
    """``src/tern/tools/registry.py`` -> ``tern.tools.registry``."""
    parts = Path(rel).with_suffix("").parts
    if not parts or parts[0] != "src":
        raise ValueError(f"not a module under src/: {rel}")
    return ".".join(parts[1:])


# -- running -------------------------------------------------------------------------------------

#: Run as ``python -c _DRIVER <modules> <pytest args...>``: imports every ``tern.perms`` module and
#: the given ones, prints where each came from, then runs pytest in this same interpreter (so the
#: modules the tests use are exactly the ones printed). A failed import prints its traceback and an
#: ``IMPORT_FAILED_LINE`` on stderr and exits ``IMPORT_FAILED_EXIT`` before pytest runs.
_DRIVER = r"""
import importlib, pkgutil, sys, traceback
import pytest
def load(name):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # any import failure is reported, then ends the run (no default)
        traceback.print_exc()
        print("@IMPORT_FAILED_LINE@" + name + "\t" + type(exc).__name__ + ": " + str(exc),
              file=sys.stderr, flush=True)
        sys.exit(@IMPORT_FAILED_EXIT@)
perms = load("tern.perms")
names = {"tern.perms." + m.name for m in pkgutil.iter_modules(perms.__path__)}
names |= {n for n in sys.argv[1].split(",") if n}
for name in sorted(names):
    mod = load(name)
    print("MUTATION-MODULE\t" + name + "\t" + str(mod.__file__), flush=True)
sys.exit(int(pytest.main(sys.argv[2:])))
""".replace("@IMPORT_FAILED_LINE@", IMPORT_FAILED_LINE.replace("\t", "\\t")).replace(
    "@IMPORT_FAILED_EXIT@", str(IMPORT_FAILED_EXIT)
)


@dataclass(frozen=True, slots=True)
class RunResult:
    exit_code: int | None  # None: timed out
    tests: int
    failed: tuple[str, ...]  # failing test ids (failures and errors), JUnit order
    modules: dict[str, str] = field(default_factory=dict)
    report: bool = True  # a JUnit report was written
    tail: str = ""  # the last lines of the child's stderr and stdout (output_tail)

    @property
    def passed(self) -> int:
        return self.tests - len(self.failed)


def env_for(root: Path) -> dict[str, str]:
    """The environment of a run in the copy at ``root``: its ``src`` first and alone on
    ``PYTHONPATH``, no bytecode written."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def parse_modules(stdout: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        if line.startswith(MODULE_LINE):
            _, name, path = line.split("\t", 2)
            out[name] = path
    return out


def output_tail(stdout: str, stderr: str) -> str:
    """The last :data:`TAIL_LINES` non-blank lines of ``stderr`` and of ``stdout`` (the driver's
    module lines left out), each cut to :data:`TAIL_WIDTH` characters; empty when both are."""

    def last(text: str) -> str:
        lines = [
            line.rstrip()[:TAIL_WIDTH]
            for line in text.splitlines()
            if line.strip() and not line.startswith(MODULE_LINE)
        ]
        return " | ".join(lines[-TAIL_LINES:])

    parts = [
        f"{label}: {text}"
        for label, text in (("stderr", last(stderr)), ("stdout", last(stdout)))
        if text
    ]
    return "; ".join(parts)


def _with_tail(detail: str, result: RunResult) -> str:
    return f"{detail} [{result.tail}]" if result.tail else detail


def check_module_paths(modules: dict[str, str], root: Path, expected: Iterable[str]) -> None:
    """:class:`HarnessError` unless every expected module was imported, and every imported
    module's file lies inside ``root/src``."""
    missing = sorted(set(expected) - set(modules))
    if missing:
        raise HarnessError(f"the run did not report these modules: {', '.join(missing)}")
    src = (root / "src").resolve()
    outside = sorted(n for n, p in modules.items() if not Path(p).resolve().is_relative_to(src))
    if outside:
        raise HarnessError(
            f"the run imported {', '.join(outside)} from outside the copy ({src}): "
            "the result would not test the mutant"
        )


def parse_junit(path: Path) -> tuple[int, tuple[str, ...]]:
    """(test count, failing test ids) from a pytest JUnit report; a test id is
    ``<classname>::<name>``."""
    tree = ET.parse(path)
    tests = 0
    failed: list[str] = []
    for case in tree.iter("testcase"):
        tests += 1
        if case.find("failure") is not None or case.find("error") is not None:
            failed.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    return tests, tuple(failed)


def classify(result: RunResult) -> tuple[Status, str]:
    """A mutant's outcome from its run (module docstring, "Outcomes")."""
    if result.exit_code is None:
        return "error", _with_tail(f"timed out after {RUN_TIMEOUT_S} s", result)
    if result.exit_code == IMPORT_FAILED_EXIT:
        return "error", _with_tail("a module failed to import before pytest ran", result)
    if not result.report:
        return "error", _with_tail(f"no JUnit report (pytest exit {result.exit_code})", result)
    if result.exit_code == 0:
        if result.failed or result.tests == 0:
            return "error", _with_tail("pytest exit 0 with failures or no tests", result)
        return "survived", f"{result.tests} tests passed"
    if result.exit_code == 1:
        perms = [t for t in result.failed if t.startswith(PERMS_CLASS_PREFIX)]
        if perms:
            return "killed", f"by {perms[0]}"
        return "error", _with_tail("pytest exit 1 without a failing tests/perms test", result)
    return "error", _with_tail(f"pytest exit {result.exit_code} (not a test failure)", result)


def run_perms(root: Path, modules: Sequence[str], *, stop_first: bool, tag: str) -> RunResult:
    """Run ``tests/perms`` in the copy at ``root`` (``slow``/``smoke`` and this harness excluded)
    and check that every ``tern.perms`` module and ``modules`` came from the copy."""
    junit = root.parent / f"junit-{tag}.xml"
    junit.unlink(missing_ok=True)
    args = [
        "-q",
        "--tb=line",
        "-p",
        "no:cacheprovider",
        "--rootdir",
        str(root),
        "-c",
        str(root / "pyproject.toml"),
        "-m",
        "not slow and not smoke",
        "--ignore",
        str(root / "tests/perms/mutation"),
        "--junitxml",
        str(junit),
    ]
    if stop_first:
        args.append("-x")
    args.append(str(root / PERMS_TESTS))
    cmd = [sys.executable, "-c", _DRIVER, ",".join(modules), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=root,
            env=env_for(root),
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return RunResult(exit_code=None, tests=0, failed=(), report=False)
    found = parse_modules(proc.stdout)
    tail = output_tail(proc.stdout, proc.stderr)
    if proc.returncode == IMPORT_FAILED_EXIT:
        # No test ran: classify() reports it as ``error`` (never killed) with the import error.
        return RunResult(proc.returncode, 0, (), found, report=False, tail=tail)
    try:
        check_module_paths(found, root, modules)
    except HarnessError as exc:
        raise HarnessError(f"{exc} [{tail or 'no output'}]") from exc
    if not junit.exists():
        return RunResult(proc.returncode, 0, (), found, report=False, tail=tail)
    tests, failed = parse_junit(junit)
    return RunResult(proc.returncode, tests, failed, found, tail=tail)


# -- the whole run -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    mutant: Mutant
    status: Status
    detail: str


@dataclass(slots=True)
class Report:
    baseline: RunResult
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def all_killed(self) -> bool:
        return all(o.status == "killed" for o in self.outcomes)

    def lines(self) -> list[str]:
        out = [f"baseline: passed ({self.baseline.tests} tests, tern.perms from the copy)"]
        out += [f"{o.status:8} {o.mutant.name}: {o.detail}" for o in self.outcomes]
        killed = sum(o.status == "killed" for o in self.outcomes)
        out.append(f"{killed}/{len(self.outcomes)} mutants killed")
        return out


def make_copy(repo: Path, dest: Path) -> Path:
    """Copy ``src/``, ``tests/`` and the pytest config of ``repo`` into ``dest/tree``."""
    root = dest / "tree"
    root.mkdir()
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for name in COPIED_DIRS:
        shutil.copytree(repo / name, root / name, ignore=skip)
    for name in COPIED_FILES:
        shutil.copy2(repo / name, root / name)
    return root


def _mutated_modules(mutants: Iterable[Mutant]) -> list[str]:
    return sorted({module_name(e.path) for m in mutants for e in m.edits})


def run_all(
    mutants: Sequence[Mutant] = MUTANTS,
    *,
    repo: Path = REPO,
    work: Path | None = None,
    keep: bool = False,
) -> Report:
    """Baseline, then every mutant (module docstring). Every anchor is checked against the
    working tree first (:class:`StaleAnchor`). :class:`HarnessError` when the baseline fails or
    the run does not import the copy. The copy lives in a new directory under ``work`` (default
    ``$TMPDIR``) and is removed afterwards unless ``keep``."""
    names = [m.name for m in mutants]
    if len(set(names)) != len(names):
        raise ValueError("mutant names must be unique")
    for mutant in mutants:
        check_anchors(repo, mutant)
    modules = _mutated_modules(mutants)
    base = Path(tempfile.mkdtemp(prefix="tern-mutation-", dir=work))
    try:
        root = make_copy(repo, base)
        baseline = run_perms(root, modules, stop_first=False, tag="baseline")
        status, detail = classify(baseline)
        if status != "survived":
            raise HarnessError(f"the unmutated copy does not pass tests/perms: {detail}")
        report = Report(baseline)
        for mutant in mutants:
            originals = apply(root, mutant)
            try:
                result = run_perms(root, modules, stop_first=True, tag=mutant.name)
            finally:
                restore(originals)
            for path in originals:
                rel = path.relative_to(root)
                if path.read_text(encoding="utf-8") != (repo / rel).read_text(encoding="utf-8"):
                    raise HarnessError(f"{rel} was not restored after {mutant.name}")
            status, detail = classify(result)
            report.outcomes.append(Outcome(mutant, status, detail))
        return report
    finally:
        if not keep:
            shutil.rmtree(base)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Permission-engine mutation harness (M31).")
    parser.add_argument("--only", action="append", default=[], help="run only this mutant")
    parser.add_argument("--list", action="store_true", help="list the mutants and exit")
    parser.add_argument("--keep", action="store_true", help="keep the copy under $TMPDIR")
    ns = parser.parse_args(argv)
    if ns.list:
        for m in MUTANTS:
            print(f"{m.name}: {m.what}")
        return 0
    by_name = {m.name: m for m in MUTANTS}
    unknown = [n for n in ns.only if n not in by_name]
    if unknown:
        parser.error(f"unknown mutant(s): {', '.join(unknown)}")
    selected = [by_name[n] for n in ns.only] if ns.only else list(MUTANTS)
    report = run_all(selected, keep=ns.keep)
    for line in report.lines():
        print(line)
    return 0 if report.all_killed else 1


if __name__ == "__main__":
    sys.exit(main())
