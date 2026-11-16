"""The thunderbird-mcp tools as tern-tool/1 built-ins (vendored ``tern.tbmcp``; decision of
2026-11-16, amending #20/#22).

Each ``tb_*`` tool has the upstream tool's name, parameters, defaults and description (the
model-facing text, verbatim from upstream's ``server.py``). Like upstream's ``_call``, the blocking
query runs in a worker thread and its result goes through ``guard.fit`` (the output budget,
``tb_max_output_chars``). Upstream's own audit log is not used: ``Registry.call`` audits every call.

The output is :class:`TbOut`: the upstream result as ``result`` (a JSON value; null where
upstream returns None, e.g. an unknown ``message_id``), with one addition (plan 08c, #38): every
object that carries a ``header_message_id`` also gets ``mid``, tern's short id of that message
(``tern.tools.ids.short_id`` of ``tern.index.mid.mid_of``). The model cites ``[mid]``; ternd turns a
valid citation into the full mid, so chips open the message. Upstream's ``message_id`` stays the id
for tool calls.

Spoofed copies (plan 08c): GLODA collapses rows by Message-ID, so of several DIFFERENT messages
sharing one (a look-alike and the genuine mail, #5) only one would show. For every mid tern's
read-only index marks ``conflicted`` (``Store.conflicted_mids``), the vendored queries list each
GLODA row of that Message-ID (``db.set_split``), and every object with that mid gets
``"conflicted": true`` and :data:`CONFLICT_WARNING`. Citing such a mid stays refused (#5).

Settings: before every call, :func:`configure` installs the call's ``ctx.config`` into the vendored
modules (``guard.configure`` for the ``tb_*`` guard keys, ``config.set_profile_dir`` for
``tb_profile_dir``, ``snapshot.set_cache_dir`` for ``<state dir>/tb-snapshot``). These are
process-wide module settings (one config per ternd), never environment variables. The state dir is
the runtime's (``ctx.state_dir``, wired by ternd, smoke, the probe and tests), never
``paths.resolve()``: an in-process caller without ``TERN_HOME`` must not put snapshot copies
into the user's real state dir. A runtime without one makes every ``tb_*`` call ``unavailable``
(``tb_no_state_dir``).

Errors: no Thunderbird profile or database (``FileNotFoundError``) and a snapshot that cannot be
copied consistently are ``unavailable``; a SQLite error is ``internal`` (reason code only, never
the text). Dates and ``HH:MM`` times that upstream would fail to parse are refused by the input
models (``invalid_params``).

Effects: ``read_index`` (R0, no bridge method): the tools read local snapshot copies, never
Thunderbird itself; no ``Effect`` names that more precisely, and the effect set is closed
(``tern-tool/1``). Outputs holding mail, calendar or contact text are ``untrusted_output``.
"""

from __future__ import annotations

import asyncio
import functools
import sqlite3
from collections.abc import Callable
from datetime import datetime, time
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, JsonValue

from tern._model import BoundaryModel
from tern.config import TernConfig
from tern.index.mid import TB_MD5_PREFIX, mid_of
from tern.tbmcp import api, briefing, calendar_db, db, guard, snapshot
from tern.tbmcp import config as tbconfig
from tern.tbmcp.api import INSTRUCTIONS
from tern.tools.context import ToolContext
from tern.tools.contract import Effect, Tier, ToolError, ToolSpec, tool
from tern.tools.ids import short_id

#: thunderbird-mcp's server instructions, for the chat agent's system prompt.
TB_INSTRUCTIONS = INSTRUCTIONS

#: The snapshot copies' directory under the runtime's state dir (``ctx.state_dir``).
SNAPSHOT_DIRNAME = "tb-snapshot"

#: The vendored agent skills (``tern/tbmcp/skills/<name>/SKILL.md``).
SkillName = Literal[
    "thunderbird-daily-brief",
    "thunderbird-follow-ups",
    "thunderbird-inbox-triage",
    "thunderbird-mail-research",
    "thunderbird-meeting-prep",
    "thunderbird-scheduling",
]


# ---- settings -----------------------------------------------------------------------------------


def guard_settings(config: TernConfig) -> dict[str, str | None]:
    """thunderbird-mcp's guard settings from tern's ``tb_*`` keys (None: upstream's default)."""
    return {
        "ACCOUNTS": ",".join(config.tb_accounts),
        "EXCLUDE_ACCOUNTS": ",".join(config.tb_exclude_accounts),
        "FOLDERS": ",".join(config.tb_folders),
        "EXCLUDE_FOLDERS": (
            None if config.tb_exclude_folders is None else ",".join(config.tb_exclude_folders)
        ),
        "CALENDARS": ",".join(config.tb_calendars),
        "EXCLUDE_CALENDARS": ",".join(config.tb_exclude_calendars),
        "MAX_AGE_DAYS": None if config.tb_max_age_days is None else str(config.tb_max_age_days),
        "PRIVATE_EVENTS": config.tb_private_events,
        "REDACT": "on" if config.tb_redact else "off",
        "MAX_OUTPUT_CHARS": str(config.tb_max_output_chars),
        "TIMEZONE": config.tb_timezone,
    }


def snapshot_dir(state_dir: Path) -> Path:
    """Where the snapshot copies live for tern state dir ``state_dir``."""
    return state_dir / SNAPSHOT_DIRNAME


def configure(
    config: TernConfig, state_dir: Path, conflicted: frozenset[str] = frozenset()
) -> None:
    """Install ``config``, the snapshot dir and the Message-IDs not to collapse (those whose mid
    is in ``conflicted``) into the vendored modules (module docstring)."""
    guard.configure(guard_settings(config))
    tbconfig.set_profile_dir(config.tb_profile_dir)
    snapshot.set_cache_dir(snapshot_dir(state_dir))
    db.set_split(functools.partial(_names_one_of, conflicted) if conflicted else None)


def header_mid(header: object) -> str | None:
    """tern's full mid of a GLODA ``header_message_id``; None when there is none: not a string,
    TB's invented ``md5:`` id (mail without a Message-ID: its mid needs the raw bytes, #5), or a
    header that normalizes to nothing. Never an invented id."""
    if not isinstance(header, str) or header.startswith(TB_MD5_PREFIX):
        return None
    return mid_of(header)


def _names_one_of(mids: frozenset[str], header: str) -> bool:
    """Whether GLODA's ``header`` is the Message-ID of one of ``mids``."""
    return header_mid(header) in mids


# ---- the call -----------------------------------------------------------------------------------


class TbOut(BoundaryModel):
    """A thunderbird-mcp tool's result, unchanged."""

    result: JsonValue = Field(description="the thunderbird-mcp tool's result")


async def _call(ctx: ToolContext, fn: Callable[..., Any], /, **kwargs: Any) -> TbOut:
    """Run a blocking call in a worker thread and apply the output budget (upstream's ``_call``
    without its audit log: ``Registry.call`` audits)."""
    if ctx.state_dir is None:
        raise ToolError(
            "unavailable",
            "this tool runtime has no state dir for the Thunderbird snapshot copies",
            reason="tb_no_state_dir",
        )
    conflicted = ctx.index.conflicted_mids()
    configure(ctx.config, ctx.state_dir, conflicted)
    try:
        result = await asyncio.to_thread(functools.partial(fn, **kwargs))
    except FileNotFoundError:
        raise ToolError(
            "unavailable",
            "the Thunderbird profile or one of its databases was not found",
            reason="tb_profile_missing",
        ) from None
    except snapshot.SnapshotError:
        raise ToolError(
            "unavailable",
            "no consistent snapshot of the Thunderbird database could be made",
            reason="tb_snapshot_failed",
        ) from None
    except sqlite3.DatabaseError as exc:
        raise ToolError(
            "internal", f"snapshot read failed ({type(exc).__name__})", reason="tb_db_error"
        ) from None
    return TbOut(result=guard.fit(with_mids(result, conflicted)))


#: Added to every object whose mid tern's index marks conflicted (``"conflicted": true``).
CONFLICT_WARNING = (
    "Several different messages share this Message-ID (possible spoof); check them in "
    "Thunderbird before acting on this one."
)


def with_mids(value: Any, conflicted: frozenset[str] = frozenset()) -> Any:
    """``value`` with ``mid`` (tern's short id) added to every object that has a
    ``header_message_id`` from which a mid can be computed (:func:`header_mid`); objects without
    one are left as they are: never an invented id. An object whose full mid is in
    ``conflicted`` (tern's index: differing copies share the Message-ID, #5) also gets
    ``"conflicted": true`` and :data:`CONFLICT_WARNING`."""
    if isinstance(value, list):
        return [with_mids(v, conflicted) for v in value]
    if isinstance(value, dict):
        out = {k: with_mids(v, conflicted) for k, v in value.items()}
        mid = header_mid(out.get("header_message_id"))
        if mid is not None and "mid" not in out:
            out["mid"] = short_id(mid)
            if mid in conflicted:
                out["conflicted"] = True
                out["warning"] = CONFLICT_WARNING
        return out
    return value


# ---- input checks (what upstream would fail to parse) -------------------------------------------


def _date(value: str | None) -> str | None:
    """``YYYY-MM-DD`` or an ISO timestamp (``db._parse_date``'s grammar)."""
    if value is None:
        return value
    s = value.strip()
    try:
        datetime.fromisoformat(s if len(s) != 10 else s + "T00:00:00")
    except ValueError:
        raise ValueError("expected YYYY-MM-DD or an ISO timestamp") from None
    return value


def _hhmm(value: str) -> str:
    """``HH:MM`` (``calendar_db._parse_hhmm``'s grammar)."""
    h, _, m = value.strip().partition(":")
    try:
        time(int(h), int(m or 0))
    except ValueError:
        raise ValueError("expected HH:MM") from None
    return value


DateStr = Annotated[str | None, AfterValidator(_date)]
HhMm = Annotated[str, AfterValidator(_hhmm)]


class NoIn(BoundaryModel):
    """No parameters."""


# ---- mail ---------------------------------------------------------------------------------------


@tool(name="tb_list_accounts", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=False)
async def tb_list_accounts(ctx: ToolContext, inp: NoIn) -> TbOut:
    """The mail accounts you may access: emails, and per account its server
    type, hostname, server username and identity emails. Also your timezone."""
    return await _call(ctx, api.visible_accounts)


@tool(name="tb_stats", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=False)
async def tb_stats(ctx: ToolContext, inp: NoIn) -> TbOut:
    """Index stats: number of messages in scope and the date range they span."""
    return await _call(ctx, db.stats)


@tool(name="tb_folders", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=False)
async def tb_folders(ctx: ToolContext, inp: NoIn) -> TbOut:
    """Folders in scope (INBOX, Sent, local folders, ...) with message counts."""
    return await _call(ctx, db.folders)


class TbSearchIn(BoundaryModel):
    query: str | None = None
    since: DateStr = None
    until: DateStr = None
    from_addr: str | None = None
    to_addr: str | None = None
    account: str | None = None
    folder: str | None = None
    unread: bool | None = None
    starred: bool | None = None
    has_attachment: bool | None = None
    limit: int = 20
    offset: int = 0


@tool(name="tb_search", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_search(ctx: ToolContext, inp: TbSearchIn) -> TbOut:
    """Search mail.

    Args:
        query: literal text to find in subject, body or attachment names.
        since: inclusive start, YYYY-MM-DD (a day in your timezone) or ISO timestamp.
        until: inclusive end; a bare YYYY-MM-DD includes that whole day.
        from_addr: sender substring. to_addr: recipient substring (To/Cc/Bcc).
        account: an account's email or server username (see tb_list_accounts).
        folder: folder name substring (e.g. "Inbox", "Sent").
        unread / starred / has_attachment: true or false to filter, omit to ignore.
        limit: 1-100 (default 20). offset: for paging (use next_offset).

    Returns {messages, next_offset}. Each message has a 600-char body preview,
    status flags (read, starred, replied, from_me, to_me, mailing_list) and
    attachment names. Newest first, one per Message-ID.
    """
    return await _call(ctx, db.search, **inp.model_dump())


class TbRecentByAddressIn(BoundaryModel):
    email_or_domain: str
    limit: int = 20
    offset: int = 0


@tool(name="tb_recent_by_address", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_recent_by_address(ctx: ToolContext, inp: TbRecentByAddressIn) -> TbOut:
    """Mail to or from a contact or domain, newest first, as {messages, next_offset}.

    Args:
        email_or_domain: e.g. "alice@example.com", "example.com" or "@example.com".
        limit: 1-100 (default 20). offset: for paging.
    """
    return await _call(ctx, db.recent_by_address, **inp.model_dump())


class TbGetMessageIn(BoundaryModel):
    message_id: int
    body_chars: int = 8_000
    strip_quotes: bool = False


@tool(name="tb_get_message", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_get_message(ctx: ToolContext, inp: TbGetMessageIn) -> TbOut:
    """One message. body_chars caps the body (default 8000, max 200000); if
    `truncated` is true, ask again with more. strip_quotes removes quoted
    earlier messages and the signature."""
    return await _call(ctx, db.get_message, **inp.model_dump())


class TbThreadIn(BoundaryModel):
    conversation_id: int
    body_chars: int = 1500
    strip_quotes: bool = True


@tool(name="tb_thread", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_thread(ctx: ToolContext, inp: TbThreadIn) -> TbOut:
    """A whole conversation, oldest first. Quoted replies and signatures are
    stripped by default so each message shows only what was newly written;
    body_chars caps each message (default 1500)."""
    return await _call(ctx, db.thread, **inp.model_dump())


class TbAttachmentsIn(BoundaryModel):
    query: str | None = None
    since: DateStr = None
    until: DateStr = None
    from_addr: str | None = None
    account: str | None = None
    limit: int = 20
    offset: int = 0


@tool(name="tb_attachments", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_attachments(ctx: ToolContext, inp: TbAttachmentsIn) -> TbOut:
    """Find attachments by file name (e.g. "invoice", ".pdf", "contract").

    Returns {attachments: [{name, type, message_id, date, from, subject, ...}],
    next_offset}. limit counts messages (1-100). Only names are available, not
    file contents.
    """
    return await _call(ctx, db.attachments, **inp.model_dump())


class TbAwaitingReplyIn(BoundaryModel):
    days: int = 3
    within_days: int = 60
    limit: int = 20


@tool(name="tb_awaiting_reply", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_awaiting_reply(ctx: ToolContext, inp: TbAwaitingReplyIn) -> TbOut:
    """Threads where you sent the last message at least `days` ago and nobody has
    replied — people you're waiting on. Looks back `within_days`. Longest wait first."""
    return await _call(ctx, db.awaiting_reply, **inp.model_dump())


class TbUnansweredIn(BoundaryModel):
    days: int = 1
    within_days: int = 30
    limit: int = 20
    include_automated: bool = False


@tool(name="tb_unanswered", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_unanswered(ctx: ToolContext, inp: TbUnansweredIn) -> TbOut:
    """Mail sent to you by people, at least `days` old, that you haven't replied
    to. Skips mailing lists and no-reply/notification senders unless
    include_automated. Looks back `within_days`."""
    return await _call(ctx, db.unanswered, **inp.model_dump())


class TbContactIn(BoundaryModel):
    email: str
    threads: int = 5


@tool(name="tb_contact", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_contact(ctx: ToolContext, inp: TbContactIn) -> TbOut:
    """Everything about one correspondent: how much you've exchanged, first/last
    contact, recent threads, upcoming meetings together, and their address-book
    card (organization, title, phones)."""
    return await _call(ctx, briefing.contact, **inp.model_dump())


# ---- calendar -----------------------------------------------------------------------------------


@tool(name="tb_calendars", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_calendars(ctx: ToolContext, inp: NoIn) -> TbOut:
    """Calendars in scope: id, name, type (storage/caldav/ics/gdata), whether
    disabled or read-only, and how many events each holds locally. Remote
    calendars are only visible if Thunderbird caches them offline."""
    return await _call(ctx, calendar_db.calendars)


class TbEventsIn(BoundaryModel):
    since: DateStr = None
    until: DateStr = None
    query: str | None = None
    calendar: str | None = None
    limit: int = 50


@tool(name="tb_events", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_events(ctx: ToolContext, inp: TbEventsIn) -> TbOut:
    """Calendar events in a time window, with recurring events expanded.

    Args:
        since: window start, YYYY-MM-DD (your timezone) or ISO timestamp. Default today.
        until: window end, inclusive for a bare date. Default 30 days after since.
            Pass an earlier window to look back.
        query: literal text to find in title, description or location.
        calendar: calendar name or id (see tb_calendars). Disabled calendars
            are skipped unless named here.
        limit: max occurrences, 1-200 (default 50).

    Returns {since, until, events, more}. Each event has start/end in its own
    timezone (all-day events give dates with an inclusive last day), title,
    location, calendar, attendee count and a description preview.
    """
    return await _call(ctx, calendar_db.events, **inp.model_dump())


class TbGetEventIn(BoundaryModel):
    event_id: str
    calendar_id: str | None = None
    description_chars: int = 8000


@tool(name="tb_get_event", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_get_event(ctx: ToolContext, inp: TbGetEventIn) -> TbOut:
    """Full details of one event or recurring series: description, location,
    URL, organizer, attendees and their responses, recurrence rules and
    modified occurrences."""
    return await _call(ctx, calendar_db.get_event, **inp.model_dump())


class TbFreeSlotsIn(BoundaryModel):
    since: DateStr = None
    until: DateStr = None
    min_minutes: int = 30
    work_start: HhMm = "09:00"
    work_end: HhMm = "17:00"
    weekdays_only: bool = True
    calendar: str | None = None


@tool(name="tb_free_slots", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_free_slots(ctx: ToolContext, inp: TbFreeSlotsIn) -> TbOut:
    """Free time in your working hours (your timezone), plus double-bookings.

    Args:
        since/until: window (default: the next 7 days); bare dates are whole days.
        min_minutes: shortest gap worth reporting (default 30).
        work_start/work_end: "HH:MM" (default 09:00-17:00).
        weekdays_only: skip Saturday and Sunday (default true).
        calendar: only count events from this calendar.

    Busy time = timed events not marked free, not cancelled, not declined by you.
    """
    return await _call(ctx, calendar_db.free_slots, **inp.model_dump())


# ---- composite ----------------------------------------------------------------------------------


class TbMeetingPrepIn(BoundaryModel):
    event_id: str
    calendar_id: str | None = None
    per_person: int = 5
    days_back: int = 90


@tool(name="tb_meeting_prep", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_meeting_prep(ctx: ToolContext, inp: TbMeetingPrepIn) -> TbOut:
    """Everything to prepare for a meeting in one call: event details, the next
    occurrence, each attendee with their organization, last contact and recent
    mail with you, plus mail mentioning the meeting's title."""
    return await _call(ctx, briefing.meeting_prep, **inp.model_dump())


class TbDailyBriefIn(BoundaryModel):
    date: DateStr = None


@tool(name="tb_daily_brief", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=True)
async def tb_daily_brief(ctx: ToolContext, inp: TbDailyBriefIn) -> TbOut:
    """One day at a glance (default today): agenda, free slots, conflicts, mail
    from people since yesterday, mail from today's meeting attendees, and (for
    today) who you owe a reply and who owes you one."""
    return await _call(ctx, briefing.daily_brief, **inp.model_dump())


@tool(name="tb_guardrails", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=False)
async def tb_guardrails(ctx: ToolContext, inp: NoIn) -> TbOut:
    """The guardrails currently in force: account/folder/calendar scope, history
    limit, private-event mode, redaction, output budget, audit log, timezone."""
    return await _call(ctx, api.guardrails)


# ---- skills -------------------------------------------------------------------------------------


class SkillIn(BoundaryModel):
    name: SkillName


class SkillOut(BoundaryModel):
    name: str
    text: str = Field(description="the skill's SKILL.md")


def skill_text(name: str) -> str:
    """The text of the vendored ``skills/<name>/SKILL.md``."""
    return (
        resources.files("tern.tbmcp")
        .joinpath("skills", name, "SKILL.md")
        .read_text(encoding="utf-8")
    )


@tool(name="skill", tier=Tier.R0, effects=[Effect.READ_INDEX], untrusted_output=False)
async def skill(ctx: ToolContext, inp: SkillIn) -> SkillOut:
    """A ready-made workflow (steps, output shape, safety rules) for combining the tb_* tools.
    Read the matching one before starting such a task:
    thunderbird-daily-brief ("What's my day look like?", morning catch-up),
    thunderbird-meeting-prep ("Prep me for my 2pm": who is in a meeting, what is open with them),
    thunderbird-follow-ups ("Who haven't I replied to?", "Who owes me a reply?", nudges),
    thunderbird-scheduling ("When am I free Thursday?", conflicts, proposing times),
    thunderbird-inbox-triage ("Triage my unread mail", spotting phishing / injection attempts),
    thunderbird-mail-research ("What did X say about Y?", attachments, history with a contact)."""
    return SkillOut(name=inp.name, text=skill_text(inp.name))


# ---- the set ------------------------------------------------------------------------------------

#: The thunderbird-mcp tools, in upstream's order.
TB_TOOLS: tuple[ToolSpec, ...] = (
    tb_list_accounts,
    tb_stats,
    tb_folders,
    tb_search,
    tb_recent_by_address,
    tb_get_message,
    tb_thread,
    tb_attachments,
    tb_awaiting_reply,
    tb_unanswered,
    tb_contact,
    tb_calendars,
    tb_events,
    tb_get_event,
    tb_free_slots,
    tb_meeting_prep,
    tb_daily_brief,
    tb_guardrails,
)

#: Their names (the chat agent's tool set is these plus ``skill``).
TB_TOOL_NAMES: tuple[str, ...] = tuple(s.name for s in TB_TOOLS)

#: Every tool this module registers: the ``tb_*`` tools and ``skill``.
TBMCP_TOOLS: tuple[ToolSpec, ...] = (*TB_TOOLS, skill)

#: The names the chat agent is offered once it switches to these tools.
CHAT_TOOL_NAMES: tuple[str, ...] = tuple(s.name for s in TBMCP_TOOLS)
