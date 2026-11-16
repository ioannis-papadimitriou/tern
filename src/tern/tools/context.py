"""``ToolContext``: everything a tool can reach (architecture §5, plan 06a step 1).

A tool gets a read-only index, the shared :class:`~tern.search.service.SearchService` (#31) over
that same index, a :class:`ScopedBridge`, a logger, the config, a clock, the runtime's state dir
(:attr:`ToolContext.state_dir`) and, for a W tool, the engine-resolved
:attr:`ToolContext.targets`; no raw sockets, no writable DB handle and no
subprocess helper.

:class:`ScopedBridge` limits a tool, plugins included, to the bridge methods its declared effects
imply (#6: "the contract restricts declared effects"). Anything else raises
:class:`BridgeScopeError` before the extension is asked (``Registry.call`` reports it as
``internal``). With no extension attached, an allowed request raises bridge error
``unavailable``.

A tool with a message-write effect (``mail_meta``, ``mail_move``, ``mail_delete``) writes only
to the locations the permission engine resolved for its call (``ctx.targets``, plan 06b step 3
review fix): a :data:`TARGETED_WRITES` request whose ``tb_ids`` are not all among them is a
:class:`BridgeScopeError`, and such a tool has no ``mail.messages.find`` (its tb_ids come from
``ctx.targets`` only). Otherwise a tool could write to messages the engine never resolved and
get around count escalation, the conflicted/unknown refusals and the undo record.
"""

from __future__ import annotations

import dataclasses
import logging
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from tern.bridge.client import ExtensionRequester
from tern.bridge.protocol import (
    ArchiveParams,
    BridgeError,
    DeleteParams,
    Mid,
    MoveParams,
    UpdateParams,
)
from tern.config import TernConfig
from tern.index.store import Store
from tern.search.service import SearchService
from tern.tools.contract import Effect

if TYPE_CHECKING:
    from tern.perms.audit import TargetState

#: Bridge 1.0 methods each effect allows (provisional mapping for writes; 06b may refine it).
EFFECT_METHODS: typing.Mapping[Effect, frozenset[str]] = types.MappingProxyType(
    {
        Effect.READ_INDEX: frozenset(),
        Effect.READ_LIVE: frozenset(
            {
                "mail.accounts.list",
                "mail.folders.list",
                "mail.messages.find",
                "mail.messages.full",
                "mail.messages.raw",
                "mail.open",
                "mail.tags.list",
            }
        ),
        Effect.MAIL_META: frozenset({"mail.update", "mail.tags.create"}),
        Effect.MAIL_MOVE: frozenset({"mail.move", "mail.archive"}),
        Effect.MAIL_DELETE: frozenset({"mail.delete"}),
        Effect.DRAFTS: frozenset({"mail.draft.save", "mail.compose.open"}),
        Effect.CONTACTS_WRITE: frozenset(),
        Effect.CALENDAR_WRITE: frozenset(),
        Effect.FILTERS_WRITE: frozenset(),
    }
)


#: Write methods that name messages by ``tb_ids``, with the params model each must be sent as:
#: a tool may name only the tb_ids of its call's ``ctx.targets``.
TARGETED_WRITES: typing.Mapping[str, type[BaseModel]] = types.MappingProxyType(
    {
        "mail.update": UpdateParams,
        "mail.move": MoveParams,
        "mail.archive": ArchiveParams,
        "mail.delete": DeleteParams,
    }
)

#: Effects that write to messages: their tools get no ``mail.messages.find``.
MESSAGE_WRITE_EFFECTS = frozenset({Effect.MAIL_META, Effect.MAIL_MOVE, Effect.MAIL_DELETE})


class BridgeScopeError(Exception):
    """A tool asked for a bridge method its declared effects do not allow (a bug: loud)."""


class ToolContextError(ValueError):
    """A ``ToolContext`` built from parts a tool must not get (e.g. a writable Store)."""


def allowed_methods(effects: Iterable[Effect]) -> frozenset[str]:
    """The union of the bridge methods ``effects`` allow."""
    allowed: frozenset[str] = frozenset()
    for e in effects:
        allowed |= EFFECT_METHODS[e]
    return allowed


class ScopedBridge:
    """The bridge as one tool sees it: only the methods its effects imply, and message writes
    only to ``target_tb_ids`` (the tb_ids of the call's ``ctx.targets``; none by default)."""

    def __init__(
        self,
        requester: ExtensionRequester | None,
        effects: Iterable[Effect],
        *,
        tool: str,
        target_tb_ids: Iterable[int] = (),
    ) -> None:
        self._requester = requester
        self._tool = tool
        effect_set = frozenset(effects)
        allowed = allowed_methods(effect_set)
        if effect_set & MESSAGE_WRITE_EFFECTS:
            allowed -= {"mail.messages.find"}
        self.allowed: frozenset[str] = allowed
        self._target_tb_ids: frozenset[int] = frozenset(target_tb_ids)

    async def request(self, method: str, params: BaseModel) -> Any:
        if method not in self.allowed:
            raise BridgeScopeError(
                f"tool {self._tool} may not call bridge method {method} (not in its effects)"
            )
        model = TARGETED_WRITES.get(method)
        if model is not None:
            params = self._checked_write(method, model, params)
        if self._requester is None:
            raise BridgeError("unavailable", "no extension attached")
        return await self._requester.request(method, params)

    def _checked_write(self, method: str, model: type[BaseModel], params: BaseModel) -> BaseModel:
        """A private copy of a write request's params, after checking that every tb_id it names
        is one of the call's targets. The copy is what is sent: a tool that changes its own params
        object after this check (e.g. from another task) changes nothing that reaches TB."""
        if type(params) is not model:
            raise BridgeScopeError(f"tool {self._tool} must send {method} as {model.__name__}")
        copy = model.model_validate(params.model_dump())
        tb_ids: object = getattr(copy, "tb_ids", None)
        if not isinstance(tb_ids, list) or not set(tb_ids) <= self._target_tb_ids:
            raise BridgeScopeError(
                f"tool {self._tool} may not call {method} on a message the permission engine "
                "did not resolve for this call"
            )
        return copy


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    """One live location of a write target, resolved by the permission engine from
    ``mail.messages.find`` just before a W tool runs (plan 06b step 0, AFK amendment 4).

    ``tb_id`` is TB's per-connection id: it lives in memory for that one call only (#5) and is
    never returned by the engine, logged, audited or put in an undo payload. It is left out of
    ``repr`` so a stray log of a context cannot leak it. ``state`` is the location's audit
    :class:`~tern.perms.audit.TargetState` (folder id, tag keys, flagged, read; never content).

    ``state`` is a private deep copy taken at construction (``TargetState`` is a mutable pydantic
    model): a tool that changes ``loc.state`` changes only its own copy, never the engine's audit
    or undo record."""

    folder_id: str
    tb_id: int = field(repr=False)
    state: TargetState

    def __post_init__(self) -> None:
        # Lazy: tern.perms.audit imports tern.tools.contract, whose package imports this module.
        from tern.perms.audit import TargetState

        if not isinstance(self.folder_id, str) or not self.folder_id:
            raise ToolContextError("ResolvedLocation.folder_id must be a non-empty str")
        if isinstance(self.tb_id, bool) or not isinstance(self.tb_id, int) or self.tb_id < 0:
            raise ToolContextError("ResolvedLocation.tb_id must be an int >= 0")
        if not isinstance(self.state, TargetState):
            raise ToolContextError("ResolvedLocation.state must be a TargetState")
        if self.state.folder_id != self.folder_id:
            raise ToolContextError("ResolvedLocation.state is for another folder")
        object.__setattr__(self, "state", self.state.model_copy(deep=True))


#: Validates a ``targets`` key against the bridge's own mid type (one definition, no drift).
_MID: TypeAdapter[str] = TypeAdapter(Mid, config=ConfigDict(hide_input_in_errors=True))


#: ``ToolContext.targets`` when the engine resolved none (every R0/R1 call).
NO_TARGETS: Mapping[str, tuple[ResolvedLocation, ...]] = types.MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool can reach (module docstring). ``Registry.call`` builds one per call."""

    #: The index, opened ``readonly=True``; a writable Store is refused.
    index: Store
    #: Shared search over a read-only Store (#31); tools never build FTS or vector SQL.
    search: SearchService
    #: Scoped to the calling tool's effects.
    bridge: ScopedBridge
    log: logging.Logger
    config: TernConfig
    #: Wall-clock time, epoch seconds.
    clock: Callable[[], float]
    #: mid → its live locations, resolved by the permission engine for a W tool (plan 06b);
    #: empty for R0/R1 tools. Stored as a read-only copy, down to each location's ``state``.
    targets: Mapping[str, tuple[ResolvedLocation, ...]] = NO_TARGETS
    #: tern's state dir of this runtime (ternd's ``paths.resolve().state``; a test's or the
    #: probe's synthetic one), absolute; None when the runtime has none. Tools that keep local
    #: state (the ``tb_*`` snapshot copies) take it from here, never from the process
    #: environment, and refuse to run without one.
    state_dir: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.index, Store) or not self.index.readonly:
            raise ToolContextError("ToolContext.index must be a Store opened readonly=True")
        if not isinstance(self.search, SearchService):
            raise ToolContextError("ToolContext.search must be a SearchService")
        if self.search.store is not self.index:
            raise ToolContextError("ToolContext.search must search ToolContext.index")
        if not isinstance(self.bridge, ScopedBridge):
            raise ToolContextError("ToolContext.bridge must be a ScopedBridge")
        if not isinstance(self.log, logging.Logger):
            raise ToolContextError("ToolContext.log must be a logging.Logger")
        if not isinstance(self.config, TernConfig):
            raise ToolContextError("ToolContext.config must be a TernConfig")
        if not callable(self.clock):
            raise ToolContextError("ToolContext.clock must be callable")
        if self.state_dir is not None and (
            not isinstance(self.state_dir, Path) or not self.state_dir.is_absolute()
        ):
            raise ToolContextError("ToolContext.state_dir must be an absolute Path or None")
        object.__setattr__(self, "targets", _frozen_targets(self.targets))


def _frozen_targets(
    targets: Mapping[str, tuple[ResolvedLocation, ...]],
) -> Mapping[str, tuple[ResolvedLocation, ...]]:
    """A read-only copy of ``targets`` after checking its shape (a tern bug otherwise: loud).

    Each :class:`ResolvedLocation` is rebuilt (so its ``state`` is copied again): the objects the
    engine passed in are never the ones the tool sees."""
    if not isinstance(targets, Mapping):
        raise ToolContextError("ToolContext.targets must be a Mapping")
    if not targets:
        return NO_TARGETS
    copy: dict[str, tuple[ResolvedLocation, ...]] = {}
    for mid, locs in targets.items():
        if not isinstance(mid, str):
            raise ToolContextError("ToolContext.targets keys must be mids")
        try:
            _MID.validate_python(mid, strict=True)
        except ValidationError:
            raise ToolContextError("ToolContext.targets keys must be mids") from None
        if (
            not isinstance(locs, tuple)
            or not locs
            or not all(isinstance(loc, ResolvedLocation) for loc in locs)
        ):
            raise ToolContextError(
                "ToolContext.targets values must be non-empty tuples of ResolvedLocation"
            )
        copy[mid] = tuple(dataclasses.replace(loc) for loc in locs)
    return types.MappingProxyType(copy)
