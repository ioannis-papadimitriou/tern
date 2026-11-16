"""Message identity (`mid`) and `content_hash`: decisions #5 as written (plan 04 step 1).

- ``mid`` = sha256 of the normalized Message-ID (unfolded, stripped, angle brackets removed,
  lower-cased). Without a Message-ID: ``"x-" + sha256(raw header block + raw body)``, the
  getRaw bytes literally, split at the first blank line (the blank line itself excluded).
- ``content_hash`` = sha256(``"<from addr>|<UTC epoch>|"`` + body), body normalized (CRLF → LF,
  one ``>`` removed from ``^>+From `` lines): the rule of ``tests/test_fixture_manifest.py``.
- Over RAW_MAX the extension's ``full`` form is all there is: ``mid_from_full`` and
  ``content_hash_full`` (a ``"full-"`` hash equals only another ``"full-"`` hash; a mid whose
  copies mix it with a raw-derived hash is conflicted, #30 amendment 7 as amended).

Everything here works on the raw bytes or TB's header strings, never on ``mime.py`` output, so
a parser change can never change an identity. Encoding (provisional, #30):

- raw path: the Message-ID value is decoded as UTF-8 with ``surrogateescape`` and the normalized
  value re-encoded the same way, so its own bytes are hashed (a byte that is not valid UTF-8 is
  hashed as itself; valid UTF-8 is lower-cased like any other text);
- strings from TB (headerMessageId, the ``full`` form) are hashed as UTF-8 with
  ``surrogatepass`` so every JS string hashes, lone surrogates included.

Valid text hashes exactly as plain UTF-8 on both paths.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC
from email.header import Header
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import parseaddr, parsedate_to_datetime

from tern.bridge.protocol import FullMessage

X_PREFIX = "x-"
FULL_PREFIX = "full-"
# TB's invented headerMessageId for mail without a Message-ID (#5: never an identity).
TB_MD5_PREFIX = "md5:"

# RFC 5322 §2.2.3 unfolding: a line break followed by WSP is removed, the WSP kept.
_UNFOLD = re.compile(r"\r?\n(?=[ \t])")
# mboxo quoting undone: one ">" removed from lines matching ^>+From .
_FROM_QUOTED = re.compile(rb"(?m)^>(>*From )")
_WSP = (b" ", b"\t")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utf8(text: str) -> bytes:
    return text.encode("utf-8", "surrogatepass")


def normalize_message_id(value: str) -> str | None:
    """Unfold, strip whitespace, strip angle brackets, lower-case; None when nothing is left.

    For an unfolded value this equals ``scripts/gen_synthetic_mailbox.py:mid_of``'s
    normalization (``.strip().strip("<>").lower()``)."""
    norm = _UNFOLD.sub("", value).strip().strip("<>").lower()
    return norm or None


def _mid_of_text(value: str) -> str | None:
    norm = normalize_message_id(value)
    return None if norm is None else _sha256(_utf8(norm))


def mid_of(message_id: str) -> str | None:
    """The ``mid`` of TB's headerMessageId (or any Message-ID string); None when it normalizes
    to nothing. Raises ValueError for TB's invented ``md5:`` ids (case-sensitive prefix): those
    are no Message-ID, so the caller must resolve the item via ``find`` + ``raw`` instead."""
    if message_id.startswith(TB_MD5_PREFIX):
        raise ValueError("TB md5: id has no mid; resolve it via find + raw")
    return _mid_of_text(message_id)


def split_raw(raw: bytes) -> tuple[bytes, bytes]:
    """(header block, body): split at the first ``\\n\\n`` or ``\\r\\n\\r\\n``, whichever comes
    first, that separator excluded; without one the whole input is the header and body is b""."""
    found = [(i, n) for i, n in ((raw.find(b"\n\n"), 2), (raw.find(b"\r\n\r\n"), 4)) if i >= 0]
    if not found:
        return raw, b""
    i, n = min(found)
    return raw[:i], raw[i + n :]


def _first_field(header: bytes, name: bytes) -> bytes | None:
    """The value of the first ``name`` field (case-insensitive) in a raw header block, with its
    continuation lines (line breaks kept), or None when absent."""
    lines = header.split(b"\n")
    for idx, line in enumerate(lines):
        if line[:1] in _WSP:
            continue
        field, colon, value = line.partition(b":")
        if not colon or field.rstrip(b" \t").lower() != name:
            continue
        parts = [value]
        for cont in lines[idx + 1 :]:
            if cont[:1] not in _WSP:
                break
            parts.append(cont)
        return b"\n".join(parts)
    return None


def mid_from_raw(raw: bytes) -> str:
    """#5: sha256 of the first Message-ID field's normalized value; missing or empty →
    ``"x-" + sha256(header block + body)`` over the literal bytes (no normalization)."""
    header, body = split_raw(raw)
    value = _first_field(header, b"message-id")
    if value is not None:
        norm = normalize_message_id(value.decode("utf-8", "surrogateescape"))
        if norm is not None:
            return _sha256(norm.encode("utf-8", "surrogateescape"))
    return X_PREFIX + _sha256(header + body)


def from_address(value: str | Header | None) -> str:
    """Lower-cased address of a From value; "" when missing or unparseable (content_hash's
    rule, also used by ``tern.index.conflicts``)."""
    if value is None:
        return ""
    return parseaddr(str(value))[1].lower()


def date_epoch(value: str | Header | None) -> str:
    """UTC epoch seconds of a Date value as a string; "" when missing or unparseable (#5: a
    Date that cannot be read hashes as missing, the same on every copy). A Date without a zone
    (``-0000``) is UTC."""
    if value is None:
        return ""
    try:
        dt = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError, OverflowError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return str(int(dt.timestamp()))


def content_hash(raw: bytes) -> str:
    """#5 round 2: sha256(``f"{addr}|{epoch}|"`` + normalized body), byte-for-byte the rule of
    ``tests/test_fixture_manifest.py:content_hash``, with missing From/Date hashed as ""."""
    norm = raw.replace(b"\r\n", b"\n")
    i = norm.find(b"\n\n")
    head, body = (norm[:i], norm[i + 2 :]) if i >= 0 else (norm, b"")
    msg = BytesHeaderParser(policy=compat32).parsebytes(head + b"\n\n")
    prefix = f"{from_address(msg.get('From'))}|{date_epoch(msg.get('Date'))}|"
    return _sha256(_utf8(prefix) + _FROM_QUOTED.sub(rb"\1", body))


def _full_header(full: FullMessage, name: str) -> str | None:
    """First value of a header (name case-insensitive) in TB's ``full`` form, or None."""
    for key, values in full.headers.items():
        if key.lower() == name and values:
            return values[0]
    return None


def mid_from_full(full: FullMessage) -> str | None:
    """The ``mid`` from the ``full`` form's Message-ID; None without one (the item is then
    skipped and counted, ``skipped_large_noid``: there are no raw bytes to fall back on)."""
    value = _full_header(full, "message-id")
    return None if value is None else _mid_of_text(value)


def content_hash_full(full: FullMessage) -> str:
    """``"full-" + sha256(f"{addr}|{epoch}|" + "\\n".join(text parts))``: comparable only with
    other ``full-`` hashes; a mix with a raw-derived hash counts as a conflict (#30)."""
    addr, epoch = from_address(_full_header(full, "from")), date_epoch(_full_header(full, "date"))
    prefix = f"{addr}|{epoch}|"
    text = "\n".join(tp.text for tp in full.text_parts)
    return FULL_PREFIX + _sha256(_utf8(prefix) + _utf8(text))
