// The chat router (plan 08b step 3, A4): between the chat tab pages (one runtime port each,
// named CHAT_PORT_NAME) and the bridge to ternd. e→d requests go out for the tab; the d→e chat
// notifications come back to the tab that owns the turn.
//
// - Only ports named CHAT_PORT_NAME whose sender is this extension's chat page are served; any
//   other runtime port (a future content or message-display script) is ignored.
// - Tab → bridge: `send` → chat.send → `turn{turn_id}` (or `error`); `cancel` → chat.cancel;
//   `status` → index.status; `openUrl` → the default browser, http:/https: only.
// - Tab → bridge 1.1 (decisions #37): `open {mid}` → ui.open_mid → `ok`; `info` → daemon.info →
//   `info_result`. On a session that negotiated minor 0 both answer `method_not_available`
//   and nothing is sent (the tab shows the 1.0 chip notice; ternd refuses remote-model chat).
// - #9 send gate (step 9b review): a tab may `send` only after it learned ternd's model setting in
//   the current bridge session (its `info` answered with `info_result`, or `method_not_available`
//   on a minor-0 session). The session ending clears that for every tab, so a send the tab posted
//   before it processed `session_end` never reaches a restarted ternd: it gets `unavailable`.
// - Permission modes, bridge 1.2 (decisions #39): a `send` with a `mode` goes out as chat.start
//   {session_id, text, mode} on a session at minor >= 2. On an older session a `bypass` send goes
//   out as chat.send (what bypass means), and a `manual` one is refused `method_not_available`
//   without sending anything: ternd could not ask, and running tools unasked is never a silent
//   fallback. A `send` without a mode is chat.send, as before 1.2.
// - Model picker, bridge 1.3: `models` → chat.models and `select_model {key}` →
//   chat.select_model, each → `models_result`. On a session below 1.3 both answer
//   `method_not_available` and nothing is sent (the tab shows no picker). After an `info` answer
//   or `status` answer on a 1.3 session the router also asks chat.models itself and sends the tab a
//   `models` event (a failure there is logged only: the tab just shows no picker yet), so the
//   picker follows models being loaded and unloaded.
// - chat.approval goes to the owning tab only (malformed: dropped with a log line, so no card can
//   show what ternd did not send). `approve` → chat.approve for a turn this tab owns
//   (another tab's or an unknown turn: `invalid_params`, nothing sent) → `approve_result {status}`.
// - Bridge not ready → `error unavailable` "ternd not connected", nothing sent.
// - chat.delta/done/error go to the owning tab only. A notification for a turn the router does
//   not know (ended, cancelled by a closed tab, or an ordering bug) is dropped with a log line
//   (turn id only; plan 08b P3: no buffering).
// - All tabs share one bridge peer, so ternd cannot tell tabs apart: the router refuses a
//   `cancel` of a turn another open tab owns.
// - A tab port that disconnects → chat.cancel for each of its running turns, and for a turn whose
//   chat.send answer arrives after the tab went away.
// - The bridge session ended (onSessionEnd) → every open tab gets `session_end`; the turns are
//   forgotten (ternd's sessions are gone or unreachable).
// - Logs carry turn ids, error codes and counts: never chat text, URLs or daemon messages.

import {
  type ApprovalStatus,
  BridgeError,
  type ChatApproveParams,
  type ChatMode,
  type ChatModel,
  type ChatModels,
  type ChatSendParams,
  type ChatStartParams,
  type DaemonInfo,
  type ErrorName,
  type IndexStatus,
  type OpenMidParams,
  type PageMessage,
  type PageRequest,
  type TurnId,
} from "../protocol";
import { CHAT_PORT_NAME } from "../tb/api";
import type { BridgeState, NotificationHandler } from "./bridge";
import type { ChatPlatform, RuntimePort } from "./platform";

/** The extension's space (TB spaces toolbar button) and the page it opens. */
export const CHAT_SPACE = { name: "tern", url: "ui/chat/index.html", title: "tern" } as const;

/** The slice of Bridge the router uses (the real Bridge in main.ts, a fake in tests). */
export interface ChatBridge {
  readonly state: BridgeState;
  /** The session's negotiated tern-bridge minor (Bridge.minor). */
  readonly minor: number;
  readonly notifications: Map<string, NotificationHandler>;
  request(method: string, params?: Record<string, unknown>): Promise<unknown>;
}

/** Schemes `openUrl` forwards. Anything else is refused. */
const OPEN_SCHEMES = new Set(["http:", "https:"]);
/** Schemes named in the refusal log line; any other scheme is logged as "other". */
const KNOWN_SCHEMES = new Set([
  "javascript:",
  "data:",
  "file:",
  "blob:",
  "about:",
  "mailto:",
  "ftp:",
  "moz-extension:",
  "chrome:",
  "resource:",
  "imap:",
  "mailbox:",
  "news:",
]);

interface Tab {
  readonly port: RuntimePort;
  readonly turns: Set<string>;
  open: boolean;
  /** The tab learned ternd's model setting in the current bridge session (#9 send gate). */
  infoChecked: boolean;
}

type Parsed = { ok: true; msg: PageRequest } | { ok: false; req: number | null };

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

const nonEmpty = (x: unknown): x is string => typeof x === "string" && x.length > 0;

/** tern-bridge's Mid pattern (protocol.py `Mid`). */
const MID_RE = /^(x-)?[0-9a-f]{64}$/;

/** The minor that has ui.open_mid and daemon.info. */
export const OPEN_MINOR = 1;

/** The minor that has chat.start, chat.approval and chat.approve (staged, decisions #39). */
export const MODE_MINOR = 2;

/** The minor that has chat.models and chat.select_model (the model picker). */
export const MODEL_MINOR = 3;

export const MANUAL_NEEDS_MINOR =
  "manual mode needs tern-bridge 1.2, which this ternd does not speak; nothing was sent";

const MODES: ReadonlySet<string> = new Set<ChatMode>(["manual", "bypass"]);
const TIERS: ReadonlySet<string> = new Set(["R0", "R1", "W1", "W2", "W3"]);
const STATUSES: ReadonlySet<string> = new Set<ApprovalStatus>([
  "approved",
  "denied",
  "expired",
  "targets_changed",
]);

/** Validate one tab message; `req` is returned when known, so the refusal can be answered. */
export function parsePageRequest(x: unknown): Parsed {
  if (!isObject(x) || typeof x.req !== "number" || !Number.isInteger(x.req)) {
    return { ok: false, req: null };
  }
  const req = x.req;
  switch (x.type) {
    case "send":
      if (nonEmpty(x.session_id) && nonEmpty(x.text)) {
        if (x.mode === undefined) {
          return { ok: true, msg: { type: "send", req, session_id: x.session_id, text: x.text } };
        }
        if (typeof x.mode === "string" && MODES.has(x.mode)) {
          const mode = x.mode as ChatMode;
          return {
            ok: true,
            msg: { type: "send", req, session_id: x.session_id, text: x.text, mode },
          };
        }
      }
      break;
    case "approve":
      if (
        nonEmpty(x.turn_id) &&
        nonEmpty(x.approval_id) &&
        nonEmpty(x.nonce) &&
        (x.decision === "approve" || x.decision === "deny") &&
        typeof x.rest_of_turn === "boolean" &&
        !(x.rest_of_turn && x.decision !== "approve")
      ) {
        return {
          ok: true,
          msg: {
            type: "approve",
            req,
            turn_id: x.turn_id,
            approval_id: x.approval_id,
            nonce: x.nonce,
            decision: x.decision,
            rest_of_turn: x.rest_of_turn,
          },
        };
      }
      break;
    case "cancel":
      if (nonEmpty(x.turn_id))
        return { ok: true, msg: { type: "cancel", req, turn_id: x.turn_id } };
      break;
    case "status":
      return { ok: true, msg: { type: "status", req } };
    case "openUrl":
      if (typeof x.url === "string") return { ok: true, msg: { type: "openUrl", req, url: x.url } };
      break;
    case "open":
      if (typeof x.mid === "string" && MID_RE.test(x.mid)) {
        return { ok: true, msg: { type: "open", req, mid: x.mid } };
      }
      break;
    case "info":
      return { ok: true, msg: { type: "info", req } };
    case "models":
      return { ok: true, msg: { type: "models", req } };
    case "select_model":
      if (nonEmpty(x.key)) return { ok: true, msg: { type: "select_model", req, key: x.key } };
      break;
  }
  return { ok: false, req };
}

/** A ChatModels value, validated and copied (unknown fields dropped); null when malformed. */
export function parseChatModels(x: unknown): ChatModels | null {
  if (!isObject(x) || !Array.isArray(x.models)) return null;
  if (!(x.selected === null || typeof x.selected === "string")) return null;
  const models: ChatModel[] = [];
  for (const m of x.models) {
    if (
      !isObject(m) ||
      typeof m.key !== "string" ||
      typeof m.model !== "string" ||
      typeof m.server !== "string" ||
      typeof m.label !== "string"
    ) {
      return null;
    }
    models.push({ key: m.key, model: m.model, server: m.server, label: m.label });
  }
  return { models, selected: x.selected };
}

/** The scheme of `url` for a log line: a known scheme, "other", or "unparseable". */
export function schemeForLog(url: string): string {
  let scheme: string;
  try {
    scheme = new URL(url).protocol;
  } catch (err) {
    if (err instanceof TypeError) return "unparseable"; // URL's only parse failure
    throw err;
  }
  return OPEN_SCHEMES.has(scheme) || KNOWN_SCHEMES.has(scheme) ? scheme : "other";
}

/** True for an absolute http: or https: URL (the only links the tab may open). */
export function isOpenableUrl(url: string): boolean {
  let scheme: string;
  try {
    scheme = new URL(url).protocol;
  } catch (err) {
    if (err instanceof TypeError) return false;
    throw err;
  }
  return OPEN_SCHEMES.has(scheme);
}

const errName = (err: unknown): string =>
  err instanceof Error ? err.name : typeof err === "object" ? "object" : typeof err;

export class ChatRouter {
  private readonly tabs = new Set<Tab>();
  /** Bumped when the bridge session ends: an answer from an older session sets no flag. */
  private session = 0;
  private readonly owners = new Map<string, Tab>();

  /**
   * `chatPageUrl`: the chat page's absolute URL (runtime.getURL(CHAT_SPACE.url)); only ports whose
   * sender is that page are served.
   */
  constructor(
    private readonly bridge: ChatBridge,
    private readonly openDefaultBrowser: (url: string) => Promise<void>,
    private readonly chatPageUrl: string,
    private readonly log: (msg: string) => void = (m) => console.info(`tern chat: ${m}`),
  ) {
    for (const method of ["chat.delta", "chat.done", "chat.error", "chat.approval"] as const) {
      if (bridge.notifications.has(method)) throw new Error(`${method} is already handled`);
    }
    bridge.notifications.set("chat.delta", (p) => this.onDelta(p));
    bridge.notifications.set("chat.done", (p) => this.onDone(p));
    bridge.notifications.set("chat.error", (p) => this.onError(p));
    bridge.notifications.set("chat.approval", (p) => this.onApproval(p));
  }

  /** Number of tabs connected now. */
  get openTabs(): number {
    return this.tabs.size;
  }

  /**
   * A runtime port connected (runtime.onConnect). Ports with another name, or not opened by the
   * chat page (sender URL without query or fragment), are ignored; the sender URL is not logged.
   */
  attach(port: RuntimePort): void {
    if (port.name !== CHAT_PORT_NAME) {
      this.log("runtime port ignored (not a chat port)");
      return;
    }
    const senderUrl = port.sender?.url;
    if (senderUrl === undefined || senderUrl.split(/[?#]/, 1)[0] !== this.chatPageUrl) {
      this.log("runtime port ignored (not from the chat page)");
      return;
    }
    const tab: Tab = { port, turns: new Set(), open: true, infoChecked: false };
    this.tabs.add(tab);
    port.onMessage.addListener((m) => void this.onTabMessage(tab, m));
    port.onDisconnect.addListener(() => this.detach(tab));
  }

  /** The bridge session ended: every open tab hears it; running turns are forgotten. */
  sessionEnded(): void {
    const lost = this.owners.size;
    this.owners.clear();
    this.session += 1;
    for (const tab of [...this.tabs]) {
      tab.turns.clear();
      tab.infoChecked = false;
      this.post(tab, { type: "session_end" });
    }
    if (lost > 0) this.log(`session ended with ${lost} running turn(s)`);
  }

  private detach(tab: Tab): void {
    if (!tab.open) return;
    tab.open = false;
    this.tabs.delete(tab);
    const turns = [...tab.turns];
    tab.turns.clear();
    for (const turnId of turns) {
      this.owners.delete(turnId);
      this.cancelOrphan(turnId);
    }
  }

  /** Cancel a turn nobody reads any more; failures are logged by code. */
  private cancelOrphan(turnId: string): void {
    if (this.bridge.state !== "ready") return; // the session is gone; so is the turn
    this.log(`cancelling turn ${turnId} (its tab closed)`);
    this.bridge.request("chat.cancel", { turn_id: turnId } satisfies TurnId).then(
      () => {},
      (err: unknown) => {
        if (!(err instanceof BridgeError)) throw err;
        this.log(`cancel of turn ${turnId} failed: ${err.code}`);
      },
    );
  }

  /** Post to a tab; a port that throws (closing) is treated as disconnected. */
  private post(tab: Tab, msg: PageMessage): void {
    if (!tab.open) return;
    try {
      tab.port.postMessage(msg);
    } catch (err) {
      this.log(`tab port post failed: ${errName(err)}`);
      this.detach(tab);
    }
  }

  private reply(tab: Tab, req: number, name: ErrorName, message: string): void {
    this.post(tab, { type: "error", req, name, message });
  }

  private async onTabMessage(tab: Tab, raw: unknown): Promise<void> {
    const parsed = parsePageRequest(raw);
    if (!parsed.ok) {
      if (parsed.req === null) this.log("malformed tab message dropped");
      else this.reply(tab, parsed.req, "invalid_params", "malformed request");
      return;
    }
    const msg = parsed.msg;
    if (msg.type === "openUrl") {
      await this.openUrl(tab, msg.req, msg.url);
      return;
    }
    if (msg.type === "cancel") {
      const owner = this.owners.get(msg.turn_id);
      if (owner !== undefined && owner !== tab) {
        this.reply(tab, msg.req, "invalid_params", "turn belongs to another tab");
        return;
      }
    }
    if (msg.type === "approve" && this.owners.get(msg.turn_id) !== tab) {
      // Only the tab that owns a running turn decides its cards (ternd checks the connection,
      // and all tabs share one).
      this.reply(tab, msg.req, "invalid_params", "no running turn of this tab has that id");
      return;
    }
    if (this.bridge.state !== "ready") {
      this.reply(tab, msg.req, "unavailable", "ternd not connected");
      return;
    }
    if ((msg.type === "open" || msg.type === "info") && this.bridge.minor < OPEN_MINOR) {
      if (msg.type === "info") tab.infoChecked = true; // a 1.0 session: ternd refuses remote chat
      this.reply(tab, msg.req, "method_not_available", "needs tern-bridge 1.1 (ternd is older)");
      return;
    }
    if ((msg.type === "models" || msg.type === "select_model") && this.bridge.minor < MODEL_MINOR) {
      this.reply(tab, msg.req, "method_not_available", "needs tern-bridge 1.3 (ternd is older)");
      return;
    }
    if (msg.type === "send" && !tab.infoChecked) {
      this.log("send refused: model setting not checked in this session");
      this.reply(tab, msg.req, "unavailable", "model setting not checked; ask info first");
      return;
    }
    const modes = this.bridge.minor >= MODE_MINOR;
    if (msg.type === "send" && msg.mode === "manual" && !modes) {
      this.log("send refused: manual mode on a session below 1.2");
      this.reply(tab, msg.req, "method_not_available", MANUAL_NEEDS_MINOR);
      return;
    }
    if (msg.type === "approve" && !modes) {
      this.reply(tab, msg.req, "method_not_available", "needs tern-bridge 1.2");
      return;
    }
    const session = this.session;
    let result: unknown;
    try {
      if (msg.type === "send" && msg.mode !== undefined && modes) {
        const params: ChatStartParams = {
          session_id: msg.session_id,
          text: msg.text,
          mode: msg.mode,
        };
        result = await this.bridge.request("chat.start", { ...params });
      } else if (msg.type === "send") {
        // No mode, or bypass on a session below 1.2: chat.send runs tools as before 1.2.
        const params: ChatSendParams = { session_id: msg.session_id, text: msg.text };
        result = await this.bridge.request("chat.send", { ...params });
      } else if (msg.type === "approve") {
        const params: ChatApproveParams = {
          turn_id: msg.turn_id,
          approval_id: msg.approval_id,
          nonce: msg.nonce,
          decision: msg.decision,
          rest_of_turn: msg.rest_of_turn,
        };
        result = await this.bridge.request("chat.approve", { ...params });
      } else if (msg.type === "cancel") {
        result = await this.bridge.request("chat.cancel", { turn_id: msg.turn_id });
      } else if (msg.type === "open") {
        const params: OpenMidParams = { mid: msg.mid };
        result = await this.bridge.request("ui.open_mid", { ...params });
      } else if (msg.type === "info") {
        result = await this.bridge.request("daemon.info", {});
      } else if (msg.type === "models") {
        result = await this.bridge.request("chat.models", {});
      } else if (msg.type === "select_model") {
        result = await this.bridge.request("chat.select_model", { key: msg.key });
      } else {
        result = await this.bridge.request("index.status", {});
      }
    } catch (err) {
      if (!(err instanceof BridgeError)) throw err;
      this.log(`${msg.type} failed: ${err.code}`);
      this.reply(tab, msg.req, err.code, err.rpcMessage);
      return;
    }
    if (msg.type === "send") this.started(tab, msg.req, result);
    else if (msg.type === "approve") this.decided(tab, msg.req, result);
    else if (msg.type === "cancel" || msg.type === "open")
      this.post(tab, { type: "ok", req: msg.req });
    else if (msg.type === "info") this.info(tab, msg.req, result, session);
    else if (msg.type === "models" || msg.type === "select_model")
      this.models(tab, msg.req, result);
    else {
      this.post(tab, { type: "status_result", req: msg.req, status: result as IndexStatus });
      // A model loaded or unloaded since: the picker follows each status refresh.
      if (session === this.session && this.bridge.minor >= MODEL_MINOR) void this.pushModels(tab);
    }
  }

  /** daemon.info's reply, checked: a malformed one fails the request (never a guessed banner). */
  private info(tab: Tab, req: number, result: unknown, session: number): void {
    if (
      !isObject(result) ||
      typeof result.remote_llm !== "boolean" ||
      typeof result.chat_model !== "string"
    ) {
      this.log("daemon.info answered with an invalid reply");
      this.reply(tab, req, "unavailable", "ternd sent an invalid reply");
      return;
    }
    const info: DaemonInfo = { remote_llm: result.remote_llm, chat_model: result.chat_model };
    // Only an answer from the current session counts (the tab drops older replies anyway).
    if (session === this.session) tab.infoChecked = true;
    this.post(tab, { type: "info_result", req, info });
    if (session === this.session && this.bridge.minor >= MODEL_MINOR) void this.pushModels(tab);
  }

  /** chat.models → a `models` event to `tab` (still open, same session); failures logged only. */
  private async pushModels(tab: Tab): Promise<void> {
    const session = this.session;
    let result: unknown;
    try {
      result = await this.bridge.request("chat.models", {});
    } catch (err) {
      if (!(err instanceof BridgeError)) throw err;
      this.log(`models failed: ${err.code}`);
      return;
    }
    const models = parseChatModels(result);
    if (models === null) {
      this.log("chat.models answered with an invalid reply");
      return;
    }
    if (tab.open && session === this.session) this.post(tab, { type: "models", models });
  }

  /** chat.models / chat.select_model's reply, checked: a malformed one fails the request. */
  private models(tab: Tab, req: number, result: unknown): void {
    const models = parseChatModels(result);
    if (models === null) {
      this.log("chat.models answered with an invalid reply");
      this.reply(tab, req, "unavailable", "ternd sent an invalid reply");
      return;
    }
    this.post(tab, { type: "models_result", req, models });
  }

  /** chat.approve's reply, checked: a malformed one fails the request (never a guessed status). */
  private decided(tab: Tab, req: number, result: unknown): void {
    if (!isObject(result) || typeof result.status !== "string" || !STATUSES.has(result.status)) {
      this.log("chat.approve answered with an invalid reply");
      this.reply(tab, req, "unavailable", "ternd sent an invalid reply");
      return;
    }
    this.post(tab, { type: "approve_result", req, status: result.status as ApprovalStatus });
  }

  private started(tab: Tab, req: number, result: unknown): void {
    if (!isObject(result) || !nonEmpty(result.turn_id)) {
      this.log("chat.send answered without a turn id");
      this.reply(tab, req, "unavailable", "ternd sent an invalid reply");
      return;
    }
    const turnId = result.turn_id;
    if (!tab.open) {
      this.cancelOrphan(turnId);
      return;
    }
    this.owners.set(turnId, tab);
    tab.turns.add(turnId);
    this.post(tab, { type: "turn", req, turn_id: turnId });
  }

  private async openUrl(tab: Tab, req: number, url: string): Promise<void> {
    if (!isOpenableUrl(url)) {
      this.log(`openUrl refused: scheme ${schemeForLog(url)}`);
      this.reply(tab, req, "invalid_params", "only http and https links open");
      return;
    }
    try {
      await this.openDefaultBrowser(url);
    } catch (err) {
      // TB's error text could echo the URL: logged and answered by type only.
      this.log(`openUrl failed: ${errName(err)}`);
      this.reply(tab, req, "unavailable", "could not open the default browser");
      return;
    }
    this.post(tab, { type: "ok", req });
  }

  /** The tab owning `params.turn_id`, or null (logged) for a malformed or unknown turn. */
  private ownerOf(method: string, params: Record<string, unknown>): Tab | null {
    const turnId = params.turn_id;
    if (!nonEmpty(turnId)) {
      this.log(`${method} without a turn id dropped`);
      return null;
    }
    const tab = this.owners.get(turnId);
    if (tab === undefined) {
      this.log(`${method} for unknown turn ${turnId} dropped`);
      return null;
    }
    return tab;
  }

  private onDelta(params: Record<string, unknown>): void {
    const tab = this.ownerOf("chat.delta", params);
    if (tab === null) return;
    if (typeof params.text !== "string") {
      this.log(`chat.delta without text dropped (turn ${String(params.turn_id)})`);
      return;
    }
    this.post(tab, { type: "delta", turn_id: params.turn_id as string, text: params.text });
  }

  /** chat.approval → the owning tab, only when every field is well-formed. */
  private onApproval(params: Record<string, unknown>): void {
    const tab = this.ownerOf("chat.approval", params);
    if (tab === null) return;
    const { approval_id, nonce, tool, tier, args, expires_at } = params;
    if (
      !nonEmpty(approval_id) ||
      !nonEmpty(nonce) ||
      !nonEmpty(tool) ||
      typeof tier !== "string" ||
      !TIERS.has(tier) ||
      !isObject(args) ||
      typeof expires_at !== "string"
    ) {
      this.log(`malformed chat.approval dropped (turn ${String(params.turn_id)})`);
      return;
    }
    this.post(tab, {
      type: "approval",
      turn_id: params.turn_id as string,
      approval_id,
      nonce,
      tool,
      tier: tier as "R0" | "R1" | "W1" | "W2" | "W3",
      args,
      expires_at,
    });
  }

  private end(params: Record<string, unknown>, tab: Tab): string {
    const turnId = params.turn_id as string;
    this.owners.delete(turnId);
    tab.turns.delete(turnId);
    return turnId;
  }

  private onDone(params: Record<string, unknown>): void {
    const tab = this.ownerOf("chat.done", params);
    if (tab === null) return;
    const turnId = this.end(params, tab);
    const cites = params.citations;
    if (!Array.isArray(cites) || !cites.every((c) => typeof c === "string")) {
      // Never show a turn's chips without the daemon's list: fail the turn visibly instead.
      this.log(`chat.done without a citation list (turn ${turnId})`);
      this.post(tab, { type: "chat_error", turn_id: turnId, reason: "internal" });
      return;
    }
    this.post(tab, { type: "done", turn_id: turnId, citations: cites as string[] });
  }

  private onError(params: Record<string, unknown>): void {
    const tab = this.ownerOf("chat.error", params);
    if (tab === null) return;
    const turnId = this.end(params, tab);
    const reason = typeof params.reason === "string" ? params.reason : "internal";
    if (typeof params.reason !== "string") this.log(`chat.error without a reason (turn ${turnId})`);
    this.post(tab, { type: "chat_error", turn_id: turnId, reason });
  }
}

/**
 * Start the chat tab plumbing (main.ts): the router, the runtime.onConnect listener (call this
 * synchronously at module load, S1 F14) and the space. Without the chat APIs (`platform` null)
 * nothing is registered and the error is logged; the mail bridge keeps working.
 */
export function startChat(
  platform: ChatPlatform | null,
  bridge: ChatBridge,
  log: (msg: string) => void = (m) => console.info(`tern chat: ${m}`),
  error: (msg: string) => void = (m) => console.error(`tern chat: ${m}`),
): ChatRouter | null {
  if (platform === null) {
    error("runtime.onConnect, spaces or windows missing: chat tab disabled");
    return null;
  }
  const router = new ChatRouter(
    bridge,
    (url) => platform.openDefaultBrowser(url),
    platform.extensionUrl(CHAT_SPACE.url),
    log,
  );
  platform.onConnect((port) => router.attach(port));
  platform.spaceEnsure(CHAT_SPACE.name, CHAT_SPACE.url, CHAT_SPACE.title).then(
    (how) => log(`space ${how}`),
    (err: unknown) => error(`space not created: ${errName(err)}`),
  );
  return router;
}
