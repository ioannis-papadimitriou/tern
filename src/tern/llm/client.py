"""OpenAI-compatible streaming chat client (plan 08a step 1; #9, #15 amendment 2026-11-14).

``LLMClient`` POSTs to ``<chat_base_url>/chat/completions`` and nothing else (runtime-specific
calls live in a shim such as ``tern.llm.lmstudio``). The request body is S3's: ``model``,
``messages``, ``tools`` with ``tool_choice: "auto"`` (both left out when no tool is offered),
``temperature``, ``seed``, ``max_tokens``, ``stream: true`` and ``stream_options.include_usage``.
The base URL passes ``tern.llm.egress.check_url`` at construction (an ``EgressDenied`` is raised
to the caller), and the httpx client never reads proxy settings from the environment and never
follows redirects.

``stream()`` is an async generator of events, parsed from SSE ``data:`` lines up to ``[DONE]``:

- ``ContentDelta``: answer text. Reasoning never appears here: ``delta.reasoning`` /
  ``delta.reasoning_content`` and ``<think>…</think>`` inside content (tags may be split across
  chunks) become ``ReasoningDelta`` events that carry only a character count.
- ``ToolCalls``: the completed tool calls, once, after the stream ends. Fragments are
  accumulated by ``index``: id and name from the first fragment that carries them, arguments
  concatenated (as raw text; the agent loop parses them). A call whose server sent no id gets
  ``call_<index>`` (the id only pairs a tool message with its call).
- ``Usage``: the token counts the server reports (``include_usage``).
- ``Done``: the finish reason; ``finish_reason == "length"`` is reported as ``truncated``. Three
  flags make a malformed ``<think>`` loud instead of silently changing what the user sees (all
  also log one WARNING with counts only): ``unclosed_think`` (a ``<think>`` still open at
  ``[DONE]`` on a finish other than ``length``: everything after it was counted as reasoning, not
  answer; the tag may come from quoted mail), ``late_think`` (a ``<think>`` opened after
  non-whitespace answer text had been yielded, closed or not: the text inside it was counted as
  reasoning and removed from the answer; again the tag may come from quoted mail) and
  ``stray_think_close`` (a ``</think>`` arrived outside a think block, as with a chat template
  that opens ``<think>`` in the prompt: the text before it was already yielded as answer
  content). The consumer (the agent loop) turns any of them into an outcome code.

Errors: ``LLMUnavailable`` for a request that cannot complete (connection, timeout, or HTTP 404,
408, 429, 5xx: the model is not loaded or the server is busy; or an HTTP 400 whose JSON error
says no model is loaded or the model is not found, :func:`model_not_loaded`, as LM Studio
answers with JIT loading off); ``LLMError`` for any other status (a redirect included), an
error event, or a malformed stream (bad JSON, a wrong shape, a stream
that ends without ``[DONE]``). Messages carry a reason code, the exception type and the model
id, never request or response text. Cancelling the consumer (or closing the generator) closes
the HTTP stream.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tern.config import TernConfig
from tern.llm.egress import check_url

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({404, 408, 429})
#: Longest HTTP 400 body read to classify it (LM Studio's error bodies are a few hundred bytes).
ERROR_BODY_MAX = 16 * 1024
#: OpenAI-style ``error.code`` values meaning the requested model is not there.
_NO_MODEL_CODES = frozenset({"model_not_found"})
#: LM Studio: "No models loaded. Please load a model in the developer page or use the 'lms
#: load' command."; others: "model not found", "Model \"x\" not found".
_NO_MODEL_TEXT = re.compile(
    r"\bno models? (?:is |are )?loaded\b|\bmodel_not_found\b|\bmodel\b[^\n]{0,200}?\bnot found\b",
    re.IGNORECASE,
)
_SSE_FIELDS = frozenset({"event", "id", "retry"})


def model_not_loaded(body: bytes) -> bool:
    """Whether an HTTP 400 body is an OpenAI-compatible error saying the server has no model
    loaded or does not find the requested one: ``{"error": "<message>"}`` (LM Studio) or
    ``{"error": {"message": ..., "code": ...}}``. A server in that state can serve once the model
    is loaded, so callers treat it as unavailable, not as a refused request. Any other body (not
    JSON, another shape or message, over :data:`ERROR_BODY_MAX`) is False: an ordinary 400. Only
    a yes/no leaves here; the body text never does."""
    if len(body) > ERROR_BODY_MAX:
        return False
    try:
        doc = json.loads(body)
    except ValueError:  # not JSON (UnicodeDecodeError included): not the no-model answer
        return False
    if not isinstance(doc, dict):
        return False
    error = doc.get("error")
    if isinstance(error, str):
        return _NO_MODEL_TEXT.search(error) is not None
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        if isinstance(code, str) and code in _NO_MODEL_CODES:
            return True
        return isinstance(message, str) and _NO_MODEL_TEXT.search(message) is not None
    return False


async def _read_capped(response: httpx.Response) -> bytes:
    """At most a little over :data:`ERROR_BODY_MAX` bytes of a streamed response body."""
    buf = bytearray()
    async for chunk in response.aiter_bytes():
        buf += chunk
        if len(buf) > ERROR_BODY_MAX:
            break
    return bytes(buf)


class LLMError(RuntimeError):
    """The chat server answered with something unusable (refused, malformed, an error event)."""


class LLMUnavailable(RuntimeError):
    """The chat server could not be reached or cannot serve the model right now."""


# ---- events -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContentDelta:
    """A piece of answer text (never reasoning)."""

    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    """Reasoning arrived; only its length is reported, the text is dropped (P7)."""

    chars: int


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One completed tool call; ``arguments`` is the raw JSON text the model produced."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolCalls:
    calls: tuple[ToolCall, ...]


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    reasoning_tokens: int | None


@dataclass(frozen=True, slots=True)
class Done:
    """The end of a stream; see the module docstring for the three ``<think>`` flags."""

    finish_reason: str | None
    truncated: bool
    unclosed_think: bool = False
    stray_think_close: bool = False
    late_think: bool = False


LLMEvent = ContentDelta | ReasoningDelta | ToolCalls | Usage | Done


# ---- stream chunk shapes (validated; unknown keys ignored) -------------------------------


class _Chunk(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True, strict=True)


class _Function(_Chunk):
    name: str | None = None
    arguments: str | None = None


class _ToolCallDelta(_Chunk):
    index: int = Field(ge=0)
    id: str | None = None
    function: _Function | None = None


class _Delta(_Chunk):
    content: str | None = None
    reasoning: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[_ToolCallDelta] | None = None


class _Choice(_Chunk):
    index: int = 0
    delta: _Delta | None = None
    finish_reason: str | None = None


class _CompletionDetails(_Chunk):
    reasoning_tokens: int | None = None


class _Usage(_Chunk):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    completion_tokens_details: _CompletionDetails | None = None


class _StreamChunk(_Chunk):
    choices: list[_Choice] = Field(default_factory=list)
    usage: _Usage | None = None


# ---- <think> splitting -------------------------------------------------------------------


class ThinkSplitter:
    """Split streamed content into (answer, reasoning); a tag may be split across chunks.

    ``inside`` tells whether a ``<think>`` is open; ``stray_closes`` counts ``</think>`` tags seen
    outside a think block (kept in the answer text, which cannot be taken back once streamed);
    ``late_opens`` counts ``<think>`` tags opened after non-whitespace answer text was emitted,
    whether or not they close (their text is reasoning, so it is missing from the answer).
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self._inside = False
        self._buf = ""
        self._answered = False
        self.stray_closes = 0
        self.late_opens = 0

    @property
    def inside(self) -> bool:
        return self._inside

    def feed(self, text: str) -> tuple[str, str]:
        self._buf += text
        answer: list[str] = []
        reasoning: list[str] = []
        while self._buf:
            if self._inside:
                i = self._buf.find(self.CLOSE)
                if i >= 0:
                    reasoning.append(self._buf[:i])
                    self._buf = self._buf[i + len(self.CLOSE) :]
                    self._inside = False
                    continue
                keep = _partial_suffix(self._buf, self.CLOSE)
            else:
                i_open = self._buf.find(self.OPEN)
                i_close = self._buf.find(self.CLOSE)
                if i_close >= 0 and (i_open < 0 or i_close < i_open):
                    self.stray_closes += 1
                    end = i_close + len(self.CLOSE)
                    self._emit(answer, self._buf[:end])
                    self._buf = self._buf[end:]
                    continue
                if i_open >= 0:
                    self._emit(answer, self._buf[:i_open])
                    if self._answered:
                        self.late_opens += 1
                    self._buf = self._buf[i_open + len(self.OPEN) :]
                    self._inside = True
                    continue
                keep = max(
                    _partial_suffix(self._buf, self.OPEN), _partial_suffix(self._buf, self.CLOSE)
                )
            cut = len(self._buf) - keep
            if self._inside:
                reasoning.append(self._buf[:cut])
            else:
                self._emit(answer, self._buf[:cut])
            self._buf = self._buf[cut:]
            break
        return "".join(answer), "".join(reasoning)

    def _emit(self, answer: list[str], text: str) -> None:
        answer.append(text)
        if text.strip():
            self._answered = True

    def flush(self) -> tuple[str, str]:
        """What is held back at the end: reasoning if a ``<think>`` is still open."""
        rest, self._buf = self._buf, ""
        return ("", rest) if self._inside else (rest, "")


def _partial_suffix(s: str, tag: str) -> int:
    """Length of the longest proper prefix of ``tag`` that ``s`` ends with."""
    for n in range(min(len(tag) - 1, len(s)), 0, -1):
        if s.endswith(tag[:n]):
            return n
    return 0


# ---- client ------------------------------------------------------------------------------


@dataclass
class _PendingCall:
    id: str = ""
    name: str = ""
    arguments: str = ""


class LLMClient:
    """A streaming OpenAI-compatible chat client (loopback unless config allows a remote)."""

    def __init__(
        self,
        cfg: TernConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        """``base_url``/``model`` override ``cfg.chat_base_url``/``cfg.chat_model`` (a model the
        user picked in the chat tab, `tern.llm.models`); the egress guard applies either way."""
        base = check_url(cfg.chat_base_url if base_url is None else base_url, cfg)
        self.base_url: str = base
        self.model_id: str = cfg.chat_model if model is None else model
        self._endpoint = base.rstrip("/") + "/chat/completions"
        self._temperature = cfg.chat_temperature
        self._seed = cfg.chat_seed
        self._max_tokens = cfg.chat_max_tokens
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=cfg.chat_request_timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def request_body(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        """The JSON body of one chat request (S3's shape)."""
        body: dict[str, Any] = {"model": self.model_id, "messages": list(messages)}
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = "auto"
        body.update(
            temperature=self._temperature,
            seed=self._seed,
            max_tokens=self._max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        return body

    def _error(self, reason: str) -> LLMError:
        return LLMError(f"{reason} (model={self.model_id})")

    def _unavailable(self, reason: str) -> LLMUnavailable:
        return LLMUnavailable(f"{reason} (model={self.model_id})")

    async def stream(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]] = ()
    ) -> AsyncGenerator[LLMEvent, None]:
        """Stream one chat completion as events (see the module docstring).

        Use it under ``contextlib.aclosing`` when the consumer may stop early, so the HTTP
        stream is closed at once rather than when the generator is collected.
        """
        body = self.request_body(messages, tools)
        try:
            async with self._client.stream("POST", self._endpoint, json=body) as response:
                status = response.status_code
                if status in _RETRYABLE_STATUS or status >= 500:
                    raise self._unavailable(f"http_{status}")
                if status == 400 and model_not_loaded(await _read_capped(response)):
                    raise self._unavailable("http_400 model_not_loaded")
                if not 200 <= status < 300:
                    raise self._error(f"http_{status}")
                async for event in self._events(response):
                    yield event
        except httpx.TransportError as exc:  # connect, timeout, peer closed mid-stream
            raise self._unavailable(f"transport_error {type(exc).__name__}") from exc
        except httpx.DecodingError as exc:
            raise self._error(f"decoding_error {type(exc).__name__}") from exc
        except httpx.RequestError as exc:  # any other request failure
            raise self._error(f"request_error {type(exc).__name__}") from exc

    async def _events(self, response: httpx.Response) -> AsyncGenerator[LLMEvent, None]:
        splitter = ThinkSplitter()
        calls: dict[int, _PendingCall] = {}
        finish: str | None = None
        n_content = n_reasoning = 0
        saw_done = False
        async for line in response.aiter_lines():
            if not line.strip() or line.startswith(":"):
                continue  # event separator or SSE comment
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field != "data":
                if field == "event" and value.strip() == "error":
                    raise self._error("error_event")
                if field in _SSE_FIELDS:
                    continue
                raise self._error("malformed_stream non_sse_line")
            if value.strip() == "[DONE]":
                saw_done = True
                break
            chunk = self._parse_chunk(value)
            if chunk.usage is not None:
                u = chunk.usage
                details = u.completion_tokens_details
                yield Usage(
                    prompt_tokens=u.prompt_tokens,
                    completion_tokens=u.completion_tokens,
                    total_tokens=u.total_tokens,
                    reasoning_tokens=None if details is None else details.reasoning_tokens,
                )
            for choice in chunk.choices:
                if choice.index != 0:
                    raise self._error("malformed_stream choice_index")
                if choice.finish_reason is not None:
                    finish = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                for reasoning in (delta.reasoning, delta.reasoning_content):
                    if reasoning:
                        n_reasoning += len(reasoning)
                        yield ReasoningDelta(len(reasoning))
                if delta.content:
                    answer, think = splitter.feed(delta.content)
                    if think:
                        n_reasoning += len(think)
                        yield ReasoningDelta(len(think))
                    if answer:
                        n_content += len(answer)
                        yield ContentDelta(answer)
                for fragment in delta.tool_calls or ():
                    self._accumulate(calls, fragment)
        if not saw_done:
            raise self._error("malformed_stream no_done")
        answer, think = splitter.flush()
        if think:
            n_reasoning += len(think)
            yield ReasoningDelta(len(think))
        if answer:
            n_content += len(answer)
            yield ContentDelta(answer)
        if calls:
            yield ToolCalls(self._complete(calls))
        truncated = finish == "length"
        unclosed = splitter.inside and not truncated
        if unclosed or splitter.stray_closes or splitter.late_opens:
            log.warning(
                "chat stream think tags malformed finish=%s unclosed_think=%s stray_closes=%d "
                "late_opens=%d content_chars=%d reasoning_chars=%d",
                finish,
                unclosed,
                splitter.stray_closes,
                splitter.late_opens,
                n_content,
                n_reasoning,
            )
        log.debug(
            "chat stream done finish=%s content_chars=%d reasoning_chars=%d tool_calls=%d",
            finish,
            n_content,
            n_reasoning,
            len(calls),
        )
        yield Done(
            finish_reason=finish,
            truncated=truncated,
            unclosed_think=unclosed,
            stray_think_close=splitter.stray_closes > 0,
            late_think=splitter.late_opens > 0,
        )

    def _parse_chunk(self, data: str) -> _StreamChunk:
        try:
            obj = json.loads(data)
        except ValueError as exc:
            raise self._error("malformed_stream invalid_json") from exc
        if not isinstance(obj, dict):
            raise self._error("malformed_stream not_an_object")
        if "error" in obj:
            raise self._error("error_event")
        try:
            return _StreamChunk.model_validate(obj)
        except ValidationError as exc:
            raise self._error("malformed_stream invalid_shape") from exc

    def _accumulate(self, calls: dict[int, _PendingCall], fragment: _ToolCallDelta) -> None:
        acc = calls.setdefault(fragment.index, _PendingCall())
        if fragment.id and not acc.id:
            acc.id = fragment.id
        fn = fragment.function
        if fn is None:
            return
        if fn.name:
            if not acc.name:
                acc.name = fn.name
            elif fn.name != acc.name:
                raise self._error("malformed_stream tool_call_name_changed")
        if fn.arguments:
            acc.arguments += fn.arguments

    def _complete(self, calls: dict[int, _PendingCall]) -> tuple[ToolCall, ...]:
        done: list[ToolCall] = []
        for index in sorted(calls):
            acc = calls[index]
            if not acc.name:
                raise self._error("malformed_stream tool_call_without_name")
            done.append(
                ToolCall(id=acc.id or f"call_{index}", name=acc.name, arguments=acc.arguments)
            )
        return tuple(done)
