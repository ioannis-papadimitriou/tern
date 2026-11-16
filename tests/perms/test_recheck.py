"""Plan 06b step 6: the execution re-check of an approved W2/W3 call (M6).

Just before an approved call executes, every card location (mid, folder id) is found again with
``mail.messages.find``. A location that is gone (the message moved to another folder, or was
deleted) refuses the whole call with ``targets_changed``: row ``approved``/``targets_changed``, not
executed, a ``targets_changed`` event, ``notifier.resolved(approval_id, "targets_changed")`` and no
write request. Extra copies in other folders do not count. Before that, the index is read
again: a target no longer indexed is ``targets_changed`` (``not_indexed``), one that became
conflicted while the card was open is ``mid_conflicted`` (row ``approved``/``mid_conflicted``,
card resolved ``denied``, merge review). A bridge failure at the re-check is row
``approved``/``unavailable`` (or ``internal``), not executed. An unchanged approval executes once,
with the re-check's fresh locations (tb_ids), not the ones the card was built from."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from tern.bridge.protocol import BridgeError, ErrorName, FindParams, TbIdParams
from tern.index.store import Store
from tern.perms.audit import AuditLog
from tern.testing.approvals import ScriptedApprovals, holding
from tern.testing.fake_bridge import FakeBridge
from tern.tools import Tier, ToolError
from tern.tools.registry import Registry, ToolResult

from .conftest import FakeSleep, engine_of
from .test_gate import (  # the gate tests' dummy tools and helpers
    DUP,
    RAN,
    TOOLS,
    card_for,
    hmid,
    methods,
    new_fb,
    outcome,
    run,
    start,
    until,
    writable_mids,
    writes,
)

FIND = "mail.messages.find"
INBOX, PROJECTS = "acct2:/INBOX", "acct2:/Projects"


class Switch:
    """The FakeBridge behind a switch: with ``mode`` set, every ``mail.messages.find`` fails with
    that bridge error name, (``"hang"``) never returns, or (``"not_found_list"``) returns
    something that is not a FoundList (a bridge bug)."""

    def __init__(self, fb: FakeBridge) -> None:
        self.fb = fb
        self.mode: str | None = None
        self.hanging = False

    async def request(self, method: str, params: BaseModel) -> Any:
        if method == FIND and self.mode == "hang":
            self.hanging = True
            await asyncio.Event().wait()
        if method == FIND and self.mode == "not_found_list":
            return {"not": "a FoundList"}
        if method == FIND and self.mode is not None:
            raise BridgeError(cast("ErrorName", self.mode), "find failed")
        return await self.fb.request(method, params)

    async def notify(self, method: str, params: BaseModel) -> None:
        await self.fb.notify(method, params)


@pytest.fixture(autouse=True)
def _reset() -> None:
    RAN.clear()


@pytest.fixture
def fb() -> FakeBridge:
    return new_fb()


@pytest.fixture
def switch(fb: FakeBridge) -> Switch:
    return Switch(fb)


@pytest.fixture
def writable(fixture_store: Store) -> list[str]:
    return writable_mids(fixture_store)


@pytest.fixture
def reg(make_registry: Any, switch: Switch, fixture_store: Store) -> Registry:
    return make_registry(TOOLS, bridge=switch, index=fixture_store, sleep=FakeSleep())


async def locations(fb: FakeBridge, store: Store, mid: str) -> dict[str, tuple[int, list[str]]]:
    """folder id -> (tb_id, tag keys) of ``mid``'s live copies (one per folder here)."""
    got = (await fb.request(FIND, FindParams(header_message_id=hmid(store, mid)))).root
    return {f.folder_id: (f.tb_id, sorted(f.tags)) for f in got}


def elsewhere(folder_id: str) -> str:
    """A folder other than ``folder_id`` to move a message to."""
    return "acct1:/Archive" if folder_id != "acct1:/Archive" else "acct1:/Invoices"


async def move_away(fb: FakeBridge, store: Store, mid: str) -> None:
    ((folder, (tb_id, _)),) = (await locations(fb, store, mid)).items()
    await fb.move([tb_id], elsewhere(folder))


async def delete(fb: FakeBridge, store: Store, mid: str) -> None:
    ((_, (tb_id, _)),) = (await locations(fb, store, mid)).items()
    await fb.delete([tb_id])


def assert_not_executed(
    audit: AuditLog, fb: FakeBridge, driver: ScriptedApprovals, reason: str, status: str
) -> None:
    """One ``approved`` row with ``reason`` that never executed, nothing written, the card
    resolved with ``status``."""
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.initial_status, rec.status, rec.undo_token) == (
        "approved",
        reason,
        None,
        None,
        None,
    )
    assert RAN == [] and writes(fb) == []
    assert driver.resolutions == [(driver.cards[0].approval_id, status)]


# ---- a card location gone between approval and execution: targets_changed ---------------------


@pytest.mark.parametrize("when", ["while_pending", "after_the_decision"])
@pytest.mark.parametrize(("name", "tier"), [("tag2", Tier.W2), ("tag3", Tier.W3)])
def test_a_target_moved_before_execution_is_targets_changed(
    reg: Registry,
    fb: FakeBridge,
    audit: AuditLog,
    fixture_store: Store,
    writable: list[str],
    when: str,
    name: str,
    tier: Tier,
) -> None:
    targets = writable[:2]

    async def scenario() -> tuple[ToolResult | ToolError, ScriptedApprovals]:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, name, {"mids": targets})
        card = await card_for(driver, task)
        if when == "while_pending":
            await move_away(fb, fixture_store, targets[1])
        assert driver.decide(card, "approve") == "approved"
        if when == "after_the_decision":  # the call has not woken yet
            assert not task.done()
            await move_away(fb, fixture_store, targets[1])
        return await outcome(task), driver

    res, driver = run(scenario)
    assert isinstance(res, ToolError)
    assert (res.code, res.reason) == ("targets_changed", "not_found")
    assert res.message == "target 2 of 2 is no longer where the approval card has it"
    assert_not_executed(audit, fb, driver, "targets_changed", "targets_changed")
    (rec,) = audit.list()
    assert rec.tier is tier and rec.target_mids == tuple(targets)
    (event,) = audit.events(rec.call_id)
    assert (event.kind, event.detail) == (
        "targets_changed",
        {"code": "targets_changed", "reason": "not_found"},
    )
    # The unmoved target was not written either: the whole action is refused.
    got = run(lambda: locations(fb, fixture_store, targets[0]))
    assert [tags for _, tags in got.values()] == [[]]
    assert engine_of(reg).approvals.pending() == []


def test_a_target_deleted_before_execution_is_targets_changed(
    reg: Registry, fb: FakeBridge, audit: AuditLog, fixture_store: Store, writable: list[str]
) -> None:
    targets = writable[:2]

    async def scenario() -> tuple[ToolResult | ToolError, ScriptedApprovals]:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": targets})
        card = await card_for(driver, task)
        await delete(fb, fixture_store, targets[0])
        driver.decide(card, "approve")
        return await outcome(task), driver

    res, driver = run(scenario)
    assert isinstance(res, ToolError) and res.code == "targets_changed"
    assert res.message == "target 1 of 2 is no longer where the approval card has it"
    assert_not_executed(audit, fb, driver, "targets_changed", "targets_changed")
    (event,) = audit.events(audit.list()[0].call_id)
    assert event.kind == "targets_changed"


def test_the_card_location_moved_refuses_a_move_but_an_extra_copy_does_not_count(
    reg: Registry, fb: FakeBridge, audit: AuditLog, fixture_store: Store
) -> None:
    """A move naming its source folder: the card holds that location only. The mid's copy in
    another folder may go away (it is not a card location); the card's own location may not."""

    async def scenario(gone: str) -> tuple[ToolResult | ToolError, ScriptedApprovals]:
        driver = holding(engine_of(reg).approvals)
        args = {"mids": [DUP], "dest_folder_id": "acct2:/Archive", "source_folder_id": INBOX}
        task = start(reg, "move", args)
        card = await card_for(driver, task)
        assert [t.folder_id for t in card.targets] == [INBOX]
        await fb.delete([(await locations(fb, fixture_store, DUP))[gone][0]])
        driver.decide(card, "approve")
        return await outcome(task), driver

    res, _ = run(lambda: scenario(PROJECTS))
    assert isinstance(res, ToolResult) and RAN == ["move"]
    assert audit.get(res.call_id).initial_status == "tb_accepted"

    RAN.clear()
    res, driver = run(lambda: scenario(INBOX))  # now the card's own location
    assert isinstance(res, ToolError) and res.code == "targets_changed"
    assert RAN == [] and writes(fb) == []
    rec = audit.list()[0]  # newest first
    assert (rec.decision, rec.reason, rec.initial_status) == ("approved", "targets_changed", None)
    assert driver.resolutions == [(driver.cards[0].approval_id, "targets_changed")]


# ---- an unchanged approval executes once, with the re-check's locations -------------------------


def test_an_unchanged_approval_executes_once_after_the_recheck(
    reg: Registry, fb: FakeBridge, audit: AuditLog, fixture_store: Store, writable: list[str]
) -> None:
    targets = writable[:2]

    async def scenario() -> tuple[ToolResult, ScriptedApprovals, int]:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": targets})
        card = await card_for(driver, task)
        at_decision = len(fb.calls)
        driver.decide(card, "approve")
        return await task, driver, at_decision

    res, driver, at_decision = run(scenario)
    # Request-time find per target; after the decision: the re-check per target, the one write,
    # the post-state find per target.
    assert methods(fb)[:at_decision] == [FIND, FIND]
    assert methods(fb)[at_decision:] == [FIND, FIND, "mail.update", FIND, FIND]
    assert RAN == ["tag2"] and res.undo_token is not None
    rec = audit.get(res.call_id)
    assert (rec.decision, rec.reason, rec.initial_status) == ("approved", None, "tb_accepted")
    assert audit.events(res.call_id) == []
    assert driver.resolutions == [(driver.cards[0].approval_id, "approved")]
    for mid in targets:
        assert [
            tags for _, tags in run(lambda m=mid: locations(fb, fixture_store, m)).values()
        ] == [["$label1"]]


def test_execution_uses_the_rechecks_fresh_locations(
    reg: Registry, fb: FakeBridge, audit: AuditLog, fixture_store: Store, writable: list[str]
) -> None:
    """A target moved away and back while pending is in its card folder again under a new tb_id:
    the re-check passes and the tool writes to the new tb_id (the card-time one is stale)."""
    target = writable[0]

    async def scenario() -> tuple[ToolResult | ToolError, int, int]:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": [target]})
        card = await card_for(driver, task)
        ((folder, (old, _)),) = (await locations(fb, fixture_store, target)).items()
        (away,) = await fb.move([old], elsewhere(folder))
        (new,) = await fb.move([away], folder)
        driver.decide(card, "approve")
        return await outcome(task), old, new

    res, old, new = run(scenario)
    assert isinstance(res, ToolResult) and old != new
    (update,) = [params for m, params in fb.calls if m == "mail.update"]
    assert update["tb_ids"] == [new]
    assert [tags for _, tags in run(lambda: locations(fb, fixture_store, target)).values()] == [
        ["$label1"]
    ]
    assert res.undo_token is not None


def test_an_extra_copy_delivered_elsewhere_does_not_count(
    reg: Registry, fb: FakeBridge, audit: AuditLog, fixture_store: Store, writable: list[str]
) -> None:
    target = writable[0]

    async def scenario() -> tuple[ToolResult | ToolError, str, str]:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": [target]})
        card = await card_for(driver, task)
        ((folder, (tb_id, _)),) = (await locations(fb, fixture_store, target)).items()
        raw = await fb.request("mail.messages.raw", TbIdParams(tb_id=tb_id))
        other = elsewhere(folder)
        await fb.deliver(other, base64.b64decode(raw.raw_b64))
        driver.decide(card, "approve")
        return await outcome(task), folder, other

    res, folder, other = run(scenario)
    assert isinstance(res, ToolResult) and RAN == ["tag2"]
    got = run(lambda: locations(fb, fixture_store, target))
    assert got[folder][1] == ["$label1"] and got[other][1] == []  # only the card location


def test_a_subset_rechecks_only_the_kept_targets(
    reg: Registry, fb: FakeBridge, audit: AuditLog, fixture_store: Store, writable: list[str]
) -> None:
    async def scenario(keep: str, drop: str, moved: str) -> ToolResult | ToolError:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": [keep, drop]})
        card = await card_for(driver, task)
        await move_away(fb, fixture_store, moved)
        driver.decide(card, "approve", subset=[keep])
        return await outcome(task)

    a, b, c, d = writable[:4]
    res = run(lambda: scenario(a, b, moved=b))  # the deselected target moved: irrelevant
    assert isinstance(res, ToolResult) and RAN == ["tag2"]
    rec = audit.get(res.call_id)
    assert rec.target_mids == (a,) and rec.original_args_sha256 is not None

    RAN.clear()
    res = run(lambda: scenario(c, d, moved=c))  # the kept target moved
    assert isinstance(res, ToolError) and res.code == "targets_changed"
    assert RAN == [] and writes(fb) == ["mail.update"]  # the first call's write only
    rec = audit.list()[0]
    assert (rec.decision, rec.reason, rec.initial_status) == ("approved", "targets_changed", None)
    assert rec.target_mids == (c,) and rec.original_args_sha256 is not None


# ---- the re-check cannot reach Thunderbird, or is cancelled -------------------------------------


@pytest.mark.parametrize(
    ("bridge_error", "code"),
    [("unavailable", "unavailable"), ("tb_error", "unavailable"), ("stale_id", "internal")],
)
def test_a_bridge_failure_at_the_recheck_runs_nothing(
    reg: Registry,
    fb: FakeBridge,
    switch: Switch,
    audit: AuditLog,
    writable: list[str],
    bridge_error: str,
    code: str,
) -> None:
    async def scenario() -> tuple[ToolResult | ToolError, ScriptedApprovals]:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": writable[:2]})
        card = await card_for(driver, task)
        switch.mode = bridge_error
        driver.decide(card, "approve")
        return await outcome(task), driver

    res, driver = run(scenario)
    assert isinstance(res, ToolError) and (res.code, res.reason) == (code, bridge_error)
    assert_not_executed(audit, fb, driver, code, "denied")
    assert audit.events(audit.list()[0].call_id) == []  # not a targets_changed


def test_a_cancel_during_the_recheck_runs_nothing(
    reg: Registry, fb: FakeBridge, switch: Switch, audit: AuditLog, writable: list[str]
) -> None:
    async def scenario() -> ScriptedApprovals:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        card = await card_for(driver, task)
        switch.mode = "hang"
        driver.decide(card, "approve")
        await until(lambda: switch.hanging, "the re-check find")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return driver

    driver = run(scenario)
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.initial_status) == ("denied", "cancelled", None)
    assert RAN == [] and writes(fb) == []
    assert driver.resolutions == [(driver.cards[0].approval_id, "denied")]


def test_a_failed_targets_changed_event_is_loud_and_still_announced(
    reg: Registry,
    fb: FakeBridge,
    audit: AuditLog,
    fixture_store: Store,
    writable: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*a: object, **kw: object) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    async def scenario() -> tuple[ToolResult | ToolError, ScriptedApprovals]:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        card = await card_for(driver, task)
        await move_away(fb, fixture_store, writable[0])
        monkeypatch.setattr(audit, "append_event", broken)
        driver.decide(card, "approve")
        return await outcome(task), driver

    res, driver = run(scenario)
    assert isinstance(res, ToolError) and (res.code, res.reason) == ("internal", "audit_failed")
    assert_not_executed(audit, fb, driver, "targets_changed", "targets_changed")


# ---- review round 1: an unexpected re-check failure, a drifted snapshot, a failing notifier ----


def test_an_unexpected_recheck_failure_leaves_a_row_and_propagates(
    reg: Registry, fb: FakeBridge, switch: Switch, audit: AuditLog, writable: list[str]
) -> None:
    async def scenario() -> ScriptedApprovals:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": writable[:2]})
        card = await card_for(driver, task)
        switch.mode = "not_found_list"
        driver.decide(card, "approve")
        with pytest.raises(TypeError, match="FoundList"):
            await task
        return driver

    driver = run(scenario)
    assert_not_executed(audit, fb, driver, "internal", "denied")
    assert audit.events(audit.list()[0].call_id) == []


def test_targets_that_drifted_from_the_card_snapshot_are_refused(
    reg: Registry, fb: FakeBridge, audit: AuditLog, writable: list[str]
) -> None:
    async def scenario() -> tuple[ToolResult | ToolError, ScriptedApprovals]:
        approvals = engine_of(reg).approvals
        driver = holding(approvals)
        task = start(reg, "tag2", {"mids": writable[:2]})
        card = await card_for(driver, task)
        approval = approvals.get(card.approval_id)
        assert approval is not None
        # A bug that changed what executes after the card was built (outside the args hash).
        first, *rest = approval.live.targets
        moved = dataclasses.replace(first, folder_ids=("acct1:/Archive",))
        approval.live = dataclasses.replace(approval.live, targets=(moved, *rest))
        driver.decide(card, "approve")
        return await outcome(task), driver

    res, driver = run(scenario)
    assert isinstance(res, ToolError) and (res.code, res.reason) == ("denied", "binding_mismatch")
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.initial_status) == ("denied", "binding_mismatch", None)
    assert RAN == [] and writes(fb) == []
    assert driver.resolutions == [(driver.cards[0].approval_id, "denied")]


def test_a_failing_notifier_does_not_replace_the_refusal(
    reg: Registry,
    fb: FakeBridge,
    audit: AuditLog,
    fixture_store: Store,
    writable: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(approval_id: str, status: str) -> None:
        raise RuntimeError("notifier bug")

    async def scenario() -> ToolResult | ToolError:
        driver = holding(engine_of(reg).approvals)
        task = start(reg, "tag2", {"mids": writable[:1]})
        card = await card_for(driver, task)
        await move_away(fb, fixture_store, writable[0])
        monkeypatch.setattr(driver, "resolved", broken)
        driver.decide(card, "approve")
        return await outcome(task)

    res = run(scenario)
    assert isinstance(res, ToolError) and res.code == "targets_changed"
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.initial_status) == ("approved", "targets_changed", None)
    assert RAN == [] and writes(fb) == []
    failed = [r for r in caplog.records if getattr(r, "reason", None) == "notifier_failed"]
    assert len(failed) == 1 and failed[0].levelname == "ERROR"


# ---- the index re-read at execution: still indexed, not conflicted (merge review) ---------------


@pytest.fixture
def writer(fixture_index: Path, tmp_path: Path) -> Iterator[Store]:
    """A writable copy of the session's fixture index (the index changing while a card is
    open)."""
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


@pytest.fixture
def live_reg(make_registry: Any, fb: FakeBridge, writer: Store) -> Iterator[Registry]:
    """A registry whose engine reads the writable copy through its own read-only Store."""
    reader = Store.open(writer.path, readonly=True)
    yield make_registry(TOOLS, bridge=fb, index=reader, sleep=FakeSleep())
    reader.close()


@pytest.mark.parametrize(("name", "tier"), [("tag2", Tier.W2), ("tag3", Tier.W3)])
def test_a_target_conflicted_while_pending_is_refused_at_execution(
    live_reg: Registry, fb: FakeBridge, audit: AuditLog, writer: Store, name: str, tier: Tier
) -> None:
    """D1 holds whenever the conflict appears: a copy with the same Message-ID but different
    content arriving while the card is open makes the mid conflicted, and the approved call is
    refused ``mid_conflicted`` before anything is found or written."""
    targets = writable_mids(writer)[:2]

    async def scenario() -> tuple[ToolResult | ToolError, ScriptedApprovals, int]:
        driver = holding(engine_of(live_reg).approvals)
        task = start(live_reg, name, {"mids": targets})
        card = await card_for(driver, task)
        writer.connection.execute("UPDATE messages SET conflicted = 1 WHERE mid = ?", (targets[1],))
        at_decision = len(fb.calls)
        assert driver.decide(card, "approve") == "approved"
        return await outcome(task), driver, at_decision

    res, driver, at_decision = run(scenario)
    assert isinstance(res, ToolError)
    assert (res.code, res.reason) == ("mid_conflicted", "mid_conflicted")
    assert res.message == "target 2 of 2 has copies with different content under one Message-ID"
    assert methods(fb)[at_decision:] == []
    assert_not_executed(audit, fb, driver, "mid_conflicted", "denied")
    (rec,) = audit.list()
    assert rec.tier is tier and audit.events(rec.call_id) == []
    assert engine_of(live_reg).approvals.pending() == []


def test_a_target_dropped_from_the_index_while_pending_is_targets_changed(
    live_reg: Registry, fb: FakeBridge, audit: AuditLog, writer: Store
) -> None:
    targets = writable_mids(writer)[:2]

    async def scenario() -> tuple[ToolResult | ToolError, ScriptedApprovals]:
        driver = holding(engine_of(live_reg).approvals)
        task = start(live_reg, "tag2", {"mids": targets})
        card = await card_for(driver, task)
        writer.connection.execute("DELETE FROM locations WHERE mid = ?", (targets[0],))
        driver.decide(card, "approve")
        return await outcome(task), driver

    res, driver = run(scenario)
    assert isinstance(res, ToolError)
    assert (res.code, res.reason) == ("targets_changed", "not_indexed")
    assert res.message == "target 1 of 2 is no longer in the index"
    assert_not_executed(audit, fb, driver, "targets_changed", "targets_changed")
    (event,) = audit.events(audit.list()[0].call_id)
    assert (event.kind, event.detail) == (
        "targets_changed",
        {"code": "targets_changed", "reason": "not_indexed"},
    )
