"""No network transport in ``tern.mcp`` (plan 07 step 5, the in-process part; amendment 4).

``import mcp`` pulls in the SDK's HTTP/ASGI stack, so "no network transport" is proved by what
``tern.mcp`` uses (AST), by the sockets a session opens (inet ``bind``/``listen`` patched to
fail, with a control), by ternd and the other ``tern`` commands never importing the SDK, and by
``server.py`` reaching the registry and the audit log only through its backend. Part B adds the
``tern mcp`` process checks (step 9).
"""

from __future__ import annotations

import ast
import asyncio
import errno
import inspect
import json
import logging
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import Implementation

import tern.mcp.server as server_mod
from tern import paths
from tern.config import TernConfig
from tern.index.store import Store
from tern.mcp.local import RegistryBackend
from tern.mcp.server import build_server
from tern.perms.audit import AuditLog, audit_path
from tern.perms.engine import Engine
from tern.perms.recipients import DenyAllRecipients
from tern.search.service import SearchService
from tern.testing.fake_bridge import FakeBridge
from tern.testing.procs import inet_inodes, socket_inodes
from tern.tools.registry import Registry, ToolRuntime, builtin_tools, describe_registry

MCP_DIR = Path(server_mod.__file__).resolve().parent
SERVER_PY = MCP_DIR / "server.py"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_mailbox"
CALLER = "mcp:transport"
CLIENT = "transport-client"

#: Modules whose import (or any submodule's) means a network transport. A name ending in ``*``
#: is a prefix (``mcp.server.streamable_http`` and ``mcp.server.streamable_http_manager``;
#: ``mcp.server._streamable_http_modern``). ``mcp.server.auth`` and
#: ``mcp.server.transport_security`` are the SDK's other starlette modules.
FORBIDDEN_MODULES = (
    "http.server",
    "socketserver",
    "uvicorn",
    "hypercorn",
    "aiohttp",
    "starlette",
    "sse_starlette",
    "websockets",
    "mcp.server.sse",
    "mcp.server.streamable_http*",
    "mcp.server._streamable_http*",
    "mcp.server.auth",
    "mcp.server.transport_security",
)

#: Names whose use (attribute, name or imported alias) means a web transport or the high-level
#: ``MCPServer`` (tern uses the low-level ``Server``; amendment 2), whose ``run`` can serve
#: HTTP. Any use of ``MCPServer`` is flagged, which covers ``MCPServer.run``. The asyncio and
#: ``socketserver`` listener entry points (``start_server``, ``create_server``,
#: ``start_unix_server``, ``serve_forever``) are flagged too.
FORBIDDEN_NAMES = frozenset(
    {
        "start_server",
        "create_server",
        "start_unix_server",
        "serve_forever",
        "run_sse_async",
        "run_streamable_http_async",
        "sse_app",
        "streamable_http_app",
        "custom_route",
        "session_manager",
        "MCPServer",
    }
)

#: What ``server.py`` must not import directly (only ``local.py`` touches the registry).
SERVER_FORBIDDEN_IMPORTS = ("tern.index", "tern.perms.audit", "tern.tools.registry")

#: The web stack the SDK imports; ternd and the other tern commands must import none of it.
WEB_STACK = ("mcp", "starlette", "uvicorn", "sse_starlette", "socketserver", "http.server")


# ---- AST helpers --------------------------------------------------------------------------------


def imported_modules(tree: ast.AST, package: str = "tern.mcp") -> set[str]:
    """Every module an ``import`` names, plus ``module.name`` for each ``from module import
    name`` (``from mcp.server import sse`` yields ``mcp.server`` and ``mcp.server.sse``).
    Relative imports are resolved against ``package``."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                anchor = ".".join(parts[: len(parts) - node.level + 1])
                base = f"{anchor}.{base}" if base else anchor
            out.add(base)
            out.update(f"{base}.{alias.name}" for alias in node.names)
    return out


def _matches(module: str, pattern: str) -> bool:
    if pattern.endswith("*"):
        return module.startswith(pattern[:-1])
    return module == pattern or module.startswith(pattern + ".")


def forbidden_imports(modules: Iterable[str], patterns: Iterable[str]) -> set[str]:
    pats = tuple(patterns)
    return {m for m in modules if any(_matches(m, p) for p in pats)}


def used_names(tree: ast.AST) -> set[str]:
    """Every identifier used as a name, an attribute or an imported alias."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                out.add(node.asname)
    return out


def transport_findings(source: str) -> set[str]:
    tree = ast.parse(source)
    found = forbidden_imports(imported_modules(tree), FORBIDDEN_MODULES)
    return found | (used_names(tree) & FORBIDDEN_NAMES)


def mcp_sources() -> list[Path]:
    files = sorted(MCP_DIR.glob("*.py"))
    assert {"__init__.py", "server.py", "local.py"} <= {f.name for f in files}
    return files


# ---- AST scan: no web-transport imports or APIs -------------------------------------------------


@pytest.mark.parametrize("path", mcp_sources(), ids=lambda p: p.name)
def test_mcp_package_uses_no_web_transport(path: Path) -> None:
    assert transport_findings(path.read_text(encoding="utf-8")) == set()


@pytest.mark.parametrize(
    ("source", "finding"),
    [
        ("import http.server", "http.server"),
        ("from http.server import HTTPServer", "http.server"),
        ("import socketserver", "socketserver"),
        ("import uvicorn", "uvicorn"),
        ("from starlette.applications import Starlette", "starlette.applications"),
        ("from sse_starlette import EventSourceResponse", "sse_starlette"),
        ("import websockets.server", "websockets.server"),
        ("from mcp.server import sse", "mcp.server.sse"),
        ("from mcp.server.sse import SseServerTransport", "mcp.server.sse"),
        ("import mcp.server.streamable_http", "mcp.server.streamable_http"),
        (
            "from mcp.server.streamable_http_manager import StreamableHTTPSessionManager",
            "mcp.server.streamable_http_manager",
        ),
        ("server.run_sse_async()", "run_sse_async"),
        ("server.run_streamable_http_async()", "run_streamable_http_async"),
        ("app = server.sse_app()", "sse_app"),
        ("app = server.streamable_http_app()", "streamable_http_app"),
        ("@server.custom_route('/x')\ndef f(): pass", "custom_route"),
        ("m = server.session_manager", "session_manager"),
        ("from mcp.server import MCPServer\nMCPServer('t').run()", "MCPServer"),
        ("from mcp.server.mcpserver import MCPServer as S", "MCPServer"),
        ("import hypercorn.asyncio", "hypercorn.asyncio"),
        ("from aiohttp import web", "aiohttp"),
        (
            "from mcp.server._streamable_http_modern import X",
            "mcp.server._streamable_http_modern",
        ),
        ("from mcp.server import _streamable_http_modern", "mcp.server._streamable_http_modern"),
        ("from mcp.server.auth.routes import create_auth_routes", "mcp.server.auth.routes"),
        ("from mcp.server import auth", "mcp.server.auth"),
        ("import mcp.server.transport_security", "mcp.server.transport_security"),
        (
            "from mcp.server.transport_security import TransportSecuritySettings",
            "mcp.server.transport_security",
        ),
        ("await asyncio.start_server(h, '127.0.0.1', 0)", "start_server"),
        ("await loop.create_server(f, '127.0.0.1', 0)", "create_server"),
        ("await asyncio.start_unix_server(h, path='s')", "start_unix_server"),
        ("from asyncio import start_server as ss", "start_server"),
        ("srv.serve_forever()", "serve_forever"),
    ],
)
def test_transport_scan_catches_each_forbidden_use(source: str, finding: str) -> None:
    """Control: the scan above can fail, for each module and name it checks."""
    assert finding in transport_findings(source)


def test_transport_scan_ignores_docstrings_and_the_stdio_transport() -> None:
    source = (
        '"""Never uvicorn, starlette or sse_app here."""\n'
        "from mcp.server import Server\n"
        "from mcp.server.stdio import stdio_server\n"
        "from mcp.types import Tool\n"
        "import http\n"
    )
    assert transport_findings(source) == set()


def _functions_using(tree: ast.Module, predicate: Any) -> set[str]:
    """Names of the top-level functions whose body has a node satisfying ``predicate``, plus
    ``<module>`` for a use outside any top-level function."""
    out: set[str] = set()
    for stmt in tree.body:
        owner = (
            stmt.name if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) else "<module>"
        )
        if isinstance(stmt, ast.ImportFrom | ast.Import):
            continue
        if any(predicate(n) for n in ast.walk(stmt)):
            out.add(owner)
    return out


def _local_names(tree: ast.Module, name: str) -> set[str]:
    """``name`` plus every alias a ``from ... import name as alias`` gives it in ``tree``."""
    out = {name}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            out.update(a.asname for a in node.names if a.name == name and a.asname)
    return out


def _refers_to(node: ast.AST, name: str, aliases: set[str]) -> bool:
    """``node`` is a bare name in ``aliases`` or a qualified ``x.name`` (``lowlevel.Server``)."""
    return (isinstance(node, ast.Name) and node.id in aliases) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


def server_call_predicate(tree: ast.Module) -> Any:
    """A predicate matching a ``Server(...)`` call in ``tree``, bare, qualified or aliased."""
    aliases = _local_names(tree, "Server")
    return lambda node: isinstance(node, ast.Call) and _refers_to(node.func, "Server", aliases)


def stdio_server_predicate(tree: ast.Module) -> Any:
    """A predicate matching a use of ``stdio_server`` in ``tree``, bare, qualified or aliased."""
    aliases = _local_names(tree, "stdio_server")
    return lambda node: _refers_to(node, "stdio_server", aliases)


def _is_run_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("run")
    )


def test_server_entry_points_are_build_server_and_run_stdio() -> None:
    """``server.py`` builds a ``Server`` only in ``build_server`` and serves one only in
    ``run_stdio(backend)`` over ``stdio_server``; it has no ``__main__`` block."""
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    assert _functions_using(tree, server_call_predicate(tree)) == {"build_server"}
    assert _functions_using(tree, stdio_server_predicate(tree)) == {"run_stdio"}
    assert _functions_using(tree, _is_run_call) == {"run_stdio"}
    main_guards = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == "__name__"
    ]
    assert main_guards == []
    assert inspect.iscoroutinefunction(server_mod.run_stdio)
    assert list(inspect.signature(server_mod.run_stdio).parameters) == ["backend"]
    assert list(inspect.signature(server_mod.build_server).parameters) == ["backend"]


def test_entry_point_scan_sees_a_second_server_and_a_main_block() -> None:
    """Control: an extra ``Server(...)`` or a ``run`` outside ``run_stdio`` is attributed to its
    function or to ``<module>``."""
    tree = ast.parse(
        "def build_server(b): return Server('t')\n"
        "def other(b): return Server('u')\n"
        "server = Server('v')\n"
        "server.run_streamable_http_async()\n"
    )
    assert _functions_using(tree, server_call_predicate(tree)) == {
        "build_server",
        "other",
        "<module>",
    }
    assert _functions_using(tree, _is_run_call) == {"<module>"}


@pytest.mark.parametrize(
    ("source", "owners"),
    [
        ("from mcp.server import lowlevel\ndef other(b): return lowlevel.Server('x')\n", {"other"}),
        ("import mcp.server\ndef other(b): return mcp.server.Server('x')\n", {"other"}),
        ("from mcp.server import Server as S\ndef other(b): return S('x')\n", {"other"}),
        ("from mcp.server import Server as S\nserver = S('x')\n", {"<module>"}),
    ],
)
def test_entry_point_scan_sees_qualified_and_aliased_servers(source: str, owners: set[str]) -> None:
    """Control: a ``Server`` built through a module attribute or an import alias is attributed."""
    tree = ast.parse(source)
    assert _functions_using(tree, server_call_predicate(tree)) == owners


@pytest.mark.parametrize(
    ("source", "owners"),
    [
        ("import mcp.server.stdio as s\nasync def other(b):\n    s.stdio_server()\n", {"other"}),
        (
            "from mcp.server.stdio import stdio_server as sio\nasync def other(b):\n    sio()\n",
            {"other"},
        ),
        ("from mcp.server.stdio import stdio_server\ncm = stdio_server()\n", {"<module>"}),
    ],
)
def test_entry_point_scan_sees_qualified_and_aliased_stdio_server(
    source: str, owners: set[str]
) -> None:
    """Control: ``stdio_server`` reached through a module attribute or an alias is attributed."""
    tree = ast.parse(source)
    assert _functions_using(tree, stdio_server_predicate(tree)) == owners


# ---- server.py reaches the registry and the audit log only through its backend ------------------


def test_server_imports_nothing_from_index_audit_or_registry() -> None:
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    assert forbidden_imports(imported_modules(tree), SERVER_FORBIDDEN_IMPORTS) == set()


def test_only_local_touches_the_registry() -> None:
    for path in mcp_sources():
        modules = imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        uses = forbidden_imports(modules, ("tern.tools.registry",))
        assert (path.name == "local.py") == bool(uses), path.name


@pytest.mark.parametrize(
    ("source", "finding"),
    [
        ("from tern.tools.registry import Registry", "tern.tools.registry"),
        ("import tern.index.store", "tern.index.store"),
        ("from tern.index import store", "tern.index"),
        ("from tern.perms import audit", "tern.perms.audit"),
        ("from tern.perms.audit import AuditLog", "tern.perms.audit"),
        ("from ..tools.registry import Registry", "tern.tools.registry"),
        ("from ..perms import audit", "tern.perms.audit"),
    ],
)
def test_server_import_scan_catches_direct_imports(source: str, finding: str) -> None:
    """Control: each forbidden direct import, absolute or relative, is found."""
    found = forbidden_imports(imported_modules(ast.parse(source)), SERVER_FORBIDDEN_IMPORTS)
    assert finding in found


# ---- ternd and tern.cli never import mcp --------------------------------------------------------

_IMPORT_PROBE = (
    "import importlib, json, sys\n"
    "importlib.import_module(sys.argv[1])\n"
    "print(json.dumps(sorted(sys.modules)))\n"
)


def _modules_after_import(module: str) -> set[str]:
    """``sys.modules`` of a fresh interpreter after ``import module`` (this process already holds
    the SDK, so the check needs its own interpreter)."""
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE, module],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=dict(os.environ),
    )
    return set(json.loads(proc.stdout))


def _web_stack(modules: set[str]) -> set[str]:
    return forbidden_imports(modules, WEB_STACK)


@pytest.mark.parametrize("module", ["tern.daemon.main", "tern.cli"])
def test_ternd_and_cli_never_import_the_sdk(module: str) -> None:
    modules = _modules_after_import(module)
    assert module in modules
    assert _web_stack(modules) == set()


def test_import_probe_sees_the_sdk_when_it_is_imported() -> None:
    """Control: the same probe on ``tern.mcp.server`` sees ``mcp`` and its web stack."""
    found = _web_stack(_modules_after_import("tern.mcp.server"))
    assert {"mcp", "mcp.server", "starlette", "uvicorn"} <= found


# ---- no inet bind/listen during a session -------------------------------------------------------

_INET = (socket.AF_INET, socket.AF_INET6)


@pytest.fixture
def inet_guard(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Patch ``socket.socket.bind``/``listen`` to record and refuse every AF_INET/AF_INET6 use
    (AF_UNIX, e.g. the event loop's self-pipe, is untouched). Returns the attempts, so a refusal
    the code under test swallowed is still seen."""
    attempts: list[tuple[str, int]] = []
    real_bind = socket.socket.bind
    real_listen = socket.socket.listen

    def bind(self: socket.socket, address: Any) -> None:
        if self.family in _INET:
            attempts.append(("bind", int(self.family)))
            raise OSError(errno.EACCES, "inet bind refused by the test")
        real_bind(self, address)

    def listen(self: socket.socket, *args: int) -> None:
        if self.family in _INET:
            attempts.append(("listen", int(self.family)))
            raise OSError(errno.EACCES, "inet listen refused by the test")
        real_listen(self, *args)

    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(socket.socket, "listen", listen)
    return attempts


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[tuple[Registry, AuditLog]]:
    """The built-ins in a real Registry (real Engine, fresh AuditLog under ``TERN_HOME``, an
    empty index, the synthetic FakeBridge)."""
    Store.open(tmp_path / "index.db").close()  # create the empty schema
    store = Store.open(tmp_path / "index.db", readonly=True)
    audit = AuditLog.open(audit_path(paths.resolve()), clock=time.time)
    bridge = FakeBridge(FIXTURE)
    engine = Engine(
        audit=audit,
        index=store,
        bridge=bridge,
        clock=time.time,
        known_recipients=DenyAllRecipients(),
    )
    runtime = ToolRuntime(
        audit=audit,
        index=store,
        search=SearchService(store, None, mode="fts"),
        bridge=bridge,
        config=TernConfig(),
        log=logging.getLogger("tern.tools.test_transport"),
        clock=time.time,
        engine=engine,
    )
    yield Registry(builtin_tools(), runtime=runtime), audit
    audit.close()
    store.close()


@pytest.mark.asyncio
async def test_full_session_binds_and_listens_on_no_inet_socket(
    inet_guard: list[tuple[str, int]], registry: tuple[Registry, AuditLog]
) -> None:
    reg, audit = registry
    server = build_server(RegistryBackend(reg, CALLER))
    async with Client(server, client_info=Implementation(name=CLIENT, version="0")) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        status = await c.call_tool("index_status", {})
        search = await c.call_tool("search_messages", {"query": "lunch"})
    assert {"index_status", "search_messages"} <= names
    assert status.is_error is False and search.is_error is False
    assert [(r.caller, r.tool) for r in reversed(audit.list(limit=None))] == [
        (CALLER, "index_status"),
        (CALLER, "search_messages"),
    ]
    assert inet_guard == []


def test_inet_guard_catches_a_bind_and_a_listen(inet_guard: list[tuple[str, int]]) -> None:
    """Control: under the patch a TCP listen and a bind are refused and recorded; AF_UNIX is
    not affected."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(OSError, match="inet listen refused"):
            s.listen()
        with pytest.raises(OSError, match="inet bind refused"):
            s.bind(("127.0.0.1", 0))
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s6:
        with pytest.raises(OSError, match="inet bind refused"):
            s6.bind(("::1", 0))
    a, b = socket.socketpair(socket.AF_UNIX)
    a.close()
    b.close()
    assert inet_guard == [
        ("listen", int(socket.AF_INET)),
        ("bind", int(socket.AF_INET)),
        ("bind", int(socket.AF_INET6)),
    ]


@pytest.mark.asyncio
async def test_inet_guard_catches_an_asyncio_server(inet_guard: list[tuple[str, int]]) -> None:
    """Control: the path a web transport takes (``loop.create_server`` under uvicorn) hits the
    patch."""

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.close()

    with pytest.raises(OSError):
        await asyncio.start_server(handle, host="127.0.0.1", port=0)
    assert inet_guard and inet_guard[0] == ("bind", int(socket.AF_INET))


# ---- run_stdio: the stdio transport works and holds no inet socket ------------------------------

_STDIO_SERVER = """
import asyncio, os
from tern.mcp.server import ToolOutcome, run_stdio
from tern.tools.registry import describe_registry

class Echo:
    def describe(self):
        return describe_registry().describe()

    async def call(self, tool, args, *, client_name):
        out = {"tool": tool, "args": dict(args), "client": client_name, "pid": os.getpid()}
        # search_messages is untrusted: listed without an output schema, its result is text
        return ToolOutcome(output=out, untrusted_output=True)

asyncio.run(run_stdio(Echo()))
"""


def _inet_held(pid: int) -> set[str]:
    return socket_inodes(pid) & inet_inodes(pid)


@pytest.mark.asyncio
async def test_run_stdio_serves_over_stdio_and_holds_no_inet_socket() -> None:
    """``run_stdio`` in a child process, driven by the SDK's stdio client: listing and a call
    work, and the child holds no TCP/UDP socket while the session is open. (The backend is a
    stub: this checks the transport; ``tern mcp`` over ternd is part B's step 9.)"""
    params = StdioServerParameters(
        command=sys.executable, args=["-c", _STDIO_SERVER], env=dict(os.environ)
    )
    async with Client(params, client_info=Implementation(name=CLIENT, version="0")) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        result = await c.call_tool("search_messages", {"n": 1})
        assert result.is_error is False and len(result.content) == 1
        item = result.content[0]
        assert item.type == "text"
        # untrusted: the JSON arrives inside one untrusted_email envelope (plan 08a step 2a)
        assert item.text.startswith("<untrusted_email>\n") and item.text.endswith(
            "\n</untrusted_email>"
        )
        out = json.loads(
            item.text.removeprefix("<untrusted_email>\n").removesuffix("\n</untrusted_email>")
        )
        pid = out["pid"]
        assert isinstance(pid, int) and pid != os.getpid()
        assert b"run_stdio" in Path(f"/proc/{pid}/cmdline").read_bytes()  # the child itself
        held = _inet_held(pid)
    assert names == {m.name for m in describe_registry().describe()}
    assert {k: out[k] for k in ("tool", "args", "client")} == {
        "tool": "search_messages",
        "args": {"n": 1},
        "client": CLIENT,
    }
    assert held == set()


def test_inet_held_sees_a_bound_udp_socket() -> None:
    """Control: ``_inet_held`` finds an inet socket this process holds."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        assert _inet_held(os.getpid()) != set()
