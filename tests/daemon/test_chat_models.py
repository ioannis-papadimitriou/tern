"""Bridge 1.3, the chat model picker: ``chat.models`` / ``chat.select_model`` on ``ChatHost``.

``serve()`` runs in-process like ``test_chat.py``; discovery and the picked model's client are
replaced (``tern.llm.models.discover``, ``tern.daemon.main._picked_llm``) so no real server is
asked. Synthetic data only.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tern.bridge.protocol import (
    BridgeError,
    ChatSelectModelParams,
    ChatSendParams,
    Empty,
)
from tern.config import TernConfig
from tern.daemon import main as daemon_main
from tern.llm import models as llm_models
from tern.llm.client import LLMClient
from tern.llm.models import ModelChoice, choice_key
from tern.testing.fake_llm import FakeLLM, ScriptStep

from .test_chat import FAKE_MODEL, chat_cfg, recording_peer, running, wait_for

OTHER_URL = "http://127.0.0.1:1234/v1"
OTHER_MODEL = "openai/gpt-oss-20b"


def _choices(cfg: TernConfig) -> list[ModelChoice]:
    return [
        ModelChoice(
            key=choice_key(cfg.chat_base_url, FAKE_MODEL),
            base_url=cfg.chat_base_url,
            model=FAKE_MODEL,
            server="Strata",
            label="Fake Chat",
        ),
        ModelChoice(
            key=choice_key(OTHER_URL, OTHER_MODEL),
            base_url=OTHER_URL,
            model=OTHER_MODEL,
            server="LM Studio",
            label=OTHER_MODEL,
        ),
    ]


@pytest.fixture
def other(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """The second server's model: discovery lists both; picking it builds a client over this."""
    fake = FakeLLM.from_steps(ScriptStep(content="From the other model."), model_id=OTHER_MODEL)
    built: list[tuple[str, str]] = []

    async def discover(cfg: TernConfig, **_: Any) -> list[ModelChoice]:
        return _choices(cfg)

    def picked(cfg: TernConfig, base_url: str, model: str) -> LLMClient:
        built.append((base_url, model))
        return LLMClient(cfg, transport=fake.transport, base_url=base_url, model=model)

    monkeypatch.setattr(llm_models, "discover", discover)
    monkeypatch.setattr(daemon_main, "_picked_llm", picked)
    fake.built = built  # type: ignore[attr-defined]
    return fake


@pytest.mark.asyncio
async def test_models_lists_both_servers_with_the_default_selected(other: FakeLLM) -> None:
    default = FakeLLM.from_steps(ScriptStep(content="unused"))
    cfg = chat_cfg()
    async with running(cfg, default) as run, recording_peer() as peer:
        listing = await run.daemon.chat.models(peer, Empty())
        assert [m.server for m in listing.models] == ["Strata", "LM Studio"]
        assert listing.selected == choice_key(cfg.chat_base_url, FAKE_MODEL)


@pytest.mark.asyncio
async def test_a_picked_model_serves_the_next_turn_and_daemon_info_names_it(
    other: FakeLLM,
) -> None:
    default = FakeLLM.from_steps(ScriptStep(content="unused"))  # never asked
    async with running(chat_cfg(), default) as run, recording_peer() as peer:
        host = run.daemon.chat
        key = choice_key(OTHER_URL, OTHER_MODEL)
        listing = await host.select_model(peer, ChatSelectModelParams(key=key))
        assert listing.selected == key and host.chat_model == OTHER_MODEL
        turn = await host.send(peer, ChatSendParams(session_id=str(uuid.uuid4()), text="hi"))
        await wait_for(lambda: peer.ended(turn.turn_id) is not None, "the turn's end")
        other.assert_done()
        assert other.requests[0]["model"] == OTHER_MODEL
        assert default.requests == []
        # Picking it again reuses its client.
        await host.select_model(peer, ChatSelectModelParams(key=key))
        assert other.built == [(OTHER_URL, OTHER_MODEL)]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_back_to_the_default_model_uses_the_default_client(other: FakeLLM) -> None:
    default = FakeLLM.from_steps(ScriptStep(content="Default."))
    cfg = chat_cfg()
    async with running(cfg, default) as run, recording_peer() as peer:
        host = run.daemon.chat
        await host.select_model(peer, ChatSelectModelParams(key=choice_key(OTHER_URL, OTHER_MODEL)))
        back = choice_key(cfg.chat_base_url, FAKE_MODEL)
        assert (await host.select_model(peer, ChatSelectModelParams(key=back))).selected == back
        sid = str(uuid.uuid4())
        turn = await host.send(peer, ChatSendParams(session_id=sid, text="hi"))
        await wait_for(lambda: peer.ended(turn.turn_id) is not None, "the turn's end")
        default.assert_done()
        assert other.requests == []


@pytest.mark.asyncio
async def test_an_unknown_key_is_invalid_params_and_changes_nothing(other: FakeLLM) -> None:
    async with (
        running(chat_cfg(), FakeLLM.from_steps(ScriptStep(content="unused"))) as run,
        recording_peer() as peer,
    ):
        host = run.daemon.chat
        with pytest.raises(BridgeError) as exc:
            await host.select_model(peer, ChatSelectModelParams(key="http://x#nope"))
        assert exc.value.name == "invalid_params"
        assert host.chat_model == FAKE_MODEL


@pytest.mark.asyncio
async def test_selected_is_null_when_the_selected_model_is_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def nothing(cfg: TernConfig, **_: Any) -> list[ModelChoice]:
        return []

    monkeypatch.setattr(llm_models, "discover", nothing)
    async with (
        running(chat_cfg(), FakeLLM.from_steps(ScriptStep(content="unused"))) as run,
        recording_peer() as peer,
    ):
        listing = await run.daemon.chat.models(peer, Empty())
        assert listing.models == [] and listing.selected is None
