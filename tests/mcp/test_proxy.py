"""Plan 07 part B step 9: ``tern mcp`` (``tern.mcp.proxy``) serving a real MCP stdio client over
ternd's bridge 1.1 ``tools.list``/``tools.call``.

``tern mcp`` runs as a subprocess (``python -m tern.cli mcp``) spawned by the SDK's stdio client;
ternd runs in this test's event loop (``serve()``), with the extension a scripted client answered
by FakeBridge over the synthetic mailbox, so the index holds mail. (A subprocess ternd would run
the real peer check, which refuses a non-Thunderbird extension, so it could not be backfilled.)

Checked: the listing is ternd's registry minus forbidden-effect tools; a ``search_messages`` call
returns wrapped untrusted text and is audited as ``mcp:<ternd's connection id>`` with the client's
name as label; a malformed tool name reaches the registry (``<invalid>`` row); the process holds
no inet socket and writes only JSON-RPC to stdout; no ternd → exit 2 with ``unavailable``; ternd
going away ends the process non-zero; ``tern mcp --help`` offers no option.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import test_fixture_manifest as tfm
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import Implementation
from pydantic import ValidationError

import tern.mcp.proxy as proxy_mod
from tern import paths
from tern.bridge.client import Peer
from tern.bridge.protocol import METHODS, BridgeError, Request, dump
from tern.bridge.server import BridgeServer
from tern.config import TernConfig
from tern.daemon import uds
from tern.daemon.main import Daemon, serve
from tern.mcp.server import UNTRUSTED_META_KEY
from tern.perms.audit import INVALID_TOOL, AuditLog, AuditRecord, audit_path
from tern.testing.bridge import NMHOST, TB_BIN, FakeProcessInfo, connect, hello_params
from tern.testing.bridge import Client as BridgeClient
from tern.testing.fake_bridge import FakeBridge
from tern.testing.procs import inet_inodes, socket_inodes, wait_for_socket

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
COPIES = MANIFEST["message_count"]
CLIENT = "proxy-test-client"
DEADLINE_S = 15.0
MCP_CMD = [sys.executable, "-m", "tern.cli", "mcp"]
HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")


@dataclass
class Run:
    tp: paths.TernPaths
    daemon: Daemon
    stop: asyncio.Event
    task: asyncio.Task[None]


async def until(cond: Callable[[], bool], what: str) -> None:
    deadline = time.perf_counter() + DEADLINE_S
    while not cond():
        assert time.perf_counter() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.02)


def fb_responder(fb: FakeBridge) -> Callable[[Peer, Request], Any]:
    async def respond(peer: Peer, req: Request) -> Any:
        if req.method == "bridge.ping":
            return {}
        spec = METHODS[req.method]
        return dump(await fb.request(req.method, spec.params.model_validate(req.params)))

    return respond


@contextlib.asynccontextmanager
async def ternd() -> AsyncIterator[Run]:
    tp = paths.resolve()
    tp.run.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = asyncio.Event()
    started: list[Daemon] = []
    task = asyncio.create_task(
        serve(
            tp,
            TernConfig(embed_model=""),
            process_info=FakeProcessInfo(),
            nmhost_path=NMHOST,
            stop=stop,
            on_started=started.append,
        )
    )
    ext: BridgeClient | None = None
    try:
        await until(lambda: bool(started) or task.done(), "on_started")
        if task.done():
            task.result()
        await asyncio.to_thread(wait_for_socket, tp.socket)
        fb = FakeBridge(FIXTURE)
        ext = await connect(
            tp.socket,
            "extension",
            serve=fb_responder(fb),
            hello=hello_params("extension", profile_id=str(uuid.uuid4())),
        )
        fb.on_event = ext.peer.notify
        conn = started[0].engine.index.connection
        await until(
            lambda: conn.execute("SELECT count(*) FROM locations").fetchone()[0] == COPIES,
            "the backfill",
        )
        yield Run(tp, started[0], stop, task)
    finally:
        if ext is not None:
            await ext.close()
        stop.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE_S)


def params() -> StdioServerParameters:
    return StdioServerParameters(command=MCP_CMD[0], args=MCP_CMD[1:], env=dict(os.environ))


def records(tp: paths.TernPaths) -> list[AuditRecord]:
    with AuditLog.open(audit_path(tp)) as reader:
        return list(reversed(reader.list(limit=1000)))


def mcp_children() -> list[int]:
    """Pids of this process's children running ``tern.cli mcp``."""
    me = os.getpid()
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        if ppid == me and b"tern.cli" in cmdline and b"mcp" in cmdline:
            found.append(int(entry.name))
    return found


def unwrap(text: str) -> Any:
    assert text.startswith("<untrusted_email>\n") and text.endswith("\n</untrusted_email>")
    return json.loads(text.removeprefix("<untrusted_email>\n").removesuffix("\n</untrusted_email>"))


# ---- a session through ternd ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_and_search_through_ternd_are_audited_as_the_proxy_connection() -> None:
    async with ternd() as run:
        async with Client(params(), client_info=Implementation(name=CLIENT, version="0")) as c:
            tools = (await c.list_tools()).tools
            result = await c.call_tool("search_messages", {"query": "invoice"})
            [pid] = mcp_children()
            held = socket_inodes(pid) & inet_inodes(pid)
        listed = {t.name for t in tools}
        expected = {m.name for m in run.daemon.registry.describe()}
        assert listed == expected  # no forbidden-effect built-in exists
        search = next(t for t in tools if t.name == "search_messages")
        assert search.meta is not None and search.meta[UNTRUSTED_META_KEY] is True
        assert result.is_error is False
        assert result.meta is not None and result.meta[UNTRUSTED_META_KEY] is True
        [item] = result.content
        assert item.type == "text"
        out = unwrap(item.text)
        assert out["mode"] == "fts" and out["hits"]
        [row] = [r for r in records(run.tp) if r.tool == "search_messages"]
        assert row.caller_label == CLIENT and row.decision == "auto"
        assert HEX32.fullmatch(row.caller.removeprefix("mcp:")) and row.caller.startswith("mcp:")
        assert held == set()


@pytest.mark.asyncio
async def test_two_sessions_are_two_callers_and_a_malformed_name_is_audited() -> None:
    async with ternd() as run:
        for _ in range(2):
            async with Client(params(), client_info=Implementation(name=CLIENT, version="0")) as c:
                result = await c.call_tool("Bad-Name", {})
                assert result.is_error is True
                [item] = result.content
                assert item.type == "text"
                assert json.loads(item.text)["error"]["code"] == "unknown_tool"
        rows = [r for r in records(run.tp) if r.tool == INVALID_TOOL]
        assert len(rows) == 2 and rows[0].caller != rows[1].caller
        assert {r.caller_label for r in rows} == {CLIENT}


@pytest.mark.asyncio
async def test_stdout_carries_only_json_rpc_and_closing_stdin_exits_0() -> None:
    async with ternd():
        proc = await asyncio.create_subprocess_exec(
            *MCP_CMD,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((json.dumps(INIT) + "\n").encode())
        await proc.stdin.drain()
        first = await asyncio.wait_for(proc.stdout.readline(), DEADLINE_S)
        proc.stdin.close()
        rest, err = await asyncio.wait_for(proc.communicate(), DEADLINE_S)
        assert proc.returncode == 0, err.decode(errors="replace")
        lines = [first, *rest.splitlines()]
        for line in filter(None, (ln.strip() for ln in lines)):
            msg = json.loads(line)
            assert msg["jsonrpc"] == "2.0"
        reply = json.loads(first)
        assert reply["id"] == 1 and reply["result"]["serverInfo"]["name"] == "tern"


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": CLIENT, "version": "0"},
    },
}


@pytest.mark.asyncio
async def test_ternd_going_away_ends_the_proxy_at_once_while_stdin_stays_open() -> None:
    """Step 9 review: the SDK reads stdin in a thread a cancel cannot interrupt, so the proxy
    must exit by itself, without waiting for the client's next line or EOF."""
    async with ternd() as run:
        proc = await asyncio.create_subprocess_exec(
            *MCP_CMD,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        try:
            # Serving: initialize answered (the tool list is fetched before serving starts).
            proc.stdin.write((json.dumps(INIT) + "\n").encode())
            await proc.stdin.drain()
            reply = json.loads(await asyncio.wait_for(proc.stdout.readline(), DEADLINE_S))
            assert reply["id"] == 1
            server = run.daemon.server
            assert any(p.role == "mcp-proxy" for p in server.peers)
            await server.close_all()
            # stdin stays open: the exit must not depend on it.
            code = await asyncio.wait_for(proc.wait(), DEADLINE_S)
            err = await proc.stderr.read()
            rest = await proc.stdout.read()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            proc.stdin.close()
    assert code == 1
    assert proxy_mod.TERND_GONE.encode() in err
    assert rest == b""  # nothing after the initialize reply reached stdout


# ---- without ternd, help, source ------------------------------------------------------------


def test_without_ternd_tern_mcp_exits_2_with_unavailable() -> None:
    proc = subprocess.run(MCP_CMD, input=b"", capture_output=True, timeout=60, check=False)
    assert proc.returncode == 2
    assert b"unavailable" in proc.stderr and b"no ternd listening" in proc.stderr
    assert proc.stdout == b""
    assert not paths.resolve().socket.exists()  # it never starts a ternd


def test_help_offers_no_option() -> None:
    proc = subprocess.run(
        [*MCP_CMD, "--help"], capture_output=True, timeout=60, check=True, text=True
    )
    options = set(re.findall(r"(?<![\w-])--?[a-z][\w-]*", proc.stdout))
    assert options == {"-h", "--help"}
    for word in ("host", "port", "http", "sse", "listen"):
        assert word not in proc.stdout.lower().replace("help", "")


def test_the_proxy_never_names_approvals_undo_or_the_registry() -> None:
    """The proxy's only bridge methods are hello, tools.list and tools.call."""
    source = Path(proxy_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    strings = {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    methods = {s for s in strings if s in METHODS}
    assert methods == {"hello", "tools.list", "tools.call"}
    imported = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module is not None
    }
    for forbidden in (
        "tern.tools.registry",
        "tern.perms.audit",
        "tern.index.store",
        "tern.mcp.local",
    ):
        assert forbidden not in imported


@pytest.mark.asyncio
@pytest.mark.parametrize("minor", [0, 1])
async def test_a_ternd_without_the_tools_minor_is_refused(minor: int) -> None:
    """A 1.0 daemon (it has no tools.*) is refused at hello; a 1.1 one is accepted."""
    server = BridgeServer(
        process_info=FakeProcessInfo(), thunderbird_bin=TB_BIN, nmhost_path=NMHOST, minor=minor
    )
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    srv = await uds.start(server, tp.socket)
    try:
        if minor == 0:
            with pytest.raises(proxy_mod.ProxyError, match="needs 1.1"):
                await proxy_mod.open_peer(tp.socket)
        else:
            peer, task = await proxy_mod.open_peer(tp.socket)
            assert peer.minor == 1
            await peer.close()
            await task
    finally:
        srv.close()
        await server.close_all()
        await srv.wait_closed()


# ---- BridgeBackend over a scripted peer (step 9 review) ----------------------------------------


class ScriptedPeer:
    """Answers ``request`` from a script: a value, or an exception to raise."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.requests: list[tuple[str, dict[str, Any], float | None]] = []

    async def request(
        self, method: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> Any:
        self.requests.append((method, params, timeout))
        answer = self.answers[method]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def backend_over(
    answers: dict[str, Any], tools: list[Any] | None = None
) -> tuple[proxy_mod.BridgeBackend, ScriptedPeer]:
    peer = ScriptedPeer(answers)
    return proxy_mod.BridgeBackend(cast(Peer, peer), tools or []), peer


@pytest.mark.asyncio
async def test_a_malformed_tools_list_is_a_proxy_error_without_values() -> None:
    peer = ScriptedPeer({"tools.list": {"tools": [{"name": "SENTINEL-tool-77"}]}})
    with pytest.raises(proxy_mod.ProxyError) as exc:
        await proxy_mod.BridgeBackend.connect(cast(Peer, peer))
    assert "malformed tools.list" in str(exc.value)
    assert "SENTINEL" not in str(exc.value)


@pytest.mark.asyncio
async def test_a_bridge_error_on_tools_call_is_an_unavailable_outcome() -> None:
    backend, peer = backend_over({"tools.call": BridgeError("unavailable", "connection dropped")})
    outcome = await backend.call("index_status", {}, client_name=CLIENT)
    assert outcome.error is not None and outcome.output is None
    assert (outcome.error.code, outcome.error.reason) == ("unavailable", "unavailable")
    [(method, params, timeout)] = peer.requests
    assert method == "tools.call"
    assert params == {"tool": "index_status", "arguments": {}, "client_name": CLIENT}
    assert timeout == proxy_mod.CALL_TIMEOUT_S > 600


@pytest.mark.asyncio
async def test_a_malformed_tools_call_reply_raises_never_a_guessed_outcome() -> None:
    backend, _ = backend_over({"tools.call": {"ok": True, "untrusted_output": False}})
    with pytest.raises(ValidationError):
        await backend.call("index_status", {}, client_name=None)


@pytest.mark.asyncio
async def test_without_a_client_name_none_is_sent() -> None:
    reply = {"ok": True, "output": {"n": 1}, "untrusted_output": True, "call_id": "c1"}
    backend, peer = backend_over({"tools.call": reply})
    outcome = await backend.call("search_messages", {"query": "x"}, client_name=None)
    assert outcome.output == {"n": 1} and outcome.untrusted_output and outcome.call_id == "c1"
    assert "client_name" not in peer.requests[0][1]


# ---- "no ternd" means only a failed connect (review item 5) ------------------------------------


@pytest.mark.asyncio
async def test_open_peer_without_a_socket_is_no_ternd() -> None:
    sock = paths.resolve().socket
    assert not sock.exists()
    with pytest.raises(proxy_mod.NoTernd, match="no ternd listening"):
        await proxy_mod.open_peer(sock)


def test_only_no_ternd_is_reported_as_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tern import cli
    from tern import log as tern_log

    # `tern mcp` routes the root logger to stderr; this in-process run must leave logging as it
    # was (tests/test_log.py checks that no test leaks a handler).
    monkeypatch.setattr(tern_log, "setup", lambda *args, **kwargs: None)

    async def no_ternd(sock: Path) -> None:
        raise proxy_mod.NoTernd(f"no ternd listening at {sock}")

    monkeypatch.setattr(proxy_mod, "serve_proxy", no_ternd)
    assert cli.main(["mcp"]) == 2
    assert "mcp: unavailable: no ternd listening at" in capsys.readouterr().err

    async def late_missing_file(sock: Path) -> None:
        raise FileNotFoundError("some file, after the connection was up")

    monkeypatch.setattr(proxy_mod, "serve_proxy", late_missing_file)
    with pytest.raises(FileNotFoundError):  # not misreported as "no ternd listening"
        cli.main(["mcp"])
