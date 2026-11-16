"""The FTS5 arm's query builder (plan 05 step 5).

A user query is never handed to ``MATCH`` as written: FTS5's query syntax gives meaning to
``-``, ``:``, ``(``, ``"``, ``AND``/``OR``/``NOT``, ``*`` and more, so ``INV-0042``,
``bob@example.com``, ``who's`` or ``Acme AND`` would raise a syntax error or silently mean
something else. :func:`build_match` turns the query into a plain disjunction of phrases:

- :func:`tern.index.chunks.fold` it (the same fold as ``chunks.fts_text``, so a Greek tonos or
  a Latin accent in the query matches the folded index);
- replace control characters with spaces (a NUL inside an FTS5 string ends it: "unterminated
  string"), then split on whitespace;
- keep the runs that contain a word character (``\\w``); punctuation-only runs carry nothing;
- put each run in double quotes, doubling any ``"`` inside, so FTS5 reads it as one phrase of
  the tokens its tokenizer finds there (``"inv-0042"`` is the phrase ``inv 0042``) and an
  operator word (``"not"``) is a plain term;
- join the phrases with `` OR ``. No prefix matching.

The result is bound as a parameter (``MATCH ?``), never formatted into SQL. A query with no
run left gives None (the caller raises ``EmptyQuery``).
"""

from __future__ import annotations

import re
import unicodedata

from tern.index.chunks import fold

_WORD_CHAR = re.compile(r"\w")


def _controls_to_space(text: str) -> str:
    return "".join(" " if unicodedata.category(c) == "Cc" else c for c in text)


def build_match(query: str) -> str | None:
    """The FTS5 ``MATCH`` expression for ``query`` (module docstring), or None when the query
    has no word character."""
    if not isinstance(query, str):
        raise TypeError("query must be a str")
    runs = _controls_to_space(fold(query)).split()
    phrases = ['"' + run.replace('"', '""') + '"' for run in runs if _WORD_CHAR.search(run)]
    if not phrases:
        return None
    return " OR ".join(phrases)
