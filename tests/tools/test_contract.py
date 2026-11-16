"""tern-tool/1 contract: @tool, tiers, effects, ToolSpec, ToolMeta (plan 06a step 1).

Test models are defined at module level: ``@tool`` resolves the In/Out annotations with
``typing.get_type_hints`` in the function's module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tern.bridge.protocol import Mid
from tern.tools import (
    IMPLIED_TIER,
    BoundaryModel,
    Effect,
    Tier,
    ToolContext,
    ToolDefinitionError,
    ToolMeta,
    ToolSpec,
    tool,
)
from tern.tools.contract import effective_tier, max_tier

MID_A = "a" * 64
MID_B = "x-" + "b" * 64


class In(BoundaryModel):
    mids: list[Mid] = Field(min_length=1)
    to: list[str] = []
    cc: list[str] = []


class Out(BoundaryModel):
    changed: int


class OneMidIn(BoundaryModel):
    mid: Mid
    reply_to: Mid | None = None
    note: str = ""


class PlainIn(BaseModel):
    x: int


class NotObjectOut(BoundaryModel):
    """A BoundaryModel whose schema is not an object (RootModel cannot be a BoundaryModel)."""

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"type": "array", "items": {"type": "integer"}}


class Item(BoundaryModel):
    mid: Mid
    note: str = ""


class AnnotatedListIn(BoundaryModel):
    mids: Annotated[list[Mid], Field(max_length=500)]


class TupleIn(BoundaryModel):
    mids: tuple[Mid, ...]


class SequenceIn(BoundaryModel):
    mids: Sequence[Mid]


class SetIn(BoundaryModel):
    mids: set[Mid]


class FrozenSetIn(BoundaryModel):
    mids: frozenset[Mid]


class OptionalListIn(BoundaryModel):
    mids: list[Mid] | None = None


class AnnotatedOptionalListIn(BoundaryModel):
    mids: Annotated[list[Mid] | None, Field(max_length=5)] = None


class AnnotatedMidIn(BoundaryModel):
    mid: Annotated[Mid, Field(description="the message")]


class NestedItemsIn(BoundaryModel):
    items: list[Item]


class DictIn(BoundaryModel):
    by_label: dict[str, Mid]


class MixedUnionIn(BoundaryModel):
    target: Mid | list[Mid]


class NoMidIn(BoundaryModel):
    label: str


type MidList = list[Mid]
type ItemList = list[Item]

#: A plugin's own alias with Mid's exact pattern: not ``tern.bridge.protocol.Mid`` (review round 2).
EquivalentMid = Annotated[str, Field(pattern=r"^(x-)?[0-9a-f]{64}$")]


class Page[T](BoundaryModel):
    items: list[T]


class MidDict(TypedDict):
    mid: Mid


@dataclass
class MidRecord:
    mid: Mid


class AliasNextToMidIn(BoundaryModel):
    mid: Mid
    more: MidList


# Each holds a detected top-level ``mid: Mid`` next to mids the detector cannot count: without the
# round-2 checks they were accepted and the other mids were left out of target_count.
class TypeAliasModelNextToMidIn(BoundaryModel):
    mid: Mid
    extra: ItemList


class GenericNextToMidIn(BoundaryModel):
    mid: Mid
    page: Page[Mid]


class TypedDictNextToMidIn(BoundaryModel):
    mid: Mid
    extra: list[MidDict]


class DataclassNextToMidIn(BoundaryModel):
    mid: Mid
    extra: list[MidRecord]


class EquivalentAliasNextToMidIn(BoundaryModel):
    mid: Mid
    mids: list[EquivalentMid]


class FieldPatternNextToMidIn(BoundaryModel):
    mid: Mid
    other: str = Field(pattern=r"^(x-)?[0-9a-f]{64}$")


class ExtraAllowIn(BoundaryModel):
    model_config = ConfigDict(extra="allow")
    mids: list[Mid]


class EchoingIn(BoundaryModel):
    model_config = ConfigDict(hide_input_in_errors=False)
    mids: list[Mid]


class EchoingOut(BoundaryModel):
    model_config = ConfigDict(hide_input_in_errors=False)
    changed: int


def _tool_for(inp_model: type, out_model: type = Out, **overrides: Any) -> ToolSpec:
    """A tool whose In/Out annotations are set to the given classes."""
    kwargs: dict[str, Any] = {
        "name": "t",
        "tier": Tier.W3,
        "effects": {Effect.MAIL_DELETE},
        "untrusted_output": False,
    }
    kwargs.update(overrides)

    async def t(ctx: ToolContext, inp: In) -> Out:
        """Doc."""
        raise AssertionError("never run")

    t.__annotations__["inp"] = inp_model
    t.__annotations__["return"] = out_model
    return tool(**kwargs)(t)


def _make(**overrides: Any) -> ToolSpec:
    kwargs: dict[str, Any] = {
        "name": "set_things",
        "tier": Tier.W1,
        "effects": {Effect.MAIL_META},
        "untrusted_output": False,
    }
    kwargs.update(overrides)

    @tool(**kwargs)
    async def set_things(ctx: ToolContext, inp: In) -> Out:
        """Set things on messages."""
        raise AssertionError("never run in this test")

    return set_things


# ---- tiers and effects -------------------------------------------------------------------------


def test_tier_values_match_bridge_literals_and_order() -> None:
    assert [t.value for t in Tier] == ["R0", "R1", "W1", "W2", "W3"]
    assert [t.rank for t in Tier] == [0, 1, 2, 3, 4]
    assert max_tier([Tier.W1, Tier.R1, Tier.W2]) is Tier.W2
    assert max_tier([]) is Tier.R0


def test_effect_set_is_exactly_decision_6() -> None:
    assert {e.value for e in Effect} == {
        "read_index",
        "read_live",
        "mail_meta",
        "mail_move",
        "mail_delete",
        "drafts",
        "contacts_write",
        "calendar_write",
        "filters_write",
    }
    assert not any("send" in e.value or "net" in e.value for e in Effect)


def test_implied_tier_table() -> None:
    assert dict(IMPLIED_TIER) == {
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


@pytest.mark.parametrize(
    ("declared", "effects", "expected"),
    [
        (Tier.R0, {Effect.READ_INDEX}, Tier.R0),
        (Tier.R0, {Effect.READ_LIVE}, Tier.R1),
        (Tier.R0, {Effect.READ_INDEX, Effect.MAIL_MOVE}, Tier.W2),
        (Tier.W1, {Effect.MAIL_DELETE}, Tier.W3),
        (Tier.W3, {Effect.READ_INDEX}, Tier.W3),
        (Tier.W2, set(), Tier.W2),
    ],
)
def test_effective_tier_is_max_of_declared_and_implied(
    declared: Tier, effects: set[Effect], expected: Tier
) -> None:
    assert effective_tier(declared, effects) is expected
    spec = _make(tier=declared, effects=effects)
    assert spec.effective_tier is expected
    meta = spec.meta("plugin")
    assert meta.tier is declared  # the declared tier is kept and shown next to the effective one
    assert meta.effective_tier is expected


# ---- ToolSpec ----------------------------------------------------------------------------------


def test_tool_spec_is_not_callable() -> None:
    spec = _make()
    assert isinstance(spec, ToolSpec)
    assert not callable(spec)
    with pytest.raises(TypeError):
        spec(None, None)  # type: ignore[operator]  # the point: a ToolSpec cannot be called


def test_spec_carries_models_and_docstring() -> None:
    spec = _make()
    assert spec.name == "set_things"
    assert spec.description == "Set things on messages."
    assert spec.input_model is In
    assert spec.output_model is Out
    assert spec.effects == frozenset({Effect.MAIL_META})


def test_target_mids_from_mid_typed_fields() -> None:
    spec = _make()
    assert spec.mid_fields == (("mids", "many"),)
    inp = In(mids=[MID_A, MID_B])
    assert spec.target_mids(inp) == [MID_A, MID_B]
    assert spec.target_count(inp) == 2  # no count_targets: the number of target mids

    @tool(name="reply", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=True)
    async def reply(ctx: ToolContext, inp: OneMidIn) -> Out:
        """Reply."""
        raise AssertionError("never run")

    assert reply.mid_fields == (("mid", "one"), ("reply_to", "one"))
    assert reply.target_mids(OneMidIn(mid=MID_A)) == [MID_A]
    assert reply.target_mids(OneMidIn(mid=MID_A, reply_to=MID_B)) == [MID_A, MID_B]


@pytest.mark.parametrize(
    ("model", "field", "kind"),
    [
        (AnnotatedListIn, "mids", "many"),
        (TupleIn, "mids", "many"),
        (SequenceIn, "mids", "many"),
        (SetIn, "mids", "many"),
        (FrozenSetIn, "mids", "many"),
        (OptionalListIn, "mids", "many"),
        (AnnotatedOptionalListIn, "mids", "many"),
        (AnnotatedMidIn, "mid", "one"),
    ],
)
def test_mid_shapes_detected(model: type[BoundaryModel], field: str, kind: str) -> None:
    spec = _tool_for(model)  # W3, no count_targets: would be refused if the mids were missed
    assert spec.mid_fields == ((field, kind),)
    if kind == "one":
        inp = model.model_validate({field: MID_A})
        assert spec.target_mids(inp) == [MID_A]
        assert spec.target_count(inp) == 1
    else:
        inp = model.model_validate({field: [MID_B, MID_A]})
        assert sorted(spec.target_mids(inp)) == [MID_A, MID_B]
        assert spec.target_count(inp) == 2


def test_optional_mid_list_none_targets_nothing() -> None:
    spec = _tool_for(OptionalListIn)
    assert spec.target_mids(OptionalListIn()) == []
    assert spec.target_count(OptionalListIn()) == 0


def test_set_mids_in_deterministic_order() -> None:
    spec = _tool_for(SetIn)
    assert spec.target_mids(SetIn(mids={MID_B, MID_A})) == [MID_A, MID_B]


@pytest.mark.parametrize("model", [NestedItemsIn, DictIn, MixedUnionIn])
def test_undetected_mid_shape_needs_count_targets(model: type[BoundaryModel]) -> None:
    with pytest.raises(ToolDefinitionError, match="count_targets"):
        _tool_for(model)
    with pytest.raises(ToolDefinitionError, match="count_targets"):
        _tool_for(model, tier=Tier.R0, effects={Effect.READ_INDEX})  # any tier
    spec = _tool_for(model, count_targets=lambda i: 1)
    assert spec.mid_fields == ()


def test_type_alias_mid_list_detected_and_counted() -> None:
    spec = _tool_for(AliasNextToMidIn)
    assert spec.mid_fields == (("mid", "one"), ("more", "many"))
    more = [f"{i:064x}" for i in range(30)]
    inp = AliasNextToMidIn(mid=MID_A, more=more)
    assert spec.target_mids(inp) == [MID_A, *more]
    assert spec.target_count(inp) == 31


@pytest.mark.parametrize(
    "model",
    [
        TypeAliasModelNextToMidIn,
        GenericNextToMidIn,
        TypedDictNextToMidIn,
        DataclassNextToMidIn,
        EquivalentAliasNextToMidIn,
        FieldPatternNextToMidIn,
    ],
)
def test_uncountable_mids_next_to_detected_mid_need_count_targets(
    model: type[BoundaryModel],
) -> None:
    with pytest.raises(ToolDefinitionError, match="count_targets"):
        _tool_for(model)
    with pytest.raises(ToolDefinitionError, match="count_targets"):
        _tool_for(model, tier=Tier.R0, effects={Effect.READ_INDEX})  # any tier
    spec = _tool_for(model, count_targets=lambda i: 7)
    assert spec.mid_fields == (("mid", "one"),)


@pytest.mark.parametrize(
    ("tier", "effects"),
    [
        (Tier.W1, {Effect.DRAFTS}),
        (Tier.R0, {Effect.CONTACTS_WRITE}),
        (Tier.W2, {Effect.READ_INDEX}),  # write tier from the declaration alone
    ],
)
def test_write_tool_without_targets_needs_count_targets(tier: Tier, effects: set[Effect]) -> None:
    with pytest.raises(ToolDefinitionError, match="write-tier"):
        _tool_for(NoMidIn, tier=tier, effects=effects)
    spec = _tool_for(NoMidIn, tier=tier, effects=effects, count_targets=lambda i: 1)
    assert spec.target_count(NoMidIn(label="x")) == 1


def test_read_tool_without_targets_ok() -> None:
    spec = _tool_for(NoMidIn, tier=Tier.R1, effects={Effect.READ_LIVE})
    assert spec.mid_fields == ()
    assert spec.target_count(NoMidIn(label="x")) == 0


def test_count_targets() -> None:
    spec = _make(count_targets=lambda i: len(i.mids) * 10)
    assert spec.target_count(In(mids=[MID_A, MID_B])) == 20
    assert spec.meta("plugin").counts_targets is True
    assert _make().meta("plugin").counts_targets is False


def test_count_targets_cannot_lower_detected_mids() -> None:
    # Merge review: a declared count_targets is floored at the distinct detected mids, so a
    # plugin cannot under-report to dodge 06b's count escalation (> 20 targets).
    spec = _make(count_targets=lambda i: 0)
    mids = [f"{i:064x}" for i in range(21)]
    assert spec.target_count(In(mids=mids)) == 21
    assert spec.target_count(In(mids=[MID_A, MID_A])) == 1  # distinct mids
    assert _make(count_targets=lambda i: 1).target_count(In(mids=[MID_A, MID_B])) == 2


def test_count_targets_bad_return_is_loud() -> None:
    spec = _make(count_targets=lambda i: "3")
    with pytest.raises(TypeError, match="count_targets"):
        spec.target_count(In(mids=[MID_A]))


def test_recipients_declared() -> None:
    spec = _make(effects={Effect.DRAFTS}, recipients=lambda i: [*i.to, *i.cc])
    meta = spec.meta("plugin")
    assert meta.declares_recipients is True
    inp = In(mids=[MID_A], to=["Ann@Example.com"], cc=["bob@example.COM"])
    assert spec.recipient_addresses(inp) == ["ann@example.com", "bob@example.com"]


def test_recipients_not_declared() -> None:
    spec = _make()
    assert spec.meta("plugin").declares_recipients is False
    assert spec.recipient_addresses(In(mids=[MID_A], to=["a@example.com"])) is None


def test_recipients_bad_return_is_loud() -> None:
    spec = _make(recipients=lambda i: "a@example.com")
    with pytest.raises(TypeError, match="recipients"):
        spec.recipient_addresses(In(mids=[MID_A]))


# ---- ToolMeta ----------------------------------------------------------------------------------


def test_meta_document() -> None:
    spec = _make(tier=Tier.R0, effects={Effect.READ_LIVE, Effect.READ_INDEX})
    meta = spec.meta("builtin")
    assert meta.contract == "tern-tool/1"
    assert meta.name == "set_things"
    assert meta.description == "Set things on messages."
    assert meta.effects == [Effect.READ_INDEX, Effect.READ_LIVE]  # sorted
    assert meta.untrusted_output is False
    assert meta.origin == "builtin"
    assert meta.input_schema == In.model_json_schema()
    assert meta.output_schema == Out.model_json_schema()
    dumped = meta.model_dump(mode="json")
    assert dumped["effects"] == ["read_index", "read_live"]
    assert dumped["tier"] == "R0" and dumped["effective_tier"] == "R1"
    assert ToolMeta.model_validate(dumped) == meta
    assert ToolMeta.model_json_schema()["properties"]["effects"]["uniqueItems"] is True
    assert set(ToolMeta.model_json_schema()["properties"]) == {
        "contract",
        "name",
        "description",
        "tier",
        "effective_tier",
        "effects",
        "untrusted_output",
        "counts_targets",
        "declares_recipients",
        "input_schema",
        "output_schema",
        "origin",
    }


# ---- definition errors (loud at decoration time) -----------------------------------------------


@pytest.mark.parametrize("name", ["", "Set", "1abc", "set-tags", "set tags", "a" * 65, "é"])
def test_bad_name(name: str) -> None:
    with pytest.raises(ToolDefinitionError, match="name"):
        _make(name=name)


def test_name_limits_accepted() -> None:
    assert _make(name="a").name == "a"
    assert _make(name="a" + "_1" * 31 + "b").name.startswith("a_1")


def test_missing_docstring() -> None:
    with pytest.raises(ToolDefinitionError, match="docstring"):

        @tool(name="t", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
        async def t(ctx: ToolContext, inp: In) -> Out:
            raise AssertionError("never run")


def test_empty_docstring() -> None:
    with pytest.raises(ToolDefinitionError, match="docstring"):

        @tool(name="t", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
        async def t(ctx: ToolContext, inp: In) -> Out:
            """ """
            raise AssertionError("never run")


def test_not_async() -> None:
    with pytest.raises(ToolDefinitionError, match="async"):

        @tool(name="t", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)  # pyright: ignore[reportArgumentType]  # the point
        def t(ctx: ToolContext, inp: In) -> Out:  # the point: a sync def
            """Doc."""
            raise AssertionError("never run")


def test_input_not_boundary_model() -> None:
    with pytest.raises(ToolDefinitionError, match="In must be a BoundaryModel"):

        @tool(name="t", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
        async def t(ctx: ToolContext, inp: PlainIn) -> Out:
            """Doc."""
            raise AssertionError("never run")


def test_output_not_boundary_model() -> None:
    with pytest.raises(ToolDefinitionError, match="Out must be a BoundaryModel"):

        @tool(name="t", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)  # pyright: ignore[reportArgumentType]  # the point
        async def t(ctx: ToolContext, inp: In) -> dict:  # the point: Out is not a model
            """Doc."""
            raise AssertionError("never run")


def test_output_not_object_model() -> None:
    with pytest.raises(ToolDefinitionError, match="Out must be an object model"):

        @tool(name="t", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
        async def t(ctx: ToolContext, inp: In) -> NotObjectOut:
            """Doc."""
            raise AssertionError("never run")


@pytest.mark.parametrize(
    ("inp", "out", "match"),
    [
        (ExtraAllowIn, Out, "In must keep extra='forbid'"),
        (EchoingIn, Out, "In must keep hide_input_in_errors=True"),
        (In, EchoingOut, "Out must keep hide_input_in_errors=True"),
    ],
)
def test_model_config_override_refused(inp: type, out: type, match: str) -> None:
    with pytest.raises(ToolDefinitionError, match=match):
        _tool_for(inp, out)


def test_missing_annotations() -> None:
    with pytest.raises(ToolDefinitionError, match="input parameter"):

        @tool(name="t", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
        async def t(ctx, inp) -> Out:  # type: ignore[no-untyped-def]  # the point
            """Doc."""
            raise AssertionError("never run")

    with pytest.raises(ToolDefinitionError, match="return annotation"):

        @tool(name="u", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
        async def u(ctx: ToolContext, inp: In):  # type: ignore[no-untyped-def]  # the point
            """Doc."""
            raise AssertionError("never run")


def test_unresolvable_annotation() -> None:
    async def t(ctx: ToolContext, inp: In) -> Out:
        """Doc."""
        raise AssertionError("never run")

    t.__annotations__["inp"] = "NoSuchModel"  # a name that does not exist in this module
    with pytest.raises(ToolDefinitionError, match="cannot be resolved"):
        tool(name="t", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)(t)


def test_wrong_arity() -> None:
    with pytest.raises(ToolDefinitionError, match="signature"):

        @tool(name="t", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)  # pyright: ignore[reportArgumentType]  # the point
        async def t(inp: In) -> Out:  # the point: no ctx
            """Doc."""
            raise AssertionError("never run")


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"tier": "R0"}, "tier"),
        ({"effects": {"read_index"}}, "effect"),
        ({"effects": {"network"}}, "effect"),
        ({"untrusted_output": 1}, "untrusted_output"),
        ({"count_targets": 3}, "count_targets"),
        ({"recipients": ["a@example.com"]}, "recipients"),
    ],
)
def test_bad_decorator_arguments(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ToolDefinitionError, match=match):
        _make(**overrides)


@pytest.mark.parametrize(
    "effects", [["read_live", "read_index"], ["read_index", "read_live", "read_index"]]
)
def test_meta_effects_must_be_sorted_and_unique(effects: list[str]) -> None:
    dumped = (
        _make(tier=Tier.R0, effects={Effect.READ_INDEX}).meta("builtin").model_dump(mode="json")
    )
    dumped["effects"] = effects
    with pytest.raises(ValidationError, match="sorted and without duplicates"):
        ToolMeta.model_validate(dumped)
