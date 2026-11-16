"""Short message ids at the model boundary (plan 08c, decisions #38).

A ``mid`` is 64 lower-case hex characters (sha256 of the normalized Message-ID), or ``x-`` plus 64
for mail without a Message-ID (#5). Models copy such strings badly: one wrong character made
``get_message`` fail (``unknown_mid``, the model searched again) or a citation invalid
(``[citation removed]``). Tools therefore *show* a short id, the first :data:`SHORT_HEX` hex
characters (``x-`` kept), and *accept* a short id or a full mid. Persistent references (citations
as stored, audit rows, undo tokens) stay full mids: only the text the model reads and writes is
short.

This module is pure: :func:`short_id`, :func:`normalize_ref` and :func:`prefix_range`. Resolving
a reference against the index (exactly one mid, else an error naming the problem, never a guess)
is the caller's job (:mod:`tern.tools.builtin.read`, :mod:`tern.agent.citations`).
"""

from __future__ import annotations

import re

#: Hex characters of a short id (48 bits: even odds of one clash need about 16 M messages, and
#: the resolvers refuse a clash instead of guessing).
SHORT_HEX = 12

#: Fewest hex characters a reference may have (a chip shows 8; anything shorter is refused).
MIN_REF_HEX = 8

_FULL_MID = re.compile(r"(x-)?[0-9a-f]{64}")
_REF = re.compile(r"(x-)?([0-9a-f]+)")
# What a model may wrap around an id it copies: brackets, quotes, an ellipsis, whitespace.
_STRIP = " \t\r\n[]【】<>()\"'`…."


def short_id(mid: str) -> str:
    """The short form of a full ``mid``: ``mid[:12]``, or ``"x-" + mid[2:14]``."""
    if not is_full_mid(mid):
        raise ValueError("not a full mid")
    if mid.startswith("x-"):
        return "x-" + mid[2 : 2 + SHORT_HEX]
    return mid[:SHORT_HEX]


def is_full_mid(value: str) -> bool:
    """True for exactly 64 lower-case hex characters, optionally prefixed ``x-``."""
    return _FULL_MID.fullmatch(value) is not None


def normalize_ref(value: str) -> str | None:
    """A model-written message reference, cleaned: surrounding brackets, quotes and ellipses
    removed, lower-cased. None unless what is left is ``(x-)?<hex>`` with at least
    :data:`MIN_REF_HEX` and at most 64 hex characters."""
    cleaned = value.strip(_STRIP).lower()
    m = _REF.fullmatch(cleaned)
    if m is None:
        return None
    hex_part = m.group(2)
    if not MIN_REF_HEX <= len(hex_part) <= 64:
        return None
    return cleaned


def prefix_range(ref: str) -> tuple[str, str]:
    """``(low, high)`` such that a mid starts with ``ref`` iff ``low <= mid < high`` (for an
    index range scan). ``ref`` must be a normalized reference."""
    if normalize_ref(ref) != ref:
        raise ValueError("not a normalized reference")
    last = ref[-1]
    # hex digits and the bound: '9' → ':' (next ASCII), 'f' → 'g'; both sort above every hex digit
    return ref, ref[:-1] + chr(ord(last) + 1)
