"""Plan 08b step 2: ternd's ``index.status`` handler (``tern.daemon.status`` and its wiring).

Every test runs ``serve()`` in-process on a real Unix socket with synthetic data only: an
extension peer serves FakeBridge (the backfill fills the index), a ``cli`` peer reads the status.
The result is checked against the frozen ``contracts/tern-bridge-1.0.schema.json`` by a small
validator for the subset of JSON Schema that ``IndexStatus`` uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm

from tern import paths
from tern.bridge.client import Peer
from tern.bridge.protocol import (
    METHODS,
    BridgeError,
    IndexStatus,
    Notification,
    Request,
    Role,
    dump,
)
from tern.config import TernConfig
from tern.daemon.main import Daemon, EmbedderFactory, serve
from tern.index.service import JobOutcome
from tern.index.store import Store, index_path
from tern.perms.audit import AuditLog, audit_path
from tern.testing.bridge import NMHOST, FakeProcessInfo, hello_params
from tern.testing.fake_bridge import FakeBridge
from tern.testing.procs import wait_for_socket

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
COPIES = MANIFEST["message_count"]
DISTINCT = MANIFEST["distinct_messages"]
FOLDER_IDS = [f"{a['key']}:/{f['path']}" for a in MANIFEST["accounts"] for f in a["folders"]]
CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "tern-bridge-1.0.schema.json"
SCHEMA: dict[str, Any] = json.loads(CONTRACT.read_text(encoding="utf-8"))
DEADLINE_S = 10.0
FAKE_MODEL = "fake-chat-1"

Responder = Callable[[Peer, Request], Awaitable[Any]]


# ---- a validator for the JSON Schema subset of IndexStatus -----------------------------------


def check_schema(value: Any, schema: dict[str, Any], where: str = "$") -> None:
    """Raise AssertionError unless ``value`` matches ``schema`` (``$ref`` into ``$defs``,
    ``anyOf``, ``type``, ``required``, ``additionalProperties: false``, ``properties``,
    ``items``, ``minimum``, ``format: date-time``). Any other keyword fails loudly, so the
    validator cannot silently accept a schema it does not understand."""
    known = {
        "$ref",
        "anyOf",
        "type",
        "required",
        "additionalProperties",
        "properties",
        "items",
        "minimum",
        "format",
        "title",
    }
    unknown = set(schema) - known
    assert not unknown, f"{where}: validator does not support {sorted(unknown)}"
    if "$ref" in schema:
        name = schema["$ref"].removeprefix("#/$defs/")
        check_schema(value, SCHEMA["$defs"][name], where)
        return
    if "anyOf" in schema:
        errors: list[str] = []
        for option in schema["anyOf"]:
            try:
                check_schema(value, option, where)
            except AssertionError as exc:
                errors.append(str(exc))
            else:
                return
        raise AssertionError(f"{where}: matches no anyOf option: {errors}")
    kind = schema.get("type")
    if kind == "object":
        assert isinstance(value, dict), f"{where}: not an object"
        missing = set(schema.get("required", [])) - set(value)
        assert not missing, f"{where}: missing {sorted(missing)}"
        props: dict[str, Any] = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            assert not extra, f"{where}: unexpected {sorted(extra)}"
        for key, sub in props.items():
            if key in value:
                check_schema(value[key], sub, f"{where}.{key}")
    elif kind == "array":
        assert isinstance(value, list), f"{where}: not an array"
        for i, item in enumerate(value):
            check_schema(item, schema["items"], f"{where}[{i}]")
    elif kind == "integer":
        assert isinstance(value, int) and not isinstance(value, bool), f"{where}: not an integer"
    elif kind == "number":
        assert isinstance(value, int | float) and not isinstance(value, bool), f"{where}: NaN"
    elif kind == "string":
        assert isinstance(value, str), f"{where}: not a string"
        if schema.get("format") == "date-time":
            parsed = datetime.fromisoformat(value)
            assert parsed.tzinfo is not None, f"{where}: date-time without an offset"
    elif kind == "null":
        assert value is None, f"{where}: not null"
    else:
        raise AssertionError(f"{where}: validator does not support type {kind!r}")
    if "minimum" in schema:
        assert value >= schema["minimum"], f"{where}: below minimum"


def check_frozen(result: dict[str, Any]) -> IndexStatus:
    """``result`` (the wire JSON) is a valid 1.0 ``index.status`` result: the frozen schema and
    the protocol's own result model both accept it."""
    ref = SCHEMA["methods"]["index.status"]["result"]
    check_schema(result, ref)
    model = METHODS["index.status"].result
    assert model is IndexStatus
    status = model.model_validate(result)
    return status


def test_the_validator_rejects_what_the_frozen_schema_forbids() -> None:
    good = {
        "messages": 1,
        "locations": 1,
        "conflicted": 0,
        "parse_failed": 0,
        "headers_only": 0,
        "embed_pending": 0,
        "lag_s": 0.0,
        "last_reconcile": None,
        "models": {"chat": "c", "embed": "", "runtime_version": ""},
        "accounts": [{"account_id": "a", "name": "", "last_sync": "2026-01-01T00:00:00Z"}],
    }
    check_frozen(good)
    for bad in (
        {**good, "jobs_failed": 0},
        {**good, "messages": -1},
        {**good, "models": {"chat": "c", "embed": None, "runtime_version": ""}},
        {**good, "accounts": [{"account_id": "a", "name": None, "last_sync": None}]},
        {k: v for k, v in good.items() if k != "lag_s"},
    ):
        with pytest.raises(AssertionError):
            check_schema(bad, SCHEMA["methods"]["index.status"]["result"])


# ---- harness ---------------------------------------------------------------------------------


@dataclass
class Run:
    tp: paths.TernPaths
    task: asyncio.Task[None]
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


@contextlib.asynccontextmanager
async def running(
    cfg: TernConfig, *, embedder_factory: EmbedderFactory | None = None
) -> AsyncIterator[Run]:
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
            embedder_factory=embedder_factory,
            on_started=started.append,
        )
    )
    run = Run(tp, task, started)
    try:
        await asyncio.to_thread(wait_for_socket, tp.socket)
        await run.until(lambda: bool(started), "on_started")
        yield run
    finally:
        stop.set()
        await asyncio.wait_for(task, DEADLINE_S)


@dataclass
class Client:
    peer: Peer
    task: asyncio.Task[None]

    async def status(self) -> dict[str, Any]:
        res = await self.peer.request("index.status", {}, timeout=5)
        assert isinstance(res, dict)
        return res

    async def close(self) -> None:
        await self.peer.close()
        await self.task


async def client(sock: Path, role: Role = "cli", *, serve: Responder | None = None) -> Client:
    reader, writer = await asyncio.open_unix_connection(str(sock))

    async def on_note(peer: Peer, msg: Notification) -> None:
        return None

    async def on_request(peer: Peer, req: Request) -> Any:
        if req.method == "bridge.ping":
            return {}
        if serve is None:
            raise NotImplementedError(req.method)
        return await serve(peer, req)

    peer = Peer(reader, writer, on_request=on_request, on_notification=on_note)
    task = asyncio.create_task(peer.run())
    try:
        await peer.request("hello", hello_params(role), timeout=5)
    except BaseException:
        await peer.close()
        await task
        raise
    return Client(peer, task)


@dataclass
class Gate:
    """In front of FakeBridge: while closed, every request from ternd waits (``waiting``)."""

    closed: bool = False
    waiting: int = 0
    _open: asyncio.Event = field(default_factory=asyncio.Event)

    def close(self) -> None:
        self.closed = True
        self._open.clear()

    def open(self) -> None:
        self.closed = False
        self._open.set()

    async def pass_(self) -> None:
        if self.closed:
            self.waiting += 1
            try:
                await self._open.wait()
            finally:
                self.waiting -= 1


def fb_responder(fb: FakeBridge, gate: Gate | None = None) -> Responder:
    async def respond(peer: Peer, req: Request) -> Any:
        if gate is not None:
            await gate.pass_()
        spec = METHODS[req.method]
        return dump(await fb.request(req.method, spec.params.model_validate(req.params)))

    return respond


async def backfilled(run: Run, gate: Gate | None = None) -> tuple[FakeBridge, Client]:
    """An extension peer over FakeBridge, once the backfill has located every copy."""
    fb = FakeBridge(FIXTURE)
    ext = await client(run.tp.socket, "extension", serve=fb_responder(fb, gate))
    fb.on_event = ext.peer.notify
    conn = run.daemon.engine.index.connection
    await run.until(
        lambda: conn.execute("SELECT count(*) FROM locations").fetchone()[0] == COPIES,
        "the backfill",
    )
    return fb, ext


def audit_rows(tp: paths.TernPaths) -> list[tuple[str, str]]:
    with AuditLog.open(audit_path(tp)) as reader:
        return [(r.tool, r.caller) for r in reader.list(limit=1000)]


def epoch(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp())


# ---- tests -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_counts_match_the_synthetic_index_and_validate_against_1_0() -> None:
    async with running(TernConfig(embed_model="", chat_model=FAKE_MODEL)) as run:
        _, ext = await backfilled(run)
        # Two folders of the first account reconciled at known times, none of the others.
        first = MANIFEST["accounts"][0]
        ids = [f"{first['key']}:/{f['path']}" for f in first["folders"]][:2]
        stamps = [epoch("2026-03-01T08:00:00+00:00"), epoch("2026-03-02T09:30:00+00:00")]
        store = run.daemon.store
        with store.transaction():
            for folder_id, stamp in zip(ids, stamps, strict=True):
                store.connection.execute(
                    "UPDATE folders SET last_reconciled_at = ? WHERE folder_id = ?",
                    (stamp, folder_id),
                )
        cli = await client(run.tp.socket)
        status = check_frozen(await cli.status())

        reader = Store.open(index_path(run.tp), readonly=True)
        try:
            counts = reader.status()
        finally:
            reader.close()
        assert (status.messages, status.locations) == (DISTINCT, COPIES)
        assert status.conflicted == counts.conflicted
        assert status.parse_failed == counts.parse_failed
        assert status.headers_only == counts.headers_only
        assert status.last_reconcile == datetime.fromtimestamp(max(stamps), UTC)
        by_id = {a.account_id: a for a in status.accounts}
        assert sorted(by_id) == sorted(a["key"] for a in MANIFEST["accounts"])
        assert {k: a.name for k, a in by_id.items()} == {
            a["key"]: a["name"] for a in MANIFEST["accounts"]
        }
        assert by_id[first["key"]].last_sync == datetime.fromtimestamp(max(stamps), UTC)
        assert all(a.last_sync is None for k, a in by_id.items() if k != first["key"])
        # Embedding disabled: the config's own "" and nothing pending; runtime not reported.
        assert status.embed_pending == 0
        assert status.models.model_dump() == {
            "chat": FAKE_MODEL,
            "embed": "",
            "runtime_version": "",
        }
        assert status.lag_s == 0.0
        await cli.close()
        await ext.close()
        # A status read is not a tool call: no audit row.
        assert audit_rows(run.tp) == []


@pytest.mark.asyncio
async def test_a_fresh_index_reports_zero_counts_and_null_reconcile() -> None:
    async with running(TernConfig(embed_model="", chat_model=FAKE_MODEL)) as run:
        cli = await client(run.tp.socket)
        status = check_frozen(await cli.status())
        await cli.close()
        assert (status.messages, status.locations, status.accounts) == (0, 0, [])
        assert status.last_reconcile is None and status.lag_s == 0.0


@pytest.mark.asyncio
async def test_embed_pending_and_model_come_from_the_embed_queue_when_enabled() -> None:
    # The conftest's unreachable embedder: the queue never embeds, every chunk stays pending.
    cfg = TernConfig(chat_model=FAKE_MODEL, embed_model="text-embedding-qwen3-embedding-0.6b")
    assert cfg.embed_model
    async with running(cfg) as run:
        _, ext = await backfilled(run)
        # Every chunk written before the counts are compared: the backfill job has recorded
        # its outcome, and the chunk count is read once before the request and once after it.
        outcomes = run.daemon.service.outcomes
        await run.until(lambda: JobOutcome("backfill", "ok") in outcomes, "the backfill outcome")
        conn = run.daemon.engine.index.connection
        n_chunks = int(conn.execute("SELECT count(*) FROM chunks").fetchone()[0])
        assert n_chunks > 0
        cli = await client(run.tp.socket)
        status = check_frozen(await cli.status())
        embed = run.daemon.embed
        assert embed is not None
        assert int(conn.execute("SELECT count(*) FROM chunks").fetchone()[0]) == n_chunks
        assert status.embed_pending == n_chunks == embed.state.pending
        assert status.models.embed == cfg.embed_model
        await cli.close()
        await ext.close()


@pytest.mark.asyncio
async def test_an_account_with_a_null_name_is_reported_as_empty_string() -> None:
    async with running(TernConfig(embed_model="", chat_model=FAKE_MODEL)) as run:
        store = run.daemon.store
        with store.transaction():
            store.connection.execute(
                "INSERT INTO accounts(account_id, name, type) VALUES ('acct-null', NULL, 'none')"
            )
        cli = await client(run.tp.socket)
        result = await cli.status()
        await cli.close()
        check_frozen(result)
        assert result["accounts"] == [{"account_id": "acct-null", "name": "", "last_sync": None}]


@pytest.mark.asyncio
async def test_lag_s_grows_while_an_event_is_blocked_and_is_zero_once_applied() -> None:
    async with running(TernConfig(embed_model="", chat_model=FAKE_MODEL)) as run:
        gate = Gate()
        fb, ext = await backfilled(run, gate)
        cli = await client(run.tp.socket)
        await run.until(lambda: not run.daemon.service._event_tasks, "no event pending")
        assert (await cli.status())["lag_s"] == 0.0
        gate.close()
        await fb.folder_changed(FOLDER_IDS[0])
        await run.until(lambda: gate.waiting > 0, "the event's request to wait at the gate")
        first = check_frozen(await cli.status()).lag_s
        assert first > 0.0
        await asyncio.sleep(0.05)
        second = check_frozen(await cli.status()).lag_s
        assert second >= first + 0.05
        gate.open()
        await run.until(lambda: not run.daemon.service._event_tasks, "the event to be applied")
        assert (await cli.status())["lag_s"] == 0.0
        await cli.close()
        await ext.close()


@pytest.mark.asyncio
async def test_mcp_proxy_is_role_rejected_and_writes_no_audit_row() -> None:
    async with running(TernConfig(embed_model="", chat_model=FAKE_MODEL)) as run:
        proxy = await client(run.tp.socket, "mcp-proxy")
        with pytest.raises(BridgeError) as refused:
            await proxy.status()
        assert refused.value.name == "role_rejected"
        await proxy.close()
        # the extension role is served
        ext = await client(run.tp.socket, "extension", serve=fb_responder(FakeBridge(FIXTURE)))
        check_frozen(await ext.status())
        await ext.close()
        assert audit_rows(run.tp) == []
