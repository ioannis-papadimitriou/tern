"""FakeBridge (plan 03 step 4): the synthetic mailbox behind tern-bridge/1.0, with TB semantics."""

from __future__ import annotations

import base64
import json
import mailbox
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from large_mail import lower_raw_max, small_large_mbox
from pydantic import ValidationError

from tern.bridge.client import ExtensionRequester
from tern.bridge.protocol import (
    RAW_MAX,
    Account,
    BridgeError,
    Empty,
    FindParams,
    FoldersListParams,
    Page,
    PageParams,
    TbIdParams,
)
from tern.testing.fake_bridge import MAX_OPEN_CURSORS, PAGE_SIZE, FakeBridge

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "synthetic_mailbox"
MANIFEST = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
FOLDERS = {f"{a['key']}:/{f['path']}": f for a in MANIFEST["accounts"] for f in a["folders"]}


@pytest.fixture
def fb() -> FakeBridge:
    return FakeBridge(FIXTURE)


async def page_all(fb: FakeBridge, folder_id: str, **window: Any) -> list[Any]:
    items: list[Any] = []
    page: Page = await fb.request("mail.messages.page", PageParams(folder_id=folder_id, **window))
    items += page.items
    while page.next_cursor is not None:
        assert len(page.items) <= fb.page_size
        page = await fb.request(
            "mail.messages.page", PageParams(folder_id=folder_id, cursor=page.next_cursor)
        )
        items += page.items
    return items


async def stale(coro: Any) -> str:
    with pytest.raises(BridgeError) as exc_info:
        await coro
    return exc_info.value.name


def test_is_an_extension_requester(fb: FakeBridge) -> None:
    link: ExtensionRequester = fb  # pyright checks the Protocol
    assert link is fb


@pytest.mark.asyncio
async def test_accounts_and_folders_match_the_manifest(fb: FakeBridge) -> None:
    accounts = (await fb.request("mail.accounts.list", Empty())).root
    assert [a.account_id for a in accounts] == [a["key"] for a in MANIFEST["accounts"]]
    assert all(len(a.identities) == 1 and a.identities[0].is_default for a in accounts)
    for a in MANIFEST["accounts"]:
        folders = (
            await fb.request("mail.folders.list", FoldersListParams(account_id=a["key"]))
        ).root
        assert {f.folder_id: f.total for f in folders} == {
            f"{a['key']}:/{f['path']}": f["count"] for f in a["folders"]
        }


@pytest.mark.asyncio
async def test_full_paging_returns_manifest_counts_in_folder_order(fb: FakeBridge) -> None:
    total = 0
    for folder_id, f in FOLDERS.items():
        items = await page_all(fb, folder_id)
        assert len(items) == f["count"], folder_id
        assert [i.tb_id for i in items] == fb.tb_ids(folder_id)  # folder (file) order
        total += len(items)
    assert total == MANIFEST["message_count"]


@pytest.mark.asyncio
async def test_pages_are_not_date_sorted(fb: FakeBridge) -> None:
    # messages.query cannot sort (S1 F3): a consumer that assumes date order must fail here.
    items = await page_all(fb, "acct1:/INBOX")
    dates = [i.date for i in items]
    assert dates != sorted(dates) and dates != sorted(dates, reverse=True)


@pytest.mark.asyncio
async def test_pages_have_page_size_items_and_single_use_cursors() -> None:
    fb = FakeBridge(FIXTURE, page_size=10)
    p1 = await fb.request("mail.messages.page", PageParams(folder_id="acct1:/INBOX"))
    assert len(p1.items) == 10 and p1.next_cursor is not None
    await fb.request(
        "mail.messages.page", PageParams(folder_id="acct1:/INBOX", cursor=p1.next_cursor)
    )
    again = fb.request(
        "mail.messages.page", PageParams(folder_id="acct1:/INBOX", cursor=p1.next_cursor)
    )
    assert await stale(again) == "stale_id"
    assert PAGE_SIZE == 100


@pytest.mark.asyncio
async def test_date_windows_are_inclusive_at_both_ends(fb: FakeBridge) -> None:
    folder = "acct1:/Newsletters"  # has several messages sharing a Date
    items = await page_all(fb, folder)
    at = sorted(i.date for i in items)[len(items) // 2]
    sharing = {i.tb_id for i in items if i.date == at}
    lower = await page_all(fb, folder, from_date=at - timedelta(days=3650), to_date=at)
    upper = await page_all(fb, folder, from_date=at, to_date=at + timedelta(days=3650))
    assert sharing <= {i.tb_id for i in lower}
    assert sharing <= {i.tb_id for i in upper}  # adjacent windows overlap at the endpoint
    assert len(lower) + len(upper) == len(items) + len(sharing)


@pytest.mark.asyncio
async def test_slow_first_page_is_empty_but_continues() -> None:
    fb = FakeBridge(FIXTURE, slow_first_page=True)
    first = await fb.request("mail.messages.page", PageParams(folder_id="acct1:/Sent"))
    assert first.items == [] and first.next_cursor is not None
    assert len(await page_all(fb, "acct1:/Sent")) == FOLDERS["acct1:/Sent"]["count"]


@pytest.mark.asyncio
async def test_tb_id_from_before_a_restart_is_stale(fb: FakeBridge) -> None:
    old = fb.tb_ids("acct1:/INBOX")[0]
    raw_before = (await fb.request("mail.messages.raw", TbIdParams(tb_id=old))).raw_b64
    fb.restart()
    for method in ("mail.messages.raw", "mail.messages.full", "mail.open"):
        assert await stale(fb.request(method, TbIdParams(tb_id=old))) == "stale_id"
    new = fb.tb_ids("acct1:/INBOX")[0]
    assert new != old
    assert (await fb.request("mail.messages.raw", TbIdParams(tb_id=new))).raw_b64 == raw_before


@pytest.mark.asyncio
async def test_cursor_from_before_a_restart_is_stale() -> None:
    fb = FakeBridge(FIXTURE, page_size=5)
    p1 = await fb.request("mail.messages.page", PageParams(folder_id="acct1:/INBOX"))
    fb.restart()
    old = fb.request(
        "mail.messages.page", PageParams(folder_id="acct1:/INBOX", cursor=p1.next_cursor)
    )
    assert await stale(old) == "stale_id"


@pytest.mark.asyncio
async def test_session_end_voids_cursors_but_not_tb_ids() -> None:
    # Mirrors handlers.test.ts "the session end makes every cursor stale": the bridge session
    # (port lost, bridge.reconnected) owns cursors; TB's session, and so its tb_ids, goes on.
    fb = FakeBridge(FIXTURE, page_size=5)
    p1 = await fb.request("mail.messages.page", PageParams(folder_id="acct1:/INBOX"))
    tb_id = p1.items[0].tb_id
    raw_before = (await fb.request("mail.messages.raw", TbIdParams(tb_id=tb_id))).raw_b64
    fb.end_session()
    old = fb.request(
        "mail.messages.page", PageParams(folder_id="acct1:/INBOX", cursor=p1.next_cursor)
    )
    assert await stale(old) == "stale_id"
    assert fb.tb_ids("acct1:/INBOX")[0] == tb_id
    raw_after = (await fb.request("mail.messages.raw", TbIdParams(tb_id=tb_id))).raw_b64
    assert raw_after == raw_before


@pytest.mark.asyncio
async def test_keeps_at_most_max_open_cursors_evicting_the_oldest() -> None:
    # Mirrors handlers.test.ts "keeps at most MAX_OPEN_CURSORS lists open, aborting the oldest".
    assert MAX_OPEN_CURSORS == 64
    fb = FakeBridge(FIXTURE, page_size=1)
    assert fb.max_open_cursors == MAX_OPEN_CURSORS
    folder = "acct1:/INBOX"
    cursors: list[str] = []
    for _ in range(MAX_OPEN_CURSORS + 1):
        page = await fb.request("mail.messages.page", PageParams(folder_id=folder))
        assert page.next_cursor is not None
        cursors.append(page.next_cursor)
    first = fb.request("mail.messages.page", PageParams(folder_id=folder, cursor=cursors[0]))
    assert await stale(first) == "stale_id"
    for token in (cursors[-1], cursors[1]):  # the newest, and the oldest still open
        page = await fb.request("mail.messages.page", PageParams(folder_id=folder, cursor=token))
        assert len(page.items) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["from_date", "to_date"])
async def test_a_naive_date_window_is_invalid_params(fb: FakeBridge, field: str) -> None:
    naive = datetime(2026, 5, 5, 12, 0, 0)  # no tzinfo: the naive date under test
    params = (
        PageParams(folder_id="acct1:/INBOX", from_date=naive)
        if field == "from_date"
        else PageParams(folder_id="acct1:/INBOX", to_date=naive)
    )
    with pytest.raises(BridgeError) as exc_info:
        await fb.request("mail.messages.page", params)
    assert (exc_info.value.name, exc_info.value.message) == (
        "invalid_params",
        f"{field}: date needs a UTC offset",
    )


@pytest.mark.asyncio
async def test_a_cursor_used_with_another_folder_is_invalid_and_not_consumed() -> None:
    fb = FakeBridge(FIXTURE, page_size=5)
    p1 = await fb.request("mail.messages.page", PageParams(folder_id="acct1:/INBOX"))
    assert p1.next_cursor is not None
    with pytest.raises(BridgeError) as exc_info:
        await fb.request(
            "mail.messages.page", PageParams(folder_id="acct1:/Sent", cursor=p1.next_cursor)
        )
    assert (exc_info.value.name, exc_info.value.message) == (
        "invalid_params",
        "cursor belongs to another folder",
    )
    p2 = await fb.request(
        "mail.messages.page", PageParams(folder_id="acct1:/INBOX", cursor=p1.next_cursor)
    )
    assert [i.tb_id for i in p2.items] == fb.tb_ids("acct1:/INBOX")[5:10]


@pytest.mark.asyncio
async def test_raw_is_the_stored_bytes(fb: FakeBridge) -> None:
    box = mailbox.Maildir(FIXTURE / "acct3" / "Offline", factory=None, create=False)
    stored = {box.get_bytes(k) for k in box.keys()}
    got = set()
    for tb_id in fb.tb_ids("acct3:/Offline"):
        raw = await fb.request("mail.messages.raw", TbIdParams(tb_id=tb_id))
        got.add(base64.b64decode(raw.raw_b64))
    assert got == stored


@pytest.mark.asyncio
async def test_find_returns_every_location_of_a_cross_folder_duplicate(fb: FakeBridge) -> None:
    msgs = {m["key"]: m for m in MANIFEST["messages"]}
    for dup in MANIFEST["cross_folder_duplicates"]:
        hmid = msgs[dup["key"]]["message_id"].strip("<>")
        found = (await fb.request("mail.messages.find", FindParams(header_message_id=hmid))).root
        assert sorted(f.folder_id for f in found) == sorted(
            f"{f.split('/')[0]}:/{f.split('/', 1)[1]}" for f in dup["folders"]
        )


@pytest.mark.asyncio
async def test_messages_without_message_id_get_a_generated_header_id(fb: FakeBridge) -> None:
    items = [i for fid in FOLDERS for i in await page_all(fb, fid)]
    generated = [i.header_message_id for i in items if i.header_message_id.startswith("md5:")]
    assert len(generated) == len(MANIFEST["no_message_id"])
    assert len(set(generated)) == len(generated)


@pytest.mark.asyncio
async def test_full_gives_headers_text_and_attachment_metadata(fb: FakeBridge) -> None:
    full = await fb.request("mail.messages.full", TbIdParams(tb_id=fb.tb_ids("acct1:/INBOX")[0]))
    assert "message-id" in full.headers and full.text_parts


@pytest.mark.asyncio
async def test_unknown_folder_and_account(fb: FakeBridge) -> None:
    with pytest.raises(BridgeError) as exc_info:
        await fb.request("mail.messages.page", PageParams(folder_id="acct1:/Nope"))
    assert (exc_info.value.name, exc_info.value.tb_code) == ("tb_error", "folder_not_found")
    with pytest.raises(BridgeError) as exc_info:
        await fb.request("mail.folders.list", FoldersListParams(account_id="nope"))
    assert exc_info.value.name == "invalid_params"


@pytest.mark.asyncio
async def test_draft_methods_are_not_served_yet(fb: FakeBridge) -> None:
    # Plan 06b step 3 serves the mail write requests; drafts (plan 10) stay unserved.
    from tern.bridge.protocol import ComposeDetails, ComposeOpenParams, DraftSaveParams

    with pytest.raises(BridgeError) as exc_info:
        await fb.request(
            "mail.draft.save",
            DraftSaveParams(identity_id="acct1-id1", to=[], cc=[], subject="s", body_text="b"),
        )
    assert exc_info.value.name == "method_not_available"
    details = ComposeDetails(to=[], cc=[], subject="s", body_text="b")
    with pytest.raises(BridgeError) as exc_info:
        await fb.request("mail.compose.open", ComposeOpenParams(mode="new", details=details))
    assert exc_info.value.name == "method_not_available"


@pytest.mark.asyncio
async def test_a_result_that_violates_its_1_0_model_raises(fb: FakeBridge) -> None:
    broken = Account.model_construct(account_id="acct9", name="x", type="imap", identities=None)
    fb._accounts.append(broken)  # pyright: ignore[reportPrivateUsage]  # planting a broken result
    with pytest.raises(ValidationError):
        await fb.request("mail.accounts.list", Empty())


@pytest.mark.asyncio
async def test_calls_are_recorded_in_wire_form(fb: FakeBridge) -> None:
    await fb.request("mail.folders.list", FoldersListParams(account_id="acct1"))
    assert fb.calls == [("mail.folders.list", {"account_id": "acct1"})]


# -- events -------------------------------------------------------------------------------------


@pytest.fixture
def events(fb: FakeBridge) -> list[tuple[str, dict[str, Any]]]:
    got: list[tuple[str, dict[str, Any]]] = []

    async def sink(method: str, params: Any) -> None:
        got.append((method, params.model_dump(mode="json")))

    fb.on_event = sink
    return got


@pytest.mark.asyncio
async def test_move_gives_new_ids_and_one_event_per_source_folder(
    fb: FakeBridge, events: list[tuple[str, dict[str, Any]]]
) -> None:
    a = fb.tb_ids("acct1:/INBOX")[:2]
    b = fb.tb_ids("acct1:/Invoices")[:1]
    new = await fb.move(a + b, "acct1:/Archive")
    assert [m for m, _ in events] == ["event.mail.moved", "event.mail.moved"]
    assert [(p["src_folder_id"], len(p["items"])) for _, p in events] == [
        ("acct1:/INBOX", 2),
        ("acct1:/Invoices", 1),
    ]
    assert await stale(fb.request("mail.messages.raw", TbIdParams(tb_id=a[0]))) == "stale_id"
    assert set(new) <= set(fb.tb_ids("acct1:/Archive"))


@pytest.mark.asyncio
async def test_deliver_delete_update_and_folder_events(
    fb: FakeBridge, events: list[tuple[str, dict[str, Any]]]
) -> None:
    raw = (
        b"Message-ID: <new-1@example.test>\r\nDate: Fri, 13 Nov 2026 10:00:00 +0000\r\n"
        b"From: a@example.test\r\nSubject: synthetic\r\n\r\nbody\r\n"
    )
    tb_id = await fb.deliver("acct1:/INBOX", raw)
    await fb.update(tb_id, flagged=True, tags=["$label1"])
    await fb.delete([tb_id])
    await fb.folder_changed("acct1:/INBOX")
    assert [m for m, _ in events] == [
        "event.mail.new",
        "event.mail.updated",
        "event.mail.deleted",
        "event.folder.changed",
    ]
    assert events[1][1]["items"][0]["flags"]["flagged"] is True
    assert events[2][1]["header_message_ids"] == ["new-1@example.test"]


@pytest.mark.asyncio
async def test_dropped_events_still_change_the_mailbox(
    fb: FakeBridge, events: list[tuple[str, dict[str, Any]]]
) -> None:
    fb.drop_events(1)
    tb_id = fb.tb_ids("acct1:/INBOX")[0]
    await fb.update(tb_id, read=True)
    assert events == [] and fb.dropped_events == 1
    found = (
        await fb.request(
            "mail.messages.find",
            FindParams(header_message_id=(await page_all(fb, "acct1:/INBOX"))[0].header_message_id),
        )
    ).root
    assert found[0].flags.read is True


# -- too_large ----------------------------------------------------------------------------------


@pytest.mark.heavy  # ~1.8 s (generates > 25 MiB); fast twin: test_too_large_above_a_lowered_raw_max
@pytest.mark.asyncio
async def test_too_large_above_raw_max(fb: FakeBridge, tmp_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "gen_synthetic_mailbox.py"),
            "--large",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
    )
    folder_id = fb.add_mbox("acct1", "Large", tmp_path / "large.mbox")
    big, many = fb.tb_ids(folder_id)
    with pytest.raises(BridgeError) as exc_info:
        await fb.request("mail.messages.raw", TbIdParams(tb_id=big))
    assert exc_info.value.name == "too_large"
    full = await fb.request("mail.messages.full", TbIdParams(tb_id=big))
    assert [a.name for a in full.attachments] == ["large.bin"]
    assert full.attachments[0].size > RAW_MAX
    raw = await fb.request("mail.messages.raw", TbIdParams(tb_id=many))  # under the limit
    assert len(base64.b64decode(raw.raw_b64)) <= RAW_MAX


@pytest.mark.asyncio
async def test_too_large_above_a_lowered_raw_max(
    fb: FakeBridge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `slow` test above with RAW_MAX lowered and the large cases scaled to it (L1)."""
    raw_max = lower_raw_max(monkeypatch)
    folder_id = fb.add_mbox("acct1", "Large", small_large_mbox(tmp_path / "large.mbox"))
    big, many = fb.tb_ids(folder_id)
    with pytest.raises(BridgeError) as exc_info:
        await fb.request("mail.messages.raw", TbIdParams(tb_id=big))
    assert exc_info.value.name == "too_large"
    full = await fb.request("mail.messages.full", TbIdParams(tb_id=big))
    assert [a.name for a in full.attachments] == ["large.bin"]
    assert full.attachments[0].size > raw_max
    raw = await fb.request("mail.messages.raw", TbIdParams(tb_id=many))  # under the limit
    assert len(base64.b64decode(raw.raw_b64)) <= raw_max


# -- plan 04 step 7 additions ---------------------------------------------------------------------


def _one_message_mbox(path: Path, message_id: str) -> Path:
    raw = (
        f"Message-ID: <{message_id}>\nDate: Fri, 13 Nov 2026 10:00:00 +0000\n"
        "From: a@example.test\nTo: b@example.test\nSubject: synthetic\n\nbody\n"
    ).encode()
    box = mailbox.mbox(path)
    try:
        box.add(raw)
    finally:
        box.close()
    return path


@pytest.mark.asyncio
async def test_add_mbox_with_special_use(fb: FakeBridge, tmp_path: Path) -> None:
    folder_id = fb.add_mbox(
        "acct1", "Sent2", _one_message_mbox(tmp_path / "s.mbox", "s@example.test"), ["sent"]
    )
    plain = fb.add_mbox("acct1", "Plain", _one_message_mbox(tmp_path / "p.mbox", "p@example.test"))
    folders = {
        f.folder_id: f
        for f in (await fb.request("mail.folders.list", FoldersListParams(account_id="acct1"))).root
    }
    assert folders[folder_id].special_use == ["sent"] and folders[folder_id].total == 1
    assert folders[plain].special_use == []
    with pytest.raises(ValueError):
        fb.add_mbox("acct1", "Plain", tmp_path / "p.mbox")


@pytest.mark.asyncio
async def test_set_headers_only_shows_in_page_items(fb: FakeBridge) -> None:
    tb_id = fb.tb_ids("acct1:/Invoices")[0]
    fb.set_headers_only(tb_id, True)
    items = {i.tb_id: i for i in await page_all(fb, "acct1:/Invoices")}
    assert items[tb_id].headers_only is True
    assert sum(i.headers_only for i in items.values()) == 1
    fb.set_headers_only(tb_id, False)
    assert not any(i.headers_only for i in await page_all(fb, "acct1:/Invoices"))


@pytest.mark.asyncio
async def test_set_date_moves_the_page_item_date_only(fb: FakeBridge) -> None:
    tb_id = fb.tb_ids("acct1:/Invoices")[0]
    raw = (await fb.request("mail.messages.raw", TbIdParams(tb_id=tb_id))).raw_b64
    before = {i.tb_id: i.date for i in await page_all(fb, "acct1:/Invoices")}
    when = before[tb_id] - timedelta(days=90)
    fb.set_date(tb_id, when)
    after = {i.tb_id: i.date for i in await page_all(fb, "acct1:/Invoices")}
    assert after == {**before, tb_id: when}
    assert (await fb.request("mail.messages.raw", TbIdParams(tb_id=tb_id))).raw_b64 == raw
    with pytest.raises(ValueError, match="UTC offset"):
        fb.set_date(tb_id, when.replace(tzinfo=None))


@pytest.mark.asyncio
async def test_rename_folder_moves_messages_to_the_new_id(fb: FakeBridge) -> None:
    old_ids = fb.tb_ids("acct2:/Projects")
    fb.page_size = 3
    page: Page = await fb.request("mail.messages.page", PageParams(folder_id="acct2:/Projects"))
    assert page.next_cursor is not None
    new_id = fb.rename_folder("acct2:/Projects", "Projects-renamed")
    assert new_id == "acct2:/Projects-renamed"
    folders = (await fb.request("mail.folders.list", FoldersListParams(account_id="acct2"))).root
    assert [f.folder_id for f in folders] == [
        "acct2:/INBOX",
        new_id,
        "acct2:/SENTINEL-FOLDER-9fb649",
    ]
    assert {f.folder_id: f.total for f in folders}[new_id] == len(old_ids) == 8
    assert fb.tb_ids("acct2:/Projects") == []
    new_ids = fb.tb_ids(new_id)
    assert len(new_ids) == 8 and not set(new_ids) & set(old_ids)
    assert await stale(fb.request("mail.messages.raw", TbIdParams(tb_id=old_ids[0]))) == "stale_id"
    assert (
        await stale(
            fb.request("mail.messages.page", PageParams(folder_id=new_id, cursor=page.next_cursor))
        )
        == "stale_id"
    )
    with pytest.raises(BridgeError):
        await fb.request("mail.messages.page", PageParams(folder_id="acct2:/Projects"))
    with pytest.raises(ValueError):
        fb.rename_folder(new_id, "INBOX")


# -- write requests (plan 06b step 3) -----------------------------------------------------------

DUP_HMID = {m["key"]: m for m in MANIFEST["messages"]}["dup-1"]["message_id"].strip("<>")


async def found(fb: FakeBridge, hmid: str = DUP_HMID) -> dict[str, Any]:
    """folder_id → (tb_id, flags, tags) of every live location of ``hmid``."""
    got = (await fb.request("mail.messages.find", FindParams(header_message_id=hmid))).root
    return {f.folder_id: (f.tb_id, f.flags, sorted(f.tags)) for f in got}


@pytest.mark.asyncio
async def test_update_returns_prev_state_and_emits_updated_per_folder(
    fb: FakeBridge, events: list[tuple[str, dict[str, Any]]]
) -> None:
    from tern.bridge.protocol import UpdateParams

    before = await found(fb)
    ids = [tb_id for tb_id, _, _ in before.values()]
    res = await fb.request(
        "mail.update", UpdateParams(tb_ids=ids, add_tags=["$label1"], flagged=True, read=True)
    )
    assert [(p.tb_id, p.tags, p.flagged, p.read) for p in res.prev_state] == [
        (t, before_tags, flags.flagged, flags.read) for t, flags, before_tags in before.values()
    ]
    after = await found(fb)
    assert all(
        tags == ["$label1"] and flags.flagged and flags.read for _, flags, tags in after.values()
    )
    assert [m for m, _ in events] == ["event.mail.updated", "event.mail.updated"]
    assert sorted(p["items"][0]["folder_id"] for _, p in events) == sorted(before)
    assert all(p["items"][0]["tags"] == ["$label1"] for _, p in events)

    res = await fb.request("mail.update", UpdateParams(tb_ids=ids[:1], remove_tags=["$label1"]))
    assert res.prev_state[0].tags == ["$label1"]
    assert (await found(fb))[next(iter(before))][2] == []


@pytest.mark.asyncio
async def test_update_with_a_stale_id_changes_nothing(
    fb: FakeBridge, events: list[tuple[str, dict[str, Any]]]
) -> None:
    from tern.bridge.protocol import UpdateParams

    before = await found(fb)
    (good, _, _), _ = before.values()
    old = fb.tb_ids("acct1:/INBOX")[0]
    await fb.move([old], "acct1:/Archive")
    events.clear()
    with pytest.raises(BridgeError) as exc_info:
        await fb.request("mail.update", UpdateParams(tb_ids=[good, old], add_tags=["$label2"]))
    assert exc_info.value.name == "stale_id"
    assert await found(fb) == before and events == []


@pytest.mark.asyncio
async def test_accept_without_applying_changes_nothing(
    fb: FakeBridge, events: list[tuple[str, dict[str, Any]]]
) -> None:
    from tern.bridge.protocol import (
        ArchiveParams,
        DeleteParams,
        MoveParams,
        TagCreateParams,
        UpdateParams,
    )

    fb.apply_writes = False
    before = await found(fb)
    ids = [tb_id for tb_id, _, _ in before.values()]
    folders = {
        f.folder_id
        for f in (await fb.request("mail.folders.list", FoldersListParams(account_id="acct2"))).root
    }
    res = await fb.request("mail.update", UpdateParams(tb_ids=ids, add_tags=["$label1"]))
    assert len(res.prev_state) == 2
    moved = await fb.request("mail.move", MoveParams(tb_ids=ids[:1], dest_folder_id="acct2:/INBOX"))
    assert moved.moved[0].new_tb_id is None
    archived = await fb.request("mail.archive", ArchiveParams(tb_ids=ids[:1]))
    assert archived.moved[0].new_tb_id is None
    assert (await fb.request("mail.delete", DeleteParams(tb_ids=ids))).trashed == ids
    await fb.request("mail.tags.create", TagCreateParams(tag="Synthetic"))
    assert await found(fb) == before and events == []
    assert {
        f.folder_id
        for f in (await fb.request("mail.folders.list", FoldersListParams(account_id="acct2"))).root
    } == folders
    assert [t.tag for t in (await fb.request("mail.tags.list", Empty())).root] == [
        "Important",
        "Work",
        "Personal",
        "To Do",
        "Later",
    ]


@pytest.mark.asyncio
async def test_move_request_returns_new_ids_in_request_order(
    fb: FakeBridge, events: list[tuple[str, dict[str, Any]]]
) -> None:
    from tern.bridge.protocol import MoveParams

    a = fb.tb_ids("acct1:/Invoices")[:1]
    b = fb.tb_ids("acct1:/INBOX")[:2]
    res = await fb.request("mail.move", MoveParams(tb_ids=a + b, dest_folder_id="acct1:/Archive"))
    assert [m.tb_id for m in res.moved] == a + b
    new = [m.new_tb_id for m in res.moved]
    assert all(n is not None for n in new) and len(set(new)) == 3
    assert set(new) <= set(fb.tb_ids("acct1:/Archive"))
    assert [p["src_folder_id"] for _, p in events] == ["acct1:/Invoices", "acct1:/INBOX"]
    assert await stale(fb.request("mail.messages.raw", TbIdParams(tb_id=a[0]))) == "stale_id"
    with pytest.raises(BridgeError) as exc_info:
        await fb.request("mail.move", MoveParams(tb_ids=b, dest_folder_id="acct1:/Nope"))
    assert (exc_info.value.name, exc_info.value.tb_code) == ("tb_error", "folder_not_found")


@pytest.mark.asyncio
async def test_archive_goes_to_the_accounts_archives_folder(
    fb: FakeBridge, events: list[tuple[str, dict[str, Any]]]
) -> None:
    from tern.bridge.protocol import ArchiveParams

    tb_id = fb.tb_ids("acct1:/INBOX")[0]
    res = await fb.request("mail.archive", ArchiveParams(tb_ids=[tb_id]))
    assert res.moved[0].new_tb_id in fb.tb_ids("acct1:/Archive")  # specialUse "archives"
    assert events[0][1]["dst_folder_id"] == "acct1:/Archive"
    # An account without an archives folder gets one, as TB creates it on first use.
    other = fb.tb_ids("acct2:/Projects")[0]
    res = await fb.request("mail.archive", ArchiveParams(tb_ids=[other]))
    assert res.moved[0].new_tb_id in fb.tb_ids("acct2:/Archives")
    listed = (await fb.request("mail.folders.list", FoldersListParams(account_id="acct2"))).root
    assert [f.special_use for f in listed if f.folder_id == "acct2:/Archives"] == [["archives"]]


@pytest.mark.asyncio
async def test_delete_moves_to_the_accounts_trash_then_deletes_from_it(
    fb: FakeBridge, events: list[tuple[str, dict[str, Any]]]
) -> None:
    from tern.bridge.protocol import DeleteParams

    before = await found(fb)
    tb_id, _, _ = before["acct2:/Projects"]
    res = await fb.request("mail.delete", DeleteParams(tb_ids=[tb_id]))
    assert res.trashed == [tb_id]
    after = await found(fb)
    assert set(after) == {"acct2:/INBOX", "acct2:/Trash"}
    listed = (await fb.request("mail.folders.list", FoldersListParams(account_id="acct2"))).root
    assert [f.special_use for f in listed if f.folder_id == "acct2:/Trash"] == [["trash"]]
    assert [(m, p["src_folder_id"], p["dst_folder_id"]) for m, p in events] == [
        ("event.mail.moved", "acct2:/Projects", "acct2:/Trash")
    ]
    assert await stale(fb.request("mail.messages.raw", TbIdParams(tb_id=tb_id))) == "stale_id"
    # From Trash, a delete is permanent (TB): event.mail.deleted.
    trashed, _, _ = after["acct2:/Trash"]
    await fb.request("mail.delete", DeleteParams(tb_ids=[trashed]))
    assert set(await found(fb)) == {"acct2:/INBOX"}
    assert events[-1] == (
        "event.mail.deleted",
        {"folder_id": "acct2:/Trash", "header_message_ids": [DUP_HMID]},
    )


@pytest.mark.asyncio
async def test_tags_create_adds_a_listed_tag(fb: FakeBridge) -> None:
    from tern.bridge.protocol import TagCreateParams

    key = await fb.request("mail.tags.create", TagCreateParams(tag="Follow Up", color="#00FF00"))
    assert key.key == "follow_up"
    tags = (await fb.request("mail.tags.list", Empty())).root
    assert (tags[-1].key, tags[-1].tag, tags[-1].color) == ("follow_up", "Follow Up", "#00FF00")
    with pytest.raises(BridgeError) as exc_info:
        await fb.request("mail.tags.create", TagCreateParams(tag="follow up"))
    assert exc_info.value.name == "invalid_params"
    again = await fb.request("mail.tags.create", TagCreateParams(tag="Follow(Up"))
    assert again.key == "follow_up_2"


@pytest.mark.asyncio
async def test_first_tb_id_plants_distinctive_ids() -> None:
    fb = FakeBridge(FIXTURE, first_tb_id=731_904)
    assert min(fb.tb_ids("acct1:/INBOX")) == 731_904
    fb.restart()
    assert min(fb.tb_ids("acct1:/INBOX")) == 1_000_000 + 731_904
    with pytest.raises(ValueError, match="first_tb_id"):
        FakeBridge(FIXTURE, first_tb_id=0)
