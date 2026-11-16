"""Peer check for ``role=extension`` (plan 02 step 3, D6, B-M10).

`tern-nmhost` is a Python console script, so the peer's ``/proc/<pid>/exe`` is the interpreter
and proves nothing. An extension peer must instead:
- have the configured TB binary as its **parent** (``/proc/<ppid>/exe``), and
- run the installed `tern-nmhost` entry point (``/proc/<pid>/cmdline``).

This hardens against MCP clients and other tools on the socket, not against same-user code
(A-21). ``/proc`` is Linux-only; macOS (plan 16) needs ``LOCAL_PEERPID`` instead.
"""

from __future__ import annotations

import os
import socket
import struct
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

NMHOST_SCRIPT = "tern-nmhost"

RejectReason = Literal["parent_not_thunderbird", "not_nmhost", "process_unreadable"]


@dataclass(frozen=True)
class PeerCred:
    pid: int
    uid: int
    gid: int


class _SockOpt(Protocol):
    def getsockopt(self, level: int, optname: int, buflen: int, /) -> bytes: ...


def peer_cred(sock: _SockOpt) -> PeerCred:
    """``SO_PEERCRED`` of a connected AF_UNIX socket."""
    fmt = "3i"
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(fmt))
    pid, uid, gid = struct.unpack(fmt, raw)
    return PeerCred(pid, uid, gid)


class ProcessInfo(Protocol):
    def exe(self, pid: int) -> Path: ...
    def ppid(self, pid: int) -> int: ...
    def cmdline(self, pid: int) -> list[str]: ...


class ProcFs:
    """``ProcessInfo`` from ``/proc``."""

    def __init__(self, root: Path = Path("/proc")) -> None:
        self._root = root

    def exe(self, pid: int) -> Path:
        return Path(os.readlink(self._root / str(pid) / "exe"))

    def ppid(self, pid: int) -> int:
        stat = (self._root / str(pid) / "stat").read_text()
        # Field 2 (comm) may contain spaces and parentheses; fields after the last ')' are fixed.
        return int(stat[stat.rindex(")") + 2 :].split()[1])

    def cmdline(self, pid: int) -> list[str]:
        raw = (self._root / str(pid) / "cmdline").read_bytes()
        return [a.decode("utf-8", "surrogateescape") for a in raw.split(b"\0") if a]


def installed_nmhost_path() -> Path:
    """Absolute path of the `tern-nmhost` launcher in this environment's scripts dir."""
    return Path(sysconfig.get_path("scripts")) / NMHOST_SCRIPT


def check_extension_peer(
    pid: int, info: ProcessInfo, *, thunderbird_bin: Path, nmhost_path: Path
) -> RejectReason | None:
    """``None`` if ``pid`` may take ``role=extension``; otherwise why not."""
    try:
        parent_exe = info.exe(info.ppid(pid))
        argv = info.cmdline(pid)
    except (OSError, ValueError, IndexError):
        return "process_unreadable"
    if os.path.realpath(parent_exe) != os.path.realpath(thunderbird_bin):
        return "parent_not_thunderbird"
    # A console script runs as `<python> <script> <args...>` (shebang); a direct exec as `<script>`.
    if not any(os.path.realpath(a) == os.path.realpath(nmhost_path) for a in argv[:2]):
        return "not_nmhost"
    return None
