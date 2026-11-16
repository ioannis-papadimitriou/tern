"""``tern tools describe`` / ``--json-schema`` (plan 06a step 2): metadata only, never config,
paths, config-path plugins, index, audit DB or socket."""

from __future__ import annotations

import importlib.util
import json
import socket
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tern import cli, config, paths
from tern.tools import BoundaryModel, Effect, Tier, ToolMeta, tool
from tern.tools import registry as reg

from .conftest import Plugins, plugin_source


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(["tools", "describe", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def _canonical(doc: object) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_describe_lists_entry_point_plugins_sorted(
    plugins: Plugins, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Plugins only: the built-ins (plan 06a step 5) are not under test here.
    monkeypatch.setattr(reg, "BUILTINS", ())
    mod = plugins.module(plugin_source("zeta_tool", "alpha_tool", recipients=["zeta_tool"]))
    plugins.entry_points(("mine", f"{mod}:TOOLS"))
    code, out, err = _run(capsys)
    assert (code, err) == (0, "")
    doc = json.loads(out)
    assert out == _canonical(doc)  # indent=2, sort_keys=True, trailing newline
    metas = [ToolMeta.model_validate(d) for d in doc]
    assert [m.name for m in metas] == ["alpha_tool", "zeta_tool"]
    assert all(m.origin == "plugin" and m.contract == "tern-tool/1" for m in metas)
    # D3: declaring `recipients` shows through the CLI; not declaring it reports false.
    assert {m.name: m.declares_recipients for m in metas} == {
        "alpha_tool": False,
        "zeta_tool": True,
    }


class _BIn(BoundaryModel):
    text: str


class _BOut(BoundaryModel):
    text: str


@tool(name="stand_in_builtin", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=False)
async def _stand_in_builtin(ctx: object, inp: _BIn) -> _BOut:
    """A stand-in built-in (``BUILTINS`` is empty until step 5)."""
    return _BOut(text=inp.text)


def test_describe_prints_the_builtins(
    plugins: Plugins, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(reg, "BUILTINS", (_stand_in_builtin,))
    mod = plugins.module(plugin_source("ep_tool"))
    plugins.entry_points(("mine", f"{mod}:TOOLS"))
    code, out, err = _run(capsys)
    assert (code, err) == (0, "")
    got = {d["name"]: d["origin"] for d in json.loads(out)}
    assert got == {"stand_in_builtin": "builtin", "ep_tool": "plugin"}


def test_describe_prints_the_real_builtins(
    plugins: Plugins, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys)
    assert code == 0
    expected = [m.model_dump(mode="json") for m in reg.Registry(reg.builtin_tools()).describe()]
    assert json.loads(out) == expected
    assert all(d["origin"] == "builtin" for d in expected)
    assert expected


def test_json_schema_is_toolmeta_schema(
    plugins: Plugins, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "--json-schema")
    assert (code, err) == (0, "")
    assert json.loads(out) == ToolMeta.model_json_schema()
    assert out == _canonical(ToolMeta.model_json_schema())


def test_broken_entry_point_exits_1_naming_it(
    plugins: Plugins, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = plugins.module("raise RuntimeError('SENTINEL-DESCRIBE')\n")
    plugins.entry_points(("broken", f"{mod}:TOOLS"))
    code, out, err = _run(capsys)
    assert code == 1 and out == ""
    assert err == f"tools describe: entry point broken ({mod}:TOOLS): cannot load (RuntimeError)\n"


def test_json_schema_needs_no_plugin_loading(
    plugins: Plugins, capsys: pytest.CaptureFixture[str]
) -> None:
    """The schema is static: even a broken entry point does not affect it."""
    mod = plugins.module("raise RuntimeError('x')\n")
    plugins.entry_points(("broken", f"{mod}:TOOLS"))
    code, out, _ = _run(capsys, "--json-schema")
    assert code == 0 and json.loads(out) == ToolMeta.model_json_schema()


@pytest.mark.parametrize("argv", [[], ["--json-schema"]])
def test_describe_never_touches_config_paths_or_data(
    argv: list[str],
    plugins: Plugins,
    tern_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Trap test: the guard exempts ``tools describe`` as metadata-only."""

    def trap(what: str) -> Any:
        def fail(*args: object, **kwargs: object) -> Any:
            raise AssertionError(f"tools describe touched {what}")

        return fail

    # A config.toml listing a plugin file that would fail loudly if it were ever loaded.
    bomb = plugins.write("bomb.py", "raise SystemExit('config-path plugin was loaded')\n")
    (tern_home / "config").mkdir(exist_ok=True)
    (tern_home / "config" / "config.toml").write_text(f'[plugins]\npaths = ["{bomb}"]\n')

    monkeypatch.setattr(paths, "resolve", trap("tern.paths.resolve"))
    monkeypatch.setattr(config, "load", trap("tern.config.load"))
    monkeypatch.setattr(reg, "path_tools", trap("config-path plugins"))
    monkeypatch.setattr(importlib.util, "spec_from_file_location", trap("a plugin file"))
    monkeypatch.setattr(sqlite3, "connect", trap("a database (index or audit)"))
    monkeypatch.setattr(socket, "socket", trap("a socket"))

    monkeypatch.setattr(reg, "BUILTINS", ())  # plugins only (built-ins: plan 06a step 5)
    mod = plugins.module(plugin_source("ep_tool"))
    plugins.entry_points(("ep", f"{mod}:TOOLS"))
    code, out, err = _run(capsys, *argv)
    assert (code, err) == (0, "")
    doc = json.loads(out)
    if not argv:
        assert [d["name"] for d in doc] == ["ep_tool"]
