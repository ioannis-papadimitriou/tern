"""The package and every planned subpackage import (scaffold check, plan 01)."""

import importlib

import pytest

SUBPACKAGES = [
    "bridge",
    "daemon",
    "index",
    "search",
    "llm",
    "agent",
    "tools",
    "perms",
    "mcp",
    "testing",
]


def test_package_version() -> None:
    import tern

    assert tern.__version__ == "0.1.0"


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name: str) -> None:
    mod = importlib.import_module(f"tern.{name}")
    assert mod.__doc__, f"tern.{name} has no module docstring"


def test_tern_mcp_does_not_shadow_sdk() -> None:
    """`tern.mcp` must not shadow the MCP SDK: absolute `import mcp` resolves to the SDK."""
    import mcp

    assert mcp.__name__ == "mcp"
    assert "tern" not in (mcp.__file__ or "")
