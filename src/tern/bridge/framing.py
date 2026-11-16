"""Frames: u32 little-endian length + UTF-8 JSON (architecture §3), and host→extension chunking.

Limits (S1, `spikes/s1/REPORT.md` `framesize`, NOTES F10):
- TB reads at most 1 MiB per host→extension message (`MAX_READ`); S1 measured 1 048 512 bytes
  ok and 1 048 640 failed. tern keeps host→extension frames below ``HOST_TO_EXT_MAX`` (512 KiB,
  architecture §3); ``tern-nmhost`` splits larger messages into ``bridge.chunk`` notifications.
- Extension→host has no TB cap (``MAX_WRITE`` = 4 GiB). tern caps every inbound frame at
  ``INBOUND_MAX``: a 25 MB `getRaw` is ~33.4 MB as base64.
An oversize frame is an error, never truncated.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from typing import Any

from tern.bridge.protocol import Chunk

HOST_TO_EXT_MAX = 512 * 1024
INBOUND_MAX = 64 * 1024 * 1024
CHUNK_RAW_BYTES = 256 * 1024  # base64 → ~341 KiB of data per chunk frame, well below the max

_LEN = struct.Struct("<I")


class FrameError(ConnectionError):
    """A frame that cannot be read: oversize, truncated, or not JSON."""


class ChunkError(ValueError):
    """A ``bridge.chunk`` sequence that cannot be reassembled."""


def encode(obj: Any) -> bytes:
    """Serialize one message to its frame payload (no length prefix)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def frame(payload: bytes) -> bytes:
    """Length-prefix ``payload``."""
    return _LEN.pack(len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader, max_len: int = INBOUND_MAX) -> bytes | None:
    """Read one frame payload. ``None`` on a clean EOF between frames."""
    try:
        header = await reader.readexactly(_LEN.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise FrameError("connection closed inside a frame header") from None
    (length,) = _LEN.unpack(header)
    if length > max_len:
        raise FrameError(f"frame of {length} bytes exceeds the {max_len}-byte limit")
    try:
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        raise FrameError(f"connection closed after {length}-byte frame header") from None


def decode(payload: bytes) -> Any:
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameError(f"frame is not UTF-8 JSON ({type(exc).__name__})") from None


def split_for_extension(payload: bytes, chunk_id: str) -> list[bytes]:
    """Payloads to send host→extension for one message, each below ``HOST_TO_EXT_MAX``.

    A message that already fits is returned as is; a larger one becomes consecutive
    ``bridge.chunk`` notifications carrying base64 slices of ``payload``.
    """
    if len(payload) < HOST_TO_EXT_MAX:
        return [payload]
    out: list[bytes] = []
    slices = range(0, len(payload), CHUNK_RAW_BYTES)
    for seq, start in enumerate(slices):
        piece = payload[start : start + CHUNK_RAW_BYTES]
        params = Chunk(
            chunk_id=chunk_id,
            seq=seq,
            last=start + CHUNK_RAW_BYTES >= len(payload),
            data_b64=base64.b64encode(piece).decode("ascii"),
        )
        msg = encode({"jsonrpc": "2.0", "method": "bridge.chunk", "params": params.model_dump()})
        if len(msg) >= HOST_TO_EXT_MAX:  # cannot happen with the constants above
            raise AssertionError("chunk frame exceeds HOST_TO_EXT_MAX")
        out.append(msg)
    return out


class Reassembler:
    """Rebuilds a message from ``bridge.chunk`` params (the reference for `bridge.ts`)."""

    def __init__(self, max_len: int = INBOUND_MAX) -> None:
        self._max = max_len
        self._id: str | None = None
        self._next = 0
        self._parts: list[bytes] = []
        self._size = 0

    def feed(self, chunk: Chunk) -> bytes | None:
        """Add one chunk; returns the full payload after the ``last`` one.

        On any error the partial message is discarded, so the next ``chunk_id`` starts clean.
        """
        try:
            return self._add(chunk)
        except ChunkError:
            self._id, self._next, self._parts, self._size = None, 0, [], 0
            raise

    def _add(self, chunk: Chunk) -> bytes | None:
        if self._id is None:
            if chunk.seq != 0:
                raise ChunkError(f"chunk sequence starts at {chunk.seq}, not 0")
            self._id = chunk.chunk_id
        elif chunk.chunk_id != self._id:
            raise ChunkError("a new chunk_id started before the previous message completed")
        if chunk.seq != self._next:
            raise ChunkError(f"expected chunk {self._next}, got {chunk.seq}")
        try:
            piece = base64.b64decode(chunk.data_b64, validate=True)
        except ValueError:
            raise ChunkError("chunk data is not base64") from None
        self._size += len(piece)
        if self._size > self._max:
            raise ChunkError(f"reassembled message exceeds {self._max} bytes")
        self._parts.append(piece)
        self._next += 1
        if not chunk.last:
            return None
        payload = b"".join(self._parts)
        self._id, self._next, self._parts, self._size = None, 0, [], 0
        return payload
