"""Plan 04 step 3: encodings, by fixture key (MANIFEST `encodings`).

RFC 2047 UTF-8 B (enc-2047-utf8) and Q (enc-cp1252's subject), ISO-8859-7 B (enc-2047-iso88597:
MANIFEST's `features` says Q, the fixture has B; recorded in the plan's Risks, fixed in a later
fixture version), windows-1252, iso-8859-1 mislabelled, and the CTEs 7bit/8bit/QP/base64.
Undecodable and unknown charsets are built inline in test_mime.py.
"""

from __future__ import annotations

import email
from email import policy

import pytest
import test_fixture_manifest as tfm

from tern.index.mime import ParsedMessage, parse_raw


def raw_of(key: str) -> bytes:
    man = tfm.load_manifest(tfm.FIXTURE)
    (copy,) = tfm.messages(man)[key]["copies"]
    return tfm.read_copy(tfm.FIXTURE, man, copy)


def parsed(key: str) -> ParsedMessage:
    return parse_raw(raw_of(key))


def cte_of(key: str) -> str:
    return str(
        email.message_from_bytes(raw_of(key), policy=policy.default)["Content-Transfer-Encoding"]
    )


def test_every_manifest_encoding_key_is_covered() -> None:
    man = tfm.load_manifest(tfm.FIXTURE)
    assert {e["key"] for e in man["encodings"]} == {
        "enc-2047-utf8",
        "enc-2047-iso88597",
        "enc-iso88597-qp",
        "enc-cp1252",
        "enc-base64",
        "enc-mislabelled",
    }


def test_rfc2047_utf8_b_subject_and_display_name() -> None:
    raw = raw_of("enc-2047-utf8")
    assert b"Subject: =?utf-8?b?" in raw and b"From: =?utf-8?b?" in raw
    p = parse_raw(raw)
    assert p.subject == "Πρόσκληση σε εγκαίνια έκθεσης"
    assert p.from_name == "Κατερίνα Βλάχου"
    assert p.parse_status == "ok"


def test_rfc2047_utf8_q_subject() -> None:
    raw = raw_of("enc-cp1252")
    assert b"Subject: =?utf-8?q?" in raw
    assert parse_raw(raw).subject == "Café booking for Friday"


def test_rfc2047_iso88597_b_subject() -> None:
    raw = raw_of("enc-2047-iso88597")
    assert b"=?iso-8859-7?b?" in raw  # B, although MANIFEST features says Q (plan Risks)
    assert parse_raw(raw).subject == "Ραντεβού στον οδοντίατρο"


def test_windows_1252_quoted_printable() -> None:
    assert cte_of("enc-cp1252") == "quoted-printable"
    p = parsed("enc-cp1252")
    assert "“corner table”" in p.main_text and "café —" in p.main_text and "€12" in p.main_text
    assert "which seems fair" in p.main_text  # QP soft line break joined
    assert p.parse_status == "ok"


def test_iso88597_8bit() -> None:
    assert cte_of("enc-2047-iso88597") == "8bit"
    p = parsed("enc-2047-iso88597")
    assert p.main_text == "Το ραντεβού μετακινήθηκε για την Τρίτη στις έξι."
    assert p.parse_status == "ok"


def test_iso88597_quoted_printable() -> None:
    assert cte_of("enc-iso88597-qp") == "quoted-printable"
    p = parsed("enc-iso88597-qp")
    assert p.main_text == "Η κοινή συνέλευση θα γίνει στην είσοδο της πολυκατοικίας."
    assert p.parse_status == "ok"


def test_utf8_8bit() -> None:
    assert cte_of("enc-2047-utf8") == "8bit"
    assert parsed("enc-2047-utf8").main_text.startswith("Γεια σου Alex,")


def test_utf8_base64() -> None:
    assert cte_of("enc-base64") == "base64"
    p = parsed("enc-base64")
    assert p.main_text.startswith("Hi Alex,\n\nI spoke to the landlord")
    assert p.parse_status == "ok"


def test_7bit() -> None:
    assert cte_of("inj-quoted-reply") == "7bit"
    assert parsed("inj-quoted-reply").main_text.startswith("Hi Alex,")


def test_iso88591_mislabelled_utf8_is_partial_with_the_greek_text() -> None:
    raw = raw_of("enc-mislabelled")
    assert b'charset="iso-8859-1"' in raw
    p = parse_raw(raw)
    assert p.main_text == "Σου στέλνω τις φωτογραφίες απόψε."
    assert p.parse_status == "partial" and p.defects == ("charset_mislabel",)


@pytest.mark.parametrize("key", ["enc-2047-utf8", "enc-iso88597-qp", "enc-mislabelled"])
def test_greek_display_names_decode(key: str) -> None:
    name = parsed(key).from_name
    assert name and all(not ("\ufffd" in name or c == "?") for c in name)
