"""Plan 08a step 1: ``FakeLLM`` serves scripted SSE that the real ``LLMClient`` parses."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from tern.config import TernConfig
from tern.llm.client import (
    ContentDelta,
    Done,
    LLMClient,
    LLMError,
    LLMEvent,
    LLMUnavailable,
    ReasoningDelta,
    ToolCalls,
)
from tern.llm.egress import EgressDenied
from tern.testing.fake_llm import (
    FakeExpect,
    FakeLLM,
    FakeLLMScriptError,
    FakeScript,
    FakeToolCall,
    ScriptStep,
    load_script,
)

MODEL = "fake-chat-1"
TOOLS = [
    {"type": "function", "function": {"name": n, "parameters": {"type": "object"}}}
    for n in ("search_messages", "list_folders")
]


def _client(fake: FakeLLM, **cfg: object) -> LLMClient:
    values: dict[str, object] = {"chat_model": fake.model_id}
    values.update(cfg)
    return LLMClient(TernConfig.model_validate(values), transport=fake.transport)


async def _turn(
    client: LLMClient, content: str = "question", tools: list[dict[str, Any]] = TOOLS
) -> list[LLMEvent]:
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": content}]
    return [e async for e in client.stream(messages, tools)]


def _text(events: list[LLMEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, ContentDelta))


@pytest.mark.asyncio
async def test_split_tool_calls_reach_the_client_whole() -> None:
    args = {"query": 'Acme "INV-0042" café', "limit": 10}
    text = json.dumps(args, ensure_ascii=False)
    fake = FakeLLM.from_steps(
        ScriptStep(
            tool_calls=[
                FakeToolCall(
                    name="search_messages",
                    arguments=args,
                    split_at=[3, text.index('\\"') + 1, text.index("é")],
                ),
                FakeToolCall(name="list_folders", id="call_given", split_at=[1]),
            ],
            interleave=True,
        )
    )
    async with _client(fake) as client:
        events = await _turn(client)
    (calls,) = [e for e in events if isinstance(e, ToolCalls)]
    first, second = calls.calls
    assert (first.id, first.name, json.loads(first.arguments)) == (
        "call_fake_0_0",
        "search_messages",
        args,
    )
    assert (second.id, second.name, second.arguments) == ("call_given", "list_folders", "{}")
    assert events[-1] == Done(finish_reason="tool_calls", truncated=False)
    fake.assert_done()


def test_interleave_really_alternates_fragment_indexes() -> None:
    step = ScriptStep(
        tool_calls=[
            FakeToolCall(name="a", arguments={"x": 1}, split_at=[2, 4]),
            FakeToolCall(name="b", arguments={"y": 2}, split_at=[2]),
        ],
        interleave=True,
    )
    fake = FakeLLM.from_steps(step)
    chunks = fake._chunks(0, {}, step)  # pyright: ignore[reportPrivateUsage]  # fixture shape
    indexes = [c["choices"][0]["delta"]["tool_calls"][0]["index"] for c in chunks[1:-2]]
    assert indexes == [0, 1, 0, 1, 0]


@pytest.mark.asyncio
async def test_raw_string_arguments_are_sent_verbatim() -> None:
    fake = FakeLLM.from_steps(
        ScriptStep(tool_calls=[FakeToolCall(name="search_messages", arguments='{"query": ')])
    )
    async with _client(fake) as client:
        events = await _turn(client)
    (calls,) = [e for e in events if isinstance(e, ToolCalls)]
    assert calls.calls[0].arguments == '{"query": '


@pytest.mark.asyncio
async def test_content_with_split_think_and_reasoning() -> None:
    step = ScriptStep(
        content="Answer [abc].",
        think="SENTINEL-thought",
        reasoning="more",
        split_at=[3, 10, 26, 29],  # inside "<think>", the thought and "</think>"
        finish_reason="length",
    )
    fake = FakeLLM.from_steps(step)
    async with _client(fake) as client:
        events = await _turn(client)
    assert _text(events) == "Answer [abc]."
    assert sum(e.chars for e in events if isinstance(e, ReasoningDelta)) == len("more") + len(
        "SENTINEL-thought"
    )
    assert events[-1] == Done(finish_reason="length", truncated=True)


@pytest.mark.asyncio
async def test_requests_are_recorded_in_order() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="one"), ScriptStep(content="two"))
    async with _client(fake) as client:
        assert _text(await _turn(client, "q1")) == "one"
        assert fake.remaining == 1
        assert _text(await _turn(client, "q2", tools=[])) == "two"
    assert [r["messages"][-1]["content"] for r in fake.requests] == ["q1", "q2"]
    assert "tools" in fake.requests[0] and "tools" not in fake.requests[1]
    fake.assert_done()


@pytest.mark.asyncio
async def test_running_out_of_script_fails_loudly() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="only"))
    async with _client(fake) as client:
        await _turn(client)
        with pytest.raises(FakeLLMScriptError, match="only 1"):
            await _turn(client)
    with pytest.raises(FakeLLMScriptError):
        fake.assert_done()


def test_unused_steps_fail_assert_done() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="a"), ScriptStep(content="b"))
    with pytest.raises(FakeLLMScriptError, match="2 scripted request"):
        fake.assert_done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expect", "message"),
    [
        (FakeExpect(tools=["search_messages"]), "offered tools"),
        (FakeExpect(contains=["not in any message"]), "contains"),
        (FakeExpect(absent=["question"]), "absent"),
    ],
)
async def test_failed_expect_raises_and_is_kept(expect: FakeExpect, message: str) -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="x", expect=expect))
    async with _client(fake) as client:
        with pytest.raises(FakeLLMScriptError, match=message):
            await _turn(client)
    assert len(fake.errors) == 1
    with pytest.raises(FakeLLMScriptError, match=message):
        fake.assert_done()


@pytest.mark.asyncio
async def test_expect_tools_with_a_nameless_offered_tool_fails_as_a_script_error() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="x", expect=FakeExpect(tools=["search_messages"])))
    nameless = [*TOOLS, {"type": "function", "function": {"parameters": {"type": "object"}}}]
    async with _client(fake) as client:
        with pytest.raises(FakeLLMScriptError, match=r"tools\[2\] has no function name"):
            await _turn(client, tools=nameless)
    assert len(fake.errors) == 1
    with pytest.raises(FakeLLMScriptError, match="no function name"):
        fake.assert_done()


@pytest.mark.asyncio
async def test_passing_expect() -> None:
    expect = FakeExpect(
        tools=["list_folders", "search_messages"], contains=["quest"], absent=["tb_id"]
    )
    fake = FakeLLM.from_steps(ScriptStep(content="x", expect=expect))
    async with _client(fake) as client:
        await _turn(client)
    fake.assert_done()


@pytest.mark.asyncio
async def test_wrong_model_is_refused() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="x"))
    async with _client(fake, chat_model="other-model") as client:
        with pytest.raises(FakeLLMScriptError, match="not a streamed request"):
            await _turn(client)


def test_egress_check_still_runs_with_the_fake_transport() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="x"))
    with pytest.raises(EgressDenied):
        _client(fake, chat_base_url="http://203.0.113.5/v1")


@pytest.mark.asyncio
async def test_models_endpoints_and_unknown_path() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="x"), model_id="m-1")
    async with httpx.AsyncClient(transport=fake.transport) as http:
        models = (await http.get("http://127.0.0.1:1234/v1/models")).json()
        v0 = (await http.get("http://127.0.0.1:1234/api/v0/models")).json()
        with pytest.raises(FakeLLMScriptError, match="unexpected request"):
            await http.get("http://127.0.0.1:1234/v1/embeddings")
    assert [m["id"] for m in models["data"]] == ["m-1"]
    assert v0["data"][0]["loaded_context_length"] == 32768


@pytest.mark.parametrize(
    "step",
    [
        {},
        {"content": "x", "tool_calls": [{"name": "a"}]},
        {"tool_calls": []},
        {"content": "abc", "split_at": [0]},
        {"content": "abc", "split_at": [3]},
        {"content": "abc", "split_at": [2, 1]},
        {"content": "abc", "interleave": True},
        {"tool_calls": [{"name": "a", "arguments": "{}", "split_at": [5]}]},
        {"tool_calls": [{"name": "a"}], "split_at": [1]},
        {"content": "x", "surprise": 1},
    ],
)
def test_invalid_steps_are_refused(step: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ScriptStep.model_validate(step)


def test_script_version_and_load(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        FakeScript.model_validate({"version": 2, "requests": [{"content": "x"}]})
    path = tmp_path / "script.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "requests": [
                    {"tool_calls": [{"name": "search_messages", "arguments": {"query": "q"}}]},
                    {"content": "done", "expect": {"contains": ["search"]}},
                ],
            }
        )
    )
    script = load_script(path)
    assert script.requests[0].tool_calls is not None
    assert script.requests[1].expect == FakeExpect(contains=["search"])


# ---- step 2b additions: http_status and pause_after ---------------------------------------


@pytest.mark.asyncio
async def test_an_http_status_step_reaches_the_client_as_its_error() -> None:
    fake = FakeLLM.from_steps(ScriptStep(http_status=503), ScriptStep(http_status=400))
    async with _client(fake) as client:
        with pytest.raises(LLMUnavailable, match="http_503"):
            await _turn(client)
        with pytest.raises(LLMError, match="http_400"):
            await _turn(client)
    fake.assert_done()


@pytest.mark.asyncio
async def test_pause_after_holds_the_stream_until_resume() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="ab", split_at=[1], pause_after=2))
    async with _client(fake) as client:
        task = asyncio.create_task(_turn(client))
        await fake.paused.wait()
        await asyncio.sleep(0.05)
        assert not task.done()
        fake.resume()
        events = await task
    assert _text(events) == "ab"
    fake.assert_done()


@pytest.mark.parametrize(
    "step",
    [
        {"http_status": 503, "content": "x"},
        {"http_status": 503, "tool_calls": [{"name": "a"}]},
        {"http_status": 503, "think": "t"},
        {"http_status": 503, "pause_after": 1},
        {"http_status": 503, "finish_reason": "stop"},
        {"http_status": 200},
        {"content": "x", "pause_after": -1},
    ],
)
def test_invalid_http_status_and_pause_steps_are_refused(step: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ScriptStep.model_validate(step)


# ---- step 2b review: silent step combinations, lead_content ---------------------------------


@pytest.mark.parametrize(
    "step", [{"http_status": 503, "split_at": [1]}, {"http_status": 503, "interleave": True}]
)
def test_stream_fields_on_an_http_status_step_are_refused_as_such(step: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="an http_status step has no stream fields"):
        ScriptStep.model_validate(step)


@pytest.mark.parametrize(
    "step", [{"content": "x", "lead_content": "y"}, {"http_status": 503, "lead_content": "y"}]
)
def test_lead_content_without_tool_calls_is_refused(step: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ScriptStep.model_validate(step)


@pytest.mark.asyncio
async def test_a_pause_point_past_the_stream_fails_instead_of_never_pausing() -> None:
    # "ab" in one piece: role chunk, content, finish, usage, [DONE] = 5 SSE events (0..4).
    fake = FakeLLM.from_steps(
        ScriptStep(content="ab", pause_after=4), ScriptStep(content="ab", pause_after=5)
    )
    async with _client(fake) as client:
        task = asyncio.create_task(_turn(client))
        await fake.paused.wait()  # 4 is the last event: it still pauses
        fake.resume()
        assert _text(await task) == "ab"
        with pytest.raises(FakeLLMScriptError, match="pause_after=5 but the stream has 5 events"):
            await _turn(client)
    assert len(fake.errors) == 1
    with pytest.raises(FakeLLMScriptError, match="pause_after=5"):
        fake.assert_done()


@pytest.mark.asyncio
async def test_lead_content_is_streamed_before_the_tool_calls() -> None:
    call = FakeToolCall(name="list_folders", arguments={})
    fake = FakeLLM.from_steps(ScriptStep(tool_calls=[call], lead_content="Let me look."))
    async with _client(fake) as client:
        events = await _turn(client)
    fake.assert_done()
    assert _text(events) == "Let me look."
    first_content = next(i for i, e in enumerate(events) if isinstance(e, ContentDelta))
    calls = next(i for i, e in enumerate(events) if isinstance(e, ToolCalls))
    assert first_content < calls
    tool_calls = events[calls]
    assert isinstance(tool_calls, ToolCalls)
    assert [c.name for c in tool_calls.calls] == ["list_folders"]
