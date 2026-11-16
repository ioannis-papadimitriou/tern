"""Consistent private copies of Thunderbird's SQLite databases.

Thunderbird keeps its databases open with an **exclusive lock** while it runs:

* ``global-messages-db.sqlite`` (the GLODA mail index) uses a rollback journal;
* ``calendar-data/local.sqlite`` and ``cache.sqlite`` use WAL;
* ``abook.sqlite`` (address book) and ``history.sqlite`` (collected addresses)
  use WAL.

Because of the exclusive lock, another process cannot open them through SQLite
at all (``database is locked``), and ``immutable=1`` is unsafe: it ignores the
WAL (dropping recent calendar changes) and any hot journal (exposing a
half-written transaction).

So we copy at the file level instead:

1. ``stat`` the database and its ``-wal`` / ``-journal`` side files;
2. copy all of them into a private temp directory;
3. ``stat`` again — if anything changed during the copy, discard it and retry;
4. open the copy normally. SQLite then does its own crash recovery on it:
   committed WAL frames are replayed, a hot rollback journal is rolled back.
   ``PRAGMA quick_check`` must pass, and the result is folded into a single
   rollback-mode file that is published with an atomic rename.

The live files are only ever read. Every copy holds private mail or calendar
data, so the cache directory is ``0700`` and every file ``0600``.

In tern the copies live under tern's state dir (``TERN_HOME``-aware), set with
:func:`set_cache_dir` by ``tern.tools.builtin.tb`` before every call; upstream's default
``~/.cache/thunderbird-mcp`` is only the value before that. Upstream's snapshot CLI
(``python -m thunderbird_mcp.snapshot``) is not vendored.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from tern.tbmcp import config

log = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".cache" / "thunderbird-mcp"
SNAPSHOT = CACHE_DIR / "snapshot.sqlite"
# calendar-data/<name> -> snapshot file name in CACHE_DIR
CALENDAR_FILES = {"local.sqlite": "calendar-local.sqlite", "cache.sqlite": "calendar-cache.sqlite"}
# <profile>/<name> -> snapshot file name: the address book and collected addresses
CONTACT_FILES = {
    "abook.sqlite": "contacts-abook.sqlite",
    "history.sqlite": "contacts-history.sqlite",
}

_SIDE_SUFFIXES = ("-wal", "-journal")


class _ChangedDuringCopy(Exception):
    pass


class SnapshotError(RuntimeError):
    """No consistent copy could be made (upstream raised a plain ``RuntimeError``)."""


def set_cache_dir(path: Path) -> None:
    """Keep the snapshot copies in ``path`` (created ``0700`` on the first copy)."""
    global CACHE_DIR, SNAPSHOT
    if path != CACHE_DIR:
        CACHE_DIR = path
        SNAPSHOT = path / "snapshot.sqlite"
        _copied_from.clear()


def snapshot_path() -> Path:
    """Path to the local mail snapshot the read queries run against."""
    return SNAPSHOT


def calendar_snapshot_paths() -> list[Path]:
    """Existing calendar snapshots."""
    return [CACHE_DIR / n for n in CALENDAR_FILES.values() if (CACHE_DIR / n).exists()]


def contacts_snapshot_paths() -> list[Path]:
    """Existing address-book snapshots."""
    return [CACHE_DIR / n for n in CONTACT_FILES.values() if (CACHE_DIR / n).exists()]


def _age(path: Path) -> float:
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return float("inf")


def snapshot_age_seconds() -> float:
    """Age of the mail snapshot, or ``inf`` if there is none."""
    return _age(SNAPSHOT)


def _prepare_cache_dir() -> None:
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # not supported on every filesystem
        os.chmod(CACHE_DIR, 0o700)


# --- the consistent copy ---------------------------------------------------------


def _fingerprint(src: Path) -> tuple:
    out = []
    for p in (src, *(Path(f"{src}{s}") for s in _SIDE_SUFFIXES)):
        try:
            st = p.stat()
            out.append((p.name, st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            out.append((p.name, None, None))
    return tuple(out)


def _verify(con: sqlite3.Connection, label: str) -> None:
    """``quick_check`` every ordinary table.

    Virtual tables are skipped: recent SQLite versions (3.44+) also check FTS
    tables, which needs Thunderbird's ``mozporter`` tokenizer. Their underlying
    shadow tables are ordinary tables and are still checked.
    """
    tables = [
        name
        for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
        if not (sql or "").lstrip().upper().startswith("CREATE VIRTUAL")
    ]
    for name in tables:
        quoted = name.replace('"', '""')
        result = [r[0] for r in con.execute(f'PRAGMA quick_check("{quoted}")')]
        if result != ["ok"]:
            raise sqlite3.DatabaseError(f"copy of {label} failed quick_check: {result[:3]}")


def _copy_once(src: Path, dst: Path) -> None:
    work = Path(tempfile.mkdtemp(prefix="copy-", dir=CACHE_DIR))  # created 0700
    try:
        before = _fingerprint(src)
        copy = work / src.name
        for name, size, _ in before:
            if size is not None:
                shutil.copyfile(src.parent / name, work / name)
        if _fingerprint(src) != before:
            raise _ChangedDuringCopy(src.name)

        con = sqlite3.connect(copy)  # recovery (WAL replay / journal rollback) happens here
        try:
            _verify(con, src.name)
            con.execute("PRAGMA journal_mode=DELETE")  # fold any WAL into the main file
        finally:
            con.close()

        os.chmod(copy, 0o600)
        copy.replace(dst)  # atomic publish
    finally:
        shutil.rmtree(work, ignore_errors=True)


def copy_database(src: Path, dst: Path, retries: int = 5, backoff: float = 1.0) -> None:
    """Make ``dst`` a consistent private copy of the live database ``src``.

    Raises:
        FileNotFoundError: if ``src`` does not exist.
        SnapshotError: if no consistent copy could be made after all retries
            (e.g. Thunderbird kept writing throughout).
    """
    if not src.exists():
        raise FileNotFoundError(f"database not found at {src}")
    _prepare_cache_dir()
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            _copy_once(src, dst)
            return
        except (_ChangedDuringCopy, sqlite3.DatabaseError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise SnapshotError(
        f"Could not copy {src.name} after {retries} attempts: {type(last_err).__name__}"
    )


# --- mail ------------------------------------------------------------------------


def take_snapshot(retries: int = 5, backoff: float = 1.0) -> None:
    """Refresh the mail snapshot from the live GLODA index.

    Raises:
        FileNotFoundError: if the Thunderbird index cannot be located.
        SnapshotError: if no consistent copy could be made.
    """
    db_path = config.database_path()
    if not db_path.exists():
        raise FileNotFoundError(f"Thunderbird index not found at {db_path}")
    copy_database(db_path, SNAPSHOT, retries=retries, backoff=backoff)


# --- calendar --------------------------------------------------------------------


def _take_group(
    src_dir: Callable[[], Path], files: dict[str, str], retries: int, backoff: float
) -> list[Path]:
    try:
        base = src_dir()
    except FileNotFoundError:
        return []
    present = []
    for src_name, dst_name in files.items():
        src, dst = base / src_name, CACHE_DIR / dst_name
        if src.exists():
            copy_database(src, dst, retries=retries, backoff=backoff)
            present.append(dst)
        else:
            dst.unlink(missing_ok=True)
    return present


def take_calendar_snapshot(retries: int = 5, backoff: float = 1.0) -> list[Path]:
    """Refresh the calendar snapshots; returns the snapshots now present.

    A profile without calendar databases simply yields an empty list. A
    snapshot whose source has disappeared is removed.
    """
    _take_group(config.calendar_dir, CALENDAR_FILES, retries, backoff)
    return calendar_snapshot_paths()


def take_contacts_snapshot(retries: int = 5, backoff: float = 1.0) -> list[Path]:
    """Refresh the address-book snapshots; returns the snapshots now present."""
    _take_group(config.profile_dir, CONTACT_FILES, retries, backoff)
    return contacts_snapshot_paths()


# --- freshness -------------------------------------------------------------------

# One refresh at a time per kind within a process. Separate processes are safe
# too: each copies into its own temp directory and publishes by atomic rename.
_locks = {"mail": threading.Lock(), "calendar": threading.Lock(), "contacts": threading.Lock()}

#: Per kind: the fingerprint (size and mtime of every source file and its side files) the
#: last successful copy was taken from. tern change: upstream refreshed on age (900 s, in the
#: background, the stale copy serving meanwhile), which hid new mail for 15 minutes and more.
_copied_from: dict[str, tuple] = {}


def _sources(kind: str) -> list[Path]:
    """The live databases a kind is copied from (present or not)."""
    if kind == "mail":
        return [config.database_path()]
    base_dir, files = (
        (config.calendar_dir, CALENDAR_FILES)
        if kind == "calendar"
        else (config.profile_dir, CONTACT_FILES)
    )
    try:
        base = base_dir()
    except FileNotFoundError:
        return []
    return [base / name for name in files]


def _ensure(kind: str, take: Callable[[], object], have: Callable[[], bool]) -> None:
    """Copy again whenever a source changed since the last copy (or there is no copy yet). The
    caller waits for the copy; a failed copy raises and nothing stale is served."""
    with _locks[kind]:
        fingerprint = tuple(_fingerprint(src) for src in _sources(kind))
        if _copied_from.get(kind) == fingerprint and have():
            return
        _copied_from.pop(kind, None)
        take()
        # taken before the copy: a change during the copy makes the next call copy again
        _copied_from[kind] = fingerprint


def ensure_fresh() -> None:
    """Make sure the mail snapshot matches the live GLODA index (copied again when it changed)."""
    _ensure("mail", take_snapshot, SNAPSHOT.exists)


def ensure_calendar_fresh() -> None:
    """Calendar counterpart of :func:`ensure_fresh`. A profile with no calendars is copied once
    and again only when a calendar database appears."""
    _ensure("calendar", take_calendar_snapshot, lambda: True)


def ensure_contacts_fresh() -> None:
    """Address-book counterpart of :func:`ensure_calendar_fresh`."""
    _ensure("contacts", take_contacts_snapshot, lambda: True)
