"""Plan 08a step 4: citation validation and the streaming citation filter (architecture §2, §9a).

The filter is tested on its own (grammar, every split position, counts) and through the agent
loop over the real registry, engine, audit log, index and ``LLMClient`` over ``FakeLLM``. The
test-only tool ``dummy_mids`` returns the mids it is given as untrusted hits, so a test can make
a tool "return" a conflicted, an ``x-`` or an unindexed mid this turn.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from tern.agent.citations import (
    CONFLICTED_MARKER,
    MAX_WS,
    REMOVED_MARKER,
    CitationFilter,
    Judge,
    Ruling,
    Verdict,
    find_citations,
    resolve,
)
from tern.agent.loop import AgentSession, AgentTurn
from tern.index.store import Store
from tern.testing.fake_llm import FakeExpect, FakeToolCall, ScriptStep
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, tool
from tern.tools.ids import normalize_ref, short_id
from tern.tools.registry import Registry

from .conftest import MANIFEST, SINGLE, Env, registry_with, steps

QUERY = "Acme invoice INV-0042"
REMINDER: str = MANIFEST["named"]["inv-0042-reminder"]["mid"]
#: The adversarial pair (MANIFEST ``adversarial_pair``): two copies that disagree.
CONFLICTED: str = MANIFEST["named"]["inv-0077"]["mid"]
#: A well-formed mid no message has (synthetic).
HALLUCINATED = hashlib.sha256(b"hallucinated-citation").hexdigest()
HALLUCINATED_X = "x-" + hashlib.sha256(b"hallucinated-x-citation").hexdigest()


class MidsIn(BoundaryModel):
    mids: list[str]


class Hit(BoundaryModel):
    mid: str


class HitsOut(BoundaryModel):
    hits: list[Hit]


@tool(name="dummy_mids", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=True)
async def dummy_mids(ctx: ToolContext, inp: MidsIn) -> HitsOut:
    """Return the given mids as hits (untrusted R0 dummy)."""
    return HitsOut(hits=[Hit(mid=m) for m in inp.mids])


@tool(name="dummy_trusted_mids", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def dummy_trusted_mids(ctx: ToolContext, inp: MidsIn) -> HitsOut:
    """Return the given mids as hits (trusted R0 dummy)."""
    return HitsOut(hits=[Hit(mid=m) for m in inp.mids])


class MidListOut(BoundaryModel):
    mids: list[str]


@tool(name="dummy_mid_list", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=True)
async def dummy_mid_list(ctx: ToolContext, inp: MidsIn) -> MidListOut:
    """Return the given mids under a ``mids`` key, not ``mid`` (untrusted R0 dummy)."""
    return MidListOut(mids=list(inp.mids))


def x_mid(store: Store) -> str:
    row = store.connection.execute(
        "SELECT mid FROM messages WHERE mid LIKE 'x-%' AND conflicted = 0 ORDER BY mid LIMIT 1"
    ).fetchone()
    assert row is not None
    return str(row[0])


def mids_call(*mids: str) -> FakeToolCall:
    return FakeToolCall(name="dummy_mids", arguments={"mids": list(mids)})


def search_call() -> FakeToolCall:
    return FakeToolCall(name="search_messages", arguments={"query": QUERY, "limit": 10})


async def run(session: AgentSession, text: str = "question") -> tuple[list[str], AgentTurn]:
    handle = session.start_turn(text)
    deltas = [d async for d in handle]
    return deltas, await handle.result()


def registry(env: Env) -> Registry:
    return registry_with(env, dummy_mids)


# ---- the filter on its own ------------------------------------------------------------------

V = "a" * 64  # a mid the judge calls valid
C = "c" * 64  # conflicted
H = "b" * 64  # invalid (hallucinated)
VX = "x-" + "d" * 64  # a valid x- mid


VERDICTS: dict[str, Verdict] = {V: "valid", C: "conflicted", VX: "valid"}


def judge(ref: str) -> Ruling:
    """The verdict of the lower-cased reference; a valid one resolves to itself."""
    verdict = VERDICTS.get(ref.lower(), "invalid")
    return (verdict, ref.lower() if verdict == "valid" else None)


def filtered(*deltas: str) -> tuple[str, CitationFilter]:
    f = CitationFilter(judge)
    out = "".join(f.feed(d) for d in deltas) + f.flush()
    return out, f


def test_matched_pairs_with_whitespace_and_prefix_are_citations() -> None:
    text = f"[{V}] 【{V}】 [ {V} ] [mid:{V}] 【 mid: {V}\t】 [ {V}\n] [{VX}] [mid: {VX}]"
    assert find_citations(text) == [V, V, V, V, V, V, VX, VX]


@pytest.mark.parametrize(
    "text",
    [
        f"[{V}】",  # mismatched pairs
        f"【{V}]",
        V,  # a bare mid
        VX,
        f"[{V}0]",  # 65 hex digits
        f"[x{V}]",
        f"[x- {V[:64]}]",
        f"[X-{V}]",
        f"[MID:{V}]",
        f"[mid {V}]",
        f"[mid:mid:{V}]",
        f"[{V} {V}]",
        f"[{V[:7]}]",  # shorter than any reference
        f"[x-{V[:7]}]",
        f"[{V}",  # never closed
        f"[[{V}",
        REMOVED_MARKER,
        "[]",
        "[mid:]",
    ],
)
def test_what_is_not_a_citation_passes_unchanged(text: str) -> None:
    out, f = filtered(text)
    assert find_citations(text) == [] and out == text
    assert (f.raw, f.invalid, f.conflicted, f.citations) == (0, 0, 0, ())


WS_PLACES = [
    ("after the opener", "[{ws}" + V + "]"),
    ("after mid:", "【mid:{ws}" + V + "】"),
    ("before the closer", "[" + V + "{ws}]"),
]


@pytest.mark.parametrize(("place", "template"), WS_PLACES)
def test_whitespace_in_each_place_is_capped(place: str, template: str) -> None:
    at_cap = template.format(ws=" \n\t\u3000" * (MAX_WS // 4))
    assert find_citations(at_cap) == [V], place
    over = template.format(ws=" " * (MAX_WS + 1))
    out, f = filtered(over)
    assert find_citations(over) == [] and out == over and f.raw == 0, place
    # The span dies on the first whitespace character past the cap: nothing is held after it.
    g = CitationFilter(judge)
    head = over[: over.index(" ") + MAX_WS + 1]
    assert g.feed(head) == head and not g.holding, place


def test_an_opener_followed_by_a_long_whitespace_run_is_released_at_once() -> None:
    f = CitationFilter(judge)
    assert f.feed("see [") == "see " and f.holding
    assert f.feed(" " * MAX_WS) == "" and f.holding
    assert f.feed("\n" * 1000) == "[" + " " * MAX_WS + "\n" * 1000 and not f.holding


def test_an_opener_inside_a_dead_span_starts_a_new_one() -> None:
    text = f"[[{H}] 【[{V}] [mid【{C}】 [{V}】[{V}]"
    assert find_citations(text) == [H, V, C, V]
    out, f = filtered(text)
    assert out == f"[{REMOVED_MARKER} 【[{V}] [mid{CONFLICTED_MARKER} [{V}】[{V}]"
    assert (f.raw, f.invalid, f.conflicted, f.citations) == (4, 2, 1, (V,))


def test_a_conflicted_citation_shows_why_and_is_no_citation() -> None:
    out, f = filtered(f"Pay it [{C}].")
    assert out == f"Pay it {CONFLICTED_MARKER}." and f.citations == ()
    assert (f.invalid, f.conflicted) == (1, 1) and find_citations(out) == []


def test_raw_counts_are_taken_before_stripping() -> None:
    text = f"A [{V}] B [{H}] C 【{C}】 D [mid: {V}] E [{VX}] F [{HALLUCINATED_X}] G [{H}]"
    out, f = filtered(text)
    assert (f.raw, f.invalid, f.conflicted) == (7, 4, 1)
    assert f.citations == (V, VX)  # distinct, in order of first appearance
    assert out == (
        f"A [{V}] B {REMOVED_MARKER} C {CONFLICTED_MARKER} D [mid: {V}] E [{VX}] F "
        f"{REMOVED_MARKER} G {REMOVED_MARKER}"
    )
    # Nothing invalid is left: the shown text's citations are the valid ones only.
    assert set(find_citations(out)) == {V, VX}


ANSWER = (
    f"Paid [{V}] and 【mid: {VX}】; not [{H}] nor 【{C}】 or [ {HALLUCINATED_X} ]. "
    f"Mixed [{V}】 stays, [x-{H}] goes, end [{V}"
)
ANSWER_SHOWN = (
    f"Paid [{V}] and 【mid: {VX}】; not {REMOVED_MARKER} nor {CONFLICTED_MARKER} or "
    f"{REMOVED_MARKER}. Mixed [{V}】 stays, {REMOVED_MARKER} goes, end [{V}"
)


def test_every_split_into_two_deltas_shows_no_invalid_citation() -> None:
    whole, ref = filtered(ANSWER)
    assert whole == ANSWER_SHOWN
    for i in range(len(ANSWER) + 1):
        out, f = filtered(ANSWER[:i], ANSWER[i:])
        assert out == ANSWER_SHOWN, i
        assert set(find_citations(out)) <= {V, VX}, i
        assert (f.raw, f.invalid, f.conflicted, f.citations) == (
            ref.raw,
            ref.invalid,
            ref.conflicted,
            ref.citations,
        ), i


def test_one_character_deltas_show_no_invalid_citation() -> None:
    f = CitationFilter(judge)
    shown = [f.feed(ch) for ch in ANSWER]
    shown.append(f.flush())
    assert "".join(shown) == ANSWER_SHOWN
    # Every prefix the user could have seen holds no invalid citation.
    for k in range(len(shown) + 1):
        assert set(find_citations("".join(shown[:k]))) <= {V, VX}


def test_a_span_is_held_until_it_closes_or_dies() -> None:
    f = CitationFilter(judge)
    assert f.feed(f"see [{H[:30]}") == "see " and f.holding
    assert f.feed(H[30:]) == "" and f.holding
    assert f.feed("]!") == f"{REMOVED_MARKER}!" and not f.holding
    assert f.feed("[ mid") == "" and f.holding
    assert f.feed("x") == "[ midx" and not f.holding
    assert f.feed("[ x") == "" and f.feed("y") == "[ xy"
    # A held span at the end of a response is released unchanged; discard drops it.
    assert f.feed(f"[{V[:10]}") == "" and f.flush() == f"[{V[:10]}" and not f.holding
    assert f.feed(f"[{V[:10]}") == ""
    f.discard()
    assert not f.holding and f.flush() == ""


def test_the_judge_runs_when_the_span_closes() -> None:
    seen: list[str] = []

    def spy(ref: str) -> Ruling:
        seen.append(ref)
        return ("valid", ref)

    f = CitationFilter(spy)
    f.feed(f"[{V}")
    assert seen == []
    f.feed("]")
    assert seen == [V]


# ---- through the agent loop -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hallucinated_mid_is_stripped(env: Env) -> None:
    answer = f"Invoice [{SINGLE}] and reminder 【{REMINDER}】, also [{HALLUCINATED}]."
    fake = steps(
        ScriptStep(tool_calls=[search_call()]),
        ScriptStep(content=answer, split_at=[30, 100]),
        # The history holds what the user saw: the hallucinated mid never goes back to the model.
        ScriptStep(
            content="ok", expect=FakeExpect(contains=[REMOVED_MARKER], absent=[HALLUCINATED])
        ),
    )
    session = env.session(fake)
    deltas, turn = await run(session)
    expected = f"Invoice [{SINGLE}] and reminder 【{REMINDER}】, also {REMOVED_MARKER}."
    assert turn.outcome == "ok" and turn.text == expected and "".join(deltas) == expected
    assert turn.citations == (SINGLE, REMINDER)
    m = turn.meta
    assert (m.raw_citations, m.invalid_citations, m.conflicted_citations) == (3, 1, 0)
    _, second = await run(session)
    assert second.outcome == "ok" and second.citations == ()
    fake.assert_done()


@pytest.mark.asyncio
async def test_a_conflicted_mid_is_stripped_even_when_a_tool_returned_it(env: Env) -> None:
    row = env.store.get_message(CONFLICTED)
    assert row is not None and row.conflicted  # the adversarial pair (T14)
    fake = steps(
        ScriptStep(tool_calls=[mids_call(CONFLICTED, SINGLE)]),
        ScriptStep(content=f"Pay per [{CONFLICTED}] or 【mid:{CONFLICTED}】, see [{SINGLE}]."),
    )
    deltas, turn = await run(env.session(fake, registry=registry(env)))
    expected = f"Pay per {CONFLICTED_MARKER} or {CONFLICTED_MARKER}, see [{SINGLE}]."
    assert turn.text == expected and "".join(deltas) == expected
    assert turn.citations == (SINGLE,)
    m = turn.meta
    assert (m.raw_citations, m.invalid_citations, m.conflicted_citations) == (3, 2, 2)
    fake.assert_done()


@pytest.mark.asyncio
async def test_a_mid_returned_in_turn_1_and_cited_in_turn_2_is_stripped(env: Env) -> None:
    fake = steps(
        ScriptStep(tool_calls=[search_call()]),
        ScriptStep(content=f"It is [{SINGLE}]."),
        ScriptStep(content=f"As I said, [{SINGLE}]."),
    )
    session = env.session(fake)
    _, first = await run(session)
    assert first.text == f"It is [{SINGLE}]." and first.citations == (SINGLE,)
    deltas, second = await run(session)
    assert second.text == f"As I said, {REMOVED_MARKER}." and "".join(deltas) == second.text
    assert second.citations == ()
    m = second.meta
    assert (m.raw_citations, m.invalid_citations, m.conflicted_citations) == (1, 1, 0)
    fake.assert_done()


@pytest.mark.asyncio
async def test_a_mismatched_pair_is_not_a_citation(env: Env) -> None:
    answer = f"See [{SINGLE}】 and 【{HALLUCINATED}]."
    fake = steps(ScriptStep(tool_calls=[search_call()]), ScriptStep(content=answer))
    _, turn = await run(env.session(fake))
    assert turn.text == answer and turn.citations == ()
    m = turn.meta
    assert (m.raw_citations, m.invalid_citations, m.conflicted_citations) == (0, 0, 0)


@pytest.mark.asyncio
async def test_x_mids_are_parsed_and_validated(env: Env) -> None:
    xm = x_mid(env.store)
    assert len(xm) == 66
    fake = steps(
        ScriptStep(tool_calls=[mids_call(xm)]),
        ScriptStep(content=f"Scan [{xm}] 【mid: {xm}】, not [{HALLUCINATED_X}]."),
    )
    _, turn = await run(env.session(fake, registry=registry(env)))
    assert turn.text == f"Scan [{xm}] 【mid: {xm}】, not {REMOVED_MARKER}."
    assert turn.citations == (xm,)
    m = turn.meta
    assert (m.raw_citations, m.invalid_citations, m.conflicted_citations) == (3, 1, 0)


@pytest.mark.asyncio
async def test_a_returned_mid_that_is_not_indexed_is_stripped(env: Env) -> None:
    assert env.store.get_message(HALLUCINATED) is None
    fake = steps(
        ScriptStep(tool_calls=[mids_call(HALLUCINATED)]),
        ScriptStep(content=f"Here [{HALLUCINATED}]."),
    )
    _, turn = await run(env.session(fake, registry=registry(env)))
    assert turn.text == f"Here {REMOVED_MARKER}." and turn.citations == ()
    assert (turn.meta.raw_citations, turn.meta.invalid_citations) == (1, 1)


@pytest.mark.asyncio
async def test_a_citation_before_the_tool_ran_is_stripped(env: Env) -> None:
    # Answer text streamed with the tool call, before its result exists this turn.
    fake = steps(
        ScriptStep(tool_calls=[search_call()], lead_content=f"Maybe [{SINGLE}]. "),
        ScriptStep(content=f"Yes [{SINGLE}]."),
    )
    _, turn = await run(env.session(fake))
    assert turn.text == f"Maybe {REMOVED_MARKER}. Yes [{SINGLE}]."
    assert turn.citations == (SINGLE,)
    assert (turn.meta.raw_citations, turn.meta.invalid_citations) == (2, 1)


@pytest.mark.asyncio
async def test_a_hallucinated_citation_split_across_two_responses_is_judged(env: Env) -> None:
    # The span opens in the text streamed with the tool call and closes in the next response.
    fake = steps(
        ScriptStep(tool_calls=[search_call()], lead_content=f"See [{HALLUCINATED}"),
        ScriptStep(content="] done."),
        # The history holds what the user saw: the hallucinated mid never goes back.
        ScriptStep(
            content="ok", expect=FakeExpect(contains=[REMOVED_MARKER], absent=[HALLUCINATED])
        ),
    )
    session = env.session(fake)
    deltas, turn = await run(session)
    visible = "".join(deltas)
    assert turn.outcome == "ok" and turn.text == f"See {REMOVED_MARKER} done." == visible
    assert find_citations(visible) == [] and turn.citations == ()
    m = turn.meta
    assert (m.raw_citations, m.invalid_citations, m.conflicted_citations) == (1, 1, 0)
    _, second = await run(session)
    assert second.outcome == "ok"
    fake.assert_done()


@pytest.mark.asyncio
async def test_a_citation_split_across_a_tool_call_is_judged_when_it_closes(env: Env) -> None:
    reg = registry(env)
    fake = steps(
        ScriptStep(tool_calls=[search_call()], lead_content=f"See [mid: {SINGLE[:10]}"),
        ScriptStep(
            tool_calls=[mids_call(CONFLICTED)],
            lead_content=f"{SINGLE[10:]}] and [{CONFLICTED[:5]}",
        ),
        ScriptStep(content=f"{CONFLICTED[5:]}] end", split_at=[7]),
    )
    deltas, turn = await run(env.session(fake, registry=reg))
    expected = f"See [mid: {SINGLE}] and {CONFLICTED_MARKER} end"
    assert turn.outcome == "ok" and turn.text == expected == "".join(deltas)
    assert turn.citations == (SINGLE,)
    m = turn.meta
    assert (m.raw_citations, m.invalid_citations, m.conflicted_citations) == (2, 1, 1)
    fake.assert_done()


@pytest.mark.asyncio
async def test_a_span_held_across_a_tool_call_dies_on_the_next_response(env: Env) -> None:
    fake = steps(
        ScriptStep(tool_calls=[search_call()], lead_content=f"Hmm [{SINGLE[:10]}"),
        ScriptStep(content="Found it."),
    )
    deltas, turn = await run(env.session(fake))
    assert turn.text == f"Hmm [{SINGLE[:10]}Found it." == "".join(deltas)
    assert turn.meta.raw_citations == 0


@pytest.mark.asyncio
async def test_a_span_held_across_a_tool_call_is_dropped_when_the_turn_fails(env: Env) -> None:
    fake = steps(
        ScriptStep(tool_calls=[search_call()], lead_content=f"See [{HALLUCINATED}"),
        ScriptStep(http_status=500),
    )
    deltas, turn = await run(env.session(fake))
    assert turn.outcome == "llm_unavailable"  # an HTTP 5xx
    assert turn.text == "See " == "".join(deltas) and turn.meta.raw_citations == 0


@pytest.mark.asyncio
async def test_a_mid_from_a_trusted_tool_result_is_a_valid_citation(env: Env) -> None:
    fake = steps(
        ScriptStep(
            tool_calls=[FakeToolCall(name="dummy_trusted_mids", arguments={"mids": [SINGLE]})]
        ),
        ScriptStep(content=f"See [{SINGLE}]."),
    )
    reg = registry_with(env, dummy_trusted_mids)
    _, turn = await run(env.session(fake, registry=reg))
    assert turn.text == f"See [{SINGLE}]." and turn.citations == (SINGLE,)
    assert (turn.meta.raw_citations, turn.meta.invalid_citations) == (1, 0)
    fake.assert_done()


@pytest.mark.asyncio
async def test_a_mid_returned_under_another_key_is_not_a_valid_citation(env: Env) -> None:
    fake = steps(
        ScriptStep(tool_calls=[FakeToolCall(name="dummy_mid_list", arguments={"mids": [SINGLE]})]),
        ScriptStep(content=f"See [{SINGLE}]."),
    )
    reg = registry_with(env, dummy_mid_list)
    _, turn = await run(env.session(fake, registry=reg))
    assert turn.text == f"See {REMOVED_MARKER}." and turn.citations == ()
    assert (turn.meta.raw_citations, turn.meta.invalid_citations) == (1, 1)
    fake.assert_done()


@pytest.mark.asyncio
async def test_an_unclosed_span_at_the_end_of_a_response_is_shown_unchanged(env: Env) -> None:
    fake = steps(ScriptStep(content=f"Cut [{SINGLE[:20]}"))
    deltas, turn = await run(env.session(fake))
    assert turn.text == f"Cut [{SINGLE[:20]}" and "".join(deltas) == turn.text
    assert turn.meta.raw_citations == 0


@pytest.mark.asyncio
async def test_a_held_span_is_not_shown_after_a_cancel(env: Env) -> None:
    fake = steps(ScriptStep(content=f"Look [{HALLUCINATED}] here", split_at=[5, 30], pause_after=3))
    handle = env.session(fake).start_turn("question")
    seen: list[str] = []

    async def consume() -> None:
        async for d in handle:
            seen.append(d)

    consumer = asyncio.create_task(consume())
    async with asyncio.timeout(10):
        await fake.paused.wait()
    await asyncio.sleep(0)
    handle.cancel()
    await consumer
    turn = await handle.result()
    assert turn.outcome == "cancelled" and turn.text == "Look " and "".join(seen) == "Look "
    assert turn.meta.raw_citations == 0


@pytest.mark.asyncio
async def test_first_visible_time_counts_only_shown_text(env: Env) -> None:
    now = [1.0]
    fake = steps(ScriptStep(content=f"[{SINGLE[:40]}|", split_at=[20], pause_after=2))
    handle = env.session(fake, clock=lambda: now[0]).start_turn("question")
    deltas: list[str] = []

    async def consume() -> None:
        async for d in handle:
            deltas.append(d)

    consumer = asyncio.create_task(consume())
    async with asyncio.timeout(10):
        await fake.paused.wait()
    await asyncio.sleep(0)
    assert deltas == []  # the first delta is a held span: nothing visible yet
    now[0] = 5.0
    fake.resume()
    await consumer
    turn = await handle.result()
    # Visible only once the span died at "|", in the second delta.
    assert turn.text == f"[{SINGLE[:40]}|" and turn.meta.t_first_visible == 5.0


@pytest.mark.asyncio
async def test_every_split_position_through_the_loop_shows_no_invalid_citation(env: Env) -> None:
    xm = x_mid(env.store)
    answer = f"A [{SINGLE}] B 【{CONFLICTED}】 C [mid: {HALLUCINATED}] D 【{xm}】 E [{SINGLE}】"
    shown = f"A [{SINGLE}] B {CONFLICTED_MARKER} C {REMOVED_MARKER} D 【{xm}】 E [{SINGLE}】"
    reg = registry(env)
    for i in range(1, len(answer)):
        fake = steps(
            ScriptStep(tool_calls=[mids_call(SINGLE, CONFLICTED, xm)]),
            ScriptStep(content=answer, split_at=[i]),
        )
        deltas, turn = await run(env.session(fake, registry=reg))
        visible = "".join(deltas)
        assert visible == shown and turn.text == shown, i
        assert set(find_citations(visible)) <= {SINGLE, xm}, i
        assert turn.citations == (SINGLE, xm), i
        m = turn.meta
        assert (m.raw_citations, m.invalid_citations, m.conflicted_citations) == (4, 2, 1), i


# ---- short ids (plan 08c step 3) --------------------------------------------------------------

#: The index rows the short-id unit tests resolve against: mid -> conflicted.
ROWS: dict[str, bool] = {V: False, C: True, VX: False, H: False}


def prefix_judge(returned: set[str]) -> Judge:
    """A judge applying ``resolve`` over :data:`ROWS` (what ``AgentSession._judge`` does over
    the index)."""

    def judge(ref: str) -> Ruling:
        norm = normalize_ref(ref)
        assert norm is not None
        rows = sorted((m, c) for m, c in ROWS.items() if m.startswith(norm))[:2]
        return resolve(norm, returned, rows)

    return judge


def test_the_input_grammar_takes_short_ids_and_prefixes_and_emits_full_mids() -> None:
    returned = {short_id(V), short_id(VX), short_id(C)}
    f = CitationFilter(prefix_judge(returned))
    text = (
        f"A [{V[:12]}] B 【mid: {V[:12].upper()}】 C [ {VX[:14]} ] D [{V[:63]}] E [{V[:8]}] "
        f"F [{V[:7]}] G [{C[:12]}] H [{H[:12]}] I [{V}]"
    )
    out = f.feed(text) + f.flush()
    assert out == (
        f"A [{V}] B 【mid: {V}】 C [ {VX} ] D [{V}] E [{V}] F [{V[:7]}] G {CONFLICTED_MARKER} "
        f"H {REMOVED_MARKER} I [{V}]"
    )
    assert f.citations == (V, VX)
    assert (f.raw, f.invalid, f.conflicted) == (8, 2, 1)
    # What leaves the filter is the output grammar the chat tab renders as chips (full mids);
    # the model's short form is not a citation there.
    assert find_citations(out) == [V, V, VX, V, V, V]
    assert find_citations(f"[{V[:12]}] [{V[:63]}]") == []


def test_resolve_is_valid_only_for_one_returned_indexed_unconflicted_mid() -> None:
    v12, vx12 = short_id(V), short_id(VX)
    v2 = V[:12] + "b" * 52  # shares V's short id
    rows_v = [(V, False)]
    assert resolve(v12, {v12}, rows_v) == ("valid", V)
    assert resolve(V, {v12}, rows_v) == ("valid", V)  # a full mid cited, a short id returned
    assert resolve(V[:8], {V}, rows_v) == ("valid", V)  # a prefix cited, the full mid returned
    assert resolve(vx12, {vx12}, [(VX, False)]) == ("valid", VX)
    assert resolve(v12, set(), rows_v) == ("invalid", None)  # not returned this turn
    assert resolve(v12, {short_id(H)}, rows_v) == ("invalid", None)
    assert resolve(v12, {v12}, []) == ("invalid", None)  # not indexed
    # An ambiguous prefix in the index is never resolved by a guess.
    assert resolve(v12, {v12}, [(V, False), (v2, False)]) == ("invalid", None)
    # Two returned ids that match the reference but name different messages.
    assert resolve(V[:8], {V, v2}, rows_v) == ("invalid", None)
    # Conflicted wins, returned or not (T14).
    assert resolve(short_id(C), {short_id(C)}, [(C, True)]) == ("conflicted", None)
    assert resolve(short_id(C), set(), [(C, True)]) == ("conflicted", None)
    with pytest.raises(ValueError, match="normalized"):
        resolve(V[:12].upper(), {v12}, rows_v)
    with pytest.raises(ValueError, match="full mid"):
        resolve(v12, {v12}, [(H, False)])


def test_a_valid_ruling_without_a_full_mid_is_a_bug() -> None:
    f = CitationFilter(lambda ref: ("valid", ref))
    with pytest.raises(ValueError, match="full mid"):
        f.feed(f"[{V[:12]}]")


@pytest.mark.asyncio
async def test_a_short_id_a_tool_returned_is_cited_as_its_full_mid(env: Env) -> None:
    xm = x_mid(env.store)
    fake = steps(
        ScriptStep(tool_calls=[mids_call(short_id(SINGLE), short_id(xm))]),
        ScriptStep(
            content=f"Paid [{short_id(SINGLE)}], scan 【{short_id(xm)}】, see [{SINGLE[:9]}].",
            split_at=[8, 20],
        ),
    )
    session = env.session(fake, registry=registry(env))
    deltas, turn = await run(session)
    expected = f"Paid [{SINGLE}], scan 【{xm}】, see [{SINGLE}]."
    assert turn.outcome == "ok" and turn.text == expected == "".join(deltas)
    assert turn.citations == (SINGLE, xm)
    assert find_citations(turn.text) == [SINGLE, xm, SINGLE]  # what the chat tab chips
    m = turn.meta
    assert (m.raw_citations, m.invalid_citations, m.conflicted_citations) == (3, 0, 0)
    # The provenance of the session names the full mids the short ids stand for.
    assert session.provenance() == (SINGLE, xm)
    fake.assert_done()


@pytest.mark.asyncio
async def test_a_short_id_of_a_full_mid_a_tool_returned_is_valid(env: Env) -> None:
    fake = steps(
        ScriptStep(tool_calls=[search_call()]),
        ScriptStep(content=f"It is [{short_id(SINGLE)}]."),
    )
    _, turn = await run(env.session(fake))
    assert turn.text == f"It is [{SINGLE}]." and turn.citations == (SINGLE,)
    fake.assert_done()


@pytest.mark.asyncio
async def test_short_ids_not_returned_conflicted_or_unknown_are_stripped(env: Env) -> None:
    fake = steps(
        ScriptStep(tool_calls=[mids_call(short_id(CONFLICTED), short_id(HALLUCINATED))]),
        ScriptStep(
            content=(
                f"A [{short_id(REMINDER)}] B [{short_id(CONFLICTED)}] "
                f"C [{short_id(HALLUCINATED)}] D [{short_id(SINGLE)}]"
            )
        ),
    )
    _, turn = await run(env.session(fake, registry=registry(env)))
    r = REMOVED_MARKER
    c = CONFLICTED_MARKER
    assert turn.text == f"A {r} B {c} C {r} D {r}" and turn.citations == ()
    m = turn.meta
    assert (m.raw_citations, m.invalid_citations, m.conflicted_citations) == (4, 4, 1)
    fake.assert_done()
