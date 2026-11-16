"""Profile resolution: explicit profile dir and profiles.ini parsing (ported from thunderbird-mcp
4921d1e; tern sets the profile dir from config, never from ``THUNDERBIRD_PROFILE``)."""

import os

import pytest

from tern.tbmcp import config


@pytest.fixture(autouse=True)
def _clear_cache():
    config.profile_dir.cache_clear()
    yield
    config.profile_dir.cache_clear()


def test_explicit_profile_dir_wins(tmp_path):
    # upstream: the THUNDERBIRD_PROFILE environment variable; tern: tb_profile_dir
    prof = tmp_path / "my-profile"
    prof.mkdir()
    config.set_profile_dir(prof)
    assert config.profile_dir() == prof


def test_explicit_profile_dir_missing_raises(tmp_path):
    config.set_profile_dir(tmp_path / "nope")
    with pytest.raises(FileNotFoundError):
        config.profile_dir()


def test_profile_env_variable_is_not_read(tmp_path, monkeypatch):
    prof = tmp_path / "env-profile"
    prof.mkdir()
    monkeypatch.setenv("THUNDERBIRD_PROFILE", str(prof))
    config.set_profile_dir(None)  # discovery, over no roots (conftest)
    with pytest.raises(FileNotFoundError):
        config.profile_dir()


def test_set_profile_dir_clears_the_cache(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    config.set_profile_dir(a)
    assert config.profile_dir() == a
    config.set_profile_dir(b)
    assert config.profile_dir() == b


def _write_ini(root, body):
    root.mkdir(parents=True, exist_ok=True)
    (root / "profiles.ini").write_text(body, encoding="utf-8")


def test_install_default_preferred(tmp_path):
    prof = tmp_path / "Profiles" / "abc.default-release"
    prof.mkdir(parents=True)
    _write_ini(
        tmp_path,
        "[Install123]\n"
        "Default=Profiles/abc.default-release\n\n"
        "[Profile0]\n"
        "Name=default-release\n"
        "IsRelative=1\n"
        "Path=Profiles/abc.default-release\n",
    )
    assert config._profile_from_ini(tmp_path) == prof


def test_profile_default_flag_fallback(tmp_path):
    prof = tmp_path / "Profiles" / "xyz.default"
    prof.mkdir(parents=True)
    _write_ini(
        tmp_path,
        "[Profile0]\nIsRelative=1\nPath=Profiles/other\n\n"
        "[Profile1]\nIsRelative=1\nPath=Profiles/xyz.default\nDefault=1\n",
    )
    assert config._profile_from_ini(tmp_path) == prof


def test_no_ini_returns_none(tmp_path):
    assert config._profile_from_ini(tmp_path) is None


def test_wsl_windows_roots(tmp_path):
    tb = tmp_path / "c" / "Users" / "You" / "AppData" / "Roaming" / "Thunderbird"
    tb.mkdir(parents=True)
    (tmp_path / "c" / "Users" / "Public").mkdir()  # no Thunderbird here
    (tmp_path / "wsl").mkdir()  # not a drive letter
    assert config._wsl_windows_roots(tmp_path) == [tb]


def test_wsl_roots_missing_mount(tmp_path):
    assert config._wsl_windows_roots(tmp_path / "nope") == []


def _install_ini(tmp_path, *names):
    for n in names:
        (tmp_path / "Profiles" / n).mkdir(parents=True)
    body = "".join(f"[Install{i}]\nDefault=Profiles/{n}\n\n" for i, n in enumerate(names))
    _write_ini(tmp_path, body)
    return [tmp_path / "Profiles" / n for n in names]


def test_several_installs_prefer_most_recently_used(tmp_path):
    old, new = _install_ini(tmp_path, "old.default-release", "new.default-release")
    (old / "prefs.js").write_text("")
    (new / "prefs.js").write_text("")
    os.utime(old / "prefs.js", (1_600_000_000, 1_600_000_000))
    assert config._profile_from_ini(tmp_path) == new


def test_several_installs_prefer_running_profile(tmp_path, monkeypatch):
    running, recent = _install_ini(tmp_path, "running", "recent")
    (recent / "prefs.js").write_text("")  # more recent, but not running
    monkeypatch.setattr(config, "_in_use", lambda p: p == running)
    assert config._profile_from_ini(tmp_path) == running


# upstream skipped this without /proc; tern v1 is Linux only, so it always runs
def test_in_use_requires_live_thunderbird_pid(tmp_path):
    prof = tmp_path / "p"
    prof.mkdir()
    assert config._in_use(prof) is False  # no lock
    os.symlink(f"127.0.1.1:+{os.getpid()}", prof / "lock")
    assert config._in_use(prof) is False  # live pid, but it's pytest, not thunderbird
    (prof / "lock").unlink()
    os.symlink("127.0.1.1:+999999999", prof / "lock")
    assert config._in_use(prof) is False  # stale lock from a dead process
