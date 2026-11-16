"""The fast twins' RAW_MAX patch stays complete (plan 04 merge review L1, review round 1)."""

from __future__ import annotations

import sys
import types

import pytest
from large_mail import SMALL_RAW_MAX, lower_raw_max

from tern.bridge import protocol


def test_lower_raw_max_lowers_every_loaded_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    from tern.index import events, ingest
    from tern.testing import fake_bridge

    assert lower_raw_max(monkeypatch) == SMALL_RAW_MAX
    assert fake_bridge.RAW_MAX == ingest.RAW_MAX == events.RAW_MAX == SMALL_RAW_MAX
    assert protocol.RAW_MAX == 25 * 1024 * 1024


def test_lower_raw_max_fails_on_a_reader_it_does_not_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A later module doing `from tern.bridge.protocol import RAW_MAX` would keep the real limit
    # in the fast twins; the helper must refuse instead of letting the twins mix the two.
    reader = types.ModuleType("tern.index._new_raw_reader")
    vars(reader)["RAW_MAX"] = protocol.RAW_MAX
    monkeypatch.setitem(sys.modules, reader.__name__, reader)
    with pytest.raises(AssertionError, match=r"tern\.index\._new_raw_reader"):
        lower_raw_max(monkeypatch)
