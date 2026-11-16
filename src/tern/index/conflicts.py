"""Classifying how two copies of one ``mid`` differ (plan 04 step 6, D1/T14; #30).

:func:`classify_pair` is a port of ``spikes/s1/s1hash.py:classify_pair`` onto the byte rules of
``tern.index.mid.content_hash`` (S1 used a near copy of them), so the classes line up with the
hash exactly: a pair is ``identical``, ``crlf`` or ``from_quote`` if and only if both copies
have the same ``content_hash``; a pair whose hashes differ (a conflicted mid) is always
``footer``, ``date_from`` or ``other``. The Store writes the class into
``messages.conflict_class`` when it detects a conflict; it is a label for the status output and
the A-13 revisit of #5, never an input to trust.

Raw bytes only: never ``mime.py`` output, the same as identity (``tern.index.mid``). Nothing here
logs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.parser import BytesHeaderParser
from email.policy import compat32
from typing import Literal

# The content_hash rules themselves (one definition of "From address" and "Date epoch"):
# test_classify_pair_* pin that the classes still agree with content_hash.
from tern.index.mid import date_epoch, from_address, split_raw

ConflictClass = Literal["identical", "crlf", "from_quote", "footer", "date_from", "other"]

#: Mildest first (``spikes/s1/s1hash.py:CLASSES``).
CLASSES: tuple[ConflictClass, ...] = (
    "identical",
    "crlf",
    "from_quote",
    "footer",
    "date_from",
    "other",
)
#: The classes of copies with equal ``content_hash``: never a conflict.
BENIGN: frozenset[ConflictClass] = frozenset(CLASSES[:3])

# content_hash's mboxo rule: one ">" removed from lines matching ^>+From .
_FROM_QUOTED = re.compile(rb"(?m)^>(>*From )")


@dataclass(frozen=True)
class _Parts:
    meta: tuple[str, str]  # (lower-cased From address, UTC epoch of Date), "" when missing
    body: bytes  # the raw body, as split by tern.index.mid.split_raw
    lf_body: bytes  # body after CRLF -> LF (content_hash's split)
    norm_body: bytes  # lf_body with content_hash's ">From " unquoting


def _parts(raw: bytes) -> _Parts:
    lf = raw.replace(b"\r\n", b"\n")
    i = lf.find(b"\n\n")
    head, lf_body = (lf[:i], lf[i + 2 :]) if i >= 0 else (lf, b"")
    msg = BytesHeaderParser(policy=compat32).parsebytes(head + b"\n\n")
    return _Parts(
        meta=(from_address(msg.get("From")), date_epoch(msg.get("Date"))),
        body=split_raw(raw)[1],
        lf_body=lf_body,
        norm_body=_FROM_QUOTED.sub(rb"\1", lf_body),
    )


def _is_appended_block(a: bytes, b: bytes) -> bool:
    """The longer body is the shorter one plus whole extra lines at the end (S1's rule)."""
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    if not short.endswith(b"\n"):
        short += b"\n"
    return len(long_) > len(short) and long_.startswith(short)


def classify_pair(raw_a: bytes, raw_b: bytes) -> ConflictClass:
    """The mildest class that explains how copy ``b`` differs from copy ``a`` (symmetric):

    - same From address and Date: ``identical`` (raw bodies equal), ``crlf`` (equal after
      CRLF → LF), ``from_quote`` (equal after content_hash's ``>From `` unquoting), ``footer``
      (one normalized body is the other plus whole lines at the end), else ``other``;
    - From or Date differ: ``date_from`` when the normalized bodies are equal, else ``other``.
    """
    a, b = _parts(raw_a), _parts(raw_b)
    if a.meta != b.meta:
        return "date_from" if a.norm_body == b.norm_body else "other"
    if a.norm_body != b.norm_body:
        # Checked first so that benign <=> equal content_hash holds even for odd line endings
        # where the raw split and content_hash's split disagree.
        return "footer" if _is_appended_block(a.norm_body, b.norm_body) else "other"
    if a.body == b.body:
        return "identical"
    if a.lf_body == b.lf_body:
        return "crlf"
    return "from_quote"


def worst(classes: list[ConflictClass]) -> ConflictClass:
    """The least benign of ``classes`` (non-empty), in :data:`CLASSES` order."""
    return max(classes, key=CLASSES.index)
