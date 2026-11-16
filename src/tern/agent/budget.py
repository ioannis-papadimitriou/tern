"""The context budget for tool outputs (plan 08a step 2b, P4; per turn and prefix-stable since
plan 08c step 5, #38).

Retrieved text (tool outputs) in one chat request is estimated as UTF-8 bytes / 4, rounded up:
no tokenizer dependency; Greek costs about twice ASCII per character, which errs on the safe
side. The session keeps every tool output of the running turn as a :class:`ToolOutput` (raw JSON
text, rendered into its tool message at request time, wrapped with ``untrusted.wrap()`` when
untrusted).

**Per turn.** The budget (``agent_context_budget_tokens``) covers the tool outputs of one turn
only. When a new user turn starts, the tool exchanges of earlier turns (the assistant's tool
calls and their outputs) are dropped from the request history; the earlier user questions and
the answers the user saw stay (``tern.agent.loop``). A follow-up question may search again;
that is cheaper than carrying old results and losing the prompt cache.

**Prefix-stable.** Within a turn nothing already sent to the model is rewritten, so each request
extends the previous one and the runtime's prompt cache (LM Studio) stays valid. :func:`enforce`
runs before each request over the turn's outputs, oldest first. The outputs from index
``newest_from`` on are the *newest group*: the outputs the model has not been sent yet (in the
loop, every output of the latest response); everything before it went out in an earlier request
and is never changed. If the total is over the budget, only the newest group is cut, to a common
cap (the largest first: an output already under the cap stays whole, so a small result next to a
large one is kept), and :data:`TRUNCATED_MARKER` is appended after the envelope of each cut
output (the cut is made on the raw text, so the closing tag stays). Cuts are persistent and
counted, so the agent turn's metadata reports them: never silent. When the outputs already sent
leave no room, the newest group is cut to empty texts (each still counted, with its marker) and
the request may be over; that, too, is counted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tern.agent.untrusted import wrap

#: The estimate's divisor: UTF-8 bytes per token.
BYTES_PER_TOKEN = 4

#: Appended (on its own line, after the envelope) to an output whose text was cut.
TRUNCATED_MARKER = "[tool output truncated: over the context budget]"


def estimate_tokens(text: str) -> int:
    """``ceil(utf8_bytes(text) / BYTES_PER_TOKEN)``."""
    return -(-len(text.encode("utf-8")) // BYTES_PER_TOKEN)


@dataclass(slots=True)
class ToolOutput:
    """One tool message of the turn. ``text`` is the JSON text the model gets; ``tool`` is the
    registered tool name for an untrusted output (the envelope's ``tool`` attribute), else None.
    ``truncated`` is set by :func:`enforce` only."""

    call_id: str
    text: str
    untrusted: bool
    tool: str | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.untrusted and self.tool is None:
            raise ValueError("an untrusted tool output needs its tool name")

    def content(self) -> str:
        """The tool message content as the model sees it."""
        body = wrap(self.text, tool=self.tool) if self.untrusted else self.text
        return f"{body}\n{TRUNCATED_MARKER}" if self.truncated else body

    def tokens(self) -> int:
        return estimate_tokens(self.content())

    def message(self) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": self.call_id, "content": self.content()}


@dataclass(frozen=True, slots=True)
class BudgetChange:
    """What one :func:`enforce` changed: outputs newly truncated."""

    truncated: int = 0


def _cut(text: str, max_bytes: int) -> str:
    """The longest prefix of ``text`` of at most ``max_bytes`` UTF-8 bytes (never half a
    character)."""
    return text.encode("utf-8")[: max(0, max_bytes)].decode("utf-8", errors="ignore")


def _cap(sizes: Sequence[int], available: int) -> int:
    """The largest ``c`` with ``sum(min(s, c) for s in sizes) <= available`` (water-filling)."""
    if available <= 0:
        return 0
    remaining = available
    ordered = sorted(sizes)
    for k, size in enumerate(ordered):
        share = remaining // (len(ordered) - k)
        if size > share:
            return share
        remaining -= size
    return ordered[-1]


def enforce(
    outputs: Sequence[ToolOutput], budget_tokens: int, *, newest_from: int | None = None
) -> BudgetChange:
    """Fit the turn's ``outputs`` (oldest first) into ``budget_tokens`` by cutting the newest
    group ``outputs[newest_from:]`` only (module docstring); ``None`` means the last output
    alone. ``outputs[:newest_from]`` are never changed."""
    if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens < 1:
        raise ValueError("budget_tokens must be an int >= 1")
    if newest_from is None:
        newest_from = max(0, len(outputs) - 1)
    if (
        isinstance(newest_from, bool)
        or not isinstance(newest_from, int)
        or not 0 <= newest_from <= len(outputs)
    ):
        raise ValueError("newest_from must be an int in [0, len(outputs)]")
    group = list(outputs[newest_from:])
    total = sum(o.tokens() for o in outputs)
    truncated = 0
    # Cut the group to a common cap, measure, and cut again until it fits or nothing is left:
    # escaping inside the envelope can grow a text, and the marker adds to each cut output.
    while total > budget_tokens:
        cuttable = [o for o in group if o.text]
        if not cuttable:
            break
        fixed = total - sum(o.tokens() for o in cuttable)
        cap = _cap([o.tokens() for o in cuttable], budget_tokens - fixed)
        for out in cuttable:
            size = out.tokens()
            if size <= cap:
                continue
            if not out.truncated:
                out.truncated = True
                truncated += 1
            max_bytes = len(out.text.encode("utf-8")) - (size - cap) * BYTES_PER_TOKEN
            out.text = _cut(out.text, max_bytes)
        total = sum(o.tokens() for o in outputs)
    return BudgetChange(truncated=truncated)
