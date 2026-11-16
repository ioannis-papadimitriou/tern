"""``tern.testing`` is test support only (plan 06b merge review).

The package ships in the wheel, but nothing outside it may import it: its approval drivers
(``tern.testing.approvals``) decide cards as caller ``ui``, so an import from daemon or tool code
would silently auto-approve every W2/W3 call, and FakeBridge would stand in for Thunderbird."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import tern

PKG = Path(tern.__file__).resolve().parent
TESTING = PKG / "testing"


def _module_parts(path: Path) -> list[str]:
    """``src/tern/tools/registry.py`` -> ``["tern", "tools", "registry"]`` (a package's
    ``__init__`` names the package itself)."""
    rel = path.relative_to(PKG.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def imports_of_testing(path: Path) -> list[str]:
    """Every import in ``path`` that reaches ``tern.testing``, as ``line: module``; relative
    imports are resolved against the module's own package."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parts = _module_parts(path)
    package = parts if path.name == "__init__.py" else parts[:-1]
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                stem = ".".join(base + ([node.module] if node.module else []))
            else:
                stem = node.module or ""
            names = [stem] + [f"{stem}.{a.name}" for a in node.names]
        else:
            continue
        for name in names:
            if name == "tern.testing" or name.startswith("tern.testing."):
                hits.append(f"{node.lineno}: {name}")
                break
    return hits


def test_no_module_outside_tern_testing_imports_it() -> None:
    offenders = {
        str(path.relative_to(PKG.parent)): hits
        for path in sorted(PKG.rglob("*.py"))
        if TESTING not in path.parents
        for hits in [imports_of_testing(path)]
        if hits
    }
    assert offenders == {}


@pytest.mark.parametrize(
    ("rel", "source"),
    [
        ("tools/x.py", "import tern.testing.approvals\n"),
        ("tools/x.py", "from tern.testing.approvals import approve_all\n"),
        ("tools/x.py", "from tern.testing import approvals\n"),
        ("tools/x.py", "from tern import testing\n"),
        ("tools/x.py", "from ..testing import approvals\n"),
        ("tools/x.py", "from ..testing.fake_bridge import FakeBridge\n"),
        ("x.py", "from .testing import approvals\n"),
        ("tools/__init__.py", "from ..testing import approvals\n"),
        ("tools/x.py", "def f() -> None:\n    from tern.testing import approvals\n"),
    ],
)
def test_the_guard_sees_every_import_form(
    rel: str, source: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tern"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.PKG", root)
    assert imports_of_testing(path) != []


@pytest.mark.parametrize(
    "source",
    [
        "import tern.tools\n",
        "from tern.perms.approvals import Approvals\n",
        "from . import testing_helpers\n",
        "from tern import tools\n",
        '"""``tern.testing.fake_bridge.FakeBridge`` in tests."""\n',
    ],
)
def test_the_guard_ignores_other_imports(
    source: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tern"
    path = root / "bridge" / "client.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.PKG", root)
    assert imports_of_testing(path) == []
