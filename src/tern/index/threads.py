"""Threads: JWZ steps 1-4 without subject merging (plan 04 step 5, architecture §4).

``thread_id`` is a display aid only: References and In-Reply-To are sender-controlled, so thread
membership never grants trust (it never makes a recipient known, decisions #7, T15).

Keys. Every stored message is a node keyed by its ``mid``; a referenced id is keyed by
``sha256(normalize_message_id(id))``, the same function that gives a Message-ID mid (#5), so a
reference to a stored message names its mid. A message's *links* (:func:`message_links`) are its
References ids in order, or ``[In-Reply-To]`` when it has no References (JWZ), de-duplicated
(first occurrence kept) and without its own key.

Containers and parents (JWZ step 1). The containers are the stored messages plus every key in
some stored message's links (an *empty* container: a message not in the index). The result is a
pure function of the set of stored messages, so it never depends on ingest order:

- a stored message with links: its parent is the last of them (JWZ 1C), whatever other
  messages' References say;
- an empty container, or a stored message with no links of its own (JWZ 1A gives both their
  parent from other messages' References, and 1C has nothing to override it with): its parent
  is the key just before it in the stored messages' link lists. JWZ 1A keeps the first such
  link it meets, which depends on order; here the candidate named by the most lists wins, ties
  broken by the smallest key. So a reply whose client dropped References and In-Reply-To is
  still placed by a later reply's References; the price is that one forged References list can put a
  genuine link-less root under another thread (display only, see above);
- every cycle (only conflicting or forged headers make one) is cut at its smallest key, which
  becomes a root.

Roots (JWZ steps 2-4). Each node's root is found by following parents. JWZ step 4 prunes empty
containers: an empty root whose stored descendants reachable through empty containers only
(its *effective children*) number exactly one is replaced by that message; with two or more it
stays as the thread's root. So ``thread_id`` = the root's mid: a stored root's own mid, or, for
an empty root kept by step 4, the mid its Message-ID would have (the missing message's mid; it
becomes the stored root's mid unchanged if that message is ingested later). An orphan (its
parent chain reaches no stored message and no sibling) is its own root.

Known limitations:

- A ``thread_id`` can be the mid of a message that is not in the index (the empty root kept by
  JWZ step 4). Consumers compare thread_ids by equality and never look one up as a message
  (cross-plan note for plan 06a's ``get_thread``).
- A Message-ID with raw bytes that are not UTF-8 gets a mid hashed from those bytes
  (``mid_from_raw``, surrogateescape), but ``mime.py`` returns References/In-Reply-To ids with
  such bytes replaced by U+FFFD, so a reply's link never equals that parent's mid and the reply
  is not threaded under it (pinned by a test). Fixing it needs the raw id bytes kept for
  References/In-Reply-To in a storable form (SQLite TEXT rejects surrogateescape strings) and
  :func:`id_key` hashing those bytes as ``mid_from_raw`` does.

:func:`assign_threads` computes this for one closed set of nodes (whole connected components);
the Store re-runs it over the component a stored or removed message touches (``store.py``).
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence

from tern.index.mid import normalize_message_id


def id_key(message_id: str) -> str | None:
    """The container key of a referenced Message-ID: its mid (``sha256`` of the normalized id,
    hashed as UTF-8 like :func:`tern.index.mid.mid_of`); None when it normalizes to nothing."""
    norm = normalize_message_id(message_id)
    if norm is None:
        return None
    return hashlib.sha256(norm.encode("utf-8", "surrogatepass")).hexdigest()


def message_links(mid: str, refs: Sequence[str], in_reply_to: str | None) -> tuple[str, ...]:
    """The keys a message points at, oldest ancestor first: References, or ``[In-Reply-To]``
    when there are no References; de-duplicated (first occurrence kept), its own key dropped."""
    ids: Iterable[str] = refs if refs else ([in_reply_to] if in_reply_to is not None else [])
    out: dict[str, None] = {}  # insertion-ordered set
    for value in ids:
        key = id_key(value)
        if key is not None and key != mid:
            out.setdefault(key)
    return tuple(out)


def _parents(links: Mapping[str, Sequence[str]]) -> dict[str, str]:
    parent: dict[str, str] = {}
    votes: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for mid, keys in links.items():
        if keys:
            parent[mid] = keys[-1]
        for prev, cur in zip(keys, keys[1:], strict=False):
            if not links.get(cur):  # an empty container, or a stored message without links
                votes[cur][prev] += 1
    for cur, count in votes.items():
        parent[cur] = min(count, key=lambda key: (-count[key], key))
    return parent


def _cut_cycles(parent: dict[str, str]) -> None:
    """Remove one parent link per cycle: the cycle's smallest key becomes a root. Each node has
    at most one parent, so cycles are disjoint and each is found whole, in any visiting order."""
    done: set[str] = set()
    for start in sorted(parent):
        path: list[str] = []
        on_path: dict[str, int] = {}
        node: str | None = start
        while node is not None and node not in done and node not in on_path:
            on_path[node] = len(path)
            path.append(node)
            node = parent.get(node)
        if node is not None and node in on_path:
            del parent[min(path[on_path[node] :])]
        done.update(path)


def assign_threads(links: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """``mid -> thread_id`` for every stored message in ``links`` (``mid -> message_links``).

    ``links`` must be closed: it holds every stored message connected to any message in it,
    else the result differs from the one over the whole index. See the module docstring for
    the rules; the result does not depend on the mapping's order."""
    parent = _parents(links)
    _cut_cycles(parent)

    root_of: dict[str, str] = {}

    def root(node: str) -> str:
        path: list[str] = []
        while node not in root_of and node in parent:
            path.append(node)
            node = parent[node]
        top = root_of.get(node, node)
        for n in path:
            root_of[n] = top
        root_of[node] = top
        return top

    children: defaultdict[str, list[str]] = defaultdict(list)
    for child, par in parent.items():
        children[par].append(child)

    def effective_children(empty_root: str) -> set[str]:
        found: set[str] = set()
        stack = [empty_root]
        while stack:
            for child in children[stack.pop()]:
                if child in links:
                    found.add(child)
                else:
                    stack.append(child)
        return found

    thread_of_root: dict[str, str] = {}
    out: dict[str, str] = {}
    for mid in links:
        top = root(mid)
        if top not in thread_of_root:
            if top in links:
                thread_of_root[top] = top
            else:
                eff = effective_children(top)
                thread_of_root[top] = next(iter(eff)) if len(eff) == 1 else top
        out[mid] = thread_of_root[top]
    return out
