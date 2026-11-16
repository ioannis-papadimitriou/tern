"""Plan 06b step 5: the approval gate.

A W2/W3 call blocks until the approval UI decides, and nothing (no row, no write) happens before;
W1 never blocks. The card is built from the index only. Non-``ui`` decides are refused; without
an approval UI a W2/W3 call is refused at once; at most 5 approvals are open per caller; a cancel
and a ternd shutdown resolve the card with their own rows. The approval drivers in
``tern.testing.approvals`` stand in for the UI. ``test_binding.py`` and ``test_expiry.py`` import
this module's dummy tools and helpers."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from tern.bridge.protocol import BridgeError, FindParams, Mid, UpdateParams
from tern.index.store import Store
from tern.perms.approvals import ApprovalCard, ApprovalStatus, DecideDecision, DecideRefused
from tern.perms.audit import AuditLog
from tern.perms.engine import Engine, Limits, RecipientCheck
from tern.testing.approvals import (
    Scripted,
    ScriptedApprovals,
    approve_all,
    deny_all,
    holding,
)
from tern.testing.fake_bridge import FakeBridge
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, ToolError, tool
from tern.tools.registry import Registry, ToolResult

from .conftest import FIXTURE, Clock, FakeSleep, Recipients, engine_of, mids

MANIFEST = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
BY_KEY = {m["key"]: m for m in MANIFEST["messages"]}
DUP = BY_KEY["dup-1"]["mid"]  # acct2/INBOX + acct2/Projects
SINGLE = BY_KEY["inv-0042"]["mid"]  # acct1/Invoices only
SINGLE_HMID = BY_KEY["inv-0042"]["message_id"].strip("<>")
NOT_INDEXED = mids(1)[0]
PLANTED = 731_904  # FakeBridge(first_tb_id=PLANTED): distinctive tb_ids to look for

RAN: list[str] = []


class MidsIn(BoundaryModel):
    mids: list[Mid]


class MoveIn(BoundaryModel):
    mids: list[Mid]
    dest_folder_id: str
    source_folder_id: str | None = None


class CountIn(BoundaryModel):
    mids: list[Mid]
    weight: int = 1


class DraftIn(BoundaryModel):
    mids: list[Mid]
    to: list[str]


class Out(BoundaryModel):
    n: int


async def _tag(ctx: ToolContext, name: str) -> Out:
    RAN.append(name)
    tb_ids = [loc.tb_id for locs in ctx.targets.values() for loc in locs]
    await ctx.bridge.request("mail.update", UpdateParams(tb_ids=tb_ids, add_tags=["$label1"]))
    return Out(n=len(tb_ids))


@tool(name="tag2", tier=Tier.W2, effects={Effect.MAIL_META}, untrusted_output=False)
async def tag2(ctx: ToolContext, inp: MidsIn) -> Out:
    """A W2 tag dummy: adds $label1 on every engine-resolved location."""
    return await _tag(ctx, "tag2")


@tool(name="tag3", tier=Tier.W3, effects={Effect.MAIL_META}, untrusted_output=False)
async def tag3(ctx: ToolContext, inp: MidsIn) -> Out:
    """A W3 tag dummy."""
    return await _tag(ctx, "tag3")


@tool(name="tag1", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def tag1(ctx: ToolContext, inp: MidsIn) -> Out:
    """A W1 tag dummy (21+ targets escalate it to W2)."""
    return await _tag(ctx, "tag1")


@tool(
    name="weighted",
    tier=Tier.W2,
    effects={Effect.MAIL_META},
    untrusted_output=False,
    count_targets=lambda inp: len(inp.mids) * inp.weight,
)
async def weighted(ctx: ToolContext, inp: CountIn) -> Out:
    """A W2 dummy that declares count_targets (its targets cannot be narrowed)."""
    return await _tag(ctx, "weighted")


@tool(name="move", tier=Tier.W2, effects={Effect.MAIL_MOVE}, untrusted_output=False)
async def move(ctx: ToolContext, inp: MoveIn) -> Out:
    """A move-shaped W2 dummy (target resolution only here)."""
    RAN.append("move")
    return Out(n=0)


@tool(
    name="draft",
    tier=Tier.W1,
    effects={Effect.DRAFTS},
    untrusted_output=False,
    recipients=lambda inp: list(inp.to),
)
async def draft(ctx: ToolContext, inp: DraftIn) -> Out:
    """A draft-shaped W1 dummy (an unknown recipient escalates it to W2)."""
    RAN.append("draft")
    return Out(n=0)


TOOLS = [tag1, tag2, tag3, weighted, move, draft]


@pytest.fixture(autouse=True)
def _reset() -> None:
    RAN.clear()


def new_fb() -> FakeBridge:
    return FakeBridge(FIXTURE, first_tb_id=PLANTED)


def writable_mids(store: Store) -> list[str]:
    """Indexed single-location mids a write can target (a Message-ID, not conflicted)."""
    rows = store.connection.execute(
        "SELECT m.mid FROM messages m JOIN locations l ON l.mid = m.mid "
        "WHERE m.mid NOT LIKE 'x-%' AND m.conflicted = 0 AND m.header_message_id IS NOT NULL "
        "GROUP BY m.mid HAVING count(*) = 1 ORDER BY m.mid LIMIT 25"
    ).fetchall()
    assert len(rows) == 25
    return [str(r[0]) for r in rows]


def gate_registry(make_registry: Any, fb: FakeBridge, store: Store, sleep: FakeSleep) -> Registry:
    """A registry over this module's dummies (FakeBridge, fixture index, fake expiry timer)."""
    return make_registry(TOOLS, bridge=fb, index=store, sleep=sleep)


# The fixtures (test_binding.py and test_expiry.py define the same four over the helpers above).


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


def methods(fb: FakeBridge) -> list[str]:
    return [m for m, _ in fb.calls]


def writes(fb: FakeBridge) -> list[str]:
    return [m for m in methods(fb) if m != "mail.messages.find"]


async def tags_of(fb: FakeBridge, mid_hmid: str) -> list[list[str]]:
    got = (await fb.request("mail.messages.find", FindParams(header_message_id=mid_hmid))).root
    return [sorted(f.tags) for f in got]


def hmid(store: Store, mid: str) -> str:
    msg = store.get_message(mid)
    assert msg is not None and msg.header_message_id is not None
    return msg.header_message_id


async def until(cond: Callable[[], bool], what: str = "condition") -> None:
    """Yield to the event loop until ``cond()`` holds (the call reached its next await)."""
    for _ in range(200):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")


async def card_for(
    driver: ScriptedApprovals, task: asyncio.Future[Any], n: int = 1
) -> ApprovalCard:
    """The ``n``-th card the driver received (the call is then blocked on it)."""
    await until(lambda: len(driver.cards) >= n or task.done(), "a card")
    assert not task.done(), "the call ended without asking for approval"
    return driver.cards[n - 1]


def start(reg: Registry, name: str, args: dict[str, Any], **kw: Any) -> asyncio.Future[Any]:
    caller = kw.pop("caller", "agent:s1")
    return asyncio.ensure_future(reg.call(caller, name, args, **kw))


async def outcome(task: asyncio.Future[Any]) -> ToolResult | ToolError:
    try:
        return await task
    except ToolError as exc:
        return exc


def run(fn: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    return asyncio.run(fn())


# ---- W2/W3 block until decided; W1 does not -----------------------------------------------------


@pytest.mark.parametrize(("name", "tier"), [("tag2", Tier.W2), ("tag3", Tier.W3)])
def test_a_w2_w3_call_blocks_until_approved_and_nothing_runs_before(
    reg: Registry,
    fb: FakeBridge,
    audit: AuditLog,
    fixture_store: Store,
    writable: list[str],
    name: str,
    tier: Tier,
) -> None:
    targets = writable[:2]
    eng = engine_of(reg)

    async def scenario() -> ToolResult:
        driver = holding(eng.approvals)
        task = start(reg, name, {"mids": targets})
        card = await card_for(driver, task)
        for _ in range(20):  # still blocked after many loop turns
            await asyncio.sleep(0)
        assert not task.done()
        assert RAN == [] and writes(fb) == [] and audit.count() == 0  # no row while pending
        assert [c.approval_id for c in eng.approvals.pending()] == [card.approval_id]
        assert (card.tool, card.tier, card.caller) == (name, tier, "agent:s1")
        assert driver.decide(card, "approve") == "approved"
        res = await task
        assert driver.resolutions == [(card.approval_id, "approved")]
        return res

    res = run(scenario)
    assert RAN == [name] and res.output == Out(n=2)
    assert writes(fb) == ["mail.update"]
    for mid in targets:
        assert run(lambda m=mid: tags_of(fb, hmid(fixture_store, m))) == [["$label1"]]
    rec = audit.get(res.call_id)
    assert (rec.tier, rec.decision, rec.reason, rec.initial_status) == (
        tier,
        "approved",
        None,
        "tb_accepted",
    )
    assert rec.target_mids == tuple(targets) and rec.original_args_sha256 is None
    assert res.undo_token is not None and rec.undo_token == res.undo_token
    assert engine_of(reg).approvals.pending() == []


def test_a_count_escalated_call_gets_a_card(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    async def scenario() -> ToolResult:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag1", {"mids": writable[:21]})
        card = await card_for(driver, task)
        assert card.tier is Tier.W2 and len(card.targets) == 21
        driver.decide(card, "approve")
        return await task

    res = run(scenario)
    assert audit.get(res.call_id).tier is Tier.W2 and RAN == ["tag1"]


def test_w1_does_not_block(reg: Registry, fb: FakeBridge, writable: list[str]) -> None:
    async def scenario() -> tuple[ToolResult, ScriptedApprovals]:
        driver = holding(engine_of(reg).approvals)
        return await reg.call("agent:s1", "tag1", {"mids": writable[:1]}), driver

    res, driver = run(scenario)
    assert RAN == ["tag1"] and driver.cards == [] and res.undo_token is not None


def test_approve_all_and_deny_all_drivers(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    async def scenario() -> tuple[Any, Any]:
        eng = engine_of(reg)
        yes = approve_all(eng.approvals)
        first = await outcome(start(reg, "tag2", {"mids": writable[:1]}))
        no = deny_all(eng.approvals)
        second = await outcome(start(reg, "tag2", {"mids": writable[1:2]}))
        assert yes.statuses == ["approved"] and no.statuses == ["denied"]
        return first, second

    first, second = run(scenario)
    assert isinstance(first, ToolResult) and RAN == ["tag2"]
    assert isinstance(second, ToolError)
    assert (second.code, second.reason) == ("denied", "user_denied")


# ---- outcomes and their rows --------------------------------------------------------------------


def only_row(audit: AuditLog) -> tuple[Any, ...]:
    (rec,) = audit.list()
    return (rec.tier, rec.decision, rec.reason, rec.initial_status, rec.undo_token)


def test_user_deny_writes_a_denied_row_and_nothing_runs(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    async def scenario() -> tuple[Any, ScriptedApprovals, ApprovalCard]:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": writable[:2]})
        card = await card_for(driver, task)
        assert driver.decide(card, "deny") == "denied"
        assert audit.count() == 1  # the row is written when the approval resolves
        return await outcome(task), driver, card

    err, driver, card = run(scenario)
    assert isinstance(err, ToolError) and (err.code, err.reason) == ("denied", "user_denied")
    assert RAN == [] and writes(fb) == []
    assert only_row(audit) == (Tier.W2, "denied", "user_denied", None, None)
    assert driver.resolutions == [(card.approval_id, "denied")]


def test_without_an_approval_ui_a_w2_call_is_unavailable_at_once(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    assert engine_of(reg).approvals.notifier is None
    with pytest.raises(ToolError) as info:
        asyncio.run(reg.call("agent:s1", "tag2", {"mids": writable[:2]}))
    assert (info.value.code, info.value.reason) == ("unavailable", "no_approval_ui")
    assert RAN == [] and fb.calls == []  # not even a find: refused before target resolution
    assert only_row(audit) == (Tier.W2, "denied", "unavailable", None, None)
    assert engine_of(reg).approvals.pending() == []


def test_a_non_ui_decide_is_refused_and_the_approval_stays_open(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    async def scenario() -> Any:
        eng = engine_of(reg)
        driver = holding(eng.approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        card = await card_for(driver, task)
        for caller in ("mcp:conn-1", "agent:s1", "job:triage"):
            with pytest.raises(DecideRefused) as info:
                eng.approvals.decide(card.approval_id, card.nonce, "approve", caller=caller)
            assert (info.value.code, info.value.reason) == ("denied", "approval_role")
        assert not task.done() and audit.count() == 0 and RAN == []
        assert [c.approval_id for c in eng.approvals.pending()] == [card.approval_id]
        driver.decide(card, "approve")
        return await task

    res = run(scenario)
    assert isinstance(res, ToolResult) and RAN == ["tag2"]


def test_an_unknown_or_resolved_approval_cannot_be_decided(
    reg: Registry, writable: list[str]
) -> None:
    async def scenario() -> None:
        eng = engine_of(reg)
        driver = holding(eng.approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        card = await card_for(driver, task)
        with pytest.raises(DecideRefused) as info:
            eng.approvals.decide("0" * 32, card.nonce, "approve", caller="ui")
        assert (info.value.code, info.value.reason) == ("invalid_params", "unknown_approval")
        with pytest.raises(DecideRefused) as info:
            eng.approvals.decide(card.approval_id, card.nonce, "maybe", caller="ui")  # type: ignore[arg-type]  # a malformed decision on purpose
        assert (info.value.code, info.value.reason) == ("invalid_params", "decision")
        driver.decide(card, "deny")
        with pytest.raises(DecideRefused) as info:
            eng.approvals.decide(card.approval_id, card.nonce, "approve", caller="ui")
        assert info.value.reason == "unknown_approval"
        await outcome(task)

    run(scenario)
    assert RAN == []


def test_the_sixth_pending_approval_of_a_caller_is_rate_limited(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    assert Limits().max_pending_per_caller == 5  # pinned

    async def scenario() -> tuple[Any, list[Any]]:
        eng = engine_of(reg)
        driver = holding(eng.approvals)
        tasks = [start(reg, "tag2", {"mids": [writable[i]]}) for i in range(5)]
        await card_for(driver, tasks[0], n=5)
        sixth = await outcome(start(reg, "tag2", {"mids": [writable[5]]}))
        other = start(reg, "tag2", {"mids": [writable[6]]}, caller="mcp:conn-2")
        await card_for(driver, other, n=6)  # another caller still gets a card
        assert len(eng.approvals.pending()) == 6
        assert len(eng.approvals.pending("agent:s1")) == 5
        for card in list(driver.cards):
            driver.decide(card, "deny")
        rest = [await outcome(t) for t in [*tasks, other]]
        return sixth, rest

    sixth, rest = run(scenario)
    assert isinstance(sixth, ToolError)
    assert (sixth.code, sixth.reason) == ("rate_limited", "pending_cap")
    assert all(isinstance(r, ToolError) and r.reason == "user_denied" for r in rest)
    rows = [(r.decision, r.reason) for r in audit.list()]
    assert rows.count(("denied", "rate_limited")) == 1
    assert rows.count(("denied", "user_denied")) == 6
    assert RAN == []


def test_cancelling_the_call_while_pending_writes_a_cancelled_row(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    async def scenario() -> tuple[ScriptedApprovals, ApprovalCard]:
        eng = engine_of(reg)
        driver = holding(eng.approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        card = await card_for(driver, task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert eng.approvals.pending() == []
        with pytest.raises(DecideRefused):
            eng.approvals.decide(card.approval_id, card.nonce, "approve", caller="ui")
        return driver, card

    driver, card = run(scenario)
    assert only_row(audit) == (Tier.W2, "denied", "cancelled", None, None)
    assert driver.resolutions == [(card.approval_id, "denied")]
    assert RAN == [] and writes(fb) == []


def test_shutdown_denies_every_open_approval(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    async def scenario() -> tuple[int, list[Any]]:
        eng = engine_of(reg)
        driver = holding(eng.approvals)
        tasks = [
            start(reg, "tag2", {"mids": [writable[0]]}),
            start(reg, "tag3", {"mids": [writable[1]]}, caller="mcp:conn-1"),
        ]
        await card_for(driver, tasks[0], n=2)
        n = eng.approvals.shutdown()
        assert audit.count() == 2  # rows written by the shutdown itself
        return n, [await outcome(t) for t in tasks]

    n, got = run(scenario)
    assert n == 2
    assert all(
        isinstance(e, ToolError) and (e.code, e.reason) == ("denied", "shutdown") for e in got
    )
    assert sorted((r.tier, r.decision, r.reason) for r in audit.list()) == [
        (Tier.W2, "denied", "shutdown"),
        (Tier.W3, "denied", "shutdown"),
    ]
    assert RAN == [] and writes(fb) == []


class BrokenUI(ScriptedApprovals):
    """A notifier whose ``requested`` fails with a bridge error (the UI is gone)."""

    async def requested(self, card: ApprovalCard) -> None:
        self.cards.append(card)
        raise BridgeError("unavailable", "no extension attached")


def test_a_notifier_bridge_error_is_unavailable(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    eng = engine_of(reg)
    eng.approvals.attach(BrokenUI(eng.approvals, lambda card: None))
    with pytest.raises(ToolError) as info:
        asyncio.run(reg.call("agent:s1", "tag2", {"mids": writable[:1]}))
    assert (info.value.code, info.value.reason) == ("unavailable", "notify_failed")
    assert only_row(audit) == (Tier.W2, "error", "unavailable", None, None)
    assert eng.approvals.pending() == [] and RAN == []


# ---- the card -----------------------------------------------------------------------------------


def test_the_card_is_built_from_the_index(
    reg: Registry, fb: FakeBridge, fixture_store: Store, writable: list[str]
) -> None:
    targets = [DUP, writable[0]]
    prov = [writable[1], NOT_INDEXED, writable[1]]

    async def scenario() -> ApprovalCard:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": targets}, provenance=prov)
        card = await card_for(driver, task)
        driver.decide(card, "deny")
        await outcome(task)
        return card

    card = run(scenario)
    paths = {f.folder_id: f.path for f in fixture_store.folders()}
    expected = []
    for mid in targets:
        msg = fixture_store.get_message(mid)
        assert msg is not None
        for loc in fixture_store.locations(mid):
            expected.append(
                (mid, loc.folder_id, paths[loc.folder_id], msg.from_addr, msg.subject, msg.date_utc)
            )
    got = [
        (t.mid, t.folder_id, t.folder_path, t.from_addr, t.subject, t.date_utc)
        for t in card.targets
    ]
    assert got == expected and len(got) == 3  # DUP's two locations, then the single one
    assert card.args_view == {"mids": targets}
    assert card.recipients is None  # the tool declares none
    p1 = fixture_store.get_message(writable[1])
    assert p1 is not None
    assert [(p.mid, p.from_addr, p.from_name, p.subject) for p in card.provenance] == [
        (writable[1], p1.from_addr, p1.from_name, p1.subject),
        (NOT_INDEXED, None, None, None),  # not in the index: nulls, never invented
    ]
    assert card.expires_at - card.requested_at == 600
    assert len(card.nonce) == 32 and card.nonce not in repr(card)
    # No mail-derived string (from, subject, folder path) in the card's repr either. (The
    # synthetic folder ids embed the path, so the path is checked by its field name.)
    shown = [v for t in card.targets for v in (t.from_addr, t.from_name, t.subject)]
    shown += [v for p in card.provenance for v in (p.from_addr, p.from_name, p.subject)]
    assert any(shown) and not [v for v in shown if v and v in repr(card)]
    assert not [f for f in ("folder_path", "from_", "subject") if f in repr(card)]
    # No tb_id anywhere on the card (FakeBridge's ids start at PLANTED).
    text = json.dumps(dataclasses.asdict(card), default=str)
    assert all(str(PLANTED + i) not in text for i in range(400))


def test_the_card_lists_the_calls_recipient_checks(
    make_registry: Any, fb: FakeBridge, fixture_store: Store, sleep: FakeSleep
) -> None:
    provider = Recipients(known={"known@example.invalid"})
    reg = make_registry(
        TOOLS, bridge=fb, index=fixture_store, sleep=sleep, known_recipients=provider
    )

    async def scenario() -> ApprovalCard:
        driver = holding(engine_of(reg).approvals)
        args = {"mids": [SINGLE], "to": ["Known@Example.invalid", "stranger@example.invalid"]}
        task = start(reg, "draft", args)
        card = await card_for(driver, task)
        driver.decide(card, "deny")
        await outcome(task)
        return card

    card = run(scenario)
    assert card.tier is Tier.W2
    assert card.recipients == (
        RecipientCheck("known@example.invalid", True),
        RecipientCheck("stranger@example.invalid", False),
    )
    assert card.args_view["to"] == ["Known@Example.invalid", "stranger@example.invalid"]
    assert "example.invalid" not in repr(card).lower()  # no address in the card's repr


def test_an_ambiguous_move_target_is_refused_with_its_row(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    async def scenario() -> tuple[Any, ScriptedApprovals]:
        driver = holding(engine_of(reg).approvals)
        args = {"mids": [DUP], "dest_folder_id": "acct2:/Archive"}
        return await outcome(start(reg, "move", args)), driver

    err, driver = run(scenario)
    assert isinstance(err, ToolError)
    assert (err.code, err.reason) == ("ambiguous_location", "ambiguous_location")
    assert "2 folders" in err.message and driver.cards == [] and RAN == []
    assert only_row(audit) == (Tier.W2, "error", "ambiguous_location", None, None)
    assert methods(fb) == []  # refused from the index, before any live find


def test_an_unknown_target_is_refused_before_a_card(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    async def scenario() -> tuple[Any, ScriptedApprovals]:
        driver = holding(engine_of(reg).approvals)
        return await outcome(start(reg, "tag2", {"mids": [NOT_INDEXED]})), driver

    err, driver = run(scenario)
    assert isinstance(err, ToolError) and (err.code, err.reason) == ("unknown_mid", "not_indexed")
    assert driver.cards == [] and only_row(audit)[1:3] == ("error", "unknown_mid")


def test_engine_rejects_a_notifier_without_the_protocol(audit: AuditLog, ro_store: Store) -> None:
    from tern.perms.engine import EngineError

    with pytest.raises(EngineError):
        Engine(
            audit=audit,
            index=ro_store,
            bridge=None,
            clock=lambda: 0.0,
            known_recipients=Recipients(),
            notifier=object(),  # type: ignore[arg-type]  # not a notifier on purpose
        )


class FailingUI(ScriptedApprovals):
    """A notifier whose ``requested`` has a bug (not a bridge error): it propagates."""

    async def requested(self, card: ApprovalCard) -> None:
        self.cards.append(card)
        raise LookupError("notifier bug")


def test_a_notifier_bug_propagates_with_an_internal_row(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    eng = engine_of(reg)
    eng.approvals.attach(FailingUI(eng.approvals, lambda card: None))
    with pytest.raises(LookupError):
        asyncio.run(reg.call("agent:s1", "tag2", {"mids": writable[:1]}))
    assert only_row(audit) == (Tier.W2, "error", "internal", None, None)
    assert eng.approvals.pending() == [] and RAN == []


def test_a_cancel_right_after_approval_runs_nothing(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    async def scenario() -> tuple[ScriptedApprovals, ApprovalCard]:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        card = await card_for(driver, task)
        assert driver.decide(card, "approve") == "approved"
        task.cancel()  # before the call woke up to execute
        with pytest.raises(asyncio.CancelledError):
            await task
        return driver, card

    driver, card = run(scenario)
    assert only_row(audit) == (Tier.W2, "denied", "cancelled", None, None)
    assert driver.resolutions == [(card.approval_id, "denied")]
    assert RAN == [] and writes(fb) == []


class DecidesThenFailsUI(ScriptedApprovals):
    """Decides as scripted, then its ``requested`` has a bug (not a bridge error)."""

    async def requested(self, card: ApprovalCard) -> None:
        await super().requested(card)
        raise LookupError("notifier bug after the decision")


def test_a_notifier_bug_after_approval_runs_nothing_with_an_internal_row(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    eng = engine_of(reg)
    ui = DecidesThenFailsUI(eng.approvals, lambda card: Scripted("approve"))
    eng.approvals.attach(ui)
    with pytest.raises(LookupError):
        asyncio.run(reg.call("agent:s1", "tag2", {"mids": writable[:1]}))
    (card,) = ui.cards
    assert ui.statuses == ["approved"]
    assert only_row(audit) == (Tier.W2, "error", "internal", None, None)
    assert ui.resolutions == [(card.approval_id, "denied")]
    assert eng.approvals.pending() == [] and RAN == [] and writes(fb) == []


class AnnounceFailsUI(ScriptedApprovals):
    """Approves every card; its ``resolved`` has a bug on ``approved``."""

    def resolved(self, approval_id: str, status: ApprovalStatus) -> None:
        super().resolved(approval_id, status)
        if status == "approved":
            raise LookupError("notifier bug")


def test_a_failed_approved_announcement_runs_nothing_and_is_not_tb_accepted(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    eng = engine_of(reg)
    ui = AnnounceFailsUI(eng.approvals, lambda card: Scripted("approve"))
    eng.approvals.attach(ui)
    with pytest.raises(LookupError):
        asyncio.run(reg.call("agent:s1", "tag2", {"mids": writable[:1]}))
    (rec,) = audit.list()
    assert (rec.decision, rec.initial_status) == ("approved", "tb_accepted")  # write-ahead row
    assert rec.status == "error"  # never a stale tb_accepted for a write that was not sent
    (event,) = audit.events(rec.call_id)
    assert (event.kind, event.detail["reason"]) == ("error", "notifier_failed")
    assert RAN == [] and writes(fb) == []


class HangingUI(ScriptedApprovals):
    """A notifier whose ``requested`` never returns (records that it was cancelled)."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.cancelled = False

    async def requested(self, card: ApprovalCard) -> None:
        self.cards.append(card)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_a_hanging_notifier_cannot_hold_an_approval_past_expiry(
    reg: Registry,
    fb: FakeBridge,
    audit: AuditLog,
    clock: Clock,
    sleep: FakeSleep,
    writable: list[str],
) -> None:
    async def scenario() -> tuple[Any, HangingUI]:
        eng = engine_of(reg)
        ui = HangingUI(eng.approvals, lambda card: None)
        eng.approvals.attach(ui)
        task = start(reg, "tag2", {"mids": writable[:1]})
        await until(lambda: sleep.calls == [600], "the expiry timer")  # runs during requested
        assert len(eng.approvals.pending()) == 1
        clock.t += 600
        sleep.fire()
        got = await outcome(task)
        assert eng.approvals.pending() == []
        return got, ui

    err, ui = run(scenario)
    assert isinstance(err, ToolError) and (err.code, err.reason) == ("expired", "expired")
    assert ui.cancelled  # the hanging request is cancelled, not left behind
    assert only_row(audit) == (Tier.W2, "timeout", "expired", None, None)
    assert RAN == [] and writes(fb) == []


def test_a_failing_timer_propagates_with_an_internal_row(
    make_registry: Any, fb: FakeBridge, fixture_store: Store, audit: AuditLog, writable: list[str]
) -> None:
    async def broken_sleep(seconds: float) -> None:
        raise ArithmeticError("timer bug")

    reg = make_registry(TOOLS, bridge=fb, index=fixture_store, sleep=broken_sleep)

    async def scenario() -> None:
        eng = engine_of(reg)
        holding(eng.approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        await until(task.done, "the call to end")  # not a busy retry loop
        with pytest.raises(ArithmeticError):
            await task
        assert eng.approvals.pending() == []

    run(scenario)
    assert only_row(audit) == (Tier.W2, "error", "internal", None, None)
    assert RAN == [] and writes(fb) == []


class DecidesThenBreaksUI(ScriptedApprovals):
    """Decides as scripted, then its ``requested`` fails with a bridge error (the link dropped
    after the UI's decision went through)."""

    async def requested(self, card: ApprovalCard) -> None:
        await super().requested(card)
        raise BridgeError("unavailable", "extension disconnected")


@pytest.mark.parametrize(("choice", "ran"), [("approve", ["tag2"]), ("deny", [])])
def test_a_bridge_error_after_the_decision_keeps_the_decision(
    reg: Registry, audit: AuditLog, writable: list[str], choice: DecideDecision, ran: list[str]
) -> None:
    eng = engine_of(reg)
    ui = DecidesThenBreaksUI(eng.approvals, lambda card: Scripted(choice))
    eng.approvals.attach(ui)

    async def scenario() -> ToolResult | ToolError:
        return await outcome(start(reg, "tag2", {"mids": writable[:1]}))

    got = run(scenario)
    (rec,) = audit.list()
    if choice == "approve":
        assert isinstance(got, ToolResult) and rec.decision == "approved"
    else:
        assert isinstance(got, ToolError) and (rec.decision, rec.reason) == (
            "denied",
            "user_denied",
        )
    assert RAN == ran
