"""Plan 04 step 5: threads (`tern.index.threads`, JWZ 1-4, no subject merging) through the Store.

Fixture messages go through the Store API (the real ingest path is step 7) with a test-local
helper like test_chunks.py's; the expected threads come from MANIFEST.json (keys, roots and
members) and the stdlib-computed mids. Inline cases use synthetic ids under `t.example`.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import replace
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm

from tern.index.mid import X_PREFIX, content_hash, mid_from_raw, mid_of
from tern.index.mime import parse_raw
from tern.index.store import AttachmentRecord, LocationState, MessageRecord, Store
from tern.index.threads import assign_threads, id_key, message_links

S0 = LocationState()
T0 = 1_753_783_200
INBOX = "acct1:/INBOX"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "data" / "index.db")
    yield s
    s.close()


def put(store: Store, rec: MessageRecord, folder: str = INBOX) -> None:
    with store.transaction():
        store.upsert_message(rec)
        store.add_location(rec.mid, folder, rec.content_hash, S0, T0, source="backfill")


def drop(store: Store, mid: str) -> None:
    with store.transaction():
        for loc in store.locations(mid):
            store.remove_location(mid, loc.folder_id, source="backfill")
    assert store.get_message(mid) is None


def thread_ids(store: Store) -> dict[str, str]:
    return dict(store.connection.execute("SELECT mid, thread_id FROM messages").fetchall())


# ---- fixture -----------------------------------------------------------------------------------


class Copy:
    def __init__(self, key: str, folder: str, rec: MessageRecord) -> None:
        self.key, self.folder, self.rec = key, folder, rec


@pytest.fixture(scope="module")
def fixture_copies() -> list[Copy]:
    man = tfm.load_manifest(tfm.FIXTURE)
    out: list[Copy] = []
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
                subject=p.subject,
                in_reply_to=p.in_reply_to,
                refs=p.refs,
                body_text=p.main_text,
                attachments=tuple(
                    AttachmentRecord(filename=a.filename, mime=a.mime, size=a.size)
                    for a in p.attachments
                ),
            )
            out.append(Copy(str(m["key"]), str(c["folder"]), rec))
    assert len(out) == 187
    return out


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return tfm.load_manifest(tfm.FIXTURE)


def key_mids(copies: Sequence[Copy]) -> dict[str, str]:
    mids: dict[str, str] = {}
    for c in copies:
        assert mids.setdefault(c.key, c.rec.mid) == c.rec.mid
    return mids


def ingest(tmp_path: Path, name: str, copies: Sequence[Copy]) -> dict[str, str]:
    with Store.open(tmp_path / name / "index.db") as s:
        for c in copies:
            put(s, c.rec, c.folder)
        assert s.status().messages == 183
        return thread_ids(s)


def test_fixture_threads_match_manifest(
    tmp_path: Path, fixture_copies: list[Copy], manifest: dict[str, Any]
) -> None:
    mids = key_mids(fixture_copies)
    for m in manifest["messages"]:  # stdlib mids agree with MANIFEST where it has one
        if m["mid"] is not None:
            assert mids[m["key"]] == m["mid"] == mid_of(m["message_id"])
    tids = ingest(tmp_path, "file-order", fixture_copies)
    assert len(tids) == 183

    in_thread: set[str] = set()
    for t in manifest["threads"]:
        members = {mids[k] for k in t["members"]}
        assert {tids[m] for m in members} == {mids[t["root"]]}, t["key"]
        in_thread |= members
    groups = Counter(tids.values())
    sizes = Counter(n for n in groups.values() if n > 1)
    assert sizes == Counter({2: 22, 3: 15})
    assert sum(sizes.values()) == len(manifest["threads"]) == 37
    singletons = [m for m in tids if m not in in_thread]
    assert len(singletons) == 94
    assert all(tids[m] == m for m in singletons)  # every other message is its own root


def _shared_subjects(copies: Sequence[Copy], group_of: dict[str, str]) -> int:
    """How many subjects (Re:/Fwd: stripped, case folded) occur in two or more groups."""
    groups: dict[str, set[str]] = {}
    for c in copies:
        subj = re.sub(r"(?i)^((re|fwd?|aw|σχετ)\s*:\s*)+", "", c.rec.subject or "").strip()
        if subj:
            groups.setdefault(subj.lower(), set()).add(group_of[c.rec.mid])
    return sum(1 for g in groups.values() if len(g) > 1)


def test_fixture_threads_sharing_a_subject_stay_apart(
    tmp_path: Path, fixture_copies: list[Copy], manifest: dict[str, Any]
) -> None:
    """No subject merging: every subject that MANIFEST's separate threads (and singletons)
    share is still shared by exactly as many separate stored threads."""
    mids = key_mids(fixture_copies)
    thread_of_key = {k: t["key"] for t in manifest["threads"] for k in t["members"]}
    expected_group = {mids[k]: thread_of_key.get(k, k) for k in mids}
    expected = _shared_subjects(fixture_copies, expected_group)
    # 17 with this normalization (the plan says 18; its counting rule is not recorded).
    assert expected == 17
    tids = ingest(tmp_path, "file-order", fixture_copies)
    assert _shared_subjects(fixture_copies, tids) == expected


def test_fixture_cross_folder_threads_and_hijack(
    tmp_path: Path, fixture_copies: list[Copy], manifest: dict[str, Any]
) -> None:
    mids = key_mids(fixture_copies)
    tids = ingest(tmp_path, "file-order", fixture_copies)
    threads = {t["key"]: t for t in manifest["threads"]}
    for key in ("t-inv-0042", "t-documents"):
        t = threads[key]
        folders = {c.folder for c in fixture_copies if c.key in t["members"]}
        assert len(folders) == 2, key
        assert {tids[mids[k]] for k in t["members"]} == {mids[t["root"]]}
    hijack = manifest["thread_hijack"]
    assert hijack["key"] in threads["t-documents"]["members"]
    assert tids[mids[hijack["key"]]] == mids[hijack["names_sent_message"]] == mids["sent-documents"]


def test_fixture_threads_do_not_depend_on_ingest_order(
    tmp_path: Path, fixture_copies: list[Copy]
) -> None:
    file_order = ingest(tmp_path, "file", fixture_copies)
    # Newest first, as backfill does: replies arrive before their roots.
    newest_first = sorted(fixture_copies, key=lambda c: -(c.rec.date_utc or 0))
    assert ingest(tmp_path, "reverse-date", newest_first) == file_order
    shuffled = list(fixture_copies)
    random.Random(20261114).shuffle(shuffled)
    assert ingest(tmp_path, "random", shuffled) == file_order


# ---- links ------------------------------------------------------------------------------------


def k(name: str) -> str:
    key = id_key(f"<{name}@t.example>")
    assert key is not None
    return key


def msg(name: str, refs: Sequence[str] = (), irt: str | None = None) -> MessageRecord:
    return MessageRecord(
        mid=k(name),
        content_hash=k("h-" + name),
        parse_status="ok",
        header_message_id=f"{name}@t.example",
        refs=tuple(f"{r}@t.example" for r in refs),
        in_reply_to=None if irt is None else f"{irt}@t.example",
    )


def test_message_links() -> None:
    assert k("a") == mid_of("<a@t.example>")
    assert id_key(" <A@T.Example>\r\n ") == k("a")  # normalized like a Message-ID mid
    assert id_key("<>") is None
    # References win over In-Reply-To; duplicates keep the first; the own key is dropped.
    assert message_links(
        k("m"), ["a@t.example", "<B@t.example>", "a@t.example"], "c@t.example"
    ) == (
        k("a"),
        k("b"),
    )
    assert message_links(k("m"), [], "c@t.example") == (k("c"),)
    assert message_links(k("m"), ["m@t.example", " "], None) == ()
    assert message_links(k("m"), [], None) == ()


# ---- inline threading cases --------------------------------------------------------------------


def test_storing_a_message_re_roots_every_message_whose_chain_reaches_it(store: Store) -> None:
    put(store, msg("c", ["a", "b"], "b"))
    assert thread_ids(store) == {k("c"): k("c")}  # orphan: its own root
    put(store, msg("b", ["a"], "a"))
    assert thread_ids(store) == {k("b"): k("b"), k("c"): k("b")}
    put(store, msg("a"))
    assert thread_ids(store) == {k("a"): k("a"), k("b"): k("a"), k("c"): k("a")}
    assert store.thread(k("c")) == sorted([k("a"), k("b"), k("c")])  # undated: ties by mid


def test_siblings_under_a_missing_root_share_its_mid(store: Store) -> None:
    put(store, msg("b", ["a"]))
    assert thread_ids(store) == {k("b"): k("b")}  # one effective child: promoted to root
    put(store, msg("c", ["a"]))
    assert thread_ids(store) == {k("b"): k("a"), k("c"): k("a")}  # JWZ 4: the empty root stays
    put(store, msg("a"))
    assert set(thread_ids(store).values()) == {k("a")}  # same thread_id once the root arrives
    drop(store, k("a"))
    assert thread_ids(store) == {k("b"): k("a"), k("c"): k("a")}
    drop(store, k("c"))
    assert thread_ids(store) == {k("b"): k("b")}  # removal re-roots too


def test_removing_a_root_re_roots_its_chain(store: Store) -> None:
    for rec in (msg("a"), msg("b", ["a"]), msg("c", ["a", "b"])):
        put(store, rec)
    assert set(thread_ids(store).values()) == {k("a")}
    drop(store, k("a"))
    assert thread_ids(store) == {k("b"): k("b"), k("c"): k("b")}


def test_a_stored_message_without_links_is_placed_by_others_references(tmp_path: Path) -> None:
    # JWZ 1A: x's References say a replied to y; a (its client dropped References) has no links
    # of its own, so it joins y's thread, in every order.
    recs = [msg("y"), msg("a"), msg("x", ["y", "a"])]
    assert _all_orders(tmp_path / "1a", recs) == {r.mid: k("y") for r in recs}
    # JWZ 1C: a message's own links win over what others' References say about it.
    recs = [msg("y"), msg("p"), msg("a", ["p"]), msg("x", ["y", "a"])]
    tids = _all_orders(tmp_path / "1c", recs)
    assert tids == {k("y"): k("y"), k("p"): k("p"), k("a"): k("p"), k("x"): k("p")}


def test_removing_a_folder_re_roots_the_messages_left(store: Store) -> None:
    other = "acct1:/Archive"
    put(store, msg("r"), INBOX)
    put(store, msg("s", ["r"]), INBOX)
    put(store, msg("s", ["r"]), other)  # same content: s also sits in Archive
    put(store, msg("t", ["r", "s"]), other)
    assert set(thread_ids(store).values()) == {k("r")}
    assert store.remove_folder(INBOX, source="reconcile") == 2  # r goes; s keeps Archive
    assert store.get_message(k("r")) is None
    # s is the missing r's only effective child: promoted to root (JWZ 4), t follows.
    assert thread_ids(store) == {k("s"): k("s"), k("t"): k("s")}


def test_a_non_utf8_message_id_does_not_thread_its_replies(store: Store) -> None:
    """Known limitation (threads.py docstring): mime.py replaces non-UTF-8 bytes in reference
    ids with U+FFFD, mid_from_raw hashes the bytes themselves, so the two keys differ."""
    parent_raw = b"Message-ID: <caf\xe9@t.example>\nSubject: p\n\nbody\n"
    reply_raw = b"Message-ID: <re@t.example>\nIn-Reply-To: <caf\xe9@t.example>\n\nbody\n"
    recs = []
    for raw in (parent_raw, reply_raw):
        p = parse_raw(raw)
        recs.append(
            MessageRecord(
                mid=mid_from_raw(raw),
                content_hash=content_hash(raw),
                parse_status=p.parse_status,
                header_message_id=p.header_message_id,
                in_reply_to=p.in_reply_to,
                refs=p.refs,
            )
        )
        put(store, recs[-1])
    parent, reply = recs
    assert reply.in_reply_to is not None and "\ufffd" in reply.in_reply_to
    assert id_key(reply.in_reply_to) != parent.mid
    assert thread_ids(store) == {parent.mid: parent.mid, reply.mid: reply.mid}


def test_replace_with_changed_links_re_threads(store: Store) -> None:
    put(store, msg("a"))
    put(store, msg("m", ["a"]))
    assert thread_ids(store)[k("m")] == k("a")
    put(store, msg("m"))  # same mid and hash, re-parsed without References: replaced
    assert thread_ids(store) == {k("a"): k("a"), k("m"): k("m")}
    put(store, msg("m", ["a"]))
    assert thread_ids(store)[k("m")] == k("a")
    # The same replace as the first write of a new connection (its link table not built yet).
    with Store.open(store.path) as s2:
        put(s2, msg("m"))
        assert thread_ids(s2) == {k("a"): k("a"), k("m"): k("m")}


def test_a_refill_with_other_links_re_threads(store: Store) -> None:
    """Step 6's refill (a remaining copy whose References differ) goes through the threading
    like any upsert, also as the first write of a new connection (link table not built). The
    s2 case pins the build-before-write order: built after the write, the new References would
    read as the old ones, no re-thread would run and m would stay in b's thread."""
    archive = "acct1:/Archive"
    put(store, msg("a"))
    put(store, msg("b"))
    first = msg("m", ["a"])
    second = replace(msg("m", ["b"]), content_hash=k("h-m2"))
    put(store, first, INBOX)
    put(store, second, archive)  # differing copy: kept out, m stays in a's thread
    assert thread_ids(store)[k("m")] == k("a")
    store.remove_location(k("m"), INBOX, source="event", refill=second)
    assert thread_ids(store) == {k("a"): k("a"), k("b"): k("b"), k("m"): k("b")}
    put(store, first, INBOX)  # back: the second copy keeps the content
    with Store.open(store.path) as s2:
        s2.remove_location(k("m"), archive, source="event", refill=first)
        assert thread_ids(s2) == {k("a"): k("a"), k("b"): k("b"), k("m"): k("a")}


def _all_orders(tmp_path: Path, recs: Sequence[MessageRecord]) -> dict[str, str]:
    results: list[dict[str, str]] = []
    for i, order in enumerate(permutations(recs)):
        with Store.open(tmp_path / f"p{i}" / "index.db") as s, s.transaction():
            for rec in order:
                s.upsert_message(rec)
                s.add_location(rec.mid, INBOX, rec.content_hash, S0, T0, source="backfill")
        with Store.open(tmp_path / f"p{i}" / "index.db") as s:
            results.append(thread_ids(s))
    assert all(r == results[0] for r in results)
    return results[0]


def test_conflicting_references_vote_for_an_empty_parent_in_any_order(tmp_path: Path) -> None:
    # x (missing) follows p in one list and q in two: q wins, whatever arrives first.
    recs = [msg("p"), msg("q"), msg("m1", ["p", "x"]), msg("m2", ["q", "x"]), msg("m3", ["q", "x"])]
    tids = _all_orders(tmp_path / "vote", recs)
    assert tids == {k("p"): k("p"), **{r.mid: k("q") for r in recs[1:]}}
    # A tie goes to the smallest key.
    tie = _all_orders(tmp_path / "tie", recs[:4])
    low = min(k("p"), k("q"))
    assert tie == {k("p"): k("p"), k("q"): k("q"), k("m1"): low, k("m2"): low}


def test_a_reference_cycle_is_cut_at_its_smallest_key(tmp_path: Path) -> None:
    recs = [msg("a", ["b"]), msg("b", ["a"]), msg("c", ["a"])]
    low = min(k("a"), k("b"))
    assert _all_orders(tmp_path, recs) == {r.mid: low for r in recs}


def _random_graph(rng: random.Random, n: int) -> list[MessageRecord]:
    names = [f"n{i}" for i in range(n)]
    recs = []
    for i, name in enumerate(names):
        refs: list[str] = []
        if rng.random() < 0.8:
            pool = names[:i] + [f"gone{j}" for j in range(3)] + names[i + 1 : i + 3]
            refs = rng.sample(pool, rng.randint(1, min(4, len(pool))))
        irt = rng.choice(names) if rng.random() < 0.2 else None
        recs.append(msg(name, refs, irt))
    return recs


def test_random_graphs_store_equals_whole_set_threading(tmp_path: Path) -> None:
    """Missing parents, conflicting lists and cycles; any insert order, then removals: the
    Store's thread_ids always equal assign_threads over the whole stored set."""
    rng = random.Random(5)
    for g in range(4):
        recs = _random_graph(rng, 14)
        expected = assign_threads(
            {r.mid: message_links(r.mid, r.refs, r.in_reply_to) for r in recs}
        )
        for trial in range(3):
            order = list(recs)
            rng.shuffle(order)
            with Store.open(tmp_path / f"g{g}t{trial}" / "index.db") as s:
                for rec in order:
                    put(s, rec)
                assert thread_ids(s) == expected
                left = {r.mid: r for r in recs}
                for rec in rng.sample(order, 6):
                    drop(s, rec.mid)
                    del left[rec.mid]
                    want = assign_threads(
                        {m: message_links(m, r.refs, r.in_reply_to) for m, r in left.items()}
                    )
                    assert thread_ids(s) == want


# ---- the connection-local link table ----------------------------------------------------------


def test_rolled_back_first_build_is_rebuilt(store: Store) -> None:
    with pytest.raises(RuntimeError, match="boom"), store.transaction():
        store.upsert_message(msg("z"))
        raise RuntimeError("boom")
    put(store, msg("a"))
    put(store, msg("b", ["a"]))
    assert thread_ids(store) == {k("a"): k("a"), k("b"): k("a")}


def test_rolled_back_savepoint_build_is_rebuilt(store: Store) -> None:
    with store.transaction():
        with pytest.raises(RuntimeError, match="boom"), store.transaction():
            store.upsert_message(msg("z"))
            raise RuntimeError("boom")
        store.upsert_message(msg("a"))
        store.add_location(k("a"), INBOX, k("h-a"), S0, T0, source="backfill")
    put(store, msg("b", ["a"]))
    assert thread_ids(store) == {k("a"): k("a"), k("b"): k("a")}


def test_another_connections_writes_are_seen(tmp_path: Path) -> None:
    path = tmp_path / "data" / "index.db"
    with Store.open(path) as s1, Store.open(path) as s2:
        put(s1, msg("a"))  # s1 builds its link table
        put(s2, msg("b", ["z"]))  # written by another connection
        put(s1, msg("z"))  # s1 must see b's link to z
        assert thread_ids(s1) == {k("a"): k("a"), k("b"): k("z"), k("z"): k("z")}
