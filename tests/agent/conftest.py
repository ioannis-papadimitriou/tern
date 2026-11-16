"""Shared fixtures for the agent-loop tests (plan 08a step 2b).

The loop runs over the real stack: a temp index of the synthetic mailbox (FTS only, ingested once
per module through ``FakeBridge``), a real ``Registry`` (built-ins plus test-only dummies) whose
``ToolRuntime`` has a real ``Engine`` (no approval notifier: W2/W3 calls are refused), a fresh
``AuditLog`` under the test's ``TERN_HOME``, and the real ``LLMClient`` over ``FakeLLM``. No
``Registry`` stub: every tool call is a ``Registry.call``.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from tern import paths
from tern.agent.loop import AgentSession, Hold
from tern.bridge.protocol import Mid
from tern.config import TernConfig
from tern.index.ingest import Backfill
from tern.index.source import IngestSource
from tern.index.store import Store
from tern.llm.client import LLMClient
from tern.perms.audit import AuditLog, AuditRecord, audit_path
from tern.perms.engine import Engine
from tern.search.service import SearchService
from tern.testing.fake_bridge import FakeBridge
from tern.testing.fake_llm import FakeLLM, ScriptStep
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, tool
from tern.tools.registry import RegisteredTool, Registry, ToolRuntime, builtin_tools

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_mailbox"
MANIFEST = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
#: A fixture message in one folder (acct1/Invoices).
SINGLE: str = next(m["mid"] for m in MANIFEST["messages"] if m["key"] == "inv-0042")
SESSION_KEY = "test-session"
CALLER = f"agent:{SESSION_KEY}"

#: Tool bodies that ran (the refused dummies must never appear).
RAN: list[str] = []
#: Set by ``dummy_slow`` once it runs (a fresh event per test).
SLOW_STARTED: list[asyncio.Event] = []


class EchoIn(BoundaryModel):
    text: str


class EchoOut(BoundaryModel):
    text: str


class TargetIn(BoundaryModel):
    mid: Mid


class CountOut(BoundaryModel):
    n: int


@tool(name="dummy_untrusted", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=True)
async def dummy_untrusted(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Echo the text as if it came from an email (untrusted R0 dummy)."""
    RAN.append("dummy_untrusted")
    return EchoOut(text=inp.text)


@tool(name="dummy_r1", tier=Tier.R1, effects={Effect.READ_LIVE}, untrusted_output=False)
async def dummy_r1(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Echo the text (R1 dummy)."""
    RAN.append("dummy_r1")
    return EchoOut(text=inp.text)


@tool(name="dummy_w2", tier=Tier.W2, effects={Effect.MAIL_MOVE}, untrusted_output=False)
async def dummy_w2(ctx: ToolContext, inp: TargetIn) -> CountOut:
    """Move one message (W2 dummy)."""
    RAN.append("dummy_w2")
    return CountOut(n=1)


@tool(
    name="dummy_filter",
    tier=Tier.R0,  # claims to be harmless
    effects={Effect.FILTERS_WRITE},
    untrusted_output=False,
    count_targets=lambda inp: 1,
)
async def dummy_filter(ctx: ToolContext, inp: EchoIn) -> CountOut:
    """Create a mail filter (forbidden-effect dummy)."""
    RAN.append("dummy_filter")
    return CountOut(n=1)


@tool(name="dummy_slow", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def dummy_slow(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Wait until cancelled (R0 dummy)."""
    SLOW_STARTED[-1].set()
    await asyncio.Event().wait()
    return EchoOut(text=inp.text)


DUMMIES = (dummy_untrusted, dummy_r1, dummy_w2, dummy_filter, dummy_slow)


class Unknown:
    """A ``KnownRecipients`` that knows nobody (no dummy declares recipients)."""

    async def is_known(self, addr: str) -> bool:
        return False


async def _build_index(path: Path) -> None:
    store = Store.open(path)
    try:
        await Backfill(store, IngestSource(FakeBridge(FIXTURE)), TernConfig()).run()
    finally:
        store.close()


@pytest.fixture(scope="module")
def fixture_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The synthetic mailbox ingested once per module (FTS only); opened read-only per test."""
    path = tmp_path_factory.mktemp("agent-index") / "index.db"
    asyncio.run(_build_index(path))
    return path


@dataclass
class RecordingHold:
    """A ``hold`` that records its reasons and depth; ``depth`` > 0 while held."""

    depth: int = 0
    reasons: list[str] = field(default_factory=list[str])

    @asynccontextmanager
    async def __call__(self, reason: str) -> AsyncIterator[None]:
        self.reasons.append(reason)
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1


class SpyTransport(httpx.AsyncBaseTransport):
    """Forwards to ``FakeLLM.transport`` and records the hold depth at every request."""

    def __init__(self, fake: FakeLLM, hold: RecordingHold) -> None:
        self.fake = fake
        self.hold = hold
        self.depths: list[int] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.depths.append(self.hold.depth)
        return await self.fake.transport.handle_async_request(request)


@dataclass
class Env:
    registry: Registry
    audit: AuditLog
    store: Store
    runtime: ToolRuntime
    clients: list[LLMClient] = field(default_factory=list[LLMClient])

    def rows(self) -> list[AuditRecord]:
        """Every audit row, oldest first."""
        return list(reversed(self.audit.list(limit=None)))

    def session(
        self,
        fake: FakeLLM,
        *,
        hold: Hold | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] | None = None,
        registry: Registry | None = None,
        **cfg: Any,
    ) -> AgentSession:
        """A session over ``fake`` (or ``transport``) with config overrides ``cfg``, over
        ``registry`` (default: the env's)."""
        config = TernConfig.model_validate({"chat_model": fake.model_id, **cfg})
        llm = LLMClient(config, transport=transport or fake.transport)
        self.clients.append(llm)
        ticks = itertools.count()
        return AgentSession(
            SESSION_KEY,
            registry or self.registry,
            llm,
            self.store,
            config,
            hold=hold,
            clock=clock or (lambda: float(next(ticks))),
        )


@pytest.fixture
def env(fixture_index: Path) -> Iterator[Env]:
    RAN.clear()
    SLOW_STARTED.clear()
    store = Store.open(fixture_index, readonly=True)
    audit = AuditLog.open(audit_path(paths.resolve()), clock=time.time)
    bridge = FakeBridge(FIXTURE)
    engine = Engine(
        audit=audit, index=store, bridge=bridge, clock=time.time, known_recipients=Unknown()
    )
    runtime = ToolRuntime(
        audit=audit,
        index=store,
        search=SearchService(store, None, mode="fts"),
        bridge=bridge,
        config=TernConfig(),
        log=logging.getLogger("tern.tools.test_agent"),
        clock=time.time,
        engine=engine,
    )
    tools = [*builtin_tools(), *(RegisteredTool(t, "plugin", "test") for t in DUMMIES)]
    e = Env(Registry(tools, runtime=runtime), audit, store, runtime)
    yield e
    for client in e.clients:
        asyncio.run(client.aclose())
    audit.close()
    store.close()


def registry_with(env: Env, *extra: Any) -> Registry:
    """The built-ins plus the test-only tools ``extra`` over the env's runtime."""
    tools = [*builtin_tools(), *(RegisteredTool(t, "plugin", "test") for t in extra)]
    return Registry(tools, runtime=env.runtime)


def steps(*items: ScriptStep) -> FakeLLM:
    return FakeLLM.from_steps(*items)


def tool_messages(request: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in request["messages"] if m.get("role") == "tool"]
