"""ternd's socket: 0600, single instance, stale-socket handling, dispatch (plan 02 step 3)."""

from __future__ import annotations

import asyncio
import socket
import stat
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

from tern import paths
from tern.bridge import framing
from tern.bridge.client import ExtensionLink
from tern.bridge.protocol import BridgeError
from tern.bridge.server import BridgeServer
from tern.daemon import uds
from tern.testing.bridge import NMHOST, TB_BIN, FakeProcessInfo, connect, hello_params
from tern.testing.procs import env_with_home, stop, ternd_cmd, wait_for_socket


@pytest.fixture
def ternd(tern_home: Path) -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(ternd_cmd(), env=env_with_home(tern_home))
    wait_for_socket(paths.resolve().socket)
    yield proc
    stop(proc)


@pytest_asyncio.fixture
async def sock() -> AsyncIterator[Path]:
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    server = BridgeServer(
        process_info=FakeProcessInfo(),
        thunderbird_bin=TB_BIN,
        nmhost_path=NMHOST,
        link=ExtensionLink(wait_s=0.2),
    )
    srv = await uds.start(server, tp.socket)
    yield tp.socket
    srv.close()
    await server.close_all()
    await srv.wait_closed()


def test_socket_is_0600_in_a_0700_dir(ternd: subprocess.Popen[bytes]) -> None:
    tp = paths.resolve()
    assert stat.S_IMODE(tp.socket.stat().st_mode) == 0o600
    assert stat.S_ISSOCK(tp.socket.stat().st_mode)
    assert stat.S_IMODE(tp.run.stat().st_mode) == 0o700


def test_second_ternd_exits_0_and_leaves_the_live_socket(
    ternd: subprocess.Popen[bytes], tern_home: Path
) -> None:
    sock = paths.resolve().socket
    inode = sock.stat().st_ino
    second = subprocess.run(ternd_cmd(), env=env_with_home(tern_home), timeout=10)
    assert second.returncode == 0
    assert sock.stat().st_ino == inode
    assert ternd.poll() is None


def test_leftover_socket_file_is_replaced(tern_home: Path) -> None:
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(tp.socket))  # bound, never listening: what a crashed daemon leaves
    dead.close()
    proc = subprocess.Popen(ternd_cmd(), env=env_with_home(tern_home))
    try:
        connected = False
        for _ in range(100):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                try:
                    c.connect(str(tp.socket))
                    connected = True
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    time.sleep(0.05)
        assert connected, "ternd never listened on the replaced socket"
    finally:
        assert stop(proc) == 0
    assert not tp.socket.exists()  # removed on clean shutdown


@pytest.mark.asyncio
async def test_cli_hello_and_ping(sock: Path) -> None:
    c = await connect(sock, "cli")
    assert (
        c.hello_result["role"] == "daemon" and c.hello_result["minor"] == 2
    )  # tern-bridge/1.2 (#39)
    assert await c.peer.request("bridge.ping", {}, timeout=2) == {}
    await c.close()


@pytest.mark.asyncio
async def test_ping_extension_without_extension_is_unavailable(sock: Path) -> None:
    c = await connect(sock, "cli")
    with pytest.raises(BridgeError) as exc:
        await c.peer.request("bridge.ping_extension", {"n": 3}, timeout=2)
    assert exc.value.name == "unavailable"
    await c.close()


@pytest.mark.asyncio
async def test_ping_extension_stats(sock: Path) -> None:
    ext = await connect(sock, "extension")
    c = await connect(sock, "cli")
    stats = await c.peer.request("bridge.ping_extension", {"n": 20}, timeout=5)
    assert stats["n"] == 20
    assert 0 < stats["p50_ms"] <= stats["p90_ms"] <= stats["p99_ms"] <= stats["max_ms"]
    await c.close()
    await ext.close()


@pytest.mark.asyncio
async def test_ping_extension_is_cli_only(sock: Path) -> None:
    c = await connect(sock, "mcp-proxy")
    with pytest.raises(BridgeError) as exc:
        await c.peer.request("bridge.ping_extension", {"n": 1}, timeout=2)
    assert exc.value.name == "role_rejected"
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "error"),
    [
        ("mail.send", {}, "method_not_available"),  # does not exist
        ("mail.accounts.list", {}, "method_not_available"),  # daemon→extension only
        ("index.status", {}, "unavailable"),  # 1.0 method, not wired until a later plan
        ("search.query", {"query": ""}, "invalid_params"),
        ("hello", hello_params("cli"), "invalid_params"),  # second hello
    ],
)
async def test_dispatch_errors(
    sock: Path, method: str, params: dict[str, object], error: str
) -> None:
    c = await connect(sock, "cli")
    with pytest.raises(BridgeError) as exc:
        await c.peer.request(method, params, timeout=2)
    assert exc.value.name == error
    assert await c.peer.request("bridge.ping", {}, timeout=2) == {}  # connection survives
    await c.close()


@pytest.mark.asyncio
async def test_first_message_must_be_hello(sock: Path) -> None:
    reader, writer = await asyncio.open_unix_connection(str(sock))
    msg = {"jsonrpc": "2.0", "id": 1, "method": "bridge.ping", "params": {}}
    writer.write(framing.frame(framing.encode(msg)))
    await writer.drain()
    reply = framing.decode(await framing.read_frame(reader) or b"")
    assert reply["error"]["data"]["name"] == "invalid_params"
    assert await framing.read_frame(reader) is None  # closed
    writer.close()


@pytest.mark.asyncio
async def test_other_major_is_rejected_and_closed(sock: Path) -> None:
    bad = hello_params("cli") | {"protocol": "tern-bridge/2"}
    with pytest.raises(BridgeError) as exc:
        await connect(sock, "cli", hello=bad)
    assert exc.value.name == "invalid_params"


def test_sigterm_stops_ternd_with_a_client_connected(ternd: subprocess.Popen[bytes]) -> None:
    """asyncio's wait_closed waits for open connections; shutdown must close them first."""
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(str(paths.resolve().socket))
    ternd.terminate()
    try:
        assert ternd.wait(5) == 0
    finally:
        c.close()
