"""Plan 04 step 2: #28 short hashes equal MANIFEST; widening on collision."""

from __future__ import annotations

import hashlib
import itertools

import pytest
import test_fixture_manifest as tfm

from tern.index.shorthash import (
    ShortHashCollision,
    account_hash,
    assign,
    folder_hash,
    folder_name,
)


def test_hashes_equal_manifest_for_every_account_and_folder() -> None:
    man = tfm.load_manifest(tfm.FIXTURE)
    accounts = man["accounts"]
    folders = [(a, f) for a in accounts for f in a["folders"]]
    assert (len(accounts), len(folders)) == (3, 10)
    for a in accounts:
        assert account_hash(a["name"]) == a["hash"]
    for a, f in folders:
        assert folder_hash(a["name"], f["path"]) == f["hash"]
        # TB paths carry a leading '/', FakeBridge's do not: both hash the same.
        assert folder_hash(a["name"], "/" + f["path"]) == f["hash"]
    acct_map = assign([a["name"] for a in accounts], "acct")
    fold_map = assign([folder_name(a["name"], f["path"]) for a, f in folders], "fold")
    assert {acct_map[a["name"]] for a in accounts} == {a["hash"] for a in accounts}
    assert [fold_map[folder_name(a["name"], "/" + f["path"])] for a, f in folders] == [
        f["hash"] for _, f in folders
    ]


def test_hash_is_sha256_prefix_of_utf8_name() -> None:
    name = "λογαριασμός@example.test"
    assert account_hash(name) == "acct-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:4]
    assert folder_hash(name, "/Α/Β") == (
        "fold-" + hashlib.sha256(f"{name}/Α/Β".encode()).hexdigest()[:4]
    )


def _colliding_pair(width: int) -> tuple[str, str]:
    seen: dict[str, str] = {}
    for i in itertools.count():
        name = f"n{i}"
        h = hashlib.sha256(name.encode()).hexdigest()[:width]
        if h in seen:
            return seen[h], name
        seen[h] = name
    raise AssertionError("unreachable")


def test_assign_widens_the_whole_set_on_a_4_hex_collision() -> None:
    a, b = _colliding_pair(4)
    other = "unrelated@example.test"
    out = assign([a, b, other, a], "fold")
    assert len(out) == 3
    assert all(len(v) == len("fold-") + 6 for v in out.values())
    assert out[other] == "fold-" + hashlib.sha256(other.encode()).hexdigest()[:6]
    assert len(set(out.values())) == 3
    # Without a collision the set stays at 4 hex, duplicates of one name are not a collision.
    assert assign([a, other, other], "acct") == {
        a: account_hash(a),
        other: account_hash(other),
    }


def test_assign_raises_when_6_hex_still_collide() -> None:
    a, b = _colliding_pair(6)
    with pytest.raises(ShortHashCollision):
        assign([a, b], "acct")
