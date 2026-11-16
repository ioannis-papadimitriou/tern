"""Plan 04 step 7: `IngestSource` + `Backfill` over FakeBridge (windows, per-item path, counters).

Expected numbers come from MANIFEST.json (copies, distinct messages, per-folder counts, the
adversarial pair, the three no-Message-ID keys) or are built inline here (mini mailboxes with
synthetic `example.test` mail); none is typed in from a run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mailbox
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm
from large_mail import MANY, MANY_ID, OVER_ID, lower_raw_max, small_large_mbox

import tern.index.ingest as ingest_mod
from tern.bridge.client import ProtocolViolation
from tern.bridge.protocol import (
    AttachmentInfo,
    BridgeError,
    ErrorName,
    FindParams,
    FullMessage,
    MailNew,
    Page,
    PageItem,
    PageParams,
    Raw,
    TextPart,
)
from tern.config import TernConfig
from tern.index.events import EventIngest
from tern.index.ingest import (
    MAX_WINDOW_RESTARTS,
    Backfill,
    BatchReport,
    Counters,
    IngestError,
    IngestReport,
    ItemIngester,
    TokenBucket,
    coalesce_dates,
    is_indexed,
)
from tern.index.mid import X_PREFIX, mid_of
from tern.index.mime import TEXT_CAP_BYTES
from tern.index.source import IngestSource
from tern.index.store import IngestRun, MessageRecord, Store
from tern.testing.fake_bridge import SESSION_STRIDE, FakeBridge

REPO = Path(__file__).resolve().parents[2]
FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
MESSAGES = tfm.messages(MANIFEST)
COPIES = MANIFEST["message_count"]
DISTINCT = MANIFEST["distinct_messages"]
FOLDER_COUNTS = {
    f"{a['key']}:/{f['path']}": f["count"] for a in MANIFEST["accounts"] for f in a["folders"]
}
INV_MID = MANIFEST["named"]["inv-0077"]["mid"]
NOW = int(datetime(2026, 11, 14, tzinfo=UTC).timestamp())
DAY = 86_400


def hmid(key: str) -> str:
    return MESSAGES[key]["message_id"].strip("<>")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "data" / "index.db")
    yield s
    s.close()


@pytest.fixture
def fb() -> FakeBridge:
    return FakeBridge(FIXTURE)


async def ingest(
    store: Store,
    source: IngestSource,
    cfg: TernConfig | None = None,
    reports: list[BatchReport] | None = None,
    clock: Any = None,
) -> IngestReport:
    kw: dict[str, Any] = {} if clock is None else {"clock": clock}
    bf = Backfill(store, source, cfg or TernConfig(), **kw)
    return await bf.run(None if reports is None else reports.append)


def one(store: Store, sql: str, *args: Any) -> Any:
    return store.connection.execute(sql, args).fetchone()[0]


def per_folder(store: Store) -> dict[str, int]:
    return dict(
        store.connection.execute("SELECT folder_id, count(*) FROM locations GROUP BY folder_id")
    )


def runs(store: Store) -> list[dict[str, Any]]:
    cur = store.connection.execute("SELECT * FROM ingest_runs ORDER BY rowid")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def raw_tb_ids(fb: FakeBridge, start: int = 0) -> list[int]:
    return [p["tb_id"] for m, p in fb.calls[start:] if m == "mail.messages.raw"]


def assert_fixture_counts(store: Store) -> None:
    st = store.status()
    assert (st.messages, st.locations, st.conflicted, st.x_mids) == (DISTINCT, COPIES, 1, 3)
    assert per_folder(store) == FOLDER_COUNTS
    mids = {r[0] for r in store.connection.execute("SELECT mid FROM messages")}
    assert {m["mid"] for m in MESSAGES.values() if m["mid"]} == {
        m for m in mids if not m.startswith(X_PREFIX)
    }
    assert one(store, "SELECT mid FROM messages WHERE conflicted = 1") == INV_MID


# ---- mini mailboxes (synthetic, inline) -----------------------------------------------------


def make_raw(
    message_id: str | None,
    date: int,
    *,
    to: str = "b@example.test",
    subject: str = "synthetic",
    body: str = "body",
) -> bytes:
    lines = [f"Message-ID: <{message_id}>"] if message_id else []
    lines += [
        f"Date: {format_datetime(datetime.fromtimestamp(date, UTC))}",
        "From: a@example.test",
        f"To: {to}",
        f"Subject: {subject}",
        "",
        body,
        "",
    ]
    return "\n".join(lines).encode()


def write_mbox(path: Path, raws: list[bytes]) -> Path:
    box = mailbox.mbox(path)
    try:
        for raw in raws:
            box.add(raw)
    finally:
        box.close()
    return path


def mini_mailbox(root: Path, folders: list[tuple[str, list[str], list[bytes]]]) -> Path:
    """A one-account mailbox dir (MANIFEST + mbox files) FakeBridge can load."""
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, (path, special_use, raws) in enumerate(folders):
        write_mbox(root / f"f{i}.mbox", raws)
        entries.append(
            {
                "path": path,
                "special_use": special_use[0] if special_use else None,
                "file": f"f{i}.mbox",
                "format": "mbox",
            }
        )
    manifest = {
        "accounts": [{"key": "m1", "name": "m1@example.test", "type": "none", "folders": entries}]
    }
    (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


# ---- full ingest ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_ingest_matches_the_manifest(store: Store, fb: FakeBridge) -> None:
    reports: list[BatchReport] = []
    report = await ingest(store, IngestSource(fb), reports=reports)
    assert_fixture_counts(store)
    st = store.status()
    assert st.conflicts_unclassified == 0
    inv = store.get_message(INV_MID)
    assert inv is not None and inv.conflict_class is not None
    malformed = store.get_message(MESSAGES["malformed"]["mid"])
    assert malformed is not None and malformed.parse_status in ("partial", "failed")
    # progress: >= 4 reports at batch_size 50, falling to 0 remaining
    assert len(reports) >= 4 and reports[-1].remaining == 0
    assert [r.remaining for r in reports] == sorted((r.remaining for r in reports), reverse=True)
    assert reports[0].total == COPIES and report.items == COPIES
    assert report.folders_done == report.folders_total == len(FOLDER_COUNTS)
    # one ingest_runs row per folder, the counters as the fixture implies
    rows = runs(store)
    assert sorted(r["folder_id"] for r in rows) == sorted(FOLDER_COUNTS)
    assert all(r["kind"] == "backfill" for r in rows)
    assert all(r["x_mid_churn"] == 0 and r["hmid_mismatch"] == 0 for r in rows)
    assert all(r["stale_retries"] == 0 and r["skipped_large_noid"] == 0 for r in rows)
    assert sum(r["md5_ids"] for r in rows) == len(MANIFEST["no_message_id"])
    assert sum(r["n_msgs"] for r in rows) == COPIES
    assert {r[0] for r in store.connection.execute("SELECT status FROM ingest_state")} == {"done"}
    # quoted injection text never reaches the index; the direct one does (query form works)
    fts = "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?"
    assert one(store, fts, '"CANARY-INJ-3"') == 0
    assert one(store, fts, '"CANARY-INJ-1"') >= 1


@pytest.mark.asyncio
async def test_rerun_of_a_done_backfill_pages_nothing(store: Store, fb: FakeBridge) -> None:
    await ingest(store, IngestSource(fb))
    start = len(fb.calls)
    report = await ingest(store, IngestSource(fb))
    methods = {m for m, _ in fb.calls[start:]}
    assert methods == {"mail.accounts.list", "mail.folders.list"}
    assert report.items == 0 and len(runs(store)) == len(FOLDER_COUNTS)
    assert_fixture_counts(store)


@pytest.mark.asyncio
async def test_small_pages_slow_first_page_one_cursor(store: Store) -> None:
    fb = FakeBridge(FIXTURE, page_size=5, slow_first_page=True, max_open_cursors=1)
    await ingest(store, IngestSource(fb))
    assert_fixture_counts(store)


@pytest.mark.asyncio
async def test_concurrency_bounds_fetches_in_flight(store: Store, fb: FakeBridge) -> None:
    class Counting(IngestSource):
        inflight = 0
        peak = 0

        async def raw(self, tb_id: int) -> bytes:
            Counting.inflight += 1
            Counting.peak = max(Counting.peak, Counting.inflight)
            try:
                await asyncio.sleep(0)
                return await super().raw(tb_id)
            finally:
                Counting.inflight -= 1

    await ingest(store, Counting(fb), TernConfig(ingest_concurrency=4))
    assert Counting.peak == 4
    assert_fixture_counts(store)


@pytest.mark.asyncio
async def test_throttle_spaces_fetches(store: Store, tmp_path: Path) -> None:
    raws = [make_raw(f"t{i}@example.test", NOW - DAY - i) for i in range(5)]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    t0 = time.perf_counter()
    await ingest(store, IngestSource(fb), TernConfig(ingest_max_msgs_per_s=50), clock=lambda: NOW)
    assert time.perf_counter() - t0 >= 4 / 50
    assert store.status().messages == 5


@pytest.mark.asyncio
async def test_token_bucket_spacing() -> None:
    t = [100.0]
    slept: list[float] = []

    async def sleep(d: float) -> None:
        slept.append(d)
        t[0] += d

    bucket = TokenBucket(10, clock=lambda: t[0], sleep=sleep)
    for _ in range(3):
        await bucket.acquire()
    assert slept == pytest.approx([0.1, 0.1])
    t[0] += 5
    await bucket.acquire()
    assert len(slept) == 2
    with pytest.raises(ValueError):
        TokenBucket(0)


def test_folder_policy() -> None:
    for excluded in ("trash", "junk", "outbox", "templates", "drafts", "Trash"):
        assert not is_indexed([excluded])
    for kept in ([], ["inbox"], ["sent"], ["archives"]):
        assert is_indexed(kept)


@pytest.mark.asyncio
async def test_excluded_folders_are_listed_not_indexed(
    store: Store, fb: FakeBridge, tmp_path: Path
) -> None:
    trash = fb.add_mbox(
        "acct1",
        "Trash",
        write_mbox(tmp_path / "t.mbox", [make_raw("tr@example.test", NOW)]),
        ["trash"],
    )
    await ingest(store, IngestSource(fb))
    assert trash in {f.folder_id for f in store.folders()}
    assert trash not in per_folder(store)
    assert not [p for m, p in fb.calls if m == "mail.messages.page" and p["folder_id"] == trash]
    assert_fixture_counts(store)


# ---- counters -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_altered_hmid_counts_hmid_mismatch(store: Store, fb: FakeBridge) -> None:
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

    await ingest(store, Altering(fb))
    assert_fixture_counts(store)  # stored under the raw-derived (MANIFEST) mid
    rows = {r["folder_id"]: r for r in runs(store)}
    assert rows["acct1:/Invoices"]["hmid_mismatch"] == 1
    assert sum(r["hmid_mismatch"] for r in rows.values()) == 1
    assert [loc.folder_id for loc in store.locations(MESSAGES["invoice-0013"]["mid"])] == [
        "acct1:/Invoices"
    ]


@pytest.mark.asyncio
async def test_an_event_replay_of_a_located_altered_hmid_item_does_not_count_again(
    store: Store, fb: FakeBridge
) -> None:
    """``hmid_mismatch`` counts once per (message, folder) (#30): a replayed new-mail event for
    an item already located in the folder re-writes it (as a resumed backfill window would)
    without counting it again."""
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

    await ingest(store, Altering(fb))
    assert sum(r["hmid_mismatch"] for r in runs(store)) == 1
    (item,) = [
        i
        for i in (await Altering(fb).page("acct1:/Invoices")).items
        if i.header_message_id == "altered@example.test"
    ]
    events = EventIngest(store, Altering(fb), TernConfig())
    for _ in range(2):
        await events.on_event("event.mail.new", MailNew(folder_id="acct1:/Invoices", items=[item]))
    replays = [r for r in runs(store) if r["kind"] == "event"]
    assert [(r["n_msgs"], r["hmid_mismatch"]) for r in replays] == [(1, 0), (1, 0)]
    assert sum(r["hmid_mismatch"] for r in runs(store)) == 1
    assert_fixture_counts(store)


# ---- resume, windows, probe ---------------------------------------------------------------


class Killed(Exception):
    pass


async def page_items(fb: FakeBridge, folder_id: str) -> list[PageItem]:
    out: list[PageItem] = []
    page: Page = await fb.request("mail.messages.page", PageParams(folder_id=folder_id))
    out += page.items
    while page.next_cursor:
        page = await fb.request(
            "mail.messages.page", PageParams(folder_id=folder_id, cursor=page.next_cursor)
        )
        out += page.items
    return out


@pytest.mark.asyncio
async def test_kill_mid_same_date_pair_and_resume(store: Store, fb: FakeBridge) -> None:
    items = await page_items(fb, "acct1:/Newsletters")
    first, second = next(
        (a, b) for i, a in enumerate(items) for b in items[i + 1 :] if a.date == b.date
    )
    fb.calls.clear()

    class Killing(IngestSource):
        async def raw(self, tb_id: int) -> bytes:
            if tb_id == second.tb_id:
                raise Killed
            return await super().raw(tb_id)

    with pytest.raises(Killed):
        await ingest(store, Killing(fb))
    first_mid, second_mid = mid_of(first.header_message_id), mid_of(second.header_message_id)
    assert first_mid and second_mid and first_mid != second_mid
    assert [loc.folder_id for loc in store.locations(first_mid)] == ["acct1:/Newsletters"]
    assert store.locations(second_mid) == []
    fb.restart()
    start = len(fb.calls)
    await ingest(store, IngestSource(fb))
    assert_fixture_counts(store)  # every mid once, nothing lost at the same-second boundary
    assert all(t > SESSION_STRIDE for t in raw_tb_ids(fb, start))  # no stale tb_id reused
    assert all(r["stale_retries"] == 0 for r in runs(store))


@pytest.mark.asyncio
async def test_a_resume_with_nothing_left_writes_no_backfill_run_row(
    store: Store, tmp_path: Path
) -> None:
    """Killed after its last window committed, before the probe ended the folder: the resumed
    run pages one empty window and probes, and writes no near-empty ``backfill`` row that would
    skew the rate (L6). A resume that still ingests something writes its row."""
    raws = [make_raw("r0@example.test", NOW - 3 * DAY), make_raw("r1@example.test", NOW - 45 * DAY)]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))

    class KillBelow(IngestSource):
        """Kills the first page call bounded above by ``below`` or lower (window or probe)."""

        def __init__(self, below: int) -> None:
            super().__init__(fb)
            self.below = below

        async def page(self, folder_id: str, *a: Any, cursor: str | None = None, **kw: Any) -> Page:
            hi = a[1] if len(a) > 1 else kw.get("to_date")
            if cursor is None and hi is not None and hi.timestamp() <= self.below:
                raise Killed
            return await super().page(folder_id, *a, cursor=cursor, **kw)

    # Run 1 commits [NOW-30d, open) and dies paging [NOW-60d, NOW-30d); the resume ingests r1.
    with pytest.raises(Killed):
        await ingest(store, KillBelow(NOW - 30 * DAY), clock=lambda: NOW)
    assert runs(store) == []
    with pytest.raises(Killed):  # run 2 commits [NOW-60d, NOW-30d) and dies below it
        await ingest(store, KillBelow(NOW - 60 * DAY), clock=lambda: NOW)
    assert runs(store) == []
    await ingest(store, IngestSource(fb), clock=lambda: NOW)  # nothing left below NOW-60d
    assert store.status().messages == 2
    assert one(store, "SELECT status FROM ingest_state") == "done"
    assert runs(store) == []

    # Control: a resume that ingests a message writes its backfill row.
    store2 = Store.open(tmp_path / "data2" / "index.db")
    try:
        with pytest.raises(Killed):
            await ingest(store2, KillBelow(NOW - 30 * DAY), clock=lambda: NOW)
        await ingest(store2, IngestSource(fb), clock=lambda: NOW)
        assert [(r["kind"], r["n_msgs"]) for r in runs(store2)] == [("backfill", 1)]
    finally:
        store2.close()


@pytest.mark.asyncio
async def test_future_dated_mail_is_in_the_first_window(store: Store, tmp_path: Path) -> None:
    raws = [
        make_raw("past@example.test", NOW - 3 * DAY),
        make_raw("future@example.test", NOW + 365 * DAY),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    await ingest(store, IngestSource(fb), clock=lambda: NOW)
    assert store.get_message(mid_of("future@example.test") or "") is not None
    assert store.status().messages == 2


@pytest.mark.asyncio
async def test_probe_takes_the_max_over_all_pages(store: Store, tmp_path: Path) -> None:
    # File order: older items first; with page_size=2 the newest one is on the probe's 2nd page,
    # and the gap to the window above is far wider than one window.
    dates = [NOW - 200 * DAY, NOW - 190 * DAY, NOW - 100 * DAY]
    raws = [make_raw(f"p{i}@example.test", d) for i, d in enumerate(dates)]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]), page_size=2)
    await ingest(store, IngestSource(fb), clock=lambda: NOW)
    assert store.status().messages == 3
    assert one(store, "SELECT status FROM ingest_state") == "done"


@pytest.mark.asyncio
async def test_window_boundaries_are_half_open(store: Store, tmp_path: Path) -> None:
    # An item exactly on a window boundary is ingested once (newer window), its neighbours too.
    w = 30 * DAY
    dates = [NOW - w, NOW - w - 1, NOW - w + 1, NOW - 2 * w, NOW - 2 * w - 1]
    raws = [make_raw(f"b{i}@example.test", d) for i, d in enumerate(dates)]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    await ingest(store, IngestSource(fb), clock=lambda: NOW)
    assert store.status().messages == 5
    assert len(raw_tb_ids(fb)) == 5


@pytest.mark.asyncio
async def test_stale_cursor_restarts_the_window(store: Store, tmp_path: Path) -> None:
    raws = [make_raw(f"c{i}@example.test", NOW - DAY - i) for i in range(5)]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]), page_size=2)

    class Voiding(IngestSource):
        voided = 0

        async def page(self, folder_id: str, *a: Any, cursor: str | None = None, **kw: Any) -> Page:
            if cursor is not None and Voiding.voided < self.limit:
                Voiding.voided += 1
                fb.end_session()  # the bridge session ends: every cursor goes stale
            return await super().page(folder_id, *a, cursor=cursor, **kw)

        limit = 1

    await ingest(store, Voiding(fb), clock=lambda: NOW)
    assert store.status().messages == 5 and Voiding.voided == 1
    first_queries = [
        p
        for m, p in fb.calls
        if m == "mail.messages.page" and "cursor" not in p and "to_date" not in p
    ]
    assert len(first_queries) == 2  # the first window was paged again from its start


@pytest.mark.asyncio
async def test_a_cursor_that_keeps_going_stale_fails_loudly(store: Store, tmp_path: Path) -> None:
    raws = [make_raw(f"c{i}@example.test", NOW - DAY - i) for i in range(5)]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]), page_size=2)

    class AlwaysVoiding(IngestSource):
        async def page(self, folder_id: str, *a: Any, cursor: str | None = None, **kw: Any) -> Page:
            if cursor is not None:
                fb.end_session()
            return await super().page(folder_id, *a, cursor=cursor, **kw)

    with pytest.raises(IngestError):
        await ingest(store, AlwaysVoiding(fb), clock=lambda: NOW)
    queries = [p for m, p in fb.calls if m == "mail.messages.page" and "cursor" not in p]
    assert len(queries) == MAX_WINDOW_RESTARTS + 1


# ---- stale ids between page and raw -------------------------------------------------------------


@pytest.mark.asyncio
async def test_item_moved_between_page_and_raw(store: Store, fb: FakeBridge) -> None:
    key = "enc-base64"
    found = (await fb.request("mail.messages.find", FindParams(header_message_id=hmid(key)))).root
    assert [f.folder_id for f in found] == ["acct1:/INBOX"]
    target = found[0].tb_id

    class Moving(IngestSource):
        moved = False

        async def raw(self, tb_id: int) -> bytes:
            if tb_id == target and not Moving.moved:
                Moving.moved = True
                await fb.move([tb_id], "acct2:/Projects")  # a folder not yet backfilled
            return await super().raw(tb_id)

    await ingest(store, Moving(fb))
    assert Moving.moved
    assert [loc.folder_id for loc in store.locations(MESSAGES[key]["mid"])] == ["acct2:/Projects"]
    rows = {r["folder_id"]: r for r in runs(store)}
    assert rows["acct1:/INBOX"]["stale_retries"] == 1
    assert sum(r["stale_retries"] for r in rows.values()) == 1
    st = store.status()
    assert (st.messages, st.locations) == (DISTINCT, COPIES)
    counts = per_folder(store)
    assert counts["acct1:/INBOX"] == FOLDER_COUNTS["acct1:/INBOX"] - 1
    assert counts["acct2:/Projects"] == FOLDER_COUNTS["acct2:/Projects"] + 1


@pytest.mark.asyncio
async def test_restart_between_page_and_raw_re_resolves(store: Store, fb: FakeBridge) -> None:
    class Restarting(IngestSource):
        restarted = False

        async def raw(self, tb_id: int) -> bytes:
            if not Restarting.restarted:
                Restarting.restarted = True
                fb.restart()
            return await super().raw(tb_id)

    await ingest(store, Restarting(fb))
    assert Restarting.restarted
    assert_fixture_counts(store)
    assert all(r["stale_retries"] == 0 for r in runs(store))
    assert any(m == "mail.messages.find" for m, _ in fb.calls)


# ---- large messages ------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def large_mbox(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("large")
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "gen_synthetic_mailbox.py"), "--large", str(out)],
        check=True,
        capture_output=True,
    )
    return out / "large.mbox"


@pytest.mark.heavy  # ~1.8 s (> 25 MiB message); fast twin: test_large_messages_at_a_lowered_raw_max
@pytest.mark.asyncio
async def test_large_messages(store: Store, tmp_path: Path, large_mbox: Path) -> None:
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], [])]))
    folder_id = fb.add_mbox("m1", "Large", large_mbox)
    big, many = fb.tb_ids(folder_id)
    await ingest(store, IngestSource(fb))
    big_row = store.get_message(mid_of("large-25mb@example.test") or "")
    many_row = store.get_message(mid_of("many-attachments@example.test") or "")
    assert big_row is not None and many_row is not None
    assert big_row.parse_status == "partial"
    assert big_row.content_hash is not None and big_row.content_hash.startswith("full-")
    assert [a.filename for a in big_row.attachments] == ["large.bin"]
    assert len(many_row.attachments) == 150
    assert big not in raw_tb_ids(fb) and many in raw_tb_ids(fb)
    assert [p["tb_id"] for m, p in fb.calls if m == "mail.messages.full"] == [big]


@pytest.mark.asyncio
async def test_large_messages_at_a_lowered_raw_max(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `slow` test above with RAW_MAX lowered and the large cases scaled to it (L1): the
    over-limit message is read by ``full`` only (page size check, no ``raw``), recorded partial
    with a ``full-`` hash and its attachment; the 150-attachment one is read raw."""
    lower_raw_max(monkeypatch)
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], [])]))
    folder_id = fb.add_mbox("m1", "Large", small_large_mbox(tmp_path / "large.mbox"))
    big, many = fb.tb_ids(folder_id)
    await ingest(store, IngestSource(fb))
    big_row = store.get_message(mid_of(OVER_ID) or "")
    many_row = store.get_message(mid_of(MANY_ID) or "")
    assert big_row is not None and many_row is not None
    assert big_row.parse_status == "partial"
    assert big_row.content_hash is not None and big_row.content_hash.startswith("full-")
    assert [a.filename for a in big_row.attachments] == ["large.bin"]
    assert len(many_row.attachments) == MANY
    assert big not in raw_tb_ids(fb) and many in raw_tb_ids(fb)
    assert [p["tb_id"] for m, p in fb.calls if m == "mail.messages.full"] == [big]


@pytest.mark.asyncio
async def test_too_large_on_raw_falls_back_to_full_and_skips_without_message_id(
    store: Store, tmp_path: Path
) -> None:
    raws = [
        make_raw("big-ok@example.test", NOW - DAY),
        make_raw(None, NOW - DAY - 1, subject="no id"),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))

    class TooLarge(IngestSource):
        async def raw(self, tb_id: int) -> bytes:
            raise BridgeError("too_large", "synthetic: over RAW_MAX")

    await ingest(store, TooLarge(fb), clock=lambda: NOW)
    st = store.status()
    assert (st.messages, st.skipped_large_noid) == (1, 1)
    assert store.get_message(mid_of("big-ok@example.test") or "") is not None
    assert runs(store)[0]["skipped_large_noid"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("spoof_first", [True, False])
async def test_a_spoof_padded_past_raw_max_conflicts_with_the_raw_genuine_copy(
    store: Store, tmp_path: Path, spoof_first: bool
) -> None:
    """T14 (#5, #30 amendment 7 as amended): a spoof that reuses the Message-ID and is padded
    past RAW_MAX takes the full path (``full-`` hash). Its mid is conflicted, in either ingest
    order (folders are backfilled one after the other), and never classified."""
    genuine = make_raw("pay@example.test", NOW - DAY, body="genuine invoice")
    spoof = make_raw("pay@example.test", NOW - DAY + 60, body="pay the attacker zqspoof")
    first, second = (spoof, genuine) if spoof_first else (genuine, spoof)
    fb = FakeBridge(
        mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], [first]), ("Bills", [], [second])])
    )
    (big,) = fb.tb_ids("m1:/INBOX" if spoof_first else "m1:/Bills")

    class SpoofTooLarge(IngestSource):
        async def raw(self, tb_id: int) -> bytes:
            if tb_id == big:
                raise BridgeError("too_large", "synthetic: padded over RAW_MAX")
            return await super().raw(tb_id)

    await ingest(store, SpoofTooLarge(fb), clock=lambda: NOW)
    mid = mid_of("pay@example.test") or ""
    kinds = sorted(loc.content_hash.startswith("full-") for loc in store.locations(mid))
    assert kinds == [False, True]  # one raw-derived copy, one full- copy
    m = store.get_message(mid)
    assert m is not None
    assert (m.conflicted, m.conflict_class) == (True, None)
    assert store.status().conflicts_unclassified == 1


# ---- headers-only items ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_headers_only_items_are_not_fetched_until_downloaded(
    store: Store, fb: FakeBridge
) -> None:
    inv = (
        (await fb.request("mail.messages.find", FindParams(header_message_id=hmid("invoice-0013"))))
        .root[0]
        .tb_id
    )
    noid_copy = MESSAGES["noid-scanner"]["copies"][0]
    noid = fb.tb_ids("acct1:/INBOX")[noid_copy["locator"]]
    noid_raw = tfm.read_copy(FIXTURE, MANIFEST, noid_copy)
    assert await IngestSource(fb).raw(noid) == noid_raw  # pins the locator
    fb.calls.clear()
    fb.set_headers_only(inv, True)
    fb.set_headers_only(noid, True)

    await ingest(store, IngestSource(fb))
    st = store.status()
    assert (st.messages, st.locations, st.headers_only, st.x_mids) == (
        DISTINCT - 2,
        COPIES - 2,
        2,
        2,
    )
    assert not {inv, noid} & set(raw_tb_ids(fb))
    rows = dict(store.connection.execute("SELECT folder_id, mid FROM headers_only_items"))
    assert rows == {"acct1:/Invoices": MESSAGES["invoice-0013"]["mid"], "acct1:/INBOX": None}
    n_runs = len(runs(store))

    fb.set_headers_only(inv, False)
    fb.set_headers_only(noid, False)
    start = len(fb.calls)
    await ingest(store, IngestSource(fb))
    assert_fixture_counts(store)
    assert store.status().headers_only == 0
    assert set(raw_tb_ids(fb, start)) == {inv, noid}  # only the two seconds were re-paged
    new = {r["folder_id"]: r for r in runs(store)[n_runs:]}
    assert set(new) == {"acct1:/Invoices", "acct1:/INBOX"}
    assert {r["kind"] for r in new.values()} == {"hourly"}  # never mixed into backfill rows
    # a late download is not churn (A5 is about TB rewriting mail it already had)
    assert new["acct1:/INBOX"]["x_mid_churn"] == 0
    assert new["acct1:/Invoices"]["x_mid_churn"] == 0


@pytest.mark.asyncio
async def test_headers_only_revisit_pages_ranges_once_and_skips_indexed_items(
    store: Store, tmp_path: Path
) -> None:
    d1, d2 = NOW - DAY, NOW - 2 * DAY
    raws = [
        make_raw("a@example.test", d1),  # headers-only, then downloaded
        make_raw("b@example.test", d1),  # indexed, same second as a
        make_raw("c@example.test", d2),  # headers-only, then downloaded
        make_raw(None, d2, subject="d"),  # indexed md5: item, no NULL-mid row at d2
        make_raw(None, d2, subject="g"),  # md5: item over RAW_MAX -> skipped_items at d2
        make_raw("f@example.test", NOW - 3 * DAY),  # indexed, at no headers-only second
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    a, _b, c, _d, g, _f = fb.tb_ids("m1:/INBOX")

    class TooLargeG(IngestSource):
        async def raw(self, tb_id: int) -> bytes:
            if tb_id == g:
                raise BridgeError("too_large", "synthetic: over RAW_MAX")
            return await super().raw(tb_id)

    fb.set_headers_only(a, True)
    fb.set_headers_only(c, True)
    await ingest(store, TooLargeG(fb), clock=lambda: NOW)
    st = store.status()
    assert (st.messages, st.headers_only, st.skipped_large_noid) == (3, 2, 1)
    n_runs = len(runs(store))

    fb.set_headers_only(a, False)
    fb.set_headers_only(c, False)
    start = len(fb.calls)
    await ingest(store, TooLargeG(fb), clock=lambda: NOW)
    queries = [p for m, p in fb.calls[start:] if m == "mail.messages.page" and "cursor" not in p]
    assert len(queries) == 1  # both seconds lie in one window: one query
    assert set(raw_tb_ids(fb, start)) == {a, c}  # b, d, f not fetched again
    st = store.status()
    assert (st.messages, st.headers_only, st.skipped_large_noid) == (5, 0, 1)
    new = runs(store)[n_runs:]
    assert [(r["kind"], r["n_msgs"]) for r in new] == [("hourly", 2)]


@pytest.mark.asyncio
async def test_headers_only_revisit_handles_a_second_split_across_ranges_once(
    store: Store, tmp_path: Path
) -> None:
    # W = 1 day: coalesce_dates puts top and top - W + 1 in one range and top - W in the next
    # (older) one, whose [lo, hi + 1] query also returns the items at top - W + 1.
    cfg = TernConfig(backfill_window_days=1)
    top = NOW - 2 * DAY
    mid_s, low = top - DAY + 1, top - DAY
    assert coalesce_dates([top, mid_s, low], DAY) == [(mid_s, top), (low, low)]
    raws = [
        make_raw("top@example.test", top),
        make_raw("mid@example.test", mid_s),
        make_raw("low@example.test", low),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    ids = fb.tb_ids("m1:/INBOX")
    for tb_id in ids:
        fb.set_headers_only(tb_id, True)
    await ingest(store, IngestSource(fb), cfg, clock=lambda: NOW)
    assert store.status().headers_only == 3
    n_runs = len(runs(store))

    for tb_id in ids:
        fb.set_headers_only(tb_id, False)
    start = len(fb.calls)
    await ingest(store, IngestSource(fb), cfg, clock=lambda: NOW)
    assert sorted(raw_tb_ids(fb, start)) == sorted(ids)  # each fetched exactly once
    assert (store.status().messages, store.status().headers_only) == (3, 0)
    assert [(r["kind"], r["n_msgs"]) for r in runs(store)[n_runs:]] == [("hourly", 3)]


@pytest.mark.asyncio
async def test_ingest_runs_rows_go_through_store_record_ingest_run(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Store.connection's docstring: ingest_runs is written by Store.record_ingest_run only.
    raws = [make_raw("a@example.test", NOW - DAY), make_raw("b@example.test", NOW - 2 * DAY)]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    a, _b = fb.tb_ids("m1:/INBOX")
    recorded: list[str] = []
    real = Store.record_ingest_run

    def spy(self: Store, run: IngestRun) -> None:
        recorded.append(run.kind)
        real(self, run)

    monkeypatch.setattr(Store, "record_ingest_run", spy)
    fb.set_headers_only(a, True)
    await ingest(store, IngestSource(fb), clock=lambda: NOW)
    fb.set_headers_only(a, False)
    await ingest(store, IngestSource(fb), clock=lambda: NOW)  # headers-only revisit
    assert recorded == ["backfill", "hourly"]
    assert [r["kind"] for r in runs(store)] == recorded


def test_a_full_record_date_outside_the_datetime_range_is_null() -> None:
    """Year 9999 shifted past datetime.max by a -2359 offset: a stored epoch that
    ``datetime.fromtimestamp`` cannot read back would break every search hit on it."""

    def rec(date: str) -> MessageRecord:
        full = FullMessage(
            headers={"Message-ID": ["<far@example.test>"], "Date": [date]},
            text_parts=[TextPart(mime="text/plain", text="x")],
            attachments=[],
        )
        return ingest_mod.record_from_full(mid_of("far@example.test") or "", full, 1)

    assert rec("Fri, 31 Dec 9999 23:59:59 -2359").date_utc is None
    assert rec("Fri, 31 Dec 9999 23:59:59 +0000").date_utc == 253402300799  # still in range


def test_coalesce_dates() -> None:
    assert coalesce_dates([], 10) == []
    assert coalesce_dates([5, 30, 21, 21, 3], 10) == [(21, 30), (3, 5)]
    assert coalesce_dates([0, 10], 10) == [(10, 10), (0, 0)]


@pytest.mark.asyncio
async def test_item_ingester_drops_only_its_own_headers_only_row(
    store: Store, tmp_path: Path
) -> None:
    # x: an ID-less (md5:) item already indexed; y: an ID-less item still headers-only at the
    # same second; z: a headers-only item with a Message-ID at that second.
    d = NOW - DAY
    raws = [
        make_raw(None, d, subject="x downloaded"),
        make_raw(None, d, subject="y held"),
        make_raw("z@example.test", d),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    x, y, z = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(y, True)
    fb.set_headers_only(z, True)
    source = IngestSource(fb)
    bf = Backfill(store, source, TernConfig(), clock=lambda: NOW)
    await bf.run()
    query = "SELECT tb_date, mid FROM headers_only_items ORDER BY ordinal"
    rows_before = [(d, None), (d, mid_of("z@example.test"))]
    assert store.connection.execute(query).fetchall() == rows_before
    (folder,) = await bf.list_folders()
    ingester = ItemIngester(store, source)

    async def write(tb_id: int) -> Counters:
        counters = Counters()
        (item,) = [i for i in await page_items(fb, folder.folder_id) if i.tb_id == tb_id]
        fetched = await ingester.fetch(folder, item, counters)
        await ingester.write(folder, fetched, source="event", counters=counters)
        return counters

    # re-writing x (already indexed here) must not take y's NULL-mid row
    await write(x)
    assert store.connection.execute(query).fetchall() == rows_before
    # a new ID-less item at another second after the backfill: x_mid_churn
    w = await fb.deliver(folder.folder_id, make_raw(None, d - 7, subject="w new"))
    assert (await write(w)).x_mid_churn == 1
    # y downloaded: its row goes, and a late download is not churn
    fb.set_headers_only(y, False)
    counters = await write(y)
    assert store.connection.execute(query).fetchall() == [(d, mid_of("z@example.test"))]
    assert (counters.x_mid_churn, counters.n_msgs) == (0, 1)
    # z downloaded: its row goes, by mid
    fb.set_headers_only(z, False)
    await write(z)
    assert store.connection.execute(query).fetchall() == []
    assert (store.status().messages, store.status().x_mids) == (4, 3)


@pytest.mark.asyncio
async def test_last_report_follows_the_last_folder_done(store: Store, tmp_path: Path) -> None:
    raws = [make_raw(f"r{i}@example.test", NOW - DAY - i) for i in range(2)]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    reports: list[BatchReport] = []
    bf = Backfill(store, IngestSource(fb), TernConfig(), batch_size=1, clock=lambda: NOW)
    report = await bf.run(reports.append)
    # the last item fell on a batch boundary; the final report still counts the folder done
    assert reports[-1] == BatchReport(
        items=2, total=2, remaining=0, folders_done=1, folders_total=1
    )
    assert report.batches == len(reports) == 3


def test_full_text_never_converts_with_a_spent_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    limits: list[int] = []
    real = ingest_mod.html_to_text_limited

    def recording(html: str, limit: int) -> tuple[str, bool]:
        limits.append(limit)
        return real(html, limit)

    monkeypatch.setattr(ingest_mod, "html_to_text_limited", recording)
    # 600k two-byte characters: under the limit in characters, over the cap in bytes
    big = "<p>" + "é" * 600_000 + "</p>"
    full = FullMessage(
        headers={"Message-ID": ["<big@example.test>"]},
        text_parts=[TextPart(mime="text/html", text=big), TextPart(mime="text/html", text="x")],
        attachments=[],
    )
    rec = ingest_mod.record_from_full(mid_of("big@example.test") or "", full, len(big))
    assert limits == [TEXT_CAP_BYTES]  # the second part is not converted at all
    assert rec.body_text is not None and len(rec.body_text.encode()) <= TEXT_CAP_BYTES


@pytest.mark.asyncio
async def test_parsing_runs_off_the_event_loop(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T13: a slow parse (bounded, but seconds on hostile headers) must not block the loop that
    serves the bridge; both the raw and the full path parse in a worker thread."""
    loop_thread = threading.get_ident()
    threads: list[tuple[str, int]] = []
    real_raw, real_full = ingest_mod.record_from_raw, ingest_mod.record_from_full

    def on_raw(raw: bytes) -> MessageRecord:
        threads.append(("raw", threading.get_ident()))
        return real_raw(raw)

    def on_full(mid: str, full: FullMessage, size: int) -> MessageRecord:
        threads.append(("full", threading.get_ident()))
        return real_full(mid, full, size)

    monkeypatch.setattr(ingest_mod, "record_from_raw", on_raw)
    monkeypatch.setattr(ingest_mod, "record_from_full", on_full)
    raws = [make_raw("small@example.test", NOW - DAY), make_raw("big@example.test", NOW - DAY - 1)]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    (_, big) = fb.tb_ids("m1:/INBOX")

    class BigTooLarge(IngestSource):
        async def raw(self, tb_id: int) -> bytes:
            if tb_id == big:
                raise BridgeError("too_large", "synthetic: over RAW_MAX")
            return await super().raw(tb_id)

    await ingest(store, BigTooLarge(fb), clock=lambda: NOW)
    assert sorted(kind for kind, _ in threads) == ["full", "raw"]
    assert all(ident != loop_thread for _, ident in threads)


def test_full_path_strips_invisible_characters_from_attachment_names() -> None:
    """T9 on the over-RAW_MAX path: zero-width and bidi characters (an RLO filename spoof)
    leave attachment names as they do on the raw path; a name of only such characters is
    no name."""
    full = FullMessage(
        headers={"Message-ID": ["<att@example.test>"]},
        text_parts=[TextPart(mime="text/plain", text="body")],
        attachments=[
            AttachmentInfo(name="invoice\u202efdp.exe", mime="application/x", size=3),
            AttachmentInfo(name="re\u200bport\U000e0041.pdf", mime="application/pdf", size=4),
            AttachmentInfo(name="\u200b\u2066", mime="application/x", size=5),
        ],
    )
    rec = ingest_mod.record_from_full(mid_of("att@example.test") or "", full, 1)
    assert [a.filename for a in rec.attachments] == ["invoicefdp.exe", "report.pdf", None]


# ---- privacy -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logs_carry_no_names_paths_or_sentinels(
    store: Store, fb: FakeBridge, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await ingest(store, IngestSource(fb))
    assert caplog.records
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
    forbidden += list(FOLDER_COUNTS)  # folder ids
    forbidden += [f["path"] for a in MANIFEST["accounts"] for f in a["folders"]]
    forbidden += [f"{a['key']}/{f['path']}" for a in MANIFEST["accounts"] for f in a["folders"]]
    forbidden += tfm.mail_strings(FIXTURE, MANIFEST)  # subjects, From/To/Cc addresses (L4)
    leaked = sorted({f for f in forbidden if f in blob})
    assert leaked == []
    assert any("fold-" in t for t in texts)  # folders appear as #28 hashes


@pytest.mark.asyncio
async def test_still_headers_only_rows_are_replaced_not_duplicated(
    store: Store, tmp_path: Path
) -> None:
    # Two headers-only items at one second (one without Message-ID, so TB gives it an md5: id),
    # re-paged by a later run while still headers-only: the rows are replaced, not added to.
    d = NOW - DAY
    raws = [make_raw("ho@example.test", d), make_raw(None, d, subject="no id")]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    for tb_id in fb.tb_ids("m1:/INBOX"):
        fb.set_headers_only(tb_id, True)
    query = "SELECT tb_date, ordinal, mid FROM headers_only_items ORDER BY ordinal"
    await ingest(store, IngestSource(fb), clock=lambda: NOW)
    rows = store.connection.execute(query).fetchall()
    assert rows == [(d, 0, mid_of("ho@example.test")), (d, 1, None)]
    for _ in range(2):
        await ingest(store, IngestSource(fb), clock=lambda: NOW)
        assert store.connection.execute(query).fetchall() == rows
    assert store.status().headers_only == 2 and store.status().messages == 0
    assert raw_tb_ids(fb) == []


class _Replying:
    def __init__(self, result: Any) -> None:
        self.result = result

    async def request(self, method: str, params: Any) -> Any:
        return self.result

    async def notify(self, method: str, params: Any) -> None:
        raise AssertionError("not used")


@pytest.mark.asyncio
async def test_source_refuses_malformed_results() -> None:
    with pytest.raises(ProtocolViolation):
        await IngestSource(_Replying(Raw(raw_b64="aGk=!"))).raw(1)
    with pytest.raises(ProtocolViolation):
        await IngestSource(_Replying(Raw(raw_b64="AA=="))).accounts()
    assert await IngestSource(_Replying(Raw(raw_b64="aGk="))).raw(1) == b"hi"


# ---- unreadable items (plan 04 merge review M5) -------------------------------------------------

TB_ERROR_TEXT = "SENTINEL-TB-ERROR-TEXT"


class Unreadable(IngestSource):
    """``raw``/``full`` of the tb_ids in ``bad`` give ``tb_error`` (TB failed to read them); the
    items in ``big`` are listed over RAW_MAX (so they go through ``full``)."""

    def __init__(self, fb: FakeBridge) -> None:
        super().__init__(fb)
        self.bad: set[int] = set()
        self.big: set[int] = set()
        self.refused: list[tuple[str, int]] = []

    async def page(
        self,
        folder_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        cursor: str | None = None,
    ) -> Page:
        page = await super().page(folder_id, from_date, to_date, cursor)
        items = [
            i.model_copy(update={"size": ingest_mod.RAW_MAX + 1}) if i.tb_id in self.big else i
            for i in page.items
        ]
        return page.model_copy(update={"items": items})

    def _check(self, method: str, tb_id: int) -> None:
        if tb_id in self.bad:
            self.refused.append((method, tb_id))
            raise BridgeError("tb_error", TB_ERROR_TEXT, tb_code="NS_ERROR_FAILURE")

    async def raw(self, tb_id: int) -> bytes:
        self._check("raw", tb_id)
        return await super().raw(tb_id)

    async def full(self, tb_id: int) -> FullMessage:
        self._check("full", tb_id)
        return await super().full(tb_id)


def skipped_rows(store: Store) -> list[tuple[int, int, str]]:
    return store.connection.execute(
        "SELECT tb_date, ordinal, reason FROM skipped_items ORDER BY tb_date, ordinal"
    ).fetchall()


@pytest.mark.asyncio
async def test_an_unreadable_item_is_skipped_and_its_folder_reaches_done(
    store: Store, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # One readable message and three TB cannot read: a Message-ID one and an ID-less one through
    # raw, a Message-ID one over RAW_MAX through full. Each gets a tb_error row; the folder is
    # done; the log carries TB's code only.
    d1, d2, d3, d4 = (NOW - k * DAY for k in (1, 2, 3, 4))
    raws = [
        make_raw("ok@example.test", d1),
        make_raw("bad@example.test", d2),
        make_raw(None, d3, subject="bad, no id"),
        make_raw("big-bad@example.test", d4),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    _ok, bad, bad_noid, big = fb.tb_ids("m1:/INBOX")
    src = Unreadable(fb)
    src.bad |= {bad, bad_noid, big}
    src.big.add(big)
    caplog.set_level(logging.INFO)
    report = await ingest(store, src, clock=lambda: NOW)
    assert one(store, "SELECT status FROM ingest_state WHERE folder_id = 'm1:/INBOX'") == "done"
    assert skipped_rows(store) == [(d4, 0, "tb_error"), (d3, 0, "tb_error"), (d2, 0, "tb_error")]
    st = store.status()
    assert (st.messages, st.skipped_tb_error, st.skipped_large_noid) == (1, 3, 0)
    assert store.get_message(mid_of("ok@example.test") or "") is not None
    assert report.counters.skipped_tb_error == 3
    assert sorted(src.refused) == sorted([("full", big), ("raw", bad), ("raw", bad_noid)])
    logged = [r for r in caplog.records if r.getMessage() == "item skipped: TB could not read it"]
    assert [(r.levelno, r.__dict__["tb_code"]) for r in logged] == [
        (logging.INFO, "NS_ERROR_FAILURE")
    ] * 3
    assert all(TB_ERROR_TEXT not in str(r.__dict__) for r in caplog.records)
    assert all("example.test" not in str(r.__dict__) for r in caplog.records)
    # a done folder is not paged again; the rows stay as they are
    start = len(fb.calls)
    await ingest(store, src, clock=lambda: NOW)
    assert not [m for m, _ in fb.calls[start:] if m == "mail.messages.page"]
    assert len(skipped_rows(store)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        pytest.param({"mail.messages.raw": "invalid_params"}, id="raw-invalid_params"),
        pytest.param({"mail.messages.raw": "unavailable"}, id="raw-unavailable"),
        pytest.param(
            {"mail.messages.raw": "stale_id", "mail.messages.find": "tb_error"},
            id="find-tb_error",
        ),
    ],
)
async def test_protocol_errors_still_stop_the_backfill(
    store: Store, tmp_path: Path, fault: dict[str, ErrorName]
) -> None:
    fb = FakeBridge(
        mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], [make_raw("a@example.test", NOW)])])
    )

    class Failing(IngestSource):
        def _check(self, method: str) -> None:
            name = fault.get(method)
            if name is not None:
                raise BridgeError(name, "synthetic", tb_code="X" if name == "tb_error" else None)

        async def raw(self, tb_id: int) -> bytes:
            self._check("mail.messages.raw")
            return await super().raw(tb_id)

        async def find(self, header_message_id: str) -> list[Any]:
            self._check("mail.messages.find")
            return list(await super().find(header_message_id))

    with pytest.raises(BridgeError):
        await ingest(store, Failing(fb), clock=lambda: NOW)
    assert skipped_rows(store) == []
    assert store.status().messages == 0


@pytest.mark.asyncio
async def test_a_stale_item_whose_re_resolved_copy_is_unreadable_is_skipped_not_stale(
    store: Store, tmp_path: Path
) -> None:
    d = NOW - DAY
    fb = FakeBridge(
        mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], [make_raw("s@example.test", d)])])
    )
    (target,) = fb.tb_ids("m1:/INBOX")

    class StaleThenUnreadable(IngestSource):
        calls = 0

        async def raw(self, tb_id: int) -> bytes:
            StaleThenUnreadable.calls += 1
            if StaleThenUnreadable.calls == 1:
                raise BridgeError("stale_id", "tb_id is not valid in this session")
            raise BridgeError("tb_error", TB_ERROR_TEXT, tb_code="NS_ERROR_FAILURE")

    report = await ingest(store, StaleThenUnreadable(fb), clock=lambda: NOW)
    assert StaleThenUnreadable.calls == 2
    assert any(m == "mail.messages.find" for m, _ in fb.calls)
    assert skipped_rows(store) == [(d, 0, "tb_error")]
    assert (report.counters.skipped_tb_error, report.counters.stale_retries) == (1, 0)
    assert runs(store)[0]["stale_retries"] == 0
    assert target in fb.tb_ids("m1:/INBOX")


@pytest.mark.asyncio
async def test_an_unreadable_re_resolved_match_beside_a_read_one_is_logged(
    store: Store, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Plan 04 M5 review: two differing copies of one Message-ID; the first one's tb_id goes
    # stale and its re-resolve reads it but not the other (tb_error). That match is logged at
    # INFO with tb_code only; its row and count come from its own page entry, once.
    d = NOW - DAY
    raws = [
        make_raw("pair@example.test", d, subject="first"),
        make_raw("pair@example.test", d - 60, subject="second"),
    ]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    first, second = fb.tb_ids("m1:/INBOX")

    class StaleOnceThenBad(Unreadable):
        stale_left = {first}

        async def raw(self, tb_id: int) -> bytes:
            if tb_id in self.stale_left:
                self.stale_left.discard(tb_id)
                raise BridgeError("stale_id", "tb_id is not valid in this session")
            return await super().raw(tb_id)

    src = StaleOnceThenBad(fb)
    src.bad.add(second)
    caplog.set_level(logging.INFO)
    report = await ingest(store, src, clock=lambda: NOW)
    assert any(m == "mail.messages.find" for m, _ in fb.calls)
    assert skipped_rows(store) == [(d - 60, 0, "tb_error")]
    assert report.counters.skipped_tb_error == 1
    [rec] = [r for r in caplog.records if r.getMessage() == "re-resolved match not read"]
    assert rec.levelno == logging.INFO and rec.__dict__["tb_code"] == "NS_ERROR_FAILURE"
    assert all(TB_ERROR_TEXT not in str(r.__dict__) for r in caplog.records)


@pytest.mark.asyncio
async def test_a_headers_only_revisit_skips_an_unreadable_download_and_keeps_other_rows(
    store: Store, tmp_path: Path
) -> None:
    # At one second: a Message-ID item headers-only at backfill, downloaded later but
    # unreadable, and an ID-less item over RAW_MAX (a large_noid row). The revisit replaces the
    # second's headers-only rows, keeps the large_noid row (not every md5: item there was
    # handled) and appends the unreadable item's tb_error row; a later run adds nothing.
    d = NOW - DAY
    raws = [make_raw("late@example.test", d), make_raw(None, d, subject="large, no id")]
    fb = FakeBridge(mini_mailbox(tmp_path / "mb", [("INBOX", ["inbox"], raws)]))
    late, large = fb.tb_ids("m1:/INBOX")
    fb.set_headers_only(late, True)

    class NoIdFull(Unreadable):
        async def full(self, tb_id: int) -> FullMessage:
            got = await super().full(tb_id)
            return got.model_copy(update={"headers": {}})

    src = NoIdFull(fb)
    src.big.add(large)
    await ingest(store, src, clock=lambda: NOW)
    assert skipped_rows(store) == [(d, 0, "large_noid")]
    assert store.status().headers_only == 1

    fb.set_headers_only(late, False)
    src.bad.add(late)
    report = await ingest(store, src, clock=lambda: NOW)
    assert report.counters.skipped_tb_error == 1
    assert skipped_rows(store) == [(d, 0, "large_noid"), (d, 1, "tb_error")]
    assert store.status().headers_only == 0
    await ingest(store, src, clock=lambda: NOW)
    assert skipped_rows(store) == [(d, 0, "large_noid"), (d, 1, "tb_error")]


@pytest.mark.asyncio
async def test_an_unreadable_content_copy_leaves_the_conflict_unclassified(
    store: Store, fb: FakeBridge, caplog: pytest.LogCaptureFixture
) -> None:
    # Plan 04 M5: the second raw of any tb_id gives tb_error. In a clean backfill only the
    # content copy of inv-0077 is read twice (to classify the new conflict): the conflict is
    # kept unclassified and the run goes on.
    seen: set[int] = set()

    class SecondReadFails(IngestSource):
        async def raw(self, tb_id: int) -> bytes:
            if tb_id in seen:
                raise BridgeError("tb_error", TB_ERROR_TEXT, tb_code="NS_ERROR_FAILURE")
            seen.add(tb_id)
            return await super().raw(tb_id)

    caplog.set_level(logging.INFO)
    await ingest(store, SecondReadFails(fb))
    assert_fixture_counts(store)
    inv = store.get_message(INV_MID)
    assert inv is not None and (inv.conflicted, inv.conflict_class) == (True, None)
    assert store.status().conflicts_unclassified == 1
    assert [r.__dict__["tb_code"] for r in caplog.records if "tb_code" in r.__dict__] == [
        "NS_ERROR_FAILURE"
    ]
    assert all(TB_ERROR_TEXT not in str(r.__dict__) for r in caplog.records)
