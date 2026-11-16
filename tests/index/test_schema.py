"""Plan 04 step 2: schema version, migrations, and architecture §4 kept equal to 001_init.sql."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

from tern.index import schema
from tern.index.schema import Migration, SchemaTooNew
from tern.index.store import Store

ARCH = Path(__file__).resolve().parents[2] / "docs" / "architecture.md"

TABLES = {
    "meta",
    "accounts",
    "folders",
    "messages",
    "locations",
    "same_folder_copies",
    "sent_recipients",
    "sent_counted",
    "attachments",
    "chunks",
    "chunks_fts",
    "embedding_model",
    "ingest_state",
    "headers_only_items",
    "skipped_items",
    "ingest_runs",
}


def _conn(path: Path | str = ":memory:") -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def _normalized_sql(sql: str) -> str:
    """CREATE statement text without ``--`` comments, whitespace collapsed (none of these
    statements has ``--`` or a quote containing one inside a literal)."""
    text = re.sub(r"--[^\n]*", "", sql)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s*([(),])\s*", r"\1", text)


def _objects(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[str, list[tuple[object, ...]]]]:
    """(type, name) → (normalized CREATE text, columns) for every user table, virtual table,
    index and trigger, without FTS5 and vec0 shadow tables. The text carries CHECKs, FTS5
    options, index column lists and trigger bodies; columns come from ``table_xinfo`` (tables)
    or ``index_xinfo`` (indexes)."""
    out: dict[tuple[str, str], tuple[str, list[tuple[object, ...]]]] = {}
    for kind, name, sql in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        if kind == "table" and re.match(r"^chunks_(fts|vec)_", name):
            continue  # shadow tables (the chunks_fts_a[idu] triggers are kept)
        pragma = {"table": "table_xinfo", "index": "index_xinfo"}.get(kind)
        cols = conn.execute(f"PRAGMA {pragma}({name})").fetchall() if pragma else []
        out[(kind, name)] = (_normalized_sql(sql), cols)
    return out


def test_packaged_migrations_are_1_to_schema_version() -> None:
    migs = schema.load_migrations()
    assert [m.version for m in migs] == list(range(1, schema.SCHEMA_VERSION + 1))
    assert migs[0].name == "001_init.sql"


def test_fresh_database_migrates_to_v1_with_every_table() -> None:
    conn = _conn()
    assert schema.current_version(conn) == 0
    assert schema.migrate(conn) == schema.SCHEMA_VERSION == 1
    assert schema.current_version(conn) == 1
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {n for n in names if not re.match(r"^chunks_fts_", n)} == TABLES
    # No chunks_vec here (plan 05, M9); no TB-issued id column anywhere (#5).
    cols = {c[1] for t in TABLES for c in conn.execute(f"PRAGMA table_info({t})")}
    assert not {c for c in cols if "tb_id" in c or "cursor" in c or "tb_hmid" in c}
    assert schema.migrate(conn) == 1  # idempotent


def test_newer_schema_is_refused_naming_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "data" / "index.db"
    Store.open(path).close()
    conn = _conn(path)
    conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    conn.close()
    for readonly in (False, True):
        with pytest.raises(SchemaTooNew, match="tern index rebuild") as err:
            Store.open(path, readonly=readonly)
        assert err.value.found == 2


def test_older_schema_runs_only_the_pending_migrations() -> None:
    conn = _conn()
    v1 = schema.load_migrations()
    schema.migrate(conn)
    conn.execute("INSERT INTO accounts(account_id, name, type) VALUES ('a', 'n', 'imap')")
    extra = Migration(2, "002_test.sql", "CREATE TABLE extra(x INTEGER);")
    assert schema.migrate(conn, [*v1, extra], target=2) == 2
    assert schema.current_version(conn) == 2
    assert conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1  # 001 not re-run
    assert conn.execute("SELECT count(*) FROM extra").fetchone()[0] == 0
    with pytest.raises(SchemaTooNew):
        schema.migrate(conn)  # code at v1, database at v2


def test_failing_migration_rolls_back_whole() -> None:
    conn = _conn()
    schema.migrate(conn)
    bad = Migration(2, "002_bad.sql", "CREATE TABLE half(x INTEGER); INSERT INTO nope VALUES (1);")
    with pytest.raises(sqlite3.OperationalError):
        schema.migrate(conn, [*schema.load_migrations(), bad], target=2)
    assert not conn.in_transaction
    assert schema.current_version(conn) == 1
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'half'").fetchone() is None


def test_architecture_section_4_matches_001_init() -> None:
    """The §4 SQL block is the schema: executed as written (chunks_vec at a test dim), it must
    give exactly 001_init.sql's objects, plus chunks_vec: the same names, columns, index
    columns and CREATE text (CHECKs, FTS5 options, trigger bodies) up to comments and
    whitespace."""
    text = ARCH.read_text(encoding="utf-8")
    section = text.split("## 4. Index schema v1", 1)[1].split("\n## 5.", 1)[0]
    block = section.split("```sql\n", 1)[1].split("```", 1)[0]
    doc = _conn()
    doc.enable_load_extension(True)
    sqlite_vec.load(doc)
    doc.enable_load_extension(False)
    doc.executescript(block.replace("{dim}", "4"))
    db = _conn()
    schema.migrate(db)
    doc_objects = _objects(doc)
    triggers = {name for kind, name in doc_objects if kind == "trigger"}
    assert triggers == {"chunks_fts_ai", "chunks_fts_ad", "chunks_fts_au"}
    vec_sql, _ = doc_objects.pop(("table", "chunks_vec"))  # §4 keeps it; plan 05 creates it (M9)
    assert "USING vec0(" in vec_sql
    assert doc_objects == _objects(db)


def test_concurrent_first_open_applies_each_migration_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two writers open the same fresh file: B migrates after A read version 0 but before A
    took the write lock. A must re-read the version under the lock and skip 001, not re-run it
    ("table meta already exists")."""
    path = tmp_path / "index.db"
    a, b = _conn(path), _conn(path)
    real = schema.current_version
    raced: list[int] = []

    def racing(conn: sqlite3.Connection) -> int:
        version = real(conn)
        if conn is a and not raced:
            raced.append(version)
            assert schema.migrate(b) == 1
        return version

    monkeypatch.setattr(schema, "current_version", racing)
    assert schema.migrate(a) == 1
    assert raced == [0]
    assert real(a) == 1
    assert not a.in_transaction
    assert a.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL  # restored after migrating
