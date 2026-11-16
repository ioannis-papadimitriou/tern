"""Plan 04 step 9: the extension hello binds the index to one TB profile (architecture §3).

A real ``BridgeServer`` over a Unix socket, bound to a real ``Store``: the first extension's
``profile_id`` is stored; another one is refused with ``profile_mismatch`` (-32004) and never
attached; the same one is admitted again.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from tern import paths
from tern.bridge import framing
from tern.bridge.client import ExtensionLink, Peer
from tern.bridge.protocol import PROTOCOL, BridgeError, Hello
from tern.bridge.server import BridgeServer
from tern.daemon import uds
from tern.index.store import Store, index_path
from tern.testing.bridge import NMHOST, TB_BIN, FakeProcessInfo, connect, hello_params


@dataclass
class Bound:
    sock: Path
    server: BridgeServer
    store: Store
    attached: list[int]


@pytest_asyncio.fixture
async def bound() -> AsyncIterator[Bound]:
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    store = Store.open(index_path(tp))
    server = BridgeServer(
        process_info=FakeProcessInfo(),
        thunderbird_bin=TB_BIN,
        nmhost_path=NMHOST,
        link=ExtensionLink(wait_s=0.2),
        liveness_ping_s=0.5,
        bind_profile=store.bind_profile,
    )
    attached: list[int] = []
    server.link.on_attach.append(lambda: attached.append(1))
    srv = await uds.start(server, tp.socket)
    yield Bound(tp.socket, server, store, attached)
    srv.close()
    await server.close_all()
    await srv.wait_closed()
    store.close()


async def raw_hello(sock: Path, hello: dict[str, Any]) -> dict[str, Any]:
    """Send one hello frame and return the daemon's reply as it is on the wire."""
    reader, writer = await asyncio.open_unix_connection(str(sock))
    try:
        req = {"jsonrpc": "2.0", "id": 1, "method": "hello", "params": hello}
        writer.write(framing.frame(framing.encode(req)))
        await writer.drain()
        payload = await asyncio.wait_for(framing.read_frame(reader), 5)
        assert payload is not None
        reply = framing.decode(payload)
        assert isinstance(reply, dict)
        return reply
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_first_extension_binds_the_profile(bound: Bound) -> None:
    pid = uuid.uuid4()
    assert bound.store.profile_id() is None
    c = await connect(bound.sock, "extension", hello=hello_params("extension", profile_id=str(pid)))
    assert bound.store.profile_id() == pid
    assert bound.server.link.active is not None and bound.attached == [1]
    await c.close()


@pytest.mark.asyncio
async def test_another_profile_is_refused_with_32004_and_not_attached(bound: Bound) -> None:
    first = str(uuid.uuid4())
    c = await connect(bound.sock, "extension", hello=hello_params("extension", profile_id=first))
    await c.close()
    for _ in range(100):  # the server notices the close in its own task
        if bound.server.link.active is None:
            break
        await asyncio.sleep(0.01)
    assert bound.server.link.active is None

    reply = await raw_hello(bound.sock, hello_params("extension", profile_id=str(uuid.uuid4())))
    assert "result" not in reply
    assert reply["error"]["code"] == -32004
    assert reply["error"]["data"]["name"] == "profile_mismatch"
    assert bound.server.link.active is None
    assert bound.attached == [1]
    assert str(bound.store.profile_id()) == first

    again = await connect(
        bound.sock, "extension", hello=hello_params("extension", profile_id=first)
    )
    assert again.hello_result["role"] == "daemon"
    assert bound.server.link.active is not None and bound.attached == [1, 1]
    await again.close()


@pytest.mark.asyncio
async def test_another_profile_never_replaces_the_live_extension(bound: Bound) -> None:
    """The binding is checked before the liveness ping: a live extension of the bound profile
    stays attached, and the other profile gets profile_mismatch, not extension_already_connected."""
    first = str(uuid.uuid4())
    c = await connect(bound.sock, "extension", hello=hello_params("extension", profile_id=first))
    live = bound.server.link.active
    with pytest.raises(BridgeError) as exc:
        await connect(
            bound.sock, "extension", hello=hello_params("extension", profile_id=str(uuid.uuid4()))
        )
    assert exc.value.name == "profile_mismatch"
    assert bound.server.link.active is live and bound.attached == [1]
    await c.close()


@pytest.mark.asyncio
async def test_an_extension_hello_without_profile_id_is_refused_not_bound(bound: Bound) -> None:
    """``Hello`` validation already requires the id; the binding re-checks it with a real error
    (an ``assert`` would vanish under ``python -O`` and bind the string 'None')."""
    hello = Hello.model_construct(
        protocol=PROTOCOL,
        minor=0,
        role="extension",
        version="test",
        capabilities=[],
        profile_id=None,
    )
    # Called directly: the check sits behind Hello's validator, unreachable over the wire. The
    # peer is never touched before the binding.
    with pytest.raises(BridgeError) as exc:
        await bound.server._admit_extension(cast(Peer, object()), hello, 1, 1)
    assert exc.value.name == "invalid_params"
    assert bound.store.profile_id() is None
    assert bound.attached == []


class AttachBug(RuntimeError):
    """An on_attach callback bug (injected; carries no content)."""


@pytest.mark.asyncio
async def test_a_raising_attach_callback_drops_the_connection(bound: Bound) -> None:
    """The success reply has gone out when the callbacks run; one that raises (a bug) propagates
    out of the hello handler, whose connection handler closes the peer, so the link reports no
    live extension (it never stays attached with no reader running for it)."""

    def bug() -> None:
        raise AttachBug()

    bound.server.link.on_attach.append(bug)
    c = await connect(
        bound.sock, "extension", hello=hello_params("extension", profile_id=str(uuid.uuid4()))
    )
    assert c.hello_result["role"] == "daemon"
    for _ in range(100):  # the server's hello handler runs in its own task
        if bound.server.link.active is None:
            break
        await asyncio.sleep(0.01)
    assert bound.server.link.active is None
    await asyncio.wait_for(c.task, 5)  # the daemon closed the connection
