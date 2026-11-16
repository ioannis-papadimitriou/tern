"""The permission engine (#7 as amended, architecture §6, plan 06b).

``Registry.call`` asks the engine for a call's **effective tier** once the arguments validated and
the targets were counted:

    effective tier = max(spec.effective_tier, count tier, recipient tier)

- ``spec.effective_tier``: the declared tier raised by the declared effects (``tern-tool/1``).
- **count tier** (D2): with more than :data:`COUNT_ESCALATION_THRESHOLD` targets
  (``spec.target_count``, floored at the detected mids), a write tier moves up one step from
  ``spec.effective_tier``: W1 -> W2, W2 -> W3; W3 stays W3. R0/R1 never escalate by count.
- **recipient tier** (D3): a tool that declares ``recipients`` with any unknown recipient is at
  least W2. A provider that raises :class:`~tern.perms.recipients.RecipientsUnavailable` makes
  every recipient of the call unknown (W2, a WARNING with the count); any other provider
  exception propagates (``internal``). The approval card lists every recipient with its ``known``
  flag (:func:`card_recipients`).

A tool cannot lower the result: the only tool-derived inputs are its declared metadata, its
target count and its declared recipients, and each can only raise the tier.

Forbidden effects (step 11, T6): a tool declaring an effect in :data:`FORBIDDEN_EFFECTS`
(``filters_write``) is refused before anything else (:meth:`Engine.forbidden_effects`).

Target resolution (step 3, :mod:`tern.perms.targets`): the engine resolves a W call's target
mids from the index (refusing unknown, ``x-``, conflicted and ambiguous targets), then finds them
live through its own bridge (``ctx.targets``, the ``prev`` state) and, after the write, again
(the ``post`` state of the undo token).

The W1 rate window (step 4, :mod:`tern.perms.ratelimit`): before a W1 call's targets are
resolved, :meth:`Engine.reserve_w1` checks its target count against the caller's window
(``Limits.w1_per_caller`` per ``Limits.window_s``) and the global one (``Limits.w1_global``),
counted from the audit log (executed W1 calls) plus in-flight reservations, and reserves it
(synchronously: no await between the check and the reservation). The registry releases the
reservation once the call's row is written or the call fails.

Approvals (step 5, :mod:`tern.perms.approvals`): a W2/W3 call's targets are resolved the same
way, then :attr:`Engine.approvals` opens an approval whose card is built from the index
(:meth:`Engine.card_targets`, :meth:`Engine.card_provenance`) and the call waits for the UI's
decision (or the 10-minute expiry). Without a notifier (no approval UI attached) a W2/W3 call
is refused at once. The execution re-check and undo follow (steps 6-8).

Write status (step 7, :mod:`tern.perms.write_status`): :attr:`Engine.write_status` watches every
executed write until the index shows all its targets in their recorded post state, then appends
one ``observed`` event to its call.

Undo (step 8, :mod:`tern.perms.undo`): ``Registry.undo`` (``ui`` only) checks every target of an
undo token live against its recorded post state (:meth:`Engine.locate_undo`) before it writes the
inverse; :attr:`Engine.undo_claims` keeps two concurrent undos of one token apart.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass

from tern._model import BoundaryModel
from tern.bridge.client import ExtensionRequester
from tern.index.store import Store
from tern.perms import targets
from tern.perms.approvals import (
    ApprovalNotifier,
    Approvals,
    CardProvenance,
    CardTarget,
    card_provenance,
    card_targets,
)
from tern.perms.audit import AuditLog, UndoPayload, UndoTarget
from tern.perms.ratelimit import RateWindow, Reservation
from tern.perms.recipients import KnownRecipients, RecipientsUnavailable
from tern.perms.targets import IndexTarget, LiveTargets
from tern.perms.undo import LocatedTarget, UndoClaims, locate
from tern.perms.write_status import WriteStatus
from tern.tools.contract import Effect, Tier, ToolSpec, max_tier

log = logging.getLogger(__name__)

#: More targets than this escalate a write tier by one step (#7, D2).
COUNT_ESCALATION_THRESHOLD = 20

#: The tier a recipient escalation raises a call to, at least (#7, D3).
UNKNOWN_RECIPIENT_TIER = Tier.W2

#: More distinct declared recipients than this are refused before the provider is asked
#: (:class:`TooManyRecipients`): each address costs one provider call (plan 10: one bridge
#: ``contacts.lookup``), and the list is model-controlled.
MAX_RECIPIENTS = 100

_COUNT_STEP: dict[Tier, Tier] = {Tier.W1: Tier.W2, Tier.W2: Tier.W3, Tier.W3: Tier.W3}

#: Effects no tool may have in v1, whatever its origin (T6, architecture §7: no filter or rule
#: tools, so injected mail cannot persist through a filter). 06a's inventory test covers the
#: built-ins and installed entry points; a config-path plugin is loaded at ternd start, so the
#: engine refuses such a tool at call time (``denied``/``forbidden_effect``), before any tool
#: code (validators, ``count_targets``, ``recipients``) runs and before any approval.
FORBIDDEN_EFFECTS: frozenset[Effect] = frozenset({Effect.FILTERS_WRITE})


class EngineError(ValueError):
    """The engine was built from parts it must not get (a tern bug: loud)."""


class TooManyRecipients(Exception):
    """A call declares more than :data:`MAX_RECIPIENTS` distinct recipients: refused before any
    provider call (``Registry.call`` records it as ``invalid_params``)."""


@dataclass(frozen=True, slots=True)
class Limits:
    """The engine's numeric limits (#7, plan 06b). The production values are the defaults; tests
    may lower them. The count-escalation threshold is not here: it is a fixed rule
    (:data:`COUNT_ESCALATION_THRESHOLD`)."""

    #: W1 targets per caller per sliding window (step 4).
    w1_per_caller: int = 500
    #: W1 targets of all callers together per sliding window (step 4).
    w1_global: int = 1000
    #: The sliding window, seconds (step 4).
    window_s: int = 3600
    #: An approval expires this long after its request, seconds (step 5).
    approval_ttl_s: int = 600
    #: Open approvals per caller (step 5).
    max_pending_per_caller: int = 5

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise EngineError(f"Limits.{name} must be an int >= 1")


def count_tier(base: Tier, target_count: int) -> Tier:
    """``base`` after count escalation: one step up for a write tier with more than
    :data:`COUNT_ESCALATION_THRESHOLD` targets (W3 stays W3); R0/R1 and small counts unchanged."""
    if not isinstance(base, Tier):
        raise TypeError("base must be a Tier")
    if isinstance(target_count, bool) or not isinstance(target_count, int) or target_count < 0:
        raise ValueError("target_count must be an int >= 0")
    if base.rank < Tier.W1.rank or target_count <= COUNT_ESCALATION_THRESHOLD:
        return base
    return _COUNT_STEP[base]


def forbidden_effects(spec: ToolSpec) -> frozenset[Effect]:
    """The declared effects of ``spec`` that no tool may have (:data:`FORBIDDEN_EFFECTS`)."""
    return frozenset(spec.effects) & FORBIDDEN_EFFECTS


def recipient_tier(spec: ToolSpec, *, recipients_known: bool) -> Tier:
    """The tier a call's recipients imply: :data:`UNKNOWN_RECIPIENT_TIER` for a tool that
    declares ``recipients`` when any recipient is unknown, else R0 (no escalation)."""
    if not isinstance(recipients_known, bool):
        raise TypeError("recipients_known must be a bool")
    if spec.recipients is None or recipients_known:
        return Tier.R0
    return UNKNOWN_RECIPIENT_TIER


def effective_tier(spec: ToolSpec, target_count: int, *, recipients_known: bool) -> Tier:
    """max(``spec.effective_tier``, count tier, recipient tier) (module docstring). Pure: the
    caller passes the target count it recorded (``spec.target_count(inp)``, computed once, so
    the audit row and the tier can never disagree) and whether every recipient is known."""
    base = spec.effective_tier
    return max_tier(
        [
            base,
            count_tier(base, target_count),
            recipient_tier(spec, recipients_known=recipients_known),
        ]
    )


@dataclass(frozen=True, slots=True)
class RecipientCheck:
    """One declared recipient (lower-cased) and whether the provider knows it."""

    addr: str
    known: bool


def card_recipients(checks: tuple[RecipientCheck, ...]) -> list[dict[str, object]]:
    """The approval card's recipient list (step 5): every checked recipient as
    ``{"addr", "known"}``, in the order checked. The index has no display name for an address;
    bridge 1.0 ``Recipient.name`` is plan 12's mapping."""
    return [{"addr": c.addr, "known": c.known} for c in checks]


class RecipientChecks(tuple[RecipientCheck, ...]):
    """:meth:`Engine.check_recipients`' result: the checks in order (a tuple, compared as one),
    plus ``unavailable``: True when the provider raised
    :class:`~tern.perms.recipients.RecipientsUnavailable`, so every ``known=False`` is assumed,
    not answered. ``Registry.call`` puts the flag on the call's log line (with its ``call_id``),
    which ties the engine's WARNING to the call's audit row."""

    unavailable: bool

    def __new__(
        cls, checks: Iterable[RecipientCheck] = (), *, unavailable: bool = False
    ) -> RecipientChecks:
        self = super().__new__(cls, checks)
        self.unavailable = unavailable
        return self


class Engine:
    """The permission engine behind ``Registry.call`` (module docstring).

    ``audit`` is the log the registry writes to (the rate window counts from it); ``index`` a
    read-only Store for target resolution; ``bridge`` the extension link (None when none can
    attach); ``clock`` epoch seconds, the same clock as ``audit``'s; ``known_recipients`` the
    recipient provider; ``notifier`` the approval UI (None: W2/W3 calls are refused, ternd until
    plan 12); ``sleep`` the approvals' expiry timer (tests inject one)."""

    def __init__(
        self,
        *,
        audit: AuditLog,
        index: Store,
        bridge: ExtensionRequester | None,
        clock: Callable[[], float],
        known_recipients: KnownRecipients,
        limits: Limits | None = None,
        notifier: ApprovalNotifier | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not isinstance(audit, AuditLog):
            raise EngineError("Engine.audit must be an AuditLog")
        if not isinstance(index, Store) or not index.readonly:
            raise EngineError("Engine.index must be a Store opened readonly=True")
        if not callable(clock):
            raise EngineError("Engine.clock must be callable")
        if not isinstance(known_recipients, KnownRecipients):
            raise EngineError("Engine.known_recipients must provide async is_known(addr)")
        if limits is None:
            limits = Limits()
        if not isinstance(limits, Limits):
            raise EngineError("Engine.limits must be a Limits")
        self._audit = audit
        self._index = index
        self._bridge = bridge
        self._clock = clock
        self._recipients = known_recipients
        self._limits = limits
        self._window = RateWindow(
            audit,
            clock,
            per_caller=limits.w1_per_caller,
            global_limit=limits.w1_global,
            window_s=limits.window_s,
        )
        if notifier is not None and not isinstance(notifier, ApprovalNotifier):
            raise EngineError("Engine.notifier must provide async requested(card), resolved()")
        if not callable(sleep):
            raise EngineError("Engine.sleep must be callable")
        self._approvals = Approvals(
            clock=clock,
            ttl_s=limits.approval_ttl_s,
            max_pending_per_caller=limits.max_pending_per_caller,
            notifier=notifier,
            sleep=sleep,
        )
        self._write_status = WriteStatus(audit, index, clock)
        self._undo_claims = UndoClaims()

    @property
    def audit(self) -> AuditLog:
        return self._audit

    @property
    def index(self) -> Store:
        return self._index

    @property
    def bridge(self) -> ExtensionRequester | None:
        return self._bridge

    @property
    def clock(self) -> Callable[[], float]:
        return self._clock

    @property
    def limits(self) -> Limits:
        return self._limits

    @property
    def rate_window(self) -> RateWindow:
        """The W1 rate window (step 4)."""
        return self._window

    @property
    def approvals(self) -> Approvals:
        """The open approvals (step 5): ``decide``, ``pending``, ``shutdown``."""
        return self._approvals

    @property
    def write_status(self) -> WriteStatus:
        """The write-status observer (step 7): ``Registry.call`` registers each executed write
        once its undo token is recorded; ternd adds :meth:`WriteStatus.observe` to the writing
        Store's ``location_observers`` and calls :meth:`WriteStatus.load` at start."""
        return self._write_status

    @property
    def undo_claims(self) -> UndoClaims:
        """The undo tokens being undone right now (step 8)."""
        return self._undo_claims

    def forbidden_effects(self, spec: ToolSpec) -> frozenset[Effect]:
        """The effects of ``spec`` the engine refuses at call time (:data:`FORBIDDEN_EFFECTS`);
        empty for every tool that may run."""
        return forbidden_effects(spec)

    def card_targets(self, resolved: Sequence[IndexTarget]) -> tuple[CardTarget, ...]:
        """The approval card's targets, from the engine's index."""
        return card_targets(self._index, resolved)

    def card_provenance(self, mids: Iterable[str]) -> tuple[CardProvenance, ...]:
        """The approval card's provenance, from the engine's index."""
        return card_provenance(self._index, mids)

    def reserve_w1(self, caller: str, target_count: int) -> Reservation:
        """Check a W1 call's ``target_count`` against the caller's and the global window and
        reserve it until the returned reservation is released;
        :class:`~tern.perms.ratelimit.RateLimited` when either window would be exceeded.
        Synchronous: nothing else runs between the check and the reservation."""
        return self._window.reserve(caller, target_count)

    async def check_recipients(self, spec: ToolSpec, inp: BoundaryModel) -> RecipientChecks:
        """Each distinct declared recipient of ``inp`` (lower-cased, first occurrence order) with
        the provider's answer; empty for a tool that declares none. Every distinct address is
        asked about once (the approval card lists them all); more than :data:`MAX_RECIPIENTS`
        is :class:`TooManyRecipients`, before any provider call.

        When the provider raises :class:`RecipientsUnavailable`, no further address is asked
        about and every address is unknown (answers already given included: the call is at
        least W2) and the result's ``unavailable`` is True, with a WARNING carrying the count
        only. Any other exception from
        ``recipients`` (tool code, :class:`RecipientsUnavailable` included: only the provider's
        declared failure escalates) or the provider propagates; a non-bool answer is a
        ``TypeError``."""
        declared = spec.recipient_addresses(inp)
        if not declared:
            return RecipientChecks()
        addrs = list(dict.fromkeys(declared))
        if len(addrs) > MAX_RECIPIENTS:
            raise TooManyRecipients(f"more than {MAX_RECIPIENTS} distinct recipients")
        checks: list[RecipientCheck] = []
        for addr in addrs:
            try:
                known = await self._recipients.is_known(addr)
            except RecipientsUnavailable:
                # The provider's declared failure: never W1 on error (plan 10). Its message may
                # name an address, so only the count is logged.
                log.warning(
                    "recipient lookup unavailable: every recipient treated as unknown",
                    extra={"tool": spec.name, "recipients": len(addrs)},
                )
                return RecipientChecks((RecipientCheck(a, False) for a in addrs), unavailable=True)
            if not isinstance(known, bool):
                raise TypeError("KnownRecipients.is_known must return a bool")
            checks.append(RecipientCheck(addr, known))
        return RecipientChecks(checks)

    def tier(
        self, spec: ToolSpec, target_count: int, recipients: tuple[RecipientCheck, ...]
    ) -> Tier:
        """The call's effective tier from its recorded target count and recipient checks."""
        return effective_tier(spec, target_count, recipients_known=all(r.known for r in recipients))

    # -- target resolution (step 3) -------------------------------------------------------------

    def resolve_index(
        self, spec: ToolSpec, inp: BoundaryModel, mids: Sequence[str]
    ) -> tuple[IndexTarget, ...]:
        """The call's targets resolved from the index; a
        :class:`~tern.perms.targets.TargetRefused` at the first that fails."""
        return targets.resolve_index(self._index, spec, inp, mids)

    async def find_live(self, resolved: Sequence[IndexTarget]) -> LiveTargets:
        """The targets' live locations through the engine's bridge (``ctx.targets``, prev
        state); ``targets_changed`` when a resolved location is gone."""
        return await targets.find_live(self._bridge, resolved)

    async def recheck(self, live: LiveTargets) -> LiveTargets:
        """The execution re-check of an approved W2/W3 call (M6, plan 06b step 6): ``live``'s
        targets (the approval card's snapshot: mid -> folder ids) found again through the
        engine's bridge just before the call executes. First the index is read again: a target
        no longer indexed is ``targets_changed`` (``not_indexed``), a target that became
        conflicted while the card was open is ``mid_conflicted`` (D1). Then a card location
        (mid, folder id) that is gone, e.g. the message moved to another folder or was deleted,
        is ``targets_changed``; extra copies in other folders do not count. Returns the fresh
        resolution (tb_ids, prev state) the call executes with."""
        targets.recheck_index(self._index, live.targets)
        return await targets.find_live(self._bridge, live.targets, against="the approval card")

    async def post_state(self, live: LiveTargets) -> list[UndoTarget]:
        """The undo targets after the write: every target found again, paired with its prev
        state."""
        return await targets.post_state(self._bridge, live)

    # -- undo (step 8) --------------------------------------------------------------------------

    async def locate_undo(self, payload: UndoPayload) -> list[LocatedTarget]:
        """Every target of an undo payload found live in its recorded post state, through the
        engine's index (Message-IDs) and bridge; a
        :class:`~tern.perms.targets.TargetRefused` (``state_changed``, ``unavailable``, ...)
        before anything is written."""
        return await locate(self._index, self._bridge, payload)
