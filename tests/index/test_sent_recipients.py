"""Plan 04 step 7: known recipients from Sent folders (B-M8, D3), counted once per mid."""

from __future__ import annotations

import mailbox
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm

from tern.config import TernConfig
from tern.index.ingest import Backfill
from tern.index.recipients import count_sent, known_recipients
from tern.index.source import IngestSource
from tern.index.store import Store
from tern.testing.fake_bridge import FakeBridge

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
MESSAGES = tfm.messages(MANIFEST)
# The fixture's Sent mail (6 messages) addresses these, by message count (MANIFEST
# sent_recipients lists the addresses; the counts follow from the Sent folder's To/Cc).
EXPECTED = {"jordan@example.com": 3, "maria.k@example.org": 2, "nikos@example.test": 2}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "data" / "index.db")
    yield s
    s.close()


def counts(store: Store) -> dict[str, int]:
    return {a: c for a, (c, _) in known_recipients(store).items()}


async def backfill(store: Store, fb: FakeBridge) -> None:
    await Backfill(store, IngestSource(fb), TernConfig()).run()


def write_mbox(path: Path, raws: list[bytes]) -> Path:
    box = mailbox.mbox(path)
    try:
        for raw in raws:
            box.add(raw)
    finally:
        box.close()
    return path


def test_fixture_sent_folder_addresses_the_manifest_recipients() -> None:
    assert sorted(EXPECTED) == sorted(MANIFEST["sent_recipients"])


@pytest.mark.asyncio
async def test_recipients_from_the_sent_folder(store: Store) -> None:
    await backfill(store, FakeBridge(FIXTURE))
    assert counts(store) == EXPECTED
    assert "nikos.papadopoulos@docs-transfer.test" not in known_recipients(store)
    # first_seen = the oldest date_utc of the counted Sent messages to that address
    sent_mids = [
        r[0]
        for r in store.connection.execute(
            "SELECT mid FROM locations WHERE folder_id = 'acct1:/Sent'"
        )
    ]
    for addr in EXPECTED:
        dates = []
        for mid in sent_mids:
            m = store.get_message(mid)
            assert m is not None
            if addr in (*m.to_addrs, *m.cc_addrs) and m.date_utc is not None:
                dates.append(m.date_utc)
        assert known_recipients(store)[addr][1] == min(dates)


@pytest.mark.asyncio
async def test_second_sent_folder_and_rerun_count_once(store: Store, tmp_path: Path) -> None:
    fb = FakeBridge(FIXTURE)
    lunch = tfm.read_copy(FIXTURE, MANIFEST, MESSAGES["sent-lunch"]["copies"][0])
    fb.add_mbox("acct3", "Sent", write_mbox(tmp_path / "s.mbox", [lunch]), ["sent"])
    await backfill(store, fb)
    await backfill(store, fb)
    assert counts(store) == EXPECTED
    assert len(store.locations(MESSAGES["sent-lunch"]["mid"])) == 2


@pytest.mark.asyncio
async def test_mixed_case_address_is_stored_lower_cased(store: Store, tmp_path: Path) -> None:
    fb = FakeBridge(FIXTURE)
    raw = (
        b"Message-ID: <mixed-case@example.test>\nDate: Fri, 13 Nov 2026 10:00:00 +0000\n"
        b"From: alex@example.test\nTo: Jordan Lee <Jordan.Lee@Example.COM>\n"
        b"Cc: JORDAN.LEE@example.com\nSubject: synthetic\n\nbody\n"
    )
    fb.add_mbox("acct2", "Sent", write_mbox(tmp_path / "s.mbox", [raw]), ["sent"])
    await backfill(store, fb)
    assert counts(store) == {**EXPECTED, "jordan.lee@example.com": 1}


def test_count_sent_is_idempotent_and_keeps_the_oldest_date(store: Store) -> None:
    a, b = "a" * 64, "b" * 64
    assert count_sent(store, a, ["X@Example.test", "x@example.test", " "], None) is True
    assert known_recipients(store) == {"x@example.test": (1, None)}
    assert count_sent(store, a, ["x@example.test"], 100) is False
    assert known_recipients(store) == {"x@example.test": (1, None)}
    assert count_sent(store, b, ["x@example.test", "y@example.test"], 200) is True
    assert known_recipients(store) == {"x@example.test": (2, 200), "y@example.test": (1, 200)}
    assert count_sent(store, "c" * 64, ["x@example.test"], 150) is True
    assert count_sent(store, "d" * 64, ["x@example.test"], None) is True
    assert known_recipients(store)["x@example.test"] == (4, 150)
    assert count_sent(store, "e" * 64, ["x@example.test"], 300) is True
    assert known_recipients(store)["x@example.test"] == (5, 150)
