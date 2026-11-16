"""Framing and chunking (plan 02 step 2)."""

from __future__ import annotations

import asyncio
import json

import pytest

from tern.bridge import framing as f
from tern.bridge.protocol import Chunk


def _reader(data: bytes, *, eof: bool = True) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    if eof:
        r.feed_eof()
    return r


@pytest.mark.asyncio
async def test_round_trip() -> None:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "bridge.ping", "params": {"t": "ü€"}}
    r = _reader(f.frame(f.encode(msg)) + f.frame(f.encode(msg)))
    assert f.decode(await f.read_frame(r) or b"") == msg
    assert f.decode(await f.read_frame(r) or b"") == msg
    assert await f.read_frame(r) is None  # clean EOF between frames


@pytest.mark.asyncio
async def test_length_is_little_endian_u32() -> None:
    assert f.frame(b"abc")[:4] == (3).to_bytes(4, "little")


@pytest.mark.asyncio
async def test_partial_reads_are_assembled() -> None:
    data = f.frame(f.encode({"x": "y" * 1000}))
    r = asyncio.StreamReader()
    task = asyncio.create_task(f.read_frame(r))
    for i in range(0, len(data), 7):
        r.feed_data(data[i : i + 7])
        await asyncio.sleep(0)
    r.feed_eof()
    assert f.decode(await task or b"") == {"x": "y" * 1000}


@pytest.mark.asyncio
async def test_oversize_inbound_frame_is_an_error_not_truncated() -> None:
    r = _reader((101).to_bytes(4, "little") + b"x" * 101)
    with pytest.raises(f.FrameError, match="exceeds"):
        await f.read_frame(r, max_len=100)


@pytest.mark.parametrize("data", [b"\x05\x00", (10).to_bytes(4, "little") + b"short"])
@pytest.mark.asyncio
async def test_truncated_frame_is_an_error(data: bytes) -> None:
    with pytest.raises(f.FrameError):
        await f.read_frame(_reader(data))


def test_non_json_payload_is_an_error() -> None:
    with pytest.raises(f.FrameError):
        f.decode(b"\xff\xfe not json")


def test_small_message_is_not_chunked() -> None:
    payload = f.encode({"a": 1})
    assert f.split_for_extension(payload, "c1") == [payload]


def test_large_message_is_chunked_below_the_limit_and_reassembles() -> None:
    big = {"jsonrpc": "2.0", "id": 9, "result": {"raw_b64": "Zm9v" * (640 * 1024), "u": "ü" * 99}}
    payload = f.encode(big)
    assert len(payload) > 2.5 * 1024 * 1024
    frames = f.split_for_extension(payload, "c1")
    assert len(frames) > 1
    assert all(len(fr) < f.HOST_TO_EXT_MAX for fr in frames)
    r = f.Reassembler()
    out = None
    for fr in frames:
        msg = json.loads(fr)
        assert msg["method"] == "bridge.chunk"
        out = r.feed(Chunk.model_validate(msg["params"]))
    assert out == payload


def test_message_at_exactly_the_limit_is_chunked() -> None:
    payload = b"x" * f.HOST_TO_EXT_MAX
    assert len(f.split_for_extension(payload, "c")) == 2


def _chunks(payload: bytes, cid: str) -> list[Chunk]:
    return [
        Chunk.model_validate(json.loads(fr)["params"]) for fr in f.split_for_extension(payload, cid)
    ]


def test_out_of_order_chunk_is_an_error() -> None:
    cs = _chunks(b"y" * f.HOST_TO_EXT_MAX * 2, "c")
    r = f.Reassembler()
    r.feed(cs[0])
    with pytest.raises(f.ChunkError, match="expected chunk 1"):
        r.feed(cs[2])


def test_reassembler_starts_clean_after_an_error() -> None:
    cs = _chunks(b"y" * f.HOST_TO_EXT_MAX * 2, "c")
    r = f.Reassembler()
    r.feed(cs[0])
    with pytest.raises(f.ChunkError):
        r.feed(cs[2])
    payload = b"z" * f.HOST_TO_EXT_MAX * 2
    out = None
    for c in _chunks(payload, "d"):
        out = r.feed(c)
    assert out == payload


def test_duplicate_chunk_is_an_error() -> None:
    cs = _chunks(b"y" * f.HOST_TO_EXT_MAX * 2, "c")
    r = f.Reassembler()
    r.feed(cs[0])
    with pytest.raises(f.ChunkError):
        r.feed(cs[0])


def test_interleaved_chunk_ids_are_an_error() -> None:
    a = _chunks(b"a" * f.HOST_TO_EXT_MAX, "a")
    b = _chunks(b"b" * f.HOST_TO_EXT_MAX, "b")
    r = f.Reassembler()
    r.feed(a[0])
    with pytest.raises(f.ChunkError, match="new chunk_id"):
        r.feed(b[0])


def test_sequence_must_start_at_zero() -> None:
    cs = _chunks(b"y" * f.HOST_TO_EXT_MAX * 2, "c")
    with pytest.raises(f.ChunkError):
        f.Reassembler().feed(cs[1])


def test_reassembly_is_size_capped() -> None:
    cs = _chunks(b"y" * f.HOST_TO_EXT_MAX * 2, "c")
    r = f.Reassembler(max_len=f.CHUNK_RAW_BYTES + 1)
    r.feed(cs[0])
    with pytest.raises(f.ChunkError, match="exceeds"):
        r.feed(cs[1])
