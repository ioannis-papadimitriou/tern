"""LM Studio runtime shim (plan 08a step 1, A1; #15 amendment 2026-11-14).

LM Studio specifics stay here, out of the generic ``tern.llm.client``. ``LMStudioShim`` is
selected by ``llm_runtime = "lmstudio"`` (the only value in v1). Its one call,
``runtime_info()``, gives the provenance fields an eval run or ``tern doctor`` needs:

- ``runtime_version`` from ``lms version`` (the CLI commit, as S3 recorded it) and ``engine``
  from ``lms runtime ls`` (the selected llama.cpp engine), through an injectable runner;
- ``quant`` and ``context_length`` from ``GET <root>/api/v0/models``, where ``<root>`` is
  ``chat_base_url`` minus its ``/v1`` suffix. Both URLs pass the egress guard at construction.

Every field must be determined: anything missing or ambiguous raises ``RuntimeInfoUnavailable``,
never a placeholder (decision #21). Messages carry a reason code, exit codes and the model id,
never the CLI's output. Callers are plan 09's eval and plan 15's doctor; the chat path never
calls this module.

The only exception callers handle is ``RuntimeInfoUnavailable``: a ``chat_base_url`` without the
``/v1`` suffix is reported as one too (``base_url_without_v1``), at construction. Egress refusals
stay ``EgressDenied``. The ``lms`` timeout (30 s) and the ``/api/v0`` timeout (10 s) are fixed on
purpose, not config: both are short local calls made only by the eval and ``tern doctor``.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from tern.config import TernConfig
from tern.llm.egress import check_url

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# S3's parser (spikes/s3/lms.py): the S3 runs recorded the CLI commit this way.
_VERSION_RE = re.compile(
    r"(?:CLI commit|commit|version)\s*[:=]?\s*v?([0-9A-Za-z][0-9A-Za-z.\-+]*)", re.IGNORECASE
)
_ENGINE_RE = re.compile(r"(llama\.cpp[\w.\-]*@[\w.\-]+)")
_SELECTED_RE = re.compile(r"selected|✓|\*|\(active\)", re.IGNORECASE)
_CLI_TIMEOUT_S = 30.0
_API_TIMEOUT_S = 10.0


class RuntimeInfoUnavailable(RuntimeError):
    """A runtime-info field could not be determined (no placeholder is ever returned)."""


class RuntimeInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    runtime: Literal["lmstudio"] = "lmstudio"
    runtime_version: str = Field(min_length=1)
    engine: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    quant: str = Field(min_length=1)
    context_length: int = Field(gt=0)


def default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd), capture_output=True, text=True, check=False, timeout=_CLI_TIMEOUT_S
    )


def api_root(chat_base_url: str) -> str:
    """``http://127.0.0.1:1234/v1`` -> ``http://127.0.0.1:1234``; ``ValueError`` without ``/v1``."""
    base = chat_base_url.rstrip("/")
    if not base.endswith("/v1"):
        raise ValueError("chat_base_url must end with /v1 for the LM Studio shim")
    return base.removesuffix("/v1")


class LMStudioShim:
    """LM Studio specifics: ``lms`` CLI and the ``/api/v0`` REST API (loopback-guarded)."""

    def __init__(
        self,
        cfg: TernConfig,
        *,
        runner: Runner = default_runner,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if cfg.llm_runtime != "lmstudio":
            raise ValueError(f"llm_runtime {cfg.llm_runtime!r} is not lmstudio")
        self.model_id: str = cfg.chat_model
        base = check_url(cfg.chat_base_url, cfg)
        try:
            root = api_root(base)
        except ValueError as exc:
            raise self._unavailable("base_url_without_v1") from exc
        self._root = check_url(root, cfg)
        self._cli = str(cfg.lmstudio_cli)
        self._runner = runner
        self._client = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False, timeout=_API_TIMEOUT_S
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> LMStudioShim:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _unavailable(self, reason: str) -> RuntimeInfoUnavailable:
        return RuntimeInfoUnavailable(f"{reason} (model={self.model_id})")

    async def runtime_info(self) -> RuntimeInfo:
        version_out = await self._lms("version")
        engine_out = await self._lms("runtime", "ls")
        quant, context = await self._model_entry()
        return RuntimeInfo(
            runtime_version=self._parse_version(version_out),
            engine=self._parse_engine(engine_out),
            model_id=self.model_id,
            quant=quant,
            context_length=context,
        )

    async def _lms(self, *args: str) -> str:
        cmd = [self._cli, *args]
        label = "lms_" + "_".join(args)
        try:
            done = await asyncio.to_thread(self._runner, cmd)
        except FileNotFoundError as exc:
            raise self._unavailable(f"{label} not_found") from exc
        except subprocess.TimeoutExpired as exc:
            raise self._unavailable(f"{label} timeout") from exc
        except OSError as exc:
            raise self._unavailable(f"{label} {type(exc).__name__}") from exc
        if done.returncode != 0:
            raise self._unavailable(f"{label} exit_{done.returncode}")
        return _ANSI_RE.sub("", done.stdout or "")

    def _parse_version(self, out: str) -> str:
        m = _VERSION_RE.search(out)
        if not m:
            raise self._unavailable("lms_version no_version")
        return m.group(1)

    def _parse_engine(self, out: str) -> str:
        engines = set(_ENGINE_RE.findall(out))
        if not engines:
            raise self._unavailable("lms_runtime_ls no_engine")
        selected = {
            e
            for line in out.splitlines()
            if _SELECTED_RE.search(line)
            for e in _ENGINE_RE.findall(line)
        }
        if len(selected) == 1:
            return selected.pop()
        if not selected and len(engines) == 1:
            return engines.pop()
        raise self._unavailable(f"lms_runtime_ls ambiguous_engine n={len(engines)}")

    async def _model_entry(self) -> tuple[str, int]:
        try:
            response = await self._client.get(self._root + "/api/v0/models")
        except httpx.TransportError as exc:
            raise self._unavailable(f"api_models transport_error {type(exc).__name__}") from exc
        except httpx.RequestError as exc:
            raise self._unavailable(f"api_models request_error {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise self._unavailable(f"api_models http_{response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise self._unavailable("api_models invalid_json") from exc
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise self._unavailable("api_models no_data")
        hits = [m for m in data if isinstance(m, dict) and m.get("id") == self.model_id]
        if len(hits) != 1:
            raise self._unavailable(f"api_models entries={len(hits)}")
        (entry,) = hits
        quant = entry.get("quantization")
        context = entry.get("loaded_context_length")
        if not isinstance(quant, str) or not quant:
            raise self._unavailable("api_models no_quantization")
        if type(context) is not int or context <= 0:
            raise self._unavailable("api_models no_loaded_context_length")
        return quant, context
