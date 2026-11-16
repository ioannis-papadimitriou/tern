"""Plan 07 part B step 8 (plan 08b's prepared ``ui.open_mid``, #36/#37): a citation chip opens its
message as ``Registry.call("ui", "open_in_thunderbird", {mid})``, an audited R1 call; a ToolError
becomes a bridge error (``unknown_mid`` → ``invalid_params``, ``mid_conflicted`` →
``state_changed``, ``denied`` → ``role_rejected``, anything else → ``unavailable``) with the tern
code kept in the message. Full in-process ternd; the extension is FakeBridge over the synthetic
mailbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, cast

import pytest
import pytest_asyncio
import test_fixture_manifest as tfm

from tern import paths
from tern.bridge.client import Peer
from tern.bridge.protocol import METHODS, BridgeError, OpenMidParams, Request, dump
from tern.config import TernConfig
from tern.daemon.handlers import OPEN_ERRORS, ui_open_mid_handler
from tern.daemon.main import Daemon, serve
from tern.perms.audit import AuditLog, audit_path
from tern.testing.bridge import NMHOST, Client, FakeProcessInfo, connect, hello_params
from tern.testing.fake_bridge import FakeBridge
from tern.testing.procs import wait_for_socket
from tern.tools.contract import ToolError
from tern.tools.registry import Registry

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
BY_KEY = {m["key"]: m for m in MANIFEST["messages"]}
SINGLE: str = BY_KEY["inv-0042"]["mid"]
CONFLICTED: str = BY_KEY[MANIFEST["adversarial_pair"]["key"]]["mid"]
COPIES = MANIFEST["message_count"]
DEADLINE_S = 10.0


@dataclass
class Run:
    tp: paths.TernPaths
    daemon: Daemon
    fb: FakeBridge
    ext: Client


async def until(cond: Callable[[], bool], what: str) -> None:
    deadline = time.perf_counter() + DEADLINE_S
    while not cond():
        assert time.perf_counter() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


def fb_responder(fb: FakeBridge) -> Callable[[Peer, Request], Any]:
    async def respond(peer: Peer, req: Request) -> Any:
        if req.method == "bridge.ping":
            return {}
        spec = METHODS[req.method]
        return dump(await fb.request(req.method, spec.params.model_validate(req.params)))

    return respond


@contextlib.asynccontextmanager
async def running(minor: int) -> AsyncIterator[Run]:
    tp = paths.resolve()
    tp.run.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = asyncio.Event()
    started: list[Daemon] = []
    task = asyncio.create_task(
        serve(
            tp,
            TernConfig(embed_model=""),
            process_info=FakeProcessInfo(),
            nmhost_path=NMHOST,
            stop=stop,
            on_started=started.append,
        )
    )
    try:
        await until(lambda: bool(started) or task.done(), "on_started")
        if task.done():
            task.result()
        await asyncio.to_thread(wait_for_socket, tp.socket)
        fb = FakeBridge(FIXTURE)
        ext = await connect(
            tp.socket,
            "extension",
            serve=fb_responder(fb),
            hello=hello_params("extension", minor=minor, profile_id=str(uuid.uuid4())),
        )
        fb.on_event = ext.peer.notify
        d = started[0]
        conn = d.engine.index.connection
        await until(
            lambda: conn.execute("SELECT count(*) FROM locations").fetchone()[0] == COPIES,
            "the backfill",
        )
        try:
            yield Run(tp, d, fb, ext)
        finally:
            await ext.close()
    finally:
        stop.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE_S)


@pytest_asyncio.fixture
async def run() -> AsyncIterator[Run]:
    async with running(minor=1) as r:
        yield r


def rows(tp: paths.TernPaths) -> list[tuple[str, str, str]]:
    with AuditLog.open(audit_path(tp)) as reader:
        return [(r.tool, r.caller, r.decision) for r in reversed(reader.list(limit=1000))]


@pytest.mark.asyncio
async def test_a_chip_opens_its_message_as_an_audited_ui_call(run: Run) -> None:
    res = await run.ext.peer.request("ui.open_mid", {"mid": SINGLE}, timeout=10)
    folders = {loc.folder_id for loc in run.daemon.engine.index.locations(SINGLE)}
    assert res["opened"] is True and res["folder_id"] in folders
    assert set(res) == {"opened", "folder_id"}
    assert rows(run.tp) == [("open_in_thunderbird", "ui", "auto")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mid", "name", "code"),
    [
        ("b" * 64, "invalid_params", "unknown_mid"),
        (CONFLICTED, "state_changed", "mid_conflicted"),
    ],
)
async def test_a_refused_open_is_a_bridge_error_naming_the_tern_code(
    run: Run, mid: str, name: str, code: str
) -> None:
    with pytest.raises(BridgeError) as exc:
        await run.ext.peer.request("ui.open_mid", {"mid": mid}, timeout=10)
    assert exc.value.name == name
    assert f"[{code}" in exc.value.message
    assert [r[:2] for r in rows(run.tp)] == [("open_in_thunderbird", "ui")]


@pytest.mark.asyncio
async def test_a_malformed_mid_is_refused_by_the_bridge_without_a_row(run: Run) -> None:
    with pytest.raises(BridgeError) as exc:
        await run.ext.peer.request("ui.open_mid", {"mid": "not-a-mid"}, timeout=10)
    assert exc.value.name == "invalid_params"
    assert rows(run.tp) == []


@pytest.mark.asyncio
async def test_a_1_0_extension_cannot_open_by_mid() -> None:
    async with running(minor=0) as r:
        with pytest.raises(BridgeError) as exc:
            await r.ext.peer.request("ui.open_mid", {"mid": SINGLE}, timeout=10)
        assert exc.value.name == "method_not_available"
        assert rows(r.tp) == []


class _Refusing:
    """A registry stand-in whose ``open_in_thunderbird`` raises one ToolError."""

    def __init__(self, err: ToolError) -> None:
        self.err = err
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call(self, caller: str, tool: str, args: dict[str, Any]) -> Any:
        self.calls.append((caller, tool, args))
        raise self.err


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "reason", "name"),
    [
        ("unknown_mid", None, "invalid_params"),
        ("invalid_params", None, "invalid_params"),
        ("mid_conflicted", None, "state_changed"),
        ("unavailable", "stale_id", "unavailable"),
        ("denied", "tier_not_enabled", "role_rejected"),
        ("internal", "tb_error", "unavailable"),
        ("rate_limited", None, "unavailable"),
    ],
)
async def test_every_tool_error_maps_to_a_bridge_error(
    code: Any, reason: str | None, name: str
) -> None:
    reg = _Refusing(ToolError(code, "content-free message", reason=reason))
    handle = ui_open_mid_handler(cast(Registry, reg))
    with pytest.raises(BridgeError) as exc:
        await handle(cast(Peer, None), OpenMidParams(mid=SINGLE))
    assert exc.value.name == name
    detail = code if reason is None else f"{code}/{reason}"
    assert exc.value.message == f"content-free message [{detail}]"
    assert reg.calls == [("ui", "open_in_thunderbird", {"mid": SINGLE})]
    assert OPEN_ERRORS.get(code, "unavailable") == name
