"""Shared fixtures for the permission-engine tests (plan 06b): one fake clock shared by the
``AuditLog``, the ``Engine`` and the ``ToolRuntime``; an empty read-only index; a registry factory
over dummy tools with an engine. Step 3 adds the session-scoped fixture index (the synthetic
mailbox ingested by the real ``Backfill`` over ``FakeBridge``, FTS only), opened read-only per
test (``fixture_store``), and ``make_registry(..., index=)``. Step 5 adds ``make_registry(...,
sleep=)`` (the approvals' expiry timer), :class:`FakeSleep` and :func:`engine_of`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tern import paths
from tern.config import TernConfig
from tern.index.ingest import Backfill
from tern.index.source import IngestSource
from tern.index.store import Store
from tern.perms.audit import AuditLog, audit_path
from tern.perms.engine import Engine, Limits
from tern.search.service import SearchService
from tern.testing.fake_bridge import FakeBridge
from tern.tools.contract import ToolSpec
from tern.tools.registry import RegisteredTool, Registry, ToolRuntime

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_mailbox"


def mids(n: int) -> list[str]:
    """``n`` distinct synthetic mids."""
    return [f"{i:064x}" for i in range(1, n + 1)]


class Clock:
    """A settable fake clock (epoch seconds)."""

    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeSleep:
    """An injectable ``sleep`` for the approvals' expiry timer: each call waits until the test
    calls :meth:`fire` (it records the requested seconds), so expiry follows the fake clock."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self._waiting: list[asyncio.Future[None]] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiting.append(fut)
        await fut

    def fire(self) -> None:
        """Wake every sleeper."""
        for fut in self._waiting:
            if not fut.done():
                fut.set_result(None)
        self._waiting.clear()


def engine_of(reg: Registry) -> Engine:
    """The engine a test registry was built with."""
    rt = reg._runtime  # pyright: ignore[reportPrivateUsage] -- the test reads the engine it built
    assert rt is not None and rt.engine is not None
    return rt.engine


class Recipients:
    """A ``KnownRecipients`` fake: knows ``known`` (lower-case), records every question."""

    def __init__(self, known: Iterable[str] = ()) -> None:
        self.known = set(known)
        self.asked: list[str] = []

    async def is_known(self, addr: str) -> bool:
        self.asked.append(addr)
        return addr in self.known


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def audit(clock: Clock) -> Iterator[AuditLog]:
    with AuditLog.open(audit_path(paths.resolve()), clock=clock) as log:
        yield log


@pytest.fixture
def ro_store(tmp_path: Path) -> Iterator[Store]:
    path = tmp_path / "index.db"
    Store.open(path).close()
    store = Store.open(path, readonly=True)
    yield store
    store.close()


async def _build_index(path: Path) -> None:
    store = Store.open(path)
    try:
        await Backfill(store, IngestSource(FakeBridge(FIXTURE)), TernConfig()).run()
    finally:
        store.close()


@pytest.fixture(scope="session")
def fixture_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The synthetic mailbox ingested once per session (FTS only: no embedder). Shared: open it
    only read-only (``fixture_store``)."""
    path = tmp_path_factory.mktemp("perms-index") / "index.db"
    asyncio.run(_build_index(path))
    return path


@pytest.fixture
def fixture_store(fixture_index: Path) -> Iterator[Store]:
    store = Store.open(fixture_index, readonly=True)
    yield store
    store.close()


@pytest.fixture
def recipients() -> Recipients:
    return Recipients()


MakeRegistry = Callable[..., Registry]


@pytest.fixture
def make_registry(
    audit: AuditLog, ro_store: Store, clock: Clock, recipients: Recipients
) -> MakeRegistry:
    """``make_registry(tools, *, engine=True, known_recipients=None, limits=None, bridge=None,
    index=None, sleep=None)``: a registry over ``tools`` (origin ``plugin``) whose runtime has an
    engine (or none); ``index`` replaces the empty read-only Store (e.g. ``fixture_store``);
    ``sleep`` replaces ``asyncio.sleep`` as the approvals' expiry timer (e.g. a
    :class:`FakeSleep`)."""

    def make(
        tools: Iterable[ToolSpec],
        *,
        engine: bool = True,
        known_recipients: Any = None,
        limits: Limits | None = None,
        bridge: Any = None,
        index: Store | None = None,
        sleep: Any = None,
    ) -> Registry:
        store = ro_store if index is None else index
        eng = (
            Engine(
                audit=audit,
                index=store,
                bridge=bridge,
                clock=clock,
                known_recipients=recipients if known_recipients is None else known_recipients,
                limits=limits,
                sleep=asyncio.sleep if sleep is None else sleep,
            )
            if engine
            else None
        )
        runtime = ToolRuntime(
            audit=audit,
            index=store,
            search=SearchService(store, None, mode="fts"),
            bridge=bridge,
            config=TernConfig(),
            log=logging.getLogger("tern.tools.test_perms"),
            clock=clock,
            engine=eng,
        )
        return Registry([RegisteredTool(t, "plugin", "test") for t in tools], runtime=runtime)

    return make
