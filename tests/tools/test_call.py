"""``Registry.call`` (plan 06a step 4): caller/tool/args validation, the write-ahead audit row,
R0/R1 runs, ``tier_not_enabled`` for W tiers, ``ToolError`` codes and ``error`` events."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sqlite3
import time
import traceback
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, Field, field_serializer, field_validator

from tern import paths
from tern.bridge.protocol import BridgeError, Empty, Mid, TbIdParams
from tern.config import TernConfig
from tern.index.store import Store
from tern.perms.audit import INVALID_TOOL, AuditLog, audit_path
from tern.perms.engine import Engine
from tern.search.service import SearchService
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, ToolContextError, ToolError, tool
from tern.tools.contract import TOOL_RAISABLE_CODES, ToolErrorCode, ToolSpec
from tern.tools.registry import (
    UNKNOWN_LOG_TOOL,
    RegisteredTool,
    Registry,
    ToolResult,
    ToolRuntime,
)

M1 = "a" * 64
M2 = "x-" + "b" * 64
SECRET = "SENTINEL-call-7f3e"

# ---- test tools (module level: In/Out come from resolved annotations) ---------------------------

RAN: list[str] = []


class EchoIn(BoundaryModel):
    q: str = Field(max_length=40)
    limit: int = 10


class EchoOut(BoundaryModel):
    q: str


class Nested(BoundaryModel):
    word: str


class NestedIn(BoundaryModel):
    inner: Nested
    tags: list[str] = []


class FloatIn(BoundaryModel):
    x: float


class FloatOut(BoundaryModel):
    x: float | None


class MidIn(BoundaryModel):
    mid: Mid


class MidsIn(BoundaryModel):
    mids: list[Mid]


class CtxOut(BoundaryModel):
    allowed: list[str]
    log_name: str
    readonly: bool


class OtherOut(BoundaryModel):
    q: str


@tool(name="echo", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def echo(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Echo the query."""
    RAN.append("echo")
    return EchoOut(q=inp.q)


@tool(name="echo_untrusted", tier=Tier.R1, effects={Effect.READ_LIVE}, untrusted_output=True)
async def echo_untrusted(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Echo the query (output flagged untrusted)."""
    RAN.append("echo_untrusted")
    return EchoOut(q=inp.q)


@tool(name="nested", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def nested(ctx: ToolContext, inp: NestedIn) -> EchoOut:
    """Nested input."""
    RAN.append("nested")
    return EchoOut(q=inp.inner.word)


@tool(name="floaty", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def floaty(ctx: ToolContext, inp: FloatIn) -> FloatOut:
    """Float input."""
    RAN.append("floaty")
    return FloatOut(x=None)


@tool(name="read_mid", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def read_mid(ctx: ToolContext, inp: MidIn) -> EchoOut:
    """Reads one message."""
    RAN.append("read_mid")
    return EchoOut(q="ok")


@tool(name="w1_tag", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def w1_tag(ctx: ToolContext, inp: MidsIn) -> EchoOut:
    """A W1 dummy."""
    RAN.append("w1_tag")
    return EchoOut(q="ran")


@tool(name="w2_move", tier=Tier.R0, effects={Effect.MAIL_MOVE}, untrusted_output=False)
async def w2_move(ctx: ToolContext, inp: MidsIn) -> EchoOut:
    """A W2 dummy (declared R0, escalated by its effect)."""
    RAN.append("w2_move")
    return EchoOut(q="ran")


@tool(name="w3_delete", tier=Tier.W3, effects={Effect.MAIL_DELETE}, untrusted_output=False)
async def w3_delete(ctx: ToolContext, inp: MidsIn) -> EchoOut:
    """A W3 dummy."""
    RAN.append("w3_delete")
    return EchoOut(q="ran")


@tool(name="boom", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def boom(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Always fails, quoting its input."""
    RAN.append("boom")
    raise RuntimeError(f"failed on {inp.q}")


@tool(name="not_found", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def not_found(ctx: ToolContext, inp: MidIn) -> EchoOut:
    """Raises unknown_mid."""
    RAN.append("not_found")
    raise ToolError("unknown_mid", "mid is not indexed", reason="not_indexed")


@tool(name="bad_code", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def bad_code(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Raises a registry-only code."""
    raise ToolError("tier_not_enabled", "not mine to raise")


@tool(name="wrong_out", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def wrong_out(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Returns the wrong model."""
    return OtherOut(q=inp.q)  # type: ignore[return-value]  # the registry must catch this


@tool(name="out_of_scope", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def out_of_scope(ctx: ToolContext, inp: MidIn) -> EchoOut:
    """An index-only tool that tries to open a message in TB."""
    await ctx.bridge.request("mail.open", TbIdParams(tb_id=1))
    return EchoOut(q="opened")


@tool(name="live", tier=Tier.R1, effects={Effect.READ_LIVE}, untrusted_output=False)
async def live(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Asks the extension for its accounts."""
    await ctx.bridge.request("mail.accounts.list", Empty())
    return EchoOut(q="live")


@tool(name="peek_ctx", tier=Tier.R1, effects={Effect.READ_LIVE}, untrusted_output=False)
async def peek_ctx(ctx: ToolContext, inp: EchoIn) -> CtxOut:
    """Reports what its context holds."""
    return CtxOut(
        allowed=sorted(ctx.bridge.allowed), log_name=ctx.log.name, readonly=ctx.index.readonly
    )


def _count_fails(inp: EchoIn) -> int:
    raise KeyError(inp.q)


@tool(
    name="bad_count",
    tier=Tier.R0,
    effects={Effect.READ_INDEX},
    untrusted_output=False,
    count_targets=_count_fails,
)
async def bad_count(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """count_targets raises."""
    RAN.append("bad_count")
    return EchoOut(q="ran")


@tool(name="slow", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def slow(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Never finishes on its own."""
    await asyncio.sleep(3600)
    return EchoOut(q="late")


@tool(name="lookup", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def lookup(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Raises a ToolError while handling an exception that quotes its input."""
    try:
        return EchoOut(q={"other": "x"}[inp.q])
    except KeyError:
        raise ToolError("unknown_mid", "no such message")  # noqa: B904  # chain under test


@tool(name="sys_exit", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def sys_exit(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Raises a BaseException that is not an Exception."""
    RAN.append("sys_exit")
    raise SystemExit(3)


class BadDumpIn(BoundaryModel):
    q: str

    @field_serializer("q")
    def _dump_q(self, value: str) -> str:
        raise RuntimeError(f"cannot dump {value}")


@tool(name="bad_dump", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def bad_dump(ctx: ToolContext, inp: BadDumpIn) -> EchoOut:
    """Its In model's serializer raises, quoting the value."""
    RAN.append("bad_dump")
    return EchoOut(q="ran")


class BadCheckIn(BoundaryModel):
    q: str

    @field_validator("q")
    @classmethod
    def _check_q(cls, value: str) -> str:
        raise KeyError(f"lookup {value}")


@tool(name="bad_check", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def bad_check(ctx: ToolContext, inp: BadCheckIn) -> EchoOut:
    """Its In model's validator raises a KeyError (not wrapped by pydantic), quoting the value."""
    RAN.append("bad_check")
    return EchoOut(q="ran")


TOOLS = [
    bad_check,
    lookup,
    sys_exit,
    bad_dump,
    echo,
    echo_untrusted,
    nested,
    floaty,
    read_mid,
    w1_tag,
    w2_move,
    w3_delete,
    boom,
    not_found,
    bad_code,
    wrong_out,
    out_of_scope,
    live,
    peek_ctx,
    bad_count,
    slow,
]


# ---- fixtures -----------------------------------------------------------------------------------


class Recorder:
    """An ``ExtensionRequester`` that records requests and answers or raises as told."""

    def __init__(self, error: BridgeError | None = None) -> None:
        self.calls: list[str] = []
        self.error = error

    async def request(self, method: str, params: BaseModel) -> Any:
        self.calls.append(method)
        if self.error is not None:
            raise self.error
        return {}

    async def notify(self, method: str, params: BaseModel) -> None:
        self.calls.append(method)


@pytest.fixture(autouse=True)
def _reset_ran() -> None:
    RAN.clear()


@pytest.fixture
def ro_store(tmp_path: Path) -> Iterator[Store]:
    path = tmp_path / "index.db"
    Store.open(path).close()
    store = Store.open(path, readonly=True)
    yield store
    store.close()


@pytest.fixture
def audit() -> Iterator[AuditLog]:
    with AuditLog.open(audit_path(paths.resolve())) as log:
        yield log


def _runtime(audit: AuditLog, store: Store, bridge: Recorder | None = None) -> ToolRuntime:
    return ToolRuntime(
        audit=audit,
        index=store,
        search=SearchService(store, None, mode="fts"),
        bridge=bridge,
        config=TernConfig(),
        log=logging.getLogger("tern.tools.test_call"),
        clock=time.time,
    )


def _registry(runtime: ToolRuntime) -> Registry:
    return Registry([RegisteredTool(t, "plugin", "test") for t in TOOLS], runtime=runtime)


@pytest.fixture
def bridge() -> Recorder:
    return Recorder()


@pytest.fixture
def reg(audit: AuditLog, ro_store: Store, bridge: Recorder) -> Registry:
    return _registry(_runtime(audit, ro_store, bridge))


def call(reg: Registry, tool_name: Any, args: Any, caller: str = "agent:s1", **kw: Any) -> Any:
    return asyncio.run(reg.call(caller, tool_name, args, **kw))


def fails(reg: Registry, tool_name: Any, args: Any, code: str, **kw: Any) -> ToolError:
    with pytest.raises(ToolError) as info:
        call(reg, tool_name, args, **kw)
    assert info.value.code == code
    return info.value


def chain_text(err: BaseException) -> str:
    """Everything a later ``log.exception`` / ``exc_info`` would print for ``err``."""
    return "".join(traceback.format_exception(err))


def _break_append_call(audit: AuditLog) -> None:
    def broken(**kw: Any) -> str:
        raise sqlite3.OperationalError("disk I/O error")

    audit.append_call = broken  # type: ignore[method-assign]  # simulate a failing audit DB


def _break_append_event(audit: AuditLog) -> None:
    def broken(*a: Any, **kw: Any) -> int:
        raise sqlite3.OperationalError("database is locked")

    audit.append_event = broken  # type: ignore[method-assign]  # simulate a failing audit DB


# ---- runs ---------------------------------------------------------------------------------------


def test_r0_call_runs_and_writes_one_row_ahead(reg: Registry, audit: AuditLog) -> None:
    result = call(reg, "echo", {"q": "hello"}, caller_label="Some Client")
    assert isinstance(result, ToolResult)
    assert result.output == EchoOut(q="hello")
    assert result.untrusted_output is False
    assert RAN == ["echo"]
    assert audit.count() == 1
    rec = audit.get(result.call_id)
    assert (rec.caller, rec.caller_label, rec.tool, rec.tier) == (
        "agent:s1",
        "Some Client",
        "echo",
        Tier.R0,
    )
    assert (rec.decision, rec.reason, rec.initial_status, rec.status) == ("auto", None, "ok", "ok")
    assert rec.args == {"q": "hello", "limit": 10}  # validated (defaults filled)
    assert audit.events(result.call_id) == []


def test_untrusted_output_is_passed_through(reg: Registry) -> None:
    result = call(reg, "echo_untrusted", {"q": "x"}, caller="mcp:client-1")
    assert result.untrusted_output is True


def test_row_is_written_before_the_tool_runs(reg: Registry, audit: AuditLog) -> None:
    seen: list[int] = []
    real = audit.append_call

    def spy(**kw: Any) -> str:
        assert RAN == []  # nothing has run when the row is appended
        call_id = real(**kw)
        seen.append(audit.count())
        return call_id

    audit.append_call = spy  # type: ignore[method-assign]  # spy on the instance only
    call(reg, "echo", {"q": "x"})
    assert seen == [1] and RAN == ["echo"]


def test_target_mids_and_count_are_recorded(reg: Registry, audit: AuditLog) -> None:
    result = call(reg, "read_mid", {"mid": M1})
    rec = audit.get(result.call_id)
    assert (rec.target_mids, rec.target_count) == ((M1,), 1)


def test_validated_infinity_is_recorded_as_null_and_runs(reg: Registry, audit: AuditLog) -> None:
    result = call(reg, "floaty", {"x": float("inf")})
    assert RAN == ["floaty"]
    assert audit.get(result.call_id).args == {"x": None}


def test_context_is_scoped_per_tool(reg: Registry) -> None:
    out = call(reg, "peek_ctx", {"q": "x"}).output
    assert out.readonly is True
    assert out.log_name == "tern.tools.test_call.peek_ctx"
    assert "mail.open" in out.allowed and "mail.update" not in out.allowed


# ---- W tiers ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tier"), [("w1_tag", Tier.W1), ("w2_move", Tier.W2), ("w3_delete", Tier.W3)]
)
def test_write_tiers_are_not_enabled(reg: Registry, audit: AuditLog, name: str, tier: Tier) -> None:
    err = fails(reg, name, {"mids": [M1, M2]}, "tier_not_enabled")
    assert tier.value in err.message
    assert RAN == []
    (rec,) = audit.list()
    assert (rec.tool, rec.tier, rec.decision, rec.reason) == (
        name,
        tier,
        "denied",
        "tier_not_enabled",
    )
    assert rec.initial_status is None and rec.status is None
    assert rec.args == {"mids": [M1, M2]}
    assert (rec.target_mids, rec.target_count) == ((M1, M2), 2)


# ---- invalid args -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"q": SECRET * 5},  # too long
        {"q": 7, "limit": SECRET},  # wrong types
        {"limit": 3},  # missing
        {"q": "ok", SECRET: 1},  # an extra key: its name is caller text too
        {"q": "ok", "limit": {"deep": SECRET}},
    ],
)
def test_invalid_args_write_one_row_and_echo_no_value(
    reg: Registry, audit: AuditLog, args: dict[str, Any]
) -> None:
    err = fails(reg, "echo", args, "invalid_params")
    assert SECRET not in str(err) and SECRET not in err.message
    assert err.message.startswith("invalid arguments: ")
    assert RAN == []
    (rec,) = audit.list()
    assert (rec.tool, rec.tier, rec.decision, rec.reason) == (
        "echo",
        Tier.R0,
        "error",
        "invalid_params",
    )
    assert rec.args is None and rec.args_sha256 is not None
    assert rec.initial_status is None


def test_invalid_args_message_names_locations_and_codes(reg: Registry) -> None:
    err = fails(reg, "nested", {"inner": {"word": 5}, "tags": ["a", 3], "x": 1}, "invalid_params")
    assert "inner.word: string_type" in err.message
    assert "tags.1: string_type" in err.message
    assert "*: extra_forbidden" in err.message


@pytest.mark.parametrize("args", [[("q", "x")], "q=x", 5, None])
def test_args_that_are_not_an_object(reg: Registry, audit: AuditLog, args: Any) -> None:
    fails(reg, "echo", args, "invalid_params")
    (rec,) = audit.list()
    assert (rec.reason, rec.args, rec.args_sha256) == ("invalid_params", None, None)


def test_raising_validator_is_recorded_as_internal(reg: Registry, audit: AuditLog) -> None:
    err = fails(reg, "bad_check", {"q": SECRET}, "internal")
    assert (err.reason, err.message) == ("validate", "cannot validate the arguments (KeyError)")
    assert SECRET not in chain_text(err)
    assert RAN == []
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.args, rec.tier) == ("error", "internal", None, Tier.R0)


def test_count_targets_failure_is_recorded_as_internal(reg: Registry, audit: AuditLog) -> None:
    err = fails(reg, "bad_count", {"q": SECRET}, "internal")
    assert SECRET not in str(err)
    assert err.reason == "targets"
    assert RAN == []
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.args) == ("error", "internal", None)


# ---- unknown tools ------------------------------------------------------------------------------


def test_unknown_snake_case_tool_is_recorded_by_name(
    reg: Registry, audit: AuditLog, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="tern.tools.registry")
    err = fails(reg, "send_mail", {"to": SECRET}, "unknown_tool")
    # Merge review: the model-chosen name is kept in the audit row only, never logged.
    (line,) = [r for r in caplog.records if r.getMessage() == "tool call"]
    assert line.tool == UNKNOWN_LOG_TOOL  # type: ignore[attr-defined]  # set via logging extra
    for r in caplog.records:
        assert "send_mail" not in repr(r.__dict__)
    assert "send_mail" not in err.message and SECRET not in err.message
    (rec,) = audit.list()
    assert (rec.tool, rec.tier, rec.decision, rec.reason) == (
        "send_mail",
        None,
        "error",
        "unknown_tool",
    )
    assert rec.args is None


@pytest.mark.parametrize("name", [f"Send {SECRET}", "Echo", "echo\n", "", 42, None])
def test_unknown_invalid_tool_name_is_recorded_as_invalid(
    reg: Registry, audit: AuditLog, name: Any
) -> None:
    err = fails(reg, name, {}, "unknown_tool")
    assert SECRET not in str(err)
    (rec,) = audit.list()
    assert rec.tool == INVALID_TOOL
    assert SECRET not in repr(rec)


# ---- tool failures ------------------------------------------------------------------------------


def test_raising_tool_gets_an_error_event(reg: Registry, audit: AuditLog) -> None:
    err = fails(reg, "boom", {"q": SECRET}, "internal")
    assert SECRET not in str(err)
    assert err.__cause__ is None and err.__suppress_context__
    assert (err.reason, err.message) == ("tool_failed", "tool failed (RuntimeError)")
    (rec,) = audit.list()
    assert (rec.decision, rec.initial_status, rec.status) == ("auto", "ok", "error")
    (ev,) = audit.events(rec.call_id)
    assert ev.kind == "error"
    assert ev.detail == {"code": "internal", "exc": "RuntimeError", "reason": "tool_failed"}


def test_tool_error_from_a_tool_passes_through(reg: Registry, audit: AuditLog) -> None:
    err = fails(reg, "not_found", {"mid": M1}, "unknown_mid")
    assert err.reason == "not_indexed"
    (rec,) = audit.list()
    (ev,) = audit.events(rec.call_id)
    assert ev.detail == {"code": "unknown_mid", "exc": "ToolError", "reason": "not_indexed"}


def test_registry_only_code_from_a_tool_is_internal(reg: Registry) -> None:
    err = fails(reg, "bad_code", {"q": "x"}, "internal")
    assert err.reason == "bad_code"


def test_wrong_output_type_is_internal(reg: Registry, audit: AuditLog) -> None:
    err = fails(reg, "wrong_out", {"q": SECRET}, "internal")
    assert err.reason == "bad_output" and SECRET not in str(err)
    (rec,) = audit.list()
    (ev,) = audit.events(rec.call_id)
    assert ev.detail["exc"] == "OtherOut"


def test_out_of_scope_bridge_call_is_internal(
    reg: Registry, audit: AuditLog, bridge: Recorder
) -> None:
    err = fails(reg, "out_of_scope", {"mid": M1}, "internal")
    assert err.reason == "bridge_scope"
    assert bridge.calls == []
    (rec,) = audit.list()
    assert audit.events(rec.call_id)[0].detail["exc"] == "BridgeScopeError"


def test_no_extension_attached_is_unavailable(audit: AuditLog, ro_store: Store) -> None:
    reg = _registry(_runtime(audit, ro_store, None))
    err = fails(reg, "live", {"q": "x"}, "unavailable")
    assert err.reason == "unavailable"


@pytest.mark.parametrize(
    ("name", "code", "reason"),
    [
        ("unavailable", "unavailable", "unavailable"),
        ("tb_error", "unavailable", "tb_error"),
        ("stale_id", "internal", "stale_id"),
        ("invalid_params", "internal", "invalid_params"),
    ],
)
def test_bridge_errors_are_mapped(
    audit: AuditLog, ro_store: Store, name: Any, code: str, reason: str
) -> None:
    bridge = Recorder(BridgeError(name, f"tb said {SECRET}"))
    reg = _registry(_runtime(audit, ro_store, bridge))
    err = fails(reg, "live", {"q": "x"}, code)
    assert err.reason == reason and SECRET not in str(err)
    assert bridge.calls == ["mail.accounts.list"]
    (rec,) = audit.list()
    assert audit.events(rec.call_id)[0].detail == {
        "code": code,
        "exc": "BridgeError",
        "reason": reason,
    }


def test_cancelled_call_gets_an_error_event(reg: Registry, audit: AuditLog) -> None:
    async def run() -> None:
        task = asyncio.create_task(reg.call("job:nightly", "slow", {"q": "x"}))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    (rec,) = audit.list()
    assert rec.status == "error"
    assert audit.events(rec.call_id)[0].detail == {
        "code": "internal",
        "exc": "cancel",
        "reason": "cancelled",
    }


# ---- audit failures -----------------------------------------------------------------------------


def test_failed_audit_append_means_the_tool_never_runs(reg: Registry, audit: AuditLog) -> None:
    def broken(**kw: Any) -> str:
        raise sqlite3.OperationalError("disk I/O error")

    audit.append_call = broken  # type: ignore[method-assign]  # simulate a failing audit DB
    err = fails(reg, "echo", {"q": "x"}, "internal")
    assert err.reason == "audit_failed"
    assert isinstance(err.__cause__, sqlite3.OperationalError)
    assert RAN == []


def test_closed_audit_db_means_the_tool_never_runs(reg: Registry, audit: AuditLog) -> None:
    audit.close()
    err = fails(reg, "echo", {"q": "x"}, "internal")
    assert err.reason == "audit_failed"
    assert RAN == []


def test_failed_error_event_is_internal(reg: Registry, audit: AuditLog) -> None:
    def broken(*a: Any, **kw: Any) -> int:
        raise sqlite3.OperationalError("database is locked")

    audit.append_event = broken  # type: ignore[method-assign]  # simulate a failing audit DB
    err = fails(reg, "not_found", {"mid": M1}, "internal")
    assert err.reason == "audit_failed"
    assert audit.count() == 1


def test_failed_error_event_does_not_chain_the_tool_exception(
    reg: Registry, audit: AuditLog
) -> None:
    _break_append_event(audit)
    err = fails(reg, "boom", {"q": SECRET}, "internal")
    assert err.reason == "audit_failed"
    assert isinstance(err.__cause__, sqlite3.OperationalError)
    assert SECRET not in chain_text(err)


def test_failed_append_does_not_chain_the_count_targets_exception(
    reg: Registry, audit: AuditLog
) -> None:
    _break_append_call(audit)
    err = fails(reg, "bad_count", {"q": SECRET}, "internal")
    assert err.reason == "audit_failed"
    assert SECRET not in chain_text(err)


def test_failed_append_does_not_chain_the_validation_error(reg: Registry, audit: AuditLog) -> None:
    _break_append_call(audit)
    err = fails(reg, "echo", {"q": "x", SECRET: 1}, "internal")
    assert err.reason == "audit_failed"
    assert SECRET not in chain_text(err)


def test_failed_append_does_not_chain_the_validator_exception(
    reg: Registry, audit: AuditLog
) -> None:
    _break_append_call(audit)
    err = fails(reg, "bad_check", {"q": SECRET}, "internal")
    assert err.reason == "audit_failed"
    assert SECRET not in chain_text(err)
    assert RAN == []


def test_failed_append_does_not_chain_the_serialization_error(
    reg: Registry, audit: AuditLog
) -> None:
    _break_append_call(audit)
    err = fails(reg, "bad_dump", {"q": SECRET}, "internal")
    assert err.reason == "audit_failed"
    assert SECRET not in chain_text(err)


def test_tool_error_raised_while_handling_content_is_not_chained(
    reg: Registry, audit: AuditLog
) -> None:
    err = fails(reg, "lookup", {"q": SECRET}, "unknown_mid")
    assert SECRET not in chain_text(err)
    _break_append_event(audit)
    err = fails(reg, "lookup", {"q": SECRET}, "internal")
    assert err.reason == "audit_failed" and SECRET not in chain_text(err)


def test_cancel_still_propagates_when_the_error_event_fails(
    reg: Registry, audit: AuditLog, caplog: pytest.LogCaptureFixture
) -> None:
    _break_append_event(audit)
    caplog.set_level(logging.INFO, logger="tern.tools.registry")

    async def run() -> asyncio.Task[Any]:
        task = asyncio.create_task(reg.call("job:nightly", "slow", {"q": "x"}))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task

    task = asyncio.run(run())
    assert task.cancelled()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errors] == ["audit error event not written"]
    lines = [r for r in caplog.records if r.getMessage() == "tool call"]
    assert [(r.levelno, r.code) for r in lines] == [(logging.WARNING, "cancelled")]  # type: ignore[attr-defined]  # extra=


def test_base_exception_gets_an_aborted_error_event(
    reg: Registry, audit: AuditLog, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="tern.tools.registry")
    with pytest.raises(SystemExit):
        call(reg, "sys_exit", {"q": "x"})
    assert RAN == ["sys_exit"]
    (rec,) = audit.list()
    assert rec.status == "error"
    assert audit.events(rec.call_id)[0].detail == {
        "code": "internal",
        "exc": "SystemExit",
        "reason": "aborted",
    }
    records = [r for r in caplog.records if r.name == "tern.tools.registry"]
    assert [(r.levelno, r.code) for r in records] == [(logging.WARNING, "aborted")]  # type: ignore[attr-defined]  # extra=


def test_base_exception_propagates_when_the_error_event_fails(
    reg: Registry, audit: AuditLog, caplog: pytest.LogCaptureFixture
) -> None:
    _break_append_event(audit)
    caplog.set_level(logging.INFO, logger="tern.tools.registry")
    with pytest.raises(SystemExit):
        call(reg, "sys_exit", {"q": "x"})
    lines = [r for r in caplog.records if r.getMessage() == "tool call"]
    assert [(r.levelno, r.code) for r in lines] == [(logging.WARNING, "aborted")]  # type: ignore[attr-defined]  # extra=


def test_unserializable_args_are_internal_not_a_count_failure(
    reg: Registry, audit: AuditLog
) -> None:
    err = fails(reg, "bad_dump", {"q": SECRET}, "internal")
    assert (err.reason, err.message) == ("serialize", "cannot serialize the arguments")
    assert SECRET not in chain_text(err)
    assert RAN == []
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.args) == ("error", "internal", None)


def test_a_registry_error_reading_targets_is_not_a_count_failure(
    reg: Registry, audit: AuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: ToolSpec, inp: BoundaryModel) -> list[str]:
        raise LookupError("registry bug")

    monkeypatch.setattr(ToolSpec, "target_mids", broken)
    with pytest.raises(LookupError):
        call(reg, "echo", {"q": "x"})
    assert RAN == []


# ---- caller and runtime -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "caller", ["", "agent", "agent:", "bot:x", "ui\n", "agent:s 1", "mcp:" + "x" * 65, 3]
)
def test_invalid_caller_is_a_value_error_without_a_row(
    reg: Registry, audit: AuditLog, caller: Any
) -> None:
    with pytest.raises(ValueError, match="caller"):
        call(reg, "echo", {"q": "x"}, caller=caller)
    assert audit.count() == 0 and RAN == []


@pytest.mark.parametrize("caller", ["ui", "agent:s1", "mcp:claude-desktop", "job:triage.v1"])
def test_valid_callers(reg: Registry, audit: AuditLog, caller: str) -> None:
    result = call(reg, "echo", {"q": "x"}, caller=caller)
    assert audit.get(result.call_id).caller == caller


@pytest.mark.parametrize("label", ["", "x" * 129, "a\nb"])
def test_invalid_caller_label_is_a_value_error(reg: Registry, audit: AuditLog, label: str) -> None:
    with pytest.raises(ValueError, match="caller_label"):
        call(reg, "echo", {"q": "x"}, caller_label=label)
    assert audit.count() == 0


def test_registry_without_runtime_cannot_call() -> None:
    reg = Registry([RegisteredTool(echo, "plugin", "test")])
    with pytest.raises(RuntimeError, match="ToolRuntime"):
        asyncio.run(reg.call("ui", "echo", {"q": "x"}))
    assert RAN == []


def test_runtime_refuses_a_writable_store(audit: AuditLog, tmp_path: Path) -> None:
    store = Store.open(tmp_path / "rw.db")
    try:
        with pytest.raises(ToolContextError, match="readonly"):
            _runtime(audit, store)
    finally:
        store.close()


def test_runtime_refuses_a_non_audit_log(ro_store: Store, audit: AuditLog) -> None:
    rt = _runtime(audit, ro_store)
    with pytest.raises(ToolContextError, match="AuditLog"):
        ToolRuntime(
            audit=object(),  # type: ignore[arg-type]  # the runtime check is what is tested
            index=rt.index,
            search=rt.search,
            bridge=None,
            config=rt.config,
            log=rt.log,
            clock=rt.clock,
        )


def test_discover_passes_the_runtime(audit: AuditLog, ro_store: Store) -> None:
    reg = Registry.discover(runtime=_runtime(audit, ro_store))
    fails(reg, "no_such_tool", {}, "unknown_tool")
    assert audit.count() == 1


# ---- logging ------------------------------------------------------------------------------------


def test_one_log_line_per_call_without_args(
    reg: Registry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="tern.tools.registry")
    call(reg, "echo", {"q": SECRET})
    fails(reg, "w1_tag", {"mids": [M1]}, "tier_not_enabled")
    fails(reg, "echo", {"q": 5, "limit": SECRET}, "invalid_params")
    records = [r for r in caplog.records if r.name == "tern.tools.registry"]
    assert [r.levelno for r in records] == [logging.INFO] * 3
    fields = [
        (r.tool, r.tier, r.caller, r.decision, r.code, r.targets)  # type: ignore[attr-defined]  # extra=
        for r in records
    ]
    assert fields == [
        ("echo", "R0", "agent:s1", "auto", None, 0),
        ("w1_tag", "W1", "agent:s1", "denied", "tier_not_enabled", 1),
        ("echo", "R0", "agent:s1", "error", "invalid_params", 0),
    ]
    assert all(r.duration_ms >= 0 for r in records)  # type: ignore[attr-defined]  # extra=
    for r in records:
        assert SECRET not in r.getMessage() and SECRET not in repr(r.__dict__)


def test_tool_failure_logs_a_warning_without_content(
    reg: Registry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="tern.tools.registry")
    fails(reg, "boom", {"q": SECRET}, "internal")
    records = [r for r in caplog.records if r.name == "tern.tools.registry"]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert SECRET not in caplog.text


# ---- ToolError ----------------------------------------------------------------------------------


def test_tool_error_fields() -> None:
    err = ToolError("unknown_mid", "mid is not indexed", reason="no_message_id")
    assert (err.code, err.message, err.reason) == (
        "unknown_mid",
        "mid is not indexed",
        "no_message_id",
    )
    assert str(err) == "unknown_mid: mid is not indexed"


@pytest.mark.parametrize(
    ("code", "message", "reason"),
    [
        ("nope", "m", None),
        ("internal", "", None),
        ("internal", "m", "Not a code"),
        ("internal", "m", "code\n"),
    ],
)
def test_tool_error_refuses_bad_values(code: Any, message: str, reason: str | None) -> None:
    with pytest.raises(ValueError):
        ToolError(code, message, reason=reason)


# ---- plan 06b step 0: engine-only error codes, empty targets for read tools --------------------

#: The permission engine's codes (plan 06b): a tool may not raise them.
ENGINE_CODES = (
    "rate_limited",
    "ambiguous_location",
    "denied",
    "expired",
    "targets_changed",
    "state_changed",
)


class TargetsOut(BoundaryModel):
    n: int
    empty: bool


@tool(name="peek_targets", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def peek_targets(ctx: ToolContext, inp: EchoIn) -> TargetsOut:
    """Reports what its context's targets hold."""
    return TargetsOut(n=len(ctx.targets), empty=ctx.targets == {})


@tool(name="peek_targets_live", tier=Tier.R1, effects={Effect.READ_LIVE}, untrusted_output=False)
async def peek_targets_live(ctx: ToolContext, inp: EchoIn) -> TargetsOut:
    """Reports what its context's targets hold (live-read tier)."""
    return TargetsOut(n=len(ctx.targets), empty=ctx.targets == {})


@tool(name="engine_code", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def engine_code(ctx: ToolContext, inp: EchoIn) -> EchoOut:
    """Raises the engine-only code named by ``q``."""
    raise ToolError(cast("ToolErrorCode", inp.q), "not mine to raise")


@pytest.fixture
def reg06b(audit: AuditLog, ro_store: Store, bridge: Recorder) -> Registry:
    tools = [peek_targets, peek_targets_live, engine_code]
    return Registry(
        [RegisteredTool(t, "plugin", "test") for t in tools],
        runtime=_runtime(audit, ro_store, bridge),
    )


def test_engine_codes_are_tool_error_codes() -> None:
    assert set(ENGINE_CODES) <= set(typing.get_args(ToolErrorCode))
    assert not set(ENGINE_CODES) & TOOL_RAISABLE_CODES


@pytest.mark.parametrize("code", ENGINE_CODES)
def test_engine_code_from_a_tool_is_internal(reg06b: Registry, audit: AuditLog, code: str) -> None:
    err = fails(reg06b, "engine_code", {"q": code}, "internal")
    assert err.reason == "bad_code"
    (rec,) = audit.list()
    (ev,) = audit.events(rec.call_id)
    assert ev.detail == {"code": "internal", "exc": "ToolError", "reason": "bad_code"}


@pytest.mark.parametrize("name", ["peek_targets", "peek_targets_live"])
def test_read_tools_get_empty_targets(reg06b: Registry, name: str) -> None:
    out = call(reg06b, name, {"q": "x"}).output
    assert (out.n, out.empty) == (0, True)


# ---- plan 06b step 1: provenance, undo_token, the engine-routed W path --------------------------


class _AllKnown:
    async def is_known(self, addr: str) -> bool:
        return True


def _engine(audit: AuditLog, store: Store, bridge: Recorder | None, clock: Any) -> Engine:
    return Engine(
        audit=audit, index=store, bridge=bridge, clock=clock, known_recipients=_AllKnown()
    )


def _engine_runtime(audit: AuditLog, store: Store, bridge: Recorder | None) -> ToolRuntime:
    rt = _runtime(audit, store, bridge)
    return dataclasses.replace(rt, engine=_engine(audit, store, bridge, rt.clock))


@pytest.mark.parametrize("provenance", [(), [M1], (M1, M2), [M2, M2]])
def test_valid_provenance_is_accepted(reg: Registry, audit: AuditLog, provenance: Any) -> None:
    result = call(reg, "echo", {"q": "x"}, provenance=provenance)
    assert RAN == ["echo"] and audit.count() == 1
    assert result.undo_token is None


@pytest.mark.parametrize(
    "provenance",
    [
        M1,  # a str is a Sequence, but not of mids
        M1.encode(),
        [SECRET],
        [M1, 5],
        [M1.upper()],
        [M1 + "\n"],
        ["x-" + M1[:63]],
        {M1},  # not a Sequence
        None,
        [None],
    ],
)
def test_invalid_provenance_is_a_value_error_without_a_row(
    reg: Registry, audit: AuditLog, provenance: Any
) -> None:
    with pytest.raises(ValueError, match="provenance") as info:
        call(reg, "echo", {"q": "x"}, provenance=provenance)
    assert SECRET not in chain_text(info.value)
    assert audit.count() == 0 and RAN == []


def test_tool_result_undo_token_defaults_to_none() -> None:
    res = ToolResult(output=EchoOut(q="x"), untrusted_output=False, call_id="c")
    assert res.undo_token is None


@pytest.mark.parametrize(("name", "tier"), [("w2_move", Tier.W2), ("w3_delete", Tier.W3)])
def test_engine_routed_write_is_still_denied_with_its_tier(
    audit: AuditLog, ro_store: Store, bridge: Recorder, name: str, tier: Tier
) -> None:
    reg = _registry(_engine_runtime(audit, ro_store, bridge))
    # Plan 06b step 5: W2/W3 need an approval; without an approval UI they are refused up front.
    err = fails(reg, name, {"mids": [M1, M2]}, "unavailable")
    assert err.reason == "no_approval_ui" and err.message.startswith(f"{tier.value} ")
    assert RAN == [] and bridge.calls == []
    (rec,) = audit.list()
    assert (rec.tier, rec.decision, rec.reason, rec.initial_status) == (
        tier,
        "denied",
        "unavailable",
        None,
    )


def test_engine_routed_w1_reaches_target_resolution(
    audit: AuditLog, ro_store: Store, bridge: Recorder
) -> None:
    # Plan 06b step 3: a W1 call is no longer denied up front; its targets are resolved first
    # (mids not in the empty index -> unknown_mid, before any bridge call).
    reg = _registry(_engine_runtime(audit, ro_store, bridge))
    err = fails(reg, "w1_tag", {"mids": [M1, M2]}, "unknown_mid")
    assert err.reason == "not_indexed"
    assert RAN == [] and bridge.calls == []
    (rec,) = audit.list()
    assert (rec.tier, rec.decision, rec.reason, rec.initial_status) == (
        Tier.W1,
        "error",
        "unknown_mid",
        None,
    )


def test_engine_routed_write_escalates_by_count(
    audit: AuditLog, ro_store: Store, bridge: Recorder
) -> None:
    reg = _registry(_engine_runtime(audit, ro_store, bridge))
    many = [f"{i:064x}" for i in range(1, 22)]
    err = fails(reg, "w1_tag", {"mids": many}, "unavailable")  # W2: no approval UI (step 5)
    assert err.message.startswith("W2 ")
    (rec,) = audit.list()
    assert (rec.tier, rec.target_count) == (Tier.W2, 21)


def test_engine_routed_read_runs(audit: AuditLog, ro_store: Store, bridge: Recorder) -> None:
    reg = _registry(_engine_runtime(audit, ro_store, bridge))
    result = call(reg, "read_mid", {"mid": M1}, provenance=[M2])
    assert RAN == ["read_mid"] and result.undo_token is None
    assert audit.get(result.call_id).tier is Tier.R0


def test_runtime_engine_defaults_to_none(audit: AuditLog, ro_store: Store) -> None:
    assert _runtime(audit, ro_store).engine is None


def test_runtime_refuses_an_engine_over_other_parts(
    audit: AuditLog, ro_store: Store, bridge: Recorder, tmp_path: Path
) -> None:
    rt = _runtime(audit, ro_store, bridge)
    other_path = tmp_path / "other-index.db"
    Store.open(other_path).close()
    other_store = Store.open(other_path, readonly=True)
    try:
        with AuditLog.open(audit_path(paths.resolve())) as other_audit:
            cases: list[tuple[Any, str]] = [
                (object(), "Engine or None"),
                (_engine(other_audit, ro_store, bridge, rt.clock), "ToolRuntime.audit"),
                (_engine(audit, ro_store, bridge, lambda: 0.0), "ToolRuntime.clock"),
                (_engine(audit, ro_store, Recorder(), rt.clock), "ToolRuntime.bridge"),
                (_engine(audit, other_store, bridge, rt.clock), "ToolRuntime.index"),
            ]
            for engine, match in cases:
                with pytest.raises(ToolContextError, match=match):
                    dataclasses.replace(rt, engine=engine)
    finally:
        other_store.close()
