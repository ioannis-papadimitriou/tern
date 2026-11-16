"""Plan 04 step 8: `Reconcile` (hourly/daily passes, missed changes, buffering) over FakeBridge.

Expected numbers come from MANIFEST.json or from changes the tests make themselves (inline
`example.test` mail); none is typed in from a run.
"""

from __future__ import annotations

import asyncio
import json
import mailbox
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm

from tern.bridge.protocol import (
    RAW_MAX,
    Account,
    BridgeError,
    FindParams,
    Flags,
    Folder,
    FolderChanged,
    MailUpdated,
    Page,
    UpdatedItem,
)
from tern.config import TernConfig
from tern.index.events import EventIngest
from tern.index.ingest import Backfill, IngestError
from tern.index.mid import X_PREFIX, mid_from_raw, mid_of
from tern.index.reconcile import Reconcile
from tern.index.source import IngestSource
from tern.index.store import LocationState, Store
from tern.testing.fake_bridge import FakeBridge

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
MESSAGES = tfm.messages(MANIFEST)
COPIES = MANIFEST["message_count"]
DISTINCT = MANIFEST["distinct_messages"]
INV_MID = MANIFEST["named"]["inv-0077"]["mid"]
NOW = int(datetime(2026, 11, 14, tzinfo=UTC).timestamp())
DAY = 86_400

Hook = tuple[str, str, LocationState | None, str]


def hmid(key: str) -> str:
    return MESSAGES[key]["message_id"].strip("<>")


def single_copy_keys(folder: str) -> list[str]:
    """Messages with a Message-ID whose only copy is in ``folder`` (MANIFEST form a/b), by
    date."""
    keys = [
        m["key"]
        for m in MANIFEST["messages"]
        if m["mid"] and [c["folder"] for c in m["copies"]] == [folder]
    ]
    return sorted(keys, key=date_of)


def single_copy_key(folder: str) -> str:
    return single_copy_keys(folder)[0]


def date_of(key: str) -> int:
    raw = tfm.read_copy(FIXTURE, MANIFEST, MESSAGES[key]["copies"][0])
    return int(parsedate_to_datetime(str(tfm.parse(raw)["Date"])).timestamp())


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


def mini_mailbox(root: Path, raws: list[bytes]) -> Path:
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


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "data" / "index.db")
    yield s
    s.close()


@pytest.fixture
def fb() -> FakeBridge:
    return FakeBridge(FIXTURE)


async def setup(
    store: Store, fb: FakeBridge, source: IngestSource | None = None, clock: Any = None
) -> tuple[EventIngest, Reconcile, list[Hook]]:
    """Full backfill, then events wired to `fb` and a Reconcile; returns the hook calls made
    after the backfill."""
    kw: dict[str, Any] = {} if clock is None else {"clock": clock}
    await Backfill(store, IngestSource(fb), TernConfig(), **kw).run()
    events = EventIngest(store, IngestSource(fb), TernConfig(), **kw)
    rec = Reconcile(store, source or IngestSource(fb), TernConfig(), events=events, **kw)
    fb.on_event = events.on_event
    hooks: list[Hook] = []
    store.location_observers.append(lambda *a: hooks.append(a))
    return events, rec, hooks


def runs(store: Store, kind: str) -> list[dict[str, Any]]:
    cur = store.connection.execute(
        "SELECT * FROM ingest_runs WHERE kind = ? ORDER BY rowid", (kind,)
    )
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def missed(store: Store, kind: str) -> int:
    return sum(r["missed"] for r in runs(store, kind))


async def tb_id(fb: FakeBridge, key: str, folder_id: str) -> int:
    found = await fb.request("mail.messages.find", FindParams(header_message_id=hmid(key)))
    (one,) = [f.tb_id for f in found.root if f.folder_id == folder_id]
    return one


def raw_calls(fb: FakeBridge, tb_ids: set[int], start: int = 0) -> int:
    return sum(1 for m, p in fb.calls[start:] if m == "mail.messages.raw" and p["tb_id"] in tb_ids)


# ---- clean index ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hourly_after_a_clean_ingest_finds_nothing(store: Store, fb: FakeBridge) -> None:
    _events, rec, hooks = await setup(store, fb)
    start = len(fb.calls)
    report = await rec.hourly()
    assert (report.missed, report.repaged, report.added, report.removed) == (0, 0, 0, 0)
    assert report.folders == len(runs(store, "backfill")) and hooks == []
    assert not [m for m, _ in fb.calls[start:] if m in ("mail.messages.raw", "mail.messages.full")]
    totals = {f["key"]: f["count"] for a in MANIFEST["accounts"] for f in a["folders"]}
    for row in store.folders():
        key = row.folder_id.replace(":/", "/")
        assert row.tb_total == totals[key] and row.last_reconciled_at is not None
    daily = await rec.daily()
    assert (daily.missed, daily.state_changes, daily.repaged) == (0, 0, 0)


# ---- the dropped-events scenario -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_dropped_events_are_repaired_and_counted(store: Store, fb: FakeBridge) -> None:
    _events, rec, hooks = await setup(store, fb)
    moved = single_copy_key("acct2/Projects")
    tagged = single_copy_key("acct1/Archive")
    deleted, other, *_rest, newest = single_copy_keys("acct1/Newsletters")
    inside = date_of(other)  # strictly inside an existing window, older than the newest
    assert inside < date_of(newest)
    fb.drop_events(4)
    await fb.move([await tb_id(fb, moved, "acct2:/Projects")], "acct2:/INBOX")
    await fb.update(await tb_id(fb, tagged, "acct1:/Archive"), tags=["$label1"])
    await fb.delete([await tb_id(fb, deleted, "acct1:/Newsletters")])
    await fb.deliver("acct1:/Newsletters", make_raw("dropped-new@example.test", inside))
    assert fb.dropped_events == 4 and hooks == []

    hourly = await rec.hourly()
    assert (hourly.added, hourly.removed, hourly.state_changes) == (2, 2, 0)
    assert missed(store, "hourly") == 4
    new_mid = mid_of("dropped-new@example.test")
    assert [loc.folder_id for loc in store.locations(MESSAGES[moved]["mid"])] == ["acct2:/INBOX"]
    assert store.get_message(MESSAGES[deleted]["mid"]) is None
    assert new_mid and [loc.folder_id for loc in store.locations(new_mid)] == ["acct1:/Newsletters"]
    # hourly leaves the tag alone (A7)
    (arch,) = store.locations(MESSAGES[tagged]["mid"])
    assert arch.state.tags == ()

    daily = await rec.daily()
    assert (daily.state_changes, daily.added, daily.removed) == (1, 0, 0)
    assert missed(store, "daily") == 1
    assert missed(store, "hourly") + missed(store, "daily") == 5
    (arch,) = store.locations(MESSAGES[tagged]["mid"])
    assert arch.state.tags == ("$label1",)
    assert len(hooks) == 5 and {h[3] for h in hooks} == {"reconcile"}
    assert sorted((h[1], h[2] is None) for h in hooks if h[2] is None) == [
        ("acct1:/Newsletters", True),
        ("acct2:/Projects", True),
    ]
    st = store.status()
    assert (st.messages, st.locations) == (DISTINCT, COPIES)


@pytest.mark.asyncio
async def test_hourly_leaves_tags_of_a_repaged_window_to_daily(
    store: Store, fb: FakeBridge
) -> None:
    _events, rec, hooks = await setup(store, fb)
    tagged = single_copy_key("acct1/Archive")
    fb.drop_events(2)
    await fb.update(await tb_id(fb, tagged, "acct1:/Archive"), flagged=True, tags=["$label2"])
    # same second as the tagged message: the window is certainly re-paged
    await fb.deliver("acct1:/Archive", make_raw("same-window@example.test", date_of(tagged)))
    hourly = await rec.hourly()
    assert (hourly.repaged, hourly.added, hourly.state_changes, hourly.missed) == (1, 1, 0, 1)
    (loc,) = store.locations(MESSAGES[tagged]["mid"])
    assert (loc.state.tags, loc.state.flagged) == ((), False)
    daily = await rec.daily()
    assert (daily.state_changes, daily.missed) == (1, 1)
    (loc,) = store.locations(MESSAGES[tagged]["mid"])
    assert (loc.state.tags, loc.state.flagged) == (("$label2",), True)
    assert [h[3] for h in hooks] == ["reconcile", "reconcile"]


@pytest.mark.asyncio
async def test_hourly_never_reapplies_the_state_of_a_held_id_less_copy(
    store: Store, fb: FakeBridge
) -> None:
    # An un-evented tag change on an ID-less message, plus an un-evented ID-less delivery at the
    # same second: hourly re-fetches both md5: items at that second, adds the new one, and must
    # leave the held copy's state to daily (which tells the two apart by fetching them).
    _events, rec, hooks = await setup(store, fb)
    copy = MESSAGES["noid-scanner"]["copies"][0]
    noid = fb.tb_ids("acct1:/INBOX")[copy["locator"]]
    raw = tfm.read_copy(FIXTURE, MANIFEST, copy)
    assert await IngestSource(fb).raw(noid) == raw  # pins the locator
    x_mid = mid_from_raw(raw)
    d = int(parsedate_to_datetime(str(tfm.parse(raw)["Date"])).timestamp())
    fb.drop_events(2)
    await fb.update(noid, tags=["$label3"])
    await fb.deliver("acct1:/INBOX", make_raw(None, d, subject="same second, no id"))
    hourly = await rec.hourly()
    assert (hourly.added, hourly.removed, hourly.state_changes) == (1, 0, 0)
    assert store.status().x_mids == 4
    (loc,) = store.locations(x_mid)
    assert loc.state.tags == ()
    daily = await rec.daily()
    assert (daily.state_changes, daily.missed) == (1, 1)
    (loc,) = store.locations(x_mid)
    assert loc.state.tags == ("$label3",)
    assert [(h[0] == x_mid, h[3]) for h in hooks] == [(False, "reconcile"), (True, "reconcile")]


@pytest.mark.asyncio
async def test_dropped_delete_of_an_id_less_message_is_repaired(
    store: Store, fb: FakeBridge
) -> None:
    _events, rec, hooks = await setup(store, fb)
    copy = MESSAGES["noid-scanner"]["copies"][0]
    noid = fb.tb_ids("acct1:/INBOX")[copy["locator"]]
    noid_raw = tfm.read_copy(FIXTURE, MANIFEST, copy)
    assert await IngestSource(fb).raw(noid) == noid_raw  # pins the locator
    x_mid = mid_from_raw(noid_raw)
    assert x_mid.startswith(X_PREFIX) and store.get_message(x_mid) is not None
    fb.drop_events(1)
    await fb.delete([noid])
    report = await rec.hourly()
    assert (report.removed, report.missed) == (1, 1)
    assert store.get_message(x_mid) is None and store.status().x_mids == 2
    assert hooks == [(x_mid, "acct1:/INBOX", None, "reconcile")]


@pytest.mark.asyncio
async def test_events_applied_before_backfill_is_done_cover_no_later_change(
    store: Store, fb: FakeBridge
) -> None:
    # An event applied while the folder's backfill is not done must not hide a genuine missed
    # change from the first pass after the backfill completes.
    _events, rec, hooks = await setup(store, fb)
    folder = "acct1:/Archive"
    key = single_copy_key("acct1/Archive")
    with store.transaction():
        store.connection.execute(
            "UPDATE ingest_state SET status = 'backfill' WHERE folder_id = ?", (folder,)
        )
    await fb.update(await tb_id(fb, key, folder), tags=["$label1"])
    with store.transaction():
        store.connection.execute(
            "UPDATE ingest_state SET status = 'done' WHERE folder_id = ?", (folder,)
        )
    fb.drop_events(1)
    await fb.delete([await tb_id(fb, key, folder)])
    report = await rec.hourly()
    assert (report.removed, report.missed) == (1, 1)
    assert [h[3] for h in hooks] == ["event", "reconcile"]


# ---- counters across passes ----------------------------------------------------------------------


def run_sum(store: Store, column: str) -> int:
    query = f"SELECT coalesce(sum({column}), 0) FROM ingest_runs"
    return store.connection.execute(query).fetchone()[0]


@pytest.mark.asyncio
async def test_an_altered_hmid_counts_once_over_backfill_and_hourly_passes(
    store: Store, fb: FakeBridge
) -> None:
    """The altered item's window never matches, so every hourly pass re-pages and re-fetches
    it; ``hmid_mismatch`` counts the item once (its first location), not once per pass."""
    target = hmid("invoice-0013")

    class Altering(IngestSource):
        async def page(self, folder_id: str, *a: Any, **kw: Any) -> Page:
            page = await super().page(folder_id, *a, **kw)
            items = [
                i.model_copy(update={"header_message_id": "altered@example.test"})
                if i.header_message_id == target
                else i
                for i in page.items
            ]
            return page.model_copy(update={"items": items})

    await Backfill(store, Altering(fb), TernConfig()).run()
    assert run_sum(store, "hmid_mismatch") == 1
    rec = Reconcile(store, Altering(fb), TernConfig())
    for _ in range(2):
        report = await rec.hourly()
        assert report.repaged == 1  # the item's window is re-paged (and re-fetched) every pass
    assert run_sum(store, "hmid_mismatch") == 1
    st = store.status()
    assert (st.messages, st.locations) == (DISTINCT, COPIES)


@pytest.mark.asyncio
async def test_an_unevented_move_of_an_id_less_message_is_not_x_mid_churn(
    store: Store, fb: FakeBridge
) -> None:
    """The source folder is passed first: its pass removes the x- mid's last location and the
    destination's pass inserts it again. The mid is not new, so ``x_mid_churn`` stays 0."""
    _events, rec, _hooks = await setup(store, fb)
    src, dst = "acct1:/INBOX", "acct1:/Invoices"
    listed = await Backfill(store, IngestSource(fb), TernConfig()).list_folders()
    order = [f.folder_id for f in listed]
    assert order.index(src) < order.index(dst)  # the source folder is passed first
    copy = MESSAGES["noid-scanner"]["copies"][0]
    noid = fb.tb_ids(src)[copy["locator"]]
    noid_raw = tfm.read_copy(FIXTURE, MANIFEST, copy)
    assert await IngestSource(fb).raw(noid) == noid_raw  # pins the locator
    x_mid = mid_from_raw(noid_raw)
    assert x_mid.startswith(X_PREFIX)
    x_mids = store.status().x_mids
    fb.drop_events(1)
    await fb.move([noid], dst)
    report = await rec.hourly()
    assert (report.removed, report.added) == (1, 1)
    assert [loc.folder_id for loc in store.locations(x_mid)] == [dst]
    assert run_sum(store, "x_mid_churn") == 0
    assert store.status().x_mids == x_mids


# ---- buffering ----------------------------------------------------------------------------------


@pytest.mark.parametrize("window", ["paged", "later"])
@pytest.mark.asyncio
async def test_a_deliver_during_a_pass_is_queued_and_applied_once(
    store: Store, fb: FakeBridge, window: str
) -> None:
    # "paged": delivered right after the window holding its date was paged (the pass must not
    # remove what the event adds); "later": delivered after the pass's first page call, into a
    # window the pass pages afterwards (the pass leaves the change to the queued event).
    target = "acct1:/Newsletters"
    key = single_copy_key("acct1/Newsletters")
    d = date_of(key)
    raw = make_raw("mid-pass@example.test", d)
    new_mid = mid_of("mid-pass@example.test")
    delivered: list[int] = []

    class DeliverAfterPage(IngestSource):
        async def page(
            self,
            folder_id: str,
            from_date: datetime | None = None,
            to_date: datetime | None = None,
            cursor: str | None = None,
        ) -> Page:
            page = await super().page(folder_id, from_date, to_date, cursor)
            lo, hi = from_date, to_date
            covers = (lo is None or lo.timestamp() <= d) and (hi is None or d <= hi.timestamp())
            if window == "later":
                assert not (covers and hi is None), "the first window must not hold the date"
                covers = True
            if folder_id == target and not delivered and cursor is None and covers:
                # between this page call and the next one, after this window was paged
                delivered.append(await fb.deliver(target, raw))
            return page

    _events, rec, hooks = await setup(store, fb, source=DeliverAfterPage(fb))
    start = len(fb.calls)
    report = await rec.hourly()
    assert delivered and report.missed == 0 and report.removed == 0
    assert new_mid and [loc.folder_id for loc in store.locations(new_mid)] == [target]
    assert raw_calls(fb, set(delivered), start) == 1  # applied once
    assert [h for h in hooks if h[0] == new_mid] == [(new_mid, target, LocationState(), "event")]
    assert hooks == [h for h in hooks if h[0] == new_mid]


# ---- re-page idempotence, late downloads, refills -----------------------------------------------


@pytest.mark.asyncio
async def test_repage_of_headers_only_window_is_idempotent(store: Store, tmp_path: Path) -> None:
    d = NOW - 3 * DAY
    raws = [
        make_raw("ho-a@example.test", d),
        make_raw(None, d, subject="ho no id"),
        make_raw("kept@example.test", d - 5),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    a, b, _c = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(a, True)
    fb.set_headers_only(b, True)
    _events, rec, hooks = await setup(store, fb, clock=lambda: NOW)
    query = "SELECT tb_date, ordinal, mid FROM headers_only_items ORDER BY tb_date, ordinal"
    rows = store.connection.execute(query).fetchall()
    assert rows == [(d, 0, mid_of("ho-a@example.test")), (d, 1, None)]
    for _ in range(2):
        res = await rec.repage_window("m1:/INBOX", NOW - 30 * DAY, None)
        assert (res.added, res.removed) == (0, 0)
        assert store.connection.execute(query).fetchall() == rows
        assert store.status().headers_only == 2 and store.status().messages == 1
    assert hooks == []


@pytest.mark.asyncio
async def test_hourly_ingests_late_downloads_without_counting_them(
    store: Store, tmp_path: Path
) -> None:
    d = NOW - 3 * DAY
    raws = [make_raw("late@example.test", d), make_raw(None, d - 9, subject="late no id")]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    a, b = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(a, True)
    fb.set_headers_only(b, True)
    _events, rec, _hooks = await setup(store, fb, clock=lambda: NOW)
    assert store.status().headers_only == 2
    fb.set_headers_only(a, False)
    fb.set_headers_only(b, False)
    report = await rec.hourly()
    assert (report.added, report.missed) == (2, 0)
    st = store.status()
    assert (st.messages, st.headers_only, st.x_mids) == (2, 0, 1)
    assert sum(r["x_mid_churn"] for r in runs(store, "hourly")) == 0


@pytest.mark.asyncio
async def test_hourly_fills_pending_refills(store: Store, fb: FakeBridge) -> None:
    _events, rec, _hooks = await setup(store, fb)
    spoof = await tb_id(fb, "inv-0077", "acct1:/INBOX")
    fb.drop_events(1)
    await fb.delete([spoof])
    # a removal that could not fetch a refill: the content is cleared, refill pending
    store.remove_location(INV_MID, "acct1:/INBOX", source="event")
    assert store.status().refill_pending == 1
    report = await rec.hourly()
    assert report.refilled == 1 and report.missed == 0
    inv = store.get_message(INV_MID)
    assert inv is not None and inv.from_addr == "billing@acme.example.com"
    assert store.status().refill_pending == 0 and not inv.conflicted


@pytest.mark.asyncio
async def test_a_pending_refill_whose_copy_is_unreadable_waits(
    store: Store, fb: FakeBridge
) -> None:
    # Plan 04 M5: TB failing to read the refill copy (tb_error) leaves the refill pending
    # instead of failing the pass; the next pass that can read it fills it.
    src = StaleRaw(fb)
    _events, rec, _hooks = await setup(store, fb, source=src)
    spoof = await tb_id(fb, "inv-0077", "acct1:/INBOX")
    genuine = await tb_id(fb, "inv-0077", "acct1:/Invoices")
    fb.drop_events(1)
    await fb.delete([spoof])
    store.remove_location(INV_MID, "acct1:/INBOX", source="event")
    src.unreadable.add(genuine)
    report = await rec.hourly()
    assert report.refilled == 0 and genuine in src.raws
    assert store.status().refill_pending == 1
    src.unreadable.clear()
    again = await rec.hourly()
    assert again.refilled == 1 and store.status().refill_pending == 0


@pytest.mark.asyncio
async def test_daily_skips_an_unreadable_id_less_item_and_refreshes_the_others(
    store: Store, tmp_path: Path
) -> None:
    # Plan 04 M5: two ID-less messages at one second are told apart by fetching them; TB cannot
    # read one. The daily pass leaves that one's state and still applies the other's change.
    d = NOW - 3 * DAY
    raw_a, raw_b = make_raw(None, d, subject="a, no id"), make_raw(None, d, subject="b, no id")
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [raw_a, raw_b]))
    a, b = fb.tb_ids("m1:/INBOX")
    src = StaleRaw(fb)
    _events, rec, _hooks = await setup(store, fb, source=src, clock=lambda: NOW)
    fb.drop_events(2)
    await fb.update(a, tags=["$label1"])
    await fb.update(b, tags=["$label2"])
    src.unreadable.add(b)
    daily = await rec.daily()
    assert daily.state_changes == 1 and b in src.raws
    (loc_a,) = store.locations(mid_from_raw(raw_a))
    (loc_b,) = store.locations(mid_from_raw(raw_b))
    assert (loc_a.state.tags, loc_b.state.tags) == (("$label1",), ())


@pytest.mark.asyncio
async def test_reconcile_removal_refills_from_the_remaining_copy(
    store: Store, fb: FakeBridge
) -> None:
    _events, rec, hooks = await setup(store, fb)
    spoof = await tb_id(fb, "inv-0077", "acct1:/INBOX")
    fb.drop_events(1)
    await fb.delete([spoof])
    report = await rec.hourly()
    assert (report.removed, report.missed) == (1, 1)
    inv = store.get_message(INV_MID)
    assert inv is not None and not inv.conflicted
    assert inv.from_addr == "billing@acme.example.com"
    assert store.status().refill_pending == 0
    assert hooks == [(INV_MID, "acct1:/INBOX", None, "reconcile")]


@pytest.mark.asyncio
async def test_repage_keeps_a_skipped_message_id_item_outside_refetched_dates(
    store: Store, tmp_path: Path
) -> None:
    # An item TB lists with a (non-md5:) id, over RAW_MAX, whose full headers carry no
    # Message-ID: a `skipped_items` row. Found by a re-page at a tb_date whose ID-less counts
    # match, it must keep its row (as Backfill writes it), and a second pass must not double it.
    d = NOW - 3 * DAY
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("kept@example.test", d)]))
    big: set[int] = set()

    class Big(IngestSource):
        async def page(
            self,
            folder_id: str,
            from_date: datetime | None = None,
            to_date: datetime | None = None,
            cursor: str | None = None,
        ) -> Page:
            page = await super().page(folder_id, from_date, to_date, cursor)
            items = [
                i.model_copy(
                    update={"header_message_id": "tb-made@example.test", "size": RAW_MAX + 1}
                )
                if i.tb_id in big
                else i
                for i in page.items
            ]
            return page.model_copy(update={"items": items})

    _events, rec, hooks = await setup(store, fb, source=Big(fb), clock=lambda: NOW)
    fb.drop_events(1)
    big.add(await fb.deliver("m1:/INBOX", make_raw(None, d - 60, subject="large, no id")))
    query = "SELECT tb_date, ordinal, reason FROM skipped_items"
    for _ in range(2):
        report = await rec.hourly()
        assert (report.added, report.removed, report.repaged) == (0, 0, 1)
        assert store.connection.execute(query).fetchall() == [(d - 60, 0, "large_noid")]
    assert hooks == [] and store.status().messages == 1


@pytest.mark.asyncio
async def test_repage_rebuilds_a_skipped_message_id_row_shared_with_an_id_less_item(
    store: Store, tmp_path: Path
) -> None:
    # The skipped Message-ID item's row (ID-less) already exists; an un-evented ID-less item
    # then arrives at the same second, so that second's ID-less counts match and only the named
    # sets differ. The re-page must not double the row, and must ingest the ID-less item.
    d = NOW - 3 * DAY
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("kept@example.test", d)]))
    big: set[int] = set()

    class Big(IngestSource):
        async def page(
            self,
            folder_id: str,
            from_date: datetime | None = None,
            to_date: datetime | None = None,
            cursor: str | None = None,
        ) -> Page:
            page = await super().page(folder_id, from_date, to_date, cursor)
            items = [
                i.model_copy(
                    update={"header_message_id": "tb-made@example.test", "size": RAW_MAX + 1}
                )
                if i.tb_id in big
                else i
                for i in page.items
            ]
            return page.model_copy(update={"items": items})

    _events, rec, hooks = await setup(store, fb, source=Big(fb), clock=lambda: NOW)
    fb.drop_events(2)
    big.add(await fb.deliver("m1:/INBOX", make_raw(None, d - 60, subject="large, no id")))
    query = "SELECT tb_date, ordinal, reason FROM skipped_items"
    await rec.hourly()
    assert store.connection.execute(query).fetchall() == [(d - 60, 0, "large_noid")]
    await fb.deliver("m1:/INBOX", make_raw(None, d - 60, subject="small, no id"))
    report = await rec.hourly()
    assert (report.added, report.removed, report.missed) == (1, 0, 1)
    assert store.connection.execute(query).fetchall() == [(d - 60, 0, "large_noid")]
    assert store.status().x_mids == 1 and store.status().messages == 2
    again = await rec.hourly()
    assert (again.added, again.removed) == (0, 0)
    assert store.connection.execute(query).fetchall() == [(d - 60, 0, "large_noid")]
    assert [h[3] for h in hooks] == ["reconcile"]


@pytest.mark.asyncio
async def test_a_stale_refetched_date_keeps_its_skipped_rows_without_doubling(
    store: Store, tmp_path: Path
) -> None:
    # At one second: an ID-less item over RAW_MAX (a skipped row), an indexed ID-less item whose
    # re-fetch goes stale, and an un-evented new ID-less item. The re-fetched second is not
    # handled (stale), so it keeps its rows: the skipped item fetched again must not add one.
    d = NOW - 3 * DAY
    raws = [
        make_raw("kept@example.test", d),
        make_raw(None, d - 60, subject="large, no id"),
        make_raw(None, d - 60, subject="goes stale"),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    _kept, big, going = fb.tb_ids("m1:/INBOX")
    stale: set[int] = set()

    class Src(IngestSource):
        async def page(
            self,
            folder_id: str,
            from_date: datetime | None = None,
            to_date: datetime | None = None,
            cursor: str | None = None,
        ) -> Page:
            page = await super().page(folder_id, from_date, to_date, cursor)
            items = [
                i.model_copy(update={"size": RAW_MAX + 1}) if i.tb_id == big else i
                for i in page.items
            ]
            return page.model_copy(update={"items": items})

        async def raw(self, tb_id: int) -> bytes:
            if tb_id in stale:
                raise BridgeError("stale_id", "tb_id is not valid in this session")
            return await super().raw(tb_id)

    src = Src(fb)
    cfg = TernConfig()
    await Backfill(store, src, cfg, clock=lambda: NOW).run()
    query = "SELECT tb_date, ordinal, reason FROM skipped_items"
    assert store.connection.execute(query).fetchall() == [(d - 60, 0, "large_noid")]
    events = EventIngest(store, src, cfg, clock=lambda: NOW)
    rec = Reconcile(store, src, cfg, events=events, clock=lambda: NOW)
    fb.on_event = events.on_event
    fb.drop_events(1)
    await fb.deliver("m1:/INBOX", make_raw(None, d - 60, subject="new, no id"))
    stale.add(going)
    report = await rec.hourly()
    assert (report.added, report.removed) == (1, 0)
    assert store.connection.execute(query).fetchall() == [(d - 60, 0, "large_noid")]
    assert store.status().x_mids == 2


# ---- serialized passes ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_passes_over_a_folder_do_not_overlap(store: Store, fb: FakeBridge) -> None:
    # A pass an event asks for may start while a scheduled pass runs (step 9's wiring): passes
    # must not overlap (they would split the applied-event coverage and remove a location twice).
    target = "acct1:/Newsletters"
    key = single_copy_key("acct1/Newsletters")
    depth: list[int] = []
    wired: list[EventIngest] = []

    class Yielding(IngestSource):
        async def page(
            self,
            folder_id: str,
            from_date: datetime | None = None,
            to_date: datetime | None = None,
            cursor: str | None = None,
        ) -> Page:
            await asyncio.sleep(0)  # the other pass runs here
            depth.append(wired[0]._paused.get(folder_id, 0))
            return await super().page(folder_id, from_date, to_date, cursor)

    events, rec, hooks = await setup(store, fb, source=Yielding(fb))
    wired.append(events)
    fb.drop_events(1)
    await fb.delete([await tb_id(fb, key, target)])
    a, b = await asyncio.gather(rec.hourly_folder(target), rec.hourly_folder(target))
    assert depth and max(depth) == 1
    assert a is not None and b is not None
    assert (a.removed + b.removed, a.missed + b.missed) == (1, 1)
    assert hooks == [(MESSAGES[key]["mid"], target, None, "reconcile")]


@pytest.mark.asyncio
async def test_a_check_asked_for_by_a_queued_event_runs_in_the_passing_task(
    store: Store, fb: FakeBridge
) -> None:
    # An md5: delete delivered during a pass over its folder is queued; applied when the pass
    # ends, it is unresolved and asks for that folder's hourly check, which runs in the same task
    # while the pass lock is still held: the lock must be re-entrant there (no deadlock).
    copy = MESSAGES["noid-scanner"]["copies"][0]
    noid = fb.tb_ids("acct1:/INBOX")[copy["locator"]]
    x_mid = mid_from_raw(tfm.read_copy(FIXTURE, MANIFEST, copy))
    deleted: list[int] = []

    class DeleteAfterPage(IngestSource):
        async def page(
            self,
            folder_id: str,
            from_date: datetime | None = None,
            to_date: datetime | None = None,
            cursor: str | None = None,
        ) -> Page:
            page = await super().page(folder_id, from_date, to_date, cursor)
            if folder_id == "acct1:/INBOX" and not deleted:
                deleted.append(noid)
                await fb.delete([noid])
            return page

    _events, rec, hooks = await setup(store, fb, source=DeleteAfterPage(fb))
    res = await asyncio.wait_for(rec.hourly_folder("acct1:/INBOX"), timeout=30)
    assert deleted and res is not None
    assert [r["unresolved_events"] for r in runs(store, "event")] == [1]
    inbox = [r for r in runs(store, "hourly") if r["folder_id"] == "acct1:/INBOX"]
    assert len(inbox) == 2 and sum(r["missed"] for r in inbox) == 0
    assert store.get_message(x_mid) is None
    assert hooks == [(x_mid, "acct1:/INBOX", None, "reconcile")]


@pytest.mark.asyncio
async def test_a_location_gone_during_a_pass_is_not_removed_again(
    store: Store, tmp_path: Path
) -> None:
    # Defence in depth beside the pass lock: a Message-ID location the pass saw in its window
    # but that is gone by the time of its removal (removed elsewhere) is skipped, as the x- loop
    # skips one, instead of raising NoSuchLocation.
    d = NOW - 3 * DAY
    fb = FakeBridge(
        mini_mailbox(
            tmp_path / "mb", [make_raw("a@example.test", d), make_raw("b@example.test", d)]
        )
    )
    a, _b = fb.tb_ids("m1:/INBOX")
    mid_a = mid_of("a@example.test")
    assert mid_a is not None
    trigger: list[int] = []

    class RemovesElsewhere(IngestSource):
        async def raw(self, tb_id: int) -> bytes:
            if tb_id in trigger:
                trigger.remove(tb_id)
                store.remove_location(mid_a, "m1:/INBOX", source="event")
            return await super().raw(tb_id)

    _events, rec, hooks = await setup(store, fb, source=RemovesElsewhere(fb), clock=lambda: NOW)
    fb.drop_events(2)
    await fb.delete([a])
    trigger.append(await fb.deliver("m1:/INBOX", make_raw("c@example.test", d - 10)))
    report = await rec.hourly()
    assert not trigger and (report.added, report.removed) == (1, 0)
    assert store.get_message(mid_a) is None and store.status().messages == 2
    assert [(h[0] == mid_a, h[3]) for h in hooks] == [(True, "event"), (False, "reconcile")]


@pytest.mark.parametrize("run", ["hourly_folder", "hourly"])
@pytest.mark.asyncio
async def test_a_pass_waiting_for_another_reads_the_folder_count_after_it(
    store: Store, fb: FakeBridge, run: str
) -> None:
    # A pass that waits behind another (an event's check behind a long scheduled pass) must read
    # TB's folder count once the lock is its own: a count read before the wait is stale by the
    # time it is stored in `folders.tb_total`.
    target = "acct1:/Newsletters"
    _events, rec, _hooks = await setup(store, fb)
    gate = asyncio.Event()
    held = asyncio.Event()

    async def long_pass() -> None:
        async with rec._passes.hold():
            held.set()
            await gate.wait()

    holder = asyncio.create_task(long_pass())
    await held.wait()
    waiting = asyncio.create_task(
        rec.hourly_folder(target) if run == "hourly_folder" else rec.hourly()
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert not waiting.done()
    fb.drop_events(1)
    await fb.deliver(target, make_raw("late@example.test", NOW - 3 * DAY))
    gate.set()
    await holder
    await waiting
    (total,) = [f.tb_total for f in store.folders() if f.folder_id == target]
    assert total == len(fb.tb_ids(target))


# ---- re-fetched seconds: x- locations rebuilt only from what was fetched -------------------------


class TbMadeIds(IngestSource):
    """TB lists the items in ``named`` under a Message-ID of its own making although their raw
    has none (they are indexed under x- mids); ``raw`` of the items in ``stale`` goes stale."""

    def __init__(self, fb: FakeBridge) -> None:
        super().__init__(fb)
        self.named: set[int] = set()
        self.stale: set[int] = set()
        self.on_page: list[Any] = []

    async def page(
        self,
        folder_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        cursor: str | None = None,
    ) -> Page:
        page = await super().page(folder_id, from_date, to_date, cursor)
        while self.on_page:
            await self.on_page.pop()()
        items = [
            i.model_copy(update={"header_message_id": "tb-made@example.test"})
            if i.tb_id in self.named
            else i
            for i in page.items
        ]
        return page.model_copy(update={"items": items})

    async def raw(self, tb_id: int) -> bytes:
        if tb_id in self.stale:
            raise BridgeError("stale_id", "tb_id is not valid in this session")
        return await super().raw(tb_id)


@pytest.mark.parametrize("why", ["queued_event", "stale_fetch", "located_twin"])
@pytest.mark.asyncio
async def test_an_x_location_of_an_unfetched_message_id_item_is_kept(
    store: Store, tmp_path: Path, why: str
) -> None:
    # A Message-ID item whose raw has none is held under an x- mid, so its second's ID-less
    # counts mismatch on every pass and that second is re-fetched. When the item itself is not
    # fetched there (a queued event covers its Message-ID; or another item with that Message-ID
    # is located here, so it looked held) or its fetch goes stale, no fetched item gives its x-
    # mid: the location must stay, not be removed and counted as missed.
    d = NOW - 3 * DAY
    first = "tb-made@example.test" if why == "located_twin" else "kept@example.test"
    raws = [make_raw(first, d), make_raw(None, d - 60, subject="tb names it")]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    _kept, made = fb.tb_ids("m1:/INBOX")
    x_mid = mid_from_raw(raws[1])
    src = TbMadeIds(fb)
    src.named.add(made)
    events, rec, hooks = await setup(store, fb, source=src, clock=lambda: NOW)
    assert [loc.folder_id for loc in store.locations(x_mid)] == ["m1:/INBOX"]
    if why == "queued_event":
        update = MailUpdated(
            items=[
                UpdatedItem(
                    tb_id=made,
                    folder_id="m1:/INBOX",
                    header_message_id="tb-made@example.test",
                    flags=Flags(read=True, flagged=False, junk=False),
                    tags=[],
                )
            ]
        )
        src.on_page.append(lambda: events.on_event("event.mail.updated", update))
    elif why == "stale_fetch":
        src.stale.add(made)
    await rec.hourly_folder("m1:/INBOX")
    assert [loc.folder_id for loc in store.locations(x_mid)] == ["m1:/INBOX"]
    assert hooks == [] and missed(store, "hourly") == 0


# ---- Message-IDs whose TB items in the folder are all headers-only -------------------------------


@pytest.mark.asyncio
async def test_a_downloaded_copy_listed_under_another_hmid_keeps_its_location(
    store: Store, tmp_path: Path
) -> None:
    # TB lists the downloaded copy under a Message-ID of its own making (hmid_mismatch) and a
    # headers-only copy under the real one. The real Message-ID has no downloaded item by TB's
    # ids, but the pass fetches the mismatched item and learns its raw mid: the location is kept
    # (dropping it would churn: removed by one pass, added back by the next).
    d = NOW - 3 * DAY
    raws = [make_raw("real@example.test", d), make_raw("real@example.test", d + 5, subject="ho")]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    downloaded, ho = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(ho, True)
    mid = mid_of("real@example.test")
    assert mid is not None
    src = TbMadeIds(fb)
    src.named.add(downloaded)
    _events, rec, hooks = await setup(store, fb, source=src, clock=lambda: NOW)
    assert [loc.folder_id for loc in store.locations(mid)] == ["m1:/INBOX"]
    for _ in range(2):
        report = await rec.hourly()
        assert (report.removed, report.added) == (0, 0)
        assert [loc.folder_id for loc in store.locations(mid)] == ["m1:/INBOX"]
    assert hooks == [] and missed(store, "hourly") == 0


def ho_rows(store: Store) -> list[tuple[str, int, str | None]]:
    return store.connection.execute(
        "SELECT folder_id, tb_date, mid FROM headers_only_items ORDER BY folder_id, tb_date, mid"
    ).fetchall()


def chunk_count(store: Store, mid: str) -> int:
    return store.connection.execute("SELECT count(*) FROM chunks WHERE mid = ?", (mid,)).fetchone()[
        0
    ]


@pytest.mark.asyncio
async def test_eviction_keeps_message_id_and_x_locations(store: Store, tmp_path: Path) -> None:
    """#30, user decision 2026-11-15 (plan 04 merge review M2): when TB evicts the body of a
    folder's only copy (the item turns headers-only, no event), the location keeps its indexed
    content and last known state, for a Message-ID and an x- location alike. The Message-ID copy
    gets a ``headers_only_items`` row (as a re-page records one for every headers-only item, also
    of a located mid), the x- location none (one x- location matches one headers-only ``md5:``
    item). Not a location change: nothing counted removed or missed, no hook. The daily pass
    skips headers-only items, so a state change TB makes without an event is not applied. Stable
    across passes and re-pages (no churn)."""
    d = NOW - 3 * DAY
    raws = [make_raw("evicted@example.test", d), make_raw(None, d - 7, subject="evicted no id")]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    named, anon = fb.tb_ids("m1:/INBOX")
    _events, rec, hooks = await setup(store, fb, clock=lambda: NOW)
    named_mid = mid_of("evicted@example.test")
    assert named_mid is not None
    (x_mid,) = [
        str(r[0])
        for r in store.connection.execute("SELECT mid FROM locations WHERE mid LIKE 'x-%'")
    ]
    st = store.status()
    assert (st.messages, st.x_mids, st.headers_only) == (2, 1, 0)
    chunks = {m: chunk_count(store, m) for m in (named_mid, x_mid)}
    assert all(chunks.values())
    before = {m: store.locations(m) for m in (named_mid, x_mid)}

    fb.set_headers_only(named, True)
    fb.set_headers_only(anon, True)
    report = await rec.hourly()
    assert (report.added, report.removed, report.missed) == (0, 0, 0)
    assert {m: store.locations(m) for m in (named_mid, x_mid)} == before
    assert {m: chunk_count(store, m) for m in (named_mid, x_mid)} == chunks
    st = store.status()
    assert (st.messages, st.x_mids, st.headers_only) == (2, 1, 1)
    assert ho_rows(store) == [("m1:/INBOX", d, named_mid)]
    assert hooks == [] and missed(store, "hourly") == 0

    # TB marks both read without an event: the daily pass skips headers-only items
    fb.drop_events(2)
    await fb.update(named, read=True, flagged=True)
    await fb.update(anon, read=True)
    daily = await rec.daily()
    assert (daily.added, daily.removed, daily.state_changes, daily.missed) == (0, 0, 0, 0)
    assert {m: store.locations(m) for m in (named_mid, x_mid)} == before
    assert all(not loc.state.read for m in before for loc in store.locations(m))

    # a re-page of the folder leaves the same index (the rows are the re-page's own)
    repaged = await rec.repage_window("m1:/INBOX", None, None)
    assert (repaged.added, repaged.removed) == (0, 0)
    again = await rec.hourly()
    assert (again.repaged, again.added, again.removed, again.missed) == (0, 0, 0, 0)
    assert {m: store.locations(m) for m in (named_mid, x_mid)} == before
    assert ho_rows(store) == [("m1:/INBOX", d, named_mid)]
    assert store.status().headers_only == 1 and hooks == []


@pytest.mark.asyncio
async def test_a_kept_x_location_removed_at_a_skipped_item_second_gets_its_null_row(
    store: Store, tmp_path: Path
) -> None:
    # Plan 04 M2 review round 1. An ID-less item TB evicted (kept as an x- location, #30) shares
    # its second with a later Message-ID item over RAW_MAX whose full headers carry none (a
    # skipped row). The skipped item makes the re-page rebuild that second although its ID-less
    # counts match; only downloaded items can be fetched there, so the x- location goes (module
    # Known limitations). Its headers-only item must then get a NULL-mid row, as a fresh backfill
    # records it.
    d = NOW - 3 * DAY
    raws = [make_raw("kept@example.test", d), make_raw(None, d - 60, subject="evicted no id")]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    _kept, anon = fb.tb_ids("m1:/INBOX")
    big: set[int] = set()

    class Big(IngestSource):
        async def page(
            self,
            folder_id: str,
            from_date: datetime | None = None,
            to_date: datetime | None = None,
            cursor: str | None = None,
        ) -> Page:
            page = await super().page(folder_id, from_date, to_date, cursor)
            items = [
                i.model_copy(
                    update={"header_message_id": "tb-made@example.test", "size": RAW_MAX + 1}
                )
                if i.tb_id in big
                else i
                for i in page.items
            ]
            return page.model_copy(update={"items": items})

    _events, rec, _hooks = await setup(store, fb, source=Big(fb), clock=lambda: NOW)
    x_mid = mid_from_raw(raws[1])
    fb.set_headers_only(anon, True)
    report = await rec.hourly()
    assert (report.added, report.removed, report.missed) == (0, 0, 0)
    assert [loc.folder_id for loc in store.locations(x_mid)] == ["m1:/INBOX"]
    assert ho_rows(store) == []
    fb.drop_events(1)
    big.add(await fb.deliver("m1:/INBOX", make_raw(None, d - 60, subject="large, no id")))
    await rec.hourly()
    assert store.locations(x_mid) == []
    assert ho_rows(store) == [("m1:/INBOX", d - 60, None)]
    skipped = "SELECT tb_date, reason FROM skipped_items"
    assert store.connection.execute(skipped).fetchall() == [(d - 60, "large_noid")]


# ---- removals decided per pass, not per window (plan 04 M3) ------------------------------------

W = TernConfig().backfill_window_days * DAY


@pytest.mark.asyncio
@pytest.mark.parametrize("row_window", ["older", "newer"])
async def test_identical_copies_in_two_windows_never_lose_the_message(
    store: Store, tmp_path: Path, row_window: str
) -> None:
    # Two byte-identical copies of a Message-ID in one folder collapse into one location (#30);
    # TB dates them in different windows (as it dates mail whose Date it cannot read by
    # arrival). Deleting, without an event, the copy whose tb_date the row holds must not remove
    # the message: the other copy, in another window, still holds its content. "older": the row
    # holds the older window (passes page it last); "newer": the row holds the newer window
    # (paged first), which needs the older copy headers-only during the backfill.
    d_new, d_old = NOW - 3 * DAY, NOW - 3 * DAY - 2 * W
    raw = make_raw("twin@example.test", d_new)
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [raw, raw]))
    a, b = fb.tb_ids("m1:/INBOX")
    fb.set_date(b, datetime.fromtimestamp(d_old, UTC))
    if row_window == "newer":
        fb.set_headers_only(b, True)
    _events, rec, hooks = await setup(store, fb, clock=lambda: NOW)
    fb.set_headers_only(b, False)
    mid = mid_of("twin@example.test")
    assert mid is not None
    (loc,) = store.locations(mid)
    assert loc.tb_date == (d_old if row_window == "older" else d_new)
    held, other_date = (b, d_new) if loc.tb_date == d_old else (a, d_old)
    fb.drop_events(1)
    await fb.delete([held])

    report = await rec.hourly()
    assert (report.removed, report.added, report.missed) == (0, 0, 0)
    assert hooks == [] and store.get_message(mid) is not None
    (loc,) = store.locations(mid)
    assert loc.tb_date == other_date  # the row now names the remaining copy
    assert store.status().headers_only == 0
    again = await rec.hourly()
    assert (again.repaged, again.removed, again.added, again.missed) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_a_repage_never_prunes_a_same_folder_copy_held_in_another_window(
    store: Store, tmp_path: Path
) -> None:
    # Two differing copies of a Message-ID in one folder, in different windows: the row holds
    # the older one, the newer one is a same-folder copy. An unevented delivery re-pages the
    # older window, which alone lists only the row's copy; the newer copy is still in TB and
    # must stay held.
    d_new, d_old = NOW - 3 * DAY, NOW - 3 * DAY - 2 * W
    raws = [
        make_raw("pair@example.test", d_new, subject="newer"),
        make_raw("pair@example.test", d_old, subject="older"),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    a, _b = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(a, True)  # the backfill writes only the older copy: the row holds it
    _events, rec, hooks = await setup(store, fb, clock=lambda: NOW)
    fb.set_headers_only(a, False)
    mid = mid_of("pair@example.test")
    assert mid is not None
    await rec.hourly()  # writes the late-downloaded newer copy as a same-folder copy
    hashes = store.location_hashes(mid, "m1:/INBOX")
    (loc,) = store.locations(mid)
    assert len(hashes) == 2 and loc.tb_date == d_old

    fb.drop_events(1)
    await fb.deliver("m1:/INBOX", make_raw("nudge@example.test", d_old + 60))
    report = await rec.hourly()
    assert (report.added, report.removed, report.missed) == (1, 0, 1)
    assert store.location_hashes(mid, "m1:/INBOX") == hashes
    (loc,) = store.locations(mid)
    assert loc.tb_date == d_old
    assert [h[0] for h in hooks] == [mid_of("nudge@example.test")]


class StaleRaw(IngestSource):
    """``raw`` of the items in ``stale`` goes stale (as after a rename TB has not reported);
    ``raw`` of the items in ``unreadable`` gives ``tb_error`` (TB failed to read them)."""

    def __init__(self, fb: FakeBridge) -> None:
        super().__init__(fb)
        self.stale: set[int] = set()
        self.unreadable: set[int] = set()
        self.raws: list[int] = []

    async def raw(self, tb_id: int) -> bytes:
        self.raws.append(tb_id)
        if tb_id in self.stale:
            raise BridgeError("stale_id", "tb_id is not valid in this session")
        if tb_id in self.unreadable:
            raise BridgeError("tb_error", "synthetic: TB failed", tb_code="NS_ERROR_FAILURE")
        return await super().raw(tb_id)


async def twins_in_two_windows(
    store: Store, tmp_path: Path, message_id: str | None
) -> tuple[FakeBridge, StaleRaw, Reconcile, list[Hook], str, int, int, int]:
    """Two byte-identical copies, TB-dated in different windows, collapsed into one location;
    the copy whose tb_date the row holds is deleted without an event. Returns (fb, src, rec,
    hooks, mid, the remaining copy's tb_id, the row's tb_date, the remaining copy's tb_date)."""
    d_new, d_old = NOW - 3 * DAY, NOW - 3 * DAY - 2 * W
    raw = make_raw(message_id, d_new)
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [raw, raw]))
    a, b = fb.tb_ids("m1:/INBOX")
    fb.set_date(b, datetime.fromtimestamp(d_old, UTC))
    src = StaleRaw(fb)
    _events, rec, hooks = await setup(store, fb, source=src, clock=lambda: NOW)
    mid = mid_from_raw(raw)
    assert mid.startswith(X_PREFIX) == (message_id is None)
    (loc,) = store.locations(mid)
    assert loc.tb_date in (d_new, d_old)
    held, other, other_date = (b, a, d_new) if loc.tb_date == d_old else (a, b, d_old)
    fb.drop_events(1)
    await fb.delete([held])
    return fb, src, rec, hooks, mid, other, loc.tb_date, other_date


@pytest.mark.asyncio
async def test_identical_id_less_copies_in_two_windows_never_lose_the_message(
    store: Store, tmp_path: Path
) -> None:
    # The x- form of the M3 test: mail without a Message-ID, two byte-identical copies TB-dated
    # in different windows; the copy the row names is deleted without an event. The other
    # window's re-fetch gives the x- mid with the row's content: kept, and the row follows it.
    _fb, _src, rec, hooks, mid, _other, _row_date, other_date = await twins_in_two_windows(
        store, tmp_path, None
    )
    report = await rec.hourly()
    assert (report.removed, report.added, report.missed) == (0, 0, 0)
    assert hooks == [] and store.get_message(mid) is not None
    (loc,) = store.locations(mid)
    assert loc.tb_date == other_date
    again = await rec.hourly()
    assert (again.repaged, again.removed, again.added, again.missed) == (0, 0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", ["twin@example.test", None])
async def test_a_removal_waits_while_the_twin_fetch_goes_stale(
    store: Store, tmp_path: Path, message_id: str | None
) -> None:
    # As above, but the remaining copy's fetch goes stale: the pass cannot tell whether the
    # folder still holds the content, so the location is left as it is (not removed, its
    # tb_date unchanged) and the next pass, whose fetch succeeds, keeps it and moves the date.
    _fb, src, rec, hooks, mid, other, row_date, other_date = await twins_in_two_windows(
        store, tmp_path, message_id
    )
    src.stale.add(other)
    report = await rec.hourly()
    assert (report.removed, report.added, report.missed) == (0, 0, 0)
    assert hooks == [] and store.get_message(mid) is not None
    (loc,) = store.locations(mid)
    assert loc.tb_date == row_date
    src.stale.clear()
    again = await rec.hourly()
    assert (again.removed, again.added, again.missed) == (0, 0, 0)
    assert hooks == []
    (loc,) = store.locations(mid)
    assert loc.tb_date == other_date


def skipped_rows(store: Store) -> list[tuple[int, int, str]]:
    return store.connection.execute(
        "SELECT tb_date, ordinal, reason FROM skipped_items ORDER BY tb_date, ordinal"
    ).fetchall()


@pytest.mark.asyncio
async def test_a_removal_waits_while_the_twin_is_unreadable(store: Store, tmp_path: Path) -> None:
    # Plan 04 M5: as above, but TB cannot read the remaining copy (tb_error). It is not a
    # fetched copy: the location waits (not removed, tb_date unchanged) and the unread item
    # gets one tb_error row, not one more per pass; once TB reads it, kept and the row goes.
    _fb, src, rec, hooks, mid, other, row_date, other_date = await twins_in_two_windows(
        store, tmp_path, "twin@example.test"
    )
    src.unreadable.add(other)
    for _ in range(2):
        report = await rec.hourly()
        assert (report.removed, report.added, report.missed) == (0, 0, 0)
        assert hooks == [] and store.get_message(mid) is not None
        (loc,) = store.locations(mid)
        assert loc.tb_date == row_date
        assert skipped_rows(store) == [(other_date, 0, "tb_error")]
    src.unreadable.clear()
    again = await rec.hourly()
    assert (again.removed, again.added, again.missed) == (0, 0, 0)
    assert hooks == []
    (loc,) = store.locations(mid)
    assert loc.tb_date == other_date
    assert skipped_rows(store) == []


@pytest.mark.asyncio
async def test_an_unreadable_id_less_item_keeps_its_seconds_x_locations_and_is_retried(
    store: Store, tmp_path: Path
) -> None:
    # Plan 04 M5: an indexed ID-less message turns unreadable and a new ID-less item arrives at
    # its second without an event. The re-fetch of that second adds the new one, keeps the
    # unread one's x- location (it may be its copy) and records one tb_error row; every pass
    # re-fetches the second (the retry) without adding rows; once TB reads it the row goes.
    d = NOW - 3 * DAY
    held_raw = make_raw(None, d, subject="held, no id")
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("kept@example.test", d), held_raw]))
    _kept, held = fb.tb_ids("m1:/INBOX")
    src = StaleRaw(fb)
    _events, rec, hooks = await setup(store, fb, source=src, clock=lambda: NOW)
    held_mid = mid_from_raw(held_raw)
    fb.drop_events(1)
    await fb.deliver("m1:/INBOX", make_raw(None, d, subject="new, no id"))
    src.unreadable.add(held)
    report = await rec.hourly()
    assert (report.added, report.removed) == (1, 0)
    assert [h[0] for h in hooks if h[2] is None] == []  # nothing removed
    assert store.get_message(held_mid) is not None
    assert skipped_rows(store) == [(d, 0, "tb_error")]
    assert store.status().skipped_tb_error == 1
    for _ in range(2):
        start = len(src.raws)
        again = await rec.hourly()
        assert (again.added, again.removed) == (0, 0)
        assert held in src.raws[start:]  # retried
        assert skipped_rows(store) == [(d, 0, "tb_error")]
        assert store.get_message(held_mid) is not None
    src.unreadable.clear()
    healed = await rec.hourly()
    assert (healed.added, healed.removed) == (0, 0)
    assert skipped_rows(store) == []
    assert store.status().x_mids == 2
    quiet = await rec.hourly()
    assert (quiet.repaged, quiet.added, quiet.removed) == (0, 0, 0)


@pytest.mark.asyncio
async def test_an_id_less_item_unreadable_at_backfill_is_retried_every_pass(
    store: Store, tmp_path: Path
) -> None:
    # Plan 04 M5: its tb_error row matches TB's ID-less count at its second, so only the
    # tb_error rule makes a pass fetch it again; indexed once TB reads it.
    d = NOW - 3 * DAY
    raws = [make_raw("kept@example.test", d), make_raw(None, d, subject="unreadable, no id")]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    _kept, bad = fb.tb_ids("m1:/INBOX")
    src = StaleRaw(fb)
    src.unreadable.add(bad)
    cfg = TernConfig()
    await Backfill(store, src, cfg, clock=lambda: NOW).run()
    assert skipped_rows(store) == [(d, 0, "tb_error")]
    events = EventIngest(store, src, cfg, clock=lambda: NOW)
    rec = Reconcile(store, src, cfg, events=events, clock=lambda: NOW)
    start = len(src.raws)
    report = await rec.hourly()
    assert (report.repaged, report.added, report.removed) == (1, 0, 0)
    assert bad in src.raws[start:]  # retried (with the second's other downloaded items)
    assert skipped_rows(store) == [(d, 0, "tb_error")]
    src.unreadable.clear()
    healed = await rec.hourly()
    assert (healed.added, healed.removed) == (1, 0)
    assert skipped_rows(store) == [] and store.status().x_mids == 1
    # TB failed a read, no event was lost: a retry, never a missed change
    assert (healed.retried, healed.missed) == (1, 0)
    assert missed(store, "hourly") == 0


@pytest.mark.asyncio
async def test_a_lost_event_beside_a_still_unreadable_item_is_still_missed(
    store: Store, tmp_path: Path
) -> None:
    # Plan 04 M5 review: only items read at last count as retried; a new item at the second of
    # an item still unreadable, whose event was lost, is a missed change.
    d = NOW - 3 * DAY
    raws = [make_raw("kept@example.test", d), make_raw(None, d, subject="unreadable, no id")]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    _kept, bad = fb.tb_ids("m1:/INBOX")
    src = StaleRaw(fb)
    src.unreadable.add(bad)
    cfg = TernConfig()
    await Backfill(store, src, cfg, clock=lambda: NOW).run()
    events = EventIngest(store, src, cfg, clock=lambda: NOW)
    rec = Reconcile(store, src, cfg, events=events, clock=lambda: NOW)
    fb.on_event = events.on_event
    await rec.hourly()
    fb.drop_events(1)
    await fb.deliver("m1:/INBOX", make_raw(None, d, subject="new, no id"))
    report = await rec.hourly()
    assert (report.added, report.removed) == (1, 0)
    assert (report.retried, report.missed) == (0, 1)
    assert skipped_rows(store) == [(d, 0, "tb_error")]


class UnreadableNew(StaleRaw):
    """As :class:`StaleRaw`, and ``raw`` of every tb_id not in ``readable`` (when set) gives
    ``tb_error``: an item delivered later is unreadable from its new-mail event on."""

    def __init__(self, fb: FakeBridge) -> None:
        super().__init__(fb)
        self.readable: set[int] | None = None

    async def raw(self, tb_id: int) -> bytes:
        if self.readable is not None and tb_id not in self.readable:
            self.raws.append(tb_id)
            raise BridgeError("tb_error", "synthetic: TB failed", tb_code="NS_ERROR_FAILURE")
        return await super().raw(tb_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", ["late@example.test", None])
async def test_an_event_item_unreadable_over_several_passes_is_a_retry_not_missed(
    store: Store, tmp_path: Path, message_id: str | None
) -> None:
    # Plan 04 M5 review: a new-mail event's item TB cannot read gets a tb_error row; it stays
    # unreadable over several passes (the applied event no longer covers it), and the pass that
    # reads it at last counts the add as retried, not missed (no event was lost).
    d = NOW - 3 * DAY
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [make_raw("kept@example.test", d - 60)]))
    src = UnreadableNew(fb)
    cfg = TernConfig()
    await Backfill(store, IngestSource(fb), cfg, clock=lambda: NOW).run()
    events = EventIngest(store, src, cfg, clock=lambda: NOW)
    rec = Reconcile(store, src, cfg, events=events, clock=lambda: NOW)
    fb.on_event = events.on_event
    src.readable = set(fb.tb_ids("m1:/INBOX"))
    new_raw = make_raw(message_id, d, subject="unreadable at first")
    new = await fb.deliver("m1:/INBOX", new_raw)
    assert skipped_rows(store) == [(d, 0, "tb_error")]
    for _ in range(3):
        start = len(src.raws)
        report = await rec.hourly()
        assert (report.added, report.removed, report.missed) == (0, 0, 0)
        assert new in src.raws[start:]  # retried
        assert skipped_rows(store) == [(d, 0, "tb_error")]
    src.readable = None
    healed = await rec.hourly()
    assert (healed.added, healed.removed) == (1, 0)
    assert (healed.retried, healed.missed) == (1, 0)
    assert missed(store, "hourly") == 0
    assert skipped_rows(store) == []
    assert store.get_message(mid_from_raw(new_raw)) is not None


async def differing_pair_in_two_windows(
    store: Store, tmp_path: Path, source: type[IngestSource] = IngestSource
) -> tuple[FakeBridge, IngestSource, Reconcile, list[Hook], str, int, int]:
    """Two differing copies of a Message-ID in different windows, both held: the row holds the
    older one, the newer one is a same-folder copy. Returns (fb, src, rec, hooks, mid, the newer
    copy's tb_id, the older copy's tb_date)."""
    d_new, d_old = NOW - 3 * DAY, NOW - 3 * DAY - 2 * W
    raws = [
        make_raw("pair@example.test", d_new, subject="newer"),
        make_raw("pair@example.test", d_old, subject="older"),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    a, _b = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(a, True)  # the backfill writes only the older copy: the row holds it
    src = source(fb)
    _events, rec, hooks = await setup(store, fb, source=src, clock=lambda: NOW)
    fb.set_headers_only(a, False)
    mid = mid_of("pair@example.test")
    assert mid is not None
    await rec.hourly()  # writes the late-downloaded newer copy as a same-folder copy
    (loc,) = store.locations(mid)
    assert len(store.location_hashes(mid, "m1:/INBOX")) == 2 and loc.tb_date == d_old
    hooks.clear()
    return fb, src, rec, hooks, mid, a, d_old


@pytest.mark.asyncio
async def test_a_pass_never_prunes_a_same_folder_copy_whose_fetch_went_stale(
    store: Store, tmp_path: Path
) -> None:
    # The row's window is re-paged (an unevented delivery) and the row's copy fetched; the
    # newer copy's fetch goes stale, so the pass has not seen every copy of the Message-ID:
    # its same-folder copy must stay held (the next pass, which fetches it, keeps it too).
    fb, src, rec, hooks, mid, newer, d_old = await differing_pair_in_two_windows(
        store, tmp_path, StaleRaw
    )
    assert isinstance(src, StaleRaw)
    hashes = store.location_hashes(mid, "m1:/INBOX")
    fb.drop_events(1)
    await fb.deliver("m1:/INBOX", make_raw("nudge@example.test", d_old + 60))
    src.stale.add(newer)
    report = await rec.hourly()
    assert (report.added, report.removed) == (1, 0)
    assert store.location_hashes(mid, "m1:/INBOX") == hashes
    src.stale.clear()
    await rec.hourly()
    assert store.location_hashes(mid, "m1:/INBOX") == hashes
    assert [h[0] for h in hooks] == [mid_of("nudge@example.test")]


@pytest.mark.asyncio
async def test_repage_window_never_prunes_a_same_folder_copy_held_in_another_window(
    store: Store, tmp_path: Path
) -> None:
    # An explicit re-page of the row's window sees only the row's copy; the newer copy, in
    # another window, is still in TB and must stay held.
    _fb, _src, rec, hooks, mid, _newer, d_old = await differing_pair_in_two_windows(store, tmp_path)
    hashes = store.location_hashes(mid, "m1:/INBOX")
    res = await rec.repage_window("m1:/INBOX", d_old - DAY, d_old + DAY)
    assert (res.added, res.removed) == (0, 0)
    assert store.location_hashes(mid, "m1:/INBOX") == hashes
    assert hooks == []


# ---- mass-removal guard (merge review M4) --------------------------------------------------------


class EmptyPages(IngestSource):
    """TB lists no items for the windows starting at a date in ``empty`` (every window with
    ``empty_all``), as a bridge or TB glitch would; folder counts and fetches are unchanged."""

    def __init__(self, fb: FakeBridge) -> None:
        super().__init__(fb)
        self.empty: set[int] = set()
        self.empty_all = False
        #: folder ids the folder listing leaves out
        self.unlisted: set[str] = set()

    async def folders(self, account_id: str) -> list[Folder]:
        return [f for f in await super().folders(account_id) if f.folder_id not in self.unlisted]

    async def page(
        self,
        folder_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        cursor: str | None = None,
    ) -> Page:
        page = await super().page(folder_id, from_date, to_date, cursor)
        start = None if from_date is None else int(from_date.timestamp())
        if self.empty_all or (cursor is None and start in self.empty):
            return Page(items=[])
        return page


def index_rows(store: Store) -> list[tuple[Any, ...]]:
    conn = store.connection
    return [
        *conn.execute("SELECT mid, folder_id, content_hash, tb_date FROM locations ORDER BY 1"),
        *conn.execute("SELECT folder_id, tb_date, mid FROM headers_only_items ORDER BY 1, 2, 3"),
    ]


async def two_window_folder(
    store: Store, tmp_path: Path
) -> tuple[FakeBridge, EmptyPages, Reconcile, list[Hook], list[int]]:
    """m1:/INBOX with two messages (one headers-only) in the newest window and one in an older
    window; the Reconcile pages through an :class:`EmptyPages`."""
    recent, old = NOW - 2 * DAY, NOW - 45 * DAY
    raws = [
        make_raw("recent-1@example.test", recent),
        make_raw("recent-ho@example.test", recent + 60),
        make_raw("old-1@example.test", old),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", raws))
    fb.set_headers_only(fb.tb_ids("m1:/INBOX")[1], True)
    src = EmptyPages(fb)
    _events, rec, hooks = await setup(store, fb, source=src, clock=lambda: NOW)
    window = TernConfig().backfill_window_days * DAY
    return fb, src, rec, hooks, [NOW - window, NOW - 2 * window]


@pytest.mark.parametrize("which", ["newest_window", "older_window", "every_window"])
@pytest.mark.asyncio
async def test_an_empty_window_holding_locations_stops_the_pass_before_any_removal(
    store: Store, tmp_path: Path, which: str
) -> None:
    # TB counts 3 messages but lists none in a window the index holds locations in: a glitch,
    # not a mass delete. The pass raises before deciding removals; nothing is removed.
    _fb, src, rec, hooks, starts = await two_window_folder(store, tmp_path)
    before = index_rows(store)
    if which == "every_window":
        src.empty_all = True
    else:
        src.empty = {starts[0] if which == "newest_window" else starts[1]}
    for run in (rec.hourly, rec.daily):
        with pytest.raises(IngestError):
            await run()
    assert index_rows(store) == before and hooks == []
    assert runs(store, "hourly") == [] and runs(store, "daily") == []


@pytest.mark.asyncio
async def test_a_window_really_emptied_in_tb_is_still_reconciled(
    store: Store, tmp_path: Path
) -> None:
    # Every message of the newest window deleted with no event: TB's pages still account for its
    # folder count, so the empty window is real and its locations go (the folder is not stuck).
    fb, _src, rec, hooks, _starts = await two_window_folder(store, tmp_path)
    a, b, _c = fb.tb_ids("m1:/INBOX")
    fb.drop_events(1)
    await fb.delete([a, b])
    report = await rec.hourly()
    assert (report.removed, report.missed) == (1, 1)
    assert [h[0] for h in hooks] == [mid_of("recent-1@example.test")]
    assert [r[0] for r in store.connection.execute("SELECT mid FROM locations")] == [
        mid_of("old-1@example.test")
    ]
    assert store.status().headers_only == 0


@pytest.mark.asyncio
async def test_a_folder_tb_counts_as_empty_is_reconciled(store: Store, tmp_path: Path) -> None:
    fb, _src, rec, _hooks, _starts = await two_window_folder(store, tmp_path)
    fb.drop_events(1)
    await fb.delete(fb.tb_ids("m1:/INBOX"))
    report = await rec.hourly()
    assert report.removed == 2 and store.status().locations == 0


@pytest.mark.asyncio
async def test_repage_window_raises_on_an_empty_window_holding_locations(
    store: Store, tmp_path: Path
) -> None:
    fb, src, rec, hooks, starts = await two_window_folder(store, tmp_path)
    before = index_rows(store)
    src.empty = {starts[0]}
    with pytest.raises(IngestError):
        await rec.repage_window("m1:/INBOX", starts[0], None)
    assert index_rows(store) == before and hooks == []
    # TB really empty (count 0): the re-page removes
    src.empty = set()
    fb.drop_events(1)
    await fb.delete(fb.tb_ids("m1:/INBOX"))
    res = await rec.repage_window("m1:/INBOX", starts[0], None)
    assert res.removed == 1


@pytest.mark.asyncio
async def test_repage_window_raises_on_an_empty_window_of_a_folder_tb_no_longer_lists(
    store: Store, tmp_path: Path
) -> None:
    # No folder count to check the empty window against: treated as "count > 0".
    _fb, src, rec, hooks, starts = await two_window_folder(store, tmp_path)
    before = index_rows(store)
    src.empty = {starts[0]}
    src.unlisted = {"m1:/INBOX"}
    with pytest.raises(IngestError):
        await rec.repage_window("m1:/INBOX", starts[0], None)
    assert index_rows(store) == before and hooks == []


class EmptyListing(IngestSource):
    """TB lists no accounts (``no_accounts``), or no folders for the accounts in ``bare``."""

    def __init__(self, fb: FakeBridge) -> None:
        super().__init__(fb)
        self.no_accounts = False
        self.bare: set[str] = set()

    async def accounts(self) -> list[Account]:
        return [] if self.no_accounts else await super().accounts()

    async def folders(self, account_id: str) -> list[Folder]:
        return [] if account_id in self.bare else await super().folders(account_id)


@pytest.mark.parametrize("glitch", ["no_folders", "no_accounts"])
@pytest.mark.asyncio
async def test_an_empty_folder_listing_raises_before_any_removal(
    store: Store, fb: FakeBridge, glitch: str
) -> None:
    await Backfill(store, IngestSource(fb), TernConfig()).run()
    src = EmptyListing(fb)
    events = EventIngest(store, src, TernConfig())
    rec = Reconcile(store, IngestSource(fb), TernConfig(), events=events)
    hooks: list[Hook] = []
    store.location_observers.append(lambda *a: hooks.append(a))
    before = index_rows(store)
    folders_before = store.folders()
    if glitch == "no_accounts":
        src.no_accounts = True
    else:
        src.bare = {"acct2"}
    with pytest.raises(IngestError):
        await rec.hourly()
    with pytest.raises(IngestError):
        await events.on_event("event.folder.changed", FolderChanged(folder_id="acct1:/INBOX"))
    assert index_rows(store) == before and hooks == []
    assert store.folders() == folders_before
    assert runs(store, "hourly") == []


@pytest.mark.asyncio
async def test_an_account_the_index_holds_nothing_for_never_blocks_the_relist(
    store: Store, fb: FakeBridge
) -> None:
    # A new account with no folders yet in TB, whose only folder rows were never indexed (no
    # locations, no backfill state): its empty listing is not a glitch the guard stops on, and
    # the other accounts still reconcile.
    await Backfill(store, IngestSource(fb), TernConfig()).run()

    class WithNewAccount(EmptyListing):
        async def accounts(self) -> list[Account]:
            new = Account(account_id="acct9", name="new@example.test", type="imap", identities=[])
            return [*await super().accounts(), new]

    src = WithNewAccount(fb)
    src.bare = {"acct9"}
    store.upsert_account("acct9", "new@example.test", "imap")
    store.upsert_folder("acct9:/INBOX", "acct9", "/INBOX", "inbox")
    events = EventIngest(store, src, TernConfig())
    rec = Reconcile(store, IngestSource(fb), TernConfig(), events=events)
    await fb.deliver("acct1:/INBOX", make_raw("unevented@example.test", NOW - DAY))
    report = await rec.hourly()
    assert report.added == 1 and report.removed == 0
    held = {r[0] for r in store.connection.execute("SELECT mid FROM locations")}
    assert mid_of("unevented@example.test") in held


@pytest.mark.asyncio
async def test_a_fresh_index_with_no_accounts_listed_syncs_nothing(
    store: Store, fb: FakeBridge
) -> None:
    # Fresh install before TB has any account: nothing held, so no listing is a glitch.
    src = EmptyListing(fb)
    src.no_accounts = True
    events = EventIngest(store, src, TernConfig())
    assert await events.sync_folders("reconcile") == []
    report = await Reconcile(store, IngestSource(fb), TernConfig(), events=events).hourly()
    assert report.folders == 0


@pytest.mark.asyncio
async def test_an_account_no_longer_listed_loses_its_folders(store: Store, fb: FakeBridge) -> None:
    # Another account still listed: a removed account's folders go as before.
    await Backfill(store, IngestSource(fb), TernConfig()).run()
    gone = {f.folder_id for f in store.folders() if f.account_id == "acct2"}
    assert gone

    class WithoutAcct2(IngestSource):
        async def accounts(self) -> list[Account]:
            return [a for a in await super().accounts() if a.account_id != "acct2"]

    events = EventIngest(store, WithoutAcct2(fb), TernConfig())
    rec = Reconcile(store, IngestSource(fb), TernConfig(), events=events)
    await rec.hourly()
    held = {r[0] for r in store.connection.execute("SELECT DISTINCT folder_id FROM locations")}
    assert held and not held & gone
