"""Plan 06b step 2: recipient escalation (D3, B-H4) on a draft-shaped W1 dummy.

Any unknown recipient makes the call at least W2; the stub provider (``DenyAllRecipients``) knows
nobody; a provider raising ``RecipientsUnavailable`` makes every recipient unknown (W2, a WARNING
with the count); any other provider exception is ``internal``. The approval card lists every
recipient with its ``known`` flag. W2/W3 calls need an approval since step 5 and these
registries attach no approval UI, so they are refused with ``unavailable``; since step 3 a W1
call reaches target resolution, where the dummy's mid (not in the empty index) is refused with
``unknown_mid``. Either way nothing runs and the tier is read from the audit row."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from tern.bridge.protocol import Mid
from tern.index.store import Store
from tern.perms.audit import AuditLog
from tern.perms.engine import Engine, RecipientCheck, RecipientChecks, card_recipients
from tern.perms.recipients import DenyAllRecipients, KnownRecipients, RecipientsUnavailable
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, ToolError, tool
from tern.tools.registry import Registry

from .conftest import Clock, Recipients

RAN: list[str] = []
SECRET_ADDR = "sentinel-recipient-7d2e@example.invalid"


class DraftIn(BoundaryModel):
    mid: Mid
    to: list[str]
    cc: list[str] = []


class Out(BoundaryModel):
    ok: bool


@tool(
    name="draft",
    tier=Tier.W1,
    effects={Effect.DRAFTS},
    untrusted_output=False,
    recipients=lambda inp: [*inp.to, *inp.cc],
)
async def draft(ctx: ToolContext, inp: DraftIn) -> Out:
    """A draft-shaped dummy."""
    RAN.append("draft")
    return Out(ok=True)


def _unavailable_recipients(inp: DraftIn) -> list[str]:
    raise RecipientsUnavailable("tool code raising the provider's failure type")


@tool(
    name="sly_draft",
    tier=Tier.W1,
    effects={Effect.DRAFTS},
    untrusted_output=False,
    recipients=_unavailable_recipients,
)
async def sly_draft(ctx: ToolContext, inp: DraftIn) -> Out:
    """A draft dummy whose own ``recipients()`` raises ``RecipientsUnavailable``."""
    RAN.append("sly_draft")
    return Out(ok=True)


TOOLS = [draft, sly_draft]
M = "a" * 64
A = "a@example.invalid"
B = "b@example.invalid"


class Unavailable:
    """A provider that answers ``known`` for the first ``answers`` addresses, then cannot answer."""

    def __init__(self, answers: int = 0, *, message: str = "contacts.lookup failed") -> None:
        self.answers = answers
        self.message = message
        self.asked: list[str] = []

    async def is_known(self, addr: str) -> bool:
        self.asked.append(addr)
        if len(self.asked) > self.answers:
            raise RecipientsUnavailable(self.message)
        return True


class Broken:
    async def is_known(self, addr: str) -> bool:
        raise RuntimeError("provider bug")


@pytest.fixture(autouse=True)
def _reset() -> None:
    RAN.clear()


def call(reg: Registry, name: str, args: dict[str, Any]) -> Any:
    return asyncio.run(reg.call("agent:s1", name, args))


def denied_tier(reg: Registry, audit: AuditLog, args: dict[str, Any], name: str = "draft") -> Tier:
    """Call ``name``; nothing may run and it must write one row: return its tier. W2/W3 are
    refused (step 5: no approval UI attached, ``unavailable``); W1 is refused by step 3's target
    resolution (the mid is not in the empty index)."""
    with pytest.raises(ToolError) as info:
        call(reg, name, args)
    assert RAN == []
    (rec,) = audit.list()
    assert rec.tier is not None
    if rec.tier is Tier.W1:
        assert info.value.code == "unknown_mid"
        assert (rec.decision, rec.reason) == ("error", "unknown_mid")
    else:
        assert (info.value.code, info.value.reason) == ("unavailable", "no_approval_ui")
        assert (rec.decision, rec.reason) == ("denied", "unavailable")
    return rec.tier


def engine(audit: AuditLog, ro_store: Store, clock: Clock, provider: KnownRecipients) -> Engine:
    return Engine(audit=audit, index=ro_store, bridge=None, clock=clock, known_recipients=provider)


def checks(eng: Engine, args: dict[str, Any]) -> RecipientChecks:
    return asyncio.run(eng.check_recipients(draft, DraftIn.model_validate(args)))


# ---- the stub -----------------------------------------------------------------------------------


def test_deny_all_is_a_provider_that_knows_nobody() -> None:
    stub = DenyAllRecipients()
    assert isinstance(stub, KnownRecipients)
    assert asyncio.run(stub.is_known(A)) is False


def test_with_the_stub_a_draft_is_w2(make_registry: Any, audit: AuditLog) -> None:
    reg = make_registry(TOOLS, known_recipients=DenyAllRecipients())
    assert denied_tier(reg, audit, {"mid": M, "to": [A]}) is Tier.W2


def test_with_the_stub_the_card_marks_every_recipient_unknown(
    audit: AuditLog, ro_store: Store, clock: Clock
) -> None:
    eng = engine(audit, ro_store, clock, DenyAllRecipients())
    got = card_recipients(checks(eng, {"mid": M, "to": [A], "cc": [B]}))
    assert got == [{"addr": A, "known": False}, {"addr": B, "known": False}]


# ---- a fake provider ----------------------------------------------------------------------------


def test_all_known_keeps_w1(make_registry: Any, audit: AuditLog) -> None:
    reg = make_registry(TOOLS, known_recipients=Recipients({A, B}))
    assert denied_tier(reg, audit, {"mid": M, "to": [A], "cc": [B]}) is Tier.W1


def test_one_unknown_is_w2(make_registry: Any, audit: AuditLog) -> None:
    reg = make_registry(TOOLS, known_recipients=Recipients({A}))
    assert denied_tier(reg, audit, {"mid": M, "to": [A], "cc": [B]}) is Tier.W2


def test_one_unknown_card_flags(audit: AuditLog, ro_store: Store, clock: Clock) -> None:
    eng = engine(audit, ro_store, clock, Recipients({A}))
    got = card_recipients(checks(eng, {"mid": M, "to": [A], "cc": [B]}))
    assert got == [{"addr": A, "known": True}, {"addr": B, "known": False}]


def test_mixed_case_addresses_are_matched_lower_cased(
    make_registry: Any, audit: AuditLog, ro_store: Store, clock: Clock
) -> None:
    provider = Recipients({A, B})
    reg = make_registry(TOOLS, known_recipients=provider)
    args = {"mid": M, "to": ["A@Example.INVALID"], "cc": ["b@EXAMPLE.invalid"]}
    assert denied_tier(reg, audit, args) is Tier.W1
    assert provider.asked == [A, B]
    got = card_recipients(checks(engine(audit, ro_store, clock, Recipients({A})), args))
    assert got == [{"addr": A, "known": True}, {"addr": B, "known": False}]


# ---- error policy -------------------------------------------------------------------------------


def test_unavailable_provider_is_w2_with_a_warning(
    make_registry: Any, audit: AuditLog, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    provider = Unavailable(message=f"lookup of {SECRET_ADDR} failed")
    reg = make_registry(TOOLS, known_recipients=provider)
    assert denied_tier(reg, audit, {"mid": M, "to": [SECRET_ADDR, A], "cc": [B]}) is Tier.W2
    # The provider is not asked again once it said it cannot answer.
    assert provider.asked == [SECRET_ADDR]
    warnings = [r for r in caplog.records if r.name == "tern.perms.engine"]
    assert len(warnings) == 1
    (rec,) = warnings
    assert rec.levelno == logging.WARNING
    assert rec.__dict__["recipients"] == 3 and rec.__dict__["tool"] == "draft"
    # Counts only: no address (and not the provider's message) in any log record.
    for r in caplog.records:
        assert SECRET_ADDR not in r.getMessage()
        assert SECRET_ADDR not in repr(r.__dict__)


def test_unavailable_is_on_the_call_log_line_with_its_call_id(
    make_registry: Any, audit: AuditLog, caplog: pytest.LogCaptureFixture
) -> None:
    # The engine's WARNING has no call id; the call's own log line says the lookup failed and
    # carries the call_id of the audit row, so an operator can match the two.
    caplog.set_level(logging.INFO, logger="tern.tools.registry")
    reg = make_registry(TOOLS, known_recipients=Unavailable())
    assert denied_tier(reg, audit, {"mid": M, "to": [A]}) is Tier.W2
    (rec,) = audit.list()
    (line,) = [r for r in caplog.records if r.getMessage() == "tool call"]
    assert line.__dict__["recipients_unavailable"] is True
    assert line.__dict__["call_id"] == rec.call_id
    assert (line.__dict__["tool"], line.__dict__["tier"]) == ("draft", "W2")


@pytest.mark.parametrize(
    "provider", [Recipients({A}), DenyAllRecipients()], ids=["one_unknown", "deny_all"]
)
def test_answered_unknown_is_not_flagged_unavailable(
    make_registry: Any, audit: AuditLog, caplog: pytest.LogCaptureFixture, provider: Any
) -> None:
    caplog.set_level(logging.INFO, logger="tern.tools.registry")
    reg = make_registry(TOOLS, known_recipients=provider)
    assert denied_tier(reg, audit, {"mid": M, "to": [A, B]}) is Tier.W2
    (line,) = [r for r in caplog.records if r.getMessage() == "tool call"]
    assert line.__dict__["recipients_unavailable"] is False


def test_check_recipients_reports_unavailable(
    audit: AuditLog, ro_store: Store, clock: Clock
) -> None:
    assert checks(engine(audit, ro_store, clock, Unavailable()), {"mid": M, "to": [A]}).unavailable
    got = checks(engine(audit, ro_store, clock, Recipients({A})), {"mid": M, "to": [A, B]})
    assert got.unavailable is False
    assert got == (RecipientCheck(A, True), RecipientCheck(B, False))


def test_unavailable_makes_every_recipient_unknown_on_the_card(
    audit: AuditLog, ro_store: Store, clock: Clock
) -> None:
    # The provider answered "known" for A, then failed on B: A counts as unknown too.
    provider = Unavailable(answers=1)
    eng = engine(audit, ro_store, clock, provider)
    got = card_recipients(checks(eng, {"mid": M, "to": [A, B, "c@example.invalid"]}))
    assert provider.asked == [A, B]
    assert got == [
        {"addr": A, "known": False},
        {"addr": B, "known": False},
        {"addr": "c@example.invalid", "known": False},
    ]


def test_unavailable_after_known_answers_is_still_w2(make_registry: Any, audit: AuditLog) -> None:
    reg = make_registry(TOOLS, known_recipients=Unavailable(answers=1))
    assert denied_tier(reg, audit, {"mid": M, "to": [A, B]}) is Tier.W2


def test_other_provider_exception_is_internal(make_registry: Any, audit: AuditLog) -> None:
    reg = make_registry(TOOLS, known_recipients=Broken())
    with pytest.raises(ToolError) as info:
        call(reg, "draft", {"mid": M, "to": [A]})
    assert (info.value.code, info.value.reason) == ("internal", "recipients")
    assert RAN == []
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.tier) == ("error", "internal", Tier.W1)


def test_tool_code_raising_unavailable_is_internal_not_escalated(
    make_registry: Any, audit: AuditLog, recipients: Recipients
) -> None:
    # Only the provider's declared failure escalates; the same type from the tool's own
    # recipients() is a tool failure.
    reg = make_registry(TOOLS)
    with pytest.raises(ToolError) as info:
        call(reg, "sly_draft", {"mid": M, "to": [A]})
    assert (info.value.code, info.value.reason) == ("internal", "recipients")
    assert recipients.asked == [] and RAN == []
    (rec,) = audit.list()
    assert (rec.decision, rec.reason) == ("error", "internal")
