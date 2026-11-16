"""Test doubles for the bridge: a fake ``ProcessInfo`` and a scripted client (no TB needed)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tern.bridge.client import Peer
from tern.bridge.protocol import PROTOCOL, Request, Role

TB_BIN = Path("/opt/thunderbird-153/thunderbird-bin")
NMHOST = Path("/venv/bin/tern-nmhost")


@dataclass
class FakeProcessInfo:
    """Every pid looks the same: parent exe ``parent_exe``, command line ``argv``."""

    parent_exe: Path = TB_BIN
    argv: list[str] = field(default_factory=lambda: ["/venv/bin/python", str(NMHOST), "m", "e"])
    unreadable: bool = False

    def exe(self, pid: int) -> Path:
        if self.unreadable:
            raise FileNotFoundError(pid)
        return self.parent_exe

    def ppid(self, pid: int) -> int:
        if self.unreadable:
            raise FileNotFoundError(pid)
        return pid + 1

    def cmdline(self, pid: int) -> list[str]:
        if self.unreadable:
            raise FileNotFoundError(pid)
        return list(self.argv)


# A real extension sends the same profile_id on every hello from its TB profile (plan 04 step 9
# binds the index to it), so the default is one fixed id, not a fresh one per hello.
TEST_PROFILE_ID = "00000000-0000-4000-8000-000000000001"


def hello_params(role: Role, *, minor: int = 0, profile_id: str | None = None) -> dict[str, Any]:
    h: dict[str, Any] = {
        "protocol": PROTOCOL,
        "minor": minor,
        "role": role,
        "version": "test",
        "capabilities": ["mail.read"] if role == "extension" else [],
    }
    if role == "extension":
        h["profile_id"] = profile_id or TEST_PROFILE_ID
    return h


async def ping_responder(peer: Peer, req: Request) -> Any:
    """A fake extension that answers ``bridge.ping`` and nothing else."""
    if req.method == "bridge.ping":
        return {}
    raise NotImplementedError(req.method)


@dataclass
class Client:
    peer: Peer
    hello_result: Any
    task: asyncio.Task[None]

    async def close(self) -> None:
        await self.peer.close()
        await self.task


async def connect(
    sock: Path,
    role: Role,
    *,
    minor: int = 0,
    serve: Any = ping_responder,
    hello: dict[str, Any] | None = None,
) -> Client:
    """Open the socket, send ``hello``, and keep reading in the background.

    Raises ``BridgeError`` if the daemon rejects the hello.
    """
    reader, writer = await asyncio.open_unix_connection(str(sock))
    peer = Peer(reader, writer, on_request=serve)
    task = asyncio.create_task(peer.run())
    try:
        result = await peer.request("hello", hello or hello_params(role, minor=minor), timeout=5)
    except BaseException:
        await peer.close()
        await task
        raise
    return Client(peer, result, task)
