"""The thunderbird-mcp tools as tern built-ins (``tern.tools.builtin.tb``).

Every call goes through ``Registry.call`` on a synthetic Thunderbird profile built in ``tmp_path``
(a GLODA-shaped ``global-messages-db.sqlite`` from ``tests/tbmcp/synthetic.py`` and a synthetic
``prefs.js``), so the real snapshot copy runs end to end into ``$TERN_HOME/state/tb-snapshot``.
The autouse fixture points discovery at no roots and restores the vendored modules' settings, so
no test can reach a real profile. Also ported here: thunderbird-mcp 4921d1e's ``test_server.py``
(its FastMCP calls become ``Registry.call``).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError
from tbmcp.synthetic import _FEB, attrs, build_snapshot

from tern import paths
from tern.config import TernConfig
from tern.index.mid import mid_of
from tern.index.store import LocationState, MessageRecord, Store
from tern.perms.audit import AuditLog, audit_path
from tern.search.service import SearchService
from tern.tbmcp import accounts, calendar_db, db, guard, snapshot
from tern.tbmcp import config as tbconfig
from tern.tools.builtin import BUILTINS, CHAT_TOOL_NAMES, TB_TOOL_NAMES
from tern.tools.builtin.tb import (
    CONFLICT_WARNING,
    TB_INSTRUCTIONS,
    guard_settings,
    skill_text,
    with_mids,
)
from tern.tools.contract import Effect, Tier, ToolError
from tern.tools.ids import short_id
from tern.tools.registry import Registry, ToolResult, ToolRuntime, builtin_tools

CALLER = "agent:t1"

PREFS = """\
user_pref("mail.account.account1.server", "server1");
user_pref("mail.account.account1.identities", "id1");
user_pref("mail.account.account2.server", "server2");
user_pref("mail.account.account2.identities", "id2");
user_pref("mail.identity.id1.useremail", "alice@example.com");
user_pref("mail.identity.id2.useremail", "bob@work.example.org");
user_pref("mail.server.server1.hostname", "imap.example.com");
user_pref("mail.server.server1.userName", "alice@example.com");
user_pref("mail.server.server1.type", "imap");
user_pref("mail.server.server2.hostname", "mail.work.example.org");
user_pref("mail.server.server2.userName", "bob");
user_pref("mail.server.server2.type", "imap");
"""

#: Upstream's parameters and defaults per tool (thunderbird-mcp 4921d1e ``server.py``); a
#: parameter without a default is required.
REQUIRED = object()
PARAMS: dict[str, dict[str, Any]] = {
    "tb_list_accounts": {},
    "tb_stats": {},
    "tb_folders": {},
    "tb_search": {
        "query": None,
        "since": None,
        "until": None,
        "from_addr": None,
        "to_addr": None,
        "account": None,
        "folder": None,
        "unread": None,
        "starred": None,
        "has_attachment": None,
        "limit": 20,
        "offset": 0,
    },
    "tb_recent_by_address": {"email_or_domain": REQUIRED, "limit": 20, "offset": 0},
    "tb_get_message": {"message_id": REQUIRED, "body_chars": 8000, "strip_quotes": False},
    "tb_thread": {"conversation_id": REQUIRED, "body_chars": 1500, "strip_quotes": True},
    "tb_attachments": {
        "query": None,
        "since": None,
        "until": None,
        "from_addr": None,
        "account": None,
        "limit": 20,
        "offset": 0,
    },
    "tb_awaiting_reply": {"days": 3, "within_days": 60, "limit": 20},
    "tb_unanswered": {"days": 1, "within_days": 30, "limit": 20, "include_automated": False},
    "tb_contact": {"email": REQUIRED, "threads": 5},
    "tb_calendars": {},
    "tb_events": {"since": None, "until": None, "query": None, "calendar": None, "limit": 50},
    "tb_get_event": {"event_id": REQUIRED, "calendar_id": None, "description_chars": 8000},
    "tb_free_slots": {
        "since": None,
        "until": None,
        "min_minutes": 30,
        "work_start": "09:00",
        "work_end": "17:00",
        "weekdays_only": True,
        "calendar": None,
    },
    "tb_meeting_prep": {
        "event_id": REQUIRED,
        "calendar_id": None,
        "per_person": 5,
        "days_back": 90,
    },
    "tb_daily_brief": {"date": None},
    "tb_guardrails": {},
}

#: The tools whose output carries no mail, calendar or contact text.
TRUSTED = {"tb_list_accounts", "tb_stats", "tb_folders", "tb_guardrails"}


# ---- fixtures -----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_tbmcp(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """The vendored modules' process-wide settings are restored after each test, and profile
    discovery has no roots: only an explicit synthetic ``tb_profile_dir`` is ever read."""
    for name in ("_settings",):
        monkeypatch.setattr(guard, name, getattr(guard, name))
    monkeypatch.setattr(tbconfig, "_override", tbconfig._override)
    monkeypatch.setattr(tbconfig, "_candidate_roots", lambda: [])
    monkeypatch.setattr(snapshot, "CACHE_DIR", snapshot.CACHE_DIR)
    monkeypatch.setattr(snapshot, "SNAPSHOT", snapshot.SNAPSHOT)
    monkeypatch.setattr(snapshot, "_copied_from", {})
    monkeypatch.setattr(accounts, "_cache", accounts._cache)
    monkeypatch.setattr(db, "_split", db._split)
    tbconfig.profile_dir.cache_clear()
    request.addfinalizer(tbconfig.profile_dir.cache_clear)


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    """A synthetic Thunderbird profile: the GLODA index and ``prefs.js``, no calendars."""
    prof = tmp_path / "profile"
    prof.mkdir()
    build_snapshot(prof / "global-messages-db.sqlite")
    (prof / "prefs.js").write_text(PREFS, encoding="utf-8")
    return prof


@dataclass
class Env:
    registry: Registry
    audit: AuditLog


@pytest.fixture
def make_env(tmp_path: Path) -> Iterator[Any]:
    opened: list[Store | AuditLog] = []

    def make(
        config: TernConfig, state_dir: Path | Literal["tern_home"] | None = "tern_home"
    ) -> Env:
        """A registry over ``config``; its runtime's state dir is ``state_dir`` ("tern_home":
        the test's ``$TERN_HOME/state``)."""
        index = tmp_path / "index.db"
        if not index.exists():
            Store.open(index).close()
        store = Store.open(index, readonly=True)
        opened.append(store)
        audit = AuditLog.open(audit_path(paths.resolve()))
        opened.append(audit)
        runtime = ToolRuntime(
            audit=audit,
            index=store,
            search=SearchService(store, None, mode="fts"),
            bridge=None,
            config=config,
            log=logging.getLogger("tern.tools.test_builtin_tb"),
            clock=time.time,
            state_dir=paths.resolve().state if state_dir == "tern_home" else state_dir,
        )
        return Env(Registry(builtin_tools(), runtime=runtime), audit)

    yield make
    for item in reversed(opened):
        item.close()


@pytest.fixture
def env(make_env: Any, profile: Path) -> Env:
    return make_env(TernConfig(tb_profile_dir=profile, tb_timezone="UTC"))


async def call(env: Env, tool: str, args: dict[str, Any]) -> Any:
    """The tool's ``result`` as the model sees it."""
    res = await env.registry.call(CALLER, tool, args)
    assert isinstance(res, ToolResult)
    return res.output.model_dump(mode="json")["result"]


async def fails(env: Env, tool: str, args: dict[str, Any]) -> ToolError:
    with pytest.raises(ToolError) as info:
        await env.registry.call(CALLER, tool, args)
    return info.value


# ---- registration -------------------------------------------------------------------------------


def test_every_upstream_tool_is_a_builtin_r0_read_tool_with_its_parameters() -> None:
    specs = {s.name: s for s in BUILTINS}
    assert set(TB_TOOL_NAMES) == set(PARAMS)
    for name, params in PARAMS.items():
        spec = specs[name]
        assert (spec.tier, spec.effective_tier) == (Tier.R0, Tier.R0), name
        assert spec.effects == frozenset({Effect.READ_INDEX}), name
        schema = spec.input_model.model_json_schema()
        props = schema.get("properties", {})
        assert list(props) == list(params), name
        required = {p for p, d in params.items() if d is REQUIRED}
        assert set(schema.get("required", [])) == required, name
        for p, default in params.items():
            if default is not REQUIRED:
                assert props[p].get("default") == default, (name, p)


def test_descriptions_are_upstreams() -> None:
    specs = {s.name: s for s in BUILTINS}
    assert specs["tb_search"].description.startswith("Search mail.\n\nArgs:")
    assert "Returns {messages, next_offset}." in specs["tb_search"].description
    assert specs["tb_stats"].description == (
        "Index stats: number of messages in scope and the date range they span."
    )
    assert specs["tb_get_message"].description.startswith("One message. body_chars caps")


def test_untrusted_outputs_are_flagged() -> None:
    specs = {s.name: s for s in BUILTINS}
    for name in TB_TOOL_NAMES:
        assert specs[name].untrusted_output is (name not in TRUSTED), name
    assert specs["skill"].untrusted_output is False


def test_chat_tool_names_are_registered() -> None:
    names = set(Registry(builtin_tools()).names())
    assert CHAT_TOOL_NAMES == (*TB_TOOL_NAMES, "skill")
    assert set(CHAT_TOOL_NAMES) <= names
    # tern's own read tools stay (smoke contracts, toolcalls-v1)
    assert {"search_messages", "get_message", "get_thread", "index_status"} <= names


def test_instructions_flag_untrusted_content() -> None:
    assert "UNTRUSTED" in TB_INSTRUCTIONS
    assert "calendar" in TB_INSTRUCTIONS


# ---- calls on the synthetic profile -------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_unread_and_get_message(env: Env) -> None:
    out = await call(env, "tb_search", {"unread": True})
    assert [m["message_id"] for m in out["messages"]] == [4]
    assert out["messages"][0]["header_message_id"] == "<m4@work.example.org>"
    assert out["messages"][0]["account"] == "bob@work.example.org"  # from prefs.js
    assert out["next_offset"] is None
    msg = await call(env, "tb_get_message", {"message_id": 1})
    assert msg["header_message_id"] == "<m1@example.com>"
    assert (msg["subject"], msg["body_preview"]) == ("Shipment quote", "Quote for the shipment")


@pytest.mark.asyncio
async def test_results_are_flagged_untrusted_and_audited_by_tern(env: Env) -> None:
    res = await env.registry.call(CALLER, "tb_search", {"query": "shipment"})
    assert res.untrusted_output is True
    [row] = env.audit.list(limit=None)
    assert (row.tool, row.tier, row.decision, row.status) == ("tb_search", "R0", "auto", "ok")
    # upstream's own audit log is not written
    assert not list(Path(paths.resolve().state).rglob("audit.jsonl"))


@pytest.mark.asyncio
async def test_snapshot_is_a_private_copy_under_the_state_dir(env: Env, profile: Path) -> None:
    source = profile / "global-messages-db.sqlite"
    before = (source.stat().st_mtime_ns, source.read_bytes())
    await call(env, "tb_stats", {})
    snap_dir = paths.resolve().state / "tb-snapshot"
    copy = snap_dir / "snapshot.sqlite"
    assert copy.is_file()
    assert stat.S_IMODE(snap_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert (source.stat().st_mtime_ns, source.read_bytes()) == before
    assert os.environ["TERN_HOME"] in str(copy)


@pytest.mark.asyncio
async def test_snapshot_goes_to_the_runtimes_state_dir_not_the_environments(
    make_env: Any, profile: Path, tmp_path: Path
) -> None:
    """The state dir comes from the runtime (``ctx.state_dir``), never from ``paths.resolve()``:
    an in-process caller whose environment points elsewhere (e.g. no ``TERN_HOME``: the user's
    real state dir) still writes only under its own runtime's dir."""
    own = tmp_path / "runtime-state"
    env = make_env(TernConfig(tb_profile_dir=profile, tb_timezone="UTC"), state_dir=own)
    await call(env, "tb_stats", {})
    assert (own / "tb-snapshot" / "snapshot.sqlite").is_file()
    assert not (paths.resolve().state / "tb-snapshot").exists()


@pytest.mark.asyncio
async def test_no_state_dir_is_unavailable(make_env: Any, profile: Path) -> None:
    env = make_env(TernConfig(tb_profile_dir=profile), state_dir=None)
    err = await fails(env, "tb_stats", {})
    assert (err.code, err.reason) == ("unavailable", "tb_no_state_dir")
    assert not (paths.resolve().state / "tb-snapshot").exists()


@pytest.mark.asyncio
async def test_unknown_message_is_null(env: Env) -> None:
    assert await call(env, "tb_get_message", {"message_id": 999}) is None


@pytest.mark.asyncio
async def test_no_profile_is_unavailable(make_env: Any, tmp_path: Path) -> None:
    env = make_env(TernConfig(tb_profile_dir=tmp_path / "missing"))
    err = await fails(env, "tb_search", {})
    assert (err.code, err.reason) == ("unavailable", "tb_profile_missing")


@pytest.mark.asyncio
async def test_no_profile_found_by_discovery_is_unavailable(make_env: Any) -> None:
    env = make_env(TernConfig())  # tb_profile_dir None: discovery, over no roots (fixture)
    err = await fails(env, "tb_stats", {})
    assert (err.code, err.reason) == ("unavailable", "tb_profile_missing")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("tb_search", {"since": "last tuesday"}),
        ("tb_events", {"until": "2025-13-01"}),
        ("tb_daily_brief", {"date": "tomorrow"}),
        ("tb_free_slots", {"work_start": "nine"}),
        ("tb_get_message", {"message_id": "one"}),
    ],
)
async def test_unparseable_inputs_are_invalid_params(
    env: Env, tool: str, args: dict[str, Any]
) -> None:
    err = await fails(env, tool, args)
    assert err.code == "invalid_params"


# ---- ported from thunderbird-mcp's test_server.py -----------------------------------------------


@pytest.mark.asyncio
async def test_every_message_carries_terns_short_mid_for_citations(env: Env) -> None:
    out = await call(env, "tb_search", {"unread": True})
    hit = out["messages"][0]
    full = mid_of(hit["header_message_id"])
    assert full is not None and hit["mid"] == short_id(full)
    msg = await call(env, "tb_get_message", {"message_id": 1})
    assert msg["mid"] == short_id(mid_of("<m1@example.com>") or "")


def test_with_mids_never_invents_an_id() -> None:
    value = {"a": [{"header_message_id": None}, {"x": 1}], "header_message_id": "<>"}
    assert with_mids(value) == value


def test_with_mids_leaves_tb_md5_ids_without_a_mid() -> None:
    """TB's headerMessageId of mail without a Message-ID (``md5:<hex>``) has no mid: the item
    gets none, and the other items of the result still get theirs (``mid_of`` raises on it)."""
    value = [{"header_message_id": "md5:0123abcd"}, {"header_message_id": "m1@example.com"}]
    out = with_mids(value)
    assert out[0] == value[0]
    assert out[1]["mid"] == short_id(mid_of("m1@example.com") or "")


def test_with_mids_marks_conflicted_mids() -> None:
    full = mid_of("<m1@example.com>") or ""
    value = {"messages": [{"header_message_id": "<m1@example.com>"}, {"header_message_id": "m2@x"}]}
    spoofed, other = with_mids(value, frozenset({full}))["messages"]
    assert spoofed == {
        "header_message_id": "<m1@example.com>",
        "mid": short_id(full),
        "conflicted": True,
        "warning": CONFLICT_WARNING,
    }
    assert "conflicted" not in other and "warning" not in other


#: A look-alike of message 1 (same Message-ID, different sender and body) in bob's Work folder.
SPOOF_OF_M1 = (
    20,
    2,
    100,
    _FEB + 7_200_000_000,
    "<m1@example.com>",
    0,
    attrs(to_me=True),
    "Our bank details have changed",
    "Shipment quote",
    "",
    "Bob Vendor <bob@vendor-payments.test>",
    "alice@example.com",
)


def _spoof_profile(tmp_path: Path) -> Path:
    prof = tmp_path / "profile"
    prof.mkdir()
    build_snapshot(prof / "global-messages-db.sqlite", extra=[SPOOF_OF_M1])
    (prof / "prefs.js").write_text(PREFS, encoding="utf-8")
    return prof


def _conflict_in_index(path: Path, header: str) -> str:
    """Index ``header``'s message as two differing copies (tern marks it conflicted); its mid."""
    mid = mid_of(header)
    assert mid is not None
    with Store.open(path) as store:
        for folder, h in (("acct1:/INBOX", "1" * 64), ("acct2:/Work", "2" * 64)):
            with store.transaction():
                store.upsert_message(
                    MessageRecord(mid=mid, content_hash=h, parse_status="ok", body_text="x")
                )
                store.add_location(
                    mid, folder, h, LocationState(), 1_753_783_200, source="backfill"
                )
        assert store.conflicted_mids() == {mid}
    return mid


@pytest.mark.asyncio
async def test_conflicted_copies_are_all_listed_and_marked(make_env: Any, tmp_path: Path) -> None:
    """GLODA collapses rows by Message-ID; for a mid tern's index marks conflicted, every row
    is listed (the look-alike cannot hide the genuine mail) and each carries the warning."""
    prof = _spoof_profile(tmp_path)
    mid = _conflict_in_index(tmp_path / "index.db", "<m1@example.com>")
    env = make_env(TernConfig(tb_profile_dir=prof, tb_timezone="UTC"))

    out = await call(env, "tb_search", {"query": "shipment"})
    by_id = {m["message_id"]: m for m in out["messages"]}
    assert set(by_id) == {1, 2, 20}
    for n in (1, 20):
        assert (by_id[n]["mid"], by_id[n]["conflicted"]) == (short_id(mid), True)
        assert by_id[n]["warning"] == CONFLICT_WARNING
    assert "conflicted" not in by_id[2] and "warning" not in by_id[2]
    thread = await call(env, "tb_thread", {"conversation_id": 100})
    assert sorted(m["message_id"] for m in thread) == [1, 2, 20]
    one = await call(env, "tb_get_message", {"message_id": 20})
    assert (one["conflicted"], one["warning"]) == (True, CONFLICT_WARNING)


@pytest.mark.asyncio
async def test_without_a_conflict_the_copies_still_collapse(make_env: Any, tmp_path: Path) -> None:
    env = make_env(TernConfig(tb_profile_dir=_spoof_profile(tmp_path), tb_timezone="UTC"))
    out = await call(env, "tb_search", {"query": "shipment"})
    assert sorted(m["message_id"] for m in out["messages"]) == [1, 2]  # upstream's dedup
    assert not any("conflicted" in m for m in out["messages"])


@pytest.mark.asyncio
async def test_search_clamps_limit(env: Env) -> None:
    out = await call(env, "tb_search", {"limit": -1})
    assert len(out["messages"]) == 1
    assert out["next_offset"] == 1


@pytest.mark.asyncio
async def test_calendar_tool_passes_its_arguments(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(**kw: Any) -> dict[str, Any]:
        return {"since": "s", "until": "u", "events": [], "more": False, **kw}

    monkeypatch.setattr(calendar_db, "events", fake)
    out = await call(env, "tb_events", {"query": "x", "limit": 3})
    assert (out["query"], out["limit"], out["calendar"]) == ("x", 3, None)


@pytest.mark.asyncio
async def test_guardrails_tool_reports_the_config(make_env: Any, profile: Path) -> None:
    env = make_env(
        TernConfig(
            tb_profile_dir=profile,
            tb_timezone="UTC",
            tb_accounts=["alice@example.com"],
            tb_private_events="busy",
            tb_max_age_days=365,
        )
    )
    g = await call(env, "tb_guardrails", {})
    assert g["accounts_allowed"] == ["alice@example.com"]
    assert "trash" in g["folders_excluded"]
    assert (g["private_events"], g["max_age_days"], g["redaction"]) == ("busy", 365, True)
    assert g["timezone"] == "UTC"
    assert g["audit_log"] == "tern audit log (every tool call)"


@pytest.mark.asyncio
async def test_output_budget_applied(make_env: Any, profile: Path) -> None:
    env = make_env(TernConfig(tb_profile_dir=profile, tb_timezone="UTC", tb_max_output_chars=1500))
    out = await call(env, "tb_search", {"limit": 100})
    assert out["output_budget_exceeded"] is True
    assert len(json.dumps(out)) <= 1500


@pytest.mark.asyncio
async def test_list_accounts_respects_scope(make_env: Any, profile: Path) -> None:
    env = make_env(TernConfig(tb_profile_dir=profile, tb_exclude_accounts=["bob@work.example.org"]))
    out = await call(env, "tb_list_accounts", {})
    assert out["emails"] == ["alice@example.com"]
    assert [a["account"] for a in out["accounts"]] == ["account1"]


@pytest.mark.asyncio
async def test_account_scope_filters_search(make_env: Any, profile: Path) -> None:
    env = make_env(TernConfig(tb_profile_dir=profile, tb_accounts=["alice@example.com"]))
    out = await call(env, "tb_search", {"unread": True})
    assert out["messages"] == []  # message 4 is in bob's account


# ---- skill --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_returns_the_skill_text(env: Env) -> None:
    res = await env.registry.call(CALLER, "skill", {"name": "thunderbird-follow-ups"})
    assert res.untrusted_output is False
    out = res.output.model_dump(mode="json")
    assert out["name"] == "thunderbird-follow-ups"
    assert out["text"] == skill_text("thunderbird-follow-ups")
    assert "## Instructions" in out["text"] and "tb_awaiting_reply" in out["text"]


@pytest.mark.asyncio
async def test_unknown_skill_is_invalid_params(env: Env) -> None:
    err = await fails(env, "skill", {"name": "no-such-skill"})
    assert err.code == "invalid_params"


# ---- config -> guard ----------------------------------------------------------------------------


def test_guard_settings_map_the_tb_config_keys() -> None:
    default = guard_settings(TernConfig())
    assert default["EXCLUDE_FOLDERS"] is None  # upstream's default list applies
    assert (default["ACCOUNTS"], default["MAX_AGE_DAYS"], default["TIMEZONE"]) == ("", None, None)
    assert (default["REDACT"], default["PRIVATE_EVENTS"]) == ("on", "show")
    cfg = TernConfig(
        tb_folders=["Inbox", "Sent"],
        tb_exclude_folders=[],
        tb_calendars=["Home"],
        tb_max_age_days=30,
        tb_redact=False,
        tb_max_output_chars=5000,
        tb_timezone="Europe/Athens",
    )
    got = guard_settings(cfg)
    assert got["FOLDERS"] == "Inbox,Sent"
    assert got["EXCLUDE_FOLDERS"] == ""  # none excluded
    assert (got["CALENDARS"], got["MAX_AGE_DAYS"], got["REDACT"]) == ("Home", "30", "off")
    assert (got["MAX_OUTPUT_CHARS"], got["TIMEZONE"]) == ("5000", "Europe/Athens")
    assert set(got) == guard.SETTING_NAMES


@pytest.mark.parametrize(
    "bad",
    [
        {"tb_profile_dir": "relative/profile"},
        {"tb_accounts": ["a@example.com,b@example.com"]},
        {"tb_exclude_folders": [" "]},
        {"tb_timezone": "Not/AZone"},
        {"tb_private_events": "maybe"},
        {"tb_max_age_days": 0},
    ],
)
def test_bad_tb_config_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TernConfig(**bad)
