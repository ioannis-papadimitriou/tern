"""Read-only lookups in Thunderbird's address books.

``abook.sqlite`` is the personal address book and ``history.sqlite`` holds
addresses Thunderbird collected from mail you sent. Both use:

    properties(card, name, value)   # PrimaryEmail, SecondEmail, DisplayName,
                                    # FirstName, LastName, _vCard, ...
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from typing import Any

from tern.tbmcp import guard, snapshot

_BOOK_NAMES = {
    "contacts-abook.sqlite": "Personal Address Book",
    "contacts-history.sqlite": "Collected Addresses",
}


def _unescape(v: str) -> str:
    return re.sub(r"\\([,;\\nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def parse_vcard(text: str) -> dict[str, list[str]]:
    """Minimal vCard reader: ``{PROPERTY: [values]}`` (unfolded, unescaped)."""
    out: dict[str, list[str]] = {}
    text = re.sub(r"\r?\n[ \t]", "", text or "")
    for line in text.splitlines():
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        name = head.split(";", 1)[0].split(".")[-1].upper()  # drop "item1." groups
        if name in ("BEGIN", "END", "VERSION", "PRODID", "UID", "REV", "PHOTO"):
            continue
        out.setdefault(name, []).append(_unescape(value).strip())
    return out


def lookup(email: str) -> list[dict[str, Any]]:
    """Address-book cards with this email (primary or secondary)."""
    snapshot.ensure_contacts_fresh()
    want = email.strip().lower()
    cards: list[dict[str, Any]] = []
    for path in snapshot.contacts_snapshot_paths():
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
            ids = [
                r[0]
                for r in con.execute(
                    "SELECT DISTINCT card FROM properties WHERE name IN"
                    " ('PrimaryEmail', 'SecondEmail') AND lower(value) = ?",
                    (want,),
                )
            ]
            for card in ids:
                props = dict(
                    con.execute(
                        "SELECT name, value FROM properties WHERE card = ?", (card,)
                    ).fetchall()
                )
                v = parse_vcard(props.get("_vCard", ""))
                org = (v.get("ORG") or [""])[0].replace(";", ", ").strip(", ")
                item = {
                    "book": _BOOK_NAMES.get(path.name, path.stem),
                    "name": props.get("DisplayName")
                    or (v.get("FN") or [None])[0]
                    or " ".join(x for x in (props.get("FirstName"), props.get("LastName")) if x)
                    or None,
                    "emails": sorted(
                        {e.lower() for e in v.get("EMAIL", [])}
                        | {
                            props[k].lower()
                            for k in ("PrimaryEmail", "SecondEmail")
                            if props.get(k)
                        }
                    ),
                    "organization": org or None,
                    "title": (v.get("TITLE") or [None])[0],
                    "phones": v.get("TEL", []),
                    "note": "\n".join(v.get("NOTE", [])) or None,
                }
                guard.protect(item, ("name", "organization", "title", "note"))
                cards.append(item)
    return cards
