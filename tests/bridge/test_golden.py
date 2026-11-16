"""Golden `tern-bridge/1.0` transcripts (plan 02 step 9), replayed against the real Python side.

Each ``tests/golden/bridge/*.jsonl`` file is one connection. Line 1 is a header
``{transcript, client, description, peer_check?, tb?}``; every other line is either
``{"from": "client" | "tern", "msg": <JSON-RPC message>}`` or ``{"event": ...}``.

- ``client: "extension"``: the client is the extension; "tern" is ``tern-nmhost`` relaying to
  ``ternd``. Replayed through a real ``Host`` and ``BridgeServer`` (here) and against
  ``Bridge`` in ``extension/test/bridge.test.ts``.
- ``client: "socket"``: a raw client on the daemon socket (cli, or a malformed hello).

Replay feeds the client lines verbatim and checks that tern sends exactly the "tern" lines, in
order, then nothing else. Daemon→extension requests are started through ``ExtensionLink`` (the
API daemon code uses). Methods whose handlers belong to later plans (``approvals.pending``) are
stubbed with the result the golden expects, so the transcript checks their wire form only.
Chunk ids are ``h<pid>-<n>``; the golden writes the pid as 0.

Events: ``daemon_down`` (listener and connections closed), ``daemon_up``, ``closed`` (tern
closed the connection).

``tb`` (``mail_read``): the synthetic profile the extension's mail handlers read, as TB's APIs
show it: the fixture accounts it lists, every message's header data, and the parts the
transcript fetches. vitest builds a fake ``messenger`` from it; here the same requests are
replayed against ``FakeBridge`` over those accounts of the fixture, and the block is checked
against the fixture. Cursors are compared by role (tokens are per implementation).
"""

from __future__ import annotations

import asyncio
import base64
import email
import json
import logging
import os
import re
import shutil
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel, ValidationError

import tern
from tern import paths
from tern.bridge import framing
from tern.bridge.client import ExtensionLink, Peer
from tern.bridge.nmhost import Host
from tern.bridge.protocol import (
    ERROR_CODES,
    METHODS,
    BridgeError,
    dump,
    parse_message,
)
from tern.bridge.server import BridgeServer
from tern.daemon import uds
from tern.testing.bridge import NMHOST, TB_BIN, FakeProcessInfo
from tern.testing.fake_bridge import DEFAULT_TAGS, FakeBridge

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden" / "bridge"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_mailbox"
DAEMON_VERSION = "0.0.0-golden"  # tern.__version__ during replay, so a release bump is no diff
REQUIRED = {
    "hello_extension",
    "hello_cli",
    "ping",
    "errors_cli",
    "error_hello_invalid",
    "error_role_rejected",
    "chunk",
    "reconnected",
    "mail_read",
}
STEP_TIMEOUT_S = 3.0
QUIET_S = 0.2  # after the last line, tern must stay silent this long
SENTINEL = "SENTINEL-golden-input"  # client input that must never be echoed in an error


@dataclass
class Transcript:
    name: str
    client: str
    peer_check: str
    steps: list[dict[str, Any]]
    tb: dict[str, Any] | None = None


def _load(path: Path) -> Transcript:
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    header, steps = lines[0], lines[1:]
    assert set(header) <= {"transcript", "client", "description", "peer_check", "tb"}, header
    assert header["transcript"] == path.stem
    assert header["client"] in ("extension", "socket")
    for s in steps:
        assert set(s) in ({"from", "msg"}, {"event"}), s
        if "from" in s:
            assert s["from"] in ("client", "tern")
        else:
            assert s["event"] in ("daemon_down", "daemon_up", "closed")
    return Transcript(
        path.stem, header["client"], header.get("peer_check", "pass"), steps, header.get("tb")
    )


TRANSCRIPTS = sorted(GOLDEN_DIR.glob("*.jsonl"))


def _strict_eq(a: Any, b: Any) -> bool:
    """JSON equality that does not let ``True == 1`` or ``1 == 1.0`` through."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_strict_eq(a[k], b[k]) for k in a)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]  # JSON values
    if isinstance(a, list):
        return len(a) == len(b) and all(_strict_eq(x, y) for x, y in zip(a, b, strict=True))  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]  # JSON values
    return a == b


def _logical(steps: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """(from, message) with each bridge.chunk run decoded back into the message it carries.

    Decoded with ``base64`` directly, not with ``framing.Reassembler`` (the code under test).
    """
    out: list[tuple[str, dict[str, Any]]] = []
    buf: list[bytes] = []
    for s in steps:
        if "msg" not in s:
            continue
        msg = s["msg"]
        if msg.get("method") == "bridge.chunk":
            buf.append(base64.b64decode(msg["params"]["data_b64"], validate=True))
            if msg["params"]["last"]:
                out.append((s["from"], json.loads(b"".join(buf))))
                buf = []
            continue
        out.append((s["from"], msg))
    assert not buf, "chunk run without a last chunk"
    return out


def _replies(t: Transcript) -> list[tuple[str, dict[str, Any], dict[str, Any] | None]]:
    """(sender, request, reply-or-None) for every request in the transcript."""
    msgs = _logical(t.steps)
    out: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
    for i, (sender, msg) in enumerate(msgs):
        if "method" not in msg or "id" not in msg:
            continue
        reply = next(
            (
                m
                for s, m in msgs[i + 1 :]
                if s != sender and "method" not in m and m.get("id") == msg["id"]
            ),
            None,
        )
        out.append((sender, msg, reply))
    return out


# ---------------------------------------------------------------------------------------------
# Static checks: the transcripts conform to the 1.0 models (the frozen contract's source)


def test_the_required_transcripts_exist() -> None:
    assert {p.stem for p in TRANSCRIPTS} >= REQUIRED


@pytest.mark.parametrize("path", TRANSCRIPTS, ids=lambda p: p.stem)
def test_messages_conform_to_the_1_0_models(path: Path) -> None:
    t = _load(path)
    for s in t.steps:
        if "msg" in s:
            parse_message(s["msg"])  # raises if not a JSON-RPC message
            if s["from"] == "tern" and s["msg"].get("method") == "bridge.chunk":
                frame = framing.encode(s["msg"])
                assert len(frame) < framing.HOST_TO_EXT_MAX
    for sender, msg, reply in _replies(t):
        spec = METHODS.get(msg["method"])
        failed_as = None if reply is None or "error" not in reply else reply["error"]
        if failed_as is not None:
            name = failed_as["data"]["name"]
            assert failed_as["code"] == ERROR_CODES[name], failed_as
            if name in ("method_not_available", "invalid_params"):
                continue  # deliberately unknown or malformed requests
        assert spec is not None and spec.minor == 0, msg["method"]
        assert spec.kind == "request"
        spec.params.model_validate(msg["params"])
        if reply is not None and "result" in reply:
            assert spec.result is not None
            spec.result.model_validate(reply["result"])
        if sender == "tern":
            assert spec.direction in ("d2e", "any")
    for sender, msg in _logical(t.steps):
        if "method" in msg and "id" not in msg:
            spec = METHODS[msg["method"]]
            assert spec.kind == "notification"
            spec.params.model_validate(msg["params"])
        if sender == "tern":
            assert SENTINEL not in json.dumps(msg)


def test_conformance_check_rejects_a_wrong_result() -> None:
    """The check above can fail: a hello result missing ``capabilities`` is not a Hello."""
    spec = METHODS["hello"]
    assert spec.result is not None
    with pytest.raises(ValidationError):
        spec.result.model_validate({"protocol": "tern-bridge/1", "minor": 0, "role": "daemon"})


# ---------------------------------------------------------------------------------------------
# Replay


class _Connected(logging.Handler):
    """Signals each time ``tern-nmhost`` reports a (re)connection to ``ternd``."""

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "connected to ternd":
            self.count += 1


@dataclass
class Daemon:
    server: BridgeServer
    sock: Path
    srv: asyncio.Server | None = None

    async def up(self) -> None:
        async def on_connect(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await self.server.handle_connection(r, w, os.getpid())  # FakeProcessInfo ignores it

        self.srv = await asyncio.start_unix_server(on_connect, path=str(self.sock))

    async def down(self) -> None:
        assert self.srv is not None
        self.srv.close()
        await self.server.close_all()
        await self.srv.wait_closed()
        self.srv = None


@dataclass
class Rig:
    daemon: Daemon
    out: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue[bytes]())
    to_host: asyncio.StreamReader = field(default_factory=asyncio.StreamReader)
    spawned: asyncio.Event = field(default_factory=asyncio.Event)
    connected: _Connected = field(default_factory=_Connected)


def _server(t: Transcript) -> BridgeServer:
    info = FakeProcessInfo()
    if t.peer_check == "parent_not_thunderbird":
        info = FakeProcessInfo(parent_exe=Path("/usr/bin/some-other-program"))
    else:
        assert t.peer_check == "pass", t.peer_check
    server = BridgeServer(
        process_info=info,
        thunderbird_bin=TB_BIN,
        nmhost_path=NMHOST,
        link=ExtensionLink(wait_s=2.0),
        # The transcripts are frozen 1.0 conversations: replayed against a daemon speaking 1.0,
        # as DAEMON_VERSION pins the version (a 1.1 daemon's hello says minor 1; that path is
        # tests/bridge/test_minor_1.py's).
        minor=0,
    )
    _stub_later_plan_handlers(server, t)
    return server


def _stub_later_plan_handlers(server: BridgeServer, t: Transcript) -> None:
    """Methods with no handler yet answer with the golden's results, in order."""
    results: dict[str, deque[BaseModel]] = defaultdict(deque)
    for sender, msg, reply in _replies(t):
        method = msg["method"]
        if sender != "client" or method in server.handlers or method == "hello":
            continue
        if reply is None or "result" not in reply:
            continue
        spec = METHODS[method]
        assert spec.result is not None
        results[method].append(spec.result.model_validate(reply["result"]))

    def make(queue: deque[BaseModel]) -> Callable[[Peer, Any], Awaitable[BaseModel]]:
        async def handler(peer: Peer, params: Any) -> BaseModel:
            return queue.popleft()

        return handler

    for method, queue in results.items():
        server.handlers[method] = make(queue)


@pytest.fixture(autouse=True)
def _golden_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tern, "__version__", DAEMON_VERSION)


@pytest_asyncio.fixture
async def runtime() -> AsyncIterator[paths.TernPaths]:
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    yield tp


def _normalize(msg: dict[str, Any]) -> dict[str, Any]:
    if msg.get("method") == "bridge.chunk":
        cid = msg["params"]["chunk_id"]
        prefix = f"h{os.getpid()}-"
        assert cid.startswith(prefix), cid
        msg["params"]["chunk_id"] = "h0-" + cid[len(prefix) :]
    return msg


async def _assert_quiet(get: Callable[[], Awaitable[Any]]) -> None:
    with pytest.raises(TimeoutError):
        extra = await asyncio.wait_for(get(), QUIET_S)
        pytest.fail(f"unexpected extra message: {extra!r}")


async def _wait_for(cond: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not cond():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), STEP_TIMEOUT_S)


async def _replay_extension(t: Transcript, tp: paths.TernPaths) -> None:
    daemon = Daemon(_server(t), tp.socket)
    rig = Rig(daemon)

    async def ext_out(payload: bytes) -> None:
        await rig.out.put(payload)

    async def recv() -> dict[str, Any]:
        return _normalize(json.loads(await rig.out.get()))

    nmhost_log = logging.getLogger("tern.nmhost")
    old_level = nmhost_log.level
    nmhost_log.setLevel(logging.INFO)  # "connected to ternd" is an INFO record
    nmhost_log.addHandler(rig.connected)
    await daemon.up()
    host = Host(
        rig.to_host, ext_out, tp.socket, rig.spawned.set, backoff_start_s=0.02, backoff_max_s=0.05
    )
    host_task = asyncio.create_task(host.run())
    daemon_requests: dict[Any, asyncio.Task[Any]] = {}
    try:
        await _wait_for(lambda: rig.connected.count == 1)
        for i, s in enumerate(t.steps, start=2):
            where = f"{t.name}.jsonl line {i}"
            if s.get("event") == "daemon_down":
                rig.spawned.clear()
                await daemon.down()
                # The host calls spawn only after it saw the loss and a connect attempt failed.
                await asyncio.wait_for(rig.spawned.wait(), STEP_TIMEOUT_S)
                continue
            if s.get("event") == "daemon_up":
                await daemon.up()
                continue
            assert "event" not in s, f"{where}: event {s['event']} is not for extension clients"
            msg = s["msg"]
            if s["from"] == "client":
                rig.to_host.feed_data(framing.frame(framing.encode(msg)))
                if "method" not in msg and msg["id"] in daemon_requests:
                    task = daemon_requests.pop(msg["id"])
                    if "error" in msg:
                        with pytest.raises(BridgeError) as failed:
                            await asyncio.wait_for(task, STEP_TIMEOUT_S)
                        assert _strict_eq(dump(failed.value.to_rpc()), msg["error"]), where
                    else:
                        got = await asyncio.wait_for(task, STEP_TIMEOUT_S)
                        assert _strict_eq(dump(got), msg["result"]), where
                continue
            if "method" in msg and "id" in msg:  # a daemon→extension request: start it
                spec = METHODS[msg["method"]]
                daemon_requests[msg["id"]] = asyncio.create_task(
                    daemon.server.link.request(
                        msg["method"], spec.params.model_validate(msg["params"])
                    )
                )
            got = await asyncio.wait_for(recv(), STEP_TIMEOUT_S)
            assert _strict_eq(got, msg), f"{where}: tern sent {got!r}"
        await _assert_quiet(recv)
        assert not daemon_requests
        assert not host_task.done()
    finally:
        nmhost_log.removeHandler(rig.connected)
        nmhost_log.setLevel(old_level)
        rig.to_host.feed_eof()
        await asyncio.wait_for(host_task, STEP_TIMEOUT_S)
        if daemon.srv is not None:
            await daemon.down()


async def _replay_socket(t: Transcript, tp: paths.TernPaths) -> None:
    daemon = Daemon(_server(t), tp.socket)
    await daemon.up()
    reader, writer = await asyncio.open_unix_connection(str(tp.socket))

    async def recv() -> dict[str, Any]:
        payload = await framing.read_frame(reader)
        assert payload is not None, "tern closed the connection"
        return json.loads(payload)

    try:
        for i, s in enumerate(t.steps, start=2):
            where = f"{t.name}.jsonl line {i}"
            if s.get("event") == "closed":
                assert await asyncio.wait_for(framing.read_frame(reader), STEP_TIMEOUT_S) is None
                return
            assert "event" not in s, f"{where}: event {s['event']} is not for socket clients"
            if s["from"] == "client":
                writer.write(framing.frame(framing.encode(s["msg"])))
                await writer.drain()
                continue
            got = await asyncio.wait_for(recv(), STEP_TIMEOUT_S)
            assert _strict_eq(got, s["msg"]), f"{where}: tern sent {got!r}"
        await _assert_quiet(recv)
    finally:
        writer.close()
        await daemon.down()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", TRANSCRIPTS, ids=lambda p: p.stem)
async def test_replay(path: Path, runtime: paths.TernPaths) -> None:
    t = _load(path)
    if t.client == "extension":
        await _replay_extension(t, runtime)
    else:
        await _replay_socket(t, runtime)


@pytest.mark.asyncio
async def test_replay_fails_on_a_changed_reply(runtime: paths.TernPaths) -> None:
    """The replay can fail: a golden expecting a different ping result is caught."""
    t = _load(GOLDEN_DIR / "hello_cli.jsonl")
    t.steps[-1] = {"from": "tern", "msg": {"jsonrpc": "2.0", "id": "p-3", "result": {"x": 1}}}
    with pytest.raises(AssertionError, match="line 7"):
        await _replay_socket(t, runtime)


@pytest.mark.asyncio
async def test_extension_replay_fails_without_bridge_reconnected(
    runtime: paths.TernPaths,
) -> None:
    """The host replay can fail: a golden that omits bridge.reconnected is caught."""
    t = _load(GOLDEN_DIR / "reconnected.jsonl")
    t.steps = [s for s in t.steps if s.get("msg", {}).get("method") != "bridge.reconnected"]
    with pytest.raises(AssertionError, match=r"reconnected\.jsonl line \d+: tern sent"):
        await _replay_extension(t, runtime)


def test_strict_eq_distinguishes_json_types() -> None:
    assert _strict_eq({"a": [1, "x"]}, {"a": [1, "x"]})
    assert not _strict_eq({"a": True}, {"a": 1})
    assert not _strict_eq({"a": 1}, {"a": 1.0})
    assert not _strict_eq({"a": 1}, {"a": 1, "b": 2})


def test_logical_decodes_chunks_independently() -> None:
    t = _load(GOLDEN_DIR / "chunk.jsonl")
    msgs = _logical(t.steps)
    sender, reply = msgs[-1]
    assert sender == "tern" and reply["id"] == 2 and isinstance(reply["result"], list)
    assert sum(1 for s in t.steps if s.get("msg", {}).get("method") == "bridge.chunk") == 3


# ---------------------------------------------------------------------------------------------
# mail_read: the same requests against FakeBridge (plan 03 step 6). vitest replays them against
# the extension's handlers over the header's `tb` block; both must give the golden's replies.


def _fixture_subset(tb: dict[str, Any], dst: Path) -> Path:
    """A mailbox dir holding only the fixture accounts the ``tb`` block lists."""
    keys = [a["id"] for a in tb["accounts"]]
    manifest = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
    accounts = [a for a in manifest["accounts"] if a["key"] in keys]
    assert [a["key"] for a in accounts] == keys
    (dst / "MANIFEST.json").write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
    for a in accounts:
        for f in a["folders"]:
            src, out = FIXTURE / f["file"], dst / f["file"]
            out.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, out)
            else:
                shutil.copyfile(src, out)
    return dst


def _aligned(got: Any, want: Any, cursors: dict[str, str]) -> Any:
    """``got`` with each ``next_cursor`` replaced by the golden's (recorded golden → actual)."""
    if isinstance(got, dict) and isinstance(want, dict):
        out: dict[str, Any] = {}
        for k, v in got.items():  # pyright: ignore[reportUnknownVariableType]  # JSON values
            w = want.get(k)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]  # JSON values
            if k == "next_cursor" and isinstance(v, str) and isinstance(w, str):
                cursors[w] = v
                out[k] = w
            else:
                out[k] = _aligned(v, w, cursors)
        return out
    if isinstance(got, list) and isinstance(want, list) and len(got) == len(want):  # pyright: ignore[reportUnknownArgumentType]  # JSON values
        return [_aligned(x, y, cursors) for x, y in zip(got, want, strict=True)]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]  # JSON values
    return got


async def _replay_fake_bridge(t: Transcript, fb: FakeBridge) -> None:
    cursors: dict[str, str] = {}
    for sender, msg, reply in _replies(t):
        if sender != "tern" or not msg["method"].startswith("mail."):
            continue
        where = f"{t.name}: request {msg['id']} ({msg['method']})"
        assert reply is not None, where
        params = dict(msg["params"])
        if params.get("cursor") in cursors:
            params["cursor"] = cursors[params["cursor"]]
        spec = METHODS[msg["method"]]
        try:
            result = await fb.request(msg["method"], spec.params.model_validate(params))
        except BridgeError as e:
            assert "error" in reply, f"{where}: failed with {e.name}"
            assert _strict_eq(dump(e.to_rpc()), reply["error"]), where
            continue
        assert "result" in reply, f"{where}: succeeded, the golden says it fails"
        got = _aligned(dump(result), reply["result"], cursors)
        assert _strict_eq(got, reply["result"]), f"{where}: FakeBridge gave {got!r}"


def _mail_read_bridge(t: Transcript, tmp_path: Path) -> FakeBridge:
    assert t.tb is not None
    return FakeBridge(_fixture_subset(t.tb, tmp_path), page_size=t.tb["page_size"])


@pytest.mark.asyncio
async def test_mail_read_replays_against_fake_bridge(tmp_path: Path) -> None:
    t = _load(GOLDEN_DIR / "mail_read.jsonl")
    fb = _mail_read_bridge(t, tmp_path)
    await _replay_fake_bridge(t, fb)
    assert [m for m, _ in fb.calls] == [
        msg["method"] for s, msg, _ in _replies(t) if s == "tern" and msg["method"] != "bridge.ping"
    ]


@pytest.mark.asyncio
async def test_mail_read_replay_fails_on_a_changed_reply(tmp_path: Path) -> None:
    """The FakeBridge replay can fail: a golden first page with one item fewer is caught."""
    t = _load(GOLDEN_DIR / "mail_read.jsonl")
    for s in t.steps:
        result = s.get("msg", {}).get("result")
        if isinstance(result, dict) and "items" in result and result.get("next_cursor"):
            result["items"].pop()  # pyright: ignore[reportUnknownMemberType]  # JSON values
            break
    else:
        pytest.fail("mail_read has no first page with a cursor")
    with pytest.raises(AssertionError, match=r"request 3 \(mail\.messages\.page\)"):
        await _replay_fake_bridge(t, _mail_read_bridge(t, tmp_path))


async def _call(fb: FakeBridge, method: str, params: dict[str, Any]) -> Any:
    return dump(await fb.request(method, METHODS[method].params.model_validate(params)))


@pytest.mark.asyncio
async def test_mail_read_tb_block_is_the_fixture(tmp_path: Path) -> None:
    """The profile vitest serves is the fixture as FakeBridge serves it, message for message."""
    t = _load(GOLDEN_DIR / "mail_read.jsonl")
    tb = t.tb
    assert tb is not None
    fb = _mail_read_bridge(t, tmp_path)
    accounts = await _call(fb, "mail.accounts.list", {})
    assert [
        {
            "id": a["id"],
            "name": a["name"],
            "type": a["type"],
            "identities": [{**i, "is_default": n == 0} for n, i in enumerate(a["identities"])],
        }
        for a in tb["accounts"]
    ] == [
        {
            "id": a["account_id"],
            "name": a["name"],
            "type": a["type"],
            "identities": [
                {
                    "id": i["identity_id"],
                    "email": i["email"],
                    "name": i["name"],
                    "is_default": i["is_default"],
                }
                for i in a["identities"]
            ],
        }
        for a in accounts
    ]
    want: list[dict[str, Any]] = []
    for a in tb["accounts"]:
        folders = await _call(fb, "mail.folders.list", {"account_id": a["id"]})
        assert [
            (f["id"], f["name"], f["path"], f["specialUse"]) for f in a["rootFolder"]["subFolders"]
        ] == [(f["folder_id"], f["name"], f["path"], f["special_use"]) for f in folders]
        for f in folders:
            page = await _call(fb, "mail.messages.page", {"folder_id": f["folder_id"]})
            while True:
                want += [
                    {
                        "id": i["tb_id"],
                        "folderId": f["folder_id"],
                        "headerMessageId": i["header_message_id"],
                        "instant": datetime.fromisoformat(i["date"]).timestamp(),
                        "size": i["size"],
                        "flags": i["flags"],
                        "tags": i["tags"],
                        "headersOnly": i["headers_only"],
                    }
                    for i in page["items"]
                ]
                if "next_cursor" not in page:
                    break
                page = await _call(
                    fb,
                    "mail.messages.page",
                    {"folder_id": f["folder_id"], "cursor": page["next_cursor"]},
                )
    got = [
        {
            "id": m["id"],
            "folderId": m["folderId"],
            "headerMessageId": m["headerMessageId"],
            "instant": datetime.fromisoformat(m["date"]).timestamp(),
            "size": m["size"],
            "flags": {"read": m["read"], "flagged": m["flagged"], "junk": m["junk"]},
            "tags": m["tags"],
            "headersOnly": m["headersOnly"],
        }
        for m in tb["messages"]
    ]
    assert got == want
    for m in tb["messages"]:
        if "raw_b64" in m:
            raw = await _call(fb, "mail.messages.raw", {"tb_id": m["id"]})
            assert m["raw_b64"] == raw["raw_b64"]
        if "headers" in m:
            full = await _call(fb, "mail.messages.full", {"tb_id": m["id"]})
            assert m["headers"] == full["headers"]
            assert [{"mime": p["contentType"], "text": p["content"]} for p in m["textParts"]] == (
                full["text_parts"]
            )
            assert [
                {"name": x["name"], "mime": x["contentType"], "size": x["size"]}
                for x in m["attachments"]
            ] == full["attachments"]
    assert tb["tags"] == [dump(x) for x in DEFAULT_TAGS]


# ---------------------------------------------------------------------------------------------
# Privacy: addresses inside chunked payloads (plan 02 review, deferred to plan 03).
# check_diff's address scan reads the golden lines as text, so it cannot see into base64
# `bridge.chunk` data or base64 `raw_b64` messages. This decodes every chunk run and scans the
# messages it carries, the header's `tb` block, and every `raw_b64` (anywhere in either) as the
# raw message, its decoded headers and each part's decoded body.
# The two regexes mirror scripts/check_diff.py (EMAIL_RE, RESERVED_DOMAIN_RE).

_EMAIL_RE = re.compile(r"[\w.+-]+@((?:[\w-]+\.)+[a-zA-Z]{2,})")
_RESERVED_RE = re.compile(
    r"(^|\.)(example\.(com|org|net)|test|example|invalid|localhost)$", re.IGNORECASE
)


def _raw_texts(raw_b64: str) -> list[str]:
    """A ``raw_b64`` message as text: its bytes, decoded headers and decoded leaf bodies."""
    raw = base64.b64decode(raw_b64, validate=True)
    # errors="replace" only turns undecodable bytes into U+FFFD; an address is ASCII either way,
    # as long as each body is decoded with its own charset (UTF-16 interleaves NULs otherwise).
    texts = [raw.decode("utf-8", errors="replace")]
    msg = email.message_from_bytes(raw, policy=policy.default)
    for part in msg.walk():
        texts += [str(v) for v in part.values()]  # RFC 2047 encoded words, decoded
        if not part.is_multipart():
            body = part.get_payload(decode=True)  # base64 / quoted-printable, decoded
            assert isinstance(body, bytes), "a leaf part without a decodable payload"
            charset = part.get_content_charset() or "utf-8"
            texts.append(body.decode(charset, errors="replace"))  # unknown charset: LookupError
    return texts


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        out: list[str] = []
        for k, v in obj.items():  # pyright: ignore[reportUnknownVariableType]  # JSON values
            out += [*_strings(k), *_strings(v)]
            if k == "raw_b64" and isinstance(v, str):
                out += _raw_texts(v)
        return out
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]  # pyright: ignore[reportUnknownVariableType]  # JSON values
    return []


def _unreserved_addresses(
    steps: list[dict[str, Any]], tb: dict[str, Any] | None = None
) -> list[str]:
    found: list[str] = []
    scanned: list[Any] = [msg for _, msg in _logical(steps)]
    if tb is not None:
        scanned.append(tb)
    for obj in scanned:
        for text in _strings(obj):
            found += [
                m.group(0) for m in _EMAIL_RE.finditer(text) if not _RESERVED_RE.search(m.group(1))
            ]
    return found


@pytest.mark.parametrize("path", TRANSCRIPTS, ids=lambda p: p.stem)
def test_no_unreserved_address_even_inside_chunks(path: Path) -> None:
    t = _load(path)
    assert _unreserved_addresses(t.steps, t.tb) == []


def test_address_scan_sees_into_chunks() -> None:
    planted = "alex" + "@" + "mail-provider" + ".de"  # built at runtime: never literal in the repo
    body = framing.encode({"jsonrpc": "2.0", "id": 9, "result": [{"from": planted}]})
    data = base64.b64encode(body).decode()
    steps: list[dict[str, Any]] = [
        {
            "from": "tern",
            "msg": {
                "jsonrpc": "2.0",
                "method": "bridge.chunk",
                "params": {"chunk_id": "h0-1", "seq": 0, "last": True, "data_b64": data},
            },
        }
    ]
    assert planted not in json.dumps(steps)  # invisible to a plain-text scan
    assert _unreserved_addresses(steps) == [planted]


def _planted_raw_b64(planted: str, charset: str = "utf-8") -> str:
    """A synthetic message whose only copy of ``planted`` is in a base64-encoded body part."""
    inner = base64.b64encode(f"reply to {planted}\r\n".encode(charset)).decode()
    raw = (
        "Message-ID: <planted-1@example.test>\r\nDate: Fri, 13 Nov 2026 10:00:00 +0000\r\n"
        "From: a@example.test\r\nSubject: synthetic\r\nMIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset={charset}\r\nContent-Transfer-Encoding: base64\r\n"
        f"\r\n{inner}\r\n"
    )
    assert planted not in raw  # only the body's decode shows it
    return base64.b64encode(raw.encode()).decode()


def test_address_scan_sees_into_raw_messages() -> None:
    planted = "alex" + "@" + "mail-provider" + ".de"  # built at runtime: never literal in the repo
    raw_b64 = _planted_raw_b64(planted)
    reply: list[dict[str, Any]] = [
        {"from": "client", "msg": {"jsonrpc": "2.0", "id": 4, "result": {"raw_b64": raw_b64}}}
    ]
    tb: dict[str, Any] = {"messages": [{"id": 1, "raw_b64": raw_b64}]}
    assert planted not in json.dumps([reply, tb])  # invisible to a plain-text scan
    assert _unreserved_addresses(reply) == [planted]
    assert _unreserved_addresses([], tb) == [planted]


def test_address_scan_decodes_raw_bodies_with_their_charset() -> None:
    planted = "alex" + "@" + "mail-provider" + ".de"  # built at runtime: never literal in the repo
    reply: list[dict[str, Any]] = [
        {
            "from": "client",
            "msg": {
                "jsonrpc": "2.0",
                "id": 4,
                "result": {"raw_b64": _planted_raw_b64(planted, "utf-16")},
            },
        }
    ]
    assert _unreserved_addresses(reply) == [planted]


def test_address_scan_fails_loudly_on_an_unknown_charset() -> None:
    raw_b64 = _planted_raw_b64("planted@example.test")
    raw = base64.b64decode(raw_b64).replace(b"charset=utf-8", b"charset=x-no-such-charset")
    with pytest.raises(LookupError):
        _raw_texts(base64.b64encode(raw).decode())


def test_address_scan_sees_the_tb_block() -> None:
    planted = "alex" + "@" + "mail-provider" + ".de"  # built at runtime: never literal in the repo
    tb: dict[str, Any] = {"accounts": [{"id": "acct3", "identities": [{"email": planted}]}]}
    assert _unreserved_addresses([]) == []
    assert _unreserved_addresses([], tb) == [planted]


def test_committed_mail_read_tb_block_is_scanned() -> None:
    """The committed ``tb`` block does carry raw messages, so the scan above has work to do."""
    tb = _load(GOLDEN_DIR / "mail_read.jsonl").tb
    assert tb is not None
    raws = [m["raw_b64"] for m in tb["messages"] if "raw_b64" in m]
    assert raws
    assert all(_EMAIL_RE.search(t) for r in raws for t in _raw_texts(r)[:1])  # reserved ones
