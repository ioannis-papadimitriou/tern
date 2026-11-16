"""Decisions #39: per-chat permission modes in ternd's chat glue (``ChatHost.start`` /
``ChatHost.approve``, bridge 1.2 ``chat.start`` / ``chat.approval`` / ``chat.approve``).

Each test runs ``serve()`` in-process (the real registry, engine, approvals queue and audit log,
``FakeLLM``) and hands a :class:`~tests.daemon.test_chat.RecordingPeer` straight to the daemon's
``ChatHost``, except the last tests, which go over the socket as a 1.2 (and a 1.1) client. The
tool is the built-in ``skill`` (R0, no mail needed). Synthetic data only."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from tern.bridge.protocol import (
    BridgeError,
    ChatApproveParams,
    ChatSendParams,
    ChatStartParams,
)
from tern.daemon import chat as chat_mod
from tern.log import JsonFormatter
from tern.perms.audit import AuditLog, audit_path
from tern.testing.fake_llm import FakeLLM, FakeToolCall, ScriptStep

from .test_chat import (
    DEADLINE_S,
    ChatClient,
    RecordingPeer,
    audit_rows,
    chat_cfg,
    chat_client,
    key_of,
    recording_peer,
    remote_cfg,
    running,
    wait_for,
)

SKILL = "thunderbird-daily-brief"
SENTINEL = "SENTINEL-chat-mode-9d2a"


def skill_call(name: str = SKILL) -> FakeToolCall:
    return FakeToolCall(name="skill", arguments={"name": name})


def approvals_of(peer: RecordingPeer, turn_id: str) -> list[dict[str, Any]]:
    return [p for m, p in peer.of(turn_id) if m == "chat.approval"]


async def approval_n(peer: RecordingPeer, turn_id: str, n: int = 1) -> dict[str, Any]:
    await wait_for(lambda: len(approvals_of(peer, turn_id)) >= n, f"approval {n}")
    return approvals_of(peer, turn_id)[n - 1]


def decide(note: dict[str, Any], decision: str, **kw: Any) -> ChatApproveParams:
    return ChatApproveParams.model_validate(
        {
            "turn_id": note["turn_id"],
            "approval_id": note["approval_id"],
            "nonce": note["nonce"],
            "decision": decision,
            **kw,
        }
    )


def start(session_id: str, mode: str, text: str = "brief me") -> ChatStartParams:
    return ChatStartParams.model_validate({"session_id": session_id, "text": text, "mode": mode})


def ended(peer: RecordingPeer, turn_id: str) -> tuple[str, dict[str, Any]] | None:
    """The turn's ``chat.done``/``chat.error`` (``RecordingPeer.ended`` predates chat.approval)."""
    return next(
        ((m, p) for m, p in peer.of(turn_id) if m in ("chat.done", "chat.error")),
        None,
    )


async def end_of(peer: RecordingPeer, turn_id: str) -> tuple[str, dict[str, Any]]:
    await wait_for(lambda: ended(peer, turn_id) is not None, "the end of the turn")
    end = ended(peer, turn_id)
    assert end is not None
    return end


def calls(run: Any) -> list[tuple[str, str, str]]:
    return [r for r in audit_rows(run.tp) if r[0] == "skill"]


@pytest.mark.asyncio
async def test_manual_sends_the_card_then_runs_the_call_once_allowed() -> None:
    fake = FakeLLM.from_steps(
        ScriptStep(tool_calls=[skill_call()]), ScriptStep(content="Here is your brief.")
    )
    async with running(chat_cfg(), fake) as run, recording_peer() as peer:
        host = run.daemon.chat
        sid = "s-manual"
        turn = await host.start(peer, start(sid, "manual"))
        note = await approval_n(peer, turn.turn_id)
        assert note["tool"] == "skill" and note["tier"] == "R0"
        assert note["args"] == {"name": SKILL}
        assert set(note) == {
            "turn_id",
            "approval_id",
            "nonce",
            "tool",
            "tier",
            "args",
            "expires_at",
        }
        assert calls(run) == []  # nothing ran, nothing recorded while it waits
        assert run.daemon.engine.approvals.pending()[0].approval_id == note["approval_id"]
        decided = await host.approve(peer, decide(note, "approve"))
        assert decided.status == "approved"
        await end_of(peer, turn.turn_id)
        assert ended(peer, turn.turn_id) == (
            "chat.done",
            {"turn_id": turn.turn_id, "citations": []},
        )
        fake.assert_done()
    assert calls(run) == [("skill", f"agent:{key_of(sid)}", "approved")]
    # The model got the skill text (the tool ran).
    tool_msg = [m for m in fake.requests[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_msg) == 1 and '"error"' not in tool_msg[0]["content"]


@pytest.mark.asyncio
async def test_manual_deny_gives_the_model_denied_and_the_turn_goes_on() -> None:
    fake = FakeLLM.from_steps(
        ScriptStep(tool_calls=[skill_call()]), ScriptStep(content="I was not allowed to.")
    )
    async with running(chat_cfg(), fake) as run, recording_peer() as peer:
        host = run.daemon.chat
        turn = await host.start(peer, start("s-deny", "manual"))
        note = await approval_n(peer, turn.turn_id)
        assert (await host.approve(peer, decide(note, "deny"))).status == "denied"
        await end_of(peer, turn.turn_id)
        assert ended(peer, turn.turn_id) == (
            "chat.done",
            {"turn_id": turn.turn_id, "citations": []},
        )
        fake.assert_done()
    [tool_msg] = [m for m in fake.requests[1]["messages"] if m.get("role") == "tool"]
    assert '"code": "denied"' in tool_msg["content"]
    assert '"reason": "user_denied"' in tool_msg["content"]
    with AuditLog.open(audit_path(run.tp)) as reader:
        [rec] = [r for r in reader.list(limit=100) if r.tool == "skill"]
    assert (rec.decision, rec.reason, rec.initial_status) == ("denied", "user_denied", None)


@pytest.mark.parametrize("how", ["bypass", "chat.send"])
@pytest.mark.asyncio
async def test_bypass_and_a_send_without_mode_never_ask(how: str) -> None:
    fake = FakeLLM.from_steps(ScriptStep(tool_calls=[skill_call()]), ScriptStep(content="Done."))
    async with running(chat_cfg(), fake) as run, recording_peer() as peer:
        host = run.daemon.chat
        if how == "bypass":
            turn = await host.start(peer, start("s-bypass", "bypass"))
        else:
            turn = await host.send(peer, ChatSendParams(session_id="s-bypass", text="brief me"))
        await end_of(peer, turn.turn_id)
        assert ended(peer, turn.turn_id) == (
            "chat.done",
            {"turn_id": turn.turn_id, "citations": []},
        )
        assert approvals_of(peer, turn.turn_id) == []
    assert [d for _, _, d in calls(run)] == ["auto"]


@pytest.mark.asyncio
async def test_rest_of_turn_approves_the_later_calls_of_that_turn_only() -> None:
    fake = FakeLLM.from_steps(
        ScriptStep(tool_calls=[skill_call(), skill_call("thunderbird-follow-ups")]),
        ScriptStep(content="Both."),
        ScriptStep(tool_calls=[skill_call()]),
        ScriptStep(content="Next turn."),
    )
    async with running(chat_cfg(), fake) as run, recording_peer() as peer:
        host = run.daemon.chat
        turn = await host.start(peer, start("s-rest", "manual"))
        note = await approval_n(peer, turn.turn_id)
        decided = await host.approve(peer, decide(note, "approve", rest_of_turn=True))
        assert decided.status == "approved"
        await end_of(peer, turn.turn_id)
        assert len(approvals_of(peer, turn.turn_id)) == 1  # the second call was not asked
        # The next turn asks again.
        nxt = await host.start(peer, start("s-rest", "manual", "again"))
        note2 = await approval_n(peer, nxt.turn_id)
        await host.approve(peer, decide(note2, "approve"))
        await end_of(peer, nxt.turn_id)
        fake.assert_done()
    # Every call still has its own approved row.
    assert [d for _, _, d in calls(run)] == ["approved", "approved", "approved"]


@pytest.mark.asyncio
async def test_chat_approve_is_refused_for_anything_but_an_open_approval_of_the_peers_turn() -> (
    None
):
    fake = FakeLLM.from_steps(ScriptStep(tool_calls=[skill_call()]), ScriptStep(content="Done."))
    async with (
        running(chat_cfg(), fake) as run,
        recording_peer() as peer,
        recording_peer() as other,
    ):
        host = run.daemon.chat
        turn = await host.start(peer, start("s-guard", "manual"))
        note = await approval_n(peer, turn.turn_id)
        refusals = [
            (other, decide(note, "approve")),  # another connection
            (peer, decide({**note, "approval_id": "f" * 32}, "approve")),  # unknown approval
            (peer, decide({**note, "turn_id": "no-such-turn"}, "approve")),  # unknown turn
        ]
        for who, params in refusals:
            with pytest.raises(BridgeError) as exc:
                await host.approve(who, params)
            assert exc.value.name == "invalid_params"
        assert run.daemon.engine.approvals.pending() != []  # none of those decided anything
        assert (await host.approve(peer, decide(note, "approve"))).status == "approved"
        with pytest.raises(BridgeError) as again:  # decided already: no longer open
            await host.approve(peer, decide(note, "deny"))
        assert again.value.name == "invalid_params"
        await end_of(peer, turn.turn_id)
    assert [d for _, _, d in calls(run)] == ["approved"]


@pytest.mark.asyncio
async def test_a_wrong_nonce_denies_the_call() -> None:
    fake = FakeLLM.from_steps(ScriptStep(tool_calls=[skill_call()]), ScriptStep(content="Done."))
    async with running(chat_cfg(), fake) as run, recording_peer() as peer:
        host = run.daemon.chat
        turn = await host.start(peer, start("s-nonce", "manual"))
        note = await approval_n(peer, turn.turn_id)
        decided = await host.approve(peer, decide({**note, "nonce": "0" * 32}, "approve"))
        assert decided.status == "denied"
        await end_of(peer, turn.turn_id)
    with AuditLog.open(audit_path(run.tp)) as reader:
        [rec] = [r for r in reader.list(limit=100) if r.tool == "skill"]
    assert (rec.decision, rec.reason) == ("denied", "binding_mismatch")


@pytest.mark.asyncio
async def test_stopping_a_turn_that_waits_for_a_decision_records_the_call_cancelled() -> None:
    fake = FakeLLM.from_steps(ScriptStep(tool_calls=[skill_call()]), ScriptStep(content="never"))
    async with running(chat_cfg(), fake) as run, recording_peer() as peer:
        host = run.daemon.chat
        turn = await host.start(peer, start("s-stop", "manual"))
        note = await approval_n(peer, turn.turn_id)
        await host.cancel(peer, chat_mod.TurnId(turn_id=turn.turn_id))
        await end_of(peer, turn.turn_id)
        assert ended(peer, turn.turn_id) == (
            "chat.error",
            {"turn_id": turn.turn_id, "reason": "cancelled"},
        )
        assert run.daemon.engine.approvals.pending() == []
        with pytest.raises(BridgeError):  # the card is dead: its turn ended
            await host.approve(peer, decide(note, "approve"))
    with AuditLog.open(audit_path(run.tp)) as reader:
        [rec] = [r for r in reader.list(limit=100) if r.tool == "skill"]
    assert (rec.decision, rec.reason) == ("denied", "cancelled")


@pytest.mark.asyncio
async def test_arguments_too_long_to_show_refuse_the_call_unseen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat_mod, "MAX_APPROVAL_ARGS_CHARS", 10)
    fake = FakeLLM.from_steps(ScriptStep(tool_calls=[skill_call()]), ScriptStep(content="Done."))
    async with running(chat_cfg(), fake) as run, recording_peer() as peer:
        host = run.daemon.chat
        turn = await host.start(peer, start("s-long", "manual"))
        await end_of(peer, turn.turn_id)
        assert approvals_of(peer, turn.turn_id) == []
    [tool_msg] = [m for m in fake.requests[1]["messages"] if m.get("role") == "tool"]
    assert '"code": "unavailable"' in tool_msg["content"]
    with AuditLog.open(audit_path(run.tp)) as reader:
        [rec] = [r for r in reader.list(limit=100) if r.tool == "skill"]
    assert (rec.decision, rec.reason) == ("error", "unavailable")


@pytest.mark.asyncio
async def test_chat_start_is_refused_like_chat_send() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="unused"))
    async with running(remote_cfg(), fake) as run, recording_peer() as peer:
        with pytest.raises(BridgeError) as exc:  # a cli peer shows no remote-model banner (#9)
            await run.daemon.chat.start(peer, start("s-remote", "manual"))
        assert exc.value.name == "unavailable"
    async with running(chat_cfg(), fake) as run, recording_peer() as peer:
        long = start("s-long", "bypass", "x" * (chat_mod.MAX_CHAT_TEXT + 1))
        with pytest.raises(BridgeError) as exc:
            await run.daemon.chat.start(peer, long)
        assert exc.value.name == "invalid_params"


@pytest.mark.asyncio
async def test_the_mode_flow_logs_ids_and_codes_never_args_or_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeLLM.from_steps(
        ScriptStep(tool_calls=[skill_call()]),
        ScriptStep(content="Done."),
    )
    async with running(chat_cfg(), fake) as run, recording_peer() as peer:
        host = run.daemon.chat
        turn = await host.start(peer, start("s-log", "manual", SENTINEL))
        note = await approval_n(peer, turn.turn_id)
        assert note["args"] == {"name": SKILL}  # the card itself shows the args
        await host.approve(peer, decide(note, "approve"))
        await end_of(peer, turn.turn_id)
    fmt = JsonFormatter()
    loud = "\n".join(fmt.format(r) for r in caplog.records if r.levelno >= logging.INFO)
    assert SENTINEL not in loud  # the user's text
    assert SKILL not in loud  # the tool's arguments
    assert "chat approval requested" in loud and "chat approval decided" in loud


def test_the_wait_is_bounded_by_the_approval_lifetime() -> None:
    assert DEADLINE_S < 600  # the tests decide long before the 10-minute expiry


# ---- over the socket, once 1.2 is spoken ------------------------------------------------------


async def note_of(c: ChatClient, method: str, turn_id: str) -> dict[str, Any]:
    await c.wait(lambda: any(m == method for m, _ in c.of(turn_id)), method)
    return next(p for m, p in c.of(turn_id) if m == method)


@pytest.mark.asyncio
async def test_a_1_2_client_runs_a_manual_turn_over_the_bridge() -> None:
    fake = FakeLLM.from_steps(ScriptStep(tool_calls=[skill_call()]), ScriptStep(content="Done."))
    async with running(chat_cfg(), fake) as run:
        c = await chat_client(run.tp.socket, "cli", minor=2)
        res = await c.peer.request(
            "chat.start", {"session_id": "s-wire", "text": "brief me", "mode": "manual"}, timeout=5
        )
        turn_id = str(res["turn_id"])
        note = await note_of(c, "chat.approval", turn_id)
        assert (note["tool"], note["tier"], note["args"]) == ("skill", "R0", {"name": SKILL})
        decided = await c.peer.request(
            "chat.approve",
            {
                "turn_id": turn_id,
                "approval_id": note["approval_id"],
                "nonce": note["nonce"],
                "decision": "approve",
            },
            timeout=5,
        )
        assert decided == {"status": "approved"}
        done = await note_of(c, "chat.done", turn_id)
        assert done == {"turn_id": turn_id, "citations": []}
        await c.close()
    assert [d for _, _, d in calls(run)] == ["approved"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.start", "chat.approve"])
async def test_a_1_1_client_cannot_reach_the_1_2_methods(method: str) -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="unused"))
    async with running(chat_cfg(), fake) as run:
        c = await chat_client(run.tp.socket, "cli", minor=1)
        with pytest.raises(BridgeError) as exc:
            await c.peer.request(method, {}, timeout=5)
        assert exc.value.name == "method_not_available"
        await c.close()
