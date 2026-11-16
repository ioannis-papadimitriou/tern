"""Plan 05 step 2: ``EmbeddingClient`` (httpx.MockTransport, no socket) and ``StubEmbedder``."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from collections.abc import Callable

import httpx
import pytest

from tern.config import TernConfig
from tern.llm.egress import EgressDenied
from tern.search.embedder import (
    Embedder,
    EmbedderError,
    EmbedderUnavailable,
    EmbeddingClient,
    InvalidVector,
    l2_norm,
    l2_normalize,
)
from tern.testing.stub_embedder import StubEmbedder

MODEL = "fake-embed-1"
SECRET_TEXT = "PLANTED-SENTINEL-TEXT-zq81"

Handler = Callable[[httpx.Request], httpx.Response]


def _cfg(**overrides: object) -> TernConfig:
    values: dict[str, object] = {"embed_model": MODEL}
    values.update(overrides)
    return TernConfig.model_validate(values)


def _ok_handler(dim: int = 3, *, shuffle: bool = False) -> tuple[Handler, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        inputs = json.loads(request.content)["input"]
        data = [
            {"object": "embedding", "index": i, "embedding": [float(i + 1)] + [0.5] * (dim - 1)}
            for i in range(len(inputs))
        ]
        if shuffle:
            data.reverse()
        return httpx.Response(200, json={"object": "list", "data": data, "model": MODEL})

    return handle, seen


def _client(handler: Handler, **overrides: object) -> EmbeddingClient:
    return EmbeddingClient(_cfg(**overrides), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_posts_model_and_input_to_base_embeddings_and_orders_by_index() -> None:
    handler, seen = _ok_handler(shuffle=True)
    async with _client(handler) as client:
        assert client.model_id == MODEL
        vectors = await client.embed_documents(["a", "b", "c"])
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0]
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1:47831/v1/embeddings"
    assert json.loads(request.content) == {"model": MODEL, "input": ["a", "b", "c"]}


@pytest.mark.asyncio
async def test_base_url_trailing_slash() -> None:
    handler, seen = _ok_handler()
    async with _client(handler, embed_base_url="http://localhost:8080/v1/") as client:
        await client.embed_documents(["a"])
    assert str(seen[0].url) == "http://localhost:8080/v1/embeddings"


@pytest.mark.asyncio
async def test_query_prefix_applies_to_queries_only() -> None:
    handler, seen = _ok_handler()
    async with _client(handler, embed_query_prefix="Query: ") as client:
        await client.embed_query("hello")
        await client.embed_documents(["hello"])
    assert json.loads(seen[0].content)["input"] == ["Query: hello"]
    assert json.loads(seen[1].content)["input"] == ["hello"]


@pytest.mark.asyncio
async def test_empty_input_makes_no_request() -> None:
    handler, seen = _ok_handler()
    async with _client(handler) as client:
        assert await client.embed_documents([]) == []
    assert seen == []


@pytest.mark.asyncio
async def test_a_single_str_is_refused() -> None:
    handler, _ = _ok_handler()
    async with _client(handler) as client:
        with pytest.raises(TypeError):
            await client.embed_documents("abc")


def test_disabled_model_is_refused() -> None:
    with pytest.raises(ValueError, match="embed_model"):
        EmbeddingClient(TernConfig(embed_model=None))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1.evil.test/v1",
        "http://localhost@evil.test/v1",
        "http://[::ffff:8.8.8.8]/v1",
    ],
)
def test_non_loopback_base_url_is_refused_at_construction(url: str) -> None:
    with pytest.raises(EgressDenied):
        EmbeddingClient(_cfg(embed_base_url=url))


@pytest.mark.asyncio
async def test_remote_base_url_allowed_by_exact_config() -> None:
    remote = "https://llm.example.com/v1"
    handler, seen = _ok_handler()
    cfg = _cfg(embed_base_url=remote, allow_remote_llm=True, remote_llm_urls=[remote])
    async with EmbeddingClient(cfg, transport=httpx.MockTransport(handler)) as client:
        await client.embed_documents(["a"])
    assert str(seen[0].url) == "https://llm.example.com/v1/embeddings"


@pytest.mark.asyncio
async def test_redirect_is_not_followed() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "127.0.0.1":
            return httpx.Response(302, headers={"Location": "http://evil.test/v1/embeddings"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    async with _client(handle) as client:
        with pytest.raises(EmbedderError, match="http_302"):
            await client.embed_documents(["a"])
    assert [r.url.host for r in seen] == ["127.0.0.1"]


@pytest.mark.asyncio
async def test_env_proxy_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no injected transport httpx builds its own; none of them may use an env proxy."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, "http://proxy.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    built: list[object] = []
    handled: list[httpx.Request] = []

    class RecordingTransport(httpx.AsyncBaseTransport):
        def __init__(self, *args: object, proxy: object = None, **kwargs: object) -> None:
            built.append(proxy)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            handled.append(request)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    monkeypatch.setattr(httpx._client, "AsyncHTTPTransport", RecordingTransport)
    async with EmbeddingClient(_cfg()) as client:
        assert await client.embed_documents(["a"]) == [[1.0, 2.0]]
    assert built == [None]  # only the direct transport, no proxy mount
    assert [str(r.url) for r in handled] == ["http://127.0.0.1:47831/v1/embeddings"]


def _respond(body: object, status: int = 200) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    return handle


def _items(*vectors: object) -> dict[str, object]:
    return {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (_respond(_items([1.0, 0.0])), "count_mismatch"),  # 1 vector for 2 inputs
        (_respond(_items([1.0, 0.0], [1.0, 0.0], [1.0, 0.0])), "count_mismatch"),
        (_respond(_items([1.0, 0.0], [1.0, 0.0, 0.0])), "dim_mismatch"),
        (_respond(_items([], [])), "empty_vector"),
        (_respond(_items([1.0, 0.0], [0.0, 0.0])), "zero_norm"),
        (
            _respond(
                b'{"data": [{"index": 0, "embedding": [NaN, 1.0]},'
                b' {"index": 1, "embedding": [1.0, 1.0]}]}'
            ),
            "non_finite",
        ),
        (
            _respond(
                b'{"data": [{"index": 0, "embedding": [Infinity, 1.0]},'
                b' {"index": 1, "embedding": [1.0, 1.0]}]}'
            ),
            "non_finite",
        ),
        (
            _respond(
                {"data": [{"index": 0, "embedding": [1.0]}, {"index": 0, "embedding": [1.0]}]}
            ),
            "bad_indices",
        ),
        (
            _respond(
                {"data": [{"index": 1, "embedding": [1.0]}, {"index": 2, "embedding": [1.0]}]}
            ),
            "bad_indices",
        ),
        (_respond(_items([1.0, True], [1.0, 1.0])), "invalid_shape"),
        (_respond(_items("AAAA", "AAAA")), "invalid_shape"),  # base64, never requested
        (_respond({"error": "nope"}), "invalid_shape"),
        (_respond(b"not json"), "invalid_json"),
        (_respond({"error": "bad request"}, status=400), "http_400"),
        (_respond({"error": "unauthorized"}, status=401), "http_401"),
    ],
)
async def test_malformed_responses_raise_embedder_error(handler: Handler, reason: str) -> None:
    async with _client(handler) as client:
        with pytest.raises(EmbedderError) as info:
            await client.embed_documents([SECRET_TEXT, SECRET_TEXT + "2"])
    message = str(info.value)
    assert reason in message
    assert MODEL in message
    assert SECRET_TEXT not in message


def _raise(exc: Exception) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        raise exc

    return handle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (_raise(httpx.ConnectError("refused")), "ConnectError"),
        (_raise(httpx.ReadTimeout("slow")), "ReadTimeout"),
        (_raise(httpx.RemoteProtocolError("eof")), "RemoteProtocolError"),
        (_respond({"error": "loading"}, status=503), "http_503"),
        (_respond({"error": "boom"}, status=500), "http_500"),
        (_respond({"error": "model not found"}, status=404), "http_404"),
        (_respond({"error": "busy"}, status=429), "http_429"),
    ],
)
async def test_unreachable_or_not_ready_raises_unavailable(handler: Handler, reason: str) -> None:
    async with _client(handler) as client:
        with pytest.raises(EmbedderUnavailable) as info:
            await client.embed_documents([SECRET_TEXT])
    message = str(info.value)
    assert reason in message
    assert MODEL in message
    assert SECRET_TEXT not in message


#: LM Studio's answer to /v1/embeddings when no model is loaded and JIT loading is off.
LMSTUDIO_NO_MODEL = (
    "No models loaded. Please load a model in the developer page or use the 'lms load' command."
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"error": LMSTUDIO_NO_MODEL},
        {"error": {"message": LMSTUDIO_NO_MODEL, "type": "invalid_request_error"}},
        {"error": {"message": f"x {SECRET_TEXT}", "code": "model_not_found"}},
        {"error": {"message": f'Model "{MODEL}.v1" not found {SECRET_TEXT}'}},
        {"error": f"model not found {SECRET_TEXT}"},
    ],
    ids=["lmstudio", "lmstudio_object", "openai_code", "model_x_not_found", "model_not_found"],
)
async def test_a_400_saying_no_model_is_loaded_is_unavailable(body: object) -> None:
    # LM Studio with JIT loading off answers 400 while the model is unloaded: the queue must
    # back off and resume after a reload, not stop for good. Only a reason code leaves.
    async with _client(_respond(body, status=400)) as client:
        with pytest.raises(EmbedderUnavailable) as info:
            await client.embed_documents([SECRET_TEXT])
    message = str(info.value)
    assert "http_400 model_not_loaded" in message and MODEL in message
    assert SECRET_TEXT not in message and "Please load" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"error": f"context length exceeded {SECRET_TEXT}"},
        {"error": {"message": "input too long", "code": "context_length_exceeded"}},
        {"error": {"message": 5, "code": 7}},
        {"detail": LMSTUDIO_NO_MODEL},  # another shape: not the OpenAI error object
        [LMSTUDIO_NO_MODEL],
        LMSTUDIO_NO_MODEL.encode(),  # not JSON
        b"\xff\xfe not utf-8",
        json.dumps({"error": LMSTUDIO_NO_MODEL, "pad": "x" * 20_000}).encode(),  # over the cap
    ],
    ids=["other", "other_code", "odd_types", "detail", "list", "plain", "bytes", "huge"],
)
async def test_any_other_400_stays_an_embedder_error(body: object) -> None:
    async with _client(_respond(body, status=400)) as client:
        with pytest.raises(EmbedderError) as info:
            await client.embed_documents([SECRET_TEXT])
    message = str(info.value)
    assert "http_400" in message and "model_not_loaded" not in message
    assert SECRET_TEXT not in message


def _undecodable_gzip(request: httpx.Request) -> httpx.Response:
    # a streamed body, so httpx decodes it in the client (as it would off a socket)
    return httpx.Response(
        200,
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        stream=httpx.ByteStream(b'{"data": [{"index": 0, "embedding": [1.0]}]}'),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (_undecodable_gzip, "decoding_error"),
        (_raise(httpx.TooManyRedirects("loop")), "request_error TooManyRedirects"),
    ],
)
async def test_non_transport_request_errors_are_classified(handler: Handler, reason: str) -> None:
    async with _client(handler) as client:
        with pytest.raises(EmbedderError) as info:
            await client.embed_documents([SECRET_TEXT])
    message = str(info.value)
    assert reason in message
    assert MODEL in message
    assert SECRET_TEXT not in message
    assert isinstance(info.value.__cause__, httpx.RequestError)


@pytest.mark.asyncio
async def test_overflowing_norm_is_refused() -> None:
    # every component is finite, but the norm (2e308) is not
    handler = _respond(_items([1e308] * 4, [1.0] * 4))
    async with _client(handler) as client:
        with pytest.raises(EmbedderError, match="non_finite_norm"):
            await client.embed_documents(["a", "b"])


@pytest.mark.asyncio
@pytest.mark.parametrize("scale", [1e-200, 1e200])
async def test_tiny_and_huge_components_are_accepted(scale: float) -> None:
    # sum(x*x) underflows to 0 at 1e-200 and overflows to inf at 1e200; hypot does neither
    vector = [scale, -scale, scale, scale]
    handler = _respond(_items(vector, [1.0, 0.0, 0.0, 0.0]))
    async with _client(handler) as client:
        vectors = await client.embed_documents(["a", "b"])
    assert vectors[0] == vector


@pytest.mark.parametrize("scale", [1e-200, 1e200, 1.0, 5e-324])
def test_l2_normalize_never_yields_a_zero_vector(scale: float) -> None:
    unit = l2_normalize([scale] * 4)
    assert unit == pytest.approx([0.5] * 4, rel=1e-12)
    assert l2_norm([scale] * 4) == pytest.approx(2 * scale, rel=1e-12)


@pytest.mark.parametrize(
    ("vector", "reason"),
    [
        ([0.0, 0.0], "zero_norm"),
        ([], "zero_norm"),
        ([math.nan, 1.0], "non_finite"),
        ([1.0, math.inf], "non_finite"),
        ([1.7e308, 1.7e308], "non_finite_norm"),
    ],
)
def test_l2_norm_rejects_unusable_vectors(vector: list[float], reason: str) -> None:
    with pytest.raises(InvalidVector) as info:
        l2_norm(vector)
    assert info.value.reason == reason
    with pytest.raises(InvalidVector):
        l2_normalize(vector)


@pytest.mark.asyncio
async def test_client_and_stub_satisfy_the_protocol() -> None:
    handler, _ = _ok_handler()
    async with _client(handler) as client:
        embedders: list[Embedder] = [StubEmbedder(), client]
        assert [e.model_id for e in embedders] == ["stub-fh-64-0", MODEL]
        assert [len(await e.embed_query("x")) for e in embedders] == [64, 3]


# --- StubEmbedder ---------------------------------------------------------------------------

STUB_TEXTS = ["Acme invoice March", "συνάντηση Πέμπτη", "", "!!!", "INV-0042 due?"]

_STUB_SCRIPT = """
import asyncio, json, sys
from tern.testing.stub_embedder import StubEmbedder
texts = json.loads(sys.argv[1])
async def main():
    out = {}
    for dim, seed in ((64, 0), (16, 7)):
        e = StubEmbedder(dim=dim, seed=seed)
        out[e.model_id] = await e.embed_documents(texts)
    print(json.dumps(out))
asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_stub_is_deterministic_across_processes() -> None:
    outputs = []
    for hashseed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=hashseed)
        proc = subprocess.run(
            [sys.executable, "-c", _STUB_SCRIPT, json.dumps(STUB_TEXTS)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        outputs.append(json.loads(proc.stdout))
    assert outputs[0] == outputs[1]
    here = {
        StubEmbedder(64, 0).model_id: await StubEmbedder(64, 0).embed_documents(STUB_TEXTS),
        StubEmbedder(16, 7).model_id: await StubEmbedder(16, 7).embed_documents(STUB_TEXTS),
    }
    assert outputs[0] == here


def test_stub_model_id_and_dim() -> None:
    stub = StubEmbedder(dim=128, seed=3)
    assert stub.model_id == "stub-fh-128-3"
    assert len(stub.vector("hello world")) == 128


def test_stub_folds_and_casefolds() -> None:
    stub = StubEmbedder()
    assert stub.vector("Συνάντηση ΠΈΜΠΤΗ") == stub.vector("συναντηση πεμπτη")
    assert stub.vector("Acme, invoice!") == stub.vector("acme invoice")


def test_stub_shared_words_are_closer_than_unrelated_text() -> None:
    stub = StubEmbedder()

    def cos(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        return dot / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))

    q = stub.vector("Acme invoice March")
    assert cos(q, stub.vector("invoice from Acme for March")) > cos(
        q, stub.vector("dinner on Thursday with friends")
    )


def test_stub_seed_changes_vectors() -> None:
    assert StubEmbedder(seed=0).vector("acme invoice") != StubEmbedder(seed=1).vector(
        "acme invoice"
    )


def test_stub_never_returns_a_zero_vector() -> None:
    stub = StubEmbedder(dim=8)
    fixed = stub.empty_vector()
    assert any(fixed)
    assert stub.vector("") == fixed
    assert stub.vector("  !!! ... ") == fixed
    # two tokens on the same bucket with opposite signs cancel out
    single = {f"t{i}": stub.vector(f"t{i}") for i in range(200)}
    pair = next((a, b) for a in single for b in single if single[a] == [-x for x in single[b]])
    assert stub.vector(" ".join(pair)) == fixed


@pytest.mark.asyncio
async def test_stub_query_and_documents_agree() -> None:
    stub = StubEmbedder()
    (doc,) = await stub.embed_documents(["acme invoice"])
    assert await stub.embed_query("acme invoice") == doc
    with pytest.raises(TypeError):
        await stub.embed_documents("acme")


@pytest.mark.parametrize(("dim", "seed"), [(0, 0), (8, -1), (8, 2**64)])
def test_stub_rejects_bad_parameters(dim: int, seed: int) -> None:
    with pytest.raises(ValueError):
        StubEmbedder(dim=dim, seed=seed)
