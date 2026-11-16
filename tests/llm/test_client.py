"""Plan 08a step 1: ``LLMClient`` over hand-written SSE (httpx.MockTransport, no socket).

The fixtures copy the OpenAI streaming shape and the fragment patterns S3's client covered
(A6: nothing could be recorded from LM Studio in the sandbox). All text is synthetic.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import httpx
import pytest

from tern.config import TernConfig
from tern.llm.client import (
    ContentDelta,
    Done,
    LLMClient,
    LLMError,
    LLMEvent,
    LLMUnavailable,
    ReasoningDelta,
    ToolCall,
    ToolCalls,
    Usage,
)
from tern.llm.egress import EgressDenied

MODEL = "fake-chat-1"
SENTINEL = "SENTINEL-llm-body-q7x"
MESSAGES = [{"role": "user", "content": "What did the synthetic sender say?"}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": "Search.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }
]

Handler = Callable[[httpx.Request], httpx.Response]


def _cfg(**overrides: object) -> TernConfig:
    values: dict[str, object] = {"chat_model": MODEL}
    values.update(overrides)
    return TernConfig.model_validate(values)


def _client(handler: Handler, **overrides: object) -> LLMClient:
    return LLMClient(_cfg(**overrides), transport=httpx.MockTransport(handler))


def _sse(*events: object) -> bytes:
    """``data:`` lines for dict events (JSON) or raw strings, plus ``[DONE]``."""
    out = []
    for event in events:
        data = event if isinstance(event, str) else json.dumps(event)
        out.append(f"data: {data}\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode()


def _delta(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _respond(body: bytes, status: int = 200) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": "text/event-stream"}, content=body)

    return handle


class _Chunks(httpx.AsyncByteStream):
    """A response body served as the given byte chunks, optionally blocking after them."""

    def __init__(self, chunks: Sequence[bytes], *, block: bool = False) -> None:
        self.chunks = list(chunks)
        self.block = block
        self.closed = False
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.sent += 1
            yield chunk
        if self.block:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


async def _collect(client: LLMClient, tools: Sequence[dict[str, Any]] = TOOLS) -> list[LLMEvent]:
    return [event async for event in client.stream(MESSAGES, tools)]


def _content(events: list[LLMEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, ContentDelta))


def _calls(events: list[LLMEvent]) -> tuple[ToolCall, ...]:
    (calls,) = [e for e in events if isinstance(e, ToolCalls)]
    return calls.calls


# ---- request ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_posts_s3_request_body_to_chat_completions() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_sse(_delta({"content": "ok"}, "stop")))

    cfg = {"chat_temperature": 0.5, "chat_seed": 7, "chat_max_tokens": 321}
    async with _client(handle, **cfg) as client:
        assert client.model_id == MODEL
        events = await _collect(client)
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1:47831/v1/chat/completions"
    assert json.loads(request.content) == {
        "model": MODEL,
        "messages": MESSAGES,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.5,
        "seed": 7,
        "max_tokens": 321,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    assert _content(events) == "ok"
    assert events[-1] == Done(finish_reason="stop", truncated=False)


@pytest.mark.asyncio
async def test_no_tools_means_no_tools_or_tool_choice_keys() -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=_sse(_delta({"content": "ok"}, "stop")))

    async with _client(handle) as client:
        await _collect(client, tools=())
    assert "tools" not in seen[0] and "tool_choice" not in seen[0]


# ---- tool-call fragments ------------------------------------------------------------------

# Two calls whose fragments interleave by index; call 0's arguments are split inside a string
# value and inside the escapes \" and é; call 1's id and name come only in its first
# fragment, and a later fragment repeats the name (allowed when equal).
INTERLEAVED = _sse(
    _delta({"role": "assistant", "content": ""}),
    _delta(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "search_messages", "arguments": '{"query": "Acme inv'},
                }
            ]
        }
    ),
    _delta(
        {
            "tool_calls": [
                {
                    "index": 1,
                    "id": "call_b",
                    "type": "function",
                    "function": {"name": "list_folders", "arguments": ""},
                }
            ]
        }
    ),
    _delta({"tool_calls": [{"index": 0, "function": {"arguments": "oice \\"}}]}),
    _delta({"tool_calls": [{"index": 1, "function": {"name": "list_folders", "arguments": "{"}}]}),
    _delta({"tool_calls": [{"index": 0, "function": {"arguments": '"Q3\\" caf\\u00'}}]}),
    _delta({"tool_calls": [{"index": 0, "function": {"arguments": 'e9", "limit": 1'}}]}),
    _delta({"tool_calls": [{"index": 1, "function": {"arguments": "}"}}]}),
    _delta({"tool_calls": [{"index": 0, "function": {"arguments": "0}"}}]}),
    _delta({}, "tool_calls"),
)


@pytest.mark.asyncio
async def test_interleaved_fragments_split_mid_string_and_mid_escape_are_joined() -> None:
    async with _client(_respond(INTERLEAVED)) as client:
        events = await _collect(client)
    first, second = _calls(events)
    assert (first.id, first.name) == ("call_a", "search_messages")
    assert first.arguments == '{"query": "Acme invoice \\"Q3\\" caf\\u00e9", "limit": 10}'
    assert json.loads(first.arguments) == {"query": 'Acme invoice "Q3" café', "limit": 10}
    assert (second.id, second.name, second.arguments) == ("call_b", "list_folders", "{}")
    assert _content(events) == ""
    assert events[-1] == Done(finish_reason="tool_calls", truncated=False)
    # The completed calls come once, after every fragment, just before Done.
    assert isinstance(events[-2], ToolCalls)


@pytest.mark.asyncio
async def test_missing_id_gets_index_id_and_missing_name_is_an_error() -> None:
    no_id = _sse(
        _delta({"tool_calls": [{"index": 3, "function": {"name": "list_folders"}}]}),
        _delta({}, "tool_calls"),
    )
    async with _client(_respond(no_id)) as client:
        (call,) = _calls(await _collect(client))
    assert (call.id, call.name, call.arguments) == ("call_3", "list_folders", "")

    no_name = _sse(
        _delta({"tool_calls": [{"index": 0, "id": "c", "function": {"arguments": "{}"}}]}),
        _delta({}, "tool_calls"),
    )
    async with _client(_respond(no_name)) as client:
        with pytest.raises(LLMError, match="tool_call_without_name"):
            await _collect(client)


@pytest.mark.asyncio
async def test_a_changed_tool_name_is_an_error() -> None:
    body = _sse(
        _delta({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "list_folders"}}]}),
        _delta({"tool_calls": [{"index": 0, "function": {"name": "search_messages"}}]}),
        _delta({}, "tool_calls"),
    )
    async with _client(_respond(body)) as client:
        with pytest.raises(LLMError, match="tool_call_name_changed"):
            await _collect(client)


# ---- reasoning ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_think_split_across_chunks_is_never_answer_content() -> None:
    reason = "SENTINEL-reasoning-text"
    body = _sse(
        _delta({"content": "<thi"}),
        _delta({"content": "nk>" + reason[:9]}),
        _delta({"content": reason[9:] + "</th"}),
        _delta({"content": "ink>The answer"}),
        _delta({"content": " is <"}),  # a lone "<" that is not a tag is answer text
        _delta({"content": "b>."}, "stop"),
    )
    async with _client(_respond(body)) as client:
        events = await _collect(client)
    assert _content(events) == "The answer is <b>."
    assert sum(e.chars for e in events if isinstance(e, ReasoningDelta)) == len(reason)
    assert all(reason[:9] not in e.text for e in events if isinstance(e, ContentDelta))


@pytest.mark.asyncio
async def test_reasoning_fields_and_an_unclosed_think_are_counted_not_yielded() -> None:
    body = _sse(
        _delta({"reasoning": "abc"}),
        _delta({"reasoning_content": "de"}),
        _delta({"content": "Answer.<think>never closed"}, "stop"),
    )
    async with _client(_respond(body)) as client:
        events = await _collect(client)
    assert _content(events) == "Answer."
    assert [e.chars for e in events if isinstance(e, ReasoningDelta)] == [3, 2, len("never closed")]
    assert events[-1] == Done(
        finish_reason="stop", truncated=False, unclosed_think=True, late_think=True
    )


@pytest.mark.asyncio
async def test_a_clean_think_sets_no_think_flag() -> None:
    body = _sse(_delta({"content": "<think>plan</think>Answer."}, "stop"))
    async with _client(_respond(body)) as client:
        events = await _collect(client)
    assert _content(events) == "Answer."
    assert events[-1] == Done(finish_reason="stop", truncated=False)


@pytest.mark.asyncio
async def test_a_think_opened_after_answer_text_and_never_closed_is_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A literal ``<think>`` (e.g. quoted from mail) must not silently swallow the answer."""
    rest = f" from mail. {SENTINEL} more answer text"
    body = _sse(
        _delta({"content": "Quote: <thi"}),
        _delta({"content": "nk>" + rest}, "stop"),
    )
    with caplog.at_level("DEBUG", logger="tern.llm.client"):
        async with _client(_respond(body)) as client:
            events = await _collect(client)
    assert _content(events) == "Quote: "
    done = events[-1]
    assert isinstance(done, Done)
    assert done.unclosed_think and not done.truncated and not done.stray_think_close
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "unclosed_think=True" in warnings[0].getMessage()
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
async def test_a_closed_think_after_answer_text_is_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A closed ``<think>…</think>`` pair quoted from mail must not silently cut the answer."""
    body = _sse(
        _delta({"content": "Answer <thi"}),
        _delta({"content": f"nk>{SENTINEL}</think> more"}, "stop"),
    )
    with caplog.at_level("DEBUG", logger="tern.llm.client"):
        async with _client(_respond(body)) as client:
            events = await _collect(client)
    assert _content(events) == "Answer  more"
    assert events[-1] == Done(finish_reason="stop", truncated=False, late_think=True)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "late_opens=1" in warnings[0].getMessage()
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
async def test_a_think_after_leading_whitespace_sets_no_think_flag() -> None:
    body = _sse(_delta({"content": "\n <think>plan</think>Answer."}, "stop"))
    async with _client(_respond(body)) as client:
        events = await _collect(client)
    assert _content(events) == "\n Answer."
    assert events[-1] == Done(finish_reason="stop", truncated=False)


@pytest.mark.asyncio
async def test_a_think_cut_by_the_length_limit_is_truncated_not_unclosed() -> None:
    body = _sse(_delta({"content": "<think>still thinking"}, "length"))
    async with _client(_respond(body)) as client:
        events = await _collect(client)
    assert events[-1] == Done(finish_reason="length", truncated=True)


@pytest.mark.asyncio
async def test_a_close_tag_without_an_open_tag_is_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A template that opens ``<think>`` in the prompt streams ``reasoning</think>answer``."""
    body = _sse(
        _delta({"content": f"{SENTINEL} plan</th"}),
        _delta({"content": "ink>Answer."}, "stop"),
    )
    with caplog.at_level("DEBUG", logger="tern.llm.client"):
        async with _client(_respond(body)) as client:
            events = await _collect(client)
    done = events[-1]
    assert isinstance(done, Done)
    assert done.stray_think_close and not done.unclosed_think
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "stray_closes=1" in warnings[0].getMessage()
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
async def test_length_finish_is_truncated_and_usage_is_reported() -> None:
    usage = {
        "prompt_tokens": 11,
        "completion_tokens": 5,
        "total_tokens": 16,
        "completion_tokens_details": {"reasoning_tokens": 2},
    }
    body = _sse(
        _delta({"content": "cut"}, "length"),
        {"id": "x", "choices": [], "usage": usage},
    )
    async with _client(_respond(body)) as client:
        events = await _collect(client)
    assert (
        Usage(prompt_tokens=11, completion_tokens=5, total_tokens=16, reasoning_tokens=2) in events
    )
    assert events[-1] == Done(finish_reason="length", truncated=True)


@pytest.mark.asyncio
async def test_multibyte_text_split_across_byte_chunks() -> None:
    event = json.dumps(_delta({"content": "Γειά σου"}, "stop"), ensure_ascii=False)
    raw = f"data: {event}\n\ndata: [DONE]\n\n".encode()
    cut = raw.index("ά".encode()) + 1  # inside the two-byte sequence
    stream = _Chunks([raw[:cut], raw[cut:]])

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with _client(handle) as client:
        assert _content(await _collect(client)) == "Γειά σου"


@pytest.mark.asyncio
async def test_sse_comments_and_other_fields_are_skipped() -> None:
    body = (
        b": keep-alive\n\nevent: message\nid: 1\ndata: "
        + json.dumps(_delta({"content": "hi"}, "stop")).encode()
        + b"\n\nretry: 10\n\ndata: [DONE]\n\n"
    )
    async with _client(_respond(body)) as client:
        assert _content(await _collect(client)) == "hi"


# ---- egress -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["http://10.0.0.5:1234/v1", "https://llm.example.com/v1", "http://localhost.evil/v1"]
)
def test_non_loopback_chat_base_url_is_denied_at_construction(url: str) -> None:
    with pytest.raises(EgressDenied):
        LLMClient(_cfg(chat_base_url=url))


@pytest.mark.asyncio
async def test_remote_base_url_allowed_by_exact_config() -> None:
    remote = "https://llm.example.com/v1"
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=_sse(_delta({"content": "ok"}, "stop")))

    async with _client(
        handle, chat_base_url=remote, allow_remote_llm=True, remote_llm_urls=[remote]
    ) as client:
        await _collect(client)
    assert seen == ["https://llm.example.com/v1/chat/completions"]


@pytest.mark.asyncio
async def test_redirect_is_not_followed() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "127.0.0.1":
            return httpx.Response(302, headers={"Location": "http://evil.test/v1/chat/completions"})
        return httpx.Response(200, content=_sse(_delta({"content": "ok"}, "stop")))

    async with _client(handle) as client:
        with pytest.raises(LLMError, match="http_302"):
            await _collect(client)
    assert [r.url.host for r in seen] == ["127.0.0.1"]


@pytest.mark.asyncio
async def test_env_proxy_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no injected transport httpx builds its own; none of them may use an env proxy."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, "http://proxy.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    built: list[object] = []
    handled: list[httpx.Request] = []

    class RecordingTransport(httpx.AsyncBaseTransport):
        def __init__(self, *args: object, proxy: object = None, **kwargs: object) -> None:
            built.append(proxy)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            handled.append(request)
            return httpx.Response(200, content=_sse(_delta({"content": "ok"}, "stop")))

    monkeypatch.setattr(httpx._client, "AsyncHTTPTransport", RecordingTransport)
    async with LLMClient(_cfg()) as client:
        assert _content(await _collect(client)) == "ok"
    assert built == [None]  # only the direct transport, no proxy mount
    assert [str(r.url) for r in handled] == ["http://127.0.0.1:47831/v1/chat/completions"]


def test_http_client_ignores_env_and_redirects_public_api() -> None:
    """The same guarantee as above through httpx's public properties only."""
    client = LLMClient(_cfg())
    assert client._client.trust_env is False
    assert client._client.follow_redirects is False


# ---- errors -------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (404, LLMUnavailable),
        (408, LLMUnavailable),
        (429, LLMUnavailable),
        (500, LLMUnavailable),
        (503, LLMUnavailable),
        (400, LLMError),
        (401, LLMError),
        (301, LLMError),
    ],
)
async def test_status_classes_and_no_body_text_in_the_error(
    status: int, exc: type[Exception]
) -> None:
    body = json.dumps({"error": {"message": f"model said {SENTINEL}"}}).encode()
    async with _client(_respond(body, status)) as client:
        with pytest.raises(exc) as info:
            await _collect(client)
    text = str(info.value)
    assert f"http_{status}" in text and MODEL in text
    assert SENTINEL not in text and SENTINEL not in repr(info.value)


LMSTUDIO_NO_MODEL = (
    "No models loaded. Please load a model in the developer page or use the 'lms load' command."
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        LMSTUDIO_NO_MODEL,
        {"message": LMSTUDIO_NO_MODEL, "type": "invalid_request_error"},
        {"message": f"gone {SENTINEL}", "type": "invalid_request_error", "code": "model_not_found"},
        f'Model "{MODEL}" not found {SENTINEL}',
    ],
    ids=["lmstudio", "lmstudio_object", "openai_code", "model_x_not_found"],
)
async def test_a_400_saying_no_model_is_loaded_is_unavailable(error: object) -> None:
    # The agent maps LLMUnavailable to llm_unavailable ("the local model is not reachable"),
    # not llm_error. Only a reason code leaves; the body never does.
    body = json.dumps({"error": error}).encode()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, headers={"content-type": "application/json"}, content=body)

    async with _client(handle) as client:
        with pytest.raises(LLMUnavailable) as info:
            await _collect(client)
    text = str(info.value)
    assert "http_400 model_not_loaded" in text and MODEL in text
    assert SENTINEL not in text and "Please load" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"error": {"message": f"bad tools {SENTINEL}"}}).encode(),
        LMSTUDIO_NO_MODEL.encode(),  # not JSON
        json.dumps({"error": LMSTUDIO_NO_MODEL, "pad": "x" * 20_000}).encode(),  # over the cap
    ],
    ids=["other", "plain", "huge"],
)
async def test_any_other_400_stays_an_llm_error(body: bytes) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, stream=_Chunks([body[:100], body[100:]]))

    async with _client(handle) as client:
        with pytest.raises(LLMError) as info:
            await _collect(client)
    text = str(info.value)
    assert "http_400" in text and "model_not_loaded" not in text and SENTINEL not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (_sse(f"not json {SENTINEL}"), "invalid_json"),
        (_sse(json.dumps([SENTINEL])), "not_an_object"),
        (_sse({"error": {"message": SENTINEL}}), "error_event"),
        (b"event: error\ndata: " + SENTINEL.encode() + b"\n\n", "error_event"),
        (_sse(_delta({"content": 5})), "invalid_shape"),
        (_sse({"choices": [{"index": 1, "delta": {"content": SENTINEL}}]}), "choice_index"),
        (b"data: " + json.dumps(_delta({"content": SENTINEL})).encode() + b"\n\n", "no_done"),
        (SENTINEL.encode() + b"\n\n", "non_sse_line"),
    ],
)
async def test_malformed_streams_are_errors_without_content(body: bytes, reason: str) -> None:
    async with _client(_respond(body)) as client:
        with pytest.raises(LLMError, match=reason) as info:
            await _collect(client)
    assert SENTINEL not in str(info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ConnectTimeout("t")]
)
async def test_transport_failures_are_unavailable(error: Exception) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise error

    async with _client(handle) as client:
        with pytest.raises(LLMUnavailable, match=type(error).__name__):
            await _collect(client)


@pytest.mark.asyncio
async def test_stream_failing_midway_is_unavailable() -> None:
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield f"data: {json.dumps(_delta({'content': 'part'}))}\n\n".encode()
            raise httpx.RemoteProtocolError("peer closed")

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Broken())

    seen: list[LLMEvent] = []
    async with _client(handle) as client:
        with pytest.raises(LLMUnavailable, match="RemoteProtocolError"):
            async for event in client.stream(MESSAGES, TOOLS):
                seen.append(event)
    assert seen == [ContentDelta("part")]


# ---- cancellation -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_closes_the_stream() -> None:
    stream = _Chunks([f"data: {json.dumps(_delta({'content': 'first'}))}\n\n".encode()], block=True)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    got: list[LLMEvent] = []
    first = asyncio.Event()

    async def consume(client: LLMClient) -> None:
        async for event in client.stream(MESSAGES, TOOLS):
            got.append(event)
            first.set()

    async with _client(handle) as client:
        task = asyncio.create_task(consume(client))
        await asyncio.wait_for(first.wait(), 5)
        assert not stream.closed
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed
    assert got == [ContentDelta("first")]


@pytest.mark.asyncio
async def test_closing_the_generator_early_closes_the_stream() -> None:
    lines = [f"data: {json.dumps(_delta({'content': c}))}\n\n".encode() for c in "abc"]
    stream = _Chunks(lines, block=True)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with _client(handle) as client:
        async with contextlib.aclosing(client.stream(MESSAGES, TOOLS)) as events:
            async for event in events:
                assert event == ContentDelta("a")
                break
        assert stream.closed
