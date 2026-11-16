"""Plan 07 part B step 8: ternd's ``tools.list``/``tools.call``/``audit.status``/``daemon.info``
(``tern.daemon.handlers``, decisions #37), over a full in-process ternd under a temp ``TERN_HOME``.

- every ``mcp-proxy`` connection gets its own ternd-assigned id; its calls are audited as
  ``mcp:<id>`` with the client name as ``caller_label`` only (two proxies with the same name get
  different callers);
- ``tools.list`` is the registry minus forbidden-effect tools, as ``ToolMeta`` documents;
- a tool's refusal or failure is result data (``ok: false``), with its audit row;
- roles other than ``mcp-proxy`` cannot list or call tools, and the proxy cannot reach
  approvals or undo;
- closing the proxy's connection mid-call cancels the call (the row's cancelled event).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import test_fixture_manifest as tfm

from tern import paths
from tern.bridge.client import Peer
from tern.bridge.protocol import METHODS, BridgeError, Request, ToolCallResult, dump
from tern.config import PluginsConfig, TernConfig
from tern.daemon import handlers
from tern.daemon.main import Daemon, serve
from tern.perms.audit import AuditLog, AuditRecord, audit_path
from tern.testing.bridge import NMHOST, Client, FakeProcessInfo, connect, hello_params
from tern.testing.fake_bridge import FakeBridge
from tern.testing.procs import wait_for_socket
from tern.tools.contract import ToolMeta

FIXTURE = tfm.FIXTURE
DEADLINE_S = 10.0
CLIENT = "LM Studio"
HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")

PLUGIN = '''
import asyncio
from pathlib import Path

from pydantic import field_serializer

from tern.bridge.protocol import Mid
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, tool

STARTED = Path(@STARTED@)


class In(BoundaryModel):
    pass


class Out(BoundaryModel):
    n: int


class MidsIn(BoundaryModel):
    mids: list[Mid]


@tool(name="slow_read", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def slow_read(ctx: ToolContext, inp: In) -> Out:
    """Wait until cancelled, then take a while to stop (a slow cleanup)."""
    STARTED.touch()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.sleep(0.3)
        raise
    return Out(n=0)


class BadOut(BoundaryModel):
    n: int

    @field_serializer("n")
    def _boom(self, value: int) -> int:
        raise ValueError("SENTINEL-serialize-4d2a")


@tool(name="bad_output", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def bad_output(ctx: ToolContext, inp: In) -> BadOut:
    """Returns an output whose serializer fails."""
    return BadOut(n=1)


@tool(name="filter_dummy", tier=Tier.W1, effects={Effect.FILTERS_WRITE}, untrusted_output=False)
async def filter_dummy(ctx: ToolContext, inp: MidsIn) -> Out:
    """A forbidden-effect tool: never listed, refused by name."""
    return Out(n=1)


TOOLS = [slow_read, filter_dummy, bad_output]
'''


@dataclass
class Run:
    tp: paths.TernPaths
    task: asyncio.Task[None]
    stop: asyncio.Event
    started_flag: Path
    started: list[Daemon] = field(default_factory=list[Daemon])

    @property
    def daemon(self) -> Daemon:
        assert len(self.started) == 1
        return self.started[0]


async def until(cond: Callable[[], bool], what: str) -> None:
    deadline = time.perf_counter() + DEADLINE_S
    while not cond():
        assert time.perf_counter() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def running(tmp_path: Path, **cfg_kw: Any) -> AsyncIterator[Run]:
    flag = tmp_path / "slow-started"
    plugin = tmp_path / "handlers_plugin.py"
    plugin.write_text(PLUGIN.replace("@STARTED@", repr(str(flag))), encoding="utf-8")
    cfg = TernConfig(embed_model="", plugins=PluginsConfig(paths=[plugin]), **cfg_kw)
    tp = paths.resolve()
    tp.run.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = asyncio.Event()
    started: list[Daemon] = []
    task = asyncio.create_task(
        serve(
            tp,
            cfg,
            process_info=FakeProcessInfo(),
            nmhost_path=NMHOST,
            stop=stop,
            on_started=started.append,
        )
    )
    try:
        # Not wait_for_socket alone: a ternd that fails to start never creates the socket.
        await until(lambda: bool(started) or task.done(), "on_started")
        if task.done():
            task.result()
        await asyncio.to_thread(wait_for_socket, tp.socket)
        yield Run(tp, task, stop, flag, started)
    finally:
        stop.set()
        if not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, DEADLINE_S)


@pytest_asyncio.fixture
async def run(tmp_path: Path) -> AsyncIterator[Run]:
    async with running(tmp_path) as r:
        yield r


def records(tp: paths.TernPaths) -> list[AuditRecord]:
    """Every audit row, oldest first, read beside ternd."""
    with AuditLog.open(audit_path(tp)) as reader:
        return list(reversed(reader.list(limit=1000)))


def fb_responder(fb: FakeBridge) -> Callable[[Peer, Request], Any]:
    async def respond(peer: Peer, req: Request) -> Any:
        if req.method == "bridge.ping":
            return {}
        spec = METHODS[req.method]
        return dump(await fb.request(req.method, spec.params.model_validate(req.params)))

    return respond


async def proxy(r: Run) -> Client:
    return await connect(r.tp.socket, "mcp-proxy", minor=1)


async def call(c: Client, tool: str, args: dict[str, Any], **kw: Any) -> ToolCallResult:
    raw = await c.peer.request(
        "tools.call", {"tool": tool, "arguments": args, "client_name": CLIENT, **kw}, timeout=10
    )
    return ToolCallResult.model_validate(raw)


# ---- caller ids -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_proxies_with_the_same_client_name_get_different_callers(run: Run) -> None:
    a, b = await proxy(run), await proxy(run)
    try:
        ra = await call(a, "index_status", {})
        rb = await call(b, "index_status", {})
        assert ra.ok and rb.ok and not ra.untrusted_output
        rows = [r for r in records(run.tp) if r.tool == "index_status"]
        assert [r.caller_label for r in rows] == [CLIENT, CLIENT]
        callers = [r.caller for r in rows]
        assert callers[0] != callers[1]
        for caller in callers:
            assert caller.startswith("mcp:") and HEX32.fullmatch(caller.removeprefix("mcp:"))
        assert [r.call_id for r in rows] == [ra.call_id, rb.call_id]
        # The same connection keeps its id: a second call is audited under the same caller.
        await call(a, "index_status", {})
        assert records(run.tp)[-1].caller == callers[0]
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_a_reconnected_proxy_gets_a_new_caller(run: Run) -> None:
    a = await proxy(run)
    await call(a, "index_status", {})
    await a.close()
    b = await proxy(run)
    try:
        await call(b, "index_status", {})
    finally:
        await b.close()
    first, second = (r.caller for r in records(run.tp) if r.tool == "index_status")
    assert first != second


@pytest.mark.asyncio
async def test_without_a_client_name_the_label_is_null(run: Run) -> None:
    c = await proxy(run)
    try:
        raw = await c.peer.request(
            "tools.call", {"tool": "index_status", "arguments": {}}, timeout=10
        )
        assert ToolCallResult.model_validate(raw).ok
    finally:
        await c.close()
    [row] = records(run.tp)
    assert row.caller_label is None and row.caller.startswith("mcp:")


# ---- list and call ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_is_the_registry_minus_forbidden_effect_tools(run: Run) -> None:
    c = await proxy(run)
    try:
        raw = await c.peer.request("tools.list", {}, timeout=10)
    finally:
        await c.close()
    listed = [ToolMeta.model_validate(doc) for doc in raw["tools"]]
    expected = [m for m in run.daemon.registry.describe() if m.name != "filter_dummy"]
    assert listed == expected
    assert "filter_dummy" in run.daemon.registry.names()
    assert "slow_read" in {m.name for m in listed}
    assert records(run.tp) == []  # listing is not a tool call


@pytest.mark.asyncio
async def test_search_messages_runs_as_the_proxy_caller_with_untrusted_output(run: Run) -> None:
    fb = FakeBridge(FIXTURE)
    ext = await connect(
        run.tp.socket,
        "extension",
        serve=fb_responder(fb),
        hello=hello_params("extension", profile_id=str(uuid.uuid4())),
    )
    fb.on_event = ext.peer.notify
    c = await proxy(run)
    try:
        conn = run.daemon.engine.index.connection
        copies = tfm.load_manifest(FIXTURE)["message_count"]
        await until(
            lambda: conn.execute("SELECT count(*) FROM locations").fetchone()[0] == copies,
            "the whole backfill",
        )
        res = await call(c, "search_messages", {"query": "invoice"})
        assert res.ok and res.untrusted_output and res.error is None
        assert res.output is not None and res.output["mode"] == "fts" and res.output["hits"]
        assert res.call_id is not None and "undo_token" not in ToolCallResult.model_fields
        [row] = [r for r in records(run.tp) if r.tool == "search_messages"]
        assert (row.caller_label, row.decision, row.call_id) == (CLIENT, "auto", res.call_id)
        assert row.caller.startswith("mcp:")
    finally:
        await c.close()
        await ext.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args", "code"),
    [
        ("no_such_tool", {}, "unknown_tool"),
        ("search_messages", {"query": 7}, "invalid_params"),
        ("filter_dummy", {"mids": ["a" * 64]}, "denied"),
        ("Bad-Name", {}, "unknown_tool"),
    ],
)
async def test_a_refusal_is_result_data_with_its_audit_row(
    run: Run, tool: str, args: dict[str, Any], code: str
) -> None:
    c = await proxy(run)
    try:
        res = await call(c, tool, args)
    finally:
        await c.close()
    assert not res.ok and res.output is None and res.error is not None
    assert res.error.code == code and res.error.message
    [row] = records(run.tp)
    # A malformed name reaches the registry, which audits it as <invalid> (#10).
    audited = "<invalid>" if tool == "Bad-Name" else tool
    assert (row.tool, row.caller_label) == (audited, CLIENT) and row.caller.startswith("mcp:")


@pytest.mark.asyncio
async def test_a_malformed_call_is_a_bridge_invalid_params_without_a_row(run: Run) -> None:
    c = await proxy(run)
    try:
        with pytest.raises(BridgeError) as exc:
            await c.peer.request("tools.call", {"tool": "index_status"}, timeout=10)
        assert exc.value.name == "invalid_params"
        with pytest.raises(BridgeError) as exc:
            await call(c, "index_status", {}, client_name="bad\x1bname")
        assert exc.value.name == "invalid_params"
    finally:
        await c.close()
    assert records(run.tp) == []


# ---- roles ------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["tools.list", "tools.call"])
async def test_cli_and_extension_cannot_list_or_call_tools(run: Run, method: str) -> None:
    params: dict[str, Any] = (
        {} if method == "tools.list" else {"tool": "index_status", "arguments": {}}
    )
    fb = FakeBridge(FIXTURE)
    cli = await connect(run.tp.socket, "cli", minor=1)
    ext = await connect(
        run.tp.socket,
        "extension",
        minor=1,
        serve=fb_responder(fb),
        hello=hello_params("extension", minor=1, profile_id=str(uuid.uuid4())),
    )
    try:
        for c in (cli, ext):
            with pytest.raises(BridgeError) as exc:
                await c.peer.request(method, params, timeout=10)
            assert exc.value.name == "role_rejected"
    finally:
        await cli.close()
        await ext.close()
    assert [r for r in records(run.tp) if r.tool == "index_status"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("approvals.decide", {"approval_id": "a", "nonce": "n", "decision": "approve"}),
        ("audit.undo", {"undo_token": "t"}),
        ("ui.open_mid", {"mid": "a" * 64}),
        ("daemon.info", {}),
        # 1.0 methods without table roles: refused by ternd's mcp-proxy allowlist (step 8 review)
        ("approvals.pending", {}),
        ("audit.list", {}),
        ("search.query", {"query": "invoice"}),
        ("chat.send", {"session_id": "s", "text": "hi"}),
        ("chat.cancel", {"turn_id": "t"}),
        ("index.status", {}),
    ],
)
async def test_the_proxy_cannot_reach_approvals_undo_or_the_ui_methods(
    run: Run, method: str, params: dict[str, Any]
) -> None:
    c = await proxy(run)
    try:
        with pytest.raises(BridgeError) as exc:
            await c.peer.request(method, params, timeout=10)
        assert exc.value.name == "role_rejected"
    finally:
        await c.close()
    assert records(run.tp) == []


@pytest.mark.asyncio
async def test_a_1_0_proxy_cannot_call_tools(run: Run) -> None:
    c = await connect(run.tp.socket, "mcp-proxy", minor=0)
    try:
        with pytest.raises(BridgeError) as exc:
            await c.peer.request("tools.list", {}, timeout=10)
        assert exc.value.name == "method_not_available"
    finally:
        await c.close()


# ---- cancel on close --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closing_the_proxy_mid_call_cancels_the_call(run: Run) -> None:
    c = await proxy(run)
    pending = asyncio.create_task(call(c, "slow_read", {}))
    await until(run.started_flag.exists, "the slow tool to start")
    await c.close()
    with pytest.raises(BridgeError):
        await pending  # the connection is gone: the reply never arrives

    def cancelled() -> bool:
        rows = [r for r in records(run.tp) if r.tool == "slow_read"]
        if not rows:
            return False
        with AuditLog.open(audit_path(run.tp)) as reader:
            events = reader.events(rows[0].call_id)
        return any(e.kind == "error" and e.detail.get("reason") == "cancelled" for e in events)

    await until(cancelled, "the row's cancelled event")
    [row] = [r for r in records(run.tp) if r.tool == "slow_read"]
    assert row.caller.startswith("mcp:") and row.caller_label == CLIENT


# ---- audit.status and daemon.info -------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_status_counts_stale_writes_and_open_approvals(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = run.daemon
    seen: list[tuple[object, float]] = []

    def fake_stale(audit: AuditLog, now: float) -> int:
        seen.append((audit, now))
        return 3

    c = await connect(run.tp.socket, "cli", minor=1)
    try:
        assert await c.peer.request("audit.status", {}, timeout=10) == {
            "stale_writes": 0,
            "pending_approvals": 0,
        }
        monkeypatch.setattr(handlers, "stale_writes", fake_stale)
        monkeypatch.setattr(d.engine.approvals, "pending", lambda caller=None: [object()] * 2)
        before = time.time()
        assert await c.peer.request("audit.status", {}, timeout=10) == {
            "stale_writes": 3,
            "pending_approvals": 2,
        }
        [(audit, now)] = seen
        assert audit is d.audit and now >= before
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_daemon_info_reports_the_config(tmp_path: Path) -> None:
    url = "https://llm.example.test/v1"
    async with running(
        tmp_path,
        chat_model="model-x",
        chat_base_url=url,
        allow_remote_llm=True,
        remote_llm_urls=[url],
    ) as r:
        c = await connect(r.tp.socket, "cli", minor=1)
        try:
            info = await c.peer.request("daemon.info", {}, timeout=10)
        finally:
            await c.close()
    assert info == {"remote_llm": True, "chat_model": "model-x"}


@pytest.mark.asyncio
async def test_daemon_info_without_a_remote_llm(run: Run) -> None:
    c = await connect(run.tp.socket, "cli", minor=1)
    try:
        info = await c.peer.request("daemon.info", {}, timeout=10)
    finally:
        await c.close()
    assert info == {"remote_llm": False, "chat_model": TernConfig().chat_model}


# ---- serializer failure and shutdown (step 8 review) -------------------------------------------


@pytest.mark.asyncio
async def test_an_unserializable_output_is_an_internal_serialize_failure(
    run: Run, caplog: pytest.LogCaptureFixture
) -> None:
    c = await proxy(run)
    try:
        res = await call(c, "bad_output", {})
    finally:
        await c.close()
    assert not res.ok and res.output is None and res.error is not None
    assert (res.error.code, res.error.reason) == ("internal", "serialize")
    [row] = records(run.tp)
    assert res.call_id == row.call_id and row.tool == "bad_output"
    assert "SENTINEL-serialize" not in caplog.text and "SENTINEL-serialize" not in res.error.message


@pytest.mark.asyncio
async def test_shutdown_waits_for_a_running_call_before_the_audit_log_closes(
    tmp_path: Path,
) -> None:
    async with running(tmp_path) as r:
        c = await proxy(r)
        pending = asyncio.create_task(call(c, "slow_read", {}))
        await until(r.started_flag.exists, "the slow tool to start")
        assert r.daemon.tool_calls.running == 1
        r.stop.set()
        await asyncio.wait_for(r.task, DEADLINE_S)  # serve() returned: the audit log is closed
        assert r.daemon.tool_calls.running == 0
        with pytest.raises(BridgeError):
            await pending
        await c.close()
    [row] = [x for x in records(r.tp) if x.tool == "slow_read"]
    with AuditLog.open(audit_path(r.tp)) as reader:
        events = reader.events(row.call_id)
    # The slow cleanup (0.3 s) finished and its cancel was recorded before the log closed.
    assert any(e.kind == "error" and e.detail.get("reason") == "cancelled" for e in events)
