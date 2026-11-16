"""Registry discovery: built-ins, entry points ``tern.tools``, config ``[plugins] paths`` (plan 06a
step 2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tern import config
from tern.tools import BoundaryModel, Effect, Tier, tool
from tern.tools import registry as reg
from tern.tools.registry import PluginLoadError, RegisteredTool, Registry

from .conftest import Plugins, plugin_source

SENTINEL = "SENTINEL-PLUGIN-7f3a"


class BIn(BoundaryModel):
    text: str


class BOut(BoundaryModel):
    text: str


@tool(name="b_builtin", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=False)
async def b_builtin(ctx: object, inp: BIn) -> BOut:
    """A stand-in built-in."""
    return BOut(text=inp.text)


@tool(name="echo", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=False)
async def echo_builtin(ctx: object, inp: BIn) -> BOut:
    """A built-in a plugin may try to shadow."""
    return BOut(text=inp.text)


def _names(tools: list[RegisteredTool]) -> list[tuple[str, str]]:
    return [(t.spec.name, t.origin) for t in tools]


# ---- built-ins ------------------------------------------------------------------------------


def test_builtins_are_registered_explicitly_with_origin_builtin(
    plugins: Plugins, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reg, "BUILTINS", (b_builtin,))
    r = Registry.discover()
    got = r.get("b_builtin")
    assert got is not None
    assert got.origin == "builtin"
    assert got.meta().origin == "builtin"
    assert got.source == "builtin"


def test_builtins_need_no_entry_points(plugins: Plugins, monkeypatch: pytest.MonkeyPatch) -> None:
    """Amendment 14: with the entry-point group empty, built-ins are still all there."""
    monkeypatch.setattr(reg, "BUILTINS", (b_builtin, echo_builtin))
    assert Registry.discover().names() == ["b_builtin", "echo"]


# ---- entry points ---------------------------------------------------------------------------


def test_entry_point_resolving_to_a_list(plugins: Plugins) -> None:
    mod = plugins.module(plugin_source("ep_one", "ep_two"))
    plugins.entry_points(("mine", f"{mod}:TOOLS"))
    tools = reg.entry_point_tools()
    assert _names(tools) == [("ep_one", "plugin"), ("ep_two", "plugin")]
    assert tools[0].source == f"entry point mine ({mod}:TOOLS)"


def test_entry_point_resolving_to_one_spec(plugins: Plugins) -> None:
    mod = plugins.module(plugin_source("ep_single"))
    plugins.entry_points(("single", f"{mod}:tool_0"))
    assert _names(reg.entry_point_tools()) == [("ep_single", "plugin")]


def test_entry_point_resolving_to_a_tuple(plugins: Plugins) -> None:
    mod = plugins.module(plugin_source("ep_a", "ep_b") + "\nAS_TUPLE = tuple(TOOLS)\n")
    plugins.entry_points(("tup", f"{mod}:AS_TUPLE"))
    assert _names(reg.entry_point_tools()) == [("ep_a", "plugin"), ("ep_b", "plugin")]


@pytest.mark.parametrize(
    "extra",
    [
        "BAD = 'ep_x'",  # a string is a sequence, but not of ToolSpecs
        "BAD = []",
        "BAD = [tool_0, 'ep_x']",
        "BAD = {'ep_x': tool_0}",
        "BAD = tool_0.impl",  # the raw coroutine function is not a ToolSpec
        "BAD = None",
    ],
)
def test_entry_point_resolving_to_anything_else_fails(plugins: Plugins, extra: str) -> None:
    mod = plugins.module(plugin_source("ep_x") + f"\n{extra}\n")
    plugins.entry_points(("badone", f"{mod}:BAD"))
    with pytest.raises(PluginLoadError, match=r"entry point badone \(.*:BAD\): resolves to"):
        reg.entry_point_tools()


def test_entry_point_whose_import_raises_names_it_without_content(plugins: Plugins) -> None:
    mod = plugins.module(f"raise RuntimeError('{SENTINEL}')\n")
    plugins.entry_points(("boom", f"{mod}:TOOLS"))
    with pytest.raises(PluginLoadError) as ei:
        reg.entry_point_tools()
    msg = str(ei.value)
    assert msg == f"entry point boom ({mod}:TOOLS): cannot load (RuntimeError)"
    assert SENTINEL not in msg
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_entry_point_with_missing_attribute_fails(plugins: Plugins) -> None:
    mod = plugins.module(plugin_source("ep_y"))
    plugins.entry_points(("noattr", f"{mod}:NOPE"))
    with pytest.raises(PluginLoadError, match=r"entry point noattr .*AttributeError"):
        reg.entry_point_tools()


def test_entry_point_with_missing_module_fails(plugins: Plugins) -> None:
    plugins.entry_points(("nomod", "tern_test_no_such_module_9c1d:TOOLS"))
    with pytest.raises(PluginLoadError, match=r"entry point nomod .*ModuleNotFoundError"):
        reg.entry_point_tools()


def test_entry_point_with_a_bad_tool_definition_says_why(plugins: Plugins) -> None:
    mod = plugins.module(plugin_source("Bad-Name"))
    plugins.entry_points(("baddef", f"{mod}:TOOLS"))
    with pytest.raises(PluginLoadError, match=r"baddef .*ToolDefinitionError: tool name must"):
        reg.entry_point_tools()


# ---- config paths ---------------------------------------------------------------------------


def test_path_plugin_loads_its_tools(plugins: Plugins) -> None:
    path = plugins.write("a/mine.py", plugin_source("path_one", "path_two"))
    tools = reg.path_tools(path)
    assert _names(tools) == [("path_one", "plugin"), ("path_two", "plugin")]
    assert tools[0].source == f"plugin file {path}"


def test_two_path_plugins_with_the_same_basename_both_load(
    plugins: Plugins, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Plugins only: the built-ins (plan 06a step 5) are not under test here.
    monkeypatch.setattr(reg, "BUILTINS", ())
    p1 = plugins.write("a/tools.py", plugin_source("same_base_a"))
    p2 = plugins.write("b/tools.py", plugin_source("same_base_b"))
    r = Registry.discover([p1, p2])
    assert r.names() == ["same_base_a", "same_base_b"]


def test_relative_path_fails(plugins: Plugins, monkeypatch: pytest.MonkeyPatch) -> None:
    plugins.write("rel.py", plugin_source("rel_tool"))
    monkeypatch.chdir(plugins.root)
    with pytest.raises(PluginLoadError, match=r"plugin file rel\.py: not an absolute path"):
        reg.path_tools(Path("rel.py"))


def test_tilde_path_is_not_expanded(plugins: Plugins) -> None:
    with pytest.raises(PluginLoadError, match="not an absolute path"):
        reg.path_tools(Path("~/plugin.py"))


def test_missing_path_fails(plugins: Plugins) -> None:
    path = plugins.root / "gone.py"
    with pytest.raises(PluginLoadError, match=f"plugin file {path}: missing"):
        reg.path_tools(path)


def test_directory_is_never_loaded(plugins: Plugins) -> None:
    """No directory auto-load (#6): a directory is refused, even one named like a module."""
    plugins.write("pkg.py/inner.py", plugin_source("inner_tool"))
    plugins.write("dir/x.py", plugin_source("dir_tool"))
    with pytest.raises(PluginLoadError, match="missing or not a regular file"):
        reg.path_tools(plugins.root / "pkg.py")
    with pytest.raises(PluginLoadError, match=r"not a \.py file"):
        reg.path_tools(plugins.root / "dir")


def test_non_py_file_fails(plugins: Plugins) -> None:
    path = plugins.write("plugin.txt", plugin_source("txt_tool"))
    with pytest.raises(PluginLoadError, match=r"not a \.py file"):
        reg.path_tools(path)


def test_path_without_tools_export_fails(plugins: Plugins) -> None:
    path = plugins.write("noexport.py", plugin_source("noexp_tool", export="OTHER"))
    before = set(sys.modules)
    with pytest.raises(PluginLoadError, match="does not export TOOLS"):
        reg.path_tools(path)
    assert not any(m.startswith("_tern_plugin_") for m in set(sys.modules) - before)


def test_path_with_wrong_tools_fails(plugins: Plugins) -> None:
    path = plugins.write("wrong.py", plugin_source("wrong_tool") + "\nTOOLS = 'wrong_tool'\n")
    before = set(sys.modules)
    with pytest.raises(PluginLoadError, match=f"plugin file {path}: resolves to str"):
        reg.path_tools(path)
    assert not any(m.startswith("_tern_plugin_") for m in set(sys.modules) - before)


def test_path_plugin_module_stays_registered_on_success(plugins: Plugins) -> None:
    path = plugins.write("ok.py", plugin_source("ok_tool"))
    before = set(sys.modules)
    reg.path_tools(path)
    assert len([m for m in set(sys.modules) - before if m.startswith("_tern_plugin_")]) == 1


def test_path_with_syntax_error_reports_no_file_content(plugins: Plugins) -> None:
    path = plugins.write("syntax.py", f"x = 1\n{SENTINEL.replace('-', '_')} = = 2\n")
    before = set(sys.modules)
    with pytest.raises(PluginLoadError) as ei:
        reg.path_tools(path)
    msg = str(ei.value)
    assert msg == f"plugin file {path}: cannot load (SyntaxError at line 2)"
    assert SENTINEL.replace("-", "_") not in msg
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert not any(m.startswith("_tern_plugin_") for m in set(sys.modules) - before)


def test_path_whose_import_raises_reports_no_content(plugins: Plugins) -> None:
    path = plugins.write("raises.py", f"raise ValueError('{SENTINEL}')\n")
    with pytest.raises(PluginLoadError) as ei:
        reg.path_tools(path)
    assert str(ei.value) == f"plugin file {path}: cannot load (ValueError)"


def test_config_paths_feed_discovery(plugins: Plugins, tmp_path: Path) -> None:
    plugin = plugins.write(
        "cfg/plugin.py", plugin_source("from_config", recipients=["from_config"])
    )
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'[plugins]\npaths = ["{plugin}"]\n')
    cfg = config.load(cfg_file)
    r = Registry.discover(cfg.plugins.paths)
    got = r.get("from_config")
    assert got is not None and got.origin == "plugin"
    assert got.meta().declares_recipients is True


# ---- names ----------------------------------------------------------------------------------


def test_plugin_shadowing_a_builtin_fails(
    plugins: Plugins, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reg, "BUILTINS", (echo_builtin,))
    mod = plugins.module(plugin_source("echo"))
    plugins.entry_points(("shadow", f"{mod}:TOOLS"))
    with pytest.raises(
        PluginLoadError,
        match=r"entry point shadow .*: tool name echo is already registered by builtin",
    ):
        Registry.discover()


def test_path_plugin_shadowing_a_builtin_fails(
    plugins: Plugins, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reg, "BUILTINS", (echo_builtin,))
    path = plugins.write("shadow.py", plugin_source("echo"))
    with pytest.raises(PluginLoadError, match=f"plugin file {path}: tool name echo is already"):
        Registry.discover([path])


def test_two_entry_points_with_one_name_fail(plugins: Plugins) -> None:
    m1 = plugins.module(plugin_source("twice"))
    m2 = plugins.module(plugin_source("twice"))
    plugins.entry_points(("first", f"{m1}:TOOLS"), ("second", f"{m2}:TOOLS"))
    with pytest.raises(
        PluginLoadError, match=r"entry point second .*registered by entry point first"
    ):
        Registry.discover()


def test_same_path_listed_twice_fails(plugins: Plugins) -> None:
    path = plugins.write("dup.py", plugin_source("dup_tool"))
    with pytest.raises(PluginLoadError, match="tool name dup_tool is already registered"):
        Registry.discover([path, path])


def test_duplicate_names_inside_one_plugin_fail(plugins: Plugins) -> None:
    path = plugins.write("selfdup.py", plugin_source("same", "same"))
    with pytest.raises(PluginLoadError, match="tool name same is already registered"):
        Registry.discover([path])


# ---- registry -------------------------------------------------------------------------------


def test_discovery_order_and_describe_sorted_by_name(
    plugins: Plugins, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reg, "BUILTINS", (b_builtin,))
    mod = plugins.module(plugin_source("zz_ep", "aa_ep"))
    plugins.entry_points(("ep", f"{mod}:TOOLS"))
    path = plugins.write("p.py", plugin_source("mm_path"))
    found = reg.discover([path])
    assert _names(found) == [
        ("b_builtin", "builtin"),
        ("zz_ep", "plugin"),
        ("aa_ep", "plugin"),
        ("mm_path", "plugin"),
    ]
    r = Registry(found)
    assert len(r) == 4 and "mm_path" in r and "nope" not in r and r.get("nope") is None
    metas = r.describe()
    assert [m.name for m in metas] == ["aa_ep", "b_builtin", "mm_path", "zz_ep"]
    assert [m.origin for m in metas] == ["plugin", "builtin", "plugin", "plugin"]


def test_describe_registry_never_loads_config_paths(
    plugins: Plugins, monkeypatch: pytest.MonkeyPatch
) -> None:
    def trap(path: Path) -> list[RegisteredTool]:
        raise AssertionError("describe must not load config-path plugins")

    monkeypatch.setattr(reg, "path_tools", trap)
    monkeypatch.setattr(reg, "BUILTINS", ())  # plugins only (built-ins: plan 06a step 5)
    mod = plugins.module(plugin_source("ep_only"))
    plugins.entry_points(("ep", f"{mod}:TOOLS"))
    assert reg.describe_registry().names() == ["ep_only"]
