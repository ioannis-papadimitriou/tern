"""Small stand-in for ``gen_synthetic_mailbox.py --large`` (plan 04 merge review L1).

The real large cases (a > 25 MiB message, a 150-attachment message) take ~2 s to generate and
handle, so the tests that use them are `slow`. Their fast twins lower ``RAW_MAX`` with
:func:`lower_raw_max` and use :func:`small_large_mbox`: the same two shapes, scaled to the
lowered limit, so the same over/under-limit code paths run in ``make check-fast``.
"""

from __future__ import annotations

import mailbox
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

#: The lowered RAW_MAX of the fast twins: above the many-attachments message (~40 KB).
SMALL_RAW_MAX = 64 * 1024
OVER_ID = "over-raw-max@example.test"
MANY_ID = "many-attachments@example.test"
MANY = 150


def lower_raw_max(monkeypatch: pytest.MonkeyPatch, raw_max: int = SMALL_RAW_MAX) -> int:
    """Lower RAW_MAX everywhere it is read at call time (FakeBridge, backfill, events).

    Raises AssertionError when another loaded ``tern.*`` module still holds the protocol's
    RAW_MAX after the patch (a new ``from tern.bridge.protocol import RAW_MAX`` reader): the fast
    twins would then mix the lowered limit with the real one, so the list below must grow.
    """
    from tern.bridge import protocol
    from tern.index import events, ingest
    from tern.testing import fake_bridge

    for module in (fake_bridge, ingest, events):
        monkeypatch.setattr(module, "RAW_MAX", raw_max)
    unpatched = sorted(
        name
        for name, module in list(sys.modules.items())
        if (name == "tern" or name.startswith("tern."))
        and module is not protocol
        and getattr(module, "RAW_MAX", None) == protocol.RAW_MAX
    )
    if unpatched:
        raise AssertionError(f"lower_raw_max: RAW_MAX not lowered in {unpatched}")
    return raw_max


def _message(message_id: str, date: str, subject: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "bulk@example.test"
    msg["To"] = "me@example.test"
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = f"<{message_id}>"
    msg.set_content(text)
    return msg


def small_large_mbox(path: Path, raw_max: int = SMALL_RAW_MAX) -> Path:
    """An mbox of two messages, in this order: one over ``raw_max`` (a single attachment
    ``large.bin`` larger than ``raw_max``) and one under it with 150 small attachments."""
    big = _message(OVER_ID, "Fri, 13 Nov 2026 10:00:00 +0000", "Large attachment", "Large.\n")
    payload = bytes(range(256)) * (raw_max // 256 + 8)
    big.add_attachment(
        payload, maintype="application", subtype="octet-stream", filename="large.bin"
    )
    many = _message(MANY_ID, "Fri, 13 Nov 2026 11:00:00 +0000", "Many attachments", "150.\n")
    for i in range(MANY):
        many.add_attachment(
            bytes([i]) * 16, maintype="text", subtype="plain", filename=f"file_{i:03d}.txt"
        )
    box = mailbox.mbox(path)
    try:
        box.add(big)
        box.add(many)
    finally:
        box.close()
    assert len(big.as_bytes()) > raw_max > len(many.as_bytes())
    return path
