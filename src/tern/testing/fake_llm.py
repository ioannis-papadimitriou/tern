"""``FakeLLM``: a transport-level scripted chat server for tests and smoke (plan 08a step 1, A5).

``FakeLLM.transport`` is an ``httpx.MockTransport`` that serves scripted SSE for
``POST /v1/chat/completions`` in OpenAI streaming shape, plus synthetic ``GET /v1/models`` and
``GET /api/v0/models``. Pass it to the real ``LLMClient`` (or ``LMStudioShim``), so every test
goes through the real client: egress check, SSE parsing, fragment accumulation, cancellation.

Script format v1 (``FakeScript``, JSON-loadable): one ``ScriptStep`` per chat request, in order.
A step answers with either tool calls (name + arguments, optional fragment ``split_at`` points in
the arguments text, optional ``interleave`` of several calls' fragments) or content (optional
``split_at`` chunking, optional ``think`` text sent as ``<think>…</think>`` inside the content,
optional ``reasoning`` sent as ``delta.reasoning``), or an HTTP error status with no stream
(``http_status``, step 2b). An optional ``expect`` checks the request: the offered tool names, and
strings some message content must or must not hold. ``pause_after`` (step 2b) stops the stream
after that many SSE events until :meth:`FakeLLM.resume` is called (``FakeLLM.paused`` is set when
it gets there), so tests can cancel or time out a turn mid-stream.

It records every chat request body (``requests``) and fails loudly: a request after the script
ran out, a failed ``expect``, an unknown path or a body that is not a streamed request for the
scripted model raise ``FakeLLMScriptError`` out of the client call and are also kept in
``errors``, which ``assert_done()`` reports together with unused steps. All text is synthetic.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import Field, model_validator

from tern._model import BoundaryModel

FAKE_QUANT = "FAKE-Q4"
FAKE_CONTEXT_LENGTH = 32768


class FakeLLMScriptError(AssertionError):
    """The code under test sent a request the script did not expect."""


def _check_splits(points: list[int], length: int, what: str) -> None:
    if any(not 0 < p < length for p in points) or points != sorted(set(points)):
        raise ValueError(f"{what}: split_at must be strictly increasing, inside (0, {length})")


def _pieces(text: str, points: Sequence[int]) -> list[str]:
    bounds = [0, *points, len(text)]
    return [text[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]


class FakeToolCall(BoundaryModel):
    name: str = Field(min_length=1)
    #: A dict is sent as its JSON text; a str is sent verbatim (e.g. malformed arguments).
    arguments: dict[str, Any] | str = Field(default_factory=dict)
    id: str | None = None
    #: Split points in the arguments text; each starts a new streamed fragment.
    split_at: list[int] = Field(default_factory=list)

    def arguments_text(self) -> str:
        if isinstance(self.arguments, str):
            return self.arguments
        return json.dumps(self.arguments, ensure_ascii=False)

    @model_validator(mode="after")
    def _splits_fit(self) -> FakeToolCall:
        _check_splits(self.split_at, len(self.arguments_text()), f"tool call {self.name}")
        return self


class FakeExpect(BoundaryModel):
    #: The offered function names, as a set (None: not checked).
    tools: list[str] | None = None
    #: Each string must occur in the content of some request message.
    contains: list[str] = Field(default_factory=list)
    #: No request message content may contain any of these.
    absent: list[str] = Field(default_factory=list)


class ScriptStep(BoundaryModel):
    tool_calls: list[FakeToolCall] | None = None
    #: Round-robin the calls' fragments (index 0, 1, 0, 1, …) instead of call after call.
    interleave: bool = False
    content: str | None = None
    #: Sent inside the content stream as ``<think>…</think>`` before ``content``.
    think: str | None = None
    #: Sent as ``delta.reasoning`` before anything else.
    reasoning: str | None = None
    #: Split points of the content stream text (``<think>`` tags included).
    split_at: list[int] = Field(default_factory=list)
    #: Default: ``tool_calls`` for a tool-call step, else ``stop``.
    finish_reason: Literal["stop", "length", "tool_calls"] | None = None
    expect: FakeExpect | None = None
    #: Answer with this HTTP status and no stream (step 2b: the client's error paths).
    http_status: int | None = Field(default=None, ge=300, le=599)
    #: Stop after this many SSE events (``data:`` lines) until ``FakeLLM.resume()`` (step 2b).
    #: Must be less than the step's number of SSE events (else the request fails loudly).
    pause_after: int | None = Field(default=None, ge=0)
    #: A tool-call step only: content streamed (in one piece) before the tool calls, as a model
    #: that writes text and then calls tools in the same response (step 2b review).
    lead_content: str | None = None

    def content_text(self) -> str:
        think = "" if self.think is None else f"<think>{self.think}</think>"
        return think + (self.content or "")

    @model_validator(mode="after")
    def _one_kind(self) -> ScriptStep:
        kinds = (self.tool_calls, self.content, self.http_status)
        if sum(k is not None for k in kinds) != 1:
            raise ValueError("a script step has exactly one of tool_calls, content or http_status")
        if self.http_status is not None and (
            self.think is not None
            or self.reasoning is not None
            or self.finish_reason is not None
            or self.pause_after is not None
            or self.split_at
            or self.interleave
        ):
            raise ValueError("an http_status step has no stream fields")
        if self.tool_calls is not None and not self.tool_calls:
            raise ValueError("tool_calls is empty")
        if self.tool_calls is not None and self.split_at:
            raise ValueError("split_at is for content; tool calls carry their own split_at")
        if self.interleave and self.tool_calls is None:
            raise ValueError("interleave needs tool_calls")
        if self.lead_content is not None and self.tool_calls is None:
            raise ValueError("lead_content needs tool_calls")
        if self.split_at:
            _check_splits(self.split_at, len(self.content_text()), "content")
        return self


class FakeScript(BoundaryModel):
    version: Literal[1]
    requests: list[ScriptStep] = Field(min_length=1)


def load_script(path: Path) -> FakeScript:
    return FakeScript.model_validate_json(path.read_bytes())


class FakeLLM:
    """A scripted OpenAI-compatible chat server behind ``httpx.MockTransport``."""

    def __init__(self, script: FakeScript, *, model_id: str = "fake-chat-1") -> None:
        self.script = script
        self.model_id = model_id
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.transport = httpx.MockTransport(self._handle)
        #: Set when a step's stream reaches its ``pause_after`` point.
        self.paused = asyncio.Event()
        self._resume = asyncio.Event()

    def resume(self) -> None:
        """Let every paused (and later) ``pause_after`` point continue."""
        self._resume.set()

    @classmethod
    def from_steps(cls, *steps: ScriptStep, model_id: str = "fake-chat-1") -> FakeLLM:
        return cls(FakeScript(version=1, requests=list(steps)), model_id=model_id)

    @property
    def remaining(self) -> int:
        return len(self.script.requests) - len(self.requests)

    def assert_done(self) -> None:
        """Raise unless every step was used and nothing failed."""
        if self.errors:
            raise FakeLLMScriptError("; ".join(self.errors))
        if self.remaining:
            raise FakeLLMScriptError(f"{self.remaining} scripted request(s) never sent")

    def _fail(self, message: str) -> FakeLLMScriptError:
        self.errors.append(message)
        return FakeLLMScriptError(message)

    # ---- routing -----------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        route = (request.method, request.url.path)
        if route == ("POST", "/v1/chat/completions"):
            return self._chat(request)
        if route == ("GET", "/v1/models"):
            return httpx.Response(200, json={"object": "list", "data": [self._model_obj()]})
        if route == ("GET", "/api/v0/models"):
            entry = {
                **self._model_obj(),
                "type": "llm",
                "state": "loaded",
                "quantization": FAKE_QUANT,
                "max_context_length": FAKE_CONTEXT_LENGTH,
                "loaded_context_length": FAKE_CONTEXT_LENGTH,
            }
            return httpx.Response(200, json={"object": "list", "data": [entry]})
        raise self._fail(f"unexpected request {request.method} {request.url.path}")

    def _model_obj(self) -> dict[str, Any]:
        return {"id": self.model_id, "object": "model", "owned_by": "fake"}

    def _chat(self, request: httpx.Request) -> httpx.Response:
        n = len(self.requests)
        try:
            body = json.loads(request.content)
        except ValueError as exc:
            raise self._fail(f"request {n}: body is not JSON") from exc
        if not isinstance(body, dict):
            raise self._fail(f"request {n}: body is not an object")
        self.requests.append(body)
        if n >= len(self.script.requests):
            raise self._fail(f"request {n}: the script has only {len(self.script.requests)}")
        if body.get("model") != self.model_id or body.get("stream") is not True:
            raise self._fail(f"request {n}: not a streamed request for {self.model_id}")
        step = self.script.requests[n]
        if step.expect is not None:
            self._check(n, body, step.expect)
        if step.http_status is not None:
            return httpx.Response(step.http_status, json={"error": {"message": "scripted"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=self._sse(n, body, step),
        )

    def _check(self, n: int, body: dict[str, Any], expect: FakeExpect) -> None:
        if expect.tools is not None:
            offered: list[str] = []
            for i, t in enumerate(body.get("tools", [])):
                fn = t.get("function") if isinstance(t, dict) else None
                name = fn.get("name") if isinstance(fn, dict) else None
                if not isinstance(name, str):
                    raise self._fail(f"request {n}: tools[{i}] has no function name")
                offered.append(name)
            offered.sort()
            if offered != sorted(expect.tools):
                raise self._fail(f"request {n}: offered tools {offered} != {sorted(expect.tools)}")
        contents = [
            m["content"]
            for m in body.get("messages", [])
            if isinstance(m, dict) and isinstance(m.get("content"), str)
        ]
        for i, needle in enumerate(expect.contains):
            if not any(needle in c for c in contents):
                raise self._fail(f"request {n}: expect.contains[{i}] not found in any message")
        for i, needle in enumerate(expect.absent):
            if any(needle in c for c in contents):
                raise self._fail(f"request {n}: expect.absent[{i}] found in a message")

    # ---- SSE -----------------------------------------------------------------------------

    def _chunk(self, n: int, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": f"chatcmpl-fake-{n}",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": self.model_id,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def _chunks(self, n: int, body: dict[str, Any], step: ScriptStep) -> list[dict[str, Any]]:
        chunks = [self._chunk(n, {"role": "assistant", "content": ""})]
        produced = 0
        if step.reasoning:
            chunks.append(self._chunk(n, {"reasoning": step.reasoning}))
            produced += len(step.reasoning)
        if step.tool_calls is not None:
            if step.lead_content:
                chunks.append(self._chunk(n, {"content": step.lead_content}))
                produced += len(step.lead_content)
            per_call: list[list[dict[str, Any]]] = []
            for index, call in enumerate(step.tool_calls):
                text = call.arguments_text()
                produced += len(text)
                pieces = _pieces(text, call.split_at)
                frags = [
                    {
                        "index": index,
                        "id": call.id or f"call_fake_{n}_{index}",
                        "type": "function",
                        "function": {"name": call.name, "arguments": pieces[0]},
                    }
                ]
                frags += [{"index": index, "function": {"arguments": p}} for p in pieces[1:]]
                per_call.append(frags)
            if step.interleave:
                order = [
                    frags[i]
                    for i in range(max(len(f) for f in per_call))
                    for frags in per_call
                    if i < len(frags)
                ]
            else:
                order = [frag for frags in per_call for frag in frags]
            chunks += [self._chunk(n, {"tool_calls": [frag]}) for frag in order]
            finish = step.finish_reason or "tool_calls"
        else:
            text = step.content_text()
            produced += len(text)
            chunks += [self._chunk(n, {"content": p}) for p in _pieces(text, step.split_at) if p]
            finish = step.finish_reason or "stop"
        chunks.append(self._chunk(n, {}, finish))
        prompt_tokens = len(json.dumps(body.get("messages", [])).encode()) // 4
        completion_tokens = produced // 4 + 1
        chunks.append(
            {
                "id": f"chatcmpl-fake-{n}",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": self.model_id,
                "choices": [],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        )
        return chunks

    async def _sse(self, n: int, body: dict[str, Any], step: ScriptStep) -> AsyncIterator[bytes]:
        lines = [
            f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            for chunk in self._chunks(n, body, step)
        ]
        lines.append(b"data: [DONE]\n\n")
        if step.pause_after is not None and step.pause_after >= len(lines):
            # It would never pause, and a test waiting on ``paused`` would hang: fail instead.
            events = len(lines)
            raise self._fail(
                f"request {n}: pause_after={step.pause_after} but the stream has {events} events"
            )
        for i, line in enumerate(lines):
            if i == step.pause_after:
                self.paused.set()
                await self._resume.wait()
            yield line
