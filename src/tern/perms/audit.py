"""Append-only audit log (#10, architecture §6, plan 06a step 3).

``$XDG_STATE_HOME/tern/audit.db``, or ``$TERN_HOME/state/audit.db`` (:func:`audit_path`), in a
0700 directory. Three insert-only tables:

- ``calls``: one row per ``Registry.call``, written **before** the tool runs (write-ahead): tool,
  effective tier, caller, decision, reason code, validated args, args hash, target mids and
  count, and for an executed call its initial status (``tb_accepted`` for a write, ``ok`` for a
  read; NULL when nothing executed).
- ``events``: rows referencing a call: ``observed`` (reconcile saw the written state),
  ``error`` (the tool failed), ``undone`` (undo applied), ``targets_changed`` (execution
  re-check). Their ``detail`` holds codes and counts only.
- ``undo_tokens``: one per executed write; the payload is per-target prev/post state (folder
  id, tag keys, flags), never content. A used token stays; its use is an ``undone`` event.

A call's current ``status`` is its latest ``observed``/``error`` event, else its initial
status; ``undone`` is a separate flag (bridge 1.0 ``AuditEntry`` carries both). Both are derived
on read, so nothing ever rewrites a row: this module has no method that changes or removes one,
and ``BEFORE UPDATE`` / ``BEFORE DELETE`` triggers (plus a ``BEFORE INSERT`` trigger refusing a
duplicate key, which an ``OR REPLACE`` insert would otherwise turn into a removal) abort any
attempt through a raw connection.

No content is stored except the caller's validated args (#10): they may include mail-derived
text the model copied from earlier tool output (a ``search_messages`` ``from``, a
``find_contacts`` query; plan 06a Q1). Nothing read from the index or returned by a tool is
written by tern itself. Args are stored only once they validated (``args_validated``); otherwise
only their hash is kept.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self, cast, get_args

from pydantic import Field, field_validator, model_validator

from tern._model import BoundaryModel
from tern.bridge.protocol import Mid
from tern.paths import TernPaths
from tern.tools.contract import NAME_RE, Tier

AUDIT_NAME = "audit.db"
SCHEMA_VERSION = 1
DIR_MODE = 0o700
FILE_MODE = 0o600
BUSY_TIMEOUT_MS = 5000

#: ``tool`` as recorded when the caller's tool name is not a valid tool name (it is
#: attacker-reachable: a model chooses it).
INVALID_TOOL = "<invalid>"

Decision = Literal["auto", "approved", "denied", "timeout", "error"]
InitialStatus = Literal["tb_accepted", "ok"]
EventKind = Literal["observed", "error", "undone", "targets_changed"]
Status = Literal["tb_accepted", "ok", "observed", "error"]

#: Permission callers (CLAUDE.md conventions): ``ui`` or ``agent|mcp|job:<id>``. The patterns
#: here are anchored with ``\A``/``\Z`` (``$`` would accept a trailing newline) and used with
#: ``fullmatch``.
CALLER_RE = re.compile(r"\A(ui|(agent|mcp|job):[A-Za-z0-9._-]{1,64})\Z")
#: Reason codes and event-detail keys.
CODE_RE = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
#: String values in event details: a code, an exception type name, a hash. No spaces, so no
#: sentence of mail text fits.
_DETAIL_STR_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")
_DETAIL_MAX_KEYS = 32
_CALLER_LABEL_MAX = 128
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_MID_RE = re.compile(r"\A(x-)?[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"\A[0-9a-f]{32}\Z")

DetailValue = int | bool | str | None


class AuditError(RuntimeError):
    """The audit log cannot record or answer as asked."""


class AuditSchemaError(AuditError):
    """``audit.db`` was written by a newer tern, or is not an audit log."""


class UnknownCall(AuditError):
    """No call row has this ``call_id``."""


class UnknownUndoToken(AuditError):
    """No undo token has this value."""


# ---- undo-token payload -----------------------------------------------------------------------


class TargetState(BoundaryModel):
    """One target's state in one folder: what undo restores or compares. Never content."""

    folder_id: str = Field(min_length=1, max_length=1024)
    #: TB tag keys (not tag names); stored sorted and de-duplicated, so states compare equal.
    tags: list[str] = Field(default_factory=list, max_length=256)
    flagged: bool
    read: bool

    @field_validator("tags")
    @classmethod
    def _tags_canonical(cls, v: list[str]) -> list[str]:
        if any(not t or len(t) > 128 for t in v):
            raise ValueError("tag keys must be 1..128 characters")
        return sorted(set(v))


class UndoTarget(BoundaryModel):
    """``prev`` is None for a target the write created (a new draft), ``post`` None for one it
    removed from TB's view; never both."""

    mid: Mid
    prev: TargetState | None
    post: TargetState | None

    @model_validator(mode="after")
    def _some_state(self) -> Self:
        if self.prev is None and self.post is None:
            raise ValueError("an undo target needs prev or post state")
        return self


class UndoPayload(BoundaryModel):
    targets: list[UndoTarget] = Field(min_length=1)


# ---- read results -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CallStatus:
    #: None for a call that never executed (denied, timed out, invalid, unknown tool).
    status: Status | None
    undone: bool


@dataclass(frozen=True)
class AuditRecord:
    """A call row plus its derived ``status``/``undone`` and its undo token, if any."""

    call_id: str
    ts: float
    caller: str
    caller_label: str | None
    tool: str
    tier: Tier | None
    decision: Decision
    reason: str | None
    #: The validated args (None when they did not validate).
    args: dict[str, Any] | None
    #: None when the raw args were not JSON (then ``args`` is None too).
    args_sha256: str | None
    original_args_sha256: str | None
    target_mids: tuple[str, ...]
    target_count: int
    initial_status: InitialStatus | None
    status: Status | None
    undone: bool
    undo_token: str | None


@dataclass(frozen=True)
class AuditEvent:
    event_id: int
    call_id: str
    ts: float
    kind: EventKind
    detail: dict[str, DetailValue]


@dataclass(frozen=True)
class UndoToken:
    token: str
    call_id: str
    ts: float
    payload: UndoPayload
    #: An ``undone`` event exists for the call.
    used: bool


# ---- helpers ----------------------------------------------------------------------------------


def audit_path(tp: TernPaths) -> Path:
    return tp.state / AUDIT_NAME


def canonical_json(value: object) -> str:
    """Sorted keys, no whitespace, non-ASCII escaped (``\\uXXXX``), no NaN/Infinity: the args
    hash input and the stored ``args_json`` (plan 06b's approval binding hashes the same form).
    ASCII escaping makes the text encodable for every Python ``str``, including a lone surrogate
    (valid JSON ``"\\ud800"`` decodes to one, and pydantic ``str`` fields accept it)."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _canonical_args(args: object) -> str:
    if not isinstance(args, Mapping):
        # A list, a string or a number is not an args object: no hash is made up for it.
        raise ValueError(f"args are not a JSON object ({type(args).__name__})")
    try:
        return canonical_json(dict(cast("Mapping[str, Any]", args)))
    except (TypeError, ValueError, RecursionError) as exc:
        # The message names the type only; the value may be mail text.
        raise ValueError(f"args are not JSON-serialisable ({type(exc).__name__})") from None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def args_sha256(args: Mapping[str, Any]) -> str:
    """The sha256 of :func:`canonical_json` of ``args``; ValueError (naming the exception type
    only) when they are not a JSON object (not a mapping, NaN, a non-JSON type)."""
    return _sha256_text(_canonical_args(args))


def valid_caller_label(label: object) -> bool:
    """``caller_label`` (an MCP client's display name) is 1..128 printable characters."""
    return isinstance(label, str) and 0 < len(label) <= _CALLER_LABEL_MAX and label.isprintable()


def _check_detail(detail: Mapping[str, object]) -> dict[str, DetailValue]:
    if len(detail) > _DETAIL_MAX_KEYS:
        raise ValueError(f"event detail has more than {_DETAIL_MAX_KEYS} keys")
    out: dict[str, DetailValue] = {}
    for key, value in detail.items():
        if not isinstance(key, str) or not CODE_RE.fullmatch(key):
            raise ValueError("event detail keys must be snake_case codes")
        if isinstance(value, str):
            if not _DETAIL_STR_RE.fullmatch(value):
                raise ValueError(f"event detail {key!r}: only codes, counts and flags are recorded")
        elif value is not None and not isinstance(value, (int, bool)):
            raise ValueError(f"event detail {key!r}: only codes, counts and flags are recorded")
        out[key] = value
    return out


# ---- schema -----------------------------------------------------------------------------------

_APPEND_ONLY = "audit log is append-only"

_DDL: tuple[str, ...] = (
    """CREATE TABLE calls(
        seq INTEGER PRIMARY KEY,
        call_id TEXT NOT NULL UNIQUE,
        ts REAL NOT NULL,
        caller TEXT NOT NULL,
        caller_label TEXT,
        tool TEXT NOT NULL,
        tier TEXT CHECK (tier IS NULL OR tier IN ('R0', 'R1', 'W1', 'W2', 'W3')),
        decision TEXT NOT NULL
            CHECK (decision IN ('auto', 'approved', 'denied', 'timeout', 'error')),
        reason TEXT,
        args_json TEXT,
        args_sha256 TEXT,
        original_args_sha256 TEXT,
        target_mids TEXT NOT NULL,
        target_count INTEGER NOT NULL CHECK (target_count >= 0),
        initial_status TEXT
            CHECK (initial_status IS NULL OR initial_status IN ('tb_accepted', 'ok')),
        CHECK (initial_status IS NULL OR decision IN ('auto', 'approved')),
        CHECK (args_json IS NULL OR args_sha256 IS NOT NULL)
    ) STRICT""",
    "CREATE INDEX calls_ts ON calls(ts)",
    "CREATE INDEX calls_caller_ts ON calls(caller, ts)",
    """CREATE TABLE events(
        event_id INTEGER PRIMARY KEY,
        call_id TEXT NOT NULL REFERENCES calls(call_id),
        ts REAL NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('observed', 'error', 'undone', 'targets_changed')),
        detail_json TEXT NOT NULL
    ) STRICT""",
    "CREATE INDEX events_call ON events(call_id, event_id)",
    """CREATE TABLE undo_tokens(
        token TEXT PRIMARY KEY,
        call_id TEXT NOT NULL UNIQUE REFERENCES calls(call_id),
        ts REAL NOT NULL,
        payload_json TEXT NOT NULL
    ) STRICT""",
    *(
        f"CREATE TRIGGER {table}_no_{op.lower()} BEFORE {op} ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{_APPEND_ONLY}'); END"
        for table in ("calls", "events", "undo_tokens")
        for op in ("UPDATE", "DELETE")
    ),
    # A duplicate key would let an ``OR REPLACE`` insert remove the old row without firing the
    # BEFORE DELETE trigger (it fires only with recursive_triggers on, a per-connection setting).
    f"""CREATE TRIGGER calls_insert_only BEFORE INSERT ON calls
        WHEN EXISTS (SELECT 1 FROM calls WHERE call_id = NEW.call_id OR seq = NEW.seq)
        BEGIN SELECT RAISE(ABORT, '{_APPEND_ONLY}'); END""",
    f"""CREATE TRIGGER events_insert_only BEFORE INSERT ON events
        WHEN EXISTS (SELECT 1 FROM events WHERE event_id = NEW.event_id)
        BEGIN SELECT RAISE(ABORT, '{_APPEND_ONLY}'); END""",
    f"""CREATE TRIGGER undo_tokens_insert_only BEFORE INSERT ON undo_tokens
        WHEN EXISTS (SELECT 1 FROM undo_tokens WHERE token = NEW.token OR call_id = NEW.call_id)
        BEGIN SELECT RAISE(ABORT, '{_APPEND_ONLY}'); END""",
)

_TABLES = frozenset({"calls", "events", "undo_tokens"})

_STATUS_SQL = (
    "COALESCE((SELECT e.kind FROM events e WHERE e.call_id = c.call_id "
    "AND e.kind IN ('observed', 'error') ORDER BY e.event_id DESC LIMIT 1), c.initial_status)"
)
_UNDONE_SQL = "EXISTS (SELECT 1 FROM events e WHERE e.call_id = c.call_id AND e.kind = 'undone')"
_RECORD_SQL = (
    "SELECT c.call_id, c.ts, c.caller, c.caller_label, c.tool, c.tier, c.decision, c.reason, "
    "c.args_json, c.args_sha256, c.original_args_sha256, c.target_mids, c.target_count, "
    f"c.initial_status, {_STATUS_SQL} AS status, {_UNDONE_SQL} AS undone, u.token "
    "FROM calls c LEFT JOIN undo_tokens u ON u.call_id = c.call_id"
)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path.parent, DIR_MODE)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        os.chmod(path, FILE_MODE)
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode != "wal":
            raise AuditError(f"journal_mode is {mode!r}, not 'wal'")
        conn.execute("PRAGMA foreign_keys = ON")
        # Belt and braces next to the insert-only triggers: REPLACE conflict resolution fires
        # the BEFORE DELETE triggers on this connection.
        conn.execute("PRAGMA recursive_triggers = ON")
        _ensure_schema(conn)
    except sqlite3.DatabaseError as exc:
        conn.close()
        if exc.sqlite_errorcode == sqlite3.SQLITE_NOTADB:
            raise AuditSchemaError("audit.db is not an audit log") from None
        raise
    except BaseException:  # close, then re-raise: never swallowed
        conn.close()
        raise
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise AuditSchemaError(
                f"audit.db schema version {version} is newer than this tern supports "
                f"({SCHEMA_VERSION}); upgrade tern"
            )
        if version == 0:
            existing = conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0]
            if existing:
                raise AuditSchemaError("audit.db has tables but no schema version")
            for stmt in _DDL:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        else:
            names = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
            }
            if not _TABLES <= names:
                raise AuditSchemaError("audit.db is missing audit tables")
        conn.execute("COMMIT")
    except BaseException:  # roll back, then re-raise: never swallowed
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ---- the log ----------------------------------------------------------------------------------


class AuditLog:
    """The audit log. Create with :meth:`open`; one per thread/task (sqlite3 connection)."""

    def __init__(self, conn: sqlite3.Connection, path: Path, clock: Callable[[], float]) -> None:
        self._conn = conn
        self.path = path
        self._clock = clock

    @classmethod
    def open(cls, path: Path, *, clock: Callable[[], float] = time.time) -> AuditLog:
        """Open (creating if needed) the log at ``path``; ``clock`` gives epoch seconds."""
        return cls(_connect(path), path, clock)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            # Inside the try: a failed COMMIT (busy, I/O) must not leave the transaction open,
            # or every later BEGIN fails.
            self._conn.execute("COMMIT")
        except BaseException:  # roll back, then re-raise: never swallowed
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _call_row(self, call_id: str) -> tuple[str | None]:
        row = self._conn.execute(
            "SELECT initial_status FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        if row is None:
            raise UnknownCall(f"no audit call {call_id!r}")
        return row

    # -- appends ----------------------------------------------------------------------------

    def append_call(
        self,
        *,
        caller: str,
        tool: str,
        tier: Tier | None,
        decision: Decision,
        args: object,
        args_validated: bool,
        reason: str | None = None,
        target_mids: Sequence[str] = (),
        target_count: int = 0,
        initial_status: InitialStatus | None = None,
        caller_label: str | None = None,
        original_args_sha256: str | None = None,
    ) -> str:
        """Append one call row and return its ``call_id``.

        ``args`` are hashed (canonical JSON) and stored only when ``args_validated``. Raw
        (unvalidated) args that are not a JSON object (not a mapping, NaN, a non-JSON type;
        ``Registry.call`` passes what the caller sent) are recorded with
        ``args_sha256`` NULL, so an invalid call still gets its row (write-ahead); validated
        args that are not JSON are a ValueError (the caller must dump them JSON-safe).
        ``tool`` is a tool name or :data:`INVALID_TOOL`; ``tier`` is the effective tier (None
        for an unknown tool); ``reason`` an error/denial code. ``initial_status`` is set only
        for a call that executes (``tb_accepted`` for a W1-W3 tool, ``ok`` for R0/R1).
        ``original_args_sha256``: the args hash before a W2 subset narrowed them (06b).
        Invalid values raise ValueError (a tern bug) and write nothing.
        """
        if not CALLER_RE.fullmatch(caller):
            raise ValueError("invalid caller id")
        if caller_label is not None and not valid_caller_label(caller_label):
            raise ValueError(f"caller_label must be 1..{_CALLER_LABEL_MAX} printable characters")
        if tool != INVALID_TOOL and not NAME_RE.fullmatch(tool):
            raise ValueError(f"tool must be a tool name or {INVALID_TOOL!r}")
        if tier is not None and not isinstance(tier, Tier):
            raise ValueError("tier must be a Tier or None")
        if decision not in get_args(Decision):
            raise ValueError("invalid decision")
        if reason is not None and not CODE_RE.fullmatch(reason):
            raise ValueError("reason must be a snake_case code")
        mids = list(target_mids)
        if isinstance(target_mids, str) or not all(
            isinstance(m, str) and _MID_RE.fullmatch(m) for m in mids
        ):
            raise ValueError("target_mids must be mids")
        if isinstance(target_count, bool) or not isinstance(target_count, int) or target_count < 0:
            raise ValueError("target_count must be an int >= 0")
        if initial_status is not None:
            if initial_status not in get_args(InitialStatus):
                raise ValueError("invalid initial_status")
            if decision not in ("auto", "approved"):
                raise ValueError("only an auto or approved call executes")
            if tier is None:
                raise ValueError("only a resolved tool (a tier) executes")
            if (initial_status == "tb_accepted") != tier.value.startswith("W"):
                raise ValueError("initial_status tb_accepted is for writes, ok for reads")
        if original_args_sha256 is not None and not _SHA256_RE.fullmatch(original_args_sha256):
            raise ValueError("original_args_sha256 must be a sha256 hex digest")
        try:
            text: str | None = _canonical_args(args)
        except ValueError:
            if args_validated:
                raise
            text = None
        digest = None if text is None else _sha256_text(text)
        args_json = text if args_validated else None
        call_id = uuid.uuid4().hex
        with self._write() as conn:
            conn.execute(
                "INSERT INTO calls(call_id, ts, caller, caller_label, tool, tier, decision, "
                "reason, args_json, args_sha256, original_args_sha256, target_mids, "
                "target_count, initial_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    call_id,
                    self._clock(),
                    caller,
                    caller_label,
                    tool,
                    None if tier is None else tier.value,
                    decision,
                    reason,
                    args_json,
                    digest,
                    original_args_sha256,
                    json.dumps(mids),
                    target_count,
                    initial_status,
                ),
            )
        return call_id

    def append_event(
        self, call_id: str, kind: EventKind, detail: Mapping[str, object] | None = None
    ) -> int:
        """Append an event to ``call_id`` and return its ``event_id``.

        ``detail`` holds codes, counts and flags only (ValueError otherwise). ``observed`` needs
        an executed write (initial status ``tb_accepted``); ``error`` an executed call;
        ``undone`` a call with an undo token that is not undone yet (AuditError otherwise).
        """
        if kind not in get_args(EventKind):
            raise ValueError("invalid event kind")
        checked = _check_detail(detail or {})
        with self._write() as conn:
            (initial,) = self._call_row(call_id)
            if kind == "observed" and initial != "tb_accepted":
                raise AuditError("only an executed write can be observed")
            if kind == "error" and initial is None:
                raise AuditError("only an executed call can fail")
            if kind == "undone":
                if (
                    conn.execute(
                        "SELECT 1 FROM undo_tokens WHERE call_id = ?", (call_id,)
                    ).fetchone()
                    is None
                ):
                    raise AuditError("the call has no undo token")
                if (
                    conn.execute(
                        "SELECT 1 FROM events WHERE call_id = ? AND kind = 'undone'", (call_id,)
                    ).fetchone()
                    is not None
                ):
                    raise AuditError("the call is already undone")
            cur = conn.execute(
                "INSERT INTO events(call_id, ts, kind, detail_json) VALUES (?, ?, ?, ?)",
                (call_id, self._clock(), kind, canonical_json(checked)),
            )
            event_id = cur.lastrowid
        assert event_id is not None
        return event_id

    def add_undo_token(self, call_id: str, payload: UndoPayload | Mapping[str, Any]) -> str:
        """Record the undo state of an executed write and return its new token (one per
        call; AuditError for a second one or a call that is not an executed write)."""
        checked = (
            payload if isinstance(payload, UndoPayload) else UndoPayload.model_validate(payload)
        )
        token = secrets.token_hex(16)
        with self._write() as conn:
            (initial,) = self._call_row(call_id)
            if initial != "tb_accepted":
                raise AuditError("only an executed write gets an undo token")
            if (
                conn.execute("SELECT 1 FROM undo_tokens WHERE call_id = ?", (call_id,)).fetchone()
                is not None
            ):
                raise AuditError("the call already has an undo token")
            conn.execute(
                "INSERT INTO undo_tokens(token, call_id, ts, payload_json) VALUES (?, ?, ?, ?)",
                (token, call_id, self._clock(), checked.model_dump_json()),
            )
        return token

    # -- reads ------------------------------------------------------------------------------

    def undo_token(self, token: str) -> UndoToken:
        if not _TOKEN_RE.fullmatch(token):
            raise UnknownUndoToken("no such undo token")
        row = self._conn.execute(
            "SELECT u.token, u.call_id, u.ts, u.payload_json, EXISTS (SELECT 1 FROM events e "
            "WHERE e.call_id = u.call_id AND e.kind = 'undone') FROM undo_tokens u "
            "WHERE u.token = ?",
            (token,),
        ).fetchone()
        if row is None:
            raise UnknownUndoToken("no such undo token")
        return UndoToken(
            token=row[0],
            call_id=row[1],
            ts=row[2],
            payload=UndoPayload.model_validate_json(row[3]),
            used=bool(row[4]),
        )

    def status(self, call_id: str) -> CallStatus:
        row = self._conn.execute(
            f"SELECT {_STATUS_SQL}, {_UNDONE_SQL} FROM calls c WHERE c.call_id = ?", (call_id,)
        ).fetchone()
        if row is None:
            raise UnknownCall(f"no audit call {call_id!r}")
        return CallStatus(status=row[0], undone=bool(row[1]))

    def get(self, call_id: str) -> AuditRecord:
        row = self._conn.execute(f"{_RECORD_SQL} WHERE c.call_id = ?", (call_id,)).fetchone()
        if row is None:
            raise UnknownCall(f"no audit call {call_id!r}")
        return _record(row)

    def events(self, call_id: str) -> list[AuditEvent]:
        self._call_row(call_id)
        rows = self._conn.execute(
            "SELECT event_id, call_id, ts, kind, detail_json FROM events WHERE call_id = ? "
            "ORDER BY event_id",
            (call_id,),
        ).fetchall()
        return [AuditEvent(r[0], r[1], r[2], r[3], json.loads(r[4])) for r in rows]

    def list(
        self,
        since: float | None = None,
        limit: int | None = 50,
        caller: str | None = None,
        *,
        until: float | None = None,
        status: Status | None = None,
        tier: Tier | None = None,
        executed: bool | None = None,
        target_mid: str | None = None,
    ) -> list[AuditRecord]:
        """Calls newest first. ``since`` (inclusive) / ``until`` (exclusive) are epoch seconds;
        ``status`` filters on the derived status; ``executed`` on whether the call ran;
        ``limit=None`` returns every match."""
        if limit is not None and (isinstance(limit, bool) or limit < 1):
            raise ValueError("limit must be >= 1 or None")
        where, params = _filters(since, until, caller, status, tier, executed, target_mid)
        sql = f"{_RECORD_SQL}{where} ORDER BY c.seq DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_record(r) for r in self._conn.execute(sql, params)]

    def count(
        self,
        since: float | None = None,
        caller: str | None = None,
        *,
        until: float | None = None,
        status: Status | None = None,
        tier: Tier | None = None,
        executed: bool | None = None,
        target_mid: str | None = None,
    ) -> int:
        """The number of calls matching the :meth:`list` filters."""
        where, params = _filters(since, until, caller, status, tier, executed, target_mid)
        return self._conn.execute(f"SELECT count(*) FROM calls c{where}", params).fetchone()[0]

    def target_total(
        self,
        since: float | None = None,
        caller: str | None = None,
        *,
        until: float | None = None,
        status: Status | None = None,
        tier: Tier | None = None,
        executed: bool | None = None,
        target_mid: str | None = None,
    ) -> int:
        """The sum of ``target_count`` over the calls matching the :meth:`list` filters (06b's
        rate windows: W1 targets of executed calls in the last hour)."""
        where, params = _filters(since, until, caller, status, tier, executed, target_mid)
        return self._conn.execute(
            f"SELECT coalesce(sum(c.target_count), 0) FROM calls c{where}", params
        ).fetchone()[0]


def _filters(
    since: float | None,
    until: float | None,
    caller: str | None,
    status: Status | None,
    tier: Tier | None,
    executed: bool | None,
    target_mid: str | None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if since is not None:
        clauses.append("c.ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("c.ts < ?")
        params.append(until)
    if caller is not None:
        clauses.append("c.caller = ?")
        params.append(caller)
    if status is not None:
        if status not in get_args(Status):
            raise ValueError("invalid status filter")
        clauses.append(f"{_STATUS_SQL} = ?")
        params.append(status)
    if tier is not None:
        clauses.append("c.tier = ?")
        params.append(Tier(tier).value)
    if executed is not None:
        clauses.append("c.initial_status IS NOT NULL" if executed else "c.initial_status IS NULL")
    if target_mid is not None:
        clauses.append("EXISTS (SELECT 1 FROM json_each(c.target_mids) j WHERE j.value = ?)")
        params.append(target_mid)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _record(r: Sequence[Any]) -> AuditRecord:
    return AuditRecord(
        call_id=r[0],
        ts=r[1],
        caller=r[2],
        caller_label=r[3],
        tool=r[4],
        tier=None if r[5] is None else Tier(r[5]),
        decision=r[6],
        reason=r[7],
        args=None if r[8] is None else json.loads(r[8]),
        args_sha256=r[9],
        original_args_sha256=r[10],
        target_mids=tuple(json.loads(r[11])),
        target_count=r[12],
        initial_status=r[13],
        status=r[14],
        undone=bool(r[15]),
        undo_token=r[16],
    )
