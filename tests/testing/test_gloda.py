"""``tern.testing.gloda``: a synthetic Thunderbird profile built from the same fixture (and the
same planted read/flagged state) as a tern index, read by the vendored thunderbird-mcp code.

The index and the profile are built once per module (``smoke``'s real ingest over ``FakeBridge``);
each test points the vendored modules at the profile and restores their settings afterwards, and
profile discovery has no roots, so nothing but the synthetic profile is ever read.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tern.bridge.protocol import AccountList, Empty, FolderList, FoldersListParams, Page, PageParams
from tern.config import TernConfig
from tern.index.mid import TB_MD5_PREFIX, mid_of
from tern.index.store import Store, index_path
from tern.tbmcp import accounts, db, guard, snapshot
from tern.tbmcp import config as tbconfig
from tern.testing.fake_bridge import FakeBridge
from tern.testing.gloda import GLODA_DB, PREFS_JS, GlodaBuildError, GlodaProfile, build_profile
from tern.testing.smoke import build_dataset, smoke_config
from tern.tools.builtin.tb import configure

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_mailbox"

UNREAD_INBOX = {
    "msg-010-1.0c5400a0@example.net",  # acct1/INBOX "Piano lessons"
    "msg-037-1.7e4904bb@example.org",  # acct2/INBOX "Weekend hike"
    "msg-055-1.d86528ea@example.test",  # acct3/INBOX "Επισκευή αυτοκινήτου"
}
UNREAD_ELSEWHERE = {"news-weekly-14.69ee8870@weekly.example.net"}  # acct1/Newsletters
FLAGGED = {"inv-0042@acme.example.com"}


async def _plant(bridge: FakeBridge, unread: set[str], flagged: set[str]) -> None:
    """Every copy read and unflagged, then ``unread`` / ``flagged`` (by headerMessageId)."""
    accts: AccountList = await bridge.request("mail.accounts.list", Empty())
    for account in accts.root:
        folders: FolderList = await bridge.request(
            "mail.folders.list", FoldersListParams(account_id=account.account_id)
        )
        for folder in folders.root:
            cursor: str | None = None
            while True:
                page: Page = await bridge.request(
                    "mail.messages.page", PageParams(folder_id=folder.folder_id, cursor=cursor)
                )
                for item in page.items:
                    hmid = item.header_message_id
                    await bridge.update(
                        item.tb_id, read=hmid not in unread, flagged=hmid in flagged
                    )
                cursor = page.next_cursor
                if cursor is None:
                    break


@dataclass(frozen=True)
class Built:
    profile: GlodaProfile
    index: Path


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Built:
    home = tmp_path_factory.mktemp("gloda-home")
    bridge = FakeBridge(FIXTURE)
    asyncio.run(_plant(bridge, UNREAD_INBOX | UNREAD_ELSEWHERE, FLAGGED))
    cfg = smoke_config(FIXTURE, home)
    build_dataset(cfg, bridge=bridge)
    store = Store.open(index_path(cfg.paths), readonly=True)
    try:
        profile = asyncio.run(build_profile(bridge, store, home / "profile"))
    finally:
        store.close()
    return Built(profile, index_path(cfg.paths))


@pytest.fixture
def tb(built: Built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The vendored modules read ``built``'s profile; their settings are restored afterwards."""
    monkeypatch.setattr(guard, "_settings", guard._settings)
    monkeypatch.setattr(tbconfig, "_override", tbconfig._override)
    monkeypatch.setattr(tbconfig, "_candidate_roots", lambda: [])
    monkeypatch.setattr(snapshot, "CACHE_DIR", snapshot.CACHE_DIR)
    monkeypatch.setattr(snapshot, "SNAPSHOT", snapshot.SNAPSHOT)
    monkeypatch.setattr(accounts, "_cache", accounts._cache)
    tbconfig.profile_dir.cache_clear()
    configure(TernConfig(tb_profile_dir=built.profile.path, tb_timezone="UTC"), tmp_path)
    yield
    tbconfig.profile_dir.cache_clear()


def _headers(result: dict[str, list[dict[str, object]]]) -> set[str]:
    return {str(m["header_message_id"]) for m in result["messages"]}


def _index_mids(index: Path) -> dict[str, bool]:
    store = Store.open(index, readonly=True)
    try:
        rows = store.connection.execute("SELECT mid, conflicted FROM messages").fetchall()
    finally:
        store.close()
    return {str(mid): bool(conflicted) for mid, conflicted in rows}


def test_unread_search_returns_exactly_the_planted_unread_messages(built: Built, tb: None) -> None:
    assert _headers(db.search(unread=True, limit=100)) == UNREAD_INBOX | UNREAD_ELSEWHERE
    assert _headers(db.search(unread=True, folder="Inbox", limit=100)) == UNREAD_INBOX
    assert _headers(db.search(starred=True, limit=100)) == FLAGGED


def test_every_header_maps_to_a_mid_in_the_tern_index(built: Built) -> None:
    indexed = _index_mids(built.index)
    msgs = built.profile.messages
    assert len(msgs) == 187  # one GLODA row per copy (MANIFEST message_count)
    assert {m.mid for m in msgs} == set(indexed)  # every indexed message has a row, and back
    for m in msgs:
        if m.header_message_id.startswith(TB_MD5_PREFIX):
            assert m.mid.startswith("x-")  # no Message-ID: tern's x- mid, from the raw bytes
        else:
            assert mid_of(m.header_message_id) == m.mid
    # The planted adversarial pair: two rows, one Message-ID, conflicted in the index.
    pair = [m for m in msgs if m.header_message_id == "inv-0077@acme.example.com"]
    assert len(pair) == 2 and indexed[pair[0].mid]


def test_tb_search_dedups_the_conflicted_pair_to_its_lowest_row(built: Built, tb: None) -> None:
    hits = db.search(query="INV-0077", limit=100)["messages"]
    assert len(hits) == 1
    rows = sorted(
        (m for m in built.profile.messages if m.header_message_id == "inv-0077@acme.example.com"),
        key=lambda m: m.id,
    )
    assert hits[0]["message_id"] == rows[0].id
    assert hits[0]["folder"] == "Inbox"  # acct1/INBOX is swept before acct1/Invoices
    assert "acme-payments.example.net" in str(hits[0]["author"])


def test_accounts_identities_and_folders_follow_the_fixture(built: Built, tb: None) -> None:
    by_id = {a.account_id: a for a in built.profile.accounts}
    assert by_id["acct1"].identities == ("alex@example.com",)
    assert by_id["acct2"].identities == ("a.morgan@studio.example.net",)  # most frequent To
    assert by_id["acct3"].identities == ("alexmorgan@example.org",)
    assert accounts.my_addresses() == set(built.profile.my_addresses)
    folders = {(f["account"], f["name"]): f["count"] for f in db.folders()}
    assert folders[("alex@example.com", "Inbox")] == 47
    assert folders[("alex@example.com", "Sent")] == 6
    assert folders[("alexmorgan@example.org", "Offline")] == 2
    assert (
        built.profile.folders[-2].uri == "mailbox://alexmorgan%40example.org@pop.example.org/INBOX"
    )


def test_flags_and_conversations(built: Built, tb: None) -> None:
    thread = {
        str(m["header_message_id"]): m for m in db.search(query="INV-0042", limit=100)["messages"]
    }
    invoice = thread["inv-0042@acme.example.com"]
    reminder = thread["inv-0042-reminder@acme.example.com"]
    assert invoice["conversation_id"] == reminder["conversation_id"]
    assert invoice["starred"] is True and invoice["attachments"] == ["INV-0042.pdf"]
    assert invoice["to_me"] is True and invoice["from_me"] is False
    # folder is a name substring ("Sent" also matches "SENTINEL-FOLDER-..."), as upstream's.
    sent = [m for m in db.search(folder="Sent", limit=100)["messages"] if m["folder"] == "Sent"]
    assert len(sent) == 6 and all(m["from_me"] for m in sent)
    news = db.search(folder="Newsletters", limit=1)["messages"]
    assert news[0]["mailing_list"] is True
    # The fixture's user never replies (every Sent message starts a thread): nothing is replied.
    assert not any(m.replied for m in built.profile.messages)


REPLY = b"""From alex@example.com Mon Apr 13 09:00:00 2026
Message-ID: <gloda-reply-test@example.com>
From: Alex Morgan <alex@example.com>
To: Dana Whitfield <dana@acme.example.com>
Subject: Re: Acme invoice INV-0042 (March 2026)
Date: Mon, 13 Apr 2026 09:00:00 +0000
In-Reply-To: <inv-0042-reminder@acme.example.com>
Content-Type: text/plain; charset=utf-8

Paid today.
"""


def test_a_message_the_user_answered_is_replied(tmp_path: Path) -> None:
    mbox = tmp_path / "reply.mbox"
    mbox.write_bytes(REPLY)
    bridge = FakeBridge(FIXTURE)
    bridge.add_mbox("acct1", "Sent Items", mbox, special_use=["sent"])
    cfg = smoke_config(FIXTURE, tmp_path / "home")
    build_dataset(cfg, bridge=bridge)
    store = Store.open(index_path(cfg.paths), readonly=True)
    try:
        profile = asyncio.run(build_profile(bridge, store, tmp_path / "profile"))
    finally:
        store.close()
    replied = {m.header_message_id for m in profile.messages if m.replied}
    assert replied == {"inv-0042-reminder@acme.example.com"}


def test_the_files_are_a_gloda_database_and_prefs(built: Built) -> None:
    assert (built.profile.path / PREFS_JS).read_text("utf-8").count("useremail") == 3
    con = sqlite3.connect(built.profile.path / GLODA_DB)
    try:
        dates = [r[0] for r in con.execute("SELECT date FROM messages")]
    finally:
        con.close()
    assert min(dates) > 1_700_000_000_000_000  # microseconds since the epoch


def test_a_copy_missing_from_the_index_is_an_error(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "empty.db")
    try:
        with pytest.raises(GlodaBuildError, match="not in the tern index"):
            asyncio.run(build_profile(FakeBridge(FIXTURE), store, tmp_path / "profile"))
    finally:
        store.close()
    assert not (tmp_path / "profile" / GLODA_DB).exists()


def test_an_existing_database_is_refused(built: Built) -> None:
    store = Store.open(built.index, readonly=True)
    try:
        with pytest.raises(GlodaBuildError, match="exists already"):
            asyncio.run(build_profile(FakeBridge(FIXTURE), store, built.profile.path))
    finally:
        store.close()
