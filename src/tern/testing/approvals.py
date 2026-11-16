"""Approval drivers for tests (architecture §5 "Testing", plan 06b step 5).

Each driver is an :class:`~tern.perms.approvals.ApprovalNotifier` that stands in for the
approval UI: when a card is requested it decides through
:meth:`Approvals.decide <tern.perms.approvals.Approvals.decide>` with caller ``ui``, exactly as
the extension's ``approvals.decide`` handler will (plan 12). It never bypasses the binding:
it passes the card's own nonce unless a script says otherwise.

- :func:`approve_all`: approve every card.
- :func:`deny_all`: deny every card.
- :func:`holding`: decide nothing (the card stays open until the test calls
  :meth:`ScriptedApprovals.decide`, the approval expires or the call is cancelled).
- :func:`scripted`: a function of the card returns a :class:`Scripted` decision, or None to hold.

Each factory attaches the driver to the given :class:`~tern.perms.approvals.Approvals` and returns
it. The driver records every card (``cards``), every immediate status ``decide`` returned
(``statuses``) and every ``resolved`` announcement (``resolutions``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from tern.perms.approvals import ApprovalCard, Approvals, ApprovalStatus, DecideDecision


@dataclass(frozen=True, slots=True)
class Scripted:
    """One scripted decision: ``subset`` narrows a W2 approval; ``nonce`` replaces the card's
    nonce (to test a wrong one)."""

    decision: DecideDecision
    subset: Sequence[str] | None = None
    nonce: str | None = None


#: Decides a card (or None: leave it open).
Script = Callable[[ApprovalCard], Scripted | None]


class ScriptedApprovals:
    """An approval UI stand-in that decides each requested card as ``script`` says (module
    docstring)."""

    def __init__(self, approvals: Approvals, script: Script) -> None:
        if not isinstance(approvals, Approvals):
            raise TypeError("approvals must be an Approvals")
        self.approvals = approvals
        self.script = script
        self.cards: list[ApprovalCard] = []
        self.statuses: list[ApprovalStatus] = []
        self.resolutions: list[tuple[str, ApprovalStatus]] = []

    async def requested(self, card: ApprovalCard) -> None:
        self.cards.append(card)
        choice = self.script(card)
        if choice is not None:
            self.statuses.append(
                self.approvals.decide(
                    card.approval_id,
                    card.nonce if choice.nonce is None else choice.nonce,
                    choice.decision,
                    choice.subset,
                    caller="ui",
                )
            )

    def resolved(self, approval_id: str, status: ApprovalStatus) -> None:
        self.resolutions.append((approval_id, status))

    def decide(
        self,
        card: ApprovalCard,
        decision: DecideDecision,
        subset: Sequence[str] | None = None,
        *,
        nonce: str | None = None,
    ) -> ApprovalStatus:
        """Decide ``card`` now, as the UI would (caller ``ui``, the card's nonce by default)."""
        status = self.approvals.decide(
            card.approval_id,
            card.nonce if nonce is None else nonce,
            decision,
            subset,
            caller="ui",
        )
        self.statuses.append(status)
        return status


def scripted(approvals: Approvals, script: Script) -> ScriptedApprovals:
    """A driver deciding by ``script``, attached to ``approvals``."""
    driver = ScriptedApprovals(approvals, script)
    approvals.attach(driver)
    return driver


def approve_all(approvals: Approvals) -> ScriptedApprovals:
    """A driver approving every card, attached to ``approvals``."""
    return scripted(approvals, lambda card: Scripted("approve"))


def deny_all(approvals: Approvals) -> ScriptedApprovals:
    """A driver denying every card, attached to ``approvals``."""
    return scripted(approvals, lambda card: Scripted("deny"))


def holding(approvals: Approvals) -> ScriptedApprovals:
    """A driver that decides nothing (the test decides), attached to ``approvals``."""
    return scripted(approvals, lambda card: None)
