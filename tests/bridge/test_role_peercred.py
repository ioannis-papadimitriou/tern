"""Extension peer check and the one-extension rule (plan 02 step 3, D6, B-M10, B-M11)."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from tern import paths
from tern.bridge.client import ExtensionLink, Peer
from tern.bridge.peercred import ProcFs, check_extension_peer
from tern.bridge.protocol import BridgeError, Request
from tern.bridge.server import BridgeServer
from tern.daemon import uds
from tern.testing.bridge import NMHOST, TB_BIN, FakeProcessInfo, connect


@dataclass
class Daemon:
    server: BridgeServer
    info: FakeProcessInfo
    sock: Path


@pytest_asyncio.fixture
async def daemon() -> AsyncIterator[Daemon]:
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    info = FakeProcessInfo()
    server = BridgeServer(
        process_info=info,
        thunderbird_bin=TB_BIN,
        nmhost_path=NMHOST,
        link=ExtensionLink(wait_s=1.0),
        liveness_ping_s=0.2,
    )
    srv = await uds.start(server, tp.socket)
    yield Daemon(server, info, tp.socket)
    srv.close()
    await server.close_all()
    await srv.wait_closed()


# -- the check itself -----------------------------------------------------------------------


def test_both_conditions_hold() -> None:
    assert (
        check_extension_peer(1, FakeProcessInfo(), thunderbird_bin=TB_BIN, nmhost_path=NMHOST)
        is None
    )


def test_parent_not_thunderbird() -> None:
    info = FakeProcessInfo(parent_exe=Path("/usr/bin/bash"))
    assert (
        check_extension_peer(1, info, thunderbird_bin=TB_BIN, nmhost_path=NMHOST)
        == "parent_not_thunderbird"
    )


def test_cmdline_not_nmhost() -> None:
    info = FakeProcessInfo(argv=["/venv/bin/python", "/venv/bin/tern-mcp"])
    assert check_extension_peer(1, info, thunderbird_bin=TB_BIN, nmhost_path=NMHOST) == "not_nmhost"


def test_nmhost_later_in_argv_does_not_count() -> None:
    info = FakeProcessInfo(argv=["/venv/bin/python", "-c", "x", str(NMHOST)])
    assert check_extension_peer(1, info, thunderbird_bin=TB_BIN, nmhost_path=NMHOST) == "not_nmhost"


def test_vanished_process() -> None:
    info = FakeProcessInfo(unreadable=True)
    assert (
        check_extension_peer(1, info, thunderbird_bin=TB_BIN, nmhost_path=NMHOST)
        == "process_unreadable"
    )


def test_procfs_reads_this_process() -> None:
    fs = ProcFs()
    me = os.getpid()
    assert fs.ppid(me) == os.getppid()
    assert fs.exe(me) == Path(os.path.realpath(sys.executable))
    assert fs.cmdline(me)[0]


# -- over the socket ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parent_not_tb_is_role_rejected(daemon: Daemon) -> None:
    daemon.info.parent_exe = Path("/usr/bin/python3")
    with pytest.raises(BridgeError) as exc:
        await connect(daemon.sock, "extension")
    assert exc.value.name == "role_rejected"
    assert daemon.server.link.active is None


@pytest.mark.asyncio
async def test_cmdline_not_nmhost_is_role_rejected(daemon: Daemon) -> None:
    daemon.info.argv = ["/usr/bin/python3", "/tmp/evil.py"]
    with pytest.raises(BridgeError) as exc:
        await connect(daemon.sock, "extension")
    assert exc.value.name == "role_rejected"


@pytest.mark.asyncio
async def test_both_match_is_accepted(daemon: Daemon) -> None:
    c = await connect(daemon.sock, "extension")
    assert c.hello_result["role"] == "daemon"
    assert daemon.server.link.active is not None
    await c.close()


@pytest.mark.asyncio
async def test_client_cannot_claim_daemon_role(daemon: Daemon) -> None:
    with pytest.raises(BridgeError) as exc:
        await connect(daemon.sock, "daemon")
    assert exc.value.name == "role_rejected"


@pytest.mark.asyncio
async def test_live_predecessor_means_extension_already_connected(daemon: Daemon) -> None:
    first = await connect(daemon.sock, "extension")
    with pytest.raises(BridgeError) as exc:
        await connect(daemon.sock, "extension")
    assert exc.value.name == "extension_already_connected"
    assert daemon.server.link.active is not None and not first.peer.closed.is_set()
    await first.close()


@pytest.mark.asyncio
async def test_silent_predecessor_is_replaced(daemon: Daemon) -> None:
    async def silent(peer: Peer, req: Request) -> Any:
        await asyncio.sleep(3600)

    first = await connect(daemon.sock, "extension", serve=silent)
    old_active = daemon.server.link.active
    second = await connect(daemon.sock, "extension")
    assert daemon.server.link.active is not None and daemon.server.link.active is not old_active
    await asyncio.wait_for(first.task, 2)  # the daemon closed the stale connection
    await second.close()


@pytest.mark.asyncio
async def test_approvals_decide_only_from_extension(daemon: Daemon) -> None:
    params = {"approval_id": "a", "nonce": "n", "decision": "approve"}
    for role in ("cli", "mcp-proxy"):
        c = await connect(daemon.sock, role)
        with pytest.raises(BridgeError) as exc:
            await c.peer.request("approvals.decide", params, timeout=2)
        assert exc.value.name == "role_rejected"
        await c.close()
    ext = await connect(daemon.sock, "extension")
    with pytest.raises(BridgeError) as exc:
        await ext.peer.request("approvals.decide", params, timeout=2)
    assert exc.value.name == "unavailable"  # placeholder until plan 06b
    await ext.close()


@pytest.mark.asyncio
async def test_mail_events_only_from_extension(daemon: Daemon) -> None:
    seen: list[str] = []

    async def record(peer: Peer, params: Any) -> None:
        seen.append(peer.role or "?")

    daemon.server.notification_handlers["event.folder.changed"] = record
    for role in ("cli", "mcp-proxy", "extension"):
        c = await connect(daemon.sock, role)
        await c.peer.notify("event.folder.changed", {"folder_id": "f1"})
        # Notifications are handled inline, in order: a ping reply means it was processed.
        await c.peer.request("bridge.ping", {}, timeout=2)
        await c.close()
    assert seen == ["extension"]
