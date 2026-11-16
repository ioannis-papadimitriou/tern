"""Plan 04 step 3: `tern.index.mime` rules, on fixture messages and inline messages.

Cases the fixture lacks (HTML-only, hidden HTML in a whole message, signatures, undecodable
charsets, the 1 MiB cap, broken headers) are built inline here. No fixture changes.
"""

from __future__ import annotations

import base64
import time
from email.headerregistry import BaseHeader, HeaderRegistry
from email.utils import parseaddr

import pytest
import test_fixture_manifest as tfm

from tern.index import html_text
from tern.index.html_text import MAX_HTML_EVENTS
from tern.index.mime import (
    HEADER_BLOCK_MAX,
    HEADER_FIELD_MAX,
    HEADER_WORK_MAX,
    TEXT_CAP_BYTES,
    Attachment,
    normalize_raw,
    parse_raw,
    split_quotes,
)


def fixture_copies(key: str) -> list[bytes]:
    man = tfm.load_manifest(tfm.FIXTURE)
    return [tfm.read_copy(tfm.FIXTURE, man, c) for c in tfm.messages(man)[key]["copies"]]


def msg(body: str | bytes, *headers: str, ctype: str = "text/plain; charset=utf-8") -> bytes:
    subject = [] if any(h.lower().startswith("subject:") for h in headers) else ["Subject: s"]
    head = ["From: Ann <ann@example.test>", *subject, *headers]
    if ctype:
        head.append(f"Content-Type: {ctype}")
    data = body.encode("utf-8") if isinstance(body, str) else body
    return "\n".join(head).encode("utf-8") + b"\n\n" + data


def multipart(subtype: str, *parts: str, boundary: str = "BND") -> bytes:
    body = "".join(f"--{boundary}\n{p}\n" for p in parts) + f"--{boundary}--\n"
    return msg(body, ctype=f'multipart/{subtype}; boundary="{boundary}"')


PLAIN = "Content-Type: text/plain; charset=utf-8\n\nplain body"
HTML = 'Content-Type: text/html; charset=utf-8\n\n<p>html body</p><p style="display:none">HID</p>'


# ---- input normalization (rule 1) ---------------------------------------------------------


def test_norm_pair_copies_give_identical_main_text() -> None:
    crlf, lf = fixture_copies("norm-pair")
    assert b"\r\n>From the desk" in crlf and b"\n>From" not in lf
    a, b = parse_raw(crlf), parse_raw(lf)
    assert a.main_text == b.main_text
    assert "\nFrom the desk of the treasurer" in a.main_text
    assert "\nFrom next month the gate code" in a.main_text
    assert a.quoted_text == "" and a.parse_status == b.parse_status == "ok"


def test_normalize_raw_only_touches_body_from_lines() -> None:
    raw = b"Subject: x\r\n>From: y\r\n\r\n>From a\r\n>>From b\r\n> From c\r\n"
    assert normalize_raw(raw) == b"Subject: x\n>From: y\n\nFrom a\n>From b\n> From c\n"


# ---- quote / attribution / signature split (rule 4) ----------------------------------------


def test_inj_quoted_reply_canary_is_in_the_quoted_part() -> None:
    (raw,) = fixture_copies("inj-quoted-reply")
    p = parse_raw(raw)
    assert "CANARY-INJ-3" in p.quoted_text
    assert "CANARY-INJ-3" not in p.main_text
    assert p.main_text.endswith("Kestrel updates")


def test_attribution_lines() -> None:
    text = "Reply\n\nOn Mon, 1 Jun 2026, Ann <a@x.test> wrote:\n\n> quoted\n>\n> more\n\nAfter"
    assert split_quotes(text) == (
        ["Reply", "", "", "After"],
        ["On Mon, 1 Jun 2026, Ann <a@x.test> wrote:", "", "> quoted", ">", "> more"],
    )
    greek = "Ναι\nΣτις 1/6, ο/η Άννα έγραψε:\n> παράθεση"
    assert split_quotes(greek) == (["Ναι"], ["Στις 1/6, ο/η Άννα έγραψε:", "> παράθεση"])


def test_attribution_without_following_quote_is_main_text() -> None:
    text = "On Monday the plumber wrote:\nthe pipe is fixed"
    assert split_quotes(text) == (text.split("\n"), [])


def test_blank_lines_between_quotes_are_quoted_others_are_main() -> None:
    main, quoted = split_quotes("> a\n\n> b\n\nmain")
    assert quoted == ["> a", "", "> b"] and main == ["", "main"]


def test_long_blank_run_after_a_quote_is_linear() -> None:
    """A blank run between quotes costs O(n): bounded with perf_counter (no pytest-timeout), on
    a size where a quadratic scan takes seconds and the linear one milliseconds."""
    # Wall-clock bound guards complexity (linear run is >10x under it), not latency: not `slow`.
    start = time.perf_counter()
    split_quotes("> a" + "\n" * 20_001 + "> b")
    assert time.perf_counter() - start < 1.0
    n = 200_000
    main, quoted = split_quotes("> a" + "\n" * (n + 1) + "> b")
    assert main == [] and len(quoted) == n + 2
    main, quoted = split_quotes("> a" + "\n" * (n + 1) + "x")
    assert quoted == ["> a"] and len(main) == n + 1


def test_signature_dropped_to_end_of_part() -> None:
    p = parse_raw(msg("Hello\n-- \nAnn Example\nPhone 123\n> old quote\n"))
    assert p.main_text == "Hello" and p.quoted_text == ""
    # "--" without the trailing space is not the delimiter.
    assert parse_raw(msg("Hello\n--\nAnn")).main_text == "Hello\n--\nAnn"


def test_signature_is_per_part() -> None:
    part1 = "Content-Type: text/plain; charset=utf-8\n\nfirst\n-- \nsig"
    part2 = "Content-Type: text/plain; charset=utf-8\n\nsecond"
    p = parse_raw(multipart("mixed", part1, part2))
    assert p.main_text == "first\n\nsecond"


def test_text_normalization() -> None:
    body = "\n\n\u200bLine one  \t\nLi\u202ene\u2066 two\n\n\n\nEnd\u200f\n\n"
    p = parse_raw(msg(body, "Subject: Sub\u200bject\u202e here "))
    assert p.main_text == "Line one\nLine two\n\nEnd"
    assert p.subject == "Subject here"
    # Inner whitespace and Unicode form are kept.
    assert parse_raw(msg("a  b\tc e\u0301")).main_text == "a  b\tc e\u0301"


def test_subject_zero_width_removed_and_stripped() -> None:
    raw = b"From: a@x.test\nSubject:  =?utf-8?q?Pay=E2=80=8Bment?= \n\nx"
    assert parse_raw(raw).subject == "Payment"
    assert parse_raw(b"From: a@x.test\n\nx").subject is None
    assert parse_raw(b"From: a@x.test\nSubject:\n\nx").subject == ""


# ---- text part choice (rule 2) -------------------------------------------------------------


def test_alternative_prefers_plain() -> None:
    p = parse_raw(multipart("alternative", HTML, PLAIN))
    assert p.main_text == "plain body" and p.attachments == ()


def test_alternative_without_plain_uses_html_to_text() -> None:
    p = parse_raw(multipart("alternative", HTML))
    assert p.main_text == "html body" and p.parse_status == "ok"


def test_html_only_message_strips_hidden_text() -> None:
    body = '<p>Spring sale</p><div style="display:none">CANARY-HIDDEN</div>'
    p = parse_raw(msg(body, ctype="text/html; charset=utf-8"))
    assert p.main_text == "Spring sale"


def test_html_blockquote_goes_to_quoted_text() -> None:
    body = "<div>Yes</div><div>On Mon, Ann wrote:</div><blockquote>Lunch?</blockquote>"
    p = parse_raw(msg(body, ctype="text/html; charset=utf-8"))
    assert (p.main_text, p.quoted_text) == ("Yes", "On Mon, Ann wrote:\n\n> Lunch?")


def test_mixed_joins_text_parts_and_lists_attachments() -> None:
    att = (
        "Content-Type: application/pdf\n"
        "Content-Disposition: attachment; filename*=utf-8''r%C3%A9sum%C3%A9.pdf\n"
        "Content-Transfer-Encoding: base64\n\n" + base64.b64encode(b"%PDF-1.4 abc").decode()
    )
    txt_att = (
        'Content-Type: text/plain; charset=utf-8\nContent-Disposition: attachment; filename="n.txt"'
        "\n\nnot body text"
    )
    inline_img = 'Content-Type: IMAGE/PNG; name="=?utf-8?q?p=C3=AF.png?="\n\nPNGDATA'
    alt = (
        'Content-Type: multipart/alternative; boundary="ALT"\n\n'
        f"--ALT\n{HTML}\n--ALT\nContent-Type: text/html\n\n<p>second html</p>\n--ALT--"
    )
    p = parse_raw(multipart("mixed", PLAIN, att, txt_att, inline_img, alt))
    assert p.main_text == "plain body\n\nhtml body"
    assert [(a.filename, a.mime, a.size) for a in p.attachments] == [
        ("résumé.pdf", "application/pdf", 12),
        ("n.txt", "text/plain", 13),
        ("pï.png", "image/png", 7),
    ]
    assert p.parse_status == "ok"


def test_forwarded_message_is_an_attachment() -> None:
    inner = "Content-Type: message/rfc822\n\nSubject: inner\nFrom: b@x.test\n\ninner body"
    p = parse_raw(multipart("mixed", PLAIN, inner))
    assert p.main_text == "plain body"
    (att,) = p.attachments
    assert (att.mime, att.filename) == ("message/rfc822", None)
    assert att.size == len(b"Subject: inner\nFrom: b@x.test\n\ninner body")


def test_broken_multipart_yields_no_text_and_no_attachments() -> None:
    body = "--OTHER\nContent-Type: text/plain\n\nFALLBACK-TEXT\n--OTHER--\n"
    p = parse_raw(msg(body, ctype='multipart/mixed; boundary="NEVER"'))
    assert (p.main_text, p.attachments, p.parse_status) == ("", (), "partial")


def test_deep_nesting_is_bounded() -> None:
    depth = 60
    body = ""
    for i in range(depth):
        body += f'--B{i}\nContent-Type: multipart/mixed; boundary="B{i + 1}"\n\n'
    body += f"--B{depth}\nContent-Type: text/plain\n\ndeep\n"
    p = parse_raw(msg(body, ctype='multipart/mixed; boundary="B0"'))
    assert p.parse_status == "partial" and "depth" in p.defects and p.main_text == ""


# ---- CTE and charset (rule 3) --------------------------------------------------------------


def test_invalid_base64_text_is_dropped() -> None:
    raw = msg("!!!this is not base64###\n", "Content-Transfer-Encoding: base64")
    p = parse_raw(raw)
    assert (p.main_text, p.parse_status) == ("", "partial")
    assert "base64_invalid" in p.defects


def test_base64_missing_padding_is_repaired_and_partial() -> None:
    raw = msg("aGVsbG8gd29ybGQ\n", "Content-Transfer-Encoding: base64")  # "hello world", no "="
    p = parse_raw(raw)
    assert (p.main_text, p.parse_status, p.defects) == (
        "hello world",
        "partial",
        ("base64_padding",),
    )


def test_unknown_charset_replaced_and_partial() -> None:
    p = parse_raw(msg("caf\xe9 ok".encode("latin-1"), ctype="text/plain; charset=x-no-such"))
    assert (p.main_text, p.parse_status) == ("caf\ufffd ok", "partial")
    assert "charset_unknown" in p.defects


def test_non_text_codec_as_charset_is_unknown() -> None:
    p = parse_raw(msg("aGk=", ctype="text/plain; charset=base64"))
    assert (p.main_text, p.parse_status, p.defects) == ("aGk=", "partial", ("charset_unknown",))


@pytest.mark.parametrize("label", ["undefined", "idna"])
def test_charset_that_cannot_decode_is_unknown_not_failed(label: str) -> None:
    """These codecs raise a bare UnicodeError (not UnicodeDecodeError) on any decode."""
    p = parse_raw(msg("caf\xe9 ok".encode("latin-1"), ctype=f"text/plain; charset={label}"))
    assert (p.main_text, p.parse_status, p.subject) == ("caf\ufffd ok", "partial", "s")
    assert "charset_unknown" in p.defects


def test_undecodable_bytes_replaced_and_partial() -> None:
    p = parse_raw(msg(b"ok \xff\xfe end", ctype="text/plain; charset=utf-8"))
    assert (p.main_text, p.parse_status) == ("ok \ufffd\ufffd end", "partial")
    assert "charset_undecodable" in p.defects


@pytest.mark.parametrize(
    "ctype", ["text/plain; charset=iso-8859-1", "text/plain; charset=us-ascii", "text/plain"]
)
def test_latin1_or_ascii_label_on_utf8_is_decoded_as_utf8_partial(ctype: str) -> None:
    p = parse_raw(msg("Καλημέρα", ctype=ctype))
    assert (p.main_text, p.parse_status, p.defects) == (
        "Καλημέρα",
        "partial",
        ("charset_mislabel",),
    )


def test_real_latin1_stays_latin1_and_ok() -> None:
    p = parse_raw(msg("café".encode("latin-1"), ctype="text/plain; charset=iso-8859-1"))
    assert (p.main_text, p.parse_status) == ("café", "ok")


def test_ascii_only_with_latin1_label_is_ok() -> None:
    assert parse_raw(msg("plain", ctype="text/plain; charset=iso-8859-1")).parse_status == "ok"


# ---- 1 MiB cap (T13) -----------------------------------------------------------------------


def test_text_cap_truncates_and_marks_partial() -> None:
    body = ("αβγδε " * 200_000).encode("utf-8")  # 2.2 MB of 2-byte characters
    p = parse_raw(msg(body))
    assert p.parse_status == "partial" and "truncated" in p.defects
    assert 0 < len(p.main_text.encode("utf-8")) <= TEXT_CAP_BYTES
    assert len(p.main_text.encode("utf-8")) > TEXT_CAP_BYTES - 16
    assert parse_raw(msg(b"x" * (TEXT_CAP_BYTES - 1))).parse_status == "ok"


@pytest.mark.heavy  # ~1 s; fast twin: test_html_tag_cap_truncates_at_a_small_cap (L1)
def test_html_tag_cap_truncates_hidden_markup() -> None:
    """T13: an all-hidden HTML part yields no text, so its tag count bounds the work."""
    body = '<div style="display:none">' + "<i>x</i>" * (MAX_HTML_EVENTS // 2 + 50_000) + "</div>"
    p = parse_raw(msg(body + "<p>after</p>", ctype="text/html; charset=utf-8"))
    assert (p.main_text, p.parse_status, p.defects) == ("", "partial", ("truncated",))


def test_html_tag_cap_truncates_at_a_small_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `slow` test above with the event cap lowered (plan 04 merge review L1): parse_raw
    records the cap as a ``truncated`` partial parse; under the cap the part parses ``ok``."""
    cap = 2_000
    monkeypatch.setattr(html_text, "MAX_HTML_EVENTS", cap)
    body = '<div style="display:none">' + "<i>x</i>" * (cap // 2 + 500) + "</div>"
    p = parse_raw(msg(body + "<p>after</p>", ctype="text/html; charset=utf-8"))
    assert (p.main_text, p.parse_status, p.defects) == ("", "partial", ("truncated",))
    under = '<div style="display:none">' + "<i>x</i>" * (cap // 4) + "</div>"
    p = parse_raw(msg(under + "<p>after</p>", ctype="text/html; charset=utf-8"))
    assert (p.main_text, p.parse_status, p.defects) == ("after", "ok", ())


def test_text_cap_spans_parts_and_html() -> None:
    big_plain = "Content-Type: text/plain; charset=utf-8\n\n" + "a " * (TEXT_CAP_BYTES // 2 - 10)
    big_html = "Content-Type: text/html; charset=utf-8\n\n<p>" + "word " * 400_000 + "</p>"
    p = parse_raw(multipart("mixed", big_plain, big_html))
    assert p.parse_status == "partial" and "truncated" in p.defects
    assert len(p.main_text.encode("utf-8")) <= TEXT_CAP_BYTES
    assert "word" in p.main_text  # the HTML part contributed up to the cap
    html_only = parse_raw(msg("<p>" + "word " * 400_000 + "</p>", ctype="text/html"))
    assert html_only.parse_status == "partial" and "truncated" in html_only.defects


# ---- headers -------------------------------------------------------------------------------


def test_header_fields() -> None:
    raw = (
        b"From: =?utf-8?q?J=C3=B6rg?= <Joerg@Example.TEST>\n"
        b"To: x@y.test, Z <Q@Sub.Example.TEST>, broken, x@Y.test\n"
        b"Cc: undisclosed-recipients:;\n"
        b"Date: Tue, 29 Jul 2025 10:00:00 -0000\n"
        b"Message-ID:\n <Abc.1@Host.TEST>\n"
        b"In-Reply-To: <Parent@h.test> (comment)\n"
        b"References: <Root@h.test>\n <Parent@h.test>\n\nbody"
    )
    p = parse_raw(raw)
    assert (p.from_addr, p.from_name) == ("joerg@example.test", "Jörg")
    assert p.to_addrs == ("x@y.test", "q@sub.example.test") and p.cc_addrs == ()
    assert p.date_utc == 1753783200
    assert p.header_message_id == "Abc.1@Host.TEST"
    assert p.in_reply_to == "Parent@h.test"
    assert p.refs == ("Root@h.test", "Parent@h.test")


def test_unparseable_from_is_none_but_content_hash_input_is_not() -> None:
    """README rule 8: the stdlib yields an Address with an empty domain; mime rejects it."""
    for value in ('"Broken Sender <broken@example.test', "<@example.test>", "nobody"):
        raw = f"From: {value}\nSubject: s\n\nbody".encode()
        assert parse_raw(raw).from_addr is None, value
    assert parseaddr('"Broken Sender <broken@example.test')[1] != ""
    (malformed,) = fixture_copies("malformed")
    assert parse_raw(malformed).from_addr is None


@pytest.mark.parametrize(
    ("header", "error"),
    [
        ('From:  "', "IndexError"),
        ("To: ::;<", "AttributeError"),
        ("To: B@[\t", "UnboundLocalError"),
    ],
)
def test_header_parser_exceptions_are_recorded_not_raised(header: str, error: str) -> None:
    p = parse_raw(f"{header}\nSubject: kept\n\nbody text".encode())
    assert p.parse_status == "partial"
    assert f"header_error:{error}" in p.defects
    assert (p.subject, p.main_text) == ("kept", "body text")


@pytest.mark.parametrize(
    ("header", "defect"),
    [
        ("Date: garbage", "header_invalid:date"),
        ("Date:", "header_invalid:date"),
        ("Date: Fri, 31 Dec 9999 23:59:59 -2359", "header_invalid:date"),  # epoch > datetime.max
        ("To: broken, c@d.test", "header_invalid:to"),
        ("Cc: <@d.test>", "header_invalid:cc"),
    ],
)
def test_header_with_lost_value_marks_partial(header: str, defect: str) -> None:
    p = parse_raw(f"From: a@b.test\n{header}\nSubject: s\n\nbody".encode())
    assert (p.parse_status, p.defects, p.main_text) == ("partial", (defect,), "body")


@pytest.mark.parametrize("value", ["nobody", "", '"Broken Sender <broken@example.test'])
def test_from_without_valid_mailbox_marks_partial(value: str) -> None:
    p = parse_raw(f"From: {value}\nSubject: s\n\nbody".encode())
    assert (p.parse_status, p.defects, p.from_addr) == ("partial", ("header_invalid:from",), None)


def test_valid_headers_and_empty_group_stay_ok() -> None:
    raw = (
        b"From: a@b.test\nTo: c@d.test\nCc: undisclosed-recipients:;\n"
        b"Date: Tue, 29 Jul 2025 10:00:00 +0000\nSubject: s\n\nbody"
    )
    assert (parse_raw(raw).parse_status, parse_raw(raw).defects) == ("ok", ())
    assert parse_raw(b"Subject: s\n\nbody").parse_status == "ok"  # absent From: not a defect


def test_malformed_css_colour_does_not_fail_the_message() -> None:
    body = '<p>Important visible text</p><p style="color:rgba(0,0,0,.)">more</p>'
    p = parse_raw(msg(body, ctype="text/html; charset=utf-8"))
    assert (p.parse_status, p.main_text) == ("ok", "Important visible text\n\nmore")


def test_undecodable_encoded_word_marks_partial() -> None:
    p = parse_raw(b"From: a@x.test\nSubject: =?x-unknown?q?caf=E9?=\n\nbody")
    assert p.subject == "caf\ufffd" and p.parse_status == "partial"


# ---- failed --------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [b"", b"\n\nbody only", b"not a header line\n\nbody", b"\x00\xff"])
def test_no_header_block_is_failed(raw: bytes) -> None:
    p = parse_raw(raw)
    assert p.parse_status == "failed" and p.main_text == "" and p.subject is None


def test_parser_exception_is_failed_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    import tern.index.mime as mime

    def boom(_msg: object, _col: object, _depth: int) -> None:
        raise LookupError("injected")

    monkeypatch.setattr(mime, "_walk", boom)
    p = mime.parse_raw(msg("body"))
    assert (p.parse_status, p.defects) == ("failed", ("parse_error:LookupError",))


@pytest.mark.parametrize("error", [TypeError, AttributeError, NameError])
def test_stdlib_type_errors_are_failed_not_raised(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """Raised outside tern (here: a test-module frame standing in for the stdlib): bad input."""
    import tern.index.mime as mime

    def boom(_msg: object, _col: object, _depth: int) -> None:
        raise error("injected")

    monkeypatch.setattr(mime, "_walk", boom)
    p = mime.parse_raw(msg("body"))
    assert (p.parse_status, p.defects) == ("failed", (f"parse_error:{error.__name__}",))


def test_bug_in_tern_code_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """An AttributeError raised in mime.py's own frame is a bug: it propagates (fail loudly)."""
    import tern.index.mime as mime

    monkeypatch.setattr(mime, "strip_invisible", lambda _s: None)  # _subject then calls None.strip
    with pytest.raises(AttributeError):
        mime.parse_raw(msg("body"))


# ---- header bounds (T13) -------------------------------------------------------------------------

#: Four times the field cap: the unbounded stdlib parse of this value took about 12 s, so a lost
#: bound fails in bounded time (and :func:`no_oversized_parse` makes it fail at once).
QUOTES = '"' * (4 * HEADER_FIELD_MAX)


@pytest.fixture
def no_oversized_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any value over the cap that reaches the stdlib's structured header parser (guarded or
    not) fails the test at once instead of running the quadratic parse."""
    real = HeaderRegistry.__call__

    def spy(self: HeaderRegistry, name: str, value: str) -> BaseHeader:
        assert len(value) <= HEADER_FIELD_MAX, f"{name}: {len(value)} chars parsed"
        return real(self, name, value)

    monkeypatch.setattr(HeaderRegistry, "__call__", spy)


@pytest.mark.usefixtures("no_oversized_parse")
def test_a_quote_filled_to_header_is_dropped() -> None:
    """The stdlib's address parser is quadratic on this value (128 KiB took 216 s unbounded)."""
    p = parse_raw(msg("body", f"To: {QUOTES}", "Cc: cc@example.test"))
    assert p.parse_status == "partial"
    assert "header_too_long:to" in p.defects
    assert p.to_addrs == ()
    assert (p.from_addr, p.subject, p.cc_addrs, p.main_text) == (
        "ann@example.test",
        "s",
        ("cc@example.test",),
        "body",
    )


@pytest.mark.slow
@pytest.mark.usefixtures("no_oversized_parse")
def test_a_one_mib_quote_filled_to_header_parses_in_well_under_a_second() -> None:
    """The plan's T13 latency check (wall clock, so ``slow``): a 1 MiB value costs only the
    linear pre-scan."""
    quotes = '"' * (1 << 20)
    raw = msg("body", f"To: {quotes}")
    t0 = time.perf_counter()
    p = parse_raw(raw)
    elapsed = time.perf_counter() - t0
    assert (p.parse_status, p.to_addrs) == ("partial", ())
    assert "header_too_long:to" in p.defects
    assert elapsed < 1.0


def test_a_top_level_field_over_the_cap_is_absent_and_one_at_the_cap_is_kept() -> None:
    over = parse_raw(msg("body", "Subject: " + "a" * (HEADER_FIELD_MAX + 1)))
    assert over.subject is None  # dropped before the parse: absent, not empty
    assert (over.parse_status, over.defects) == ("partial", ("header_too_long:subject",))
    at = parse_raw(msg("body", "Subject: " + "a" * HEADER_FIELD_MAX))
    assert (at.parse_status, at.subject) == ("ok", "a" * HEADER_FIELD_MAX)
    # the cap is on the unfolded value
    folded = parse_raw(msg("body", "Subject: " + "\n ".join(["a" * 80] * 120)))
    assert folded.subject is None and "header_too_long:subject" in folded.defects


def test_an_uncommon_field_over_the_cap_is_named_other() -> None:
    p = parse_raw(msg("body", "X-Secret-Name: " + "a" * (HEADER_FIELD_MAX + 1)))
    assert p.defects == ("header_too_long:other",)


def test_raw_only_fields_are_not_capped() -> None:
    """Message-ID, In-Reply-To and References never reach the structured parser."""
    refs = " ".join(f"<r{i}@example.test>" for i in range(1000))  # ~19 KiB
    p = parse_raw(msg("body", f"References: {refs}"))
    assert p.parse_status == "ok"
    assert len(p.refs) == 1000


@pytest.mark.usefixtures("no_oversized_parse")
def test_a_top_level_content_type_over_the_cap_makes_the_body_opaque() -> None:
    body = "--BND\nContent-Type: text/plain\n\nnot guessed as text\n--BND--\n"
    p = parse_raw(msg(body, ctype=f"multipart/mixed; boundary=BND; a={QUOTES}"))
    assert "header_too_long:content-type" in p.defects
    assert p.main_text == ""
    assert p.attachments == (Attachment(None, "application/octet-stream", len(body)),)


@pytest.mark.parametrize(
    "filler", [QUOTES, "a" * (HEADER_FIELD_MAX + 1)], ids=["quotes", "letters"]
)
@pytest.mark.usefixtures("no_oversized_parse")
def test_a_part_header_over_the_cap_is_not_parsed(filler: str) -> None:
    """Part headers never pass the top-level pre-scan: the parse-time guard bounds them."""
    hostile = f"Content-Type: text/plain; a={filler}\n\npart text"
    p = parse_raw(multipart("mixed", PLAIN, hostile))
    assert "header_too_long:content-type" in p.defects
    assert p.main_text == "plain body"  # the hostile part is opaque, not text
    assert [a.mime for a in p.attachments] == ["application/octet-stream"]


ATTACHED_TEXT = "SECRET ATTACHMENT TEXT"


@pytest.mark.usefixtures("no_oversized_parse")
def test_a_part_disposition_over_the_cap_keeps_the_part_an_attachment() -> None:
    """A dropped Content-Disposition is parsed as ``attachment``: the part is not merged into
    the text (the same as a dropped Content-Type)."""
    long_name = "a" * (HEADER_FIELD_MAX + 1)
    part = (
        "Content-Type: text/plain\n"
        f'Content-Disposition: attachment; filename="{long_name}"\n\n{ATTACHED_TEXT}'
    )
    p = parse_raw(multipart("mixed", PLAIN, part))
    assert (p.parse_status, p.defects) == ("partial", ("header_too_long:content-disposition",))
    assert p.main_text == "plain body"
    assert ATTACHED_TEXT not in p.main_text
    assert [(a.filename, a.mime) for a in p.attachments] == [(None, "text/plain")]


@pytest.mark.usefixtures("no_oversized_parse")
def test_a_top_level_disposition_over_the_cap_makes_the_body_an_attachment() -> None:
    long_name = "a" * (HEADER_FIELD_MAX + 1)
    p = parse_raw(msg(ATTACHED_TEXT, f'Content-Disposition: attachment; filename="{long_name}"'))
    assert "header_too_long:content-disposition" in p.defects
    assert p.main_text == ""
    assert [(a.filename, a.mime) for a in p.attachments] == [(None, "text/plain")]


@pytest.mark.usefixtures("no_oversized_parse")
def test_an_attached_message_header_over_the_cap_is_not_parsed() -> None:
    """An attached message's headers are re-folded when its size is measured."""
    inner = f"Content-Type: message/rfc822\n\nFrom: b@example.test\nTo: {QUOTES}\n\ninner"
    p = parse_raw(multipart("mixed", PLAIN, inner))
    assert "header_too_long:to" in p.defects
    assert [a.mime for a in p.attachments] == ["message/rfc822"]


def test_structured_parse_work_is_bounded_per_message() -> None:
    """Four values at the cap use up the message's work; later part headers are not parsed."""
    pad = "a" * (HEADER_FIELD_MAX - 30)
    parts = [f"Content-Type: text/plain; p{i}={pad}\n\npart {i}" for i in range(6)]
    p = parse_raw(multipart("mixed", *parts))
    assert HEADER_WORK_MAX == 4 * HEADER_FIELD_MAX**2
    assert "header_budget:content-type" in p.defects
    assert "part 0" in p.main_text and "part 5" not in p.main_text
    assert p.attachments and {a.mime for a in p.attachments} == {"application/octet-stream"}


def test_a_header_block_over_the_cap_loses_its_later_fields() -> None:
    pads = [f"X-Pad-{i}: " + "a" * 100 for i in range(HEADER_BLOCK_MAX // 100)]
    p = parse_raw(msg("body", *pads, "To: late@example.test"))
    assert "header_block_too_long" in p.defects
    assert p.to_addrs == ()  # after the cap
    assert (p.from_addr, p.subject, p.main_text) == ("ann@example.test", "s", "body")


@pytest.mark.usefixtures("no_oversized_parse")
def test_a_message_of_only_an_oversized_field_fails_with_the_reason() -> None:
    p = parse_raw(f"To: {QUOTES}\n\nbody".encode())
    assert (p.parse_status, p.defects) == ("failed", ("header_too_long:to", "no_headers"))


CR_BODY = "\r".join(f"line {i} of the body" for i in range(800))  # over HEADER_FIELD_MAX


def test_a_large_cr_only_message_keeps_its_headers_and_body() -> None:
    """The feedparser also ends lines at a lone CR (normalize_raw keeps those): the pre-scan
    splits fields the same way, so it does not take the whole message for one oversized field."""
    head = "From: Ann <ann@example.test>\rSubject: hello\rTo: b@example.test\r"
    raw = (head + "Content-Type: text/plain; charset=utf-8\r\r" + CR_BODY).encode()
    assert len(raw) > HEADER_FIELD_MAX and b"\n" not in raw
    p = parse_raw(raw)
    assert (p.parse_status, p.defects) == ("ok", ())
    assert (p.subject, p.from_addr, p.to_addrs) == (
        "hello",
        "ann@example.test",
        ("b@example.test",),
    )
    assert p.main_text.startswith("line 0 of the body\nline 1 of the body\n")
    assert p.main_text.endswith("line 799 of the body")


@pytest.mark.parametrize("eol", ["\r", "\r\r\n"], ids=["cr", "cr-crlf"])
@pytest.mark.usefixtures("no_oversized_parse")
def test_an_oversized_field_is_dropped_under_cr_line_ends(eol: str) -> None:
    """Only the oversized field goes; the fields around it and the body stay."""
    head = eol.join(["From: Ann <ann@example.test>", f"To: {QUOTES}", "Subject: hello"])
    p = parse_raw((head + eol + eol + "body").encode())
    assert (p.parse_status, p.defects) == ("partial", ("header_too_long:to",))
    assert (p.subject, p.from_addr, p.to_addrs, p.main_text) == (
        "hello",
        "ann@example.test",
        (),
        "body",
    )


def test_raw_only_fields_past_the_block_cap_are_kept() -> None:
    """Message-ID, In-Reply-To and References are not dropped with the later fields, so the
    record's ids agree with the raw Message-ID the mid is made from."""
    pads = [f"X-Pad-{i}: " + "a" * 100 for i in range(HEADER_BLOCK_MAX // 100)]
    ids = ["Message-ID: <m1@example.test>", "In-Reply-To: <p1@example.test>"]
    p = parse_raw(msg("body", *pads, *ids, "References: <r1@example.test>"))
    assert "header_block_too_long" in p.defects
    assert (p.header_message_id, p.in_reply_to, p.refs) == (
        "m1@example.test",
        "p1@example.test",
        ("r1@example.test",),
    )
    assert (p.from_addr, p.subject, p.main_text) == ("ann@example.test", "s", "body")


@pytest.mark.parametrize("eol", ["\r", "\r\r\n"], ids=["cr", "cr-crlf"])
def test_a_field_at_the_cap_is_kept_under_cr_line_ends(eol: str) -> None:
    """The cap is on the value the parser sees: its CR and LF line ends are not counted."""
    unfolded = "a" * 64 + "".join([" " + "a" * 63] * (HEADER_FIELD_MAX // 64 - 1))
    assert len(unfolded) == HEADER_FIELD_MAX
    folded = "a" * 64 + "".join([eol + " " + "a" * 63] * (HEADER_FIELD_MAX // 64 - 1))
    head = eol.join(["From: Ann <ann@example.test>", f"Subject: {folded}"])
    p = parse_raw((head + eol + eol + "body").encode())
    assert (p.parse_status, p.from_addr, p.main_text) == ("ok", "ann@example.test", "body")
    assert p.subject == unfolded
