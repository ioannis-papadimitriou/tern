"""The committed ``tern-tool/1`` contract equals what this build exports (plan 06a step 7).

``contracts/tern-tool-1.schema.json`` is the JSON Schema of ``ToolMeta``, the metadata document
every tool exports through ``tern tools describe`` (architecture §5; not the built-ins' own
input/output schemas, which 08a's ``toolcalls-v1`` pins). It is frozen (decisions #6). If this
fails, ``ToolMeta`` (or a pydantic upgrade's schema output) changed: restore it, or make the
change ``tern-tool/2`` with a decisions entry. Never regenerate the v1 file to make this pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tern import cli
from tern.tools import CONTRACT, ToolMeta
from tern.tools.builtin import BUILTINS
from tern.tools.registry import describe_registry

CONTRACT_FILE = Path(__file__).resolve().parents[2] / "contracts" / "tern-tool-1.schema.json"


def _committed() -> dict[str, Any]:
    doc = json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _describe(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    assert cli.main(["tools", "describe"]) == 0
    out, err = capsys.readouterr()
    assert err == ""
    doc = json.loads(out)
    assert isinstance(doc, list)
    return doc


def test_committed_contract_equals_the_export(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["tools", "describe", "--json-schema"]) == 0
    exported = capsys.readouterr().out
    committed = CONTRACT_FILE.read_text(encoding="utf-8")
    assert _committed() == ToolMeta.model_json_schema()  # readable diff on a model change
    assert exported == committed  # byte-identical, as `diff <(tern tools describe ...)` checks


def test_contract_marker_is_required_and_closed() -> None:
    schema = _committed()
    assert schema["title"] == "ToolMeta"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["contract"] == {
        "const": CONTRACT,
        "title": "Contract",
        "type": "string",
    }
    # Every field of the document is required, the contract marker included.
    assert set(schema["required"]) == set(schema["properties"])


def test_every_builtin_describe_entry_validates_against_the_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = _describe(capsys)
    schema = _committed()
    names = {e["name"] for e in entries}
    assert {s.name for s in BUILTINS} <= names and BUILTINS  # the real built-ins are listed
    for entry in entries:
        meta = ToolMeta.model_validate(entry)
        assert meta.contract == CONTRACT
        # The entry carries exactly the frozen properties (no jsonschema dependency: the
        # structural part of the schema is checked by hand, the rest by ToolMeta itself).
        assert set(entry) == set(schema["properties"]), entry["name"]
        assert entry["tier"] in schema["$defs"]["Tier"]["enum"]
        assert entry["effective_tier"] in schema["$defs"]["Tier"]["enum"]
        assert set(entry["effects"]) <= set(schema["$defs"]["Effect"]["enum"])
        assert entry["origin"] in schema["properties"]["origin"]["enum"]
        assert meta.model_dump(mode="json") == entry
    # The same documents as the registry's own describe (no CLI-only reshaping).
    assert entries == [m.model_dump(mode="json") for m in describe_registry().describe()]


@pytest.mark.parametrize(
    ("change", "loc"),
    [
        (lambda d: d.pop("contract"), "contract"),
        (lambda d: d.__setitem__("contract", "tern-tool/2"), "contract"),
        (lambda d: d.__setitem__("extra_field", True), "extra_field"),
    ],
    ids=["contract_missing", "contract_v2", "extra_field"],
)
def test_a_document_outside_the_contract_is_refused(
    capsys: pytest.CaptureFixture[str], change: Any, loc: str
) -> None:
    entry = _describe(capsys)[0]
    ToolMeta.model_validate(entry)  # the unchanged entry is valid
    change(entry)
    with pytest.raises(ValidationError) as info:
        ToolMeta.model_validate(entry)
    assert [e["loc"] for e in info.value.errors()] == [(loc,)]
