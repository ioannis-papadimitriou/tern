"""Write status (M6, B-M9, architecture §6, plan 06b step 7).

An executed W call's audit row carries ``initial_status=tb_accepted`` (06a write-ahead): TB
accepted the request. :class:`WriteStatus` appends one ``observed`` event to the call once the
index has seen **every** target of the call reach its recorded post state (the undo token's
payload). It never updates a row (the audit log is append-only); the derived status of the call
becomes ``observed``.

What "reached" means, per undo target, decided when the write is registered:

- the post location is in an **indexed** folder (``ingest.is_indexed`` over the folder's
  ``special_use``): the index's state of (mid, post folder) equals ``post`` (tag keys, flagged,
  read); for a **move between folders** whose prev folder is indexed too, the prev location
  (mid, prev folder) must also be observed **removed** (the destination may already hold a copy
  of the mid in the post state, so the destination alone cannot confirm the move);
- the post location is in a folder the index **excludes** (Trash, Junk, Drafts, ...; or a folder
  the index does not know yet, e.g. a Trash TB just created), or the target was removed from TB's
  view (``post`` None): the prev location (mid, prev folder) is observed **removed**. A
  delete-to-Trash is observable only this way (plan 04 B4).

A target whose recorded post state equals its prev state (TB reported no change: a write TB
accepted without applying, or a no-op such as a tag the message already had) has nothing to
observe and is left out. A call is **not watched** when none of its targets changed, or when a
changed target's post and prev folders are both unobservable (e.g. a move from Junk to Trash, or
a created draft: ``prev`` None, ``post`` in Drafts): it stays ``tb_accepted`` and
:func:`stale_writes` counts it after 24 h (never an ``observed`` the index could not confirm).
Once a target's condition was met it stays met (TB applied the write; a later change by the user
does not undo that). Several targets on one (mid, folder) location (same-folder copies) are met
by any of their recorded states.

Watching:

- **Pending set** (in memory): call id -> conditions, plus a (mid, folder) -> call ids map, so
  :meth:`WriteStatus.observe` (a ``Store.location_observers`` entry, fired once per location
  change; backfill fires one per location) is a dict lookup.
- :meth:`WriteStatus.load` fills it at ternd start from the audit log: executed writes still
  ``tb_accepted``, with an undo token and not undone, from the last :data:`LOAD_WINDOW_S`
  seconds. Older ones stay counted by :func:`stale_writes` but are no longer watched.
- **Check on register** (:meth:`WriteStatus.register`, called right after a write's undo token is
  recorded, and for every loaded write): the conditions are compared with the index's current
  state first, since TB's events may already have been applied. A removal condition met there
  (no index row for (mid, prev folder)) is a removal the index applied: every prev location is
  one of the index's rows when the call's targets were resolved (``targets.resolve_index`` /
  ``find_live`` keep only the index's folders of each mid).

An undone write is dropped from the pending set (:meth:`WriteStatus.forget`, plan 06b step 8);
the undo call is registered like any write (its own undo token).

Exceptions from :meth:`WriteStatus.observe` propagate (the Store re-raises them after its last
observer: loud, never swallowed). Nothing here logs a folder, a tag or content: call ids and
counts only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from tern.index.ingest import is_indexed
from tern.index.store import LocationState, Store
from tern.perms.audit import AuditLog, TargetState, UndoPayload, UndoTarget

log = logging.getLogger(__name__)

#: A write still ``tb_accepted`` this long after its call is a stale write (architecture §6).
STALE_AFTER_S = 86_400

#: Executed writes from this far back are loaded into the pending set at start (7 days).
LOAD_WINDOW_S = 7 * 86_400

#: A (mid, folder id) location.
Location = tuple[str, str]


def stale_writes(audit: AuditLog, now: float) -> int:
    """Executed writes still ``tb_accepted`` more than :data:`STALE_AFTER_S` after their call
    (``tern audit`` prints it; the bridge field needs ``tern-bridge-1.1``)."""
    return audit.count(status="tb_accepted", until=now - STALE_AFTER_S, executed=True)


def _location_state(state: TargetState) -> LocationState:
    return LocationState(tags=tuple(state.tags), flagged=state.flagged, read=state.read)


@dataclass(slots=True)
class _Condition:
    """One location's condition: met when the index shows one of ``states`` there (None: the
    location is gone)."""

    states: set[LocationState | None] = field(default_factory=lambda: set[LocationState | None]())
    met: bool = False


def conditions(
    payload: UndoPayload, indexed: Callable[[str], bool]
) -> dict[Location, set[LocationState | None]] | None:
    """The location conditions of a write's undo payload (module docstring): one per changed
    target's location; None when no target changed or a changed target cannot be observed.
    ``indexed(folder_id)`` says whether the index keeps that folder."""
    out: dict[Location, set[LocationState | None]] = {}
    for t in payload.targets:
        if t.prev == t.post:
            continue
        wanted = _target_conditions(t, indexed)
        if not wanted:
            return None
        for key, state in wanted:
            out.setdefault(key, set()).add(state)
    return out or None


def _target_conditions(
    t: UndoTarget, indexed: Callable[[str], bool]
) -> list[tuple[Location, LocationState | None]]:
    """One changed target's location conditions; empty when it cannot be observed."""
    removed: list[tuple[Location, LocationState | None]] = (
        [((t.mid, t.prev.folder_id), None)]
        if t.prev is not None and indexed(t.prev.folder_id)
        else []
    )
    if t.post is not None and indexed(t.post.folder_id):
        reached: tuple[Location, LocationState | None] = (
            (t.mid, t.post.folder_id),
            _location_state(t.post),
        )
        moved = t.prev is not None and t.prev.folder_id != t.post.folder_id
        return [reached, *removed] if moved else [reached]
    return removed


class WriteStatus:
    """The write-status observer (module docstring). ``audit`` is the log the ``observed``
    events go to; ``index`` the Store the check on register reads (ternd: the engine's
    read-only Store over the same file the ingest writes)."""

    def __init__(self, audit: AuditLog, index: Store, clock: Callable[[], float]) -> None:
        self._audit = audit
        self._index = index
        self._clock = clock
        self._pending: dict[str, dict[Location, _Condition]] = {}
        self._by_location: dict[Location, set[str]] = {}

    def pending(self) -> frozenset[str]:
        """The call ids being watched."""
        return frozenset(self._pending)

    def _indexed_folders(self) -> Callable[[str], bool]:
        kept = {f.folder_id for f in self._index.folders() if is_indexed(f.special_use)}
        return kept.__contains__

    def register(self, call_id: str, payload: UndoPayload, *, check: bool = True) -> bool:
        """Watch the write ``call_id`` whose undo token holds ``payload``, after checking its
        conditions against the index's current state. Returns True when the write is (now)
        ``observed``, False while it is pending or when it cannot be observed (not watched).

        ``check=False`` skips the check on register, so only a later index event can meet a
        condition: the undo of a write the index has not seen yet (its conditions are that
        write's prev state, which the lagging index still shows, so the check would mark the
        undo ``observed`` before anything confirmed TB applied it; plan 06b step 8 review).

        The call is in the pending set before the check runs, so a failing check (an index or
        audit error, raised) leaves it watched by the observer; a failure reading the folder
        list (before the conditions are known) leaves it unwatched until the next :meth:`load`."""
        if call_id in self._pending:
            raise ValueError("this write is already watched")
        wanted = conditions(payload, self._indexed_folders())
        if wanted is None:
            log.info("write not observable: not watched", extra={"call_id": call_id})
            return False
        conds = {key: _Condition(states) for key, states in wanted.items()}
        self._pending[call_id] = conds
        for key in conds:
            self._by_location.setdefault(key, set()).add(call_id)
        if not check:
            return False
        for mid in sorted({mid for mid, _ in conds}):
            here = {loc.folder_id: loc.state for loc in self._index.locations(mid)}
            for (cmid, folder_id), cond in conds.items():
                if cmid == mid and here.get(folder_id) in cond.states:
                    cond.met = True
        return self._settle(call_id)

    def load(self) -> int:
        """Fill the pending set from the audit log (module docstring); returns how many writes
        are watched afterwards. Each loaded write gets the check on register."""
        records = self._audit.list(
            since=self._clock() - LOAD_WINDOW_S,
            limit=None,
            status="tb_accepted",
            executed=True,
        )
        observed = 0
        for rec in reversed(records):  # oldest first
            if rec.undo_token is None or rec.undone or rec.call_id in self._pending:
                continue
            if self.register(rec.call_id, self._audit.undo_token(rec.undo_token).payload):
                observed += 1
        log.info(
            "write status loaded",
            extra={"watched": len(self._pending), "observed": observed},
        )
        return len(self._pending)

    def observe(self, mid: str, folder_id: str, state: LocationState | None, source: str) -> None:
        """``Store.location_observers`` entry: a location was added, removed (``state`` None)
        or changed. A dict lookup when no watched write targets it."""
        watching = self._by_location.get((mid, folder_id))
        if not watching:
            return
        for call_id in sorted(watching):
            cond = self._pending[call_id][(mid, folder_id)]
            if not cond.met and state in cond.states:
                cond.met = True
                self._settle(call_id)

    def forget(self, call_id: str) -> bool:
        """Stop watching ``call_id`` (an undone write: its post state will not come back). True
        when it was watched."""
        conds = self._pending.get(call_id)
        if conds is None:
            return False
        self._forget(call_id, conds)
        log.info("write no longer watched: undone", extra={"call_id": call_id})
        return True

    def _settle(self, call_id: str) -> bool:
        """Append ``observed`` and stop watching once every condition is met. The event is
        appended first: if it fails (raised), the write stays watched."""
        conds = self._pending[call_id]
        if not all(c.met for c in conds.values()):
            return False
        self._audit.append_event(call_id, "observed", {"locations": len(conds)})
        self._forget(call_id, conds)
        log.info("write observed", extra={"call_id": call_id, "locations": len(conds)})
        return True

    def _forget(self, call_id: str, conds: Iterable[Location]) -> None:
        del self._pending[call_id]
        for key in conds:
            ids = self._by_location[key]
            ids.discard(call_id)
            if not ids:
                del self._by_location[key]
