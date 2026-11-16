"""tern-nmhost relay (plan 02 step 4): daemon start, loss and return, gating, chunking."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
import pytest_asyncio

from tern import paths
from tern.bridge import framing, tbprofile
from tern.bridge.client import ExtensionLink, Peer
from tern.bridge.nmhost import Host, HostExit
from tern.bridge.peercred import installed_nmhost_path
from tern.bridge.protocol import ApprovalRequested, Chunk, Empty
from tern.bridge.server import BridgeServer
from tern.daemon import uds
from tern.index.store import Store, index_path
from tern.testing.bridge import NMHOST, TB_BIN, FakeProcessInfo, hello_params
from tern.testing.procs import env_with_home


class FakeExt:
    """The extension side of the host's stdio: send frames in, read (reassembled) frames out."""

    def __init__(self) -> None:
        self.to_host = asyncio.StreamReader()
        self.out: asyncio.Queue[bytes] = asyncio.Queue()
        self.raw_frames: list[bytes] = []
        self._ids = 0
        self._re = framing.Reassembler()

    async def ext_out(self, payload: bytes) -> None:
        self.raw_frames.append(payload)
        await self.out.put(payload)

    def send(self, obj: dict[str, Any]) -> None:
        self.to_host.feed_data(framing.frame(framing.encode(obj)))

    def request(self, method: str, params: dict[str, Any]) -> int:
        self._ids += 1
        self.send({"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params})
        return self._ids

    async def recv(self, timeout: float = 3.0) -> dict[str, Any]:
        while True:
            msg = json.loads(await asyncio.wait_for(self.out.get(), timeout))
            if msg.get("method") != "bridge.chunk":
                return msg
            whole = self._re.feed(Chunk.model_validate(msg["params"]))
            if whole is not None:
                return json.loads(whole)

    async def recv_reply(self, rid: int) -> dict[str, Any]:
        while True:
            msg = await self.recv()
            if msg.get("id") == rid and "method" not in msg:
                return msg

    async def hello(self) -> dict[str, Any]:
        return await self.recv_reply(self.request("hello", hello_params("extension")))


@dataclass
class Rig:
    ext: FakeExt
    server: BridgeServer
    sock: Path
    host_task: asyncio.Task[None]
    spawns: list[int] = field(default_factory=list)
    srv: asyncio.Server | None = None

    async def start_daemon(self) -> None:
        if self.srv is None:
            self.srv = await uds.start(self.server, self.sock)

    async def drop_connections(self) -> None:
        await self.server.close_all()


@pytest_asyncio.fixture
async def rig() -> AsyncIterator[Rig]:
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    server = BridgeServer(
        process_info=FakeProcessInfo(),
        thunderbird_bin=TB_BIN,
        nmhost_path=NMHOST,
        link=ExtensionLink(wait_s=2.0),
    )
    ext = FakeExt()
    rig_ref: list[Rig] = []

    def spawn() -> None:  # what tern-nmhost does when the socket is absent
        rig_ref[0].spawns.append(1)
        asyncio.get_running_loop().create_task(rig_ref[0].start_daemon())

    host = Host(ext.to_host, ext.ext_out, tp.socket, spawn, backoff_start_s=0.02, backoff_max_s=0.1)
    r = Rig(ext, server, tp.socket, asyncio.create_task(host.run()))
    rig_ref.append(r)
    yield r
    ext.to_host.feed_eof()
    await asyncio.wait_for(r.host_task, 3)
    if r.srv is not None:
        r.srv.close()
        await server.close_all()
        await r.srv.wait_closed()


@pytest.mark.asyncio
async def test_absent_socket_starts_the_daemon_then_hello_works(rig: Rig) -> None:
    await _connected(rig)  # an early hello is refused, then bridge.reconnected, then it works
    assert rig.spawns


@pytest.mark.asyncio
async def test_daemon_to_extension_request_round_trips(rig: Rig) -> None:
    await _connected(rig)
    pending = asyncio.create_task(rig.server.link.request("bridge.ping", Empty()))
    req = await rig.ext.recv()
    assert req["method"] == "bridge.ping"
    rig.ext.send({"jsonrpc": "2.0", "id": req["id"], "result": {}})
    assert await asyncio.wait_for(pending, 2) == Empty()


@pytest.mark.asyncio
async def test_daemon_loss_keeps_the_port_and_recovers(rig: Rig) -> None:
    await _connected(rig)
    await rig.drop_connections()
    # Disconnected or reconnected-but-gated: a non-hello request is refused at once.
    rid = rig.ext.request("index.status", {})
    msg = await rig.ext.recv()
    while msg.get("id") != rid:  # bridge.reconnected may come first
        assert msg.get("method") == "bridge.reconnected"
        msg = await rig.ext.recv()
    assert msg["error"]["data"]["name"] == "unavailable"
    assert not rig.host_task.done()  # the TB port stays open
    # The extension is told to re-hello (if it was not told before the refusal above).
    await _drain_until_reconnected(rig)
    assert (await rig.ext.hello())["result"]["role"] == "daemon"
    rid = rig.ext.request("bridge.ping", {})
    assert (await rig.ext.recv_reply(rid))["result"] == {}


@pytest.mark.asyncio
async def test_in_flight_extension_request_fails_when_the_daemon_drops(rig: Rig) -> None:
    started = asyncio.Event()

    async def hang(peer: Peer, params: Any) -> Any:
        started.set()
        await asyncio.sleep(3600)

    rig.server.handlers["index.status"] = hang
    await _connected(rig)
    rid = rig.ext.request("index.status", {})
    await asyncio.wait_for(started.wait(), 2)
    await rig.drop_connections()
    reply = await rig.ext.recv_reply(rid)
    assert reply["error"]["data"]["name"] == "unavailable"


@pytest.mark.asyncio
async def test_request_before_any_daemon_is_unavailable(tern_home: Path) -> None:
    ext = FakeExt()
    host = Host(
        ext.to_host, ext.ext_out, paths.resolve().socket, lambda: None, backoff_start_s=0.02
    )
    task = asyncio.create_task(host.run())
    reply = await ext.recv_reply(ext.request("hello", hello_params("extension")))
    assert reply["error"]["data"]["name"] == "unavailable"
    ext.send({"jsonrpc": "2.0", "method": "event.folder.changed", "params": {"folder_id": "f"}})
    ext.to_host.feed_eof()
    await asyncio.wait_for(task, 2)
    assert host.dropped == 1


@pytest.mark.asyncio
async def test_failing_daemon_start_ends_the_host_loudly(
    tern_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def spawn() -> None:
        raise OSError("ternd cannot be started")

    ext = FakeExt()
    host = Host(ext.to_host, ext.ext_out, paths.resolve().socket, spawn, backoff_start_s=0.02)
    with pytest.raises(HostExit, match="cannot reach ternd") as exc_info:
        await asyncio.wait_for(host.run(), 2)  # stdin stays open: the error alone ends it
    assert isinstance(exc_info.value.__cause__, OSError)
    assert any(
        r.levelname == "ERROR" and getattr(r, "error", None) == "OSError" for r in caplog.records
    )


@pytest.mark.asyncio
async def test_unreachable_socket_ends_the_host_instead_of_answering_unavailable(
    tern_home: Path,
) -> None:
    # A socket path under a regular file: NotADirectoryError for any user (root included), an
    # OSError the connector must not retry silently, like a permission error.
    not_a_dir = tern_home / "file"
    not_a_dir.write_bytes(b"")
    ext = FakeExt()
    host = Host(ext.to_host, ext.ext_out, not_a_dir / "bridge.sock", lambda: None)
    with pytest.raises(HostExit, match="cannot reach ternd") as exc_info:
        await asyncio.wait_for(host.run(), 2)
    assert isinstance(exc_info.value.__cause__, NotADirectoryError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        framing._LEN.pack(framing.INBOUND_MAX + 1),  # oversize: header only, never read on
        framing._LEN.pack(100) + b'{"jsonrpc"',  # truncated: EOF inside the payload
    ],
    ids=["oversize", "truncated"],
)
async def test_unreadable_extension_frame_ends_the_host_with_one_error(
    tern_home: Path, caplog: pytest.LogCaptureFixture, data: bytes
) -> None:
    # Plan 02 review (deferred to plan 03): closed with a logged error, not an escaping traceback.
    ext = FakeExt()
    host = Host(ext.to_host, ext.ext_out, paths.resolve().socket, lambda: None)
    ext.to_host.feed_data(data)
    ext.to_host.feed_eof()
    with pytest.raises(HostExit, match="unreadable extension frame"):
        await asyncio.wait_for(host.run(), 2)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert [r.getMessage() for r in errors] == ["unreadable extension frame; exiting"]
    assert errors[0].exc_info is None


@pytest.mark.asyncio
async def test_large_daemon_message_arrives_in_chunks_below_512_kib(rig: Rig) -> None:
    await _connected(rig)
    big = ApprovalRequested(
        approval_id="a1",
        nonce="n",
        tool="move_messages",
        tier="W2",
        targets=[],
        args_view={"blob": "x" * (2_500_000)},
        provenance=[],
        expires_at="2026-11-13T12:00:00Z",  # pyright: ignore[reportArgumentType]  # pydantic parses ISO strings
    )
    rig.ext.raw_frames.clear()
    await rig.server.link.notify("approval.requested", big)
    msg = await rig.ext.recv(timeout=5)
    assert msg["method"] == "approval.requested"
    assert msg["params"]["args_view"]["blob"] == "x" * 2_500_000
    assert len(rig.ext.raw_frames) > 1
    assert all(len(fr) < framing.HOST_TO_EXT_MAX for fr in rig.ext.raw_frames)


async def _connected(rig: Rig) -> None:
    reply = await rig.ext.hello()
    while "error" in reply:  # the daemon was still starting
        await _drain_until_reconnected(rig)
        reply = await rig.ext.hello()
    assert rig.server.link.active is not None


async def _drain_until_reconnected(rig: Rig) -> None:
    while (await rig.ext.recv()).get("method") != "bridge.reconnected":
        pass


# -- the real script, a real ternd ----------------------------------------------------------


def _children(pid: int) -> list[int]:
    kids: list[int] = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        kids += [int(k) for k in (task / "children").read_text().split()]
    return kids


@pytest.mark.asyncio
async def test_real_nmhost_starts_ternd_survives_its_death_and_reconnects(tern_home: Path) -> None:
    script = installed_nmhost_path()
    assert script.exists(), "tern-nmhost is not installed in this environment (make setup)"
    (tern_home / "config").mkdir()
    # The peer check wants TB as the host's parent; here the parent is this Python.
    (tern_home / "config" / "config.toml").write_text(
        f'thunderbird_bin = "{os.path.realpath(sys.executable)}"\n'
    )
    proc = await asyncio.create_subprocess_exec(
        str(script),
        "/x/tern.json",
        "tern@tern.invalid",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        env=env_with_home(tern_home),  # embedding off: no LM Studio traffic
    )
    assert proc.stdin is not None and proc.stdout is not None
    stdin, stdout = proc.stdin, proc.stdout
    ids = iter(range(1, 1000))

    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        rid = next(ids)
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        stdin.write(framing.frame(framing.encode(msg)))
        await stdin.drain()
        while True:
            reply = framing.decode(await asyncio.wait_for(framing.read_frame(stdout), 10) or b"")
            if reply.get("id") == rid and "method" not in reply:
                return reply

    async def hello_until_ok() -> dict[str, Any]:
        reply: dict[str, Any] = {}
        for _ in range(100):
            reply = await call("hello", hello_params("extension"))
            if "result" in reply:
                return reply
            await asyncio.sleep(0.1)
        raise AssertionError(f"hello never succeeded: {reply}")

    ternd_pid = 0
    try:
        assert (await hello_until_ok())["result"]["role"] == "daemon"
        kids = _children(proc.pid)
        assert len(kids) == 1, kids
        ternd_pid = kids[0]
        os.kill(ternd_pid, signal.SIGTERM)
        await _exited(ternd_pid)
        reply = await call("bridge.ping", {})
        assert reply["error"]["data"]["name"] == "unavailable"
        assert proc.returncode is None  # the host kept the port
        assert (await hello_until_ok())["result"]["role"] == "daemon"  # a new ternd
        assert (await call("bridge.ping", {}))["result"] == {}
    finally:
        stdin.close()
        await asyncio.wait_for(proc.wait(), 10)  # EOF on stdin ends the host
        for pid in _children_of_session(tern_home):
            os.kill(pid, signal.SIGTERM)
    assert proc.returncode == 0


async def _exited(pid: int) -> None:
    """Wait until ``pid`` is gone or a zombie (it is the host's child, reaped later)."""
    for _ in range(200):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return
        if stat[stat.rindex(")") + 2] == "Z":
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"process {pid} did not exit")


def _children_of_session(tern_home: Path) -> list[int]:
    """ternd processes serving this TERN_HOME (they outlive the host by design)."""
    pids: list[int] = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            env = (d / "environ").read_bytes()
            cmd = (d / "cmdline").read_bytes()
        except OSError:
            continue
        if b"tern.daemon.main" in cmd and f"TERN_HOME={tern_home}".encode() in env.split(b"\0"):
            pids.append(int(d.name))
    return pids


@pytest.mark.asyncio
async def test_real_nmhost_exits_1_without_traceback_on_an_oversize_frame(tern_home: Path) -> None:
    script = installed_nmhost_path()
    assert script.exists(), "tern-nmhost is not installed in this environment (make setup)"
    proc = await asyncio.create_subprocess_exec(
        str(script),
        "/x/tern.json",
        "tern@tern.invalid",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env_with_home(tern_home),  # embedding off: no LM Studio traffic
    )
    try:
        _, err = await asyncio.wait_for(
            proc.communicate(framing._LEN.pack(framing.INBOUND_MAX + 1)), 10
        )
    finally:
        for pid in _children_of_session(tern_home):  # the ternd it may have started
            os.kill(pid, signal.SIGTERM)
    assert proc.returncode == 1
    lines = [json.loads(line) for line in err.decode().splitlines()]
    assert [r["msg"] for r in lines if r["level"] == "ERROR"] == [
        "unreadable extension frame; exiting"
    ]
    assert "Traceback" not in err.decode()


# -- the profile id ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hello_goes_out_with_the_hosts_profile_id(tern_home: Path) -> None:
    """The host's path-derived id replaces the extension's stored one, which a reinstall loses."""
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    bound: list[UUID] = []
    server = BridgeServer(
        process_info=FakeProcessInfo(),
        thunderbird_bin=TB_BIN,
        nmhost_path=NMHOST,
        link=ExtensionLink(wait_s=2.0),
        bind_profile=bound.append,
    )
    srv = await uds.start(server, tp.socket)
    ext = FakeExt()
    stable = uuid5(NAMESPACE_URL, "profile")
    host = Host(
        ext.to_host,
        ext.ext_out,
        tp.socket,
        lambda: None,
        backoff_start_s=0.02,
        profile_id=str(stable),
    )
    task = asyncio.create_task(host.run())
    try:
        reply = await ext.hello()
        while "error" in reply:
            while (await ext.recv()).get("method") != "bridge.reconnected":
                pass
            reply = await ext.hello()
        assert hello_params("extension")["profile_id"] != str(stable)
        assert bound == [stable]
    finally:
        ext.to_host.feed_eof()
        await asyncio.wait_for(task, 3)
        srv.close()
        await server.close_all()
        await srv.wait_closed()


@pytest.mark.asyncio
async def test_real_nmhost_keeps_the_index_bound_across_add_on_reinstalls(
    tern_home: Path, tmp_path: Path
) -> None:
    """A real host under a stand-in TB started with ``--profile``, a real ternd and an index bound
    to an old random extension id: two "installs" with different stored ids both connect."""
    script = installed_nmhost_path()
    assert script.exists(), "tern-nmhost is not installed in this environment (make setup)"
    (tern_home / "config").mkdir()
    (tern_home / "config" / "config.toml").write_text(
        f'thunderbird_bin = "{os.path.realpath(sys.executable)}"\n'
    )
    profile = tmp_path / "tb" / "abcd.default-release"
    profile.mkdir(parents=True)
    (profile / "prefs.js").write_text("// synthetic\n")
    with Store.open(index_path(paths.resolve())) as s:
        s.bind_profile(uuid4())  # what the earlier, now uninstalled, add-on had bound
    # The stand-in TB: the host's parent, with `--profile <dir>` on its command line.
    parent = (
        "import subprocess, sys; "
        "sys.exit(subprocess.call([sys.argv[1], '/x/tern.json', 'tern@tern.invalid']))"
    )

    async def install(stored_id: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            parent,
            str(script),
            "--profile",
            str(profile),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=env_with_home(tern_home),
        )
        assert proc.stdin is not None and proc.stdout is not None
        stdin, stdout = proc.stdin, proc.stdout
        reply: dict[str, Any] = {}
        try:
            for rid in range(1, 100):
                msg = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "hello",
                    "params": hello_params("extension", profile_id=stored_id),
                }
                stdin.write(framing.frame(framing.encode(msg)))
                await stdin.drain()
                while True:
                    raw = await asyncio.wait_for(framing.read_frame(stdout), 10)
                    reply = framing.decode(raw or b"")
                    if reply.get("id") == rid and "method" not in reply:
                        break
                if "result" in reply:
                    return
                await asyncio.sleep(0.1)
            raise AssertionError(f"hello never succeeded: {reply}")
        finally:
            stdin.close()
            await asyncio.wait_for(proc.wait(), 10)

    try:
        await install(str(uuid4()))
        await install(str(uuid4()))  # removed and reinstalled: a new stored id
    finally:
        for pid in _children_of_session(tern_home):
            os.kill(pid, signal.SIGTERM)
    with Store.open(index_path(paths.resolve()), readonly=True) as ro:
        assert ro.profile_id() == tbprofile.profile_uuid(profile)
