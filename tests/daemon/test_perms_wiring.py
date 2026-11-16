"""Plan 06b step 7: ``ternd`` opens the audit log, builds the permission engine and adds its
write-status observer to the index Store.

The extension is a scripted client answered by FakeBridge (the synthetic mailbox), as in
``test_ingest_wiring.py``; its TB events go over the wire. ternd registers no W tool before plan
10, so the live write here runs a W1 dummy through a ``Registry`` built over ``Daemon.engine``,
as plans 07/08a will build ``Daemon.registry``.

Plan 06b step 10: ternd's ``audit.undo`` handler (roles ``extension`` and ``cli`` undo as caller
``ui``; ``mcp-proxy`` is ``role_rejected``), reached by ``tern undo`` over the socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
import pytest_asyncio
import test_fixture_manifest as tfm

from tern import cli, paths
from tern.bridge.client import Peer
from tern.bridge.protocol import (
    METHODS,
    BridgeError,
    FindParams,
    Mid,
    Request,
    UndoParams,
    UpdateParams,
    dump,
)
from tern.config import TernConfig
from tern.daemon.main import UNDO_ROLES, Daemon, audit_undo_handler, serve
from tern.index.store import Store, index_path
from tern.perms.approvals import Approvals
from tern.perms.audit import AuditLog, TargetState, UndoPayload, UndoTarget, audit_path
from tern.perms.recipients import DenyAllRecipients
from tern.search.service import SearchService
from tern.testing.bridge import NMHOST, Client, FakeProcessInfo, connect, hello_params
from tern.testing.fake_bridge import FakeBridge
from tern.testing.procs import wait_for_socket
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, tool
from tern.tools.contract import ToolError
from tern.tools.registry import RegisteredTool, Registry, ToolRuntime

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
COPIES = MANIFEST["message_count"]
BY_KEY = {m["key"]: m for m in MANIFEST["messages"]}
SINGLE = BY_KEY["inv-0042"]["mid"]  # acct1/Invoices only
SINGLE_HMID = BY_KEY["inv-0042"]["message_id"].strip("<>")
DEADLINE_S = 10.0


class MidsIn(BoundaryModel):
    mids: list[Mid]


class Out(BoundaryModel):
    n: int


@tool(name="tag", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def tag(ctx: ToolContext, inp: MidsIn) -> Out:
    """A set_tags-shaped W1 dummy."""
    ids = [loc.tb_id for locs in ctx.targets.values() for loc in locs]
    await ctx.bridge.request("mail.update", UpdateParams(tb_ids=ids, add_tags=["$label4"]))
    return Out(n=len(ids))


def fb_responder(fb: FakeBridge) -> Callable[[Peer, Request], Any]:
    async def respond(peer: Peer, req: Request) -> Any:
        if req.method == "bridge.ping":
            return {}
        spec = METHODS[req.method]
        return dump(await fb.request(req.method, spec.params.model_validate(req.params)))

    return respond


@dataclass
class Run:
    tp: paths.TernPaths
    task: asyncio.Task[None]
    stop: asyncio.Event
    started: list[Daemon] = field(default_factory=list)

    @property
    def daemon(self) -> Daemon:
        assert len(self.started) == 1, "on_started was not called exactly once"
        return self.started[0]

    async def until(self, cond: Callable[[], bool], what: str) -> None:
        deadline = time.perf_counter() + DEADLINE_S
        while not cond():
            if self.task.done():
                self.task.result()
                raise AssertionError(f"serve() returned before {what}")
            assert time.perf_counter() < deadline, f"timed out waiting for {what}"
            await asyncio.sleep(0.01)

    def backfill_done(self) -> bool:
        conn = self.daemon.engine.index.connection
        rows = conn.execute("SELECT status FROM ingest_state").fetchall()
        located = conn.execute("SELECT count(*) FROM locations").fetchone()[0]
        return bool(rows) and {r[0] for r in rows} == {"done"} and located == COPIES


async def start(tp: paths.TernPaths) -> Run:
    tp.run.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = asyncio.Event()
    started: list[Daemon] = []
    task = asyncio.create_task(
        serve(
            tp,
            TernConfig(),
            process_info=FakeProcessInfo(),
            nmhost_path=NMHOST,
            stop=stop,
            on_started=started.append,
        )
    )
    await asyncio.to_thread(wait_for_socket, tp.socket)
    return Run(tp, task, stop, started)


@pytest_asyncio.fixture
async def run() -> AsyncIterator[Run]:
    r = await start(paths.resolve())
    yield r
    r.stop.set()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(r.task, DEADLINE_S)


async def extension(r: Run, fb: FakeBridge, profile_id: str | None = None) -> Client:
    c = await connect(
        r.tp.socket,
        "extension",
        serve=fb_responder(fb),
        hello=hello_params(
            "extension", profile_id=str(uuid.uuid4()) if profile_id is None else profile_id
        ),
    )
    fb.on_event = c.peer.notify
    return c


def registry_over(d: Daemon) -> Registry:
    """A registry over ternd's engine (what plans 07/08a's ``Daemon.registry`` will be)."""
    index = d.engine.index
    runtime = ToolRuntime(
        audit=d.audit,
        index=index,
        search=SearchService(index, None, mode="fts"),
        bridge=d.server.link,
        config=TernConfig(),
        log=logging.getLogger("tern.tools.test_perms_wiring"),
        clock=time.time,
        engine=d.engine,
    )
    return Registry([RegisteredTool(tag, "plugin", "test")], runtime=runtime)


def derived_status(tp: paths.TernPaths, call_id: str) -> str | None:
    """The call's status as a separate reader of ``<TERN_HOME>/state/audit.db`` sees it."""
    with AuditLog.open(audit_path(tp)) as reader:
        return reader.status(call_id).status


@pytest.mark.asyncio
async def test_ternd_wires_the_audit_log_engine_and_observer(run: Run) -> None:
    d = run.daemon
    assert d.audit.path == audit_path(run.tp) == run.tp.state / "audit.db"
    assert d.engine.audit is d.audit
    assert d.engine.index.readonly and d.engine.index is not d.store
    assert d.engine.index.path == d.store.path == index_path(run.tp)
    assert d.engine.bridge is d.server.link
    assert d.engine.approvals.notifier is None  # no approval UI before plan 12
    assert isinstance(d.engine._recipients, DenyAllRecipients)  # pyright: ignore[reportPrivateUsage] -- pins the deny-all provider
    # One clock for the engine (its rate window) and the audit log (step 4 review note).
    assert d.engine.clock is time.time
    assert d.audit._clock is time.time  # pyright: ignore[reportPrivateUsage] -- AuditLog has no clock accessor (audit.py is not to be touched)
    assert d.store.location_observers.count(d.engine.write_status.observe) == 1


@pytest.mark.asyncio
async def test_a_write_in_ternd_is_observed_in_the_audit_db(run: Run) -> None:
    fb = FakeBridge(FIXTURE)
    c = await extension(run, fb)
    await run.until(run.backfill_done, "the backfill")
    reg = registry_over(run.daemon)
    res = await reg.call("agent:s1", "tag", {"mids": [SINGLE]})
    assert res.undo_token is not None
    await run.until(lambda: derived_status(run.tp, res.call_id) == "observed", "the observed event")
    with AuditLog.open(audit_path(run.tp)) as reader:
        assert [e.kind for e in reader.events(res.call_id)] == ["observed"]
    assert run.daemon.engine.write_status.pending() == frozenset()
    await c.close()


async def stop_run(r: Run) -> None:
    r.stop.set()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(r.task, DEADLINE_S)


@pytest.mark.asyncio
async def test_ternd_loads_pending_writes_at_start() -> None:
    """A write of a previous run, still ``tb_accepted`` (seeded into ``audit.db`` between two
    runs): loaded at start, not settled by the check on register (the index still shows the prev
    state), and observed when TB's event brings the location to its post state."""
    tp = paths.resolve()
    fb = FakeBridge(FIXTURE)
    profile = str(uuid.uuid4())
    first = await start(tp)
    try:
        c = await extension(first, fb, profile)
        await first.until(first.backfill_done, "the backfill")
        await c.close()
    finally:
        await stop_run(first)
    [loc] = (await fb.request("mail.messages.find", FindParams(header_message_id=SINGLE_HMID))).root
    prev = TargetState(
        folder_id=loc.folder_id, tags=loc.tags, flagged=loc.flags.flagged, read=loc.flags.read
    )
    post = prev.model_copy(update={"tags": sorted({*prev.tags, "$label4"})})
    assert post != prev
    with AuditLog.open(audit_path(tp)) as log:
        call_id = log.append_call(
            caller="agent:s0",
            tool="tag",
            tier=Tier.W1,
            decision="auto",
            args={"mids": [SINGLE]},
            args_validated=True,
            target_mids=[SINGLE],
            target_count=1,
            initial_status="tb_accepted",
        )
        log.add_undo_token(
            call_id, UndoPayload(targets=[UndoTarget(mid=SINGLE, prev=prev, post=post)])
        )
    second = await start(tp)
    try:
        assert second.daemon.engine.write_status.pending() == {call_id}
        c = await extension(second, fb, profile)
        await second.until(second.backfill_done, "the attach")
        assert derived_status(tp, call_id) == "tb_accepted"
        await fb.update(loc.tb_id, tags=post.tags)
        await second.until(lambda: derived_status(tp, call_id) == "observed", "the observed event")
        assert second.daemon.engine.write_status.pending() == frozenset()
        await c.close()
    finally:
        await stop_run(second)


@pytest.mark.asyncio
async def test_shutdown_resolves_approvals_before_the_audit_log_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    real_shutdown = Approvals.shutdown
    real_audit_close = AuditLog.close
    real_store_close = Store.close

    def shutdown(self: Approvals) -> int:
        order.append("approvals shutdown")
        return real_shutdown(self)

    def audit_close(self: AuditLog) -> None:
        order.append("audit closed")
        real_audit_close(self)

    def store_close(self: Store) -> None:
        order.append("reader closed" if self.readonly else "index closed")
        real_store_close(self)

    monkeypatch.setattr(Approvals, "shutdown", shutdown)
    monkeypatch.setattr(AuditLog, "close", audit_close)
    monkeypatch.setattr(Store, "close", store_close)
    r = await start(paths.resolve())
    r.stop.set()
    await asyncio.wait_for(r.task, DEADLINE_S)
    assert order == ["approvals shutdown", "audit closed", "reader closed", "index closed"]


# ---- audit.undo (plan 06b step 10) -----------------------------------------------------------


async def single_state(fb: FakeBridge) -> TargetState:
    [loc] = (await fb.request("mail.messages.find", FindParams(header_message_id=SINGLE_HMID))).root
    return TargetState(
        folder_id=loc.folder_id, tags=loc.tags, flagged=loc.flags.flagged, read=loc.flags.read
    )


def call_count(tp: paths.TernPaths) -> int:
    with AuditLog.open(audit_path(tp)) as reader:
        return reader.count()


async def tern(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """``tern <argv>`` in a thread (its own event loop, as the real CLI process has)."""
    code = await asyncio.to_thread(cli.main, list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.asyncio
async def test_tern_undo_undoes_a_w1_write_through_ternd(
    run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    fb = FakeBridge(FIXTURE)
    c = await extension(run, fb)
    await run.until(run.backfill_done, "the backfill")
    before = await single_state(fb)
    res = await registry_over(run.daemon).call("agent:s1", "tag", {"mids": [SINGLE]})
    assert res.undo_token is not None
    assert "$label4" in (await single_state(fb)).tags and "$label4" not in before.tags

    code, out, err = await tern(capsys, "undo", res.undo_token)
    assert (code, err) == (0, "") and out.startswith("undone")
    assert await single_state(fb) == before
    with AuditLog.open(audit_path(run.tp)) as reader:
        assert reader.status(res.call_id).undone
        [undo_row] = reader.list(limit=1)
        assert (undo_row.tool, undo_row.caller, undo_row.decision) == ("undo", "ui", "auto")
        assert undo_row.args == {"undo_of": res.call_id} and undo_row.undo_token is not None

    # tern audit (a reader beside the running ternd) shows the write undone and the redo token.
    code, out, err = await tern(capsys, "audit", "--last", "2")
    assert (code, err) == (0, "")
    tag_line, undo_line, stale = out.splitlines()
    assert "tool=tag" in tag_line and "undone=yes" in tag_line
    assert f"undo_token={res.undo_token}" in tag_line
    assert "tool=undo tier=W1 caller=ui decision=auto" in undo_line
    assert f"undo_token={undo_row.undo_token}" in undo_line
    assert stale == "stale writes (>24 h tb_accepted): 0"
    await c.close()


@pytest.mark.asyncio
async def test_a_used_or_unknown_token_is_refused_with_its_bridge_name(
    run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    fb = FakeBridge(FIXTURE)
    c = await extension(run, fb)
    await run.until(run.backfill_done, "the backfill")
    res = await registry_over(run.daemon).call("agent:s1", "tag", {"mids": [SINGLE]})
    assert res.undo_token is not None
    assert (await tern(capsys, "undo", res.undo_token))[0] == 0
    after = await single_state(fb)

    code, out, err = await tern(capsys, "undo", res.undo_token)
    assert (code, out) == (1, "")
    assert err.startswith("undo: state_changed: ") and "[state_changed/already_undone]" in err
    code, out, err = await tern(capsys, "undo", "0" * 32)
    assert (code, out) == (1, "")
    assert err.startswith("undo: invalid_params: ") and "[invalid_params/unknown]" in err
    assert await single_state(fb) == after
    await c.close()


@pytest.mark.asyncio
async def test_undo_without_an_extension_is_unavailable(
    run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    fb = FakeBridge(FIXTURE)
    c = await extension(run, fb)
    await run.until(run.backfill_done, "the backfill")
    res = await registry_over(run.daemon).call("agent:s1", "tag", {"mids": [SINGLE]})
    assert res.undo_token is not None
    await c.close()
    await run.until(lambda: run.daemon.server.link.active is None, "the extension to detach")
    run.daemon.server.link.wait_s = 0.2
    code, out, err = await tern(capsys, "undo", res.undo_token)
    assert (code, out) == (1, "") and err.startswith("undo: unavailable: ")
    assert "[unavailable/" in err  # Registry.undo's own refusal, not an unwired method
    with AuditLog.open(audit_path(run.tp)) as reader:
        assert not reader.status(res.call_id).undone
        [undo_row] = reader.list(limit=1)
        assert (undo_row.tool, undo_row.caller) == ("undo", "ui")
        assert (undo_row.decision, undo_row.reason) == ("error", "unavailable")


@pytest.mark.asyncio
async def test_an_mcp_proxy_audit_undo_is_role_rejected(run: Run) -> None:
    fb = FakeBridge(FIXTURE)
    c = await extension(run, fb)
    await run.until(run.backfill_done, "the backfill")
    res = await registry_over(run.daemon).call("agent:s1", "tag", {"mids": [SINGLE]})
    assert res.undo_token is not None
    rows = call_count(run.tp)
    after = await single_state(fb)
    proxy = await connect(run.tp.socket, "mcp-proxy")
    with pytest.raises(BridgeError) as exc:
        await proxy.peer.request("audit.undo", {"undo_token": res.undo_token}, timeout=10)
    assert exc.value.name == "role_rejected"
    # Refused before the registry: no undo row, nothing undone, nothing written.
    assert call_count(run.tp) == rows
    with AuditLog.open(audit_path(run.tp)) as reader:
        assert not reader.status(res.call_id).undone
    assert await single_state(fb) == after
    await proxy.close()
    await c.close()


@pytest.mark.asyncio
async def test_the_extension_undoes_as_ui(run: Run) -> None:
    fb = FakeBridge(FIXTURE)
    c = await extension(run, fb)
    await run.until(run.backfill_done, "the backfill")
    before = await single_state(fb)
    res = await registry_over(run.daemon).call("agent:s1", "tag", {"mids": [SINGLE]})
    assert res.undo_token is not None
    raw = await c.peer.request("audit.undo", {"undo_token": res.undo_token}, timeout=10)
    assert raw == {"undone": True}
    assert await single_state(fb) == before
    with AuditLog.open(audit_path(run.tp)) as reader:
        assert reader.list(limit=1)[0].caller == "ui"
    await c.close()


class FakeRole:
    def __init__(self, role: str) -> None:
        self.role = role


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "reason", "name"),
    [
        ("invalid_params", "unknown", "invalid_params"),
        ("state_changed", "not_locatable", "state_changed"),
        ("mid_conflicted", "mid_conflicted", "state_changed"),
        ("unavailable", "tb_error", "unavailable"),
        ("internal", "audit_failed", "unavailable"),
        ("denied", "undo_not_allowed", "role_rejected"),
    ],
)
async def test_undo_refusals_map_to_bridge_names(
    monkeypatch: pytest.MonkeyPatch, code: Any, reason: str, name: str
) -> None:
    async def refuse(self: Registry, caller: str, token: str) -> Any:
        assert caller == "ui"
        raise ToolError(code, "refused", reason=reason)

    monkeypatch.setattr(Registry, "undo", refuse)
    handler = audit_undo_handler(Registry(()))
    with pytest.raises(BridgeError) as exc:
        await handler(cast("Peer", FakeRole("cli")), UndoParams(undo_token="0" * 32))
    assert exc.value.name == name and exc.value.message == f"refused [{code}/{reason}]"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["mcp-proxy", "daemon"])
async def test_only_the_extension_and_the_cli_may_undo(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    async def never(self: Registry, caller: str, token: str) -> Any:
        raise AssertionError("the registry must not be reached")

    monkeypatch.setattr(Registry, "undo", never)
    handler = audit_undo_handler(Registry(()))
    with pytest.raises(BridgeError) as exc:
        await handler(cast("Peer", FakeRole(role)), UndoParams(undo_token="0" * 32))
    assert exc.value.name == "role_rejected"
    assert UNDO_ROLES == {"extension", "cli"}
