"""Plan 04 step 3: `tern.index.mime` against the hand-written goldens of step 3a (A9).

The goldens were committed before any mime code and are frozen: a failing case is reported, never
edited. They are read only through ``GOLDEN_DIR`` (step 3g moves them to ``tests/golden/mime``
and changes this one line). File ``<key>.json`` is the fixture message ``key`` (single copy);
``<key>.<variant>.json`` is that copy of it.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm

from tern.index.mime import parse_raw

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden" / "mime"

FIELDS = ("subject", "from_addr", "main_text", "quoted_text", "parse_status", "attachments")
CASES = sorted(GOLDEN_DIR.glob("*.json"))


def _raw(stem: str) -> bytes:
    man = tfm.load_manifest(tfm.FIXTURE)
    key, _, variant = stem.partition(".")
    copies = tfm.messages(man)[key]["copies"]
    if variant:
        copy = tfm.copy_of(man, key, variant)
    else:
        assert len(copies) == 1, f"{key}: several copies, the golden must name its variant"
        (copy,) = copies
    return tfm.read_copy(tfm.FIXTURE, man, copy)


def test_golden_set_is_complete() -> None:
    """Every case the plan lists (step 3a) has a golden."""
    man = tfm.load_manifest(tfm.FIXTURE)
    expected = {e["key"] for e in man["encodings"]}
    expected |= {"inj-hidden-html", "inj-quoted-reply", "malformed"}
    expected |= {f"norm-pair.{v}" for v in man["normalization_pair"]["copies"]}
    assert {p.stem for p in CASES} == expected


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_mime_matches_golden(path: Path) -> None:
    expected: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    assert set(expected) == set(FIELDS)
    parsed = parse_raw(_raw(path.stem))
    got = {f: getattr(parsed, f) for f in FIELDS}
    got["attachments"] = [asdict(a) for a in parsed.attachments]
    assert got == expected
