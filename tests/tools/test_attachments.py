"""``tern.tools.attachments``: attachment parts of a raw message and their text.

Every file is built here from made-up words (a minimal PDF with one text line, a .docx, an .xlsx,
a text and an HTML part, an attached e-mail); addresses are ``example.com``.
"""

from __future__ import annotations

import io
import zipfile
from email.message import EmailMessage

import pytest

from tern.tools import attachments as att


def pdf(text: str) -> bytes:
    """A minimal one-page PDF that shows ``text`` (Helvetica), with a correct xref table."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    )
    return out.getvalue()


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
        )
    return buf.getvalue()


def xlsx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="{S}"><si><t>item</t></si><si><t>lantern oil</t></si></sst>',
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{S}"><sheetData>'
            '<row><c t="s"><v>0</v></c><c t="inlineStr"><is><t>qty</t></is></c></row>'
            '<row><c t="s"><v>1</v></c><c><v>12</v></c></row>'
            "</sheetData></worksheet>",
        )
    return buf.getvalue()


def message(*parts: tuple[str, str, bytes]) -> bytes:
    """A multipart message with a plain body and the given (filename, mime, data) attachments."""
    msg = EmailMessage()
    msg["From"] = "Quill Sender <quill@example.com>"
    msg["To"] = "reader@example.com"
    msg["Subject"] = "papers for the lantern fair"
    msg["Message-ID"] = "<lantern-fair@example.com>"
    msg.set_content("the body, not an attachment")
    for name, mime, data in parts:
        maintype, subtype = mime.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


def only(raw: bytes) -> att.Part:
    [part] = att.attachments(raw)
    return part


def test_lists_attachments_in_order_and_not_the_body() -> None:
    raw = message(("a.txt", "text/plain", b"one"), ("b.pdf", "application/pdf", pdf("two")))
    parts = att.attachments(raw)
    assert [(p.name, p.mime) for p in parts] == [
        ("a.txt", "text/plain"),
        ("b.pdf", "application/pdf"),
    ]
    assert parts[0].data == b"one"


def test_a_single_part_message_has_no_attachments() -> None:
    msg = EmailMessage()
    msg["Subject"] = "plain"
    msg.set_content("just text")
    assert att.attachments(msg.as_bytes()) == []


def test_pdf_text_is_extracted() -> None:
    got = att.extract_text(
        only(message(("bill.pdf", "application/pdf", pdf("Lantern total 42 coins")))), 1000
    )
    assert got.status == "ok"
    assert got.text is not None and "Lantern total 42 coins" in got.text
    assert not got.truncated


def test_a_pdf_with_no_pages_is_empty_and_garbage_is_unreadable() -> None:
    fake = b"%PDF-1.4\n% placeholder\n%%EOF\n"
    got = att.extract_text(only(message(("x.pdf", "application/pdf", fake))), 1000)
    assert got.status in {"empty", "unreadable"}
    assert got.text is None
    junk = att.extract_text(only(message(("y.pdf", "application/pdf", b"not a pdf at all"))), 1000)
    assert junk.status == "unreadable"
    assert junk.detail is not None and junk.detail.startswith("the PDF could not be parsed (")


def test_docx_and_xlsx_text() -> None:
    d = att.extract_text(
        only(message(("notes.docx", att._DOCX, docx("first line", "second line")))), 1000
    )
    assert (d.status, d.text) == ("ok", "first line\nsecond line")
    x = att.extract_text(only(message(("stock.xlsx", att._XLSX, xlsx()))), 1000)
    assert (x.status, x.text) == ("ok", "# sheet1\nitem\tqty\nlantern oil\t12")


def test_text_csv_and_html() -> None:
    t = att.extract_text(only(message(("list.csv", "text/csv", "a,b\n1,ž\n".encode()))), 1000)
    assert (t.status, t.text) == ("ok", "a,b\n1,ž")
    h = att.extract_text(
        only(message(("page.html", "text/html", b"<p>hello <b>there</b></p>"))), 1000
    )
    assert h.status == "ok" and h.text is not None and "hello there" in h.text


def test_an_attached_email_gives_its_body() -> None:
    inner = EmailMessage()
    inner["Subject"] = "forwarded"
    inner.set_content("the forwarded words")
    outer = EmailMessage()
    outer["Subject"] = "fwd"
    outer.set_content("see attached")
    outer.add_attachment(inner)
    part = only(outer.as_bytes())
    assert part.mime == "message/rfc822"
    got = att.extract_text(part, 1000)
    assert (got.status, got.text) == ("ok", "the forwarded words")


def test_text_is_capped_and_marked_truncated() -> None:
    got = att.extract_text(only(message(("long.txt", "text/plain", b"x" * 500))), 100)
    assert got.status == "ok" and got.text == "x" * 100 and got.truncated


def test_unsupported_and_broken_zip() -> None:
    img = att.extract_text(only(message(("p.png", "image/png", b"\x89PNG..."))), 100)
    assert img.status == "unsupported"
    bad = att.extract_text(only(message(("b.docx", att._DOCX, b"PK broken"))), 100)
    assert bad.status == "unreadable"


def test_an_oversized_zip_member_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(att, "ZIP_MEMBER_MAX", 10)
    got = att.extract_text(only(message(("n.docx", att._DOCX, docx("more than ten bytes")))), 100)
    assert got.status == "unreadable" and got.detail is not None and "_TooLarge" in got.detail


def test_pick_by_name() -> None:
    raw = message(("Invoice-7.PDF", "application/pdf", b"a"), ("notes.txt", "text/plain", b"b"))
    parts = att.attachments(raw)
    assert att.pick(parts, "Invoice-7.PDF") is parts[0]
    assert att.pick(parts, "invoice-7.pdf") is parts[0]
    assert att.pick(parts, "notes") is parts[1]
    assert att.pick(parts, "missing") is None
    assert att.pick(parts, None) is None  # several: the name is needed
    assert att.pick(parts[:1], None) is parts[0]
    two = att.attachments(message(("a1.txt", "text/plain", b"a"), ("a2.txt", "text/plain", b"b")))
    assert att.pick(two, "a") is None  # ambiguous substring: never a guess
