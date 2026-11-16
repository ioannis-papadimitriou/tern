"""Raw message bytes → the fields the index stores (plan 04 step 3; rules A9/T9/T13).

:func:`parse_raw` never raises on bad input: every problem is recorded in ``defects`` (short
codes, never content) and summarized in ``parse_status``:

- ``failed``: no header block could be parsed (no header field at all), or the stdlib parser
  raised one of :data:`PARSE_ERRORS`; nothing else is extracted. A ``TypeError``,
  ``AttributeError`` or ``NameError`` raised by tern's own code (innermost frame in a ``tern.``
  module) is a bug, not bad input, and is re-raised;
- ``partial``: headers parsed, but a MIME structure defect, a charset problem (unknown or
  undecodable charset → U+FFFD replacement; an ``iso-8859-1``/``us-ascii`` label on valid
  non-ASCII UTF-8 → decoded as UTF-8; a part without a charset label counts as ``us-ascii``, the
  RFC 2045 default), invalid base64, a header whose parser raised (``header_error:<Type>``),
  a header whose value is lost (``header_invalid:<field>``: a ``Date`` with no readable date or
  one whose UTC epoch is outside Python's datetime range, a
  ``From`` without a valid first mailbox, a rejected ``From``/``To``/``Cc`` mailbox; a group
  with no members, e.g. ``undisclosed-recipients:;``, is valid), a header dropped by the header
  bounds below (``header_too_long:<field>``, ``header_budget:<field>``,
  ``header_block_too_long``), or truncation at :data:`TEXT_CAP_BYTES` or at
  ``html_text.MAX_HTML_EVENTS`` (T13);
- ``ok``: none of these.

Rules (the staged goldens' README, plan 04 step 3a):

1. Input: the content_hash-normalized bytes (CRLF → LF; in the body ``^>(>*From )`` → ``\\1``),
   parsed with ``email.policy.default`` (header factory guarded and header block bounded, see
   "Header bounds" below).
2. Text part: a single text/plain part as is; multipart/alternative → the text/plain branch when
   present, else ``html_to_text`` of the HTML branch; multipart/mixed (and any other multipart) →
   every non-attachment text/plain or text/html part in order, joined by one blank line.
   Decoding: CTE first (7bit/8bit/binary, quoted-printable, base64), then the charset.
3. A declared multipart whose boundary never appears has no parts: its body is not used as text
   and yields no attachments. Text from invalid base64 is dropped, not guessed (a payload that
   only lacks ``=`` padding is decoded with the padding restored and marked partial).
4. Quote split, per line of each part: a line starting with ``>`` is quoted; an attribution line
   (``On … wrote:``, Greek ``… έγραψε:``) followed, blank lines skipped, by a quoted line is
   quoted too; a blank line between two quoted lines is quoted; from a line that is exactly
   ``-- `` to the end of the part is the signature, kept in neither field.
5. Normalization of both text fields: LF line ends, trailing spaces/tabs stripped per line,
   zero-width and bidi characters removed (also from the subject and display name), blank-line
   runs collapsed to one, no leading or trailing blank lines. No Unicode normalization.
6. ``from_addr``: the first From mailbox's addr-spec, lower-cased, or None when absent or
   unparseable; an addr-spec with an empty local part or domain is rejected explicitly (the
   stdlib accepts ``"Broken Sender <broken@example.test`` as a mailbox with ``domain=''``);
   a present From that yields None records ``header_invalid:from`` (``partial``). An absent
   From is not a defect. ``content_hash`` (``tern.index.mid``) keeps hashing ``parseaddr``'s
   string unchanged.
7. Attachments (metadata only, MIME order): leaves with ``Content-Disposition: attachment``,
   every leaf that is not text/plain or text/html, and ``message/*`` parts (not descended into).
   Alternatives that are not chosen are neither text nor attachments.

Message-ID, In-Reply-To and References are read from the raw header source (unfolded, angle
brackets stripped, case kept), so no parser rewrite can change them.

Header bounds (T13). The stdlib's structured header parser costs O(n²) on token-dense values:
one message whose ``To`` value or ``Content-Type`` parameter is made of double quotes took, before
these bounds, 0.2-0.6 s at 4 KiB, 0.9-1.9 s at 8 KiB, 3.3-7.3 s at 16 KiB and 216 s at 128 KiB;
the same holds for part headers and for the headers of an attached message (re-folded when its
size is measured). So:

- before the parse, the top-level fields are split as the stdlib's feedparser splits them (lines
  end in CRLF, a lone CR or LF: normalize_raw leaves lone CRs alone) and every one whose
  unfolded value is over :data:`HEADER_FIELD_MAX` is dropped (``header_too_long:<field>``); a
  dropped ``Content-Type`` becomes ``application/octet-stream`` and a dropped
  ``Content-Disposition`` becomes ``attachment`` (the body is then an opaque attachment, not
  guessed as text). Fields that would take the kept block over :data:`HEADER_BLOCK_MAX` are
  dropped with every later field (``header_block_too_long``). Message-ID, In-Reply-To and
  References are exempt from both drops and not counted in the block: they are only read from
  the raw source (linear), and so always agree with the raw Message-ID the mid is made from;
- every structured parse (any part, any header) goes through a guard: a value over
  :data:`HEADER_FIELD_MAX` is not parsed (``header_too_long:<field>``), nor is one that would
  take the message's work over :data:`HEADER_WORK_MAX`, counted as the squared length of each
  distinct value parsed (``header_budget:<field>``); a dropped value is parsed as empty
  (``Content-Type``: ``application/octet-stream``; ``Content-Disposition``: ``attachment``).
  Parsed values are cached per message.

``<field>`` is the lower-cased name of a common field, else ``other`` (no mail content). The
worst case left is about four 8 KiB quote-filled values per message (measured: four such address
fields 2.3 s, ten such part ``Content-Type`` values 1.1 s); callers in async code run the parse
in a worker thread. The bounds drop legitimate values too: a ``To``/``Cc`` list over 8 KiB
(roughly 150+ recipients) loses its addresses (``header_too_long:to``).

Known T13 gap (not bounded here): breadth. The work budget counts squared lengths, so many short
distinct values cost next to nothing against it, and the number of MIME parts is not capped: a
message of many tiny parts, each with its own ``Content-Type``, costs about 4.3 s per MiB of raw
(measured: 10 000 parts in 478 KB 1.8 s, 40 000 in 1.98 MB 8.5 s; linear), so about 110 s at
RAW_MAX (25 MiB), in the worker thread. A per-message cap on parts or parses needs its own plan
item: ``plans/90-post-mvp.md`` B5 "MIME part-breadth cap".
"""

from __future__ import annotations

import binascii
import codecs
import email.errors
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from email.headerregistry import Address, BaseHeader, HeaderRegistry
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.policy import default as default_policy
from types import TracebackType
from typing import Literal

from tern.index.html_text import html_to_text_limited, strip_invisible

log = logging.getLogger(__name__)

ParseStatus = Literal["ok", "partial", "failed"]

#: Text kept per message, in UTF-8 bytes (T13); more is truncated and marked ``partial``.
TEXT_CAP_BYTES = 1 << 20
#: Deeper MIME nesting is not walked (recorded as a defect).
MAX_DEPTH = 32
#: Longest unfolded header value given to the stdlib's structured parser (T13; module docstring).
HEADER_FIELD_MAX = 8 << 10
#: Top-level header bytes kept; the field that would go over it and all later ones are dropped.
HEADER_BLOCK_MAX = 256 << 10
#: Structured header parse work per message: sum of squared lengths of distinct parsed values.
HEADER_WORK_MAX = 4 * HEADER_FIELD_MAX**2

#: What the stdlib email parser is known to raise on hostile input; caught, recorded as a defect
#: with the exception type name only, never re-raised (plan 04: parse failures never raise).
PARSE_ERRORS: tuple[type[Exception], ...] = (
    ValueError,
    TypeError,
    LookupError,
    AttributeError,
    NameError,  # UnboundLocalError from the stdlib address parser (e.g. "To: B@[\t")
    RecursionError,
    email.errors.MessageError,
)
#: Of :data:`PARSE_ERRORS`, the types that signal a programming error when tern raised them.
_BUG_TYPES: tuple[type[Exception], ...] = (TypeError, AttributeError, NameError)

_FROM_QUOTED = re.compile(rb"(?m)^>(>*From )")
_UNFOLD = re.compile(r"\r?\n(?=[ \t])")
_MSG_ID = re.compile(r"<([^<>]*)>")
_B64_ALPHABET = re.compile(rb"[A-Za-z0-9+/]*")
_ATTRIBUTION_EN = re.compile(r"^On\s.*\swrote:$")
_ATTRIBUTION_EL = re.compile(r"^.*\sέγραψε:$")
_SIGNATURE = "-- "
_BODY_TYPES = ("text/plain", "text/html")
#: A header line as the stdlib's feedparser sees one (its ``headerRE``); group 1 is a name.
_HEADER_LINE = re.compile(rb"From |([\x21-\x39\x3b-\x7e]*):|[ \t]")
#: One header field: its first line and its continuation lines, with the line end. Lines end as
#: the feedparser ends them (its ``NLCRE``: CRLF, a lone CR or LF), since normalize_raw leaves a
#: lone CR (and the CR of CR CR LF) alone.
_FIELD = re.compile(rb"[^\r\n]*(?:(?:\r\n|\r|\n)[ \t][^\r\n]*)*(?:\r\n|\r|\n)?")
#: Top-level fields only read from the raw source: never structurally parsed, never capped, and
#: kept (uncounted) past the block bound, so they always agree with the mid's raw Message-ID.
_RAW_ONLY = frozenset({"message-id", "in-reply-to", "references"})
#: Field names a header-bound defect may name; any other is ``other``.
_LABELLED = frozenset(
    {
        "from",
        "to",
        "cc",
        "bcc",
        "reply-to",
        "sender",
        "subject",
        "date",
        "mime-version",
        "content-type",
        "content-disposition",
        "content-transfer-encoding",
    }
)
_OPAQUE_TYPE = "application/octet-stream"
#: What a dropped header is parsed as: a part whose type or disposition was dropped stays an
#: opaque attachment, never text (any other dropped field is parsed as empty).
_DROPPED_VALUE = {"content-type": _OPAQUE_TYPE, "content-disposition": "attachment"}
_LATIN1_ASCII = ("ascii", "iso8859-1")


@dataclass(frozen=True)
class Attachment:
    filename: str | None
    mime: str
    size: int | None  # decoded payload bytes; None when the payload could not be decoded


@dataclass(frozen=True)
class ParsedMessage:
    parse_status: ParseStatus
    defects: tuple[str, ...] = ()
    subject: str | None = None
    from_addr: str | None = None
    from_name: str | None = None
    to_addrs: tuple[str, ...] = ()
    cc_addrs: tuple[str, ...] = ()
    date_utc: int | None = None
    header_message_id: str | None = None
    in_reply_to: str | None = None
    refs: tuple[str, ...] = ()
    main_text: str = ""
    quoted_text: str = ""
    attachments: tuple[Attachment, ...] = ()


def normalize_raw(raw: bytes) -> bytes:
    """The content_hash normalization (``tern.index.mid.content_hash``): CRLF → LF, and one
    ``>`` removed from body lines matching ``^>+From ``."""
    norm = raw.replace(b"\r\n", b"\n")
    i = norm.find(b"\n\n")
    if i < 0:
        return norm
    return norm[: i + 2] + _FROM_QUOTED.sub(rb"\1", norm[i + 2 :])


class _Collector:
    """Mutable state of one parse: defects, the text budget, text parts and attachments."""

    def __init__(self) -> None:
        self.defects: list[str] = []
        self.parts: list[str] = []
        self.attachments: list[Attachment] = []
        self.budget = TEXT_CAP_BYTES
        self.truncated = False

    def defect(self, code: str) -> None:
        if code not in self.defects:
            self.defects.append(code)

    def add_text(self, text: str) -> None:
        if self.truncated:
            return
        data = text.encode("utf-8", "surrogatepass")
        if len(data) > self.budget:
            text = data[: self.budget].decode("utf-8", "ignore")
            self.truncated = True
            self.defect("truncated")
        self.budget -= len(text.encode("utf-8", "surrogatepass"))
        self.parts.append(text)


def _label(name: str) -> str:
    name = name.lower()
    return name if name in _LABELLED else "other"


def _cap_header_block(norm: bytes, col: _Collector) -> bytes:
    """The normalized message with its top-level header block bounded (T13, module docstring);
    unchanged when nothing is over a bound. Fields are split as the stdlib's feedparser splits
    them (any of its line ends); the block ends at the first line it does not take as a header
    line (the blank separator line, or the first body line). Once the block bound is hit, the
    later fields are dropped, except the raw-only ones."""
    if len(norm) <= HEADER_FIELD_MAX:  # no field and no block can be over a bound
        return norm
    kept: list[bytes] = []
    size = 0
    changed = False
    full = False
    pos = 0
    while pos < len(norm):
        m = _FIELD.match(norm, pos)
        assert m is not None and m.end() > pos  # the pattern always matches; pos < len(norm)
        field = m.group()
        line = _HEADER_LINE.match(field)
        if line is None:  # the stdlib's body starts here
            break
        pos = m.end()
        name = line.group(1).decode("ascii").lower() if line.group(1) else None
        if name in _RAW_ONLY:
            kept.append(field)
            continue
        if full:
            continue
        if name is not None:
            # The value as the parser sees it: unfolded, leading blanks stripped.
            value = field[len(name) + 1 :].lstrip(b" \t")
            if len(value) - value.count(b"\n") - value.count(b"\r") > HEADER_FIELD_MAX:
                col.defect(f"header_too_long:{_label(name)}")
                changed = True
                if name not in _DROPPED_VALUE:
                    continue
                field = f"{name}: {_DROPPED_VALUE[name]}\n".encode("ascii")
        if size + len(field) > HEADER_BLOCK_MAX:
            col.defect("header_block_too_long")
            changed = full = True
            continue
        size += len(field)
        kept.append(field)
    if not changed:
        return norm
    return b"".join(kept) + norm[pos:]


class _HeaderGuard:
    """The ``header_factory`` of one parse's policy: every structured header parse goes through
    it (fetch, and the re-fold of an attached message's headers), bounded per value and per
    message (T13, module docstring)."""

    def __init__(self, col: _Collector) -> None:
        self._col = col
        self._base = HeaderRegistry()  # what email.policy.default uses
        self._work = 0
        self._cache: dict[tuple[str, str], BaseHeader] = {}

    def __getitem__(self, name: str) -> type[BaseHeader]:
        return self._base[name]

    def __call__(self, name: str, value: str) -> BaseHeader:
        key = (name.lower(), value)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        n = len(value)
        if n > HEADER_FIELD_MAX:
            code = "header_too_long"
        elif self._work + n * n > HEADER_WORK_MAX:
            code = "header_budget"
        else:
            self._work += n * n
            header = self._base(name, value)
            self._cache[key] = header
            return header
        self._col.defect(f"{code}:{_label(name)}")
        return self._base(name, _DROPPED_VALUE.get(key[0], ""))


def _header_str(value: str) -> str:
    """A raw header source value as text: unfolded; 8-bit bytes read as UTF-8 (RFC 6532),
    U+FFFD for bytes that are not."""
    text = _UNFOLD.sub("", value)
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _text(value: str, col: _Collector) -> str:
    """A parsed header string made storable: 8-bit bytes the stdlib kept as lone surrogates are
    read as UTF-8, U+FFFD for the rest (recorded as ``header_charset``)."""
    clean = value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    if clean != value or "\ufffd" in clean:
        col.defect("header_charset")
    return clean


def _raw_header(msg: Message, name: str) -> str | None:
    for key, value in msg.raw_items():
        if key.lower() == name:
            return _header_str(str(value))
    return None


def _ids(value: str | None) -> list[str]:
    if value is None:
        return []
    return [i.strip() for i in _MSG_ID.findall(value) if i.strip()]


def _valid(addr: Address, col: _Collector) -> str | None:
    if not addr.username or not addr.domain:
        return None
    return _text(addr.addr_spec, col).lower()


def _addresses(msg: Message, name: str, col: _Collector) -> list[Address] | None:
    """The parsed mailboxes of header ``name`` ([] when absent); None when its parser raised
    (recorded as ``header_error:<Type>``)."""
    if name not in msg:
        return []
    try:
        header = msg[name]
        return list(getattr(header, "addresses", ()))
    except PARSE_ERRORS as exc:
        col.defect(f"header_error:{type(exc).__name__}")
        return None


def _addr_list(msg: Message, name: str, col: _Collector) -> tuple[str, ...]:
    out: dict[str, None] = {}  # insertion-ordered set
    for addr in _addresses(msg, name, col) or ():
        spec = _valid(addr, col)
        if spec is None:
            col.defect(f"header_invalid:{name}")
        else:
            out.setdefault(spec)
    return tuple(out)


def _from(msg: Message, col: _Collector) -> tuple[str | None, str]:
    """(from_addr, display name) of the first From mailbox; ``header_invalid:from`` when a
    From field is present but its first mailbox is missing or has no valid addr-spec."""
    from_list = _addresses(msg, "from", col)
    if from_list is None:
        return None, ""
    first = from_list[0] if from_list else None
    addr = _valid(first, col) if first is not None else None
    if addr is None and "from" in msg:
        col.defect("header_invalid:from")
    name = strip_invisible(_text(first.display_name, col)).strip() if first is not None else ""
    return addr, name


def _subject(msg: Message, col: _Collector) -> str | None:
    if "subject" not in msg:
        return None
    try:
        text = str(msg["subject"])
    except PARSE_ERRORS as exc:
        col.defect(f"header_error:{type(exc).__name__}")
        return None
    return strip_invisible(_text(text, col)).strip()


def _date_utc(msg: Message, col: _Collector) -> int | None:
    if "date" not in msg:
        return None
    try:
        dt = getattr(msg["date"], "datetime", None)
    except PARSE_ERRORS as exc:
        col.defect(f"header_error:{type(exc).__name__}")
        return None
    epoch = None if dt is None else utc_epoch(dt)
    if epoch is None:
        col.defect("header_invalid:date")
    return epoch


def utc_epoch(dt: datetime) -> int | None:
    """``dt`` (naive = UTC) as integer unix seconds, or None when ``datetime.fromtimestamp``
    cannot read that epoch back (a date at year 9999 or year 1 shifted past the range by its
    zone offset, e.g. ``31 Dec 9999 23:59:59 -2359``): such a value in ``date_utc`` would make
    every reader of the column (search hits, plan 05) raise."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    epoch = int(dt.timestamp())
    try:
        datetime.fromtimestamp(epoch, UTC)
    except (OverflowError, ValueError, OSError):
        return None
    return epoch


def _payload_bytes(part: Message, col: _Collector) -> bytes | None:
    """The leaf's payload with its CTE decoded; None when base64 is invalid (rule 3)."""
    cte = str(part.get("content-transfer-encoding", "7bit")).strip().lower()
    if cte == "base64":
        # get_payload() would decode leniently (guessing); the ASCII text is all base64 needs,
        # and any 8-bit byte (replaced by the stdlib) makes it invalid anyway.
        text = part.get_payload()
        if not isinstance(text, str):
            col.defect("payload_type")
            return None
        return _b64(text.encode("utf-8", "surrogateescape"), col)
    if cte not in ("7bit", "8bit", "binary", "", "quoted-printable"):
        col.defect("cte_unknown")
    # The raw payload bytes (8-bit bytes kept), with quoted-printable decoded.
    data = part.get_payload(decode=True)
    if not isinstance(data, bytes):
        col.defect("payload_type")
        return None
    return data


def _b64(data: bytes, col: _Collector) -> bytes | None:
    compact = b"".join(data.split())
    stripped = compact.rstrip(b"=")
    if not _B64_ALPHABET.fullmatch(stripped) or len(stripped) % 4 == 1:
        col.defect("base64_invalid")
        return None
    padded = stripped + b"=" * (-len(stripped) % 4)
    if padded != compact:
        col.defect("base64_padding")
    return binascii.a2b_base64(padded, strict_mode=True)


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _decode_charset(data: bytes, label: str | None, col: _Collector) -> str:
    """Rule 3: declared charset (us-ascii when absent); mislabel and unknown/undecodable
    charsets are decoded as described in the module docstring and recorded."""
    try:
        codec = codecs.lookup(label or "us-ascii").name
    except LookupError:
        col.defect("charset_unknown")
        return data.decode("utf-8", "replace")
    if codec in _LATIN1_ASCII and not data.isascii() and _is_utf8(data):
        col.defect("charset_mislabel")
        return data.decode("utf-8")
    try:
        try:
            return data.decode(codec)
        except UnicodeDecodeError:
            col.defect("charset_undecodable")
            return data.decode(codec, "replace")
    # A non-text codec (e.g. "base64": LookupError) or one that cannot decode at all
    # (e.g. "undefined", "idna": a bare UnicodeError) named as a charset.
    except (UnicodeError, LookupError):
        col.defect("charset_unknown")
        return data.decode("utf-8", "replace")


def _is_attachment(part: Message) -> bool:
    return part.get_content_disposition() == "attachment"


def _attachment(part: Message, col: _Collector) -> None:
    ctype = part.get_content_type()
    try:
        filename = part.get_filename()
    except PARSE_ERRORS as exc:
        col.defect(f"header_error:{type(exc).__name__}")
        filename = None
    if filename is not None:
        filename = strip_invisible(_text(filename, col))
    size: int | None
    if part.is_multipart():  # message/*: the enclosed message(s), re-serialized
        try:
            size = sum(len(sub.as_bytes()) for sub in _subparts(part))
        except PARSE_ERRORS as exc:
            col.defect(f"attachment_size:{type(exc).__name__}")
            size = None
    else:
        data = _payload_bytes(part, col)
        size = None if data is None else len(data)
    col.attachments.append(Attachment(filename=filename, mime=ctype, size=size))


def _text_leaf(part: Message, col: _Collector) -> None:
    if col.truncated:
        return
    data = _payload_bytes(part, col)
    if data is None:
        return
    text = _decode_charset(data, part.get_content_charset(), col)
    if part.get_content_type() == "text/html":
        text, stopped = html_to_text_limited(text, col.budget)
        col.add_text(text)
        if stopped:
            col.truncated = True
            col.defect("truncated")
        return
    col.add_text(text)


def _subparts(part: Message) -> list[Message]:
    payload = part.get_payload()
    return [p for p in payload if isinstance(p, Message)] if isinstance(payload, list) else []


def _best_kind(part: Message, depth: int) -> int:
    """2 = holds a text/plain body part, 1 = a text/html one, 0 = neither."""
    if depth > MAX_DEPTH or _is_attachment(part):
        return 0
    ctype = part.get_content_type()
    if ctype.startswith("message/"):
        return 0
    if part.is_multipart():
        return max((_best_kind(p, depth + 1) for p in _subparts(part)), default=0)
    return {"text/plain": 2, "text/html": 1}.get(ctype, 0)


def _walk(part: Message, col: _Collector, depth: int) -> None:
    if depth > MAX_DEPTH:
        col.defect("depth")
        return
    ctype = part.get_content_type()
    if ctype.startswith("message/") or _is_attachment(part):
        _attachment(part, col)
        return
    if part.is_multipart():
        payload = _subparts(part)
        if ctype == "multipart/alternative" and payload:
            kinds = [_best_kind(p, depth + 1) for p in payload]
            best = max(kinds)
            if best:
                _walk(payload[kinds.index(best)], col, depth + 1)
            return
        for sub in payload:
            _walk(sub, col, depth + 1)
        return
    if part.get_content_maintype() == "multipart":
        # Declared multipart without parts (boundary never found): no text, no attachments.
        col.defect("multipart_no_parts")
        return
    if ctype in _BODY_TYPES:
        _text_leaf(part, col)
    else:
        _attachment(part, col)


def _structural_defects(msg: Message, col: _Collector) -> None:
    for part in msg.walk():
        for d in part.defects:
            col.defect(f"mime:{type(d).__name__}")


def _normalize_lines(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        line = line.rstrip(" \t")
        if line or (out and out[-1]):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _is_attribution(line: str) -> bool:
    line = unicodedata.normalize("NFC", line.rstrip(" \t"))
    return bool(_ATTRIBUTION_EN.match(line) or _ATTRIBUTION_EL.match(line))


def split_quotes(text: str) -> tuple[list[str], list[str]]:
    """Rule 4 on one part's text: (main lines, quoted lines), signature dropped."""
    lines = strip_invisible(text.replace("\r\n", "\n").replace("\r", "\n")).split("\n")
    if _SIGNATURE in lines:
        lines = lines[: lines.index(_SIGNATURE)]
    quoted = [line.startswith(">") for line in lines]
    # For a blank line: whether the next non-blank line is a quote line (final value).
    quote_follows = [False] * len(lines)
    # Attribution lines followed (blank lines skipped) by a quote line.
    next_nonblank_quoted = False
    for i in range(len(lines) - 1, -1, -1):
        blank = not lines[i].strip(" \t")
        if not blank and not quoted[i] and next_nonblank_quoted and _is_attribution(lines[i]):
            quoted[i] = True
        if not blank:
            next_nonblank_quoted = quoted[i]
        else:
            quote_follows[i] = next_nonblank_quoted
    # Blank lines between two quote lines (linear: a long blank run must not cost O(n²)).
    prev_quoted = False
    for i, line in enumerate(lines):
        if line.strip(" \t"):
            prev_quoted = quoted[i]
        elif prev_quoted and quote_follows[i]:
            quoted[i] = True
    main = [line for line, q in zip(lines, quoted, strict=True) if not q]
    quote = [line for line, q in zip(lines, quoted, strict=True) if q]
    return main, quote


def _parse(raw: bytes) -> ParsedMessage:
    col = _Collector()
    policy = default_policy.clone(header_factory=_HeaderGuard(col))
    msg = BytesParser(policy=policy).parsebytes(_cap_header_block(normalize_raw(raw), col))
    assert isinstance(msg, EmailMessage)
    if not msg.keys():
        return ParsedMessage(parse_status="failed", defects=(*col.defects, "no_headers"))
    _structural_defects(msg, col)
    from_addr, from_name = _from(msg, col)
    hmid = _raw_header(msg, "message-id")
    hmid = hmid.strip().strip("<>") if hmid is not None else None
    in_reply_to = _ids(_raw_header(msg, "in-reply-to"))
    subject = _subject(msg, col)
    date_utc = _date_utc(msg, col)
    to_addrs = _addr_list(msg, "to", col)
    cc_addrs = _addr_list(msg, "cc", col)
    _walk(msg, col, 0)
    main: list[str] = []
    quote: list[str] = []
    for text in col.parts:
        m, q = split_quotes(text)
        main += [*m, ""]
        quote += [*q, ""]
    return ParsedMessage(
        parse_status="partial" if col.defects else "ok",
        defects=tuple(col.defects),
        subject=subject,
        from_addr=from_addr,
        from_name=from_name or None,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        date_utc=date_utc,
        header_message_id=hmid or None,
        in_reply_to=in_reply_to[0] if in_reply_to else None,
        refs=tuple(_ids(_raw_header(msg, "references"))),
        main_text=_normalize_lines(main),
        quoted_text=_normalize_lines(quote),
        attachments=tuple(col.attachments),
    )


def _innermost(tb: TracebackType | None) -> TracebackType | None:
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    return tb


def _frames(tb: TracebackType | None) -> str:
    """Code locations of a traceback (module:line), no exception message (it may echo mail)."""
    out: list[str] = []
    while tb is not None:
        out.append(f"{tb.tb_frame.f_globals.get('__name__', '?')}:{tb.tb_lineno}")
        tb = tb.tb_next
    return " < ".join(reversed(out))


def parse_raw(raw: bytes) -> ParsedMessage:
    """Parse one message's raw bytes (getRaw) into its indexed fields. Never raises for bad
    input: a parser exception gives ``parse_status='failed'`` with ``parse_error:<Type>``. A
    ``TypeError``/``AttributeError``/``NameError`` raised by tern code itself is re-raised."""
    try:
        return _parse(raw)
    except PARSE_ERRORS as exc:
        last = _innermost(exc.__traceback__)
        where = "" if last is None else str(last.tb_frame.f_globals.get("__name__", ""))
        if isinstance(exc, _BUG_TYPES) and where.startswith("tern."):
            raise
        log.debug("parse_raw failed: %s at %s", type(exc).__name__, _frames(exc.__traceback__))
        return ParsedMessage(parse_status="failed", defects=(f"parse_error:{type(exc).__name__}",))
