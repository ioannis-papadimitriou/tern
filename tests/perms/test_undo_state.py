"""Plan 06b step 8: undo with a live state check.

``Registry.undo(caller, token)`` (``ui`` only) finds every target of the token live through
FakeBridge and writes the inverse only when each is still in its recorded post state; otherwise
``state_changed`` and FakeBridge records no write. The undo has its own audit row and undo token
(prev/post swapped), so undoing it is a redo. The index is the session's read-only fixture index
(FakeBridge's TB events are not applied: undo reads TB live, never the index's state)."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tern.bridge.protocol import BridgeError, FindParams, Mid, MoveParams, UpdateParams
from tern.index.store import Store
from tern.perms.audit import AuditLog, EventKind, TargetState, UndoPayload, UndoTarget
from tern.perms.targets import TargetRefused
from tern.perms.undo import (
    UNDO_TOOL,
    LocatedTarget,
    UndoClaims,
    check_payload,
    inverse_payload,
    inverse_requests,
    inverse_tier,
)
from tern.testing.approvals import approve_all
from tern.testing.fake_bridge import FakeBridge
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, ToolError, tool
from tern.tools.registry import Registry, ToolResult, UndoResult

from .conftest import FIXTURE, engine_of, mids
from .test_write_status import Events

MANIFEST = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
BY_KEY = {m["key"]: m for m in MANIFEST["messages"]}
SINGLE = BY_KEY["inv-0042"]["mid"]  # acct1/Invoices only
SINGLE_HMID = BY_KEY["inv-0042"]["message_id"].strip("<>")
SINGLE_FOLDER = "acct1:/Invoices"
DEST = "acct1:/Newsletters"
DUP = BY_KEY["dup-1"]["mid"]  # acct2/INBOX + acct2/Projects
DUP_HMID = BY_KEY["dup-1"]["message_id"].strip("<>")
CONFLICTED_KEY = MANIFEST["adversarial_pair"]["key"]  # inv-0077: genuine + spoof, one Message-ID
CONFLICTED = BY_KEY[CONFLICTED_KEY]["mid"]
CONFLICTED_HMID = BY_KEY[CONFLICTED_KEY]["message_id"].strip("<>")
CONFLICTED_FOLDER = "acct1:/Invoices"  # the genuine copy
LABEL = "$label4"
PLANTED = 846_213  # FakeBridge(first_tb_id=PLANTED): distinctive tb_ids to look for

WRITE_METHODS = ("mail.update", "mail.move", "mail.archive", "mail.delete")


class MidsIn(BoundaryModel):
    mids: list[Mid]


class MoveIn(BoundaryModel):
    mids: list[Mid]
    dest_folder_id: str
    source_folder_id: str | None = None


class Out(BoundaryModel):
    n: int


def _tb_ids(ctx: ToolContext) -> list[int]:
    return [loc.tb_id for locs in ctx.targets.values() for loc in locs]


@tool(name="tag", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def tag(ctx: ToolContext, inp: MidsIn) -> Out:
    """A set_tags-shaped W1 dummy: adds LABEL and the flag on every engine-resolved location."""
    ids = _tb_ids(ctx)
    await ctx.bridge.request(
        "mail.update", UpdateParams(tb_ids=ids, add_tags=[LABEL], flagged=True)
    )
    return Out(n=len(ids))


@tool(name="move", tier=Tier.W2, effects={Effect.MAIL_MOVE}, untrusted_output=False)
async def move(ctx: ToolContext, inp: MoveIn) -> Out:
    """A move-shaped dummy: moves every engine-resolved location to ``dest_folder_id``."""
    ids = _tb_ids(ctx)
    await ctx.bridge.request("mail.move", MoveParams(tb_ids=ids, dest_folder_id=inp.dest_folder_id))
    return Out(n=len(ids))


TOOLS = [tag, move]


@pytest.fixture
def fb() -> FakeBridge:
    return FakeBridge(FIXTURE, first_tb_id=PLANTED)


@pytest.fixture
def reg(make_registry: Any, fb: FakeBridge, fixture_store: Store) -> Registry:
    registry = make_registry(TOOLS, bridge=fb, index=fixture_store)
    approve_all(engine_of(registry).approvals)
    return registry


def run_tag(reg: Registry, targets: list[str]) -> ToolResult:
    res = asyncio.run(reg.call("agent:s1", "tag", {"mids": targets}))
    assert res.undo_token is not None
    return res


def undo(reg: Registry, token: str, caller: str = "ui") -> UndoResult:
    return asyncio.run(reg.undo(caller, token))


def undo_fails(reg: Registry, token: str, caller: str = "ui") -> ToolError:
    with pytest.raises(ToolError) as info:
        undo(reg, token, caller)
    return info.value


def live(fb: FakeBridge, hmid: str) -> list[tuple[str, list[str], bool]]:
    """(folder, tags, flagged) of every live copy, sorted."""
    found = fb._find(FindParams(header_message_id=hmid))  # pyright: ignore[reportPrivateUsage] -- the fake's state, not a recorded call
    return sorted((f.folder_id, sorted(f.tags), f.flags.flagged) for f in found.root)


def writes(fb: FakeBridge) -> list[str]:
    return [m for m, _ in fb.calls if m in WRITE_METHODS]


def undo_rows(audit: AuditLog) -> list[Any]:
    return [r for r in audit.list(limit=None) if r.tool == UNDO_TOOL]


# ---- tag -> undo -> redo ----------------------------------------------------------------------


def test_tag_then_undo_restores_the_original_state(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    before = live(fb, SINGLE_HMID)
    res = run_tag(reg, [SINGLE])
    assert live(fb, SINGLE_HMID) != before

    out = undo(reg, res.undo_token or "")

    assert live(fb, SINGLE_HMID) == before
    assert out.undone_call_id == res.call_id
    assert audit.status(res.call_id).undone is True
    [row] = undo_rows(audit)
    assert row.call_id == out.call_id
    assert (row.caller, row.tier, row.decision, row.reason) == ("ui", Tier.W1, "auto", None)
    assert row.initial_status == "tb_accepted"
    assert row.args == {"undo_of": res.call_id}
    assert row.target_mids == (SINGLE,) and row.target_count == 1
    assert row.undo_token == out.undo_token
    [undone] = [e for e in audit.events(res.call_id) if e.kind == "undone"]
    assert undone.detail == {"undo_call_id": out.call_id}
    # the inverse: one mail.update removing the label and the flag (only what differs)
    method, params = fb.calls[-1]
    assert method == "mail.update"
    assert params["remove_tags"] == [LABEL] and params["flagged"] is False
    assert "add_tags" not in params and "read" not in params  # None fields are not sent


def test_the_undo_token_has_prev_and_post_swapped(reg: Registry, audit: AuditLog) -> None:
    res = run_tag(reg, [SINGLE])
    out = undo(reg, res.undo_token or "")
    original = audit.undo_token(res.undo_token or "").payload
    mine = audit.undo_token(out.undo_token).payload
    assert mine == inverse_payload(original)
    [t] = mine.targets
    assert t.prev is not None and LABEL in t.prev.tags
    assert t.post is not None and LABEL not in t.post.tags


def test_a_redo_restores_the_post_state(reg: Registry, fb: FakeBridge, audit: AuditLog) -> None:
    res = run_tag(reg, [SINGLE])
    after_write = live(fb, SINGLE_HMID)
    out = undo(reg, res.undo_token or "")

    redo = undo(reg, out.undo_token)

    assert live(fb, SINGLE_HMID) == after_write
    assert redo.undone_call_id == out.call_id
    assert audit.status(out.call_id).undone is True
    assert audit.get(redo.call_id).args == {"undo_of": out.call_id}
    # and the redo can be undone again
    undo(reg, redo.undo_token)
    assert LABEL not in live(fb, SINGLE_HMID)[0][1]


def test_undo_is_not_a_tool(reg: Registry) -> None:
    assert UNDO_TOOL not in reg.names()
    assert all(m.name != UNDO_TOOL for m in reg.describe())


# ---- the state check -------------------------------------------------------------------------


def test_a_retag_in_between_is_state_changed_and_writes_nothing(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    res = run_tag(reg, [SINGLE])
    tb_id = _tb_id_of(fb, SINGLE_HMID, SINGLE_FOLDER)
    asyncio.run(fb.update(tb_id, tags=[LABEL, "$label2"]))  # the user adds another tag
    n = len(writes(fb))

    err = undo_fails(reg, res.undo_token or "")

    assert (err.code, err.reason) == ("state_changed", "state_changed")
    assert len(writes(fb)) == n
    assert audit.status(res.call_id).undone is False
    [row] = undo_rows(audit)
    assert (row.decision, row.reason, row.initial_status) == ("error", "state_changed", None)
    assert row.args == {"undo_of": res.call_id}


def test_one_of_two_targets_changed_changes_nothing(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    res = run_tag(reg, [DUP])
    assert len(audit.undo_token(res.undo_token or "").payload.targets) == 2
    asyncio.run(fb.update(_tb_id_of(fb, DUP_HMID, "acct2:/Projects"), flagged=False))
    state = live(fb, DUP_HMID)
    n = len(writes(fb))

    err = undo_fails(reg, res.undo_token or "")

    assert err.code == "state_changed"
    assert live(fb, DUP_HMID) == state  # the unchanged copy still has the label
    assert len(writes(fb)) == n
    assert audit.status(res.call_id).undone is False


def test_both_copies_are_restored(reg: Registry, fb: FakeBridge) -> None:
    before = live(fb, DUP_HMID)
    res = run_tag(reg, [DUP])
    undo(reg, res.undo_token or "")
    assert live(fb, DUP_HMID) == before


def test_a_target_no_longer_in_the_index_is_not_locatable(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    gone = mids(1)[0]
    token = _seeded_token(audit, gone, prev_tags=[], post_tags=[LABEL])
    err = undo_fails(reg, token)
    assert (err.code, err.reason) == ("state_changed", "not_locatable")
    assert fb.calls == []


def test_a_null_side_is_unsupported(reg: Registry, fb: FakeBridge, audit: AuditLog) -> None:
    call_id = _seeded_call(audit, SINGLE)
    payload = UndoPayload(
        targets=[UndoTarget(mid=SINGLE, prev=None, post=_state(SINGLE_FOLDER, [LABEL]))]
    )
    token = audit.add_undo_token(call_id, payload)
    err = undo_fails(reg, token)
    assert (err.code, err.reason) == ("state_changed", "unsupported_target")
    assert fb.calls == []
    [row] = undo_rows(audit)
    assert (row.decision, row.reason) == ("error", "state_changed")


# ---- move ------------------------------------------------------------------------------------


def test_move_then_undo_is_back_in_the_source_folder(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    before = live(fb, SINGLE_HMID)
    res = asyncio.run(reg.call("agent:s1", "move", {"mids": [SINGLE], "dest_folder_id": DEST}))
    assert [f for f, _, _ in live(fb, SINGLE_HMID)] == [DEST]
    assert res.undo_token is not None
    at_dest = _tb_id_of(fb, SINGLE_HMID, DEST)

    out = undo(reg, res.undo_token)

    assert live(fb, SINGLE_HMID) == before
    assert fb.calls[-1] == ("mail.move", {"tb_ids": [at_dest], "dest_folder_id": SINGLE_FOLDER})
    row = audit.get(out.call_id)
    assert row.tier is Tier.W2  # a move back
    assert row.decision == "auto"  # undo is ui-initiated: no approval card
    assert audit.status(res.call_id).undone is True


def test_a_moved_target_moved_again_is_state_changed(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    res = asyncio.run(reg.call("agent:s1", "move", {"mids": [SINGLE], "dest_folder_id": DEST}))
    asyncio.run(fb.move([_tb_id_of(fb, SINGLE_HMID, DEST)], "acct1:/INBOX"))  # the user moves it
    n = len(writes(fb))
    err = undo_fails(reg, res.undo_token or "")
    assert err.code == "state_changed"
    assert len(writes(fb)) == n
    assert [f for f, _, _ in live(fb, SINGLE_HMID)] == ["acct1:/INBOX"]


# ---- token, caller ---------------------------------------------------------------------------


def test_a_token_used_twice_is_state_changed(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    res = run_tag(reg, [SINGLE])
    undo(reg, res.undo_token or "")
    n = len(fb.calls)

    err = undo_fails(reg, res.undo_token or "")

    assert (err.code, err.reason) == ("state_changed", "already_undone")
    assert len(fb.calls) == n  # not even a find
    assert [r.reason for r in undo_rows(audit)] == ["state_changed", None]


@pytest.mark.parametrize("caller", ["agent:s1", "mcp:conn-1", "job:triage"])
def test_a_non_ui_caller_is_denied(
    reg: Registry, fb: FakeBridge, audit: AuditLog, caller: str
) -> None:
    res = run_tag(reg, [SINGLE])
    n = len(fb.calls)

    err = undo_fails(reg, res.undo_token or "", caller=caller)

    assert (err.code, err.reason) == ("denied", "undo_not_allowed")
    assert len(fb.calls) == n
    [row] = undo_rows(audit)
    assert (row.caller, row.decision, row.reason) == (caller, "denied", "undo_not_allowed")
    assert row.args == {} and row.target_count == 0  # the token is not read for it
    # the token is still usable by the user
    undo(reg, res.undo_token or "")
    assert audit.status(res.call_id).undone is True


@pytest.mark.parametrize("token", ["0" * 32, "not-a-token", ""])
def test_an_unknown_token_is_invalid_params(
    reg: Registry, fb: FakeBridge, audit: AuditLog, token: str
) -> None:
    err = undo_fails(reg, token)
    assert (err.code, err.reason) == ("invalid_params", "unknown")
    assert fb.calls == []
    [row] = undo_rows(audit)
    assert (row.decision, row.reason, row.args) == ("error", "invalid_params", {})


def test_an_invalid_caller_id_is_a_value_error_without_a_row(
    reg: Registry, audit: AuditLog
) -> None:
    with pytest.raises(ValueError):
        undo(reg, "0" * 32, caller="root")
    assert undo_rows(audit) == []


def test_concurrent_undos_of_one_token_write_once(
    make_registry: Any, fb: FakeBridge, fixture_store: Store, audit: AuditLog
) -> None:
    reg = make_registry(TOOLS, bridge=Yielding(fb), index=fixture_store)
    res = run_tag(reg, [SINGLE])
    token = res.undo_token or ""

    async def both() -> list[UndoResult | BaseException]:
        return await asyncio.gather(
            reg.undo("ui", token), reg.undo("ui", token), return_exceptions=True
        )

    first, second = asyncio.run(both())
    assert isinstance(first, UndoResult)
    assert isinstance(second, ToolError)
    assert (second.code, second.reason) == ("state_changed", "in_progress")
    assert writes(fb).count("mail.update") == 2  # the tag and one inverse
    assert engine_of(reg).undo_claims.held() == frozenset()


# ---- failures --------------------------------------------------------------------------------


def test_a_bridge_failure_mid_way_is_an_error_event_without_undone(
    make_registry: Any, fb: FakeBridge, fixture_store: Store, audit: AuditLog
) -> None:
    flaky = Flaky(fb)
    reg = make_registry(TOOLS, bridge=flaky, index=fixture_store)
    res = run_tag(reg, [DUP])
    flaky.fail_update_after = 1  # the second inverse mail.update fails

    err = undo_fails(reg, res.undo_token or "")

    assert (err.code, err.reason) == ("unavailable", "partial")
    assert audit.status(res.call_id).undone is False
    [row] = undo_rows(audit)
    assert (row.decision, row.initial_status) == ("auto", "tb_accepted")
    assert row.undo_token is None
    assert audit.status(row.call_id).status == "error"
    [event] = [e for e in audit.events(row.call_id) if e.kind == "error"]
    assert event.detail == {
        "code": "unavailable",
        "exc": "tb_error",
        "applied": 1,
        "reason": "tb_error",
    }


def test_an_unavailable_bridge_at_the_state_check_writes_nothing(
    make_registry: Any, fb: FakeBridge, fixture_store: Store, audit: AuditLog
) -> None:
    flaky = Flaky(fb)
    reg = make_registry(TOOLS, bridge=flaky, index=fixture_store)
    res = run_tag(reg, [SINGLE])
    flaky.fail_find = True
    n = len(writes(fb))

    err = undo_fails(reg, res.undo_token or "")

    assert err.code == "unavailable"
    assert len(writes(fb)) == n
    [row] = undo_rows(audit)
    assert (row.decision, row.reason, row.initial_status) == ("error", "unavailable", None)


def test_a_conflicted_mid_is_refused_before_any_find(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    # The adversarial pair: a spoof under the genuine invoice's Message-ID (#5, T14). The token
    # names the genuine copy's real state, so without the refusal the inverse would run.
    genuine = _tb_id_of(fb, CONFLICTED_HMID, CONFLICTED_FOLDER)
    [found] = [
        f
        for f in fb._find(FindParams(header_message_id=CONFLICTED_HMID)).root  # pyright: ignore[reportPrivateUsage] -- the fake's state
        if f.tb_id == genuine
    ]
    flags = found.flags
    prev = _state(CONFLICTED_FOLDER, sorted(found.tags), flags.flagged, flags.read)
    post = _state(CONFLICTED_FOLDER, sorted({*found.tags, LABEL}), flags.flagged, flags.read)
    asyncio.run(fb.update(genuine, tags=post.tags))
    call_id = _seeded_call(audit, CONFLICTED)
    token = audit.add_undo_token(
        call_id, UndoPayload(targets=[UndoTarget(mid=CONFLICTED, prev=prev, post=post)])
    )

    err = undo_fails(reg, token)

    assert (err.code, err.reason) == ("mid_conflicted", "mid_conflicted")
    assert fb.calls == []  # not even a find
    assert audit.status(call_id).undone is False
    [row] = undo_rows(audit)
    assert (row.decision, row.reason, row.initial_status) == ("error", "mid_conflicted", None)


@pytest.mark.parametrize("failure", ["sqlite", "validation"])
def test_an_unreadable_token_is_an_audit_read_row(
    reg: Registry, fb: FakeBridge, audit: AuditLog, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    res = run_tag(reg, [SINGLE])
    n = len(fb.calls)

    def unreadable(token: str) -> Any:
        if failure == "sqlite":
            raise sqlite3.OperationalError("database is locked")
        return UndoPayload.model_validate_json("{}")  # a stored payload that no longer validates

    monkeypatch.setattr(audit, "undo_token", unreadable)
    err = undo_fails(reg, res.undo_token or "")

    assert (err.code, err.reason) == ("internal", "audit_read")
    assert len(fb.calls) == n
    [row] = undo_rows(audit)
    assert (row.decision, row.reason, row.args) == ("error", "internal", {})
    assert audit.status(res.call_id).undone is False


def test_a_cancel_during_the_state_check_is_a_cancelled_row_and_writes_nothing(
    make_registry: Any, fb: FakeBridge, fixture_store: Store, audit: AuditLog
) -> None:
    blocking = Blocking(fb)
    reg = make_registry(TOOLS, bridge=blocking, index=fixture_store)
    res = run_tag(reg, [SINGLE])
    n = len(writes(fb))

    async def go() -> None:
        blocking.block_find = True
        task = asyncio.ensure_future(reg.undo("ui", res.undo_token or ""))
        await blocking.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())

    assert len(writes(fb)) == n
    [row] = undo_rows(audit)
    assert (row.decision, row.reason, row.initial_status) == ("denied", "cancelled", None)
    assert audit.status(res.call_id).undone is False
    assert engine_of(reg).undo_claims.held() == frozenset()


def test_a_cancel_during_the_inverse_is_an_error_event_and_propagates(
    make_registry: Any, fb: FakeBridge, fixture_store: Store, audit: AuditLog
) -> None:
    flaky = Flaky(fb)
    reg = make_registry(TOOLS, bridge=flaky, index=fixture_store)
    res = run_tag(reg, [DUP])
    flaky.fail_update_after = 1
    flaky.update_failure = asyncio.CancelledError  # the second inverse mail.update is cancelled

    with pytest.raises(asyncio.CancelledError):
        undo(reg, res.undo_token or "")

    assert audit.status(res.call_id).undone is False
    [row] = undo_rows(audit)
    assert (row.decision, row.initial_status, row.undo_token) == ("auto", "tb_accepted", None)
    assert audit.status(row.call_id).status == "error"
    [event] = [e for e in audit.events(row.call_id) if e.kind == "error"]
    assert event.detail == {
        "code": "internal",
        "exc": "cancel",
        "applied": 1,
        "reason": "cancelled",
    }
    assert engine_of(reg).undo_claims.held() == frozenset()


def test_a_failed_undone_append_is_audit_failed_and_the_original_stays_watched(
    reg: Registry, audit: AuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = run_tag(reg, [SINGLE])
    ws = engine_of(reg).write_status
    real = audit.append_event

    def failing(call_id: str, kind: EventKind, detail: Any = None) -> Any:
        if kind == "undone":
            raise sqlite3.OperationalError("disk I/O error")
        return real(call_id, kind, detail)

    monkeypatch.setattr(audit, "append_event", failing)
    err = undo_fails(reg, res.undo_token or "")

    assert (err.code, err.reason) == ("internal", "audit_failed")
    assert audit.status(res.call_id).undone is False
    assert ws.pending() == {res.call_id}  # not undone: still watched
    [row] = undo_rows(audit)
    assert row.undo_token is None
    [event] = [e for e in audit.events(row.call_id) if e.kind == "error"]
    assert event.detail == {
        "code": "internal",
        "exc": "OperationalError",
        "reason": "audit_failed",
    }


def test_a_failed_token_write_after_undone_leaves_no_orphaned_pending_write(
    reg: Registry, audit: AuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = run_tag(reg, [SINGLE])
    ws = engine_of(reg).write_status
    assert ws.pending() == {res.call_id}

    def failing(call_id: str, payload: Any) -> str:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(audit, "add_undo_token", failing)
    err = undo_fails(reg, res.undo_token or "")

    assert (err.code, err.reason) == ("internal", "audit_failed")
    assert audit.status(res.call_id).undone is True
    assert ws.pending() == frozenset()  # the undone original is not left to go stale
    [row] = undo_rows(audit)
    assert row.undo_token is None
    assert audit.status(row.call_id).status == "error"


# ---- write status ----------------------------------------------------------------------------


def test_the_undone_write_is_no_longer_watched_and_the_undo_is(
    reg: Registry, audit: AuditLog
) -> None:
    res = run_tag(reg, [SINGLE])
    ws = engine_of(reg).write_status
    assert ws.pending() == {res.call_id}  # the index never sees FakeBridge's events here

    out = undo(reg, res.undo_token or "")

    # The undone write is dropped. The undo is watched, but the index still showing the
    # original prev state (it never saw the original write) confirms nothing about the inverse:
    # no check on register, so the undo stays tb_accepted until an index event lands (step 8
    # review round 1; it was "observed" at once before).
    assert ws.pending() == {out.call_id}
    assert audit.status(out.call_id).status == "tb_accepted"
    assert audit.status(res.call_id).status == "tb_accepted"


@pytest.fixture
def writer(fixture_index: Path, tmp_path: Path) -> Iterator[Store]:
    """A writable copy of the session's fixture index."""
    dst = tmp_path / "index.db"
    src = sqlite3.connect(f"file:{fixture_index}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    store = Store.open(dst)
    yield store
    store.close()


def test_write_status_watches_the_undo_until_its_event_lands(
    make_registry: Any, fb: FakeBridge, writer: Store, audit: AuditLog
) -> None:
    reader = Store.open(writer.path, readonly=True)
    try:
        reg = make_registry(TOOLS, bridge=fb, index=reader)
        ws = engine_of(reg).write_status
        writer.location_observers.append(ws.observe)
        events = Events(fb, writer)
        res = run_tag(reg, [SINGLE])
        asyncio.run(events.apply())
        assert audit.status(res.call_id).status == "observed"

        out = undo(reg, res.undo_token or "")
        assert ws.pending() == {out.call_id}  # the index still shows the tag
        assert audit.status(out.call_id).status == "tb_accepted"

        asyncio.run(events.apply())
        assert audit.status(out.call_id).status == "observed"
        assert ws.pending() == frozenset()
    finally:
        reader.close()


def test_an_undo_before_the_index_saw_the_write_waits_for_the_index(
    make_registry: Any, fb: FakeBridge, writer: Store, audit: AuditLog
) -> None:
    reader = Store.open(writer.path, readonly=True)
    try:
        reg = make_registry(TOOLS, bridge=fb, index=reader)
        ws = engine_of(reg).write_status
        writer.location_observers.append(ws.observe)
        events = Events(fb, writer)
        res = run_tag(reg, [SINGLE])
        assert ws.pending() == {res.call_id}  # the tag's event is held: the index lags

        out = undo(reg, res.undo_token or "")
        assert ws.pending() == {out.call_id}
        assert audit.status(out.call_id).status == "tb_accepted"

        asyncio.run(events.apply())  # the tag, then the inverse, reach the index
        assert audit.status(out.call_id).status == "observed"
        assert ws.pending() == frozenset()
        assert audit.status(res.call_id).status == "tb_accepted"  # undone, never observed
    finally:
        reader.close()


def test_the_undo_of_a_write_that_is_not_watched_gets_the_check_on_register(
    reg: Registry, fb: FakeBridge, audit: AuditLog
) -> None:
    # An earlier write (seeded: not in the pending set) whose post state TB still shows; the
    # index shows its prev state, which is the undo's post state: the check on register runs.
    [found] = fb._find(FindParams(header_message_id=SINGLE_HMID)).root  # pyright: ignore[reportPrivateUsage] -- the fake's state
    flags = found.flags
    prev = _state(SINGLE_FOLDER, sorted(found.tags), flags.flagged, flags.read)
    post = _state(SINGLE_FOLDER, sorted({*found.tags, LABEL}), flags.flagged, flags.read)
    asyncio.run(fb.update(found.tb_id, tags=post.tags))
    call_id = _seeded_call(audit, SINGLE)
    token = audit.add_undo_token(call_id, _payload(prev, post))
    ws = engine_of(reg).write_status
    assert ws.pending() == frozenset()

    out = undo(reg, token)

    assert audit.status(out.call_id).status == "observed"
    assert ws.pending() == frozenset()


def test_no_tb_id_in_the_undo_rows_events_payload_or_logs(
    reg: Registry, audit: AuditLog, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    res = run_tag(reg, [DUP])
    out = undo(reg, res.undo_token or "")
    redo = undo(reg, out.undo_token)
    texts = [caplog.text, repr(caplog.records)]
    for cid in (out.call_id, redo.call_id):
        texts.append(repr(audit.get(cid)))
        texts.append(repr(audit.events(cid)))
        texts.append(audit.undo_token(audit.get(cid).undo_token or "").payload.model_dump_json())
    planted = [str(t) for t in range(PLANTED, PLANTED + 200)]
    for text in texts:
        assert not any(p in text for p in planted)


# ---- pure parts ------------------------------------------------------------------------------


def test_inverse_tier_is_w2_only_for_a_move() -> None:
    inplace = _payload(_state(SINGLE_FOLDER, []), _state(SINGLE_FOLDER, [LABEL]))
    moved = _payload(_state(SINGLE_FOLDER, []), _state(DEST, []))
    assert inverse_tier(inplace) is Tier.W1
    assert inverse_tier(moved) is Tier.W2


def test_inverse_requests_update_before_move_and_only_what_differs() -> None:
    prev = TargetState(folder_id=SINGLE_FOLDER, tags=["$label1"], flagged=False, read=True)
    post = TargetState(folder_id=DEST, tags=[LABEL], flagged=False, read=False)
    [t] = _payload(prev, post).targets
    reqs = inverse_requests([LocatedTarget(t, 7)])
    assert [m for m, _ in reqs] == ["mail.update", "mail.move"]
    upd, mv = reqs[0][1], reqs[1][1]
    assert isinstance(upd, UpdateParams) and isinstance(mv, MoveParams)
    assert upd.tb_ids == [7] and upd.add_tags == ["$label1"] and upd.remove_tags == [LABEL]
    assert upd.flagged is None and upd.read is True
    assert mv.tb_ids == [7] and mv.dest_folder_id == SINGLE_FOLDER
    same = _payload(prev, prev).targets[0]
    assert inverse_requests([LocatedTarget(same, 7)]) == []


def test_check_payload_refuses_a_null_post() -> None:
    payload = UndoPayload(
        targets=[UndoTarget(mid=SINGLE, prev=_state(SINGLE_FOLDER, []), post=None)]
    )
    with pytest.raises(TargetRefused) as info:
        check_payload(payload)
    assert (info.value.code, info.value.reason) == ("state_changed", "unsupported_target")


def test_undo_claims_hold_one_token_once() -> None:
    claims = UndoClaims()
    assert claims.claim("a") is True
    assert claims.claim("a") is False
    claims.release("a")
    assert claims.claim("a") is True


def test_a_located_target_keeps_its_tb_id_out_of_repr() -> None:
    [t] = _payload(_state(SINGLE_FOLDER, []), _state(SINGLE_FOLDER, [LABEL])).targets
    assert str(PLANTED) not in repr(LocatedTarget(t, PLANTED))


# ---- helpers ---------------------------------------------------------------------------------


class Yielding:
    """FakeBridge behind a suspension point (as a real extension round trip), so two undos
    interleave."""

    def __init__(self, fb: FakeBridge) -> None:
        self.fb = fb

    async def request(self, method: str, params: BaseModel) -> Any:
        await asyncio.sleep(0)
        return await self.fb.request(method, params)

    async def notify(self, method: str, params: BaseModel) -> None:
        await self.fb.notify(method, params)


class Flaky:
    """FakeBridge whose ``find`` can fail (``unavailable``) and whose ``mail.update`` fails
    (``tb_error``) after ``fail_update_after`` more successful updates."""

    def __init__(self, fb: FakeBridge) -> None:
        self.fb = fb
        self.fail_find = False
        self.fail_update_after: int | None = None
        self.update_failure: type[BaseException] | None = None  # None: a tb_error BridgeError

    async def request(self, method: str, params: BaseModel) -> Any:
        if method == "mail.messages.find" and self.fail_find:
            raise BridgeError("unavailable", "extension not connected")
        if method == "mail.update" and self.fail_update_after is not None:
            if self.fail_update_after == 0:
                if self.update_failure is not None:
                    raise self.update_failure()
                raise BridgeError("tb_error", "Thunderbird call failed")
            self.fail_update_after -= 1
        return await self.fb.request(method, params)

    async def notify(self, method: str, params: BaseModel) -> None:
        await self.fb.notify(method, params)


class Blocking:
    """FakeBridge whose ``find`` never returns once ``block_find`` is set (``entered`` is set
    when it starts waiting), so a test can cancel an undo during its state check."""

    def __init__(self, fb: FakeBridge) -> None:
        self.fb = fb
        self.block_find = False
        self.entered = asyncio.Event()

    async def request(self, method: str, params: BaseModel) -> Any:
        if method == "mail.messages.find" and self.block_find:
            self.entered.set()
            await asyncio.Event().wait()
        return await self.fb.request(method, params)

    async def notify(self, method: str, params: BaseModel) -> None:
        await self.fb.notify(method, params)


def _tb_id_of(fb: FakeBridge, hmid: str, folder: str) -> int:
    found = fb._find(FindParams(header_message_id=hmid))  # pyright: ignore[reportPrivateUsage] -- the fake's state
    [tb_id] = [f.tb_id for f in found.root if f.folder_id == folder]
    return tb_id


def _state(folder: str, tags: list[str], flagged: bool = False, read: bool = True) -> TargetState:
    return TargetState(folder_id=folder, tags=tags, flagged=flagged, read=read)


def _payload(prev: TargetState, post: TargetState) -> UndoPayload:
    return UndoPayload(targets=[UndoTarget(mid=SINGLE, prev=prev, post=post)])


def _seeded_call(audit: AuditLog, mid: str) -> str:
    """An executed W1 write row, as an earlier call left it."""
    return audit.append_call(
        caller="agent:s1",
        tool="tag",
        tier=Tier.W1,
        decision="auto",
        args={"mids": [mid]},
        args_validated=True,
        target_mids=[mid],
        target_count=1,
        initial_status="tb_accepted",
    )


def _seeded_token(audit: AuditLog, mid: str, *, prev_tags: list[str], post_tags: list[str]) -> str:
    call_id = _seeded_call(audit, mid)
    payload = UndoPayload(
        targets=[
            UndoTarget(
                mid=mid,
                prev=_state(SINGLE_FOLDER, prev_tags),
                post=_state(SINGLE_FOLDER, post_tags),
            )
        ]
    )
    return audit.add_undo_token(call_id, payload)
