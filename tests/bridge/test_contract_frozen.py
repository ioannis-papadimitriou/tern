"""The committed ``tern-bridge/1.N`` contracts equal what this build exports (plan 02 step 9;
1.1: plan 07 part B, decisions #37; 1.2: decisions #39).

``contracts/tern-bridge-1.{0,1,2}.schema.json`` are frozen
(decisions #3, #6). If this fails, the models of that minor changed: restore them, or add the
change as a new minor (``tern-bridge-1.N``) or as ``tern-bridge/2`` with a decisions entry. Never
regenerate a committed file to make this pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tern import cli
from tern.bridge.protocol import MINOR, export_schema

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
VERSIONS = ("1.0", "1.1", "1.2", "1.3")


@pytest.mark.parametrize("version", VERSIONS)
def test_committed_contract_equals_the_export(
    version: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["bridge", "schema", "--version", version]) == 0
    exported = capsys.readouterr().out
    committed = (CONTRACTS / f"tern-bridge-{version}.schema.json").read_text(encoding="utf-8")
    assert json.loads(committed) == export_schema(version)  # readable diff on a model change
    assert exported == committed  # byte-identical, as `diff <(tern bridge schema ...)` checks


def test_every_minor_this_build_speaks_has_a_committed_contract() -> None:
    assert VERSIONS == tuple(f"1.{n}" for n in range(MINOR + 1))
    committed = sorted(p.name for p in CONTRACTS.glob("tern-bridge-1.*.schema.json"))
    assert committed == [f"tern-bridge-{v}.schema.json" for v in VERSIONS]
