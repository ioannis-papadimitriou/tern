"""Subprocess helpers for tests that run real ``ternd`` / ``tern-nmhost`` processes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from tern import paths

#: What :func:`isolate_from_embeddings` writes: ``""`` disables embedding (``TernConfig``).
EMBED_OFF_LINE = 'embed_model = ""\n'


def ternd_cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "tern.daemon.main", *args]


def isolate_from_embeddings(home: Path) -> Path:
    """Disable embedding in ``home``'s ``config.toml`` unless the file already sets ``embed_model``.

    The default config embeds through the loopback embeddings URL (the user's LM Studio, when one
    runs), so a subprocess ``ternd`` would open a TCP connection at start and send it requests.
    Keys already in the file are kept (the line is appended); a file that names ``embed_model``
    is the caller's explicit choice and is left alone. Returns the config file's path."""
    cfg = paths.resolve({"TERN_HOME": str(home)}).config_file
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(EMBED_OFF_LINE, encoding="utf-8")
        return cfg
    text = cfg.read_text(encoding="utf-8")
    if "embed_model" in tomllib.loads(text):
        return cfg
    sep = "" if not text or text.endswith("\n") else "\n"
    cfg.write_text(text + sep + EMBED_OFF_LINE, encoding="utf-8")
    return cfg


def env_with_home(home: Path) -> dict[str, str]:
    """The environment for a subprocess ``ternd``/``tern-nmhost`` on ``home``: ``TERN_HOME`` set
    and, through :func:`isolate_from_embeddings`, embedding off unless the config turns it on."""
    isolate_from_embeddings(home)
    env = dict(os.environ)
    env["TERN_HOME"] = str(home)
    return env


def wait_for_socket(sock: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"socket {sock} did not appear within {timeout} s")


def stop(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> int:
    if proc.poll() is None:
        proc.terminate()
    return proc.wait(timeout)


def socket_inodes(pid: int) -> set[str]:
    """Inodes of the sockets ``pid`` holds open."""
    out: set[str] = set()
    fd_dir = Path(f"/proc/{pid}/fd")
    for fd in fd_dir.iterdir():
        try:
            target = os.readlink(fd)
        except FileNotFoundError:
            continue  # fd closed while listing
        if target.startswith("socket:["):
            out.add(target[len("socket:[") : -1])
    return out


def inet_inodes(pid: int) -> set[str]:
    """Inodes of every TCP/UDP socket (v4 and v6) in ``pid``'s network namespace."""
    out: set[str] = set()
    for table in ("tcp", "tcp6", "udp", "udp6"):
        path = Path(f"/proc/{pid}/net/{table}")
        if not path.exists():
            continue
        for line in path.read_text().splitlines()[1:]:
            cols = line.split()
            if len(cols) > 9:
                out.add(cols[9])
    return out


def unix_inodes(pid: int) -> set[str]:
    out: set[str] = set()
    for line in Path(f"/proc/{pid}/net/unix").read_text().splitlines()[1:]:
        cols = line.split()
        if len(cols) > 6:
            out.add(cols[6])
    return out
