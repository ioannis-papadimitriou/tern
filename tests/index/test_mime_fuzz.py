"""Plan 04 step 3: seeded stdlib fuzz of `parse_raw` (no hypothesis, decisions/plan item 34).

Every fixture copy is mutated by truncation, removal of a boundary line or of a header colon,
base64 corruption and random byte edits. `parse_raw` must never raise and must always record a
parse_status. The mutation mix is checked to reach the failure paths (partial and failed).
"""

from __future__ import annotations

import random
import re
from collections import Counter
from collections.abc import Callable

import pytest
import test_fixture_manifest as tfm

from tern.index.mime import ParsedMessage, parse_raw

SEED = 20261114
ROUNDS = 4  # mutations per fixture copy and per mutator

Mutator = Callable[[random.Random, bytes], bytes]


def truncate(rng: random.Random, raw: bytes) -> bytes:
    return raw[: rng.randrange(len(raw) + 1)]


def drop_boundary_line(rng: random.Random, raw: bytes) -> bytes:
    lines = raw.split(b"\n")
    marks = [i for i, line in enumerate(lines) if line.startswith(b"--")]
    if not marks:
        return truncate(rng, raw)
    del lines[rng.choice(marks)]
    return b"\n".join(lines)


def drop_header_colon(rng: random.Random, raw: bytes) -> bytes:
    end = raw.find(b"\n\n")
    head = raw[: end if end >= 0 else len(raw)]
    colons = [m.start() for m in re.finditer(rb":", head)]
    if not colons:
        return truncate(rng, raw)
    i = rng.choice(colons)
    return raw[:i] + raw[i + 1 :]


def corrupt_base64(rng: random.Random, raw: bytes) -> bytes:
    blocks = [m.span() for m in re.finditer(rb"(?m)^[A-Za-z0-9+/=]{16,}$", raw)]
    if not blocks:
        return flip_bytes(rng, raw)
    start, end = rng.choice(blocks)
    data = bytearray(raw)
    for _ in range(rng.randint(1, 4)):
        data[rng.randrange(start, end)] = rng.choice(b"!#*-_=\x00\xff")
    if rng.random() < 0.3:
        del data[rng.randrange(start, end)]
    return bytes(data)


def flip_bytes(rng: random.Random, raw: bytes) -> bytes:
    data = bytearray(raw)
    for _ in range(rng.randint(1, 8)):
        if not data:
            break
        data[rng.randrange(len(data))] = rng.randrange(256)
    return bytes(data)


MUTATORS: list[Mutator] = [
    truncate,
    drop_boundary_line,
    drop_header_colon,
    corrupt_base64,
    flip_bytes,
]


def check(p: ParsedMessage) -> None:
    assert p.parse_status in ("ok", "partial", "failed")
    assert all(isinstance(d, str) and d for d in p.defects)
    assert (p.parse_status == "ok") == (not p.defects)
    assert isinstance(p.main_text, str) and isinstance(p.quoted_text, str)
    # Every string field must be storable as UTF-8 text (no lone surrogates).
    texts = [p.main_text, p.quoted_text, p.subject, p.from_addr, p.from_name, p.header_message_id]
    texts += [p.in_reply_to, *p.to_addrs, *p.cc_addrs, *p.refs]
    texts += [a.filename for a in p.attachments] + [a.mime for a in p.attachments]
    for text in texts:
        if text is not None:
            text.encode("utf-8")


def _fixture_raws() -> list[bytes]:
    man = tfm.load_manifest(tfm.FIXTURE)
    return [tfm.read_copy(tfm.FIXTURE, man, c) for m in man["messages"] for c in m["copies"]]


@pytest.mark.heavy  # ~1.5 s; fast twin: test_one_fuzz_round_never_raises_and_reaches_every_path
def test_fuzz_never_raises_and_always_records_status() -> None:
    rng = random.Random(SEED)
    man = tfm.load_manifest(tfm.FIXTURE)
    raws = [tfm.read_copy(tfm.FIXTURE, man, c) for m in man["messages"] for c in m["copies"]]
    statuses: Counter[str] = Counter()
    per_mutator: Counter[str] = Counter()
    for raw in raws:
        for mutate in MUTATORS:
            for _ in range(ROUNDS):
                p = parse_raw(mutate(rng, raw))
                check(p)
                statuses[p.parse_status] += 1
                if p.parse_status != "ok":
                    per_mutator[mutate.__name__] += 1
    assert sum(statuses.values()) == len(raws) * len(MUTATORS) * ROUNDS
    # The fuzz reaches the defect paths, not only clean parses.
    assert statuses["partial"] > 100 and statuses["failed"] > 10, statuses
    assert set(per_mutator) == {m.__name__ for m in MUTATORS}, per_mutator


def test_one_fuzz_round_never_raises_and_reaches_every_path() -> None:
    """Fast twin of the `slow` fuzz above (plan 04 merge review L1): one mutation per fixture
    copy and mutator instead of ROUNDS, same checks; it still reaches partial and failed parses
    (at the full fuzz's rate per round) and every mutator still produces a non-ok parse."""
    rng = random.Random(SEED + 1)
    raws = _fixture_raws()
    statuses: Counter[str] = Counter()
    per_mutator: Counter[str] = Counter()
    for raw in raws:
        for mutate in MUTATORS:
            p = parse_raw(mutate(rng, raw))
            check(p)
            statuses[p.parse_status] += 1
            if p.parse_status != "ok":
                per_mutator[mutate.__name__] += 1
    assert sum(statuses.values()) == len(raws) * len(MUTATORS)
    # The full fuzz's floors (partial > 100, failed > 10) at the same rate per round.
    assert statuses["partial"] > 100 // ROUNDS and statuses["failed"] > 10 // ROUNDS, statuses
    assert set(per_mutator) == {m.__name__ for m in MUTATORS}, per_mutator
