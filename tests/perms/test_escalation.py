"""Plan 06b step 1: count escalation through ``Registry.call`` with an engine. The audit row and
the log line carry the escalated tier. Since step 5 a W2/W3 call needs an approval, and these
registries have no approval UI (no notifier): it is refused up front with ``unavailable``
(``no_approval_ui``, a ``denied``/``unavailable`` row). Since step 3 a W1 call reaches target
resolution, where these dummies' synthetic mids (not in the empty index) are refused with
``unknown_mid``."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from tern.bridge.protocol import Mid
from tern.perms.audit import AuditLog
from tern.perms.engine import MAX_RECIPIENTS
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, ToolError, tool
from tern.tools.registry import Registry

RAN: list[str] = []
COUNTED: list[int] = []
SECRET = "SENTINEL-escalation-41c9"


def mids(n: int) -> list[str]:
    return [f"{i:064x}" for i in range(1, n + 1)]


class MidsIn(BoundaryModel):
    mids: list[Mid]


class DraftIn(BoundaryModel):
    mid: Mid
    to: list[str]
    body: str = ""


class BulkDraftIn(BoundaryModel):
    mids: list[Mid]
    to: list[str]


class Out(BoundaryModel):
    ok: bool


@tool(name="set_tags", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def set_tags(ctx: ToolContext, inp: MidsIn) -> Out:
    """A set_tags-shaped dummy."""
    RAN.append("set_tags")
    return Out(ok=True)


@tool(name="move", tier=Tier.W2, effects={Effect.MAIL_MOVE}, untrusted_output=False)
async def move(ctx: ToolContext, inp: MidsIn) -> Out:
    """A move-shaped dummy."""
    RAN.append("move")
    return Out(ok=True)


@tool(name="delete", tier=Tier.W3, effects={Effect.MAIL_DELETE}, untrusted_output=False)
async def delete(ctx: ToolContext, inp: MidsIn) -> Out:
    """A W3 dummy."""
    RAN.append("delete")
    return Out(ok=True)


@tool(name="sneaky_move", tier=Tier.R0, effects={Effect.MAIL_MOVE}, untrusted_output=False)
async def sneaky_move(ctx: ToolContext, inp: MidsIn) -> Out:
    """A plugin dummy declaring R0 with mail_move."""
    RAN.append("sneaky_move")
    return Out(ok=True)


def _million(inp: MidsIn) -> int:
    COUNTED.append(1)
    return 10**6


def _zero(inp: MidsIn) -> int:
    COUNTED.append(1)
    return 0


@tool(
    name="huge_tag",
    tier=Tier.W1,
    effects={Effect.MAIL_META},
    untrusted_output=False,
    count_targets=_million,
)
async def huge_tag(ctx: ToolContext, inp: MidsIn) -> Out:
    """A W1 dummy whose count_targets claims 10**6 targets."""
    RAN.append("huge_tag")
    return Out(ok=True)


@tool(
    name="huge_move",
    tier=Tier.R0,
    effects={Effect.MAIL_MOVE},
    untrusted_output=False,
    count_targets=_million,
)
async def huge_move(ctx: ToolContext, inp: MidsIn) -> Out:
    """A move-shaped plugin dummy whose count_targets claims 10**6 targets."""
    RAN.append("huge_move")
    return Out(ok=True)


@tool(
    name="lowball_tag",
    tier=Tier.W1,
    effects={Effect.MAIL_META},
    untrusted_output=False,
    count_targets=_zero,
)
async def lowball_tag(ctx: ToolContext, inp: MidsIn) -> Out:
    """A W1 dummy whose count_targets claims no targets at all."""
    RAN.append("lowball_tag")
    return Out(ok=True)


@tool(name="bulk_read", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def bulk_read(ctx: ToolContext, inp: MidsIn) -> Out:
    """A read tool over many mids."""
    RAN.append("bulk_read")
    return Out(ok=True)


@tool(
    name="draft",
    tier=Tier.W1,
    effects={Effect.DRAFTS},
    untrusted_output=False,
    recipients=lambda inp: inp.to,
)
async def draft(ctx: ToolContext, inp: DraftIn) -> Out:
    """A draft-shaped dummy."""
    RAN.append("draft")
    return Out(ok=True)


@tool(
    name="r0_with_recipients",
    tier=Tier.R0,
    effects={Effect.READ_INDEX},
    untrusted_output=False,
    recipients=lambda inp: inp.to,
)
async def r0_with_recipients(ctx: ToolContext, inp: DraftIn) -> Out:
    """A read tool that declares recipients."""
    RAN.append("r0_with_recipients")
    return Out(ok=True)


@tool(
    name="bulk_draft",
    tier=Tier.W1,
    effects={Effect.DRAFTS},
    untrusted_output=False,
    recipients=lambda inp: inp.to,
)
async def bulk_draft(ctx: ToolContext, inp: BulkDraftIn) -> Out:
    """A draft-shaped dummy over many mids (count escalation and recipients together)."""
    RAN.append("bulk_draft")
    return Out(ok=True)


def _bad_recipients(inp: DraftIn) -> list[str]:
    raise KeyError(inp.body)


@tool(
    name="bad_recipients",
    tier=Tier.W1,
    effects={Effect.DRAFTS},
    untrusted_output=False,
    recipients=_bad_recipients,
)
async def bad_recipients(ctx: ToolContext, inp: DraftIn) -> Out:
    """recipients() raises, quoting the body."""
    RAN.append("bad_recipients")
    return Out(ok=True)


TOOLS = [
    set_tags,
    move,
    delete,
    sneaky_move,
    huge_tag,
    huge_move,
    lowball_tag,
    bulk_read,
    draft,
    r0_with_recipients,
    bulk_draft,
    bad_recipients,
]


@pytest.fixture(autouse=True)
def _reset() -> None:
    RAN.clear()
    COUNTED.clear()


@pytest.fixture
def reg(make_registry: Any) -> Registry:
    return make_registry(TOOLS)


def call(reg: Registry, name: str, args: dict[str, Any], caller: str = "agent:s1") -> Any:
    return asyncio.run(reg.call(caller, name, args))


def denied(reg: Registry, name: str, args: dict[str, Any], code: str = "unavailable") -> ToolError:
    """The call fails with ``code``: ``unavailable`` for W2/W3 (step 5: no approval UI is
    attached), ``unknown_mid`` for a W1 call (step 3's target resolution: the mids are not in the
    empty index)."""
    with pytest.raises(ToolError) as info:
        call(reg, name, args)
    assert info.value.code == code
    return info.value


# ---- count escalation (the step's verify cases) -------------------------------------------------


@pytest.mark.parametrize(
    ("name", "n", "tier"),
    [
        ("set_tags", 20, Tier.W1),
        ("set_tags", 21, Tier.W2),
        ("move", 20, Tier.W2),
        ("move", 21, Tier.W3),
        ("delete", 21, Tier.W3),
        ("sneaky_move", 21, Tier.W3),
    ],
)
def test_audit_row_carries_the_escalated_tier(
    reg: Registry, audit: AuditLog, name: str, n: int, tier: Tier
) -> None:
    if tier is Tier.W1:  # step 3: a W1 call reaches target resolution (mids not indexed)
        denied(reg, name, {"mids": mids(n)}, code="unknown_mid")
        outcome = ("error", "unknown_mid")
    else:
        err = denied(reg, name, {"mids": mids(n)})
        assert tier.value in err.message and err.reason == "no_approval_ui"
        outcome = ("denied", "unavailable")
    assert RAN == []
    (rec,) = audit.list()
    assert (rec.tool, rec.tier, rec.decision, rec.reason) == (name, tier, *outcome)
    assert (rec.target_count, len(rec.target_mids)) == (n, n)
    assert rec.initial_status is None


@pytest.mark.parametrize(("name", "tier"), [("huge_tag", Tier.W2), ("huge_move", Tier.W3)])
def test_oversized_count_targets_escalates_once(
    reg: Registry, audit: AuditLog, name: str, tier: Tier
) -> None:
    denied(reg, name, {"mids": mids(1)})
    (rec,) = audit.list()
    assert (rec.tier, rec.target_count) == (tier, 10**6)
    assert len(COUNTED) == 1  # count_targets ran once: the row's count is the tier's count


def test_count_targets_cannot_lower_the_count(reg: Registry, audit: AuditLog) -> None:
    denied(reg, "lowball_tag", {"mids": mids(21)})
    (rec,) = audit.list()
    assert (rec.tier, rec.target_count) == (Tier.W2, 21)


def test_duplicate_mids_count_once_when_count_targets_floors(
    reg: Registry, audit: AuditLog
) -> None:
    # Without count_targets every listed mid counts (06a); the floor counts distinct mids.
    denied(reg, "lowball_tag", {"mids": mids(1) * 21}, code="unknown_mid")  # W1: step 3
    (rec,) = audit.list()
    assert (rec.tier, rec.target_count) == (Tier.W1, 1)


def test_read_tools_never_escalate_by_count(reg: Registry, audit: AuditLog) -> None:
    result = call(reg, "bulk_read", {"mids": mids(500)})
    assert RAN == ["bulk_read"] and result.undo_token is None
    rec = audit.get(result.call_id)
    assert (rec.tier, rec.decision, rec.initial_status, rec.target_count) == (
        Tier.R0,
        "auto",
        "ok",
        500,
    )


def test_log_line_carries_the_escalated_tier(
    reg: Registry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="tern.tools.registry")
    denied(reg, "set_tags", {"mids": mids(21)})
    (line,) = [r for r in caplog.records if r.getMessage() == "tool call"]
    assert (line.tier, line.code, line.targets) == ("W2", "unavailable", 21)  # type: ignore[attr-defined]  # extra=


def test_without_an_engine_the_tier_is_not_escalated(make_registry: Any, audit: AuditLog) -> None:
    # 06a behaviour (amendment 3): no engine, no escalation, every W call tier_not_enabled.
    reg = make_registry(TOOLS, engine=False)
    err = denied(reg, "set_tags", {"mids": mids(21)}, code="tier_not_enabled")
    assert "no permission engine" in err.message
    (rec,) = audit.list()
    assert rec.tier is Tier.W1


# ---- recipients (the engine side of step 2 builds on this) --------------------------------------

DRAFT = {"mid": "a" * 64, "to": ["Known@Example.invalid", "other@example.invalid"]}


def test_unknown_recipient_escalates_the_row(
    make_registry: Any, audit: AuditLog, recipients: Any
) -> None:
    recipients.known = {"known@example.invalid"}
    reg = make_registry(TOOLS)
    denied(reg, "draft", DRAFT)
    assert recipients.asked == ["known@example.invalid", "other@example.invalid"]
    (rec,) = audit.list()
    assert rec.tier is Tier.W2


def test_known_recipients_keep_the_declared_tier(
    make_registry: Any, audit: AuditLog, recipients: Any
) -> None:
    recipients.known = {"known@example.invalid", "other@example.invalid"}
    reg = make_registry(TOOLS)
    denied(reg, "draft", DRAFT, code="unknown_mid")  # W1: step 3's target resolution
    (rec,) = audit.list()
    assert rec.tier is Tier.W1


def test_a_read_tool_with_an_unknown_recipient_is_escalated(reg: Registry, audit: AuditLog) -> None:
    denied(reg, "r0_with_recipients", DRAFT)
    assert RAN == []
    (rec,) = audit.list()
    assert rec.tier is Tier.W2


def test_failing_recipients_is_internal_with_a_row(reg: Registry, audit: AuditLog) -> None:
    with pytest.raises(ToolError) as info:
        call(reg, "bad_recipients", {**DRAFT, "body": SECRET})
    err = info.value
    assert (err.code, err.reason) == ("internal", "recipients")
    assert SECRET not in err.message and err.__cause__ is None
    assert RAN == []
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.tier, rec.initial_status) == (
        "error",
        "internal",
        Tier.W1,
        None,
    )


class Raising:
    async def is_known(self, addr: str) -> bool:
        raise RuntimeError(f"lookup of {addr} failed")


def test_failing_provider_is_internal_with_a_row(make_registry: Any, audit: AuditLog) -> None:
    reg = make_registry(TOOLS, known_recipients=Raising())
    with pytest.raises(ToolError) as info:
        call(reg, "draft", DRAFT)
    assert (info.value.code, info.value.reason) == ("internal", "recipients")
    assert "example" not in info.value.message
    (rec,) = audit.list()
    assert (rec.decision, rec.reason) == ("error", "internal")


def test_failing_provider_row_carries_the_count_escalated_tier(
    make_registry: Any, audit: AuditLog
) -> None:
    reg = make_registry(TOOLS, known_recipients=Raising())
    with pytest.raises(ToolError) as info:
        call(reg, "bulk_draft", {"mids": mids(21), "to": ["a@example.invalid"]})
    assert (info.value.code, info.value.reason) == ("internal", "recipients")
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.tier, rec.target_count) == (
        "error",
        "internal",
        Tier.W2,
        21,
    )


def test_duplicate_recipients_are_asked_about_once(
    make_registry: Any, audit: AuditLog, recipients: Any
) -> None:
    reg = make_registry(TOOLS)
    to = ["A@example.invalid", "b@example.invalid", "a@example.invalid", "B@Example.invalid"]
    denied(reg, "draft", {"mid": "a" * 64, "to": to})
    assert recipients.asked == ["a@example.invalid", "b@example.invalid"]


def test_too_many_recipients_are_refused_before_any_lookup(
    make_registry: Any, audit: AuditLog, recipients: Any
) -> None:
    reg = make_registry(TOOLS)
    to = [f"r{i}@example.invalid" for i in range(MAX_RECIPIENTS + 1)]
    with pytest.raises(ToolError) as info:
        call(reg, "bulk_draft", {"mids": mids(21), "to": to})
    assert info.value.code == "invalid_params" and "example" not in info.value.message
    assert recipients.asked == [] and RAN == []
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.tier) == ("error", "invalid_params", Tier.W2)


def test_the_recipient_cap_counts_distinct_addresses(
    make_registry: Any, audit: AuditLog, recipients: Any
) -> None:
    reg = make_registry(TOOLS)
    to = [f"r{i}@example.invalid" for i in range(MAX_RECIPIENTS)] * 2
    denied(reg, "draft", {"mid": "a" * 64, "to": to})
    assert len(recipients.asked) == MAX_RECIPIENTS
    (rec,) = audit.list()
    assert (rec.decision, rec.reason) == ("denied", "unavailable")


class Hanging:
    async def is_known(self, addr: str) -> bool:
        await asyncio.sleep(3600)
        return True


def test_cancel_during_the_recipient_lookup_writes_a_cancelled_row(
    make_registry: Any, audit: AuditLog
) -> None:
    reg = make_registry(TOOLS, known_recipients=Hanging())

    async def run() -> None:
        task = asyncio.create_task(reg.call("agent:s1", "draft", DRAFT))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert RAN == []
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.initial_status) == ("denied", "cancelled", None)


def test_cancelled_row_carries_the_count_escalated_tier(
    make_registry: Any, audit: AuditLog
) -> None:
    reg = make_registry(TOOLS, known_recipients=Hanging())

    async def run() -> None:
        args = {"mids": mids(21), "to": ["a@example.invalid"]}
        task = asyncio.create_task(reg.call("agent:s1", "bulk_draft", args))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.tier) == ("denied", "cancelled", Tier.W2)


def test_cancel_still_propagates_when_its_row_fails(
    make_registry: Any, audit: AuditLog, caplog: pytest.LogCaptureFixture
) -> None:
    reg = make_registry(TOOLS, known_recipients=Hanging())
    caplog.set_level(logging.INFO, logger="tern.tools.registry")

    def broken(**kw: Any) -> str:
        raise ValueError("audit refused")

    async def run() -> asyncio.Task[Any]:
        task = asyncio.create_task(reg.call("agent:s1", "draft", DRAFT))
        await asyncio.sleep(0.01)
        audit.append_call = broken  # type: ignore[method-assign]  # simulate a failing audit DB
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task

    assert asyncio.run(run()).cancelled()
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == ["audit write failed: call not run"]
    lines = [r for r in caplog.records if r.getMessage() == "tool call"]
    assert [(r.levelno, r.code) for r in lines] == [(logging.WARNING, "cancelled")]  # type: ignore[attr-defined]  # extra=
