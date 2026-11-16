"""The approvals backend (#7 as amended, architecture §6, plan 06b step 5).

A call whose escalated tier is W2 or W3 waits for a decision by the user. ``Registry.call``
resolves its targets (index, then live) and asks :meth:`Approvals.open` for an approval, then
awaits :meth:`Approvals.wait`. In order:

- **Card.** Built from engine-resolved data only (:class:`ApprovalCard`): for each target
  location, ``from``/``subject``/``date`` and the folder path from the index (nullable fields stay
  null; the bridge mapping is plan 12's); the tool, the tier, ``args_view`` (the validated args),
  the recipients with ``known`` (tools that declare ``recipients``) and the provenance (from +
  subject from the index for each provenance mid). Model text is never put on a card by tern:
  ``args_view`` is the validated args, shown as data.
- **Binding.** The approval stores ``sha256(canonical_json({"tool": name, "args": args}))``
  (:func:`approval_sha256`, 06a's :func:`~tern.perms.audit.canonical_json`), a nonce
  (``secrets.token_hex(16)``, given only to the notifier, i.e. the UI) and the resolved target
  snapshot (mid -> folder ids). ``Registry.call`` re-hashes the args it is about to execute and
  denies the call (``binding_mismatch``) when the hash differs.
- **Decide** (:meth:`Approvals.decide`): only caller ``ui`` may decide (any other caller is
  refused with ``denied``/``approval_role`` and the approval stays open). A wrong nonce denies the
  approval (``binding_mismatch``). A ``subset`` (W2 only, M5) must be a non-empty subset of the
  original target mids; the args are rebuilt with only those mids (the detected top-level ``Mid``
  list fields, re-validated with the In model) and re-hashed. A superset, an empty set, a subset on
  a W3 approval, or a tool whose mids cannot be narrowed (``count_targets``, or a single-``Mid``
  field) denies the approval (``binding_mismatch``).
- **Expiry.** :attr:`ApprovalCard.expires_at` is ``Limits.approval_ttl_s`` (600 s) after the
  request. :meth:`Approvals.wait` is the timer: it sleeps (``sleep``, injectable) until then and
  expires the approval when the clock has reached it; the timer starts with ``notifier.requested``
  (not after it), so a hanging notifier cannot hold the approval open. A
  :meth:`~Approvals.decide` at or after ``expires_at`` expires it too, checked against the clock
  (deterministic); a decide on an approval the timer already expired also returns ``expired``
  (resolved ids are remembered for ``ttl_s``, at most :data:`RESOLVED_KEEP`).
- **Pending.** :meth:`Approvals.pending` returns the open cards (a UI resync); at most
  ``Limits.max_pending_per_caller`` (5) are open per caller (:class:`PendingCapReached`).
- **No notifier** (no approval UI attached): ``Registry.call`` refuses a W2/W3 call at once
  (``unavailable``, ``no_approval_ui``) and never opens an approval.

**Ask approvals** (decisions #39, a chat in ``manual`` mode): a caller may also ask for an
approval of a call below W2 (``Registry.call(..., ask=notifier)``). It is the same approval: a
card (no targets: an R0/R1 call resolves none, and a W1 call's are resolved after the decision),
the args hash and nonce, the expiry, the per-caller cap, ``decide`` by ``ui`` only and the same
rows by outcome; its ``notifier`` is the one the call passed (the chat tab of the turn), not the
engine's, so the card goes to whoever asked for it. No subset (W2 only).

**Rows by outcome.** No audit ``call`` row exists while an approval is open. Every non-approved
outcome writes its row when the approval resolves, through the ``record`` callback the registry
passes to :meth:`Approvals.open` (:data:`OUTCOME_ROWS`): user deny ``denied``/``user_denied``;
wrong nonce or bad subset ``denied``/``binding_mismatch``; expiry ``timeout``/``expired``; the
caller's task cancelled while pending ``denied``/``cancelled``; ternd shutdown
(:meth:`Approvals.shutdown`) ``denied``/``shutdown``. An approved call's row (``approved``, or
``denied``/``binding_mismatch`` when the re-hash differs) is written by ``Registry.call`` right
after it wakes, before anything executes (write-ahead holds).

Each resolution except ``approved`` is announced with ``notifier.resolved(approval_id, status)``
(bridge ``approval.resolved``: ``denied``/``expired``); ``Registry.call`` announces an approved one
just before it executes, or ``targets_changed`` when its execution re-check (step 6) finds a
card location gone (``denied`` when the re-check cannot reach Thunderbird or is cancelled).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from tern._model import BoundaryModel
from tern.bridge.protocol import BridgeError
from tern.index.store import Store
from tern.perms.audit import CALLER_RE, Decision, args_sha256, canonical_json
from tern.perms.targets import IndexTarget, LiveTargets
from tern.tools.contract import Tier, ToolSpec

if TYPE_CHECKING:
    from tern.perms.engine import RecipientCheck

log = logging.getLogger(__name__)

#: How an approval ended. ``notify_unavailable``/``notifier_failed``: the notifier could not
#: show the card (a bridge error, or any other exception, which propagates).
Outcome = Literal[
    "approved",
    "user_denied",
    "binding_mismatch",
    "expired",
    "cancelled",
    "shutdown",
    "notify_unavailable",
    "notifier_failed",
]

#: The audit row each non-approved outcome writes (decision, reason).
OUTCOME_ROWS: Mapping[Outcome, tuple[Decision, str]] = {
    "user_denied": ("denied", "user_denied"),
    "binding_mismatch": ("denied", "binding_mismatch"),
    "expired": ("timeout", "expired"),
    "cancelled": ("denied", "cancelled"),
    "shutdown": ("denied", "shutdown"),
    "notify_unavailable": ("error", "unavailable"),
    "notifier_failed": ("error", "internal"),
}

#: Bridge 1.0 ``ApprovalStatus`` values tern announces.
ApprovalStatus = Literal["approved", "denied", "expired", "targets_changed"]

#: The ``approval.resolved`` status of each outcome the notifier is told about.
OUTCOME_STATUS: Mapping[Outcome, ApprovalStatus] = {
    "approved": "approved",
    "user_denied": "denied",
    "binding_mismatch": "denied",
    "expired": "expired",
    "cancelled": "denied",
    "shutdown": "denied",
}

#: ``approvals.decide`` decisions (bridge 1.0 ``ApprovalsDecideParams.decision``).
DecideDecision = Literal["approve", "deny"]

#: The only caller that may decide (the extension's approval UI; architecture §6).
DECIDING_CALLER = "ui"

#: At most this many resolved approval ids are remembered (``Approvals.decide``: a late decide on
#: an expired one is ``expired``); each is also forgotten ``ttl_s`` after it resolved.
RESOLVED_KEEP = 1024


def approval_sha256(tool: str, args: Mapping[str, Any]) -> str:
    """``sha256(canonical_json({"tool": tool, "args": args}))``: the approval binding hash."""
    text = canonical_json({"tool": tool, "args": dict(args)})
    return hashlib.sha256(text.encode("ascii")).hexdigest()


# ---- the card -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CardTarget:
    """One target location on a card, from the index (never from model text). Nullable index
    fields stay None (plan 12 maps them for bridge 1.0 ``ApprovalTarget``)."""

    mid: str
    folder_id: str
    #: The folder's full path, or None when the index has no folder row for it. The path and the
    #: mail-derived fields below are kept out of ``repr`` (a stray log line names no content).
    folder_path: str | None = field(repr=False)
    from_addr: str | None = field(repr=False)
    from_name: str | None = field(repr=False)
    subject: str | None = field(repr=False)
    #: The message date, epoch seconds (UTC), or None.
    date_utc: int | None


@dataclass(frozen=True, slots=True)
class CardProvenance:
    """A message whose untrusted text the caller read (from + subject from the index; None when
    the index has no such message). The mail-derived fields are kept out of ``repr``."""

    mid: str
    from_addr: str | None = field(repr=False)
    from_name: str | None = field(repr=False)
    subject: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class ApprovalCard:
    """What the approval UI shows (module docstring). ``nonce`` is for the UI only (the notifier
    and :meth:`Approvals.pending`); ``recipients`` is None for a tool that declares none. The
    nonce, the args and the recipients' addresses are kept out of ``repr`` (as are the targets' and
    provenance's mail-derived fields)."""

    approval_id: str
    nonce: str = field(repr=False)
    caller: str
    tool: str
    tier: Tier
    targets: tuple[CardTarget, ...]
    #: The validated args (a private JSON copy; display only: execution never reads it).
    args_view: dict[str, Any] = field(repr=False)
    recipients: tuple[RecipientCheck, ...] | None = field(repr=False)
    provenance: tuple[CardProvenance, ...]
    requested_at: float
    expires_at: float


def card_targets(index: Store, targets: Sequence[IndexTarget]) -> tuple[CardTarget, ...]:
    """One :class:`CardTarget` per resolved (mid, folder), in target order."""
    paths = {f.folder_id: f.path for f in index.folders()}
    out: list[CardTarget] = []
    for t in targets:
        msg = index.get_message(t.mid)
        for folder_id in t.folder_ids:
            out.append(
                CardTarget(
                    mid=t.mid,
                    folder_id=folder_id,
                    folder_path=paths.get(folder_id),
                    from_addr=None if msg is None else msg.from_addr,
                    from_name=None if msg is None else msg.from_name,
                    subject=None if msg is None else msg.subject,
                    date_utc=None if msg is None else msg.date_utc,
                )
            )
    return tuple(out)


def card_provenance(index: Store, mids: Iterable[str]) -> tuple[CardProvenance, ...]:
    """Each distinct provenance mid (first occurrence order) with from + subject from the
    index."""
    out: list[CardProvenance] = []
    for mid in dict.fromkeys(mids):
        msg = index.get_message(mid)
        out.append(
            CardProvenance(
                mid=mid,
                from_addr=None if msg is None else msg.from_addr,
                from_name=None if msg is None else msg.from_name,
                subject=None if msg is None else msg.subject,
            )
        )
    return tuple(out)


# ---- the notifier -------------------------------------------------------------------------------


@runtime_checkable
class ApprovalNotifier(Protocol):
    """The approval UI's link (plan 12: bridge ``approval.requested``/``approval.resolved``).

    ``requested`` runs once the approval is open, alongside the expiry timer (the UI may decide
    before it returns; it is cancelled when the approval resolves or expires first); a
    :class:`~tern.bridge.protocol.BridgeError` from it refuses the call (``unavailable``), any
    other exception propagates. ``resolved`` is synchronous (a notification; plan 12 schedules
    the bridge send) and its exceptions propagate."""

    async def requested(self, card: ApprovalCard) -> None: ...

    def resolved(self, approval_id: str, status: ApprovalStatus) -> None: ...


# ---- errors -------------------------------------------------------------------------------------


class PendingCapReached(Exception):
    """The caller already has ``Limits.max_pending_per_caller`` open approvals."""

    def __init__(self, limit: int) -> None:
        self.message = f"at most {limit} open approvals per caller"
        super().__init__(self.message)


class DecideRefused(Exception):
    """``decide`` was refused without resolving anything: ``code`` is a ``ToolErrorCode``
    (``denied`` for a non-``ui`` caller, ``invalid_params`` for an unknown approval or a malformed
    decision), ``reason`` a detail code. The message names no args or content."""

    def __init__(self, code: Literal["denied", "invalid_params"], reason: str, message: str):
        super().__init__(f"{code} ({reason}): {message}")
        self.code: Literal["denied", "invalid_params"] = code
        self.reason = reason
        self.message = message


class SubsetRefused(Exception):
    """A ``subset`` that is not allowed (the approval is denied, ``binding_mismatch``). The
    message is a static string naming the rule (no mids, args or content): it is logged."""


# ---- one approval -------------------------------------------------------------------------------

#: The registry's row writer for a non-approved outcome: returns the audit failure, or None.
RecordRow = Callable[[Outcome], BaseException | None]


class Approval:
    """One open (or resolved) approval: the card, the bound args and the targets to execute.

    ``inp``/``args_sha256``/``live``/``target_mids`` are what executes; a W2 subset replaces them
    (and sets ``original_args_sha256``, the audit-format hash of the original args)."""

    def __init__(
        self,
        *,
        card: ApprovalCard,
        spec: ToolSpec,
        inp: BoundaryModel,
        args: Mapping[str, Any],
        live: LiveTargets,
        target_mids: Sequence[str],
        record: RecordRow,
        future: asyncio.Future[Outcome],
        notifier: ApprovalNotifier | None = None,
    ) -> None:
        self.card = card
        self.spec = spec
        self.inp = inp
        self.args_sha256 = approval_sha256(spec.name, args)
        self.original_args_sha256: str | None = None
        self.live = live
        self.target_mids: tuple[str, ...] = tuple(target_mids)
        #: The resolved target snapshot: mid -> folder ids (the card's locations).
        self.snapshot: Mapping[str, tuple[str, ...]] = {t.mid: t.folder_ids for t in live.targets}
        self.outcome: Outcome | None = None
        #: An audit failure while writing the outcome's row (the waiting call raises it).
        self.row_failure: BaseException | None = None
        self.record_row = record
        self.future = future
        #: Who shows the card and hears how it ended: the engine's notifier when the approval
        #: opened, or the call's own (an ask approval, decisions #39).
        self.notifier = notifier

    @property
    def approval_id(self) -> str:
        return self.card.approval_id

    @property
    def caller(self) -> str:
        return self.card.caller

    @property
    def narrowed(self) -> bool:
        return self.original_args_sha256 is not None


def _narrowed_input(spec: ToolSpec, inp: BoundaryModel, keep: frozenset[str]) -> BoundaryModel:
    """``inp`` with only the ``keep`` mids in its detected top-level ``Mid`` list fields,
    re-validated with the In model; :class:`SubsetRefused` when the tool's mids cannot be narrowed
    that way."""
    if spec.count_targets is not None or any(kind != "many" for _, kind in spec.mid_fields):
        raise SubsetRefused("this tool's targets cannot be narrowed")
    data = inp.model_dump(by_alias=True)
    fields = type(inp).model_fields
    for name, _ in spec.mid_fields:
        key = fields[name].alias or name
        value: object = data.get(key)
        if value is None:
            continue
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise SubsetRefused("a mid field is not a list")
        items: list[object] = sorted(value) if isinstance(value, (set, frozenset)) else list(value)
        data[key] = [m for m in items if m in keep]
    try:
        narrowed = spec.input_model.model_validate(data)
    except ValidationError:
        raise SubsetRefused("the narrowed arguments are not valid") from None
    # A validator that adds a mid back is refused; ``keep`` <= the original targets is the
    # caller's check (decide), not repeated here.
    got = set(spec.target_mids(narrowed))
    if not got or not got <= keep:
        raise SubsetRefused("the narrowed arguments target more than the subset")
    return narrowed


def _narrowed_live(live: LiveTargets, keep: frozenset[str]) -> LiveTargets:
    return LiveTargets(
        tuple(t for t in live.targets if t.mid in keep),
        {m: v for m, v in live.locations.items() if m in keep},
        {m: v for m, v in live.untouched.items() if m in keep},
    )


# ---- the queue ----------------------------------------------------------------------------------


class Approvals:
    """The open approvals of one engine (module docstring). In memory only: a ternd restart
    drops them (a clean shutdown writes a ``denied``/``shutdown`` row for each)."""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        ttl_s: int,
        max_pending_per_caller: int,
        notifier: ApprovalNotifier | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not callable(clock) or not callable(sleep):
            raise TypeError("Approvals.clock and Approvals.sleep must be callable")
        for name, value in (("ttl_s", ttl_s), ("max_pending_per_caller", max_pending_per_caller)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"Approvals.{name} must be an int >= 1")
        self._clock = clock
        self._ttl_s = ttl_s
        self._cap = max_pending_per_caller
        self._sleep = sleep
        self._open: dict[str, Approval] = {}
        #: Recently resolved approvals: id -> (outcome, resolved at), oldest first; kept for
        #: ``ttl_s`` after resolution and at most :data:`RESOLVED_KEEP` (so a late ``decide`` on an
        #: expired approval is ``expired`` whether or not the timer ran first).
        self._resolved: OrderedDict[str, tuple[Outcome, float]] = OrderedDict()
        self._notifier: ApprovalNotifier | None = None
        if notifier is not None:
            self.attach(notifier)

    @property
    def notifier(self) -> ApprovalNotifier | None:
        return self._notifier

    def attach(self, notifier: ApprovalNotifier | None) -> None:
        """Set (or, with None, remove) the approval UI's notifier."""
        if notifier is not None and not isinstance(notifier, ApprovalNotifier):
            raise TypeError("the notifier must provide async requested(card) and resolved(id, s)")
        self._notifier = notifier

    def get(self, approval_id: str) -> Approval | None:
        """The open approval ``approval_id``, or None."""
        return self._open.get(approval_id)

    def pending(self, caller: str | None = None) -> list[ApprovalCard]:
        """The open cards (of ``caller``, or all), oldest first: the UI's resync."""
        return [a.card for a in self._open.values() if caller is None or a.caller == caller]

    # -- request ----------------------------------------------------------------------------

    def open(
        self,
        *,
        caller: str,
        spec: ToolSpec,
        tier: Tier,
        inp: BoundaryModel,
        args: Mapping[str, Any],
        live: LiveTargets,
        target_mids: Sequence[str],
        targets: tuple[CardTarget, ...],
        recipients: tuple[RecipientCheck, ...] | None,
        provenance: tuple[CardProvenance, ...],
        record: RecordRow,
        ask: ApprovalNotifier | None = None,
    ) -> Approval:
        """Open an approval for a W2/W3 call, or, with ``ask`` (the notifier that shows its card),
        an ask approval for a call below W2 (module docstring). Synchronous: the per-caller cap
        check and the registration have no await in between. :class:`PendingCapReached` when the
        caller has the maximum open already."""
        if not isinstance(caller, str) or not CALLER_RE.fullmatch(caller):
            raise ValueError("invalid caller id")
        if ask is None and tier not in (Tier.W2, Tier.W3):
            raise ValueError("only W2/W3 calls need an approval")
        if ask is not None:
            if not isinstance(ask, ApprovalNotifier):
                raise TypeError("ask must provide async requested(card) and resolved(id, s)")
            if tier.rank >= Tier.W2.rank:
                raise ValueError("a W2/W3 call has its own approval; ask is for calls below W2")
        if sum(1 for a in self._open.values() if a.caller == caller) >= self._cap:
            raise PendingCapReached(self._cap)
        now = self._clock()
        card = ApprovalCard(
            approval_id=uuid.uuid4().hex,
            nonce=secrets.token_hex(16),
            caller=caller,
            tool=spec.name,
            tier=tier,
            targets=targets,
            args_view=_json_copy(args),
            recipients=recipients,
            provenance=provenance,
            requested_at=now,
            expires_at=now + self._ttl_s,
        )
        approval = Approval(
            card=card,
            spec=spec,
            inp=inp,
            args=args,
            live=live,
            target_mids=target_mids,
            record=record,
            future=asyncio.get_running_loop().create_future(),
            notifier=self._notifier if ask is None else ask,
        )
        self._open[card.approval_id] = approval
        log.info(
            "approval requested",
            extra={
                "approval_id": card.approval_id,
                "tool": spec.name,
                "tier": tier.value,
                "caller": caller,
                "targets": len(targets),
            },
        )
        return approval

    async def wait(self, approval: Approval) -> Outcome:
        """Show the card (``notifier.requested``) and wait until the approval resolves or expires
        (the timer: ``sleep`` until ``expires_at``, then the clock decides). The timer runs while
        ``requested`` is still pending, so a notifier that hangs cannot keep the approval open
        past ``expires_at`` (``requested`` is then cancelled); it is also cancelled when the
        approval resolves first. A cancel resolves it ``cancelled`` and propagates; an exception
        from the injected ``sleep`` propagates like a notifier bug."""
        notifier = approval.notifier
        shown: asyncio.Future[None] | None = None
        sleeper: asyncio.Future[None] | None = None
        try:
            if notifier is None:  # removed after the call checked: nobody can see the card
                self._resolve(approval, "notify_unavailable")
            else:
                shown = asyncio.ensure_future(notifier.requested(approval.card))
            while approval.outcome is None:
                if shown is not None and shown.done():
                    done, shown = shown, None
                    self._shown(approval, done)
                    continue
                if sleeper is not None and sleeper.done():
                    done, sleeper = sleeper, None
                    failed = done.exception()
                    if failed is not None:
                        raise failed
                remaining = approval.card.expires_at - self._clock()
                if remaining <= 0:
                    self._resolve(approval, "expired")
                    break
                if sleeper is None:
                    sleeper = asyncio.ensure_future(self._sleep(remaining))
                waiting: set[asyncio.Future[Any]] = {approval.future, sleeper}
                if shown is not None:
                    waiting.add(shown)
                await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if shown is not None and shown.done():  # it ended together with the decision
                done, shown = shown, None
                self._shown(approval, done)
        except asyncio.CancelledError:
            if approval.outcome is None:
                self._resolve(approval, "cancelled")
            elif approval.outcome == "approved":
                # Approved, but cancelled before the call woke: nothing executed and no row yet.
                approval.row_failure = approval.record_row("cancelled")
                self.announce(approval, "denied")
            raise
        except BaseException:  # a notifier bug (or KeyboardInterrupt): row, then re-raised
            if approval.outcome is None:
                self._resolve(approval, "notifier_failed")
            elif approval.outcome == "approved":
                # Approved, then ``requested`` failed with a bug: nothing executes, and the
                # registry writes no row for a raised wait, so write it here and tell the UI.
                approval.row_failure = approval.record_row("notifier_failed")
                self.announce(approval, "denied")
            raise
        finally:
            for pending in (shown, sleeper):
                if pending is not None and not pending.done():
                    pending.cancel()
        assert approval.outcome is not None
        return approval.outcome

    def _shown(self, approval: Approval, shown: asyncio.Future[None]) -> None:
        """Handle a finished ``notifier.requested``: a bridge error refuses an open approval
        (``notify_unavailable``; after a decision it is only logged: the UI did decide); any
        other exception propagates (``wait`` writes the ``notifier_failed`` row, also after an
        approve decision, and announces ``denied``)."""
        failed = shown.exception()
        if failed is None:
            return
        if not isinstance(failed, BridgeError):
            raise failed
        if approval.outcome is None:
            self._resolve(approval, "notify_unavailable")
        else:
            log.warning(
                "approval card request failed after the decision",
                extra={"approval_id": approval.approval_id, "error": failed.name},
            )

    # -- decide -----------------------------------------------------------------------------

    def decide(
        self,
        approval_id: str,
        nonce: str,
        decision: DecideDecision,
        subset: Sequence[str] | None = None,
        *,
        caller: str,
    ) -> ApprovalStatus:
        """Decide an open approval (module docstring) and return its immediate status
        (``approved``, ``denied`` or ``expired``). :class:`DecideRefused` when the caller is not
        ``ui`` (the approval stays open), the approval is unknown or already resolved, or the
        decision is malformed."""
        if caller != DECIDING_CALLER:
            log.warning("approval decide refused: not the ui", extra={"caller": caller})
            raise DecideRefused("denied", "approval_role", "only the approval UI may decide")
        if decision not in ("approve", "deny"):
            raise DecideRefused("invalid_params", "decision", "decision must be approve or deny")
        approval = self._open.get(approval_id) if isinstance(approval_id, str) else None
        if approval is None:
            self._prune_resolved()
            past = self._resolved.get(approval_id) if isinstance(approval_id, str) else None
            if past is not None and past[0] == "expired":
                return "expired"  # the timer expired it first: same answer as below
            raise DecideRefused(
                "invalid_params", "unknown_approval", "no open approval has this id"
            )
        if self._clock() >= approval.card.expires_at:
            self._resolve(approval, "expired")
            return "expired"
        if not (
            isinstance(nonce, str)
            and nonce.isascii()
            and secrets.compare_digest(nonce, approval.card.nonce)
        ):
            self._resolve(approval, "binding_mismatch")
            return "denied"
        if decision == "deny":
            self._resolve(approval, "user_denied")
            return "denied"
        if subset is not None:
            try:
                self._narrow(approval, subset)
            except SubsetRefused as exc:
                log.info(
                    "approval subset refused",
                    extra={"approval_id": approval_id, "why": str(exc)},
                )
                self._resolve(approval, "binding_mismatch")
                return "denied"
        self._resolve(approval, "approved")
        return "approved"

    def _narrow(self, approval: Approval, subset: object) -> None:
        """Apply a W2 ``subset`` to ``approval`` (module docstring); :class:`SubsetRefused`
        leaves it unchanged."""
        if approval.card.tier is not Tier.W2:
            raise SubsetRefused("a subset is allowed on a W2 approval only")
        if isinstance(subset, (str, bytes)) or not isinstance(subset, Sequence):
            raise SubsetRefused("subset must be a list of mids")
        items = list(cast("Sequence[object]", subset))
        if not items or not all(isinstance(m, str) for m in items):
            raise SubsetRefused("subset must be a non-empty list of mids")
        keep = frozenset(str(m) for m in items)
        original = frozenset(approval.target_mids)
        if not keep <= original:
            raise SubsetRefused("subset is not a subset of the original targets")
        if keep == original:
            return  # every target kept: the args are unchanged
        spec = approval.spec
        narrowed = _narrowed_input(spec, approval.inp, keep)
        dumped = _dump_json(narrowed)
        original_dump = _dump_json(approval.inp)
        # The audit-format hash (args only) of what was asked, next to the narrowed args' row.
        approval.original_args_sha256 = args_sha256(original_dump)
        approval.inp = narrowed
        approval.args_sha256 = approval_sha256(spec.name, dumped)
        # What the narrowed input targets (a validator may drop a kept mid): the live targets,
        # ctx.targets, the bridge allow-list and the undo token follow it, as the row does.
        approval.target_mids = tuple(spec.target_mids(narrowed))
        approval.live = _narrowed_live(approval.live, frozenset(approval.target_mids))

    # -- resolution -------------------------------------------------------------------------

    def _resolve(self, approval: Approval, outcome: Outcome) -> None:
        """End ``approval`` with ``outcome``: drop it from the open set, write a non-approved
        outcome's row (``record``), wake the waiting call and, for an outcome the UI must hear
        about, announce it. Nothing awaits in between."""
        if approval.outcome is not None:
            raise RuntimeError("approval already resolved")
        approval.outcome = outcome
        self._open.pop(approval.approval_id, None)
        self._resolved[approval.approval_id] = (outcome, self._clock())
        self._prune_resolved()
        if outcome != "approved":
            approval.row_failure = approval.record_row(outcome)
        if not approval.future.done():
            approval.future.set_result(outcome)
        log.info(
            "approval resolved",
            extra={"approval_id": approval.approval_id, "outcome": outcome},
        )
        if outcome != "approved" and outcome in OUTCOME_STATUS:
            self.announce(approval, OUTCOME_STATUS[outcome])

    def _prune_resolved(self) -> None:
        """Forget resolved ids older than ``ttl_s``, and the oldest beyond :data:`RESOLVED_KEEP`."""
        horizon = self._clock() - self._ttl_s
        while self._resolved:
            first = next(iter(self._resolved.values()))
            if len(self._resolved) <= RESOLVED_KEEP and first[1] > horizon:
                break
            self._resolved.popitem(last=False)

    def announce(self, approval: Approval, status: ApprovalStatus) -> None:
        """``resolved(approval_id, status)`` on the approval's notifier, when it has one."""
        if approval.notifier is not None:
            approval.notifier.resolved(approval.approval_id, status)

    def shutdown(self) -> int:
        """Resolve every open approval ``shutdown`` (``denied``/``shutdown`` rows); returns how
        many. ternd calls it before it closes the audit log."""
        open_now = list(self._open.values())
        for approval in open_now:
            self._resolve(approval, "shutdown")
        return len(open_now)


def _json_copy(args: Mapping[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = json.loads(canonical_json(dict(args)))
    return copied


def _dump_json(inp: BoundaryModel) -> dict[str, Any]:
    """Validated args as JSON; :class:`SubsetRefused` when the In model cannot serialize them (a
    custom serializer is tool code)."""
    try:
        dumped: dict[str, Any] = json.loads(inp.model_dump_json())
    except PydanticSerializationError:
        raise SubsetRefused("the narrowed arguments cannot be serialized") from None
    return dumped
