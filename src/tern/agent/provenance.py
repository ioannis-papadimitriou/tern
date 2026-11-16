"""Whole-session provenance (plan 08a step 3, M4, A10).

:class:`SessionProvenance` tracks every mid whose untrusted text entered one chat session's
context, over all of its turns (not only the current one): the values under a ``mid`` key, at
any depth, of each ``untrusted_output`` tool result added to the session. Distinct mids are kept
in order of first appearance.

The agent loop passes :meth:`SessionProvenance.mids` to ``Registry.call(..., provenance=...)`` on
every call; the registry uses it only for W2/W3 approval cards (#33, 06b), so passing it always
needs no tier check in the loop.

Only untrusted results count: a trusted result (``list_folders``) holds no mail text, and an
error result holds none either. A result is tracked once it is added to the session's history
(the next request sends it), so a result returned in the same response as a later call is on
that call's card too (conservative: the card may list a mail the model had not yet seen, never
miss one it had). A result elided later by the context budget stays tracked: its text was in
the context.

Known gap (plan Risks): ``find_contacts`` returns derived addresses with no mids, so it adds
nothing.

Short ids (plan 08c, #38): tools show a short id (``tern.tools.ids.short_id``) under ``mid``
keys. A card needs full mids, so a session built with a ``resolve`` function maps each short id
to every indexed mid it is a prefix of (all of them, conservatively: a clash may put a mail on
the card that the model did not see, never leave off one it did). :func:`output_refs` collects
both forms; :func:`output_mids` only full mids.

A value under a ``mid`` key that is neither a valid mid (the bridge ``Mid`` pattern) nor, with a
resolver, a short id naming an indexed message cannot name a message on a card (the registry
refuses such provenance), so it is dropped, with a WARNING that carries the tool name and a count
only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, cast

from pydantic import ConfigDict, TypeAdapter, ValidationError

from tern.bridge.protocol import Mid
from tern.tools.ids import is_full_mid, normalize_ref

log = logging.getLogger(__name__)

#: The key whose values are message references in a tool's output (06a output models).
MID_KEY = "mid"

#: The same mid type the registry validates provenance against (one definition, no drift).
_MID: TypeAdapter[str] = TypeAdapter(Mid, config=ConfigDict(hide_input_in_errors=True))


def _valid_mid(value: str) -> bool:
    try:
        _MID.validate_python(value, strict=True)
    except ValidationError:
        return False
    return True


def _is_ref(value: str) -> bool:
    """A full mid, or a reference as tools write it (``normalize_ref`` leaves it unchanged)."""
    return _valid_mid(value) or normalize_ref(value) == value


def _walk(output: object, accept: Callable[[str], bool]) -> tuple[list[str], int]:
    found: list[str] = []
    invalid = 0
    # (key the value sits under, or None for a list item / the root; the value), popped in
    # document order: a dict's items and a list's elements are pushed in reverse.
    stack: list[tuple[str | None, object]] = [(None, output)]
    while stack:
        key, value = stack.pop()
        if key == MID_KEY:
            if isinstance(value, str) and accept(value):
                found.append(value)
                continue
            invalid += 1
        if isinstance(value, dict):
            items = list(cast("dict[str, Any]", value).items())
            stack.extend(reversed(items))
        elif isinstance(value, list):
            stack.extend((None, item) for item in reversed(cast("list[object]", value)))
    return found, invalid


def output_mids(output: object) -> tuple[list[str], int]:
    """The valid mids under a ``mid`` key anywhere in the JSON value ``output`` (dicts and
    lists), in document order (each ``mid`` at its position among its dict's items, so a dict's
    own ``mid`` written after a nested one comes after it), and how many ``mid`` values were not
    valid mids."""
    return _walk(output, _valid_mid)


def output_refs(output: object) -> tuple[list[str], int]:
    """As :func:`output_mids`, but a short id (any normalized reference: 8–64 lower-case hex
    digits with an optional ``x-``) counts too: the ids a tool showed the model."""
    return _walk(output, _is_ref)


#: Every indexed mid a normalized reference is a prefix of.
Resolve = Callable[[str], Sequence[str]]


class SessionProvenance:
    """The mids whose untrusted text entered one session (module docstring)."""

    def __init__(self, resolve: Resolve | None = None) -> None:
        self._mids: dict[str, None] = {}
        self._resolve = resolve

    def add(self, output: object, *, tool: str) -> int:
        """Track the mids of the untrusted result ``output`` (its JSON value) of ``tool``;
        returns how many were new to the session. Without a resolver a short id is dropped
        (counted in the WARNING)."""
        if self._resolve is None:
            found, invalid = output_mids(output)
        else:
            refs, invalid = output_refs(output)
            found = []
            for ref in refs:
                if is_full_mid(ref):
                    found.append(ref)
                    continue
                mids = [m for m in self._resolve(ref) if _valid_mid(m) and m.startswith(ref)]
                if not mids:
                    invalid += 1  # names no indexed message: nothing to show on a card
                found.extend(mids)
        if invalid:
            log.warning(
                "untrusted tool output has mid values that are not mids",
                extra={"tool": tool, "count": invalid},
            )
        before = len(self._mids)
        self._mids.update(dict.fromkeys(found))
        return len(self._mids) - before

    def mids(self) -> tuple[str, ...]:
        """The distinct mids tracked so far, in order of first appearance."""
        return tuple(self._mids)

    def __len__(self) -> int:
        return len(self._mids)
