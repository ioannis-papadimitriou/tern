"""Shared fixtures for the tool tests (plan 06a).

``plugins``: writes synthetic plugin modules into a temp dir and fakes the ``tern.tools`` entry
points (``importlib.metadata.entry_points`` patched), so discovery runs on real files and real
``EntryPoint.load()`` without installing a package.

``fixture_index`` (module-scoped, step 5): the synthetic mailbox ingested by the real
``Backfill`` over ``FakeBridge`` and embedded by ``EmbedQueue.drain`` with the ``StubEmbedder``,
built once per test module. ``tool_env`` opens an index read-only and wires a ``Registry`` of the
built-ins around it (fresh audit DB under the test's ``TERN_HOME``), so tests call tools only
through ``Registry.call``.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tern import paths
from tern.bridge.client import ExtensionRequester
from tern.config import TernConfig
from tern.index.ingest import Backfill
from tern.index.source import IngestSource
from tern.index.store import Store
from tern.perms.audit import AuditLog, audit_path
from tern.search import vec
from tern.search.embed_queue import EmbedQueue
from tern.search.embedder import Embedder
from tern.search.service import Mode, SearchService
from tern.testing.fake_bridge import FakeBridge
from tern.testing.stub_embedder import StubEmbedder
from tern.tools.registry import ENTRY_POINT_GROUP, Registry, ToolRuntime, builtin_tools

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_mailbox"
STUB = StubEmbedder(dim=64)

_HEADER = """
from tern.tools import BoundaryModel, Effect, Tier, tool


class EchoIn(BoundaryModel):
    text: str


class EchoOut(BoundaryModel):
    text: str
"""

_TOOL = '''

@tool(
    name="{name}",
    tier=Tier.R0,
    effects=[Effect.READ_INDEX],
    untrusted_output=False,{extra}
)
async def {fn}(ctx, inp: EchoIn) -> EchoOut:
    """Echo the text ({name})."""
    return EchoOut(text=inp.text)
'''


def plugin_source(*names: str, recipients: Iterable[str] = (), export: str = "TOOLS") -> str:
    """A plugin module defining one echo tool per name (``recipients`` names declare
    ``recipients``) and exporting them as a list under ``export``."""
    with_recipients = set(recipients)
    parts = [_HEADER]
    for i, name in enumerate(names):
        extra = "\n    recipients=lambda i: [i.text]," if name in with_recipients else ""
        parts.append(_TOOL.format(name=name, fn=f"tool_{i}", extra=extra))
    fns = ", ".join(f"tool_{i}" for i in range(len(names)))
    parts.append(f"\n{export} = [{fns}]\n")
    return "".join(parts)


@dataclass
class Plugins:
    root: Path
    monkeypatch: pytest.MonkeyPatch

    def write(self, relpath: str, text: str) -> Path:
        """Write ``text`` to ``root/relpath``; returns the absolute path."""
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def module(self, text: str) -> str:
        """An importable module (unique name, on ``sys.path``) holding ``text``; its name."""
        name = f"tern_test_plugin_{uuid.uuid4().hex}"
        self.write(f"mods/{name}.py", text)
        return name

    def entry_points(self, *pairs: tuple[str, str]) -> None:
        """Fake the ``tern.tools`` group as these ``(name, "module:attr")`` entry points."""
        eps = importlib.metadata.EntryPoints(
            importlib.metadata.EntryPoint(name, value, ENTRY_POINT_GROUP) for name, value in pairs
        )

        def fake(**params: Any) -> importlib.metadata.EntryPoints:
            assert params == {"group": ENTRY_POINT_GROUP}
            return eps

        self.monkeypatch.setattr(importlib.metadata, "entry_points", fake)


@pytest.fixture
def plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Plugins:
    root = tmp_path / "plugins"
    (root / "mods").mkdir(parents=True)
    monkeypatch.syspath_prepend(str(root / "mods"))
    p = Plugins(root, monkeypatch)
    p.entry_points()  # no real entry points leak in from the environment
    return p


# ---- the fixture index and a registry of the built-ins (step 5) ---------------------------------


async def _build_index(path: Path) -> None:
    store = Store.open(path)
    try:
        await Backfill(store, IngestSource(FakeBridge(FIXTURE)), TernConfig()).run()
        embedded = await EmbedQueue(store, STUB, TernConfig(embed_model=STUB.model_id)).drain()
        assert embedded > 0 and vec.pending_count(store.connection) == 0
    finally:
        store.close()


@pytest.fixture(scope="module")
def fixture_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The synthetic mailbox, ingested and fully embedded (read it only through a read-only
    Store: the file is shared by the module's tests)."""
    path = tmp_path_factory.mktemp("tools-index") / "index.db"
    asyncio.run(_build_index(path))
    return path


@dataclass
class ToolEnv:
    registry: Registry
    audit: AuditLog
    store: Store
    search: SearchService


class EnvFactory:
    """Builds :class:`ToolEnv` s; every Store and audit log it opened is closed at teardown."""

    def __init__(self) -> None:
        self._open: list[Store | AuditLog] = []

    def __call__(
        self,
        index: Path,
        *,
        mode: Mode = "hybrid",
        embedder: Embedder | None = STUB,
        bridge: ExtensionRequester | None = None,
    ) -> ToolEnv:
        store = Store.open(index, readonly=True)
        self._open.append(store)
        audit = AuditLog.open(audit_path(paths.resolve()))
        self._open.append(audit)
        search = SearchService(store, embedder if mode == "hybrid" else None, mode=mode)
        runtime = ToolRuntime(
            audit=audit,
            index=store,
            search=search,
            bridge=bridge,
            config=TernConfig(),
            log=logging.getLogger("tern.tools.test_builtin"),
            clock=time.time,
        )
        return ToolEnv(Registry(builtin_tools(), runtime=runtime), audit, store, search)

    def close(self) -> None:
        for item in reversed(self._open):
            item.close()


@pytest.fixture
def tool_env() -> Iterator[EnvFactory]:
    factory = EnvFactory()
    yield factory
    factory.close()
