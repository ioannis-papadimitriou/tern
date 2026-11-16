"""Audit DB (plan 06a step 3): append-only tables, abort triggers, derived status/undone, the API
plan 06b consumes unchanged, and no index content in ``audit.db``."""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import test_fixture_manifest as tfm
from pydantic import ValidationError

from tern import paths
from tern.config import TernConfig
from tern.index.ingest import Backfill
from tern.index.source import IngestSource
from tern.index.store import LocationState, MessageRecord, Store, index_path
from tern.perms import audit
from tern.perms.audit import (
    INVALID_TOOL,
    AuditError,
    AuditLog,
    AuditSchemaError,
    CallStatus,
    TargetState,
    UndoPayload,
    UnknownCall,
    UnknownUndoToken,
    args_sha256,
    audit_path,
)
from tern.search.service import SearchService
from tern.testing.fake_bridge import FakeBridge
from tern.tools.contract import Tier, ToolError
from tern.tools.registry import Registry, ToolRuntime, builtin_tools

M1 = "a" * 64
M2 = "x-" + "b" * 64
M3 = "c" * 64
TABLES = ("calls", "events", "undo_tokens")


class Clock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def log(clock: Clock) -> Iterator[AuditLog]:
    with AuditLog.open(audit_path(paths.resolve()), clock=clock) as opened:
        yield opened


def write_call(log: AuditLog, **kw: Any) -> str:
    """An executed W1 write over ``M1`` (overridable)."""
    base: dict[str, Any] = {
        "caller": "agent:s1",
        "tool": "set_tags",
        "tier": Tier.W1,
        "decision": "auto",
        "args": {"mids": [M1], "add": ["$label1"]},
        "args_validated": True,
        "target_mids": [M1],
        "target_count": 1,
        "initial_status": "tb_accepted",
    }
    base.update(kw)
    return log.append_call(**base)


def read_call(log: AuditLog, **kw: Any) -> str:
    base: dict[str, Any] = {
        "caller": "mcp:conn-1",
        "tool": "get_message",
        "tier": Tier.R0,
        "decision": "auto",
        "args": {"mid": M1, "max_chars": 4000},
        "args_validated": True,
        "target_mids": [M1],
        "target_count": 1,
        "initial_status": "ok",
    }
    base.update(kw)
    return log.append_call(**base)


def state(folder: str = "acct1:/INBOX", tags: list[str] | None = None) -> TargetState:
    return TargetState(folder_id=folder, tags=tags or [], flagged=False, read=True)


def payload(mid: str = M1) -> UndoPayload:
    return UndoPayload.model_validate(
        {"targets": [{"mid": mid, "prev": state().model_dump(), "post": state(tags=["$label1"])}]}
    )


def db_bytes(path: Path) -> bytes:
    """The main file plus the WAL (where fresh appends live until a checkpoint)."""
    out = path.read_bytes()
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        out += wal.read_bytes()
    return out


# ---- file, location, schema ---------------------------------------------------------------------


def test_opens_under_tern_home_state_with_private_modes(log: AuditLog, tern_home: Path) -> None:
    assert log.path == tern_home / "state" / "audit.db"
    assert stat.S_IMODE(os.stat(log.path.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(log.path).st_mode) == 0o600
    conn = sqlite3.connect(log.path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == audit.SCHEMA_VERSION == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert set(TABLES) <= names
    finally:
        conn.close()


def test_reopen_keeps_rows(log: AuditLog, clock: Clock) -> None:
    call_id = read_call(log)
    log.close()
    with AuditLog.open(log.path, clock=clock) as again:
        assert again.get(call_id).tool == "get_message"


def test_newer_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    AuditLog.open(path).close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 2")
    conn.close()
    with pytest.raises(AuditSchemaError, match="newer"):
        AuditLog.open(path)


def test_foreign_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE other(x)")
    conn.close()
    with pytest.raises(AuditSchemaError, match="no schema version"):
        AuditLog.open(path)


def test_a_file_that_is_not_sqlite_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    path.write_bytes(b"this is not an sqlite database file, just bytes" * 20)
    with pytest.raises(AuditSchemaError, match="not an audit log"):
        AuditLog.open(path)


def test_a_versioned_database_without_audit_tables_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    with pytest.raises(AuditSchemaError, match="missing"):
        AuditLog.open(path)


class _ConnProxy:
    """A real connection whose ``execute`` can be made to answer or fail one statement."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.journal_mode: str | None = None
        self.fail_commit = False
        self.closed = False

    def execute(self, sql: str, *params: Any) -> Any:
        if self.journal_mode is not None and sql.startswith("PRAGMA journal_mode"):
            return self.conn.execute("SELECT ?", (self.journal_mode,))
        if self.fail_commit and sql == "COMMIT":
            self.fail_commit = False
            raise sqlite3.OperationalError("disk I/O error")
        return self.conn.execute(sql, *params)

    @property
    def in_transaction(self) -> bool:
        return self.conn.in_transaction

    def close(self) -> None:
        self.closed = True
        self.conn.close()


def test_a_journal_mode_other_than_wal_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect
    proxies: list[_ConnProxy] = []

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        proxy = _ConnProxy(real_connect(*args, **kwargs))
        proxy.journal_mode = "delete"
        proxies.append(proxy)
        return cast(sqlite3.Connection, proxy)

    monkeypatch.setattr(audit.sqlite3, "connect", connect)
    with pytest.raises(AuditError, match="'wal'"):
        AuditLog.open(tmp_path / "audit.db")
    assert [p.closed for p in proxies] == [True]


def test_a_failed_commit_rolls_back_and_the_log_stays_usable(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "audit.db"
    AuditLog.open(path).close()
    proxy = _ConnProxy(sqlite3.connect(path, isolation_level=None))
    with AuditLog(cast(sqlite3.Connection, proxy), path, clock) as flaky:
        proxy.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
            read_call(flaky)
        assert not proxy.in_transaction
        call_id = read_call(flaky)
        assert [r.call_id for r in flaky.list()] == [call_id]


# ---- append-only --------------------------------------------------------------------------------

_SOURCE = Path(audit.__file__)
_MUTATING_SQL = re.compile(r"\b(UPDATE|DELETE)\b(?!\s+ON\b)|\bREPLACE\b|\bDROP\b|\bALTER\b", re.I)


def _non_docstring_strings(tree: ast.AST) -> list[str]:
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def test_audit_module_has_no_update_or_delete_sql() -> None:
    strings = _non_docstring_strings(ast.parse(_SOURCE.read_text(encoding="utf-8")))
    assert any("INSERT INTO calls" in s for s in strings)  # the scan sees the SQL at all
    # Only the triggers' "BEFORE UPDATE ON" / "BEFORE DELETE ON" (built from the "UPDATE" and
    # "DELETE" constants) may name those verbs.
    offending = [s for s in strings if _MUTATING_SQL.search(s) and s not in ("UPDATE", "DELETE")]
    assert offending == []


def test_the_scan_would_catch_mutating_sql() -> None:
    tree = ast.parse(
        '"""UPDATE calls SET x"""\nX = "UPDATE calls SET decision = 1"\nY = "DELETE FROM events"\n'
    )
    assert len([s for s in _non_docstring_strings(tree) if _MUTATING_SQL.search(s)]) == 2


def test_no_mutating_methods() -> None:
    public = [n for n in dir(AuditLog) if not n.startswith("_")]
    assert public, "AuditLog has public methods"
    assert not [
        n
        for n in public
        if re.search(r"update|delete|remove|clear|edit|replace|purge|drop|set_", n)
    ]


@pytest.mark.parametrize("table", TABLES)
def test_raw_update_and_delete_are_aborted(log: AuditLog, table: str) -> None:
    call_id = write_call(log)
    log.add_undo_token(call_id, payload())
    log.append_event(call_id, "observed")
    before = sqlite3.connect(log.path).execute(f"SELECT * FROM {table}").fetchall()
    raw = sqlite3.connect(log.path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(f"UPDATE {table} SET ts = 0")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(f"DELETE FROM {table}")
        assert raw.execute(f"SELECT * FROM {table}").fetchall() == before
    finally:
        raw.close()


def test_raw_insert_or_replace_cannot_overwrite(log: AuditLog) -> None:
    call_id = read_call(log)
    raw = sqlite3.connect(log.path, isolation_level=None)  # recursive_triggers off here
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(
                "INSERT OR REPLACE INTO calls(call_id, ts, caller, tool, decision, args_sha256, "
                "target_mids, target_count) VALUES (?, 0, 'ui', 'x', 'denied', ?, '[]', 0)",
                (call_id, "0" * 64),
            )
        event_id = log.append_event(call_id, "error", {"code": "internal"})
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(
                "INSERT OR REPLACE INTO events(event_id, call_id, ts, kind, detail_json) "
                "VALUES (?, ?, 0, 'observed', '{}')",
                (event_id, call_id),
            )
    finally:
        raw.close()
    assert log.get(call_id).decision == "auto"
    assert [e.kind for e in log.events(call_id)] == ["error"]


# ---- status derivation --------------------------------------------------------------------------


def test_write_without_events_is_tb_accepted(log: AuditLog) -> None:
    assert log.status(write_call(log)) == CallStatus("tb_accepted", undone=False)


def test_observed_event_sets_observed(log: AuditLog) -> None:
    call_id = write_call(log)
    log.append_event(call_id, "observed", {"targets": 1})
    assert log.status(call_id) == CallStatus("observed", undone=False)


def test_undone_after_observed_keeps_status_and_sets_undone(log: AuditLog) -> None:
    call_id = write_call(log)
    log.add_undo_token(call_id, payload())
    log.append_event(call_id, "observed")
    log.append_event(call_id, "undone", {"targets": 1})
    assert log.status(call_id) == CallStatus("observed", undone=True)
    rec = log.get(call_id)
    assert (rec.status, rec.undone, rec.initial_status) == ("observed", True, "tb_accepted")


def test_error_event_sets_error(log: AuditLog) -> None:
    call_id = write_call(log)
    log.append_event(call_id, "error", {"code": "tb_error"})
    assert log.status(call_id) == CallStatus("error", undone=False)


def test_latest_of_observed_and_error_wins(log: AuditLog) -> None:
    call_id = write_call(log)
    log.append_event(call_id, "error", {"code": "tb_error"})
    log.append_event(call_id, "observed")
    assert log.status(call_id).status == "observed"
    log.append_event(call_id, "targets_changed", {"targets": 1})  # not a status
    assert log.status(call_id).status == "observed"


def test_read_is_ok_and_unexecuted_has_no_status(log: AuditLog) -> None:
    assert log.status(read_call(log)) == CallStatus("ok", undone=False)
    denied = write_call(log, decision="denied", reason="tier_not_enabled", initial_status=None)
    assert log.status(denied) == CallStatus(None, undone=False)


def test_a_failed_read_is_error(log: AuditLog) -> None:
    call_id = read_call(log)
    log.append_event(call_id, "error", {"code": "internal", "exc": "RuntimeError"})
    assert log.status(call_id).status == "error"
    [event] = log.events(call_id)
    assert (event.kind, event.detail) == ("error", {"code": "internal", "exc": "RuntimeError"})


# ---- append_call --------------------------------------------------------------------------------


def test_append_call_round_trip(log: AuditLog, clock: Clock) -> None:
    clock.t = 1234.5
    args = {"mids": [M1, M2], "add": ["$label1"]}
    call_id = write_call(
        log,
        caller="mcp:conn-7",
        caller_label="Some Client 1.0",
        args=args,
        target_mids=[M1, M2],
        target_count=2,
        original_args_sha256="f" * 64,
    )
    assert re.fullmatch(r"[0-9a-f]{32}", call_id)
    rec = log.get(call_id)
    assert rec.ts == 1234.5
    assert (rec.caller, rec.caller_label, rec.tool, rec.tier) == (
        "mcp:conn-7",
        "Some Client 1.0",
        "set_tags",
        Tier.W1,
    )
    assert (rec.decision, rec.reason, rec.initial_status) == ("auto", None, "tb_accepted")
    assert rec.args == args
    assert rec.args_sha256 == args_sha256(args) == args_sha256(dict(reversed(args.items())))
    assert rec.original_args_sha256 == "f" * 64
    assert (rec.target_mids, rec.target_count) == ((M1, M2), 2)
    assert rec.undo_token is None


def test_unvalidated_args_keep_only_their_hash(log: AuditLog) -> None:
    raw = {"mid": "not-a-mid PLANTED-RAW-ARG", "max_chars": "lots"}
    call_id = read_call(
        log,
        args=raw,
        args_validated=False,
        decision="error",
        reason="invalid_params",
        target_mids=[],
        target_count=0,
        initial_status=None,
    )
    rec = log.get(call_id)
    assert rec.args is None
    assert rec.args_sha256 == args_sha256(raw)
    assert b"PLANTED-RAW-ARG" not in db_bytes(log.path)


def test_unknown_tool_is_recorded_with_no_tier(log: AuditLog) -> None:
    call_id = log.append_call(
        caller="agent:s1",
        tool=INVALID_TOOL,
        tier=None,
        decision="error",
        reason="unknown_tool",
        args={},
        args_validated=False,
    )
    rec = log.get(call_id)
    assert (rec.tool, rec.tier, rec.status, rec.target_mids) == (INVALID_TOOL, None, None, ())


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"caller": "agent"}, "caller"),
        ({"caller": "mcp:bad id"}, "caller"),
        ({"caller": "root:x"}, "caller"),
        ({"caller_label": "bad\nlabel"}, "caller_label"),
        ({"caller_label": "x" * 129}, "caller_label"),
        ({"tool": "Set Tags"}, "tool"),
        ({"tier": "W1"}, "tier"),
        ({"decision": "maybe"}, "decision"),
        ({"reason": "Not a code"}, "reason"),
        ({"target_mids": ["nope"]}, "target_mids"),
        ({"target_mids": M1}, "target_mids"),
        ({"target_count": -1}, "target_count"),
        ({"target_count": True}, "target_count"),
        ({"decision": "denied"}, "executes"),
        ({"initial_status": "observed"}, "initial_status"),
        ({"original_args_sha256": "abc"}, "original_args_sha256"),
        # ``$`` matches before a trailing newline; each value must match in full.
        ({"caller": "agent:s1\n"}, "caller"),
        ({"caller": "ui\n"}, "caller"),
        ({"tool": "set_tags\n"}, "tool"),
        ({"reason": "internal\n"}, "reason"),
        ({"target_mids": [M1 + "\n"]}, "target_mids"),
        ({"original_args_sha256": "f" * 64 + "\n"}, "original_args_sha256"),
        # the initial status must fit the tier
        ({"tier": None}, "tier"),
        ({"tier": Tier.R0}, "tb_accepted"),
        ({"tier": Tier.R1}, "tb_accepted"),
        ({"initial_status": "ok"}, "tb_accepted"),
        ({"args": {"x": float("nan")}}, "JSON"),
        ({"args": {"x": object()}}, "JSON"),
    ],
)
def test_append_call_rejects_invalid_values_and_writes_nothing(
    log: AuditLog, override: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        write_call(log, **override)
    assert log.count() == 0


def test_ok_on_a_write_tier_is_rejected(log: AuditLog) -> None:
    for tier in (Tier.W2, Tier.W3):
        with pytest.raises(ValueError, match="tb_accepted"):
            write_call(log, tier=tier, initial_status="ok")
    assert log.count() == 0


def test_lone_surrogate_args_are_recorded(log: AuditLog) -> None:
    """``json.loads('"\\ud800"')`` is a lone surrogate, and pydantic ``str`` accepts it."""
    raw = {"q": "PLANTED-SURROGATE \ud800"}
    unvalidated = read_call(
        log,
        tool="search_messages",
        args=raw,
        args_validated=False,
        decision="error",
        reason="invalid_params",
        target_mids=[],
        target_count=0,
        initial_status=None,
    )
    validated = read_call(log, tool="search_messages", args=raw, target_mids=[], target_count=0)
    assert log.count() == 2
    rec = log.get(unvalidated)
    assert rec.args is None
    assert rec.args_sha256 == args_sha256(raw)
    assert log.get(validated).args == raw
    assert log.get(validated).args_sha256 == args_sha256(raw)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), object()])
def test_raw_args_that_are_not_json_still_get_a_row(log: AuditLog, bad: object) -> None:
    call_id = read_call(
        log,
        args={"x": bad, "note": "PLANTED-NON-JSON"},
        args_validated=False,
        decision="error",
        reason="invalid_params",
        target_mids=[],
        target_count=0,
        initial_status=None,
    )
    rec = log.get(call_id)
    assert (rec.args, rec.args_sha256, rec.reason) == (None, None, "invalid_params")
    assert log.count() == 1
    assert b"PLANTED-NON-JSON" not in db_bytes(log.path)


@pytest.mark.parametrize("raw", [[("q", "PLANTED-PAIRS")], "PLANTED-STR", 5, None, ["a", "b"]])
def test_raw_args_that_are_not_an_object_get_a_row_without_a_hash(
    log: AuditLog, raw: object
) -> None:
    """``Registry.call`` passes what the caller sent; a list of pairs must not be hashed as the
    dict it would convert to (step 4)."""
    call_id = read_call(
        log,
        args=raw,
        args_validated=False,
        decision="error",
        reason="invalid_params",
        target_mids=[],
        target_count=0,
        initial_status=None,
    )
    rec = log.get(call_id)
    assert (rec.args, rec.args_sha256) == (None, None)
    assert b"PLANTED" not in db_bytes(log.path)


def test_validated_args_that_are_not_an_object_are_refused(log: AuditLog) -> None:
    with pytest.raises(ValueError, match="JSON object"):
        read_call(log, args=[("q", "x")])
    with pytest.raises(ValueError, match="JSON object"):
        args_sha256(cast("Any", [("q", "x")]))
    assert log.count() == 0


@pytest.mark.parametrize(
    ("label", "ok"),
    [("Claude Desktop", True), ("x" * 128, True), ("", False), ("x" * 129, False), ("a\nb", False)],
)
def test_valid_caller_label(label: str, ok: bool) -> None:
    assert audit.valid_caller_label(label) is ok
    assert audit.valid_caller_label(None) is False


# ---- events -------------------------------------------------------------------------------------


def test_event_on_unknown_call(log: AuditLog) -> None:
    with pytest.raises(UnknownCall):
        log.append_event("0" * 32, "error")
    with pytest.raises(UnknownCall):
        log.status("0" * 32)
    with pytest.raises(UnknownCall):
        log.get("0" * 32)
    with pytest.raises(UnknownCall):
        log.events("0" * 32)


@pytest.mark.parametrize(
    "detail",
    [
        {"subject": "Re: quarterly numbers"},  # free text (a space)
        {"Subject": "x"},  # key not a code
        {"hits": [1, 2]},  # not a scalar
        {"x": 1.5},
        {"x": "a" * 65},
        {"x": "code\n"},  # ``$`` would accept the trailing newline
        {"k\n": 1},
        {f"k{i}": i for i in range(33)},
    ],
)
def test_event_detail_holds_codes_and_counts_only(log: AuditLog, detail: dict[str, Any]) -> None:
    call_id = read_call(log)
    with pytest.raises(ValueError):
        log.append_event(call_id, "error", detail)
    assert log.events(call_id) == []


def test_event_kind_is_closed(log: AuditLog) -> None:
    with pytest.raises(ValueError, match="kind"):
        log.append_event(read_call(log), "result")  # pyright: ignore[reportArgumentType]  # invalid on purpose


def test_event_preconditions(log: AuditLog) -> None:
    read = read_call(log)
    with pytest.raises(AuditError, match="observed"):
        log.append_event(read, "observed")
    with pytest.raises(AuditError, match="undo token"):
        log.append_event(read, "undone")
    denied = write_call(log, decision="denied", reason="rate_limited", initial_status=None)
    with pytest.raises(AuditError, match="executed"):
        log.append_event(denied, "error")
    write = write_call(log)
    with pytest.raises(AuditError, match="undo token"):
        log.append_event(write, "undone")
    log.add_undo_token(write, payload())
    log.append_event(write, "undone")
    with pytest.raises(AuditError, match="already undone"):
        log.append_event(write, "undone")
    assert [e.kind for e in log.events(write)] == ["undone"]
    log.append_event(denied, "targets_changed", {"targets": 2})


# ---- undo tokens --------------------------------------------------------------------------------


def test_undo_token_round_trip_and_used_flag(log: AuditLog, clock: Clock) -> None:
    call_id = write_call(log)
    clock.t += 5
    token = log.add_undo_token(call_id, payload().model_dump())
    assert re.fullmatch(r"[0-9a-f]{32}", token)
    got = log.undo_token(token)
    assert (got.call_id, got.ts, got.used) == (call_id, clock.t, False)
    assert got.payload == payload()
    assert log.get(call_id).undo_token == token
    log.append_event(call_id, "undone")
    assert log.undo_token(token).used is True  # recorded as an event, the token stays


def test_target_state_tags_are_canonical() -> None:
    assert state(tags=["b", "a", "b"]).tags == ["a", "b"]
    assert state(tags=["b", "a"]) == state(tags=["a", "b"])


def test_undo_token_preconditions(log: AuditLog) -> None:
    with pytest.raises(AuditError, match="executed write"):
        log.add_undo_token(read_call(log), payload())
    with pytest.raises(UnknownCall):
        log.add_undo_token("0" * 32, payload())
    write = write_call(log)
    log.add_undo_token(write, payload())
    with pytest.raises(AuditError, match="already has"):
        log.add_undo_token(write, payload())
    with pytest.raises(UnknownUndoToken):
        log.undo_token("0" * 32)
    with pytest.raises(UnknownUndoToken):
        log.undo_token("'; --")


@pytest.mark.parametrize(
    "bad",
    [
        {"targets": []},
        {"targets": [{"mid": M1, "prev": None, "post": None}]},
        {"targets": [{"mid": "nope", "prev": None, "post": state().model_dump()}]},
        {"targets": [{"mid": M1, "prev": None, "post": {**state().model_dump(), "subject": "x"}}]},
        {"targets": [{"mid": M1, "prev": None, "post": {**state().model_dump(), "tags": [""]}}]},
        {"targets": [{"mid": M1, "prev": None, "post": state().model_dump()}], "body": "x"},
    ],
)
def test_undo_payload_is_state_only(log: AuditLog, bad: dict[str, Any]) -> None:
    call_id = write_call(log)
    with pytest.raises(ValidationError):
        log.add_undo_token(call_id, bad)
    assert log.get(call_id).undo_token is None


# ---- list / count / target_total ----------------------------------------------------------------


def test_list_filters_and_order(log: AuditLog, clock: Clock) -> None:
    clock.t = 100.0
    a = read_call(log, caller="agent:s1")
    clock.t = 200.0
    b = write_call(log, caller="agent:s1", target_mids=[M1, M3], target_count=2)
    clock.t = 300.0
    c = write_call(log, caller="mcp:c1", target_mids=[M3], target_count=1)
    log.append_event(c, "observed")
    clock.t = 400.0
    d = write_call(
        log, caller="mcp:c1", decision="denied", reason="rate_limited", initial_status=None
    )

    def ids(**kw: Any) -> list[str]:
        return [r.call_id for r in log.list(**kw)]

    assert ids() == [d, c, b, a]
    assert ids(limit=2) == [d, c]
    assert ids(limit=None) == [d, c, b, a]
    assert ids(since=200.0) == [d, c, b]
    assert ids(until=300.0) == [b, a]
    assert ids(caller="agent:s1") == [b, a]
    assert ids(status="tb_accepted") == [b]
    assert ids(status="observed") == [c]
    assert ids(status="ok") == [a]
    assert ids(tier=Tier.W1) == [d, c, b]
    assert ids(executed=True) == [c, b, a]
    assert ids(executed=False) == [d]
    assert ids(target_mid=M3) == [c, b]
    assert ids(tier=Tier.W1, executed=True, since=150.0, caller="agent:s1") == [b]
    assert log.count() == 4
    assert log.count(status="tb_accepted", until=250.0) == 1
    assert log.target_total(tier=Tier.W1, executed=True) == 3
    assert log.target_total(tier=Tier.W1, executed=True, caller="mcp:c1") == 1
    assert log.target_total(caller="ui") == 0
    with pytest.raises(ValueError):
        log.list(limit=0)
    with pytest.raises(ValueError):
        log.list(status="undone")  # pyright: ignore[reportArgumentType]  # invalid on purpose


# ---- sentinel: index content never reaches audit.db ---------------------------------------------

SENTINEL_SUBJECT = "SENTINEL-audit-subject-7f3e"
SENTINEL_ADDR = "sentinel-audit-7f3e@example.com"
SENTINEL_NAME = "SENTINEL-audit-name-7f3e"
SENTINEL_BODY = "SENTINEL-audit-body-7f3e"
SENTINEL_FOLDER_ID = "acct1:/INBOX"


def _sentinel_index(tern_home: Path) -> Path:
    path = index_path(paths.resolve())
    with Store.open(path) as store, store.transaction():
        store.upsert_message(
            MessageRecord(
                mid=M1,
                content_hash="3" * 64,
                parse_status="ok",
                subject=f"Re: {SENTINEL_SUBJECT}",
                from_addr=SENTINEL_ADDR,
                from_name=SENTINEL_NAME,
                to_addrs=(SENTINEL_ADDR,),
                body_text=f"hello {SENTINEL_BODY}",
                date_utc=1_700_000_000,
            )
        )
        store.add_location(
            M1, SENTINEL_FOLDER_ID, "3" * 64, LocationState(tags=("$label2",)), 0, source="backfill"
        )
    return path


def _sentinels(raw: bytes) -> list[str]:
    return [
        s
        for s in (SENTINEL_SUBJECT, SENTINEL_ADDR, SENTINEL_NAME, SENTINEL_BODY)
        if s.encode() in raw
    ]


def test_audited_calls_over_a_sentinel_message_leave_no_sentinel(
    log: AuditLog, tern_home: Path
) -> None:
    """What a read and a write call over the message record (step 4's Registry.call and 06b's
    engine write these rows): ids, codes, counts, the target's state. The end-to-end variant
    through Registry.call and the built-in tools is added with steps 4/5."""
    with Store.open(_sentinel_index(tern_home), readonly=True) as store:
        row = store.get_message(M1)
        assert row is not None and SENTINEL_SUBJECT in (row.subject or "")
        [loc] = store.locations(M1)

        read = read_call(log, args={"mid": row.mid, "max_chars": 4000})
        log.append_event(read, "error", {"code": "internal", "exc": "RuntimeError"})
        with pytest.raises(ValueError):  # the subject cannot ride along in an event
            log.append_event(read, "error", {"code": row.subject})

        prev = TargetState(
            folder_id=loc.folder_id,
            tags=list(loc.state.tags),
            flagged=loc.state.flagged,
            read=loc.state.read,
        )
        post = prev.model_copy(update={"tags": sorted([*prev.tags, "$label1"])})
        write = write_call(log, args={"mids": [row.mid], "add": ["$label1"]})
        log.add_undo_token(
            write,
            UndoPayload.model_validate({"targets": [{"mid": M1, "prev": prev, "post": post}]}),
        )
        log.append_event(write, "observed", {"targets": 1})
        log.list()

    raw = db_bytes(log.path)
    assert M1.encode() in raw  # the scan reads the rows (mid recorded)
    assert _sentinels(raw) == []
    log.close()
    assert _sentinels(db_bytes(log.path)) == []


def _builtin_registry(log: AuditLog, store: Store, bridge: FakeBridge | None = None) -> Registry:
    """The real built-ins over ``store`` (FTS-only search: no embedder needed)."""
    runtime = ToolRuntime(
        audit=log,
        index=store,
        search=SearchService(store, None, mode="fts"),
        bridge=bridge,
        config=TernConfig(),
        log=logging.getLogger("tern.tools.test_audit"),
        clock=lambda: 1_700_000_000.0,
    )
    return Registry(builtin_tools(), runtime=runtime)


@pytest.mark.asyncio
async def test_builtin_tool_calls_over_a_sentinel_message_leave_no_sentinel(
    log: AuditLog, tern_home: Path
) -> None:
    """End to end (plan 06a step 5): every built-in called through Registry.call over the
    sentinel record; the outputs carry the sentinels, audit.db does not."""
    with Store.open(_sentinel_index(tern_home), readonly=True) as store:
        reg = _builtin_registry(log, store)
        seen: list[str] = []
        for tool, args in [
            ("get_message", {"mid": M1}),
            ("get_thread", {"mid": M1}),
            ("search_messages", {"query": "hello"}),
            ("find_contacts", {"query": "audit"}),
            ("list_folders", {}),
            ("index_status", {}),
        ]:
            res = await reg.call("agent:s1", tool, args)
            seen.append(res.output.model_dump_json(by_alias=True))
        for tool, args in [
            ("open_in_thunderbird", {"mid": M1}),  # no Message-ID: refused, no extension needed
            ("get_message", {"mid": M3}),  # not indexed
        ]:
            with pytest.raises(ToolError):
                await reg.call("agent:s1", tool, args)
        assert sorted(_sentinels("".join(seen).encode())) == sorted(
            [SENTINEL_SUBJECT, SENTINEL_ADDR, SENTINEL_NAME, SENTINEL_BODY]
        )
        assert len(log.list(limit=None)) == 8

    raw = db_bytes(log.path)
    assert M1.encode() in raw
    assert _sentinels(raw) == []


@pytest.fixture(scope="module")
def fixture_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The synthetic mailbox ingested by the real Backfill (no vectors: FTS-only search)."""
    path = tmp_path_factory.mktemp("audit-index") / "index.db"

    async def build() -> None:
        with Store.open(path) as store:
            await Backfill(store, IngestSource(FakeBridge(tfm.FIXTURE)), TernConfig()).run()

    asyncio.run(build())
    return path


@pytest.mark.asyncio
async def test_builtin_reads_over_the_fixture_leave_no_mail_strings(
    log: AuditLog, fixture_index: Path
) -> None:
    """get_message / get_thread / search_messages over the fixture index: no Subject or address
    of the fixture (``test_fixture_manifest.mail_strings``) reaches audit.db."""
    man = tfm.load_manifest(tfm.FIXTURE)
    strings = tfm.mail_strings(tfm.FIXTURE, man)
    assert strings
    with Store.open(fixture_index, readonly=True) as store:
        reg = _builtin_registry(log, store)
        mids = [r[0] for r in store.connection.execute("SELECT mid FROM messages ORDER BY mid")]
        outputs: list[str] = []
        for mid in mids:
            for tool in ("get_message", "get_thread"):
                try:
                    res = await reg.call("mcp:audit-test", tool, {"mid": mid})
                except ToolError as exc:
                    assert exc.code == "mid_conflicted"
                    continue
                outputs.append(res.output.model_dump_json(by_alias=True))
        for query in ("invoice", "meeting", "Πέμπτη", "newsletter", "project"):
            res = await reg.call("mcp:audit-test", "search_messages", {"query": query})
            outputs.append(res.output.model_dump_json(by_alias=True))
    out_text = "".join(outputs)
    assert sum(s in out_text for s in strings) > len(strings) // 2  # the reads did return mail
    raw = db_bytes(log.path)
    assert [s for s in strings if s.encode() in raw] == []


def test_the_sentinel_scan_finds_a_planted_value(tmp_path: Path) -> None:
    """Control: the same scan sees a sentinel that does get stored (args are kept, #10)."""
    with AuditLog.open(tmp_path / "audit.db") as planted:
        planted.append_call(
            caller="agent:s1",
            tool="search_messages",
            tier=Tier.R0,
            decision="auto",
            args={"query": SENTINEL_SUBJECT},
            args_validated=True,
            initial_status="ok",
        )
        assert _sentinels(db_bytes(planted.path)) == [SENTINEL_SUBJECT]
