"""`tern bridge ping` and `tern bridge schema` (plan 02 steps 1, 8, 10); `tern index status` and
`tern index rebuild` (plan 04 step 10); `tern search` and the `index status` embed lines (plan 05
step 6); `tern audit` and `tern undo` (plan 06b step 10)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import sqlite3
import stat
import time
import unicodedata
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import httpx
import platformdirs
import pytest
import test_fixture_manifest as tfm

from tern import cli, paths
from tern.bridge.client import ExtensionLink
from tern.bridge.protocol import BridgeError, UndoParams, UndoResult, export_schema
from tern.bridge.server import BridgeServer
from tern.config import TernConfig
from tern.daemon import uds
from tern.index.events import EventIngest
from tern.index.ingest import Backfill
from tern.index.mid import mid_from_raw
from tern.index.reconcile import Reconcile
from tern.index.schema import SCHEMA_VERSION
from tern.index.service import ALL_FOLDERS, ERROR_PREFIX
from tern.index.source import IngestSource
from tern.index.store import IngestRun, LocationState, MessageRecord, Store, StoreError, index_path
from tern.perms.audit import (
    INVALID_TOOL,
    AuditLog,
    TargetState,
    UndoPayload,
    UndoTarget,
    audit_path,
)
from tern.search import vec
from tern.search.embedder import EmbeddingClient
from tern.search.service import Hit, SearchFilters, SearchResult, SearchService
from tern.testing.bridge import NMHOST, TB_BIN, FakeProcessInfo, connect
from tern.testing.fake_bridge import FakeBridge
from tern.testing.stub_embedder import StubEmbedder
from tern.tools import Tier


@pytest.fixture(autouse=True)
def tern_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    home = tmp_path_factory.mktemp("th")
    monkeypatch.setenv("TERN_HOME", str(home))
    yield home


def test_schema_prints_the_1_0_export(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["bridge", "schema", "--version", "1.0"]) == 0
    assert json.loads(capsys.readouterr().out) == export_schema("1.0")


def test_schema_unknown_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["bridge", "schema", "--version", "1.4"]) == 2
    assert "unknown" in capsys.readouterr().err


def test_ping_without_daemon(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["bridge", "ping", "--n", "3"]) == 2
    assert "no ternd listening" in capsys.readouterr().err


def test_ping_against_a_daemon_with_an_extension() -> None:
    async def run() -> None:
        tp = paths.resolve()
        uds.prepare_runtime_dir(tp.run)
        server = BridgeServer(
            process_info=FakeProcessInfo(),
            thunderbird_bin=TB_BIN,
            nmhost_path=NMHOST,
            link=ExtensionLink(wait_s=1),
        )
        srv = await uds.start(server, tp.socket)
        ext = await connect(tp.socket, "extension")
        stats = await cli.ping_extension(tp.socket, 50)
        assert stats.n == 50 and stats.p50_ms < cli.RTT_P50_GATE_MS
        await ext.close()
        srv.close()
        await server.close_all()
        await srv.wait_closed()

    asyncio.run(run())


# ---- tern index status / tern index rebuild (plan 04 step 10) --------------------------------
# Expected numbers come from MANIFEST.json or from the rows a test writes; none is typed in from
# a run.

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
MESSAGES = tfm.messages(MANIFEST)
FOLDERS = {f["key"]: (a, f) for a in MANIFEST["accounts"] for f in a["folders"]}
PROFILE = UUID("00000000-0000-4000-8000-0000000000aa")


@pytest.fixture(scope="module")
def fixture_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A closed index.db built by Backfill over FakeBridge(FIXTURE), copied into each test."""
    path = tmp_path_factory.mktemp("built") / "index.db"
    with Store.open(path) as store:
        asyncio.run(Backfill(store, IngestSource(FakeBridge(FIXTURE)), TernConfig()).run())
    return path


@pytest.fixture
def built(fixture_index: Path) -> Path:
    """The fixture index, installed at this test's TERN_HOME index path."""
    path = index_path(paths.resolve())
    path.parent.mkdir(mode=0o700, parents=True)
    shutil.copyfile(fixture_index, path)
    return path


def status(capsys: pytest.CaptureFixture[str]) -> list[str]:
    assert cli.main(["index", "status"]) == 0
    return capsys.readouterr().out.splitlines()


def scalar(lines: list[str], key: str) -> str:
    (value,) = [ln.split(" ", 1)[1] for ln in lines if ln.split(" ", 1)[0] == key]
    return value


def fields(line: str) -> dict[str, str]:
    return dict(tok.split("=", 1) for tok in line.split() if "=" in tok)


def rows(lines: list[str], prefix: str) -> dict[str, list[str]]:
    return {ln.split()[0]: ln.split()[1:] for ln in lines if ln.startswith(prefix)}


def write(path: Path, sql: str, *args: object) -> None:
    with Store.open(path) as store, store.transaction():
        store.connection.execute(sql, args)


def x_mids_of_fixture() -> list[str]:
    return [
        mid_from_raw(tfm.read_copy(FIXTURE, MANIFEST, c))
        for key in MANIFEST["no_message_id"]
        for c in MESSAGES[key]["copies"]
    ]


def test_index_status_hashes_and_counts_equal_manifest(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = status(capsys)
    distinct, copies = MANIFEST["distinct_messages"], MANIFEST["message_count"]
    assert scalar(lines, "schema_version") == str(SCHEMA_VERSION)
    assert (scalar(lines, "messages"), scalar(lines, "locations")) == (str(distinct), str(copies))
    assert (scalar(lines, "conflicted"), scalar(lines, "x_mids")) == ("1", "3")
    assert scalar(lines, "x_mid_digest") == x_mid_digest_of(x_mids_of_fixture())
    assert scalar(lines, "headers_only") == scalar(lines, "skipped_large_noid") == "0"
    assert scalar(lines, "backfill") == f"folders_done {len(FOLDERS)}/{len(FOLDERS)}"
    assert scalar(lines, "jobs_failed") == "0"
    parse = fields(scalar(lines, "parse_status"))
    assert sum(int(v) for v in parse.values()) == distinct

    accounts = rows(lines, "acct-")
    assert set(accounts) == {a["hash"] for a in MANIFEST["accounts"]}
    md5 = {a["key"]: 0 for a in MANIFEST["accounts"]}
    for key in MANIFEST["no_message_id"]:
        for c in MESSAGES[key]["copies"]:
            md5[FOLDERS[c["folder"]][0]["key"]] += 1
    for a in MANIFEST["accounts"]:
        got = fields(" ".join(accounts[a["hash"]]))
        assert got["type"] == a["type"]
        assert int(got["folders"]) == len(a["folders"])
        assert int(got["locations"]) == sum(f["count"] for f in a["folders"])
        assert int(got["md5_ids"]) == md5[a["key"]]
        assert got["headers_only"] == "0"
    inv_acct = FOLDERS[MANIFEST["adversarial_pair"]["copies"]["genuine"]["folder"]][0]["hash"]
    assert fields(" ".join(accounts[inv_acct]))["conflicted"] == "1"
    with Store.open(built, readonly=True) as store:
        inv = store.get_message(MANIFEST["named"]["inv-0077"]["mid"])
    assert inv is not None and inv.conflict_class is not None
    cls = inv.conflict_class
    assert [ln for ln in lines if ln.startswith("conflict_class ")] == [
        f"conflict_class {inv_acct} {cls} 1"
    ]

    folders = rows(lines, "fold-")
    assert set(folders) == {f["hash"] for _, f in FOLDERS.values()}
    for a, f in FOLDERS.values():
        assert folders[f["hash"]][0] == a["hash"]
        assert fields(" ".join(folders[f["hash"]]))["locations"] == str(f["count"])

    rate = {ln.split()[1]: fields(ln) for ln in lines if ln.startswith("rate ")}
    for kind in {a["type"] for a in MANIFEST["accounts"]}:
        want = sum(
            f["count"] for a in MANIFEST["accounts"] if a["type"] == kind for f in a["folders"]
        )
        assert int(rate[kind]["msgs"]) == want
        assert rate[kind]["concurrency"] == str(TernConfig().ingest_concurrency)


def x_mid_digest_of(x_mids: list[str]) -> str:
    """The digest rule, recomputed: first 8 hex of sha256 over the sorted mids, one per line."""
    return hashlib.sha256("\n".join(sorted(x_mids)).encode()).hexdigest()[:8]


def test_x_mid_digest_sorts_its_input() -> None:
    """The index happens to return x- mids in key order; the digest must not depend on that."""
    a, b = "x-" + "a" * 64, "x-" + "b" * 64
    assert cli.x_mid_digest([b, a]) == cli.x_mid_digest([a, b]) == x_mid_digest_of([a, b])
    assert x_mid_digest_of([a, b]) != hashlib.sha256(f"{b}\n{a}".encode()).hexdigest()[:8]


def test_index_status_shows_no_names_paths_subjects_or_sentinels(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blob = "\n".join(status(capsys))
    forbidden = [s["value"] for s in MANIFEST["sentinels"]]
    forbidden += [a["name"] for a in MANIFEST["accounts"]]
    forbidden += [f["path"] for _, f in FOLDERS.values()] + list(FOLDERS)
    forbidden += tfm.mail_strings(FIXTURE, MANIFEST)  # subjects, From/To/Cc addresses (L4)
    assert len(forbidden) > 50
    assert sorted({f for f in forbidden if f in blob}) == []


def test_index_status_without_an_index_creates_nothing(
    tern_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["index", "status"]) == 2
    assert "no index" in capsys.readouterr().err
    path = index_path(paths.resolve())
    assert not path.exists() and not path.parent.exists()


def test_index_status_is_one_snapshot_beside_a_writer(
    built: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer (ternd) commits while status is between its queries: every line still comes
    from the snapshot taken at the first read, and the commit is seen by the next status."""
    real_assign = cli.assign
    with Store.open(built) as writer:

        def assign_after_a_commit(names: Any, kind: Any) -> dict[str, str]:
            if kind == "acct":  # once, after store.status() and before the run queries
                writer.record_ingest_run(
                    IngestRun(
                        ts=int(time.time()),
                        kind="backfill",
                        folder_id=None,
                        account_type="pop3",
                        n_msgs=1000,
                        secs=1.0,
                    )
                )
            return real_assign(names, kind)

        monkeypatch.setattr(cli, "assign", assign_after_a_commit)
        lines = status(capsys)
        monkeypatch.setattr(cli, "assign", real_assign)
        pop3 = sum(
            f["count"] for a in MANIFEST["accounts"] if a["type"] == "pop3" for f in a["folders"]
        )
        rate = {ln.split()[1]: fields(ln) for ln in lines if ln.startswith("rate ")}
        assert int(rate["pop3"]["msgs"]) == pop3
        rate = {ln.split()[1]: fields(ln) for ln in status(capsys) if ln.startswith("rate ")}
        assert int(rate["pop3"]["msgs"]) == pop3 + 1000


def test_index_status_on_a_newer_schema_names_rebuild(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(built, "UPDATE meta SET value = ? WHERE key = 'schema_version'", str(SCHEMA_VERSION + 1))
    assert cli.main(["index", "status"]) == 1
    assert "tern index rebuild" in capsys.readouterr().err


def test_index_status_x_mid_digest_and_churn_after_a_rewritten_id_less_message(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = status(capsys)
    assert scalar(before, "x_mid_churn") == "0"
    (copy,) = MESSAGES["noid-scanner"]["copies"]
    folder_id = "{}:/{}".format(*copy["folder"].split("/", 1))
    raw = tfm.read_copy(FIXTURE, MANIFEST, copy)

    async def rewrite() -> None:
        fb = FakeBridge(FIXTURE)
        with Store.open(built) as store:
            events = EventIngest(store, IngestSource(fb), TernConfig())
            Reconcile(store, IngestSource(fb), TernConfig(), events=events)  # the hourly check
            fb.on_event = events.on_event
            await fb.delete([fb.tb_ids(folder_id)[copy["locator"]]])
            await fb.deliver(folder_id, b"X-Mozilla-Status: 0001\n" + raw)

    asyncio.run(rewrite())
    after = status(capsys)
    assert scalar(after, "x_mids") == scalar(before, "x_mids") == "3"
    assert scalar(after, "x_mid_churn") == "1"
    assert scalar(after, "x_mid_digest") != scalar(before, "x_mid_digest")
    old, new = mid_from_raw(raw), mid_from_raw(b"X-Mozilla-Status: 0001\n" + raw)
    want = [new if m == old else m for m in x_mids_of_fixture()]
    assert scalar(after, "x_mid_digest") == x_mid_digest_of(want)


def test_index_status_counts_run_counters_and_missed_in_the_last_24h(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = int(time.time())
    for ts, missed, n in ((now - 60, 2, 1), (now - 2 * cli.MISSED_WINDOW_S, 5, 10)):
        write(
            built,
            "INSERT INTO ingest_runs(ts, kind, missed, stale_retries, hmid_mismatch, "
            "unresolved_events) VALUES (?, 'hourly', ?, ?, ?, ?)",
            ts,
            missed,
            n,
            n,
            n,
        )
    lines = status(capsys)
    assert scalar(lines, "missed_24h") == "2"
    assert scalar(lines, "stale_retries") == scalar(lines, "hmid_mismatch") == "11"
    assert scalar(lines, "unresolved_events") == "11"


def test_index_status_lists_a_failed_job(built: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a, f = FOLDERS["acct2/Projects"]
    write(
        built,
        "UPDATE ingest_state SET status = ? WHERE folder_id = ?",
        ERROR_PREFIX + "RuntimeError",
        f"{a['key']}:/{f['path']}",
    )
    lines = status(capsys)
    tail = lines[lines.index("jobs_failed 1") :]
    assert tail == ["jobs_failed 1", f"  {f['hash']} error:RuntimeError"]
    assert scalar(lines, "backfill") == f"folders_done {len(FOLDERS) - 1}/{len(FOLDERS)}"


def test_index_status_lists_a_failed_job_of_the_star_row(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        built,
        "INSERT INTO ingest_state(folder_id, status, updated_at) VALUES (?, ?, 0)",
        ALL_FOLDERS,
        ERROR_PREFIX + "KeyError",
    )
    lines = status(capsys)
    assert lines[lines.index("jobs_failed 1") :] == ["jobs_failed 1", "  * error:KeyError"]
    assert scalar(lines, "backfill") == f"folders_done {len(FOLDERS)}/{len(FOLDERS)}"


def test_index_status_labels_a_failed_job_of_an_unlisted_folder(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        built,
        "INSERT INTO ingest_state(folder_id, status, updated_at) VALUES (?, ?, 0)",
        "acct9:/Gone",
        ERROR_PREFIX + "OSError",
    )
    lines = status(capsys)
    assert lines[lines.index("jobs_failed 1") :] == [
        "jobs_failed 1",
        "  fold-unlisted error:OSError",
    ]


def test_index_status_rate_lines_come_from_the_backfill_rows(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the rows this test writes: the fixture's runs are deleted first."""
    write(built, "DELETE FROM ingest_runs")
    runs = [  # (kind, account_type, n_msgs, secs, concurrency, throttle)
        ("backfill", "imap", 30, 2.0, 1, None),
        ("backfill", "imap", 10, 2.0, 4, 5.0),
        ("backfill", "pop3", 7, 0.0, 2, None),
        ("backfill", None, 3, 1.5, None, None),
        ("hourly", "imap", 1000, 1.0, 8, 9.0),  # not a backfill: not in any rate
    ]
    for run in runs:
        write(
            built,
            "INSERT INTO ingest_runs(ts, kind, account_type, n_msgs, secs, concurrency, throttle) "
            "VALUES (0, ?, ?, ?, ?, ?, ?)",
            *run,
        )
    assert [ln for ln in status(capsys) if ln.startswith("rate ")] == [
        "rate imap msgs=40 secs=4.0 msg_per_s=10.0 concurrency=1,4 throttle=5,-",
        "rate pop3 msgs=7 secs=0.0 msg_per_s=- concurrency=2 throttle=-",
        "rate - msgs=3 secs=1.5 msg_per_s=2.0 concurrency=- throttle=-",
    ]


def test_index_status_md5_ids_count_headers_only_and_skipped_items(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, f = FOLDERS["acct2/Projects"]
    fid = f"{a['key']}:/{f['path']}"
    base = sum(
        1
        for key in MANIFEST["no_message_id"]
        for c in MESSAGES[key]["copies"]
        if FOLDERS[c["folder"]][0]["key"] == a["key"]
    )
    sql = "INSERT INTO headers_only_items(folder_id, tb_date, ordinal, mid) VALUES (?, 1, ?, ?)"
    write(built, sql, fid, 0, None)  # an md5: TB id: no mid
    write(built, sql, fid, 1, "headers-only@example.invalid")
    write(
        built,
        "INSERT INTO skipped_items(folder_id, tb_date, ordinal, reason) "
        "VALUES (?, 2, 0, 'large_noid')",
        fid,
    )
    lines = status(capsys)
    got = fields(" ".join(rows(lines, "acct-")[a["hash"]]))
    assert (got["md5_ids"], got["headers_only"]) == (str(base + 2), "2")
    assert (scalar(lines, "headers_only"), scalar(lines, "skipped_large_noid")) == ("2", "1")


def test_index_status_counts_tb_error_rows_apart_from_md5_ids(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # plan 04 M5: a tb_error row may stand for a Message-ID item, so md5_ids leaves it out
    a, f = FOLDERS["acct2/Projects"]
    fid = f"{a['key']}:/{f['path']}"
    before = fields(" ".join(rows(status(capsys), "acct-")[a["hash"]]))["md5_ids"]
    assert scalar(status(capsys), "skipped_tb_error") == "0"
    write(
        built,
        "INSERT INTO skipped_items(folder_id, tb_date, ordinal, reason) "
        "VALUES (?, 2, 0, 'tb_error')",
        fid,
    )
    lines = status(capsys)
    assert fields(" ".join(rows(lines, "acct-")[a["hash"]]))["md5_ids"] == before
    assert (scalar(lines, "skipped_tb_error"), scalar(lines, "skipped_large_noid")) == ("1", "0")


def test_index_status_of_a_corrupt_index_names_rebuild_and_changes_nothing(
    tern_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = index_path(paths.resolve())
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_bytes(b"not a database: garbage bytes " * 8)
    assert cli.main(["index", "status"]) == 1
    err = capsys.readouterr().err
    assert "DatabaseError" in err and "tern index rebuild" in err
    assert path.read_bytes() == b"not a database: garbage bytes " * 8
    assert sorted(p.name for p in path.parent.iterdir()) == ["index.db"]


@pytest.mark.parametrize("where", ["open", "read"])
def test_index_status_on_an_operational_error_says_retry_not_rebuild(
    built: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    def locked(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    if where == "open":
        monkeypatch.setattr(cli.Store, "open", staticmethod(locked))
    else:
        monkeypatch.setattr(cli, "index_status_lines", locked)
    assert cli.main(["index", "status"]) == 1
    err = capsys.readouterr().err
    assert "OperationalError" in err and "retry" in err
    assert "rebuild" not in err


@pytest.mark.parametrize(
    # every sqlite3.DatabaseError subclass but OperationalError (InterfaceError is not one: it
    # was never caught as a DatabaseError, so it could not test this)
    "error",
    [
        sqlite3.ProgrammingError,
        sqlite3.IntegrityError,
        sqlite3.DataError,
        sqlite3.InternalError,
        sqlite3.NotSupportedError,
    ],
)
@pytest.mark.parametrize("where", ["open", "read"])
def test_index_status_raises_programming_errors_instead_of_advising_a_rebuild(
    built: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    where: str,
    error: type[sqlite3.Error],
) -> None:
    """A bug in tern's SQL must surface as itself, not as advice to throw the index away."""

    def buggy(*args: Any, **kwargs: Any) -> Any:
        raise error("test: a programming error")

    if where == "open":
        monkeypatch.setattr(cli.Store, "open", staticmethod(buggy))
    else:
        monkeypatch.setattr(cli, "index_status_lines", buggy)
    with pytest.raises(error, match="test: a programming error"):
        cli.main(["index", "status"])
    assert "rebuild" not in capsys.readouterr().err


def test_index_status_with_a_missing_table_names_rebuild(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A table missing at a valid schema version is damage, not a passing lock: no "retry"."""
    write(built, "DROP TABLE ingest_runs")
    assert cli.main(["index", "status"]) == 1
    err = capsys.readouterr().err
    assert "no such table" in err and "tern index rebuild" in err and "retry" not in err


def test_index_status_with_a_missing_column_names_rebuild_and_a_bug_if_it_persists(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "no such column" at a valid schema version is schema damage too; if tern's own SQL named a
    wrong column, a rebuild fails the same way, so the advice says that means a tern bug."""
    write(built, "ALTER TABLE ingest_runs DROP COLUMN x_mid_churn")
    assert cli.main(["index", "status"]) == 1
    err = capsys.readouterr().err
    assert "no such column: x_mid_churn" in err and "tern index rebuild" in err
    assert "retry" not in err and "tern bug" in err


def test_index_status_exits_cleanly_on_a_short_hash_collision(
    built: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def collide(names: object, kind: str) -> dict[str, str]:
        raise cli.ShortHashCollision(f"{kind} hashes collide at 6 hex chars (2 names)")

    monkeypatch.setattr(cli, "assign", collide)
    assert cli.main(["index", "status"]) == 1
    captured = capsys.readouterr()
    assert "hashes collide" in captured.err and captured.out == ""
    assert "Traceback" not in captured.err


def test_index_status_refuses_an_account_without_a_name(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        built,
        "UPDATE accounts SET name = NULL WHERE account_id = ?",
        MANIFEST["accounts"][0]["key"],
    )
    assert cli.main(["index", "status"]) == 1
    captured = capsys.readouterr()
    assert "without a name" in captured.err and captured.out == ""


def bound(path: Path) -> UUID | None:
    with Store.open(path, readonly=True) as store:
        return store.profile_id()


def test_index_rebuild_keeps_the_profile_and_the_old_index(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store.open(built) as store:
        store.bind_profile(PROFILE)
    assert cli.main(["index", "rebuild"]) == 0
    out = capsys.readouterr().out
    assert "kept the TB profile binding" in out
    (backup,) = built.parent.glob("index.db.bak-*[0-9]")
    assert bound(built) == PROFILE
    with Store.open(built, readonly=True) as store:
        st = store.status()
        assert (st.schema_version, st.messages, st.locations) == (SCHEMA_VERSION, 0, 0)
    with Store.open(backup, readonly=True) as old:
        assert old.status().messages == MANIFEST["distinct_messages"]
        assert old.profile_id() == PROFILE


def test_index_rebuild_rebind_leaves_it_unbound(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store.open(built) as store:
        store.bind_profile(PROFILE)
    assert cli.main(["index", "rebuild", "--rebind"]) == 0
    assert "--rebind" in capsys.readouterr().out
    assert bound(built) is None
    assert len(list(built.parent.glob("index.db.bak-*[0-9]"))) == 1


def test_index_rebuild_of_a_newer_schema_keeps_the_profile(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store.open(built) as store:
        store.bind_profile(PROFILE)
    write(built, "UPDATE meta SET value = ? WHERE key = 'schema_version'", str(SCHEMA_VERSION + 1))
    assert cli.main(["index", "rebuild"]) == 0
    assert bound(built) == PROFILE


def test_index_rebuild_moves_the_wal_beside_its_backup(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keep = Store.open(built, readonly=True)  # keeps -wal/-shm on disk while the rebuild runs
    try:
        with Store.open(built) as store:
            store.bind_profile(PROFILE)  # committed to the WAL, not checkpointed (reader open)
        assert built.with_name("index.db-wal").exists()
        assert cli.main(["index", "rebuild"]) == 0
    finally:
        keep.close()
    (backup,) = built.parent.glob("index.db.bak-*[0-9]")
    assert backup.with_name(backup.name + "-wal").exists()
    with Store.open(backup, readonly=True) as old:
        assert old.profile_id() == PROFILE  # the binding lived only in the moved WAL


def test_index_rebuild_refused_while_ternd_holds_the_lock(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    lock = uds.InstanceLock(tp.lock)
    assert lock.acquire()
    try:
        assert cli.main(["index", "rebuild"]) == 1
    finally:
        lock.release()
    assert "ternd is running" in capsys.readouterr().err
    assert sorted(p.name for p in built.parent.iterdir()) == ["index.db"]
    with Store.open(built, readonly=True) as store:
        assert store.status().messages == MANIFEST["distinct_messages"]


def test_index_rebuild_refuses_a_taken_backup_name(
    built: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.time, "time", lambda: 1_700_000_000.5)
    taken = built.with_name("index.db.bak-1700000000")
    taken.write_bytes(b"older backup")
    assert cli.main(["index", "rebuild"]) == 1
    assert "already taken" in capsys.readouterr().err
    assert taken.read_bytes() == b"older backup"
    with Store.open(built, readonly=True) as store:
        assert store.status().messages == MANIFEST["distinct_messages"]


def test_index_rebuild_refuses_an_unreadable_binding_until_rebind(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store.open(built) as store:
        store.bind_profile(PROFILE)
    write(built, "UPDATE meta SET value = 'not-a-uuid' WHERE key = 'tb_profile_id'")
    before = built.read_bytes()
    assert cli.main(["index", "rebuild"]) == 1
    assert "--rebind" in capsys.readouterr().err
    # Nothing moved: index.db is unchanged and there is no backup. (Reading the binding may
    # leave an empty -wal/-shm beside it: a read-only SQLite connection cannot remove them.)
    assert list(built.parent.glob("index.db.bak-*")) == []
    assert built.read_bytes() == before
    assert cli.main(["index", "rebuild", "--rebind"]) == 0
    assert bound(built) is None
    assert len(list(built.parent.glob("index.db.bak-*[0-9]"))) == 1


def test_index_rebuild_without_an_index_creates_an_unbound_one(
    tern_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = index_path(paths.resolve())
    assert cli.main(["index", "rebuild"]) == 0
    out = capsys.readouterr().out
    assert "there was no index" in out and "no binding to keep" in out
    assert path.is_file() and bound(path) is None
    assert list(path.parent.glob("index.db.bak-*")) == []


def test_index_rebuild_of_a_database_without_meta_is_unbound(
    tern_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = index_path(paths.resolve())
    path.parent.mkdir(mode=0o700, parents=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE other(x)")
    conn.commit()
    conn.close()
    assert cli.main(["index", "rebuild"]) == 0
    assert "no binding to keep" in capsys.readouterr().out
    assert bound(path) is None
    (backup,) = path.parent.glob("index.db.bak-*[0-9]")
    conn = sqlite3.connect(backup)
    assert conn.execute("SELECT name FROM sqlite_master").fetchall() == [("other",)]
    conn.close()


# ---- tern search / index status embed lines (plan 05 step 6) --------------------------------
# Expected mids and folder hashes come from MANIFEST.json, other expected values from the
# index's own tables read here; none is typed in from a run.

STUB = StubEmbedder(dim=64)
MID = {m["key"]: m["mid"] for m in MANIFEST["messages"]}
STUB_CONFIG = f'embed_model = "{STUB.model_id}"\n'
HIT_LINE = re.compile(
    r"(?P<rank>[0-9]+) (?P<mid>\S+) (?P<date>-|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z) "
    r"(?P<score>[0-9]\.[0-9]{6})(?P<conflicted> conflicted)? folders=(?P<folders>\S+)"
)


def embed_all(store: Store, emb: StubEmbedder = STUB) -> None:
    model = vec.register(store, emb.model_id, emb.dim)
    while batch := vec.pending(store.connection, 100):
        vec.insert(store, model, [(p.chunk_id, p.text, emb.vector(p.text)) for p in batch])


@pytest.fixture(scope="module")
def embedded_index(fixture_index: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fixture index with every chunk embedded by the stub (closed)."""
    path = tmp_path_factory.mktemp("embedded") / "index.db"
    shutil.copyfile(fixture_index, path)
    with Store.open(path) as store:
        embed_all(store)
    return path


@pytest.fixture
def embedded(embedded_index: Path) -> Path:
    """The embedded index at this test's TERN_HOME index path, with a config naming the stub."""
    path = index_path(paths.resolve())
    path.parent.mkdir(mode=0o700, parents=True)
    shutil.copyfile(embedded_index, path)
    write_config(STUB_CONFIG)
    return path


def write_config(text: str) -> None:
    cfg = paths.resolve().config_file
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")


class SpyEmbedder:
    """The stub, counting query embeddings."""

    def __init__(self, inner: StubEmbedder = STUB) -> None:
        self.inner = inner
        self.model_id = inner.model_id
        self.queries = 0

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self.inner.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        self.queries += 1
        return await self.inner.embed_query(text)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> SpyEmbedder:
    emb = SpyEmbedder()
    monkeypatch.setattr(cli, "_make_embedder", lambda cfg: emb)
    return emb


def search(capsys: pytest.CaptureFixture[str], *argv: str, code: int = 0) -> list[str]:
    assert cli.main(["search", *argv]) == code
    captured = capsys.readouterr()
    if code == 0:
        assert captured.err == ""
    return captured.out.splitlines() if code == 0 else captured.err.splitlines()


def hits(lines: list[str]) -> list[re.Match[str]]:
    """The hit lines, each checked to be followed by its from/subject/snippet lines."""
    out = []
    for i, ln in enumerate(lines[1:], start=1):
        if ln.startswith(" "):
            continue
        m = HIT_LINE.fullmatch(ln)
        assert m is not None, ln
        assert [x.split(":", 1)[0] for x in lines[i + 1 : i + 4]] == [
            "  from",
            "  subject",
            "  snippet",
        ]
        out.append(m)
    assert [int(m["rank"]) for m in out] == list(range(1, len(out) + 1))
    return out


def folder_hashes(key: str) -> str:
    return ",".join(sorted({FOLDERS[c["folder"]][1]["hash"] for c in MESSAGES[key]["copies"]}))


def test_search_hybrid_finds_the_acme_invoice_and_its_reminder(
    embedded: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = search(capsys, "Acme", "invoice", "March", "--limit", "3")
    assert lines[0] == f"mode: hybrid model={STUB.model_id} embed_pending=0"
    got = hits(lines)
    assert len(got) == 3 and spy.queries == 1
    by_mid = {m["mid"]: m for m in got}
    assert {MID["inv-0042"], MID["inv-0042-reminder"]} <= set(by_mid)
    for key in ("inv-0042", "inv-0042-reminder"):
        assert by_mid[MID[key]]["folders"] == folder_hashes(key)
        assert by_mid[MID[key]]["conflicted"] is None
    with Store.open(embedded, readonly=True) as store:
        date_utc, subject = store.connection.execute(
            "SELECT date_utc, subject FROM messages WHERE mid = ?", (MID["inv-0042"],)
        ).fetchone()
    m = by_mid[MID["inv-0042"]]
    assert m["date"] == datetime.fromtimestamp(date_utc, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    i = lines.index(m.group(0))
    assert lines[i + 2] == f"  subject: {subject}"


def test_search_fts_only_never_builds_an_embedder(
    built: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_embedder(cfg: object) -> object:
        raise AssertionError("--fts-only built an embedder")

    monkeypatch.setattr(cli, "_make_embedder", no_embedder)
    write_config('embed_model = ""\n')  # no embedding model needed in fts-only mode
    lines = search(capsys, "Acme invoice March", "--limit", "2", "--fts-only")
    assert lines[0] == "mode: fts-only"
    assert {m["mid"] for m in hits(lines)} == {MID["inv-0042"], MID["inv-0042-reminder"]}


def test_search_with_no_hit_exits_0(built: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert search(capsys, "qwzzyxplonk", "--fts-only") == ["mode: fts-only"]


def test_search_flags_a_conflicted_mid(built: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mid = MANIFEST["named"]["inv-0077"]["mid"]
    with Store.open(built, readonly=True) as store:
        (subject,) = store.connection.execute(
            "SELECT subject FROM messages WHERE mid = ?", (mid,)
        ).fetchone()
    got = {m["mid"]: m for m in hits(search(capsys, subject, "--fts-only", "--limit", "50"))}
    assert got[mid]["conflicted"] == " conflicted"
    assert sum(1 for m in got.values() if m["conflicted"]) == 1


def test_search_passes_every_filter_to_the_service(
    embedded: Path,
    spy: SpyEmbedder,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, SearchFilters, int]] = []
    real = SearchService.search

    async def record(
        self: SearchService, query: str, filters: SearchFilters, limit: int
    ) -> SearchResult:
        seen.append((query, filters, limit))
        return await real(self, query, filters, limit)

    monkeypatch.setattr(SearchService, "search", record)
    a, f = FOLDERS["acct1/Invoices"]
    search(
        capsys,
        "invoice",
        "--folder",
        f["hash"],
        "--from",
        "Acme",
        "--after",
        "2024-03-01",
        "--before",
        "2024-04-01",
        "--has-attachment",
        "--limit",
        "7",
    )
    assert seen == [
        (
            "invoice",
            SearchFilters(
                folder_id=f"{a['key']}:/{f['path']}",
                from_="Acme",
                after=datetime(2024, 3, 1, tzinfo=UTC),
                before=datetime(2024, 4, 1, tzinfo=UTC),
                has_attachment=True,
            ),
            7,
        )
    ]


def test_search_folder_filter_returns_only_that_folder(
    embedded: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    _, f = FOLDERS["acct1/Invoices"]
    got = hits(search(capsys, "invoice", "--folder", f["hash"], "--limit", "50"))
    want = {
        MID[key]
        for key, msg in MESSAGES.items()
        if any(c["folder"] == "acct1/Invoices" for c in msg["copies"])
    }
    assert got and {m["mid"] for m in got} <= want
    assert all(f["hash"] in m["folders"].split(",") for m in got)


def test_search_neutralises_control_characters(
    embedded: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    mid = "e" * 64
    with Store.open(embedded) as store:
        with store.transaction():
            store.upsert_message(
                MessageRecord(
                    mid=mid,
                    content_hash="3" * 64,
                    parse_status="ok",
                    subject="zorblat \x1b[2J\x1b[H cleared",
                    from_name="Mallory\x1b]0;title\x07",
                    from_addr="m\x9b@example.invalid",
                    body_text="zorblat quindle\x7f\x00",
                    date_utc=1_700_000_000,
                )
            )
            store.add_location(mid, "acct1:/INBOX", "3" * 64, LocationState(), 0, source="backfill")
        embed_all(store)
    lines = search(capsys, "zorblat", "--limit", "1")
    blob = "\n".join(lines)
    assert [m["mid"] for m in hits(lines)] == [mid]
    assert not any(0x00 <= ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0 for ch in blob if ch != "\n")
    assert "  subject: zorblat �[2J�[H cleared" in lines
    assert "  from: Mallory�]0;title� <m�@example.invalid>" in lines


#: The bidi controls (Cf) printable() must replace; other Cf characters (soft hyphen, ZWSP,
#: ZWJ, word joiner, BOM, ...) stay.
BIDI = {
    "\u061c",
    "\u200e",
    "\u200f",
    *map(chr, range(0x202A, 0x202F)),
    *map(chr, range(0x2066, 0x206A)),
}


def test_printable_replaces_cc_bidi_controls_and_line_separators() -> None:
    text = "".join(chr(c) for c in (*range(0x3000), 0xFEFF))
    out = cli.printable(text)
    for a, b in zip(text, out, strict=True):
        replaced = unicodedata.category(a) in ("Cc", "Zl", "Zp") or a in BIDI
        assert b == ("�" if replaced else a), hex(ord(a))
    assert {unicodedata.category(c) for c in BIDI} == {"Cf"}
    assert "\u200b\u200d\u2060\ufeff\u00ad" == cli.printable("\u200b\u200d\u2060\ufeff\u00ad")


def hit(mid: str = "a" * 64, folder_ids: tuple[str, ...] = ("acct1:/INBOX",)) -> Hit:
    return Hit(
        mid=mid,
        date=None,
        from_addr=None,
        from_name="\u202eevil",
        subject=None,
        snippet="s",
        score=0.5,
        folder_ids=folder_ids,
        conflicted=False,
    )


def test_search_lines_neutralises_a_bidi_override_in_a_field() -> None:
    lines = cli.search_lines(SearchResult((hit(),), "fts", None, 0), {"acct1:/INBOX": "fold-abcd"})
    assert lines[1:] == [
        f"1 {'a' * 64} - 0.500000 folders=fold-abcd",
        "  from: �evil",
        "  subject: -",
        "  snippet: s",
    ]


def test_search_lines_refuses_a_hit_in_a_folder_it_has_no_label_for() -> None:
    result = SearchResult((hit(folder_ids=("acct1:/INBOX", "acct1:/New")),), "fts", None, 0)
    with pytest.raises(StoreError, match="retry"):
        cli.search_lines(result, {"acct1:/INBOX": "fold-abcd"})
    with pytest.raises(StoreError, match="retry"):
        cli.search_lines(SearchResult((hit(),), "fts", None, 0), {})


def test_search_lines_refuses_a_hybrid_result_without_a_model_id() -> None:
    with pytest.raises(RuntimeError, match="without a model_id"):
        cli.search_lines(SearchResult((), "hybrid", None, 0), {})


def test_search_of_a_reversed_date_range_finds_nothing_and_exits_0(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ("invoice", "--fts-only", "--after", "2024-04-01", "--before", "2024-03-01")
    assert search(capsys, *argv) == ["mode: fts-only"]
    assert hits(search(capsys, "invoice", "--fts-only", "--limit", "1"))


def test_search_on_an_operational_error_during_the_search_says_retry_not_rebuild(
    embedded: Path,
    spy: SpyEmbedder,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def locked(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "_search_in", locked)
    (err,) = search(capsys, "invoice", code=1)
    assert err.startswith("search: cannot read the index (OperationalError") and "retry" in err
    assert "rebuild" not in err


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.ProgrammingError,
        sqlite3.IntegrityError,
        sqlite3.DataError,
        sqlite3.InternalError,
        sqlite3.NotSupportedError,
    ],
)
def test_search_raises_programming_errors_during_the_search(
    embedded: Path,
    spy: SpyEmbedder,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error: type[sqlite3.Error],
) -> None:
    def buggy(*args: Any, **kwargs: Any) -> Any:
        raise error("test: a programming error")

    monkeypatch.setattr(SearchService, "search", buggy)
    with pytest.raises(error, match="test: a programming error"):
        cli.main(["search", "invoice"])
    assert "rebuild" not in capsys.readouterr().err


def test_search_and_status_read_only_tern_home(
    embedded: Path,
    spy: SpyEmbedder,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traps = {
        name: tmp_path / "trap" / name
        for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR")
    }
    for name, trap in traps.items():
        monkeypatch.setenv(name, str(trap))

    def no_platformdirs(*args: object, **kwargs: object) -> str:
        raise AssertionError("platformdirs used although TERN_HOME is set")

    for fn in ("user_data_dir", "user_state_dir", "user_config_dir", "user_runtime_dir"):
        monkeypatch.setattr(platformdirs, fn, no_platformdirs)
    opened: list[str] = []
    real_connect = sqlite3.connect

    def spy_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    assert hits(search(capsys, "Acme invoice March"))
    assert hits(search(capsys, "Acme invoice March", "--fts-only"))
    assert any(ln.startswith("embed ") for ln in status(capsys))
    want = "file:" + str(embedded.absolute()) + "?mode=ro"
    assert opened and {unquote(o) for o in opened} == {want}
    assert not (tmp_path / "trap").exists()


def test_search_without_an_index_exits_2_and_creates_nothing(
    tern_home: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(STUB_CONFIG)
    for mode in ([], ["--fts-only"]):
        assert "no index" in search(capsys, "invoice", *mode, code=2)[0]
    assert sorted(p.name for p in tern_home.iterdir()) == ["config"]


@pytest.mark.parametrize("query", ["?!", "  ", "---"])
def test_search_of_an_empty_query_exits_2(
    embedded: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str], query: str
) -> None:
    for mode in ([], ["--fts-only"]):
        assert "no word character" in search(capsys, *mode, "--", query, code=2)[0]
    assert spy.queries == 0


def test_search_without_a_configured_model_exits_2_with_the_fts_hint(
    embedded: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config('embed_model = ""\n')
    (err,) = search(capsys, "invoice", code=2)
    assert "no embedding model configured" in err and "--fts-only" in err
    assert spy.queries == 0


def test_search_without_a_registered_model_exits_2_with_the_fts_hint(
    built: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(STUB_CONFIG)
    (err,) = search(capsys, "invoice", code=2)
    assert "no embedding model is registered" in err and "--fts-only" in err
    assert spy.queries == 0


def test_search_of_an_unknown_folder_hash_exits_2(
    embedded: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    (err,) = search(capsys, "invoice", "--folder", "fold-zzzz", code=2)
    assert "unknown folder short hash" in err


def test_search_of_a_blank_from_exits_2(
    embedded: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    (err,) = search(capsys, "invoice", "--from", "  ", code=2)
    assert err.startswith("search: --from:")


@pytest.mark.parametrize(
    "argv",
    [
        ["--limit", "0"],
        ["--limit", "51"],
        ["--limit", "ten"],
        ["--limit", "+5"],
        ["--after", "2024-13-01"],
        ["--after", "2024-3-1"],
        ["--before", "20240301"],
        ["--before", "2024-02-30"],
        ["--bogus"],
    ],
)
def test_search_bad_arguments_exit_2(
    embedded: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["search", "invoice", *argv])
    assert info.value.code == 2
    assert spy.queries == 0


def test_search_with_an_invalid_config_exits_2(
    embedded: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config("no_such_key = 1\n")
    (err, *_) = search(capsys, "invoice", code=2)
    assert err.startswith("search: invalid config")


@pytest.mark.parametrize("cmd", [["search", "invoice"], ["index", "status"]])
@pytest.mark.parametrize("broken", ["not-utf8", "directory"])
def test_an_unreadable_config_exits_2(
    embedded: Path,
    spy: SpyEmbedder,
    capsys: pytest.CaptureFixture[str],
    cmd: list[str],
    broken: str,
) -> None:
    cfg = paths.resolve().config_file
    cfg.unlink()
    if broken == "not-utf8":
        cfg.write_bytes(b"\xff\n")
    else:
        cfg.mkdir()
    assert cli.main(cmd) == 2
    err = capsys.readouterr().err
    assert err.startswith(("index status" if cmd[0] == "index" else "search") + ": invalid config")
    assert spy.queries == 0


def test_search_with_an_unreachable_embedder_exits_1(
    embedded: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        cli,
        "_make_embedder",
        lambda cfg: EmbeddingClient(cfg, transport=httpx.MockTransport(refuse)),
    )
    (err,) = search(capsys, "invoice", code=1)
    assert err.startswith("search: embedder unreachable")


def test_search_with_a_malformed_embedder_response_exits_1(
    embedded: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(
        cli,
        "_make_embedder",
        lambda cfg: EmbeddingClient(cfg, transport=httpx.MockTransport(garbage)),
    )
    (err,) = search(capsys, "invoice", code=1)
    assert err.startswith("search: embedder error")


def test_search_refuses_a_remote_embedder_url(
    embedded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real ``_make_embedder``: the egress guard refuses before any request."""
    write_config(STUB_CONFIG + 'embed_base_url = "http://embed.example.invalid/v1"\n')
    (err,) = search(capsys, "invoice", code=1)
    assert err.startswith("search: embedder URL refused")


def test_search_with_another_model_exits_1(
    embedded: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    other = SpyEmbedder(StubEmbedder(dim=64, seed=1))
    monkeypatch.setattr(cli, "_make_embedder", lambda cfg: other)
    (err,) = search(capsys, "invoice", code=1)
    assert err.startswith("search: ModelMismatch") and other.queries == 0


def test_search_of_a_corrupt_index_exits_1(
    tern_home: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(STUB_CONFIG)
    path = index_path(paths.resolve())
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_bytes(b"not a database: garbage bytes " * 8)
    (err,) = search(capsys, "invoice", code=1)
    assert "DatabaseError" in err and "tern index rebuild" in err


def test_search_on_a_newer_schema_exits_1(
    embedded: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        embedded, "UPDATE meta SET value = ? WHERE key = 'schema_version'", str(SCHEMA_VERSION + 1)
    )
    (err,) = search(capsys, "invoice", "--fts-only", code=1)
    assert "tern index rebuild" in err


def test_search_with_inconsistent_vector_tables_exits_1(
    embedded: Path, spy: SpyEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    write(embedded, "DROP TRIGGER chunks_vec_au")
    (err,) = search(capsys, "invoice", code=1)
    assert err.startswith("search: inconsistent index")


def embed_lines(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.startswith("embed ")]


def chunk_counts(path: Path, max_chars: int) -> tuple[int, int]:
    with Store.open(path, readonly=True) as store:
        total, long = store.connection.execute(
            "SELECT count(*), count(CASE WHEN length(text) > ? THEN 1 END) FROM chunks",
            (max_chars,),
        ).fetchone()
    return int(total), int(long)


def test_index_status_embed_lines_without_a_model(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    total, long = chunk_counts(built, TernConfig().embed_max_input_chars)
    assert embed_lines(status(capsys)) == [
        "embed model=none dim=- quant=-",
        f"embed done=0 pending={total} truncated={long}",
        "embed complete_at=-",
    ]


def test_index_status_embed_lines_of_an_embedded_index(
    embedded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(STUB_CONFIG + "embed_max_input_chars = 40\n")
    total, long = chunk_counts(embedded, 40)
    assert 0 < long < total
    with Store.open(embedded) as store:
        vec.set_complete(store, 1_700_000_000)
    lines = status(capsys)
    assert embed_lines(lines) == [
        f"embed model={STUB.model_id} dim={STUB.dim} quant={vec.QUANT}",
        f"embed done={total} pending=0 truncated={long}",
        "embed complete_at=" + datetime.fromtimestamp(1_700_000_000, UTC).isoformat()[:19] + "Z",
    ]
    assert lines[-1].startswith("jobs_failed ")


def test_index_status_embed_lines_show_no_names_or_sentinels(
    embedded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store.open(embedded) as store:
        vec.set_complete(store, 1_700_000_000)
    blob = "\n".join(embed_lines(status(capsys)))
    forbidden = [s["value"] for s in MANIFEST["sentinels"]]
    forbidden += [a["name"] for a in MANIFEST["accounts"]]
    forbidden += [f["path"] for _, f in FOLDERS.values()] + list(FOLDERS)
    forbidden += tfm.mail_strings(FIXTURE, MANIFEST)
    assert len(forbidden) > 50 and blob.count("\n") == 2
    assert sorted({f for f in forbidden if f in blob}) == []


def test_index_status_with_inconsistent_vector_tables_exits_1(
    embedded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(embedded, "DROP TRIGGER chunks_vec_ad")
    assert cli.main(["index", "status"]) == 1
    captured = capsys.readouterr()
    assert "inconsistent index" in captured.err and captured.out == ""


def test_index_status_with_an_invalid_config_exits_2(
    built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config("embed_max_input_chars = 0\n")
    assert cli.main(["index", "status"]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("index status: invalid config") and captured.out == ""


# ---- tern audit / tern undo (plan 06b step 10) -----------------------------------------------

AUDIT_MID = "a" * 64
AUDIT_T0 = 1_800_000_000.0


def audit_db() -> Path:
    return audit_path(paths.resolve())


def undo_payload(folder: str = "acct1:/INBOX") -> UndoPayload:
    prev = TargetState(folder_id=folder, tags=[], flagged=False, read=False)
    post = TargetState(folder_id=folder, tags=["$label4"], flagged=False, read=False)
    return UndoPayload(targets=[UndoTarget(mid=AUDIT_MID, prev=prev, post=post)])


def seed_write(log: AuditLog, *, caller: str = "agent:s1", **kw: Any) -> tuple[str, str]:
    """An executed W1 write with its undo token: (call_id, token)."""
    call_id = log.append_call(
        caller=caller,
        tool="tag",
        tier=Tier.W1,
        decision="auto",
        args={"mids": [AUDIT_MID]},
        args_validated=True,
        target_mids=[AUDIT_MID],
        target_count=1,
        initial_status="tb_accepted",
        **kw,
    )
    return call_id, log.add_undo_token(call_id, undo_payload())


def audit_out(capsys: pytest.CaptureFixture[str], *argv: str) -> list[str]:
    assert cli.main(["audit", *argv]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return captured.out.splitlines()


def test_audit_without_a_log_creates_nothing(
    tern_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert audit_out(capsys) == ["no audit log"]
    assert not audit_db().exists() and not audit_db().parent.exists()
    assert sorted(p.name for p in tern_home.iterdir()) == []


def test_audit_never_prints_args_mids_caller_labels_or_event_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with AuditLog.open(audit_db()) as log:
        call_id, _ = seed_write(log, caller="mcp:conn-1", caller_label="SENTINEL-label-7f3a")
        log.append_call(
            caller="agent:s1",
            tool="draft",
            tier=Tier.W2,
            decision="denied",
            reason="unavailable",
            args={"to": ["SENTINEL-addr-7f3a"], "body": "SENTINEL-body-7f3a"},
            args_validated=True,
            target_mids=[AUDIT_MID],
            target_count=1,
        )
        log.append_event(call_id, "error", {"code": "internal", "exc": "SENTINEL-detail-7f3a"})
    wal = audit_db().with_name("audit.db-wal")
    raw = audit_db().read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    for planted in (b"SENTINEL-label", b"SENTINEL-addr", b"SENTINEL-body", b"SENTINEL-detail"):
        assert planted in raw  # 06a Q1: validated args (and labels, details) are in audit.db
    blob = "\n".join(audit_out(capsys))
    assert "SENTINEL" not in blob and AUDIT_MID not in blob and "conn-1" in blob
    assert "tool=draft" in blob and "tool=tag" in blob


def test_audit_prints_each_call_oldest_first_with_its_codes_and_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = [AUDIT_T0]
    with AuditLog.open(audit_db(), clock=lambda: now[0]) as log:
        log.append_call(
            caller="ui",
            tool=INVALID_TOOL,
            tier=None,
            decision="error",
            reason="unknown_tool",
            args={"x": 1},
            args_validated=False,
        )
        now[0] += 60
        first, first_token = seed_write(log)
        now[0] += 60
        second, second_token = seed_write(log, caller="job:triage")
        log.append_event(second, "observed", {"locations": 1})
        log.append_event(first, "undone", {"undo_call_id": second})
    lines = audit_out(capsys, "--last", "2")
    assert lines == [
        "2027-01-15T08:01:00Z tool=tag tier=W1 caller=agent:s1 decision=auto reason=- "
        f"status=tb_accepted undone=yes targets=1 undo_token={first_token}",
        "2027-01-15T08:02:00Z tool=tag tier=W1 caller=job:triage decision=auto reason=- "
        f"status=observed undone=no targets=1 undo_token={second_token}",
        "stale writes (>24 h tb_accepted): 0",
    ]
    (oldest, *_) = audit_out(capsys)
    assert oldest == (
        "2027-01-15T08:00:00Z tool=<invalid> tier=- caller=ui decision=error "
        "reason=unknown_tool status=- undone=no targets=0 undo_token=-"
    )


def test_audit_counts_stale_writes_after_24_h(capsys: pytest.CaptureFixture[str]) -> None:
    now = [time.time() - 86_400 - 60]
    with AuditLog.open(audit_db(), clock=lambda: now[0]) as log:
        seed_write(log)  # 24 h + 1 min ago, still tb_accepted: stale
        observed, _ = seed_write(log)  # as old, but observed: not stale
        log.append_event(observed, "observed", {"locations": 1})
        now[0] = time.time()
        seed_write(log)  # recent: not stale yet
    assert audit_out(capsys)[-1] == "stale writes (>24 h tb_accepted): 1"


def test_audit_of_an_empty_log_says_no_calls(capsys: pytest.CaptureFixture[str]) -> None:
    AuditLog.open(audit_db()).close()
    assert audit_out(capsys) == ["no calls", "stale writes (>24 h tb_accepted): 0"]


def test_audit_reads_beside_a_writer_holding_its_write_lock(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ternd is the only writer: ``tern audit`` takes no write lock, so it neither waits on a
    write transaction nor fails with ``database is locked`` (``AuditLog.open`` would)."""
    with AuditLog.open(audit_db()) as log:
        seed_write(log)
    writer = sqlite3.connect(audit_db(), isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        t0 = time.perf_counter()
        lines = audit_out(capsys)
        assert time.perf_counter() - t0 < 2.0
        assert len(lines) == 2 and "tool=tag" in lines[0]
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_audit_of_an_unknown_schema_version_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    AuditLog.open(audit_db()).close()
    write(audit_db(), "PRAGMA user_version = 99")
    assert cli.main(["audit"]) == 1
    captured = capsys.readouterr()
    assert "schema version 99" in captured.err and captured.out == ""


def test_audit_of_a_file_that_is_not_a_database_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit_db().parent.mkdir(parents=True)
    audit_db().write_bytes(b"not a database, just text" * 100)
    assert cli.main(["audit"]) == 1
    captured = capsys.readouterr()
    assert "cannot read the audit log" in captured.err and captured.out == ""


def test_audit_side_files_of_a_wal_log_keep_the_logs_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On an existing WAL log the read-only reader may leave ``-wal``/``-shm`` behind (step 10
    review): they carry ``audit.db``'s 0600 mode and nothing else is created."""
    with AuditLog.open(audit_db()) as log:
        seed_write(log)
    assert audit_out(capsys)[-1] == "stale writes (>24 h tb_accepted): 0"
    names = sorted(p.name for p in audit_db().parent.iterdir())
    assert set(names) <= {"audit.db", "audit.db-wal", "audit.db-shm"}
    for name in names:
        assert stat.S_IMODE((audit_db().parent / name).stat().st_mode) == 0o600, name


def test_audit_keeps_the_real_error_when_sqlite_rolled_back_by_itself(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An IOERR/FULL/NOMEM-class failure ends the transaction inside SQLite; a second ROLLBACK
    would raise "no transaction is active" and replace the real reason."""
    AuditLog.open(audit_db()).close()

    def failing_read(audit: AuditLog, *, last: int, now: float) -> list[str]:
        audit._conn.execute("ROLLBACK")  # pyright: ignore[reportPrivateUsage] -- simulates SQLite's own rollback on IOERR
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cli, "audit_lines", failing_read)
    assert cli.main(["audit"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "audit: cannot read the audit log (OperationalError: disk I/O error)\n"


@pytest.mark.parametrize("last", ["0", "-1", "1001", "x"])
def test_audit_refuses_a_bad_last(last: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["audit", "--last", last])
    assert exc.value.code == 2
    assert not audit_db().exists()


def test_undo_without_daemon(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["undo", "0" * 32]) == 2
    assert "no ternd listening" in capsys.readouterr().err


async def undo_server(handler: Any) -> tuple[Any, BridgeServer]:
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    server = BridgeServer(
        process_info=FakeProcessInfo(), thunderbird_bin=TB_BIN, nmhost_path=NMHOST
    )
    server.handlers["audit.undo"] = handler
    return await uds.start(server, tp.socket), server


async def close_server(srv: Any, server: BridgeServer) -> None:
    srv.close()
    await server.close_all()
    await srv.wait_closed()


def run_undo_cli(handler: Any, token: str) -> tuple[int, list[tuple[str | None, str]]]:
    """``tern undo <token>`` against an in-process bridge server whose ``audit.undo`` is
    ``handler``; returns the exit code and the (role, token) pairs the handler saw."""
    seen: list[tuple[str | None, str]] = []

    async def spy(peer: Any, params: UndoParams) -> UndoResult:
        seen.append((peer.role, params.undo_token))
        return await handler(peer, params)

    async def run() -> int:
        srv, server = await undo_server(spy)
        try:
            return await asyncio.to_thread(cli.main, ["undo", token])
        finally:
            await close_server(srv, server)

    return asyncio.run(run()), seen


def test_undo_sends_audit_undo_as_role_cli(capsys: pytest.CaptureFixture[str]) -> None:
    async def undone(peer: Any, params: UndoParams) -> UndoResult:
        return UndoResult(undone=True)

    code, seen = run_undo_cli(undone, "ab" * 16)
    assert code == 0 and seen == [("cli", "ab" * 16)]
    assert capsys.readouterr().out.startswith("undone")


@pytest.mark.parametrize("name", ["state_changed", "invalid_params", "unavailable"])
def test_undo_prints_ternds_refusal_and_exits_1(
    name: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    async def refuse(peer: Any, params: UndoParams) -> UndoResult:
        raise BridgeError(name, "refused \x1b[2Jhere [code]")

    code, _ = run_undo_cli(refuse, "ab" * 16)
    captured = capsys.readouterr()
    assert code == 1 and captured.out == ""
    assert captured.err == f"undo: {name}: refused �[2Jhere [code]\n"


def test_undo_reports_nothing_undone_as_a_failure(capsys: pytest.CaptureFixture[str]) -> None:
    async def not_undone(peer: Any, params: UndoParams) -> UndoResult:
        return UndoResult(undone=False)

    code, _ = run_undo_cli(not_undone, "ab" * 16)
    assert code == 1 and "nothing undone" in capsys.readouterr().err


def test_undo_without_a_reply_in_time_says_the_outcome_is_unknown() -> None:
    async def run() -> None:
        hang = asyncio.Event()

        async def never(peer: Any, params: UndoParams) -> UndoResult:
            await hang.wait()
            return UndoResult(undone=True)

        srv, server = await undo_server(never)
        try:
            with pytest.raises(cli.UndoNoReply, match="no reply from ternd within 0.2 s"):
                await cli.undo_via_daemon(paths.resolve().socket, "ab" * 16, timeout=0.2)
        finally:
            hang.set()
            await close_server(srv, server)

    asyncio.run(run())


def test_tern_undo_without_a_reply_in_time_exits_1_with_the_audit_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "UNDO_REPLY_TIMEOUT_S", 0.2)

    async def late(peer: Any, params: UndoParams) -> UndoResult:
        await asyncio.sleep(1.0)  # past the CLI's 0.2 s; bounded so the server closes
        return UndoResult(undone=True)

    code, seen = run_undo_cli(late, "ab" * 16)
    captured = capsys.readouterr()
    assert code == 1 and captured.out == "" and seen == [("cli", "ab" * 16)]
    assert captured.err == (
        "undo: no reply from ternd within 0.2 s; it may still complete: check `tern audit`\n"
    )


class _UndoResultWithExtra(UndoResult):
    """Passes the server's isinstance check; its extra field is not in 1.0 ``UndoResult``."""

    extra: str = "SENTINEL-extra-7f3a"


def test_tern_undo_of_an_invalid_reply_exits_1_with_types_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def bad(peer: Any, params: UndoParams) -> UndoResult:
        return _UndoResultWithExtra(undone=True)

    code, _ = run_undo_cli(bad, "ab" * 16)
    captured = capsys.readouterr()
    assert code == 1 and captured.out == "" and "SENTINEL" not in captured.err
    assert captured.err == (
        "undo: invalid reply from ternd (extra: extra_forbidden); it may have completed: "
        "check `tern audit`\n"
    )
