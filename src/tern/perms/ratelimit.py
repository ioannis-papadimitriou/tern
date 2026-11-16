"""The W1 rate window (#7 as amended, D2/M2/B-M17; architecture §6, plan 06b step 4).

Each caller gets at most ``Limits.w1_per_caller`` (500) W1 targets per sliding
``Limits.window_s`` (3600 s), and all callers together at most ``Limits.w1_global`` (1000). A call
that would exceed either is refused (:class:`RateLimited`; ``Registry.call`` records it as
``decision=denied``, reason ``rate_limited``).

- **Caller ids** are the permission callers (``agent:<session>``, ``mcp:<proxy connection id>``,
  ``job:<name>``, ``ui``). An MCP client's self-declared name (``caller_label``) is never part of
  the key, so rotating names does not reset a window.
- **One source of truth:** the used part of a window is read from the audit log
  (``AuditLog.target_total(since=now - window_s, caller=..., tier=W1, executed=True)``), so it
  survives restarts and cannot drift from the rows. Only calls whose *escalated* tier is W1 and
  that executed count: a 21-target call (W2) and a refused W1 call never do.
- **Check and reserve with no await in between:** :meth:`RateWindow.reserve` is synchronous. It
  adds an in-memory reservation for the call's targets, which counts against both limits until
  :meth:`Reservation.release` (called once the call's row is written, when the audit counts it,
  or when the call fails before that). Without it, concurrent calls that each pass the check
  before any row exists would overshoot the window.
- A W1 call with no target mids (a ``count_targets``-only tool reporting 0) adds nothing to the
  window but is refused once the window is full (it is checked as one target).

The window reads ``clock`` (epoch seconds), which must be the clock of the ``AuditLog`` that
stamps the rows (ternd: ``time.time`` for both; tests: one fake clock).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from tern.perms.audit import CALLER_RE, AuditLog
from tern.tools.contract import Tier

#: Which limit refused a call.
Scope = Literal["caller", "global"]


class RateLimited(Exception):
    """A W1 call would exceed its caller's window (``scope="caller"``) or the global one
    (``"global"``). The message names the limit only, never the caller or the targets."""

    def __init__(self, scope: Scope, limit: int, window_s: int) -> None:
        who = "this caller" if scope == "caller" else "all callers together"
        self.scope: Scope = scope
        self.message = f"W1 rate window: at most {limit} W1 targets per {window_s} s for {who}"
        super().__init__(self.message)

    @property
    def reason(self) -> str:
        """The ToolError reason: ``caller_window`` or ``global_window``."""
        return f"{self.scope}_window"


class Reservation:
    """One call's in-flight targets in the window, until :meth:`release` (idempotent)."""

    __slots__ = ("_on_release", "_released", "caller", "targets")

    def __init__(
        self, caller: str, targets: int, on_release: Callable[[Reservation], None]
    ) -> None:
        self._on_release = on_release
        self.caller = caller
        self.targets = targets
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        """Give the reserved targets back (the call's row now counts them, or the call
        failed). A second call does nothing."""
        if self._released:
            return
        self._released = True
        self._on_release(self)


class RateWindow:
    """The per-caller and global W1 windows over ``audit`` (module docstring)."""

    def __init__(
        self,
        audit: AuditLog,
        clock: Callable[[], float],
        *,
        per_caller: int,
        global_limit: int,
        window_s: int,
    ) -> None:
        if not isinstance(audit, AuditLog):
            raise TypeError("RateWindow.audit must be an AuditLog")
        if not callable(clock):
            raise TypeError("RateWindow.clock must be callable")
        for name, value in (
            ("per_caller", per_caller),
            ("global_limit", global_limit),
            ("window_s", window_s),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"RateWindow.{name} must be an int >= 1")
        self._audit = audit
        self._clock = clock
        self._per_caller = per_caller
        self._global = global_limit
        self._window_s = window_s
        self._by_caller: dict[str, int] = {}
        self._total = 0

    def reserved(self, caller: str | None = None) -> int:
        """In-flight targets of ``caller`` (or of every caller)."""
        return self._total if caller is None else self._by_caller.get(caller, 0)

    def used(self, caller: str | None = None, *, since: float | None = None) -> int:
        """Executed W1 targets of ``caller`` (or of every caller) in the window starting at
        ``since`` (default: the current window, ``clock() - window_s``), from the audit log."""
        if since is None:
            since = self._clock() - self._window_s
        return self._audit.target_total(since=since, caller=caller, tier=Tier.W1, executed=True)

    def reserve(self, caller: str, targets: int) -> Reservation:
        """Check ``targets`` more W1 targets for ``caller`` against both windows and reserve
        them, or raise :class:`RateLimited` (nothing reserved). Synchronous on purpose: no
        other call can run between the check and the reservation. An audit read failure
        (``sqlite3.Error``) propagates."""
        if not isinstance(caller, str) or not CALLER_RE.fullmatch(caller):
            raise ValueError("invalid caller id")
        if isinstance(targets, bool) or not isinstance(targets, int) or targets < 0:
            raise ValueError("targets must be an int >= 0")
        need = max(targets, 1)  # a 0-target call is refused once the window is full
        since = self._clock() - self._window_s  # one clock read: both windows end at one instant
        if self.used(caller, since=since) + self.reserved(caller) + need > self._per_caller:
            raise RateLimited("caller", self._per_caller, self._window_s)
        if self.used(since=since) + self._total + need > self._global:
            raise RateLimited("global", self._global, self._window_s)
        if targets:  # a 0-target reservation holds nothing: no entry, so nothing to delete
            self._by_caller[caller] = self._by_caller.get(caller, 0) + targets
            self._total += targets
        return Reservation(caller, targets, self._release)

    def _release(self, reservation: Reservation) -> None:
        if not reservation.targets:
            return
        left = self._by_caller[reservation.caller] - reservation.targets
        if left:
            self._by_caller[reservation.caller] = left
        else:
            del self._by_caller[reservation.caller]
        self._total -= reservation.targets
