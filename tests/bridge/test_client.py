"""Daemon→extension requests across reconnects (plan 02 step 5, H13, A-24, B-M12).

The 30 s wait is scaled down (``wait_s``) so the tests run in well under a second; the
behaviour, not the constant, is under test. ``test_default_wait_is_30_s`` pins the constant.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from time import monotonic
from typing import Any

import pytest
import pytest_asyncio

from tern import paths
from tern.bridge.client import WAIT_FOR_EXTENSION_S, ExtensionLink, Peer, ProtocolViolation
from tern.bridge.protocol import BridgeError, Empty, FoldersListParams, Request
from tern.bridge.server import BridgeServer
from tern.daemon import uds
from tern.testing.bridge import NMHOST, TB_BIN, FakeProcessInfo, connect

WAIT = 0.4


@pytest_asyncio.fixture
async def setup() -> AsyncIterator[tuple[ExtensionLink, Path]]:
    tp = paths.resolve()
    uds.prepare_runtime_dir(tp.run)
    link = ExtensionLink(wait_s=WAIT, request_timeout_s=2.0)
    server = BridgeServer(
        process_info=FakeProcessInfo(), thunderbird_bin=TB_BIN, nmhost_path=NMHOST, link=link
    )
    srv = await uds.start(server, tp.socket)
    yield link, tp.socket
    srv.close()
    await server.close_all()
    await srv.wait_closed()


def test_default_wait_is_30_s() -> None:
    assert WAIT_FOR_EXTENSION_S == 30.0
    assert ExtensionLink().wait_s == 30.0


@pytest.mark.asyncio
async def test_request_waits_for_a_late_connect(setup: tuple[ExtensionLink, Path]) -> None:
    link, sock = setup
    pending = asyncio.create_task(link.request("bridge.ping", Empty()))
    await asyncio.sleep(WAIT / 4)
    assert not pending.done()
    ext = await connect(sock, "extension")
    assert await asyncio.wait_for(pending, 1) == Empty()
    await ext.close()


@pytest.mark.asyncio
async def test_no_connect_fails_unavailable_after_the_wait(
    setup: tuple[ExtensionLink, Path],
) -> None:
    link, _ = setup
    t0 = monotonic()
    with pytest.raises(BridgeError) as exc:
        await link.request("bridge.ping", Empty())
    assert exc.value.name == "unavailable"
    assert WAIT * 0.9 <= monotonic() - t0 < WAIT * 3


@pytest.mark.asyncio
async def test_in_flight_request_fails_at_once_when_the_port_drops(
    setup: tuple[ExtensionLink, Path],
) -> None:
    link, sock = setup
    got_request = asyncio.Event()

    async def hang(peer: Peer, req: Request) -> Any:
        got_request.set()
        await asyncio.sleep(3600)

    ext = await connect(sock, "extension", serve=hang)
    pending = asyncio.create_task(link.request("bridge.ping", Empty()))
    await asyncio.wait_for(got_request.wait(), 1)
    t0 = monotonic()
    await ext.close()
    with pytest.raises(BridgeError) as exc:
        await pending
    assert exc.value.name == "unavailable"
    assert monotonic() - t0 < WAIT / 2  # did not wait for a reconnect


@pytest.mark.asyncio
async def test_no_silent_retry_after_failure(setup: tuple[ExtensionLink, Path]) -> None:
    link, sock = setup
    calls = 0

    async def count(peer: Peer, req: Request) -> Any:
        nonlocal calls
        calls += 1
        raise BridgeError("tb_error", "TB refused", tb_code="X")

    ext = await connect(sock, "extension", serve=count)
    with pytest.raises(BridgeError) as exc:
        await link.request("bridge.ping", Empty())
    assert (exc.value.name, exc.value.tb_code, calls) == ("tb_error", "X", 1)
    await ext.close()


@pytest.mark.asyncio
async def test_reconnect_then_request_succeeds(setup: tuple[ExtensionLink, Path]) -> None:
    link, sock = setup
    first = await connect(sock, "extension")
    await first.close()
    while link.active is not None:  # the daemon notices the drop
        await asyncio.sleep(0.01)
    pending = asyncio.create_task(link.request("bridge.ping", Empty()))
    await asyncio.sleep(WAIT / 4)
    second = await connect(sock, "extension")
    assert await asyncio.wait_for(pending, 1) == Empty()
    await second.close()


@pytest.mark.asyncio
async def test_malformed_extension_result_is_a_protocol_violation(
    setup: tuple[ExtensionLink, Path],
) -> None:
    link, sock = setup

    async def wrong(peer: Peer, req: Request) -> Any:
        return [{"folder_id": "f", "unexpected": 1}]

    ext = await connect(sock, "extension", serve=wrong)
    with pytest.raises(ProtocolViolation):
        await link.request("mail.folders.list", FoldersListParams(account_id="a"))
    await ext.close()


@pytest.mark.asyncio
async def test_only_daemon_to_extension_methods(setup: tuple[ExtensionLink, Path]) -> None:
    link, _ = setup
    with pytest.raises(ValueError):
        await link.request("search.query", Empty())


@pytest.mark.asyncio
async def test_handler_bug_logs_type_at_error_and_traceback_at_debug(
    setup: tuple[ExtensionLink, Path], caplog: pytest.LogCaptureFixture
) -> None:
    # Plan 02 review (deferred to plan 03): the exception text and traceback can carry mail
    # content, so ERROR carries the type only; the traceback is DEBUG.
    link, sock = setup

    async def buggy(peer: Peer, req: Request) -> Any:
        raise KeyError("SENTINEL-SUBJECT-handler-bug")

    ext = await connect(sock, "extension", serve=buggy)
    caplog.set_level(logging.DEBUG, logger="tern.bridge")
    with pytest.raises(BridgeError) as exc_info:
        await link.request("mail.folders.list", FoldersListParams(account_id="a"))
    await ext.close()
    assert exc_info.value.name == "unavailable"
    failed = [r for r in caplog.records if r.getMessage() == "bridge handler failed"]
    assert len(failed) == 1
    err = failed[0]
    assert err.levelno == logging.ERROR and err.exc_info is None
    assert getattr(err, "error", None) == "KeyError"
    assert "SENTINEL" not in logging.Formatter().format(err)
    tb = [r for r in caplog.records if r.getMessage() == "bridge handler traceback"]
    assert len(tb) == 1 and tb[0].levelno == logging.DEBUG and tb[0].exc_info is not None
