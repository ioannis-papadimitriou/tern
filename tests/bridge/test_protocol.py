"""tern-bridge/1 protocol models: 1.0 (plan 02 step 1) and 1.1 (plan 07 part B, decisions #37)."""

from __future__ import annotations

import json
import uuid
from typing import Any, get_args

import pytest

from tern.bridge import protocol as p

SENTINEL = "SENTINEL-mailbody-7f3a"
MID = "a" * 64
EXT_HELLO: dict[str, Any] = {
    "protocol": "tern-bridge/1",
    "minor": 0,
    "role": "extension",
    "version": "0.1.0",
    "capabilities": ["mail.read"],
    "profile_id": str(uuid.uuid4()),
}

# Architecture §3, the 1.0 table plus the transport additions (decisions #3, plan 02 approval).
SECTION3_1_0 = {
    "hello", "bridge.ping", "bridge.ping_extension", "bridge.chunk", "bridge.reconnected",
    "mail.accounts.list", "mail.folders.list", "mail.messages.page", "mail.messages.raw",
    "mail.messages.full", "mail.messages.find", "mail.open", "mail.tags.list", "mail.tags.create",
    "mail.update", "mail.draft.save", "mail.compose.open", "mail.move", "mail.archive",
    "mail.delete", "chat.send", "chat.cancel", "chat.delta", "chat.done", "chat.error",
    "approvals.decide", "approvals.pending", "search.query", "index.status", "audit.list",
    "audit.undo", "event.mail.new", "event.mail.moved", "event.mail.deleted",
    "event.mail.updated", "event.folder.changed", "approval.requested", "approval.resolved",
}  # fmt: skip
PLANNED_1_N = {"contacts.lookup", "triage.list", "triage.feedback", "contacts.search",
               "contacts.upsert", "calendar.list_events", "calendar.create_event"}  # fmt: skip


def test_1_0_has_exactly_the_section3_methods() -> None:
    assert {n for n, s in p.METHODS.items() if s.minor == 0} == SECTION3_1_0
    assert not PLANNED_1_N & set(p.METHODS)


def test_minor_negotiation_uses_the_lower_minor() -> None:
    assert p.negotiate_minor(0, 3) == 0
    assert p.negotiate_minor(2, 1) == 1


def test_a_higher_minor_method_on_a_1_0_connection_is_method_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_1_1 = p.MethodSpec("contacts.lookup", "request", "d2e", p.Empty, p.Empty, minor=1)
    assert isinstance(p.METHODS, dict)
    monkeypatch.setitem(p.METHODS, "contacts.lookup", spec_1_1)
    assert p.method_spec("contacts.lookup", 1) is spec_1_1
    with pytest.raises(p.BridgeError) as exc:
        p.method_spec("contacts.lookup", 0)
    assert exc.value.name == "method_not_available"


def test_unknown_method_is_method_not_available() -> None:
    with pytest.raises(p.BridgeError) as exc:
        p.method_spec("mail.send", 0)
    assert exc.value.name == "method_not_available"


@pytest.mark.parametrize("name", get_args(p.ErrorName))
def test_every_error_name_round_trips(name: p.ErrorName) -> None:
    err = p.BridgeError(name, "detail", tb_code="E1" if name == "tb_error" else None)
    wire = json.loads(json.dumps(p.dump(p.ErrorResponse(id=7, error=err.to_rpc()))))
    msg = p.parse_message(wire)
    assert isinstance(msg, p.ErrorResponse)
    back = p.BridgeError.from_rpc(msg.error)
    assert (back.name, back.message, back.tb_code) == (err.name, "detail", err.tb_code)
    assert msg.error.code == p.ERROR_CODES[name]


def test_error_names_match_section3() -> None:
    section3 = {
        "unavailable", "invalid_params", "method_not_available", "stale_id", "targets_changed",
        "tb_error", "profile_mismatch", "extension_already_connected", "role_rejected",
        "too_large", "state_changed",
    }  # fmt: skip
    assert set(get_args(p.ErrorName)) == section3 == set(p.ERROR_CODES)
    assert len(set(p.ERROR_CODES.values())) == len(p.ERROR_CODES)


def test_extension_hello_requires_profile_id() -> None:
    p.validate_params(p.Hello, EXT_HELLO)
    with pytest.raises(p.BridgeError) as exc:
        p.validate_params(p.Hello, {k: v for k, v in EXT_HELLO.items() if k != "profile_id"})
    assert exc.value.name == "invalid_params"


def test_non_extension_hello_must_not_carry_profile_id() -> None:
    with pytest.raises(p.BridgeError):
        p.validate_params(p.Hello, {**EXT_HELLO, "role": "cli"})
    cli = {k: v for k, v in EXT_HELLO.items() if k != "profile_id"} | {"role": "cli"}
    assert p.validate_params(p.Hello, cli).role == "cli"


def test_other_major_protocol_is_rejected() -> None:
    with pytest.raises(p.BridgeError):
        p.validate_params(p.Hello, {**EXT_HELLO, "protocol": "tern-bridge/2"})


def test_invalid_params_never_echo_the_input() -> None:
    bad = {"query": SENTINEL, "limit": f"many {SENTINEL}"}
    with pytest.raises(p.BridgeError) as exc:
        p.validate_params(p.SearchQueryParams, bad)
    assert exc.value.name == "invalid_params"
    assert "limit" in exc.value.message
    assert SENTINEL not in str(exc.value)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(p.BridgeError):
        p.validate_params(p.TbIdParams, {"tb_id": 1, "extra": True})


def test_from_alias_on_the_wire() -> None:
    hit = p.SearchHit.model_validate(
        {
            "mid": MID,
            "date": "2026-01-02T03:04:05Z",
            "from": "a@example.com",
            "subject": "s",
            "snippet": "x",
            "score": 0.5,
            "folders": ["f1"],
        }  # fmt: skip
    )
    assert p.dump(hit)["from"] == "a@example.com"


def test_draft_attachments_capped_at_5_mb() -> None:
    base = {"identity_id": "id1", "to": [], "cc": [], "subject": "s", "body_text": "b"}
    ok = "A" * (4 * 1024 * 1024 // 3 * 4)  # ~4 MiB decoded
    one = [{"name": "a", "mime": "x/y", "content_b64": ok}]
    p.validate_params(p.DraftSaveParams, base | {"attachments": one})
    too_big = one * 2
    with pytest.raises(p.BridgeError):
        p.validate_params(p.DraftSaveParams, base | {"attachments": too_big})


def test_parse_message_classifies() -> None:
    assert isinstance(p.parse_message({"jsonrpc": "2.0", "id": 1, "method": "m"}), p.Request)
    assert isinstance(p.parse_message({"jsonrpc": "2.0", "method": "m"}), p.Notification)
    assert isinstance(p.parse_message({"jsonrpc": "2.0", "id": 1, "result": {}}), p.Response)
    for bad in ([], {"jsonrpc": "2.0"}, {"jsonrpc": "1.0", "id": 1, "method": "m"}):
        with pytest.raises(ValueError):
            p.parse_message(bad)


def test_role_restrictions() -> None:
    assert p.METHODS["approvals.decide"].roles == frozenset({"extension"})
    assert p.METHODS["bridge.ping_extension"].roles == frozenset({"cli"})


def test_schema_export_is_deterministic_and_complete() -> None:
    a = json.dumps(p.export_schema("1.0"), sort_keys=True)
    b = json.dumps(p.export_schema("1.0"), sort_keys=True)
    assert a == b
    doc = json.loads(a)
    assert set(doc["methods"]) == SECTION3_1_0
    for entry in doc["methods"].values():
        assert entry["params"]["$ref"].removeprefix("#/$defs/") in doc["$defs"]
    with pytest.raises(ValueError):
        p.export_schema("1.4")


# ---- minor 1 (plan 07 part B, plan 08b; decisions #37) --------------------------------------

MINOR_1 = {"tools.list", "tools.call", "audit.status", "ui.open_mid", "daemon.info"}


def test_minor_1_adds_exactly_its_methods_and_this_build_speaks_it() -> None:
    assert p.MINOR >= 1  # 2 since #39
    assert {n for n, s in p.METHODS.items() if s.minor == 1} == MINOR_1
    assert {s.minor for s in p.METHODS.values()} == {0, 1, 2, 3}  # 2: #39; 3: model picker
    for name in MINOR_1:
        assert p.method_spec(name, 1) is p.METHODS[name]
        with pytest.raises(p.BridgeError) as exc:
            p.method_spec(name, 0)
        assert exc.value.name == "method_not_available"


def test_minor_1_directions_and_roles() -> None:
    m = p.METHODS
    assert (m["tools.list"].direction, m["tools.list"].roles) == ("mcp2d", {"mcp-proxy"})
    assert (m["tools.call"].direction, m["tools.call"].roles) == ("mcp2d", {"mcp-proxy"})
    assert (m["audit.status"].direction, m["audit.status"].roles) == ("e2d", None)
    assert (m["ui.open_mid"].direction, m["ui.open_mid"].roles) == ("e2d", {"extension"})
    assert (m["daemon.info"].direction, m["daemon.info"].roles) == ("e2d", {"extension", "cli"})
    assert all(m[n].kind == "request" for n in MINOR_1)


def test_the_1_1_export_is_a_superset_of_1_0_with_1_0_shapes_unchanged() -> None:
    old, new = p.export_schema("1.0"), p.export_schema("1.1")
    assert (old["minor"], new["minor"]) == (0, 1)
    assert set(new["methods"]) == set(old["methods"]) | MINOR_1
    for name, entry in old["methods"].items():
        assert new["methods"][name] == entry
    for name, schema in old["$defs"].items():
        assert new["$defs"][name] == schema
    assert new["errors"] == old["errors"]
    assert {new["methods"][n]["minor"] for n in MINOR_1} == {1}
    for entry in new["methods"].values():
        assert entry["params"]["$ref"].removeprefix("#/$defs/") in new["$defs"]


def test_tool_call_result_holds_exactly_one_of_output_and_error() -> None:
    p.ToolCallResult(ok=True, output={"a": None}, untrusted_output=False)
    p.ToolCallResult(
        ok=False, error=p.ToolCallError(code="denied", message="m"), untrusted_output=False
    )
    err = {"code": "denied", "message": "m"}
    for bad in (
        {"ok": True, "untrusted_output": False},
        {"ok": False, "untrusted_output": False},
        {"ok": True, "output": {}, "error": err, "untrusted_output": False},
        {"ok": False, "output": {}, "error": err, "untrusted_output": False},
        {"ok": False, "output": {}, "untrusted_output": False},
        {"ok": True, "error": err, "untrusted_output": False},
    ):
        with pytest.raises(p.BridgeError) as exc:
            p.validate_params(p.ToolCallResult, bad)
        assert exc.value.name == "invalid_params"


def test_tool_call_result_keeps_nulls_inside_the_output_on_the_wire() -> None:
    out = {"subject": None, "nested": {"x": None}, "list": [None]}
    wire = p.dump(p.ToolCallResult(ok=True, output=out, untrusted_output=True))
    assert wire["output"] == out
    assert "error" not in wire


@pytest.mark.parametrize(
    "params",
    [
        {"arguments": {}},
        {"tool": 7, "arguments": {}},
        {"tool": "search_messages", "arguments": []},
        {"tool": "search_messages", "arguments": {}, "client_name": ""},
        {"tool": "search_messages", "arguments": {}, "client_name": "x" * 129},
        {"tool": "search_messages", "arguments": {}, "client_name": "bad\x1bname"},
        {"tool": "search_messages", "arguments": {}, "extra": 1},
    ],
)
def test_tool_call_params_are_validated(params: dict[str, Any]) -> None:
    with pytest.raises(p.BridgeError) as exc:
        p.validate_params(p.ToolCallParams, params)
    assert exc.value.name == "invalid_params"


@pytest.mark.parametrize("name", ["Search", "a" * 65, "foo-bar", ""])
def test_any_tool_name_string_passes_the_bridge(name: str) -> None:
    """The registry refuses a malformed name and audits it as <invalid> (#10); the bridge must
    not refuse it first, or the call would leave no row (step 7 review)."""
    assert p.validate_params(p.ToolCallParams, {"tool": name, "arguments": {}}).tool == name


@pytest.mark.parametrize(
    "error",
    [
        {"code": "", "message": "m"},
        {"code": "Denied", "message": "m"},
        {"code": "denied", "message": ""},
        {"code": "denied", "reason": "Not-Snake", "message": "m"},
    ],
)
def test_tool_call_error_fields_follow_tool_error(error: dict[str, Any]) -> None:
    with pytest.raises(p.BridgeError):
        p.validate_params(p.ToolCallError, error)


def test_tool_call_result_carries_no_undo_token() -> None:
    assert "undo_token" not in p.ToolCallResult.model_fields
    with pytest.raises(p.BridgeError):
        p.validate_params(
            p.ToolCallResult,
            {"ok": True, "output": {}, "untrusted_output": False, "undo_token": "t"},
        )


def test_tool_call_params_accept_a_valid_call() -> None:
    ok = p.validate_params(
        p.ToolCallParams,
        {"tool": "search_messages", "arguments": {"query": SENTINEL}, "client_name": "LM Studio"},
    )
    assert (ok.tool, ok.client_name) == ("search_messages", "LM Studio")


def test_minor_1_results_validate() -> None:
    p.validate_params(p.AuditStatus, {"stale_writes": 0, "pending_approvals": 2})
    with pytest.raises(p.BridgeError):
        p.validate_params(p.AuditStatus, {"stale_writes": -1, "pending_approvals": 0})
    p.validate_params(p.OpenMidParams, {"mid": MID})
    with pytest.raises(p.BridgeError):
        p.validate_params(p.OpenMidParams, {"mid": "not-a-mid"})
    p.validate_params(p.OpenMidResult, {"opened": True, "folder_id": "f1"})
    with pytest.raises(p.BridgeError):
        p.validate_params(p.OpenMidResult, {"opened": False, "folder_id": "f1"})
    p.validate_params(p.DaemonInfo, {"remote_llm": False, "chat_model": "m"})
    p.validate_params(p.ToolList, {"tools": [{"name": "x", "extra": None}]})


# ---- minor 2, staged (decisions #39: per-chat permission modes) -------------------------------

MINOR_2 = {"chat.start", "chat.approval", "chat.approve"}


def test_minor_2_is_spoken_and_exported() -> None:
    assert p.MINOR >= 2 and p.SCHEMA_MINOR >= 2  # 3 since the model picker
    assert {n for n, s in p.METHODS.items() if s.minor == 2} == MINOR_2
    for name in MINOR_2:
        assert p.method_spec(name, 2) is p.METHODS[name]
        for older in (0, 1):
            with pytest.raises(p.BridgeError) as exc:
                p.method_spec(name, older)
            assert exc.value.name == "method_not_available"


def test_minor_2_directions_kinds_and_roles() -> None:
    m = p.METHODS
    both = {"extension", "cli"}
    start, approve, approval = m["chat.start"], m["chat.approve"], m["chat.approval"]
    assert (start.kind, start.direction, start.roles, start.result) == (
        "request",
        "e2d",
        both,
        p.TurnId,
    )
    assert (approve.kind, approve.direction, approve.roles, approve.result) == (
        "request",
        "e2d",
        both,
        p.ApprovalDecided,
    )
    assert (approval.kind, approval.direction, approval.result) == ("notification", "d2e", None)


def test_the_1_2_export_is_a_superset_of_1_1_with_older_shapes_unchanged() -> None:
    old, new = p.export_schema("1.1"), p.export_schema("1.2")
    assert (new["minor"], new["$id"]) == (2, "tern-bridge-1.2")
    assert set(new["methods"]) == set(old["methods"]) | MINOR_2
    for name, entry in old["methods"].items():
        assert new["methods"][name] == entry
    for name, schema in old["$defs"].items():
        assert new["$defs"][name] == schema
    assert new["errors"] == old["errors"]
    assert {new["methods"][n]["minor"] for n in MINOR_2} == {2}
    # chat.send keeps its 1.0 params: the mode lives on the new method only.
    assert "mode" not in new["$defs"]["ChatSendParams"]["properties"]
    assert new["$defs"]["ChatStartParams"]["required"] == ["session_id", "text", "mode"]


def test_chat_start_params_need_a_known_mode() -> None:
    ok = p.validate_params(p.ChatStartParams, {"session_id": "s", "text": "t", "mode": "manual"})
    assert ok.mode == "manual"
    for bad in (
        {"session_id": "s", "text": "t"},
        {"session_id": "s", "text": "t", "mode": "auto"},
        {"session_id": "s", "text": "", "mode": "bypass"},
    ):
        with pytest.raises(p.BridgeError) as exc:
            p.validate_params(p.ChatStartParams, bad)
        assert exc.value.name == "invalid_params"
    # chat.send stays strict: a mode there is an unknown field.
    with pytest.raises(p.BridgeError):
        p.validate_params(p.ChatSendParams, {"session_id": "s", "text": "t", "mode": "manual"})


def test_chat_approve_rest_of_turn_goes_with_approve_only() -> None:
    base = {"turn_id": "t", "approval_id": "a", "nonce": "n"}
    assert p.validate_params(p.ChatApproveParams, {**base, "decision": "approve"}).rest_of_turn is (
        False
    )
    p.validate_params(p.ChatApproveParams, {**base, "decision": "approve", "rest_of_turn": True})
    p.validate_params(p.ChatApproveParams, {**base, "decision": "deny"})
    with pytest.raises(p.BridgeError):
        p.validate_params(p.ChatApproveParams, {**base, "decision": "deny", "rest_of_turn": True})
    with pytest.raises(p.BridgeError):
        p.validate_params(p.ChatApproveParams, {**base, "decision": "allow"})


def test_chat_approval_keeps_the_args_whole_on_the_wire_and_never_echoes_them() -> None:
    args: dict[str, Any] = {"name": SENTINEL, "nested": {"x": [1, None]}, "empty": None}
    wire = p.dump(
        p.validate_params(
            p.ChatApproval,
            {
                "turn_id": "t",
                "approval_id": "a",
                "nonce": "n",
                "tool": "skill",
                "tier": "R0",
                "args": args,
                "expires_at": "2026-11-16T10:00:00Z",
            },
        )
    )
    assert wire["args"] == args  # nulls kept: the user sees exactly what will run
    with pytest.raises(p.BridgeError) as exc:
        p.validate_params(p.ChatApproval, {**wire, "tier": SENTINEL})
    assert SENTINEL not in str(exc.value)


# ---- minor 3 (the chat model picker) ----------------------------------------------------------

MINOR_3 = {"chat.models", "chat.select_model"}


def test_minor_3_is_spoken_and_exported() -> None:
    assert (p.MINOR, p.SCHEMA_MINOR) == (3, 3)
    assert {n for n, s in p.METHODS.items() if s.minor == 3} == MINOR_3
    for name in MINOR_3:
        spec = p.method_spec(name, 3)
        assert (spec.kind, spec.direction, spec.roles, spec.result) == (
            "request",
            "e2d",
            {"extension", "cli"},
            p.ChatModels,
        )
        for older in (0, 1, 2):
            with pytest.raises(p.BridgeError) as exc:
                p.method_spec(name, older)
            assert exc.value.name == "method_not_available"


def test_the_1_3_export_is_a_superset_of_1_2_with_older_shapes_unchanged() -> None:
    old, new = p.export_schema("1.2"), p.export_schema("1.3")
    assert (new["minor"], new["$id"]) == (3, "tern-bridge-1.3")
    assert set(new["methods"]) == set(old["methods"]) | MINOR_3
    for name, entry in old["methods"].items():
        assert new["methods"][name] == entry
    for name, schema in old["$defs"].items():
        assert new["$defs"][name] == schema
    assert new["errors"] == old["errors"]


def test_chat_models_shapes() -> None:
    ok = p.validate_params(
        p.ChatModels,
        {
            "models": [{"key": "k", "model": "m", "server": "Strata", "label": "M"}],
            "selected": None,
        },
    )
    assert ok.selected is None and ok.models[0].key == "k"
    for bad in ({"key": ""}, {}, {"key": "k", "x": 1}):
        with pytest.raises(p.BridgeError):
            p.validate_params(p.ChatSelectModelParams, bad)


def test_chat_models_keeps_a_null_selected_on_the_wire() -> None:
    # dump() drops None fields; selected is required by the 1.3 contract, so it stays as null.
    assert p.dump(p.ChatModels(models=[], selected=None)) == {"models": [], "selected": None}
