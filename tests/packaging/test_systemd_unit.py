"""The systemd user unit template (plan 08b step 6, A9).

``systemd-analyze --user verify`` cannot run in the sandbox (EROFS on ``/tmp``), so this test
parses the unit file itself; the joint session runs ``systemd-analyze`` on the rendered unit (J7).
"""

from __future__ import annotations

import importlib.util
import shlex
from pathlib import Path

import pytest

from tern import paths
from tern.bridge import nmhost

TEMPLATE = Path(__file__).resolve().parents[2] / "packaging" / "systemd" / "ternd.service.in"
PLACEHOLDER = "@TERN_PYTHON@"
VENV_PYTHON = "/home/someone/tern/.venv/bin/python"


def render(text: str, python: str) -> str:
    """What J7 (and later ``tern install``) does: substitute the venv's interpreter."""
    if not Path(python).is_absolute():
        raise ValueError("the interpreter path must be absolute")
    return text.replace(PLACEHOLDER, python)


def parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """A strict reader of systemd's unit syntax: sections, ``Key=value`` lines, comments.

    Repeated keys are kept as lists (systemd allows them); continuation lines are refused
    because this template has none, so a malformed edit fails here instead of being skipped."""
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            assert name not in sections, f"line {n}: section [{name}] repeated"
            current = sections[name] = {}
            continue
        assert current is not None, f"line {n}: key outside a section"
        assert "=" in line, f"line {n}: not a Key=value line"
        assert not line.endswith("\\"), f"line {n}: continuation line"
        key, value = (part.strip() for part in line.split("=", 1))
        assert key and key.replace("-", "").isalnum(), f"line {n}: bad key {key!r}"
        current.setdefault(key, []).append(value)
    return sections


@pytest.fixture
def template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture
def unit(template: str) -> dict[str, dict[str, list[str]]]:
    return parse_unit(render(template, VENV_PYTHON))


def test_template_is_a_plain_text_unit_with_one_placeholder(template: str) -> None:
    assert template.endswith("\n") and "\r" not in template
    assert template.count(PLACEHOLDER) == 1
    assert "@" not in render(template, VENV_PYTHON)


def test_sections_and_keys_are_exactly_the_planned_ones(
    unit: dict[str, dict[str, list[str]]],
) -> None:
    assert set(unit) == {"Unit", "Service", "Install"}
    assert set(unit["Unit"]) == {"Description", "StartLimitIntervalSec", "StartLimitBurst"}
    assert unit["Unit"]["StartLimitIntervalSec"] == ["120"]
    assert unit["Unit"]["StartLimitBurst"] == ["5"]
    assert unit["Service"] == {
        "Type": ["simple"],
        "ExecStart": [f"{VENV_PYTHON} -m tern.daemon.main"],
        "Restart": ["on-failure"],
        "RestartSec": ["5"],
    }
    assert unit["Install"] == {"WantedBy": ["default.target"]}


def test_a_unit_failing_at_every_start_reaches_the_failed_state(
    unit: dict[str, dict[str, list[str]]],
) -> None:
    """``Restart=on-failure`` restarts every ``RestartSec``; the start limit must trip at that
    spacing (systemd counts starts within ``StartLimitIntervalSec``; its default 5 in 10 s never
    trips at 5 s apart), so a ternd that fails at every start stops instead of looping forever."""
    (restart_sec,) = unit["Service"]["RestartSec"]
    interval = int(unit["Unit"].get("StartLimitIntervalSec", ["10"])[0])
    burst = int(unit["Unit"].get("StartLimitBurst", ["5"])[0])
    assert burst >= 2
    # the burst-th start comes (burst - 1) restart delays after the first, plus start-up time
    assert (burst - 1) * int(restart_sec) < interval / 2


def test_exec_start_is_absolute_and_the_command_tern_nmhost_spawns(
    unit: dict[str, dict[str, list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[list[str]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        spawned.append(list(argv))
        return object()

    monkeypatch.setattr(nmhost.subprocess, "Popen", fake_popen)
    nmhost.spawn_ternd(paths.resolve())  # under the autouse tmp TERN_HOME
    (nmhost_argv,) = spawned
    (exec_start,) = unit["Service"]["ExecStart"]
    argv = shlex.split(exec_start)
    assert Path(argv[0]).is_absolute() and argv[0] == VENV_PYTHON
    assert argv[1:] == nmhost_argv[1:] == ["-m", "tern.daemon.main"]
    assert "--foreground" not in argv  # logs go to <state>/ternd.log
    assert importlib.util.find_spec("tern.daemon.main") is not None


def test_no_environment_and_no_tern_home(template: str) -> None:
    unit = parse_unit(render(template, VENV_PYTHON))
    keys = {k for sec in unit.values() for k in sec}
    assert not keys & {"Environment", "EnvironmentFile", "PassEnvironment", "UnsetEnvironment"}
    body = "\n".join(
        line for line in template.splitlines() if line.strip() and line.strip()[0] not in "#;"
    )
    assert "TERN_HOME" not in body


def test_no_network_directive(unit: dict[str, dict[str, list[str]]]) -> None:
    for name, sec in unit.items():
        for key, values in sec.items():
            assert not key.startswith(("Listen", "IPAddress", "Socket")), (name, key)
            assert key not in {"Sockets", "RestrictAddressFamilies", "PrivateNetwork"}, key
            for value in values:
                assert "network" not in value.lower(), (name, key, value)
                assert ".socket" not in value, (name, key, value)


def test_render_refuses_a_relative_interpreter(template: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        render(template, ".venv/bin/python")


@pytest.mark.parametrize(
    "bad",
    [
        "Restart=on-failure\n",  # key outside a section
        "[Service]\nnot a key line\n",
        "[Service]\nExecStart=/x \\\n  -m y\n",
        "[Service]\n[Service]\n",
    ],
)
def test_the_parser_refuses_malformed_units(bad: str) -> None:
    with pytest.raises(AssertionError):
        parse_unit(bad)
