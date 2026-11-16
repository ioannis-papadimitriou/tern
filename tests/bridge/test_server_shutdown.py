"""``BridgeServer.shutdown`` (plan 05 step 7): a connection accepted before the listener closed
but handed over only after ``close_all`` ran must not keep ``wait_closed`` (and ternd's
shutdown) waiting for its 10 s hello timeout. ``close_all`` alone still takes new connections.
"""

from __future__ import annotations

import asyncio

import pytest

from tern import paths
from tern.bridge.server import HELLO_TIMEOUT_S, BridgeServer
from tern.daemon import uds
from tern.testing.bridge import NMHOST, TB_BIN, FakeProcessInfo, connect

PROMPT_S = 2.0
assert PROMPT_S < HELLO_TIMEOUT_S


def make_server() -> BridgeServer:
    return BridgeServer(process_info=FakeProcessInfo(), thunderbird_bin=TB_BIN, nmhost_path=NMHOST)


@pytest.mark.asyncio
async def test_a_connection_handed_over_after_shutdown_is_closed_at_once() -> None:
    """The late hand-over is forced: the accepted connection's ``on_connect`` waits until
    ``shutdown()`` has run before it calls ``handle_connection``."""
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    server = make_server()
    accepted = asyncio.Event()
    shut = asyncio.Event()
    handled = asyncio.Event()

    async def on_connect(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        accepted.set()
        await shut.wait()
        await server.handle_connection(r, w, 0)
        handled.set()

    srv = await asyncio.start_unix_server(on_connect, path=str(tp.socket))
    reader, writer = await asyncio.open_unix_connection(str(tp.socket))
    await asyncio.wait_for(accepted.wait(), PROMPT_S)
    srv.close()
    await server.shutdown()
    shut.set()
    await asyncio.wait_for(handled.wait(), PROMPT_S)  # not after the hello timeout
    assert await asyncio.wait_for(reader.read(), PROMPT_S) == b""  # closed by the server
    await asyncio.wait_for(srv.wait_closed(), PROMPT_S)
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_close_all_alone_keeps_taking_connections() -> None:
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    server = make_server()
    srv = await uds.start(server, tp.socket)
    await server.close_all()
    c = await asyncio.wait_for(connect(tp.socket, "cli"), PROMPT_S)  # hello answered
    await c.close()
    srv.close()
    await server.shutdown()
    await asyncio.wait_for(srv.wait_closed(), PROMPT_S)
