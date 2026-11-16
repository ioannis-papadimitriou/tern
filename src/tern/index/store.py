"""The index store: one SQLite file per TB profile (architecture §4, plan 04 step 2).

- :func:`index_path` is ``<data dir>/index.db`` (``$TERN_HOME/data/index.db`` when set).
- :meth:`Store.open` puts the file in a 0700 dir, uses WAL, ``foreign_keys=ON`` and a 5 s busy
  timeout, and migrates (or refuses a newer schema). ``readonly=True`` opens with
  ``file:…?mode=ro``: no file is created and every write is refused.
- Every connection, read-write and read-only, loads sqlite-vec (the pinned
  :data:`SQLITE_VEC_VERSION`, checked at load; plan 05 step 3): ``tern.search.vec``'s triggers
  on ``chunks`` reference the ``chunks_vec`` vec0 table, and without the extension a chunk
  delete would fail with ``no such module: vec0``.
- Writes run in :meth:`Store.transaction` (re-entrant: an ingest wraps one message's
  ``upsert_message`` + ``add_location`` in a single transaction; a nested level is a SAVEPOINT,
  undone on its own if it raises). Each location write recomputes
  ``messages.conflicted`` over the hashes in ``locations ∪ same_folder_copies`` (#5, #30): it is
  set when two hashes differ, including a raw-derived and a ``full-`` hash of one mid (the two
  kinds cannot be compared, so a mix is a conflict, never classified: :func:`hashes_conflict`),
  and cleared when they agree. The last location of a mid going
  away deletes the message with its chunks (FTS via triggers) and attachments;
  ``sent_recipients`` is never touched.
- Conflicts (step 6, D1/T14): ``messages.content_hash`` names the copy whose content fills the
  row. When a removal leaves no copy with that hash (e.g. the spoof that was ingested first is
  deleted), the content is re-filled in the same transaction, before ``conflicted`` is
  recomputed, from the ``refill`` record the caller parsed from a remaining copy
  (:meth:`refill_candidates` names them; steps 7–8 fetch one with ``find`` + ``raw``). Without a
  refill the stale content is cleared instead (fields, chunks/FTS, attachments; the row stays
  with ``content_hash`` NULL, counted as ``refill_pending``): a deleted copy's text never stays
  searchable, and the next upsert of any copy fills the row again; :meth:`pending_refills`
  lists those mids with their remaining copies so a later job can fetch one. When a conflict is
  detected and the caller passes the raw bytes of the added copy and of the content copy
  (:meth:`content_copy`), the pair is classified by ``tern.index.conflicts.classify_pair`` into
  ``messages.conflict_class`` (the worst class seen while conflicted; NULL when no raw pair was
  supplied, e.g. ``full-`` copies or a raw/``full-`` mix, counted as
  ``conflicts_unclassified``; cleared with the conflict).
- ``upsert_message`` writes the message's chunks (``tern.index.chunks``: subject, body from
  ``body_text``, attachment names) in the same transaction, and the FTS triggers keep
  ``chunks_fts`` in sync. On a replace whose chunks come out identical (same ord, kind, text and
  fts_text, e.g. a same-hash re-ingest) the rows are left alone, so their chunk ids stay
  stable; otherwise all of the message's chunks are deleted and inserted again.
- ``thread_id`` (``tern.index.threads``, step 5): ``upsert_message`` and the removal of a message
  re-run the threading over the connected component the message touches (every stored message
  linked to it through References/In-Reply-To keys, transitively), in the same transaction, so a
  stored message re-roots every message whose chain now reaches it and a removed one releases
  them. The result is a pure function of the stored set: the same for any ingest order. The
  component's links live in the connection-local TEMP table ``thread_links`` (mid, pos, key),
  never in the index file: built from ``messages`` on first use, rebuilt when another
  connection has committed (``PRAGMA data_version``) or when a rolled-back scope had rebuilt it.
  Invariant: every write of ``messages.refs`` / ``in_reply_to`` and every ``messages`` delete
  goes through ``_ensure_links`` (before the write), ``_set_links`` and ``_rethread``; the
  step 6 refill does so by going through :meth:`upsert_message`. A new write path must too, or
  ``thread_links`` and ``thread_id`` go stale.
- Observers fire only after the outermost transaction commits, in order:
  ``location_observers(mid, folder_id, state | None, source)`` for every location added, removed
  (``None``) or whose tags/flagged/read changed; ``message_removed_observers(mid)`` for every
  deleted message. A rolled-back transaction fires nothing. Every committed event reaches every
  observer even if some raise; the failures are re-raised after the last call.

- Step 8 (events and reconcile): :meth:`folder_locations` and :meth:`location_hashes` (reads
  for a reconcile window), :meth:`set_folder_reconciled` (``folders.tb_total``,
  ``last_reconciled_at``) and :meth:`record_ingest_run` (one ``ingest_runs`` row, incl.
  ``missed`` and ``unresolved_events``). The :attr:`connection` rule now names the bookkeeping
  tables the ingest jobs may write directly.

Nothing here logs content: mids and counts only (CLAUDE.md).

Known limitations (cross-plan notes for plan 05):

- A replace that changes a message's chunks gives the new rows new chunk ids and fires no
  observer (``message_removed_observers`` fire only when a message is removed). Plan 05's
  ``chunks_vec`` (keyed by ``chunk_id``) must drop vec rows in step with chunk deletes, e.g. an
  ``AFTER DELETE ON chunks`` trigger it creates with the per-model table, or a chunks-replaced
  signal added here. Resolved in plan 05 (decisions #31): ``tern.search.vec`` creates the
  ``chunks_vec_ad`` / ``chunks_vec_au`` triggers with the table.
- Re-threading costs one component walk per stored message (a few indexed queries per member),
  so ingesting a k-message thread costs O(k²) queries in total; fine for personal mail, slow for
  a thread of many thousands.
- Any commit by another write connection makes this connection rebuild ``thread_links`` in
  full (O(all messages)) on its next write. With one writer connection per process that is
  only the first write after open; a second writing ``Store`` used alongside backfill (e.g.
  plan 05 writing ``chunks_vec`` or embedding state through its own connection) would make
  every ingested message pay a full rebuild, O(N²) overall. Plan 05: write through the ingest's
  connection, or move the links into a persistent table (a 001_init.sql change, two-way).
  Resolved in plan 05 (decisions #31): the embed queue writes through the ingest's ``Store``.
- A ``thread_id`` may be the mid of a message that is not indexed (an empty root kept by JWZ
  step 4, ``tern.index.threads``): compare thread_ids by equality, never look one up with
  :meth:`get_message` (plan 06a).
- ``chunks.chunk_id`` is a plain ``INTEGER PRIMARY KEY`` (001_init.sql, step 2), so SQLite may
  reuse a deleted maximum rowid: a vector left behind by a missed delete could pair with a
  different chunk. ``AUTOINCREMENT`` would prevent reuse; it is a 001_init.sql change (not in
  step 4's files) and is still cheap while the schema is unshipped. Plan 05 (decisions #31)
  keeps reuse and has ``tern.search.vec.insert`` re-check the chunk's id and text instead.
- ``MessageRecord.body_text`` must be the main text only (quotes stripped); the Store cannot
  check that. The real ingest path is pinned by tests/index/test_ingest.py (CANARY-INJ-3 = 0
  FTS rows after Backfill); any other caller must honour it.

Known limitations and obligations of step 6 (conflicts; for steps 7–8 and 10):

- A removal without a ``refill`` clears the content; nothing in the Store fills it again. The
  caller must either pass a refill (call :meth:`refill_candidates` before the removal, fetch one
  with ``find`` + ``raw``) or re-fetch the mids :meth:`pending_refills` lists (reconcile should
  treat ``refill_pending > 0`` as work). Backfill alone may never revisit such a copy, since its
  watermark has already passed it.
- ``conflict_class`` is set only when the caller passes the raw pair to :meth:`add_location`.
  Steps 7–8 must fetch the content copy's raw (:meth:`content_copy`, then ``find`` + ``raw``) on
  every new raw-derived conflict, or step 12's per-account class counts stay incomplete
  (``conflicts_unclassified`` makes the gap visible). ``full-`` conflicts, and conflicts of a
  raw-derived with a ``full-`` copy, are never classified.
- ``conflict_class`` is the worst class seen while the mid stays conflicted, not the class of
  the copies that remain: removing the copy that caused ``other`` while a ``footer`` pair is left
  keeps ``other``. Recomputing would need the remaining copies' raws on every removal.
- :meth:`remove_location` and :meth:`remove_folder` reject a ``refill`` that is not one of the
  remaining copies even when no refill is needed or no location is left (StoreError, nothing
  applied). A caller that picks its refill from :meth:`refill_candidates` before an ``await``
  must therefore hold the removal and that pick in one serialized pass: if the other copies
  vanish in between, the removal now raises instead of deleting the message (plan 04 steps
  8–9 note; fails loudly, never a wrong count).
- ``add_location`` checks ``content_raw`` against the stored content even when this copy turns
  out to be the content copy. An ingest that fetches ``content_raw`` before its write
  transaction and loses a race with a removal that cleared the row (so this copy's upsert
  refills it) gets ValueError; the caller should re-read ``messages.content_hash`` inside the
  transaction and drop a stale ``content_raw`` (ingest.py, plan 04 steps 8–9 note).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal, Self
from urllib.parse import quote
from uuid import UUID

import sqlite_vec

from tern.bridge.protocol import BridgeError
from tern.index import schema
from tern.index.chunks import build_chunks
from tern.index.conflicts import BENIGN, CLASSES, ConflictClass, classify_pair, worst
from tern.index.mid import FULL_PREFIX, X_PREFIX
from tern.index.mid import content_hash as raw_content_hash
from tern.index.threads import assign_threads, message_links
from tern.paths import TernPaths

log = logging.getLogger(__name__)

INDEX_NAME = "index.db"
BUSY_TIMEOUT_MS = 5000
#: The sqlite-vec build every connection must load (pinned in pyproject; pre-1.0 API).
SQLITE_VEC_VERSION = "v0.1.9"
DIR_MODE = 0o700

_MID = re.compile(r"^(x-)?[0-9a-f]{64}$")

# Connection-local (TEMP) link table for threading: never part of the index file's schema.
_LINKS_DDL = (
    "CREATE TEMP TABLE IF NOT EXISTS thread_links(mid TEXT NOT NULL, pos INTEGER NOT NULL, "
    "key TEXT NOT NULL, PRIMARY KEY(mid, pos))",
    "CREATE INDEX IF NOT EXISTS temp.thread_links_key ON thread_links(key)",
)
_HASH = re.compile(r"^(full-)?[0-9a-f]{64}$")

Source = Literal["backfill", "event", "reconcile"]
ParseStatus = Literal["ok", "partial", "failed"]
UpsertOutcome = Literal["inserted", "replaced", "kept"]


class StoreError(RuntimeError):
    """A Store call that cannot be applied as asked."""


class StoreReadOnly(StoreError):
    """A write on a Store opened with ``readonly=True``."""


class NoSuchLocation(StoreError):
    """The (mid, folder_id) location, or the named copy in it, is not in the index."""


def index_path(tp: TernPaths) -> Path:
    return tp.data / INDEX_NAME


@dataclass(frozen=True)
class LocationState:
    """TB's per-location state; ``tags`` are kept sorted and de-duplicated."""

    tags: tuple[str, ...] = ()
    flagged: bool = False
    read: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", tuple(sorted(set(self.tags))))


LocationObserver = Callable[[str, str, LocationState | None, str], None]
MessageRemovedObserver = Callable[[str], None]


@dataclass(frozen=True)
class AttachmentRecord:
    filename: str | None
    mime: str | None
    size: int | None


@dataclass(frozen=True)
class MessageRecord:
    """One parsed copy of a message: what ``upsert_message`` writes."""

    mid: str
    content_hash: str
    parse_status: ParseStatus
    header_message_id: str | None = None
    date_utc: int | None = None
    from_addr: str | None = None
    from_name: str | None = None
    to_addrs: tuple[str, ...] = ()
    cc_addrs: tuple[str, ...] = ()
    subject: str | None = None
    in_reply_to: str | None = None
    refs: tuple[str, ...] = ()
    size: int | None = None
    #: The main text only, ``tern.index.mime.ParsedMessage.main_text``: quotes and signature
    #: already removed. Body chunks (and so FTS and plan 05's vectors) are built from it, so
    #: quoted mail passed here would become searchable. Not enforced here (the Store cannot
    #: tell quoted text from main text); tests/index/test_ingest.py pins it on the real ingest
    #: path (CANARY-INJ-3, only in a quoted part, matches 0 FTS rows after Backfill).
    body_text: str | None = None
    attachments: tuple[AttachmentRecord, ...] = ()


@dataclass(frozen=True)
class MessageRow:
    mid: str
    content_hash: str | None
    parse_status: ParseStatus | None
    header_message_id: str | None
    thread_id: str | None
    date_utc: int | None
    from_addr: str | None
    from_name: str | None
    to_addrs: tuple[str, ...]
    cc_addrs: tuple[str, ...]
    subject: str | None
    in_reply_to: str | None
    refs: tuple[str, ...]
    lang: str | None
    size: int | None
    has_attachments: bool
    body_text: str | None
    conflicted: bool
    conflict_class: str | None
    indexed_at: int | None
    attachments: tuple[AttachmentRecord, ...]


@dataclass(frozen=True)
class LocationRow:
    mid: str
    folder_id: str
    content_hash: str
    state: LocationState
    tb_date: int


@dataclass(frozen=True)
class FolderRow:
    folder_id: str
    account_id: str
    path: str
    special_use: tuple[str, ...]
    tb_total: int | None
    last_reconciled_at: int | None


@dataclass(frozen=True)
class StatusCounts:
    schema_version: int
    messages: int
    locations: int
    same_folder_copies: int
    conflicted: int
    x_mids: int
    parse_ok: int
    parse_partial: int
    parse_failed: int
    headers_only: int
    skipped_large_noid: int
    #: Items TB failed to read (``raw``/``full`` gave ``tb_error``), skipped until re-fetched.
    skipped_tb_error: int
    #: Messages whose content was cleared because no remaining copy holds it (no refill yet).
    refill_pending: int
    #: Conflicted messages with no ``conflict_class`` (no raw pair was supplied, or a ``full-``
    #: copy is involved).
    conflicts_unclassified: int


RunKind = Literal["backfill", "hourly", "daily", "event"]


@dataclass(frozen=True)
class IngestRun:
    """One ``ingest_runs`` row (#30): counts only, never names or content."""

    ts: int
    kind: RunKind
    folder_id: str | None
    account_type: str | None
    n_msgs: int = 0
    secs: float = 0.0
    concurrency: int | None = None
    throttle: float | None = None
    missed: int = 0
    stale_retries: int = 0
    skipped_large_noid: int = 0
    md5_ids: int = 0
    x_mid_churn: int = 0
    hmid_mismatch: int = 0
    unresolved_events: int = 0


def hashes_conflict(hashes: Iterable[str]) -> bool:
    """True when the copies' hashes are not all equal (#5, #30 amendment 7 as amended): two
    raw-derived hashes differ, two ``full-`` hashes differ, or the copies mix the two kinds.
    A raw-derived and a ``full-`` hash cannot be compared, so a mix counts as a conflict (left
    unclassified): otherwise a spoof padded past RAW_MAX would never be checked against the
    genuine copy (T14). A genuine message whose size straddles RAW_MAX between two stores is
    flagged too, the denial of service #5 already accepts."""
    return len(set(hashes)) > 1


def _conflict_class(value: str) -> ConflictClass:
    for cls in CLASSES:
        if cls == value:
            return cls
    raise StoreError("unknown conflict_class in the index")


def _refill_order(copy: tuple[str, str]) -> tuple[bool, str, str]:
    """Raw-derived hashes before ``full-`` ones (a raw copy gives the full parse), then by
    folder_id and hash."""
    folder_id, content_hash = copy
    return (content_hash.startswith(FULL_PREFIX), folder_id, content_hash)


def _check_mid(mid: str) -> None:
    if not _MID.match(mid):
        raise ValueError("not a mid")


def _check_hash(content_hash: str) -> None:
    if not _HASH.match(content_hash):
        raise ValueError("not a content_hash")


def _json_list(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _tuple(text: str | None) -> tuple[str, ...]:
    return () if text is None else tuple(json.loads(text))


def _state(tags: str | None, flagged: int | None, read: int | None) -> LocationState:
    return LocationState(tags=_tuple(tags), flagged=bool(flagged), read=bool(read))


def _load_vec(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec into ``conn`` (extension loading is enabled only for the load) and check
    its version; a different build raises StoreError."""
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    version = conn.execute("SELECT vec_version()").fetchone()[0]
    if version != SQLITE_VEC_VERSION:
        raise StoreError(f"sqlite-vec {version!r} loaded, {SQLITE_VEC_VERSION!r} required")


def _connect_rw(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path.parent, DIR_MODE)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        _load_vec(conn)
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode != "wal":
            raise StoreError(f"journal_mode is {mode!r}, not 'wal'")
        conn.execute("PRAGMA foreign_keys = ON")
        schema.check_version(conn)
        schema.migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _connect_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"no index at {path}")
    uri = "file:" + quote(str(path.absolute()), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        _load_vec(conn)
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        version = schema.check_version(conn)
        if version < schema.SCHEMA_VERSION:
            raise schema.SchemaTooOld(version)
    except BaseException:
        conn.close()
        raise
    return conn


class Store:
    """The index. Create with :meth:`open`; not thread-safe (one Store per thread/task)."""

    def __init__(self, conn: sqlite3.Connection, path: Path, *, readonly: bool) -> None:
        self._conn = conn
        self.path = path
        self.readonly = readonly
        self.location_observers: list[LocationObserver] = []
        self.message_removed_observers: list[MessageRemovedObserver] = []
        self._depth = 0
        self._pending_locations: list[tuple[str, str, LocationState | None, str]] = []
        self._pending_removed: list[str] = []
        # ``thread_links`` is current for this main-db data_version (None: rebuild before use).
        self._links_version: int | None = None
        self._links_builds = 0  # bumped on every (re)build; a rollback that undid one resets

    @classmethod
    def open(cls, path: Path, *, readonly: bool = False) -> Store:
        conn = _connect_ro(path) if readonly else _connect_rw(path)
        return cls(conn, path, readonly=readonly)

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

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection, for reads and tests. Writes to ``messages``,
        ``locations``, ``same_folder_copies``, ``chunks`` and ``attachments`` go through Store
        methods only: a direct write there skips the conflict recompute, the threading and the
        observer bookkeeping. The ingest jobs' own bookkeeping tables (``ingest_state``,
        ``headers_only_items``, ``skipped_items``, ``sent_counted``, ``sent_recipients``), which
        carry none of that, may be written through this connection inside
        :meth:`transaction` (plan 04 step 8 narrowed this rule to what step 7 does);
        :meth:`record_ingest_run` and :meth:`set_folder_reconciled` cover ``ingest_runs`` and
        ``folders``. ``tern.search.vec`` (plan 05 step 3) writes the vector state the same way,
        inside :meth:`transaction`: ``chunks_vec`` and its triggers ``chunks_vec_ad`` /
        ``chunks_vec_au``, ``embedding_model``, and the ``meta`` key ``embed_complete_at``
        (no other ``meta`` key)."""
        return self._conn

    # ---- transactions and observers --------------------------------------------------------

    def _require_writable(self) -> None:
        if self.readonly:
            raise StoreReadOnly("index opened read-only")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One write transaction (BEGIN IMMEDIATE … COMMIT). A nested call runs in a SAVEPOINT:
        if it raises, its writes and the observer events it queued are undone (``ROLLBACK TO``)
        even when an outer caller catches the exception and goes on to commit. Observers queued
        inside fire after the outermost COMMIT; an exception that leaves the outermost level
        rolls everything back and nothing fires."""
        self._require_writable()
        if self._depth:
            yield from self._savepoint()
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        builds = self._links_builds
        try:
            yield
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            self._pending_locations.clear()
            self._pending_removed.clear()
            if self._links_builds != builds:
                self._links_version = None
            raise
        finally:
            self._depth = 0
        located, self._pending_locations = self._pending_locations, []
        removed, self._pending_removed = self._pending_removed, []
        self._notify(located, removed)

    def _savepoint(self) -> Iterator[None]:
        self._depth += 1
        name = f"tern_sp_{self._depth}"
        n_located, n_removed = len(self._pending_locations), len(self._pending_removed)
        builds = self._links_builds
        self._conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self._conn.execute(f"ROLLBACK TO {name}")
            self._conn.execute(f"RELEASE {name}")
            del self._pending_locations[n_located:]
            del self._pending_removed[n_removed:]
            if self._links_builds != builds:
                self._links_version = None
            raise
        else:
            self._conn.execute(f"RELEASE {name}")
        finally:
            self._depth -= 1

    def _notify(
        self, located: list[tuple[str, str, LocationState | None, str]], removed: list[str]
    ) -> None:
        """Deliver every committed event to every observer, even when some of them raise (the
        changes are committed; a failing observer must not hide them from the others). The
        failures are re-raised afterwards: one as itself, several as an ExceptionGroup."""
        errors: list[Exception] = []
        for args in located:
            for obs in list(self.location_observers):
                try:
                    obs(*args)
                except Exception as e:  # re-raised below, after every observer ran
                    errors.append(e)
        for mid in removed:
            for rm in list(self.message_removed_observers):
                try:
                    rm(mid)
                except Exception as e:  # re-raised below, after every observer ran
                    errors.append(e)
        if not errors:
            return
        log.error(
            "index observers failed",
            extra={"failures": len(errors), "types": sorted({type(e).__name__ for e in errors})},
        )
        if len(errors) == 1:
            raise errors[0]
        raise ExceptionGroup("index observers failed", errors)

    # ---- profile binding ---------------------------------------------------------------------

    def profile_id(self) -> UUID | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'tb_profile_id'").fetchone()
        return None if row is None else UUID(row[0])

    def bind_profile(self, profile_id: UUID) -> None:
        """Bind the index to a TB profile on first use; another profile → ``profile_mismatch``.

        A path-derived id (UUID version 5, ``tern.bridge.tbprofile``) replaces an older random
        extension id (version 4) once: that random id was lost whenever the add-on was
        reinstalled, while the path-derived one is stable.
        """
        with self.transaction():
            bound = self.profile_id()
            if bound is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('tb_profile_id', ?)", (str(profile_id),)
                )
                log.info("index bound to a TB profile")
            elif bound.version == 4 and profile_id.version == 5:
                self._conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'tb_profile_id'", (str(profile_id),)
                )
                log.info("index re-bound to the TB profile directory")
            elif bound != profile_id:
                raise BridgeError("profile_mismatch", "index is bound to another TB profile")

    # ---- accounts and folders ----------------------------------------------------------------

    def upsert_account(self, account_id: str, name: str, account_type: str) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO accounts(account_id, name, type) VALUES (?, ?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET name = excluded.name, type = excluded.type",
                (account_id, name, account_type),
            )

    def upsert_folder(
        self, folder_id: str, account_id: str, path: str, special_use: Sequence[str]
    ) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO folders(folder_id, account_id, path, special_use) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(folder_id) DO UPDATE SET "
                "account_id = excluded.account_id, path = excluded.path, "
                "special_use = excluded.special_use",
                (folder_id, account_id, path, _json_list(special_use)),
            )

    def set_folder_reconciled(self, folder_id: str, tb_total: int, at: int) -> None:
        """A reconcile pass over ``folder_id`` finished: TB's message count and the time."""
        with self.transaction():
            cur = self._conn.execute(
                "UPDATE folders SET tb_total = ?, last_reconciled_at = ? WHERE folder_id = ?",
                (tb_total, at, folder_id),
            )
            if cur.rowcount != 1:
                raise StoreError("set_folder_reconciled on a folder not in the index")

    def record_ingest_run(self, run: IngestRun) -> None:
        """One ``ingest_runs`` row (counts only)."""
        with self.transaction():
            self._conn.execute(
                "INSERT INTO ingest_runs(ts, kind, folder_id, account_type, n_msgs, secs, "
                "concurrency, throttle, missed, stale_retries, skipped_large_noid, md5_ids, "
                "x_mid_churn, hmid_mismatch, unresolved_events) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.ts,
                    run.kind,
                    run.folder_id,
                    run.account_type,
                    run.n_msgs,
                    run.secs,
                    run.concurrency,
                    run.throttle,
                    run.missed,
                    run.stale_retries,
                    run.skipped_large_noid,
                    run.md5_ids,
                    run.x_mid_churn,
                    run.hmid_mismatch,
                    run.unresolved_events,
                ),
            )

    def remove_folder(
        self,
        folder_id: str,
        *,
        source: Source,
        refills: Mapping[str, MessageRecord] | None = None,
    ) -> int:
        """Drop a folder that vanished from TB: every location in it is removed (observers get
        ``None``), messages left without a location are deleted, and the folder's rows in
        ``folders``, ``ingest_state``, ``headers_only_items`` and ``skipped_items`` go too.
        ``refills`` maps a mid to its refill record, as in :meth:`remove_location`; a key that
        has no location in the folder raises ValueError, and a refill that is not one of the
        mid's remaining copies raises StoreError, needed or not (nothing is applied either
        way). A valid refill that turns out not to be needed is ignored and counted in the log.
        Returns the number of locations removed."""
        refills = refills or {}
        if any(rec.mid != mid for mid, rec in refills.items()):
            raise ValueError("refill is for another mid")
        unused = 0
        with self.transaction():
            mids = [
                r[0]
                for r in self._conn.execute(
                    "SELECT mid FROM locations WHERE folder_id = ? ORDER BY mid", (folder_id,)
                )
            ]
            if not set(refills) <= set(mids):
                raise ValueError("refill for a mid with no location in this folder")
            for mid in mids:
                self._drop_location(mid, folder_id, source)
                refill = refills.get(mid)
                if not self._after_location_change(mid, refill) and refill is not None:
                    unused += 1
            for table in ("folders", "ingest_state", "headers_only_items", "skipped_items"):
                self._conn.execute(f"DELETE FROM {table} WHERE folder_id = ?", (folder_id,))
        log.info(
            "folder removed",
            extra={"locations": len(mids), "refills": len(refills), "refills_unused": unused},
        )
        return len(mids)

    # ---- messages and locations --------------------------------------------------------------

    def upsert_message(self, rec: MessageRecord) -> UpsertOutcome:
        """Insert a message, or replace its content from ``rec``. A copy whose hash differs from
        the stored content's is ``"kept"`` out (nothing written) while some location or
        same-folder copy still holds the stored content's hash; otherwise it replaces it.
        Inserted content gets its chunks; replaced content keeps its chunk rows when they come
        out identical, otherwise they are deleted and rebuilt. An insert, or a replace that
        changes the message's thread links, re-threads the component it touches."""
        _check_mid(rec.mid)
        _check_hash(rec.content_hash)
        if rec.mid.startswith(X_PREFIX) and rec.header_message_id is not None:
            raise ValueError("an x- mid has no header_message_id")
        content = (
            rec.header_message_id,
            rec.date_utc,
            rec.from_addr,
            rec.from_name,
            _json_list(rec.to_addrs),
            _json_list(rec.cc_addrs),
            rec.subject,
            rec.in_reply_to,
            _json_list(rec.refs),
            rec.size,
            int(bool(rec.attachments)),
            rec.body_text,
            rec.parse_status,
            rec.content_hash,
            int(time.time()),
        )
        with self.transaction():
            self._ensure_links()  # before any write: a rebuild must see the old links
            row = self._conn.execute(
                "SELECT content_hash FROM messages WHERE mid = ?", (rec.mid,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO messages(header_message_id, date_utc, from_addr, from_name, "
                    "to_addrs, cc_addrs, subject, in_reply_to, refs, size, has_attachments, "
                    "body_text, parse_status, content_hash, indexed_at, mid, thread_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*content, rec.mid, rec.mid),
                )
                outcome: UpsertOutcome = "inserted"
            elif row[0] != rec.content_hash and self._hash_held(rec.mid, row[0]):
                return "kept"
            else:
                self._conn.execute(
                    "UPDATE messages SET header_message_id = ?, date_utc = ?, from_addr = ?, "
                    "from_name = ?, to_addrs = ?, cc_addrs = ?, subject = ?, in_reply_to = ?, "
                    "refs = ?, size = ?, has_attachments = ?, body_text = ?, parse_status = ?, "
                    "content_hash = ?, indexed_at = ? WHERE mid = ?",
                    (*content, rec.mid),
                )
                self._conn.execute("DELETE FROM attachments WHERE mid = ?", (rec.mid,))
                outcome = "replaced"
            self._conn.executemany(
                "INSERT INTO attachments(mid, idx, filename, mime, size) VALUES (?, ?, ?, ?, ?)",
                [(rec.mid, i, a.filename, a.mime, a.size) for i, a in enumerate(rec.attachments)],
            )
            self._write_chunks(rec, replace=outcome == "replaced")
            links = message_links(rec.mid, rec.refs, rec.in_reply_to)
            old = self._set_links(rec.mid, links)
            if outcome == "inserted" or old != links:
                self._rethread([rec.mid, *old, *links])
        return outcome

    def add_location(
        self,
        mid: str,
        folder_id: str,
        content_hash: str,
        state: LocationState,
        tb_date: int,
        *,
        source: Source,
        raw: bytes | None = None,
        content_raw: bytes | None = None,
    ) -> None:
        """Record a copy of ``mid`` in ``folder_id``. A new location fires the observers. In a
        folder that already holds the mid, an identical copy collapses into the location row
        and its state and tb_date overwrite the row's (observers fire on a state change); which
        state collapsed copies should keep is provisional, unresolved (#30). A differing copy
        goes to ``same_folder_copies`` and leaves the location row as it is.

        ``raw`` (this copy's getRaw bytes) and ``content_raw`` (those of the copy holding
        ``messages.content_hash``, see :meth:`content_copy`) are optional and go together: when
        this copy's hash differs from the content's, the pair is classified into
        ``conflict_class``. Each must hash to its copy's content_hash (ValueError otherwise,
        also when the pair is not compared because this copy is the content copy); on a row
        whose content was cleared (``refill_pending``) they are refused. Conflict detection
        itself never depends on them."""
        _check_hash(content_hash)
        if (raw is None) != (content_raw is None):
            raise ValueError("raw and content_raw go together")
        with self.transaction():
            stored = self._conn.execute(
                "SELECT content_hash FROM messages WHERE mid = ?", (mid,)
            ).fetchone()
            if stored is None:
                raise StoreError("add_location before upsert_message")
            pair_class = None
            if raw is not None and content_raw is not None:
                pair_class = self._pair_class(content_hash, raw, stored[0], content_raw)
            row = self._conn.execute(
                "SELECT content_hash, tags, flagged, read, tb_date FROM locations "
                "WHERE mid = ? AND folder_id = ?",
                (mid, folder_id),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO locations(mid, folder_id, content_hash, tags, flagged, read, "
                    "tb_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        mid,
                        folder_id,
                        content_hash,
                        _json_list(state.tags),
                        int(state.flagged),
                        int(state.read),
                        tb_date,
                    ),
                )
                self._pending_locations.append((mid, folder_id, state, source))
            elif row[0] != content_hash:
                self._conn.execute(
                    "INSERT OR IGNORE INTO same_folder_copies(mid, folder_id, content_hash) "
                    "VALUES (?, ?, ?)",
                    (mid, folder_id, content_hash),
                )
            else:
                self._write_state(mid, folder_id, _state(row[1], row[2], row[3]), state, source)
                if row[4] != tb_date:
                    self._conn.execute(
                        "UPDATE locations SET tb_date = ? WHERE mid = ? AND folder_id = ?",
                        (tb_date, mid, folder_id),
                    )
            self._recompute_conflicted(mid, pair_class)

    def set_location_state(
        self, mid: str, folder_id: str, state: LocationState, *, source: Source
    ) -> None:
        """Update tags/flagged/read of a location; observers fire only when it changed."""
        with self.transaction():
            row = self._conn.execute(
                "SELECT tags, flagged, read FROM locations WHERE mid = ? AND folder_id = ?",
                (mid, folder_id),
            ).fetchone()
            if row is None:
                raise NoSuchLocation("no such location")
            self._write_state(mid, folder_id, _state(*row), state, source)
            self._recompute_conflicted(mid)

    def set_location_tb_date(self, mid: str, folder_id: str, tb_date: int) -> None:
        """Move a location's ``tb_date`` to that of another TB copy of its content in the folder
        (reconcile, plan 04 M3: the copy whose date the row held is gone, an identical one
        remains). Not a state change: observers do not fire."""
        with self.transaction():
            cur = self._conn.execute(
                "UPDATE locations SET tb_date = ? WHERE mid = ? AND folder_id = ?",
                (tb_date, mid, folder_id),
            )
            if cur.rowcount != 1:
                raise NoSuchLocation("no such location")

    def remove_location(
        self,
        mid: str,
        folder_id: str,
        *,
        content_hash: str | None = None,
        source: Source,
        refill: MessageRecord | None = None,
    ) -> None:
        """Remove a location (``content_hash=None``: every copy of ``mid`` in the folder), or one
        copy in it: a same-folder copy is dropped; the location row's own copy is replaced by
        the remaining same-folder copy with the smallest hash, if any. Observers get ``None``
        only when the location is gone. The mid's last location gone → message deleted.

        If no remaining copy holds ``messages.content_hash``, the content is rebuilt from
        ``refill`` (a remaining copy's record, one of :meth:`refill_candidates`) before the
        conflict flag is recomputed, or cleared when ``refill`` is None. A ``refill`` for
        another mid, or whose hash no remaining copy has (also when the removal leaves no
        location at all), raises (nothing is applied), whether or not it was needed; a valid
        one that is not needed (a remaining copy still holds the content) is ignored."""
        if refill is not None and refill.mid != mid:
            raise ValueError("refill is for another mid")
        with self.transaction():
            row = self._conn.execute(
                "SELECT content_hash FROM locations WHERE mid = ? AND folder_id = ?",
                (mid, folder_id),
            ).fetchone()
            if row is None:
                raise NoSuchLocation("no such location")
            copies = [
                r[0]
                for r in self._conn.execute(
                    "SELECT content_hash FROM same_folder_copies WHERE mid = ? AND folder_id = ? "
                    "ORDER BY content_hash",
                    (mid, folder_id),
                )
            ]
            if content_hash is None or (content_hash == row[0] and not copies):
                self._drop_location(mid, folder_id, source)
            elif content_hash in copies:
                self._delete_copy(mid, folder_id, content_hash)
            elif content_hash == row[0]:
                self._conn.execute(
                    "UPDATE locations SET content_hash = ? WHERE mid = ? AND folder_id = ?",
                    (copies[0], mid, folder_id),
                )
                self._delete_copy(mid, folder_id, copies[0])
            else:
                raise NoSuchLocation("no copy with that content_hash in this location")
            self._after_location_change(mid, refill)

    # ---- internals ---------------------------------------------------------------------------

    def _write_chunks(self, rec: MessageRecord, *, replace: bool) -> None:
        chunks = build_chunks(rec.subject, rec.body_text, (a.filename for a in rec.attachments))
        rows = [(c.ord, c.kind, c.text, c.fts_text) for c in chunks]
        if replace:
            old = self._conn.execute(
                "SELECT ord, kind, text, fts_text FROM chunks WHERE mid = ? ORDER BY ord",
                (rec.mid,),
            ).fetchall()
            if [tuple(r) for r in old] == rows:
                return
            self._conn.execute("DELETE FROM chunks WHERE mid = ?", (rec.mid,))
        self._conn.executemany(
            "INSERT INTO chunks(mid, ord, kind, text, fts_text) VALUES (?, ?, ?, ?, ?)",
            [(rec.mid, *row) for row in rows],
        )

    def _hash_held(self, mid: str, content_hash: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM locations WHERE mid = ? AND content_hash = ? UNION ALL "
                "SELECT 1 FROM same_folder_copies WHERE mid = ? AND content_hash = ? LIMIT 1",
                (mid, content_hash, mid, content_hash),
            ).fetchone()
            is not None
        )

    def _write_state(
        self, mid: str, folder_id: str, old: LocationState, new: LocationState, source: Source
    ) -> None:
        if old == new:
            return
        self._conn.execute(
            "UPDATE locations SET tags = ?, flagged = ?, read = ? WHERE mid = ? AND folder_id = ?",
            (_json_list(new.tags), int(new.flagged), int(new.read), mid, folder_id),
        )
        self._pending_locations.append((mid, folder_id, new, source))

    def _delete_copy(self, mid: str, folder_id: str, content_hash: str) -> None:
        self._conn.execute(
            "DELETE FROM same_folder_copies WHERE mid = ? AND folder_id = ? AND content_hash = ?",
            (mid, folder_id, content_hash),
        )

    def _drop_location(self, mid: str, folder_id: str, source: Source) -> None:
        self._conn.execute(
            "DELETE FROM same_folder_copies WHERE mid = ? AND folder_id = ?", (mid, folder_id)
        )
        self._conn.execute(
            "DELETE FROM locations WHERE mid = ? AND folder_id = ?", (mid, folder_id)
        )
        self._pending_locations.append((mid, folder_id, None, source))

    def _after_location_change(self, mid: str, refill: MessageRecord | None = None) -> bool:
        """Re-fill or clear the content if no remaining copy holds it, recompute the conflict,
        or delete the message when its last location is gone. True when ``refill`` was used.
        A ``refill`` must be one of the remaining copies whether or not it is needed (also when
        no location is left), so a wrong record never passes silently (StoreError)."""
        if refill is not None and not self._hash_held(mid, refill.content_hash):
            raise StoreError("refill is not one of the remaining copies")
        left = self._conn.execute(
            "SELECT count(*) FROM locations WHERE mid = ?", (mid,)
        ).fetchone()[0]
        if left:
            used = False
            stored = self._conn.execute(
                "SELECT content_hash FROM messages WHERE mid = ?", (mid,)
            ).fetchone()[0]
            if stored is None or not self._hash_held(mid, stored):
                if refill is None:
                    self._clear_content(mid)
                else:
                    self.upsert_message(refill)  # the stored hash is no longer held: replaces
                    used = True
                    log.info("message re-filled from a remaining copy", extra={"mid": mid})
            self._recompute_conflicted(mid)
            return used
        self._ensure_links()
        old = self._set_links(mid, ())
        chunks = self._conn.execute("DELETE FROM chunks WHERE mid = ?", (mid,)).rowcount
        self._conn.execute("DELETE FROM attachments WHERE mid = ?", (mid,))
        self._conn.execute("DELETE FROM messages WHERE mid = ?", (mid,))
        self._rethread([mid, *old])
        self._pending_removed.append(mid)
        log.info("message removed: last location gone", extra={"mid": mid, "chunks": chunks})
        return False

    def _clear_content(self, mid: str) -> None:
        """No remaining copy holds the stored content and no refill was given: drop the content
        (fields, chunks/FTS, attachments, thread links) and keep the row with ``content_hash``
        NULL until an upsert of a remaining copy fills it (``refill_pending``)."""
        if self._conn.execute(
            "SELECT content_hash IS NULL FROM messages WHERE mid = ?", (mid,)
        ).fetchone()[0]:
            return
        self._ensure_links()
        old = self._set_links(mid, ())
        self._conn.execute(
            "UPDATE messages SET date_utc = NULL, from_addr = NULL, from_name = NULL, "
            "to_addrs = '[]', cc_addrs = '[]', subject = NULL, in_reply_to = NULL, refs = '[]', "
            "size = NULL, has_attachments = 0, body_text = NULL, parse_status = NULL, "
            "content_hash = NULL, indexed_at = ? WHERE mid = ?",
            (int(time.time()), mid),
        )
        chunks = self._conn.execute("DELETE FROM chunks WHERE mid = ?", (mid,)).rowcount
        self._conn.execute("DELETE FROM attachments WHERE mid = ?", (mid,))
        if old:
            self._rethread([mid, *old])
        log.info(
            "message content cleared: no remaining copy holds it, refill pending",
            extra={"mid": mid, "chunks": chunks},
        )

    def _pair_class(
        self, content_hash: str, raw: bytes, stored: str | None, content_raw: bytes
    ) -> ConflictClass | None:
        """The class of (content copy, added copy), or None when the added copy is the content
        copy (nothing to compare). Both raws must hash to their copies' hashes, checked before
        anything else: a wrong ``content_raw`` raises even when it would not be compared. A row
        whose content was cleared (``stored`` None) has no content copy, so any ``content_raw``
        is wrong."""
        if raw_content_hash(raw) != content_hash:
            raise ValueError("raw does not hash to content_hash")
        if stored is None:
            raise ValueError("content_raw given but the message has no content copy")
        if raw_content_hash(content_raw) != stored:
            raise ValueError("content_raw does not hash to the stored content_hash")
        if stored == content_hash:
            return None
        cls = classify_pair(content_raw, raw)
        if cls in BENIGN:  # impossible for differing hashes (tern.index.conflicts)
            raise StoreError(f"differing content hashes classified as {cls}")
        return cls

    # ---- threads (tern.index.threads) ---------------------------------------------------------

    def _ensure_links(self) -> None:
        """Make ``thread_links`` hold every stored message's links. Called inside a write
        transaction, so no other connection can commit between the check and the use."""
        version = int(self._conn.execute("PRAGMA data_version").fetchone()[0])
        if self._links_version == version:
            return
        for ddl in _LINKS_DDL:
            self._conn.execute(ddl)
        self._conn.execute("DELETE FROM thread_links")
        n = 0
        for mid, refs, in_reply_to in self._conn.execute(
            "SELECT mid, refs, in_reply_to FROM messages"
        ).fetchall():
            n += 1
            self._conn.executemany(
                "INSERT INTO thread_links(mid, pos, key) VALUES (?, ?, ?)",
                [
                    (mid, pos, key)
                    for pos, key in enumerate(message_links(mid, _tuple(refs), in_reply_to))
                ],
            )
        self._links_version = version
        self._links_builds += 1
        log.debug("thread links built", extra={"messages": n})

    def _links(self, mid: str) -> tuple[str, ...]:
        return tuple(
            r[0]
            for r in self._conn.execute(
                "SELECT key FROM thread_links WHERE mid = ? ORDER BY pos", (mid,)
            )
        )

    def _set_links(self, mid: str, links: tuple[str, ...]) -> tuple[str, ...]:
        """Store ``mid``'s links; returns the previous ones."""
        old = self._links(mid)
        if old != links:
            self._conn.execute("DELETE FROM thread_links WHERE mid = ?", (mid,))
            self._conn.executemany(
                "INSERT INTO thread_links(mid, pos, key) VALUES (?, ?, ?)",
                [(mid, pos, key) for pos, key in enumerate(links)],
            )
        return old

    def _rethread(self, seeds: Iterable[str]) -> None:
        """Re-run the threading over every stored message connected to a seed key (a mid or a
        referenced id's key), and write the thread_ids that changed."""
        todo = list(seeds)
        seen: set[str] = set()
        links: dict[str, tuple[str, ...]] = {}
        while todo:
            key = todo.pop()
            if key in seen:
                continue
            seen.add(key)
            found = [
                r[0]
                for r in self._conn.execute(
                    "SELECT mid FROM messages WHERE mid = ? UNION "
                    "SELECT mid FROM thread_links WHERE key = ?",
                    (key, key),
                )
            ]
            for mid in found:
                if mid not in links:
                    links[mid] = self._links(mid)
                    todo.append(mid)
                    todo.extend(links[mid])
        threads = assign_threads(links)
        changed = 0
        for mid, thread_id in sorted(threads.items()):
            changed += self._conn.execute(
                "UPDATE messages SET thread_id = ? WHERE mid = ? AND thread_id IS NOT ?",
                (thread_id, mid, thread_id),
            ).rowcount
        if changed:
            log.debug("re-threaded", extra={"component": len(threads), "changed": changed})

    def _recompute_conflicted(self, mid: str, pair_class: ConflictClass | None = None) -> None:
        """Set or clear ``conflicted`` from the hashes of every copy. While conflicted,
        ``conflict_class`` becomes the worst of its value and ``pair_class``; cleared with the
        flag."""
        hashes = [
            r[0]
            for r in self._conn.execute(
                "SELECT content_hash FROM locations WHERE mid = ? UNION "
                "SELECT content_hash FROM same_folder_copies WHERE mid = ?",
                (mid, mid),
            )
        ]
        conflicted = hashes_conflict(hashes)
        was = self._conn.execute(
            "SELECT conflicted, conflict_class FROM messages WHERE mid = ?", (mid,)
        ).fetchone()
        if was is None:
            return
        cls: str | None = None
        if conflicted:
            cls = was[1]
            if pair_class is not None:
                cls = pair_class if cls is None else worst([_conflict_class(cls), pair_class])
        if (bool(was[0]), was[1]) == (conflicted, cls):
            return
        self._conn.execute(
            "UPDATE messages SET conflicted = ?, conflict_class = ? WHERE mid = ?",
            (int(conflicted), cls, mid),
        )
        if bool(was[0]) != conflicted:
            log.info(
                "mid conflict set" if conflicted else "mid conflict cleared",
                extra={"mid": mid, "hashes": len(hashes)},
            )
        if cls != was[1] and cls is not None:
            log.info("mid conflict classified", extra={"mid": mid, "conflict_class": cls})

    # ---- reads -------------------------------------------------------------------------------

    def get_message(self, mid: str) -> MessageRow | None:
        row = self._conn.execute(
            "SELECT mid, content_hash, parse_status, header_message_id, thread_id, date_utc, "
            "from_addr, from_name, to_addrs, cc_addrs, subject, in_reply_to, refs, lang, size, "
            "has_attachments, body_text, conflicted, conflict_class, indexed_at "
            "FROM messages WHERE mid = ?",
            (mid,),
        ).fetchone()
        if row is None:
            return None
        attachments = tuple(
            AttachmentRecord(filename=a[0], mime=a[1], size=a[2])
            for a in self._conn.execute(
                "SELECT filename, mime, size FROM attachments WHERE mid = ? ORDER BY idx", (mid,)
            )
        )
        return MessageRow(
            mid=row[0],
            content_hash=row[1],
            parse_status=row[2],
            header_message_id=row[3],
            thread_id=row[4],
            date_utc=row[5],
            from_addr=row[6],
            from_name=row[7],
            to_addrs=_tuple(row[8]),
            cc_addrs=_tuple(row[9]),
            subject=row[10],
            in_reply_to=row[11],
            refs=_tuple(row[12]),
            lang=row[13],
            size=row[14],
            has_attachments=bool(row[15]),
            body_text=row[16],
            conflicted=bool(row[17]),
            conflict_class=row[18],
            indexed_at=row[19],
            attachments=attachments,
        )

    def locations(self, mid: str) -> list[LocationRow]:
        return [
            LocationRow(
                mid=r[0], folder_id=r[1], content_hash=r[2], state=_state(*r[3:6]), tb_date=r[6]
            )
            for r in self._conn.execute(
                "SELECT mid, folder_id, content_hash, tags, flagged, read, tb_date FROM locations "
                "WHERE mid = ? ORDER BY folder_id",
                (mid,),
            )
        ]

    def folder_locations(
        self, folder_id: str, lo: int | None = None, hi: int | None = None
    ) -> list[LocationRow]:
        """The locations of ``folder_id`` with ``lo <= tb_date < hi`` (None = unbounded), by
        tb_date then mid."""
        cond = "folder_id = ?"
        args: list[object] = [folder_id]
        if lo is not None:
            cond += " AND tb_date >= ?"
            args.append(lo)
        if hi is not None:
            cond += " AND tb_date < ?"
            args.append(hi)
        return [
            LocationRow(
                mid=r[0], folder_id=r[1], content_hash=r[2], state=_state(*r[3:6]), tb_date=r[6]
            )
            for r in self._conn.execute(
                "SELECT mid, folder_id, content_hash, tags, flagged, read, tb_date FROM locations "
                f"WHERE {cond} ORDER BY tb_date, mid",
                args,
            )
        ]

    def location_hashes(self, mid: str, folder_id: str) -> set[str]:
        """The content hashes of every copy of ``mid`` in ``folder_id`` (the location row's and
        its same-folder copies'); empty when the mid has no location there."""
        return {
            r[0]
            for r in self._conn.execute(
                "SELECT content_hash FROM locations WHERE mid = ? AND folder_id = ? UNION "
                "SELECT content_hash FROM same_folder_copies WHERE mid = ? AND folder_id = ?",
                (mid, folder_id, mid, folder_id),
            )
        }

    def same_folder_copies(self, mid: str) -> list[tuple[str, str]]:
        """(folder_id, content_hash) of every extra differing copy of ``mid``."""
        return [
            (r[0], r[1])
            for r in self._conn.execute(
                "SELECT folder_id, content_hash FROM same_folder_copies WHERE mid = ? "
                "ORDER BY folder_id, content_hash",
                (mid,),
            )
        ]

    def content_copy(self, mid: str) -> tuple[str, str] | None:
        """(folder_id, content_hash) of a copy holding the message's stored content (location
        rows first, then same-folder copies, each by folder_id), or None when no copy does
        (content cleared, or ``mid`` not indexed). Steps 7–8 fetch its raw for ``content_raw``."""
        row = self._conn.execute(
            "SELECT folder_id, content_hash FROM ("
            "SELECT 0 AS k, l.folder_id, l.content_hash FROM locations l "
            "JOIN messages m ON m.mid = l.mid AND m.content_hash = l.content_hash "
            "WHERE l.mid = ? UNION ALL "
            "SELECT 1, s.folder_id, s.content_hash FROM same_folder_copies s "
            "JOIN messages m ON m.mid = s.mid AND m.content_hash = s.content_hash "
            "WHERE s.mid = ?) ORDER BY k, folder_id LIMIT 1",
            (mid, mid),
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def refill_candidates(
        self, mid: str, folder_id: str, content_hash: str | None = None
    ) -> list[tuple[str, str]]:
        """The copies (folder_id, content_hash) a ``refill`` for
        ``remove_location(mid, folder_id, content_hash=...)`` may come from: every copy that
        remains, raw-derived hashes first, then by folder and hash. Empty when the removal
        leaves a copy holding the stored content (no refill needed) or leaves no location (the
        message is deleted)."""
        copies = [(loc.folder_id, loc.content_hash) for loc in self.locations(mid)]
        copies += self.same_folder_copies(mid)
        if content_hash is None:
            left = [c for c in copies if c[0] != folder_id]
        else:
            left = [c for c in copies if c != (folder_id, content_hash)]
        stored = self._conn.execute(
            "SELECT content_hash FROM messages WHERE mid = ?", (mid,)
        ).fetchone()
        if stored is None or not left:
            return []
        if stored[0] is not None and any(h == stored[0] for _, h in left):
            return []
        return sorted(set(left), key=_refill_order)

    def pending_refills(self) -> dict[str, list[tuple[str, str]]]:
        """Every mid whose content was cleared (``refill_pending``), mapped to its remaining
        copies (folder_id, content_hash) in :meth:`refill_candidates` order: the work a later job
        must do (fetch one copy, ``upsert_message`` it) so the message becomes searchable again."""
        out: dict[str, list[tuple[str, str]]] = {}
        for (mid,) in self._conn.execute(
            "SELECT mid FROM messages WHERE content_hash IS NULL ORDER BY mid"
        ).fetchall():
            copies = [(loc.folder_id, loc.content_hash) for loc in self.locations(mid)]
            out[mid] = sorted(set(copies + self.same_folder_copies(mid)), key=_refill_order)
        return out

    def thread(self, mid: str) -> list[str]:
        """The mids sharing ``mid``'s thread_id (``tern.index.threads``), oldest first (undated
        last, ties by mid); empty when ``mid`` is not indexed. The thread_id itself may be the
        mid of a message that is not indexed (JWZ step 4), so it is never looked up."""
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT mid FROM messages WHERE thread_id = "
                "(SELECT thread_id FROM messages WHERE mid = ?) "
                "ORDER BY date_utc IS NULL, date_utc, mid",
                (mid,),
            )
        ]

    def conflicted_mids(self) -> frozenset[str]:
        """Every mid the index marks ``conflicted`` (#5: copies with different content share
        the Message-ID). Few in practice; the ``tb_*`` tools mark and split them (plan 08c)."""
        return frozenset(
            r[0] for r in self._conn.execute("SELECT mid FROM messages WHERE conflicted = 1")
        )

    def folders(self) -> list[FolderRow]:
        return [
            FolderRow(
                folder_id=r[0],
                account_id=r[1],
                path=r[2],
                special_use=_tuple(r[3]),
                tb_total=r[4],
                last_reconciled_at=r[5],
            )
            for r in self._conn.execute(
                "SELECT folder_id, account_id, path, special_use, tb_total, last_reconciled_at "
                "FROM folders ORDER BY folder_id"
            )
        ]

    def status(self) -> StatusCounts:
        def one(sql: str) -> int:
            return int(self._conn.execute(sql).fetchone()[0])

        parse = dict(
            self._conn.execute(
                "SELECT parse_status, count(*) FROM messages GROUP BY parse_status"
            ).fetchall()
        )
        return StatusCounts(
            schema_version=schema.current_version(self._conn),
            messages=one("SELECT count(*) FROM messages"),
            locations=one("SELECT count(*) FROM locations"),
            same_folder_copies=one("SELECT count(*) FROM same_folder_copies"),
            conflicted=one("SELECT count(*) FROM messages WHERE conflicted = 1"),
            x_mids=one(f"SELECT count(*) FROM messages WHERE mid LIKE '{X_PREFIX}%'"),
            parse_ok=int(parse.get("ok", 0)),
            parse_partial=int(parse.get("partial", 0)),
            parse_failed=int(parse.get("failed", 0)),
            headers_only=one("SELECT count(*) FROM headers_only_items"),
            skipped_large_noid=one(
                "SELECT count(*) FROM skipped_items WHERE reason = 'large_noid'"
            ),
            skipped_tb_error=one("SELECT count(*) FROM skipped_items WHERE reason = 'tb_error'"),
            refill_pending=one("SELECT count(*) FROM messages WHERE content_hash IS NULL"),
            conflicts_unclassified=one(
                "SELECT count(*) FROM messages WHERE conflicted = 1 AND conflict_class IS NULL"
            ),
        )
