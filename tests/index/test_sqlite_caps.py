"""Plan 04 step 2 (#17): the SQLite capabilities the index relies on exist in this environment.

FTS5 with external content and the unicode61 tokenizer, through the real schema; sqlite-vec at
the pinned version with ``vec_quantize_int8(v, 'unit')`` (the quantization #17 records).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import sqlite_vec

from tern.index.store import Store


def _match(conn: sqlite3.Connection, query: str) -> list[int]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rowid", (query,)
        )
    ]


def test_fts5_external_content_tokenizer_and_triggers(tmp_path: Path) -> None:
    with Store.open(tmp_path / "index.db") as s:
        conn = s.connection
        opts = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'chunks_fts'").fetchone()[0]
        assert "content='chunks'" in opts and "unicode61 remove_diacritics 2" in opts
        with s.transaction():
            conn.execute(
                "INSERT INTO chunks(chunk_id, mid, ord, kind, text, fts_text) VALUES "
                "(1, 'm', 0, 'body', 'Café crème', 'Café crème'), "
                "(2, 'm', 1, 'body', 'raw only', 'folded words')"
            )
        # unicode61 + remove_diacritics 2 folds Latin diacritics and case.
        assert _match(conn, "cafe") == [1]
        assert _match(conn, "CREME") == [1]
        # The index is built from fts_text, not text (#30).
        assert _match(conn, "folded") == [2]
        assert _match(conn, "raw") == []
        # External content: the FTS row reads its text back from chunks.
        hit = conn.execute(
            "SELECT chunks_fts.fts_text FROM chunks_fts WHERE chunks_fts MATCH 'folded'"
        ).fetchone()
        assert hit == ("folded words",)
        with s.transaction():
            conn.execute("UPDATE chunks SET fts_text = 'renamed' WHERE chunk_id = 2")
            conn.execute("DELETE FROM chunks WHERE chunk_id = 1")
        assert (_match(conn, "folded"), _match(conn, "renamed"), _match(conn, "cafe")) == (
            [],
            [2],
            [],
        )
        assert (
            conn.execute(
                "INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)"
            ).fetchall()
            == []
        )


def test_sqlite_vec_pinned_version_and_int8_unit_quantization() -> None:
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    assert conn.execute("SELECT vec_version()").fetchone()[0] == "v0.1.9"
    v = [1.0, -1.0, 0.5, 0.0, -0.25]
    q = f"vec_quantize_int8(vec_f32('{json.dumps(v)}'), 'unit')"
    vtype, values = conn.execute(f"SELECT vec_type({q}), vec_to_json({q})").fetchone()
    assert vtype == "int8"
    out = json.loads(values)
    # 'unit' maps [-1, 1] linearly onto [-128, 127]; v0.1.9 truncates in float32, so a value
    # may land one step below the exact (x + 1) * 127.5 - 128 (1.0 gives 126).
    assert len(out) == len(v)
    assert all(-128 <= q <= 127 for q in out)
    assert all(abs(q - ((x + 1) * 127.5 - 128)) <= 1 for q, x in zip(out, v, strict=True))
    # Order is kept: 1.0 > 0.5 > 0.0 > -0.25 > -1.0.
    assert out[0] > out[2] > out[3] > out[4] > out[1] == -128
