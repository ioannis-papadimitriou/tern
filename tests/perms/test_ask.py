"""Decisions #39: ``Registry.call(..., ask=notifier)`` asks the user before a call below W2 runs
(a chat in ``manual`` mode), through the engine's approvals queue and the registry's audit rows.

The asking UI is a :class:`~tern.testing.approvals.ScriptedApprovals` passed as ``ask`` and NOT
attached to the engine (the engine's own notifier stays None, as in ternd until plan 12): the card
reaches whoever the call names. Synthetic data only."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tern.index.store import Store
from tern.perms.approvals import Approvals
from tern.perms.audit import AuditLog
from tern.testing.approvals import Scripted, ScriptedApprovals
from tern.testing.fake_bridge import FakeBridge
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, ToolError, tool
from tern.tools.registry import Registry, ToolResult

from .conftest import Clock, FakeSleep, engine_of
from .test_gate import TOOLS as GATE_TOOLS
from .test_gate import card_for, new_fb, outcome, run, start, writable_mids, writes

READS: list[str] = []
SENTINEL = "SENTINEL-ask-args-41c7"


class QueryIn(BoundaryModel):
    query: str
    limit: int | None = None


class CountOut(BoundaryModel):
    n: int


@tool(name="peek", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def peek(ctx: ToolContext, inp: QueryIn) -> CountOut:
    """An R0 read dummy that records that it ran."""
    READS.append(inp.query)
    return CountOut(n=len(inp.query))


@tool(name="peek1", tier=Tier.R1, effects={Effect.READ_INDEX}, untrusted_output=False)
async def peek1(ctx: ToolContext, inp: QueryIn) -> CountOut:
    """An R1 read dummy that records that it ran."""
    READS.append(inp.query)
    return CountOut(n=0)


@pytest.fixture(autouse=True)
def _reset() -> None:
    READS.clear()


@pytest.fixture
def fb() -> FakeBridge:
    return new_fb()


@pytest.fixture
def sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
def reg(make_registry: Any, fb: FakeBridge, fixture_store: Store, sleep: FakeSleep) -> Registry:
    return make_registry([peek, peek1, *GATE_TOOLS], bridge=fb, index=fixture_store, sleep=sleep)


def asker(approvals: Approvals, script: Any = None) -> ScriptedApprovals:
    """A chat tab stand-in: decides as ``script`` says (None: holds every card). Not attached."""
    return ScriptedApprovals(approvals, script if script is not None else (lambda card: None))


def rows(audit: AuditLog) -> list[tuple[Any, ...]]:
    return [
        (r.tool, r.tier, r.decision, r.reason, r.initial_status)
        for r in reversed(audit.list(limit=100))
    ]


@pytest.mark.parametrize(("name", "tier"), [("peek", Tier.R0), ("peek1", Tier.R1)])
def test_an_asked_read_waits_for_the_decision_then_runs_recorded_approved(
    reg: Registry, audit: AuditLog, name: str, tier: Tier
) -> None:
    eng = engine_of(reg)

    async def scenario() -> tuple[Any, ScriptedApprovals]:
        tab = asker(eng.approvals)
        task = start(reg, name, {"query": SENTINEL}, ask=tab)
        card = await card_for(tab, task)
        for _ in range(20):  # still blocked after many loop turns
            await asyncio.sleep(0)
        assert not task.done()
        assert READS == [] and audit.count() == 0  # nothing ran, no row while pending
        assert (card.tool, card.tier, card.caller, card.targets) == (name, tier, "agent:s1", ())
        assert card.args_view == {"query": SENTINEL, "limit": None}
        assert eng.approvals.notifier is None  # the engine's own UI was never involved
        assert tab.decide(card, "approve") == "approved"
        return await outcome(task), tab

    res, tab = run(scenario)
    assert isinstance(res, ToolResult) and READS == [SENTINEL]
    assert rows(audit) == [(name, tier, "approved", None, "ok")]
    assert tab.resolutions == [(tab.cards[0].approval_id, "approved")]


def test_a_denied_read_never_runs_and_the_model_gets_denied(reg: Registry, audit: AuditLog) -> None:
    async def scenario() -> tuple[Any, ScriptedApprovals]:
        tab = asker(engine_of(reg).approvals, lambda card: Scripted("deny"))
        return await outcome(start(reg, "peek", {"query": "q"}, ask=tab)), tab

    err, tab = run(scenario)
    assert isinstance(err, ToolError) and (err.code, err.reason) == ("denied", "user_denied")
    assert READS == []
    assert rows(audit) == [("peek", Tier.R0, "denied", "user_denied", None)]
    assert tab.resolutions == [(tab.cards[0].approval_id, "denied")]


def test_without_ask_a_read_runs_at_once_recorded_auto(reg: Registry, audit: AuditLog) -> None:
    res = run(lambda: reg.call("agent:s1", "peek", {"query": "q"}))
    assert isinstance(res, ToolResult) and READS == ["q"]
    assert rows(audit) == [("peek", Tier.R0, "auto", None, "ok")]
    assert engine_of(reg).approvals.pending() == []


def test_a_cancelled_wait_writes_the_cancelled_row(reg: Registry, audit: AuditLog) -> None:
    async def scenario() -> None:
        tab = asker(engine_of(reg).approvals)
        task = start(reg, "peek", {"query": "q"}, ask=tab)
        await card_for(tab, task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert engine_of(reg).approvals.pending() == []

    run(scenario)
    assert READS == []
    assert rows(audit) == [("peek", Tier.R0, "denied", "cancelled", None)]


def test_an_unanswered_ask_expires_with_the_timeout_row(
    reg: Registry, audit: AuditLog, clock: Clock, sleep: FakeSleep
) -> None:
    async def scenario() -> Any:
        tab = asker(engine_of(reg).approvals)
        task = start(reg, "peek", {"query": "q"}, ask=tab)
        await card_for(tab, task)
        await asyncio.sleep(0)
        clock.t += 600
        sleep.fire()
        return await outcome(task)

    err = run(scenario)
    assert isinstance(err, ToolError) and (err.code, err.reason) == ("expired", "expired")
    assert READS == []
    assert rows(audit) == [("peek", Tier.R0, "timeout", "expired", None)]


def test_a_wrong_nonce_denies_the_asked_call(reg: Registry, audit: AuditLog) -> None:
    async def scenario() -> Any:
        tab = asker(engine_of(reg).approvals, lambda card: Scripted("approve", nonce="0" * 32))
        return await outcome(start(reg, "peek", {"query": "q"}, ask=tab))

    err = run(scenario)
    assert isinstance(err, ToolError) and (err.code, err.reason) == ("denied", "binding_mismatch")
    assert READS == []
    assert rows(audit) == [("peek", Tier.R0, "denied", "binding_mismatch", None)]


def test_an_asked_call_takes_no_subset(reg: Registry, audit: AuditLog) -> None:
    async def scenario() -> Any:
        tab = asker(engine_of(reg).approvals, lambda card: Scripted("approve", subset=["a" * 64]))
        return await outcome(start(reg, "peek", {"query": "q"}, ask=tab))

    err = run(scenario)
    assert isinstance(err, ToolError) and err.reason == "binding_mismatch"
    assert READS == []


def test_a_card_the_asker_cannot_show_refuses_the_call(reg: Registry, audit: AuditLog) -> None:
    from tern.bridge.protocol import BridgeError

    class Gone(ScriptedApprovals):
        async def requested(self, card: Any) -> None:
            raise BridgeError("unavailable", "connection closed")

    async def scenario() -> Any:
        return await outcome(
            start(reg, "peek", {"query": "q"}, ask=Gone(engine_of(reg).approvals, lambda c: None))
        )

    err = run(scenario)
    assert isinstance(err, ToolError) and err.code == "unavailable"
    assert READS == []
    assert rows(audit) == [("peek", Tier.R0, "error", "unavailable", None)]


def test_an_asked_w1_call_is_asked_then_runs_as_a_w1_write_recorded_approved(
    reg: Registry, audit: AuditLog, fb: FakeBridge, fixture_store: Store
) -> None:
    mid = writable_mids(fixture_store)[0]

    async def scenario() -> Any:
        tab = asker(engine_of(reg).approvals)
        task = start(reg, "tag1", {"mids": [mid]}, ask=tab)
        card = await card_for(tab, task)
        assert writes(fb) == [] and card.tier is Tier.W1 and card.targets == ()
        tab.decide(card, "approve")
        return await outcome(task)

    res = run(scenario)
    assert isinstance(res, ToolResult) and res.undo_token is not None
    assert writes(fb) == ["mail.update"]
    assert rows(audit) == [("tag1", Tier.W1, "approved", None, "tb_accepted")]


def test_ask_never_replaces_a_w2_approval(
    reg: Registry, audit: AuditLog, fixture_store: Store
) -> None:
    """W2/W3 keep their own approval (the engine's UI): with none attached the call is refused
    at once, and the chat's asker is never shown it (#7/#10 are not moved or lifted)."""
    mid = writable_mids(fixture_store)[0]

    async def scenario() -> tuple[Any, ScriptedApprovals]:
        tab = asker(engine_of(reg).approvals, lambda card: Scripted("approve"))
        return await outcome(start(reg, "tag2", {"mids": [mid]}, ask=tab)), tab

    err, tab = run(scenario)
    assert isinstance(err, ToolError) and (err.code, err.reason) == (
        "unavailable",
        "no_approval_ui",
    )
    assert tab.cards == []
    assert rows(audit) == [("tag2", Tier.W2, "denied", "unavailable", None)]


def test_ask_without_an_engine_refuses_the_call(make_registry: Any, audit: AuditLog) -> None:
    reg = make_registry([peek], engine=False)

    class Never(ScriptedApprovals):
        def __init__(self) -> None:
            self.cards = []

        async def requested(self, card: Any) -> None:
            raise AssertionError("no card without an engine")

        def resolved(self, approval_id: str, status: Any) -> None:
            raise AssertionError("nothing to resolve")

    async def scenario() -> Any:
        return await outcome(start(reg, "peek", {"query": "q"}, ask=Never()))

    err = run(scenario)
    assert isinstance(err, ToolError) and (err.code, err.reason) == (
        "unavailable",
        "no_approval_ui",
    )
    assert READS == []
    assert rows(audit) == [("peek", Tier.R0, "denied", "unavailable", None)]


def test_the_ask_cap_is_the_per_caller_pending_cap(reg: Registry, audit: AuditLog) -> None:
    eng = engine_of(reg)

    async def scenario() -> Any:
        tab = asker(eng.approvals)
        tasks = [start(reg, "peek", {"query": str(i)}, ask=tab) for i in range(6)]
        for _ in range(50):
            await asyncio.sleep(0)
        refused = [t for t in tasks if t.done()]
        assert len(refused) == 1 and len(eng.approvals.pending("agent:s1")) == 5
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return await outcome(refused[0])

    err = run(scenario)
    assert isinstance(err, ToolError) and (err.code, err.reason) == ("rate_limited", "pending_cap")
    assert READS == []
