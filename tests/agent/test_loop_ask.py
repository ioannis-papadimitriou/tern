"""Decisions #39: a turn started with ``ask`` (the chat's ``manual`` mode) asks before every tool
call of the turn; a refusal reaches the model as an error tool message and the turn goes on.

Over the real registry, engine, audit log and LLMClient (``tests/agent/conftest.py``); the asking
UI is a :class:`~tern.testing.approvals.ScriptedApprovals` that is not attached to the engine."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tern.agent.loop import AgentSession, AgentTurn
from tern.perms.approvals import ApprovalCard, Approvals
from tern.testing.approvals import Scripted, ScriptedApprovals
from tern.testing.fake_llm import FakeToolCall, ScriptStep

from .conftest import CALLER, RAN, Env, steps, tool_messages


def approvals_of(env: Env) -> Approvals:
    assert env.runtime.engine is not None
    return env.runtime.engine.approvals


async def run(session: AgentSession, ask: ScriptedApprovals | None) -> tuple[list[str], AgentTurn]:
    handle = session.start_turn("question", ask=ask) if ask else session.start_turn("question")
    deltas = [d async for d in handle]
    return deltas, await handle.result()


def two_calls_then_answer() -> list[ScriptStep]:
    return [
        ScriptStep(
            tool_calls=[
                FakeToolCall(name="dummy_r1", arguments={"text": "first"}),
                FakeToolCall(name="dummy_untrusted", arguments={"text": "second"}),
            ]
        ),
        ScriptStep(content="Answer."),
    ]


@pytest.mark.asyncio
async def test_manual_asks_before_every_call_and_runs_the_allowed_one(env: Env) -> None:
    fake = steps(*two_calls_then_answer())
    script: dict[str, Scripted] = {
        "dummy_r1": Scripted("deny"),
        "dummy_untrusted": Scripted("approve"),
    }
    tab = ScriptedApprovals(approvals_of(env), lambda card: script[card.tool])
    _, turn = await run(env.session(fake), tab)
    fake.assert_done()
    assert turn.outcome == "ok" and turn.text == "Answer."
    assert [(c.tool, c.caller) for c in tab.cards] == [
        ("dummy_r1", CALLER),
        ("dummy_untrusted", CALLER),
    ]
    assert RAN == ["dummy_untrusted"]  # the denied call never ran
    assert [(r.tool, r.decision, r.reason) for r in env.rows()] == [
        ("dummy_r1", "denied", "user_denied"),
        ("dummy_untrusted", "approved", None),
    ]
    # The model saw the refusal of one call and the result of the other, then answered.
    first, second = tool_messages(fake.requests[1])
    assert "second" in second["content"]
    err = json.loads(first["content"])["error"]
    assert (err["code"], err["reason"]) == ("denied", "user_denied")


@pytest.mark.asyncio
async def test_a_manual_call_waits_until_the_user_decides(env: Env) -> None:
    fake = steps(
        ScriptStep(tool_calls=[FakeToolCall(name="dummy_r1", arguments={"text": "x"})]),
        ScriptStep(content="Done."),
    )
    tab = ScriptedApprovals(approvals_of(env), lambda card: None)  # holds every card
    handle = env.session(fake).start_turn("question", ask=tab)
    for _ in range(200):
        if tab.cards:
            break
        await asyncio.sleep(0.005)
    card: ApprovalCard = tab.cards[0]
    await asyncio.sleep(0.05)
    assert not handle.done and RAN == [] and env.rows() == []  # waiting, nothing ran or recorded
    assert tab.decide(card, "approve") == "approved"
    turn = await handle.result()
    assert turn.outcome == "ok" and RAN == ["dummy_r1"]
    assert [(r.tool, r.decision) for r in env.rows()] == [("dummy_r1", "approved")]


@pytest.mark.asyncio
async def test_bypass_and_no_mode_never_ask(env: Env) -> None:
    fake = steps(*two_calls_then_answer())
    _, turn = await run(env.session(fake), None)
    assert turn.outcome == "ok"
    assert RAN == ["dummy_r1", "dummy_untrusted"]
    assert [(r.tool, r.decision) for r in env.rows()] == [
        ("dummy_r1", "auto"),
        ("dummy_untrusted", "auto"),
    ]
    assert approvals_of(env).pending() == []


@pytest.mark.asyncio
async def test_a_cancelled_manual_turn_records_the_waiting_call_cancelled(env: Env) -> None:
    fake = steps(
        ScriptStep(tool_calls=[FakeToolCall(name="dummy_r1", arguments={"text": "x"})]),
        ScriptStep(content="never"),
    )
    tab = ScriptedApprovals(approvals_of(env), lambda card: None)
    handle = env.session(fake).start_turn("question", ask=tab)
    for _ in range(200):
        if tab.cards:
            break
        await asyncio.sleep(0.005)
    assert tab.cards
    handle.cancel()
    turn = await handle.result()
    assert turn.outcome == "cancelled" and RAN == []
    assert [(r.tool, r.decision, r.reason) for r in env.rows()] == [
        ("dummy_r1", "denied", "cancelled")
    ]
    assert approvals_of(env).pending() == []


def test_ask_must_be_a_notifier(env: Env) -> None:
    session = env.session(steps(ScriptStep(content="x")))
    bad: Any = object()
    with pytest.raises(TypeError):
        session.start_turn("question", ask=bad)
