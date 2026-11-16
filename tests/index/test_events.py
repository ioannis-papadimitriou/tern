"""Plan 04 step 8: `EventIngest` (TB events applied to the index) over FakeBridge.

Expected numbers come from MANIFEST.json or from the changes the tests make (inline
`example.test` mail); none is typed in from a run.
"""

from __future__ import annotations

import json
import logging
import mailbox
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm

import tern.index.events as events_mod
from tern.bridge.protocol import (
    Account,
    BridgeError,
    FindParams,
    Folder,
    FoundLocation,
    FullMessage,
    MailNew,
    Page,
)
from tern.config import TernConfig
from tern.index.events import EventIngest
from tern.index.ingest import Backfill
from tern.index.mid import FULL_PREFIX, X_PREFIX, content_hash_full, mid_from_raw, mid_of
from tern.index.reconcile import Reconcile
from tern.index.source import IngestSource
from tern.index.store import LocationState, MessageRecord, Store
from tern.testing.fake_bridge import FakeBridge

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
MESSAGES = tfm.messages(MANIFEST)
COPIES = MANIFEST["message_count"]
DISTINCT = MANIFEST["distinct_messages"]
INV_MID = MANIFEST["named"]["inv-0077"]["mid"]
GENUINE = MANIFEST["adversarial_pair"]["copies"]["genuine"]
SPOOF = MANIFEST["adversarial_pair"]["copies"]["spoof"]
FOLDER_COUNTS = {
    f"{a['key']}:/{f['path']}": f["count"] for a in MANIFEST["accounts"] for f in a["folders"]
}

Hook = tuple[str, str, LocationState | None, str]


def hmid(key: str) -> str:
    return MESSAGES[key]["message_id"].strip("<>")


def make_raw(message_id: str | None, date: int, *, subject: str = "synthetic") -> bytes:
    lines = [f"Message-ID: <{message_id}>"] if message_id else []
    lines += [
        f"Date: {format_datetime(datetime.fromtimestamp(date, UTC))}",
        "From: a@example.test",
        "To: b@example.test",
        f"Subject: {subject}",
        "",
        "body",
        "",
    ]
    return "\n".join(lines).encode()


def empty_mbox(path: Path) -> Path:
    mailbox.mbox(path).close()
    return path


def mini_mailbox(root: Path, raws: list[bytes]) -> Path:
    """A one-account fixture (m1:/INBOX) holding ``raws``."""
    root.mkdir(parents=True, exist_ok=True)
    box = mailbox.mbox(root / "f0.mbox")
    try:
        for raw in raws:
            box.add(raw)
    finally:
        box.close()
    entry = {"path": "INBOX", "special_use": "inbox", "file": "f0.mbox", "format": "mbox"}
    manifest = {
        "accounts": [{"key": "m1", "name": "m1@example.test", "type": "none", "folders": [entry]}]
    }
    (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def raw_calls(fb: FakeBridge, tb_ids: set[int], start: int = 0) -> int:
    return sum(1 for m, p in fb.calls[start:] if m == "mail.messages.raw" and p["tb_id"] in tb_ids)


def ho_rows(store: Store) -> list[tuple[str, int, str | None]]:
    return store.connection.execute(
        "SELECT folder_id, tb_date, mid FROM headers_only_items ORDER BY folder_id, tb_date, mid"
    ).fetchall()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "data" / "index.db")
    yield s
    s.close()


@pytest.fixture
def fb() -> FakeBridge:
    return FakeBridge(FIXTURE)


class Wired:
    def __init__(self, store: Store, fb: FakeBridge) -> None:
        self.events = EventIngest(store, IngestSource(fb), TernConfig())
        self.rec = Reconcile(store, IngestSource(fb), TernConfig(), events=self.events)
        fb.on_event = self.events.on_event
        self.hooks: list[Hook] = []
        self.removed: list[str] = []
        store.location_observers.append(lambda *a: self.hooks.append(a))
        store.message_removed_observers.append(self.removed.append)


async def wired(store: Store, fb: FakeBridge) -> Wired:
    await Backfill(store, IngestSource(fb), TernConfig()).run()
    return Wired(store, fb)


def runs(store: Store, kind: str) -> list[dict[str, Any]]:
    cur = store.connection.execute(
        "SELECT * FROM ingest_runs WHERE kind = ? ORDER BY rowid", (kind,)
    )
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def per_folder(store: Store) -> dict[str, int]:
    return dict(
        store.connection.execute("SELECT folder_id, count(*) FROM locations GROUP BY folder_id")
    )


def fts_mid(store: Store, token: str, mid: str) -> int:
    """Chunks of ``mid`` matching ``token`` (other fixture mail may hold the same digits)."""
    return store.connection.execute(
        "SELECT count(*) FROM chunks_fts JOIN chunks ON chunks.chunk_id = chunks_fts.rowid "
        "WHERE chunks_fts MATCH ? AND chunks.mid = ?",
        (f'"{token}"', mid),
    ).fetchone()[0]


async def tb_id(fb: FakeBridge, key: str, folder_id: str) -> int:
    found = await fb.request("mail.messages.find", FindParams(header_message_id=hmid(key)))
    (one,) = [f.tb_id for f in found.root if f.folder_id == folder_id]
    return one


def noid_scanner(fb: FakeBridge) -> tuple[int, bytes]:
    copy = MESSAGES["noid-scanner"]["copies"][0]
    return fb.tb_ids("acct1:/INBOX")[copy["locator"]], tfm.read_copy(FIXTURE, MANIFEST, copy)


def assert_genuine(store: Store) -> None:
    inv = store.get_message(INV_MID)
    assert inv is not None and not inv.conflicted and inv.conflict_class is None
    assert inv.from_addr == GENUINE["from"]
    assert inv.body_text is not None and GENUINE["iban"] in inv.body_text
    assert [loc.folder_id for loc in store.locations(INV_MID)] == ["acct1:/Invoices"]
    # the spoof's IBAN is no longer searchable under the mid, the genuine one is
    assert fts_mid(store, "0110", INV_MID) >= 1 and fts_mid(store, "0260", INV_MID) == 0
    assert store.status().refill_pending == 0


# ---- events applied ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evented_tag_change_fires_the_hook_and_is_not_missed(
    store: Store, fb: FakeBridge
) -> None:
    w = await wired(store, fb)
    key = "invoice-0013"
    await fb.update(await tb_id(fb, key, "acct1:/Invoices"), tags=["$label4"], read=True)
    mid = MESSAGES[key]["mid"]
    want = LocationState(tags=("$label4",), read=True)
    assert w.hooks == [(mid, "acct1:/Invoices", want, "event")]
    (loc,) = store.locations(mid)
    assert loc.state == want
    daily = await w.rec.daily()
    assert (daily.missed, daily.state_changes) == (0, 0)
    assert [r["folder_id"] for r in runs(store, "event")] == ["acct1:/Invoices"]


@pytest.mark.asyncio
async def test_evented_move_moves_the_location(store: Store, fb: FakeBridge) -> None:
    w = await wired(store, fb)
    key = "invoice-0013"
    mid = MESSAGES[key]["mid"]
    await fb.move([await tb_id(fb, key, "acct1:/Invoices")], "acct1:/Archive")
    assert [loc.folder_id for loc in store.locations(mid)] == ["acct1:/Archive"]
    assert [(h[0], h[1], h[2] is None, h[3]) for h in w.hooks] == [
        (mid, "acct1:/Archive", False, "event"),  # added first: never without a location
        (mid, "acct1:/Invoices", True, "event"),
    ]
    assert w.removed == []
    hourly = await w.rec.hourly()
    assert (hourly.missed, hourly.repaged) == (0, 0)


@pytest.mark.asyncio
async def test_new_event_ingests_and_headers_only_items_get_a_row(
    store: Store, fb: FakeBridge
) -> None:
    w = await wired(store, fb)
    d = int(datetime(2026, 6, 1, tzinfo=UTC).timestamp())
    await fb.deliver("acct3:/INBOX", make_raw("evented@example.test", d))
    mid = mid_of("evented@example.test")
    assert mid and [loc.folder_id for loc in store.locations(mid)] == ["acct3:/INBOX"]
    # a headers-only item: not fetched, recorded; its later download (by reconcile) ingests it
    fb.drop_events(1)
    held = await fb.deliver("acct3:/INBOX", make_raw("held@example.test", d + 1))
    fb.set_headers_only(held, True)
    (item,) = [
        i
        for i in (
            await fb.request(
                "mail.messages.find", FindParams(header_message_id="held@example.test")
            )
        ).root
    ]
    page_item = next(
        p for p in (await IngestSource(fb).page("acct3:/INBOX")).items if p.tb_id == item.tb_id
    )
    start = len(fb.calls)
    await w.events.on_event("event.mail.new", MailNew(folder_id="acct3:/INBOX", items=[page_item]))
    assert not [p for m, p in fb.calls[start:] if m == "mail.messages.raw"]
    assert store.status().headers_only == 1
    fb.set_headers_only(held, False)
    hourly = await w.rec.hourly()
    assert (hourly.added, hourly.missed) == (1, 0)
    assert store.status().headers_only == 0
    assert [h[3] for h in w.hooks] == ["event", "reconcile"]


@pytest.mark.asyncio
async def test_copy_fetch_parses_off_the_event_loop(
    store: Store, fb: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T13: a same-folder copy fetched by the event path is parsed in a worker thread, not on
    the bridge's loop (new items go through the backfill's ItemIngester, tested in
    test_ingest)."""
    w = await wired(store, fb)
    (spoof_copy,) = [c for c in MESSAGES["inv-0077"]["copies"] if c["folder"] == "acct1/INBOX"]
    spoof_raw = tfm.read_copy(FIXTURE, MANIFEST, spoof_copy)
    await fb.delete([await tb_id(fb, "inv-0077", "acct1:/INBOX")])
    await fb.deliver("acct1:/Invoices", spoof_raw)
    ((folder, spoof_hash),) = store.same_folder_copies(INV_MID)
    loop_thread = threading.get_ident()
    threads: list[int] = []
    real = events_mod.record_from_raw

    def on_raw(raw: bytes) -> MessageRecord:
        threads.append(threading.get_ident())
        return real(raw)

    monkeypatch.setattr(events_mod, "record_from_raw", on_raw)
    rec = await w.events.fetcher.fetch_copy(INV_MID, folder, spoof_hash)
    assert rec is not None and rec.content_hash == spoof_hash
    assert threads and all(ident != loop_thread for ident in threads)


@pytest.mark.asyncio
async def test_full_path_copy_fetch_parses_off_the_event_loop(
    store: Store, fb: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T13 on the full path: a copy whose stored hash is a ``full-`` hash (ingested over
    RAW_MAX) is refetched with getFull and parsed in a worker thread, not on the loop."""
    w = await wired(store, fb)
    folder = store.connection.execute(
        "SELECT folder_id FROM locations WHERE mid = ? ORDER BY folder_id LIMIT 1", (INV_MID,)
    ).fetchone()[0]
    full = await IngestSource(fb).full(await tb_id(fb, "inv-0077", folder))
    wanted = content_hash_full(full)
    assert wanted.startswith(FULL_PREFIX)
    loop_thread = threading.get_ident()
    threads: list[int] = []
    real = events_mod.record_from_full

    def on_full(mid: str, full: FullMessage, size: int) -> MessageRecord:
        threads.append(threading.get_ident())
        return real(mid, full, size)

    monkeypatch.setattr(events_mod, "record_from_full", on_full)
    rec = await w.events.fetcher.fetch_copy(INV_MID, folder, wanted)
    assert rec is not None and rec.content_hash == wanted
    assert threads and all(ident != loop_thread for ident in threads)


@pytest.mark.asyncio
async def test_events_for_excluded_folders_are_ignored(
    store: Store, fb: FakeBridge, tmp_path: Path
) -> None:
    trash = fb.add_mbox("acct1", "Trash", empty_mbox(tmp_path / "t.mbox"), ["trash"])
    w = await wired(store, fb)
    await fb.deliver(trash, make_raw("trashed@example.test", 1_780_000_000))
    assert w.events.ignored == 1 and w.hooks == [] and runs(store, "event") == []
    assert store.get_message(mid_of("trashed@example.test") or "") is None


# ---- unresolved events and x- churn ----------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolvable_md5_delete_runs_the_hourly_check(store: Store, fb: FakeBridge) -> None:
    w = await wired(store, fb)
    noid, raw = noid_scanner(fb)
    assert await IngestSource(fb).raw(noid) == raw  # pins the locator
    x_mid = mid_from_raw(raw)
    assert x_mid.startswith(X_PREFIX) and store.get_message(x_mid) is not None
    await fb.delete([noid])  # TB's md5: id: find cannot resolve a deleted message
    (event,) = runs(store, "event")
    assert (event["folder_id"], event["unresolved_events"]) == ("acct1:/INBOX", 1)
    (hourly,) = runs(store, "hourly")
    assert (hourly["folder_id"], hourly["missed"]) == ("acct1:/INBOX", 0)  # covered
    assert store.get_message(x_mid) is None and store.status().x_mids == 2
    assert w.hooks == [(x_mid, "acct1:/INBOX", None, "reconcile")]


@pytest.mark.asyncio
async def test_rewritten_id_less_message_counts_x_mid_churn(store: Store, fb: FakeBridge) -> None:
    await wired(store, fb)
    noid, raw = noid_scanner(fb)
    await fb.delete([noid])
    changed = b"X-Mozilla-Status: 0001\n" + raw
    await fb.deliver("acct1:/INBOX", changed)
    deliver_run = runs(store, "event")[-1]
    assert (deliver_run["x_mid_churn"], deliver_run["n_msgs"]) == (1, 1)
    assert store.status().x_mids == 3
    assert store.get_message(mid_from_raw(changed)) is not None
    assert store.get_message(mid_from_raw(raw)) is None


# ---- conflicts end to end -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_the_spoof_restores_the_genuine_content(
    store: Store, fb: FakeBridge
) -> None:
    w = await wired(store, fb)
    inv = store.get_message(INV_MID)
    assert inv is not None and inv.conflicted and inv.from_addr == SPOOF["from"]
    assert fts_mid(store, "0260", INV_MID) >= 1  # the spoof (ingested first) fills the row
    await fb.delete([await tb_id(fb, "inv-0077", "acct1:/INBOX")])
    assert_genuine(store)
    assert w.hooks == [(INV_MID, "acct1:/INBOX", None, "event")]


@pytest.mark.asyncio
async def test_moving_the_spoof_to_trash_clears_the_conflict(
    store: Store, fb: FakeBridge, tmp_path: Path
) -> None:
    trash = fb.add_mbox("acct1", "Trash", empty_mbox(tmp_path / "t.mbox"), ["trash"])
    w = await wired(store, fb)
    await fb.move([await tb_id(fb, "inv-0077", "acct1:/INBOX")], trash)
    assert_genuine(store)
    assert w.hooks == [(INV_MID, "acct1:/INBOX", None, "event")]
    assert trash not in per_folder(store)


@pytest.mark.asyncio
async def test_a_same_folder_spoof_delivered_by_event_is_conflict_checked(
    store: Store, fb: FakeBridge
) -> None:
    # Step 6 case (i) through events: the genuine sits in Invoices, the spoof arrives there. The
    # mid is already located in the folder, so only fetching the new item can see the conflict.
    await wired(store, fb)
    (spoof_copy,) = [c for c in MESSAGES["inv-0077"]["copies"] if c["folder"] == "acct1/INBOX"]
    spoof_raw = tfm.read_copy(FIXTURE, MANIFEST, spoof_copy)
    await fb.delete([await tb_id(fb, "inv-0077", "acct1:/INBOX")])
    assert_genuine(store)
    await fb.deliver("acct1:/Invoices", spoof_raw)
    inv = store.get_message(INV_MID)
    assert inv is not None and inv.conflicted
    assert [f for f, _h in store.same_folder_copies(INV_MID)] == ["acct1:/Invoices"]
    assert inv.from_addr == GENUINE["from"]  # the located copy keeps the content


@pytest.mark.asyncio
async def test_a_same_folder_copy_dated_apart_from_its_location_is_fetched(
    store: Store, fb: FakeBridge
) -> None:
    # Invoices holds the genuine (location row, its tb_date) and the spoof (same-folder copy,
    # another date). Fetching the spoof copy must fall back to `find` when the item at the
    # location's tb_date (the genuine) does not hash to it.
    w = await wired(store, fb)
    (spoof_copy,) = [c for c in MESSAGES["inv-0077"]["copies"] if c["folder"] == "acct1/INBOX"]
    spoof_raw = tfm.read_copy(FIXTURE, MANIFEST, spoof_copy)
    await fb.delete([await tb_id(fb, "inv-0077", "acct1:/INBOX")])
    await fb.deliver("acct1:/Invoices", spoof_raw)
    ((folder, spoof_hash),) = store.same_folder_copies(INV_MID)
    rec = await w.events.fetcher.fetch_copy(INV_MID, folder, spoof_hash)
    assert rec is not None and rec.content_hash == spoof_hash


def spoof_of(raw: bytes) -> bytes:
    """Another body under the same headers (a same-Message-ID spoof)."""
    out = raw.replace(b"\nbody\n", b"\nzqspoof\n")
    assert out != raw
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["hourly", "daily"])
async def test_a_same_folder_spoof_evented_while_headers_only_is_fetched_once_downloaded(
    store: Store, tmp_path: Path, kind: str
) -> None:
    # IMAP order: the header arrives first (its new-mail event is applied while the item is
    # headers-only), the body later, with no event. The mid is already located in the folder,
    # so only a headers-only row for that item and the pass's per-mid copy count find the spoof.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("dup@example.test", d)]))
    w = await wired(store, fb)
    mid = mid_of("dup@example.test") or ""
    fb.drop_events(1)
    s = await fb.deliver("m1:/INBOX", spoof_of(make_raw("dup@example.test", d + 5)))
    fb.set_headers_only(s, True)
    (item,) = [p for p in (await IngestSource(fb).page("m1:/INBOX")).items if p.tb_id == s]
    await w.events.on_event("event.mail.new", MailNew(folder_id="m1:/INBOX", items=[item]))
    assert ho_rows(store) == [("m1:/INBOX", d + 5, mid)]
    fb.set_headers_only(s, False)  # the body is downloaded: TB fires no event
    report = await (w.rec.hourly() if kind == "hourly" else w.rec.daily())
    assert (report.copies, report.missed) == (1, 0)
    m = store.get_message(mid)
    assert m is not None and m.conflicted
    assert [f for f, _h in store.same_folder_copies(mid)] == ["m1:/INBOX"]
    assert ho_rows(store) == []
    # the same as a fresh backfill of this TB state ...
    with Store.open(tmp_path / "fresh" / "index.db") as fresh:
        await Backfill(fresh, IngestSource(fb), TernConfig()).run()
        fm = fresh.get_message(mid)
        assert fm is not None and fm.conflicted
        assert len(fresh.same_folder_copies(mid)) == 1
    # ... and once the copies are counted, a pass fetches nothing more
    start = len(fb.calls)
    await w.rec.hourly()
    assert raw_calls(fb, set(fb.tb_ids("m1:/INBOX")), start) == 0


@pytest.mark.asyncio
async def test_a_written_copy_drops_only_its_own_headers_only_row(
    store: Store, tmp_path: Path
) -> None:
    # A headers-only spoof of a Message-ID is recorded; then a downloaded copy of that
    # Message-ID arrives (another tb_date). Writing it must not take the spoof's row.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("other@example.test", d - 60)]))
    w = await wired(store, fb)
    mid = mid_of("dup@example.test") or ""
    fb.drop_events(1)
    s = await fb.deliver("m1:/INBOX", spoof_of(make_raw("dup@example.test", d)))
    fb.set_headers_only(s, True)
    (item,) = [p for p in (await IngestSource(fb).page("m1:/INBOX")).items if p.tb_id == s]
    await w.events.on_event("event.mail.new", MailNew(folder_id="m1:/INBOX", items=[item]))
    await fb.deliver("m1:/INBOX", make_raw("dup@example.test", d + 5))
    assert [loc.folder_id for loc in store.locations(mid)] == ["m1:/INBOX"]
    assert ho_rows(store) == [("m1:/INBOX", d, mid)]


@pytest.mark.asyncio
@pytest.mark.parametrize("dup", ["same_bytes", "other_to_header"])
async def test_an_identical_copy_evented_while_headers_only_leaves_no_row_once_downloaded(
    store: Store, tmp_path: Path, dup: str
) -> None:
    # A second delivery of a located message, byte-identical or with an equal content_hash
    # (another To:), arrives header-first: its event records a headers_only row. Once its body
    # is downloaded (no event), the per-mid copy count fetches it; its hash is already held, so
    # nothing is written, and its row must still go, as a fresh backfill has none.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    raw = make_raw("dup@example.test", d)
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [raw]))
    w = await wired(store, fb)
    mid = mid_of("dup@example.test") or ""
    fb.drop_events(1)
    copy = raw if dup == "same_bytes" else raw.replace(b"To: b@example.test", b"To: c@example")
    assert dup == "same_bytes" or copy != raw
    s = await fb.deliver("m1:/INBOX", copy)
    fb.set_headers_only(s, True)
    (item,) = [p for p in (await IngestSource(fb).page("m1:/INBOX")).items if p.tb_id == s]
    await w.events.on_event("event.mail.new", MailNew(folder_id="m1:/INBOX", items=[item]))
    assert ho_rows(store) == [("m1:/INBOX", d, mid)]
    fb.set_headers_only(s, False)
    report = await w.rec.hourly()
    assert (report.copies, report.missed) == (0, 0)
    m = store.get_message(mid)
    assert m is not None and not m.conflicted
    assert ho_rows(store) == []
    with Store.open(tmp_path / "fresh" / "index.db") as fresh:
        await Backfill(fresh, IngestSource(fb), TernConfig()).run()
        assert ho_rows(fresh) == []
        assert store.status().headers_only == fresh.status().headers_only == 0


@pytest.mark.asyncio
async def test_a_still_headers_only_copy_keeps_its_row_when_its_identical_sibling_downloads(
    store: Store, tmp_path: Path
) -> None:
    # Three items of one Message-ID at one second: the located one, an identical copy (header
    # first, then downloaded, no event) and a differing copy still headers-only. The count's
    # row trim must keep the row TB still backs (one headers-only item there).
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    raw = make_raw("dup@example.test", d)
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [raw]))
    w = await wired(store, fb)
    mid = mid_of("dup@example.test") or ""
    fb.drop_events(2)
    s1 = await fb.deliver("m1:/INBOX", raw)
    s2 = await fb.deliver("m1:/INBOX", spoof_of(raw))
    fb.set_headers_only(s1, True)
    fb.set_headers_only(s2, True)
    items = [p for p in (await IngestSource(fb).page("m1:/INBOX")).items if p.tb_id in {s1, s2}]
    await w.events.on_event("event.mail.new", MailNew(folder_id="m1:/INBOX", items=items))
    assert ho_rows(store) == [("m1:/INBOX", d, mid)]  # one row per (mid, tb_date) from events
    fb.set_headers_only(s1, False)
    await w.rec.hourly()
    assert ho_rows(store) == [("m1:/INBOX", d, mid)]
    with Store.open(tmp_path / "fresh" / "index.db") as fresh:
        await Backfill(fresh, IngestSource(fb), TernConfig()).run()
        assert ho_rows(fresh) == ho_rows(store)


@pytest.mark.asyncio
async def test_a_stale_headers_only_event_for_a_located_item_leaves_no_row_after_a_pass(
    store: Store, tmp_path: Path
) -> None:
    # A replayed (or late) new-mail event whose snapshot still says headers-only, for an item
    # the index already holds downloaded. The event cannot tell it from a header-first spoof,
    # so it records a row; TB lists no headers-only item there, so the next pass drops it and
    # the index equals a fresh backfill (before, no hourly or daily pass removed it).
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("dup@example.test", d)]))
    w = await wired(store, fb)
    mid = mid_of("dup@example.test") or ""
    (item,) = (await IngestSource(fb).page("m1:/INBOX")).items
    stale = item.model_copy(update={"headers_only": True})
    await w.events.on_event("event.mail.new", MailNew(folder_id="m1:/INBOX", items=[stale]))
    assert ho_rows(store) == [("m1:/INBOX", d, mid)]
    report = await w.rec.hourly()
    assert (report.repaged, report.added, report.removed, report.copies, report.missed) == (
        0,
        0,
        0,
        0,
        0,
    )
    assert ho_rows(store) == [] and store.status().headers_only == 0
    assert [loc.folder_id for loc in store.locations(mid)] == ["m1:/INBOX"]
    with Store.open(tmp_path / "fresh" / "index.db") as fresh:
        await Backfill(fresh, IngestSource(fb), TernConfig()).run()
        assert ho_rows(fresh) == ho_rows(store)
        assert fresh.status().headers_only == store.status().headers_only


@pytest.mark.asyncio
async def test_a_deleted_headers_only_copy_of_a_located_mid_loses_its_row_at_the_next_pass(
    store: Store, tmp_path: Path
) -> None:
    # A located Message-ID with a differing headers-only copy at another second (its row backed
    # by TB: kept by a pass). The copy is deleted: the event resyncs the location only (the
    # downloaded copy keeps it), so its row stays until the next pass drops it.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("dup@example.test", d)]))
    w = await wired(store, fb)
    mid = mid_of("dup@example.test") or ""
    fb.drop_events(1)
    s = await fb.deliver("m1:/INBOX", spoof_of(make_raw("dup@example.test", d + 5)))
    fb.set_headers_only(s, True)
    (item,) = [p for p in (await IngestSource(fb).page("m1:/INBOX")).items if p.tb_id == s]
    await w.events.on_event("event.mail.new", MailNew(folder_id="m1:/INBOX", items=[item]))
    await w.rec.hourly()
    assert ho_rows(store) == [("m1:/INBOX", d + 5, mid)]  # TB backs it: the pass keeps it
    await fb.delete([s])
    assert [loc.folder_id for loc in store.locations(mid)] == ["m1:/INBOX"]
    assert ho_rows(store) == [("m1:/INBOX", d + 5, mid)]
    report = await w.rec.hourly()
    assert (report.removed, report.missed) == (0, 0)
    assert ho_rows(store) == []
    with Store.open(tmp_path / "fresh" / "index.db") as fresh:
        await Backfill(fresh, IngestSource(fb), TernConfig()).run()
        assert ho_rows(fresh) == ho_rows(store)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["hourly", "daily"])
@pytest.mark.parametrize("how", ["no_event", "header_first"])
async def test_a_same_folder_copy_found_by_count_is_missed_only_without_its_event(
    store: Store, tmp_path: Path, kind: str, how: str
) -> None:
    # A differing copy of a located Message-ID, written by the per-mid copy count:
    # - no_event: it arrives downloaded and its new-mail event is lost: missed;
    # - header_first: its event was applied while it was headers-only, before an earlier pass
    #   (whose coverage is gone), and TB downloaded the body later with no event: a late
    #   download (its write drops the event's row), not missed.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("dup@example.test", d)]))
    w = await wired(store, fb)
    mid = mid_of("dup@example.test") or ""
    fb.drop_events(1)
    s = await fb.deliver("m1:/INBOX", spoof_of(make_raw("dup@example.test", d + 5)))
    if how == "header_first":
        fb.set_headers_only(s, True)
        (item,) = [p for p in (await IngestSource(fb).page("m1:/INBOX")).items if p.tb_id == s]
        await w.events.on_event("event.mail.new", MailNew(folder_id="m1:/INBOX", items=[item]))
        first = await w.rec.hourly()  # takes the event's coverage; TB backs the row
        assert (first.copies, first.missed) == (0, 0)
        assert ho_rows(store) == [("m1:/INBOX", d + 5, mid)]
        fb.set_headers_only(s, False)
    n = len(runs(store, kind))
    report = await (w.rec.hourly() if kind == "hourly" else w.rec.daily())
    want = 1 if how == "no_event" else 0
    assert (report.copies, report.missed) == (1, want)
    assert [r["missed"] for r in runs(store, kind)[n:]] == [want]
    m = store.get_message(mid)
    assert m is not None and m.conflicted
    assert [f for f, _h in store.same_folder_copies(mid)] == ["m1:/INBOX"]
    assert ho_rows(store) == []
    again = await w.rec.hourly()
    assert (again.copies, again.missed) == (0, 0)


@pytest.mark.asyncio
async def test_identical_copies_take_the_daily_state_of_the_first_listed_item(
    store: Store, tmp_path: Path
) -> None:
    # Pins #30's identical-copy bullet (provisional, open for the user; no rule chosen): two
    # byte-identical copies collapse into one location. An evented read=True of the second copy
    # sets the row's state; the next daily sets it to the first downloaded item TB lists (still
    # unread), with one observer event and nothing counted as missed (the event's coverage),
    # and later dailies change nothing.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    raw = make_raw("twice@example.test", d)
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [raw, raw]))
    _first, second = fb.tb_ids("m1:/INBOX")
    w = await wired(store, fb)
    mid = mid_of("twice@example.test") or ""
    (loc,) = store.locations(mid)
    assert not loc.state.read
    await fb.update(second, read=True)
    (loc,) = store.locations(mid)
    assert loc.state.read and [h[3] for h in w.hooks] == ["event"]
    reports = [await w.rec.daily() for _ in range(3)]
    (loc,) = store.locations(mid)
    assert not loc.state.read
    assert w.hooks[1:] == [(mid, "m1:/INBOX", loc.state, "reconcile")]
    assert [r.state_changes for r in reports] == [1, 0, 0]
    assert [r.missed for r in reports] == [0, 0, 0]
    assert [r["missed"] for r in runs(store, "daily")] == [0, 0, 0]


@pytest.mark.asyncio
async def test_a_repage_records_a_headers_only_row_for_a_located_mid(
    store: Store, tmp_path: Path
) -> None:
    # A located Message-ID gets a differing headers-only copy with no event. A re-page of the
    # window records that copy's row, as a fresh backfill does (and as the event path would).
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("dup@example.test", d)]))
    w = await wired(store, fb)
    mid = mid_of("dup@example.test") or ""
    fb.drop_events(1)
    s = await fb.deliver("m1:/INBOX", spoof_of(make_raw("dup@example.test", d + 5)))
    fb.set_headers_only(s, True)
    assert ho_rows(store) == []
    await w.rec.repage_window("m1:/INBOX", None, None)
    assert ho_rows(store) == [("m1:/INBOX", d + 5, mid)]
    with Store.open(tmp_path / "fresh" / "index.db") as fresh:
        await Backfill(fresh, IngestSource(fb), TernConfig()).run()
        assert ho_rows(fresh) == ho_rows(store)


@pytest.mark.asyncio
async def test_a_reconcile_write_at_another_second_than_a_headers_only_row_counts_as_missed(
    store: Store, tmp_path: Path
) -> None:
    # A Message-ID has only a headers-only item in the folder (its row at d). A downloaded copy
    # of it at d + 5 arrives without an event: its write drops no row (a write drops a row at
    # its own tb_date only), so it is a missed add, not a late download.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("ho@example.test", d)]))
    (held,) = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(held, True)
    w = await wired(store, fb)
    mid = mid_of("ho@example.test") or ""
    assert ho_rows(store) == [("m1:/INBOX", d, mid)]
    fb.drop_events(1)
    await fb.deliver("m1:/INBOX", spoof_of(make_raw("ho@example.test", d + 5)))
    report = await w.rec.hourly()
    assert (report.added, report.missed) == (1, 1)
    assert ho_rows(store) == [("m1:/INBOX", d, mid)]


@pytest.mark.asyncio
async def test_move_of_one_of_two_headers_only_copies_moves_only_its_row(
    store: Store, tmp_path: Path
) -> None:
    # Two headers-only items of one Message-ID at different seconds; the later one is moved.
    # The source keeps the row of the one that stayed (the move drops the row at the moved
    # item's tb_date, not the mid's oldest row).
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    raws = [make_raw("ho@example.test", d), spoof_of(make_raw("ho@example.test", d + 5))]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    fb.add_mbox("m1", "Other", empty_mbox(tmp_path / "other.mbox"))
    stay, go = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(stay, True)
    fb.set_headers_only(go, True)
    w = await wired(store, fb)
    mid = mid_of("ho@example.test") or ""
    assert ho_rows(store) == [("m1:/INBOX", d, mid), ("m1:/INBOX", d + 5, mid)]
    await fb.move([go], "m1:/Other")
    assert ho_rows(store) == [("m1:/INBOX", d, mid), ("m1:/Other", d + 5, mid)]
    assert [r["unresolved_events"] for r in runs(store, "event")] == [0]
    assert (await w.rec.hourly()).missed == 0


class PassFailed(Exception):
    """A reconcile pass that raised (test-made)."""


class QueuedFailed(Exception):
    """The failure of an event left queued (test-made)."""


class ArrivingFailed(Exception):
    """The failure of the arriving event (test-made)."""


@pytest.mark.asyncio
@pytest.mark.parametrize("arriving", ["ok", "fails"])
async def test_an_event_after_a_failed_drain_is_applied_and_the_failure_says_whose(
    store: Store, tmp_path: Path, arriving: str
) -> None:
    # A pass raised with a delivery to INBOX queued; the next event (a delivery to Other) drains
    # it and it fails. Nothing older is queued on Other, so the arriving event is applied
    # anyway; then the queued event's failure is raised, naming INBOX in failed_event_folders.
    # If the arriving event fails too, its own failure is raised, caused by the queued one's.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("a@example.test", d)]))
    fb.add_mbox("m1", "Other", empty_mbox(tmp_path / "other.mbox"))
    faults: list[type[Exception]] = []

    class Faulty(IngestSource):
        async def raw(self, tb_id: int) -> bytes:
            if faults:
                raise faults.pop(0)()
            return await super().raw(tb_id)

    await Backfill(store, IngestSource(fb), TernConfig()).run()
    events = EventIngest(store, Faulty(fb), TernConfig())
    fb.on_event = events.on_event
    with pytest.raises(PassFailed):
        async with events.paused("m1:/INBOX"):
            await fb.deliver("m1:/INBOX", make_raw("queued@example.test", d + 1))
            raise PassFailed
    assert [type(e).__name__ for e in events._queue] == ["MailNew"]
    faults.append(QueuedFailed)
    if arriving == "fails":
        faults.append(ArrivingFailed)
    new_mid = mid_of("arriving@example.test") or ""
    if arriving == "ok":
        with pytest.raises(QueuedFailed):
            await fb.deliver("m1:/Other", make_raw("arriving@example.test", d + 2))
        assert events_mod.failed_event_folders.get() == frozenset({"m1:/INBOX"})
        assert [loc.folder_id for loc in store.locations(new_mid)] == ["m1:/Other"]
    else:
        with pytest.raises(ArrivingFailed) as info:
            await fb.deliver("m1:/Other", make_raw("arriving@example.test", d + 2))
        assert isinstance(info.value.__cause__, QueuedFailed)
        assert events_mod.failed_event_folders.get() is None
        assert store.locations(new_mid) == []
    assert events._queue == [] and faults == []
    assert store.locations(mid_of("queued@example.test") or "") == []  # dropped: reconcile's


# ---- headers-only items: never fetched (Decision 8) ------------------------------------------


@pytest.fixture
def ho_fb(tmp_path: Path) -> FakeBridge:
    """m1:/INBOX with a headers-only Message-ID item, a headers-only ID-less item (md5:) and a
    downloaded one, plus an empty m1:/Other."""
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    raws = [
        make_raw("ho-named@example.test", d),
        make_raw(None, d - 7),
        make_raw("kept@example.test", d - 20),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    fb.add_mbox("m1", "Other", empty_mbox(tmp_path / "other.mbox"))
    named, anon, _kept = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(named, True)
    fb.set_headers_only(anon, True)
    return fb


@pytest.mark.asyncio
async def test_updates_of_headers_only_items_fetch_nothing(store: Store, ho_fb: FakeBridge) -> None:
    w = await wired(store, ho_fb)
    rows = ho_rows(store)
    assert len(rows) == 2
    named, anon, _kept = ho_fb.tb_ids("m1:/INBOX")
    start = len(ho_fb.calls)
    await ho_fb.update(named, read=True)
    await ho_fb.update(anon, flagged=True)
    assert raw_calls(ho_fb, {named, anon}, start) == 0
    assert not [m for m, _p in ho_fb.calls[start:] if m == "mail.messages.full"]
    assert [r["unresolved_events"] for r in runs(store, "event")] == [0, 0]
    assert runs(store, "hourly") == [] and w.hooks == [] and ho_rows(store) == rows


@pytest.mark.asyncio
async def test_delete_of_a_headers_only_message_id_item_drops_its_row(
    store: Store, ho_fb: FakeBridge
) -> None:
    w = await wired(store, ho_fb)
    named, _anon, _kept = ho_fb.tb_ids("m1:/INBOX")
    start = len(ho_fb.calls)
    await ho_fb.delete([named])
    assert not [
        m for m, _p in ho_fb.calls[start:] if m in ("mail.messages.raw", "mail.messages.full")
    ]
    assert [r["unresolved_events"] for r in runs(store, "event")] == [0]
    assert runs(store, "hourly") == [] and w.hooks == []
    assert [r[2] for r in ho_rows(store)] == [None]
    assert (await w.rec.hourly()).missed == 0


@pytest.mark.asyncio
async def test_copy_fetch_finds_an_item_dated_within_its_second(
    store: Store, fb: FakeBridge
) -> None:
    # TB dates may carry a fraction of a second (tb_date floors it) and its date filter is
    # inclusive: a query ending exactly at the second would miss the item. noid-scanner has an
    # x- mid, so `find` cannot stand in for the page.
    await Backfill(store, IngestSource(fb), TernConfig()).run()

    class SubSecond(IngestSource):
        """TB as seen with every date 0.5 s later, filtered on that date."""

        async def page(
            self,
            folder_id: str,
            from_date: datetime | None = None,
            to_date: datetime | None = None,
            cursor: str | None = None,
        ) -> Page:
            wider = None if to_date is None else to_date + timedelta(seconds=1)
            page = await super().page(folder_id, from_date, wider, cursor)
            items = [
                i.model_copy(update={"date": i.date + timedelta(seconds=0.5)}) for i in page.items
            ]
            if to_date is not None:
                items = [i for i in items if i.date <= to_date]
            return page.model_copy(update={"items": items})

    events = EventIngest(store, SubSecond(fb), TernConfig())
    _noid, raw = noid_scanner(fb)
    x_mid = mid_from_raw(raw)
    (loc,) = store.locations(x_mid)
    rec = await events.fetcher.fetch_copy(x_mid, loc.folder_id, loc.content_hash)
    assert rec is not None and rec.mid == x_mid


@pytest.mark.parametrize("apart", [0, 9])
@pytest.mark.asyncio
async def test_copy_fetch_skips_headers_only_items_of_the_mid(
    store: Store, tmp_path: Path, apart: int
) -> None:
    # Two copies of one Message-ID in m1:/INBOX: one downloaded (indexed), one headers-only,
    # `apart` seconds older (0: at the location's tb_date). A copy fetch, by page or by `find`,
    # must never fetch the headers-only one (Decision 8).
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    raws = [
        make_raw("twice@example.test", d),
        make_raw("twice@example.test", d - apart, subject="x"),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    _got, held = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(held, True)
    w = await wired(store, fb)
    mid = mid_of("twice@example.test")
    assert mid is not None
    start = len(fb.calls)
    assert await w.events.fetcher.fetch_copy(mid, "m1:/INBOX", "0" * 64) is None
    assert any(m == "mail.messages.find" for m, _p in fb.calls[start:])
    assert raw_calls(fb, {held}, start) == 0


@pytest.mark.asyncio
async def test_move_of_headers_only_items_moves_their_rows(store: Store, ho_fb: FakeBridge) -> None:
    w = await wired(store, ho_fb)
    named, anon, _kept = ho_fb.tb_ids("m1:/INBOX")
    before = {(d, m) for _f, d, m in ho_rows(store)}
    start = len(ho_fb.calls)
    new_ids = await ho_fb.move([named, anon], "m1:/Other")
    assert raw_calls(ho_fb, {named, anon, *new_ids}, start) == 0
    assert not [m for m, _p in ho_fb.calls[start:] if m == "mail.messages.full"]
    assert {(f, d, m) for f, d, m in ho_rows(store)} == {("m1:/Other", d, m) for d, m in before}
    assert [r["unresolved_events"] for r in runs(store, "event")] == [0]
    report = await w.rec.hourly()
    assert (report.missed, report.repaged) == (0, 0) and w.hooks == []


@pytest.mark.asyncio
async def test_move_of_a_downloaded_id_less_item_whose_mid_is_unknown_drops_no_row(
    store: Store, tmp_path: Path
) -> None:
    # A downloaded md5: item shares its second with a headers-only md5: item. It is moved to an
    # excluded folder and its copy there goes stale, so its mid cannot be learned. The other
    # item's NULL-mid headers-only row must stay; the move is unresolved and the source's hourly
    # check removes the moved item's x- location.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    raws = [make_raw(None, d, subject="held"), make_raw(None, d, subject="moved")]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    fb.add_mbox("m1", "Trash", empty_mbox(tmp_path / "trash.mbox"), ["trash"])
    held, moved = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(held, True)
    x_mid = mid_from_raw(raws[1])
    await Backfill(store, IngestSource(fb), TernConfig()).run()
    known = set(fb.tb_ids("m1:/INBOX"))

    class NewIdsStale(IngestSource):
        async def raw(self, tb_id: int) -> bytes:
            if tb_id not in known:
                raise BridgeError("stale_id", "tb_id is not valid in this session")
            return await super().raw(tb_id)

    events = EventIngest(store, NewIdsStale(fb), TernConfig())
    Reconcile(store, NewIdsStale(fb), TernConfig(), events=events)
    fb.on_event = events.on_event
    assert ho_rows(store) == [("m1:/INBOX", d, None)]
    assert [loc.folder_id for loc in store.locations(x_mid)] == ["m1:/INBOX"]
    await fb.move([moved], "m1:/Trash")
    assert [r["unresolved_events"] for r in runs(store, "event")] == [1]
    assert ho_rows(store) == [("m1:/INBOX", d, None)]
    assert store.get_message(x_mid) is None
    assert [(r["folder_id"], r["missed"]) for r in runs(store, "hourly")] == [("m1:/INBOX", 0)]


def spoof_fb(tmp_path: Path) -> tuple[FakeBridge, int, bytes]:
    """m1:/INBOX holding a headers-only genuine copy of dup@example.test, an empty m1:/Trash;
    returns (bridge, the genuine's tb_date, a spoof with the same Message-ID, 5 s newer, whose
    body carries a token no other mail holds)."""
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("dup@example.test", d)]))
    fb.add_mbox("m1", "Trash", empty_mbox(tmp_path / "trash.mbox"), ["trash"])
    (genuine,) = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(genuine, True)
    spoof = make_raw("dup@example.test", d + 5, subject="spoof").replace(
        b"\nbody\n", b"\nzqspoof\n"
    )
    assert b"zqspoof" in spoof
    return fb, d, spoof


@pytest.mark.parametrize("how", ["delete", "move_to_trash"])
@pytest.mark.asyncio
async def test_a_removed_spoof_of_a_headers_only_copy_leaves_no_text(
    store: Store, tmp_path: Path, how: str
) -> None:
    # The spoof takes the Message-ID of a headers-only genuine copy in the same folder (its
    # write keeps the genuine's headers_only_items row: a write drops only a row at its own
    # tb_date, and the genuine is still headers-only, as a fresh backfill records). Once the spoof
    # is gone, `find` still lists the genuine there: being headers-only, it must not keep the
    # location (and the spoof's text) alive. The index ends as a fresh backfill builds it.
    fb, d, spoof = spoof_fb(tmp_path)
    (genuine,) = fb.tb_ids("m1:/INBOX")
    w = await wired(store, fb)
    mid = mid_of("dup@example.test")
    assert mid is not None and ho_rows(store) == [("m1:/INBOX", d, mid)]
    s = await fb.deliver("m1:/INBOX", spoof)
    assert ho_rows(store) == [("m1:/INBOX", d, mid)] and fts_mid(store, "zqspoof", mid) == 1
    start = len(fb.calls)
    if how == "delete":
        await fb.delete([s])
    else:
        await fb.move([s], "m1:/Trash")
    assert raw_calls(fb, {genuine}, start) == 0
    assert store.locations(mid) == [] and store.get_message(mid) is None
    assert store.connection.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'zqspoof'"
    ).fetchone() == (0,)
    assert ho_rows(store) == [("m1:/INBOX", d, mid)]
    assert [r["unresolved_events"] for r in runs(store, "event")] == [0, 0]
    assert w.hooks[-1] == (mid, "m1:/INBOX", None, "event")
    report = await w.rec.hourly()
    assert (report.repaged, report.missed) == (0, 0)


@pytest.mark.asyncio
async def test_hourly_cannot_tell_a_dropped_spoof_delete_from_an_eviction(
    store: Store, tmp_path: Path
) -> None:
    # As above, but the delete event is lost. Reconcile sees a located Message-ID whose only TB
    # copy in the folder is headers-only: the same state TB's eviction of an offline body leaves,
    # which keeps the location and its content (#30, user decision 2026-11-15; plan 04 merge
    # review M2). The spoof's text therefore stays (Known limitation in both module docstrings:
    # telling them apart would need the headers-only copy's body, Decision 8). Before M2 this
    # test was test_hourly_repairs_a_dropped_delete_of_a_spoof_of_a_headers_only_copy (dropped).
    fb, d, spoof = spoof_fb(tmp_path)
    (genuine,) = fb.tb_ids("m1:/INBOX")
    w = await wired(store, fb)
    mid = mid_of("dup@example.test")
    assert mid is not None
    s = await fb.deliver("m1:/INBOX", spoof)
    await w.rec.hourly()
    fb.drop_events(1)
    await fb.delete([s])
    assert fts_mid(store, "zqspoof", mid) == 1
    before = store.locations(mid)
    hooks = len(w.hooks)
    start = len(fb.calls)
    report = await w.rec.hourly()
    assert (report.removed, report.added, report.missed) == (0, 0, 0)
    assert raw_calls(fb, {genuine}, start) == 0
    assert store.locations(mid) == before and fts_mid(store, "zqspoof", mid) == 1
    assert ho_rows(store) == [("m1:/INBOX", d, mid)]
    assert len(w.hooks) == hooks
    again = await w.rec.hourly()
    assert (again.repaged, again.removed, again.added, again.missed) == (0, 0, 0, 0)
    assert store.locations(mid) == before


def chunk_count(store: Store, mid: str) -> int:
    (n,) = store.connection.execute("SELECT count(*) FROM chunks WHERE mid = ?", (mid,)).fetchone()
    return int(n)


@pytest.mark.parametrize("how", ["delete", "move"])
@pytest.mark.asyncio
async def test_deleting_a_headers_only_duplicate_of_an_evicted_location_keeps_it(
    store: Store, tmp_path: Path, how: str
) -> None:
    # Plan 04 M2 review round 1: two copies of one Message-ID in a folder, one headers-only from
    # the start. TB evicts the downloaded copy's body; reconcile keeps the location (#30) and
    # records a row per headers-only copy. Deleting (or moving away) the headers-only duplicate
    # must not drop the kept location: a recorded headers-only copy left, not a downloaded one
    # (unlike the spoof delete above, where the folder's rows match TB's remaining copies).
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    raws = [make_raw("dupe@example.test", d), make_raw("dupe@example.test", d + 5)]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    fb.add_mbox("m1", "Other", empty_mbox(tmp_path / "other.mbox"))
    evicted, dup = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(dup, True)
    w = await wired(store, fb)
    mid = mid_of("dupe@example.test")
    assert mid is not None and ho_rows(store) == [("m1:/INBOX", d + 5, mid)]
    before = store.locations(mid)
    chunks = chunk_count(store, mid)
    assert [loc.folder_id for loc in before] == ["m1:/INBOX"] and chunks > 0
    fb.set_headers_only(evicted, True)
    report = await w.rec.hourly()
    assert (report.removed, report.added, report.missed) == (0, 0, 0)
    assert store.locations(mid) == before
    assert ho_rows(store) == [("m1:/INBOX", d, mid), ("m1:/INBOX", d + 5, mid)]
    hooks = len(w.hooks)
    start = len(fb.calls)
    if how == "delete":
        await fb.delete([dup])
    else:
        await fb.move([dup], "m1:/Other")
    assert raw_calls(fb, set(fb.tb_ids("m1:/INBOX")), start) == 0
    assert store.locations(mid) == before and chunk_count(store, mid) == chunks
    left = [("m1:/INBOX", d, mid)] + ([("m1:/Other", d + 5, mid)] if how == "move" else [])
    assert ho_rows(store) == left
    assert [r["unresolved_events"] for r in runs(store, "event")] == [0]
    assert len(w.hooks) == hooks
    again = await w.rec.hourly()
    assert (again.repaged, again.removed, again.added, again.missed) == (0, 0, 0, 0)
    assert store.locations(mid) == before and ho_rows(store) == left


@pytest.mark.asyncio
async def test_a_stale_headers_only_row_never_keeps_a_deleted_spoofs_text(
    store: Store, tmp_path: Path
) -> None:
    # Plan 04 M2 review round 2: two headers-only genuine copies (rows at d and d+10), then a
    # downloaded spoof at d+5 takes their Message-ID. Deleting one genuine copy leaves its row
    # (the downloaded spoof keeps the location; the row goes at the next pass, as pinned by
    # test_a_deleted_headers_only_copy_of_a_located_mid_loses_its_row_at_the_next_pass). The
    # spoof's delete then sees more rows than TB's remaining copies, but the location (the
    # spoof's, at d+5) has no row of its own: it is not an evicted location, so it goes with
    # the spoof's text (T14), and the next pass does not take it back as an eviction.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    raws = [make_raw("dup@example.test", d), make_raw("dup@example.test", d + 10)]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    g1, g2 = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(g1, True)
    fb.set_headers_only(g2, True)
    w = await wired(store, fb)
    mid = mid_of("dup@example.test")
    assert mid is not None
    assert ho_rows(store) == [("m1:/INBOX", d, mid), ("m1:/INBOX", d + 10, mid)]
    spoof = make_raw("dup@example.test", d + 5, subject="spoof").replace(
        b"\nbody\n", b"\nzqspoof\n"
    )
    s = await fb.deliver("m1:/INBOX", spoof)
    assert fts_mid(store, "zqspoof", mid) == 1
    await fb.delete([g2])
    assert ho_rows(store) == [("m1:/INBOX", d, mid), ("m1:/INBOX", d + 10, mid)]  # stale row
    start = len(fb.calls)
    await fb.delete([s])
    assert raw_calls(fb, {g1}, start) == 0
    assert store.locations(mid) == [] and store.get_message(mid) is None
    assert fts_mid(store, "zqspoof", mid) == 0
    assert ho_rows(store) == [("m1:/INBOX", d, mid)]
    assert [r["unresolved_events"] for r in runs(store, "event")] == [0, 0, 0]
    report = await w.rec.hourly()
    assert (report.added, report.removed, report.missed) == (0, 0, 0)
    assert store.locations(mid) == [] and fts_mid(store, "zqspoof", mid) == 0


@pytest.mark.asyncio
async def test_moving_the_evicted_copy_away_from_its_headers_only_duplicate_drops_its_text(
    store: Store, tmp_path: Path
) -> None:
    # Plan 04 M2 review round 2: as in the duplicate test above, but the evicted copy itself
    # (at the location's tb_date) is moved away while the recorded headers-only duplicate stays.
    # A move carries the item's date, so this is told from the duplicate leaving: the source
    # location goes with the evicted copy's text (as for a lone evicted copy, pinned by
    # test_moving_an_evicted_message_drops_its_kept_content), the duplicate keeps its row.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    raws = [make_raw("dupe@example.test", d), make_raw("dupe@example.test", d + 5)]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    fb.add_mbox("m1", "Other", empty_mbox(tmp_path / "other.mbox"))
    evicted, dup = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(dup, True)
    w = await wired(store, fb)
    mid = mid_of("dupe@example.test")
    assert mid is not None
    fb.set_headers_only(evicted, True)
    await w.rec.hourly()
    assert [loc.folder_id for loc in store.locations(mid)] == ["m1:/INBOX"]
    assert ho_rows(store) == [("m1:/INBOX", d, mid), ("m1:/INBOX", d + 5, mid)]
    start = len(fb.calls)
    (moved,) = await fb.move([evicted], "m1:/Other")
    assert raw_calls(fb, {dup, moved}, start) == 0
    assert store.locations(mid) == [] and store.get_message(mid) is None
    assert chunk_count(store, mid) == 0
    assert ho_rows(store) == [("m1:/INBOX", d + 5, mid), ("m1:/Other", d, mid)]
    assert [r["unresolved_events"] for r in runs(store, "event")] == [0]
    assert w.hooks[-1] == (mid, "m1:/INBOX", None, "event")
    report = await w.rec.hourly()
    assert (report.added, report.removed, report.missed) == (0, 0, 0)
    assert store.locations(mid) == []
    assert ho_rows(store) == [("m1:/INBOX", d + 5, mid), ("m1:/Other", d, mid)]


@pytest.mark.asyncio
async def test_moving_an_evicted_message_drops_its_kept_content(
    store: Store, tmp_path: Path
) -> None:
    # Known limitation (tern.index.events, #30 amendment, plan 04 M2 review round 1): an evicted,
    # kept message moved to another folder loses its kept content. The source location goes
    # (TB lists no copy there) and the destination copy is headers-only: a row, never fetched
    # (Decision 8). Pins today's behaviour; carrying the content over is tracked in
    # plans/90-post-mvp.md (B5).
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("evicted@example.test", d)]))
    fb.add_mbox("m1", "Other", empty_mbox(tmp_path / "other.mbox"))
    (evicted,) = fb.tb_ids("m1:/INBOX")
    w = await wired(store, fb)
    mid = mid_of("evicted@example.test")
    assert mid is not None
    fb.set_headers_only(evicted, True)
    await w.rec.hourly()
    assert [loc.folder_id for loc in store.locations(mid)] == ["m1:/INBOX"]
    assert chunk_count(store, mid) > 0
    start = len(fb.calls)
    (moved,) = await fb.move([evicted], "m1:/Other")
    assert raw_calls(fb, {evicted, moved}, start) == 0
    assert store.locations(mid) == [] and store.get_message(mid) is None
    assert chunk_count(store, mid) == 0
    assert ("m1:/Other", d, mid) in ho_rows(store)
    assert w.hooks[-1] == (mid, "m1:/INBOX", None, "event")
    report = await w.rec.hourly()
    assert (report.added, report.removed, report.missed) == (0, 0, 0)
    assert store.locations(mid) == [] and ho_rows(store) == [("m1:/Other", d, mid)]


class UnreadableNew(IngestSource):
    """``raw`` of every tb_id not in ``readable`` (when set) gives ``tb_error``: TB fails to
    read a message under any tb_id it gets later (e.g. once moved)."""

    def __init__(self, fb: FakeBridge) -> None:
        super().__init__(fb)
        self.readable: set[int] | None = None

    async def raw(self, tb_id: int) -> bytes:
        if self.readable is not None and tb_id not in self.readable:
            raise BridgeError("tb_error", "synthetic: TB failed", tb_code="NS_ERROR_FAILURE")
        return await super().raw(tb_id)


@pytest.mark.asyncio
async def test_a_moved_message_unreadable_in_its_destination_drops_until_the_retry_reads_it(
    store: Store, tmp_path: Path
) -> None:
    # Known limitation (tern.index.events, plan 04 M5 review): the moved copy cannot be read in
    # its destination, so it gets a tb_error row there, and its source location (its last one)
    # goes: the message is dropped until a destination pass reads it, then added back as a
    # retry (not missed). Pins today's behaviour.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("moved@example.test", d)]))
    fb.add_mbox("m1", "Other", empty_mbox(tmp_path / "other.mbox"))
    (old,) = fb.tb_ids("m1:/INBOX")
    await Backfill(store, IngestSource(fb), TernConfig()).run()
    src = UnreadableNew(fb)
    events = EventIngest(store, src, TernConfig())
    rec = Reconcile(store, src, TernConfig(), events=events)
    fb.on_event = events.on_event
    removed: list[str] = []
    store.message_removed_observers.append(removed.append)
    mid = mid_of("moved@example.test")
    assert mid is not None
    src.readable = {old}
    await fb.move([old], "m1:/Other")
    assert store.locations(mid) == [] and store.get_message(mid) is None
    assert removed == [mid]
    skipped = store.connection.execute(
        "SELECT folder_id, tb_date, reason FROM skipped_items"
    ).fetchall()
    assert skipped == [("m1:/Other", d, "tb_error")]
    src.readable = None
    report = await rec.hourly()
    assert (report.added, report.retried, report.missed) == (1, 1, 0)
    assert [loc.folder_id for loc in store.locations(mid)] == ["m1:/Other"]
    assert store.connection.execute("SELECT count(*) FROM skipped_items").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_spoof_delete_queued_during_a_pass_is_left_to_its_event(
    store: Store, tmp_path: Path
) -> None:
    # The spoof is deleted while a pass runs, before its folder is paged: the pass sees only the
    # headers-only genuine copy, but the queued delete covers the Message-ID, so the pass leaves
    # it to the event. Were the pass to record it first, the event (finding no location) would
    # then drop the genuine's headers_only_items row as if it were the deleted item.
    fb, d, spoof = spoof_fb(tmp_path)
    mid = mid_of("dup@example.test")
    assert mid is not None
    to_delete: list[int] = []

    class DeleteBeforePage(IngestSource):
        async def page(
            self,
            folder_id: str,
            from_date: datetime | None = None,
            to_date: datetime | None = None,
            cursor: str | None = None,
        ) -> Page:
            if folder_id == "m1:/INBOX" and to_delete:
                await fb.delete([to_delete.pop()])
            return await super().page(folder_id, from_date, to_date, cursor)

    await Backfill(store, IngestSource(fb), TernConfig()).run()
    events = EventIngest(store, IngestSource(fb), TernConfig())
    rec = Reconcile(store, DeleteBeforePage(fb), TernConfig(), events=events)
    fb.on_event = events.on_event
    to_delete.append(await fb.deliver("m1:/INBOX", spoof))
    report = await rec.hourly()
    assert not to_delete and (report.removed, report.missed) == (0, 0)
    assert store.locations(mid) == [] and fts_mid(store, "zqspoof", mid) == 0
    assert ho_rows(store) == [("m1:/INBOX", d, mid)]
    assert [r["unresolved_events"] for r in runs(store, "event")] == [0, 0]


@pytest.mark.asyncio
async def test_a_removed_copy_whose_remaining_copy_is_downloaded_keeps_the_location(
    store: Store, tmp_path: Path
) -> None:
    # Two byte-identical downloaded copies of one Message-ID (collapsed into one location): one
    # deleted, the other still downloaded there, so the location and its content stay.
    d = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    raw = make_raw("twice@example.test", d)
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [raw, raw]))
    first, second = fb.tb_ids("m1:/INBOX")
    w = await wired(store, fb)
    mid = mid_of("twice@example.test")
    assert mid is not None
    start = len(fb.calls)
    await fb.delete([first])
    assert raw_calls(fb, {second}, start) == 0
    assert [loc.folder_id for loc in store.locations(mid)] == ["m1:/INBOX"]
    assert ho_rows(store) == [] and w.hooks == []
    assert [r["unresolved_events"] for r in runs(store, "event")] == [0]


@pytest.mark.asyncio
async def test_a_find_match_seen_in_no_page_leaves_the_removal_unresolved(
    store: Store, tmp_path: Path
) -> None:
    # `find` lists a copy that no page shows (it went away in between): whether a downloaded
    # copy remains cannot be told, so the event is unresolved and the hourly check runs.
    fb, d, spoof = spoof_fb(tmp_path)
    ghost = 987_654_321

    class Ghost(IngestSource):
        async def find(self, header_message_id: str) -> list[FoundLocation]:
            found = await super().find(header_message_id)
            return [*(f.model_copy(update={"tb_id": ghost}) for f in found[:1]), *found]

    await Backfill(store, IngestSource(fb), TernConfig()).run()
    events = EventIngest(store, Ghost(fb), TernConfig())
    Reconcile(store, IngestSource(fb), TernConfig(), events=events)
    fb.on_event = events.on_event
    s = await fb.deliver("m1:/INBOX", spoof)
    await fb.delete([s])
    assert [r["unresolved_events"] for r in runs(store, "event")] == [0, 1]
    assert [r["folder_id"] for r in runs(store, "hourly")] == ["m1:/INBOX"]
    assert not [m for m, p in fb.calls if m == "mail.messages.raw" and p["tb_id"] == ghost]
    # the hourly check sees a located Message-ID whose only TB copy there is headers-only, as
    # after an eviction, so it keeps the location (#30, plan 04 merge review M2; before M2 it
    # dropped it as the event path does): the spoof's text stays (Known limitation)
    mid = mid_of("dup@example.test")
    assert mid is not None
    assert [loc.folder_id for loc in store.locations(mid)] == ["m1:/INBOX"]
    assert fts_mid(store, "zqspoof", mid) == 1
    assert ho_rows(store) == [("m1:/INBOX", d, mid)]
    assert [r["missed"] for r in runs(store, "hourly")] == [0]


# ---- folders ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_renamed_folder_keeps_its_locations(store: Store, fb: FakeBridge) -> None:
    w = await wired(store, fb)
    old = "acct2:/Projects"
    before = {
        r[0]
        for r in store.connection.execute("SELECT mid FROM locations WHERE folder_id = ?", (old,))
    }
    assert len(before) == FOLDER_COUNTS[old]
    new = fb.rename_folder(old, "Projects-2026")
    await fb.folder_changed(new)
    counts = per_folder(store)
    assert counts.get(new) == FOLDER_COUNTS[old] and old not in counts
    after = {
        r[0]
        for r in store.connection.execute("SELECT mid FROM locations WHERE folder_id = ?", (new,))
    }
    assert after == before
    assert w.removed == []  # nothing was deleted and re-added
    st = store.status()
    assert (st.messages, st.locations) == (DISTINCT, COPIES)
    assert old not in {f.folder_id for f in store.folders()}
    assert store.connection.execute(
        "SELECT status FROM ingest_state WHERE folder_id = ?", (new,)
    ).fetchone() == ("done",)
    removals = [h for h in w.hooks if h[2] is None]
    assert {h[1] for h in removals} == {old} and len(removals) == FOLDER_COUNTS[old]


class Unlisted(IngestSource):
    """The extension leaves the folders in ``unlisted`` out of ``mail.folders.list`` (TB could
    not give their counts); a page of one raises ``page_error`` when set, else TB pages it."""

    def __init__(self, fb: FakeBridge) -> None:
        super().__init__(fb)
        self.unlisted: set[str] = set()
        self.page_error: BridgeError | None = None
        self.probes: list[tuple[str, datetime | None, datetime | None]] = []

    async def folders(self, account_id: str) -> list[Folder]:
        return [f for f in await super().folders(account_id) if f.folder_id not in self.unlisted]

    async def page(
        self,
        folder_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        cursor: str | None = None,
    ) -> Page:
        if folder_id in self.unlisted:
            self.probes.append((folder_id, from_date, to_date))
            if self.page_error is not None:
                raise self.page_error
        return await super().page(folder_id, from_date, to_date, cursor)


def unlisted_events(store: Store, fb: FakeBridge) -> tuple[Unlisted, EventIngest, list[Hook]]:
    src = Unlisted(fb)
    events = EventIngest(store, src, TernConfig())
    hooks: list[Hook] = []
    store.location_observers.append(lambda *a: hooks.append(a))
    return src, events, hooks


@pytest.mark.parametrize(
    "page_error",
    [None, BridgeError("tb_error", "Thunderbird call failed", tb_code="page_failed")],
    ids=["pages", "page_failed"],
)
@pytest.mark.asyncio
async def test_a_folder_left_out_of_the_listing_but_still_in_tb_keeps_its_locations(
    store: Store, fb: FakeBridge, caplog: pytest.LogCaptureFixture, page_error: BridgeError | None
) -> None:
    # The extension leaves out a folder whose counts TB cannot give (a local folder whose
    # summary is not built): not a deleted folder. TB still pages it, or fails it with another
    # code than folder_not_found, so nothing is removed and a count-only line says so.
    await Backfill(store, IngestSource(fb), TernConfig()).run()
    src, events, hooks = unlisted_events(store, fb)
    folder = "acct2:/Projects"
    src.unlisted = {folder}
    src.page_error = page_error
    before = per_folder(store)
    caplog.set_level(logging.INFO, logger="tern.index.events")
    listed = await events.sync_folders("reconcile")
    assert folder not in {i.folder_id for i, _ in listed}
    assert per_folder(store) == before and hooks == []
    assert folder in {f.folder_id for f in store.folders()}
    assert [(f, lo, hi) for f, lo, hi in src.probes] == [
        (folder, events_mod._PROBE_DATE, events_mod._PROBE_DATE)
    ]
    kept = [
        r for r in caplog.records if r.getMessage() == "unlisted folders kept: TB still has them"
    ]
    assert [r.__dict__["kept"] for r in kept] == [1]
    synced = [r for r in caplog.records if r.getMessage() == "folders synced"]
    assert [r.__dict__["gone"] for r in synced] == [0]
    assert folder not in caplog.text


@pytest.mark.asyncio
async def test_a_folder_left_out_of_the_listing_and_not_found_in_tb_loses_its_locations(
    store: Store, fb: FakeBridge
) -> None:
    await Backfill(store, IngestSource(fb), TernConfig()).run()
    src, events, hooks = unlisted_events(store, fb)
    folder = "acct2:/Projects"
    src.unlisted = {folder}
    src.page_error = BridgeError("tb_error", "Thunderbird call failed", tb_code="folder_not_found")
    await events.sync_folders("reconcile")
    assert folder not in per_folder(store)
    assert folder not in {f.folder_id for f in store.folders()}
    removals = [h for h in hooks if h[2] is None]
    assert {h[1] for h in removals} == {folder} and len(removals) == FOLDER_COUNTS[folder]


@pytest.mark.asyncio
async def test_folders_of_an_account_no_longer_listed_go_without_a_probe(
    store: Store, fb: FakeBridge
) -> None:
    # The extension leaves out folders, never accounts: an account TB no longer lists takes its
    # folders with it (after the M4 guard), and they are not paged first.
    await Backfill(store, IngestSource(fb), TernConfig()).run()

    class WithoutAcct2(Unlisted):
        async def accounts(self) -> list[Account]:
            return [a for a in await super().accounts() if a.account_id != "acct2"]

    src = WithoutAcct2(fb)
    src.unlisted = {f.folder_id for f in store.folders() if f.account_id == "acct2"}
    await EventIngest(store, src, TernConfig()).sync_folders("reconcile")
    assert src.probes == []
    assert not {f for f in per_folder(store) if f.startswith("acct2:")}


@pytest.mark.asyncio
async def test_a_bridge_failure_on_the_gone_probe_removes_nothing(
    store: Store, fb: FakeBridge
) -> None:
    await Backfill(store, IngestSource(fb), TernConfig()).run()
    src, events, hooks = unlisted_events(store, fb)
    src.unlisted = {"acct2:/Projects"}
    src.page_error = BridgeError("unavailable", "no reply")
    before = per_folder(store)
    with pytest.raises(BridgeError) as exc_info:
        await events.sync_folders("reconcile")
    assert exc_info.value.name == "unavailable"
    assert per_folder(store) == before and hooks == []


@pytest.mark.asyncio
async def test_a_folder_left_out_of_the_first_backfill_is_backfilled_once_listed(
    store: Store, fb: FakeBridge
) -> None:
    # The backfill indexes every folder the listing gives (one left out never fails the job);
    # the left-out folder is backfilled by the first re-list that gives it.
    src, events, _hooks = unlisted_events(store, fb)
    folder = "acct2:/Projects"
    src.unlisted = {folder}
    report = await Backfill(store, src, TernConfig()).run()
    assert report.folders_done == report.folders_total
    assert folder not in per_folder(store)
    src.unlisted = set()
    await events.sync_folders("reconcile")
    assert per_folder(store)[folder] == FOLDER_COUNTS[folder]
    st = store.status()
    assert (st.messages, st.locations) == (DISTINCT, COPIES)


# ---- privacy -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logs_carry_no_names_paths_or_sentinels(
    store: Store, fb: FakeBridge, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    trash = fb.add_mbox("acct1", "Trash", empty_mbox(tmp_path / "t.mbox"), ["trash"])
    w = await wired(store, fb)
    sentinel_folder = next(f for f in FOLDER_COUNTS if "SENTINEL" in f)
    noid, _raw = noid_scanner(fb)
    await fb.delete([noid])
    await fb.move([await tb_id(fb, "inv-0077", "acct1:/INBOX")], trash)
    sentinel_tb = fb.tb_ids(sentinel_folder)
    await fb.update(sentinel_tb[0], flagged=True)
    fb.drop_events(1)
    await fb.delete([sentinel_tb[1]])
    new = fb.rename_folder(sentinel_folder, "SENTINEL-FOLDER-9fb649-renamed")
    await fb.folder_changed(new)
    await w.rec.hourly()
    await w.rec.daily()
    texts = []
    for r in caplog.records:
        extras = {
            k: v
            for k, v in r.__dict__.items()
            if k not in logging.LogRecord("", 0, "", 0, "", None, None).__dict__
        }
        texts.append(r.getMessage() + " " + json.dumps(extras, default=str, ensure_ascii=False))
    blob = "\n".join(texts)
    forbidden = [s["value"] for s in MANIFEST["sentinels"]]
    forbidden += [a["name"] for a in MANIFEST["accounts"]]
    forbidden += [*FOLDER_COUNTS, trash, new]
    forbidden += [f["path"] for a in MANIFEST["accounts"] for f in a["folders"]]
    forbidden += [f"{a['key']}/{f['path']}" for a in MANIFEST["accounts"] for f in a["folders"]]
    forbidden += tfm.mail_strings(FIXTURE, MANIFEST)  # subjects, From/To/Cc addresses (L4)
    assert sorted({f for f in forbidden if f in blob}) == []
    assert any("fold-" in t for t in texts)
