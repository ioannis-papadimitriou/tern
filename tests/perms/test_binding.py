"""Plan 06b step 5: approval binding (args hash + nonce) and the W2 subset rule (M5).

A subset narrows a W2 approval to some of its target mids: the args are rebuilt with only those
mids, re-validated and re-hashed, and the row records both hashes. A superset, an empty set, a
subset on W3, a subset of a tool whose targets cannot be narrowed, a wrong nonce, and args changed
after the approval was opened all deny the call (``denied``/``binding_mismatch``)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import field_validator

from tern.bridge.protocol import Mid
from tern.index.store import Store
from tern.perms.approvals import ApprovalCard, approval_sha256
from tern.perms.audit import AuditLog, args_sha256
from tern.testing.approvals import Scripted, ScriptedApprovals, holding, scripted
from tern.testing.fake_bridge import FakeBridge
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, ToolError, tool
from tern.tools.registry import Registry, ToolResult

from .conftest import FakeSleep, engine_of
from .test_gate import (  # the gate tests' dummy tools and helpers
    RAN,
    TOOLS,
    Out,
    _tag,  # pyright: ignore[reportPrivateUsage] -- the gate tests' shared tag body
    card_for,
    gate_registry,
    hmid,
    new_fb,
    outcome,
    run,
    start,
    tags_of,
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


def decided(
    reg: Registry, name: str, args: dict[str, Any], choice: Scripted
) -> tuple[ToolResult | ToolError, ScriptedApprovals]:
    """Run one call whose card the UI decides with ``choice``."""

    async def scenario() -> tuple[ToolResult | ToolError, ScriptedApprovals]:
        driver = scripted(engine_of(reg).approvals, lambda card: choice)
        return await outcome(start(reg, name, args)), driver

    return run(scenario)


def assert_binding_denied(got: Any, audit: AuditLog, fb: FakeBridge, tier: Tier) -> None:
    assert isinstance(got, ToolError)
    assert (got.code, got.reason) == ("denied", "binding_mismatch")
    assert RAN == [] and writes(fb) == []
    (rec,) = audit.list()
    assert (rec.tier, rec.decision, rec.reason, rec.initial_status) == (
        tier,
        "denied",
        "binding_mismatch",
        None,
    )


# ---- subsets ------------------------------------------------------------------------------------


def test_a_subset_narrows_the_args_and_records_both_hashes(
    reg: Registry, fb: FakeBridge, audit: AuditLog, fixture_store: Store, writable: list[str]
) -> None:
    targets, keep = writable[:3], [writable[2], writable[0]]
    got, driver = decided(reg, "tag2", {"mids": targets}, Scripted("approve", subset=keep))

    assert isinstance(got, ToolResult) and driver.statuses == ["approved"]
    assert got.output == Out(n=2) and RAN == ["tag2"]
    tagged = [run(lambda m=m: tags_of(fb, hmid(fixture_store, m))) for m in targets]
    assert tagged == [[["$label1"]], [[]], [["$label1"]]]  # the deselected target is untouched
    rec = audit.get(got.call_id)
    narrowed = {"mids": [writable[0], writable[2]]}  # the original order, minus the deselected
    assert rec.target_mids == tuple(narrowed["mids"]) and rec.target_count == 2
    assert rec.args == narrowed
    assert rec.args_sha256 == args_sha256(narrowed)
    assert rec.original_args_sha256 == args_sha256({"mids": targets})
    assert (rec.decision, rec.initial_status, rec.tier) == ("approved", "tb_accepted", Tier.W2)
    token = audit.undo_token(got.undo_token or "")
    assert sorted(t.mid for t in token.payload.targets) == narrowed["mids"]


def test_a_subset_equal_to_every_target_changes_nothing(
    reg: Registry, audit: AuditLog, writable: list[str]
) -> None:
    targets = writable[:2]
    got, _ = decided(reg, "tag2", {"mids": targets}, Scripted("approve", subset=targets[::-1]))
    assert isinstance(got, ToolResult)
    rec = audit.get(got.call_id)
    assert rec.original_args_sha256 is None and rec.args_sha256 == args_sha256({"mids": targets})


@pytest.mark.parametrize(
    "subset",
    [
        pytest.param("superset", id="superset"),
        pytest.param("empty", id="empty"),
        pytest.param("other", id="partly-outside"),
    ],
)
def test_a_subset_that_is_not_a_subset_denies(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str], subset: str
) -> None:
    targets = writable[:2]
    chosen = {
        "superset": [*targets, writable[2]],
        "empty": [],
        "other": [targets[0], writable[3]],
    }[subset]
    got, driver = decided(reg, "tag2", {"mids": targets}, Scripted("approve", subset=chosen))
    assert driver.statuses == ["denied"]
    assert_binding_denied(got, audit, fb, Tier.W2)
    assert driver.resolutions == [(driver.cards[0].approval_id, "denied")]


def test_a_subset_on_a_w3_approval_denies(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    targets = writable[:2]
    got, _ = decided(reg, "tag3", {"mids": targets}, Scripted("approve", subset=targets[:1]))
    assert_binding_denied(got, audit, fb, Tier.W3)


def test_a_subset_of_a_count_targets_tool_denies(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    targets = writable[:2]
    got, _ = decided(reg, "weighted", {"mids": targets}, Scripted("approve", subset=targets[:1]))
    assert_binding_denied(got, audit, fb, Tier.W2)


@pytest.mark.parametrize(
    ("name", "subset", "rule"),
    [
        ("tag2", "superset", "subset is not a subset of the original targets"),
        ("tag3", "one", "a subset is allowed on a W2 approval only"),
        ("weighted", "one", "this tool's targets cannot be narrowed"),
    ],
)
def test_a_refused_subset_logs_its_rule(
    reg: Registry,
    writable: list[str],
    caplog: pytest.LogCaptureFixture,
    name: str,
    subset: str,
    rule: str,
) -> None:
    caplog.set_level(logging.INFO, logger="tern.perms.approvals")
    targets = writable[:2]
    chosen = [*targets, writable[2]] if subset == "superset" else targets[:1]
    got, _ = decided(reg, name, {"mids": targets}, Scripted("approve", subset=chosen))
    assert isinstance(got, ToolError) and got.reason == "binding_mismatch"
    (line,) = [r for r in caplog.records if r.getMessage() == "approval subset refused"]
    assert getattr(line, "why", None) == rule
    assert not [m for m in writable[:3] if m in str(line.__dict__)]  # names the rule, no mid


class PairIn(BoundaryModel):
    mids: list[Mid]

    @field_validator("mids")
    @classmethod
    def _two_become_one(cls, value: list[str]) -> list[str]:
        return value[:1] if len(value) == 2 else value  # drops a kept mid after a subset of 2


@tool(name="pair2", tier=Tier.W2, effects={Effect.MAIL_META}, untrusted_output=False)
async def pair2(ctx: ToolContext, inp: PairIn) -> Out:
    """A W2 tag dummy whose In validator drops the second of exactly two mids."""
    return await _tag(ctx, "pair2")


def test_a_subset_the_validator_narrows_further_targets_only_what_the_args_name(
    make_registry: Any,
    fb: FakeBridge,
    fixture_store: Store,
    sleep: FakeSleep,
    audit: AuditLog,
    writable: list[str],
) -> None:
    reg = make_registry([*TOOLS, pair2], bridge=fb, index=fixture_store, sleep=sleep)
    targets, keep = writable[:3], writable[:2]
    got, _ = decided(reg, "pair2", {"mids": targets}, Scripted("approve", subset=keep))

    assert isinstance(got, ToolResult) and RAN == ["pair2"]
    assert got.output == Out(n=1)  # ctx.targets held only the mid the narrowed args name
    tagged = [run(lambda m=m: tags_of(fb, hmid(fixture_store, m))) for m in targets]
    assert tagged == [[["$label1"]], [[]], [[]]]
    rec = audit.get(got.call_id)
    assert rec.target_mids == (writable[0],) and rec.args == {"mids": [writable[0]]}
    token = audit.undo_token(got.undo_token or "")
    assert [t.mid for t in token.payload.targets] == [writable[0]]


# ---- nonce and args binding ---------------------------------------------------------------------


@pytest.mark.parametrize("nonce", ["0" * 32, "", "é" * 32])
def test_a_wrong_nonce_denies(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str], nonce: str
) -> None:
    got, driver = decided(reg, "tag2", {"mids": writable[:1]}, Scripted("approve", nonce=nonce))
    assert driver.statuses == ["denied"]
    assert_binding_denied(got, audit, fb, Tier.W2)


def tampered(reg: Registry, args: dict[str, Any], tamper: Any) -> ToolResult | ToolError:
    """Approve a card, then change the stored approval before the call wakes."""

    async def scenario() -> ToolResult | ToolError:
        eng = engine_of(reg)
        driver = holding(eng.approvals)
        task = start(reg, "tag2", args)
        card: ApprovalCard = await card_for(driver, task)
        approval = eng.approvals.get(card.approval_id)
        assert approval is not None
        assert driver.decide(card, "approve") == "approved"
        tamper(approval)  # after the decision, before execution
        return await outcome(task)

    return run(scenario)


def test_args_changed_in_the_stored_approval_deny(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    got = tampered(reg, {"mids": writable[:1]}, lambda a: a.inp.mids.append(writable[5]))
    assert_binding_denied(got, audit, fb, Tier.W2)


def test_a_replaced_input_in_the_stored_approval_denies(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    def swap(a: Any) -> None:
        a.inp = type(a.inp)(mids=[writable[7]])

    got = tampered(reg, {"mids": writable[:1]}, swap)
    assert_binding_denied(got, audit, fb, Tier.W2)


def test_the_binding_hash_covers_the_tool_name(writable: list[str]) -> None:
    args = {"mids": writable[:1]}
    assert approval_sha256("tag2", args) != approval_sha256("tag3", args)
    assert approval_sha256("tag2", args) == approval_sha256("tag2", dict(args))
