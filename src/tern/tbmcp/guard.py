"""Guardrails enforced by the server, whatever the model or client does.

In tern, the settings come from tern's config (``tern.tools.builtin.tb`` maps ``tb_*`` keys of
``config.toml`` onto them and calls :func:`configure`); the environment is never read. Upstream
read the same names as ``THUNDERBIRD_MCP_<NAME>`` environment variables on every call:

ACCOUNTS           only these accounts (emails or server usernames)
EXCLUDE_ACCOUNTS   never these accounts
FOLDERS            only folders with these names
EXCLUDE_FOLDERS    never these folders; default: Junk, Spam, Trash,
                   Bin, Deleted Items, Deleted Messages ("" = none)
CALENDARS          only these calendars (names or ids)
EXCLUDE_CALENDARS  never these calendars
MAX_AGE_DAYS       hide mail and events older than N days
PRIVATE_EVENTS     show (default) | busy (title "Busy", no details) | hide
REDACT             on (default) | off — mask codes, reset links, cards, IBANs,
                   API keys and passwords in text sent to the model
MAX_OUTPUT_CHARS   cap on one tool result (default 100000)
TIMEZONE           your IANA timezone; default: Thunderbird's calendar
                   timezone, else the system's

Lists are comma-separated and case-insensitive. Upstream's ``AUDIT`` setting and audit log are
not vendored: tern's audit log (``Registry.call``) records every call.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

DEFAULT_EXCLUDED_FOLDERS = "Junk,Spam,Trash,Bin,Deleted Items,Deleted Messages"
DEFAULT_MAX_OUTPUT_CHARS = 100_000

#: The setting names :func:`configure` accepts (upstream: ``THUNDERBIRD_MCP_<NAME>``).
SETTING_NAMES = frozenset(
    {
        "ACCOUNTS",
        "EXCLUDE_ACCOUNTS",
        "FOLDERS",
        "EXCLUDE_FOLDERS",
        "CALENDARS",
        "EXCLUDE_CALENDARS",
        "MAX_AGE_DAYS",
        "PRIVATE_EVENTS",
        "REDACT",
        "MAX_OUTPUT_CHARS",
        "TIMEZONE",
    }
)


# --- settings --------------------------------------------------------------------

#: The settings in force (unset names use the defaults above). Replaced as a whole by
#: :func:`configure`, so a reader never sees half an update.
_settings: dict[str, str] = {}


def configure(settings: Mapping[str, str | None]) -> None:
    """Replace every guard setting; a ``None`` value means "unset" (the default applies)."""
    unknown = set(settings) - SETTING_NAMES
    if unknown:
        raise ValueError(f"unknown guard settings: {sorted(unknown)}")
    global _settings
    _settings = {k: v for k, v in settings.items() if v is not None}


def _env(name: str) -> str | None:
    return _settings.get(name)


def _list(name: str, default: str = "") -> set[str]:
    raw = _env(name)
    raw = default if raw is None else raw
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _on(name: str, default: bool = True) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "off", "false", "no")


def max_age_cutoff_us() -> int | None:
    """Oldest allowed timestamp (us since epoch), or None for no limit."""
    raw = _env("MAX_AGE_DAYS")
    if not raw or not raw.strip().isdigit():
        return None
    return int((time.time() - int(raw) * 86400) * 1_000_000)


def private_events_mode() -> str:
    mode = (_env("PRIVATE_EVENTS") or "show").strip().lower()
    return mode if mode in ("show", "busy", "hide") else "show"


def max_output_chars() -> int:
    raw = _env("MAX_OUTPUT_CHARS")
    return int(raw) if raw and raw.strip().isdigit() else DEFAULT_MAX_OUTPUT_CHARS


def user_timezone() -> tzinfo:
    """Your timezone: configured override, Thunderbird's calendar timezone, then system.

    tern validates the configured name, so an unknown name can only come from Thunderbird's
    prefs; it is skipped with a warning (upstream skipped it silently)."""
    from tern.tbmcp import accounts  # local import: accounts imports config only

    for name in (_env("TIMEZONE"), accounts.local_timezone()):
        if name:
            try:
                return ZoneInfo(name.strip())
            except (ZoneInfoNotFoundError, ValueError):
                log.warning("unknown timezone id in the Thunderbird calendar prefs; skipped")
    try:
        link = os.readlink("/etc/localtime")
        return ZoneInfo(link.split("zoneinfo/", 1)[1])
    except (OSError, IndexError, ZoneInfoNotFoundError, ValueError):
        # no /etc/localtime symlink into a zoneinfo tree: the system's local offset
        return datetime.now().astimezone().tzinfo or UTC


# --- scope -----------------------------------------------------------------------


def account_allowed(names: set[str]) -> bool:
    """``names``: every lower-cased name an account answers to (emails, username)."""
    allow, deny = _list("ACCOUNTS"), _list("EXCLUDE_ACCOUNTS")
    if allow and not names & allow:
        return False
    return not names & deny


def folder_allowed(name: str | None, uri: str | None) -> bool:
    candidates = {(name or "").lower()}
    if uri:
        candidates.add(uri.rstrip("/").rsplit("/", 1)[-1].lower())
    allow = _list("FOLDERS")
    deny = _list("EXCLUDE_FOLDERS", DEFAULT_EXCLUDED_FOLDERS)
    if allow and not candidates & allow:
        return False
    return not candidates & deny


def calendar_allowed(cal_id: str, name: str | None) -> bool:
    keys = {cal_id.lower(), (name or "").lower()}
    allow, deny = _list("CALENDARS"), _list("EXCLUDE_CALENDARS")
    if allow and not keys & allow:
        return False
    return not keys & deny


# --- text hygiene: hidden characters, redaction, injection flags -------------------

# Zero-width, bidi controls, soft hyphen, BOM, and Unicode "tag" characters
# (U+E0000-E007F), which can smuggle invisible instructions to a model.
_HIDDEN = re.compile("[­​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _iban_ok(s: str) -> bool:
    s = s.replace(" ", "").upper()
    if not 15 <= len(s) <= 34:
        return False
    num = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(num) % 97 == 1


_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_SENSITIVE_URL = re.compile(
    r"reset|recover|verif|confirm|activat|magic|one-?time|otp|sign-?in|signin|log-?in|"
    r"login|auth|token|passw|[?&](code|key|sig|signature|secret|pwd|otp)=",
    re.IGNORECASE,
)
_CODE_NEAR_WORD = re.compile(
    r"(?i)(?<!postal )(?<!post )(?<!zip )(?<!area )(?<!promo )(?<!discount )"
    r"(\b(?:code|otp|passcode|pin|verification|one[- ]time|2fa|security code|"
    r"κωδικός|código|codice|Code)\b[^\n\d]{0,40}?)(\d[\d -]{2,8}\d)\b"
)
_CODE_IS_YOUR = re.compile(
    r"(?i)\b(\d{4,8})\b(?=\s+is\s+your\b[^\n]{0,40}\b(?:code|otp|pin|password|passcode))"
)
# Card brands' leading digits (Visa, Mastercard, Amex, Discover), not inside a URL or ID.
_CARD = re.compile(
    r"(?<![\w/=&?#.+-])(?:4\d{3}|5[1-5]\d{2}|2[2-7]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))"
    r"(?:[ -]?\d){9,15}(?![\w/-])"
)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
_KEYS = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|sk_live_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{30,}|"
    r"github_pat_[A-Za-z0-9_]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{35}|glpat-[A-Za-z0-9_-]{20,})\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
_PASSWORD = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|passphrase|κωδικός πρόσβασης|contraseña|passwort)"
    r"\s*[:=]\s*)(\S+)"
)


def redact(text: str) -> tuple[str, int]:
    """Mask secrets; returns the new text and how many were masked."""
    count = 0

    def sub(pattern: re.Pattern, repl, s: str) -> str:
        nonlocal count
        out, n = pattern.subn(repl, s)
        count += n
        return out

    def url(m: re.Match) -> str:
        nonlocal count
        if _SENSITIVE_URL.search(m.group(0)):
            count += 1
            return "[REDACTED:link]"
        return m.group(0)

    def card(m: re.Match) -> str:
        nonlocal count
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            count += 1
            return "[REDACTED:card]"
        return m.group(0)

    def iban(m: re.Match) -> str:
        nonlocal count
        if _iban_ok(m.group(0)):
            count += 1
            return "[REDACTED:iban]"
        return m.group(0)

    text = sub(_PRIVATE_KEY, "[REDACTED:private-key]", text)
    text = _URL.sub(url, text)
    text = sub(_KEYS, "[REDACTED:api-key]", text)
    text = sub(_PASSWORD, r"\1[REDACTED:password]", text)
    text = sub(_CODE_NEAR_WORD, r"\1[REDACTED:code]", text)
    text = sub(_CODE_IS_YOUR, "[REDACTED:code]", text)
    text = _IBAN.sub(iban, text)
    text = _CARD.sub(card, text)
    return text, count


_INJECTION = [
    (
        "ignore-instructions",
        r"\b(ignore|disregard|forget|override)\b[^\n]{0,30}"
        r"\b(previous|prior|above|earlier|all|any|your)\b[^\n]{0,20}"
        r"\b(instructions?|prompts?|rules|directions)\b",
    ),
    (
        "role-play",
        r"\byou are (now )?(an? )?(ai|assistant|agent|language model|chatgpt|"
        r"claude|llm)\b",
    ),
    ("system-prompt", r"\b(system prompt|system message|developer message)\b"),
    ("chat-markup", r"<\|im_start\|>|<\|im_end\|>|\[/?INST\]|<<SYS>>|</?system>"),
    (
        "exfiltration",
        # bulk or sensitive objects only: "send an email to x@y" is ordinary mail
        r"\b(forward|send|e-?mail|upload|post|share|export|copy)\b[^\n]{0,40}"
        r"(\b(all|every|entire|whole|this|these)\b[^\n]{0,20}"
        r"\b(e-?mails?|messages?|inbox|mailbox|threads?|conversations?|contacts|"
        r"passwords?|credentials?|files|data|history)\b"
        r"|\b(inbox|mailbox|credentials|passwords|address book|contacts list)\b)"
        r"[^\n]{0,40}\b(to|at)\b\s+[\w.+-]+@[\w-]+\.[\w.]+",
    ),
    (
        "conceal",
        r"\b(do not|don't|never)\b[^\n]{0,20}\b(tell|inform|mention|reveal|show)"
        r"\b[^\n]{0,30}\b(user|owner|recipient|him|her|them)\b",
    ),
    (
        "tool-call",
        r"\b(call|use|invoke|run|execute)\b[^\n]{0,20}\b(the )?(tool|function|"
        r"command|shell|terminal)\b",
    ),
]
_INJECTION_RE = [(name, re.compile(p, re.IGNORECASE)) for name, p in _INJECTION]


def suspicious_reasons(text: str) -> list[str]:
    return [name for name, rx in _INJECTION_RE if rx.search(text)]


def clean(text: str | None) -> tuple[str, dict[str, Any]]:
    """Remove hidden characters and (if enabled) redact secrets.

    Returns the cleaned text and info: ``hidden`` (characters removed),
    ``redactions`` (secrets masked).
    """
    text = text or ""
    stripped = _HIDDEN.sub("", text)
    info: dict[str, Any] = {"hidden": len(text) - len(stripped), "redactions": 0}
    stripped = unicodedata.normalize("NFC", stripped)
    if _on("REDACT"):
        stripped, info["redactions"] = redact(stripped)
    return stripped, info


def protect(
    item: dict,
    fields: tuple[str, ...],
    full_texts: dict[str, str] | None = None,
    limits: dict[str, int] | None = None,
) -> dict[str, int]:
    """Clean ``fields`` of ``item`` in place and add guard annotations.

    ``full_texts`` supplies the untruncated text for a field; it is cleaned
    first and only then cut to ``limits[field]``, so redaction never sees half
    a secret and the injection check sees the whole text. Returns the cleaned
    full length of each field, for the caller's length/truncated fields.
    """
    full_texts = full_texts or {}
    limits = limits or {}
    redactions = hidden = 0
    reasons: set[str] = set()
    lengths: dict[str, int] = {}
    for f in fields:
        source = full_texts.get(f, item.get(f))
        if not isinstance(source, str) or not source:
            lengths[f] = 0
            continue
        text, info = clean(source)
        redactions += info["redactions"]
        hidden += info["hidden"]
        reasons.update(suspicious_reasons(text))
        lengths[f] = len(text)
        item[f] = text[: limits[f]] if f in limits else text
    if hidden:
        reasons.add("hidden-characters")
    if redactions:
        item["redactions"] = redactions
    if reasons:
        item["suspicious"] = sorted(reasons)
    return lengths


# --- output budget ---------------------------------------------------------------


def _size(obj: Any) -> int:
    # escaped form: the longest a client's JSON encoder could make it
    return len(json.dumps(obj, ensure_ascii=True, default=str))


def fit(result: Any, limit: int | None = None) -> Any:
    """Shrink a tool result to at most ``limit`` JSON characters.

    Lists lose items from the end (a marker records how many); dicts have
    their longest list trimmed first, then their longest strings cut.
    """
    limit = limit or max_output_chars()
    if _size(result) <= limit:
        return result
    if isinstance(result, list):
        lo, hi = 0, len(result)
        while lo < hi:  # largest prefix that fits with the marker
            mid = (lo + hi + 1) // 2
            if _size([*result[:mid], {"omitted": len(result) - mid}]) <= limit:
                lo = mid
            else:
                hi = mid - 1
        return [*result[:lo], {"output_budget_exceeded": True, "omitted": len(result) - lo}]
    if isinstance(result, dict):
        out = dict(result)
        lists = sorted(
            (k for k, v in out.items() if isinstance(v, list)), key=lambda k: -_size(out[k])
        )
        for k in lists:
            items = out[k]
            while items and _size(out) > limit:
                items = items[: max(0, len(items) - max(1, len(items) // 4))]
                out[k] = items
                out["output_budget_exceeded"] = True
                out[f"{k}_omitted"] = len(result[k]) - len(items)
        for k in sorted(
            (k for k, v in out.items() if isinstance(v, str)), key=lambda k: -len(out[k])
        ):
            over = _size(out) - limit
            if over <= 0:
                break
            out[k] = out[k][: max(0, len(out[k]) - over - 50)] + "…"
            out["output_budget_exceeded"] = True
        return out
    if isinstance(result, str):
        return result[: max(0, limit - 1)] + "…"
    return result
