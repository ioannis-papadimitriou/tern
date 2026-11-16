"""``tern-nmhost``: the native-messaging host TB spawns; relays frames between TB and ``ternd``.

TB starts it with ``[manifest_path, extension_id]`` and its own environment (S1 NOTES F11), so
``TERN_HOME`` reaches it. Behaviour (plan 02 step 4, decisions #3 amendment of 2026-11-13):

- It **never closes the TB port because the daemon is gone**: an open port keeps the MV3
  background alive (F12). It starts ``ternd`` (detached, so it outlives TB) when it cannot
  connect, and reconnects with backoff (0.25 s doubling to 5 s) indefinitely.
- While not connected, or connected but before the extension's ``hello`` succeeded on this
  connection, extension requests other than ``hello`` get ``unavailable`` at once; extension
  notifications and responses are dropped (counted in the log). The 30 s wait for an extension
  lives in the daemon, not here.
- Extension requests in flight when the daemon connection drops get ``unavailable`` at once.
- After (re)connecting, if the extension has to say ``hello`` again, the host sends it a
  ``bridge.reconnected`` notification.
- Daemon→extension messages of 512 KiB or more go out as ``bridge.chunk`` frames.
- The extension's ``hello`` goes out with ``profile_id`` replaced by an id derived from TB's
  profile directory (``tern.bridge.tbprofile``) when the host can find it, so reinstalling the
  add-on (which wipes the extension's stored id) does not unbind the index.
- It exits when TB closes the port (EOF on stdin): status 0. It exits with status 1 and one
  ERROR log line (no traceback) when it cannot reach ``ternd`` for a reason other than "not
  there yet", or when the extension sends an unreadable frame (oversize or truncated).

Logs go to stderr (TB's browser console); they carry counts and reasons, never mail content.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from tern import log as tern_log
from tern import paths
from tern.bridge import framing, tbprofile
from tern.bridge.protocol import (
    BridgeError,
    ErrorResponse,
    Request,
    Response,
    dump,
    parse_message,
)

log = logging.getLogger("tern.nmhost")

BACKOFF_START_S = 0.25
BACKOFF_MAX_S = 5.0

ExtOut = Callable[[bytes], Awaitable[None]]  # writes one frame payload to the extension
SpawnDaemon = Callable[[], None]


class HostExit(RuntimeError):
    """The host must end with status 1; the cause is already logged at ERROR."""


def spawn_ternd(tp: paths.TernPaths) -> subprocess.Popen[bytes]:
    """Start ``ternd`` in its own session (it outlives TB); stderr goes to its log file."""
    tp.state.mkdir(parents=True, exist_ok=True)
    with open(tp.state / "ternd.log", "ab") as errlog:
        return subprocess.Popen(
            [sys.executable, "-m", "tern.daemon.main"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errlog,
            start_new_session=True,
            close_fds=True,
        )


class Host:
    def __init__(
        self,
        ext_in: asyncio.StreamReader,
        ext_out: ExtOut,
        sock_path: Path,
        spawn_daemon: SpawnDaemon,
        *,
        backoff_start_s: float = BACKOFF_START_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        profile_id: str | None = None,
    ) -> None:
        self._ext_in = ext_in
        self._ext_out = ext_out
        self._sock = sock_path
        self._spawn = spawn_daemon
        self._backoff_start = backoff_start_s
        self._backoff_max = backoff_max_s
        self._profile_id = profile_id
        self._daemon: asyncio.StreamWriter | None = None
        self._gate_open = False  # the extension's hello succeeded on the current connection
        self._hello_id: Any = None  # id of the extension hello in flight on this connection
        self._ext_needs_hello = False  # tell the extension to re-hello when we connect
        self._out_lock = asyncio.Lock()
        self._chunk_ids = itertools.count(1)
        self._inflight: set[Any] = set()  # extension request ids sent on this connection
        self.dropped = 0

    # -- output to the extension --------------------------------------------------------------

    async def _to_ext(self, payload: bytes) -> None:
        frames = framing.split_for_extension(payload, f"h{os.getpid()}-{next(self._chunk_ids)}")
        async with self._out_lock:  # a chunk sequence is never interleaved with other frames
            for fr in frames:
                await self._ext_out(fr)

    async def _to_ext_obj(self, obj: dict[str, Any]) -> None:
        await self._to_ext(framing.encode(obj))

    async def _unavailable(self, rid: Any, why: str) -> None:
        err = BridgeError("unavailable", why).to_rpc()
        await self._to_ext_obj({"jsonrpc": "2.0", "id": rid, "error": dump(err)})

    # -- main loops ---------------------------------------------------------------------------

    async def run(self) -> None:
        """Run until the extension closes the port.

        The connector retries only "socket absent/refused". Any other failure (socket permissions,
        ternd cannot be started, ...) ends the host with an error instead of leaving it alive
        and answering ``unavailable`` forever; the port then closes and the extension's
        reconnect alarm takes over.
        """
        connector = asyncio.create_task(self._connect_loop())
        ext = asyncio.create_task(self._ext_loop())
        try:
            done, _ = await asyncio.wait({connector, ext}, return_when=asyncio.FIRST_COMPLETED)
            if connector in done:  # _connect_loop never returns: it failed
                exc = connector.exception()
                log.error("ternd connector failed; exiting", extra={"error": type(exc).__name__})
                raise HostExit("tern-nmhost cannot reach ternd") from exc
            try:
                ext.result()
            except framing.FrameError as exc:
                # Oversize (> INBOUND_MAX) or truncated: the stream position is lost, so the
                # port cannot be read any further. The message names lengths only.
                log.error("unreadable extension frame; exiting", extra={"reason": str(exc)})
                raise HostExit("tern-nmhost got an unreadable extension frame") from exc
            log.info("port closed by Thunderbird; exiting", extra={"dropped": self.dropped})
        finally:
            for t in (connector, ext):
                t.cancel()
            await asyncio.gather(connector, ext, return_exceptions=True)
            if self._daemon is not None:
                self._daemon.close()

    async def _ext_loop(self) -> None:
        while True:
            payload = await framing.read_frame(self._ext_in)
            if payload is None:
                return
            try:
                msg = parse_message(framing.decode(payload))
            except (framing.FrameError, ValueError) as exc:
                self.dropped += 1
                log.warning("malformed extension message dropped", extra={"reason": str(exc)})
                continue
            if isinstance(msg, Request) and msg.method == "hello":
                payload = self._with_profile_id(payload)
            if self._daemon is not None and self._gate_open:
                if isinstance(msg, Request):
                    self._inflight.add(msg.id)
                await self._to_daemon(payload)
                continue
            await self._ext_frame_while_gated(msg, payload)

    def _with_profile_id(self, payload: bytes) -> bytes:
        """The hello payload with ``params.profile_id`` set to the profile directory's id."""
        if self._profile_id is None:
            return payload
        obj = framing.decode(payload)
        params = obj.get("params") if isinstance(obj, dict) else None
        if not isinstance(params, dict):
            return payload
        params["profile_id"] = self._profile_id
        return framing.encode(obj)

    async def _ext_frame_while_gated(self, msg: object, payload: bytes) -> None:
        if isinstance(msg, Request):
            if msg.method == "hello" and self._daemon is not None and self._hello_id is None:
                self._hello_id = msg.id
                self._inflight.add(msg.id)
                await self._to_daemon(payload)
                return
            if msg.method == "hello":
                self._ext_needs_hello = True
            await self._unavailable(msg.id, "ternd is not connected yet")
            return
        self.dropped += 1
        log.info("extension message dropped: ternd not connected", extra={"dropped": self.dropped})

    async def _to_daemon(self, payload: bytes) -> None:
        writer = self._daemon
        if writer is None:
            return
        writer.write(framing.frame(payload))
        try:
            await writer.drain()
        except ConnectionError:
            log.info("ternd connection lost while sending")

    async def _connect_loop(self) -> None:
        delay = self._backoff_start
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(str(self._sock))
            except (FileNotFoundError, ConnectionRefusedError):
                self._spawn()
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._backoff_max)
                continue
            delay = self._backoff_start
            self._daemon, self._gate_open, self._hello_id = writer, False, None
            log.info("connected to ternd")
            if self._ext_needs_hello:
                self._ext_needs_hello = False
                await self._to_ext_obj(
                    {"jsonrpc": "2.0", "method": "bridge.reconnected", "params": {}}
                )
            try:
                await self._daemon_loop(reader)
            finally:
                writer.close()
                was_open = self._gate_open or self._hello_id is not None
                self._daemon, self._gate_open, self._hello_id = None, False, None
                self._ext_needs_hello = self._ext_needs_hello or was_open
                inflight, self._inflight = self._inflight, set()
                log.info("ternd connection lost; reconnecting", extra={"failed": len(inflight)})
            for rid in inflight:
                await self._unavailable(rid, "ternd connection dropped")

    async def _daemon_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                payload = await framing.read_frame(reader)
                if payload is None:
                    return
                self._track_reply(payload)
                await self._to_ext(payload)
        except (framing.FrameError, ConnectionError) as exc:
            log.warning("ternd stream error", extra={"reason": type(exc).__name__})

    def _track_reply(self, payload: bytes) -> None:
        """Note replies to extension requests; open the gate on a successful hello reply."""
        try:
            msg = parse_message(framing.decode(payload))
        except (framing.FrameError, ValueError) as exc:
            log.warning("malformed message from ternd", extra={"reason": str(exc)})
            return
        if not isinstance(msg, Response | ErrorResponse):
            return
        self._inflight.discard(msg.id)
        if self._hello_id is not None and msg.id == self._hello_id:
            self._hello_id = None
            self._gate_open = isinstance(msg, Response)


async def _stdio() -> tuple[asyncio.StreamReader, ExtOut]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=framing.INBOUND_MAX)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)

    async def out(payload: bytes) -> None:
        writer.write(framing.frame(payload))
        await writer.drain()

    return reader, out


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    tern_log.setup(sys.stderr)
    log.info("tern-nmhost started", extra={"argc": len(args)})
    tp = paths.resolve()
    profile_id = tbprofile.parent_profile_id()
    log.info("tb profile", extra={"found": profile_id is not None})
    children: list[subprocess.Popen[bytes]] = []

    def spawn() -> None:
        children[:] = [c for c in children if c.poll() is None]  # reap exited ones
        if not children:
            log.info("starting ternd")
            children.append(spawn_ternd(tp))

    async def run() -> None:
        ext_in, ext_out = await _stdio()
        await Host(ext_in, ext_out, tp.socket, spawn, profile_id=profile_id).run()

    try:
        asyncio.run(run())
    except HostExit:
        return 1  # already logged at ERROR
    return 0


if __name__ == "__main__":
    sys.exit(main())
