"""The context budget for tool outputs (``tern.agent.budget``, P4): plan 08a step 2b, per turn
and prefix-stable since plan 08c step 5."""

from __future__ import annotations

import json

import pytest

from tern.agent.budget import (
    TRUNCATED_MARKER,
    BudgetChange,
    ToolOutput,
    enforce,
    estimate_tokens,
)
from tern.testing.fake_llm import FakeToolCall, ScriptStep

from .conftest import Env, steps, tool_messages


def out(call_id: str, chars: int, *, untrusted: bool = False, ch: str = "a") -> ToolOutput:
    text = json.dumps({"text": ch * chars})
    return ToolOutput(call_id, text, untrusted, "search_messages" if untrusted else None)


def test_the_estimate_is_utf8_bytes_over_four_rounded_up() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1 and estimate_tokens("abcde") == 2
    assert estimate_tokens("αβ") == 1  # 4 UTF-8 bytes: Greek costs twice ASCII per character
    assert estimate_tokens("α" * 100) == 50


def test_within_the_budget_nothing_changes() -> None:
    outputs = [out("a", 100), out("b", 100)]
    before = [o.content() for o in outputs]
    assert enforce(outputs, 1000) == BudgetChange(0)
    assert [o.content() for o in outputs] == before


def test_outputs_already_sent_are_never_changed_only_the_newest_is_cut() -> None:
    # Plan 08c: no elision. Each output is ~103 tokens; over 250, the sent ones stay whole.
    outputs = [out("a", 400), out("b", 400), out("c", 400)]
    sent = [o.content() for o in outputs[:2]]
    change = enforce(outputs, 250)
    assert change == BudgetChange(truncated=1)
    assert [o.content() for o in outputs[:2]] == sent
    assert [o.truncated for o in outputs] == [False, False, True]
    assert outputs[2].content().endswith(TRUNCATED_MARKER)
    assert sum(o.tokens() for o in outputs) <= 250
    # Persistent and counted once: a second pass changes and counts nothing.
    assert enforce(outputs, 250) == BudgetChange(0)


def test_a_newest_output_too_large_alone_is_truncated_with_a_visible_marker() -> None:
    outputs = [out("a", 400), out("b", 4000, untrusted=True, ch="α")]
    first = outputs[0].content()
    change = enforce(outputs, 300)
    assert change == BudgetChange(truncated=1)
    assert outputs[0].content() == first
    newest = outputs[1]
    content = newest.content()
    assert content.endswith(f"\n</untrusted_email>\n{TRUNCATED_MARKER}")
    assert content.startswith('<untrusted_email tool="search_messages">\n')
    assert content.count("</untrusted_email>") == 1
    assert sum(o.tokens() for o in outputs) <= 300
    assert "�" not in content  # never half a character
    assert enforce(outputs, 300) == BudgetChange(0)  # counted once


def test_truncation_fits_even_when_escaping_grows_the_text() -> None:
    text = "<untrusted_email>" * 200  # each escape adds three characters inside the envelope
    outputs = [ToolOutput("x", text, True, "get_message")]
    assert enforce(outputs, 256) == BudgetChange(truncated=1)
    content = outputs[0].content()
    assert outputs[0].tokens() <= 256
    assert content.count("<untrusted_email") == 1 and content.count("</untrusted_email>") == 1


def test_sent_outputs_that_fill_the_budget_leave_the_newest_cut_to_empty_and_counted() -> None:
    outputs = [out(str(i), 400) for i in range(4)]
    sent = [o.content() for o in outputs[:3]]
    change = enforce(outputs, 256)
    assert change == BudgetChange(truncated=1)
    assert [o.content() for o in outputs[:3]] == sent
    assert outputs[-1].text == ""
    assert outputs[-1].content().endswith(TRUNCATED_MARKER)


def test_the_newest_group_is_cut_largest_first() -> None:
    # The review probe: a large search output and a small one from the same response.
    search = ToolOutput("s", json.dumps({"text": "a" * 60000}), True, "search_messages")
    folders = ToolOutput("f", '{"folders": []}', False)
    change = enforce([search, folders], 12000, newest_from=0)
    assert change == BudgetChange(truncated=1)
    assert search.content().endswith(f"</untrusted_email>\n{TRUNCATED_MARKER}")
    assert folders.content() == '{"folders": []}'
    assert search.tokens() + folders.tokens() <= 12000


def test_the_newest_group_is_cut_to_a_common_cap() -> None:
    outputs = [out("sent", 400), out("a", 400), out("b", 4000)]
    sent = outputs[0].content()
    assert enforce(outputs, 250, newest_from=1) == BudgetChange(truncated=2)
    assert outputs[0].content() == sent
    assert [o.truncated for o in outputs] == [False, True, True]
    assert sum(o.tokens() for o in outputs) <= 250
    outputs = [out("a", 4000), out("b", 4000)]
    assert enforce(outputs, 400, newest_from=0) == BudgetChange(truncated=2)
    assert sum(o.tokens() for o in outputs) <= 400
    assert abs(outputs[0].tokens() - outputs[1].tokens()) <= 1


def test_an_empty_newest_group_changes_nothing_even_over_the_budget() -> None:
    outputs = [out("a", 400), out("b", 400)]
    before = [o.content() for o in outputs]
    assert enforce(outputs, 150, newest_from=2) == BudgetChange(0)
    assert [o.content() for o in outputs] == before


def test_invalid_arguments_are_refused() -> None:
    with pytest.raises(ValueError, match="budget_tokens"):
        enforce([], 0)
    for bad in (-1, 3, True):
        with pytest.raises(ValueError, match="newest_from"):
            enforce([out("a", 1), out("b", 1)], 100, newest_from=bad)
    with pytest.raises(ValueError, match="tool name"):
        ToolOutput("x", "{}", True)


@pytest.mark.asyncio
async def test_a_turn_drops_earlier_tool_outputs_and_keeps_the_answers(env: Env) -> None:
    search = {"query": "invoice", "limit": 20}
    fake = steps(
        ScriptStep(tool_calls=[FakeToolCall(name="search_messages", arguments=search)]),
        ScriptStep(tool_calls=[FakeToolCall(name="list_folders", arguments={})]),
        ScriptStep(content="First answer."),
        ScriptStep(tool_calls=[FakeToolCall(name="search_messages", arguments=search)]),
        ScriptStep(content="Second answer."),
    )
    session = env.session(fake)
    first = session.start_turn("first")
    assert [d async for d in first] == ["First answer."]
    t1 = await first.result()
    second = session.start_turn("second")
    assert [d async for d in second] == ["Second answer."]
    t2 = await second.result()
    fake.assert_done()
    assert (t1.meta.dropped_outputs, t2.meta.dropped_outputs) == (0, 2)
    # Turn 2's first request: turn 1 is its question and the answer the user saw, nothing else.
    msgs = fake.requests[3]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[2] == {"role": "assistant", "content": "First answer."}
    assert all("tool_calls" not in m for m in msgs)
    # Turn 2's own output is in its next request, after the same prefix.
    assert fake.requests[4]["messages"][:4] == msgs
    assert len(tool_messages(fake.requests[4])) == 1


@pytest.mark.asyncio
async def test_within_a_turn_each_request_extends_the_previous_one(env: Env) -> None:
    # Over the budget, the old policy elided an output the model had already seen, which
    # rewrote the prompt prefix (and LM Studio's prompt cache). Now only new outputs are cut.
    search = {"query": "invoice", "limit": 20}
    fake = steps(
        ScriptStep(tool_calls=[FakeToolCall(name="search_messages", arguments=search)]),
        ScriptStep(tool_calls=[FakeToolCall(name="list_folders", arguments={})]),
        ScriptStep(tool_calls=[FakeToolCall(name="search_messages", arguments=search)]),
        ScriptStep(content="Answer."),
    )
    # Unbudgeted, the search output is ~825 tokens and the folders ~370.
    session = env.session(fake, agent_context_budget_tokens=1300)
    handle = session.start_turn("question")
    assert [d async for d in handle] == ["Answer."]
    turn = await handle.result()
    fake.assert_done()
    assert turn.outcome == "ok" and turn.meta.dropped_outputs == 0
    reqs = [r["messages"] for r in fake.requests]
    for k in range(1, len(reqs)):
        assert reqs[k][: len(reqs[k - 1])] == reqs[k - 1], k
    contents = [m["content"] for m in tool_messages(fake.requests[3])]
    assert len(contents) == 3
    assert not any(c.endswith(TRUNCATED_MARKER) for c in contents[:2])  # they fit whole
    assert contents[2].endswith(TRUNCATED_MARKER)  # the budget ran out: the newest is cut
    assert turn.meta.truncated_outputs == 1
    assert sum(estimate_tokens(c) for c in contents) <= 1300
