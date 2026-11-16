"""Every test runs with ``TERN_HOME`` in a temp dir.

Directory conftests (``tests/bridge``, ``tests/daemon``) override ``tern_home`` with the same
behaviour.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# ``heavy`` (plan 08c, carries plan 04's L1): functional tests that are merely expensive. `make
# check` / `make test` run them; only `make check-fast` skips them, to keep it under 60 s. Unlike
# ``slow`` (wall-clock budgets, `make test-slow` only), nothing here leaves `make check`. Entries
# are nodeid prefixes: a directory ending in "/", a file, or "file::test". Tests can also carry
# ``@pytest.mark.heavy`` themselves (the L1 large-input originals do).
HEAVY: tuple[str, ...] = (
    "tests/daemon/",  # real ternd over a socket, per test
    "tests/index/test_service.py",
    "tests/index/test_reconcile.py",
    "tests/index/test_events.py",
    "tests/index/test_ingest.py",
    "tests/mcp/test_proxy.py",  # subprocess `tern mcp` + ternd
    "tests/mcp/test_transport.py",
    "tests/bridge/test_nmhost.py",  # subprocess native-messaging host
    "tests/test_fixture_manifest.py",
    "tests/agent/test_citations.py::test_every_split_position_through_the_loop_shows_no_invalid_citation",
)


def is_heavy(nodeid: str) -> bool:
    """True when ``nodeid`` falls under an entry of ``HEAVY``."""
    return any(
        nodeid.startswith(p)
        if p.endswith("/")
        else nodeid == p or nodeid.startswith(p + "::") or nodeid.startswith(p + "[")
        for p in HEAVY
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if is_heavy(item.nodeid):
            item.add_marker(pytest.mark.heavy)


@pytest.fixture(autouse=True)
def tern_home(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Path | None:
    # Short base path: AF_UNIX socket paths are limited to 107 bytes.
    home = tmp_path_factory.mktemp("th")
    monkeypatch.setenv("TERN_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _index_db_without_fsync(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Index connections opened in-process by a test skip fsync (``PRAGMA synchronous = OFF``).

    Plan 04 merge review L1 (``make check-fast`` budget): the default (FULL) fsyncs the WAL on
    every commit, which was ~40% of a full-fixture backfill and ~14 s of the unit run. What a
    test can observe is unchanged: every read, write, transaction, WAL mode and cross-connection
    visibility is the same; only durability against power loss differs, which no unit test can
    exercise. Production code keeps SQLite's default. A subprocess (e.g. a real ``ternd``) is not
    affected.
    """
    from tern.index import store

    connect_rw = store._connect_rw  # pyright: ignore[reportPrivateUsage]  # test-only wrapper

    def without_fsync(path: Path) -> sqlite3.Connection:
        conn = connect_rw(path)
        conn.execute("PRAGMA synchronous = OFF")
        return conn

    monkeypatch.setattr(store, "_connect_rw", without_fsync)
