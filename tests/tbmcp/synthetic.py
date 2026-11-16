"""A synthetic GLODA-shaped snapshot (ported from thunderbird-mcp 4921d1e's ``tests/conftest.py``).

Synthetic data only: reserved domains (``example.com``, ``.test``)."""

import json
import sqlite3

# Microseconds-since-epoch timestamps (GLODA's format).
_JAN = 1_700_000_000_000_000  # 2023-11-14T22:13:20Z
_FEB = 1_705_000_000_000_000  # 2024-01-11T19:06:40Z
_DAY = 86_400_000_000

ME = {"alice@example.com", "bob@work.example.org"}


def attrs(read=True, star=False, replied=False, from_me=False, to_me=False, mlist=False):
    a = {"43": 1, "57": [], "58": star, "59": read, "60": replied, "61": False}
    if from_me:
        a["54"] = [[1, 1]]
    if to_me:
        a["55"] = [[1, 1]]
    if mlist:
        a["56"] = ["list@lists.example.com"]
    return json.dumps(a)


# id, folder, conv, date, header-id, deleted, attrs, body, subject, attachments, author, rcpts
BASE = [
    (
        1,
        1,
        100,
        _JAN,
        "<m1@example.com>",
        0,
        attrs(to_me=True),
        "Quote for the shipment",
        "Shipment quote",
        "",
        "Bob Vendor <bob@vendor.test>",
        "alice@example.com",
    ),
    (
        2,
        1,
        100,
        _FEB,
        "<m2@example.com>",
        0,
        attrs(from_me=True),
        "Re: thanks, approved",
        "Re: Shipment quote",
        "po.pdf",
        "alice@example.com",
        "Bob Vendor <bob@vendor.test>",
    ),
    (
        3,
        1,
        200,
        _FEB,
        "<m3@example.com>",
        1,
        attrs(),
        "deleted body",
        "Deleted",
        "",
        "spam@bad.test",
        "alice@example.com",
    ),
    (
        4,
        2,
        300,
        _FEB + 3_600_000_000,
        "<m4@work.example.org>",
        0,
        attrs(read=False),
        "100% done, see x" * 100,
        "Status_report",
        "",
        "boss@work.example.org",
        "bob",
    ),
]


def build_snapshot(path, extra=()):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE folderLocations (id INTEGER PRIMARY KEY, folderURI TEXT, name TEXT);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, folderID INTEGER, conversationID INTEGER,
            date INTEGER, headerMessageID TEXT, deleted INTEGER, jsonAttributes TEXT
        );
        CREATE TABLE messagesText_content (
            docid INTEGER PRIMARY KEY, c0body TEXT, c1subject TEXT,
            c2attachmentNames TEXT, c3author TEXT, c4recipients TEXT
        );
        """)
    con.executemany(
        "INSERT INTO folderLocations VALUES (?, ?, ?)",
        [
            (1, "imap://alice%40example.com@imap.example.com/INBOX", "Inbox"),
            (2, "imap://bob@mail.work.example.org/INBOX", "Work"),
            (3, "imap://alice%40example.com@imap.example.com/Trash", "Trash"),
            (4, "imap://alice%40example.com@imap.example.com/Drafts", "Drafts"),
            (5, "imap://alice%40example.com@imap.example.com/[Gmail]/All Mail", "All Mail"),
            (6, "imap://alice%40example.com@imap.example.com/Sent", "Sent"),
        ],
    )
    for r in (*BASE, *extra):
        con.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", r[:7])
        con.execute("INSERT INTO messagesText_content VALUES (?, ?, ?, ?, ?, ?)", (r[0], *r[7:]))
    con.commit()
    con.close()


def got[T](value: T | None) -> T:
    """``value``, asserted present (an Optional result a test expects to exist)."""
    assert value is not None
    return value
