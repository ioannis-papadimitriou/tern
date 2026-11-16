"""Every bridge test runs with TERN_HOME in a temp dir (CLAUDE.md conventions)."""

from pathlib import Path

import httpx
import pytest

from tern.config import TernConfig
from tern.daemon import main
from tern.llm.client import LLMClient
from tern.search.embedder import Embedder, EmbeddingClient


@pytest.fixture(autouse=True)
def tern_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Short base path: AF_UNIX socket paths are limited to 107 bytes.
    home = tmp_path_factory.mktemp("th")
    monkeypatch.setenv("TERN_HOME", str(home))
    return home


def unreachable_embedder(cfg: TernConfig) -> Embedder:
    """An ``EmbeddingClient`` whose transport refuses every request (embedder unreachable)."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("test: no embeddings server", request=request)

    return EmbeddingClient(cfg, transport=httpx.MockTransport(refuse))


@pytest.fixture(autouse=True)
def no_embeddings_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-process ``serve()`` would otherwise POST to the default loopback embeddings URL (a real
    LM Studio, when one runs outside the sandbox). Tests that pass ``embedder_factory`` override
    this. Subprocess ternd runs get embedding off from ``tern.testing.procs.env_with_home``."""
    monkeypatch.setattr(main, "make_embedder", unreachable_embedder)


def unreachable_llm(cfg: TernConfig) -> LLMClient:
    """An ``LLMClient`` whose transport refuses every request (chat model unreachable)."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("test: no chat server", request=request)

    return LLMClient(cfg, transport=httpx.MockTransport(refuse))


@pytest.fixture(autouse=True)
def no_chat_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-process ``serve()`` would otherwise send chat turns to the default loopback chat URL (a
    real LM Studio, when one runs outside the sandbox). Tests that pass ``llm_factory`` override
    this (plan 08b step 1)."""
    monkeypatch.setattr(main, "make_llm", unreachable_llm)
