"""Plan 04 step 6: conflict detection, spoof re-fill and same-folder copies at the Store level
(D1, T14; #5, #30), plus `tern.index.conflicts.classify_pair`.

Fixture copies are parsed by a test-local helper (the real ingest path is step 7): mid and
content_hash from `tern.index.mid`, fields from `tern.index.mime.parse_raw`, the main text as
`body_text`. The spoof-only and genuine-only tokens are computed here from the two copies'
extracted text, so no expectation is typed in by hand except the plan's named strings.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm

from tern.index.chunks import fold
from tern.index.conflicts import BENIGN, CLASSES, classify_pair, worst
from tern.index.mid import X_PREFIX, content_hash, mid_from_raw
from tern.index.mime import parse_raw
from tern.index.store import (
    AttachmentRecord,
    LocationState,
    MessageRecord,
    Store,
    StoreError,
)

S0 = LocationState()
T0 = 1_753_783_200
INBOX, INVOICES = "acct1/INBOX", "acct1/Invoices"
GENUINE_FROM = "billing@acme.example.com"
GENUINE_IBAN = "GR00 0110 1250 0000 0001 2300 695"
_TOKEN = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class Copy:
    key: str
    folder: str
    raw: bytes
    rec: MessageRecord

    @property
    def mid(self) -> str:
        return self.rec.mid

    @property
    def hash(self) -> str:
        return self.rec.content_hash


def record_of(raw: bytes) -> MessageRecord:
    mid, p = mid_from_raw(raw), parse_raw(raw)
    return MessageRecord(
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
            AttachmentRecord(filename=a.filename, mime=a.mime, size=a.size) for a in p.attachments
        ),
    )


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return tfm.load_manifest(tfm.FIXTURE)


def copies_of(man: dict[str, Any], key: str) -> list[Copy]:
    out = []
    for c in tfm.messages(man)[key]["copies"]:
        raw = tfm.read_copy(tfm.FIXTURE, man, c)
        out.append(Copy(key, str(c["folder"]), raw, record_of(raw)))
    return out


@pytest.fixture(scope="module")
def inv(manifest: dict[str, Any]) -> dict[str, Copy]:
    """inv-0077's two copies by variant: 'genuine' (acct1/Invoices), 'spoof' (acct1/INBOX)."""
    out = {}
    for c in tfm.messages(manifest)["inv-0077"]["copies"]:
        raw = tfm.read_copy(tfm.FIXTURE, manifest, c)
        out[str(c["variant"])] = Copy("inv-0077", str(c["folder"]), raw, record_of(raw))
    assert (out["genuine"].folder, out["spoof"].folder) == (INVOICES, INBOX)
    assert out["genuine"].mid == out["spoof"].mid
    assert out["genuine"].hash != out["spoof"].hash
    return out


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "data" / "index.db")
    yield s
    s.close()


def put(
    store: Store,
    c: Copy,
    folder: str | None = None,
    *,
    content_raw: bytes | None = None,
) -> str:
    """One copy in one transaction, as the ingest will write it; with ``content_raw`` the raw
    pair (this copy, the content copy) is passed for classification."""
    with store.transaction():
        outcome = store.upsert_message(c.rec)
        store.add_location(
            c.mid,
            folder or c.folder,
            c.hash,
            S0,
            T0,
            source="backfill",
            raw=None if content_raw is None else c.raw,
            content_raw=content_raw,
        )
    return outcome


def tokens(rec: MessageRecord) -> set[str]:
    """FTS-like tokens (unicode61: letters/digits, folded, lower-cased) of what is chunked."""
    text = " ".join(
        [rec.subject or "", rec.body_text or ""] + [a.filename or "" for a in rec.attachments]
    )
    return {t.lower() for t in _TOKEN.findall(fold(text))}


def match(store: Store, token: str) -> list[str]:
    """The distinct mids of the chunks matching one token (quoted as an FTS string)."""
    return sorted(
        {
            r[0]
            for r in store.connection.execute(
                "SELECT c.mid FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.rowid "
                "WHERE chunks_fts MATCH ?",
                ('"' + token + '"',),
            )
        }
    )


def fts_ok(store: Store) -> None:
    store.connection.execute(
        "INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)"
    )


def row(store: Store, mid: str) -> Any:
    r = store.get_message(mid)
    assert r is not None
    return r


# ---- inv-0077: spoof and genuine in two folders ------------------------------------------------


def test_spoof_first_then_genuine_conflicts_and_removing_the_spoof_refills_genuine(
    store: Store, inv: dict[str, Copy]
) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    mid = spoof.mid
    spoof_only = tokens(spoof.rec) - tokens(genuine.rec)
    assert "0260" in spoof_only and len(spoof_only) >= 5

    assert put(store, spoof) == "inserted"
    assert store.content_copy(mid) == (INBOX, spoof.hash)
    assert put(store, genuine, content_raw=spoof.raw) == "kept"
    r = row(store, mid)
    assert (r.conflicted, r.conflict_class, r.content_hash) == (True, "other", spoof.hash)
    assert store.status().conflicted == 1
    assert match(store, "0260") == [mid]  # the spoof's content is what is indexed so far

    assert store.refill_candidates(mid, INBOX) == [(INVOICES, genuine.hash)]
    store.remove_location(mid, INBOX, source="event", refill=genuine.rec)

    r = row(store, mid)
    assert (r.conflicted, r.conflict_class) == (False, None)
    assert r.from_addr == GENUINE_FROM
    assert GENUINE_IBAN in (r.body_text or "")
    assert r.content_hash == genuine.hash
    assert [loc.folder_id for loc in store.locations(mid)] == [INVOICES]
    for tok in sorted(spoof_only):
        assert match(store, tok) == [], tok
    assert match(store, "0110") == [mid]
    assert store.status().refill_pending == 0
    fts_ok(store)


@pytest.mark.parametrize("first", ["spoof", "genuine"])
@pytest.mark.parametrize("removed", ["spoof", "genuine"])
def test_removing_either_copy_leaves_exactly_the_other_copys_content(
    store: Store, inv: dict[str, Copy], first: str, removed: str
) -> None:
    second = "genuine" if first == "spoof" else "spoof"
    kept = "genuine" if removed == "spoof" else "spoof"
    gone, left = inv[removed], inv[kept]
    put(store, inv[first])
    put(store, inv[second])
    assert row(store, left.mid).conflicted
    store.remove_location(gone.mid, gone.folder, source="event", refill=left.rec)
    r = row(store, left.mid)
    assert (r.conflicted, r.content_hash, r.from_addr) == (False, left.hash, left.rec.from_addr)
    assert r.body_text == left.rec.body_text
    assert len(store.locations(left.mid)) == 1
    for tok in sorted(tokens(gone.rec) - tokens(left.rec)):
        assert match(store, tok) == [], tok
    for tok in sorted(tokens(left.rec) - tokens(gone.rec)):
        assert match(store, tok) == [left.mid], tok
    fts_ok(store)


def test_removing_the_genuine_instead_leaves_the_spoof_body_and_one_location(
    store: Store, inv: dict[str, Copy]
) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    put(store, spoof)
    put(store, genuine)
    assert store.refill_candidates(genuine.mid, INVOICES) == []  # the spoof holds the content
    store.remove_location(genuine.mid, INVOICES, source="event")
    r = row(store, spoof.mid)
    assert (r.body_text, r.conflicted) == (spoof.rec.body_text, False)
    assert [loc.folder_id for loc in store.locations(spoof.mid)] == [INBOX]


def test_removal_without_a_refill_clears_the_stale_content_until_a_copy_is_upserted(
    store: Store, inv: dict[str, Copy]
) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    mid = spoof.mid
    put(store, spoof)
    put(store, genuine)
    store.remove_location(mid, INBOX, source="event")
    r = row(store, mid)
    assert (r.content_hash, r.body_text, r.from_addr, r.subject, r.parse_status) == (
        None,
        None,
        None,
        None,
        None,
    )
    assert not r.conflicted
    assert store.connection.execute(
        "SELECT count(*) FROM chunks WHERE mid = ?", (mid,)
    ).fetchone() == (0,)
    for tok in sorted(tokens(spoof.rec)):
        assert match(store, tok) == [], tok
    assert store.status().refill_pending == 1
    assert store.pending_refills() == {mid: [(INVOICES, genuine.hash)]}  # the work left to do
    assert store.content_copy(mid) is None
    assert store.refill_candidates(mid, INVOICES) == []  # removing it deletes the message
    # The next upsert of the remaining copy fills the row again.
    assert store.upsert_message(genuine.rec) == "replaced"
    assert match(store, "0110") == [mid]
    assert (store.status().refill_pending, row(store, mid).from_addr) == (0, GENUINE_FROM)
    assert store.pending_refills() == {}


def test_a_wrong_refill_raises_and_nothing_is_applied(store: Store, inv: dict[str, Copy]) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    put(store, spoof)
    put(store, genuine)
    with pytest.raises(StoreError):  # the removed copy itself is no remaining copy
        store.remove_location(spoof.mid, INBOX, source="event", refill=spoof.rec)
    other = MessageRecord(mid="d" * 64, content_hash=genuine.hash, parse_status="ok")
    with pytest.raises(ValueError):
        store.remove_location(spoof.mid, INBOX, source="event", refill=other)
    with pytest.raises(ValueError):
        store.remove_folder(INBOX, source="event", refills={spoof.mid: other})
    assert len(store.locations(spoof.mid)) == 2
    assert (row(store, spoof.mid).content_hash, match(store, "0260")) == (
        spoof.hash,
        [spoof.mid],
    )


def test_remove_folder_refills_from_a_remaining_copy(store: Store, inv: dict[str, Copy]) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    put(store, spoof)
    put(store, genuine)
    assert store.remove_folder(INBOX, source="reconcile", refills={spoof.mid: genuine.rec}) == 1
    r = row(store, spoof.mid)
    assert (r.from_addr, r.conflicted, match(store, "0260")) == (GENUINE_FROM, False, [])


def test_remove_folder_rejects_a_refill_for_a_mid_not_in_the_folder(
    store: Store, inv: dict[str, Copy], manifest: dict[str, Any]
) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    dup = copies_of(manifest, "dup-1")[0]
    put(store, spoof)
    put(store, genuine)
    put(store, dup, INVOICES)
    with pytest.raises(ValueError):  # keyed by the wrong mid: would silently take the clear path
        store.remove_folder(INBOX, source="reconcile", refills={dup.mid: dup.rec})
    assert len(store.locations(spoof.mid)) == 2
    assert (row(store, spoof.mid).content_hash, match(store, "0260")) == (spoof.hash, [spoof.mid])


def test_remove_folder_counts_an_unneeded_refill(
    store: Store, inv: dict[str, Copy], caplog: pytest.LogCaptureFixture
) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    put(store, spoof)
    put(store, genuine)
    caplog.set_level(logging.INFO, logger="tern")
    # The spoof in INBOX holds the content, so removing Invoices needs no refill; the refill is
    # a remaining copy (valid), so it is ignored and counted.
    store.remove_folder(INVOICES, source="reconcile", refills={spoof.mid: spoof.rec})
    assert row(store, spoof.mid).content_hash == spoof.hash
    (rec,) = [r for r in caplog.records if r.getMessage() == "folder removed"]
    assert (vars(rec)["refills"], vars(rec)["refills_unused"]) == (1, 1)


def test_remove_folder_rejects_a_refill_that_is_no_remaining_copy(
    store: Store, inv: dict[str, Copy]
) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    put(store, spoof)
    put(store, genuine)
    # Not needed (the spoof in INBOX keeps the content) and not remaining (the genuine is in
    # the removed folder): a wrong record, refused like in remove_location.
    with pytest.raises(StoreError):
        store.remove_folder(INVOICES, source="reconcile", refills={spoof.mid: genuine.rec})
    assert len(store.locations(spoof.mid)) == 2
    assert row(store, spoof.mid).content_hash == spoof.hash
    store.remove_folder(INVOICES, source="reconcile")
    # No location left: no copy remains, so any refill is refused and nothing is applied.
    with pytest.raises(StoreError):
        store.remove_folder(INBOX, source="reconcile", refills={spoof.mid: spoof.rec})
    assert [loc.folder_id for loc in store.locations(spoof.mid)] == [INBOX]


# ---- same folder ---------------------------------------------------------------------------


def test_same_folder_spoof_conflicts_and_either_copy_can_go(
    store: Store, inv: dict[str, Copy]
) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    mid = genuine.mid
    put(store, genuine)
    put(store, spoof, INVOICES, content_raw=genuine.raw)
    r = row(store, mid)
    assert (r.conflicted, r.conflict_class, r.content_hash) == (True, "other", genuine.hash)
    assert store.status().conflicts_unclassified == 0
    assert [loc.content_hash for loc in store.locations(mid)] == [genuine.hash]
    assert store.same_folder_copies(mid) == [(INVOICES, spoof.hash)]
    assert match(store, "0260") == []  # never overwritten silently by the same-folder spoof
    # The genuine copy goes: the spoof copy is promoted and the content re-filled from it.
    assert store.refill_candidates(mid, INVOICES, genuine.hash) == [(INVOICES, spoof.hash)]
    store.remove_location(
        mid, INVOICES, content_hash=genuine.hash, source="event", refill=spoof.rec
    )
    r = row(store, mid)
    assert (r.conflicted, r.content_hash, r.body_text) == (False, spoof.hash, spoof.rec.body_text)
    assert [loc.content_hash for loc in store.locations(mid)] == [spoof.hash]
    assert (store.same_folder_copies(mid), match(store, "0110"), match(store, "0260")) == (
        [],
        [],
        [mid],
    )


def test_same_folder_spoof_removed_keeps_the_genuine(store: Store, inv: dict[str, Copy]) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    put(store, genuine)
    put(store, spoof, INVOICES)
    assert row(store, genuine.mid).conflict_class is None  # detected; no raw pair, no class
    assert store.status().conflicts_unclassified == 1  # ... and the gap is visible
    store.remove_location(genuine.mid, INVOICES, content_hash=spoof.hash, source="event")
    r = row(store, genuine.mid)
    assert (r.conflicted, r.from_addr, store.same_folder_copies(genuine.mid)) == (
        False,
        GENUINE_FROM,
        [],
    )


def test_byte_identical_same_folder_copy_collapses(store: Store, inv: dict[str, Copy]) -> None:
    genuine = inv["genuine"]
    put(store, genuine)
    put(store, genuine, content_raw=genuine.raw)  # no error; nothing to compare
    assert (len(store.locations(genuine.mid)), store.same_folder_copies(genuine.mid)) == (1, [])
    r = row(store, genuine.mid)
    assert (r.conflicted, r.conflict_class) == (False, None)
    assert store.status().locations == 1


def test_refill_candidates_put_raw_derived_copies_before_full_ones(store: Store) -> None:
    mid = "a" * 64
    content, full, raw2 = "0" * 64, "full-" + "1" * 64, "2" * 64
    store.upsert_message(MessageRecord(mid=mid, content_hash=content, parse_status="ok"))
    # The full- copy sits in the alphabetically first folder: only the kind can put it last.
    for folder, h in (("acct1/F0", content), ("acct1/A", full), ("acct1/B", raw2)):
        store.add_location(mid, folder, h, S0, T0, source="backfill")
    expected = [("acct1/B", raw2), ("acct1/A", full)]
    assert store.refill_candidates(mid, "acct1/F0") == expected
    store.remove_location(mid, "acct1/F0", source="event")
    assert store.pending_refills() == {mid: expected}


def test_content_copy_falls_back_to_a_same_folder_copy(store: Store, inv: dict[str, Copy]) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    put(store, spoof)  # INBOX: the spoof fills the row
    put(store, genuine)  # Invoices location row: genuine
    put(store, spoof, INVOICES)  # Invoices same-folder copy: spoof
    # INBOX goes: the content (spoof) is now held only by the same-folder copy in Invoices.
    assert store.refill_candidates(spoof.mid, INBOX) == []
    store.remove_location(spoof.mid, INBOX, source="event")
    r = row(store, spoof.mid)
    assert (r.content_hash, r.conflicted) == (spoof.hash, True)
    assert [loc.content_hash for loc in store.locations(spoof.mid)] == [genuine.hash]
    assert store.content_copy(spoof.mid) == (INVOICES, spoof.hash)


# ---- benign duplicates -----------------------------------------------------------------------


@pytest.mark.parametrize("key", ["dup-1", "dup-2", "norm-pair"])
def test_benign_duplicates_do_not_conflict_and_vanish_with_their_last_location(
    store: Store, manifest: dict[str, Any], key: str
) -> None:
    copies = copies_of(manifest, key)
    assert len(copies) == 2 and copies[0].mid == copies[1].mid
    mid = copies[0].mid
    gone: list[str] = []
    store.message_removed_observers.append(gone.append)
    put(store, copies[0])
    put(store, copies[1], content_raw=copies[0].raw)
    assert (row(store, mid).conflicted, len(store.locations(mid))) == (False, 2)
    toks = sorted(tokens(copies[0].rec))
    assert toks and all(match(store, t) == [mid] for t in toks)
    for c in copies:
        store.remove_location(mid, c.folder, source="event")
    assert (store.get_message(mid), gone) == (None, [mid])
    for tok in toks:
        assert match(store, tok) == [], tok
    assert store.connection.execute("SELECT count(*) FROM chunks_fts").fetchone() == (0,)
    fts_ok(store)


# ---- classification ----------------------------------------------------------------------------

_HEAD = b"From: A <a@example.com>\nDate: Wed, 29 Jul 2026 10:00:00 +0000\nMessage-ID: <c@x>\n"


def _msg(body: bytes, head: bytes = _HEAD, eol: bytes = b"\n") -> bytes:
    return (head + b"\n" + body).replace(b"\n", eol)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (_msg(b"hi\n"), _msg(b"hi\n", _HEAD + b"X-Mozilla-Status: 0001\n"), "identical"),
        (_msg(b"hi\nthere\n"), _msg(b"hi\nthere\n", eol=b"\r\n"), "crlf"),
        (_msg(b"x\n>From here\n"), _msg(b"x\nFrom here\n"), "from_quote"),
        (_msg(b"x\n>From here\n", eol=b"\r\n"), _msg(b"x\nFrom here\n"), "from_quote"),
        (_msg(b"hello\n"), _msg(b"hello\n-- \nsent from a list\n"), "footer"),
        (_msg(b"hello"), _msg(b"hello\nmore\n"), "footer"),
        (_msg(b"hello\n"), _msg(b"hellO\n"), "other"),
        (_msg(b"hello\n"), _msg(b"hello\n", _HEAD.replace(b"10:00:00", b"10:00:01")), "date_from"),
        (_msg(b"hello\n"), _msg(b"hello\n", _HEAD.replace(b"a@ex", b"b@ex")), "date_from"),
        (_msg(b"hello\n"), _msg(b"bye\n", _HEAD.replace(b"a@ex", b"b@ex")), "other"),
    ],
)
def test_classify_pair_cases(a: bytes, b: bytes, expected: str) -> None:
    assert classify_pair(a, b) == expected
    assert classify_pair(b, a) == expected
    assert (expected in BENIGN) == (content_hash(a) == content_hash(b))


def test_classify_pair_agrees_with_content_hash_on_every_fixture_pair(
    manifest: dict[str, Any],
) -> None:
    by_mid: dict[str, list[bytes]] = {}
    for m in manifest["messages"]:
        for c in m["copies"]:
            raw = tfm.read_copy(tfm.FIXTURE, manifest, c)
            by_mid.setdefault(mid_from_raw(raw), []).append(raw)
    classes = {}
    for mid, raws in by_mid.items():
        for i, a in enumerate(raws):
            for b in raws[i + 1 :]:
                cls = classify_pair(a, b)
                assert (cls in BENIGN) == (content_hash(a) == content_hash(b))
                classes[mid] = cls
    inv_mid = tfm.mid_of("<inv-0077@acme.example.com>")
    assert classes.pop(inv_mid) == "other"
    assert len(classes) == 3 and set(classes.values()) <= BENIGN  # dup-1, dup-2, norm-pair


def test_worst_class() -> None:
    assert worst(["footer", "other", "date_from"]) == "other"
    assert worst(["identical", "footer"]) == "footer"
    assert CLASSES[0] == "identical" and CLASSES[-1] == "other"


def test_conflict_class_is_the_worst_pair_and_clears_with_the_conflict(store: Store) -> None:
    base = _msg(b"hello\n")
    footer = _msg(b"hello\n-- \nlist footer\n")
    other = _msg(b"something else\n")
    footer2 = _msg(b"hello\n-- \nanother footer\n")
    raws = [base, footer, other, footer2]
    copies = [Copy("c", f"acct1/F{i}", raw, record_of(raw)) for i, raw in enumerate(raws)]
    put(store, copies[0])
    put(store, copies[1], content_raw=base)
    mid = copies[0].mid
    assert row(store, mid).conflict_class == "footer"
    put(store, copies[2], content_raw=base)
    assert row(store, mid).conflict_class == "other"
    put(store, copies[3], content_raw=base)  # a milder pair never lowers the class
    assert row(store, mid).conflict_class == "other"
    for c in (copies[2], copies[3]):
        store.remove_location(mid, c.folder, source="event")
    assert row(store, mid).conflict_class == "other"  # the worst seen while conflicted
    store.remove_location(mid, copies[1].folder, source="event")
    assert (row(store, mid).conflicted, row(store, mid).conflict_class) == (False, None)


def test_raw_pair_must_match_the_hashes(store: Store, inv: dict[str, Copy]) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    put(store, spoof)
    with pytest.raises(ValueError):  # raw and content_raw go together
        store.add_location(
            genuine.mid, INVOICES, genuine.hash, S0, T0, source="backfill", raw=genuine.raw
        )
    with pytest.raises(ValueError):  # content_raw is not the content copy's
        store.add_location(
            genuine.mid,
            INVOICES,
            genuine.hash,
            S0,
            T0,
            source="backfill",
            raw=genuine.raw,
            content_raw=genuine.raw,
        )
    with pytest.raises(ValueError):  # raw is not this copy's
        store.add_location(
            genuine.mid,
            INVOICES,
            genuine.hash,
            S0,
            T0,
            source="backfill",
            raw=spoof.raw,
            content_raw=spoof.raw,
        )
    assert [loc.folder_id for loc in store.locations(spoof.mid)] == [INBOX]


def test_a_wrong_content_raw_raises_even_when_nothing_is_compared(
    store: Store, inv: dict[str, Copy]
) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    other = "acct1/Archive"
    put(store, spoof)
    with pytest.raises(ValueError):  # this copy is the content copy, but content_raw is not it
        store.add_location(
            spoof.mid,
            other,
            spoof.hash,
            S0,
            T0,
            source="backfill",
            raw=spoof.raw,
            content_raw=genuine.raw,
        )
    assert [loc.folder_id for loc in store.locations(spoof.mid)] == [INBOX]
    # The right pair for the content copy itself is accepted, with nothing to classify.
    store.add_location(
        spoof.mid,
        other,
        spoof.hash,
        S0,
        T0,
        source="backfill",
        raw=spoof.raw,
        content_raw=spoof.raw,
    )
    assert row(store, spoof.mid).conflict_class is None
    # A cleared row (refill pending) has no content copy: any content_raw is wrong.
    put(store, genuine)
    for folder in (INBOX, other):
        store.remove_location(spoof.mid, folder, source="event")
    assert row(store, spoof.mid).content_hash is None
    with pytest.raises(ValueError):
        store.add_location(
            genuine.mid,
            other,
            genuine.hash,
            S0,
            T0,
            source="backfill",
            raw=genuine.raw,
            content_raw=spoof.raw,
        )
    assert [loc.folder_id for loc in store.locations(genuine.mid)] == [INVOICES]


def test_an_unneeded_refill_must_still_be_a_remaining_copy(
    store: Store, inv: dict[str, Copy]
) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    put(store, spoof)
    put(store, genuine)
    # The spoof in INBOX keeps the content, so no refill is needed; the genuine is the removed
    # copy, so it is no remaining copy either: a wrong record, refused.
    with pytest.raises(StoreError):
        store.remove_location(spoof.mid, INVOICES, source="event", refill=genuine.rec)
    assert len(store.locations(spoof.mid)) == 2
    # A remaining copy that is not needed is accepted and ignored.
    store.remove_location(spoof.mid, INVOICES, source="event", refill=spoof.rec)
    assert row(store, spoof.mid).content_hash == spoof.hash
    # No location left: no copy remains, so any refill is refused and nothing is applied.
    with pytest.raises(StoreError):
        store.remove_location(spoof.mid, INBOX, source="event", refill=spoof.rec)
    assert [loc.folder_id for loc in store.locations(spoof.mid)] == [INBOX]


# ---- logging -----------------------------------------------------------------------------------


def test_logs_carry_mids_and_counts_only(
    store: Store, inv: dict[str, Copy], caplog: pytest.LogCaptureFixture
) -> None:
    spoof, genuine = inv["spoof"], inv["genuine"]
    caplog.set_level(logging.DEBUG, logger="tern")
    put(store, spoof)
    put(store, genuine, content_raw=spoof.raw)
    store.remove_location(spoof.mid, INBOX, source="event", refill=genuine.rec)
    store.remove_location(spoof.mid, INVOICES, source="event")
    assert caplog.records
    forbidden = ["acme", "Alex", "INV-0077", "0110", "0260", "invoice", INBOX, INVOICES]
    # Standard LogRecord attributes (created, msecs, thread, process, ...) are dropped: they are
    # volatile digits that can contain "0110"/"0260" by chance. The message and every custom
    # (extra) attribute are still checked, and so are a logged traceback and stack.
    std = logging.LogRecord("", 0, "", 0, "", None, None).__dict__
    for rec in caplog.records:
        extras = " ".join(str(v) for k, v in vars(rec).items() if k not in std)
        text = " ".join([rec.getMessage(), extras, rec.exc_text or "", rec.stack_info or ""])
        for word in forbidden:
            assert word not in text, (rec.getMessage(), word)
