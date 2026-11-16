"""Plan 07 step 3: ``ternd`` builds the one tool registry, ``Daemon.registry``.

Its ``ToolRuntime`` runs over ``Daemon.engine`` (the engine's audit log, read-only index, extension
link and clock); its ``SearchService`` is ``hybrid`` with the embed queue's embedder when
``embed_model`` is set, else ``fts``. Config-path plugins load at start; one that cannot load stops
ternd before its socket exists. ``audit.undo`` runs on this registry (a W1 write made through
``Daemon.registry`` by a config-path plugin is undone by ``tern undo``; ``mcp-proxy`` stays
``role_rejected``).

The extension is a scripted client answered by FakeBridge (the synthetic mailbox), as in
``test_perms_wiring.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import test_fixture_manifest as tfm

from tern import cli, paths
from tern.bridge.client import Peer
from tern.bridge.protocol import METHODS, BridgeError, FindParams, Request, dump
from tern.config import PluginsConfig, TernConfig
from tern.daemon.main import Daemon, serve
from tern.perms.audit import AuditLog, audit_path
from tern.testing.bridge import NMHOST, Client, FakeProcessInfo, connect, hello_params
from tern.testing.fake_bridge import FakeBridge
from tern.testing.procs import env_with_home, ternd_cmd, wait_for_socket
from tern.tools.builtin import BUILTINS
from tern.tools.contract import ToolError
from tern.tools.registry import PluginLoadError, Registry, ToolRuntime, discover

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
COPIES = MANIFEST["message_count"]
BY_KEY = {m["key"]: m for m in MANIFEST["messages"]}
SINGLE = BY_KEY["inv-0042"]["mid"]  # acct1/Invoices only
SINGLE_HMID = BY_KEY["inv-0042"]["message_id"].strip("<>")
DEADLINE_S = 10.0
NO_EMBED = TernConfig(embed_model="")

#: A config-path plugin with a set_tags-shaped W1 tool (ternd has no W built-in before plan 10).
W1_PLUGIN = '''
from tern.bridge.protocol import Mid, UpdateParams
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, tool


class MidsIn(BoundaryModel):
    mids: list[Mid]


class Out(BoundaryModel):
    n: int


@tool(name="plugin_tag", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def plugin_tag(ctx: ToolContext, inp: MidsIn) -> Out:
    """A W1 dummy that tags its targets."""
    ids = [loc.tb_id for locs in ctx.targets.values() for loc in locs]
    await ctx.bridge.request("mail.update", UpdateParams(tb_ids=ids, add_tags=["$label4"]))
    return Out(n=len(ids))


TOOLS = [plugin_tag]
'''

#: A plugin whose tool takes a built-in's name (a duplicate: PluginLoadError).
SHADOW_PLUGIN = W1_PLUGIN.replace('name="plugin_tag"', 'name="search_messages"')


def fb_responder(fb: FakeBridge) -> Callable[[Peer, Request], Any]:
    async def respond(peer: Peer, req: Request) -> Any:
        if req.method == "bridge.ping":
            return {}
        spec = METHODS[req.method]
        return dump(await fb.request(req.method, spec.params.model_validate(req.params)))

    return respond


@dataclass
class Run:
    tp: paths.TernPaths
    cfg: TernConfig
    task: asyncio.Task[None]
    stop: asyncio.Event
    started: list[Daemon] = field(default_factory=list)

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

    def backfill_done(self) -> bool:
        conn = self.daemon.engine.index.connection
        rows = conn.execute("SELECT status FROM ingest_state").fetchall()
        located = conn.execute("SELECT count(*) FROM locations").fetchone()[0]
        return bool(rows) and {r[0] for r in rows} == {"done"} and located == COPIES


def serve_task(
    tp: paths.TernPaths, cfg: TernConfig, started: list[Daemon]
) -> tuple[asyncio.Task[None], asyncio.Event]:
    tp.run.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(
            tp,
            cfg,
            process_info=FakeProcessInfo(),
            nmhost_path=NMHOST,
            stop=stop,
            on_started=started.append,
        )
    )
    return task, stop


@contextlib.asynccontextmanager
async def running(cfg: TernConfig) -> AsyncIterator[Run]:
    tp = paths.resolve()
    started: list[Daemon] = []
    task, stop = serve_task(tp, cfg, started)
    try:
        await asyncio.to_thread(wait_for_socket, tp.socket)
        yield Run(tp, cfg, task, stop, started)
    finally:
        stop.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE_S)


@pytest_asyncio.fixture
async def run() -> AsyncIterator[Run]:
    """ternd with embedding off (the registry searches in ``fts`` mode)."""
    async with running(NO_EMBED) as r:
        yield r


async def extension(r: Run, fb: FakeBridge) -> Client:
    c = await connect(
        r.tp.socket,
        "extension",
        serve=fb_responder(fb),
        hello=hello_params("extension", profile_id=str(uuid.uuid4())),
    )
    fb.on_event = c.peer.notify
    return c


def runtime_of(registry: Registry) -> ToolRuntime:
    rt = registry._runtime  # pyright: ignore[reportPrivateUsage] -- Registry has no runtime accessor (registry.py is not in plan 07's files)
    assert rt is not None, "Daemon.registry has no ToolRuntime"
    return rt


def rows(tp: paths.TernPaths) -> list[tuple[str, str, str]]:
    """(tool, caller, decision) of every audit row, oldest first, read beside ternd."""
    with AuditLog.open(audit_path(tp)) as reader:
        return [(r.tool, r.caller, r.decision) for r in reversed(reader.list(limit=1000))]


# ---- the registry and its runtime -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_registry_lists_the_builtins(run: Run) -> None:
    reg = run.daemon.registry
    assert {s.name for s in BUILTINS} <= set(reg.names())
    for spec in BUILTINS:
        tool = reg.get(spec.name)
        assert tool is not None and tool.origin == "builtin"
    # Exactly what discovery finds with no config paths (built-ins + any entry points).
    assert reg.describe() == Registry(discover()).describe()


@pytest.mark.asyncio
async def test_the_runtime_is_the_engines(run: Run) -> None:
    d = run.daemon
    rt = runtime_of(d.registry)
    assert rt.audit is d.audit
    assert rt.engine is d.engine
    assert rt.index is d.engine.index and rt.index.readonly and rt.index is not d.store
    assert rt.bridge is d.server.link
    assert rt.clock is d.engine.clock
    assert rt.config is run.cfg
    assert rt.search.store is d.engine.index
    # tools keep local state (the tb_* snapshot copies) under ternd's own state dir
    assert rt.state_dir == run.tp.state


@pytest.mark.asyncio
async def test_without_embed_model_search_is_fts(run: Run) -> None:
    d = run.daemon
    assert d.embed is None
    assert runtime_of(d.registry).search.mode == "fts"
    res = await d.registry.call("mcp:t1", "search_messages", {"query": "invoice"})
    assert res.output.model_dump()["mode"] == "fts"
    assert rows(run.tp) == [("search_messages", "mcp:t1", "auto")]


@pytest.mark.asyncio
async def test_with_embed_model_search_is_hybrid_on_the_queues_embedder() -> None:
    # An embed_model is set; the conftest's factory gives an unreachable embedder.
    async with running(TernConfig(embed_model="text-embedding-qwen3-embedding-0.6b")) as r:
        d = r.daemon
        assert d.embed is not None
        search = runtime_of(d.registry).search
        assert search.mode == "hybrid"
        assert search._embedder is d.embed.embedder  # pyright: ignore[reportPrivateUsage] -- SearchService has no embedder accessor (search is not in plan 07's files)
        # No model is registered yet (the embedder is unreachable): hybrid refuses, never
        # falls back to FTS.
        with pytest.raises(ToolError) as exc:
            await d.registry.call("mcp:t1", "search_messages", {"query": "invoice"})
        assert (exc.value.code, exc.value.reason) == ("unavailable", "no_embedding_model")
        # The write-ahead row, then the tool's error event (Registry.call's tool-run path).
        assert rows(r.tp) == [("search_messages", "mcp:t1", "auto")]
        with AuditLog.open(audit_path(r.tp)) as reader:
            [row] = reader.list(limit=1)
            assert [e.kind for e in reader.events(row.call_id)] == ["error"]


# ---- config-path plugins ----------------------------------------------------------------------


def plugin_cfg(*plugin_paths: Path) -> TernConfig:
    return TernConfig(embed_model="", plugins=PluginsConfig(paths=list(plugin_paths)))


@pytest.mark.asyncio
async def test_config_path_plugins_are_registered(tmp_path: Path) -> None:
    plugin = tmp_path / "w1_plugin.py"
    plugin.write_text(W1_PLUGIN, encoding="utf-8")
    async with running(plugin_cfg(plugin)) as r:
        tool = r.daemon.registry.get("plugin_tag")
        assert tool is not None
        assert (tool.origin, tool.source) == ("plugin", f"plugin file {plugin}")


def missing(tmp_path: Path) -> Path:
    return tmp_path / "no_such_plugin.py"


def raising(tmp_path: Path) -> Path:
    path = tmp_path / "raising_plugin.py"
    path.write_text("raise RuntimeError('boom at import')\n", encoding="utf-8")
    return path


def exiting(tmp_path: Path) -> Path:
    path = tmp_path / "exiting_plugin.py"
    path.write_text("import sys\n\nsys.exit(0)\n", encoding="utf-8")
    return path


def shadowing(tmp_path: Path) -> Path:
    path = tmp_path / "shadow_plugin.py"
    path.write_text(SHADOW_PLUGIN, encoding="utf-8")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [missing, raising, exiting, shadowing])
async def test_a_broken_plugin_stops_ternd_before_the_socket(
    tmp_path: Path, broken: Callable[[Path], Path], caplog: pytest.LogCaptureFixture
) -> None:
    tp = paths.resolve()
    started: list[Daemon] = []
    caplog.set_level(logging.INFO, logger="tern")
    task, _ = serve_task(tp, plugin_cfg(broken(tmp_path)), started)
    with pytest.raises(PluginLoadError):
        await asyncio.wait_for(task, DEADLINE_S)
    assert started == []
    assert not tp.socket.exists()
    [err] = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert err.name == "tern.daemon" and err.getMessage() == (
        "tool plugins failed to load; ternd stops"
    )
    assert err.__dict__["error"] == "PluginLoadError"
    assert "ternd listening" not in caplog.messages and "ternd stopped" in caplog.messages


@pytest.mark.parametrize("broken", [missing, exiting])
def test_ternd_exits_non_zero_on_a_broken_plugin_path(
    tern_home: Path, tmp_path: Path, broken: Callable[[Path], Path]
) -> None:
    tp = paths.resolve()
    tp.config_file.parent.mkdir(parents=True, exist_ok=True)
    # embed_model first: env_with_home leaves a config that names it alone.
    tp.config_file.write_text(
        f'embed_model = ""\n\n[plugins]\npaths = ["{broken(tmp_path)}"]\n', encoding="utf-8"
    )
    proc = subprocess.run(
        ternd_cmd("--foreground"),
        env=env_with_home(tern_home),
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode != 0
    assert not tp.socket.exists()
    stderr = proc.stderr.decode()
    assert "tool plugins failed to load; ternd stops" in stderr
    # The re-raise reaches main(): its traceback, not only the log line's `error` field.
    assert "Traceback" in stderr and "tern.tools.registry.PluginLoadError" in stderr
    assert "ternd listening" not in stderr


# ---- audit.undo runs on Daemon.registry -------------------------------------------------------


async def tags_of_single(fb: FakeBridge) -> list[str]:
    [loc] = (await fb.request("mail.messages.find", FindParams(header_message_id=SINGLE_HMID))).root
    return list(loc.tags)


@pytest.mark.asyncio
async def test_a_plugin_write_through_daemon_registry_is_undone_by_tern_undo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plugin = tmp_path / "w1_plugin.py"
    plugin.write_text(W1_PLUGIN, encoding="utf-8")
    async with running(plugin_cfg(plugin)) as r:
        fb = FakeBridge(FIXTURE)
        c = await extension(r, fb)
        await r.until(r.backfill_done, "the backfill")
        before = await tags_of_single(fb)
        res = await r.daemon.registry.call("agent:s1", "plugin_tag", {"mids": [SINGLE]})
        assert res.undo_token is not None
        assert "$label4" in await tags_of_single(fb) and "$label4" not in before

        # mcp-proxy: refused before the registry (no row, nothing undone).
        proxy = await connect(r.tp.socket, "mcp-proxy")
        with pytest.raises(BridgeError) as exc:
            await proxy.peer.request("audit.undo", {"undo_token": res.undo_token}, timeout=10)
        assert exc.value.name == "role_rejected"
        await proxy.close()
        assert rows(r.tp) == [("plugin_tag", "agent:s1", "auto")]

        # tern undo (role cli) undoes it as caller ui.
        code = await asyncio.to_thread(cli.main, ["undo", res.undo_token])
        out, err = capsys.readouterr()
        assert (code, err) == (0, "") and out.startswith("undone")
        assert await tags_of_single(fb) == before
        assert rows(r.tp) == [("plugin_tag", "agent:s1", "auto"), ("undo", "ui", "auto")]
        with AuditLog.open(audit_path(r.tp)) as reader:
            assert reader.status(res.call_id).undone
        await c.close()
