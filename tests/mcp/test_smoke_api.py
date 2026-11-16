"""Plan 07 step 4: ``tern.testing.smoke.api_client``, the MCP contract behind the smoke adapter's
``api_client``/``api_smoke_request``.

One module-scoped client (a fresh temp index of the synthetic mailbox, hybrid search with the
StubEmbedder) serves every test; each request is one in-memory MCP session over the real
``Registry`` + ``Engine`` + ``AuditLog``. Expected mids come from MANIFEST.json keys."""

from __future__ import annotations

import json
import typing
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from tern.mcp.local import RegistryBackend
from tern.mcp.server import ToolFailure, ToolOutcome, call_result
from tern.perms.audit import AuditLog, AuditRecord
from tern.testing import smoke
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, tool
from tern.tools.contract import ToolErrorCode, ToolMeta
from tern.tools.registry import RegisteredTool, Registry, builtin_tools

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_mailbox"
_MANIFEST = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
KEY_OF_MID = {m["mid"]: m["key"] for m in _MANIFEST["messages"]}


@pytest.fixture(scope="module")
def api(tmp_path_factory: pytest.TempPathFactory) -> smoke.ApiClient:
    return smoke.api_client(FIXTURE, tmp_path_factory.mktemp("smoke-api"))


def rows(api: smoke.ApiClient) -> list[AuditRecord]:
    """Every audit row, oldest first."""
    with AuditLog.open(api.audit_path) as audit:
        return list(reversed(audit.list(limit=None)))


def test_search_messages_is_200_hybrid_with_one_audit_row(api: smoke.ApiClient) -> None:
    before = len(rows(api))
    resp = api.request("TOOL", "search_messages", {"query": "Acme invoice", "limit": 5})
    assert resp.status_code == 200, resp.text[:300]
    # untrusted: the JSON arrives inside one untrusted_email envelope (plan 08a step 2a)
    assert resp.text.startswith("<untrusted_email>\n") and resp.text.endswith(
        "\n</untrusted_email>"
    )
    out = json.loads(
        resp.text.removeprefix("<untrusted_email>\n").removesuffix("\n</untrusted_email>")
    )
    assert out["mode"] == "hybrid" and out["embed_pending"] == 0
    keys = {KEY_OF_MID[h["mid"]] for h in out["hits"]}
    assert 1 <= len(out["hits"]) <= 5
    assert "inv-0042" in keys
    new = rows(api)[before:]
    assert [(r.caller, r.caller_label, r.tool, r.decision) for r in new] == [
        (smoke.API_CALLER, smoke.API_CLIENT_NAME, "search_messages", "auto")
    ]


def test_trusted_tool_is_200(api: smoke.ApiClient) -> None:
    resp = api.request("TOOL", "index_status", None)
    assert resp.status_code == 200, resp.text[:300]
    assert isinstance(json.loads(resp.text), dict)


def test_invalid_params_is_422_with_its_row(api: smoke.ApiClient) -> None:
    before = len(rows(api))
    resp = api.request("TOOL", "search_messages", {"query": ""})
    assert resp.status_code == 422
    assert json.loads(resp.text)["error"]["code"] == "invalid_params"
    [row] = rows(api)[before:]
    assert (row.caller, row.tool, row.reason) == (
        smoke.API_CALLER,
        "search_messages",
        "invalid_params",
    )


def test_unknown_tool_is_500_with_its_row(api: smoke.ApiClient) -> None:
    before = len(rows(api))
    resp = api.request("TOOL", "no_such_tool", {})
    assert resp.status_code == 500
    assert json.loads(resp.text)["error"]["code"] == "unknown_tool"
    [row] = rows(api)[before:]
    assert (row.caller, row.tool, row.reason) == (smoke.API_CALLER, "no_such_tool", "unknown_tool")


def test_only_the_tool_method_is_accepted(api: smoke.ApiClient) -> None:
    before = len(rows(api))
    with pytest.raises(smoke.SmokeError, match="only method 'TOOL'"):
        api.request("POST", "search_messages", {"query": "Acme"})
    assert len(rows(api)) == before


def _failure(code: str) -> CallToolResult:
    return call_result(ToolOutcome(error=ToolFailure(code, "refused", None)))


#: The status of every ``ToolErrorCode``, classified on purpose (plan 07 step 4).
EXPECTED_STATUS: dict[str, int] = {
    "invalid_params": 422,
    "denied": 403,
    "expired": 403,
    "rate_limited": 403,
    "tier_not_enabled": 403,
    "targets_changed": 403,
    "state_changed": 403,
    "unknown_tool": 500,
    "unknown_mid": 500,
    "mid_conflicted": 500,
    "ambiguous_location": 500,
    "unavailable": 500,
    "internal": 500,
}


def test_every_tool_error_code_has_an_expected_status() -> None:
    # A code added to ToolErrorCode fails here until it is classified in EXPECTED_STATUS.
    assert set(EXPECTED_STATUS) == set(typing.get_args(ToolErrorCode))
    assert set(typing.get_args(ToolErrorCode)) >= smoke.API_FORBIDDEN_CODES


@pytest.mark.parametrize("code", typing.get_args(ToolErrorCode))
def test_api_status_maps_error_codes(code: str) -> None:
    assert smoke.api_status(_failure(code)) == EXPECTED_STATUS[code]


def test_api_status_success_is_200() -> None:
    trusted = call_result(ToolOutcome(output={"n": 1}))
    untrusted = call_result(ToolOutcome(output={"n": 1}, untrusted_output=True))
    assert smoke.api_status(trusted) == smoke.api_status(untrusted) == 200


@pytest.mark.parametrize("text", ["boom", "[]", '{"error": {}}', '{"error": {"code": 7}}'])
def test_api_status_refuses_a_malformed_error_body(text: str) -> None:
    result = CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
    with pytest.raises(smoke.SmokeError):
        smoke.api_status(result)


class _Bypass:
    """A backend that answers without ``Registry.call`` (no audit row)."""

    def __init__(self, registry: Registry, caller: str) -> None:
        self._registry = registry

    def describe(self) -> list[ToolMeta]:
        return self._registry.describe()

    async def call(
        self, tool: str, args: Mapping[str, Any], *, client_name: str | None
    ) -> ToolOutcome:
        return ToolOutcome(output={"hits": []}, untrusted_output=True)


class _Twice(RegistryBackend):
    """A backend that runs the call through ``Registry.call`` twice (two rows)."""

    async def call(
        self, tool: str, args: Mapping[str, Any], *, client_name: str | None
    ) -> ToolOutcome:
        await super().call(tool, args, client_name=client_name)
        return await super().call(tool, args, client_name=client_name)


class _OtherTool(RegistryBackend):
    """A backend whose one row names another tool than the one requested."""

    async def call(
        self, tool: str, args: Mapping[str, Any], *, client_name: str | None
    ) -> ToolOutcome:
        return await super().call("index_status", {}, client_name=client_name)


class _NoLabel(RegistryBackend):
    """A backend whose one row has no caller label (the client name was dropped)."""

    async def call(
        self, tool: str, args: Mapping[str, Any], *, client_name: str | None
    ) -> ToolOutcome:
        return await super().call(tool, args, client_name=None)


@pytest.mark.parametrize(
    ("backend", "match"),
    [
        (_Bypass, "0 audit rows written"),
        (_Twice, "2 audit rows written"),
        (_OtherTool, "does not name"),
        (_NoLabel, "does not name"),
    ],
)
def test_a_call_without_exactly_one_matching_row_is_a_smoke_error(
    api: smoke.ApiClient, monkeypatch: pytest.MonkeyPatch, backend: type, match: str
) -> None:
    # search_messages lists no output schema, so the SDK client does not validate the
    # substituted outputs against one.
    monkeypatch.setattr(smoke, "RegistryBackend", backend)
    with pytest.raises(smoke.SmokeError, match=match):
        api.request("TOOL", "search_messages", {"query": "Acme invoice"})


def test_each_request_is_a_fresh_session_on_the_same_audit_log(api: smoke.ApiClient) -> None:
    before = len(rows(api))
    for _ in range(2):
        assert api.request("TOOL", "list_folders", {}).status_code == 200
    new = rows(api)[before:]
    assert [r.tool for r in new] == ["list_folders", "list_folders"]
    assert len({r.call_id for r in new}) == 2


class _EchoIn(BoundaryModel):
    text: str


class _CountOut(BoundaryModel):
    n: int


FILTER_RAN: list[str] = []


@tool(
    name="smoke_dummy_filter",
    tier=Tier.R0,  # claims to be harmless
    effects={Effect.FILTERS_WRITE},
    untrusted_output=False,
    count_targets=lambda inp: 1,
)
async def smoke_dummy_filter(ctx: ToolContext, inp: _EchoIn) -> _CountOut:
    """Create a mail filter (forbidden-effect dummy, test only)."""
    FILTER_RAN.append(inp.text)
    return _CountOut(n=1)


def test_engine_refusal_is_403_end_to_end(
    api: smoke.ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No built-in can be refused before plan 10; a test-only forbidden-effect tool is refused
    # by the real Engine inside Registry.call (denied/forbidden_effect) through the server.
    def with_dummy() -> list[RegisteredTool]:
        return [*builtin_tools(), RegisteredTool(smoke_dummy_filter, "plugin", "test")]

    monkeypatch.setattr(smoke, "builtin_tools", with_dummy)
    before = len(rows(api))
    resp = api.request("TOOL", "smoke_dummy_filter", {"text": "x"})
    assert resp.status_code == 403, resp.text[:300]
    assert json.loads(resp.text)["error"]["code"] == "denied"
    [row] = rows(api)[before:]
    assert (row.caller, row.caller_label, row.tool, row.decision, row.reason) == (
        smoke.API_CALLER,
        smoke.API_CLIENT_NAME,
        "smoke_dummy_filter",
        "denied",
        "forbidden_effect",
    )
    assert FILTER_RAN == []
