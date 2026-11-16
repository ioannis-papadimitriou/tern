"""Plan 05 step 1: reciprocal rank fusion over mids (decisions #17).

Expected scores are hand-computed from score = sum of 1 / (k + rank), rank 1-based.
"""

from __future__ import annotations

import itertools

import pytest

from tern.search.rrf import DEFAULT_K, collapse, rrf


def test_default_k_is_60() -> None:
    assert DEFAULT_K == 60
    # rank 1 alone with the default k: 1/61
    assert rrf([["a"]]) == [("a", pytest.approx(1 / 61, abs=0, rel=1e-15))]


def test_hand_computed_rank_1_plus_rank_3() -> None:
    # "m1" is rank 1 in the first arm and rank 3 in the second: 1/61 + 1/63 = 0.0322664...
    fused = dict(rrf([["m1", "m2", "m3"], ["m4", "m5", "m1"]]))
    assert 0.0322664 <= fused["m1"] < 0.0322665  # 124/3843 = 0.03226645849...
    assert fused["m1"] == pytest.approx(124 / 3843, rel=1e-15)
    assert fused["m2"] == pytest.approx(1 / 62, rel=1e-15)
    assert fused["m4"] == pytest.approx(1 / 61, rel=1e-15)


def test_sorted_by_descending_score() -> None:
    fused = rrf([["a", "b", "c"], ["c", "b", "a"], ["b"]])
    # a: 1/61 + 1/63, b: 1/62 + 1/62 + 1/61, c: 1/63 + 1/61
    assert [mid for mid, _ in fused] == ["b", "a", "c"]
    scores = [score for _, score in fused]
    assert scores == sorted(scores, reverse=True)


def test_ties_broken_by_mid() -> None:
    # "zz" and "aa" swap ranks across the two arms, so their scores are equal: mid decides.
    fused = rrf([["zz", "aa"], ["aa", "zz"]])
    assert fused[0][1] == fused[1][1]
    assert [mid for mid, _ in fused] == ["aa", "zz"]
    # single-arm ties (same rank in different arms) are also ordered by mid
    fused = rrf([["m9"], ["m3"], ["m5"]])
    assert [mid for mid, _ in fused] == ["m3", "m5", "m9"]


def test_order_of_rankings_does_not_change_result() -> None:
    rankings = [["a", "b", "c", "d"], ["d", "c"], ["b", "e", "a"]]
    expected = rrf(rankings)
    for perm in itertools.permutations(rankings):
        assert rrf(list(perm)) == expected


def test_one_mid_with_five_chunks_occupies_one_slot() -> None:
    # chunk ranking from one arm: mid "x" owns chunks at positions 1, 2, 4, 5, 7
    chunk_hits = ["x", "x", "y", "x", "x", "z", "x"]
    mids = collapse(chunk_hits)
    assert mids == ["x", "y", "z"]
    fused = rrf([mids])
    assert [mid for mid, _ in fused] == ["x", "y", "z"]
    # "y" keeps the rank of its first appearance among distinct mids (2), not its chunk rank (3)
    assert dict(fused)["y"] == pytest.approx(1 / 62, rel=1e-15)
    assert dict(fused)["x"] == pytest.approx(1 / 61, rel=1e-15)


def test_collapse_keeps_first_appearance_order() -> None:
    assert collapse([]) == []
    assert collapse(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
    assert collapse(iter(["q", "q"])) == ["q"]


def test_empty_inputs() -> None:
    assert rrf([]) == []
    assert rrf([[], []]) == []
    assert rrf([[], ["a"]]) == [("a", pytest.approx(1 / 61, rel=1e-15))]


def test_custom_k() -> None:
    fused = dict(rrf([["a", "b"]], k=0))
    assert fused == {"a": 1.0, "b": 0.5}


def test_duplicate_mid_in_a_ranking_raises() -> None:
    with pytest.raises(ValueError, match="repeats a mid"):
        rrf([["a", "b", "a"]])


@pytest.mark.parametrize("bad_k", [-1, 1.5, True, "60", None])
def test_bad_k_raises(bad_k: object) -> None:
    with pytest.raises(ValueError, match="k must be a non-negative int"):
        rrf([["a"]], k=bad_k)  # type: ignore[arg-type]  # deliberately wrong type under test


@pytest.mark.parametrize(
    "bad",
    [
        "abc",  # rankings itself is a str
        ["abc"],  # one ranking is a str: would become mids "a", "b", "c"
        ["aa"],
        [["m1"], "m2"],
        ("xy",),
    ],
)
def test_a_str_ranking_raises_type_error(bad: object) -> None:
    with pytest.raises(TypeError, match="str"):
        rrf(bad)  # type: ignore[arg-type]  # deliberately wrong type under test


@pytest.mark.parametrize("bad", ["", "abc"])
def test_rankings_as_one_str_raises_type_error(bad: str) -> None:
    # "" would otherwise fuse to [] silently: zero rankings, no error
    with pytest.raises(TypeError, match="rankings must be a sequence of rankings"):
        rrf(bad)
