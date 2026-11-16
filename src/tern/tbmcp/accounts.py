"""Parse Thunderbird's ``prefs.js`` and link accounts, identities and servers.

Thunderbird's model, as stored in ``prefs.js``::

    mail.account.account1.server      = "server1"
    mail.account.account1.identities  = "id1,id3"
    mail.identity.id1.useremail       = "alice@example.com"
    mail.server.server1.hostname      = "imap.example.com"
    mail.server.server1.userName      = "alice"          # not always the email
    mail.server.server1.type          = "imap"

Folder URIs in the GLODA index carry the *server* username and hostname
(``imap://alice@imap.example.com/INBOX``), so mapping a folder back to an email
address means going server -> account -> identities.
"""

from __future__ import annotations

import re
import threading

from tern.tbmcp import config

_PAT_PREF = re.compile(
    r'user_pref\(\s*"mail\.(identity|server|account)\.([A-Za-z0-9_]+)\.([A-Za-z]+)"\s*,'
    r'\s*"((?:[^"\\]|\\.)*)"\s*\)'
)

_EMPTY: dict = {"emails": [], "accounts": [], "by_id": {}, "servers": {}}


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def parse_prefs(text: str) -> dict:
    """Extract account info from the contents of a ``prefs.js`` file.

    Returns a dict with:
        emails:   sorted list of configured email addresses
        accounts: [{account, type, hostname, username, emails}] one per account
                  that has a server, emails being that account's identities
        by_id:    {identity_id: {"email": ...}}
        servers:  {server_id: {"hostname", "username", "type"}} (keys present
                  only when set in prefs.js)
    """
    identities: dict[str, dict[str, str]] = {}
    servers: dict[str, dict[str, str]] = {}
    accts: dict[str, dict[str, str]] = {}
    tables = {"identity": identities, "server": servers, "account": accts}
    for kind, key, field, value in _PAT_PREF.findall(text):
        tables[kind].setdefault(key, {})[field] = _unescape(value)

    by_id = {
        ident: {"email": f["useremail"]} for ident, f in identities.items() if f.get("useremail")
    }
    server_info = {
        srv: {
            k_out: f[k_in]
            for k_in, k_out in (
                ("hostname", "hostname"),
                ("userName", "username"),
                ("type", "type"),
            )
            if k_in in f
        }
        for srv, f in servers.items()
    }
    server_info = {k: v for k, v in server_info.items() if v}

    accounts: list[dict] = []
    for acct, f in sorted(accts.items()):
        srv = server_info.get(f.get("server", ""))
        if not srv:
            continue
        id_keys = [i.strip() for i in f.get("identities", "").split(",") if i.strip()]
        accounts.append(
            {
                "account": acct,
                "type": srv.get("type"),
                "hostname": srv.get("hostname"),
                "username": srv.get("username"),
                "emails": [by_id[i]["email"] for i in id_keys if i in by_id],
            }
        )

    emails = sorted({v["email"] for v in by_id.values()})
    return {"emails": emails, "accounts": accounts, "by_id": by_id, "servers": server_info}


_PAT_CAL = re.compile(
    r'user_pref\(\s*"calendar\.registry\.([^."]+)\.([A-Za-z0-9_.]+)"\s*,'
    r'\s*("(?:[^"\\]|\\.)*"|true|false|-?\d+)\s*\)'
)


def parse_calendar_registry(text: str) -> dict[str, dict]:
    """Calendars declared in ``prefs.js``: ``{calendar_id: {name, type, uri, ...}}``.

    ``calendar_id`` is the ``cal_id`` used in the calendar databases.
    """
    raw: dict[str, dict] = {}
    for cal_id, field, value in _PAT_CAL.findall(text):
        if value.startswith('"'):
            parsed: object = _unescape(value[1:-1])
        elif value in ("true", "false"):
            parsed = value == "true"
        else:
            parsed = int(value)
        raw.setdefault(cal_id, {})[field] = parsed
    return {
        cal_id: {
            "name": f.get("name", cal_id),
            "type": f.get("type"),
            "uri": f.get("uri"),
            "disabled": bool(f.get("disabled", False)),
            "read_only": bool(f.get("readOnly", False)),
            "cached": bool(f.get("cache.enabled", False)),
        }
        for cal_id, f in raw.items()
        if "type" in f or "name" in f
    }


_cache_lock = threading.Lock()
_NO_PREFS: dict = {"accounts": _EMPTY, "calendars": {}, "timezone": None}
_cache: tuple[tuple[str, float] | None, dict] = (None, _NO_PREFS)
_PAT_TZ = re.compile(r'user_pref\(\s*"calendar\.timezone\.local"\s*,\s*"([^"]+)"\s*\)')


def _load_prefs() -> dict:
    """Parse the active profile's ``prefs.js``, re-reading it when it changes."""
    global _cache
    try:
        prefs = config.prefs_path()
        stamp = (str(prefs), prefs.stat().st_mtime)
    except (FileNotFoundError, OSError):
        return _NO_PREFS
    with _cache_lock:
        if _cache[0] != stamp:
            text = prefs.read_text(encoding="utf-8", errors="ignore")
            _cache = (
                stamp,
                {
                    "accounts": parse_prefs(text),
                    "calendars": parse_calendar_registry(text),
                    "timezone": (m.group(1) if (m := _PAT_TZ.search(text)) else None),
                },
            )
        return _cache[1]


def load_accounts() -> dict:
    """Mail accounts from ``prefs.js`` (empty when there is no profile)."""
    return _load_prefs()["accounts"]


def load_calendars() -> dict[str, dict]:
    """Calendar registry from ``prefs.js`` (empty when there is no profile)."""
    return _load_prefs()["calendars"]


def local_timezone() -> str | None:
    """Thunderbird's calendar timezone (``calendar.timezone.local``), if set."""
    return _load_prefs()["timezone"]


def my_addresses() -> set[str]:
    """Every address that means "me": identity emails and email-like usernames."""
    acc = load_accounts()
    out = {e.lower() for e in acc["emails"]}
    out |= {
        a["username"].lower() for a in acc["accounts"] if a.get("username") and "@" in a["username"]
    }
    return out


def list_emails() -> list[str]:
    return load_accounts()["emails"]


def server_emails() -> dict[tuple[str, str], list[str]]:
    """``{(username, hostname): [emails]}``, lower-cased, for folder-URI lookup."""
    out: dict[tuple[str, str], list[str]] = {}
    for a in load_accounts()["accounts"]:
        if a.get("username") is None or a.get("hostname") is None:
            continue
        out[(a["username"].lower(), a["hostname"].lower())] = a["emails"]
    return out
