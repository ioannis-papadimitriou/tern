"""`tern-bridge/1` protocol models (architecture §3, decisions #3).

Every method of minor 0 (``tern-bridge/1.0``) is declared in ``METHODS`` with typed params and
result. Later minors (``1.N``) only add entries; they never change a 1.0 shape. The frozen JSON
schema of each minor is ``export_schema("1.N")``, committed as
``contracts/tern-bridge-1.N.schema.json`` (a superset of the minors before it).

Minor 1 (``tern-bridge/1.1``, decisions #37; plan 07 part B and plan 08b): ``tools.list`` and
``tools.call`` (direction ``mcp2d``, role ``mcp-proxy`` only: how ``tern mcp`` reaches
``Registry.call``), ``audit.status``, ``ui.open_mid`` (role ``extension``: a citation chip opens
its message) and ``daemon.info`` (roles ``extension``, ``cli``). A tool's refusal or failure is
``tools.call`` result data (``ok: false``), not a bridge error: its codes are not in the 1.0 error
table, and a minor only adds methods.

Minor 2 (``tern-bridge/1.2``, decisions #39; ``contracts/tern-bridge-1.2.schema.json``): per-chat
permission modes. ``chat.start`` is ``chat.send`` with a required ``mode`` (``manual``/``bypass``;
a new method, because a minor never changes a 1.0 method's params and ``chat.send`` forbids extra
fields); in a ``manual`` turn ternd sends ``chat.approval`` before each tool call runs and the
client answers ``chat.approve``.

Messages are JSON-RPC 2.0. Both sides send ``hello`` first. Errors carry a §3 error name in
``error.data.name``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    ValidationError,
    model_validator,
)
from pydantic.json_schema import models_json_schema

from tern._model import BoundaryModel

PROTOCOL = "tern-bridge/1"
MINOR = 2  # the newest minor this build speaks
#: The newest minor whose models and methods this build defines and exports. Above :data:`MINOR`
#: while a new minor waits for its frozen contract file (1.2 was staged so, decisions #39, until
#: ``contracts/tern-bridge-1.2.schema.json`` was committed).
SCHEMA_MINOR = 2

# ---------------------------------------------------------------------------------------------
# Errors

ErrorName = Literal[
    "unavailable",
    "invalid_params",
    "method_not_available",
    "stale_id",
    "targets_changed",
    "tb_error",
    "profile_mismatch",
    "extension_already_connected",
    "role_rejected",
    "too_large",
    "state_changed",
]

# JSON-RPC error codes. -32601/-32602 are the JSON-RPC standard ones; the rest are tern's.
ERROR_CODES: dict[str, int] = {
    "method_not_available": -32601,
    "invalid_params": -32602,
    "unavailable": -32000,
    "stale_id": -32001,
    "targets_changed": -32002,
    "tb_error": -32003,
    "profile_mismatch": -32004,
    "extension_already_connected": -32005,
    "role_rejected": -32006,
    "too_large": -32007,
    "state_changed": -32008,
}


class ErrorData(BoundaryModel):
    name: ErrorName
    tb_code: str | None = None  # only for `tb_error`


class RpcError(BoundaryModel):
    code: int
    message: str
    data: ErrorData


class BridgeError(Exception):
    """A protocol-level error with a §3 name. ``message`` must never contain mail content."""

    def __init__(self, name: ErrorName, message: str, *, tb_code: str | None = None) -> None:
        super().__init__(f"{name}: {message}")
        self.name: ErrorName = name
        self.message = message
        self.tb_code = tb_code

    def to_rpc(self) -> RpcError:
        return RpcError(
            code=ERROR_CODES[self.name],
            message=self.message,
            data=ErrorData(name=self.name, tb_code=self.tb_code),
        )

    @classmethod
    def from_rpc(cls, err: RpcError) -> BridgeError:
        return cls(err.data.name, err.message, tb_code=err.data.tb_code)


# ---------------------------------------------------------------------------------------------
# Envelopes (JSON-RPC 2.0)

RpcId = int | str


class Request(BoundaryModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: RpcId
    method: str
    params: dict[str, JsonValue] = Field(default_factory=dict)


class Notification(BoundaryModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, JsonValue] = Field(default_factory=dict)


class Response(BoundaryModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: RpcId
    result: JsonValue


class ErrorResponse(BoundaryModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: RpcId | None
    error: RpcError


Message = Request | Notification | Response | ErrorResponse


def parse_message(obj: object) -> Message:
    """Classify and validate one decoded JSON-RPC message; raises ``ValueError`` if malformed."""
    if not isinstance(obj, dict):
        raise ValueError("a JSON-RPC message must be an object")
    keys = set(obj)  # pyright: ignore[reportUnknownArgumentType]  # validated just below
    try:
        if "method" in keys:
            return Request.model_validate(obj) if "id" in keys else Notification.model_validate(obj)
        if "error" in keys:
            return ErrorResponse.model_validate(obj)
        if "result" in keys:
            return Response.model_validate(obj)
    except ValidationError as exc:
        raise ValueError(f"malformed JSON-RPC message: {_locs(exc)}") from None
    raise ValueError("not a JSON-RPC request, notification or response")


# ---------------------------------------------------------------------------------------------
# Shared field types

Mid = Annotated[str, Field(pattern=r"^(x-)?[0-9a-f]{64}$")]
TbId = Annotated[int, Field(ge=0)]  # TB's runtime message id: this connection only, never stored
Role = Literal["extension", "daemon", "mcp-proxy", "cli"]
Capability = Literal[
    "mail.read", "mail.write", "drafts", "contacts.read", "contacts.write", "calendar"
]
Tier = Literal["R0", "R1", "W1", "W2", "W3"]
NonEmptyTbIds = Annotated[list[TbId], Field(min_length=1)]

MAX_DRAFT_ATTACHMENT_BYTES = 5 * 1024 * 1024  # §3: attachments <= 5 MB total
# §3: `mail.messages.raw` above this answers `too_large` (the extension's RAW_MAX, protocol.ts).
RAW_MAX = 25 * 1024 * 1024


class _List[T](RootModel[list[T]]):
    model_config = ConfigDict(hide_input_in_errors=True)


class Empty(BoundaryModel):
    pass


# ---------------------------------------------------------------------------------------------
# Handshake and transport


class Hello(BoundaryModel):
    protocol: Literal["tern-bridge/1"]
    minor: int = Field(ge=0)
    role: Role
    version: str = Field(min_length=1, max_length=64)
    capabilities: list[Capability]
    profile_id: UUID | None = None

    @model_validator(mode="after")
    def _profile_id_only_for_extension(self) -> Hello:
        if self.role == "extension" and self.profile_id is None:
            raise ValueError("an extension hello requires profile_id")
        if self.role != "extension" and self.profile_id is not None:
            raise ValueError("only an extension hello carries profile_id")
        return self


class PingExtensionParams(BoundaryModel):
    n: int = Field(ge=1, le=10000)


class PingStats(BoundaryModel):
    n: int
    p50_ms: float
    p90_ms: float
    p99_ms: float
    max_ms: float


class Chunk(BoundaryModel):
    chunk_id: str = Field(min_length=1, max_length=64)
    seq: int = Field(ge=0)
    last: bool
    data_b64: str


# ---------------------------------------------------------------------------------------------
# Mail (daemon -> extension)


class Identity(BoundaryModel):
    identity_id: str
    email: str
    name: str
    is_default: bool


class Account(BoundaryModel):
    account_id: str
    name: str
    type: str
    identities: list[Identity]


class AccountList(_List[Account]):
    pass


class FoldersListParams(BoundaryModel):
    account_id: str


class Folder(BoundaryModel):
    folder_id: str
    path: str
    name: str
    special_use: list[str]
    total: int = Field(ge=0)
    unread: int = Field(ge=0)


class FolderList(_List[Folder]):
    pass


class Flags(BoundaryModel):
    read: bool
    flagged: bool
    junk: bool


class PageItem(BoundaryModel):
    tb_id: TbId
    header_message_id: str
    date: datetime
    size: int = Field(ge=0)
    flags: Flags
    tags: list[str]
    # TB exposes no "body stored offline" property; `headersOnly` is what it has (S1 NOTES F1).
    headers_only: bool


class PageParams(BoundaryModel):
    folder_id: str
    from_date: datetime | None = None  # inclusive (S1 paging-pre)
    to_date: datetime | None = None  # inclusive (S1 paging-pre)
    cursor: str | None = None  # session-scoped, never persisted


class Page(BoundaryModel):
    items: list[PageItem]
    next_cursor: str | None = None


class TbIdParams(BoundaryModel):
    tb_id: TbId


class Raw(BoundaryModel):
    raw_b64: str


class TextPart(BoundaryModel):
    mime: str
    text: str


class AttachmentInfo(BoundaryModel):
    name: str
    mime: str
    size: int = Field(ge=0)


class FullMessage(BoundaryModel):
    headers: dict[str, list[str]]
    text_parts: list[TextPart]
    attachments: list[AttachmentInfo]


class FindParams(BoundaryModel):
    header_message_id: str = Field(min_length=1)


class FoundLocation(BoundaryModel):
    tb_id: TbId
    folder_id: str
    flags: Flags
    tags: list[str]


class FoundList(_List[FoundLocation]):
    pass


class Tag(BoundaryModel):
    key: str
    tag: str
    color: str


class TagList(_List[Tag]):
    pass


class TagCreateParams(BoundaryModel):
    tag: str = Field(min_length=1)
    color: str | None = None


class TagKey(BoundaryModel):
    key: str


class UpdateParams(BoundaryModel):
    tb_ids: NonEmptyTbIds
    add_tags: list[str] | None = None
    remove_tags: list[str] | None = None
    flagged: bool | None = None
    read: bool | None = None


class PrevState(BoundaryModel):
    tb_id: TbId
    tags: list[str]
    flagged: bool
    read: bool


class UpdateResult(BoundaryModel):
    prev_state: list[PrevState]


class DraftAttachment(BoundaryModel):
    name: str
    mime: str
    content_b64: str


class DraftSaveParams(BoundaryModel):
    identity_id: str
    to: list[str]
    cc: list[str]
    subject: str
    body_text: str
    in_reply_to_tb_id: TbId | None = None
    attachments: list[DraftAttachment] | None = None

    @model_validator(mode="after")
    def _attachments_within_limit(self) -> DraftSaveParams:
        total = sum(len(a.content_b64) * 3 // 4 for a in self.attachments or [])
        if total > MAX_DRAFT_ATTACHMENT_BYTES:
            raise ValueError("draft attachments exceed 5 MB in total")
        return self


class DraftSaved(BoundaryModel):
    tb_id: TbId


class ComposeDetails(BoundaryModel):
    to: list[str]
    cc: list[str]
    subject: str
    body_text: str


class ComposeOpenParams(BoundaryModel):
    mode: Literal["new", "reply", "forward", "draft"]
    ref_tb_id: TbId | None = None
    details: ComposeDetails


class MoveParams(BoundaryModel):
    tb_ids: NonEmptyTbIds
    dest_folder_id: str


class ArchiveParams(BoundaryModel):
    tb_ids: NonEmptyTbIds
    dest_folder_id: str | None = None


class Moved(BoundaryModel):
    tb_id: TbId
    new_tb_id: TbId | None = None


class MoveResult(BoundaryModel):
    moved: list[Moved]


class DeleteParams(BoundaryModel):
    tb_ids: NonEmptyTbIds


class DeleteResult(BoundaryModel):
    trashed: list[TbId]


# ---------------------------------------------------------------------------------------------
# Chat, approvals, search, status, audit (extension -> daemon) and their notifications


class ChatSendParams(BoundaryModel):
    session_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class TurnId(BoundaryModel):
    turn_id: str


class ChatDelta(BoundaryModel):
    turn_id: str
    text: str


class ChatDone(BoundaryModel):
    turn_id: str
    citations: list[Mid]


class ChatErrorNotif(BoundaryModel):
    turn_id: str
    reason: str


ApprovalStatus = Literal["approved", "denied", "expired", "targets_changed"]


class ApprovalsDecideParams(BoundaryModel):
    approval_id: str
    nonce: str
    decision: Literal["approve", "deny"]
    subset: list[Mid] | None = None


class ApprovalDecided(BoundaryModel):
    status: ApprovalStatus


class ApprovalTarget(BoundaryModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, populate_by_name=True)
    mid: Mid
    from_: str = Field(alias="from")
    subject: str
    date: datetime
    folder: str


class Recipient(BoundaryModel):
    addr: str
    name: str
    known: bool


class Provenance(BoundaryModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, populate_by_name=True)
    mid: Mid
    from_: str = Field(alias="from")
    subject: str


class ApprovalRequested(BoundaryModel):
    approval_id: str
    nonce: str
    tool: str
    tier: Tier
    targets: list[ApprovalTarget]
    args_view: dict[str, JsonValue]
    recipients: list[Recipient] | None = None
    provenance: list[Provenance]
    expires_at: datetime


class ApprovalList(_List[ApprovalRequested]):
    pass


class ApprovalResolved(BoundaryModel):
    approval_id: str
    status: ApprovalStatus


class SearchFilters(BoundaryModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, populate_by_name=True)
    folder_id: str | None = None
    from_: str | None = Field(default=None, alias="from")
    after: datetime | None = None
    before: datetime | None = None
    has_attachment: bool | None = None


class SearchQueryParams(BoundaryModel):
    query: str = Field(min_length=1)
    filters: SearchFilters | None = None
    limit: int = Field(default=10, ge=1, le=50)


class SearchHit(BoundaryModel):
    """Same shape as the `search_messages` tool output (architecture §5)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, populate_by_name=True)
    mid: Mid
    date: datetime
    from_: str = Field(alias="from")
    subject: str
    snippet: str
    score: float
    folders: list[str]


class SearchHitList(_List[SearchHit]):
    pass


class ModelIds(BoundaryModel):
    chat: str
    embed: str
    runtime_version: str


class AccountSync(BoundaryModel):
    account_id: str
    name: str
    last_sync: datetime | None


class IndexStatus(BoundaryModel):
    messages: int = Field(ge=0)
    locations: int = Field(ge=0)
    conflicted: int = Field(ge=0)
    parse_failed: int = Field(ge=0)
    headers_only: int = Field(ge=0)
    embed_pending: int = Field(ge=0)
    lag_s: float
    last_reconcile: datetime | None
    models: ModelIds
    accounts: list[AccountSync]


class AuditListParams(BoundaryModel):
    since: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)
    caller: str | None = None


class AuditEntry(BoundaryModel):
    audit_id: str
    ts: datetime
    caller: str
    tool: str
    tier: Tier
    target_count: int = Field(ge=0)
    decision: str
    status: Literal["tb_accepted", "observed", "error"]
    undo_token: str | None = None
    undone: bool


class AuditList(_List[AuditEntry]):
    pass


class UndoParams(BoundaryModel):
    undo_token: str


class UndoResult(BoundaryModel):
    undone: bool


class MailNew(BoundaryModel):
    folder_id: str
    items: list[PageItem]


class MailMoved(BoundaryModel):
    src_folder_id: str
    dst_folder_id: str
    items: list[PageItem]


class MailDeleted(BoundaryModel):
    folder_id: str
    header_message_ids: list[str]


class UpdatedItem(BoundaryModel):
    tb_id: TbId
    folder_id: str
    header_message_id: str
    flags: Flags
    tags: list[str]


class MailUpdated(BoundaryModel):
    items: list[UpdatedItem]


class FolderChanged(BoundaryModel):
    folder_id: str


# ---------------------------------------------------------------------------------------------
# Minor 1 (decisions #37): tools for the MCP proxy, audit status, opening a cited message,
# daemon info

# Docstrings of these models become ``description`` strings in the frozen 1.1 schema: they say
# what the wire means, nothing about the implementation (step 7 review).

#: A snake_case code (a ToolError's ``code``/``reason``; ``tern.perms.audit.CODE_RE``).
_Code = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


# Each entry is validated by the proxy with ``ToolMeta.model_validate`` (this module does not
# import ``tern.tools``, which imports it).
class ToolList(BoundaryModel):
    """Each entry is a tern-tool/1 tool metadata document; tools with a forbidden effect are not
    listed."""

    tools: list[dict[str, JsonValue]]


# ``tool`` is any string, not tern-tool/1's name pattern: the registry refuses a malformed name
# itself and audits it as ``<invalid>`` (#10: every MCP call leaves a row; step 7 review).
# ``client_name`` must also be printable (validator below; JSON Schema cannot say it).
class ToolCallParams(BoundaryModel):
    """client_name is a display label for the audit log only, never an id."""

    tool: str
    arguments: dict[str, JsonValue]
    client_name: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _client_name_printable(self) -> ToolCallParams:
        if self.client_name is not None and not self.client_name.isprintable():
            raise ValueError("client_name must be printable")
        return self


class ToolCallError(BoundaryModel):
    """A refused or failed tool call: content-free code, reason and message."""

    code: _Code
    reason: _Code | None = None
    message: str = Field(min_length=1)


# The exactly-one rule is a validator (JSON Schema shows two independent optional fields).
# No undo token: undo is the user's (role ui), never an MCP client's (step 7 review).
class ToolCallResult(BoundaryModel):
    """ok: output is set and error is absent; not ok: error is set and output is absent."""

    ok: bool
    output: dict[str, JsonValue] | None = None
    untrusted_output: bool
    call_id: str | None = None
    error: ToolCallError | None = None

    @model_validator(mode="after")
    def _exactly_one_of_output_and_error(self) -> ToolCallResult:
        if self.ok != (self.output is not None) or self.ok == (self.error is not None):
            raise ValueError("ok carries output and no error; not ok carries error and no output")
        return self


class AuditStatus(BoundaryModel):
    #: Executed writes still ``tb_accepted`` more than 24 h after their call (architecture §6).
    stale_writes: int = Field(ge=0)
    #: Open approval cards, every caller.
    pending_approvals: int = Field(ge=0)


class OpenMidParams(BoundaryModel):
    mid: Mid


class OpenMidResult(BoundaryModel):
    opened: Literal[True]
    #: The folder of the copy that was opened.
    folder_id: str


class DaemonInfo(BoundaryModel):
    #: ``allow_remote_llm`` is set: the chat tab shows #9's persistent remote-model banner.
    remote_llm: bool
    chat_model: str


# ---------------------------------------------------------------------------------------------
# Minor 2 (decisions #39): per-chat permission modes

# Docstrings of these models become ``description`` strings in the frozen 1.2 schema: they say
# what the wire means only.

ChatMode = Literal["manual", "bypass"]


class ChatStartParams(BoundaryModel):
    """chat.send with a permission mode. manual: before each tool call of the turn runs, the
    daemon sends chat.approval and waits for chat.approve; bypass: tool calls run without asking,
    as with chat.send. Neither lifts the per-action approval of W2/W3 tools."""

    session_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    mode: ChatMode


class ChatApproval(BoundaryModel):
    """A tool call of a manual-mode turn waits for the user's decision (chat.approve). args are
    the call's validated arguments: model-chosen data, to be shown as text only. After
    expires_at the call is refused as expired."""

    turn_id: str
    approval_id: str
    nonce: str
    tool: str
    tier: Tier
    args: dict[str, JsonValue]
    expires_at: datetime


class ChatApproveParams(BoundaryModel):
    """The decision on a chat.approval of a turn of this connection. rest_of_turn (approve only):
    every later tool call of the same turn runs without asking."""

    turn_id: str
    approval_id: str
    nonce: str
    decision: Literal["approve", "deny"]
    rest_of_turn: bool = False

    @model_validator(mode="after")
    def _rest_of_turn_only_with_approve(self) -> ChatApproveParams:
        if self.rest_of_turn and self.decision != "approve":
            raise ValueError("rest_of_turn goes with approve only")
        return self


# ---------------------------------------------------------------------------------------------
# Method table

Direction = Literal["d2e", "e2d", "any", "cli2d", "host2e", "mcp2d"]
Kind = Literal["request", "notification"]


@dataclass(frozen=True)
class MethodSpec:
    name: str
    kind: Kind
    direction: Direction
    params: type[BaseModel]
    result: type[BaseModel] | None  # None for notifications
    minor: int = 0
    # Roles that may send this to the daemon; None = no restriction beyond direction.
    roles: frozenset[Role] | None = None


def _req(
    name: str,
    direction: Direction,
    params: type[BaseModel],
    result: type[BaseModel],
    roles: frozenset[Role] | None = None,
    *,
    minor: int = 0,
) -> MethodSpec:
    return MethodSpec(name, "request", direction, params, result, minor=minor, roles=roles)


def _notif(
    name: str, direction: Direction, params: type[BaseModel], *, minor: int = 0
) -> MethodSpec:
    return MethodSpec(name, "notification", direction, params, None, minor=minor)


_SPECS: tuple[MethodSpec, ...] = (
    _req("hello", "any", Hello, Hello),
    _req("bridge.ping", "any", Empty, Empty),
    _req("bridge.ping_extension", "cli2d", PingExtensionParams, PingStats, frozenset({"cli"})),
    _notif("bridge.chunk", "host2e", Chunk),
    _notif("bridge.reconnected", "host2e", Empty),
    _req("mail.accounts.list", "d2e", Empty, AccountList),
    _req("mail.folders.list", "d2e", FoldersListParams, FolderList),
    _req("mail.messages.page", "d2e", PageParams, Page),
    _req("mail.messages.raw", "d2e", TbIdParams, Raw),
    _req("mail.messages.full", "d2e", TbIdParams, FullMessage),
    _req("mail.messages.find", "d2e", FindParams, FoundList),
    _req("mail.open", "d2e", TbIdParams, Empty),
    _req("mail.tags.list", "d2e", Empty, TagList),
    _req("mail.tags.create", "d2e", TagCreateParams, TagKey),
    _req("mail.update", "d2e", UpdateParams, UpdateResult),
    _req("mail.draft.save", "d2e", DraftSaveParams, DraftSaved),
    _req("mail.compose.open", "d2e", ComposeOpenParams, Empty),
    _req("mail.move", "d2e", MoveParams, MoveResult),
    _req("mail.archive", "d2e", ArchiveParams, MoveResult),
    _req("mail.delete", "d2e", DeleteParams, DeleteResult),
    _req("chat.send", "e2d", ChatSendParams, TurnId),
    _req("chat.cancel", "e2d", TurnId, Empty),
    _notif("chat.delta", "d2e", ChatDelta),
    _notif("chat.done", "d2e", ChatDone),
    _notif("chat.error", "d2e", ChatErrorNotif),
    _req(
        "approvals.decide",
        "e2d",
        ApprovalsDecideParams,
        ApprovalDecided,
        frozenset({"extension"}),
    ),
    _req("approvals.pending", "e2d", Empty, ApprovalList),
    _req("search.query", "e2d", SearchQueryParams, SearchHitList),
    _req("index.status", "e2d", Empty, IndexStatus),
    _req("audit.list", "e2d", AuditListParams, AuditList),
    _req("audit.undo", "e2d", UndoParams, UndoResult),
    _notif("event.mail.new", "e2d", MailNew),
    _notif("event.mail.moved", "e2d", MailMoved),
    _notif("event.mail.deleted", "e2d", MailDeleted),
    _notif("event.mail.updated", "e2d", MailUpdated),
    _notif("event.folder.changed", "e2d", FolderChanged),
    _notif("approval.requested", "d2e", ApprovalRequested),
    _notif("approval.resolved", "d2e", ApprovalResolved),
    # minor 1 (decisions #37)
    _req("tools.list", "mcp2d", Empty, ToolList, frozenset({"mcp-proxy"}), minor=1),
    _req("tools.call", "mcp2d", ToolCallParams, ToolCallResult, frozenset({"mcp-proxy"}), minor=1),
    _req("audit.status", "e2d", Empty, AuditStatus, minor=1),
    _req("ui.open_mid", "e2d", OpenMidParams, OpenMidResult, frozenset({"extension"}), minor=1),
    _req("daemon.info", "e2d", Empty, DaemonInfo, frozenset({"extension", "cli"}), minor=1),
    # minor 2 (decisions #39)
    _req("chat.start", "e2d", ChatStartParams, TurnId, frozenset({"extension", "cli"}), minor=2),
    _notif("chat.approval", "d2e", ChatApproval, minor=2),
    _req(
        "chat.approve",
        "e2d",
        ChatApproveParams,
        ApprovalDecided,
        frozenset({"extension", "cli"}),
        minor=2,
    ),
)

METHODS: Mapping[str, MethodSpec] = {s.name: s for s in _SPECS}


def negotiate_minor(local: int, remote: int) -> int:
    """Both sides use the lower minor (#3 amendment)."""
    return min(local, remote)


def method_spec(name: str, minor: int) -> MethodSpec:
    """The spec for ``name`` if it exists at ``minor``; otherwise ``method_not_available``."""
    spec = METHODS.get(name)
    if spec is None or spec.minor > minor:
        raise BridgeError(
            "method_not_available", f"method {name!r} is not in tern-bridge/1.{minor}"
        )
    return spec


def _locs(exc: ValidationError) -> str:
    """Field locations and error types only: never the input values (may be mail content)."""
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['type']}"
        for e in exc.errors(include_input=False, include_url=False)
    )


def validate_params[M: BaseModel](model: type[M], params: object) -> M:
    try:
        return model.model_validate(params)
    except ValidationError as exc:
        raise BridgeError("invalid_params", _locs(exc)) from None


def dump(model: BaseModel) -> Any:
    """Wire form: JSON-compatible, field aliases (``from``), unset optionals omitted."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


# ---------------------------------------------------------------------------------------------
# Schema export


def export_schema(version: str) -> dict[str, Any]:
    """The JSON schema document for ``tern-bridge/<version>``: ``1.0`` up to
    ``1.<SCHEMA_MINOR>``, each holding the methods of its minor and every minor before it."""
    known = [f"1.{n}" for n in range(SCHEMA_MINOR + 1)]
    if version not in known:
        raise ValueError(
            f"unknown tern-bridge version {version!r}; this build exports {', '.join(known)}"
        )
    minor = int(version.split(".")[1])
    specs = [s for s in _SPECS if s.minor <= minor]
    models: dict[str, type[BaseModel]] = {}
    for model in (Request, Notification, Response, ErrorResponse):
        models[model.__name__] = model
    for s in specs:
        models[s.params.__name__] = s.params
        if s.result is not None:
            models[s.result.__name__] = s.result
    _, defs = models_json_schema(
        [(m, "validation") for _, m in sorted(models.items())],
        ref_template="#/$defs/{model}",
    )
    methods: dict[str, Any] = {}
    for s in sorted(specs, key=lambda x: x.name):
        entry: dict[str, Any] = {
            "kind": s.kind,
            "direction": s.direction,
            "minor": s.minor,
            "params": {"$ref": f"#/$defs/{s.params.__name__}"},
        }
        if s.result is not None:
            entry["result"] = {"$ref": f"#/$defs/{s.result.__name__}"}
        if s.roles is not None:
            entry["roles"] = sorted(s.roles)
        methods[s.name] = entry
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"tern-bridge-{version}",
        "title": f"{PROTOCOL}.{minor} ({PROTOCOL} minor {minor})",
        "protocol": PROTOCOL,
        "minor": minor,
        "framing": {
            "length_prefix": "u32 little-endian",
            "encoding": "UTF-8 JSON",
            "host_to_extension_max_frame_bytes": 512 * 1024,
        },
        "errors": dict(sorted(ERROR_CODES.items())),
        "methods": methods,
        "$defs": defs.get("$defs", {}),
    }
