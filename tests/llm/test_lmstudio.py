"""Plan 08a step 1: ``LMStudioShim.runtime_info()`` (injected ``lms`` runner, FakeLLM transport).

The ``lms`` outputs below are synthetic, in the shape S3's parser handled (the CLI commit and a
selected llama.cpp engine). No real CLI and no socket.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from tern.config import TernConfig
from tern.llm.egress import EgressDenied
from tern.llm.lmstudio import (
    LMStudioShim,
    RuntimeInfo,
    RuntimeInfoUnavailable,
    api_root,
)
from tern.testing.fake_llm import FAKE_CONTEXT_LENGTH, FAKE_QUANT, FakeLLM, ScriptStep

MODEL = "fake-chat-1"
CLI = Path("/opt/fake-lmstudio/lms")
SENTINEL = "SENTINEL-lms-output-k2"
VERSION_OUT = "\x1b[1mlms - LM Studio CLI\x1b[0m\nCLI commit: 43d4b26\n"
ENGINE_OUT = (
    "LLM ENGINE                                          SELECTED\n"
    "llama.cpp-linux-x86_64-avx2@2.73.0\n"
    "llama.cpp-linux-x86_64-nvidia-cuda13-avx2@2.73.0    ✓\n"
)


def _cfg(**overrides: object) -> TernConfig:
    values: dict[str, object] = {"chat_model": MODEL, "lmstudio_cli": CLI}
    values.update(overrides)
    return TernConfig.model_validate(values)


class FakeRunner:
    def __init__(self, outputs: dict[str, tuple[int, str]] | None = None) -> None:
        self.outputs = outputs or {"version": (0, VERSION_OUT), "runtime ls": (0, ENGINE_OUT)}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        code, out = self.outputs[" ".join(cmd[1:])]
        return subprocess.CompletedProcess(list(cmd), code, stdout=out, stderr=SENTINEL)


def _fake() -> FakeLLM:
    return FakeLLM.from_steps(ScriptStep(content="unused"), model_id=MODEL)


def _shim(runner: FakeRunner, transport: httpx.AsyncBaseTransport, **cfg: object) -> LMStudioShim:
    return LMStudioShim(_cfg(**cfg), runner=runner, transport=transport)


@pytest.mark.asyncio
async def test_runtime_info_from_lms_and_api_v0() -> None:
    runner = FakeRunner()
    async with _shim(runner, _fake().transport) as shim:
        info = await shim.runtime_info()
    assert info == RuntimeInfo(
        runtime="lmstudio",
        runtime_version="43d4b26",
        engine="llama.cpp-linux-x86_64-nvidia-cuda13-avx2@2.73.0",
        model_id=MODEL,
        quant=FAKE_QUANT,
        context_length=FAKE_CONTEXT_LENGTH,
    )
    assert runner.calls == [[str(CLI), "version"], [str(CLI), "runtime", "ls"]]


@pytest.mark.asyncio
async def test_api_v0_is_requested_at_the_root_without_v1() -> None:
    seen: list[str] = []
    fake = _fake()

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return fake.transport.handle_request(request)

    async with _shim(FakeRunner(), httpx.MockTransport(handle)) as shim:
        await shim.runtime_info()
    assert seen == ["http://127.0.0.1:47831/api/v0/models"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outputs", "reason"),
    [
        ({"version": (1, SENTINEL), "runtime ls": (0, ENGINE_OUT)}, "lms_version exit_1"),
        ({"version": (0, VERSION_OUT), "runtime ls": (2, SENTINEL)}, "lms_runtime_ls exit_2"),
        (
            {"version": (0, f"no number {SENTINEL[:3]}"), "runtime ls": (0, ENGINE_OUT)},
            "no_version",
        ),
        ({"version": (0, VERSION_OUT), "runtime ls": (0, "no engines")}, "no_engine"),
        (
            {
                "version": (0, VERSION_OUT),
                "runtime ls": (0, "llama.cpp-a@1.0\nllama.cpp-b@1.0\n"),
            },
            "ambiguous_engine",
        ),
    ],
)
async def test_failing_or_unparseable_lms_raises(
    outputs: dict[str, tuple[int, str]], reason: str
) -> None:
    async with _shim(FakeRunner(outputs), _fake().transport) as shim:
        with pytest.raises(RuntimeInfoUnavailable, match=reason) as info:
            await shim.runtime_info()
    assert SENTINEL not in str(info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (FileNotFoundError(2, "No such file"), "not_found"),
        (subprocess.TimeoutExpired(["lms"], 30), "timeout"),
        (PermissionError(13, "denied"), "PermissionError"),
    ],
)
async def test_lms_runner_errors_raise(error: Exception, reason: str) -> None:
    def runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise error

    shim = LMStudioShim(_cfg(), runner=runner, transport=_fake().transport)
    async with shim:
        with pytest.raises(RuntimeInfoUnavailable, match=reason):
            await shim.runtime_info()


def _models(*entries: object, status: int = 200) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v0/models"
        return httpx.Response(status, json={"object": "list", "data": list(entries)})

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport", "reason"),
    [
        (_models({"id": "other", "quantization": "Q4", "loaded_context_length": 8}), "entries=0"),
        (
            _models(
                {"id": MODEL, "quantization": "Q4", "loaded_context_length": 8},
                {"id": MODEL, "quantization": "Q8", "loaded_context_length": 8},
            ),
            "entries=2",
        ),
        (_models({"id": MODEL, "loaded_context_length": 8}), "no_quantization"),
        # A model that is listed but not loaded has no loaded_context_length.
        (_models({"id": MODEL, "quantization": "Q4", "state": "not-loaded"}), "no_loaded_context"),
        (
            _models({"id": MODEL, "quantization": "Q4", "loaded_context_length": 0}),
            "no_loaded_context",
        ),
        (
            _models({"id": MODEL, "quantization": "Q4", "loaded_context_length": True}),
            "no_loaded_context",
        ),
        (_models(status=500), "http_500"),
        (httpx.MockTransport(lambda r: httpx.Response(200, content=b"not json")), "invalid_json"),
        (httpx.MockTransport(lambda r: httpx.Response(200, json=[1])), "no_data"),
    ],
)
async def test_missing_model_fields_raise(transport: httpx.MockTransport, reason: str) -> None:
    async with _shim(FakeRunner(), transport) as shim:
        with pytest.raises(RuntimeInfoUnavailable, match=reason):
            await shim.runtime_info()


@pytest.mark.asyncio
async def test_unreachable_api_raises() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with _shim(FakeRunner(), httpx.MockTransport(handle)) as shim:
        with pytest.raises(RuntimeInfoUnavailable, match="ConnectError"):
            await shim.runtime_info()


def test_non_loopback_base_url_is_denied() -> None:
    with pytest.raises(EgressDenied):
        LMStudioShim(_cfg(chat_base_url="http://192.168.1.9:1234/v1"), runner=FakeRunner())


def test_remote_root_must_be_allowed_too() -> None:
    remote = "https://llm.example.com/v1"
    cfg = _cfg(chat_base_url=remote, allow_remote_llm=True, remote_llm_urls=[remote])
    with pytest.raises(EgressDenied):
        LMStudioShim(cfg, runner=FakeRunner())
    both = _cfg(
        chat_base_url=remote,
        allow_remote_llm=True,
        remote_llm_urls=[remote, "https://llm.example.com"],
    )
    LMStudioShim(both, runner=FakeRunner())


@pytest.mark.parametrize(
    ("base", "root"),
    [
        ("http://127.0.0.1:1234/v1", "http://127.0.0.1:1234"),
        ("http://127.0.0.1:1234/v1/", "http://127.0.0.1:1234"),
        ("http://localhost:9/lm/v1", "http://localhost:9/lm"),
    ],
)
def test_api_root(base: str, root: str) -> None:
    assert api_root(base) == root


def test_base_without_v1_is_refused() -> None:
    with pytest.raises(ValueError, match="/v1"):
        api_root("http://127.0.0.1:1234/api")


def test_shim_reports_a_base_without_v1_as_runtime_info_unavailable() -> None:
    with pytest.raises(RuntimeInfoUnavailable, match="base_url_without_v1"):
        LMStudioShim(_cfg(chat_base_url="http://127.0.0.1:1234/api"), runner=FakeRunner())
