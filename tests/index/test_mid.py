"""Plan 04 step 1: `mid` and `content_hash` (decisions #5 as written).

The hand-computed cases build the expected digest from the literal byte layout with hashlib, never
through `tern.index.mid`. The fixture cases compare every copy with MANIFEST and with the
test-local reference helper in `tests/test_fixture_manifest.py` (its helpers only are imported).
"""

from __future__ import annotations

import ast
import base64
import email
import hashlib
import subprocess
import sys
import time
from collections.abc import Iterator
from email import policy
from pathlib import Path

import pytest
import test_fixture_manifest as tfm

from tern.bridge.protocol import FullMessage, Raw, TbIdParams, TextPart
from tern.index.mid import (
    TB_MD5_PREFIX,
    content_hash,
    content_hash_full,
    mid_from_full,
    mid_from_raw,
    mid_of,
    normalize_message_id,
)
from tern.testing.fake_bridge import FakeBridge

MID_PY = Path(__file__).resolve().parents[2] / "src" / "tern" / "index" / "mid.py"
EPOCH = "1753783200"  # Tue, 29 Jul 2025 10:00:00 +0000 (= 13:00:00 +0300)
DATE = b"Date: Tue, 29 Jul 2025 10:00:00 +0000"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def athens_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A non-UTC local zone, so reading a naive Date as local time would change the epoch."""
    monkeypatch.setenv("TZ", "Europe/Athens")
    time.tzset()
    assert time.timezone != 0, "Europe/Athens did not load (tzdata missing?)"
    yield
    monkeypatch.undo()
    time.tzset()


# ---- 8 hand-computed cases ------------------------------------------------------------------


def test_mixed_case_message_id_is_lower_cased() -> None:
    raw = b"From: a@b.test\nMessage-ID: <AbC.12@Example.TEST>\n\nbody\n"
    assert mid_from_raw(raw) == sha(b"abc.12@example.test")
    assert mid_from_raw(raw.replace(b"Message-ID", b"message-id")) == sha(b"abc.12@example.test")
    assert mid_of("  <AbC.12@Example.TEST> ") == sha(b"abc.12@example.test")


def test_folded_message_id_is_unfolded() -> None:
    # A fold before the id, and one inside it (RFC 5322 unfolding keeps the WSP).
    outer = b"Message-ID:\r\n <Fold.1@x.test>\r\nFrom: a@b.test\r\n\r\nbody\r\n"
    assert mid_from_raw(outer) == sha(b"fold.1@x.test")
    inner = b"Message-ID: <long-left-part\r\n @x.test>\r\nFrom: a@b.test\r\n\r\nbody\r\n"
    assert mid_from_raw(inner) == sha(b"long-left-part @x.test")
    assert normalize_message_id("<long-left-part\n\t@x.test>") == "long-left-part\t@x.test"
    # The raw path agrees with a full form whose header was unfolded by its parser.
    parsed = email.message_from_bytes(outer, policy=policy.default)
    full = FullMessage(
        headers={"message-id": [str(parsed["Message-ID"])]}, text_parts=[], attachments=[]
    )
    assert mid_from_full(full) == mid_from_raw(outer)


def test_content_hash_crlf_equals_lf() -> None:
    lf = b"From: Alice <Alice@B.test>\n" + DATE + b"\nSubject: s\n\nline 1\nline 2\n"
    crlf = lf.replace(b"\n", b"\r\n")
    expected = sha(f"alice@b.test|{EPOCH}|".encode() + b"line 1\nline 2\n")
    assert content_hash(lf) == content_hash(crlf) == expected


def test_content_hash_from_quoting_is_undone_once() -> None:
    head = b"From: a@b.test\n" + DATE + b"\n\n"
    prefix = f"a@b.test|{EPOCH}|".encode()
    quoted = content_hash(head + b"x\n>From here\n")
    unquoted = content_hash(head + b"x\nFrom here\n")
    assert quoted == unquoted == sha(prefix + b"x\nFrom here\n")
    # One ">" is removed from ^>+From lines, never more; only line-initial quoting counts.
    assert content_hash(head + b">>From there\n") == sha(prefix + b">From there\n")
    assert content_hash(head + b"x >From here\n") == sha(prefix + b"x >From here\n")
    assert content_hash(head + b">From: not quoting\n") == sha(prefix + b">From: not quoting\n")


def test_content_hash_missing_or_unreadable_date_hashes_as_empty(athens_tz: None) -> None:
    body = b"hello\n"
    assert content_hash(b"From: a@b.test\n\n" + body) == sha(b"a@b.test||" + body)
    assert content_hash(b"From: a@b.test\nDate: someday soon\n\n" + body) == sha(
        b"a@b.test||" + body
    )
    # A Date with -0000 (no zone information) is read as UTC.
    no_zone = b"From: a@b.test\nDate: Tue, 29 Jul 2025 10:00:00 -0000\n\n" + body
    assert content_hash(no_zone) == sha(f"a@b.test|{EPOCH}|".encode() + body)


def test_content_hash_malformed_or_missing_from_hashes_as_empty() -> None:
    body = b"hello\n"
    assert content_hash(b"From: not an address <<<\n" + DATE + b"\n\n" + body) == sha(
        f"|{EPOCH}|".encode() + body
    )
    assert content_hash(DATE + b"\n\n" + body) == sha(f"|{EPOCH}|".encode() + body)


def test_fallback_mid_is_pinned_to_the_literal_byte_layout() -> None:
    head, body = b"From: a@b.test\r\nSubject: s", b"line\r\n>From x\r\n"
    assert mid_from_raw(head + b"\r\n\r\n" + body) == "x-" + sha(head + body)
    lf_head, lf_body = b"From: a@b.test\nSubject: s", b"line\n"
    assert mid_from_raw(lf_head + b"\n\n" + lf_body) == "x-" + sha(lf_head + lf_body)
    assert mid_from_raw(lf_head + b"\n") == "x-" + sha(lf_head + b"\n")  # no blank line
    empty_id = b"From: a@b.test\nMessage-ID: <>\nSubject: s"
    assert mid_from_raw(empty_id + b"\n\n" + lf_body) == "x-" + sha(empty_id + lf_body)
    # "\n\n" before "\r\n\r\n": the first blank line wins, whatever its line ending.
    mixed = b"From: a@b.test\n\nbody\r\n\r\nmore"
    assert mid_from_raw(mixed) == "x-" + sha(b"From: a@b.test" + b"body\r\n\r\nmore")


def test_fallback_mid_changes_with_an_x_mozilla_status_line() -> None:
    """Documents the gap behind Open question C2: #5's literal fallback is not stable if TB
    adds or rewrites X-Mozilla headers in what getRaw returns."""
    plain = b"From: a@b.test\nSubject: s\n\nbody\n"
    marked = b"X-Mozilla-Status: 0001\n" + plain
    assert mid_from_raw(plain) == "x-" + sha(b"From: a@b.test\nSubject: s" + b"body\n")
    assert mid_from_raw(marked) == "x-" + sha(
        b"X-Mozilla-Status: 0001\nFrom: a@b.test\nSubject: s" + b"body\n"
    )
    assert mid_from_raw(plain) != mid_from_raw(marked)
    assert content_hash(plain) == content_hash(marked)  # content_hash ignores other headers


# ---- more unit cases ------------------------------------------------------------------------


def test_non_utf8_message_id_hashes_its_own_bytes() -> None:
    """Provisional (#30): bytes that are not valid UTF-8 are hashed as themselves; valid UTF-8
    around them is lower-cased as text (here "\u00c9" -> "\u00e9", C3 89 -> C3 A9)."""
    assert mid_from_raw(b"Message-ID: <CAF\xe9@X.test>\n\nb") == sha(b"caf\xe9@x.test")
    raw = b"Message-ID: <\xc3\x89\xe9.1@X.test>\r\n\r\nb"
    assert mid_from_raw(raw) == sha(b"\xc3\xa9\xe9.1@x.test")


def test_mid_of_rejects_tb_md5_ids() -> None:
    assert TB_MD5_PREFIX == "md5:"
    with pytest.raises(ValueError, match="md5"):
        mid_of("md5:0123456789abcdef0123456789abcdef")
    # Only TB's form is refused: a real header value hashes as written, on every path.
    assert mid_of("<md5:abc@x.test>") == sha(b"md5:abc@x.test")
    assert mid_from_raw(b"Message-ID: md5:abc\n\nb") == sha(b"md5:abc")
    full = FullMessage(headers={"message-id": ["md5:abc"]}, text_parts=[], attachments=[])
    assert mid_from_full(full) == sha(b"md5:abc")


def test_first_message_id_field_of_the_header_block_wins() -> None:
    raw = b"Message-ID: <one@x.test>\nMessage-Id: <two@x.test>\n\nMessage-ID: <three@x.test>\n"
    assert mid_from_raw(raw) == sha(b"one@x.test")
    body_only = b"From: a@b.test\n\nMessage-ID: <three@x.test>\n"
    assert mid_from_raw(body_only).startswith("x-")
    assert normalize_message_id("  <> ") is None
    assert mid_of("") is None


def test_full_form_hashes() -> None:
    full = FullMessage(
        headers={
            "From": ["Alice <Alice@B.test>"],
            "date": ["Tue, 29 Jul 2025 13:00:00 +0300"],
            "message-id": ["<Big.1@X.test>"],
        },
        text_parts=[
            TextPart(mime="text/plain", text="καλημέρα"),
            TextPart(mime="text/html", text="b"),
        ],
        attachments=[],
    )
    assert mid_from_full(full) == sha(b"big.1@x.test")
    expected = sha(f"alice@b.test|{EPOCH}|".encode() + "καλημέρα\nb".encode())
    assert content_hash_full(full) == "full-" + expected
    no_id = full.model_copy(update={"headers": {"from": ["a@b.test"]}})
    assert mid_from_full(no_id) is None
    assert content_hash_full(no_id) == "full-" + sha(b"a@b.test||" + "καλημέρα\nb".encode())


# ---- every fixture copy -------------------------------------------------------------------


def _fixture_copies() -> list[tuple[dict[str, object], bytes]]:
    man = tfm.load_manifest(tfm.FIXTURE)
    return [(m, tfm.read_copy(tfm.FIXTURE, man, c)) for m in man["messages"] for c in m["copies"]]


def test_fixture_mids_match_manifest_and_three_are_fallbacks() -> None:
    man = tfm.load_manifest(tfm.FIXTURE)
    copies = _fixture_copies()
    assert len(copies) == man["message_count"] == 187
    x_mids: dict[str, str] = {}
    for m, raw in copies:
        mid = mid_from_raw(raw)
        if m["mid"] is None:
            assert mid.startswith("x-") and len(mid) == 66, m["key"]
            x_mids[mid] = str(m["key"])
        else:
            assert mid == m["mid"], m["key"]
            assert mid == mid_of(str(m["message_id"])) == tfm.mid_of(str(m["message_id"]))
    assert sorted(x_mids.values()) == sorted(man["no_message_id"])
    assert len(x_mids) == 3


def test_fixture_content_hash_matches_the_reference_helper() -> None:
    for m, raw in _fixture_copies():
        assert content_hash(raw) == tfm.content_hash(raw), m["key"]


@pytest.mark.asyncio
async def test_fixture_full_form_mid_agrees_with_raw() -> None:
    fb = FakeBridge(tfm.FIXTURE)
    man = tfm.load_manifest(tfm.FIXTURE)
    n = 0
    for a in man["accounts"]:
        for f in a["folders"]:
            for tb_id in fb.tb_ids(f"{a['key']}:/{f['path']}"):
                raw_msg = await fb.request("mail.messages.raw", TbIdParams(tb_id=tb_id))
                full = await fb.request("mail.messages.full", TbIdParams(tb_id=tb_id))
                assert isinstance(raw_msg, Raw) and isinstance(full, FullMessage)
                mid = mid_from_raw(base64.b64decode(raw_msg.raw_b64))
                assert mid_from_full(full) == (None if mid.startswith("x-") else mid)
                assert content_hash_full(full).startswith("full-")
                n += 1
    assert n == 187


# ---- identity never depends on the MIME parser ----------------------------------------------


def test_mid_does_not_import_mime() -> None:
    tree = ast.parse(MID_PY.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)
    banned = ("mime", "html_text")
    hits = {name for name in imported if name.rsplit(".", 1)[-1] in banned}
    assert not hits, hits


def test_mid_does_not_load_mime_transitively() -> None:
    code = (
        "import sys, tern.index.mid; "
        "print(sorted(m for m in ('tern.index.mime', 'tern.index.html_text') if m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "[]"
