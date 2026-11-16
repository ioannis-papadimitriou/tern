"""Plan 08b step 4 (A8): the chat tab's citation tokenizer and ``tern.agent.citations`` share one
grammar, checked over one fixture.

``extension/test/fixtures/citation_grammar.json`` is read here by ``find_citations`` and by
``extension/test/citations.test.ts`` by the tab's ``findCitations``; a change to either grammar
that the other does not follow fails one of the two. The fixture's ``whitespace`` list is pinned
to ``str.isspace`` here and to the tab's whitespace set there, because Python's and JavaScript's
``\\s`` differ.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tern.agent.citations import MAX_WS, REMOVED_MARKER, find_citations

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "extension"
    / "test"
    / "fixtures"
    / "citation_grammar.json"
)


def _fixture() -> dict[str, Any]:
    with FIXTURE.open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


CASES: list[dict[str, Any]] = _fixture()["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_find_citations_matches_the_shared_fixture(case: dict[str, Any]) -> None:
    assert find_citations(case["text"]) == case["mids"]


def test_the_fixture_has_citations_and_non_citations() -> None:
    assert sum(1 for c in CASES if c["mids"]) >= 10
    assert sum(1 for c in CASES if not c["mids"]) >= 10


def test_the_fixture_constants_are_the_python_ones() -> None:
    data = _fixture()
    assert data["max_ws"] == MAX_WS
    assert data["removed_marker"] == REMOVED_MARKER


def test_the_fixture_whitespace_is_exactly_str_isspace() -> None:
    expected = [c for c in range(0x110000) if chr(c).isspace()]
    assert _fixture()["whitespace"] == expected


def test_every_whitespace_code_point_has_a_case() -> None:
    texts = {c["text"] for c in CASES if c["mids"]}
    mid = "a" * 64
    for cp in _fixture()["whitespace"]:
        ws = chr(cp)
        assert f"[{ws}mid:{ws}{mid}{ws}]" in texts, f"U+{cp:04X}"
