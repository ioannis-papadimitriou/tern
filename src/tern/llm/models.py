"""Chat model discovery for the chat tab's model picker (bridge 1.3 ``chat.models``).

:func:`discover` asks each chat server (``cfg.chat_base_url`` first, then
``cfg.chat_extra_base_urls``) which chat models it has **loaded** and returns them as
:class:`ModelChoice`\\ s. Every URL passes the loopback egress guard (``tern.llm.egress``, #9)
first; a denied or unreachable server is skipped (logged), never an error: the picker shows what
answers.

What "loaded" means per server:

- **LM Studio** may list every *downloaded* model at ``/v1/models`` (with JIT loading on). Its
  native API says which are loaded: ``<origin>/api/v1/models`` (0.4+: entries with
  ``loaded_instances`` and a ``display_name`` label), else ``<origin>/api/v0/models``
  (``state == "loaded"``). Only ``llm``/``vlm`` types; embedding models are left out.
- **Any other server** (Strata, llama.cpp, vLLM, ...) lists what it serves at ``/v1/models``;
  each entry is taken as loaded. Ids that look like embedding models are left out.

Only ids, owners and display labels are read; bodies are size-capped and parsed leniently.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from tern.config import TernConfig
from tern.llm.egress import EgressDenied, check_url

log = logging.getLogger("tern.llm.models")

#: Per-request timeout of discovery (a picker must not hang on a dead server).
DISCOVER_TIMEOUT_S = 3.0
#: Largest discovery body read (a model list is small).
_MAX_BODY = 1 << 20


@dataclass(frozen=True)
class ModelChoice:
    """One loaded chat model on one server."""

    #: Stable key: ``<base_url>#<model id>`` (what ``chat.select_model`` takes).
    key: str
    base_url: str
    model: str
    #: The server's display name: ``Strata``, ``LM Studio`` or ``host:port``.
    server: str
    #: The model's display name (the server's label when it gives one, else the id).
    label: str


def choice_key(base_url: str, model: str) -> str:
    return f"{base_url}#{model}"


def _origin(base_url: str) -> str:
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def _hostport(base_url: str) -> str:
    return urlsplit(base_url).netloc


def _looks_like_embedding(model_id: str) -> bool:
    low = model_id.lower()
    return "embed" in low or "rerank" in low


async def _get_json(client: httpx.AsyncClient, url: str) -> Any | None:
    """GET ``url`` → parsed JSON, or None (unreachable, non-200, too big, not JSON)."""
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return None
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > _MAX_BODY:
                    return None
    except httpx.HTTPError:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _entries(doc: Any, key: str = "data") -> list[dict[str, Any]]:
    if not isinstance(doc, dict):
        return []
    items = doc.get(key)
    if not isinstance(items, list):
        return []
    return [e for e in items if isinstance(e, dict)]


async def _lmstudio_loaded(client: httpx.AsyncClient, origin: str) -> list[tuple[str, str]] | None:
    """The loaded LLMs as (id, display label) if ``origin`` is LM Studio (its native API
    answers), else None. ``/api/v1/models`` (0.4+: every downloaded model, with
    ``loaded_instances`` and ``display_name``) first, then ``/api/v0/models`` (``state``)."""
    v1 = _entries(await _get_json(client, f"{origin}/api/v1/models"), "models")
    if v1 and all("loaded_instances" in e for e in v1):
        out: list[tuple[str, str]] = []
        for e in v1:
            if e.get("type", "llm") not in ("llm", "vlm"):
                continue
            name = e.get("display_name")
            for inst in e.get("loaded_instances") or []:
                if isinstance(inst, dict) and isinstance(inst.get("id"), str) and inst["id"]:
                    label = name if isinstance(name, str) and name else inst["id"]
                    out.append((inst["id"], label))
        return out
    v0 = _entries(await _get_json(client, f"{origin}/api/v0/models"))
    if v0 and all("state" in e for e in v0):
        return [
            (e["id"], e["id"])
            for e in v0
            if isinstance(e.get("id"), str)
            and e.get("state") == "loaded"
            and e.get("type", "llm") in ("llm", "vlm")
        ]
    return None


async def _server_models(client: httpx.AsyncClient, base_url: str) -> list[ModelChoice]:
    base = base_url.rstrip("/")
    listed = _entries(await _get_json(client, f"{base}/models"))
    if not listed:
        return []
    owners = {e.get("owned_by") for e in listed}
    loaded = await _lmstudio_loaded(client, _origin(base))
    if loaded is not None:
        server = "LM Studio"
        ids = [i for i, _ in loaded if not _looks_like_embedding(i)]
        labels: dict[str, str] = dict(loaded)
    else:
        server = "Strata" if owners == {"strata"} else _hostport(base)
        ids, labels = [], {}
        for e in listed:
            mid = e.get("id")
            if not isinstance(mid, str) or not mid or _looks_like_embedding(mid):
                continue
            ids.append(mid)
            meta = e.get("meta")
            if isinstance(meta, dict) and isinstance(meta.get("label"), str) and meta["label"]:
                labels[mid] = meta["label"]
    seen: set[str] = set()
    out: list[ModelChoice] = []
    for mid in ids:
        if mid in seen:
            continue
        seen.add(mid)
        out.append(
            ModelChoice(
                key=choice_key(base_url, mid),
                base_url=base_url,
                model=mid,
                server=server,
                label=labels.get(mid, mid),
            )
        )
    return out


def server_urls(cfg: TernConfig) -> list[str]:
    """``chat_base_url`` then ``chat_extra_base_urls``, deduplicated, egress-checked."""
    out: list[str] = []
    for url in (cfg.chat_base_url, *cfg.chat_extra_base_urls):
        if url in out:
            continue
        try:
            check_url(url, cfg)
        except EgressDenied as exc:
            log.warning("model server skipped: egress denied", extra={"reason": str(exc)})
            continue
        out.append(url)
    return out


async def discover(
    cfg: TernConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[ModelChoice]:
    """Every loaded chat model on the configured servers, servers in config order."""
    urls = server_urls(cfg)
    async with httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=DISCOVER_TIMEOUT_S,
    ) as client:
        found = await asyncio.gather(*(_server_models(client, u) for u in urls))
    return [m for models in found for m in models]
