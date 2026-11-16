"""The daemon's Unix socket: single instance, mode 0600, no TCP (decisions #3).

The runtime dir is created 0700 and the socket 0600. A ``flock`` on ``ternd.lock`` makes one
``ternd`` per runtime dir: a second one exits without touching the live socket. Only the lock
holder removes a leftover socket file (a crashed daemon's), so a live socket is never unlinked.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from tern.bridge.peercred import peer_cred
from tern.bridge.server import BridgeServer

log = logging.getLogger(__name__)


class InstanceLock:
    """An exclusive, non-blocking ``flock`` held for the daemon's lifetime."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[bytes] | None = None

    def acquire(self) -> bool:
        fh = open(self.path, "ab")  # noqa: SIM115  # held open for the lock's lifetime
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def prepare_runtime_dir(run_dir: Path) -> None:
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)


@contextlib.contextmanager
def _umask(mask: int) -> Iterator[None]:
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


async def start(server: BridgeServer, sock_path: Path) -> asyncio.Server:
    """Listen on ``sock_path`` (the caller holds the instance lock)."""
    if sock_path.exists() or sock_path.is_symlink():
        log.info("removing a leftover bridge socket file")
        sock_path.unlink()

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")  # asyncio's TransportSocket wrapper
        if sock is None or sock.family != socket.AF_UNIX:
            raise RuntimeError("bridge connection is not AF_UNIX")
        await server.handle_connection(reader, writer, peer_cred(sock).pid)

    # umask 0177 at bind time: the socket is never visible with wider permissions.
    with _umask(0o177):
        srv = await asyncio.start_unix_server(on_connect, path=str(sock_path))
    os.chmod(sock_path, 0o600)
    return srv
