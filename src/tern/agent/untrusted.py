"""Wrapping of untrusted (mail-derived) text before it reaches a model (decisions #18, #34).

``wrap(text, tool=..., mid=...)`` returns one envelope::

    <untrusted_email tool="search_messages">
    ...text...
    </untrusted_email>

(plan 08a P1: one envelope per untrusted tool result, ``tool`` attribute; ``mid`` only when a
caller wraps one message; MCP results carry no attribute.)

Inside the text, every ``<`` that starts something a model could read as an opening or closing
``untrusted_email`` tag becomes ``&lt;``, so mail text can neither close the envelope early nor
open a fake one (#18 amendment). Candidates are found in a folded copy of the text: per
character NFKC (fullwidth ``＜``, small-form ``﹤`` and fullwidth letters fold to ASCII) and
case folding; from the folded output, format characters (``Cf``, e.g. zero-width spaces),
combining and spacing marks (``Mn``, ``Mc``; also the ones case folding creates, as in
``İ`` → ``i`` + U+0307) and the invisible fillers (Hangul fillers, braille blank) are dropped.
After ``<`` and between ``untrusted`` and ``email`` any run of characters that are neither
ASCII letters, digits nor ``>`` (whitespace, ``/`` and its look-alikes ``∕`` ``⁄``, ``\\``,
``_``, ``-``, dots, ...) may stand, JSON escapes (``\\n``, ``\\t``, ``\\u0009``: a tool's
JSON encodes a newline in mail text that way) included; the run after ``<`` excludes ``<``
itself (``<<…`` is matched from its last ``<``, which keeps matching linear). Escaping more
than needed changes data, never structure, so the matcher is generous. Known limits: other
letters that NFKC does not fold to ASCII are not matched, i.e. cross-script homoglyphs (a
Cyrillic ``е`` in ``email``) and precomposed accented letters (``ü``; a combining accent is
dropped, a precomposed one is a different letter). JSON stays valid JSON: ``<`` never occurs
in JSON syntax outside strings, and ``&lt;`` is plain string text.

Attributes are validated, never escaped: ``tool`` must match the tool-name rule and ``mid`` the
bridge ``Mid`` pattern, else ``ValueError`` (no attribute injection is possible).

Leaf module (stdlib only): ``tern.mcp.server`` imports it without pulling in the index, the
audit log, the registry or httpx (#34). The two patterns and the ``<`` forms are restated here
for that reason; tests pin them to ``tern.tools.contract.NAME_RE``, ``tern.bridge.protocol.Mid``
and a scan of every code point.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["LT_FORMS", "MID_RE", "TAG", "TOOL_NAME_RE", "escape", "wrap"]

#: The envelope's tag name (decisions #18; plan 08a A7/P1).
TAG = "untrusted_email"

#: ``tern.tools.contract.NAME_RE``'s pattern (restated: leaf module).
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
#: ``tern.bridge.protocol.Mid``'s pattern (restated: leaf module).
MID_RE = re.compile(r"^(x-)?[0-9a-f]{64}$")

#: Every code point whose NFKC + casefold form contains ``<``: ASCII, small form U+FE64 and
#: fullwidth U+FF1C (each folds to exactly ``<``; a test scans all code points).
LT_FORMS = frozenset({"<", "﹤", "＜"})

#: A JSON escape whose letters would otherwise end a separator run (``\\n``, ``\\u0009``).
#: Only letter suffixes: a lone ``\\`` is already a separator, so each position has one path
#: and matching stays linear (no backtracking blow-up on long separator runs).
_JSON_ESC = r"\\(?:u[0-9a-f]{4}|[bfnrt])"

#: An opening or closing tag start in the folded text.
_TAG_START = re.compile(rf"<(?:[^a-z0-9<>]|{_JSON_ESC})*untrusted(?:[^a-z0-9>]|{_JSON_ESC})*email")

#: Categories dropped from the folded output: format characters, combining and spacing marks.
_DROPPED_CATEGORIES = frozenset({"Cf", "Mn", "Mc"})
#: Invisible letters/symbols outside those categories: Hangul choseong/jungseong fillers,
#: Hangul filler and its halfwidth form (both NFKC to U+1160), braille pattern blank.
_FILLERS = frozenset({"\u115f", "\u1160", "\u3164", "\uffa0", "\u2800"})


def _dropped(ch: str) -> bool:
    return ch in _FILLERS or unicodedata.category(ch) in _DROPPED_CATEGORIES


def _folded(text: str) -> tuple[str, list[int]]:
    """The folded copy of ``text`` and, per folded character, the index of the original
    character it came from."""
    out: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        if ch.isascii():
            folded = ch.lower()
        else:  # drop after folding: casefold and NFKC can create marks (İ -> i + U+0307)
            folded = "".join(
                c for c in unicodedata.normalize("NFKC", ch).casefold() if not _dropped(c)
            )
        out.append(folded)
        origin.extend([i] * len(folded))
    return "".join(out), origin


def escape(text: str) -> str:
    """``text`` with every ``<`` (or a form folding to ``<``) that starts an opening or closing
    ``untrusted_email`` tag replaced by ``&lt;``. Nothing else changes."""
    if not isinstance(text, str):
        raise TypeError("untrusted text must be a str")
    folded, origin = _folded(text)
    starts = [origin[m.start()] for m in _TAG_START.finditer(folded)]
    if not starts:
        return text
    parts: list[str] = []
    last = 0
    for i in starts:
        if text[i] not in LT_FORMS:  # only these fold to "<"; LT_FORMS is pinned by a test
            raise RuntimeError("untrusted.escape(): a tag start maps to no '<' form")
        parts.append(text[last:i])
        parts.append("&lt;")
        last = i + 1
    parts.append(text[last:])
    return "".join(parts)


def wrap(text: str, *, tool: str | None = None, mid: str | None = None) -> str:
    """``text`` inside one ``<untrusted_email …>`` envelope, with tag-like sequences inside
    escaped (:func:`escape`). ``tool``: the tool whose result this is (tool-name rule); ``mid``:
    the one message this text comes from (``Mid`` pattern). A bad attribute raises
    ``ValueError`` naming the attribute only, never its value."""
    attrs = ""
    if tool is not None:
        if not isinstance(tool, str) or not TOOL_NAME_RE.fullmatch(tool):
            raise ValueError("wrap(): tool does not match the tool-name rule")
        attrs += f' tool="{tool}"'
    if mid is not None:
        if not isinstance(mid, str) or not MID_RE.fullmatch(mid):
            raise ValueError("wrap(): mid does not match the Mid pattern")
        attrs += f' mid="{mid}"'
    return f"<{TAG}{attrs}>\n{escape(text)}\n</{TAG}>"
