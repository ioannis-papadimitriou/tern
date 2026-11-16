"""Plan 06b step 1: the engine skeleton and the pure tier computation (``count_tier``,
``recipient_tier``, ``effective_tier``), ``Limits`` and the ``Engine`` constructor checks."""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any

import pytest

from tern.bridge.protocol import Mid
from tern.index.store import Store
from tern.perms.audit import AuditLog
from tern.perms.engine import (
    COUNT_ESCALATION_THRESHOLD,
    Engine,
    EngineError,
    Limits,
    RecipientCheck,
    count_tier,
    effective_tier,
    recipient_tier,
)
from tern.perms.recipients import KnownRecipients
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, tool
from tern.tools.contract import ToolSpec

W = (Tier.W1, Tier.W2, Tier.W3)


class MidsIn(BoundaryModel):
    mids: list[Mid]


class DraftIn(BoundaryModel):
    mid: Mid
    to: list[str]


class Out(BoundaryModel):
    ok: bool


@tool(name="set_tags", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def set_tags(ctx: ToolContext, inp: MidsIn) -> Out:
    """A set_tags-shaped dummy."""
    return Out(ok=True)


@tool(name="move", tier=Tier.W2, effects={Effect.MAIL_MOVE}, untrusted_output=False)
async def move(ctx: ToolContext, inp: MidsIn) -> Out:
    """A move-shaped dummy."""
    return Out(ok=True)


@tool(name="delete", tier=Tier.W3, effects={Effect.MAIL_DELETE}, untrusted_output=False)
async def delete(ctx: ToolContext, inp: MidsIn) -> Out:
    """A delete-shaped dummy."""
    return Out(ok=True)


@tool(name="sneaky_move", tier=Tier.R0, effects={Effect.MAIL_MOVE}, untrusted_output=False)
async def sneaky_move(ctx: ToolContext, inp: MidsIn) -> Out:
    """A plugin that declares R0 but moves mail."""
    return Out(ok=True)


@tool(name="reader", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def reader(ctx: ToolContext, inp: MidsIn) -> Out:
    """A read tool."""
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
    return Out(ok=True)


# ---- count_tier ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "n", "expected"),
    [
        (Tier.R0, 21, Tier.R0),
        (Tier.R1, 10**6, Tier.R1),
        (Tier.W1, 0, Tier.W1),
        (Tier.W1, 20, Tier.W1),
        (Tier.W1, 21, Tier.W2),
        (Tier.W2, 20, Tier.W2),
        (Tier.W2, 21, Tier.W3),
        (Tier.W3, 21, Tier.W3),
        (Tier.W1, 10**6, Tier.W2),
    ],
)
def test_count_tier(base: Tier, n: int, expected: Tier) -> None:
    assert count_tier(base, n) is expected


def test_threshold_is_twenty() -> None:
    assert COUNT_ESCALATION_THRESHOLD == 20


@pytest.mark.parametrize("n", [-1, True, 1.5, "21", None])
def test_count_tier_refuses_a_bad_count(n: Any) -> None:
    with pytest.raises(ValueError):
        count_tier(Tier.W1, n)


# ---- effective_tier -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "n", "expected"),
    [
        (set_tags, 20, Tier.W1),
        (set_tags, 21, Tier.W2),
        (move, 21, Tier.W3),
        (delete, 21, Tier.W3),
        (sneaky_move, 20, Tier.W2),
        (sneaky_move, 21, Tier.W3),
        (sneaky_move, 10**6, Tier.W3),
        (reader, 10**6, Tier.R0),
    ],
)
def test_effective_tier_count_escalation(spec: ToolSpec, n: int, expected: Tier) -> None:
    assert effective_tier(spec, n, recipients_known=True) is expected


@pytest.mark.parametrize(
    ("n", "known", "expected"),
    [
        (1, True, Tier.W1),
        (1, False, Tier.W2),
        (21, True, Tier.W2),
        # max(W1, count W2, recipient W2): count escalation steps up from the spec's tier, it
        # does not compound with the recipient tier (plan 06b step 1's formula).
        (21, False, Tier.W2),
    ],
)
def test_effective_tier_recipients(n: int, known: bool, expected: Tier) -> None:
    assert effective_tier(draft, n, recipients_known=known) is expected


def test_recipient_tier_needs_declared_recipients() -> None:
    assert recipient_tier(set_tags, recipients_known=False) is Tier.R0
    assert recipient_tier(draft, recipients_known=False) is Tier.W2
    assert recipient_tier(draft, recipients_known=True) is Tier.R0
    with pytest.raises(TypeError):
        recipient_tier(draft, recipients_known=1)  # type: ignore[arg-type]  # runtime check


@pytest.mark.parametrize(
    ("spec", "n", "known"),
    list(
        itertools.product(
            [set_tags, move, delete, sneaky_move, reader, draft],
            [0, 1, 20, 21, 10**6],
            [True, False],
        )
    ),
)
def test_effective_tier_never_below_the_spec_tier(spec: ToolSpec, n: int, known: bool) -> None:
    got = effective_tier(spec, n, recipients_known=known)
    assert got.rank >= spec.effective_tier.rank
    if spec.effective_tier in W and n > COUNT_ESCALATION_THRESHOLD:
        assert got.rank == min(spec.effective_tier.rank + 1, Tier.W3.rank)


# ---- Limits -------------------------------------------------------------------------------------


def test_limits_defaults_are_the_production_values() -> None:
    lim = Limits()
    assert (
        lim.w1_per_caller,
        lim.w1_global,
        lim.window_s,
        lim.approval_ttl_s,
        lim.max_pending_per_caller,
    ) == (500, 1000, 3600, 600, 5)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "5"])
def test_limits_refuse_bad_values(bad: Any) -> None:
    with pytest.raises(EngineError):
        Limits(w1_per_caller=bad)


# ---- Engine construction ------------------------------------------------------------------------


class Known:
    def __init__(self, answer: Any = True) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def is_known(self, addr: str) -> bool:
        self.asked.append(addr)
        return self.answer


def _engine(log: AuditLog, store: Store, **kw: Any) -> Engine:
    base: dict[str, Any] = {
        "audit": log,
        "index": store,
        "bridge": None,
        "clock": lambda: 0.0,
        "known_recipients": Known(),
    }
    base.update(kw)
    return Engine(**base)


def test_engine_holds_its_parts(audit: AuditLog, ro_store: Store) -> None:
    eng = _engine(audit, ro_store)
    assert eng.audit is audit and eng.index is ro_store and eng.bridge is None
    assert eng.limits == Limits()
    lim = Limits(w1_per_caller=3)
    assert _engine(audit, ro_store, limits=lim).limits is lim
    assert isinstance(Known(), KnownRecipients)


def test_engine_refuses_a_writable_store(audit: AuditLog, tmp_path: Path) -> None:
    store = Store.open(tmp_path / "rw.db")
    try:
        with pytest.raises(EngineError, match="readonly"):
            _engine(audit, store)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("audit", object(), "AuditLog"),
        ("clock", 5, "clock"),
        ("known_recipients", object(), "is_known"),
        ("limits", {"w1_per_caller": 1}, "Limits"),
    ],
)
def test_engine_refuses_bad_parts(
    audit: AuditLog, ro_store: Store, field: str, value: Any, match: str
) -> None:
    with pytest.raises(EngineError, match=match):
        _engine(audit, ro_store, **{field: value})


# ---- recipient checks ---------------------------------------------------------------------------

M = "a" * 64


def test_check_recipients_asks_about_every_address_lower_cased(
    audit: AuditLog, ro_store: Store
) -> None:
    known = Known(False)
    eng = _engine(audit, ro_store, known_recipients=known)
    inp = DraftIn(mid=M, to=["Bob@Example.invalid", "carol@example.invalid"])
    checks = asyncio.run(eng.check_recipients(draft, inp))
    assert checks == (
        RecipientCheck("bob@example.invalid", False),
        RecipientCheck("carol@example.invalid", False),
    )
    assert known.asked == ["bob@example.invalid", "carol@example.invalid"]
    assert eng.tier(draft, 1, checks) is Tier.W2
    assert eng.tier(draft, 1, (RecipientCheck("bob@example.invalid", True),)) is Tier.W1


def test_check_recipients_without_declared_recipients(audit: AuditLog, ro_store: Store) -> None:
    known = Known()
    eng = _engine(audit, ro_store, known_recipients=known)
    assert asyncio.run(eng.check_recipients(set_tags, MidsIn(mids=[M]))) == ()
    assert asyncio.run(eng.check_recipients(draft, DraftIn(mid=M, to=[]))) == ()
    assert known.asked == []


@pytest.mark.parametrize("answer", [1, None, "yes"])
def test_a_non_bool_answer_is_a_type_error(audit: AuditLog, ro_store: Store, answer: Any) -> None:
    eng = _engine(audit, ro_store, known_recipients=Known(answer))
    with pytest.raises(TypeError, match="bool"):
        asyncio.run(eng.check_recipients(draft, DraftIn(mid=M, to=["a@example.invalid"])))
