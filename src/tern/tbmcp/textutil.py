"""Text helpers: address extraction, quoted-reply stripping, automated-sender checks."""

from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def emails_in(text: str | None) -> list[str]:
    """Lower-cased email addresses in a header-like string, in order, de-duplicated."""
    seen: dict[str, None] = {}
    for e in _EMAIL.findall(text or ""):
        seen.setdefault(e.lower().strip("."), None)
    return list(seen)


def display_name(header: str | None) -> str | None:
    """``"Jane Doe" <jane@x.org>`` -> ``Jane Doe``."""
    if not header:
        return None
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', header)
    return m.group(1).strip() if m else None


# --- quoted replies -----------------------------------------------------------------

# Lines that start the quoted part of a reply, in a few common languages.
_REPLY_HEADERS = [
    r"^On .{4,200}(wrote|écrit|schrieb|escribió|scritto|έγραψε)\s*:?\s*$",
    r"^Am .{4,200}schrieb .{0,100}:?\s*$",
    r"^Le .{4,200}a écrit\s*:?\s*$",
    r"^El .{4,200}escribió\s*:?\s*$",
    r"^Il .{4,200}ha scritto\s*:?\s*$",
    r"^Στις .{4,200}έγραψε\s*:?\s*$",
    r"^-{2,}\s*Original Message\s*-{2,}\s*$",
    r"^-{2,}\s*(Forwarded|Weitergeleitete) (message|Nachricht)\s*-{2,}\s*$",
    r"^_{10,}\s*$",  # Outlook separator line
    r"^From:\s.+\n(Sent|Date):\s.+\n(To|Subject):",  # Outlook header block
]
_REPLY_RE = re.compile("|".join(f"(?:{p})" for p in _REPLY_HEADERS), re.MULTILINE | re.IGNORECASE)
_SIG_RE = re.compile(r"^-- ?$", re.MULTILINE)


def strip_quotes(text: str) -> tuple[str, int]:
    """Remove quoted earlier messages and the signature from a reply body.

    Cuts at the first reply header ("On … wrote:", "-----Original Message-----",
    an Outlook header block) or signature delimiter ("-- "), then drops any
    remaining lines quoted with ``>``. Returns the text and how many
    characters were removed. Forwarded content is kept when it is the whole
    message (nothing new was written above it).
    """
    if not text:
        return "", 0
    original = len(text)
    cut = len(text)
    m = _REPLY_RE.search(text)
    if m and text[: m.start()].strip():
        cut = m.start()
    s = _SIG_RE.search(text[:cut])
    if s and text[: s.start()].strip():
        cut = s.start()
    kept = [ln for ln in text[:cut].splitlines() if not ln.lstrip().startswith(">")]
    out = "\n".join(kept).strip()
    return out, original - len(out)


# --- automated senders --------------------------------------------------------------

_AUTOMATED_LOCAL = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notifications?|notify|alerts?|mailer-daemon|"
    r"postmaster|bounces?|news(letter)?|digest|updates?|marketing|info|support|billing|"
    r"receipts?|automated|system|calendar-notification|jira|github|gitlab)([+._-].*)?$",
    re.IGNORECASE,
)


def is_automated(author_header: str | None, mailing_list: bool = False) -> bool:
    """Heuristic: mailing lists and role/no-reply addresses aren't people."""
    if mailing_list:
        return True
    addrs = emails_in(author_header)
    return bool(addrs) and bool(_AUTOMATED_LOCAL.match(addrs[0].split("@", 1)[0]))
