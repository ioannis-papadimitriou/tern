"""Plan 06b step 7: write status.

An executed write's row stays ``tb_accepted`` until the index sees every target in its recorded
post state; then :class:`~tern.perms.write_status.WriteStatus` appends one ``observed`` event.
Here the index is a writable copy of the session's fixture index, its TB events applied by the
real :class:`~tern.index.events.EventIngest` from FakeBridge's notifications (buffered, so a
test decides whether they land before or after the write is registered). The engine reads the
copy through its own read-only Store, as in ternd."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tern.bridge.protocol import DeleteParams, Mid, MoveParams, UpdateParams
from tern.config import TernConfig
from tern.index.events import EventIngest
from tern.index.source import IngestSource
from tern.index.store import LocationState, Store
from tern.perms.audit import AuditLog, TargetState, UndoPayload, UndoTarget
from tern.perms.engine import Engine
from tern.perms.write_status import (
    LOAD_WINDOW_S,
    STALE_AFTER_S,
    WriteStatus,
    conditions,
    stale_writes,
)
from tern.testing.approvals import approve_all
from tern.testing.fake_bridge import FakeBridge
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, tool
from tern.tools.registry import Registry, ToolResult

from .conftest import FIXTURE, Clock, engine_of

MANIFEST = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
BY_KEY = {m["key"]: m for m in MANIFEST["messages"]}
SINGLE = BY_KEY["inv-0042"]["mid"]  # acct1/Invoices only
SINGLE_FOLDER = "acct1:/Invoices"
DUP = BY_KEY["dup-1"]["mid"]  # acct2/INBOX + acct2/Projects
DUP_INBOX = "acct2:/INBOX"
DUP_PROJECTS = "acct2:/Projects"
OTHER = BY_KEY["invoice-0013"]["mid"]  # acct1/Invoices only
THIRD = BY_KEY["invoice-0027"]["mid"]  # acct1/Invoices only
LABEL = "$label4"


class MidsIn(BoundaryModel):
    mids: list[Mid]


class DeleteIn(BoundaryModel):
    mids: list[Mid]
    folder_id: str | None = None


class Out(BoundaryModel):
    n: int


def _tb_ids(ctx: ToolContext) -> list[int]:
    return [loc.tb_id for locs in ctx.targets.values() for loc in locs]


@tool(name="tag", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def tag(ctx: ToolContext, inp: MidsIn) -> Out:
    """A set_tags-shaped W1 dummy: adds LABEL on every engine-resolved location."""
    ids = _tb_ids(ctx)
    await ctx.bridge.request("mail.update", UpdateParams(tb_ids=ids, add_tags=[LABEL]))
    return Out(n=len(ids))


@tool(name="trash", tier=Tier.W3, effects={Effect.MAIL_DELETE}, untrusted_output=False)
async def trash(ctx: ToolContext, inp: DeleteIn) -> Out:
    """A delete-shaped dummy: moves every engine-resolved location to the account's Trash."""
    ids = _tb_ids(ctx)
    await ctx.bridge.request("mail.delete", DeleteParams(tb_ids=ids))
    return Out(n=len(ids))


@tool(name="move", tier=Tier.W2, effects={Effect.MAIL_MOVE}, untrusted_output=False)
async def move(ctx: ToolContext, inp: DeleteIn) -> Out:
    """A move-shaped dummy: moves every engine-resolved location to DUP_PROJECTS."""
    ids = _tb_ids(ctx)
    await ctx.bridge.request("mail.move", MoveParams(tb_ids=ids, dest_folder_id=DUP_PROJECTS))
    return Out(n=len(ids))


TOOLS = [tag, trash, move]


class Events:
    """FakeBridge's TB notifications, held until :meth:`apply` hands them to the index (or at
    once with ``direct``)."""

    def __init__(self, fb: FakeBridge, store: Store) -> None:
        self.ingest = EventIngest(store, IngestSource(fb), TernConfig())
        self.held: list[tuple[str, BaseModel]] = []
        self.direct = False
        fb.on_event = self.sink

    async def sink(self, method: str, params: BaseModel) -> None:
        if self.direct:
            await self.ingest.on_event(method, params)
        else:
            self.held.append((method, params))

    async def apply(self, n: int | None = None) -> None:
        """Apply the first ``n`` held events (all by default)."""
        take = len(self.held) if n is None else n
        batch, self.held = self.held[:take], self.held[take:]
        for method, params in batch:
            await self.ingest.on_event(method, params)


@pytest.fixture
def index_copy(fixture_index: Path, tmp_path: Path) -> Path:
    """A writable copy of the session's fixture index."""
    dst = tmp_path / "index.db"
    src = sqlite3.connect(f"file:{fixture_index}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


@pytest.fixture
def writer(index_copy: Path) -> Iterator[Store]:
    store = Store.open(index_copy)
    yield store
    store.close()


@pytest.fixture
def reader(index_copy: Path, writer: Store) -> Iterator[Store]:
    store = Store.open(index_copy, readonly=True)
    yield store
    store.close()


@pytest.fixture
def fb() -> FakeBridge:
    return FakeBridge(FIXTURE)


@pytest.fixture
def events(fb: FakeBridge, writer: Store) -> Events:
    return Events(fb, writer)


@pytest.fixture
def reg(make_registry: Any, fb: FakeBridge, reader: Store, writer: Store) -> Registry:
    registry = make_registry(TOOLS, bridge=fb, index=reader)
    writer.location_observers.append(engine_of(registry).write_status.observe)
    return registry


def ws(reg: Registry) -> WriteStatus:
    return engine_of(reg).write_status


def call(reg: Registry, name: str, mids: list[str]) -> ToolResult:
    return asyncio.run(reg.call("agent:s1", name, {"mids": mids}))


def observed_events(audit: AuditLog, call_id: str) -> list[dict[str, Any]]:
    return [e.detail for e in audit.events(call_id) if e.kind == "observed"]


def payload(audit: AuditLog, result: ToolResult) -> UndoPayload:
    assert result.undo_token is not None
    return audit.undo_token(result.undo_token).payload


# ---- observed through the observer ------------------------------------------------------------


def test_a_tag_event_reaching_the_index_makes_the_write_observed(
    reg: Registry, audit: AuditLog, events: Events
) -> None:
    res = call(reg, "tag", [SINGLE])
    [target] = payload(audit, res).targets
    assert target.prev != target.post  # the write changed something to observe
    assert audit.status(res.call_id).status == "tb_accepted"
    assert ws(reg).pending() == {res.call_id}
    assert [m for m, _ in events.held] == ["event.mail.updated"]

    asyncio.run(events.apply())

    assert audit.status(res.call_id).status == "observed"
    assert observed_events(audit, res.call_id) == [{"locations": 1}]
    assert ws(reg).pending() == frozenset()


def test_every_location_must_be_observed(reg: Registry, audit: AuditLog, events: Events) -> None:
    res = call(reg, "tag", [DUP])
    assert len(payload(audit, res).targets) == 2
    assert len(events.held) == 2  # one event.mail.updated per folder

    asyncio.run(events.apply(1))
    assert audit.status(res.call_id).status == "tb_accepted"
    assert ws(reg).pending() == {res.call_id}

    asyncio.run(events.apply())
    assert audit.status(res.call_id).status == "observed"
    assert observed_events(audit, res.call_id) == [{"locations": 2}]


def test_a_delete_to_trash_is_observed_on_the_source_removal(
    reg: Registry, audit: AuditLog, events: Events, reader: Store
) -> None:
    approve_all(engine_of(reg).approvals)
    res = call(reg, "trash", [SINGLE])
    [target] = payload(audit, res).targets
    assert target.prev is not None and target.prev.folder_id == SINGLE_FOLDER
    assert target.post is not None and target.post.folder_id != SINGLE_FOLDER  # the Trash
    assert ws(reg).pending() == {res.call_id}

    asyncio.run(events.apply())

    assert [loc.folder_id for loc in reader.locations(SINGLE)] == []
    assert audit.status(res.call_id).status == "observed"
    assert observed_events(audit, res.call_id) == [{"locations": 1}]


def test_a_move_into_a_folder_holding_a_copy_waits_for_the_source_removal(
    reg: Registry, audit: AuditLog, events: Events, reader: Store
) -> None:
    """DUP's Projects copy already shows the moved copy's post state, so the destination alone
    proves nothing: the write is observed only once the INBOX location is seen removed."""
    approve_all(engine_of(reg).approvals)
    res = asyncio.run(reg.call("agent:s1", "move", {"mids": [DUP], "folder_id": DUP_INBOX}))
    [target] = payload(audit, res).targets
    assert target.prev is not None and target.prev.folder_id == DUP_INBOX
    assert target.post is not None and target.post.folder_id == DUP_PROJECTS
    here = {loc.folder_id: loc.state for loc in reader.locations(DUP)}
    assert here[DUP_PROJECTS] == LocationState(
        tags=tuple(sorted(set(target.post.tags))),
        flagged=target.post.flagged,
        read=target.post.read,
    )  # the destination already shows the post state before any event
    assert DUP_INBOX in here
    assert audit.status(res.call_id).status == "tb_accepted"
    assert ws(reg).pending() == {res.call_id}

    asyncio.run(events.apply())

    assert DUP_INBOX not in {loc.folder_id for loc in reader.locations(DUP)}
    assert audit.status(res.call_id).status == "observed"
    assert observed_events(audit, res.call_id) == [{"locations": 2}]


# ---- check on register ------------------------------------------------------------------------


def test_an_event_applied_before_registration_is_observed_by_the_check_on_register(
    reg: Registry, audit: AuditLog, events: Events, reader: Store
) -> None:
    events.direct = True  # the index applies TB's event while the tool's request runs
    res = call(reg, "tag", [SINGLE])
    [loc] = reader.locations(SINGLE)
    assert LABEL in loc.state.tags
    assert audit.status(res.call_id).status == "observed"
    assert observed_events(audit, res.call_id) == [{"locations": 1}]
    assert ws(reg).pending() == frozenset()


def test_a_failed_check_on_register_keeps_the_write_watched(
    reg: Registry,
    audit: AuditLog,
    events: Events,
    reader: Store,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The write and its token stand: the call returns its token, the failure is logged at
    ERROR, and the observer settles the write later."""

    real = Store.locations.__get__(reader)
    seen: list[str] = []

    def locations(mid: str) -> list[Any]:
        seen.append(mid)
        if len(seen) == 2:  # 1: target resolution; 2: the check on register
            raise sqlite3.OperationalError("database is locked")
        return real(mid)

    monkeypatch.setattr(reader, "locations", locations)
    caplog.set_level(logging.INFO)

    res = call(reg, "tag", [SINGLE])

    assert res.undo_token is not None
    assert ws(reg).pending() == {res.call_id}
    errors = [
        (r.getMessage(), r.__dict__.get("call_id"), r.__dict__.get("error"))
        for r in caplog.records
        if r.levelno >= logging.ERROR
    ]
    assert errors == [
        ("write status check failed: the write stays watched", res.call_id, "OperationalError")
    ]
    assert seen == [SINGLE, SINGLE]
    asyncio.run(events.apply())
    assert audit.status(res.call_id).status == "observed"


def test_a_failed_folder_read_on_register_says_the_write_is_not_watched(
    reg: Registry,
    audit: AuditLog,
    reader: Store,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure before the conditions are known leaves the write unwatched until the next
    load; the ERROR says so, and the call still returns its token."""

    def folders() -> list[Any]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reader, "folders", folders)
    caplog.set_level(logging.INFO)

    res = call(reg, "tag", [SINGLE])

    assert res.undo_token is not None
    assert ws(reg).pending() == frozenset()
    errors = [
        (r.getMessage(), r.__dict__.get("call_id"), r.__dict__.get("error"))
        for r in caplog.records
        if r.levelno >= logging.ERROR
    ]
    assert errors == [
        ("write status check failed: not watched until restart", res.call_id, "OperationalError")
    ]
    assert audit.status(res.call_id).status == "tb_accepted"


# ---- not applied: tb_accepted, stale after 24 h -----------------------------------------------


def test_a_write_tb_accepts_without_applying_stays_tb_accepted_and_goes_stale(
    reg: Registry, audit: AuditLog, events: Events, fb: FakeBridge, clock: Clock
) -> None:
    fb.apply_writes = False
    res = call(reg, "tag", [SINGLE])
    assert res.undo_token is not None
    [target] = payload(audit, res).targets
    assert target.prev == target.post  # TB reports no change: nothing to observe
    assert events.held == []
    asyncio.run(events.apply())
    assert ws(reg).pending() == frozenset()  # not watched
    assert audit.status(res.call_id).status == "tb_accepted"

    assert stale_writes(audit, clock()) == 0
    clock.t += STALE_AFTER_S
    assert stale_writes(audit, clock()) == 0  # exactly 24 h: not yet
    clock.t += 1
    assert stale_writes(audit, clock()) == 1


def test_an_observed_write_is_never_stale(
    reg: Registry, audit: AuditLog, events: Events, clock: Clock
) -> None:
    res = call(reg, "tag", [SINGLE])
    asyncio.run(events.apply())
    assert audit.status(res.call_id).status == "observed"
    clock.t += STALE_AFTER_S + 1
    assert stale_writes(audit, clock()) == 0


def test_the_stale_window_is_pinned() -> None:
    assert (STALE_AFTER_S, LOAD_WINDOW_S) == (86_400, 7 * 86_400)


# ---- loaded at start --------------------------------------------------------------------------


def fresh(audit: AuditLog, reader: Store, writer: Store, clock: Clock) -> WriteStatus:
    """A new WriteStatus over the same log and index (a ternd restart), observing ``writer``."""
    status = WriteStatus(audit, reader, clock)
    writer.location_observers.clear()
    writer.location_observers.append(status.observe)
    return status


def test_pending_writes_are_loaded_from_the_audit_log(
    reg: Registry, audit: AuditLog, events: Events, reader: Store, writer: Store, clock: Clock
) -> None:
    res = call(reg, "tag", [SINGLE])
    restarted = fresh(audit, reader, writer, clock)
    assert restarted.load() == 1
    assert restarted.pending() == {res.call_id}
    asyncio.run(events.apply())
    assert audit.status(res.call_id).status == "observed"
    assert restarted.pending() == frozenset()


def test_a_loaded_write_whose_event_already_landed_is_observed_at_load(
    reg: Registry, audit: AuditLog, events: Events, reader: Store, writer: Store, clock: Clock
) -> None:
    res = call(reg, "tag", [SINGLE])
    writer.location_observers.clear()  # ternd was down when the event landed
    asyncio.run(events.apply())
    assert audit.status(res.call_id).status == "tb_accepted"
    restarted = fresh(audit, reader, writer, clock)
    assert restarted.load() == 0
    assert audit.status(res.call_id).status == "observed"


def test_load_skips_old_undone_and_settled_writes(
    reg: Registry, audit: AuditLog, events: Events, reader: Store, writer: Store, clock: Clock
) -> None:
    old = call(reg, "tag", [SINGLE])
    clock.t += LOAD_WINDOW_S + 1
    events.held.clear()  # the old write's event never lands
    undone = call(reg, "tag", [DUP])
    audit.append_event(undone.call_id, "undone", {})
    events.held.clear()
    settled = call(reg, "tag", [OTHER])
    asyncio.run(events.apply())
    assert audit.status(settled.call_id).status == "observed"
    pending = call(reg, "tag", [THIRD])  # events held: stays tb_accepted
    restarted = fresh(audit, reader, writer, clock)
    assert restarted.load() == 1
    assert restarted.pending() == {pending.call_id}
    assert restarted.load() == 1  # a second load skips what is already watched (no ValueError)
    assert restarted.pending() == {pending.call_id}
    assert stale_writes(audit, clock()) == 1  # the old one is still counted
    assert audit.status(old.call_id).status == "tb_accepted"


def test_an_observer_failure_propagates_and_the_write_stays_watched(
    reg: Registry, audit: AuditLog, events: Events, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = call(reg, "tag", [SINGLE])

    def refuse(*args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(audit, "append_event", refuse)
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(events.apply())
    assert ws(reg).pending() == {res.call_id}
    assert audit.status(res.call_id).status == "tb_accepted"


# ---- the conditions ---------------------------------------------------------------------------

INDEXED = "acct1:/INBOX"
TRASH = "acct1:/Trash"
OTHER_INDEXED = "acct1:/Invoices"
JUNK = "acct1:/Junk"


def state(folder: str, *tags: str, read: bool = False) -> TargetState:
    return TargetState(folder_id=folder, tags=list(tags), flagged=False, read=read)


def conds(*targets: UndoTarget) -> dict[tuple[str, str], set[LocationState | None]] | None:
    is_indexed: Callable[[str], bool] = lambda f: f not in (TRASH, JUNK)  # noqa: E731  # a short predicate
    return conditions(UndoPayload(targets=list(targets)), is_indexed)


def test_conditions_by_destination() -> None:
    m = SINGLE
    tagged = UndoTarget(mid=m, prev=state(INDEXED), post=state(INDEXED, "$x", "$a"))
    assert conds(tagged) == {(m, INDEXED): {LocationState(tags=("$a", "$x"))}}
    trashed = UndoTarget(mid=m, prev=state(INDEXED), post=state(TRASH))
    assert conds(trashed) == {(m, INDEXED): {None}}
    removed = UndoTarget(mid=m, prev=state(INDEXED), post=None)
    assert conds(removed) == {(m, INDEXED): {None}}
    restored = UndoTarget(mid=m, prev=state(TRASH), post=state(INDEXED, read=True))
    assert conds(restored) == {(m, INDEXED): {LocationState(read=True)}}
    moved = UndoTarget(mid=m, prev=state(INDEXED), post=state(OTHER_INDEXED, "$a"))
    assert conds(moved) == {  # the destination alone cannot confirm a move
        (m, OTHER_INDEXED): {LocationState(tags=("$a",))},
        (m, INDEXED): {None},
    }


def test_unobservable_and_unchanged_targets() -> None:
    m = SINGLE
    junk_to_trash = UndoTarget(mid=m, prev=state(JUNK), post=state(TRASH))
    unchanged = UndoTarget(mid=m, prev=state(INDEXED, "$a"), post=state(INDEXED, "$a"))
    changed = UndoTarget(mid=DUP, prev=state(INDEXED), post=state(INDEXED, "$a"))
    assert conds(junk_to_trash) is None
    assert conds(junk_to_trash, changed) is None  # one target cannot be verified: not watched
    assert conds(unchanged) is None  # nothing changed: not watched
    assert conds(unchanged, changed) == {(DUP, INDEXED): {LocationState(tags=("$a",))}}
    created = UndoTarget(mid=m, prev=None, post=state(TRASH))
    assert conds(created) is None


def test_register_refuses_a_write_twice(reg: Registry, audit: AuditLog) -> None:
    res = call(reg, "tag", [SINGLE])
    with pytest.raises(ValueError, match="already watched"):
        ws(reg).register(res.call_id, payload(audit, res))


def test_the_engine_builds_its_write_status(audit: AuditLog, reader: Store, clock: Clock) -> None:
    class NoRecipients:
        async def is_known(self, addr: str) -> bool:
            return False

    eng = Engine(
        audit=audit, index=reader, bridge=None, clock=clock, known_recipients=NoRecipients()
    )
    assert isinstance(eng.write_status, WriteStatus)
    assert eng.write_status is eng.write_status
    assert eng.write_status.pending() == frozenset()
