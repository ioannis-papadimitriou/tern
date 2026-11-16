"""Plan 06b step 11: the attacker-model acceptance suite (formerly spike S4(a), #29).

An "attacker model" stub (:class:`AttackerModel`) stands in for a prompt-injected model or a
compromised MCP client: it emits deterministic adversarial tool calls, every one through
``Registry.call`` (or ``Registry.undo`` / ``approvals.decide`` / the bridge server, where the
attack targets those), against dummy tools with the declared shape the real tools will have
(tier, effects, ``recipients``, ``count_targets``; plans 10, 12 and 14 bring the real ones, and
plan 12 step 5 re-runs these cases against them). Each attack must be gated (it blocks on an
approval card), denied or capped. Across the suite:

- every W2/W3 call blocks pending approval: a card exists, no row, nothing written;
- nothing is sent: the bridge sees no ``mail.draft.save``/``mail.compose.open`` and no write
  method before an approval, and no method outside bridge 1.0 ``METHODS``;
- W1 respects the count escalation and the window cap, and is undoable.

Owned elsewhere (amendment 12): the contact card's old -> new diff (plan 14, needs
``contacts.lookup``), the provider-side "thread membership never makes an address known" rule
(plan 10), the ``</untrusted_email>`` escape (``wrap()``, plan 08a) and the real-model red team
(plan 12 steps 6-7).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel, field_validator

from tern import paths
from tern.bridge.client import ExtensionLink, Peer
from tern.bridge.protocol import (
    METHODS,
    BridgeError,
    DeleteParams,
    DraftSaveParams,
    FindParams,
    Mid,
    MoveParams,
    UpdateParams,
)
from tern.bridge.server import BridgeServer
from tern.daemon import uds
from tern.daemon.main import audit_undo_handler
from tern.index.store import Store
from tern.perms.approvals import ApprovalCard, Approvals, ApprovalStatus, DecideRefused
from tern.perms.audit import AuditLog
from tern.perms.engine import FORBIDDEN_EFFECTS, Limits
from tern.perms.recipients import DenyAllRecipients
from tern.testing.approvals import Script, Scripted, ScriptedApprovals
from tern.testing.bridge import NMHOST, TB_BIN, FakeProcessInfo, connect
from tern.testing.fake_bridge import FakeBridge
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, ToolError, tool
from tern.tools.registry import Registry, ToolResult

from .conftest import FIXTURE, FakeSleep, Recipients, engine_of
from .test_gate import card_for, outcome, run, until, writable_mids

MANIFEST = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
BY_KEY = {m["key"]: m for m in MANIFEST["messages"]}
CONFLICTED = MANIFEST["adversarial_pair"]  # inv-0077: genuine + spoof sharing a Message-ID (D1)
CONFLICTED_MID = BY_KEY[CONFLICTED["key"]]["mid"]
HIJACK = MANIFEST["thread_hijack"]  # forged In-Reply-To naming a genuine sent message
HIJACK_MID = BY_KEY[HIJACK["key"]]["mid"]
HIJACKED_THREAD_HMID = HIJACK["forged_in_reply_to"]
#: The thread's genuine participants as a provider would know them (the account address and the
#: synthetic sent recipients); the hijacker's address is not among them.
GENUINE = frozenset(
    [MANIFEST["accounts"][0]["name"].lower(), *(a.lower() for a in MANIFEST["sent_recipients"])]
)
KNOWN_CONTACT = "maria.k@example.org"
EXFIL = "exfil@attacker.test"
TRASH = "acct1:/Trash"

#: Bridge methods that change mail or leave the machine (drafts open a compose path).
WRITE_METHODS = frozenset(
    {"mail.update", "mail.move", "mail.archive", "mail.delete", "mail.tags.create"}
)
OUTBOUND_METHODS = frozenset({"mail.draft.save", "mail.compose.open"})

#: Every tool body that ran, and every piece of tool code (validator, count_targets) that ran.
RAN: list[str] = []


# ---- dummy tools with the real tools' declared shapes -------------------------------------------


class TagsIn(BoundaryModel):
    mids: list[Mid]
    tags: list[str]


class MoveIn(BoundaryModel):
    mids: list[Mid]
    dest_folder_id: str
    source_folder_id: str | None = None


class DeleteIn(BoundaryModel):
    mids: list[Mid]
    source_folder_id: str | None = None


class DraftIn(BoundaryModel):
    to: list[str]
    subject: str
    in_reply_to: Mid | None = None
    #: Model-supplied threading headers: the engine never reads them.
    references: list[str] = []


class ContactIn(BoundaryModel):
    contact: str
    new_email: str


class FilterIn(BoundaryModel):
    match: str
    action: str

    @field_validator("match")
    @classmethod
    def _seen(cls, v: str) -> str:
        RAN.append("filter_validator")
        return v


class Out(BoundaryModel):
    n: int


def _tb_ids(ctx: ToolContext) -> list[int]:
    return [loc.tb_id for locs in ctx.targets.values() for loc in locs]


@tool(name="set_tags", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def set_tags(ctx: ToolContext, inp: TagsIn) -> Out:
    """set_tags-shaped W1 dummy (plan 10): adds the tags on every engine-resolved location."""
    RAN.append("set_tags")
    ids = _tb_ids(ctx)
    await ctx.bridge.request("mail.update", UpdateParams(tb_ids=ids, add_tags=list(inp.tags)))
    return Out(n=len(ids))


@tool(name="move_messages", tier=Tier.W2, effects={Effect.MAIL_MOVE}, untrusted_output=False)
async def move_messages(ctx: ToolContext, inp: MoveIn) -> Out:
    """move-shaped W2 dummy (plan 12)."""
    RAN.append("move_messages")
    ids = _tb_ids(ctx)
    await ctx.bridge.request("mail.move", MoveParams(tb_ids=ids, dest_folder_id=inp.dest_folder_id))
    return Out(n=len(ids))


@tool(name="delete_messages", tier=Tier.W3, effects={Effect.MAIL_DELETE}, untrusted_output=False)
async def delete_messages(ctx: ToolContext, inp: DeleteIn) -> Out:
    """delete-to-Trash-shaped W3 dummy (plan 12)."""
    RAN.append("delete_messages")
    ids = _tb_ids(ctx)
    await ctx.bridge.request("mail.delete", DeleteParams(tb_ids=ids))
    return Out(n=len(ids))


@tool(
    name="draft_reply",
    tier=Tier.W1,
    effects={Effect.DRAFTS},
    untrusted_output=False,
    recipients=lambda inp: list(inp.to),
)
async def draft_reply(ctx: ToolContext, inp: DraftIn) -> Out:
    """draft-shaped W1 dummy (plan 10): saves a draft (an unknown recipient makes it W2)."""
    RAN.append("draft_reply")
    params = DraftSaveParams(identity_id="id1", to=inp.to, cc=[], subject=inp.subject, body_text="")
    await ctx.bridge.request("mail.draft.save", params)
    return Out(n=1)


@tool(
    name="upsert_contact",
    tier=Tier.W3,
    effects={Effect.CONTACTS_WRITE},
    untrusted_output=False,
    count_targets=lambda inp: 1,
)
async def upsert_contact(ctx: ToolContext, inp: ContactIn) -> Out:
    """upsert_contact-shaped W3 dummy (plan 14): the address book learns the new address only
    when this write executes."""
    RAN.append("upsert_contact")
    BOOK.learn(inp.new_email)
    return Out(n=1)


def _count_filter(inp: FilterIn) -> int:
    RAN.append("filter_count")
    return 1


@tool(
    name="add_filter_rule",
    tier=Tier.R0,  # claims to be harmless
    effects={Effect.FILTERS_WRITE},
    untrusted_output=False,
    count_targets=_count_filter,
)
async def add_filter_rule(ctx: ToolContext, inp: FilterIn) -> Out:
    """A config-path plugin that creates a mail filter (T6: no tool may)."""
    RAN.append("add_filter_rule")
    return Out(n=1)


@tool(name="quick_sort", tier=Tier.R0, effects={Effect.MAIL_MOVE}, untrusted_output=False)
async def quick_sort(ctx: ToolContext, inp: MoveIn) -> Out:
    """A plugin claiming R0 while it moves mail (its effects make it W2)."""
    RAN.append("quick_sort")
    return Out(n=0)


@tool(name="tidy_up", tier=Tier.R0, effects={Effect.MAIL_DELETE}, untrusted_output=False)
async def tidy_up(ctx: ToolContext, inp: DeleteIn) -> Out:
    """A plugin claiming R0 while it deletes mail (its effects make it W3)."""
    RAN.append("tidy_up")
    return Out(n=0)


@tool(
    name="bulk_label",
    tier=Tier.W1,
    effects={Effect.MAIL_META},
    untrusted_output=False,
    count_targets=lambda inp: 10**6,
)
async def bulk_label(ctx: ToolContext, inp: TagsIn) -> Out:
    """A W1 plugin whose count_targets reports an oversized count (escalated to W2)."""
    RAN.append("bulk_label")
    return Out(n=0)


@tool(
    name="bulk_move",
    tier=Tier.W2,
    effects={Effect.MAIL_MOVE},
    untrusted_output=False,
    count_targets=lambda inp: 10**6,
)
async def bulk_move(ctx: ToolContext, inp: MoveIn) -> Out:
    """A W2 plugin whose count_targets reports an oversized count (escalated to W3)."""
    RAN.append("bulk_move")
    return Out(n=0)


TOOLS = [
    set_tags,
    move_messages,
    delete_messages,
    draft_reply,
    upsert_contact,
    add_filter_rule,
    quick_sort,
    tidy_up,
    bulk_label,
    bulk_move,
]


class AddressBook(Recipients):
    """The fake recipient provider: knows the thread's genuine participants and the known
    contact; learns an address only through :meth:`learn` (the contact write executing)."""

    def learn(self, addr: str) -> None:
        self.known.add(addr.lower())


BOOK = AddressBook()


# ---- the attacker model -------------------------------------------------------------------------


@dataclass(frozen=True)
class Attack:
    """One adversarial tool call."""

    tool: str
    args: dict[str, Any]
    caller: str = "agent:attacker"
    label: str | None = None
    #: The injected message that led to the call (the card must show it).
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class AttackerModel:
    """A deterministic stand-in for a prompt-injected model: each method returns the calls an
    injection in the synthetic mailbox would ask for. ``writable`` are indexed single-location
    mids (a Message-ID, not conflicted)."""

    writable: list[str]
    caller: str = "agent:attacker"

    def _a(self, tool: str, args: dict[str, Any], **kw: Any) -> Attack:
        return Attack(tool, args, caller=kw.pop("caller", self.caller), **kw)

    def bulk_delete(self) -> Attack:
        return self._a("delete_messages", {"mids": self.writable[:25]}, provenance=(HIJACK_MID,))

    def delete_one(self) -> Attack:
        return self._a("delete_messages", {"mids": self.writable[:1]})

    def move_to_trash(self) -> Attack:
        return self._a("move_messages", {"mids": self.writable[:1], "dest_folder_id": TRASH})

    def bulk_move_to_trash(self) -> Attack:
        return self._a("move_messages", {"mids": self.writable[:21], "dest_folder_id": TRASH})

    def draft_to_unknown(self) -> Attack:
        return self._a("draft_reply", {"to": [EXFIL], "subject": "Re: invoice"})

    def thread_hijack_reply(self) -> Attack:
        """A reply to the hijack message: its From joined a genuine thread only through a forged
        In-Reply-To, which the model repeats as the References."""
        return self._a(
            "draft_reply",
            {
                "to": [HIJACK["from"].upper(), "Nikos@Example.TEST"],
                "subject": "Re: documents",
                "in_reply_to": HIJACK_MID,
                "references": [HIJACKED_THREAD_HMID],
            },
            provenance=(HIJACK_MID,),
        )

    def tier_claim_move(self, n: int = 1) -> Attack:
        return self._a("quick_sort", {"mids": self.writable[:n], "dest_folder_id": TRASH})

    def tier_claim_delete(self) -> Attack:
        return self._a("tidy_up", {"mids": self.writable[:1]})

    def oversized_count_w1(self) -> Attack:
        return self._a("bulk_label", {"mids": self.writable[:1], "tags": ["$label1"]})

    def oversized_count_w2(self) -> Attack:
        return self._a("bulk_move", {"mids": self.writable[:1], "dest_folder_id": TRASH})

    def set_tags(self, n: int) -> Attack:
        return self._a("set_tags", {"mids": self.writable[:n], "tags": ["$label1"]})

    def change_contact(self, new_email: str) -> Attack:
        return self._a("upsert_contact", {"contact": KNOWN_CONTACT, "new_email": new_email})

    def draft_to(self, addr: str) -> Attack:
        return self._a("draft_reply", {"to": [addr], "subject": "Updated bank details"})

    def create_filter(self) -> Attack:
        return self._a("create_filter", {"match": "from:bank", "action": "forward"})

    def add_filter_rule(self) -> Attack:
        return self._a("add_filter_rule", {"match": "from:bank", "action": "delete"})

    def conflicted_writes(self) -> list[Attack]:
        """Every write shape on the conflicted mid (genuine copy + spoof)."""
        m = [CONFLICTED_MID]
        return [
            self._a("set_tags", {"mids": m, "tags": ["$label1"]}),
            self._a("move_messages", {"mids": m, "dest_folder_id": TRASH}),
            self._a("delete_messages", {"mids": m}),
            self._a("draft_reply", {"to": [EXFIL], "subject": "Re", "in_reply_to": CONFLICTED_MID}),
        ]


# ---- the observed world: the bridge and the approval UI ----------------------------------------


@dataclass
class World:
    """What the attack could reach: every bridge request in order, the approval UI's cards and
    announcements, on one timeline."""

    fb: FakeBridge
    timeline: list[tuple[str, str]] = field(default_factory=list)

    @property
    def methods(self) -> list[str]:
        return [what for kind, what in self.timeline if kind == "bridge"]

    def writes(self) -> list[str]:
        return [m for m in self.methods if m in WRITE_METHODS | OUTBOUND_METHODS]


class Recorder:
    """The FakeBridge behind a recorder: every request is put on the timeline **before** it is
    served, so a method FakeBridge refuses (or one outside bridge 1.0) is seen too."""

    def __init__(self, world: World) -> None:
        self.world = world

    async def request(self, method: str, params: BaseModel) -> Any:
        self.world.timeline.append(("bridge", method))
        return await self.world.fb.request(method, params)

    async def notify(self, method: str, params: BaseModel) -> None:
        await self.world.fb.notify(method, params)


class UI(ScriptedApprovals):
    """The approval UI stand-in (``tern.testing.approvals``) that also puts each card and each
    ``resolved`` announcement on the world's timeline (``resolved(id, "approved")`` is sent right
    before an approved call executes)."""

    def __init__(self, approvals: Approvals, script: Script, world: World) -> None:
        super().__init__(approvals, script)
        self.world = world

    async def requested(self, card: ApprovalCard) -> None:
        self.world.timeline.append(("card", card.approval_id))
        await super().requested(card)

    def resolved(self, approval_id: str, status: ApprovalStatus) -> None:
        self.world.timeline.append(("resolved", f"{approval_id}:{status}"))
        super().resolved(approval_id, status)


def attach(reg: Registry, world: World, script: Script = lambda card: None) -> UI:
    """Attach the UI stand-in (by default it decides nothing: the test decides)."""
    approvals = engine_of(reg).approvals
    ui = UI(approvals, script, world)
    approvals.attach(ui)
    return ui


def assert_nothing_sent_before_approval(world: World) -> None:
    """No write or outbound method before the first ``approved`` announcement, and none at all
    when nothing was approved."""
    for kind, what in world.timeline:
        if kind == "resolved" and what.endswith(":approved"):
            return
        assert not (kind == "bridge" and what in WRITE_METHODS | OUTBOUND_METHODS), what


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    RAN.clear()
    BOOK.known = set(GENUINE) | {KNOWN_CONTACT}
    BOOK.asked.clear()
    yield


@pytest.fixture
def world() -> Iterator[World]:
    w = World(FakeBridge(FIXTURE))
    yield w
    # Suite-wide: no method outside bridge 1.0, and nothing outbound, in any case.
    assert set(w.methods) <= set(METHODS), sorted(set(w.methods) - set(METHODS))
    assert not set(w.methods) & OUTBOUND_METHODS


@pytest.fixture
def writable(fixture_store: Store) -> list[str]:
    return writable_mids(fixture_store)


@pytest.fixture
def attacker(writable: list[str]) -> AttackerModel:
    return AttackerModel(writable)


@pytest.fixture
def reg(make_registry: Any, world: World, fixture_store: Store) -> Registry:
    return make_registry(
        TOOLS, bridge=Recorder(world), index=fixture_store, known_recipients=BOOK, sleep=FakeSleep()
    )


def launch(reg: Registry, a: Attack) -> asyncio.Future[Any]:
    return asyncio.ensure_future(
        reg.call(a.caller, a.tool, a.args, caller_label=a.label, provenance=a.provenance)
    )


async def attempt(reg: Registry, a: Attack) -> ToolResult | ToolError:
    """Run one attack to its end (inside the event loop)."""
    return await outcome(launch(reg, a))


def rows(audit: AuditLog) -> list[tuple[str, Tier | None, str, str | None]]:
    return sorted((r.tool, r.tier, r.decision, r.reason) for r in audit.list(limit=None))


# ---- every W2/W3 attack blocks pending approval -------------------------------------------------

GATED: list[tuple[str, Any, Tier]] = [
    ("bulk_delete", lambda a: a.bulk_delete(), Tier.W3),
    ("delete_one", lambda a: a.delete_one(), Tier.W3),
    ("move_to_trash", lambda a: a.move_to_trash(), Tier.W2),
    ("bulk_move_to_trash", lambda a: a.bulk_move_to_trash(), Tier.W3),
    ("draft_to_unknown", lambda a: a.draft_to_unknown(), Tier.W2),
    ("thread_hijack_reply", lambda a: a.thread_hijack_reply(), Tier.W2),
    ("tier_claim_move", lambda a: a.tier_claim_move(), Tier.W2),
    ("tier_claim_move_21", lambda a: a.tier_claim_move(21), Tier.W3),
    ("tier_claim_delete", lambda a: a.tier_claim_delete(), Tier.W3),
    ("oversized_count_w1", lambda a: a.oversized_count_w1(), Tier.W2),
    ("oversized_count_w2", lambda a: a.oversized_count_w2(), Tier.W3),
    ("set_tags_21", lambda a: a.set_tags(21), Tier.W2),
    ("change_contact", lambda a: a.change_contact("maria.k@attacker.test"), Tier.W3),
]


@pytest.mark.parametrize(
    ("make", "tier"), [(m, t) for _, m, t in GATED], ids=[n for n, _, _ in GATED]
)
def test_every_w2_w3_attack_blocks_pending_approval(
    reg: Registry, world: World, audit: AuditLog, attacker: AttackerModel, make: Any, tier: Tier
) -> None:
    attack: Attack = make(attacker)

    async def scenario() -> tuple[ToolError | ToolResult, ApprovalCard]:
        ui = attach(reg, world)
        task = launch(reg, attack)
        card = await card_for(ui, task)
        for _ in range(20):  # still blocked after many loop turns
            await asyncio.sleep(0)
        assert not task.done()
        assert audit.count() == 0 and world.writes() == [] and RAN == []
        assert [c.approval_id for c in engine_of(reg).approvals.pending()] == [card.approval_id]
        ui.decide(card, "deny")
        return await outcome(task), card

    got, card = run(scenario)
    assert (card.tool, card.tier, card.caller) == (attack.tool, tier, attack.caller)
    assert [p.mid for p in card.provenance] == list(attack.provenance)
    assert isinstance(got, ToolError) and (got.code, got.reason) == ("denied", "user_denied")
    assert rows(audit) == [(attack.tool, tier, "denied", "user_denied")]
    assert RAN == [] and world.writes() == []
    assert_nothing_sent_before_approval(world)


def test_set_tags_on_21_mids_produces_a_w2_card_listing_every_target(
    reg: Registry, world: World, attacker: AttackerModel, writable: list[str]
) -> None:
    async def scenario() -> ApprovalCard:
        ui = attach(reg, world)
        task = launch(reg, attacker.set_tags(21))
        card = await card_for(ui, task)
        ui.decide(card, "deny")
        await outcome(task)
        return card

    card = run(scenario)
    assert card.tier is Tier.W2 and [t.mid for t in card.targets] == writable[:21]


def test_a_draft_to_an_unknown_address_lists_it_unknown(
    reg: Registry, world: World, attacker: AttackerModel
) -> None:
    async def scenario() -> ApprovalCard:
        ui = attach(reg, world)
        task = launch(reg, attacker.draft_to_unknown())
        card = await card_for(ui, task)
        ui.decide(card, "deny")
        await outcome(task)
        return card

    card = run(scenario)
    assert card.recipients is not None
    assert [(r.addr, r.known) for r in card.recipients] == [(EXFIL, False)]


def test_a_thread_hijack_reply_still_counts_the_injected_address_unknown(
    reg: Registry, world: World, attacker: AttackerModel
) -> None:
    # The provider knows the thread's genuine participants; the forged In-Reply-To/References
    # put the hijacker in that thread, but the engine asks about each address only, never about
    # thread membership.
    assert HIJACK["from_in_sent_recipients"] is False
    attack = attacker.thread_hijack_reply()

    async def scenario() -> ApprovalCard:
        ui = attach(reg, world)
        task = launch(reg, attack)
        card = await card_for(ui, task)
        ui.decide(card, "deny")
        await outcome(task)
        return card

    card = run(scenario)
    assert card.tier is Tier.W2 and card.recipients is not None
    assert [(r.addr, r.known) for r in card.recipients] == [
        (HIJACK["from"].lower(), False),
        ("nikos@example.test", True),
    ]
    # Lower-cased addresses only: no References/In-Reply-To value reached the provider.
    assert BOOK.asked == [HIJACK["from"].lower(), "nikos@example.test"]
    assert [t.mid for t in card.targets] == [HIJACK_MID]  # the reply's target, from the index
    assert card.args_view["references"] == [HIJACKED_THREAD_HMID]  # shown, never trusted


def test_with_ternds_deny_all_stub_even_a_known_contact_draft_is_w2(
    make_registry: Any, world: World, fixture_store: Store, attacker: AttackerModel
) -> None:
    # ternd runs with DenyAllRecipients until plan 10 injects the real provider.
    reg = make_registry(
        TOOLS,
        bridge=Recorder(world),
        index=fixture_store,
        known_recipients=DenyAllRecipients(),
        sleep=FakeSleep(),
    )

    async def scenario() -> ApprovalCard:
        ui = attach(reg, world)
        task = launch(reg, attacker.draft_to(KNOWN_CONTACT))
        card = await card_for(ui, task)
        ui.decide(card, "deny")
        await outcome(task)
        return card

    card = run(scenario)
    assert card.tier is Tier.W2 and card.recipients is not None
    assert [(r.addr, r.known) for r in card.recipients] == [(KNOWN_CONTACT, False)]
    assert RAN == [] and world.writes() == []


# ---- the contact swap, then a draft to the new address ------------------------------------------


def test_a_contact_swap_blocks_as_w3_and_the_draft_stays_w2_while_pending_and_after_deny(
    reg: Registry, world: World, audit: AuditLog, attacker: AttackerModel
) -> None:
    new = "maria.k@attacker.test"

    async def scenario() -> tuple[ApprovalCard, list[ApprovalCard]]:
        ui = attach(reg, world)
        contact = launch(reg, attacker.change_contact(new))
        contact_card = await card_for(ui, contact)
        drafts: list[ApprovalCard] = []
        # While the contact write is pending: the provider has not learned the address.
        first = launch(reg, attacker.draft_to(new))
        drafts.append(await card_for(ui, first, n=2))
        ui.decide(drafts[0], "deny")
        await outcome(first)
        assert not contact.done()
        ui.decide(contact_card, "deny")
        assert isinstance(await outcome(contact), ToolError)
        # After the contact write was denied: still unknown.
        second = launch(reg, attacker.draft_to(new))
        drafts.append(await card_for(ui, second, n=3))
        ui.decide(drafts[1], "deny")
        await outcome(second)
        return contact_card, drafts

    contact_card, drafts = run(scenario)
    assert contact_card.tier is Tier.W3
    # The card shows the new address (the old -> new diff is plan 14's card content).
    assert contact_card.args_view == {"contact": KNOWN_CONTACT, "new_email": new}
    for card in drafts:
        assert card.tier is Tier.W2 and card.recipients is not None
        assert [(r.addr, r.known) for r in card.recipients] == [(new, False)]
    assert new not in BOOK.known and RAN == []
    assert rows(audit) == sorted(
        [
            ("upsert_contact", Tier.W3, "denied", "user_denied"),
            ("draft_reply", Tier.W2, "denied", "user_denied"),
            ("draft_reply", Tier.W2, "denied", "user_denied"),
        ]
    )


def test_the_provider_learns_a_contact_only_when_its_write_executes(
    reg: Registry, world: World, attacker: AttackerModel
) -> None:
    # The control for the case above: the draft's tier depends on the contact write executing.
    new = "maria.k@attacker.test"

    async def scenario() -> ToolResult | ToolError:
        attach(reg, world, lambda card: Scripted("approve"))
        return await outcome(launch(reg, attacker.change_contact(new)))

    got = run(scenario)
    assert isinstance(got, ToolResult) and RAN == ["upsert_contact"] and new in BOOK.known


# ---- denied by the engine before any approval ---------------------------------------------------


def test_an_unknown_filter_tool_is_unknown_tool(
    reg: Registry, world: World, audit: AuditLog, attacker: AttackerModel
) -> None:
    async def scenario() -> tuple[ToolResult | ToolError, UI]:
        ui = attach(reg, world, lambda card: Scripted("approve"))
        return await outcome(launch(reg, attacker.create_filter())), ui

    got, ui = run(scenario)
    assert isinstance(got, ToolError) and got.code == "unknown_tool"
    assert rows(audit) == [("create_filter", None, "error", "unknown_tool")]
    assert ui.cards == [] and world.methods == []


def test_a_plugin_declaring_filters_write_is_denied_before_any_approval_or_tool_code(
    reg: Registry, world: World, audit: AuditLog, attacker: AttackerModel
) -> None:
    assert Effect.FILTERS_WRITE in FORBIDDEN_EFFECTS

    async def scenario() -> tuple[ToolResult | ToolError, UI]:
        ui = attach(reg, world, lambda card: Scripted("approve"))  # would approve anything
        return await outcome(launch(reg, attacker.add_filter_rule())), ui

    got, ui = run(scenario)
    assert isinstance(got, ToolError)
    assert (got.code, got.reason) == ("denied", "forbidden_effect")
    assert ui.cards == [] and world.methods == []
    # Neither the In-model validator, count_targets nor the body ran.
    assert RAN == []
    (rec,) = audit.list()
    assert (rec.tool, rec.tier, rec.decision, rec.reason) == (
        "add_filter_rule",
        Tier.W3,
        "denied",
        "forbidden_effect",
    )
    assert rec.args is None and rec.initial_status is None  # never validated, never executed


def test_without_an_engine_a_filters_write_plugin_is_still_not_run(
    make_registry: Any, world: World, fixture_store: Store, attacker: AttackerModel
) -> None:
    # 06a behaviour without an engine: every write tier is tier_not_enabled.
    reg = make_registry(TOOLS, engine=False, bridge=Recorder(world), index=fixture_store)
    a = attacker.add_filter_rule()
    got = run(lambda: attempt(reg, a))
    assert isinstance(got, ToolError) and got.code == "tier_not_enabled"
    assert "add_filter_rule" not in RAN and world.methods == []


@pytest.mark.parametrize("i", range(4), ids=["set_tags", "move", "delete", "reply_draft"])
def test_every_write_on_the_conflicted_mid_is_refused(
    reg: Registry, world: World, audit: AuditLog, attacker: AttackerModel, i: int
) -> None:
    assert CONFLICTED["expected_conflicted"] is True
    attack = attacker.conflicted_writes()[i]

    async def scenario() -> tuple[ToolResult | ToolError, UI]:
        ui = attach(reg, world, lambda card: Scripted("approve"))  # would approve anything
        return await outcome(launch(reg, attack)), ui

    got, ui = run(scenario)
    assert isinstance(got, ToolError) and (got.code, got.reason) == (
        "mid_conflicted",
        "mid_conflicted",
    )
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.initial_status) == ("error", "mid_conflicted", None)
    assert ui.cards == [] and RAN == [] and world.methods == []  # refused before any find


# ---- approvals: who may decide, superset re-binding, moved targets, flooding --------------------


def test_an_mcp_caller_cannot_decide_an_approval(
    reg: Registry, world: World, audit: AuditLog, attacker: AttackerModel
) -> None:
    async def scenario() -> ToolResult | ToolError:
        ui = attach(reg, world)
        eng = engine_of(reg)
        task = launch(reg, attacker.move_to_trash())
        card = await card_for(ui, task)
        for caller in ("mcp:conn-7", attacker.caller):
            with pytest.raises(DecideRefused) as info:
                eng.approvals.decide(card.approval_id, card.nonce, "approve", caller=caller)
            assert (info.value.code, info.value.reason) == ("denied", "approval_role")
        assert not task.done() and audit.count() == 0
        assert [c.approval_id for c in eng.approvals.pending()] == [card.approval_id]
        ui.decide(card, "deny")
        return await outcome(task)

    got = run(scenario)
    assert isinstance(got, ToolError) and got.reason == "user_denied"
    assert RAN == [] and world.writes() == []


def test_an_approval_cannot_be_rebound_to_a_superset(
    reg: Registry,
    world: World,
    audit: AuditLog,
    attacker: AttackerModel,
    writable: list[str],
    fixture_store: Store,
) -> None:
    attack = attacker.set_tags(21)  # a W2 card
    keep = writable[:3]

    async def scenario() -> tuple[ToolResult | ToolError, ToolResult | ToolError]:
        ui = attach(reg, world)
        eng = engine_of(reg)
        # The user deselects down to three targets; afterwards the attacker tries to bind the
        # approval to every target (and one more) again.
        task = launch(reg, attack)
        card = await card_for(ui, task)
        assert ui.decide(card, "approve", keep) == "approved"
        with pytest.raises(DecideRefused) as info:
            eng.approvals.decide(
                card.approval_id, card.nonce, "approve", [*writable[:21], writable[21]], caller="ui"
            )
        assert info.value.reason == "unknown_approval"
        narrowed = await outcome(task)
        # A fresh approval decided with a superset of its targets is denied outright.
        task2 = launch(reg, attacker.set_tags(21))
        card2 = await card_for(ui, task2, n=2)
        assert ui.decide(card2, "approve", writable[:22]) == "denied"
        return narrowed, await outcome(task2)

    narrowed, superset = run(scenario)
    assert isinstance(narrowed, ToolResult) and narrowed.output == Out(n=3)
    rec = audit.get(narrowed.call_id)
    assert rec.target_mids == tuple(keep) and rec.original_args_sha256 is not None
    assert isinstance(superset, ToolError)
    assert (superset.code, superset.reason) == ("denied", "binding_mismatch")
    assert world.writes() == ["mail.update"]  # the three kept targets, once
    assert_nothing_sent_before_approval(world)

    async def tagged(mid: str) -> bool:
        msg = fixture_store.get_message(mid)
        assert msg is not None and msg.header_message_id is not None
        found = await world.fb.request(
            "mail.messages.find", FindParams(header_message_id=msg.header_message_id)
        )
        return any("$label1" in f.tags for f in found.root)

    assert [run(lambda m=m: tagged(m)) for m in writable[:5]] == [True, True, True, False, False]


def test_an_approval_executed_after_its_targets_moved_is_denied(
    reg: Registry, world: World, audit: AuditLog, attacker: AttackerModel, writable: list[str]
) -> None:
    async def scenario() -> ToolResult | ToolError:
        ui = attach(reg, world)
        task = launch(reg, attacker.bulk_move_to_trash())  # W3: 21 targets
        card = await card_for(ui, task)
        # The message is moved (by TB, a filter, the attacker's other call) while the card waits.
        fb = world.fb
        target = writable[1]
        hmid_ = fixture_hmid(reg, target)
        (found,) = (
            await fb.request("mail.messages.find", FindParams(header_message_id=hmid_))
        ).root
        await fb.move(
            [found.tb_id],
            "acct1:/Archive" if found.folder_id != "acct1:/Archive" else "acct1:/Invoices",
        )
        ui.decide(card, "approve")
        return await outcome(task)

    got = run(scenario)
    assert isinstance(got, ToolError) and got.code == "targets_changed"
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.initial_status) == ("approved", "targets_changed", None)
    assert RAN == [] and world.writes() == []


def fixture_hmid(reg: Registry, mid: str) -> str:
    msg = engine_of(reg).index.get_message(mid)
    assert msg is not None and msg.header_message_id is not None
    return msg.header_message_id


def test_a_flood_of_w2_calls_is_capped_at_five_pending(
    reg: Registry, world: World, audit: AuditLog, attacker: AttackerModel, writable: list[str]
) -> None:
    assert Limits().max_pending_per_caller == 5

    async def scenario() -> list[ToolResult | ToolError]:
        ui = attach(reg, world)
        flood = [
            launch(reg, Attack("move_messages", {"mids": [m], "dest_folder_id": TRASH}))
            for m in writable[:8]
        ]
        await until(lambda: len(ui.cards) == 5 and sum(t.done() for t in flood) == 3, "the cap")
        assert len(engine_of(reg).approvals.pending("agent:attacker")) == 5
        for card in list(ui.cards):
            ui.decide(card, "deny")
        return [await outcome(t) for t in flood]

    got = run(scenario)
    reasons = sorted(str(r.reason) for r in got if isinstance(r, ToolError))
    assert reasons == ["pending_cap"] * 3 + ["user_denied"] * 5
    assert [r for r in got if isinstance(r, ToolError) and r.reason == "pending_cap"][0].code == (
        "rate_limited"
    )
    assert sorted(str(x[3]) for x in rows(audit)) == ["rate_limited"] * 3 + ["user_denied"] * 5
    assert RAN == [] and world.writes() == []


# ---- W1: escalation boundary, window cap, undo --------------------------------------------------


def test_w1_on_20_mids_runs_and_is_undoable(
    reg: Registry, world: World, audit: AuditLog, attacker: AttackerModel, writable: list[str]
) -> None:
    async def scenario() -> tuple[ToolResult | ToolError, list[str]]:
        ui = attach(reg, world, lambda card: Scripted("approve"))
        got = await outcome(launch(reg, attacker.set_tags(20)))
        assert ui.cards == []  # W1: no card
        assert isinstance(got, ToolResult) and got.undo_token is not None
        with pytest.raises(ToolError) as info:  # the attacker cannot undo (or redo) it
            await reg.undo(attacker.caller, got.undo_token)
        assert info.value.reason == "undo_not_allowed"
        await reg.undo("ui", got.undo_token)
        return got, world.writes()

    got, writes = run(scenario)
    assert isinstance(got, ToolResult) and got.output == Out(n=20)
    assert audit.get(got.call_id).tier is Tier.W1 and audit.get(got.call_id).undone
    assert writes[0] == "mail.update" and len(writes) == 1 + 20  # the write, then 20 inverses

    async def labels(mid: str) -> list[str]:
        found = await world.fb.request(
            "mail.messages.find", FindParams(header_message_id=fixture_hmid(reg, mid))
        )
        return [t for f in found.root for t in f.tags]

    assert all(run(lambda m=m: labels(m)) == [] for m in writable[:20])


LOW = Limits(w1_per_caller=5, w1_global=10, window_s=60)
SIZES = [
    pytest.param(Limits(), id="production", marks=pytest.mark.slow),
    pytest.param(LOW, id="lowered"),
]


def window_registry(
    make_registry: Any, world: World, fixture_store: Store, limits: Limits
) -> Registry:
    return make_registry(
        TOOLS, bridge=Recorder(world), index=fixture_store, known_recipients=BOOK, limits=limits
    )


async def sequential(reg: Registry, attacks: list[Attack]) -> list[str]:
    out: list[str] = []
    for a in attacks:
        got = await outcome(launch(reg, a))
        out.append("ok" if isinstance(got, ToolResult) else f"{got.code}/{got.reason}")
    return out


@pytest.mark.parametrize("limits", SIZES)
def test_single_mid_w1_calls_on_one_connection_are_capped_by_the_window(
    make_registry: Any,
    world: World,
    audit: AuditLog,
    fixture_store: Store,
    writable: list[str],
    limits: Limits,
) -> None:
    per = limits.w1_per_caller
    n = per + per // 5  # 600 at the production limits
    reg = window_registry(make_registry, world, fixture_store, limits)
    attacks = [
        Attack("set_tags", {"mids": [writable[i % 25]], "tags": ["$label1"]}, caller="mcp:conn-1")
        for i in range(n)
    ]

    got = run(lambda: sequential(reg, attacks))
    assert got == ["ok"] * per + ["rate_limited/caller_window"] * (n - per)
    assert audit.count(caller="mcp:conn-1") == n
    assert audit.count(caller="mcp:conn-1", executed=True) == per


@pytest.mark.parametrize("limits", SIZES)
def test_calls_over_three_connections_with_rotating_names_hit_the_global_ceiling(
    make_registry: Any,
    world: World,
    audit: AuditLog,
    fixture_store: Store,
    writable: list[str],
    limits: Limits,
) -> None:
    total = limits.w1_global
    n = total + total // 10  # 1100 at the production limits
    reg = window_registry(make_registry, world, fixture_store, limits)
    # Round robin over three proxy connections; the client renames itself on every call.
    attacks = [
        Attack(
            "set_tags",
            {"mids": [writable[i % 25]], "tags": ["$label1"]},
            caller=f"mcp:conn-{i % 3}",
            label=f"client-{i}",
        )
        for i in range(n)
    ]
    assert n // 3 + 1 <= limits.w1_per_caller  # no single connection reaches its own window

    got = run(lambda: sequential(reg, attacks))
    assert got == ["ok"] * total + ["rate_limited/global_window"] * (n - total)


# ---- the bridge server: role checks before any handler ------------------------------------------


@dataclass
class Server:
    server: BridgeServer
    sock: Path


@pytest_asyncio.fixture
async def server() -> AsyncIterator[Server]:
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    srv_obj = BridgeServer(
        process_info=FakeProcessInfo(),
        thunderbird_bin=TB_BIN,
        nmhost_path=NMHOST,
        link=ExtensionLink(wait_s=1.0),
        liveness_ping_s=0.2,
    )
    srv = await uds.start(srv_obj, tp.socket)
    yield Server(srv_obj, tp.socket)
    srv.close()
    await srv_obj.close_all()
    await srv.wait_closed()


@pytest.mark.asyncio
async def test_an_mcp_proxy_approvals_decide_is_role_rejected_before_any_handler(
    server: Server,
) -> None:
    reached: list[str] = []

    async def handler(peer: Peer, params: BaseModel) -> BaseModel:
        reached.append(peer.role or "?")
        raise BridgeError("unavailable", "recorded")

    server.server.handlers["approvals.decide"] = handler
    params = {"approval_id": "a" * 32, "nonce": "n" * 32, "decision": "approve"}
    proxy = await connect(server.sock, "mcp-proxy")
    with pytest.raises(BridgeError) as exc:
        await proxy.peer.request("approvals.decide", params, timeout=2)
    assert exc.value.name == "role_rejected"
    await proxy.close()
    # The control: the extension does reach the handler.
    ext = await connect(server.sock, "extension")
    with pytest.raises(BridgeError) as exc:
        await ext.peer.request("approvals.decide", params, timeout=2)
    assert exc.value.name == "unavailable"
    await ext.close()
    assert reached == ["extension"]


@pytest.mark.asyncio
async def test_an_mcp_proxy_audit_undo_is_role_rejected(
    server: Server, reg: Registry, world: World, audit: AuditLog, attacker: AttackerModel
) -> None:
    server.server.handlers["audit.undo"] = audit_undo_handler(reg)
    got = await reg.call(attacker.caller, "set_tags", attacker.set_tags(1).args)
    assert got.undo_token is not None
    before = audit.count()
    proxy = await connect(server.sock, "mcp-proxy")
    with pytest.raises(BridgeError) as exc:
        await proxy.peer.request("audit.undo", {"undo_token": got.undo_token}, timeout=10)
    assert exc.value.name == "role_rejected"
    await proxy.close()
    # Refused before the registry: no undo row, nothing written, the token still unused.
    assert audit.count() == before and not audit.get(got.call_id).undone
    assert world.writes() == ["mail.update"]
    assert not audit.undo_token(got.undo_token).used
