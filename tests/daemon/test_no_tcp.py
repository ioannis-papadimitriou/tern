"""ternd opens no TCP/UDP socket; its listener is AF_UNIX (decisions #3, plan 02 step 3)."""

from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path

from tern import paths
from tern.testing.procs import (
    env_with_home,
    inet_inodes,
    socket_inodes,
    stop,
    ternd_cmd,
    unix_inodes,
    wait_for_socket,
)


def test_every_daemon_socket_is_af_unix(tern_home: Path) -> None:
    proc = subprocess.Popen(ternd_cmd(), env=env_with_home(tern_home))
    try:
        sock = paths.resolve().socket
        wait_for_socket(sock)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(sock))  # an accepted connection is a socket too
        held = socket_inodes(proc.pid)
        assert held, "ternd holds no sockets: the check would pass vacuously"
        assert held <= unix_inodes(proc.pid)
        assert not held & inet_inodes(proc.pid)
        c.close()
    finally:
        assert stop(proc) == 0


def test_the_check_detects_a_tcp_socket(tmp_path: Path) -> None:
    """Control: the same inode check flags a process that holds a TCP socket."""
    code = (
        "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); time.sleep(30)"
    )
    proc = subprocess.Popen(["python3", "-c", code])
    held: set[str] = set()
    try:
        for _ in range(100):
            held = socket_inodes(proc.pid)
            if held:
                break
            time.sleep(0.05)
        assert held & inet_inodes(proc.pid)
    finally:
        proc.kill()
        proc.wait()
