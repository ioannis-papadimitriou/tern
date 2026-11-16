"""Citation validation (plan 08a step 4, plan 08c step 3; architecture §2, §9a; #15 amendment,
A8, A11, #38).

**Two grammars, one state machine.** The *output* grammar is what ternd emits and the chat tab
renders as a chip: ``[mid]`` or ``【mid】`` as a *matched pair* (``[mid】`` and ``【mid]`` are not
citations), with optional whitespace inside the brackets and an optional ``mid:`` prefix
(followed by optional whitespace), as S3's scorer v2; ``mid`` is the bridge ``Mid`` pattern
``(x-)?[0-9a-f]{64}`` (``x-`` mids are 66 characters, A8). :func:`find_citations` parses it (and
``extension/src/ui/chat/citations.ts`` accepts exactly the same, checked over one fixture). The
*input* grammar is what the model may write (:class:`CitationFilter`): the same, except that the
reference is ``(x-)?`` plus :data:`~tern.tools.ids.MIN_REF_HEX` to 64 hex digits, either case,
so the short id a tool showed (``[a1b2c3d4e5f6]``, plan 08c), any longer prefix and the full mid
are all citations. Whitespace is Unicode whitespace (``str.isspace``, the same set as ``\\s`` in
a ``str`` regex), at most :data:`MAX_WS` characters in each of the three places (after the
opener, after ``mid:``, before the closer), so a held span is bounded: an opener followed by a
longer run is not a citation and is released at once. A bare reference is not a citation.

**Valid** (decided by the caller's :data:`Judge`; :func:`resolve` is the rule, the loop looks the
index up): the normalized reference (``tern.tools.ids.normalize_ref``) is a prefix of exactly
one indexed mid, that mid is not ``conflicted``, and an id under a ``mid`` key of a tool result
returned this turn (a short id or a full mid) is a prefix of it, all such ids naming that same
mid. Anything else is invalid (no indexed match, an ambiguous prefix, not returned this turn); a
conflicted mid is always invalid, even when a tool returned it (T14).

**Streaming filter** (:class:`CitationFilter`): answer text is fed delta by delta; text that
cannot be part of a citation is released at once, while a span that starts with ``[`` or ``【``
is held back until it closes (and is judged) or can no longer become a citation (and is
released unchanged). A valid citation is released as the model wrote it with the reference
replaced by the full mid it resolved to (brackets, prefix and whitespace kept), so what leaves
ternd is always the output grammar and a chip names a full mid (#5: persistent references are
full mids); an invalid one is replaced by :data:`REMOVED_MARKER`, which is not a citation itself
("stripped and flagged", §2, P2). So no invalid citation reaches the user, however the answer is
split into deltas. :meth:`CitationFilter.flush` ends the turn's answer (the caller calls it once,
after the turn's last response, not at a tool-call boundary, so a span may continue into the
next response): a span still held there never closed, so it is not a citation and is released
unchanged.

A bracketed run of 8 or more hex digits that is not a message reference (``[20261116]``) is
judged like any other and, matching nothing, is shown as the removed marker: the price of
accepting short ids, which the model is told to use only as citations.

**Counts** are taken per occurrence, before stripping: ``raw`` (every citation the model wrote),
``invalid`` (those replaced, conflicted ones included) and ``conflicted`` (those whose mid the
index marks conflicted), so plan 09 can compute ``raw_invalid_citation_rate`` (M20, §9a).
:attr:`CitationFilter.citations` lists the surviving full mids in order of first appearance
(``chat.done``).

This module depends on the stdlib and the pure codec :mod:`tern.tools.ids` only.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Literal

from tern.tools.ids import MIN_REF_HEX, is_full_mid, normalize_ref

#: What an invalid citation is replaced with (P2). Not a citation itself.
REMOVED_MARKER = "[citation removed]"

#: What replaces a citation of a ``conflicted`` mid (plan 08c): several different messages share
#: its Message-ID, a likely spoof (#5, T14). Never a citation itself (no hex id inside); the user
#: sees why instead of a bare removal.
CONFLICTED_MARKER = "[⚠ conflicting copies: check in Thunderbird]"

#: Opening bracket → its matching closing bracket.
PAIRS: dict[str, str] = {"[": "]", "【": "】"}

#: The optional prefix inside the brackets (S3 scorer v2).
_PREFIX = "mid:"
_HEX = frozenset("0123456789abcdef")
_HEX_ANY_CASE = frozenset("0123456789abcdefABCDEF")
_HEX_LEN = 64
#: The most whitespace characters accepted in each place inside a citation (module docstring).
MAX_WS = 8

#: The judge's verdict on one cited reference.
Verdict = Literal["valid", "invalid", "conflicted"]

#: A verdict and, for ``valid``, the full mid the reference resolved to (else None).
Ruling = tuple[Verdict, str | None]

#: Judges one cited reference (``(x-)?`` plus 8–64 hex digits, as the model wrote it) at the
#: moment its span closes.
Judge = Callable[[str], Ruling]


@dataclass(frozen=True, slots=True)
class _Grammar:
    """The hex digits a reference may have: how few, and whether upper case is accepted."""

    min_hex: int
    hex_chars: frozenset[str]


#: What ternd emits and the chat tab renders (full mids only).
OUTPUT = _Grammar(min_hex=_HEX_LEN, hex_chars=_HEX)
#: What the model may write (short ids, prefixes, full mids; either case).
INPUT = _Grammar(min_hex=MIN_REF_HEX, hex_chars=_HEX_ANY_CASE)


@dataclass
class _Span:
    """A held span: the text so far (opener first), the state, the reference characters so far
    (``ref_at``: where they start in ``text``)."""

    text: list[str]
    close: str
    # "ws1": after the opener; "m1".."m3": inside "mid:"; "ws2": after "mid:"; "x1": after "x";
    # "hex": inside the hex digits; "ws3": after the hex digits.
    state: str = "ws1"
    mid: list[str] = field(default_factory=list[str])
    ref_at: int = 0
    hex_count: int = 0
    #: Whitespace characters in the current whitespace place ("ws1", "ws2" or "ws3").
    ws: int = 0


def _space(span: _Span) -> Literal["more", "dead"]:
    """One more whitespace character in the current place: dead past :data:`MAX_WS`."""
    span.ws += 1
    return "more" if span.ws <= MAX_WS else "dead"


def _step(span: _Span, ch: str, g: _Grammar) -> Literal["more", "full", "dead"]:
    """Advance ``span`` by ``ch`` (already appended to its text) under grammar ``g``."""
    st = span.state
    if st in ("ws1", "ws2"):
        if ch.isspace():
            return _space(span)
        if st == "ws1" and ch == _PREFIX[0]:
            span.state = "m1"
            return "more"
        if ch == "x":
            span.ref_at = len(span.text) - 1
            span.mid.append(ch)
            span.state = "x1"
            return "more"
        if ch in g.hex_chars:
            span.ref_at = len(span.text) - 1
            span.mid.append(ch)
            span.hex_count = 1
            span.state = "hex"
            return "more"
        return "dead"
    if st in ("m1", "m2", "m3"):
        k = int(st[1])
        if ch != _PREFIX[k]:
            return "dead"
        span.state = "ws2" if k == len(_PREFIX) - 1 else f"m{k + 1}"
        span.ws = 0
        return "more"
    if st == "x1":
        if ch != "-":
            return "dead"
        span.mid.append(ch)
        span.state = "x2"
        return "more"
    if st == "x2":
        if ch not in g.hex_chars:
            return "dead"
        span.mid.append(ch)
        span.hex_count = 1
        span.state = "hex"
        return "more"
    if st == "hex":
        if ch in g.hex_chars:
            if span.hex_count == _HEX_LEN:
                return "dead"
            span.mid.append(ch)
            span.hex_count += 1
            return "more"
        if span.hex_count < g.min_hex:
            return "dead"
        if ch.isspace():
            span.state = "ws3"
            span.ws = 0
            return _space(span)
        return "full" if ch == span.close else "dead"
    # "ws3"
    if ch.isspace():
        return _space(span)
    return "full" if ch == span.close else "dead"


def resolve(ref: str, returned: Collection[str], indexed: Sequence[tuple[str, bool]]) -> Ruling:
    """The rule for one cited reference (module docstring, **Valid**). ``ref`` is a normalized
    reference; ``returned`` the ids under ``mid`` keys of this turn's tool results (short ids or
    full mids); ``indexed`` the ``(mid, conflicted)`` rows of the index whose mid starts with
    ``ref`` (at least two of them when there are two or more)."""
    if normalize_ref(ref) != ref:
        raise ValueError("not a normalized reference")
    if len(indexed) != 1:
        return ("invalid", None)  # nothing indexed, or an ambiguous prefix: never a guess
    mid, conflicted = indexed[0]
    if not is_full_mid(mid) or not mid.startswith(ref):
        raise ValueError("an indexed row that is not a full mid starting with the reference")
    if conflicted:
        return ("conflicted", None)
    matching = [r for r in returned if r.startswith(ref) or ref.startswith(r)]
    if not matching or not all(mid.startswith(r) for r in matching):
        return ("invalid", None)  # not returned this turn, or it names another message too
    return ("valid", mid)


class CitationFilter:
    """The streaming citation filter (module docstring). One per turn; :meth:`flush` once, at
    the end of the turn's last response. ``grammar`` is :data:`INPUT` (the default) or
    :data:`OUTPUT` (:func:`find_citations`)."""

    def __init__(self, judge: Judge, *, grammar: _Grammar = INPUT) -> None:
        self._judge = judge
        self._grammar = grammar
        self._span: _Span | None = None
        self.raw = 0
        self.invalid = 0
        self.conflicted = 0
        self._citations: dict[str, None] = {}

    @property
    def citations(self) -> tuple[str, ...]:
        """The full mids of the citations that survived, distinct, in order of first
        appearance."""
        return tuple(self._citations)

    @property
    def holding(self) -> bool:
        """Whether text is held back (a span that may still become a citation)."""
        return self._span is not None

    def feed(self, text: str) -> str:
        """Feed answer text; returns the text that may be shown now."""
        out: list[str] = []
        for ch in text:
            self._feed_char(ch, out)
        return "".join(out)

    def flush(self) -> str:
        """End of the turn's answer: a held span never closed, so it is released unchanged."""
        if self._span is None:
            return ""
        text = "".join(self._span.text)
        self._span = None
        return text

    def discard(self) -> None:
        """Drop a held span without showing it (the turn ended abnormally)."""
        self._span = None

    def _feed_char(self, ch: str, out: list[str]) -> None:
        span = self._span
        if span is None:
            close = PAIRS.get(ch)
            if close is None:
                out.append(ch)
            else:
                self._span = _Span(text=[ch], close=close)
            return
        span.text.append(ch)
        result = _step(span, ch, self._grammar)
        if result == "more":
            return
        self._span = None
        if result == "full":
            out.append(self._judged(span))
            return
        # Dead: the held text minus its last character holds no opener (only whitespace,
        # "mid:", "x-" and hex digits follow a viable opener), so it is released as is and the
        # last character, which may open a new span, is fed again.
        out.append("".join(span.text[:-1]))
        self._feed_char(ch, out)

    def _judged(self, span: _Span) -> str:
        self.raw += 1
        ref = "".join(span.mid)
        verdict, mid = self._judge(ref)
        if verdict == "valid":
            if mid is None or not is_full_mid(mid):
                raise ValueError("a valid ruling must name a full mid")
            self._citations.setdefault(mid, None)
            text = "".join(span.text)
            return text[: span.ref_at] + mid + text[span.ref_at + len(ref) :]
        self.invalid += 1
        if verdict == "conflicted":
            self.conflicted += 1
            return CONFLICTED_MARKER
        return REMOVED_MARKER


def find_citations(text: str) -> list[str]:
    """The mids of every citation in ``text`` under the output grammar (full mids only), per
    occurrence, in order (the streaming filter's state machine; nothing is judged)."""
    found: list[str] = []

    def judge(mid: str) -> Ruling:
        found.append(mid)
        return ("valid", mid)

    f = CitationFilter(judge, grammar=OUTPUT)
    f.feed(text)
    f.flush()
    return found
