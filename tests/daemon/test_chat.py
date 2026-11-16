"""Plan 08b step 1: ternd's chat glue (``tern.daemon.chat.ChatHost`` and its ``serve()`` wiring).

Every test runs ``serve()`` in-process on a real Unix socket, with the real ``LLMClient`` over
``FakeLLM`` (``llm_factory``), the real registry/engine/audit log and synthetic data only. Clients
are bridge peers (``cli`` unless the test needs the extension) that record the notifications they
receive; where frame order matters the test reads raw frames off the socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import logging
import re
import socket
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm
from pydantic import BaseModel

from tern import paths
from tern.agent.loop import AgentSession
from tern.bridge import framing
from tern.bridge.client import Peer
from tern.bridge.protocol import (
    METHODS,
    BridgeError,
    ChatSendParams,
    Empty,
    Hello,
    Notification,
    Request,
    Role,
    dump,
)
from tern.config import PluginsConfig, TernConfig
from tern.daemon import chat as chat_mod
from tern.daemon.main import Daemon, make_llm, serve
from tern.llm.client import LLMClient
from tern.llm.egress import EgressDenied
from tern.log import JsonFormatter
from tern.perms.audit import AuditLog, audit_path
from tern.testing.bridge import NMHOST, FakeProcessInfo, hello_params
from tern.testing.fake_bridge import FakeBridge
from tern.testing.fake_llm import FakeLLM, FakeToolCall, ScriptStep
from tern.testing.procs import wait_for_socket

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
COPIES = MANIFEST["message_count"]
SINGLE: str = next(m["mid"] for m in MANIFEST["messages"] if m["key"] == "inv-0042")
DEADLINE_S = 10.0
FAKE_MODEL = "fake-chat-1"


def chat_cfg(**kw: Any) -> TernConfig:
    return TernConfig(embed_model="", chat_model=FAKE_MODEL, **kw)


def key_of(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def paused(text: str = "Hello world") -> ScriptStep:
    """A content step that stops after its first content chunk ("Hello") until resumed."""
    return ScriptStep(content=text, split_at=[5], pause_after=2)


# ---- harness ---------------------------------------------------------------------------------


@dataclass
class Run:
    tp: paths.TernPaths
    task: asyncio.Task[None]
    stop: asyncio.Event
    started: list[Daemon] = field(default_factory=list[Daemon])

    @property
    def daemon(self) -> Daemon:
        assert len(self.started) == 1, "on_started was not called exactly once"
        return self.started[0]

    async def until(self, cond: Callable[[], bool], what: str) -> None:
        deadline = time.perf_counter() + DEADLINE_S
        while not cond():
            if self.task.done():
                self.task.result()
                raise AssertionError(f"serve() returned before {what}")
            assert time.perf_counter() < deadline, f"timed out waiting for {what}"
            await asyncio.sleep(0.01)

    async def finish(self) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, DEADLINE_S)


def llm_over(fake: FakeLLM) -> Callable[[TernConfig], LLMClient]:
    return lambda cfg: LLMClient(cfg, transport=fake.transport)


@contextlib.asynccontextmanager
async def running(cfg: TernConfig, fake: FakeLLM) -> AsyncIterator[Run]:
    tp = paths.resolve()
    tp.run.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = asyncio.Event()
    started: list[Daemon] = []
    task = asyncio.create_task(
        serve(
            tp,
            cfg,
            process_info=FakeProcessInfo(),
            nmhost_path=NMHOST,
            stop=stop,
            llm_factory=llm_over(fake),
            on_started=started.append,
        )
    )
    run = Run(tp, task, stop, started)
    try:
        await asyncio.to_thread(wait_for_socket, tp.socket)
        await run.until(lambda: bool(started), "on_started")
        yield run
    finally:
        fake.resume()
        stop.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE_S)


@dataclass
class ChatClient:
    peer: Peer
    task: asyncio.Task[None]
    notes: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    async def send(self, session_id: str, text: str) -> str:
        res = await self.peer.request(
            "chat.send", {"session_id": session_id, "text": text}, timeout=5
        )
        return str(res["turn_id"])

    async def cancel(self, turn_id: str) -> Any:
        return await self.peer.request("chat.cancel", {"turn_id": turn_id}, timeout=5)

    def of(self, turn_id: str) -> list[tuple[str, dict[str, Any]]]:
        return [(m, p) for m, p in self.notes if p.get("turn_id") == turn_id]

    def ended(self, turn_id: str) -> tuple[str, dict[str, Any]] | None:
        return next(((m, p) for m, p in self.of(turn_id) if m != "chat.delta"), None)

    async def wait(self, cond: Callable[[], bool], what: str) -> None:
        deadline = time.perf_counter() + DEADLINE_S
        while not cond():
            remaining = deadline - time.perf_counter()
            assert remaining > 0, f"timed out waiting for {what}"
            self.changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), min(remaining, 0.05))

    async def end_of(self, turn_id: str) -> tuple[list[str], tuple[str, dict[str, Any]]]:
        """The turn's deltas and its ending notification (``chat.done``/``chat.error``)."""
        await self.wait(lambda: self.ended(turn_id) is not None, f"the end of turn {turn_id}")
        end = self.ended(turn_id)
        assert end is not None
        deltas = [p["text"] for m, p in self.of(turn_id) if m == "chat.delta"]
        return deltas, end

    async def close(self) -> None:
        await self.peer.close()
        await self.task


async def chat_client(
    sock: Path,
    role: Role = "cli",
    *,
    serve_requests: Callable[[Peer, Request], Any] | None = None,
    minor: int = 0,
) -> ChatClient:
    reader, writer = await asyncio.open_unix_connection(str(sock))
    holder: list[ChatClient] = []

    async def on_note(peer: Peer, msg: Notification) -> None:
        c = holder[0]
        c.notes.append((msg.method, dict(msg.params)))
        c.changed.set()

    async def on_request(peer: Peer, req: Request) -> Any:
        if req.method == "bridge.ping":
            return {}
        if serve_requests is None:
            raise NotImplementedError(req.method)
        return await serve_requests(peer, req)

    peer = Peer(reader, writer, on_request=on_request, on_notification=on_note)
    task = asyncio.create_task(peer.run())
    client = ChatClient(peer, task)
    holder.append(client)
    try:
        await peer.request("hello", hello_params(role, minor=minor), timeout=5)
    except BaseException:
        await peer.close()
        await task
        raise
    return client


def fb_responder(fb: FakeBridge) -> Callable[[Peer, Request], Any]:
    async def respond(peer: Peer, req: Request) -> Any:
        spec = METHODS[req.method]
        return dump(await fb.request(req.method, spec.params.model_validate(req.params)))

    return respond


def audit_rows(tp: paths.TernPaths) -> list[tuple[str, str, str]]:
    """(tool, caller, decision) of every audit row, oldest first, read beside ternd."""
    with AuditLog.open(audit_path(tp)) as reader:
        return [(r.tool, r.caller, r.decision) for r in reversed(reader.list(limit=1000))]


class RecordingPeer(Peer):
    """A ``cli`` peer handed straight to ``ChatHost`` (no bridge server in between): it records
    what the host notifies and stays open unless the test closes it; with ``fail_delta`` every
    ``chat.delta`` raises ``BridgeError("unavailable")`` while ``closed`` stays unset."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, fail_delta: bool
    ) -> None:
        super().__init__(reader, writer, hello=Hello.model_validate(hello_params("cli")))
        self.fail_delta = fail_delta
        self.notes: list[tuple[str, dict[str, Any]]] = []
        self.changed = asyncio.Event()

    async def notify(self, method: str, params: BaseModel | dict[str, Any]) -> None:
        if self.fail_delta and method == "chat.delta":
            raise BridgeError("unavailable", "test peer refuses deltas")
        self.notes.append((method, dump(params) if isinstance(params, BaseModel) else params))
        self.changed.set()

    def of(self, turn_id: str) -> list[tuple[str, dict[str, Any]]]:
        return [(m, p) for m, p in self.notes if p.get("turn_id") == turn_id]

    def ended(self, turn_id: str) -> tuple[str, dict[str, Any]] | None:
        return next(((m, p) for m, p in self.of(turn_id) if m != "chat.delta"), None)


@contextlib.asynccontextmanager
async def recording_peer(*, fail_delta: bool = False) -> AsyncIterator[RecordingPeer]:
    """A :class:`RecordingPeer` over one end of a socket pair (nothing is written to it)."""
    ours, theirs = socket.socketpair()
    reader, writer = await asyncio.open_unix_connection(sock=ours)
    try:
        yield RecordingPeer(reader, writer, fail_delta=fail_delta)
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
        theirs.close()


async def wait_for(cond: Callable[[], bool], what: str) -> None:
    deadline = time.perf_counter() + DEADLINE_S
    while not cond():
        assert time.perf_counter() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


def located(run: Run) -> bool:
    conn = run.daemon.engine.index.connection
    return conn.execute("SELECT count(*) FROM locations").fetchone()[0] == COPIES


# ---- streaming, citations, audit caller ------------------------------------------------------


@pytest.mark.asyncio
async def test_extension_turn_streams_deltas_then_done_with_citations_as_agent_session() -> None:
    answer = f"The invoice is [{SINGLE}]."
    fake = FakeLLM.from_steps(
        ScriptStep(tool_calls=[FakeToolCall(name="get_message", arguments={"mid": SINGLE})]),
        ScriptStep(content=answer, split_at=[4, 16, 40]),
    )
    async with running(chat_cfg(), fake) as run:
        fb = FakeBridge(FIXTURE)
        ext = await chat_client(run.tp.socket, "extension", serve_requests=fb_responder(fb))
        fb.on_event = ext.peer.notify
        await run.until(lambda: located(run), "the backfill")
        sid = str(uuid.uuid4())
        turn_id = await ext.send(sid, "Which invoice?")
        deltas, (method, params) = await ext.end_of(turn_id)
        fake.assert_done()
        assert len(deltas) >= 2 and "".join(deltas) == answer
        assert method == "chat.done" and params == {"turn_id": turn_id, "citations": [SINGLE]}
        # Every notification of the turn came in order: deltas, then exactly one end.
        kinds = [m for m, _ in ext.of(turn_id)]
        assert kinds == ["chat.delta"] * len(deltas) + ["chat.done"]
        await ext.close()
        rows = [r for r in audit_rows(run.tp) if r[0] == "get_message"]
        assert rows == [("get_message", f"agent:{key_of(sid)}", "auto")]


@pytest.mark.asyncio
async def test_the_send_response_frame_precedes_the_first_delta_frame() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="Hi there.", split_at=[3]))
    async with running(chat_cfg(), fake) as run:
        reader, writer = await asyncio.open_unix_connection(str(run.tp.socket))
        try:
            hello = {"jsonrpc": "2.0", "id": 1, "method": "hello", "params": hello_params("cli")}
            writer.write(framing.frame(framing.encode(hello)))
            await writer.drain()
            first = await asyncio.wait_for(framing.read_frame(reader), DEADLINE_S)
            assert first is not None and "result" in framing.decode(first)
            send = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "chat.send",
                "params": {"session_id": "s-order", "text": "hello"},
            }
            writer.write(framing.frame(framing.encode(send)))
            await writer.drain()
            frames: list[dict[str, Any]] = []
            while not frames or frames[-1].get("method") not in ("chat.done", "chat.error"):
                raw = await asyncio.wait_for(framing.read_frame(reader), DEADLINE_S)
                assert raw is not None, "connection closed before the turn ended"
                frames.append(framing.decode(raw))
        finally:
            writer.close()
            await writer.wait_closed()
        assert frames[0].get("id") == 2 and "result" in frames[0]
        turn_id = frames[0]["result"]["turn_id"]
        assert [f.get("method") for f in frames[1:]] == ["chat.delta", "chat.delta", "chat.done"]
        assert all(f["params"]["turn_id"] == turn_id for f in frames[1:])


# ---- outcomes ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_tool_calls_reaches_chat_error_reason() -> None:
    calls = [FakeToolCall(name="list_folders"), FakeToolCall(name="list_folders")]
    fake = FakeLLM.from_steps(ScriptStep(tool_calls=calls))
    async with running(chat_cfg(agent_max_tool_calls=1), fake) as run:
        c = await chat_client(run.tp.socket)
        turn_id = await c.send("s-max", "list everything")
        deltas, end = await c.end_of(turn_id)
        fake.assert_done()
        assert deltas == [] and end == (
            "chat.error",
            {"turn_id": turn_id, "reason": "max_tool_calls"},
        )
        await c.close()


@pytest.mark.asyncio
async def test_llm_unavailable_reaches_chat_error_reason() -> None:
    fake = FakeLLM.from_steps(ScriptStep(http_status=503))
    async with running(chat_cfg(), fake) as run:
        c = await chat_client(run.tp.socket)
        turn_id = await c.send("s-503", "anyone there?")
        _, end = await c.end_of(turn_id)
        fake.assert_done()
        assert end == ("chat.error", {"turn_id": turn_id, "reason": "llm_unavailable"})
        await c.close()


@pytest.mark.asyncio
async def test_the_default_test_client_refuses_every_request() -> None:
    """The autouse ``no_chat_server`` fixture: without ``llm_factory`` a turn never reaches a
    real chat server; it ends ``llm_unavailable``."""
    tp = paths.resolve()
    tp.run.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(tp, chat_cfg(), process_info=FakeProcessInfo(), nmhost_path=NMHOST, stop=stop)
    )
    try:
        await asyncio.to_thread(wait_for_socket, tp.socket)
        c = await chat_client(tp.socket)
        turn_id = await c.send("s-default", "hello")
        _, end = await c.end_of(turn_id)
        assert end == ("chat.error", {"turn_id": turn_id, "reason": "llm_unavailable"})
        await c.close()
    finally:
        stop.set()
        await asyncio.wait_for(task, DEADLINE_S)


# ---- cancel -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_mid_stream_ends_cancelled_with_no_delta_after_the_reply() -> None:
    fake = FakeLLM.from_steps(paused())
    async with running(chat_cfg(), fake) as run:
        c = await chat_client(run.tp.socket)
        turn_id = await c.send("s-cancel", "talk")
        await c.wait(lambda: bool(c.of(turn_id)), "the first delta")
        assert await c.cancel(turn_id) == {}
        at_reply = len(c.of(turn_id))
        fake.resume()
        deltas, end = await c.end_of(turn_id)
        assert deltas == ["Hello"]
        assert end == ("chat.error", {"turn_id": turn_id, "reason": "cancelled"})
        assert [m for m, _ in c.of(turn_id)[at_reply:]] == ["chat.error"]
        # Cancelling an ended turn is a no-op; an unknown id is refused.
        assert await c.cancel(turn_id) == {}
        with pytest.raises(BridgeError) as unknown:
            await c.cancel("no-such-turn")
        assert unknown.value.name == "invalid_params"
        await c.close()


@pytest.mark.asyncio
async def test_another_peers_turn_cannot_be_cancelled() -> None:
    fake = FakeLLM.from_steps(paused())
    async with running(chat_cfg(), fake) as run:
        owner = await chat_client(run.tp.socket)
        other = await chat_client(run.tp.socket)
        turn_id = await owner.send("s-owner", "talk")
        await owner.wait(lambda: bool(owner.of(turn_id)), "the first delta")
        with pytest.raises(BridgeError) as refused:
            await other.cancel(turn_id)
        assert refused.value.name == "invalid_params"
        fake.resume()
        _, end = await owner.end_of(turn_id)
        assert end == ("chat.done", {"turn_id": turn_id, "citations": []})
        with pytest.raises(BridgeError) as ended:
            await other.cancel(turn_id)
        assert ended.value.name == "invalid_params"
        await owner.close()
        await other.close()


@pytest.mark.asyncio
async def test_peer_disconnecting_mid_turn_cancels_the_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tern.daemon.chat")
    fake = FakeLLM.from_steps(paused())
    async with running(chat_cfg(), fake) as run:
        c = await chat_client(run.tp.socket)
        turn_id = await c.send("s-gone", "talk")
        await c.wait(lambda: bool(c.of(turn_id)), "the first delta")
        await c.close()
        await run.until(lambda: run.daemon.chat.running == 0, "the turn to end")
        ends = [r for r in caplog.records if r.getMessage() == "chat turn ended"]
        assert [(r.__dict__["turn_id"], r.__dict__["outcome"]) for r in ends] == [
            (turn_id, "cancelled")
        ]
        # The session is free again: the same session id starts a new turn.
        again = await chat_client(run.tp.socket)
        assert await again.send("s-gone", "again") != turn_id
        await again.close()


# ---- refusals ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_send_while_a_turn_runs_is_invalid_params() -> None:
    fake = FakeLLM.from_steps(paused(), ScriptStep(content="Second."))
    async with running(chat_cfg(), fake) as run:
        c = await chat_client(run.tp.socket)
        first = await c.send("s-busy", "one")
        await c.wait(lambda: bool(c.of(first)), "the first delta")
        with pytest.raises(BridgeError) as busy:
            await c.send("s-busy", "two")
        assert busy.value.name == "invalid_params"
        fake.resume()
        await c.end_of(first)
        second = await c.send("s-busy", "two")
        _, end = await c.end_of(second)
        assert end == ("chat.done", {"turn_id": second, "citations": []})
        fake.assert_done()
        # The second request carried the first turn's history (one session).
        roles = [m["role"] for m in fake.requests[1]["messages"]]
        assert roles == ["system", "user", "assistant", "user"]
        await c.close()


@pytest.mark.asyncio
async def test_mcp_proxy_role_is_rejected_before_anything_runs() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="never"))
    async with running(chat_cfg(), fake) as run:
        c = await chat_client(run.tp.socket, "mcp-proxy")
        with pytest.raises(BridgeError) as send:
            await c.send("s-mcp", "hello")
        assert send.value.name == "role_rejected"
        with pytest.raises(BridgeError) as cancel:
            await c.cancel("whatever")
        assert cancel.value.name == "role_rejected"
        await c.close()
        assert fake.requests == [] and run.daemon.chat.sessions == 0
        assert audit_rows(run.tp) == []


@pytest.mark.asyncio
async def test_allow_remote_llm_refuses_chat_send() -> None:
    url = "https://llm.example.test/v1"
    cfg = chat_cfg(chat_base_url=url, allow_remote_llm=True, remote_llm_urls=[url])
    fake = FakeLLM.from_steps(ScriptStep(content="never"))
    async with running(cfg, fake) as run:
        c = await chat_client(run.tp.socket)
        with pytest.raises(BridgeError) as refused:
            await c.send("s-remote", "hello")
        assert refused.value.name == "unavailable"
        await c.close()
        assert fake.requests == [] and run.daemon.chat.sessions == 0


REMOTE_URL = "https://llm.example.test/v1"


def remote_cfg() -> TernConfig:
    return chat_cfg(chat_base_url=REMOTE_URL, allow_remote_llm=True, remote_llm_urls=[REMOTE_URL])


@pytest.mark.asyncio
async def test_allow_remote_llm_chat_runs_for_a_1_1_extension() -> None:
    """Plan 07 part B (#37): a 1.1 extension's tab reads daemon.info and shows #9's banner, so
    its chat.send is no longer refused under allow_remote_llm."""
    fake = FakeLLM.from_steps(ScriptStep(content="Hello there"))
    async with running(remote_cfg(), fake) as run:
        fb = FakeBridge(FIXTURE)
        ext = await chat_client(
            run.tp.socket, "extension", serve_requests=fb_responder(fb), minor=1
        )
        fb.on_event = ext.peer.notify
        info = await ext.peer.request("daemon.info", {}, timeout=5)
        assert info == {"remote_llm": True, "chat_model": FAKE_MODEL}
        turn_id = await ext.send("s-remote", "hello")
        deltas, (method, _) = await ext.end_of(turn_id)
        assert method == "chat.done" and "".join(deltas) == "Hello there"
        fake.assert_done()
        await ext.close()


@pytest.mark.asyncio
# ("extension", 1): a 1.1 extension that has not read daemon.info on this connection.
@pytest.mark.parametrize(("role", "minor"), [("extension", 0), ("cli", 1), ("extension", 1)])
async def test_allow_remote_llm_still_refuses_peers_without_the_banner(
    role: Role, minor: int
) -> None:
    """A 1.0 extension cannot learn remote_llm, a 1.1 one that did not read it shows no banner,
    and no `tern` command shows one (fail closed)."""
    fake = FakeLLM.from_steps(ScriptStep(content="never"))
    async with running(remote_cfg(), fake) as run:
        fb = FakeBridge(FIXTURE)
        c = await chat_client(run.tp.socket, role, serve_requests=fb_responder(fb), minor=minor)
        if role == "extension":
            fb.on_event = c.peer.notify
        with pytest.raises(BridgeError) as refused:
            await c.send("s-remote", "hello")
        assert refused.value.name == "unavailable"
        await c.close()
        assert fake.requests == [] and run.daemon.chat.sessions == 0


@pytest.mark.asyncio
async def test_allow_remote_llm_refuses_a_cli_peer_even_after_daemon_info() -> None:
    """A `cli` peer may read daemon.info, but no `tern` command shows #9's banner: reading it does
    not unlock remote-model chat (only an extension's tab shows the banner; review item 3)."""
    fake = FakeLLM.from_steps(ScriptStep(content="never"))
    async with running(remote_cfg(), fake) as run:
        c = await chat_client(run.tp.socket, "cli", minor=1)
        info = await c.peer.request("daemon.info", {}, timeout=5)
        assert info["remote_llm"] is True
        with pytest.raises(BridgeError) as refused:
            await c.send("s-remote", "hello")
        assert refused.value.name == "unavailable"
        await c.close()
        assert fake.requests == [] and run.daemon.chat.sessions == 0


def test_the_chat_text_limit_matches_the_extension() -> None:
    """ternd's ``MAX_CHAT_TEXT`` is the tab's (``state.ts``); manifest.test.ts ties the tab's
    constant to ``index.html``'s ``maxlength``, so the three copies cannot drift apart."""
    repo = Path(__file__).resolve().parents[2]
    state_ts = (repo / "extension/src/ui/chat/state.ts").read_text(encoding="utf-8")
    found = re.findall(r"^export const MAX_CHAT_TEXT = (\d+);$", state_ts, flags=re.MULTILINE)
    assert found == [str(chat_mod.MAX_CHAT_TEXT)]


@pytest.mark.asyncio
async def test_text_over_the_limit_is_invalid_params() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="ok"))
    async with running(chat_cfg(), fake) as run:
        c = await chat_client(run.tp.socket)
        with pytest.raises(BridgeError) as long:
            await c.send("s-long", "x" * (chat_mod.MAX_CHAT_TEXT + 1))
        assert long.value.name == "invalid_params"
        turn_id = await c.send("s-long", "x" * chat_mod.MAX_CHAT_TEXT)
        _, end = await c.end_of(turn_id)
        assert end[0] == "chat.done"
        await c.close()


@pytest.mark.asyncio
async def test_all_sessions_busy_is_unavailable_and_an_idle_one_is_evicted() -> None:
    n = chat_mod.MAX_SESSIONS
    fake = FakeLLM.from_steps(*[paused() for _ in range(n)], ScriptStep(content="Evicted one."))
    async with running(chat_cfg(), fake) as run:
        c = await chat_client(run.tp.socket)
        turns = [await c.send(f"s-{i}", "talk") for i in range(n)]
        with pytest.raises(BridgeError) as full:
            await c.send("s-extra", "talk")
        assert full.value.name == "unavailable"
        await c.cancel(turns[0])
        await c.end_of(turns[0])
        extra = await c.send("s-extra", "talk")
        assert run.daemon.chat.sessions == n
        fake.resume()
        _, end = await c.end_of(extra)
        assert end[0] == "chat.done"
        for t in turns[1:]:
            await c.end_of(t)
        await c.close()


# ---- egress, logs, shutdown ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_loopback_chat_url_stops_serve_before_the_socket_exists() -> None:
    tp = paths.resolve()
    tp.run.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg = chat_cfg(chat_base_url="http://llm.example.test/v1")
    with pytest.raises(EgressDenied):
        await asyncio.wait_for(
            serve(
                tp,
                cfg,
                process_info=FakeProcessInfo(),
                nmhost_path=NMHOST,
                stop=asyncio.Event(),
                llm_factory=make_llm,
            ),
            DEADLINE_S,
        )
    assert not tp.socket.exists()


@pytest.mark.asyncio
async def test_planted_sentinels_never_reach_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    user = "SENTINEL-USER-7f3a what about it?"
    model = "SENTINEL-MODEL-91c2 is the answer."
    sid = "SENTINEL-SESSION-5d1e"
    fake = FakeLLM.from_steps(
        ScriptStep(tool_calls=[FakeToolCall(name="list_folders")]),
        ScriptStep(content=model, split_at=[9]),
    )
    async with running(chat_cfg(), fake) as run:
        c = await chat_client(run.tp.socket)
        turn_id = await c.send(sid, user)
        deltas, end = await c.end_of(turn_id)
        assert "".join(deltas) == model and end[0] == "chat.done"
        with pytest.raises(BridgeError):
            await c.send(sid, "SENTINEL-LONG-0b0b " + "y" * chat_mod.MAX_CHAT_TEXT)
        await c.close()
    fmt = JsonFormatter()
    text = "\n".join(fmt.format(r) for r in caplog.records)
    assert turn_id in text  # the glue logged the turn
    assert key_of(sid) in text  # by session key
    for planted in ("SENTINEL-USER", "SENTINEL-MODEL", "SENTINEL-SESSION", "SENTINEL-LONG"):
        assert planted not in text


SLOW_PLUGIN = '''
import asyncio
from pathlib import Path

from tern.tools import BoundaryModel, Effect, Tier, ToolContext, tool

STARTED = Path(@STARTED@)


class In(BoundaryModel):
    text: str


class Out(BoundaryModel):
    n: int


@tool(name="slow_read", tier=Tier.R0, effects={Effect.READ_INDEX}, untrusted_output=False)
async def slow_read(ctx: ToolContext, inp: In) -> Out:
    """Wait until cancelled, then take a while to stop (a slow cleanup)."""
    STARTED.touch()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.sleep(0.3)
        raise
    return Out(n=0)


TOOLS = [slow_read]
'''


@pytest.mark.asyncio
async def test_shutdown_ends_a_running_turn_before_the_audit_log_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="tern.daemon.chat")
    started = tmp_path / "started"
    plugin = tmp_path / "slow_plugin.py"
    assert SLOW_PLUGIN.count("@STARTED@") == 1
    plugin.write_text(SLOW_PLUGIN.replace("@STARTED@", repr(str(started))), encoding="utf-8")
    cfg = chat_cfg(plugins=PluginsConfig(paths=[plugin]))
    fake = FakeLLM.from_steps(
        ScriptStep(tool_calls=[FakeToolCall(name="slow_read", arguments={"text": "x"})])
    )
    events: list[tuple[str, int]] = []
    real_close = AuditLog.close

    async with running(cfg, fake) as run:

        def close(self: AuditLog) -> None:
            if self is run.daemon.audit:
                events.append(("audit_close", run.daemon.chat.running))
            real_close(self)

        monkeypatch.setattr(AuditLog, "close", close)
        c = await chat_client(run.tp.socket)
        sid = "s-shutdown"
        await c.send(sid, "go slow")
        await run.until(started.exists, "the slow tool to start")
        await run.finish()
    ended = [r.__dict__["outcome"] for r in caplog.records if r.getMessage() == "chat turn ended"]
    assert ended == ["cancelled"]
    assert events == [("audit_close", 0)]
    # The cancelled call's row was written (the audit log was still open).
    with AuditLog.open(audit_path(run.tp)) as reader:
        [rec] = [r for r in reader.list(limit=100) if r.tool == "slow_read"]
        details = [e.detail for e in reader.events(rec.call_id) if e.kind == "error"]
    assert rec.caller == f"agent:{key_of(sid)}" and rec.status == "error"
    assert details == [{"code": "internal", "exc": "cancel", "reason": "cancelled"}]


# ---- review fixes (round 1): internal failures, shutdown, peer refusals, ended turns ---------


@pytest.mark.asyncio
async def test_a_raising_turn_task_ends_chat_error_internal_logged_by_type_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sentinel = "SENTINEL-EXC-4e7b"
    real_run_limited = AgentSession._run_limited
    raised: list[str] = []

    async def raise_once(self: AgentSession, handle: Any, st: Any) -> Any:
        if not raised:
            raised.append(sentinel)
            raise RuntimeError(sentinel)
        return await real_run_limited(self, handle, st)

    monkeypatch.setattr(AgentSession, "_run_limited", raise_once)
    fake = FakeLLM.from_steps(ScriptStep(content="Fine now."))
    async with running(chat_cfg(), fake) as run:
        c = await chat_client(run.tp.socket)
        turn_id = await c.send("s-internal", "hello")
        deltas, end = await c.end_of(turn_id)
        assert deltas == []
        assert end == ("chat.error", {"turn_id": turn_id, "reason": "internal"})
        assert run.daemon.chat.running == 0
        # The session was freed: the same session id starts a new turn, which completes.
        again = await c.send("s-internal", "again")
        _, end_again = await c.end_of(again)
        assert end_again == ("chat.done", {"turn_id": again, "citations": []})
        fake.assert_done()
        await c.close()
    assert raised == [sentinel]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [(r.getMessage(), r.__dict__["error"], r.__dict__["turn_id"]) for r in errors] == [
        ("chat turn failed", "RuntimeError", turn_id)
    ]
    ended = [
        (r.__dict__["turn_id"], r.__dict__["outcome"])
        for r in caplog.records
        if r.getMessage() == "chat turn ended"
    ]
    assert ended == [(turn_id, "internal"), (again, "ok")]
    fmt = JsonFormatter()
    loud = "\n".join(fmt.format(r) for r in caplog.records if r.levelno >= logging.INFO)
    assert sentinel not in loud


@pytest.mark.asyncio
async def test_shutdown_cancels_a_running_turn_whose_peer_stays_open() -> None:
    fake = FakeLLM.from_steps(paused())
    async with running(chat_cfg(), fake) as run, recording_peer() as peer:
        host = run.daemon.chat
        turn = await host.send(peer, ChatSendParams(session_id="s-open", text="talk"))
        await wait_for(lambda: bool(peer.of(turn.turn_id)), "the first delta")
        await asyncio.wait_for(host.shutdown(), DEADLINE_S)
        assert not peer.closed.is_set()
        assert peer.ended(turn.turn_id) == (
            "chat.error",
            {"turn_id": turn.turn_id, "reason": "cancelled"},
        )
        assert host.running == 0
        with pytest.raises(BridgeError) as refused:
            await host.send(peer, ChatSendParams(session_id="s-open", text="again"))
        assert refused.value.name == "unavailable"


@pytest.mark.asyncio
async def test_shutdown_awaits_every_pump_before_it_reraises_a_pump_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="unused"))
    async with running(chat_cfg(), fake) as run:
        host = run.daemon.chat
        finished: list[str] = []

        async def failing_pump() -> None:
            raise RuntimeError("pump bug")

        async def slow_pump() -> None:
            await asyncio.sleep(0.2)
            finished.append("slow")

        loop = asyncio.get_running_loop()
        pumps = [loop.create_task(failing_pump()), loop.create_task(slow_pump())]
        with monkeypatch.context() as m:
            m.setattr(host, "_pumps", pumps)  # a list: the failing pump is awaited first
            with pytest.raises(RuntimeError, match="pump bug"):
                await asyncio.wait_for(host.shutdown(), DEADLINE_S)
        assert finished == ["slow"]


@pytest.mark.asyncio
async def test_a_delta_the_peer_refuses_cancels_the_turn_while_closed_is_unset() -> None:
    fake = FakeLLM.from_steps(paused())
    async with running(chat_cfg(), fake) as run, recording_peer(fail_delta=True) as peer:
        host = run.daemon.chat
        turn = await host.send(peer, ChatSendParams(session_id="s-refuse", text="talk"))
        await wait_for(lambda: peer.ended(turn.turn_id) is not None, "the end of the turn")
        assert peer.notes == [("chat.error", {"turn_id": turn.turn_id, "reason": "cancelled"})]
        assert not peer.closed.is_set() and host.running == 0


@pytest.mark.asyncio
async def test_an_ended_turn_does_not_keep_its_peer_alive() -> None:
    fake = FakeLLM.from_steps(ScriptStep(content="Done."))
    async with running(chat_cfg(), fake) as run:
        host = run.daemon.chat

        async def one_turn() -> weakref.ref[RecordingPeer]:
            async with recording_peer() as peer:
                turn = await host.send(peer, ChatSendParams(session_id="s-weak", text="hi"))
                await wait_for(lambda: peer.ended(turn.turn_id) is not None, "the turn's end")
                # Cancelling the ended turn is still an idempotent no-op for its owner.
                assert await host.cancel(peer, turn) == Empty()
                return weakref.ref(peer)

        ref = await one_turn()
        await wait_for(lambda: not host._pumps, "the pump to finish")
        await asyncio.sleep(0.05)  # the cancelled closed-watch task runs its callback
        gc.collect()
        assert ref() is None, "ChatHost keeps a closed peer alive"
