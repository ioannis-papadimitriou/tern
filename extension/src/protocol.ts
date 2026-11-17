// tern-bridge/1 wire types (architecture §3). Hand-written; checked against the golden
// transcripts in tests/golden/bridge/ (plan 02 step 9). Python's src/tern/bridge/protocol.py
// is the source of truth and exports the frozen JSON schema.

export const PROTOCOL = "tern-bridge/1";
/** The newest minor this extension speaks (tern-bridge/1.3: the chat model picker). */
export const MINOR = 3;

// TB reads at most 1 MiB per host→extension message; tern keeps frames < 512 KiB and the host
// splits larger messages into bridge.chunk notifications (S1, framing.py).
export const HOST_TO_EXT_MAX = 512 * 1024;
export const INBOUND_MAX = 64 * 1024 * 1024;

export type Role = "extension" | "daemon" | "mcp-proxy" | "cli";
export type Capability =
  | "mail.read"
  | "mail.write"
  | "drafts"
  | "contacts.read"
  | "contacts.write"
  | "calendar";

export interface Hello {
  protocol: typeof PROTOCOL;
  minor: number;
  role: Role;
  version: string;
  capabilities: Capability[];
  profile_id?: string;
}

export type ErrorName =
  | "unavailable"
  | "invalid_params"
  | "method_not_available"
  | "stale_id"
  | "targets_changed"
  | "tb_error"
  | "profile_mismatch"
  | "extension_already_connected"
  | "role_rejected"
  | "too_large"
  | "state_changed";

export const ERROR_CODES: Record<ErrorName, number> = {
  method_not_available: -32601,
  invalid_params: -32602,
  unavailable: -32000,
  stale_id: -32001,
  targets_changed: -32002,
  tb_error: -32003,
  profile_mismatch: -32004,
  extension_already_connected: -32005,
  role_rejected: -32006,
  too_large: -32007,
  state_changed: -32008,
};

export interface RpcError {
  code: number;
  message: string;
  data: { name: ErrorName; tb_code?: string };
}

export type RpcId = number | string;

export interface RpcRequest {
  jsonrpc: "2.0";
  id: RpcId;
  method: string;
  params: Record<string, unknown>;
}

export interface RpcNotification {
  jsonrpc: "2.0";
  method: string;
  params: Record<string, unknown>;
}

export interface RpcResponse {
  jsonrpc: "2.0";
  id: RpcId;
  result: unknown;
}

export interface RpcErrorResponse {
  jsonrpc: "2.0";
  id: RpcId | null;
  error: RpcError;
}

export type RpcMessage = RpcRequest | RpcNotification | RpcResponse | RpcErrorResponse;

export interface Chunk {
  chunk_id: string;
  seq: number;
  last: boolean;
  data_b64: string;
}

export interface PingStats {
  n: number;
  p50_ms: number;
  p90_ms: number;
  p99_ms: number;
  max_ms: number;
}

export class BridgeError extends Error {
  readonly code: ErrorName;
  readonly tbCode: string | undefined;
  /** The message as sent on the wire (Error.message carries the code in front, for logs). */
  readonly rpcMessage: string;

  constructor(code: ErrorName, message: string, tbCode?: string) {
    super(`${code}: ${message}`);
    this.code = code;
    this.tbCode = tbCode;
    this.rpcMessage = message;
  }

  toRpc(): RpcError {
    const data: RpcError["data"] = { name: this.code };
    if (this.tbCode !== undefined) data.tb_code = this.tbCode;
    return { code: ERROR_CODES[this.code], message: this.rpcMessage, data };
  }

  static fromRpc(err: RpcError): BridgeError {
    return new BridgeError(err.data.name, err.message, err.data.tb_code);
  }
}

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

/** Classify one decoded message; throws on anything that is not JSON-RPC 2.0. */
export function parseMessage(x: unknown): RpcMessage {
  if (!isObject(x) || x.jsonrpc !== "2.0") throw new Error("not a JSON-RPC 2.0 message");
  const params = x.params === undefined ? {} : x.params;
  if (typeof x.method === "string") {
    if (!isObject(params)) throw new Error("params must be an object");
    if ("id" in x) {
      if (typeof x.id !== "number" && typeof x.id !== "string") throw new Error("bad id");
      return { jsonrpc: "2.0", id: x.id, method: x.method, params };
    }
    return { jsonrpc: "2.0", method: x.method, params };
  }
  if ("error" in x) {
    const e = x.error;
    if (!isObject(e) || !isObject(e.data) || typeof e.data.name !== "string") {
      throw new Error("bad error object");
    }
    return x as unknown as RpcErrorResponse;
  }
  if ("result" in x && (typeof x.id === "number" || typeof x.id === "string")) {
    return { jsonrpc: "2.0", id: x.id, result: x.result };
  }
  throw new Error("not a request, notification or response");
}

// ---------------------------------------------------------------------------------------------
// R1 mail methods (daemon → extension) and mail events (extension → daemon), tern-bridge/1.0.
// Field names are the wire names. Dates are ISO 8601 strings in UTC.

/** `mail.messages.raw` above this size answers `too_large` (architecture §3: 25 MB). */
export const RAW_MAX = 25 * 1024 * 1024;

export interface Identity {
  identity_id: string;
  email: string;
  name: string;
  is_default: boolean;
}

export interface Account {
  account_id: string;
  name: string;
  type: string;
  identities: Identity[];
}

export interface Folder {
  folder_id: string;
  path: string;
  name: string;
  special_use: string[];
  total: number;
  unread: number;
}

export interface Flags {
  read: boolean;
  flagged: boolean;
  junk: boolean;
}

export interface PageItem {
  tb_id: number;
  header_message_id: string;
  date: string;
  size: number;
  flags: Flags;
  tags: string[];
  headers_only: boolean;
}

export interface Page {
  items: PageItem[];
  next_cursor?: string;
}

export interface Raw {
  raw_b64: string;
}

export interface TextPart {
  mime: string;
  text: string;
}

export interface AttachmentInfo {
  name: string;
  mime: string;
  size: number;
}

export interface FullMessage {
  headers: Record<string, string[]>;
  text_parts: TextPart[];
  attachments: AttachmentInfo[];
}

export interface FoundLocation {
  tb_id: number;
  folder_id: string;
  flags: Flags;
  tags: string[];
}

export interface Tag {
  key: string;
  tag: string;
  color: string;
}

export interface MailNew {
  folder_id: string;
  items: PageItem[];
}

export interface MailMoved {
  src_folder_id: string;
  dst_folder_id: string;
  items: PageItem[];
}

export interface MailDeleted {
  folder_id: string;
  header_message_ids: string[];
}

export interface UpdatedItem {
  tb_id: number;
  folder_id: string;
  header_message_id: string;
  flags: Flags;
  tags: string[];
}

export interface MailUpdated {
  items: UpdatedItem[];
}

export interface FolderChanged {
  folder_id: string;
}

// ---------------------------------------------------------------------------------------------
// Chat and status, tern-bridge/1.0 (plan 08b step 3). Mirrors of src/tern/bridge/protocol.py;
// test/chat_router.test.ts checks samples of each against the frozen 1.0 contract file.

/** `chat.send` params (e→d request). Both strings non-empty; ternd refuses text > 8000 chars. */
export interface ChatSendParams {
  session_id: string;
  text: string;
}

/** `chat.send` result and `chat.cancel` params. */
export interface TurnId {
  turn_id: string;
}

/** `chat.delta` (d→e notification): the next piece of the answer text. */
export interface ChatDelta {
  turn_id: string;
  text: string;
}

/** `chat.done` (d→e notification): the turn ended `ok`; `citations` are the valid mids. */
export interface ChatDone {
  turn_id: string;
  citations: string[];
}

/** `chat.error` (d→e notification): the turn ended otherwise; `reason` is the outcome code. */
export interface ChatErrorNotif {
  turn_id: string;
  reason: string;
}

/** `models.embed` "" = embedding disabled; `runtime_version` "" = not reported (plan 08b P7). */
export interface ModelIds {
  chat: string;
  embed: string;
  runtime_version: string;
}

export interface AccountSync {
  account_id: string;
  name: string;
  /** ISO 8601 UTC; null when no folder of the account was reconciled yet. */
  last_sync: string | null;
}

/** `index.status` result (e→d request, params `{}`). */
export interface IndexStatus {
  messages: number;
  locations: number;
  conflicted: number;
  parse_failed: number;
  headers_only: number;
  embed_pending: number;
  /** Age in seconds of the oldest TB event not applied yet (0 when caught up). */
  lag_s: number;
  last_reconcile: string | null;
  models: ModelIds;
  accounts: AccountSync[];
}

// tern-bridge/1.1 (decisions #37): opening a cited message, daemon info (e→d requests).

/** `ui.open_mid` params (role extension). */
export interface OpenMidParams {
  mid: string;
}

/** `ui.open_mid` result: the folder of the copy Thunderbird opened. */
export interface OpenMidResult {
  opened: true;
  folder_id: string;
}

/** `daemon.info` result (params `{}`). */
export interface DaemonInfo {
  /** allow_remote_llm is set: the chat tab shows the remote-model banner (#9). */
  remote_llm: boolean;
  chat_model: string;
}

// tern-bridge/1.2, staged (decisions #39): per-chat permission modes. Mirrors of the Python
// models; MINOR stays 1 until contracts/tern-bridge-1.2.schema.json is committed, so the router
// uses them only on a session that negotiated minor >= 2.

/** `manual`: ternd asks the tab (chat.approval) before each tool call; `bypass`: it does not. */
export type ChatMode = "manual" | "bypass";

/** `chat.start` params (e→d request, roles extension/cli): chat.send with a mode. */
export interface ChatStartParams {
  session_id: string;
  text: string;
  mode: ChatMode;
}

export type Tier = "R0" | "R1" | "W1" | "W2" | "W3";

/** `chat.approval` (d→e notification): a tool call of a manual turn waits for the user. */
export interface ChatApproval {
  turn_id: string;
  approval_id: string;
  nonce: string;
  tool: string;
  tier: Tier;
  /** The call's validated arguments: model-chosen data, shown as text only. */
  args: Record<string, unknown>;
  /** ISO 8601; the call is refused as expired after it. */
  expires_at: string;
}

/** `chat.approve` params (e→d request). `rest_of_turn` (approve only): ask no more this turn. */
export interface ChatApproveParams {
  turn_id: string;
  approval_id: string;
  nonce: string;
  decision: "approve" | "deny";
  rest_of_turn: boolean;
}

export type ApprovalStatus = "approved" | "denied" | "expired" | "targets_changed";

/** `chat.approve` result (the 1.0 ApprovalDecided shape). */
export interface ApprovalDecided {
  status: ApprovalStatus;
}

// tern-bridge/1.3: the chat model picker (chat.models, chat.select_model; e→d requests).

/** A chat model loaded on one of ternd's model servers. `key` is opaque (chat.select_model). */
export interface ChatModel {
  key: string;
  model: string;
  server: string;
  label: string;
}

/** `chat.models` / `chat.select_model` result. `selected`: null when that model is not listed. */
export interface ChatModels {
  models: ChatModel[];
  selected: string | null;
}

/** `chat.select_model` params. */
export interface ChatSelectModelParams {
  key: string;
}

// ---------------------------------------------------------------------------------------------
// Chat tab ↔ background, over one runtime port per open tab (name CHAT_PORT_NAME in tb/api.ts).
// Extension-internal, NOT tern-bridge: the background's ChatRouter (background/chat.ts) turns
// these into bridge requests and routes the bridge's chat notifications back to the owning tab.
// Every request carries a tab-chosen `req` number that its reply echoes.

export type PageRequest =
  /** `mode` absent: chat.send as before 1.2 (no asking). Present: chat.start on a 1.2 session. */
  | { type: "send"; req: number; session_id: string; text: string; mode?: ChatMode }
  /** The user's decision on a chat.approval card (chat.approve; bridge 1.2). */
  | {
      type: "approve";
      req: number;
      turn_id: string;
      approval_id: string;
      nonce: string;
      decision: "approve" | "deny";
      rest_of_turn: boolean;
    }
  | { type: "cancel"; req: number; turn_id: string }
  | { type: "status"; req: number }
  | { type: "openUrl"; req: number; url: string }
  /** A citation chip: open the message in Thunderbird (ui.open_mid; bridge 1.1). */
  | { type: "open"; req: number; mid: string }
  /** daemon.info (bridge 1.1). */
  | { type: "info"; req: number }
  /** chat.models (bridge 1.3). */
  | { type: "models"; req: number }
  /** chat.select_model (bridge 1.3). */
  | { type: "select_model"; req: number; key: string };

export type PageReply =
  | { type: "turn"; req: number; turn_id: string }
  | { type: "status_result"; req: number; status: IndexStatus }
  | { type: "info_result"; req: number; info: DaemonInfo }
  | { type: "models_result"; req: number; models: ChatModels }
  | { type: "approve_result"; req: number; status: ApprovalStatus }
  | { type: "ok"; req: number }
  | { type: "error"; req: number; name: ErrorName; message: string };

export type PageEvent =
  | { type: "delta"; turn_id: string; text: string }
  | { type: "done"; turn_id: string; citations: string[] }
  | { type: "chat_error"; turn_id: string; reason: string }
  /** chat.approval for a turn of this tab (bridge 1.2). */
  | {
      type: "approval";
      turn_id: string;
      approval_id: string;
      nonce: string;
      tool: string;
      tier: Tier;
      args: Record<string, unknown>;
      expires_at: string;
    }
  /** chat.models, fetched by the background after each successful `info` (bridge 1.3). */
  | { type: "models"; models: ChatModels }
  /** The daemon session ended: running turns are lost, and so may be the conversation. */
  | { type: "session_end" };

export type PageMessage = PageReply | PageEvent;
