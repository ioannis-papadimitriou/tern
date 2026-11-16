"""Query layer tested against a synthetic GLODA-shaped snapshot."""

import pytest

from tern.tbmcp import db

from .synthetic import got

pytestmark = pytest.mark.usefixtures("fake_snapshot")


def _s(**kw):
    return db.search(**kw)["messages"]


def _r(*a, **kw):
    return db.recent_by_address(*a, **kw)["messages"]


def test_stats_excludes_deleted():
    s = db.stats()
    assert s["messages"] == 3
    assert s["oldest"].startswith("2023-11")
    assert s["newest"].startswith("2024-01")


def test_search_query_matches_body_and_subject():
    hits = _s(query="shipment")
    assert {h["message_id"] for h in hits} == {1, 2}
    # newest first
    assert hits[0]["message_id"] == 2


def test_search_excludes_deleted():
    assert all(h["message_id"] != 3 for h in _s(query="deleted"))


def test_search_by_from_addr():
    hits = _s(from_addr="bob@vendor.test")
    assert [h["message_id"] for h in hits] == [1]


def test_search_since_filter():
    hits = _s(since="2024-01-01")
    assert [h["message_id"] for h in hits] == [4, 2]


def test_account_inferred_from_uri():
    assert _s(query="shipment")[0]["account"] == "alice@example.com"


def test_recent_by_address_both_directions():
    hits = _r("bob@vendor.test")
    assert {h["message_id"] for h in hits} == {1, 2}


def test_thread_is_chronological():
    msgs = db.thread(100)
    assert [m["message_id"] for m in msgs] == [1, 2]


def test_get_message_full_body():
    msg = db.get_message(1)
    assert msg is not None
    assert msg["subject"] == "Shipment quote"
    assert msg["body_preview"] == "Quote for the shipment"
    assert db.get_message(999) is None


def test_folders_counts_non_deleted():
    folders = db.folders()
    assert folders[0]["name"] == "Inbox"
    assert folders[0]["account"] == "alice@example.com"
    assert folders[0]["count"] == 2


# --- regressions for the review fixes -----------------------------------------


def test_until_date_only_includes_that_whole_day():
    # messages 2 and 4 are on 2024-01-11 (19:06Z, 20:06Z); a bare date includes both
    assert [h["message_id"] for h in _s(until="2024-01-11")] == [4, 2, 1]
    assert [h["message_id"] for h in _s(until="2024-01-10")] == [1]


def test_until_full_timestamp_is_exact():
    assert [h["message_id"] for h in _s(until="2024-01-11T00:00:00Z")] == [1]


def test_limit_is_clamped():
    assert len(_s(limit=-1)) == 1  # SQLite would treat -1 as "no limit"
    assert len(_s(limit=0)) == 1
    assert len(_s(limit=10**9)) == 3


def test_like_wildcards_are_literal():
    assert _s(from_addr="bob_vendor") == []
    assert [h["message_id"] for h in _s(query="Status_report")] == [4]
    assert [h["message_id"] for h in _s(query="100%")] == [4]
    assert [h["message_id"] for h in _s(query="%")] == [4]  # only literal %


def test_account_filter_by_identity_email_when_username_differs():
    # server username is "bob", but prefs.js links the identity bob@work.example.org
    assert [h["message_id"] for h in _s(account="bob@work.example.org")] == [4]
    assert [h["message_id"] for h in _s(account="BOB")] == [4]
    assert _s(account="nobody@nowhere.test") == []


def test_account_filter_is_exact_not_substring():
    assert _s(account="lice@example.com") == []
    assert {h["message_id"] for h in _s(account="alice@example.com")} == {1, 2}


def test_account_reported_as_identity_email():
    assert got(db.get_message(4))["account"] == "bob@work.example.org"


def test_get_message_hides_deleted():
    assert db.get_message(3) is None


def test_body_truncation_flag_and_clamp():
    msg = db.get_message(4, body_chars=10)
    assert msg is not None
    assert len(msg["body_preview"]) == 10
    assert msg["truncated"] is True
    assert msg["body_length"] == 1600
    assert got(db.get_message(4, body_chars=-5))["body_preview"] == ""
    assert got(db.get_message(1))["truncated"] is False


def test_search_preview_is_capped():
    hit = _s(query="Status_report")[0]
    assert len(hit["body_preview"]) == db.PREVIEW_CHARS
