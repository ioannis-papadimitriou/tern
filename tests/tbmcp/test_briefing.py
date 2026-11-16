"""Composite tools (meeting prep, daily brief, contact) over mail + calendar + contacts."""

import sqlite3
from datetime import datetime

import pytest

from tern.tbmcp import accounts, briefing, calendar_db, db, snapshot

from . import test_calendar as cal
from .synthetic import attrs


def _us(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1_000_000)


MAIL = [
    (
        20,
        1,
        2000,
        _us("2025-03-05T09:00:00+00:00"),
        "<j1@x.test>",
        0,
        attrs(to_me=True),
        "Numbers for the budget review attached.\n-- \nJane",
        "Budget figures",
        "q1.xlsx",
        '"Doe, Jane" <jane@example.com>',
        "alice@example.com",
    ),
    (
        21,
        6,
        2000,
        _us("2025-03-06T09:00:00+00:00"),
        "<j2@x.test>",
        0,
        attrs(from_me=True),
        "Thanks Jane, looks good.",
        "Re: Budget figures",
        "",
        "alice@example.com",
        "jane@example.com",
    ),
    (
        22,
        1,
        2100,
        _us("2025-03-10T07:30:00+00:00"),
        "<j3@x.test>",
        0,
        attrs(to_me=True, read=False),
        "Running 5 min late to the Budget review",
        "Late",
        "",
        "jane@example.com",
        "alice@example.com",
    ),
    (
        23,
        1,
        2200,
        _us("2025-03-09T12:00:00+00:00"),
        "<n2@x.test>",
        0,
        attrs(to_me=True, mlist=True),
        "Sale!",
        "Deals",
        "",
        "news@shop.test",
        "alice@example.com",
    ),
]

VCARD = (
    "BEGIN:VCARD\r\nVERSION:4.0\r\nFN:Jane Doe\r\nORG:Acme Corp;Sales\r\n"
    "TITLE:Product Manager\r\nTEL;TYPE=work:+30 210 000 0000\r\n"
    "EMAIL:jane@example.com\r\nNOTE:Prefers\\, mornings\r\nEND:VCARD\r\n"
)

pytestmark = pytest.mark.parametrize("fake_snapshot", [MAIL], indirect=True)


@pytest.fixture(autouse=True)
def world(fake_snapshot, tmp_path, monkeypatch):
    local, cache = tmp_path / "cal-local.sqlite", tmp_path / "cal-cache.sqlite"
    cal._build(local, duplicate_only=True)
    cal._build(cache)
    monkeypatch.setattr(snapshot, "calendar_snapshot_paths", lambda: [local, cache])
    monkeypatch.setattr(snapshot, "ensure_calendar_fresh", lambda: None)
    monkeypatch.setattr(accounts, "load_calendars", lambda: cal.REGISTRY)

    abook = tmp_path / "contacts-abook.sqlite"
    con = sqlite3.connect(abook)
    con.execute("CREATE TABLE properties (card TEXT, name TEXT, value TEXT)")
    con.executemany(
        "INSERT INTO properties VALUES ('c1', ?, ?)",
        [("PrimaryEmail", "Jane@Example.com"), ("DisplayName", "Jane Doe"), ("_vCard", VCARD)],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(snapshot, "contacts_snapshot_paths", lambda: [abook])

    now = datetime.fromisoformat("2025-03-10T06:00:00+00:00")
    monkeypatch.setattr(briefing, "_now", lambda: now)
    monkeypatch.setattr(calendar_db, "_now_us", lambda: _us(now.isoformat()))
    monkeypatch.setattr(db, "_now_us", lambda: _us(now.isoformat()))


def test_meeting_prep():
    prep = briefing.meeting_prep("review")
    assert prep is not None
    assert prep["event"]["title"] == "Budget review"
    assert prep["next_occurrence"]["start"] == "2025-03-10T09:00:00+02:00"
    people = {p["email"]: p for p in prep["people"]}
    assert set(people) == {"boss@example.com", "jane@example.com", "bob@example.com"}
    jane = people["jane@example.com"]
    assert jane["status"] == "ACCEPTED"
    assert jane["organization"] == "Acme Corp, Sales"
    assert [m["message_id"] for m in jane["recent_mail"]] == [22, 21, 20]
    assert jane["recent_mail"][2]["preview"] == "Numbers for the budget review attached."
    assert jane["last_contact"].startswith("2025-03-10")
    assert people["boss@example.com"]["recent_mail"] == []
    assert {m["message_id"] for m in prep["related_mail"]} == {20, 22}


def test_meeting_prep_unknown_event():
    assert briefing.meeting_prep("nope") is None


def test_daily_brief():
    b = briefing.daily_brief("2025-03-10")
    assert [e["title"] for e in b["agenda"]] == ["Budget review", "Daily walk"]
    mail = b["mail_since_yesterday"]
    assert (mail["total"], mail["from_people"], mail["unread_from_people"]) == (2, 1, 1)
    (late,) = b["mail_from_todays_attendees"]
    assert (late["message_id"], late["meeting"]) == (22, "Budget review")
    # today (frozen "now"), so follow-ups are included: my reply to Jane went unanswered
    assert [w["conversation_id"] for w in b["awaiting_reply"]] == [2000]
    assert b["free_slots"] and b["conflicts"] == []
    assert briefing.daily_brief()["date"] == "2025-03-10"


def test_daily_brief_other_day_has_no_followups():
    b = briefing.daily_brief("2025-03-11")
    assert [e["title"] for e in b["agenda"]] == ["Dentist", "Daily walk"]
    assert b["awaiting_reply"] == [] and b["unanswered"] == []


def test_brief_mail_items_carry_their_header_message_id():
    """tern adds ``header_message_id`` to ``_mail_brief`` items (daily brief, meeting prep), so
    ``tern.tools.builtin.tb.with_mids`` can give each a citable mid."""
    b = briefing.daily_brief("2025-03-10")
    (late,) = b["mail_from_todays_attendees"]
    assert late["header_message_id"] == "<j3@x.test>"
    assert [m["header_message_id"] for m in b["mail_since_yesterday"]["top"]] == ["<j3@x.test>"]
    assert [w["header_message_id"] for w in b["awaiting_reply"]] == ["<j2@x.test>"]
    prep = briefing.meeting_prep("review")
    assert prep is not None
    jane = next(p for p in prep["people"] if p["email"] == "jane@example.com")
    assert [m["header_message_id"] for m in jane["recent_mail"]] == [
        "<j3@x.test>",
        "<j2@x.test>",
        "<j1@x.test>",
    ]


def test_contact_with_address_book_and_meetings(monkeypatch):
    c = briefing.contact("jane@example.com")
    assert (c["name"], c["messages"], c["received"], c["sent"]) == ("Doe, Jane", 3, 2, 1)
    (card,) = c["address_book"]
    assert (card["name"], card["organization"], card["title"]) == (
        "Jane Doe",
        "Acme Corp, Sales",
        "Product Manager",
    )
    assert card["phones"] == ["+30 210 000 0000"]
    assert card["note"] == "Prefers, mornings"
    assert [m["title"] for m in c["upcoming_meetings"]] == ["Budget review"]
