"""ToolContext and ScopedBridge (plan 06a step 1); search over the index and
``ToolContext.targets`` (plan 06b step 0)."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm

from tern import paths
from tern.bridge.protocol import (
    METHODS,
    ArchiveParams,
    BridgeError,
    DeleteParams,
    Empty,
    MoveParams,
    TbIdParams,
    UpdateParams,
)
from tern.config import TernConfig
from tern.index.store import Store
from tern.perms.audit import AuditLog, TargetState, audit_path
from tern.search.service import SearchService
from tern.testing.fake_bridge import FakeBridge
from tern.tools import (
    BoundaryModel,
    BridgeScopeError,
    Effect,
    ScopedBridge,
    Tier,
    ToolContext,
    ToolContextError,
    tool,
)
from tern.tools.context import EFFECT_METHODS, NO_TARGETS, ResolvedLocation, allowed_methods
from tern.tools.registry import ToolRuntime


class In(BoundaryModel):
    q: str


class Out(BoundaryModel):
    n: int


@tool(name="r0_tool", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def r0_tool(ctx: ToolContext, inp: In) -> Out:
    """An index-only tool."""
    raise AssertionError("never run in this test")


@tool(name="r1_tool", tier=Tier.R1, effects={Effect.READ_LIVE}, untrusted_output=False)
async def r1_tool(ctx: ToolContext, inp: In) -> Out:
    """A live-read tool."""
    raise AssertionError("never run in this test")


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.db"
    Store.open(path).close()  # creates the schema
    return path


@pytest.fixture
def ro_store(index_path: Path) -> Iterator[Store]:
    store = Store.open(index_path, readonly=True)
    yield store
    store.close()


def _ctx(store: Store, bridge: ScopedBridge) -> ToolContext:
    return ToolContext(
        index=store,
        search=SearchService(store, None, mode="fts"),
        bridge=bridge,
        log=logging.getLogger("tern.tools.test"),
        config=TernConfig(),
        clock=time.time,
    )


def test_readonly_store_accepted(ro_store: Store) -> None:
    ctx = _ctx(ro_store, ScopedBridge(None, r0_tool.effects, tool=r0_tool.name))
    assert ctx.index is ro_store


def test_writable_store_refused(index_path: Path) -> None:
    store = Store.open(index_path)
    try:
        with pytest.raises(ToolContextError, match="readonly"):
            _ctx(store, ScopedBridge(None, r0_tool.effects, tool=r0_tool.name))
    finally:
        store.close()


def test_raw_requester_refused_as_bridge(ro_store: Store) -> None:
    fb = FakeBridge(tfm.FIXTURE)
    with pytest.raises(ToolContextError, match="ScopedBridge"):
        _ctx(ro_store, fb)  # type: ignore[arg-type]  # the point: an unscoped bridge is refused


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("search", object(), "SearchService"),
        ("log", "tern.tools.test", "logging.Logger"),
        ("config", {}, "TernConfig"),
        ("clock", 0.0, "clock"),
    ],
)
def test_wrong_part_types_refused(ro_store: Store, field: str, value: object, match: str) -> None:
    parts: dict[str, object] = {
        "index": ro_store,
        "search": SearchService(ro_store, None, mode="fts"),
        "bridge": ScopedBridge(None, r0_tool.effects, tool=r0_tool.name),
        "log": logging.getLogger("tern.tools.test"),
        "config": TernConfig(),
        "clock": time.time,
    }
    parts[field] = value
    with pytest.raises(ToolContextError, match=match):
        ToolContext(**parts)  # type: ignore[arg-type]  # the point: wrong part types


@pytest.mark.parametrize("state_dir", [Path("relative/state"), "/abs/but/a/str"])
def test_state_dir_must_be_an_absolute_path(ro_store: Store, state_dir: object) -> None:
    parts: dict[str, object] = {
        "index": ro_store,
        "search": SearchService(ro_store, None, mode="fts"),
        "bridge": ScopedBridge(None, r0_tool.effects, tool=r0_tool.name),
        "log": logging.getLogger("tern.tools.test"),
        "config": TernConfig(),
        "clock": time.time,
        "state_dir": state_dir,
    }
    with pytest.raises(ToolContextError, match="state_dir"):
        ToolContext(**parts)  # type: ignore[arg-type]  # the point: a bad state_dir
    assert _ctx(ro_store, ScopedBridge(None, (), tool="t")).state_dir is None


def test_context_is_frozen(ro_store: Store) -> None:
    ctx = _ctx(ro_store, ScopedBridge(None, r0_tool.effects, tool=r0_tool.name))
    with pytest.raises(AttributeError):
        ctx.index = ro_store  # type: ignore[misc]  # the point: frozen


@pytest.mark.asyncio
async def test_r0_tool_cannot_reach_bridge(ro_store: Store) -> None:
    fb = FakeBridge(tfm.FIXTURE)
    ctx = _ctx(ro_store, ScopedBridge(fb, r0_tool.effects, tool=r0_tool.name))
    with pytest.raises(BridgeScopeError, match="mail.open"):
        await ctx.bridge.request("mail.open", TbIdParams(tb_id=1))
    with pytest.raises(BridgeScopeError):
        await ctx.bridge.request("mail.accounts.list", Empty())
    assert fb.calls == []


@pytest.mark.asyncio
async def test_r1_tool_reaches_read_methods_only(ro_store: Store) -> None:
    fb = FakeBridge(tfm.FIXTURE)
    ctx = _ctx(ro_store, ScopedBridge(fb, r1_tool.effects, tool=r1_tool.name))
    await ctx.bridge.request("mail.accounts.list", Empty())
    assert [m for m, _ in fb.calls] == ["mail.accounts.list"]
    with pytest.raises(BridgeScopeError, match="mail.delete"):
        await ctx.bridge.request("mail.delete", Empty())
    with pytest.raises(BridgeScopeError, match="mail.update"):
        await ctx.bridge.request("mail.update", TbIdParams(tb_id=1))
    assert len(fb.calls) == 1


@pytest.mark.asyncio
async def test_no_extension_is_unavailable() -> None:
    bridge = ScopedBridge(None, {Effect.READ_LIVE}, tool="r1_tool")
    with pytest.raises(BridgeError) as exc:
        await bridge.request("mail.accounts.list", Empty())
    assert exc.value.name == "unavailable"


@pytest.mark.asyncio
async def test_scope_checked_before_availability() -> None:
    bridge = ScopedBridge(None, {Effect.READ_INDEX}, tool="r0_tool")
    with pytest.raises(BridgeScopeError):
        await bridge.request("mail.accounts.list", Empty())


def test_effect_method_mapping() -> None:
    assert set(EFFECT_METHODS) == set(Effect)
    assert EFFECT_METHODS[Effect.READ_INDEX] == frozenset()
    assert EFFECT_METHODS[Effect.READ_LIVE] == {
        "mail.accounts.list",
        "mail.folders.list",
        "mail.messages.find",
        "mail.messages.full",
        "mail.messages.raw",
        "mail.open",
        "mail.tags.list",
    }
    # paging is the indexer's, never a tool's; a raw fetch is read_attachment's only (R1)
    assert "mail.messages.page" not in allowed_methods(Effect)
    for method in allowed_methods(Effect):
        spec = METHODS[method]
        assert spec.kind == "request" and spec.direction == "d2e", method
    assert allowed_methods({Effect.READ_LIVE, Effect.MAIL_MOVE}) == (
        EFFECT_METHODS[Effect.READ_LIVE] | {"mail.move", "mail.archive"}
    )


# ---- plan 06b step 0: search over the index, ToolContext.targets ------------------------------

MID_A = "a" * 64
MID_X = "x-" + "b" * 64
PLANTED_TB_ID = 987_654_321


def _loc(folder: str = "f-inbox", tb_id: int = PLANTED_TB_ID) -> ResolvedLocation:
    return ResolvedLocation(
        folder_id=folder,
        tb_id=tb_id,
        state=TargetState(folder_id=folder, tags=["$label1"], flagged=False, read=True),
    )


def _parts(store: Store, **override: Any) -> dict[str, Any]:
    parts: dict[str, Any] = {
        "index": store,
        "search": SearchService(store, None, mode="fts"),
        "bridge": ScopedBridge(None, r0_tool.effects, tool=r0_tool.name),
        "log": logging.getLogger("tern.tools.test"),
        "config": TernConfig(),
        "clock": time.time,
    }
    parts.update(override)
    return parts


def test_search_over_another_store_refused(ro_store: Store, index_path: Path) -> None:
    # Same file, another connection: still not the context's index.
    other = Store.open(index_path, readonly=True)
    try:
        with pytest.raises(ToolContextError, match=r"search must search ToolContext\.index"):
            ToolContext(**_parts(ro_store, search=SearchService(other, None, mode="fts")))
    finally:
        other.close()


def test_runtime_refuses_search_over_another_store(ro_store: Store, index_path: Path) -> None:
    other = Store.open(index_path, readonly=True)
    try:
        with AuditLog.open(audit_path(paths.resolve())) as audit:
            with pytest.raises(ToolContextError, match=r"search must search ToolContext\.index"):
                ToolRuntime(
                    audit=audit,
                    index=ro_store,
                    search=SearchService(other, None, mode="fts"),
                    bridge=None,
                    config=TernConfig(),
                    log=logging.getLogger("tern.tools.test"),
                    clock=time.time,
                )
    finally:
        other.close()


def test_targets_default_to_an_empty_read_only_mapping(ro_store: Store) -> None:
    ctx = _ctx(ro_store, ScopedBridge(None, r0_tool.effects, tool=r0_tool.name))
    assert ctx.targets == {}
    assert ctx.targets is NO_TARGETS
    with pytest.raises(TypeError):
        ctx.targets[MID_A] = (_loc(),)  # type: ignore[index]  # the point: read-only


def test_targets_are_a_read_only_copy(ro_store: Store) -> None:
    source = {MID_A: (_loc("f-inbox"), _loc("f-archive", 5)), MID_X: (_loc(),)}
    ctx = ToolContext(**_parts(ro_store, targets=source))
    source.pop(MID_X)
    assert set(ctx.targets) == {MID_A, MID_X}
    assert [loc.folder_id for loc in ctx.targets[MID_A]] == ["f-inbox", "f-archive"]
    with pytest.raises(TypeError):
        ctx.targets[MID_A] = ()  # type: ignore[index]  # the point: read-only


@pytest.mark.parametrize(
    "targets",
    [
        [(MID_A, (_loc(),))],  # not a Mapping
        {"not-a-mid": (_loc(),)},
        {MID_A.upper(): (_loc(),)},
        {MID_A: ()},  # a target with no location
        {MID_A: [_loc()]},  # not a tuple
        {MID_A: ("f-inbox",)},  # not a ResolvedLocation
    ],
)
def test_malformed_targets_refused(ro_store: Store, targets: object) -> None:
    with pytest.raises(ToolContextError, match="targets"):
        ToolContext(**_parts(ro_store, targets=targets))


def test_resolved_location_keeps_tb_id_out_of_repr(ro_store: Store) -> None:
    loc = _loc()
    assert loc.tb_id == PLANTED_TB_ID
    ctx = ToolContext(**_parts(ro_store, targets={MID_A: (loc,)}))
    assert str(PLANTED_TB_ID) not in repr(loc)
    assert str(PLANTED_TB_ID) not in repr(ctx.targets)
    with pytest.raises(AttributeError):
        loc.tb_id = 1  # type: ignore[misc]  # the point: frozen


@pytest.mark.parametrize(
    ("folder", "tb_id", "state", "match"),
    [
        ("", 1, None, "folder_id"),
        ("f-inbox", -1, None, "tb_id"),
        ("f-inbox", True, None, "tb_id"),
        ("f-inbox", "1", None, "tb_id"),
        ("f-inbox", 1, {"folder_id": "f-inbox", "flagged": False, "read": True}, "TargetState"),
        (
            "f-inbox",
            1,
            TargetState(folder_id="f-other", tags=[], flagged=False, read=True),
            "another folder",
        ),
    ],
)
def test_resolved_location_refuses_bad_parts(
    folder: object, tb_id: object, state: object, match: str
) -> None:
    if state is None:
        state = TargetState(folder_id="f-inbox", tags=[], flagged=False, read=True)
    with pytest.raises(ToolContextError, match=match):
        ResolvedLocation(folder_id=folder, tb_id=tb_id, state=state)  # type: ignore[arg-type]  # bad parts


# ---- plan 06b step 0 review fixes (round 1) ---------------------------------------------------


def _state(folder: str = "f-inbox") -> TargetState:
    return TargetState(folder_id=folder, tags=["$label1"], flagged=False, read=True)


def _tamper(loc: ResolvedLocation) -> None:
    loc.state.folder_id = "f-other"
    loc.state.tags.append("$label2")
    loc.state.flagged = True


def test_resolved_location_keeps_a_private_copy_of_its_state() -> None:
    engine_state = _state()
    loc = ResolvedLocation(folder_id="f-inbox", tb_id=1, state=engine_state)
    _tamper(loc)
    assert engine_state == _state()  # the engine's record is unchanged


def test_tool_changing_targets_state_leaves_the_engine_record_alone(ro_store: Store) -> None:
    engine_loc = ResolvedLocation(folder_id="f-inbox", tb_id=1, state=_state())
    ctx = ToolContext(**_parts(ro_store, targets={MID_A: (engine_loc,)}))
    seen = ctx.targets[MID_A][0]
    assert seen is not engine_loc
    assert seen == engine_loc
    _tamper(seen)
    assert engine_loc.state == _state()  # what step 3 builds the audit/undo rows from


@pytest.mark.parametrize(
    "key",
    [
        MID_A,
        MID_X,
        MID_A + "\n",  # `$` in a Python regex would accept this
        MID_A[:-1],
        MID_A + "a",
        MID_A.upper(),
        "X-" + "b" * 64,
        "y-" + "b" * 64,
        " " + MID_A,
    ],
)
def test_targets_keys_agree_with_the_bridge_mid_type(ro_store: Store, key: str) -> None:
    from pydantic import BaseModel, ValidationError

    from tern.bridge.protocol import Mid

    class _M(BaseModel):
        mid: Mid

    try:
        _M(mid=key)
    except ValidationError:
        with pytest.raises(ToolContextError, match="keys must be mids"):
            ToolContext(**_parts(ro_store, targets={key: (_loc(),)}))
    else:
        assert set(ToolContext(**_parts(ro_store, targets={key: (_loc(),)})).targets) == {key}


# ---- plan 06b step 3 review fix: message writes only to the call's targets ----------------------


class _Recorder:
    """An ``ExtensionRequester`` that records what reaches it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def request(self, method: str, params: Any) -> Any:
        self.sent.append((method, params))
        return None

    async def notify(self, method: str, params: Any) -> None:
        raise AssertionError("no notifications")


WRITES = [
    (Effect.MAIL_META, "mail.update", lambda ids: UpdateParams(tb_ids=ids, add_tags=["$label1"])),
    (Effect.MAIL_MOVE, "mail.move", lambda ids: MoveParams(tb_ids=ids, dest_folder_id="a:/X")),
    (Effect.MAIL_MOVE, "mail.archive", lambda ids: ArchiveParams(tb_ids=ids)),
    (Effect.MAIL_DELETE, "mail.delete", lambda ids: DeleteParams(tb_ids=ids)),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("effect", "method", "make"), WRITES)
async def test_a_write_may_name_only_target_tb_ids(effect: Effect, method: str, make: Any) -> None:
    rec = _Recorder()
    bridge = ScopedBridge(rec, {effect}, tool="w", target_tb_ids={3, 5})
    await bridge.request(method, make([5, 3]))
    await bridge.request(method, make([3]))
    for outside in ([3, 4], [7]):
        with pytest.raises(BridgeScopeError, match="did not resolve"):
            await bridge.request(method, make(outside))
    assert [m for m, _ in rec.sent] == [method, method]


@pytest.mark.asyncio
@pytest.mark.parametrize(("effect", "method", "make"), WRITES)
async def test_a_write_without_targets_is_refused(effect: Effect, method: str, make: Any) -> None:
    rec = _Recorder()
    bridge = ScopedBridge(rec, {effect}, tool="w")
    with pytest.raises(BridgeScopeError, match="did not resolve"):
        await bridge.request(method, make([1]))
    assert rec.sent == []


@pytest.mark.asyncio
async def test_a_write_must_be_sent_as_its_params_model() -> None:
    class Sneaky(UpdateParams):
        pass

    rec = _Recorder()
    bridge = ScopedBridge(rec, {Effect.MAIL_META}, tool="w", target_tb_ids={1})
    with pytest.raises(BridgeScopeError, match="must send mail.update as UpdateParams"):
        await bridge.request("mail.update", Sneaky(tb_ids=[1]))
    with pytest.raises(BridgeScopeError, match="must send mail.update as UpdateParams"):
        await bridge.request("mail.update", Empty())
    assert rec.sent == []


@pytest.mark.asyncio
async def test_the_checked_copy_is_what_is_sent() -> None:
    rec = _Recorder()
    bridge = ScopedBridge(rec, {Effect.MAIL_META}, tool="w", target_tb_ids={1})
    params = UpdateParams(tb_ids=[1], add_tags=["$label1"])
    await bridge.request("mail.update", params)
    ((method, sent),) = rec.sent
    assert method == "mail.update" and sent is not params and sent == params


@pytest.mark.parametrize(
    "effects",
    [
        {Effect.MAIL_META},
        {Effect.READ_LIVE, Effect.MAIL_META},
        {Effect.READ_LIVE, Effect.MAIL_MOVE},
        {Effect.READ_LIVE, Effect.MAIL_DELETE},
    ],
)
def test_a_message_write_tool_has_no_find(effects: set[Effect]) -> None:
    bridge = ScopedBridge(None, effects, tool="w")
    assert bridge.allowed == allowed_methods(effects) - {"mail.messages.find"}


@pytest.mark.parametrize("effects", [{Effect.READ_LIVE}, {Effect.READ_LIVE, Effect.DRAFTS}])
def test_other_tools_keep_find(effects: set[Effect]) -> None:
    assert ScopedBridge(None, effects, tool="r").allowed == allowed_methods(effects)
