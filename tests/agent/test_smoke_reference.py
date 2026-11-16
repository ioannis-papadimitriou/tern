"""The smoke reference case helper (plan 08a step 5, A9): ``tern.testing.smoke.run_reference``.

Runs the committed FakeLLM script ``evals/synthetic/smoke_script.json`` through the real agent
session over a fresh hybrid index of the synthetic mailbox, and shows that the helper fails when
citation stripping is switched off (the score alone would not notice)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tern.agent.citations import CONFLICTED_MARKER, REMOVED_MARKER, Ruling, find_citations
from tern.agent.loop import AgentSession
from tern.index.store import Store
from tern.testing import smoke
from tern.testing.fake_llm import FakeScript, load_script

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_mailbox"
SCRIPT = ROOT / "evals" / "synthetic" / "smoke_script.json"
MANIFEST = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
MIDS = {m["key"]: m["mid"] for m in MANIFEST["messages"]}
QUESTION = "When is the Acme invoice INV-0042 due and who sent the reminder?"
EXPECTED = (MIDS["inv-0042"], MIDS["inv-0042-reminder"])
CONFLICTED = MIDS[MANIFEST["adversarial_pair"]["key"]]
HALLUCINATED = "deadbeef" * 8
CASE = {
    "question": QUESTION,
    "expected": EXPECTED,
    "hallucinated": HALLUCINATED,
    "conflicted": CONFLICTED,
}


@pytest.fixture(scope="module")
def ref(tmp_path_factory: pytest.TempPathFactory) -> smoke.ReferenceIndex:
    return smoke.reference_index(FIXTURE, tmp_path_factory.mktemp("reference"))


def test_the_manifest_marks_the_adversarial_pair_conflicted() -> None:
    assert MANIFEST["adversarial_pair"]["expected_conflicted"] is True
    assert HALLUCINATED not in MIDS.values()


def test_the_committed_script_scores_one_with_both_bad_mids_stripped(
    ref: smoke.ReferenceIndex,
) -> None:
    result = smoke.run_reference(ref, SCRIPT, **CASE)
    turn = result.turn
    assert result.computed == 1.0
    assert turn.outcome == "ok"
    assert turn.citations == EXPECTED
    assert (turn.meta.raw_citations, turn.meta.invalid_citations) == (4, 2)
    assert turn.meta.conflicted_citations == 1
    assert turn.meta.tool_calls == 1
    assert turn.text.count(REMOVED_MARKER) == 1  # the hallucinated mid
    assert turn.text.count(CONFLICTED_MARKER) == 1  # the conflicted one shows why
    assert HALLUCINATED not in turn.text and CONFLICTED not in turn.text
    assert find_citations(turn.text) == list(EXPECTED)
    assert len(result.deltas) > 1 and "".join(result.deltas) == turn.text


def test_the_script_cites_all_four_mids_and_searches_the_probed_query() -> None:
    script = load_script(SCRIPT)
    first, answer = script.requests
    assert first.tool_calls is not None
    [call] = first.tool_calls
    assert (call.name, call.arguments) == (
        "search_messages",
        {"query": "Acme invoice INV-0042", "limit": 10},
    )
    assert answer.content is not None
    assert find_citations(answer.content) == [*EXPECTED, HALLUCINATED, CONFLICTED]


def test_disabled_stripping_fails_the_reference_case(
    ref: smoke.ReferenceIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(AgentSession, "_judge", lambda self, returned, ref: ("valid", ref))
    with pytest.raises(smoke.SmokeError, match="hallucinated mid was not stripped"):
        smoke.run_reference(ref, SCRIPT, **CASE)


def test_ignoring_the_conflicted_flag_fails_the_reference_case(
    ref: smoke.ReferenceIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    def judge(self: AgentSession, returned: set[str], ref: str) -> Ruling:
        indexed = self.index.get_message(ref) is not None
        return ("valid", ref) if indexed and ref in returned else ("invalid", None)

    monkeypatch.setattr(AgentSession, "_judge", judge)
    # The search never returns the conflicted mid, so it is still stripped (as invalid): this
    # case is caught only by the conflicted count. A tool returning the conflicted mid is
    # covered by step 4's tests/agent/test_citations.py.
    with pytest.raises(smoke.SmokeError, match="citation counts"):
        smoke.run_reference(ref, SCRIPT, **CASE)


def _without(script: FakeScript, mid: str) -> FakeScript:
    data = script.model_dump(mode="json", exclude_none=True)
    answer = data["requests"][1]
    cut = answer["content"].replace(f" [{mid}]", "")
    assert cut != answer["content"]
    answer["content"] = cut
    answer.pop("split_at")
    return FakeScript.model_validate(data)


def test_a_script_that_never_cites_the_hallucinated_mid_is_refused(
    ref: smoke.ReferenceIndex,
) -> None:
    script = _without(load_script(SCRIPT), HALLUCINATED)
    with pytest.raises(smoke.SmokeError, match="never cites the hallucinated mid"):
        smoke.run_reference(ref, script, **CASE)


def test_an_indexed_mid_cannot_stand_in_for_the_hallucinated_one(
    ref: smoke.ReferenceIndex,
) -> None:
    other = next(
        m for k, m in MIDS.items() if m and k not in ("inv-0042", "inv-0042-reminder", "inv-0077")
    )
    with pytest.raises(smoke.SmokeError, match="hallucinated mid is indexed"):
        smoke.run_reference(ref, SCRIPT, **{**CASE, "hallucinated": other})


def test_a_mid_that_is_not_conflicted_cannot_stand_in_for_the_conflicted_one(
    ref: smoke.ReferenceIndex,
) -> None:
    store = Store.open(ref.index, readonly=True)
    try:
        row = store.get_message(MIDS["inv-0042-reminder"])
    finally:
        store.close()
    assert row is not None and not row.conflicted
    case = {**CASE, "expected": (MIDS["inv-0042"],), "conflicted": MIDS["inv-0042-reminder"]}
    with pytest.raises(smoke.SmokeError, match="not indexed as conflicted"):
        smoke.run_reference(ref, SCRIPT, **case)


def _with_search_args(script: FakeScript, arguments: dict[str, object]) -> FakeScript:
    """The committed script with other search arguments and no ``expect`` on the answer
    request, so only the helper's own checks can notice a failed search."""
    data = script.model_dump(mode="json", exclude_none=True)
    data["requests"][0]["tool_calls"][0]["arguments"] = arguments
    data["requests"][1].pop("expect")
    return FakeScript.model_validate(data)


def test_a_search_that_did_not_execute_fails_the_reference_case(
    ref: smoke.ReferenceIndex,
) -> None:
    script = _with_search_args(load_script(SCRIPT), {"query": "Acme invoice INV-0042", "limit": 0})
    with pytest.raises(smoke.SmokeError, match="not executed, or failed"):
        smoke.run_reference(ref, script, **CASE)


def test_audited_tools_that_differ_from_the_script_fail_the_reference_case(
    ref: smoke.ReferenceIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FakeLLM always calls the script's tools, so the mismatch is induced on the expected side.
    monkeypatch.setattr(smoke, "_script_tool_names", lambda script: ["get_message"])
    with pytest.raises(smoke.SmokeError, match="not the ones the script makes"):
        smoke.run_reference(ref, SCRIPT, **CASE)


def test_script_citations_follow_script_order_and_skip_think_text() -> None:
    a, b, c, t = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
    script = FakeScript.model_validate(
        {
            "version": 1,
            "requests": [
                {"content": f"first [{a}]"},
                {
                    "lead_content": f"looking [{b}]",
                    "tool_calls": [{"name": "search_messages", "arguments": {"query": "x"}}],
                },
                {"think": f"maybe [{t}]", "content": f"then [{c}]"},
            ],
        }
    )
    assert smoke._script_citations(script) == [a, b, c]
