"""The Thunderbird profile that started ``tern-nmhost``, as a stable id for the index binding.

The index is bound to one TB profile (``Store.bind_profile``). The extension's own id lives in
its ``storage.local``, which TB wipes when the add-on is removed, so a reinstall used to orphan
the index (``profile_mismatch``). The host knows better: TB is its parent process, and TB holds
``<profile>/.parentlock`` open while the profile is in use. The id derived from that directory
is the same across reinstalls, temporary and permanent installs, and TB restarts.

Linux only (``/proc``); anything unreadable gives ``None`` and the extension's id is kept.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

#: Namespace of path-derived profile ids (UUID version 5). Random extension ids are version 4.
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://tern.invalid/tb-profile")


def profile_uuid(profile_dir: Path) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, str(profile_dir.resolve()))


def _is_profile(d: Path) -> bool:
    return (d / "prefs.js").is_file()


def _from_cmdline(proc: Path) -> Path | None:
    try:
        argv = (proc / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    args = [a.decode(errors="replace") for a in argv]
    for i, a in enumerate(args):
        if a in ("--profile", "-profile") and i + 1 < len(args):
            d = Path(args[i + 1]).expanduser()
            return d if _is_profile(d) else None
        if a.startswith("--profile="):
            d = Path(a.split("=", 1)[1]).expanduser()
            return d if _is_profile(d) else None
    return None


def _from_lock(proc: Path) -> Path | None:
    try:
        fds = list((proc / "fd").iterdir())
    except OSError:
        return None
    for fd in fds:
        try:
            target = Path(os.readlink(fd))
        except OSError:
            continue
        if target.name == ".parentlock" and _is_profile(target.parent):
            return target.parent
    return None


def profile_dir(pid: int, proc_root: Path = Path("/proc")) -> Path | None:
    """The profile directory of TB process ``pid``: ``--profile`` if given, else its held lock."""
    proc = proc_root / str(pid)
    return _from_cmdline(proc) or _from_lock(proc)


def parent_profile_id() -> str | None:
    """The path-derived id of the profile of TB, the parent of this process; None if unknown."""
    d = profile_dir(os.getppid())
    return None if d is None else str(profile_uuid(d))
