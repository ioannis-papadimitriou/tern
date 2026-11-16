"""The agent loop (plan 08a step 2b): one chat session, turn by turn.

``AgentSession(session_key, registry, llm, index, cfg, *, hold=None, clock)`` holds one session's
history in memory (system prompt, user turns, assistant content without reasoning, tool
messages; nothing is persisted). Its caller id is ``agent:<session_key>``, checked against the
audit caller rule at construction.

**Offered tools** (:func:`offered_tools`): the registry's tools minus any with a forbidden effect
(``tern.perms.engine.forbidden_effects``, a pure function: ternd never imports the MCP SDK, #34)
minus any whose effective tier is above R1 (08a is read-only; plan 10 lifts this), in OpenAI
function format from ``ToolMeta.description`` and ``input_schema``. Of the built-ins only those
named in ``cfg.agent_tools`` are offered (None: :data:`DEFAULT_CHAT_BUILTINS`, the vendored
thunderbird-mcp read tools, ``skill`` and ``open_in_thunderbird``, #38); plugin tools are always
offered.

**A turn** (:meth:`AgentSession.start_turn` → :class:`TurnHandle`) runs in its own task:

- at its start, the tool exchanges of earlier turns (assistant tool calls and their outputs)
  are dropped from the history: each earlier turn becomes its user message and one assistant
  message holding the answer the user saw (``dropped_outputs`` counts the outputs dropped);
- before each chat request, the context budget (``tern.agent.budget``, per turn and
  prefix-stable) is enforced over the turn's tool outputs, with every output not sent in an
  earlier request (all outputs of the latest response) as the newest group: only those are cut
  if need be, and nothing already sent is rewritten, so each request extends the previous one;
  each request runs inside ``hold("chat")`` when a hold is given (``Daemon.embed.hold``),
  released on error and cancel too;
- answer deltas are yielded by iterating the handle; reasoning is only counted (P7);
- every tool call the model emits goes to ``Registry.call(agent:<key>, name, args,
  provenance=session.provenance())``, offered or not: the registry audits it and the engine
  decides (#10, #7, #34). The provenance is every mid whose untrusted text entered the session,
  over all its turns (``tern.agent.provenance``, M4); the registry shows it on W2/W3 approval
  cards only, so it is passed on every call without a tier check. Calls of one response run
  sequentially, in order. Arguments that are not a JSON object never reach the registry (no
  argument mapping to audit, P5): the model gets an ``invalid_params`` tool message, the turn
  counts it, a WARNING logs counts only. An untrusted result is sent as
  ``wrap(json, tool=name)``, a trusted one as JSON, a ``ToolError`` as
  ``{"error": {code, reason, message}}``: plain JSON for a built-in tool (its messages are
  content-free, architecture §5), wrapped like a result for a plugin tool declaring
  ``untrusted_output`` (the content-free rule is only a convention for third-party code).
- a turn started with ``ask`` (a notifier: the chat's ``manual`` mode, decisions #39) passes it
  on every ``Registry.call`` of the turn, so the user is asked before each call runs; a refusal
  (``denied``/``user_denied``, ``expired``) is a ``ToolError`` like any other, so the model gets
  it as an error tool message and the turn goes on. Without ``ask`` nothing is asked (``bypass``,
  and every caller before 1.2).

Outcomes (:data:`Outcome`): ``ok`` (an answer without tool calls); ``max_tool_calls`` (a response
whose calls would take the turn over ``agent_max_tool_calls``; none of them runs, as in S3; 0 = no
limit); ``timeout`` (``agent_turn_timeout_s`` for the whole turn; 0 = none); ``cancelled``
(:meth:`TurnHandle.cancel`: the chat stream is closed and an awaiting ``Registry.call`` is
cancelled, so the registry records it); ``llm_unavailable`` / ``llm_error``; ``malformed_think`` (a
``Done`` flagged ``unclosed_think``, ``late_think`` or ``stray_think_close``: the text already
streamed is not taken back; the outcome makes it loud, P14); ``truncated`` (the server cut the
response at ``max_tokens``). The last two end the turn without running that response's calls. A
cancellation from outside (shutdown) is re-raised, never turned into an outcome; the handle's
deltas end even when it lands before the turn's first step.

**Citations** (``tern.agent.citations``, step 4; short ids since plan 08c): every answer delta
passes through the turn's streaming citation filter before it is yielded, kept in the turn text
or put into the history. The model may cite the short id a tool showed, a longer prefix or the
full mid. A citation is valid only when, when its span closes, its reference is a prefix of
exactly one indexed mid (a range scan of ``messages``), that mid is not ``conflicted``, and an id
under a ``mid`` key of a tool result returned this turn (any successful result, trusted or not;
a short id or a full mid) is a prefix of it; a valid one leaves ternd with the full mid in place
of the reference (what the chat tab renders as a chip and ``chat.done`` lists), an invalid one
is replaced by ``[citation removed]``. A span still held when the turn's last response ends
normally is released unchanged (it never closed, so it is not a citation); at the end of a
response with tool calls it stays held, so the next response's text continues it and a citation
split across two responses is judged like any other. When the turn ends abnormally (cancel,
timeout, LLM failure) a held span is dropped. The history holds the filtered text, i.e. what the
user saw, full mids included (a span held across a tool call is kept with the response whose
text resolved it). The raw, invalid and conflicted counts (before stripping) go into the turn
metadata; ``AgentTurn.citations`` is the surviving full mids in order of first appearance.

A turn that ends early leaves the history well-formed: the partial answer is kept as assistant
content, a call that never got its result gets a ``not_run`` error tool message, and an
assistant message (possibly empty) follows the turn's last user or tool message; the next turn
then drops the tool exchange as for any earlier turn.

Logging: one INFO line per turn (turn id, outcome, counts, durations); never content.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import time
import uuid
from collections.abc import Callable, Collection
from contextlib import AbstractAsyncContextManager, aclosing
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, cast

from pydantic_core import PydanticSerializationError

from tern.agent import system_prompt
from tern.agent.budget import ToolOutput, enforce
from tern.agent.citations import CitationFilter, Ruling, resolve
from tern.agent.provenance import SessionProvenance, output_refs
from tern.config import TernConfig
from tern.index.store import Store
from tern.llm.client import (
    ContentDelta,
    Done,
    LLMClient,
    LLMError,
    LLMUnavailable,
    ReasoningDelta,
    ToolCall,
    ToolCalls,
    Usage,
)
from tern.perms.approvals import ApprovalNotifier
from tern.perms.audit import CALLER_RE
from tern.perms.engine import forbidden_effects
from tern.tools.builtin import CHAT_TOOL_NAMES
from tern.tools.contract import Tier, ToolError
from tern.tools.ids import normalize_ref, prefix_range
from tern.tools.registry import Registry

log = logging.getLogger(__name__)

#: How a turn ended (module docstring). 08b sends anything but ``ok`` as ``chat.error.reason``.
Outcome = Literal[
    "ok",
    "max_tool_calls",
    "timeout",
    "cancelled",
    "llm_unavailable",
    "llm_error",
    "malformed_think",
    "truncated",
]

#: The highest effective tier offered to the model in 08a (read-only; plan 10 lifts it).
MAX_OFFERED_TIER = Tier.R1

#: ``hold(reason)``: an async context manager during which background model work pauses
#: (``tern.search.embed_queue.EmbedQueue.hold``).
Hold = Callable[[str], AbstractAsyncContextManager[object]]

#: The reason passed to ``hold``.
HOLD_REASON = "chat"


def _today_line(today: date | None) -> str:
    """The date line appended to the system prompt (``today`` given: ternd's chat sessions), so
    the model can resolve "today" or "since yesterday"; empty when None."""
    if today is None:
        return ""
    return f"\nToday is {today.isoformat()} ({today.strftime('%A')}).\n"


#: The built-ins the chat model is offered by default (#38): thunderbird-mcp's read tools, the
#: skill texts, and opening a message in Thunderbird.
DEFAULT_CHAT_BUILTINS: frozenset[str] = frozenset(
    {*CHAT_TOOL_NAMES, "open_in_thunderbird", "read_attachment"}
)


def offered_tools(
    registry: Registry,
    *,
    max_tier: Tier = MAX_OFFERED_TIER,
    builtins: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """The tools offered to the model, in OpenAI function format, sorted by name: no forbidden
    effect, an effective tier at most ``max_tier``, and, for a built-in, a name in ``builtins``
    (None: :data:`DEFAULT_CHAT_BUILTINS`). Plugin tools are not filtered by name."""
    allowed = DEFAULT_CHAT_BUILTINS if builtins is None else frozenset(builtins)
    offered: list[dict[str, Any]] = []
    for name in registry.names():
        reg = registry.get(name)
        assert reg is not None  # a name the registry just listed
        if forbidden_effects(reg.spec) or reg.spec.effective_tier.rank > max_tier.rank:
            continue
        if reg.origin == "builtin" and name not in allowed:
            continue
        meta = reg.meta()
        offered.append(
            {
                "type": "function",
                "function": {
                    "name": meta.name,
                    "description": meta.description,
                    "parameters": meta.input_schema,
                },
            }
        )
    return offered


@dataclass(frozen=True, slots=True)
class TurnMeta:
    """A turn's counts and timestamps. ``tool_calls``: calls passed to ``Registry.call``;
    ``unparseable_args``: calls refused because their arguments were not a JSON object;
    ``dropped_outputs``: tool outputs of earlier turns dropped from the history at this turn's
    start; ``truncated_outputs``: outputs of this turn cut by the context budget;
    ``usage``: what the server reported, one entry per chat request; ``raw_citations`` (every
    citation the model wrote, per occurrence, before stripping), ``invalid_citations`` (those
    stripped, conflicted ones included) and ``conflicted_citations`` (those of a mid the index
    marks conflicted). Timestamps come from the session's clock: ``t_start`` (the turn started),
    ``t_first_visible`` (the first visible answer delta, after the citation filter, with
    non-whitespace text; None without one) and ``t_done`` (architecture §9a)."""

    tool_calls: int
    unparseable_args: int
    dropped_outputs: int
    truncated_outputs: int
    llm_requests: int
    reasoning_chars: int
    usage: tuple[Usage, ...]
    raw_citations: int
    invalid_citations: int
    conflicted_citations: int
    t_start: float
    t_first_visible: float | None
    t_done: float


@dataclass(frozen=True, slots=True)
class AgentTurn:
    """One finished turn: the answer text shown to the user (every delta of the turn, joined,
    after the citation filter), the surviving cited mids in order of first appearance, the
    outcome and the metadata."""

    turn_id: str
    text: str
    citations: tuple[str, ...]
    outcome: Outcome
    meta: TurnMeta


_END = object()


class TurnHandle:
    """A running turn. Iterate it for answer deltas (``async for text in handle``); ``await
    handle.result()`` for the :class:`AgentTurn`; :meth:`cancel` to stop it."""

    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._ended = False
        self._end_queued = False
        self._started = False
        self._cancel_requested = False
        self._task: asyncio.Task[AgentTurn] | None = None

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    @property
    def done(self) -> bool:
        return self._task is not None and self._task.done()

    def cancel(self) -> None:
        """Stop the turn (outcome ``cancelled``). No delta is yielded after this returns. A
        no-op once the turn has finished."""
        if self._cancel_requested or self.done:
            return
        self._cancel_requested = True
        if self._started and self._task is not None:
            self._task.cancel()

    def _push(self, text: str) -> None:
        if not self._cancel_requested:
            self._queue.put_nowait(text)

    def _end(self) -> None:
        """Queue the end of the deltas, once (``_run`` does it, and the task's done callback
        does it for a task cancelled from outside before its first step, when ``_run`` never
        ran)."""
        if not self._end_queued:
            self._end_queued = True
            self._queue.put_nowait(_END)

    def __aiter__(self) -> TurnHandle:
        return self

    async def __anext__(self) -> str:
        if self._ended or self._cancel_requested:
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is _END:
            self._ended = True
            raise StopAsyncIteration
        if self._cancel_requested:
            raise StopAsyncIteration
        return cast("str", item)

    async def result(self) -> AgentTurn:
        """The finished turn. Raises whatever ended the turn's task otherwise (a cancellation
        from outside, a tern bug)."""
        assert self._task is not None  # set by AgentSession.start_turn
        return await self._task


@dataclass
class _TurnState:
    t_start: float
    cite: CitationFilter
    #: Ids (short ids or full mids) under a ``mid`` key of a tool result returned this turn
    #: (citation validation).
    returned: set[str] = field(default_factory=set[str])
    tool_calls: int = 0
    unparseable_args: int = 0
    dropped: int = 0
    truncated: int = 0
    llm_requests: int = 0
    reasoning_chars: int = 0
    usage: list[Usage] = field(default_factory=list[Usage])
    t_first_visible: float | None = None
    text: list[str] = field(default_factory=list[str])
    #: The answer text of the response being streamed, until it is put into the history.
    pending: list[str] | None = None
    #: Tool-call ids put into the history whose result is not there yet.
    open_calls: list[str] = field(default_factory=list[str])
    #: Asks the user before each tool call of the turn (``Registry.call(ask=...)``), or None.
    ask: ApprovalNotifier | None = None


@dataclass(frozen=True, slots=True)
class _Response:
    content: str
    calls: tuple[ToolCall, ...]
    done: Done


def _error_json(code: str, reason: str | None, message: str) -> str:
    return json.dumps({"error": {"code": code, "reason": reason, "message": message}})


class AgentSession:
    """One chat session (module docstring). One turn at a time."""

    def __init__(
        self,
        session_key: str,
        registry: Registry,
        llm: LLMClient,
        index: Store,
        cfg: TernConfig,
        *,
        hold: Hold | None = None,
        clock: Callable[[], float] = time.monotonic,
        today: date | None = None,
    ) -> None:
        caller = f"agent:{session_key}" if isinstance(session_key, str) else ""
        if not CALLER_RE.fullmatch(caller):
            raise ValueError("invalid session key (the caller rule allows [A-Za-z0-9._-]{1,64})")
        if not isinstance(registry, Registry):
            raise TypeError("registry must be a Registry")
        self.session_key = session_key
        self.caller = caller
        self._registry = registry
        self._llm = llm
        #: Citation validation looks cited mids up here (``_judge``).
        self.index = index
        self._cfg = cfg
        self._hold = hold
        self._clock = clock
        self._tools = offered_tools(registry, builtins=cfg.agent_tools)
        self._history: list[dict[str, Any] | ToolOutput] = [
            {"role": "system", "content": system_prompt() + _today_line(today)}
        ]
        #: The running turn's tool outputs (the budget's scope).
        self._outputs: list[ToolOutput] = []
        self._provenance = SessionProvenance(resolve=self._prefix_mids)
        #: How many of ``_outputs`` went out in an earlier request of the turn (the budget's
        #: newest group starts here).
        self._sent = 0
        self._active: TurnHandle | None = None

    @property
    def tools(self) -> list[dict[str, Any]]:
        """The offered tools (OpenAI function format)."""
        return list(self._tools)

    def provenance(self) -> tuple[str, ...]:
        """Every mid whose untrusted text entered this session so far (all turns), in order of
        first appearance: what each ``Registry.call`` gets as ``provenance``."""
        return self._provenance.mids()

    def messages(self) -> list[dict[str, Any]]:
        """The history as chat messages (what the next request of the running turn would send,
        before the budget; a new turn first drops earlier tool exchanges)."""
        return [m.message() if isinstance(m, ToolOutput) else dict(m) for m in self._history]

    def start_turn(self, text: str, *, ask: ApprovalNotifier | None = None) -> TurnHandle:
        """Start a turn for the user message ``text``; needs a running event loop. A second turn
        while one is running is a ``RuntimeError``. ``ask``: ask the user through it before each
        tool call of the turn runs (module docstring)."""
        if not isinstance(text, str):
            raise TypeError("text must be a str")
        if ask is not None and not isinstance(ask, ApprovalNotifier):
            raise TypeError("ask must provide async requested(card) and resolved(id, status)")
        if self._active is not None and not self._active.done:
            raise RuntimeError("a turn is already running in this session")
        handle = TurnHandle(uuid.uuid4().hex)
        handle._task = asyncio.get_running_loop().create_task(
            self._run(handle, text, ask), name=f"agent-turn-{handle.turn_id}"
        )
        handle._task.add_done_callback(lambda _task: handle._end())
        self._active = handle
        return handle

    # -- one turn -------------------------------------------------------------------------------

    async def _run(
        self, handle: TurnHandle, text: str, ask: ApprovalNotifier | None = None
    ) -> AgentTurn:
        handle._started = True
        returned: set[str] = set()
        st = _TurnState(
            t_start=self._clock(),
            cite=CitationFilter(functools.partial(self._judge, returned)),
            returned=returned,
            ask=ask,
        )
        st.dropped = self._drop_tool_exchanges()
        self._history.append({"role": "user", "content": text})
        outcome: Outcome
        try:
            if handle.cancel_requested:  # cancelled before the task first ran
                outcome = "cancelled"
            else:
                outcome = await self._run_limited(handle, st)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if not handle.cancel_requested or task is None or task.uncancel() > 0:
                self._close_history(st)
                handle._end()
                raise
            outcome = "cancelled"
        except BaseException:
            self._close_history(st)
            handle._end()
            raise
        self._close_history(st)
        turn = self._turn(handle.turn_id, st, outcome)
        handle._end()
        m = turn.meta
        log.info(
            "agent turn done",
            extra={
                "turn_id": turn.turn_id,
                "outcome": outcome,
                "tool_calls": m.tool_calls,
                "unparseable_args": m.unparseable_args,
                "dropped_outputs": m.dropped_outputs,
                "truncated_outputs": m.truncated_outputs,
                "llm_requests": m.llm_requests,
                "reasoning_chars": m.reasoning_chars,
                "raw_citations": m.raw_citations,
                "invalid_citations": m.invalid_citations,
                "conflicted_citations": m.conflicted_citations,
                "citations": len(turn.citations),
                "answer_chars": len(turn.text),
                "first_visible_ms": None
                if m.t_first_visible is None
                else round((m.t_first_visible - m.t_start) * 1000),
                "total_ms": round((m.t_done - m.t_start) * 1000),
            },
        )
        return turn

    async def _run_limited(self, handle: TurnHandle, st: _TurnState) -> Outcome:
        """The exchange under the turn timeout, with LLM failures mapped to outcomes."""
        limit_s = self._cfg.agent_turn_timeout_s
        timeout = asyncio.timeout(limit_s if limit_s > 0 else None)  # 0: no turn limit
        try:
            async with timeout:
                return await self._exchange(handle, st)
        except TimeoutError:
            if not timeout.expired():
                raise  # not the turn's timeout: a bug elsewhere, loud
            return "timeout"
        except LLMUnavailable as exc:
            log.warning("agent chat request unavailable: %s", exc)  # content-free by contract
            return "llm_unavailable"
        except LLMError as exc:
            log.warning("agent chat request failed: %s", exc)  # content-free by contract
            return "llm_error"

    async def _exchange(self, handle: TurnHandle, st: _TurnState) -> Outcome:
        while True:
            change = enforce(
                self._outputs, self._cfg.agent_context_budget_tokens, newest_from=self._sent
            )
            st.truncated += change.truncated
            messages = self.messages()
            resp = await self._request(handle, st, messages, sent=len(self._outputs))
            done = resp.done
            if done.unclosed_think or done.late_think or done.stray_think_close:
                self._final(handle, st)
                return "malformed_think"
            if done.truncated:
                self._final(handle, st)
                return "truncated"
            if not resp.calls:
                self._final(handle, st)
                return "ok"
            attempted = st.tool_calls + st.unparseable_args
            cap = self._cfg.agent_max_tool_calls  # 0: no limit
            if cap and attempted + len(resp.calls) > cap:
                self._final(handle, st)
                return "max_tool_calls"
            # The turn goes on: a span still held is not in this response's history content (not
            # shown yet); what it becomes is shown, and kept, with the next response's text.
            st.pending = None
            self._history.append(
                {
                    "role": "assistant",
                    "content": resp.content,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.arguments},
                        }
                        for c in resp.calls
                    ],
                }
            )
            st.open_calls = [c.id for c in resp.calls]
            for call in resp.calls:
                self._add_output(st, await self._tool_message(st, call))

    async def _request(
        self, handle: TurnHandle, st: _TurnState, messages: list[dict[str, Any]], *, sent: int
    ) -> _Response:
        """One chat request. ``sent`` = how many outputs ``messages`` carries: they count as
        sent (``self._sent``) once the server answers with a first event, so a request refused
        before that (HTTP 503, connect error) leaves them in the budget's newest group."""
        st.llm_requests += 1
        st.pending = parts = []
        calls: tuple[ToolCall, ...] = ()
        done: Done | None = None
        held = contextlib.nullcontext() if self._hold is None else self._hold(HOLD_REASON)
        try:
            async with held, aclosing(self._llm.stream(messages, self._tools)) as events:
                async for event in events:
                    self._sent = sent
                    if isinstance(event, ContentDelta):
                        self._show(handle, st, parts, st.cite.feed(event.text))
                    elif isinstance(event, ReasoningDelta):
                        st.reasoning_chars += event.chars
                    elif isinstance(event, ToolCalls):
                        calls = event.calls
                    elif isinstance(event, Usage):
                        st.usage.append(event)
                    else:
                        done = event
        except BaseException:
            # A held span never closed and the response did not finish: not shown.
            st.cite.discard()
            raise
        # A span still held stays in the turn's filter: the next response of the turn may close
        # it (and it is judged then); only the turn's last response flushes it (``_final``).
        if done is None:  # the client always ends with Done or raises
            raise LLMError(f"malformed_stream no_done_event (model={self._llm.model_id})")
        return _Response("".join(parts), calls, done)

    def _show(self, handle: TurnHandle, st: _TurnState, parts: list[str], text: str) -> None:
        """Show filtered answer text: the response's content, the turn text, the handle."""
        if not text:
            return
        parts.append(text)
        st.text.append(text)
        if st.t_first_visible is None and text.strip():
            st.t_first_visible = self._clock()
        handle._push(text)

    def _judge(self, returned: set[str], ref: str) -> Ruling:
        """Rule on a cited reference (``tern.agent.citations.resolve``) against the index as
        it is now and the ids this turn's tool results returned."""
        norm = normalize_ref(ref)
        if norm is None:  # the input grammar only passes (x-)?<8..64 hex>
            raise ValueError("the citation grammar passed a reference normalize_ref refuses")
        return resolve(norm, returned, self._indexed(norm, limit=2))

    def _indexed(self, ref: str, *, limit: int | None) -> list[tuple[str, bool]]:
        """``(mid, conflicted)`` of the indexed messages whose mid starts with the normalized
        reference ``ref``, in mid order, at most ``limit`` (a range scan of the primary key)."""
        low, high = prefix_range(ref)
        sql = "SELECT mid, conflicted FROM messages WHERE mid >= ? AND mid < ? ORDER BY mid"
        params: tuple[object, ...] = (low, high)
        if limit is not None:
            sql += " LIMIT ?"
            params = (low, high, limit)
        return [(str(r[0]), bool(r[1])) for r in self.index.connection.execute(sql, params)]

    def _prefix_mids(self, ref: str) -> list[str]:
        """Every indexed mid a tool-shown short id is a prefix of (provenance resolver)."""
        return [mid for mid, _ in self._indexed(ref, limit=None)]

    async def _tool_message(self, st: _TurnState, call: ToolCall) -> ToolOutput:
        try:
            args: object = json.loads(call.arguments)
        except (ValueError, RecursionError):
            args = None
        if not isinstance(args, dict):
            st.unparseable_args += 1
            log.warning(
                "agent tool arguments are not a JSON object",
                extra={"tool": call.name if call.name in self._registry else "<unknown>"},
            )
            text = _error_json(
                "invalid_params",
                "unparseable_arguments",
                "the tool arguments are not a JSON object",
            )
            return ToolOutput(call.id, text, untrusted=False)
        st.tool_calls += 1
        try:
            result = await self._registry.call(
                self.caller,
                call.name,
                cast("dict[str, Any]", args),
                provenance=self.provenance(),
                ask=st.ask,  # None (bypass, chat.send): nothing is asked
            )
        except ToolError as exc:
            text = _error_json(exc.code, exc.reason, exc.message)
            reg = self._registry.get(call.name)
            if reg is not None and reg.origin != "builtin" and reg.spec.untrusted_output:
                # A plugin's message could quote mail despite the contract: wrapped (P34).
                return ToolOutput(call.id, text, untrusted=True, tool=call.name)
            return ToolOutput(call.id, text, untrusted=False)
        try:
            text = result.output.model_dump_json(by_alias=True)
        except PydanticSerializationError:
            # Tool-authored serializer after the call ran (as tern.mcp.local): an explicit error
            # for the model, never a partial output; the message may quote content, so dropped.
            log.warning("agent tool output not serializable", extra={"call_id": result.call_id})
            text = _error_json("internal", "serialize", "cannot serialize the output")
            return ToolOutput(call.id, text, untrusted=False)
        value: object = json.loads(text)
        st.returned.update(output_refs(value)[0])
        if result.untrusted_output:
            # Tracked as it enters the history (the caller adds it at once, no await between).
            self._provenance.add(value, tool=call.name)
            return ToolOutput(call.id, text, untrusted=True, tool=call.name)
        return ToolOutput(call.id, text, untrusted=False)

    # -- history --------------------------------------------------------------------------------

    def _drop_tool_exchanges(self) -> int:
        """A new turn starts: every earlier turn becomes its user message and one assistant
        message holding the answer text the user saw (its responses' contents, joined); the
        assistant tool calls and the tool outputs go. Returns how many outputs were dropped."""
        kept: list[dict[str, Any] | ToolOutput] = [self._history[0]]
        dropped = 0
        answer: list[str] | None = None
        for m in self._history[1:]:
            if isinstance(m, ToolOutput):
                dropped += 1
            elif m["role"] == "user":
                if answer is not None:
                    kept.append({"role": "assistant", "content": "".join(answer)})
                kept.append(m)
                answer = []
            else:
                assert answer is not None  # an assistant message follows a user message
                answer.append(cast("str", m.get("content") or ""))
        if answer is not None:
            kept.append({"role": "assistant", "content": "".join(answer)})
        self._history = kept
        self._outputs = []
        self._sent = 0
        return dropped

    def _add_output(self, st: _TurnState, out: ToolOutput) -> None:
        self._history.append(out)
        self._outputs.append(out)
        st.open_calls.remove(out.call_id)

    def _final(self, handle: TurnHandle, st: _TurnState) -> None:
        """The turn's last response ended normally: a span still held never closed, so it is
        not a citation and is shown unchanged (P28); then the answer goes into the history."""
        assert st.pending is not None  # set by _request
        self._show(handle, st, st.pending, st.cite.flush())
        self._answer(st)

    def _answer(self, st: _TurnState) -> None:
        """Put the streamed response's answer text into the history (its calls never ran)."""
        if st.pending is not None:
            self._history.append({"role": "assistant", "content": "".join(st.pending)})
            st.pending = None

    def _close_history(self, st: _TurnState) -> None:
        """Leave the history well-formed after the turn, however it ended."""
        # A span still held here means the turn ended abnormally (a normal end flushed it in
        # ``_final``): it is never shown (P28).
        st.cite.discard()
        for call_id in list(st.open_calls):
            text = _error_json("not_run", "turn_ended", "the turn ended before this call ran")
            self._add_output(st, ToolOutput(call_id, text, untrusted=False))
        self._answer(st)
        last = self._history[-1]
        if isinstance(last, ToolOutput) or last.get("role") != "assistant" or "tool_calls" in last:
            self._history.append({"role": "assistant", "content": ""})

    def _turn(self, turn_id: str, st: _TurnState, outcome: Outcome) -> AgentTurn:
        meta = TurnMeta(
            tool_calls=st.tool_calls,
            unparseable_args=st.unparseable_args,
            dropped_outputs=st.dropped,
            truncated_outputs=st.truncated,
            llm_requests=st.llm_requests,
            reasoning_chars=st.reasoning_chars,
            usage=tuple(st.usage),
            raw_citations=st.cite.raw,
            invalid_citations=st.cite.invalid,
            conflicted_citations=st.cite.conflicted,
            t_start=st.t_start,
            t_first_visible=st.t_first_visible,
            t_done=self._clock(),
        )
        return AgentTurn(
            turn_id=turn_id,
            text="".join(st.text),
            citations=st.cite.citations,
            outcome=outcome,
            meta=meta,
        )
