"""Index schema version and migrations (architecture §4, plan 04 step 2).

Migrations are ``migrations/NNN_<name>.sql`` files shipped inside the package and read through
``importlib.resources``; they are numbered 1..SCHEMA_VERSION with no gaps. ``meta.schema_version``
records the applied version (0 = an empty database).

- version > SCHEMA_VERSION → :class:`SchemaTooNew` (fail loudly, never downgrade);
- version < SCHEMA_VERSION → the pending migrations run, each in its own transaction, and
  ``schema_version`` is written inside that same transaction.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from importlib import resources

SCHEMA_VERSION = 1

_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class SchemaError(RuntimeError):
    """The index schema cannot be used as it is."""


class SchemaTooNew(SchemaError):
    """The index was written by a newer tern (its schema_version is higher than this code's)."""

    def __init__(self, found: int, supported: int = SCHEMA_VERSION) -> None:
        super().__init__(
            f"index schema_version {found} is newer than this tern supports ({supported}); "
            "upgrade tern, or recreate the index with `tern index rebuild`"
        )
        self.found = found
        self.supported = supported


class SchemaTooOld(SchemaError):
    """A read-only open found an index that still needs migrations (only a writer migrates)."""

    def __init__(self, found: int, supported: int = SCHEMA_VERSION) -> None:
        super().__init__(
            f"index schema_version {found} needs migrating to {supported}; "
            "start ternd once (it migrates on open)"
        )
        self.found = found
        self.supported = supported


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    """The packaged migrations in order; raises SchemaError unless they are exactly
    1..SCHEMA_VERSION."""
    root = resources.files("tern.index").joinpath("migrations")
    found: list[Migration] = []
    for entry in root.iterdir():
        m = _MIGRATION_NAME.match(entry.name)
        if m is None:
            continue
        found.append(Migration(int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort(key=lambda mig: mig.version)
    versions = [mig.version for mig in found]
    if versions != list(range(1, SCHEMA_VERSION + 1)):
        raise SchemaError(f"packaged migrations {versions} != 1..{SCHEMA_VERSION}")
    return found


def current_version(conn: sqlite3.Connection) -> int:
    """``meta.schema_version``; 0 for a database without a ``meta`` table."""
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if has_meta is None:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        raise SchemaError("index has a meta table but no schema_version")
    return int(row[0])


def check_version(conn: sqlite3.Connection) -> int:
    """The current version; raises SchemaTooNew if it is newer than this code supports."""
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise SchemaTooNew(version)
    return version


def migrate(
    conn: sqlite3.Connection,
    migrations: list[Migration] | None = None,
    *,
    target: int = SCHEMA_VERSION,
) -> int:
    """Apply every migration above the current version up to ``target``; returns the new
    version. ``conn`` must be in autocommit mode (``isolation_level=None``) with no open
    transaction. A failing migration is rolled back whole and re-raised."""
    if conn.in_transaction:
        raise SchemaError("migrate() needs a connection with no open transaction")
    pending = load_migrations() if migrations is None else migrations
    version = current_version(conn)
    if version > target:
        raise SchemaTooNew(version, target)
    for mig in pending:
        if mig.version <= version or mig.version > target:
            continue
        version = _apply(conn, mig, target)
    if version != target:
        raise SchemaError(f"migrations reached version {version}, expected {target}")
    return version


def _apply(conn: sqlite3.Connection, mig: Migration, target: int) -> int:
    """Run one migration in its own BEGIN IMMEDIATE transaction; returns the version after it.

    The version is re-read under the write lock, so a second writer that opened the same old
    database meanwhile skips what the first one already applied. ``executescript`` commits any
    open transaction first under legacy transaction control, so ``autocommit`` is switched to
    True for the duration (then ``executescript`` leaves our transaction open)."""
    previous = conn.autocommit
    conn.autocommit = True
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = current_version(conn)
            if version > target:
                raise SchemaTooNew(version, target)
            if version < mig.version:
                if mig.version != version + 1:
                    raise SchemaError(f"migration {mig.version} does not follow version {version}")
                conn.executescript(
                    mig.sql
                    + "\n;INSERT OR REPLACE INTO meta(key, value) "
                    + f"VALUES ('schema_version', '{mig.version}');"
                )
                version = mig.version
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.autocommit = previous
    return version
