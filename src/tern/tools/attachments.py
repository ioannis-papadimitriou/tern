"""Attachments of a raw message and their text, for the ``read_attachment`` tool.

Pure functions over the message's raw bytes (what ``mail.messages.raw`` returns): no I/O, no
logging. :func:`attachments` lists the attachment parts; :func:`extract_text` turns one into
text:

- ``text/*`` (plain, csv, markdown, calendar, ...) and ``.txt``/``.csv``/``.md``/``.ics``/
  ``.json``/``.xml``: decoded with the part's charset (UTF-8 when none; undecodable bytes become
  U+FFFD);
- ``text/html``/``.html``: :func:`tern.index.html_text.html_to_text_limited`;
- ``application/pdf``: ``pypdf``'s page text, page by page until the cap; an encrypted PDF that
  does not open with an empty password is ``encrypted``;
- ``.docx`` and ``.xlsx``: the document's text read from its zip members with the standard
  library (paragraphs; sheets as tab-separated rows);
- ``message/rfc822`` (an attached e-mail): its plain-text (else HTML) body.

Anything else is ``unsupported``. A file the parser rejects is ``unreadable`` (the error type is
the only detail). Text is capped at ``max_chars`` (``truncated`` says so). Zip members above
:data:`ZIP_MEMBER_MAX` bytes uncompressed are refused (``unreadable``), so a zip bomb cannot
expand.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from typing import Literal
from xml.etree import ElementTree

import pypdf
import pypdf.errors

from tern.index.html_text import html_to_text_limited

#: Uncompressed size above which a .docx/.xlsx member is refused.
ZIP_MEMBER_MAX = 32 * 1024 * 1024
#: PDF pages read at most (the text cap usually stops earlier).
PDF_MAX_PAGES = 500

Status = Literal["ok", "empty", "unsupported", "encrypted", "unreadable"]

_TEXT_EXT = {".txt", ".csv", ".tsv", ".md", ".ics", ".vcf", ".json", ".xml", ".log", ".eml"}
_HTML_EXT = {".html", ".htm"}
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@dataclass(frozen=True)
class Part:
    """One attachment: its file name (None when it has none), MIME type and decoded bytes."""

    name: str | None
    mime: str
    data: bytes
    charset: str | None
    message: Message | None  # the attached message of a message/rfc822 part


@dataclass(frozen=True)
class Extracted:
    status: Status
    text: str | None
    truncated: bool
    #: Why a file is ``unreadable`` (the parser's error type only).
    detail: str | None = None


def attachments(raw: bytes) -> list[Part]:
    """Every attachment part of the message, in MIME order: a leaf part with a file name or an
    ``attachment`` disposition, or an attached message (``message/rfc822``)."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    out: list[Part] = []
    _walk(msg, out, top=True)
    return out


def _walk(msg: Message, out: list[Part], *, top: bool) -> None:
    if msg.get_content_type() == "message/rfc822" and not top:
        inner = msg.get_payload()
        attached = inner[0] if isinstance(inner, list) and inner else None
        out.append(
            Part(
                name=msg.get_filename(),
                mime="message/rfc822",
                data=attached.as_bytes() if isinstance(attached, Message) else b"",
                charset=None,
                message=attached if isinstance(attached, Message) else None,
            )
        )
        return
    if msg.is_multipart():
        for sub in msg.get_payload():
            if isinstance(sub, Message):
                _walk(sub, out, top=False)
        return
    if top:
        return  # a single-part message has a body, no attachment
    name = msg.get_filename()
    if name is None and msg.get_content_disposition() != "attachment":
        return  # a body part
    payload = msg.get_payload(decode=True)
    out.append(
        Part(
            name=name,
            mime=msg.get_content_type(),
            data=payload if isinstance(payload, bytes) else b"",
            charset=msg.get_content_charset(),
            message=None,
        )
    )


def pick(parts: list[Part], name: str | None) -> Part | None:
    """The part named ``name`` (exact, then case-insensitive, then a unique case-insensitive
    substring); with no ``name``, the only part. None when nothing (or more than one) matches."""
    if name is None:
        return parts[0] if len(parts) == 1 else None
    for match in (
        lambda p: p.name == name,
        lambda p: (p.name or "").casefold() == name.casefold(),
        lambda p: name.casefold() in (p.name or "").casefold(),
    ):
        found = [p for p in parts if match(p)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return None
    return None


def _ext(part: Part) -> str:
    return posixpath.splitext((part.name or "").lower())[1]


def extract_text(part: Part, max_chars: int) -> Extracted:
    """The part's text, at most ``max_chars`` characters (module docstring)."""
    ext = _ext(part)
    if part.mime == "message/rfc822":
        return _from_message(part.message, max_chars)
    if part.mime == "text/html" or ext in _HTML_EXT:
        text, stopped = html_to_text_limited(_decode(part), max_chars)
        return _done(text, max_chars, cut=stopped)
    if part.mime.startswith("text/") or ext in _TEXT_EXT:
        return _done(_decode(part), max_chars)
    if part.mime == "application/pdf" or ext == ".pdf":
        return _pdf(part.data, max_chars)
    if part.mime == _DOCX or ext == ".docx":
        return _zip_text(part.data, max_chars, _DocxReader)
    if part.mime == _XLSX or ext == ".xlsx":
        return _zip_text(part.data, max_chars, _XlsxReader)
    return Extracted("unsupported", None, False)


def _decode(part: Part) -> str:
    charset = part.charset or "utf-8"
    try:
        return part.data.decode(charset, errors="replace")
    except LookupError:  # an unknown charset label
        return part.data.decode("utf-8", errors="replace")


def _done(text: str, max_chars: int, *, cut: bool = False) -> Extracted:
    text = text.strip()
    if not text:
        return Extracted("empty", None, False)
    if len(text) > max_chars:
        return Extracted("ok", text[:max_chars], True)
    return Extracted("ok", text, cut)


def _from_message(msg: Message | None, max_chars: int) -> Extracted:
    if not isinstance(msg, EmailMessage):
        return Extracted("unreadable", None, False)
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return Extracted("empty", None, False)
    content = body.get_content()
    if not isinstance(content, str):
        return Extracted("unreadable", None, False)
    if body.get_content_type() == "text/html":
        text, stopped = html_to_text_limited(content, max_chars)
        return _done(text, max_chars, cut=stopped)
    return _done(content, max_chars)


def _pdf(data: bytes, max_chars: int) -> Extracted:
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            return Extracted("encrypted", None, False)
        chunks: list[str] = []
        size = 0
        for i, page in enumerate(reader.pages):
            if i >= PDF_MAX_PAGES or size > max_chars:
                return _done("\n\n".join(chunks), max_chars, cut=True)
            text = page.extract_text() or ""
            chunks.append(text)
            size += len(text)
    except pypdf.errors.FileNotDecryptedError:
        return Extracted("encrypted", None, False)
    except (pypdf.errors.PyPdfError, ValueError, KeyError, TypeError) as exc:
        # pypdf raises these plain types on malformed files too; the type is the only detail
        return Extracted(
            "unreadable", None, False, f"the PDF could not be parsed ({type(exc).__name__})"
        )
    return _done("\n\n".join(chunks), max_chars)


def _zip_text(data: bytes, max_chars: int, read: type[_ZipReader]) -> Extracted:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            text = read(zf).text()
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, _TooLarge) as exc:
        return Extracted(
            "unreadable", None, False, f"the file could not be parsed ({type(exc).__name__})"
        )
    return _done(text, max_chars)


class _TooLarge(Exception):
    pass


class _ZipReader:
    def __init__(self, zf: zipfile.ZipFile) -> None:
        self._zf = zf

    def xml(self, member: str) -> ElementTree.Element:
        info = self._zf.getinfo(member)
        if info.file_size > ZIP_MEMBER_MAX:
            raise _TooLarge(member)
        return ElementTree.fromstring(self._zf.read(info))

    def has(self, member: str) -> bool:
        return member in self._zf.namelist()

    def text(self) -> str:
        raise NotImplementedError


class _DocxReader(_ZipReader):
    def text(self) -> str:
        root = self.xml("word/document.xml")
        paragraphs = ["".join(t.text or "" for t in p.iter(f"{_W}t")) for p in root.iter(f"{_W}p")]
        return "\n".join(paragraphs)


class _XlsxReader(_ZipReader):
    def text(self) -> str:
        shared: list[str] = []
        if self.has("xl/sharedStrings.xml"):
            shared = [
                "".join(t.text or "" for t in si.iter(f"{_S}t"))
                for si in self.xml("xl/sharedStrings.xml").iter(f"{_S}si")
            ]
        sheets = sorted(
            n for n in self._zf.namelist() if n.startswith("xl/worksheets/") and n.endswith(".xml")
        )
        out: list[str] = []
        for sheet in sheets:
            out.append(f"# {posixpath.basename(sheet)[:-4]}")
            for row in self.xml(sheet).iter(f"{_S}row"):
                out.append("\t".join(self._cell(c, shared) for c in row.iter(f"{_S}c")))
        return "\n".join(out)

    @staticmethod
    def _cell(cell: ElementTree.Element, shared: list[str]) -> str:
        kind = cell.get("t")
        if kind == "inlineStr":
            return "".join(t.text or "" for t in cell.iter(f"{_S}t"))
        value = cell.find(f"{_S}v")
        raw = value.text if value is not None and value.text is not None else ""
        if kind == "s" and raw.isdigit() and int(raw) < len(shared):
            return shared[int(raw)]
        return raw
