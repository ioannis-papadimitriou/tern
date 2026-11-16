"""Plan 04 step 4: chunking and FTS (`tern.index.chunks` + the Store writing chunks, #17/#30).

Fixture messages are written through the Store API by a test-local helper (the real ingest path
is step 7): mid/content_hash from `tern.index.mid`, fields from `tern.index.mime.parse_raw`, the
main text (quotes and signature stripped) as `body_text`. The expected FTS hits are computed with
the stdlib parser, independently of `tern.index.mime`.
"""

from __future__ import annotations

import email
import re
from collections.abc import Iterator
from email import policy
from pathlib import Path

import pytest
import test_fixture_manifest as tfm

from tern.index.chunks import BODY_CHUNK_WORDS, build_chunks, fold, split_body
from tern.index.mid import X_PREFIX, content_hash, mid_from_raw
from tern.index.mime import parse_raw
from tern.index.store import AttachmentRecord, LocationState, MessageRecord, Store

S0 = LocationState()
T0 = 1_753_783_200
INBOX = "acct1:/INBOX"
M1, M2 = "a" * 64, "b" * 64
H1, H2 = "1" * 64, "2" * 64
BODY_SENTINEL = "SENTINEL-BODY-1ea394"
QUOTED_CANARY = "CANARY-INJ-3"  # only in inj-quoted-reply's quoted part


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "data" / "index.db")
    yield s
    s.close()


def match(store: Store, query: str) -> list[tuple[str, str]]:
    """(mid, kind) of every chunk matching ``query``, sorted."""
    return sorted(
        store.connection.execute(
            "SELECT c.mid, c.kind FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ?",
            (query,),
        ).fetchall()
    )


def chunk_ids(store: Store, mid: str) -> list[int]:
    return [
        r[0]
        for r in store.connection.execute(
            "SELECT chunk_id FROM chunks WHERE mid = ? ORDER BY ord", (mid,)
        ).fetchall()
    ]


def chunk_rows(store: Store, mid: str) -> list[tuple[int, str, str, str]]:
    return store.connection.execute(
        "SELECT ord, kind, text, fts_text FROM chunks WHERE mid = ? ORDER BY ord", (mid,)
    ).fetchall()


def fts_ok(store: Store) -> None:
    store.connection.execute(
        "INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)"
    )


def put(store: Store, rec: MessageRecord, folder: str = INBOX) -> str:
    with store.transaction():
        outcome = store.upsert_message(rec)
        store.add_location(rec.mid, folder, rec.content_hash, S0, T0, source="backfill")
    return outcome


# ---- chunk layout -----------------------------------------------------------------------------


def test_2000_word_body_gives_exactly_6_body_chunks(store: Store) -> None:
    words = [f"w{i}" for i in range(2000)]
    seps = ["\n" if i % 17 == 16 else "\t" if i % 29 == 28 else " " for i in range(1999)]
    body = "".join(w + s for w, s in zip(words, [*seps, ""], strict=True))
    chunks = build_chunks(None, body, [])
    assert [c.kind for c in chunks] == ["body"] * 6
    assert [len(c.text.split()) for c in chunks] == [BODY_CHUNK_WORDS] * 5 + [50]
    assert [w for c in chunks for w in c.text.split()] == words
    assert "\n" in chunks[0].text  # the original span, line breaks kept
    put(store, MessageRecord(mid=M1, content_hash=H1, parse_status="ok", body_text=body))
    assert [r[1] for r in chunk_rows(store, M1)] == ["body"] * 6


def test_split_body_edges() -> None:
    assert split_body("") == split_body(" \n\t ") == []
    assert split_body("  one  two \n") == ["one  two"]
    assert split_body("a b c d e", 2) == ["a b", "c d", "e"]


def test_chunk_order_kinds_and_folding(store: Store) -> None:
    rec = MessageRecord(
        mid=M1,
        content_hash=H1,
        parse_status="ok",
        subject="Συνάντηση Τρίτη",
        body_text="Καλημέρα σας,\nτα λέμε.",
        attachments=(
            AttachmentRecord(filename="πρόγραμμα.pdf", mime="application/pdf", size=10),
            AttachmentRecord(filename=None, mime="image/png", size=3),
            AttachmentRecord(filename="  ", mime="image/png", size=3),
            AttachmentRecord(filename="café.txt", mime="text/plain", size=1),
        ),
    )
    put(store, rec)
    assert chunk_rows(store, M1) == [
        (0, "subject", "Συνάντηση Τρίτη", "Συναντηση Τριτη"),
        (1, "body", "Καλημέρα σας,\nτα λέμε.", "Καλημερα σας,\nτα λεμε."),
        (2, "attachment_name", "πρόγραμμα.pdf", "προγραμμα.pdf"),
        (3, "attachment_name", "café.txt", "cafe.txt"),
    ]
    assert build_chunks(None, None, []) == build_chunks("  ", "", [None]) == []


def test_fold() -> None:
    assert fold("καλημέρα") == "καλημερα"
    assert fold("ΚΑΛΗΜΈΡΑ") == "ΚΑΛΗΜΕΡΑ"
    assert fold("ϊΐ café Ångström") == "ιι cafe Angstrom"
    assert fold("é") == "e"  # decomposed input folds the same as precomposed


# ---- FTS -------------------------------------------------------------------------------------


def test_greek_query_without_tonos_matches_folded_chunk(store: Store) -> None:
    put(store, MessageRecord(mid=M1, content_hash=H1, parse_status="ok", body_text="Καλημέρα σας"))
    assert match(store, fold("καλημερα")) == [(M1, "body")]
    assert match(store, fold("καλημέρα")) == [(M1, "body")]
    assert chunk_rows(store, M1)[0][2] == "Καλημέρα σας"  # text stays unfolded


def _fixture_records() -> list[tuple[MessageRecord, str]]:
    man = tfm.load_manifest(tfm.FIXTURE)
    out: list[tuple[MessageRecord, str]] = []
    for m in man["messages"]:
        for c in m["copies"]:
            raw = tfm.read_copy(tfm.FIXTURE, man, c)
            mid, p = mid_from_raw(raw), parse_raw(raw)
            rec = MessageRecord(
                mid=mid,
                content_hash=content_hash(raw),
                parse_status=p.parse_status,
                header_message_id=None if mid.startswith(X_PREFIX) else p.header_message_id,
                date_utc=p.date_utc,
                from_addr=p.from_addr,
                from_name=p.from_name,
                to_addrs=p.to_addrs,
                cc_addrs=p.cc_addrs,
                subject=p.subject,
                in_reply_to=p.in_reply_to,
                refs=p.refs,
                body_text=p.main_text,
                attachments=tuple(
                    AttachmentRecord(filename=a.filename, mime=a.mime, size=a.size)
                    for a in p.attachments
                ),
            )
            out.append((rec, str(c["folder"])))
    return out


def _decoded_body(raw: bytes) -> str:
    """Every text part's decoded content, by the stdlib parser alone."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    return "\n".join(
        part.get_content() for part in msg.walk() if part.get_content_maintype() == "text"
    )


def test_fixture_body_sentinel_matches_exactly_its_messages_then_nothing_after_removal(
    store: Store,
) -> None:
    man = tfm.load_manifest(tfm.FIXTURE)
    expected = {
        mid_from_raw(raw)
        for m in man["messages"]
        for c in m["copies"]
        if BODY_SENTINEL in _decoded_body(raw := tfm.read_copy(tfm.FIXTURE, man, c))
    }
    assert len(expected) == 1
    quoted = {
        mid_from_raw(raw)
        for m in man["messages"]
        for c in m["copies"]
        if QUOTED_CANARY in _decoded_body(raw := tfm.read_copy(tfm.FIXTURE, man, c))
    }
    assert len(quoted) == 1
    records = _fixture_records()
    assert len(records) == 187
    for rec, folder in records:
        put(store, rec, folder)
    assert store.status().messages == 183
    token = BODY_SENTINEL.rsplit("-", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{6}", token)
    hits = match(store, token)
    assert {mid for mid, _ in hits} == expected
    assert {kind for _, kind in hits} == {"body"}
    # body_text is the main text: the quoted canary is stored nowhere in chunks (MessageRecord).
    assert match(store, '"canary inj 3"') == []
    assert match(store, '"spring sale"') != []  # the phrase query form does match
    fts_ok(store)

    (mid,) = expected
    with store.transaction():
        for loc in store.locations(mid):
            store.remove_location(mid, loc.folder_id, source="backfill")
    assert store.get_message(mid) is None
    assert match(store, token) == []
    assert chunk_rows(store, mid) == []
    fts_ok(store)


def test_reingest_replaces_chunks_and_a_kept_copy_leaves_them(store: Store) -> None:
    def rec(h: str, body: str) -> MessageRecord:
        return MessageRecord(mid=M1, content_hash=h, parse_status="ok", subject="s", body_text=body)

    assert put(store, rec(H1, "alpha words")) == "inserted"
    first = chunk_rows(store, M1)
    assert put(store, rec(H1, "alpha words")) == "replaced"
    assert chunk_rows(store, M1) == first
    assert len(store.connection.execute("SELECT * FROM chunks").fetchall()) == 2
    assert put(store, rec(H1, "bravo words")) == "replaced"
    assert match(store, "alpha") == [] and match(store, "bravo") == [(M1, "body")]
    # A differing copy while H1 is still held: kept out, its text never reaches FTS.
    with store.transaction():
        assert store.upsert_message(rec(H2, "charlie words")) == "kept"
    assert match(store, "charlie") == [] and match(store, "bravo") == [(M1, "body")]
    assert len(store.connection.execute("SELECT * FROM chunks").fetchall()) == 2
    fts_ok(store)


def test_unchanged_chunks_keep_their_ids_on_replace(store: Store) -> None:
    def rec(mid: str, subject: str, body: str) -> MessageRecord:
        return MessageRecord(
            mid=mid, content_hash=H1, parse_status="ok", subject=subject, body_text=body
        )

    put(store, rec(M1, "oldsubj", "alpha words"))
    put(store, rec(M2, "othersubj", "other words"))  # M1's chunks no longer hold the max rowid
    ids = chunk_ids(store, M1)
    assert put(store, rec(M1, "oldsubj", "alpha words")) == "replaced"
    assert chunk_ids(store, M1) == ids
    assert match(store, "oldsubj") == [(M1, "subject")]  # the token is found before the change
    # Same hash, changed subject (not hashed) or re-parsed text: rebuilt, new ids, FTS follows.
    assert put(store, rec(M1, "newsubj", "alpha words")) == "replaced"
    new_ids = chunk_ids(store, M1)
    assert set(new_ids).isdisjoint(ids) and len(new_ids) == 2
    assert [r[2] for r in chunk_rows(store, M1)] == ["newsubj", "alpha words"]
    assert match(store, "newsubj") == [(M1, "subject")] and match(store, "oldsubj") == []
    fts_ok(store)


def test_rolled_back_message_leaves_no_chunks(store: Store) -> None:
    rec = MessageRecord(mid=M1, content_hash=H1, parse_status="ok", body_text="delta words")
    with pytest.raises(RuntimeError, match="boom"), store.transaction():
        store.upsert_message(rec)
        raise RuntimeError("boom")
    assert match(store, "delta") == []
    assert store.connection.execute("SELECT count(*) FROM chunks").fetchone() == (0,)
