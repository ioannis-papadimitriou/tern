"""Tool inventory (plan 06a step 6; T6, #8, #9): no tool can send, none can write filters, and the
built-ins match architecture §5's "Default toolset (v1)" table.

The inventory checks run over what the registry really holds (built-ins + the entry points of
this environment, as ternd and ``tern tools describe`` discover them). Each check is a helper
that returns its offenders, and each helper is also run over a planted offender (a tool, a spec,
an effect-to-method map), so a check that cannot fail is caught here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import pytest

from tern.tools import BoundaryModel, Effect, Tier, ToolSpec, tool
from tern.tools import registry as reg
from tern.tools.builtin import BUILTINS
from tern.tools.context import EFFECT_METHODS
from tern.tools.registry import RegisteredTool, Registry

ARCHITECTURE = Path(__file__).resolve().parents[2] / "docs" / "architecture.md"
TABLE_HEADING = "### Default toolset (v1)"

#: Whole name tokens that mean "mail leaves the machine" (or a receipt/invitation does).
SEND_TOKENS = frozenset(
    {
        "send",
        "resend",
        "sendmail",
        "submit",
        "transmit",
        "dispatch",
        "deliver",
        "forward",
        "fwd",
        "reply",
        "respond",
        "bounce",
        "redirect",
        "outbox",
        "smtp",
        "mdn",
        "receipt",
        "invite",
        "rsvp",
        "post",
        "publish",
        "upload",
        "network",
        "http",
        "egress",
    }
)
#: Substrings that are send-like wherever they sit in a name ("sendnow", "autosend", "smtprelay").
SEND_SUBSTRINGS = ("send", "smtp", "outbox", "deliver", "transmit", "dispatch")

#: The read-only effects a built-in may declare in 06a.
READ_EFFECTS = frozenset({Effect.READ_INDEX, Effect.READ_LIVE})

#: #6's closed effect set: no network or send effect exists (#8, #9).
EFFECTS_6 = frozenset(
    {
        "read_index",
        "read_live",
        "mail_meta",
        "mail_move",
        "mail_delete",
        "drafts",
        "contacts_write",
        "calendar_write",
        "filters_write",
    }
)


def _tokens(name: str) -> list[str]:
    """``name`` split on ``_``, ``.``, ``-`` and camelCase humps, lower-cased."""
    # A break after a lowercase letter/digit ("sendMessage") and inside a capital run before a
    # capitalised word ("HTTPPost" -> HTTP_Post).
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return [t for t in re.split(r"[_.\-\s]+", spaced.lower()) if t]


def send_like(name: str) -> bool:
    """True when ``name`` (a tool name, effect value or bridge method) reads as sending."""
    low = name.lower()
    return any(t in SEND_TOKENS for t in _tokens(name)) or any(s in low for s in SEND_SUBSTRINGS)


#: Reviewed built-in names whose send-like token is a false positive: ``tb_awaiting_reply`` (the
#: vendored thunderbird-mcp tool, upstream's name kept) lists threads where the user waits for an
#: answer. Exempt only as a built-in that stays R0/R1 with read effects only (``reviewed_read``).
REVIEWED_READ_NAMES = frozenset({"tb_awaiting_reply"})


def reviewed_read(t: RegisteredTool) -> bool:
    """``t`` is a reviewed built-in read tool (``REVIEWED_READ_NAMES``) that still only reads."""
    return (
        t.origin == "builtin"
        and t.spec.name in REVIEWED_READ_NAMES
        and non_read_builtins([t.spec]) == []
    )


def send_like_tools(tools: Iterable[RegisteredTool]) -> list[str]:
    """``<source>: <name>`` of every tool with a send-like name or effect (``reviewed_read``
    names excepted)."""
    return [
        f"{t.source}: {t.spec.name}"
        for t in tools
        if (send_like(t.spec.name) and not reviewed_read(t))
        or any(send_like(e.value) for e in t.spec.effects)
    ]


def filter_writers(tools: Iterable[RegisteredTool]) -> list[str]:
    """``<source>: <name>`` of every tool declaring ``filters_write`` (T6)."""
    return [f"{t.source}: {t.spec.name}" for t in tools if Effect.FILTERS_WRITE in t.spec.effects]


def send_like_methods(effect_methods: Mapping[Effect, Iterable[str]]) -> list[str]:
    """``<effect>: <method>`` of every send-like bridge method an effect's scope allows."""
    return sorted(f"{e.value}: {m}" for e, ms in effect_methods.items() for m in ms if send_like(m))


def non_read_builtins(specs: Iterable[ToolSpec]) -> list[str]:
    """``<name>: <why>`` of every spec with no effect, a non-read effect or a write tier."""
    out: list[str] = []
    for spec in specs:
        if not spec.effects:
            out.append(f"{spec.name}: no effect")
        extra = sorted(e.value for e in spec.effects - READ_EFFECTS)
        if extra:
            out.append(f"{spec.name}: effects {extra}")
        if spec.effective_tier not in {Tier.R0, Tier.R1}:
            out.append(f"{spec.name}: tier {spec.effective_tier}")
    return out


def _registered(registry: Registry) -> list[RegisteredTool]:
    found: list[RegisteredTool] = []
    for name in registry.names():
        t = registry.get(name)
        assert t is not None
        found.append(t)
    return found


#: What ternd (``Registry.discover()``, no config paths) and ``tern tools describe`` hold;
#: built when each test runs, not at collection.
LIVE = pytest.mark.parametrize(
    "live", [Registry.discover, reg.describe_registry], ids=["discover", "describe"]
)


# -- the architecture table --------------------------------------------------------------------


def _table() -> dict[str, tuple[str, str]]:
    """Architecture §5's default-toolset table: tool name → (tier cell, untrusted_output cell)."""
    lines = ARCHITECTURE.read_text(encoding="utf-8").splitlines()
    start = lines.index(TABLE_HEADING) + 1
    rows: dict[str, tuple[str, str]] = {}
    in_table = False
    for line in lines[start:]:
        if not line.startswith("|"):
            if in_table:
                break
            continue
        in_table = True
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        assert len(cells) == 4, f"architecture §5 table row has {len(cells)} cells: {line[:40]}"
        if cells[0] == "Tool" or set(cells[0]) <= {"-"}:
            continue
        names = re.findall(r"`([a-z][a-z0-9_]*)`", cells[0])
        assert names, f"architecture §5 table row names no tool: {line[:40]}"
        for n in names:
            assert n not in rows, f"architecture §5 table lists {n} twice"
            rows[n] = (cells[1], cells[3])
    return rows


def table_mismatches(specs: Iterable[ToolSpec], rows: Mapping[str, tuple[str, str]]) -> list[str]:
    """``<name>: <why>`` of every spec whose tier or untrusted flag differs from ``rows``."""
    tiers = {t.value for t in Tier}
    out: list[str] = []
    for spec in specs:
        if spec.name not in rows:
            out.append(f"{spec.name}: not in architecture §5's table")
            continue
        tier_cell, untrusted_cell = rows[spec.name]
        if tier_cell not in tiers:
            out.append(f"{spec.name}: non-plain tier cell {tier_cell!r}")
        elif spec.effective_tier != Tier(tier_cell):
            out.append(f"{spec.name}: tier {spec.effective_tier} != table {tier_cell}")
        if untrusted_cell not in {"yes", "no"}:
            out.append(f"{spec.name}: untrusted cell {untrusted_cell!r}")
        elif spec.untrusted_output != (untrusted_cell == "yes"):
            out.append(
                f"{spec.name}: untrusted_output {spec.untrusted_output} != table {untrusted_cell}"
            )
    return out


def test_architecture_table_parses() -> None:
    rows = _table()
    # A sample of rows the parser must find, including a multi-tool row and the escaped `\|`.
    assert {"search_messages", "open_in_thunderbird", "set_flag", "extract_items"} <= set(rows)
    assert rows["extract_items"] == ("R0", "yes")
    assert rows["delete_messages"] == ("W3", "no")


def test_every_builtin_matches_the_architecture_table() -> None:
    assert BUILTINS, "no built-in tools registered"
    assert table_mismatches(BUILTINS, _table()) == []


def test_builtin_metadata_matches_the_spec_through_describe() -> None:
    # What `tern tools describe` prints carries the same tier/untrusted values as the specs.
    metas = {m.name: m for m in reg.describe_registry().describe() if m.origin == "builtin"}
    assert set(metas) == {s.name for s in BUILTINS}
    for spec in BUILTINS:
        m = metas[spec.name]
        assert (m.effective_tier, m.untrusted_output) == (
            spec.effective_tier,
            spec.untrusted_output,
        )


def test_builtins_declare_only_read_effects() -> None:
    assert BUILTINS, "no built-in tools registered"
    assert non_read_builtins(BUILTINS) == []


# -- no send, no filters (T6) ------------------------------------------------------------------


@LIVE
def test_no_tool_has_a_send_like_name_or_effect(live: Callable[[], Registry]) -> None:
    registry = live()
    assert len(registry) >= len(BUILTINS)
    assert send_like_tools(_registered(registry)) == []


@LIVE
def test_no_tool_declares_filters_write(live: Callable[[], Registry]) -> None:
    registry = live()
    assert len(registry) >= len(BUILTINS)
    assert filter_writers(_registered(registry)) == []


def test_the_effect_set_is_closed_and_has_no_send_effect() -> None:
    assert {e.value for e in Effect} == EFFECTS_6
    assert [e.value for e in Effect if send_like(e.value)] == []


def test_no_effect_allows_a_send_like_bridge_method() -> None:
    assert set(EFFECT_METHODS) == set(Effect)
    assert any(EFFECT_METHODS.values()), "no effect allows any bridge method"
    assert send_like_methods(EFFECT_METHODS) == []


# -- the checks can fail -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "send_message",
        "send",
        "sendmail",
        "autosend_reply",
        "resend_draft",
        "submit_draft",
        "forward_message",
        "reply_all",
        "smtp_relay",
        "flush_outbox",
        "deliver_now",
        "send_mdn",
        "mail.compose.send",
        "messages.sendMessage",
        "HTTPPost",
        "SMTPRelay",
    ],
)
def test_send_like_flags_send_names(name: str) -> None:
    assert send_like(name)


def test_send_like_spares_the_planned_v1_tools_and_effects() -> None:
    # Every v1 tool architecture §5 plans (and the post-MVP calendar names), every effect value and
    # every bridge method an effect allows stays legal.
    planned = (
        set(_table())
        | {"calendar_list_events", "calendar_create_event"}
        | {e.value for e in Effect}
        | {m for ms in EFFECT_METHODS.values() for m in ms}
    )
    # ...except the reviewed read names, which the table lists as R0 read tools.
    assert [n for n in sorted(planned) if send_like(n)] == sorted(REVIEWED_READ_NAMES)


def test_reviewed_read_names_are_exempt_only_as_read_builtins() -> None:
    @tool(
        name="tb_awaiting_reply", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True
    )
    async def _read(ctx: object, inp: _In) -> _Out:
        """A reviewed read name."""
        return _Out(ok=True)

    @tool(
        name="tb_awaiting_reply",
        tier=Tier.W1,
        effects=[Effect.DRAFTS],
        untrusted_output=False,
        count_targets=lambda i: 1,
    )
    async def _writer(ctx: object, inp: _In) -> _Out:
        """The same name on a write tool."""
        return _Out(ok=True)

    assert send_like_tools([RegisteredTool(_read, "builtin", "builtin")]) == []
    assert send_like_tools([RegisteredTool(_read, "plugin", "planted")]) == [
        "planted: tb_awaiting_reply"
    ]
    assert send_like_tools([RegisteredTool(_writer, "builtin", "builtin")]) == [
        "builtin: tb_awaiting_reply"
    ]


class _In(BoundaryModel):
    rule: str


class _Out(BoundaryModel):
    ok: bool


@tool(name="send_message", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=False)
async def _send_message(ctx: object, inp: _In) -> _Out:
    """A planted send-like name."""
    return _Out(ok=True)


@tool(
    name="add_rule",
    tier=Tier.W3,
    effects=[Effect.FILTERS_WRITE],
    untrusted_output=False,
    count_targets=lambda i: 1,
)
async def _add_rule(ctx: object, inp: _In) -> _Out:
    """A planted filter writer."""
    return _Out(ok=True)


def _with(spec: ToolSpec) -> Registry:
    return Registry([*reg.builtin_tools(), RegisteredTool(spec, "plugin", "planted")])


def test_send_like_tools_catches_a_planted_tool() -> None:
    assert send_like_tools(_registered(_with(_send_message))) == ["planted: send_message"]
    assert filter_writers(_registered(_with(_send_message))) == []


def test_filter_writers_catches_a_planted_tool() -> None:
    assert filter_writers(_registered(_with(_add_rule))) == ["planted: add_rule"]
    assert send_like_tools(_registered(_with(_add_rule))) == []


# Planted specs for the table and read-only checks: real table names with a wrong tier or flag.
@tool(name="search_messages", tier=Tier.R1, effects=[Effect.READ_INDEX], untrusted_output=True)
async def _wrong_tier(ctx: object, inp: _In) -> _Out:
    """search_messages is R0 in the table."""
    return _Out(ok=True)


@tool(name="open_in_thunderbird", tier=Tier.R1, effects=[Effect.READ_LIVE], untrusted_output=True)
async def _wrong_untrusted(ctx: object, inp: _In) -> _Out:
    """open_in_thunderbird's output is not untrusted in the table."""
    return _Out(ok=True)


def test_table_mismatches_catches_planted_specs() -> None:
    rows = _table()
    assert table_mismatches([_wrong_tier], rows) == ["search_messages: tier R1 != table R0"]
    assert table_mismatches([_wrong_untrusted], rows) == [
        "open_in_thunderbird: untrusted_output True != table no"
    ]
    assert table_mismatches([_send_message], rows) == [
        "send_message: not in architecture §5's table"
    ]
    odd = {"search_messages": ("R0 / W2", "maybe")}
    assert table_mismatches([_wrong_tier], odd) == [
        "search_messages: non-plain tier cell 'R0 / W2'",
        "search_messages: untrusted cell 'maybe'",
    ]


def test_non_read_builtins_catches_a_planted_writer() -> None:
    assert non_read_builtins([_wrong_tier, _wrong_untrusted]) == []
    assert non_read_builtins([_add_rule]) == [
        "add_rule: effects ['filters_write']",
        "add_rule: tier W3",
    ]


def test_send_like_methods_catches_a_planted_method() -> None:
    planted = {**EFFECT_METHODS, Effect.DRAFTS: frozenset({"mail.draft.save", "mail.compose.send"})}
    assert send_like_methods(planted) == ["drafts: mail.compose.send"]
