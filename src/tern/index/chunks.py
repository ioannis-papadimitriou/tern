"""Chunks of a message for FTS5 (and plan 05's vectors): plan 04 step 4, decisions #17, #30.

One message gives, in this order, with ``ord`` counting from 0 across all of them:

- one ``subject`` chunk (none when the subject is absent or blank);
- ``body`` chunks from the main text (the caller passes ``mime.ParsedMessage.main_text``:
  quotes and signature are already stripped), split every :data:`BODY_CHUNK_WORDS` whitespace
  words (≈ 512 tokens). A chunk's text is the original span from its first word to its last, so
  line breaks inside it are kept; whitespace between two chunks belongs to neither;
- one ``attachment_name`` chunk per attachment with a non-blank filename, in MIME order.

``fts_text`` is :func:`fold` of ``text``; ``text`` stays as written. ``chunks_fts`` indexes
``fts_text`` (unicode61 with ``remove_diacritics 2`` lower-cases and folds Latin marks but not the
Greek tonos), so queries must be folded with the same :func:`fold` (plan 05).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

#: Whitespace words per body chunk, a proxy for ≈ 512 tokens (#17).
BODY_CHUNK_WORDS = 390

ChunkKind = Literal["subject", "body", "attachment_name"]

_WORD = re.compile(r"\S+")


@dataclass(frozen=True)
class Chunk:
    ord: int
    kind: ChunkKind
    text: str
    fts_text: str


def fold(text: str) -> str:
    """NFD, drop combining marks (category Mn), NFC: ``"καλημέρα"`` → ``"καλημερα"``. Used for
    ``chunks.fts_text`` and, in plan 05, for every FTS query."""
    decomposed = unicodedata.normalize("NFD", text)
    kept = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return unicodedata.normalize("NFC", kept)


def split_body(text: str, words: int = BODY_CHUNK_WORDS) -> list[str]:
    """``text`` cut into spans of at most ``words`` whitespace-separated words; empty for a
    blank text."""
    if words < 1:
        raise ValueError("words must be >= 1")
    spans = [m.span() for m in _WORD.finditer(text)]
    return [
        text[spans[i][0] : spans[min(i + words, len(spans)) - 1][1]]
        for i in range(0, len(spans), words)
    ]


def build_chunks(
    subject: str | None, main_text: str | None, attachment_names: Iterable[str | None]
) -> list[Chunk]:
    """The chunks of one message (module docstring for the rules)."""
    parts: list[tuple[ChunkKind, str]] = []
    if subject is not None and subject.strip():
        parts.append(("subject", subject))
    parts.extend(("body", span) for span in split_body(main_text or ""))
    parts.extend(
        ("attachment_name", name) for name in attachment_names if name is not None and name.strip()
    )
    return [
        Chunk(ord=i, kind=kind, text=text, fts_text=fold(text))
        for i, (kind, text) in enumerate(parts)
    ]
