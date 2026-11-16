"""Plan 06b step 5: approval expiry (10 min, ``Limits.approval_ttl_s``).

With a fake clock and a fake expiry timer (:class:`~tests.perms.conftest.FakeSleep`): a decide at
or after ``expires_at`` expires the approval (checked against the clock, so it is deterministic);
the timer firing at ``expires_at`` writes the ``timeout``/``expired`` row; a timer that fires early
changes nothing."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tern.index.store import Store
from tern.perms.approvals import RESOLVED_KEEP, ApprovalCard, Approvals, DecideRefused
from tern.perms.audit import AuditLog
from tern.perms.engine import Limits
from tern.testing.approvals import ScriptedApprovals, holding
from tern.testing.fake_bridge import FakeBridge
from tern.tools import Tier, ToolError
from tern.tools.registry import Registry, ToolResult

from .conftest import Clock, FakeSleep, engine_of
from .test_gate import (  # the gate tests' dummy tools and helpers
    RAN,
    card_for,
    gate_registry,
    new_fb,
    outcome,
    run,
    start,
    writable_mids,
    writes,
)


@pytest.fixture
def fb() -> FakeBridge:
    return new_fb()


@pytest.fixture
def writable(fixture_store: Store) -> list[str]:
    return writable_mids(fixture_store)


@pytest.fixture
def sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
def reg(make_registry: Any, fb: FakeBridge, fixture_store: Store, sleep: FakeSleep) -> Registry:
    return gate_registry(make_registry, fb, fixture_store, sleep)


@pytest.fixture(autouse=True)
def _reset() -> None:
    RAN.clear()


def test_the_production_ttl_is_ten_minutes() -> None:
    assert Limits().approval_ttl_s == 600  # pinned


def expired_row(audit: AuditLog) -> tuple[Any, ...]:
    (rec,) = audit.list()
    return (rec.tier, rec.decision, rec.reason, rec.initial_status)


def test_a_decide_after_600_s_is_expired(
    reg: Registry, fb: FakeBridge, audit: AuditLog, clock: Clock, writable: list[str]
) -> None:
    async def scenario() -> tuple[str, Any, ScriptedApprovals, ApprovalCard]:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        card = await card_for(driver, task)
        assert card.expires_at == card.requested_at + 600
        clock.t += 600  # exactly at expires_at: expired
        status = driver.decide(card, "approve")
        return status, await outcome(task), driver, card

    status, err, driver, card = run(scenario)
    assert status == "expired"
    assert isinstance(err, ToolError) and (err.code, err.reason) == ("expired", "expired")
    assert expired_row(audit) == (Tier.W2, "timeout", "expired", None)
    assert driver.resolutions == [(card.approval_id, "expired")]
    assert RAN == [] and writes(fb) == []


def test_a_decide_just_before_expiry_is_approved(
    reg: Registry, clock: Clock, audit: AuditLog, writable: list[str]
) -> None:
    async def scenario() -> tuple[str, Any]:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        card = await card_for(driver, task)
        clock.t += 599.5
        return driver.decide(card, "approve"), await outcome(task)

    status, res = run(scenario)
    assert status == "approved" and isinstance(res, ToolResult) and RAN == ["tag2"]


def test_the_timer_firing_writes_the_timeout_row(
    reg: Registry,
    fb: FakeBridge,
    audit: AuditLog,
    clock: Clock,
    sleep: FakeSleep,
    writable: list[str],
) -> None:
    async def scenario() -> tuple[Any, ScriptedApprovals, ApprovalCard]:
        eng = engine_of(reg)
        driver = holding(eng.approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        card = await card_for(driver, task)
        await asyncio.sleep(0)
        assert sleep.calls == [600]  # the timer sleeps until expires_at
        clock.t += 600
        sleep.fire()
        got = await outcome(task)
        assert eng.approvals.pending() == []
        return got, driver, card

    err, driver, card = run(scenario)
    assert isinstance(err, ToolError) and (err.code, err.reason) == ("expired", "expired")
    assert expired_row(audit) == (Tier.W2, "timeout", "expired", None)
    assert driver.resolutions == [(card.approval_id, "expired")]
    assert RAN == [] and writes(fb) == []


def test_a_decide_after_the_timer_expired_the_approval_is_expired(
    reg: Registry, audit: AuditLog, clock: Clock, sleep: FakeSleep, writable: list[str]
) -> None:
    async def scenario() -> tuple[Any, ScriptedApprovals, ApprovalCard]:
        eng = engine_of(reg)
        driver = holding(eng.approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        card = await card_for(driver, task)
        await asyncio.sleep(0)
        clock.t += 600
        sleep.fire()
        got = await outcome(task)  # the timer expired it: the call has ended
        assert driver.decide(card, "approve") == "expired"  # the late click, as the UI sends it
        assert driver.decide(card, "deny") == "expired"
        clock.t += 600  # remembered for ttl_s after it resolved, then forgotten
        with pytest.raises(DecideRefused) as info:
            driver.decide(card, "approve")
        assert (info.value.code, info.value.reason) == ("invalid_params", "unknown_approval")
        return got, driver, card

    err, driver, card = run(scenario)
    assert isinstance(err, ToolError) and (err.code, err.reason) == ("expired", "expired")
    assert expired_row(audit) == (Tier.W2, "timeout", "expired", None)  # one row, unchanged
    assert driver.resolutions == [(card.approval_id, "expired")]  # announced once
    assert RAN == []


def test_the_resolved_record_is_bounded(clock: Clock) -> None:
    approvals = Approvals(clock=clock, ttl_s=600, max_pending_per_caller=5)
    resolved = approvals._resolved  # pyright: ignore[reportPrivateUsage] -- the bound under test
    for i in range(RESOLVED_KEEP + 10):
        resolved[f"{i:032x}"] = ("expired", clock.t)
    approvals._prune_resolved()  # pyright: ignore[reportPrivateUsage] -- the bound under test
    assert len(resolved) == RESOLVED_KEEP and next(iter(resolved)) == f"{10:032x}"


def test_a_timer_that_fires_early_changes_nothing(
    reg: Registry, audit: AuditLog, clock: Clock, sleep: FakeSleep, writable: list[str]
) -> None:
    async def scenario() -> Any:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        card = await card_for(driver, task)
        await asyncio.sleep(0)
        clock.t += 400
        sleep.fire()  # woken before expires_at: the call keeps waiting for the rest
        for _ in range(10):
            await asyncio.sleep(0)
        assert not task.done() and audit.count() == 0
        assert sleep.calls == [600, 200]
        assert driver.decide(card, "approve") == "approved"
        return await outcome(task)

    res = run(scenario)
    assert isinstance(res, ToolResult) and RAN == ["tag2"]
