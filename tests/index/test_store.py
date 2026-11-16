"""Plan 04 step 2: Store open/bind/read-only rules and the write API's conflict and hook rules.

Content here is synthetic and inline; plan 04 step 6 runs the fixture conflict scenarios through
this API (tests/index/test_mid_conflict.py).
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from tern import paths
from tern.bridge.protocol import BridgeError
from tern.index.store import (
    AttachmentRecord,
    LocationState,
    MessageRecord,
    NoSuchLocation,
    Store,
    StoreError,
    StoreReadOnly,
    index_path,
)

M1, M2 = "a" * 64, "b" * 64
MX = "x-" + "c" * 64
H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64
F1, F2 = "full-" + "4" * 64, "full-" + "5" * 64
INBOX, ARCHIVE = "acct1:/INBOX", "acct1:/Archive"
S0 = LocationState()
T0 = 1_753_783_200

Event = tuple[str, str, LocationState | None, str]


def rec(
    mid: str,
    content_hash: str,
    body: str = "body",
    *,
    header_message_id: str | None = None,
    attachments: tuple[AttachmentRecord, ...] = (),
) -> MessageRecord:
    return MessageRecord(
        mid=mid,
        content_hash=content_hash,
        parse_status="ok",
        body_text=body,
        header_message_id=header_message_id,
        attachments=attachments,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "data" / "index.db")
    yield s
    s.close()


@pytest.fixture
def events(store: Store) -> list[Event]:
    seen: list[Event] = []
    store.location_observers.append(lambda m, f, st, src: seen.append((m, f, st, src)))
    return seen


@pytest.fixture
def removed(store: Store) -> list[str]:
    seen: list[str] = []
    store.message_removed_observers.append(seen.append)
    return seen


def add(store: Store, mid: str, h: str, folder: str = INBOX, body: str = "body") -> None:
    with store.transaction():
        store.upsert_message(rec(mid, h, body))
        store.add_location(mid, folder, h, S0, T0, source="backfill")


def conflicted(store: Store, mid: str) -> bool:
    row = store.get_message(mid)
    assert row is not None
    return row.conflicted


# ---- open, path, profile, read-only -----------------------------------------------------------


def test_index_path_honours_tern_home() -> None:
    home = Path(os.environ["TERN_HOME"])
    assert index_path(paths.resolve()) == home / "data" / "index.db"


def test_open_sets_dir_mode_wal_foreign_keys_busy_timeout(tmp_path: Path) -> None:
    path = tmp_path / "d" / "index.db"
    with Store.open(path) as s:
        conn = s.connection
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # An existing, looser dir is tightened.
    path.parent.chmod(0o755)
    Store.open(path).close()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_path_derived_id_replaces_a_random_one_once(tmp_path: Path) -> None:
    """A reinstalled add-on lost its random id; the host's path-derived id (v5) takes over."""
    path = tmp_path / "index.db"
    legacy, stable, other = uuid4(), uuid5(NAMESPACE_URL, "a"), uuid5(NAMESPACE_URL, "b")
    with Store.open(path) as s:
        s.bind_profile(legacy)
        s.bind_profile(stable)
        assert s.profile_id() == stable
        with pytest.raises(BridgeError) as err:
            s.bind_profile(other)  # another profile directory: still refused
        assert err.value.name == "profile_mismatch"
        with pytest.raises(BridgeError):
            s.bind_profile(uuid4())  # and never back to a random id
        assert s.profile_id() == stable


def test_second_profile_id_is_profile_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    first, other = uuid4(), uuid4()
    with Store.open(path) as s:
        assert s.profile_id() is None
        s.bind_profile(first)
        s.bind_profile(first)  # same profile again: fine
    with Store.open(path) as s:
        with pytest.raises(BridgeError) as err:
            s.bind_profile(other)
        assert err.value.name == "profile_mismatch"
        assert s.profile_id() == first
    with Store.open(path, readonly=True) as ro:
        assert ro.profile_id() == UUID(str(first))


def test_readonly_open_refuses_writes_and_creates_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "none" / "index.db"
    with pytest.raises(FileNotFoundError):
        Store.open(missing, readonly=True)
    assert not missing.parent.exists()

    path = tmp_path / "index.db"
    with Store.open(path) as rw:
        add(rw, M1, H1)
    with Store.open(path, readonly=True) as ro:
        assert ro.status().messages == 1
        with pytest.raises(StoreReadOnly):
            ro.upsert_message(rec(M2, H2))
        with pytest.raises(StoreReadOnly):
            ro.bind_profile(uuid4())
        # The connection itself is read-only (file:…?mode=ro), not only the Store's check.
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.connection.execute("INSERT INTO meta(key, value) VALUES ('probe', '1')")
    with Store.open(path, readonly=True) as ro:
        assert ro.status().messages == 1


def test_readonly_reader_beside_a_writer(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    with Store.open(path) as rw:
        add(rw, M1, H1)
        with Store.open(path, readonly=True) as ro:
            add(rw, M2, H2)
            assert ro.status().messages == 2


# ---- write API --------------------------------------------------------------------------------


def test_upsert_insert_replace_and_keep(store: Store) -> None:
    att = (AttachmentRecord("a.pdf", "application/pdf", 10),)
    with store.transaction():
        assert store.upsert_message(rec(M1, H1, "first", attachments=att)) == "inserted"
        store.add_location(M1, INBOX, H1, S0, T0, source="backfill")
    row = store.get_message(M1)
    assert row is not None
    assert (row.body_text, row.has_attachments, row.attachments, row.thread_id) == (
        "first",
        True,
        att,
        M1,
    )
    # Same hash: re-ingest replaces the content.
    assert store.upsert_message(rec(M1, H1, "again")) == "replaced"
    row = store.get_message(M1)
    assert row is not None and (row.body_text, row.attachments) == ("again", ())
    # A differing copy never overwrites content a location still holds.
    assert store.upsert_message(rec(M1, H2, "other")) == "kept"
    row = store.get_message(M1)
    assert row is not None and (row.body_text, row.content_hash) == ("again", H1)


def test_upsert_validates_identity(store: Store) -> None:
    with pytest.raises(ValueError):
        store.upsert_message(rec("A" * 64, H1))
    with pytest.raises(ValueError):
        store.upsert_message(rec(M1, "nothex"))
    with pytest.raises(ValueError):
        store.upsert_message(rec(MX, H1, header_message_id="id@x.test"))
    with pytest.raises(StoreError):
        store.add_location(M1, INBOX, H1, S0, T0, source="backfill")


def test_cross_folder_conflict_set_and_cleared(
    store: Store, events: list[Event], removed: list[str]
) -> None:
    add(store, M1, H1, INBOX, "spoof")
    add(store, M1, H1, ARCHIVE)  # identical copy elsewhere: no conflict
    assert not conflicted(store, M1)
    assert store.conflicted_mids() == frozenset()
    add(store, M1, H2, "acct1:/Invoices", "genuine")
    assert conflicted(store, M1)
    assert store.status().conflicted == 1
    assert store.conflicted_mids() == {M1}
    store.remove_location(M1, "acct1:/Invoices", source="event")
    assert not conflicted(store, M1)
    assert [len(store.locations(M1)), removed] == [2, []]
    assert events[-1] == (M1, "acct1:/Invoices", None, "event")


def test_same_folder_differing_copy_is_kept_and_conflicts(store: Store) -> None:
    add(store, M1, H1, INBOX)
    add(store, M1, H1, INBOX)  # byte-identical second copy collapses, no error
    assert (len(store.locations(M1)), store.same_folder_copies(M1)) == (1, [])
    assert not conflicted(store, M1)
    add(store, M1, H2, INBOX)
    assert store.same_folder_copies(M1) == [(INBOX, H2)]
    assert [loc.content_hash for loc in store.locations(M1)] == [H1]
    assert conflicted(store, M1)
    # The differing copy goes; the location stays and the conflict clears.
    store.remove_location(M1, INBOX, content_hash=H2, source="reconcile")
    assert (store.same_folder_copies(M1), conflicted(store, M1)) == ([], False)


def test_removing_the_location_rows_copy_promotes_the_same_folder_copy(
    store: Store, events: list[Event]
) -> None:
    add(store, M1, H1, INBOX)
    add(store, M1, H2, INBOX)
    n = len(events)
    store.remove_location(M1, INBOX, content_hash=H1, source="event")
    assert [loc.content_hash for loc in store.locations(M1)] == [H2]
    assert (store.same_folder_copies(M1), conflicted(store, M1)) == ([], False)
    assert len(events) == n  # the location is still there: no observer call
    with pytest.raises(NoSuchLocation):
        store.remove_location(M1, INBOX, content_hash=H3, source="event")


def test_a_raw_full_mix_conflicts_as_two_differing_full_hashes_do(store: Store) -> None:
    add(store, M1, H1, INBOX)
    add(store, M1, F1, ARCHIVE)
    assert conflicted(store, M1)  # raw vs full-: not comparable, so a conflict (T14, #30)
    store.remove_location(M1, INBOX, source="event")
    assert not conflicted(store, M1)  # only the full- copy is left
    add(store, M1, F2, "acct1:/Big")
    assert conflicted(store, M1)  # two full- hashes differ
    store.remove_location(M1, "acct1:/Big", source="event")
    assert not conflicted(store, M1)


def test_last_location_gone_deletes_message_rows(
    store: Store, events: list[Event], removed: list[str]
) -> None:
    with store.transaction():
        store.upsert_message(rec(M1, H1, attachments=(AttachmentRecord("a", "b", 1),)))
        store.add_location(M1, INBOX, H1, S0, T0, source="backfill")
        conn = store.connection
        conn.execute(
            "INSERT INTO chunks(mid, ord, kind, text, fts_text) "
            "VALUES (?, 0, 'body', 'x', 'zebra')",
            (M1,),
        )
        conn.execute("INSERT INTO sent_recipients(addr, first_seen, count) VALUES ('r', 1, 1)")
    add(store, M2, H2, INBOX)
    conn = store.connection
    assert conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'zebra'").fetchone()
    store.remove_location(M1, INBOX, source="event")
    assert store.get_message(M1) is None
    assert store.locations(M1) == []
    for table in ("attachments", "chunks"):
        assert conn.execute(f"SELECT count(*) FROM {table} WHERE mid = ?", (M1,)).fetchone()[0] == 0
    assert (
        conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'zebra'").fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT count(*) FROM sent_recipients").fetchone()[0] == 1
    assert removed == [M1]
    assert events[-1] == (M1, INBOX, None, "event")
    assert store.get_message(M2) is not None


def test_observers_fire_after_commit_and_never_on_rollback(
    store: Store, events: list[Event]
) -> None:
    in_tx: list[bool] = []
    store.location_observers.append(lambda *a: in_tx.append(store.connection.in_transaction))
    with pytest.raises(RuntimeError, match="boom"):
        with store.transaction():
            store.upsert_message(rec(M1, H1))
            store.add_location(M1, INBOX, H1, S0, T0, source="backfill")
            raise RuntimeError("boom")
    assert (events, store.get_message(M1)) == ([], None)
    add(store, M1, H1, INBOX)
    assert events == [(M1, INBOX, S0, "backfill")]
    assert in_tx == [False]


def test_every_observer_sees_every_committed_event_even_if_one_raises(store: Store) -> None:
    seen: list[Event] = []
    gone: list[str] = []

    def failing(*_: object) -> None:
        raise RuntimeError("observer bug")

    store.location_observers.extend([failing, lambda m, f, st, src: seen.append((m, f, st, src))])
    with pytest.raises(ExceptionGroup) as err:  # one failure per event, re-raised after all ran
        with store.transaction():
            store.upsert_message(rec(M1, H1))
            store.add_location(M1, INBOX, H1, S0, T0, source="backfill")
            store.add_location(M1, ARCHIVE, H1, S0, T0, source="backfill")
    assert [type(e) for e in err.value.exceptions] == [RuntimeError, RuntimeError]
    assert seen == [(M1, INBOX, S0, "backfill"), (M1, ARCHIVE, S0, "backfill")]
    assert len(store.locations(M1)) == 2  # committed: the observer failure undoes nothing

    store.location_observers.clear()
    store.message_removed_observers.extend([failing, gone.append])
    store.remove_location(M1, INBOX, source="event")  # M1 still in ARCHIVE: nothing fires
    with pytest.raises(RuntimeError, match="observer bug"):  # a single failure: as itself
        store.remove_location(M1, ARCHIVE, source="event")
    assert gone == [M1]


def test_nested_failure_is_undone_even_when_the_outer_caller_goes_on(
    store: Store, events: list[Event], removed: list[str]
) -> None:
    """A per-item error caught inside an outer transaction (ingest loops) must not commit the
    item's partial writes or its queued observer events."""
    add(store, M2, H2, ARCHIVE)
    events.clear()
    with store.transaction():
        store.upsert_message(rec(M1, H1))
        store.add_location(M1, INBOX, H1, S0, T0, source="backfill")
        with pytest.raises(RuntimeError, match="item failed"):
            with store.transaction():
                store.add_location(M1, ARCHIVE, H1, S0, T0, source="backfill")
                store.remove_location(M2, ARCHIVE, source="backfill")
                raise RuntimeError("item failed")
        assert store.connection.in_transaction
    assert [loc.folder_id for loc in store.locations(M1)] == [INBOX]
    assert [loc.folder_id for loc in store.locations(M2)] == [ARCHIVE]
    assert events == [(M1, INBOX, S0, "backfill")]
    assert removed == []


def test_set_location_state_fires_only_on_change(store: Store, events: list[Event]) -> None:
    add(store, M1, H1, INBOX)
    events.clear()
    tagged = LocationState(tags=("$label1", "$label1", "a"), flagged=True, read=True)
    store.set_location_state(M1, INBOX, tagged, source="reconcile")
    store.set_location_state(M1, INBOX, tagged, source="reconcile")
    assert events == [(M1, INBOX, LocationState(("a", "$label1"), True, True), "reconcile")]
    assert store.locations(M1)[0].state.tags == ("$label1", "a")
    # Re-adding the identical copy with another state is a state change too.
    store.add_location(M1, INBOX, H1, S0, T0 + 5, source="event")
    assert events[-1] == (M1, INBOX, S0, "event")
    assert store.locations(M1)[0].tb_date == T0 + 5
    with pytest.raises(NoSuchLocation):
        store.set_location_state(M1, ARCHIVE, S0, source="event")


def test_set_location_tb_date_moves_the_date_only(store: Store, events: list[Event]) -> None:
    add(store, M1, H1, INBOX)
    events.clear()
    store.set_location_tb_date(M1, INBOX, T0 - 86_400)
    (loc,) = store.locations(M1)
    assert (loc.tb_date, loc.content_hash, loc.state) == (T0 - 86_400, H1, S0)
    assert events == []
    with pytest.raises(NoSuchLocation):
        store.set_location_tb_date(M1, ARCHIVE, T0)


def test_remove_folder(store: Store, events: list[Event], removed: list[str]) -> None:
    store.upsert_account("acct1", "alex@example.com", "imap")
    store.upsert_folder(INBOX, "acct1", "INBOX", ["inbox"])
    store.upsert_folder(ARCHIVE, "acct1", "Archive", ["archives"])
    assert [f.special_use for f in store.folders()] == [("archives",), ("inbox",)]
    add(store, M1, H1, INBOX)
    add(store, M2, H2, INBOX)
    add(store, M2, H2, ARCHIVE)
    folder_tables = ("ingest_state", "headers_only_items", "skipped_items")
    with store.transaction():
        conn = store.connection
        for folder in (INBOX, ARCHIVE):
            conn.execute(
                "INSERT INTO headers_only_items(folder_id, tb_date, ordinal, mid) "
                "VALUES (?, 1, 0, NULL)",
                (folder,),
            )
            conn.execute(
                "INSERT INTO skipped_items(folder_id, tb_date, ordinal, reason) "
                "VALUES (?, 1, 0, 'large_noid')",
                (folder,),
            )
            conn.execute(
                "INSERT INTO ingest_state(folder_id, status, updated_at) VALUES (?, 'done', 1)",
                (folder,),
            )
    events.clear()
    assert store.remove_folder(INBOX, source="reconcile") == 2
    assert sorted(events) == [(M1, INBOX, None, "reconcile"), (M2, INBOX, None, "reconcile")]
    assert removed == [M1]
    assert [f.folder_id for f in store.folders()] == [ARCHIVE]
    for table in folder_tables:  # INBOX's rows gone, ARCHIVE's kept
        rows = store.connection.execute(f"SELECT folder_id FROM {table}").fetchall()
        assert rows == [(ARCHIVE,)], table
    assert (store.status().headers_only, store.status().skipped_large_noid) == (1, 1)
    assert [loc.folder_id for loc in store.locations(M2)] == [ARCHIVE]


def test_status_counts_skipped_rows_per_reason(store: Store) -> None:
    with store.transaction():
        for ordinal, reason in enumerate(("large_noid", "tb_error", "tb_error")):
            store.connection.execute(
                "INSERT INTO skipped_items(folder_id, tb_date, ordinal, reason) "
                "VALUES (?, 1, ?, ?)",
                (INBOX, ordinal, reason),
            )
    st = store.status()
    assert (st.skipped_large_noid, st.skipped_tb_error) == (1, 2)


def test_status_and_thread(store: Store) -> None:
    add(store, M1, H1)
    add(store, MX, H2)
    with store.transaction():
        store.upsert_message(MessageRecord(mid=M2, content_hash=H3, parse_status="failed"))
        store.add_location(M2, INBOX, H3, S0, T0, source="backfill")
    st = store.status()
    assert (st.schema_version, st.messages, st.locations, st.x_mids) == (1, 3, 3, 1)
    assert (st.parse_ok, st.parse_partial, st.parse_failed, st.conflicted) == (2, 0, 1, 0)
    assert store.thread(M1) == [M1]  # its own root until threads.py (step 5)
    assert store.thread("d" * 64) == []
