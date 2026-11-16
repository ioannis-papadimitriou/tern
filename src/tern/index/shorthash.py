"""Short stable hashes for accounts and folders in content-free output (decisions #28).

An account is ``acct-`` + the first 4 hex chars of sha256(UTF-8 account name); a folder is
``fold-`` + the same over ``"<account name>/<folder path>"``, the path with any leading ``/``
removed (FakeBridge paths have none, TB's have one; both hash alike). :func:`assign` widens a
whole set to 6 hex chars when two distinct names collide at 4 (#28's "widen in the tern code").
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Literal

WIDTH = 4
WIDE = 6

Kind = Literal["acct", "fold"]


class ShortHashCollision(RuntimeError):
    """Distinct names still collide at the widest width; never resolved silently."""


def _hex(name: str, width: int) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:width]


def folder_name(account_name: str, path: str) -> str:
    """The name a folder hash is taken over: ``<account name>/<path without leading '/'>``."""
    return f"{account_name}/{path.lstrip('/')}"


def account_hash(name: str, width: int = WIDTH) -> str:
    return "acct-" + _hex(name, width)


def folder_hash(account_name: str, path: str, width: int = WIDTH) -> str:
    return "fold-" + _hex(folder_name(account_name, path), width)


def assign(names: Iterable[str], kind: Kind) -> dict[str, str]:
    """Hash every distinct name in ``names`` (account names, or :func:`folder_name` values for
    ``kind="fold"``) at 4 hex chars; if any two distinct names collide, the whole set is
    hashed at 6. Raises ShortHashCollision if they still collide."""
    distinct = sorted(set(names))
    for width in (WIDTH, WIDE):
        out = {n: f"{kind}-{_hex(n, width)}" for n in distinct}
        if len(set(out.values())) == len(out):
            return out
    raise ShortHashCollision(f"{kind} hashes collide at {WIDE} hex chars ({len(distinct)} names)")
