"""Reciprocal rank fusion over mids (decisions #17, plan 05 step 1).

Each arm of hybrid search (FTS5 BM25, sqlite-vec KNN) ranks chunks. ``collapse`` turns a chunk
ranking into a mid ranking (a mid keeps the rank of its best chunk), so one message with many
matching chunks occupies one slot. ``rrf`` then fuses the mid rankings:

    score(mid) = sum over rankings containing mid of 1 / (k + rank),   rank 1-based

and sorts by (-score, mid), so ties are broken by mid and the order never depends on input
order beyond the ranks themselves. Results are distinct mids, as ``recall_at_10`` (architecture
§9a) and the smoke ``predict`` contract need.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

DEFAULT_K = 60


def collapse(chunk_hits: Iterable[str]) -> list[str]:
    """Return the mids of a chunk ranking in order of first appearance.

    ``chunk_hits`` holds the mid of each chunk hit, best chunk first. A mid appearing again
    further down (another chunk of the same message) is dropped, so the result is a ranking of
    distinct mids where each mid has its best chunk's position.
    """
    seen: set[str] = set()
    out: list[str] = []
    for mid in chunk_hits:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def rrf(rankings: Sequence[Sequence[str]], k: int = DEFAULT_K) -> list[tuple[str, float]]:
    """Fuse rankings of distinct mids (best first) with reciprocal rank fusion.

    Returns ``(mid, score)`` pairs sorted by (-score, mid). Raises ``ValueError`` when ``k`` is
    not a non-negative int or a ranking repeats a mid (collapse chunk rankings first), and
    ``TypeError`` when ``rankings`` or a ranking is a bare ``str`` (a str is a ``Sequence[str]``
    and would otherwise be split into single-character mids).
    """
    if isinstance(k, bool) or not isinstance(k, int) or k < 0:
        raise ValueError(f"rrf: k must be a non-negative int, got {k!r}")
    if isinstance(rankings, str):
        raise TypeError("rrf: rankings must be a sequence of rankings, not one str")
    contributions: dict[str, list[float]] = {}
    for index, ranking in enumerate(rankings):
        if isinstance(ranking, str):
            raise TypeError(f"rrf: ranking {index} is a str, not a sequence of mids")
        if len(set(ranking)) != len(ranking):
            raise ValueError(f"rrf: ranking {index} repeats a mid; collapse chunk hits first")
        for rank, mid in enumerate(ranking, start=1):
            contributions.setdefault(mid, []).append(1.0 / (k + rank))
    fused = [(mid, math.fsum(parts)) for mid, parts in contributions.items()]
    fused.sort(key=lambda pair: (-pair[1], pair[0]))
    return fused
