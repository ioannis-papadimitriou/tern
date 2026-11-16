"""``tern-tool/1``: the ``@tool`` decorator, tiers, effects and tool metadata (#6, plan 06a step 1).

A tool is an ``async def (ctx: ToolContext, inp: In) -> Out`` whose ``In``/``Out`` are
:class:`~tern._model.BoundaryModel` subclasses. ``@tool`` validates the definition at decoration
time (:class:`ToolDefinitionError`, loud) and returns a :class:`ToolSpec`: metadata plus the
coroutine function. A ``ToolSpec`` is **not callable**; the only way to run a tool is
``Registry.call`` (permission engine + audit, CLAUDE.md hard rule).

Tiers (#7): ``R0 < R1 < W1 < W2 < W3``. The effective tier is the maximum of the declared tier and
the tiers implied by the declared effects; a declared tier below the implied one is allowed and
escalated (both are shown in :class:`ToolMeta`), never an error. Count escalation (``count_targets``
> 20) and recipient escalation (``recipients``) are computed from the input by the permission
engine (plan 06b), never by the tool.

``Effect`` is closed: exactly #6's set. There is no network and no send effect (#8, #9).
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import types
import typing
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from tern._model import BoundaryModel
from tern.bridge.protocol import Mid

if TYPE_CHECKING:
    from tern.tools.context import ToolContext

CONTRACT: Literal["tern-tool/1"] = "tern-tool/1"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

Origin = Literal["builtin", "plugin"]


class Tier(StrEnum):
    """Permission tiers (#7). Values equal bridge 1.0's ``Tier`` literals."""

    R0 = "R0"
    R1 = "R1"
    W1 = "W1"
    W2 = "W2"
    W3 = "W3"

    @property
    def rank(self) -> int:
        return _TIER_ORDER.index(self)


_TIER_ORDER: tuple[Tier, ...] = (Tier.R0, Tier.R1, Tier.W1, Tier.W2, Tier.W3)


def max_tier(tiers: Iterable[Tier]) -> Tier:
    """The highest tier of ``tiers`` (``R0`` for none)."""
    return max(tiers, key=lambda t: t.rank, default=Tier.R0)


class Effect(StrEnum):
    """What a tool may touch (#6). Closed: a tool cannot declare anything else."""

    READ_INDEX = "read_index"
    READ_LIVE = "read_live"
    MAIL_META = "mail_meta"
    MAIL_MOVE = "mail_move"
    MAIL_DELETE = "mail_delete"
    DRAFTS = "drafts"
    CONTACTS_WRITE = "contacts_write"
    CALENDAR_WRITE = "calendar_write"
    FILTERS_WRITE = "filters_write"


#: The tier each effect implies (#7 table).
IMPLIED_TIER: typing.Mapping[Effect, Tier] = types.MappingProxyType(
    {
        Effect.READ_INDEX: Tier.R0,
        Effect.READ_LIVE: Tier.R1,
        Effect.MAIL_META: Tier.W1,
        Effect.DRAFTS: Tier.W1,
        Effect.MAIL_MOVE: Tier.W2,
        Effect.MAIL_DELETE: Tier.W3,
        Effect.CONTACTS_WRITE: Tier.W3,
        Effect.CALENDAR_WRITE: Tier.W3,
        Effect.FILTERS_WRITE: Tier.W3,
    }
)


def effective_tier(declared: Tier, effects: Iterable[Effect]) -> Tier:
    """max(declared tier, every tier implied by ``effects``)."""
    return max_tier([declared, *(IMPLIED_TIER[e] for e in effects)])


class ToolDefinitionError(Exception):
    """A tool definition violates ``tern-tool/1`` (raised at decoration time)."""


#: Every way ``Registry.call`` fails (architecture §5). The last six are the permission engine's
#: (plan 06b). Error codes are not part of the frozen ``ToolMeta`` schema, so adding one is not a
#: ``tern-tool/1`` contract change.
ToolErrorCode = Literal[
    "invalid_params",
    "unknown_tool",
    "tier_not_enabled",
    "unknown_mid",
    "mid_conflicted",
    "unavailable",
    "internal",
    "rate_limited",
    "ambiguous_location",
    "denied",
    "expired",
    "targets_changed",
    "state_changed",
]

#: The codes a tool itself may raise. The others (``unknown_tool``, ``tier_not_enabled`` and the
#: engine's codes) are the registry's and the engine's own: a tool raising one is reported as
#: ``internal`` (reason ``bad_code``).
TOOL_RAISABLE_CODES: frozenset[str] = frozenset(
    {"invalid_params", "unknown_mid", "mid_conflicted", "unavailable", "internal"}
)

_REASON_RE = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")


class ToolError(Exception):
    """A tool call failed. ``code`` is a :data:`ToolErrorCode`; ``reason`` an optional
    snake_case detail code (``no_message_id``, ``tb_error``, ...). ``message`` carries field
    locations and reason codes, never input values or mail content."""

    def __init__(self, code: ToolErrorCode, message: str, *, reason: str | None = None) -> None:
        if code not in typing.get_args(ToolErrorCode):
            raise ValueError("invalid ToolError code")
        if not isinstance(message, str) or not message:
            raise ValueError("ToolError needs a message")
        if reason is not None and (not isinstance(reason, str) or not _REASON_RE.fullmatch(reason)):
            raise ValueError("ToolError reason must be a snake_case code")
        super().__init__(f"{code}: {message}")
        self.code: ToolErrorCode = code
        self.message = message
        self.reason = reason


class ToolMeta(BoundaryModel):
    """The metadata document every tool exports (``tern tools describe``). Its JSON Schema is
    the frozen ``contracts/tern-tool-1.schema.json``; a change needs ``tern-tool/2``."""

    #: Required, no default (like the bridge's ``Hello.protocol``): a document without its
    #: contract marker is not a ``tern-tool/1`` document.
    contract: Literal["tern-tool/1"]
    name: str = Field(pattern=NAME_RE.pattern)
    description: str = Field(min_length=1)
    #: The tier the tool declares.
    tier: Tier
    #: max(declared tier, tiers implied by effects); count and recipient escalation come on top
    #: per call (permission engine).
    effective_tier: Tier
    #: Sorted, no duplicates (validated; ``uniqueItems`` in the schema).
    effects: list[Effect] = Field(json_schema_extra={"uniqueItems": True})
    #: The output holds attacker-controllable text (agent wraps it with ``untrusted.wrap()``).
    untrusted_output: bool
    #: The tool declares ``count_targets``.
    counts_targets: bool
    #: The tool declares ``recipients``.
    declares_recipients: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    origin: Origin

    @field_validator("effects")
    @classmethod
    def _effects_sorted_unique(cls, v: list[Effect]) -> list[Effect]:
        if v != sorted(set(v)):
            raise ValueError("effects must be sorted and without duplicates")
        return v


ToolFn = Callable[["ToolContext", Any], Awaitable[BoundaryModel]]


#: The ``Field(pattern=...)`` object inside ``Mid``. ``Annotated`` flattening keeps it by identity,
#: so ``Annotated[Mid, Field(...)]`` and ``list[Annotated[Mid, ...]]`` are still recognised as Mid.
_MID_FIELD: object = typing.get_args(Mid)[1]

#: Containers whose single type argument ``Mid`` makes a "many" mid field (``tuple[Mid, ...]`` too).
_MID_CONTAINERS: tuple[object, ...] = (list, set, frozenset, Sequence)

#: ``Mid``'s JSON Schema ``pattern``. Any ``In`` property whose schema holds it (however pydantic
#: got there: an equivalent hand-written alias, a TypedDict, a dataclass, a generic model) holds
#: mids.
_MID_PATTERN: str = TypeAdapter(Mid).json_schema()["pattern"]


def _is_mid(hint: object) -> bool:
    """``hint`` is ``Mid`` (possibly with extra ``Annotated`` metadata)."""
    return (
        typing.get_origin(hint) is typing.Annotated
        and typing.get_args(hint)[0] is str
        and any(m is _MID_FIELD for m in typing.get_args(hint)[1:])
    )


def _strip(hint: object) -> object:
    """Strip PEP 695 ``type`` aliases, outer ``Annotated[...]`` layers (other than Mid's own) and
    ``X | None``."""
    while True:
        if _is_mid(hint):
            return hint
        if isinstance(hint, typing.TypeAliasType):
            hint = hint.__value__
            continue
        origin = typing.get_origin(hint)
        if origin is typing.Annotated:
            hint = typing.get_args(hint)[0]
            continue
        if origin in (typing.Union, types.UnionType):
            args = typing.get_args(hint)
            rest = [a for a in args if a is not type(None)]
            if len(rest) == 1 and len(rest) < len(args):
                hint = rest[0]
                continue
        return hint


def _is_mid_hint(hint: object) -> Literal["one", "many"] | None:
    """``Mid`` → one; ``list``/``set``/``frozenset``/``Sequence[Mid]`` or ``tuple[Mid, ...]`` →
    many. Each may be optional (``| None``) and wrapped in ``Annotated[...]``."""
    hint = _strip(hint)
    if _is_mid(hint):
        return "one"
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin in _MID_CONTAINERS and len(args) == 1 and _is_mid(args[0]):
        return "many"
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis and _is_mid(args[0]):
        return "many"
    return None


def _contains_mid(hint: object, seen: set[object], name: str) -> bool:
    """``Mid`` appears anywhere in ``hint``: generic arguments, unions, ``type`` aliases, nested
    models (parametrized generic ones included), TypedDicts and dataclasses."""
    if _is_mid(hint):
        return True
    if isinstance(hint, typing.TypeAliasType):
        if hint in seen:
            return False
        seen.add(hint)
        return _contains_mid(hint.__value__, seen, name)
    is_model = isinstance(hint, type) and issubclass(hint, BaseModel)
    if (
        is_model
        or typing.is_typeddict(hint)
        or (isinstance(hint, type) and dataclasses.is_dataclass(hint))
    ):
        if hint in seen:
            return False
        seen.add(hint)
        try:
            field_hints = typing.get_type_hints(hint, include_extras=True)
        except (NameError, TypeError) as exc:
            raise ToolDefinitionError(
                f"tool {name}: nested annotations cannot be resolved ({type(exc).__name__})"
            ) from exc
        # ``Page[Mid]``: get_type_hints shows ``list[T]``; the arguments are in pydantic's metadata.
        generic_args: tuple[object, ...] = (
            getattr(hint, "__pydantic_generic_metadata__", {}).get("args", ()) if is_model else ()
        )
        return any(_contains_mid(h, seen, name) for h in (*field_hints.values(), *generic_args))
    return any(_contains_mid(a, seen, name) for a in typing.get_args(hint))


def _schema_has_mid(node: object, defs: dict[str, Any], seen: set[str]) -> bool:
    """A JSON Schema node (``$ref`` into ``defs`` followed) holds ``"pattern": <Mid's pattern>``."""
    if isinstance(node, list):
        return any(_schema_has_mid(n, defs, seen) for n in node)
    if not isinstance(node, dict):
        return False
    if node.get("pattern") == _MID_PATTERN:
        return True
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        key = ref.removeprefix("#/$defs/")
        if key not in seen:
            seen.add(key)
            if _schema_has_mid(defs.get(key), defs, seen):
                return True
    return any(_schema_has_mid(v, defs, seen) for k, v in node.items() if k != "$ref")


@dataclass(frozen=True, slots=True, eq=False)
class ToolSpec:
    """A validated tool: metadata plus the coroutine function (``impl``).

    Deliberately not callable. Only ``Registry.call`` awaits ``impl``.
    """

    name: str
    description: str
    tier: Tier
    effects: frozenset[Effect]
    untrusted_output: bool
    input_model: type[BoundaryModel]
    output_model: type[BoundaryModel]
    impl: ToolFn
    count_targets: Callable[[Any], int] | None
    recipients: Callable[[Any], list[str]] | None
    #: Top-level ``In`` fields typed ``Mid`` or a Mid container (see ``_is_mid_hint``), in field
    #: order.
    mid_fields: tuple[tuple[str, Literal["one", "many"]], ...]

    @property
    def effective_tier(self) -> Tier:
        return effective_tier(self.tier, self.effects)

    def target_mids(self, inp: BoundaryModel) -> list[str]:
        """The mids an (already validated) input targets, from the ``Mid``-typed fields."""
        mids: list[str] = []
        for field_name, kind in self.mid_fields:
            value = getattr(inp, field_name)
            if value is None:
                continue
            if kind == "one":
                mids.append(value)
            elif isinstance(value, (set, frozenset)):
                mids.extend(sorted(value))  # deterministic order for the audit row
            else:
                mids.extend(value)
        return mids

    def target_count(self, inp: BoundaryModel) -> int:
        """The number of target mids; with ``count_targets`` declared, ``count_targets(inp)``
        floored at the number of distinct detected mids (a tool may raise its count, never lower
        it below what tern detects: architecture §5)."""
        if self.count_targets is None:
            return len(self.target_mids(inp))
        n = self.count_targets(inp)
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise TypeError(f"count_targets of tool {self.name} must return an int >= 0")
        return max(n, len(set(self.target_mids(inp))))

    def recipient_addresses(self, inp: BoundaryModel) -> list[str] | None:
        """The declared recipients of an input, lower-cased; None when the tool declares none."""
        if self.recipients is None:
            return None
        addrs = self.recipients(inp)
        if not isinstance(addrs, list) or not all(isinstance(a, str) for a in addrs):
            raise TypeError(f"recipients of tool {self.name} must return a list of str")
        return [a.lower() for a in addrs]

    def meta(self, origin: Origin) -> ToolMeta:
        """The ``tern-tool/1`` metadata document; ``origin`` is set by the registry."""
        return ToolMeta(
            contract=CONTRACT,
            name=self.name,
            description=self.description,
            tier=self.tier,
            effective_tier=self.effective_tier,
            effects=sorted(self.effects),
            untrusted_output=self.untrusted_output,
            counts_targets=self.count_targets is not None,
            declares_recipients=self.recipients is not None,
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_model.model_json_schema(),
            origin=origin,
        )


def _model_type(hint: object, what: str, name: str) -> type[BoundaryModel]:
    if not (isinstance(hint, type) and issubclass(hint, BoundaryModel)):
        raise ToolDefinitionError(f"tool {name}: {what} must be a BoundaryModel subclass")
    # A subclass can override model_config; the boundary guarantees must survive it.
    if hint.model_config.get("hide_input_in_errors") is not True:
        raise ToolDefinitionError(f"tool {name}: {what} must keep hide_input_in_errors=True")
    if hint.model_config.get("extra") != "forbid":
        raise ToolDefinitionError(f"tool {name}: {what} must keep extra='forbid'")
    if hint.model_json_schema().get("type") != "object":
        raise ToolDefinitionError(f"tool {name}: {what} must be an object model")
    return hint


def tool(
    *,
    name: str,
    tier: Tier,
    effects: Iterable[Effect],
    untrusted_output: bool,
    count_targets: Callable[[Any], int] | None = None,
    recipients: Callable[[Any], list[str]] | None = None,
) -> Callable[[ToolFn], ToolSpec]:
    """Declare a ``tern-tool/1`` tool. See the module docstring."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ToolDefinitionError("tool name must match ^[a-z][a-z0-9_]{0,63}$")
    if not isinstance(tier, Tier):
        raise ToolDefinitionError(f"tool {name}: tier must be a Tier")
    effect_set = frozenset(effects)
    if not all(isinstance(e, Effect) for e in effect_set):
        raise ToolDefinitionError(f"tool {name}: every effect must be an Effect")
    if not isinstance(untrusted_output, bool):
        raise ToolDefinitionError(f"tool {name}: untrusted_output must be a bool")
    if count_targets is not None and not callable(count_targets):
        raise ToolDefinitionError(f"tool {name}: count_targets must be callable")
    if recipients is not None and not callable(recipients):
        raise ToolDefinitionError(f"tool {name}: recipients must be callable")

    def decorate(fn: ToolFn) -> ToolSpec:
        if not inspect.iscoroutinefunction(fn):
            raise ToolDefinitionError(f"tool {name}: must be an async def")
        doc = inspect.getdoc(fn)
        if doc is None or not doc.strip():
            raise ToolDefinitionError(f"tool {name}: needs a docstring (its description)")
        params = list(inspect.signature(fn).parameters.values())
        positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        if len(params) != 2 or any(p.kind not in positional for p in params):
            raise ToolDefinitionError(f"tool {name}: signature must be (ctx, inp) -> Out")
        try:
            hints = typing.get_type_hints(fn)
        except (NameError, TypeError) as exc:
            raise ToolDefinitionError(
                f"tool {name}: annotations cannot be resolved ({type(exc).__name__})"
            ) from exc
        inp_hint = hints.get(params[1].name)
        if inp_hint is None:
            raise ToolDefinitionError(f"tool {name}: the input parameter needs an annotation")
        if "return" not in hints:
            raise ToolDefinitionError(f"tool {name}: needs a return annotation")
        input_model = _model_type(inp_hint, "In", name)
        output_model = _model_type(hints["return"], "Out", name)
        in_hints = typing.get_type_hints(input_model, include_extras=True)
        # Keyed by field name (not alias) so it lines up with model_fields.
        in_schema = input_model.model_json_schema(by_alias=False)
        in_props: dict[str, Any] = in_schema.get("properties", {})
        in_defs: dict[str, Any] = in_schema.get("$defs", {})
        mid_fields: list[tuple[str, Literal["one", "many"]]] = []
        undetected: list[str] = []
        for field_name in input_model.model_fields:
            field_hint = in_hints.get(field_name)
            kind = _is_mid_hint(field_hint)
            if kind is not None:
                mid_fields.append((field_name, kind))
            elif _contains_mid(field_hint, set(), name) or _schema_has_mid(
                in_props.get(field_name), in_defs, set()
            ):
                # Only ``tern.bridge.protocol.Mid`` itself is detected; anything else pydantic
                # validates with Mid's pattern (an equivalent alias included) is undetected.
                undetected.append(field_name)
        # Target mids feed the audit row and 06b's count escalation: a Mid the detector cannot
        # read, or a write tool with no target at all, must say how it counts its targets.
        if undetected and count_targets is None:
            raise ToolDefinitionError(
                f"tool {name}: In fields {', '.join(undetected)} hold mids in a shape that is not "
                "a top-level Mid or Mid container; declare count_targets"
            )
        if (
            effective_tier(tier, effect_set).rank >= Tier.W1.rank
            and not mid_fields
            and count_targets is None
        ):
            raise ToolDefinitionError(
                f"tool {name}: a write-tier tool needs a top-level Mid field or count_targets"
            )
        return ToolSpec(
            name=name,
            description=doc.strip(),
            tier=tier,
            effects=effect_set,
            untrusted_output=untrusted_output,
            input_model=input_model,
            output_model=output_model,
            impl=fn,
            count_targets=count_targets,
            recipients=recipients,
            mid_fields=tuple(mid_fields),
        )

    return decorate
