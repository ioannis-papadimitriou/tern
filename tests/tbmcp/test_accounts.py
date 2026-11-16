"""prefs.js parsing and account linkage."""

import os

from tern.tbmcp import accounts, config

SAMPLE_PREFS = r"""
user_pref("mail.account.account1.identities", "id1,id3");
user_pref("mail.account.account1.server", "server1");
user_pref("mail.account.account2.identities", "id2");
user_pref("mail.account.account2.server", "server2");
user_pref("mail.account.account3.server", "server3");
user_pref("mail.identity.id1.useremail", "alice@example.com");
user_pref("mail.identity.id2.useremail", "bob@work.example.org");
user_pref("mail.identity.id3.useremail", "alice.alias@example.com");
user_pref("mail.server.server1.hostname", "imap.example.com");
user_pref("mail.server.server1.userName", "alice@example.com");
user_pref("mail.server.server1.type", "imap");
user_pref("mail.server.server2.hostname", "mail.work.example.org");
user_pref("mail.server.server2.userName", "bob");
user_pref("mail.server.server2.type", "imap");
user_pref("mail.server.server3.hostname", "Local Folders");
user_pref("mail.server.server3.userName", "nobody");
user_pref("mail.server.server3.type", "none");
user_pref("mail.server.server3.name", "Say \"hi\"");
user_pref("unrelated.pref", "ignore me");
"""


def test_parse_prefs_extracts_emails():
    result = accounts.parse_prefs(SAMPLE_PREFS)
    assert result["emails"] == [
        "alice.alias@example.com",
        "alice@example.com",
        "bob@work.example.org",
    ]


def test_parse_prefs_servers():
    servers = accounts.parse_prefs(SAMPLE_PREFS)["servers"]
    assert servers["server1"] == {
        "hostname": "imap.example.com",
        "username": "alice@example.com",
        "type": "imap",
    }
    assert servers["server2"]["username"] == "bob"


def test_parse_prefs_links_accounts_to_identities():
    accts = {a["account"]: a for a in accounts.parse_prefs(SAMPLE_PREFS)["accounts"]}
    assert accts["account1"]["emails"] == ["alice@example.com", "alice.alias@example.com"]
    assert accts["account2"] == {
        "account": "account2",
        "type": "imap",
        "hostname": "mail.work.example.org",
        "username": "bob",
        "emails": ["bob@work.example.org"],
    }
    assert accts["account3"]["emails"] == []


def test_parse_prefs_empty():
    assert accounts.parse_prefs("") == {"emails": [], "accounts": [], "by_id": {}, "servers": {}}


def test_load_accounts_rereads_when_prefs_change(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs.js"
    prefs.write_text('user_pref("mail.identity.id1.useremail", "a@xcom.test");\n')
    monkeypatch.setattr(config, "prefs_path", lambda: prefs)
    assert accounts.list_emails() == ["a@xcom.test"]

    prefs.write_text('user_pref("mail.identity.id1.useremail", "b@xcom.test");\n')
    st = prefs.stat()
    os.utime(prefs, (st.st_atime, st.st_mtime + 5))
    assert accounts.list_emails() == ["b@xcom.test"]


def test_load_accounts_without_profile_is_empty(monkeypatch):
    def missing():
        raise FileNotFoundError("no profile")

    monkeypatch.setattr(config, "prefs_path", missing)
    assert accounts.load_accounts()["emails"] == []


CALENDAR_PREFS = r"""
user_pref("calendar.registry.0b4a-11ef.name", "Work \"main\"");
user_pref("calendar.registry.0b4a-11ef.type", "caldav");
user_pref("calendar.registry.0b4a-11ef.uri", "https://dav.example.com/cal/");
user_pref("calendar.registry.0b4a-11ef.cache.enabled", true);
user_pref("calendar.registry.0b4a-11ef.color", "#ff0000");
user_pref("calendar.registry.9c1d.name", "Old");
user_pref("calendar.registry.9c1d.type", "storage");
user_pref("calendar.registry.9c1d.disabled", true);
user_pref("calendar.registry.9c1d.readOnly", true);
user_pref("calendar.list.sortOrder", "0b4a-11ef 9c1d");
"""


def test_parse_calendar_registry():
    cals = accounts.parse_calendar_registry(CALENDAR_PREFS)
    assert cals == {
        "0b4a-11ef": {
            "name": 'Work "main"',
            "type": "caldav",
            "uri": "https://dav.example.com/cal/",
            "disabled": False,
            "read_only": False,
            "cached": True,
        },
        "9c1d": {
            "name": "Old",
            "type": "storage",
            "uri": None,
            "disabled": True,
            "read_only": True,
            "cached": False,
        },
    }
