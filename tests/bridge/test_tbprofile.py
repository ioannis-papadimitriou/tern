"""The TB profile id the host derives from its parent process (``tern.bridge.tbprofile``)."""

from __future__ import annotations

import os
from pathlib import Path

from tern.bridge import tbprofile


def _profile(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "prefs.js").write_text("// synthetic\n")
    (d / ".parentlock").touch()
    return d


def _proc(root: Path, pid: int, argv: list[str], fds: list[Path]) -> Path:
    proc = root / "proc"
    p = proc / str(pid)
    (p / "fd").mkdir(parents=True)
    (p / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    for i, target in enumerate(fds):
        os.symlink(target, p / "fd" / str(i))
    return proc


def test_the_profile_of_a_default_launch_comes_from_its_held_lock(tmp_path: Path) -> None:
    prof = _profile(tmp_path, "tb/abcd.default-release")
    install = tmp_path / "opt" / "thunderbird"
    install.mkdir(parents=True)
    (install / ".parentlock").touch()  # TB also locks its install dir: not a profile
    proc = _proc(
        tmp_path,
        42,
        ["/opt/thunderbird/thunderbird"],
        [tmp_path / "x.sqlite", install / ".parentlock", prof / ".parentlock"],
    )
    assert tbprofile.profile_dir(42, proc) == prof


def test_an_explicit_profile_argument_wins(tmp_path: Path) -> None:
    prof = _profile(tmp_path, "tb/one")
    other = _profile(tmp_path, "tb/two")
    proc = _proc(tmp_path, 7, ["thunderbird", "--profile", str(prof)], [other / ".parentlock"])
    assert tbprofile.profile_dir(7, proc) == prof
    proc2 = _proc(tmp_path / "b", 8, ["thunderbird", f"--profile={other}"], [])
    assert tbprofile.profile_dir(8, proc2) == other


def test_no_profile_found_is_none(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 9, ["something-else"], [tmp_path / "file"])
    assert tbprofile.profile_dir(9, proc) is None
    assert tbprofile.profile_dir(12345, tmp_path / "nowhere") is None


def test_the_id_is_stable_per_directory_and_version_5(tmp_path: Path) -> None:
    a, b = _profile(tmp_path, "a"), _profile(tmp_path, "b")
    assert tbprofile.profile_uuid(a) == tbprofile.profile_uuid(tmp_path / "x" / ".." / "a")
    assert tbprofile.profile_uuid(a) != tbprofile.profile_uuid(b)
    assert tbprofile.profile_uuid(a).version == 5
