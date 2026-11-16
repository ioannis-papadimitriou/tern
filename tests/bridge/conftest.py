"""Every bridge test runs with TERN_HOME in a temp dir (CLAUDE.md conventions)."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def tern_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Short base path: AF_UNIX socket paths are limited to 107 bytes.
    home = tmp_path_factory.mktemp("th")
    monkeypatch.setenv("TERN_HOME", str(home))
    return home
