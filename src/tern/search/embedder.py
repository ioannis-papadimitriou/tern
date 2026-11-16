"""Embedders: the ``Embedder`` protocol and an OpenAI-compatible ``EmbeddingClient`` (plan 05).

``EmbeddingClient`` POSTs ``{"model": <id>, "input": [...]}`` to ``<embed_base_url>/embeddings``
and nothing else: no runtime-specific endpoint (#15 amendment). The base URL passes
``tern.llm.egress.check_url`` at construction (#9), and the httpx client never reads proxy
settings from the environment and never follows redirects.

Every response is validated: one vector per input, ordered by ``index`` (each index 0..n-1
exactly once), all of the same nonzero length, finite floats, a finite nonzero norm (``l2_norm``,
which ``vec`` reuses before it normalises). A response that fails any check, or whose body cannot
be decoded (``decoding_error``), raises ``EmbedderError``; a request that cannot complete
(connection, timeout, a server that is up without the model or capacity) raises
``EmbedderUnavailable``. Error messages carry a reason code, the exception type and the model id
only, never input text.

HTTP status classes: 2xx is parsed; 404, 408, 429 and 5xx raise ``EmbedderUnavailable`` (the
model is not loaded yet or the server is busy: retry with back-off), and so does a 400 whose JSON
error says no model is loaded or the model is not found (``tern.llm.client.model_not_loaded``:
LM Studio with JIT loading off and the model unloaded; reason ``http_400 model_not_loaded``);
any other status, including any other 400 and a redirect (never followed), raises
``EmbedderError``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from types import TracebackType
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from tern.config import TernConfig
from tern.llm.client import model_not_loaded
from tern.llm.egress import check_url

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({404, 408, 429})


class EmbedderError(RuntimeError):
    """The embeddings server answered with something unusable (malformed or refused)."""


class EmbedderUnavailable(RuntimeError):
    """The embeddings server could not be reached or cannot serve the model right now."""


class InvalidVector(ValueError):
    """A vector cannot be normalised. ``reason`` is ``non_finite`` (a NaN or infinite component),
    ``zero_norm`` or ``non_finite_norm`` (finite components whose norm overflows a float)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def l2_norm(vector: Sequence[float]) -> float:
    """The Euclidean norm of ``vector``; raises ``InvalidVector`` unless it is finite and nonzero.

    ``math.hypot`` scales internally, so tiny components (1e-200) do not underflow to a zero
    norm and large ones (1e200) do not overflow; only a norm beyond the float range is refused.
    """
    if not all(math.isfinite(x) for x in vector):
        raise InvalidVector("non_finite")
    norm = math.hypot(*vector)
    if norm == 0.0:
        raise InvalidVector("zero_norm")
    if not math.isfinite(norm):
        raise InvalidVector("non_finite_norm")
    return norm


def l2_normalize(vector: Sequence[float]) -> list[float]:
    """``vector`` scaled to unit length (checked by ``l2_norm`` first)."""
    norm = l2_norm(vector)
    return [x / norm for x in vector]


class Embedder(Protocol):
    """What search and the embed queue need from an embedder."""

    model_id: str

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in input order."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """The vector of a search query (with any configured query prefix)."""
        ...


class _Item(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    index: int
    embedding: list[float]


class _Response(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    data: list[_Item]


class EmbeddingClient:
    """An OpenAI-compatible ``/v1/embeddings`` client (loopback unless config allows a remote)."""

    def __init__(
        self, cfg: TernConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if cfg.embed_model is None:
            raise ValueError("embed_model is not configured (embedding is disabled)")
        base = check_url(cfg.embed_base_url, cfg)
        self.model_id: str = cfg.embed_model
        self._endpoint = base.rstrip("/") + "/embeddings"
        self._query_prefix = cfg.embed_query_prefix
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=cfg.embed_timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> EmbeddingClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _error(self, reason: str) -> EmbedderError:
        return EmbedderError(f"{reason} (model={self.model_id})")

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if isinstance(texts, str):
            raise TypeError("embed_documents takes a sequence of texts, not one str")
        if not texts:
            return []
        payload = {"model": self.model_id, "input": list(texts)}
        try:
            response = await self._client.post(self._endpoint, json=payload)
        except httpx.TransportError as exc:
            raise EmbedderUnavailable(f"{type(exc).__name__} (model={self.model_id})") from exc
        except httpx.DecodingError as exc:  # e.g. Content-Encoding: gzip on a non-gzip body
            raise self._error("decoding_error") from exc
        except httpx.RequestError as exc:  # any other non-transport request failure
            raise self._error(f"request_error {type(exc).__name__}") from exc
        status = response.status_code
        if status in _RETRYABLE_STATUS or status >= 500:
            raise EmbedderUnavailable(f"http_{status} (model={self.model_id})")
        if status == 400 and model_not_loaded(response.content):
            raise EmbedderUnavailable(f"http_400 model_not_loaded (model={self.model_id})")
        if not 200 <= status < 300:
            raise self._error(f"http_{status}")
        vectors = self._parse(response, len(texts))
        log.debug("embedded n=%d dim=%d", len(vectors), len(vectors[0]))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        (vector,) = await self.embed_documents([self._query_prefix + text])
        return vector

    def _parse(self, response: httpx.Response, n: int) -> list[list[float]]:
        try:
            body = response.json()
        except ValueError as exc:  # json.JSONDecodeError and UnicodeDecodeError
            raise self._error("invalid_json") from exc
        try:
            parsed = _Response.model_validate(body)
        except ValidationError as exc:
            raise self._error("invalid_shape") from exc
        if len(parsed.data) != n:
            raise self._error(f"count_mismatch expected={n} got={len(parsed.data)}")
        by_index = sorted(parsed.data, key=lambda item: item.index)
        if [item.index for item in by_index] != list(range(n)):
            raise self._error("bad_indices")
        vectors = [item.embedding for item in by_index]
        dim = len(vectors[0])
        if dim == 0:
            raise self._error("empty_vector")
        if any(len(v) != dim for v in vectors):
            raise self._error("dim_mismatch")
        for vector in vectors:
            try:
                l2_norm(vector)
            except InvalidVector as exc:
                raise self._error(exc.reason) from exc
        return vectors
