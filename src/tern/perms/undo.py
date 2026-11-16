"""Undo with a state check (M6, architecture §6, plan 06b step 8).

Undo is not a tool: it is reached only through ``Registry.undo(caller, token)``, so it is never in
``tools describe``, MCP or an agent's tool list. Only ``caller == "ui"`` may undo (the CLI and the
extension's audit view, through ternd's ``audit.undo`` handler). The registry writes the undo's
own write-ahead ``call`` row (tool :data:`UNDO_TOOL`, tier :func:`inverse_tier`,
``decision=auto``, ``initial_status=tb_accepted``); this module holds the pure parts and the live
state check:

- :func:`check_payload`: a payload target with ``prev`` or ``post`` null (a created draft, plan
  10) is refused (``state_changed``, ``unsupported_target``) until plan 10 defines its inverse.
- :func:`locate`: every target is found again **live** (``mail.messages.find``: the index lags
  and excludes Trash). Each payload target needs its own live location (a distinct ``tb_id``) in
  its post folder whose state (tag keys, flagged, read) equals the recorded ``post``; otherwise
  the whole undo is refused (``state_changed``) and nothing is written. The Message-ID ``find``
  needs comes from the index (the payload holds mids only): a mid the index no longer holds is
  refused (``state_changed``, ``not_locatable``); a mid the index marks ``conflicted`` (a copy
  with different content under its Message-ID arrived, #5) is refused ``mid_conflicted`` before
  any find, as every write tool refuses it.
- :func:`inverse_requests`: per target, ``mail.update`` back to the prev tags/flagged/read (only
  what differs), then, for a target whose folder changed (a move, a trash), ``mail.move`` back to
  its prev folder. The requests name the live ``tb_id``\\s :func:`locate` found (in memory only).
- :func:`inverse_payload`: the undo call's own undo payload (prev/post swapped), so a second undo
  on the undo's token re-applies the original write ("redo") and write status watches the undo
  like any write.

Every refusal is a :class:`~tern.perms.targets.TargetRefused` (code, detail reason, a message
naming positions and counts only). ``tb_id``\\s are never in a refusal, a payload, a ``repr`` or a
log record (#5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from tern.bridge.client import ExtensionRequester
from tern.bridge.protocol import MoveParams, UpdateParams
from tern.index.mid import X_PREFIX
from tern.index.store import Store
from tern.perms.audit import TargetState, UndoPayload, UndoTarget
from tern.perms.targets import TargetRefused, find_locations, resolved_location
from tern.tools.contract import Tier

#: The tool name of an undo's audit row (undo is not a registered tool).
UNDO_TOOL = "undo"


@dataclass(frozen=True, slots=True)
class LocatedTarget:
    """One payload target and the live location it was found at, in its post state."""

    target: UndoTarget
    tb_id: int = field(repr=False)


def check_payload(payload: UndoPayload) -> None:
    """Refuse a payload with a target the undo cannot invert (module docstring)."""
    n = len(payload.targets)
    for i, t in enumerate(payload.targets, start=1):
        if t.prev is None or t.post is None:
            raise TargetRefused(
                "state_changed",
                "unsupported_target",
                f"target {i} of {n} was created or removed by the write; it cannot be undone yet",
            )


def _states(t: UndoTarget) -> tuple[TargetState, TargetState]:
    assert t.prev is not None and t.post is not None  # check_payload ran
    return t.prev, t.post


def is_move(t: UndoTarget) -> bool:
    """Whether undoing ``t`` moves it back to another folder."""
    prev, post = _states(t)
    return prev.folder_id != post.folder_id


def inverse_tier(payload: UndoPayload) -> Tier:
    """W2 if any inverse is a move, else W1."""
    return Tier.W2 if any(is_move(t) for t in payload.targets) else Tier.W1


def target_mids(payload: UndoPayload) -> list[str]:
    """The payload's distinct mids, first occurrence order (the undo row's targets)."""
    return list(dict.fromkeys(t.mid for t in payload.targets))


def inverse_payload(payload: UndoPayload) -> UndoPayload:
    """The undo call's undo payload: every target with prev and post swapped."""
    return UndoPayload(
        targets=[UndoTarget(mid=t.mid, prev=t.post, post=t.prev) for t in payload.targets]
    )


async def locate(
    index: Store, bridge: ExtensionRequester | None, payload: UndoPayload
) -> list[LocatedTarget]:
    """Every payload target's live location in its recorded post state (module docstring), in
    payload order. Raises :class:`TargetRefused` (``state_changed``, ``mid_conflicted``;
    ``unavailable``/``internal`` for a bridge failure) before anything is written."""
    targets = payload.targets
    n = len(targets)
    located: dict[int, LocatedTarget] = {}
    for mid in target_mids(payload):
        msg = None if mid.startswith(X_PREFIX) else index.get_message(mid)
        mine = [(i, t) for i, t in enumerate(targets) if t.mid == mid]
        if msg is None or not msg.header_message_id:
            raise TargetRefused(
                "state_changed",
                "not_locatable",
                f"target {mine[0][0] + 1} of {n} is no longer in the index, so Thunderbird "
                "cannot locate it",
            )
        if msg.conflicted:
            # #5/T14: every write refuses a conflicted mid; a spoof that reused the Message-ID
            # after the original write could otherwise be the copy the inverse acts on.
            raise TargetRefused(
                "mid_conflicted",
                "mid_conflicted",
                f"target {mine[0][0] + 1} of {n} has copies with different content under one "
                "Message-ID",
            )
        found = [resolved_location(f) for f in await find_locations(bridge, msg.header_message_id)]
        used: set[int] = set()
        for i, t in mine:
            _, post = _states(t)
            loc = next((f for f in found if f.tb_id not in used and f.state == post), None)
            if loc is None:
                raise TargetRefused(
                    "state_changed",
                    "state_changed",
                    f"target {i + 1} of {n} is no longer in the state the write left it",
                )
            used.add(loc.tb_id)
            located[i] = LocatedTarget(t, loc.tb_id)
    return [located[i] for i in range(n)]


def inverse_requests(located: Sequence[LocatedTarget]) -> list[tuple[str, BaseModel]]:
    """The bridge requests that put every located target back in its prev state (module
    docstring): an update before the move, so it names the tb_id :func:`locate` found."""
    out: list[tuple[str, BaseModel]] = []
    for lt in located:
        prev, post = _states(lt.target)
        add = sorted(set(prev.tags) - set(post.tags))
        remove = sorted(set(post.tags) - set(prev.tags))
        flagged = prev.flagged if prev.flagged != post.flagged else None
        read = prev.read if prev.read != post.read else None
        if add or remove or flagged is not None or read is not None:
            out.append(
                (
                    "mail.update",
                    UpdateParams(
                        tb_ids=[lt.tb_id],
                        add_tags=add or None,
                        remove_tags=remove or None,
                        flagged=flagged,
                        read=read,
                    ),
                )
            )
        if prev.folder_id != post.folder_id:
            out.append(("mail.move", MoveParams(tb_ids=[lt.tb_id], dest_folder_id=prev.folder_id)))
    return out


class UndoClaims:
    """Undo tokens being undone right now (in memory). A second undo of a token while the first
    runs is refused, so two concurrent undos cannot both pass the state check and write. The
    check and the claim are one synchronous step."""

    def __init__(self) -> None:
        self._held: set[str] = set()

    def claim(self, token: str) -> bool:
        """True (and the token held) unless it is held already."""
        if token in self._held:
            return False
        self._held.add(token)
        return True

    def release(self, token: str) -> None:
        self._held.discard(token)

    def held(self) -> frozenset[str]:
        return frozenset(self._held)
