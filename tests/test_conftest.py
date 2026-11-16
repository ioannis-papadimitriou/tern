"""The shared test setup in ``tests/conftest.py`` (plan 04 merge review L1)."""

from __future__ import annotations

from pathlib import Path

from tern.index.store import Store


def test_index_connections_opened_by_tests_skip_fsync(tmp_path: Path) -> None:
    """``make check-fast`` budget: an in-process index connection runs with
    ``synchronous = OFF`` (no fsync per commit) and is otherwise the production connection
    (WAL, foreign keys, migrated schema)."""
    with Store.open(tmp_path / "data" / "index.db") as s:
        conn = s.connection
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 0
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert s.status().messages == 0


def test_heavy_entries_name_existing_tests_and_match_only_their_own_nodeids() -> None:
    """``make check-fast`` budget (plan 08c): every ``HEAVY`` entry names a test directory, file
    or function that exists, so a rename cannot silently drop it (or make it match nothing), and
    prefix matching stops at the file/test boundary."""
    from conftest import HEAVY, is_heavy

    root = Path(__file__).resolve().parents[1]
    for entry in HEAVY:
        path, _, test = entry.partition("::")
        assert (root / path).exists(), entry
        if test:
            assert f"def {test}(" in (root / path).read_text(), entry
    assert is_heavy("tests/daemon/test_uds.py::test_x")
    assert is_heavy("tests/index/test_events.py::test_x[a]")
    assert not is_heavy("tests/index/test_event_lag.py::test_x")
    assert not is_heavy("tests/index/test_service_extra.py::test_x")
    split = (
        "tests/agent/test_citations.py::"
        "test_every_split_position_through_the_loop_shows_no_invalid_citation"
    )
    assert is_heavy(split)
    assert is_heavy(split + "[p]")
    assert not is_heavy("tests/agent/test_citations.py::test_a_hallucinated_mid_is_stripped")
