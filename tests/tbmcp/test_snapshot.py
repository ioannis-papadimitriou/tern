"""Snapshots of live databases in the locking modes Thunderbird actually uses."""

import sqlite3
import stat
import threading

import pytest

from tern.tbmcp import config, snapshot


@pytest.fixture
def cache(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(snapshot, "CACHE_DIR", cache)
    monkeypatch.setattr(snapshot, "SNAPSHOT", cache / "snapshot.sqlite")
    monkeypatch.setattr(snapshot, "_copied_from", {})
    return cache


def _profile_db(tmp_path, monkeypatch, name="global-messages-db.sqlite"):
    src = tmp_path / "profile" / name
    src.parent.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "database_path", lambda: src)
    return src


def _count(path, table="t"):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _writer(src, journal_mode, exclusive):
    w = sqlite3.connect(src, isolation_level=None)
    if exclusive:
        w.execute("PRAGMA locking_mode=EXCLUSIVE")
    w.execute(f"PRAGMA journal_mode={journal_mode}")
    w.execute("PRAGMA wal_autocheckpoint=0")
    w.execute("CREATE TABLE t(x)")
    w.execute("BEGIN")
    w.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(1000)])
    w.execute("COMMIT")
    return w


@pytest.mark.parametrize(
    "journal_mode, exclusive",
    [
        ("WAL", False),
        ("WAL", True),  # Thunderbird calendar-data/*.sqlite
        ("DELETE", True),  # Thunderbird global-messages-db.sqlite
    ],
)
def test_snapshot_of_live_database(tmp_path, monkeypatch, cache, journal_mode, exclusive):
    src = _profile_db(tmp_path, monkeypatch)
    w = _writer(src, journal_mode, exclusive)
    try:
        if exclusive:  # a normal reader cannot get in — the reason we copy files
            other = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=0)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("SELECT COUNT(*) FROM t").fetchone()
            other.close()
        snapshot.take_snapshot()
    finally:
        w.close()
    assert _count(snapshot.SNAPSHOT) == 1000  # includes rows still in the WAL


def test_half_written_transaction_is_rolled_back(tmp_path, monkeypatch, cache):
    src = _profile_db(tmp_path, monkeypatch)
    w = _writer(src, "DELETE", exclusive=True)
    try:
        w.execute("PRAGMA cache_size=1")  # force uncommitted pages out to the db file
        w.execute("BEGIN")
        w.executemany("INSERT INTO t VALUES (?)", [(str(i) * 50,) for i in range(5000)])
        assert (src.parent / f"{src.name}-journal").exists()
        snapshot.take_snapshot()  # Thunderbird is mid-transaction
    finally:
        w.close()
    assert _count(snapshot.SNAPSHOT) == 1000  # only committed data


def test_fts_table_with_unknown_tokenizer_is_tolerated(tmp_path, monkeypatch, cache):
    src = _profile_db(tmp_path, monkeypatch)
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.execute("CREATE VIRTUAL TABLE messagesText USING fts3(body, tokenize=porter)")
    con.execute("PRAGMA writable_schema=ON")
    con.execute(
        "UPDATE sqlite_master SET sql = replace(sql, 'porter', 'mozporter') "
        "WHERE name = 'messagesText'"
    )
    con.commit()
    con.close()
    snapshot.take_snapshot()
    assert _count(snapshot.SNAPSHOT) == 1


def test_snapshot_does_not_modify_source(tmp_path, monkeypatch, cache):
    src = _profile_db(tmp_path, monkeypatch)
    _writer(src, "DELETE", exclusive=False).close()
    before = src.read_bytes()
    snapshot.take_snapshot()
    assert src.read_bytes() == before
    assert sorted(p.name for p in src.parent.iterdir()) == [src.name]


def test_change_during_copy_is_retried(tmp_path, monkeypatch, cache):
    src = _profile_db(tmp_path, monkeypatch)
    _writer(src, "DELETE", exclusive=False).close()
    real = snapshot._fingerprint
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        return ("changed", calls["n"]) if calls["n"] == 2 else real(path)

    monkeypatch.setattr(snapshot, "_fingerprint", flaky)
    snapshot.take_snapshot(backoff=0)
    assert _count(snapshot.SNAPSHOT) == 1000
    assert calls["n"] >= 4  # first attempt rejected, second accepted


def test_constant_writes_give_up(tmp_path, monkeypatch, cache):
    src = _profile_db(tmp_path, monkeypatch)
    _writer(src, "DELETE", exclusive=False).close()
    real, counter = snapshot._fingerprint, iter(range(10**6))
    # a fingerprint that never matches itself: the file "changes" during every copy
    monkeypatch.setattr(snapshot, "_fingerprint", lambda p: (*real(p), (next(counter), None, 0)))
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        snapshot.take_snapshot(retries=3, backoff=0)
    assert not snapshot.SNAPSHOT.exists()


def test_corrupt_source_is_rejected(tmp_path, monkeypatch, cache):
    src = _profile_db(tmp_path, monkeypatch)
    src.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
    with pytest.raises(RuntimeError):
        snapshot.take_snapshot(retries=2, backoff=0)
    assert not snapshot.SNAPSHOT.exists()


def test_snapshot_permissions_and_no_leftovers(tmp_path, monkeypatch, cache):
    src = _profile_db(tmp_path, monkeypatch)
    _writer(src, "WAL", exclusive=True).close()
    snapshot.take_snapshot()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert stat.S_IMODE(snapshot.SNAPSHOT.stat().st_mode) == 0o600
    assert [p.name for p in cache.iterdir()] == ["snapshot.sqlite"]


def test_concurrent_snapshots_do_not_clobber(tmp_path, monkeypatch, cache):
    src = _profile_db(tmp_path, monkeypatch)
    _writer(src, "WAL", exclusive=False).close()
    errors = []

    def run():
        try:
            snapshot.take_snapshot()
        except Exception as e:  # any thread failure is collected and asserted empty below
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert _count(snapshot.SNAPSHOT) == 1000


def test_age_follows_snapshot_file(tmp_path, monkeypatch, cache):
    src = _profile_db(tmp_path, monkeypatch)
    _writer(src, "DELETE", exclusive=False).close()
    assert snapshot.snapshot_age_seconds() == float("inf")
    snapshot.take_snapshot()
    assert snapshot.snapshot_age_seconds() < 5
    snapshot.SNAPSHOT.unlink()
    assert snapshot.snapshot_age_seconds() == float("inf")


def test_ensure_fresh_copies_again_as_soon_as_the_source_changes(tmp_path, monkeypatch, cache):
    src = _profile_db(tmp_path, monkeypatch)
    _writer(src, "DELETE", exclusive=False).close()
    snapshot.ensure_fresh()
    assert _count(snapshot.SNAPSHOT) == 1000

    w = sqlite3.connect(src)
    w.execute("INSERT INTO t VALUES (1)")
    w.commit()
    w.close()
    snapshot.ensure_fresh()  # the copy is seconds old, but the source changed
    assert _count(snapshot.SNAPSHOT) == 1001


def test_ensure_fresh_does_not_copy_an_unchanged_source(tmp_path, monkeypatch, cache):
    src = _profile_db(tmp_path, monkeypatch)
    _writer(src, "DELETE", exclusive=False).close()
    calls = []
    real = snapshot.take_snapshot
    monkeypatch.setattr(snapshot, "take_snapshot", lambda: calls.append(1) or real())
    for _ in range(5):
        snapshot.ensure_fresh()
    assert calls == [1]

    snapshot.SNAPSHOT.unlink()  # the copy went away: copied again
    snapshot.ensure_fresh()
    assert calls == [1, 1]


def test_a_failed_refresh_raises_instead_of_serving_the_old_copy(tmp_path, monkeypatch, cache):
    src = _profile_db(tmp_path, monkeypatch)
    _writer(src, "DELETE", exclusive=False).close()
    snapshot.ensure_fresh()
    w = sqlite3.connect(src)
    w.execute("INSERT INTO t VALUES (1)")
    w.commit()
    w.close()

    def fail():
        raise snapshot.SnapshotError("no consistent copy")

    monkeypatch.setattr(snapshot, "take_snapshot", fail)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.ensure_fresh()
    with pytest.raises(snapshot.SnapshotError):  # still stale: tried again, not served
        snapshot.ensure_fresh()


def test_missing_database_raises(tmp_path, monkeypatch, cache):
    monkeypatch.setattr(config, "database_path", lambda: tmp_path / "nope.sqlite")
    with pytest.raises(FileNotFoundError):
        snapshot.take_snapshot()


# --- calendar --------------------------------------------------------------------


def test_calendar_snapshot_copies_present_dbs_and_drops_vanished(tmp_path, monkeypatch, cache):
    cal_dir = tmp_path / "profile" / "calendar-data"
    cal_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "calendar_dir", lambda: cal_dir)
    _writer(cal_dir / "cache.sqlite", "WAL", exclusive=True).close()

    assert [p.name for p in snapshot.take_calendar_snapshot()] == ["calendar-cache.sqlite"]
    assert _count(cache / "calendar-cache.sqlite") == 1000

    (cal_dir / "cache.sqlite").unlink()
    assert snapshot.take_calendar_snapshot() == []
    assert not (cache / "calendar-cache.sqlite").exists()


def test_calendar_snapshot_without_profile(monkeypatch, cache):
    def missing():
        raise FileNotFoundError

    monkeypatch.setattr(config, "calendar_dir", missing)
    assert snapshot.take_calendar_snapshot() == []


def test_no_calendars_is_copied_once_and_again_when_one_appears(tmp_path, monkeypatch, cache):
    cal_dir = tmp_path / "profile" / "calendar-data"
    cal_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "calendar_dir", lambda: cal_dir)
    calls = []
    real = snapshot.take_calendar_snapshot
    monkeypatch.setattr(snapshot, "take_calendar_snapshot", lambda: calls.append(1) or real())
    for _ in range(5):
        snapshot.ensure_calendar_fresh()
    assert calls == [1]

    _writer(cal_dir / "local.sqlite", "WAL", exclusive=False).close()
    snapshot.ensure_calendar_fresh()
    assert calls == [1, 1]
    assert _count(cache / "calendar-local.sqlite") == 1000
