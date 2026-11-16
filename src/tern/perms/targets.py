"""Write-target resolution (#5, #7, architecture §6, plan 06b step 3).

Before a W tool runs, the permission engine resolves the call's target mids
(``spec.target_mids(inp)``) in two stages. Every refusal is a :class:`TargetRefused` carrying a
``ToolErrorCode`` (the audit row's ``reason``), a detail code and a content-free message.

1. **Index** (:func:`resolve_index`, read-only ``Store``): each distinct mid, in order, must be
   - in the index with at least one location, else ``unknown_mid`` (``not_indexed``);
   - a mid with a Message-ID, else ``unknown_mid`` (``no_message_id``): an ``x-`` mid cannot be
     located by ``mail.messages.find``;
   - not conflicted (D1), else ``mid_conflicted``;
   - for a tool with ``mail_move`` or ``mail_delete``: in one folder, or the call names the
     folder in a top-level In field ``source_folder_id`` or ``folder_id`` (architecture §5), else
     ``ambiguous_location``, reported as a location count only (M7). A named folder the mid has
     no location in is ``unknown_mid`` (``not_in_folder``). Other write tools act on every
     location (architecture §5, multi-location semantics).
2. **Live** (:func:`find_live`): ``mail.messages.find`` per target through the engine's own
   bridge. Every (mid, folder) the index resolved must be found live, else ``targets_changed``
   (``not_found``): nothing runs against a location TB no longer has. The live locations in the
   resolved folders (same-folder copies included) become ``ctx.targets`` and the per-location
   ``prev`` state; the mid's live locations elsewhere are remembered by ``tb_id`` so the
   post-state can leave them out.

The execution re-check of an approved W2/W3 call (plan 06b step 6, M6) is first
:func:`recheck_index` (each target still indexed, else ``targets_changed`` (``not_indexed``), and
not conflicted since the request, else ``mid_conflicted``), then :func:`find_live` again over the
approval's targets (the card's snapshot: mid -> folder ids), just before its row: the same rule,
so a card location that is gone (moved, deleted) refuses the whole call with
``targets_changed``, and extra copies elsewhere do not count. What then executes is that fresh
resolution (fresh tb_ids and prev state), never the one the card was built from.

After the tool ran, :func:`post_state` finds every target again and pairs prev and post
locations into undo targets (:func:`pair_states`). For a tool without ``mail_move`` or
``mail_delete`` (an in-place write: tags, flags, read) only the prev locations' tb_ids are kept: a
copy that appeared in the meantime (a new delivery, a copy into any folder) is not the write's
result and is left out. A move/delete tool's new locations can be anywhere (its destination is
tool input), so its post state keeps every location not seen elsewhere during the live find.
A bridge failure in either find is ``unavailable`` (bridge ``unavailable``/``tb_error``) or
``internal`` (any other bridge error).

Undo (plan 06b step 8, :mod:`tern.perms.undo`) finds its targets with the same
:func:`find_locations` / :func:`resolved_location`.

``tb_id``\\s stay in memory for the call (#5): they are never in a refusal, an undo target, a
``repr`` or a log record.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import ValidationError

from tern._model import BoundaryModel
from tern.bridge.client import ExtensionRequester
from tern.bridge.protocol import BridgeError, FindParams, FoundList, FoundLocation
from tern.index.mid import X_PREFIX
from tern.index.store import Store
from tern.perms.audit import TargetState, UndoTarget
from tern.tools.context import ResolvedLocation
from tern.tools.contract import Effect, ToolErrorCode, ToolSpec

#: Top-level In fields that name the folder a move/delete acts on, in precedence order
#: (architecture §5's table: ``source_folder_id`` for move/archive, ``folder_id`` for delete).
FOLDER_FIELDS = ("source_folder_id", "folder_id")

#: Effects whose tools act on one location of a mid, never on every copy.
_SINGLE_LOCATION = frozenset({Effect.MAIL_MOVE, Effect.MAIL_DELETE})


class TargetRefused(Exception):
    """A write target cannot be resolved: nothing runs. ``code`` is the ``ToolErrorCode`` (and
    the audit row's ``reason``), ``reason`` a detail code; ``message`` names positions and
    counts only, never a mid's content, a folder or a ``tb_id``."""

    def __init__(self, code: ToolErrorCode, reason: str, message: str) -> None:
        super().__init__(f"{code} ({reason}): {message}")
        self.code: ToolErrorCode = code
        self.reason = reason
        self.message = message


@dataclass(frozen=True, slots=True)
class IndexTarget:
    """One target mid as the index resolved it: its Message-ID (for ``find``) and the folders
    the write acts in."""

    mid: str
    header_message_id: str = field(repr=False)
    folder_ids: tuple[str, ...]
    #: The tool changes locations in place only (no ``mail_move``/``mail_delete``): its post state
    #: is the prev locations' tb_ids only.
    in_place: bool = False


@dataclass(frozen=True, slots=True)
class LiveTargets:
    """The live resolution of a call's targets (in memory for that call only)."""

    targets: tuple[IndexTarget, ...]
    #: mid → its live locations in the resolved folders: the tool's ``ctx.targets`` and the
    #: undo ``prev`` state.
    locations: Mapping[str, tuple[ResolvedLocation, ...]] = field(repr=False)
    #: mid → tb_ids of its live locations outside the resolved folders (left out of the post
    #: state: the write did not target them).
    untouched: Mapping[str, frozenset[int]] = field(repr=False)


def named_folder(inp: BoundaryModel) -> str | None:
    """The folder a call names in a top-level :data:`FOLDER_FIELDS` field (the first set one),
    or None. A set value that is not a non-empty ``str`` is ``invalid_params``."""
    fields = type(inp).model_fields
    for name in FOLDER_FIELDS:
        if name not in fields:
            continue
        value: object = getattr(inp, name)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise TargetRefused(
                "invalid_params", "folder_field", f"{name} must be a folder id (a non-empty str)"
            )
        return value
    return None


def resolve_index(
    index: Store, spec: ToolSpec, inp: BoundaryModel, mids: Sequence[str]
) -> tuple[IndexTarget, ...]:
    """Each distinct mid of ``mids`` (first occurrence order) resolved from the index (module
    docstring, stage 1). Raises :class:`TargetRefused` at the first target that fails."""
    single = bool(spec.effects & _SINGLE_LOCATION)
    folder = named_folder(inp) if single else None
    distinct = list(dict.fromkeys(mids))
    n = len(distinct)
    out: list[IndexTarget] = []
    for i, mid in enumerate(distinct, start=1):
        where = f"target {i} of {n}"
        msg = index.get_message(mid)
        locs = index.locations(mid) if msg is not None else []
        if msg is None or not locs:
            raise TargetRefused("unknown_mid", "not_indexed", f"{where} is not in the index")
        if mid.startswith(X_PREFIX) or not msg.header_message_id:
            raise TargetRefused(
                "unknown_mid",
                "no_message_id",
                f"{where} has no Message-ID, so Thunderbird cannot locate it",
            )
        if msg.conflicted:
            raise TargetRefused(
                "mid_conflicted",
                "mid_conflicted",
                f"{where} has copies with different content under one Message-ID",
            )
        folder_ids = tuple(loc.folder_id for loc in locs)
        if single:
            if folder is not None:
                if folder not in folder_ids:
                    raise TargetRefused(
                        "unknown_mid", "not_in_folder", f"{where} is not in the named folder"
                    )
                folder_ids = (folder,)
            elif len(folder_ids) > 1:
                raise TargetRefused(
                    "ambiguous_location",
                    "ambiguous_location",
                    f"{where} is in {len(folder_ids)} folders; name one with "
                    f"{' or '.join(FOLDER_FIELDS)}",
                )
        out.append(IndexTarget(mid, msg.header_message_id, folder_ids, in_place=not single))
    return tuple(out)


def recheck_index(index: Store, targets: Sequence[IndexTarget]) -> None:
    """The index half of the execution re-check (M6, merge review): each approved target, in
    order, must still be in the index with at least one location, else ``targets_changed``
    (``not_indexed``), and must not have become conflicted while the card was open (e.g. a copy
    with the same Message-ID but different content arrived), else ``mid_conflicted`` (D1: no write
    to a conflicted mid, whenever the conflict appeared). Runs before :func:`find_live`."""
    n = len(targets)
    for i, t in enumerate(targets, start=1):
        where = f"target {i} of {n}"
        current = index.get_message(t.mid)
        if current is None or not index.locations(t.mid):
            raise TargetRefused(
                "targets_changed", "not_indexed", f"{where} is no longer in the index"
            )
        if current.conflicted:
            raise TargetRefused(
                "mid_conflicted",
                "mid_conflicted",
                f"{where} has copies with different content under one Message-ID",
            )


async def find_locations(
    bridge: ExtensionRequester | None, header_message_id: str
) -> list[FoundLocation]:
    """``mail.messages.find`` through the engine's bridge; a bridge failure is a
    :class:`TargetRefused` (``unavailable`` or ``internal``)."""
    if bridge is None:
        raise TargetRefused(
            "unavailable", "unavailable", "the Thunderbird extension is not available"
        )
    try:
        found = await bridge.request(
            "mail.messages.find", FindParams(header_message_id=header_message_id)
        )
    except BridgeError as exc:
        if exc.name in ("unavailable", "tb_error"):
            raise TargetRefused(
                "unavailable", exc.name, "Thunderbird could not locate the targets"
            ) from None
        raise TargetRefused("internal", exc.name, f"bridge error {exc.name}") from None
    if not isinstance(found, FoundList):
        raise TypeError("mail.messages.find did not return a FoundList")
    return list(found.root)


def resolved_location(loc: FoundLocation) -> ResolvedLocation:
    """A found location with its audit state. A state the audit log cannot record (e.g. more
    than 256 tag keys, an empty folder id) refuses the call (``internal``) instead of
    escaping as a validation error without a row."""
    try:
        state = TargetState(
            folder_id=loc.folder_id, tags=loc.tags, flagged=loc.flags.flagged, read=loc.flags.read
        )
    except ValidationError:
        raise TargetRefused(
            "internal", "unrecordable_state", "a target's state cannot be recorded"
        ) from None
    return ResolvedLocation(folder_id=loc.folder_id, tb_id=loc.tb_id, state=state)


def _order(loc: ResolvedLocation) -> tuple[str, int]:
    return (loc.folder_id, loc.tb_id)


async def find_live(
    bridge: ExtensionRequester | None,
    targets: Sequence[IndexTarget],
    *,
    against: str = "the index",
) -> LiveTargets:
    """The live locations of ``targets`` (module docstring, stage 2). ``against`` names what the
    folders came from in a ``targets_changed`` message (the index, or the approval card for the
    execution re-check)."""
    locations: dict[str, tuple[ResolvedLocation, ...]] = {}
    untouched: dict[str, frozenset[int]] = {}
    n = len(targets)
    for i, t in enumerate(targets, start=1):
        found = [resolved_location(f) for f in await find_locations(bridge, t.header_message_id)]
        wanted = set(t.folder_ids)
        mine = sorted((f for f in found if f.folder_id in wanted), key=_order)
        if {f.folder_id for f in mine} != wanted:
            raise TargetRefused(
                "targets_changed",
                "not_found",
                f"target {i} of {n} is no longer where {against} has it",
            )
        locations[t.mid] = tuple(mine)
        untouched[t.mid] = frozenset(f.tb_id for f in found if f.folder_id not in wanted)
    return LiveTargets(tuple(targets), locations, untouched)


def pair_states(
    prev: Sequence[ResolvedLocation], post: Sequence[ResolvedLocation]
) -> list[tuple[TargetState | None, TargetState | None]]:
    """One mid's prev and post locations paired: first by ``tb_id`` (changed in place), then by
    folder (one copy each side), then the rest in (folder id, tb_id) order (a move: the vanished
    prev with the new post location). An unpaired side is None (step 8 refuses to undo it)."""
    left = sorted(prev, key=_order)
    right = sorted(post, key=_order)
    pairs: list[tuple[TargetState | None, TargetState | None]] = []
    for match in (
        lambda a, b: a.tb_id == b.tb_id,
        lambda a, b: a.folder_id == b.folder_id,
    ):
        rest: list[ResolvedLocation] = []
        for a in left:
            b = next((b for b in right if match(a, b)), None)
            if b is None:
                rest.append(a)
            else:
                right.remove(b)
                pairs.append((a.state, b.state))
        left = rest
    for k in range(max(len(left), len(right))):
        pairs.append(
            (
                left[k].state if k < len(left) else None,
                right[k].state if k < len(right) else None,
            )
        )
    return pairs


async def post_state(bridge: ExtensionRequester | None, live: LiveTargets) -> list[UndoTarget]:
    """Every target found again after the write; one undo target per paired location (no
    ``tb_id``, never content). Empty for a call without target mids. An in-place target keeps
    only its prev tb_ids; a move/delete target every location not left out by the live find
    (module docstring)."""
    out: list[UndoTarget] = []
    for t in live.targets:
        skip = live.untouched[t.mid]
        prev_ids = {p.tb_id for p in live.locations[t.mid]}
        found = await find_locations(bridge, t.header_message_id)
        if t.in_place:
            kept = [f for f in found if f.tb_id in prev_ids]
        else:
            kept = [f for f in found if f.tb_id not in skip]
        post = [resolved_location(f) for f in kept]
        out.extend(
            UndoTarget(mid=t.mid, prev=p, post=q)
            for p, q in pair_states(live.locations[t.mid], post)
        )
    return out
