"""tern.tools.ids: the short-id codec (plan 08c)."""

import pytest

from tern.tools import ids

FULL = "ca20f5b069f8a8eec269b0e50cbf82cda8896811ab89e1eed85f993b8ebf732e"
XFULL = "x-" + FULL


def test_short_id_takes_twelve_hex_and_keeps_the_x_prefix() -> None:
    assert ids.short_id(FULL) == "ca20f5b069f8"
    assert ids.short_id(XFULL) == "x-ca20f5b069f8"


@pytest.mark.parametrize("bad", ["", "abc", FULL[:-1], FULL.upper(), "y-" + FULL])
def test_short_id_refuses_anything_but_a_full_mid(bad: str) -> None:
    with pytest.raises(ValueError):
        ids.short_id(bad)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ca20f5b069f8", "ca20f5b069f8"),
        ("[ca20f5b069f8]", "ca20f5b069f8"),
        ("【CA20F5B069F8】", "ca20f5b069f8"),
        (" ca20f5b0…", "ca20f5b0"),
        ("x-ca20f5b069f8", "x-ca20f5b069f8"),
        (FULL, FULL),
    ],
)
def test_normalize_ref_cleans_what_models_wrap_around_an_id(raw: str, expected: str) -> None:
    assert ids.normalize_ref(raw) == expected


@pytest.mark.parametrize("bad", ["ca20f5b", "zz20f5b069f8", "", "ca20 f5b069f8", FULL + "0"])
def test_normalize_ref_refuses_short_or_non_hex(bad: str) -> None:
    assert ids.normalize_ref(bad) is None


def test_prefix_range_brackets_exactly_the_mids_with_that_prefix() -> None:
    low, high = ids.prefix_range("ca20f5b069f8")
    assert low <= FULL < high
    assert not (low <= "ca20f5b069f9" + "0" * 52 < high)
    low9, high9 = ids.prefix_range("ca20f5b069")
    assert high9 == "ca20f5b06:"
    assert low9 <= "ca20f5b069ff" + "0" * 52 < high9
