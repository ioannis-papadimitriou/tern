"""Locate the active Thunderbird profile directory across platforms.

Resolution order:

1. An explicit profile directory: in tern, ``tb_profile_dir`` in ``config.toml``, installed with
   :func:`set_profile_dir` (upstream: the ``THUNDERBIRD_PROFILE`` environment variable, which
   tern does not read). Use this when Thunderbird lives on a different filesystem than the
   server, e.g. running under WSL against a Windows install, or pointing at a backup.
2. The default profile recorded in Thunderbird's ``profiles.ini``, searched in the
   standard per-platform locations. Under WSL the Windows-side
   ``/mnt/<drive>/Users/*/AppData/Roaming/Thunderbird`` roots are searched too.

A "profile directory" is the folder that contains ``global-messages-db.sqlite``
and ``prefs.js``.
"""

from __future__ import annotations

import configparser
import os
import sys
from functools import lru_cache
from pathlib import Path


def _candidate_roots() -> list[Path]:
    """Standard locations of Thunderbird's ``profiles.ini`` per platform."""
    home = Path.home()
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "Thunderbird"] if appdata else []
    if sys.platform == "darwin":
        return [home / "Library" / "Thunderbird"]
    # Linux / other unix, including the Snap and Flatpak sandboxes.
    roots = [
        home / ".thunderbird",
        home / ".mozilla-thunderbird",
        home / "snap" / "thunderbird" / "common" / ".thunderbird",
        home / ".var" / "app" / "org.mozilla.Thunderbird" / ".thunderbird",
    ]
    if _is_wsl():
        roots += _wsl_windows_roots()
    return roots


def _is_wsl() -> bool:
    """True when running under Windows Subsystem for Linux."""
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _wsl_windows_roots(mnt: Path = Path("/mnt")) -> list[Path]:
    """Windows-side Thunderbird roots visible from WSL (``/mnt/<drive>/Users/*``)."""
    try:
        drives = sorted(p for p in mnt.iterdir() if len(p.name) == 1)
    except OSError:
        return []
    return [
        user / "AppData" / "Roaming" / "Thunderbird"
        for drive in drives
        for user in sorted((drive / "Users").glob("*"))
        if (user / "AppData" / "Roaming" / "Thunderbird").is_dir()
    ]


def _in_use(profile: Path) -> bool:
    """True if a live Thunderbird process holds this profile's ``lock``.

    On Linux the lock is a symlink to ``<ip>:+<pid>``; it is left behind after
    a crash, so the pid must still be a running Thunderbird. Other platforms
    use lock files that persist after exit, so they never count as in use and
    the choice falls back to recency.
    """
    try:
        target = os.readlink(profile / "lock")
    except OSError:
        return False
    _, _, pid = target.rpartition("+")
    if not pid.isdigit():
        return False
    try:
        comm = Path(f"/proc/{pid}/comm").read_text()
    except OSError:
        return False
    return "thunderbird" in comm.lower()


def _last_used(profile: Path) -> float:
    """Most recent mtime of files Thunderbird writes while in use."""
    times = []
    for name in ("prefs.js", "global-messages-db.sqlite", "times.json"):
        try:
            times.append((profile / name).stat().st_mtime)
        except OSError:
            pass
    return max(times, default=0.0)


def _profile_from_ini(root: Path) -> Path | None:
    """Resolve the default profile path declared in ``root/profiles.ini``."""
    ini = root / "profiles.ini"
    if not ini.exists():
        return None

    cp = configparser.ConfigParser()
    cp.read(ini, encoding="utf-8")

    # Prefer an [InstallXXXX] Default= entry: that's the profile Thunderbird
    # actually launches. With several installs (e.g. a distro package plus a
    # tarball in /opt), each has its own default, so pick the one Thunderbird
    # is running right now, else the most recently used.
    installs = []
    for section in cp.sections():
        if section.startswith("Install") and cp.has_option(section, "Default"):
            path = root / cp.get(section, "Default")
            if path.exists() and path not in installs:
                installs.append(path)
    if installs:
        return max(installs, key=lambda p: (_in_use(p), _last_used(p)))

    # Fall back to the [ProfileN] flagged Default=1, else the first one declared.
    fallback: Path | None = None
    for section in cp.sections():
        if not section.startswith("Profile") or not cp.has_option(section, "Path"):
            continue
        rel = cp.get(section, "Path")
        is_relative = cp.getboolean(section, "IsRelative", fallback=True)
        path = (root / rel) if is_relative else Path(rel)
        if cp.getboolean(section, "Default", fallback=False):
            return path
        fallback = fallback or path
    return fallback


#: The explicit profile directory (tern's ``tb_profile_dir``), or None for discovery.
_override: Path | None = None


def set_profile_dir(path: Path | None) -> None:
    """Use ``path`` as the profile directory (None: discover it from ``profiles.ini``)."""
    global _override
    if path != _override:
        _override = path
        profile_dir.cache_clear()


@lru_cache(maxsize=1)
def profile_dir() -> Path:
    """Return the resolved Thunderbird profile directory.

    Raises:
        FileNotFoundError: if no profile can be located and no override is set.
    """
    if _override is not None:
        path = _override.expanduser()
        if not path.exists():
            raise FileNotFoundError("tb_profile_dir points to a missing directory")
        return path

    for root in _candidate_roots():
        profile = _profile_from_ini(root)
        if profile and profile.exists():
            return profile

    raise FileNotFoundError(
        "Could not locate a Thunderbird profile automatically. Set tb_profile_dir in "
        "tern's config.toml to your profile directory "
        "(the folder containing global-messages-db.sqlite)."
    )


def database_path() -> Path:
    """Path to the GLODA global message index inside the active profile."""
    return profile_dir() / "global-messages-db.sqlite"


def prefs_path() -> Path:
    """Path to ``prefs.js`` inside the active profile."""
    return profile_dir() / "prefs.js"


def calendar_dir() -> Path:
    """``calendar-data`` directory inside the active profile."""
    return profile_dir() / "calendar-data"
