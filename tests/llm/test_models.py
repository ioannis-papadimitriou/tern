"""``tern.llm.models.discover``: loaded chat models per server, over a mock transport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tern.config import TernConfig
from tern.llm.models import choice_key, discover

STRATA = "http://127.0.0.1:47831/v1"
LMS = "http://127.0.0.1:1234/v1"


def _json(doc: Any) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(doc).encode())


def transport(routes: dict[str, Any]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        doc = routes.get(str(request.url))
        if doc is None:
            return httpx.Response(404)
        if isinstance(doc, Exception):
            raise doc
        return _json(doc)

    return httpx.MockTransport(handle)


STRATA_MODELS = {
    "data": [
        {"id": "iq3_xxs", "owned_by": "strata", "meta": {"label": "Qwen3.8-Flash-Next IQ3_XXS"}}
    ]
}
LMS_V1 = {
    "data": [
        {"id": "openai/gpt-oss-20b", "owned_by": "organization_owner"},
        {"id": "qwen3-8b", "owned_by": "organization_owner"},
        {"id": "text-embedding-nomic-embed-text-v1.5", "owned_by": "organization_owner"},
    ]
}
LMS_V0 = {
    "data": [
        {"id": "openai/gpt-oss-20b", "type": "llm", "state": "loaded"},
        {"id": "qwen3-8b", "type": "llm", "state": "not-loaded"},
        {"id": "text-embedding-nomic-embed-text-v1.5", "type": "embeddings", "state": "loaded"},
    ]
}


@pytest.mark.asyncio
async def test_both_servers_only_loaded_chat_models() -> None:
    routes = {
        f"{STRATA}/models": STRATA_MODELS,
        f"{LMS}/models": LMS_V1,
        "http://127.0.0.1:1234/api/v0/models": LMS_V0,
    }
    found = await discover(TernConfig(), transport=transport(routes))
    assert [(m.server, m.model, m.label) for m in found] == [
        ("Strata", "iq3_xxs", "Qwen3.8-Flash-Next IQ3_XXS"),
        ("LM Studio", "openai/gpt-oss-20b", "openai/gpt-oss-20b"),
    ]
    assert found[1].key == choice_key(LMS, "openai/gpt-oss-20b")


@pytest.mark.asyncio
async def test_lmstudio_native_v1_api_with_loaded_instances() -> None:
    v1 = {
        "models": [
            {
                "key": "openai/gpt-oss-20b",
                "type": "llm",
                "display_name": "gpt-oss 20B",
                "loaded_instances": [{"id": "gpt-oss"}],
            },
            {"key": "qwen3-8b", "type": "llm", "loaded_instances": []},
            {"key": "nomic", "type": "embedding", "loaded_instances": [{"id": "nomic"}]},
        ]
    }
    routes = {f"{LMS}/models": LMS_V1, "http://127.0.0.1:1234/api/v1/models": v1}
    found = await discover(TernConfig(), transport=transport(routes))
    assert [(m.server, m.model, m.label) for m in found] == [
        ("LM Studio", "gpt-oss", "gpt-oss 20B")
    ]


@pytest.mark.asyncio
async def test_a_dead_or_broken_server_is_skipped() -> None:
    routes = {
        f"{STRATA}/models": httpx.ConnectError("refused"),
        f"{LMS}/models": "not a model list",
    }
    assert await discover(TernConfig(), transport=transport(routes)) == []


@pytest.mark.asyncio
async def test_unknown_server_is_named_by_host_and_embeddings_are_dropped() -> None:
    other = "http://127.0.0.1:8080/v1"
    routes = {f"{other}/models": {"data": [{"id": "llama"}, {"id": "bge-embed"}]}}
    cfg = TernConfig(chat_base_url=other, chat_extra_base_urls=[])
    found = await discover(cfg, transport=transport(routes))
    assert [(m.server, m.model) for m in found] == [("127.0.0.1:8080", "llama")]


@pytest.mark.asyncio
async def test_a_non_loopback_extra_url_is_never_asked() -> None:
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(404)

    cfg = TernConfig(chat_extra_base_urls=["http://evil.test/v1"])
    await discover(cfg, transport=httpx.MockTransport(handle))
    assert all("evil.test" not in u for u in asked)
