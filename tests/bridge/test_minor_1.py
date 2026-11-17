"""Hello and dispatch once the daemon speaks ``tern-bridge/1.1`` (plan 07 part B step 7, #37).

The golden transcripts are frozen 1.0 conversations and replay against ``BridgeServer(minor=0)``
(``test_golden.py``); this file covers what a 1.1 daemon says and does: its hello carries its own
minor (1), each connection uses the lower minor (architecture §3), a 1.1 method on a 1.0
connection is ``method_not_available``, and the 1.1 role rules hold before any handler runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from tern import paths
from tern.bridge.client import ExtensionLink, Peer
from tern.bridge.protocol import MINOR, BridgeError, DaemonInfo, Empty, Role, ToolList
from tern.bridge.server import BridgeServer
from tern.daemon import uds
from tern.testing.bridge import NMHOST, TB_BIN, FakeProcessInfo, connect

# role -> a 1.1 method that role may call (served by a stub that records who called).
ALLOWED: dict[Role, str] = {"mcp-proxy": "tools.list", "cli": "daemon.info"}
REPLY: dict[str, object] = {
    "tools.list": {"tools": []},
    "daemon.info": {"remote_llm": False, "chat_model": "m"},
}


async def _start(server: BridgeServer) -> tuple[Path, asyncio.Server]:
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    srv = await uds.start(server, tp.socket)
    return tp.socket, srv


def _server(minor: int = 1) -> BridgeServer:
    # A 1.1 daemon (what this file covers); this build speaks MINOR (1.2 since #39), and a
    # BridgeServer may still be built at any lower minor.
    return BridgeServer(
        process_info=FakeProcessInfo(),
        thunderbird_bin=TB_BIN,
        nmhost_path=NMHOST,
        link=ExtensionLink(wait_s=1.0),
        minor=minor,
    )


@pytest_asyncio.fixture
async def daemon() -> AsyncIterator[tuple[BridgeServer, Path, list[str]]]:
    server = _server()
    seen: list[str] = []

    async def tools(peer: Peer, params: Empty) -> ToolList:
        seen.append(str(peer.role))
        return ToolList(tools=[])

    async def info(peer: Peer, params: Empty) -> DaemonInfo:
        seen.append(str(peer.role))
        return DaemonInfo(remote_llm=False, chat_model="m")

    # Stand-ins so dispatch, not a step-8 handler, is what these tests exercise.
    server.handlers["tools.list"] = tools
    server.handlers["daemon.info"] = info
    sock, srv = await _start(server)
    yield server, sock, seen
    srv.close()
    await server.close_all()
    await srv.wait_closed()


def test_this_build_speaks_minor_3_and_still_builds_a_1_1_daemon() -> None:
    assert MINOR == 3  # tern-bridge/1.3 (the chat model picker)
    assert _server(MINOR).minor == 3
    assert _server().minor == 1


@pytest.mark.parametrize("minor", [-1, MINOR + 1])
def test_a_server_minor_outside_the_build_is_refused(minor: int) -> None:
    with pytest.raises(ValueError):
        _server(minor)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["cli", "mcp-proxy"])
async def test_a_1_0_client_sees_minor_1_and_uses_1_0(
    daemon: tuple[BridgeServer, Path, list[str]], role: Role
) -> None:
    _, sock, seen = daemon
    client = await connect(sock, role, minor=0)
    try:
        assert client.hello_result["minor"] == 1  # the daemon's own hello (architecture §3)
        with pytest.raises(BridgeError) as exc:
            await client.peer.request(ALLOWED[role], {}, timeout=5)
        assert exc.value.name == "method_not_available"
        assert seen == []
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["cli", "mcp-proxy"])
async def test_a_1_1_client_reaches_its_1_1_methods(
    daemon: tuple[BridgeServer, Path, list[str]], role: Role
) -> None:
    _, sock, seen = daemon
    client = await connect(sock, role, minor=1)
    try:
        assert client.hello_result["minor"] == 1
        method = ALLOWED[role]
        assert await client.peer.request(method, {}, timeout=5) == REPLY[method]
        assert seen == [role]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_client_newer_than_the_daemon_uses_the_daemon_minor(
    daemon: tuple[BridgeServer, Path, list[str]],
) -> None:
    _, sock, seen = daemon
    client = await connect(sock, "cli", minor=7)
    try:
        assert client.hello_result["minor"] == 1
        assert await client.peer.request("daemon.info", {}, timeout=5) == REPLY["daemon.info"]
        assert seen == ["cli"]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "method"),
    [
        ("cli", "tools.list"),
        ("cli", "tools.call"),
        ("mcp-proxy", "daemon.info"),
        ("mcp-proxy", "ui.open_mid"),
        ("cli", "ui.open_mid"),
    ],
)
async def test_1_1_role_rules_hold_before_params_and_handlers(
    daemon: tuple[BridgeServer, Path, list[str]], role: Role, method: str
) -> None:
    _, sock, seen = daemon
    client = await connect(sock, role, minor=1)
    try:
        with pytest.raises(BridgeError) as exc:
            await client.peer.request(method, {}, timeout=5)
        assert exc.value.name == "role_rejected"
        assert seen == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_1_0_server_says_minor_0() -> None:
    server = _server(minor=0)

    async def info(peer: Peer, params: Empty) -> DaemonInfo:
        return DaemonInfo(remote_llm=False, chat_model="m")

    server.handlers["daemon.info"] = info
    sock, srv = await _start(server)
    try:
        client = await connect(sock, "cli", minor=1)
        try:
            assert client.hello_result["minor"] == 0
            with pytest.raises(BridgeError) as exc:
                await client.peer.request("daemon.info", {}, timeout=5)
            assert exc.value.name == "method_not_available"
        finally:
            await client.close()
    finally:
        srv.close()
        await server.close_all()
        await srv.wait_closed()
