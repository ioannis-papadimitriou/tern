"""Plan 06b step 3: target resolution and the W1 execution path.

The engine resolves a W call's target mids from the (fixture) index, refusing unknown, ``x-``,
conflicted and ambiguous targets with a ``decision=error`` row; then finds them live through the
runtime's bridge (``ctx.targets``, the prev state); a W1 call then gets its write-ahead row
(``auto``/``tb_accepted``), runs, and the engine finds the targets again for the undo token. No
``tb_id`` reaches an audit row, event, undo payload or log record."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tern.bridge.protocol import (
    BridgeError,
    ErrorName,
    FindParams,
    Mid,
    TbIdParams,
    UpdateParams,
)
from tern.index.store import Store
from tern.perms.audit import AuditLog, TargetState
from tern.perms.engine import Engine
from tern.perms.targets import (
    FOLDER_FIELDS,
    TargetRefused,
    find_live,
    named_folder,
    pair_states,
    post_state,
    resolve_index,
)
from tern.testing.fake_bridge import FakeBridge
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, ToolError, tool
from tern.tools.context import ResolvedLocation
from tern.tools.registry import Registry

from .conftest import FIXTURE, Clock, Recipients, mids

MANIFEST = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
BY_KEY = {m["key"]: m for m in MANIFEST["messages"]}
DUP = BY_KEY["dup-1"]["mid"]  # acct2/INBOX + acct2/Projects, byte-identical
DUP_HMID = BY_KEY["dup-1"]["message_id"].strip("<>")
SINGLE_HMID = BY_KEY["inv-0042"]["message_id"].strip("<>")
CONFLICTED = BY_KEY[MANIFEST["adversarial_pair"]["key"]]["mid"]  # inv-0077, D1
SINGLE = BY_KEY["inv-0042"]["mid"]  # acct1/Invoices only
NOT_INDEXED = mids(1)[0]
DUP_FOLDERS = ("acct2:/INBOX", "acct2:/Projects")
PLANTED = 731_904  # FakeBridge(first_tb_id=PLANTED): distinctive tb_ids to look for

SEEN: list[Mapping[str, tuple[ResolvedLocation, ...]]] = []
RAN: list[str] = []
#: tb_ids a test plants for ``tag_outside`` to write to besides its own targets.
OUTSIDE: list[int] = []


class MidsIn(BoundaryModel):
    mids: list[Mid]


class TagIn(BoundaryModel):
    mids: list[Mid]
    folder_id: str | None = None  # ignored for a tag tool: it acts on every location


class MoveIn(BoundaryModel):
    mids: list[Mid]
    dest_folder_id: str
    source_folder_id: str | None = None


class DeleteIn(BoundaryModel):
    mids: list[Mid]
    folder_id: str | None = None


class CreateTagIn(BoundaryModel):
    tag: str


class Out(BoundaryModel):
    n: int


@tool(name="tag", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def tag(ctx: ToolContext, inp: TagIn) -> Out:
    """A set_tags-shaped W1 dummy: adds $label1 on every engine-resolved location."""
    RAN.append("tag")
    SEEN.append(ctx.targets)
    tb_ids = [loc.tb_id for locs in ctx.targets.values() for loc in locs]
    await ctx.bridge.request("mail.update", UpdateParams(tb_ids=tb_ids, add_tags=["$label1"]))
    return Out(n=len(tb_ids))


@tool(name="tag_fails", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def tag_fails(ctx: ToolContext, inp: MidsIn) -> Out:
    """A W1 dummy that fails after the engine resolved its targets."""
    RAN.append("tag_fails")
    raise RuntimeError("dummy failure")


@tool(name="tag_outside", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def tag_outside(ctx: ToolContext, inp: MidsIn) -> Out:
    """A W1 dummy that writes to its targets plus tb_ids the engine never resolved (OUTSIDE)."""
    RAN.append("tag_outside")
    tb_ids = [loc.tb_id for locs in ctx.targets.values() for loc in locs] + OUTSIDE
    await ctx.bridge.request("mail.update", UpdateParams(tb_ids=tb_ids, add_tags=["$label1"]))
    return Out(n=len(tb_ids))


@tool(
    name="tag_finder",
    tier=Tier.W1,
    effects={Effect.READ_LIVE, Effect.MAIL_META},
    untrusted_output=False,
)
async def tag_finder(ctx: ToolContext, inp: MidsIn) -> Out:
    """A W1 dummy with READ_LIVE that tries to find tb_ids itself."""
    RAN.append("tag_finder")
    await ctx.bridge.request("mail.messages.find", FindParams(header_message_id=SINGLE_HMID))
    return Out(n=0)


@tool(
    name="create_tag",
    tier=Tier.W1,
    effects={Effect.MAIL_META},
    untrusted_output=False,
    count_targets=lambda inp: 1,
)
async def create_tag(ctx: ToolContext, inp: CreateTagIn) -> Out:
    """A W1 dummy with no target mids (count_targets only)."""
    RAN.append("create_tag")
    SEEN.append(ctx.targets)
    return Out(n=len(ctx.targets))


@tool(name="move", tier=Tier.W2, effects={Effect.MAIL_MOVE}, untrusted_output=False)
async def move(ctx: ToolContext, inp: MoveIn) -> Out:
    """A move-shaped W2 dummy (resolver cases only: W2 has no execution path before step 5)."""
    raise AssertionError("never runs")


@tool(name="delete", tier=Tier.W3, effects={Effect.MAIL_DELETE}, untrusted_output=False)
async def delete(ctx: ToolContext, inp: DeleteIn) -> Out:
    """A delete-shaped W3 dummy (resolver cases only)."""
    raise AssertionError("never runs")


TOOLS = [tag, tag_fails, tag_outside, tag_finder, create_tag, move, delete]


@pytest.fixture(autouse=True)
def _reset() -> None:
    SEEN.clear()
    RAN.clear()
    OUTSIDE.clear()


@pytest.fixture
def fb() -> FakeBridge:
    return FakeBridge(FIXTURE, first_tb_id=PLANTED)


@pytest.fixture
def reg(make_registry: Any, fb: FakeBridge, fixture_store: Store) -> Registry:
    return make_registry(TOOLS, bridge=fb, index=fixture_store)


def call(reg: Registry, name: str, args: dict[str, Any]) -> Any:
    return asyncio.run(reg.call("agent:s1", name, args))


def refused(reg: Registry, name: str, args: dict[str, Any], code: str, reason: str) -> ToolError:
    with pytest.raises(ToolError) as info:
        call(reg, name, args)
    assert (info.value.code, info.value.reason) == (code, reason)
    return info.value


def methods(fb: FakeBridge) -> list[str]:
    return [m for m, _ in fb.calls]


async def live(fb: FakeBridge, hmid: str = DUP_HMID) -> dict[str, tuple[int, list[str]]]:
    got = (await fb.request("mail.messages.find", FindParams(header_message_id=hmid))).root
    return {f.folder_id: (f.tb_id, sorted(f.tags)) for f in got}


def x_mid(store: Store) -> str:
    row = store.connection.execute(
        "SELECT mid FROM messages WHERE mid LIKE 'x-%' LIMIT 1"
    ).fetchone()
    assert row is not None, "the fixture has messages without a Message-ID"
    return str(row[0])


# ---- the W1 execution path ----------------------------------------------------------------------


def test_w1_tag_on_a_two_location_mid_tags_both(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    before = asyncio.run(live(fb))
    assert sorted(before) == list(DUP_FOLDERS)
    fb.calls.clear()

    res = call(reg, "tag", {"mids": [DUP]})

    assert res.output == Out(n=2) and RAN == ["tag"]
    (targets,) = SEEN
    assert list(targets) == [DUP]
    assert [(loc.folder_id, loc.tb_id) for loc in targets[DUP]] == [
        (f, before[f][0]) for f in DUP_FOLDERS
    ]
    assert methods(fb) == ["mail.messages.find", "mail.update", "mail.messages.find"]
    after = asyncio.run(live(fb))
    assert {f: tags for f, (_, tags) in after.items()} == {f: ["$label1"] for f in DUP_FOLDERS}

    rec = audit.get(res.call_id)
    assert (rec.tier, rec.decision, rec.reason, rec.initial_status) == (
        Tier.W1,
        "auto",
        None,
        "tb_accepted",
    )
    assert rec.target_mids == (DUP,) and rec.status == "tb_accepted"
    assert res.undo_token is not None and rec.undo_token == res.undo_token


def test_the_undo_payload_has_one_target_per_location(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    res = call(reg, "tag", {"mids": [DUP]})
    token = audit.undo_token(res.undo_token)
    assert token.call_id == res.call_id and not token.used
    got = [(t.mid, t.prev, t.post) for t in token.payload.targets]
    assert got == [
        (
            DUP,
            TargetState(folder_id=f, tags=[], flagged=False, read=False),
            TargetState(folder_id=f, tags=["$label1"], flagged=False, read=False),
        )
        for f in DUP_FOLDERS
    ]


def test_duplicate_target_mids_are_resolved_once(reg: Registry, fb: FakeBridge) -> None:
    res = call(reg, "tag", {"mids": [SINGLE, SINGLE]})
    assert res.output == Out(n=1)
    assert methods(fb) == ["mail.messages.find", "mail.update", "mail.messages.find"]


def test_a_w1_tool_without_target_mids_runs_without_targets_or_token(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    res = call(reg, "create_tag", {"tag": "synthetic"})
    assert RAN == ["create_tag"] and SEEN == [{}] and res.output == Out(n=0)
    assert res.undo_token is None and fb.calls == []
    rec = audit.get(res.call_id)
    assert (rec.decision, rec.initial_status, rec.undo_token) == ("auto", "tb_accepted", None)


def test_a_failing_w1_tool_gets_an_error_event_and_no_token(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    with pytest.raises(ToolError) as info:
        call(reg, "tag_fails", {"mids": [DUP]})
    assert info.value.code == "internal" and RAN == ["tag_fails"]
    (rec,) = audit.list()
    assert (rec.decision, rec.initial_status, rec.status, rec.undo_token) == (
        "auto",
        "tb_accepted",
        "error",
        None,
    )
    assert methods(fb) == ["mail.messages.find"]  # no post-state find after a failure


# ---- a W1 tool writes only to its ctx.targets (step 3 review fix) ------------------------------


def test_a_w1_tool_cannot_write_outside_its_targets(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    (single_tb_id, single_tags) = asyncio.run(live(fb, SINGLE_HMID))["acct1:/Invoices"]
    OUTSIDE.append(single_tb_id)  # a real tb_id the engine did not resolve for this call
    fb.calls.clear()

    refused(reg, "tag_outside", {"mids": [DUP]}, "internal", "bridge_scope")

    assert RAN == ["tag_outside"]
    assert methods(fb) == ["mail.messages.find"]  # the engine's live find; no mail.update
    assert asyncio.run(live(fb, SINGLE_HMID))["acct1:/Invoices"] == (single_tb_id, single_tags)
    assert {f: tags for f, (_, tags) in asyncio.run(live(fb)).items()} == {
        f: [] for f in DUP_FOLDERS
    }
    (rec,) = audit.list()
    assert (rec.target_count, rec.status, rec.undo_token) == (1, "error", None)
    (event,) = audit.events(rec.call_id)
    assert event.detail == {"code": "internal", "reason": "bridge_scope", "exc": "BridgeScopeError"}


def test_a_w1_tool_gets_no_find_even_with_read_live(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    refused(reg, "tag_finder", {"mids": [DUP]}, "internal", "bridge_scope")
    assert RAN == ["tag_finder"]
    assert methods(fb) == ["mail.messages.find"]  # the engine's own find only
    (rec,) = audit.list()
    assert (rec.status, rec.undo_token) == ("error", None)


# ---- refusals (each with its row; nothing runs) -------------------------------------------------


def assert_refused_row(audit: AuditLog, code: str) -> None:
    (rec,) = audit.list()
    assert (rec.tier, rec.decision, rec.reason, rec.initial_status) == (
        Tier.W1,
        "error",
        code,
        None,
    )
    assert rec.undo_token is None


def test_a_mid_not_in_the_index_is_unknown(reg: Registry, fb: FakeBridge, audit: AuditLog) -> None:
    err = refused(reg, "tag", {"mids": [DUP, NOT_INDEXED]}, "unknown_mid", "not_indexed")
    assert err.message == "target 2 of 2 is not in the index"
    assert RAN == [] and fb.calls == []
    assert_refused_row(audit, "unknown_mid")


def test_an_x_mid_is_unknown_without_a_message_id(
    reg: Registry, fb: FakeBridge, audit: AuditLog, fixture_store: Store
) -> None:
    refused(reg, "tag", {"mids": [x_mid(fixture_store)]}, "unknown_mid", "no_message_id")
    assert RAN == [] and fb.calls == []
    assert_refused_row(audit, "unknown_mid")


def test_a_conflicted_mid_is_refused(reg: Registry, fb: FakeBridge, audit: AuditLog) -> None:
    refused(reg, "tag", {"mids": [SINGLE, CONFLICTED]}, "mid_conflicted", "mid_conflicted")
    assert RAN == [] and fb.calls == []
    assert_refused_row(audit, "mid_conflicted")


def test_a_target_gone_from_a_resolved_folder_is_targets_changed(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    # TB moved the Projects copy away; the index still has it there.
    projects = asyncio.run(live(fb))["acct2:/Projects"][0]
    asyncio.run(fb.move([projects], "acct1:/Archive"))
    fb.calls.clear()
    err = refused(reg, "tag", {"mids": [DUP]}, "targets_changed", "not_found")
    assert err.message == "target 1 of 1 is no longer where the index has it"
    assert RAN == [] and methods(fb) == ["mail.messages.find"]  # no write
    assert_refused_row(audit, "targets_changed")


def test_no_extension_is_unavailable(
    make_registry: Any, fixture_store: Store, audit: AuditLog
) -> None:
    reg = make_registry(TOOLS, bridge=None, index=fixture_store)
    refused(reg, "tag", {"mids": [DUP]}, "unavailable", "unavailable")
    assert RAN == []
    assert_refused_row(audit, "unavailable")


class Failing:
    """Wraps a FakeBridge; ``find`` fails with ``error`` from the ``fail_from``-th find on."""

    def __init__(self, fb: FakeBridge, error: BridgeError, fail_from: int = 1) -> None:
        self.fb = fb
        self.error = error
        self.fail_from = fail_from
        self.finds = 0

    async def request(self, method: str, params: BaseModel) -> Any:
        if method == "mail.messages.find":
            self.finds += 1
            if self.finds >= self.fail_from:
                raise self.error
        return await self.fb.request(method, params)

    async def notify(self, method: str, params: BaseModel) -> None:
        await self.fb.notify(method, params)


@pytest.mark.parametrize(
    ("name", "code"),
    [("unavailable", "unavailable"), ("tb_error", "unavailable"), ("stale_id", "internal")],
)
def test_a_bridge_failure_while_finding_refuses(
    make_registry: Any,
    fb: FakeBridge,
    fixture_store: Store,
    audit: AuditLog,
    name: ErrorName,
    code: str,
) -> None:
    bridge = Failing(fb, BridgeError(name, "synthetic failure"))
    reg = make_registry(TOOLS, bridge=bridge, index=fixture_store)
    refused(reg, "tag", {"mids": [DUP]}, code, name)
    assert RAN == [] and fb.calls == []
    assert_refused_row(audit, code)


def test_a_bridge_failure_after_the_write_is_an_error_without_a_token(
    make_registry: Any, fb: FakeBridge, fixture_store: Store, audit: AuditLog
) -> None:
    bridge = Failing(fb, BridgeError("unavailable", "port lost"), fail_from=2)
    reg = make_registry(TOOLS, bridge=bridge, index=fixture_store)
    err = refused(reg, "tag", {"mids": [DUP]}, "unavailable", "post_state")
    assert "no undo token" in err.message
    assert RAN == ["tag"] and methods(fb) == ["mail.messages.find", "mail.update"]
    (rec,) = audit.list()
    assert (rec.decision, rec.initial_status, rec.status, rec.undo_token) == (
        "auto",
        "tb_accepted",
        "error",
        None,
    )
    (event,) = audit.events(rec.call_id)
    assert (event.kind, event.detail) == (
        "error",
        {"code": "unavailable", "reason": "post_state", "exc": "unavailable"},
    )


class Hanging:
    async def request(self, method: str, params: BaseModel) -> Any:
        await asyncio.sleep(3600)

    async def notify(self, method: str, params: BaseModel) -> None:
        raise AssertionError("no notifications")


def test_a_cancel_while_finding_writes_a_cancelled_row(
    make_registry: Any, fixture_store: Store, audit: AuditLog
) -> None:
    reg = make_registry(TOOLS, bridge=Hanging(), index=fixture_store)

    async def run() -> None:
        task = asyncio.create_task(reg.call("agent:s1", "tag", {"mids": [DUP]}))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert RAN == []
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.initial_status) == ("denied", "cancelled", None)


class HangOnPostFind:
    """Wraps a FakeBridge; the second find (the post state, after the write) hangs."""

    def __init__(self, fb: FakeBridge) -> None:
        self.fb = fb
        self.finds = 0
        self.hanging = asyncio.Event()

    async def request(self, method: str, params: BaseModel) -> Any:
        if method == "mail.messages.find":
            self.finds += 1
            if self.finds == 2:
                self.hanging.set()
                await asyncio.sleep(3600)
        return await self.fb.request(method, params)

    async def notify(self, method: str, params: BaseModel) -> None:
        await self.fb.notify(method, params)


def test_a_cancel_during_the_post_state_find_is_an_error_event_without_a_token(
    make_registry: Any, fb: FakeBridge, fixture_store: Store, audit: AuditLog
) -> None:
    bridge = HangOnPostFind(fb)
    reg = make_registry(TOOLS, bridge=bridge, index=fixture_store)

    async def run() -> None:
        task = asyncio.create_task(reg.call("agent:s1", "tag", {"mids": [DUP]}))
        await bridge.hanging.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert RAN == ["tag"] and methods(fb) == ["mail.messages.find", "mail.update"]
    (rec,) = audit.list()
    assert (rec.decision, rec.initial_status, rec.status, rec.undo_token) == (
        "auto",
        "tb_accepted",
        "error",
        None,
    )
    (event,) = audit.events(rec.call_id)
    assert (event.kind, event.detail) == (
        "error",
        {"code": "internal", "reason": "cancelled", "exc": "cancel"},
    )


def test_an_undo_token_audit_failure_is_an_error_event_without_a_token(
    reg: Registry, fb: FakeBridge, audit: AuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: AuditLog, call_id: str, payload: object) -> str:
        raise sqlite3.OperationalError("synthetic: disk I/O error")

    monkeypatch.setattr(AuditLog, "add_undo_token", broken)
    err = refused(reg, "tag", {"mids": [DUP]}, "internal", "audit_failed")
    assert err.message == "the write ran but its undo token could not be recorded"
    assert RAN == ["tag"]
    assert methods(fb) == ["mail.messages.find", "mail.update", "mail.messages.find"]
    (rec,) = audit.list()
    assert (rec.decision, rec.initial_status, rec.status, rec.undo_token) == (
        "auto",
        "tb_accepted",
        "error",
        None,
    )
    (event,) = audit.events(rec.call_id)
    assert (event.kind, event.detail) == (
        "error",
        {"code": "internal", "reason": "audit_failed", "exc": "OperationalError"},
    )


class DeliversACopy:
    """Wraps a FakeBridge; right after the write, a copy of message ``tb_id`` arrives in
    ``folder`` (a new delivery the write did not cause)."""

    def __init__(self, fb: FakeBridge, tb_id: int, folder: str) -> None:
        self.fb = fb
        self.tb_id = tb_id
        self.folder = folder

    async def request(self, method: str, params: BaseModel) -> Any:
        out = await self.fb.request(method, params)
        if method == "mail.update":
            raw = await self.fb.request("mail.messages.raw", TbIdParams(tb_id=self.tb_id))
            await self.fb.deliver(self.folder, base64.b64decode(raw.raw_b64))
        return out

    async def notify(self, method: str, params: BaseModel) -> None:
        await self.fb.notify(method, params)


def test_a_copy_arriving_during_an_in_place_write_is_not_an_undo_target(
    make_registry: Any, fb: FakeBridge, fixture_store: Store, audit: AuditLog
) -> None:
    inbox_tb_id = asyncio.run(live(fb))["acct2:/INBOX"][0]
    reg = make_registry(
        TOOLS, bridge=DeliversACopy(fb, inbox_tb_id, "acct1:/INBOX"), index=fixture_store
    )
    res = call(reg, "tag", {"mids": [DUP]})
    assert sorted(asyncio.run(live(fb))) == ["acct1:/INBOX", *DUP_FOLDERS]  # the copy arrived
    token = audit.undo_token(res.undo_token)
    got = [(t.prev, t.post) for t in token.payload.targets]
    assert got == [
        (
            TargetState(folder_id=f, tags=[], flagged=False, read=False),
            TargetState(folder_id=f, tags=["$label1"], flagged=False, read=False),
        )
        for f in DUP_FOLDERS
    ]


# ---- no tb_id anywhere --------------------------------------------------------------------------


def test_planted_tb_ids_reach_no_row_event_payload_or_log(
    make_registry: Any,
    fb: FakeBridge,
    fixture_store: Store,
    audit: AuditLog,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    planted = {str(t) for t, _ in asyncio.run(live(fb)).values()}
    assert all(int(t) >= PLANTED for t in planted)
    reg = make_registry(TOOLS, bridge=fb, index=fixture_store)
    ok = call(reg, "tag", {"mids": [DUP]})
    # A refusal after a live find, and a post-state failure, too.
    projects = asyncio.run(live(fb))["acct2:/Projects"][0]
    planted |= {str(t) for t, _ in asyncio.run(live(fb)).values()}
    asyncio.run(fb.move([projects], "acct1:/Archive"))
    planted |= {str(t) for t, _ in asyncio.run(live(fb)).values()}
    with pytest.raises(ToolError) as changed:
        call(reg, "tag", {"mids": [DUP]})
    failing = make_registry(
        TOOLS, bridge=Failing(fb, BridgeError("unavailable", "x"), 2), index=fixture_store
    )
    with pytest.raises(ToolError) as post:
        call(failing, "tag", {"mids": [SINGLE]})
    planted |= {str(t) for _, p in fb.calls if "tb_ids" in p for t in p["tb_ids"]}

    dump = "\n".join(sqlite3.connect(audit.path).iterdump())
    texts = {
        "audit.db": dump,
        "events": repr([audit.events(r.call_id) for r in audit.list()]),
        "undo payload": audit.undo_token(ok.undo_token).payload.model_dump_json(),
        "result": repr(ok),
        "errors": f"{changed.value!r} {changed.value.message} {post.value!r} {post.value.message}",
        "ctx.targets repr": repr(SEEN),
        "logs": "\n".join(f"{r.getMessage()} {r.__dict__!r}" for r in caplog.records),
    }
    assert len(audit.list()) == 3 and caplog.records
    for where, text in texts.items():
        leaked = sorted(t for t in planted if t in text)
        assert leaked == [], f"tb_id in {where}"


# ---- the resolver (index stage) -----------------------------------------------------------------


def resolve(store: Store, spec: Any, args: dict[str, Any]) -> Any:
    inp = spec.input_model.model_validate(args)
    return resolve_index(store, spec, inp, spec.target_mids(inp))


def test_a_move_on_a_multi_location_mid_needs_a_folder(fixture_store: Store) -> None:
    with pytest.raises(TargetRefused) as info:
        resolve(fixture_store, move, {"mids": [DUP], "dest_folder_id": "acct1:/Archive"})
    assert (info.value.code, info.value.reason) == ("ambiguous_location", "ambiguous_location")
    # M7: a location count only, never the folders.
    assert info.value.message == (
        "target 1 of 1 is in 2 folders; name one with source_folder_id or folder_id"
    )
    assert "acct2" not in str(info.value)


def test_a_named_folder_resolves_a_move_or_delete_to_it(fixture_store: Store) -> None:
    (t,) = resolve(
        fixture_store,
        move,
        {"mids": [DUP], "dest_folder_id": "acct1:/Archive", "source_folder_id": "acct2:/Projects"},
    )
    assert (t.mid, t.folder_ids, t.header_message_id) == (DUP, ("acct2:/Projects",), DUP_HMID)
    (t,) = resolve(fixture_store, delete, {"mids": [DUP], "folder_id": "acct2:/INBOX"})
    assert t.folder_ids == ("acct2:/INBOX",)
    with pytest.raises(TargetRefused) as info:
        resolve(fixture_store, delete, {"mids": [DUP]})
    assert info.value.code == "ambiguous_location"


def test_a_named_folder_without_the_mid_is_unknown(fixture_store: Store) -> None:
    with pytest.raises(TargetRefused) as info:
        resolve(fixture_store, delete, {"mids": [DUP], "folder_id": "acct1:/INBOX"})
    assert (info.value.code, info.value.reason) == ("unknown_mid", "not_in_folder")


def test_a_single_location_mid_needs_no_folder(fixture_store: Store) -> None:
    (t,) = resolve(fixture_store, move, {"mids": [SINGLE], "dest_folder_id": "acct1:/Archive"})
    assert t.folder_ids == ("acct1:/Invoices",)


def test_a_tag_tool_acts_on_every_location_whatever_folder_it_names(
    fixture_store: Store,
) -> None:
    (t,) = resolve(fixture_store, tag, {"mids": [DUP], "folder_id": "acct2:/INBOX"})
    assert t.folder_ids == DUP_FOLDERS


def test_the_resolver_refuses_in_target_order(fixture_store: Store) -> None:
    with pytest.raises(TargetRefused) as info:
        resolve(fixture_store, tag, {"mids": [CONFLICTED, NOT_INDEXED]})
    assert info.value.code == "mid_conflicted"
    with pytest.raises(TargetRefused) as info:
        resolve(fixture_store, tag, {"mids": [NOT_INDEXED, CONFLICTED]})
    assert info.value.code == "unknown_mid"


def test_named_folder_reads_the_documented_fields_only() -> None:
    assert FOLDER_FIELDS == ("source_folder_id", "folder_id")
    assert named_folder(MoveIn(mids=[], dest_folder_id="d")) is None
    assert named_folder(MoveIn(mids=[], dest_folder_id="d", source_folder_id="s")) == "s"
    assert named_folder(DeleteIn(mids=[], folder_id="f")) == "f"

    class Both(BoundaryModel):
        source_folder_id: str | None = None
        folder_id: str | None = None

    assert named_folder(Both(source_folder_id="s", folder_id="f")) == "s"
    assert named_folder(Both(folder_id="f")) == "f"
    with pytest.raises(TargetRefused) as info:
        named_folder(Both(folder_id=""))
    assert (info.value.code, info.value.reason) == ("invalid_params", "folder_field")


def test_the_engine_resolves_through_its_own_index(
    audit: AuditLog, fixture_store: Store, clock: Clock
) -> None:
    eng = Engine(
        audit=audit, index=fixture_store, bridge=None, clock=clock, known_recipients=Recipients()
    )
    inp = TagIn(mids=[DUP])
    (t,) = eng.resolve_index(tag, inp, [DUP])
    assert t.folder_ids == DUP_FOLDERS
    with pytest.raises(TargetRefused) as info:
        asyncio.run(eng.find_live((t,)))
    assert info.value.code == "unavailable"  # no bridge


# ---- prev/post pairing --------------------------------------------------------------------------


def loc(folder: str, tb_id: int, *tags: str) -> ResolvedLocation:
    state = TargetState(folder_id=folder, tags=list(tags), flagged=False, read=False)
    return ResolvedLocation(folder_id=folder, tb_id=tb_id, state=state)


def folders(pairs: list[tuple[Any, Any]]) -> list[tuple[str | None, str | None]]:
    return [
        (None if p is None else p.folder_id, None if q is None else q.folder_id) for p, q in pairs
    ]


def test_pairing_in_place_by_tb_id() -> None:
    # Two copies in one folder: each pairs with itself (same tb_id), not by position.
    prev = [loc("a:/X", 2), loc("a:/X", 1, "k")]
    post = [loc("a:/X", 1, "k", "t"), loc("a:/X", 2, "t")]
    pairs = pair_states(prev, post)
    got = [(None if p is None else p.tags, None if q is None else q.tags) for p, q in pairs]
    assert got == [(["k"], ["k", "t"]), ([], ["t"])]


def test_pairing_a_move_by_folder_order() -> None:
    pairs = pair_states([loc("a:/B", 1), loc("a:/A", 2)], [loc("a:/T", 7), loc("a:/S", 8)])
    assert folders(pairs) == [("a:/A", "a:/S"), ("a:/B", "a:/T")]


def test_pairing_same_folder_new_tb_id() -> None:
    assert folders(pair_states([loc("a:/A", 1)], [loc("a:/A", 9)])) == [("a:/A", "a:/A")]


def test_unpaired_sides_are_none() -> None:
    assert folders(pair_states([loc("a:/A", 1), loc("a:/B", 2)], [loc("a:/A", 1)])) == [
        ("a:/A", "a:/A"),
        ("a:/B", None),
    ]
    assert folders(pair_states([loc("a:/A", 1)], [loc("a:/A", 1), loc("a:/C", 5)])) == [
        ("a:/A", "a:/A"),
        (None, "a:/C"),
    ]


def test_fixture_index_is_shared_read_only(fixture_index: Path) -> None:
    store = Store.open(fixture_index, readonly=True)
    try:
        assert store.readonly and store.get_message(DUP) is not None
    finally:
        store.close()


def test_post_state_leaves_out_the_untouched_copies(fixture_store: Store, fb: FakeBridge) -> None:
    # A move named to one folder of a two-folder mid: the other copy is not a target, so the
    # undo payload holds the moved copy only (prev Projects -> post Archive).
    inp = MoveIn(mids=[DUP], dest_folder_id="acct1:/Archive", source_folder_id="acct2:/Projects")
    resolved = resolve_index(fixture_store, move, inp, [DUP])
    found = asyncio.run(find_live(fb, resolved))
    (projects,) = found.locations[DUP]
    assert projects.folder_id == "acct2:/Projects"
    asyncio.run(fb.move([projects.tb_id], "acct1:/Archive"))
    (target,) = asyncio.run(post_state(fb, found))
    assert target.mid == DUP
    assert (target.prev, target.post) == (
        TargetState(folder_id="acct2:/Projects", flagged=False, read=False),
        TargetState(folder_id="acct1:/Archive", flagged=False, read=False),
    )


def test_a_state_the_audit_cannot_record_refuses_with_a_row(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    tb_id = asyncio.run(live(fb))["acct2:/INBOX"][0]
    asyncio.run(fb.update(tb_id, tags=[f"k{i}" for i in range(257)]))  # TargetState: <= 256
    fb.calls.clear()
    refused(reg, "tag", {"mids": [DUP]}, "internal", "unrecordable_state")
    assert RAN == [] and methods(fb) == ["mail.messages.find"]
    assert_refused_row(audit, "internal")
