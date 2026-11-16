"""tern.paths: TERN_HOME relocates all four dirs; no silent fallback for the runtime dir."""

from pathlib import Path

import pytest

from tern import paths


def test_tern_home_relocates_all_four() -> None:
    p = paths.resolve({"TERN_HOME": "/tmp/x", "XDG_RUNTIME_DIR": "/run/user/1"})
    assert (p.data, p.state, p.run, p.config) == tuple(
        Path("/tmp/x") / d for d in ("data", "state", "run", "config")
    )
    assert p.socket == Path("/tmp/x/run/bridge.sock")
    assert p.config_file == Path("/tmp/x/config/config.toml")


def test_default_socket_is_under_xdg_runtime_dir() -> None:
    p = paths.resolve({"XDG_RUNTIME_DIR": "/run/user/1"})
    assert p.socket == Path("/run/user/1/tern/bridge.sock")


@pytest.mark.parametrize(
    "env", [{}, {"TERN_HOME": ""}, {"TERN_HOME": "relative/dir"}], ids=["no-xdg", "empty", "rel"]
)
def test_unusable_environment_raises(env: dict[str, str]) -> None:
    with pytest.raises(paths.PathsError):
        paths.resolve(env)


def test_every_test_runs_under_a_temp_tern_home(tmp_path_factory: pytest.TempPathFactory) -> None:
    # tests/conftest.py (plan 02 review): no test outside tests/hooks sees the real platform dirs.
    import os

    home = Path(os.environ["TERN_HOME"])
    assert home.is_relative_to(tmp_path_factory.getbasetemp())
    assert paths.resolve().socket.is_relative_to(home)
