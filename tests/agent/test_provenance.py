"""Plan 08a step 3: whole-session provenance on every ``Registry.call`` (M4, A10).

The main test drives a real approval card: the env's real ``Engine`` gets the ``holding``
approval driver, and the model calls the test-only W2 tool ``dummy_w2`` in turn 3. The card's
provenance must hold a mid the model read in turn 1.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from tern.agent.loop import AgentSession, AgentTurn
from tern.agent.provenance import SessionProvenance, output_mids, output_refs
from tern.perms.approvals import ApprovalCard, ApprovalNotifier
from tern.testing.approvals import ScriptedApprovals, holding
from tern.testing.fake_llm import FakeToolCall, ScriptStep
from tern.tools.registry import RegisteredTool, Registry, ToolResult, builtin_tools

from .conftest import CALLER, DUMMIES, MANIFEST, RAN, SINGLE, Env, steps, tool_messages

QUERY = "Acme invoice INV-0042"
MID_A = "a" * 64
MID_B = "b" * 64
MID_X = "x-" + "c" * 64


class SpyRegistry(Registry):
    """A real ``Registry`` that records the provenance of every call before running it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[tuple[str, tuple[str, ...]]] = []

    async def call(
        self,
        caller: str,
        tool: str,
        args: Mapping[str, Any],
        *,
        caller_label: str | None = None,
        provenance: Sequence[str] = (),
        ask: ApprovalNotifier | None = None,  # decisions #39: forwarded unchanged
    ) -> ToolResult:
        self.seen.append((tool, tuple(provenance)))
        return await super().call(
            caller, tool, args, caller_label=caller_label, provenance=provenance, ask=ask
        )


def spy_registry(env: Env) -> SpyRegistry:
    tools = [*builtin_tools(), *(RegisteredTool(t, "plugin", "test") for t in DUMMIES)]
    return SpyRegistry(tools, runtime=env.runtime)


def call(name: str, arguments: dict[str, Any]) -> FakeToolCall:
    return FakeToolCall(name=name, arguments=arguments)


async def run(session: AgentSession, text: str = "question") -> AgentTurn:
    handle = session.start_turn(text)
    async for _ in handle:
        pass
    return await handle.result()


def unwrap(content: str, tool: str) -> Any:
    head = f'<untrusted_email tool="{tool}">\n'
    assert content.startswith(head) and content.endswith("\n</untrusted_email>")
    return json.loads(content.removeprefix(head).removesuffix("\n</untrusted_email>"))


async def first_card(driver: ScriptedApprovals, turn: asyncio.Task[AgentTurn]) -> ApprovalCard:
    async with asyncio.timeout(10):
        while not driver.cards:
            assert not turn.done(), "the turn ended without an approval card"
            await asyncio.sleep(0.01)
    [card] = driver.cards
    return card


# ---- the tracker ----------------------------------------------------------------------------


def test_output_mids_finds_every_mid_key_in_document_order() -> None:
    output = {
        "hits": [
            {"mid": MID_A, "folders": ["d" * 64], "thread": {"mid": MID_X}},
            {"mid": MID_B, "snippet": MID_A},
        ],
        "mid": MID_B,
    }
    found, invalid = output_mids(output)
    assert found == [MID_A, MID_X, MID_B, MID_B] and invalid == 0
    # A mid under another key (folders, snippet) is not a message the result read.
    assert output_mids({"folders": [MID_A], "snippet": MID_B, "mids": [MID_A]}) == ([], 0)


def test_a_dicts_own_mid_after_a_nested_one_comes_after_it() -> None:
    # Review round 1: a dict's own `mid` was collected before any of its children.
    assert output_mids({"thread": {"mid": MID_X}, "mid": MID_A}) == ([MID_X, MID_A], 0)
    assert output_mids({"mid": MID_A, "thread": {"mid": MID_X}}) == ([MID_A, MID_X], 0)
    # A non-string `mid` (a dict) is counted invalid and still searched.
    assert output_mids({"mid": {"mid": MID_B}, "x": [{"mid": MID_A}]}) == ([MID_B, MID_A], 1)
    first = SessionProvenance()
    first.add({"hits": [{"mid": MID_B}], "mid": MID_A}, tool="t")
    assert first.mids() == (MID_B, MID_A)


def test_short_ids_resolve_to_every_indexed_mid_they_prefix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Plan 08c: tools show short ids; a card needs full mids, conservatively all matches.
    clash = MID_A[:12] + "f" * 52
    index = {MID_A, clash, MID_X}

    def resolve(ref: str) -> list[str]:
        return sorted(m for m in index if m.startswith(ref))

    output = {"hits": [{"mid": MID_A[:12]}, {"mid": MID_X[:14]}, {"mid": MID_B}]}
    assert output_refs(output) == ([MID_A[:12], MID_X[:14], MID_B], 0)
    assert output_mids(output) == ([MID_B], 2)
    prov = SessionProvenance(resolve=resolve)
    with caplog.at_level(logging.WARNING, logger="tern.agent"):
        assert prov.add(output, tool="t") == 4
        assert prov.add({"mid": "b" * 12}, tool="t") == 0  # names no indexed message
    assert prov.mids() == (MID_A, clash, MID_X, MID_B)
    [rec] = [r for r in caplog.records if r.name == "tern.agent.provenance"]
    assert rec.__dict__["count"] == 1
    # Without a resolver a short id cannot name a message: dropped, counted.
    plain = SessionProvenance()
    assert plain.add(output, tool="t") == 1 and plain.mids() == (MID_B,)


def test_the_session_keeps_distinct_mids_in_first_order_over_adds() -> None:
    prov = SessionProvenance()
    assert prov.add({"hits": [{"mid": MID_B}, {"mid": MID_A}]}, tool="t") == 2
    assert prov.add({"mid": MID_A}, tool="t") == 0
    assert prov.add({"mid": MID_X}, tool="t") == 1
    assert prov.mids() == (MID_B, MID_A, MID_X) and len(prov) == 3


def test_a_mid_value_that_is_not_a_mid_is_dropped_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    prov = SessionProvenance()
    planted = "SENTINEL-not-a-mid"
    with caplog.at_level(logging.WARNING, logger="tern.agent"):
        n = prov.add(
            {"items": [{"mid": planted}, {"mid": 7}, {"mid": MID_A}, {"mid": None}]}, tool="t"
        )
    assert n == 1 and prov.mids() == (MID_A,)
    [rec] = [r for r in caplog.records if r.name == "tern.agent.provenance"]
    assert rec.levelno == logging.WARNING and rec.__dict__["count"] == 3
    assert planted not in caplog.text


# ---- the loop -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_call_gets_the_whole_session_provenance(env: Env) -> None:
    reg = spy_registry(env)
    fake = steps(
        ScriptStep(tool_calls=[call("search_messages", {"query": QUERY, "limit": 10})]),
        ScriptStep(content="Turn one."),
        # Turn 2: a trusted result and an error add nothing.
        ScriptStep(tool_calls=[call("list_folders", {}), call("get_message", {"mid": "f" * 64})]),
        ScriptStep(content="Turn two."),
        # Turn 3: one more untrusted read, then a tool that reads nothing.
        ScriptStep(tool_calls=[call("get_message", {"mid": SINGLE}), call("index_status", {})]),
        ScriptStep(content="Turn three."),
    )
    session = env.session(fake, registry=reg)
    assert session.provenance() == ()
    for _ in range(3):
        assert (await run(session)).outcome == "ok"
    fake.assert_done()
    [search_msg] = tool_messages(fake.requests[1])
    read = [h["mid"] for h in unwrap(search_msg["content"], "search_messages")["hits"]]
    assert read and SINGLE in read  # the probed query ranks inv-0042 first
    turn1 = tuple(read)
    assert reg.seen == [
        ("search_messages", ()),
        ("list_folders", turn1),
        ("get_message", turn1),
        ("get_message", turn1),
        ("index_status", turn1),  # SINGLE was read in turn 1 already: nothing new
    ]
    assert session.provenance() == turn1


@pytest.mark.asyncio
async def test_an_untrusted_result_is_on_the_next_calls_provenance_in_the_same_response(
    env: Env,
) -> None:
    other = next(m["mid"] for m in MANIFEST["messages"] if m["mid"] != SINGLE)
    reg = spy_registry(env)
    fake = steps(
        ScriptStep(
            tool_calls=[
                call("get_message", {"mid": SINGLE}),
                call("get_message", {"mid": other}),
                call("list_folders", {}),
            ]
        ),
        ScriptStep(content="Done."),
    )
    session = env.session(fake, registry=reg)
    assert (await run(session)).outcome == "ok"
    assert reg.seen == [
        ("get_message", ()),
        ("get_message", (SINGLE,)),
        ("list_folders", (SINGLE, other)),
    ]


@pytest.mark.asyncio
async def test_the_turn_3_approval_card_shows_a_mid_read_in_turn_1(env: Env) -> None:
    """Through the real ``Engine``: the W2 call of turn 3 opens a card whose provenance holds
    the mids the model read in turn 1 (fails if only the current turn is tracked)."""
    engine = env.runtime.engine
    assert engine is not None
    driver = holding(engine.approvals)
    fake = steps(
        ScriptStep(tool_calls=[call("search_messages", {"query": QUERY, "limit": 10})]),
        ScriptStep(content="Turn one."),
        ScriptStep(tool_calls=[call("list_folders", {})]),
        ScriptStep(content="Turn two."),
        ScriptStep(tool_calls=[call("dummy_w2", {"mid": SINGLE})]),
        ScriptStep(content="Turn three."),
    )
    session = env.session(fake)
    assert (await run(session)).outcome == "ok"
    assert (await run(session)).outcome == "ok"
    [search_msg] = tool_messages(fake.requests[1])
    turn1 = [h["mid"] for h in unwrap(search_msg["content"], "search_messages")["hits"]]
    assert turn1

    turn3 = asyncio.ensure_future(run(session))
    card = await first_card(driver, turn3)
    assert card.tool == "dummy_w2" and card.caller == CALLER
    assert [p.mid for p in card.provenance] == turn1
    # The card's provenance comes from the index (from + subject), never from model text.
    p0 = env.store.get_message(turn1[0])
    assert p0 is not None
    assert (card.provenance[0].from_addr, card.provenance[0].subject) == (p0.from_addr, p0.subject)
    driver.decide(card, "deny")
    turn = await turn3
    fake.assert_done()
    assert turn.outcome == "ok" and RAN == []
    [w2_msg] = tool_messages(fake.requests[5])[-1:]
    assert json.loads(w2_msg["content"])["error"]["code"] == "denied"
    rows = [(r.caller, r.tool, r.decision) for r in env.rows() if r.tool == "dummy_w2"]
    assert rows == [(CALLER, "dummy_w2", "denied")]
