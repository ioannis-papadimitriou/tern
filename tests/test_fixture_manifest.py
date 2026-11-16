"""Plan 01 step 7: the synthetic mailbox matches its MANIFEST.json, read with the stdlib only.

Each `check_*` takes a fixture root, so the same checks run on the committed fixture and, in the
mutation tests at the bottom, on a temp copy with one planted case broken (proving each check can
fail). The fixture itself is PROTECTED; these tests never write to it.
"""

from __future__ import annotations

import email
import hashlib
import json
import mailbox
import re
import shutil
from collections.abc import Callable
from email import policy
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_mailbox"
REQUIRED_SENTINEL_KINDS = {"subject", "body", "name", "folder", "account"}
RESERVED_DOMAIN_RE = re.compile(r"(^|\.)(example\.(com|org|net)|test|example|invalid)$", re.I)
EMAIL_RE = re.compile(rb"[\w.+-]+@((?:[\w-]+\.)+[a-zA-Z]{2,})")


# ---- helpers ------------------------------------------------------------------------------


def load_manifest(root: Path) -> dict[str, Any]:
    return json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))


def folders(man: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["key"]: f for a in man["accounts"] for f in a["folders"]}


def open_folder(root: Path, folder: dict[str, Any]) -> mailbox.Mailbox[Any]:
    path = root / folder["file"]
    if folder["format"] == "mbox":
        return mailbox.mbox(path, create=False)
    if folder["format"] == "maildir":
        return mailbox.Maildir(path, factory=None, create=False)
    raise AssertionError(f"unknown folder format {folder['format']!r}")


def read_copy(root: Path, man: dict[str, Any], copy: dict[str, Any]) -> bytes:
    box = open_folder(root, folders(man)[copy["folder"]])
    try:
        return box.get_bytes(copy["locator"])
    finally:
        box.close()


def messages(man: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["key"]: m for m in man["messages"]}


def copy_of(man: dict[str, Any], key: str, variant: str) -> dict[str, Any]:
    (c,) = [c for c in messages(man)[key]["copies"] if c["variant"] == variant]
    return c


def parse(raw: bytes) -> Message:
    return email.message_from_bytes(raw, policy=policy.compat32)


def content_hash(raw: bytes) -> str:
    """decisions #5 normalization: From = lower-cased address, Date = UTC epoch seconds, body =
    raw body bytes with CRLF -> LF and one `>` removed from lines matching `^>+From `.
    Test-local; tern.index (plan 04) owns the production implementation."""
    msg = parse(raw)
    sender = parseaddr(str(msg["From"]))[1].lower()
    date = int(parsedate_to_datetime(str(msg["Date"])).timestamp())
    body = raw.replace(b"\r\n", b"\n").split(b"\n\n", 1)[1]
    body = re.sub(rb"(?m)^>(>*From )", rb"\1", body)
    return hashlib.sha256(f"{sender}|{date}|".encode() + body).hexdigest()


def mid_of(message_id: str) -> str:
    return hashlib.sha256(message_id.strip().strip("<>").lower().encode()).hexdigest()


def decoded_text(raw: bytes) -> str:
    """Decoded Subject, From and every text part (what a parser would expose to tern)."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    out = [str(msg.get("Subject", "")), str(msg.get("From", ""))]
    for part in msg.walk():
        if part.get_content_maintype() == "text":
            out.append(part.get_content())
    return "\n".join(out)


def mail_strings(root: Path, man: dict[str, Any], min_subject: int = 8) -> list[str]:
    """Strings no log or status line may carry (CLAUDE.md log privacy): every copy's decoded
    Subject of at least ``min_subject`` characters (shorter ones are common words) and every
    From/To/Cc addr-spec, as written and lower-cased."""
    out: set[str] = set()
    for m in man["messages"]:
        for c in m["copies"]:
            raw = read_copy(root, man, c)
            subject = str(email.message_from_bytes(raw, policy=policy.default).get("Subject", ""))
            if len(subject.strip()) >= min_subject:
                out.add(subject.strip())
            msg = parse(raw)
            fields = [str(v) for h in ("From", "To", "Cc") for v in msg.get_all(h, [])]
            for _name, addr in getaddresses(fields):
                if "@" in addr:
                    out |= {addr, addr.lower()}
    return sorted(out)


# ---- checks -------------------------------------------------------------------------------


def check_counts(root: Path) -> None:
    man = load_manifest(root)
    fs = folders(man)
    per_folder = {k: 0 for k in fs}
    for m in man["messages"]:
        for c in m["copies"]:
            per_folder[c["folder"]] += 1
    total = 0
    for key, f in fs.items():
        box = open_folder(root, f)
        try:
            n = len(box)
        finally:
            box.close()
        assert n == f["count"] == per_folder[key], f"{key}: file {n}, manifest {f['count']}"
        total += n
    assert total == man["message_count"] == sum(per_folder.values())
    assert len(man["messages"]) == man["distinct_messages"]


def check_identities(root: Path) -> None:
    """Every copy's Message-ID header and mid match MANIFEST; mids are distinct per message."""
    man = load_manifest(root)
    no_id = set(man["no_message_id"])
    seen: set[str] = set()
    for m in man["messages"]:
        if m["key"] in no_id:
            assert m["message_id"] is None and m["mid"] is None
        else:
            assert m["mid"] == mid_of(m["message_id"]) and m["mid"] not in seen
            seen.add(m["mid"])
        for c in m["copies"]:
            header = parse(read_copy(root, man, c))["Message-ID"]
            assert (str(header).strip() if header is not None else None) == m["message_id"], m[
                "key"
            ]
    assert len(no_id) == 3
    for name, entry in man["named"].items():
        assert messages(man)[name]["mid"] == entry["mid"] == mid_of(entry["message_id"])


def check_d1_pairs(root: Path) -> None:
    man = load_manifest(root)
    norm = man["normalization_pair"]
    a = read_copy(root, man, copy_of(man, norm["key"], "crlf-quoted"))
    b = read_copy(root, man, copy_of(man, norm["key"], "lf-unquoted"))
    assert a != b, "normalization pair copies must differ in bytes"
    assert b"\r\n" in a and b"\n>From " in a, "copy A: CRLF and >From-quoted"
    assert b"\r" not in b and b"\nFrom " in b, "copy B: LF and unquoted"
    assert content_hash(a) == content_hash(b), "benign normalization pair must not conflict"
    assert norm["expected_conflicted"] is False

    adv = man["adversarial_pair"]
    g = read_copy(root, man, copy_of(man, adv["key"], "genuine"))
    s = read_copy(root, man, copy_of(man, adv["key"], "spoof"))
    assert parse(g)["Message-ID"] == parse(s)["Message-ID"] == adv["message_id"]
    assert parseaddr(str(parse(g)["From"]))[1] == adv["copies"]["genuine"]["from"]
    assert parseaddr(str(parse(s)["From"]))[1] == adv["copies"]["spoof"]["from"]
    assert adv["copies"]["genuine"]["iban"].encode() in g
    assert adv["copies"]["spoof"]["iban"].encode() in s
    assert content_hash(g) != content_hash(s), "adversarial pair must conflict"
    assert adv["expected_conflicted"] is True and adv["key"] != "inv-0042"


def check_duplicates(root: Path) -> None:
    man = load_manifest(root)
    dups = man["cross_folder_duplicates"]
    assert len(dups) == 2
    for d in dups:
        copies = messages(man)[d["key"]]["copies"]
        assert sorted(c["folder"] for c in copies) == sorted(d["folders"])
        raws = {read_copy(root, man, c) for c in copies}
        assert len(raws) == 1, f"{d['key']}: copies are not byte-identical"


def check_sent(root: Path) -> None:
    man = load_manifest(root)
    sent_folders = [f for f in folders(man).values() if f["special_use"] == "sent"]
    assert sent_folders, "no Sent folder"
    recipients: set[str] = set()
    for f in sent_folders:
        box = open_folder(root, f)
        try:
            for key in box.keys():
                msg = parse(box.get_bytes(key))
                fields = msg.get_all("To", []) + msg.get_all("Cc", [])
                recipients |= {a.lower() for _, a in getaddresses(fields)}
        finally:
            box.close()
    assert sorted(recipients) == man["sent_recipients"] and len(recipients) == 3

    hj = man["thread_hijack"]
    raw = read_copy(root, man, messages(man)[hj["key"]]["copies"][0])
    msg = parse(raw)
    target = messages(man)[hj["names_sent_message"]]
    assert str(msg["In-Reply-To"]).strip() == hj["forged_in_reply_to"] == target["message_id"]
    assert target["copies"][0]["folder"] in {f["key"] for f in sent_folders}
    sender = parseaddr(str(msg["From"]))[1].lower()
    assert sender == hj["from"] and sender not in recipients


def check_sentinels(root: Path) -> None:
    man = load_manifest(root)
    sentinels = man["sentinels"]
    assert REQUIRED_SENTINEL_KINDS <= {s["kind"] for s in sentinels}
    accounts = {a["name"] for a in man["accounts"]}
    folder_paths = {f["path"] for f in folders(man).values()}
    texts: list[tuple[bytes, str]] = []
    for m in man["messages"]:
        if m["kind"] == "malformed":
            continue  # not decodable by design; it carries no sentinel
        for c in m["copies"]:
            raw = read_copy(root, man, c)
            texts.append((raw, decoded_text(raw)))
    for s in sentinels:
        value = s["value"]
        assert value.startswith("SENTINEL-")
        if s["kind"] == "account":
            assert value in accounts
        elif s["kind"] == "folder":
            assert value in folder_paths
        else:
            hits = [raw for raw, text in texts if value in text]
            assert hits, f"{s['kind']} sentinel not found in any decoded message"
            if s["kind"] in ("b64", "2047"):  # must exist only in encoded form on disk
                assert all(value.encode() not in raw for raw in hits)
    # Content-free output tests compare against hashes: each name has one, and they are distinct.
    hashes = [a["hash"] for a in man["accounts"]] + [f["hash"] for f in folders(man).values()]
    assert len(set(hashes)) == len(hashes)
    assert all(re.fullmatch(r"(acct|fold)-[0-9a-f]{4}", h) for h in hashes)


def check_cases(root: Path) -> None:
    """Injections carry their canary in their own message only; the language set is en + el;
    the malformed message is present and defective; every address uses a reserved domain."""
    man = load_manifest(root)
    assert set(man["languages"]) == {"en", "el"}
    assert {m["lang"] for m in man["messages"]} == {"en", "el"}
    injections = man["injections"]
    assert {i["kind"] for i in injections} == {
        "direct", "hidden-html", "quoted-reply", "invite", "multilingual",
    }  # fmt: skip
    for inj in injections:
        holders = [
            m["key"]
            for m in man["messages"]
            if m["kind"] != "malformed"
            and inj["canary"] in decoded_text(read_copy(root, man, m["copies"][0]))
        ]
        assert holders == [inj["key"]], inj["kind"]
    mal = parse(read_copy(root, man, messages(man)[man["malformed"]["key"]]["copies"][0]))
    assert mal.defects, "malformed message parses without defects"
    for m in man["messages"]:
        for c in m["copies"]:
            for match in EMAIL_RE.finditer(read_copy(root, man, c)):
                domain = match.group(1).decode()
                assert RESERVED_DOMAIN_RE.search(domain), f"non-reserved domain in {m['key']}"


CHECKS: dict[str, Callable[[Path], None]] = {
    "counts": check_counts,
    "identities": check_identities,
    "d1_pairs": check_d1_pairs,
    "duplicates": check_duplicates,
    "sent": check_sent,
    "sentinels": check_sentinels,
    "cases": check_cases,
}


@pytest.mark.parametrize("name", list(CHECKS))
def test_fixture_matches_manifest(name: str) -> None:
    CHECKS[name](FIXTURE)


# ---- mutation tests: each check fails when its planted case is broken ---------------------


def _edit_bytes(root: Path, rel: str, old: bytes, new: bytes) -> None:
    path = root / rel
    data = path.read_bytes()
    assert data.count(old) == 1, f"mutation target not unique in {rel}"
    path.write_bytes(data.replace(old, new))


def _edit_manifest(root: Path, change: Callable[[dict[str, Any]], None]) -> None:
    man = load_manifest(root)
    change(man)
    (root / "MANIFEST.json").write_text(json.dumps(man), encoding="utf-8")


def _count_off_by_one(root: Path) -> None:
    _edit_manifest(root, lambda m: m.update(message_count=m["message_count"] + 1))


def _wrong_mid(root: Path) -> None:
    def change(m: dict[str, Any]) -> None:
        m["messages"][0]["mid"] = "0" * 64

    _edit_manifest(root, change)


def _norm_pair_content_differs(root: Path) -> None:
    _edit_bytes(root, "acct3/INBOX.mbox", b">From the desk", b">From THE desk")


def _adversarial_copies_identical(root: Path) -> None:
    def change(m: dict[str, Any]) -> None:
        copies = messages(m)["inv-0077"]["copies"]
        genuine = next(c for c in copies if c["variant"] == "genuine")
        spoof = next(c for c in copies if c["variant"] == "spoof")
        spoof.update(folder=genuine["folder"], locator=genuine["locator"])

    _edit_manifest(root, change)


def _duplicate_drifts(root: Path) -> None:
    _edit_bytes(root, "acct2/Projects.mbox", b"Travel is down", b"Travel is flat")


def _sent_to_fourth_address(root: Path) -> None:
    _edit_bytes(
        root, "acct1/Sent.mbox", b"Lunch on Wednesday?", b"Lunch on Wednesday?\nCc: x@example.test"
    )


def _sentinel_missing(root: Path) -> None:
    man = load_manifest(root)
    (value,) = [s["value"] for s in man["sentinels"] if s["kind"] == "body"]
    _edit_bytes(root, "acct1/INBOX.mbox", value.encode(), b"REFERENCE-REMOVED")


def _canary_missing(root: Path) -> None:
    _edit_bytes(root, "acct1/INBOX.mbox", b"CANARY-INJ-1", b"CANARY-GONE")


MUTATIONS: list[tuple[str, Callable[[Path], None], str]] = [
    ("count", _count_off_by_one, "counts"),
    ("mid", _wrong_mid, "identities"),
    ("normalization-pair", _norm_pair_content_differs, "d1_pairs"),
    ("adversarial-pair", _adversarial_copies_identical, "d1_pairs"),
    ("duplicate", _duplicate_drifts, "duplicates"),
    ("sent-recipients", _sent_to_fourth_address, "sent"),
    ("sentinel", _sentinel_missing, "sentinels"),
    ("canary", _canary_missing, "cases"),
]


@pytest.mark.parametrize(("mutate", "check"), [(m, c) for _, m, c in MUTATIONS],
                         ids=[n for n, _, _ in MUTATIONS])  # fmt: skip
def test_check_fails_when_case_broken(
    tmp_path: Path, mutate: Callable[[Path], None], check: str
) -> None:
    root = tmp_path / "synthetic_mailbox"
    shutil.copytree(FIXTURE, root)
    CHECKS[check](root)  # passes on the intact copy
    mutate(root)
    with pytest.raises(AssertionError):
        CHECKS[check](root)
