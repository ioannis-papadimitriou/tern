// Plan 08b step 5: the chat tab's state, as a pure reducer. `update(state, action)` returns the
// next state and the requests to post to the background (PageRequest, protocol.ts); main.ts
// posts them. Nothing here touches the DOM, the port or any persistence (main.ts saves the chats
// through history.ts and hands saved ones back with `restore`).
//
// - Chats: the shown chat is `sessionId` + `turns` (+ `seq`, `forgotten`); every other chat of the
//   tab is in `others`. Each chat is its own ternd session (its id is the session id sent with
//   `chat.send`). "New chat" and switching to another chat leave the shown one: a running turn is
//   cancelled (ternd) and kept as stopped, so at most one turn runs in the tab. ternd keeps only
//   its 8 most recent sessions and none across a restart, which the tab cannot see; so turns
//   restored from saved history, and every turn before a `session_end`, count as `forgotten`:
//   the view marks them as possibly unknown to the assistant. At most MAX_CHATS chats are kept
//   (the oldest by last activity go first).
//
// - Turns: pending (sent, no text yet) → streaming (deltas) → done | error | cancelled, keyed
//   by the daemon's turn id once the `turn` reply names it. One running turn at a time.
// - Every request carries a `req` number; `waiting` remembers what each one was for, so its reply
//   lands in the right place. `session_end` (ternd restarted or the native port died) and a
//   closed page port fail every request still waiting and every running turn: the bridge can
//   leave a request unsettled in both cases (plan 08b step 3 review), and the turns are gone.
// - Citations (invariant "invalid citations shown = 0"): while a turn streams, its chips are
//   shown as pending (the daemon's filter already checked them); when it ends `done`, a chip whose
//   mid is not in `chat.done.citations` becomes plain text; a turn that ended otherwise has no
//   list from the daemon, so all its chips become plain text (P32).
// - Bridge 1.1 (plan 07 step 9b, decisions #37): a chip click sends `open {mid}` (ui.open_mid);
//   only `method_not_available` (a session at minor 0) shows the 1.0 notice. While ternd's model
//   setting is `unknown` (a new tab, or after the session ended: ternd may have restarted with
//   another config), each status request also asks `info` (daemon.info): `known` → the
//   remote-model banner shows while `remote_llm` is true (#9); `legacy` (minor 0: ternd then
//   refuses remote-model chat itself). A send while `unknown` is refused locally and asks `info`
//   (fail closed: a remote model is never used without the banner).
// - Permission modes (decisions #39): each chat has a `mode`, `manual` (the default: ternd asks
//   before each tool call) or `bypass`, chosen in the header, saved with the chat and sent with
//   every `send`. A `chat.approval` adds an approval card to its running turn; Allow, Deny and
//   "Allow all for this turn" send `approve` once (the card is disabled while it waits and after
//   the answer). A card still undecided when its turn ends is closed (its call was refused or the
//   turn stopped). Cards are not saved.

import {
  type ApprovalStatus,
  type ChatMode,
  ERROR_CODES,
  type ErrorName,
  type IndexStatus,
  type PageMessage,
  type PageRequest,
  type Tier,
} from "../../protocol";
import { type Block, type Inline, type ListItem, sanitize, type TableCell } from "../sanitize";

/** ternd refuses longer `chat.send` text (plan 08b P4); the tab refuses it first. */
export const MAX_CHAT_TEXT = 8000;

export type Phase = "pending" | "streaming" | "done" | "error" | "cancelled";

/** A new chat's mode: ask before every tool call (decisions #39). */
export const DEFAULT_MODE: ChatMode = "manual";

/**
 * An approval card's state: `open` (buttons live), `sending` (a decision is on its way), the
 * daemon's answer, `failed` (the decision could not be sent), `closed` (the turn ended first).
 */
export type CardState = "open" | "sending" | ApprovalStatus | "failed" | "closed";

/** A tool call of a manual turn that waits (or waited) for the user (chat.approval). */
export interface ApprovalCard {
  readonly approvalId: string;
  readonly nonce: string;
  readonly tool: string;
  readonly tier: Tier;
  /** The arguments as shown: pretty JSON with invisible and direction controls escaped. */
  readonly argsText: string;
  readonly state: CardState;
  /** The user chose "Allow all for this turn" on this card. */
  readonly restOfTurn: boolean;
  /** The long arguments are shown in full. */
  readonly expanded: boolean;
}

/**
 * Characters that change how text looks without being seen (zero-width, bidi embeddings,
 * overrides and isolates, BOM): in the arguments shown on a card they become \uXXXX escapes, so
 * model-chosen text cannot hide or reorder what the user approves. JSON.stringify already
 * escapes the C0 controls.
 */
const HIDDEN_CHARS = /[\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]/g;

/** The text a card shows for `args`: 2-space JSON, hidden characters escaped. */
export function argsText(args: Readonly<Record<string, unknown>>): string {
  return JSON.stringify(args, null, 2).replace(
    HIDDEN_CHARS,
    (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

export interface Turn {
  /** Local key, unique within the tab. */
  readonly key: number;
  /** The user's message, shown as plain text. */
  readonly user: string;
  /** The daemon's turn id, from the `turn` reply. */
  readonly turnId: string | null;
  /** The answer so far: every delta, joined. */
  readonly text: string;
  readonly phase: Phase;
  /** Why the turn failed: a `chat.error` outcome, a bridge error name, `session_end`, `disconnected`. */
  readonly reason: string | null;
  /** The bridge error's message (ternd's or the router's own words, never mail text). */
  readonly detail: string | null;
  /** The daemon's valid mids, from `chat.done`. */
  readonly citations: readonly string[] | null;
  readonly stopRequested: boolean;
  /** The turn's approval cards, oldest first (manual mode; never saved). */
  readonly approvals?: readonly ApprovalCard[];
}

/** The most chats the tab keeps (shown one included); older ones are dropped, saved ones too. */
export const MAX_CHATS = 50;

/** A chat that is not shown. */
export interface Chat {
  /** Its ternd session id. */
  readonly id: string;
  /** Last-activity order: higher is newer (the sidebar lists newest first). */
  readonly seq: number;
  readonly turns: readonly Turn[];
  /** How many leading turns the assistant may no longer remember (module comment). */
  readonly forgotten: number;
  /** Its permission mode; absent = DEFAULT_MODE. */
  readonly mode?: ChatMode;
}

/** A finished turn as saved by history.ts (no keys, no turn ids). */
export interface SavedTurn {
  readonly user: string;
  readonly text: string;
  readonly phase: "done" | "error" | "cancelled";
  readonly reason: string | null;
  readonly detail: string | null;
  readonly citations: readonly string[] | null;
}

export interface SavedChat {
  readonly id: string;
  readonly seq: number;
  readonly turns: readonly SavedTurn[];
  /** Absent = DEFAULT_MODE (entries saved before modes, and manual chats). */
  readonly mode?: ChatMode;
}

/** A code block, table or long list opened in the side panel (a snapshot of its text). */
export interface Artifact {
  /** What the panel's title shows, e.g. `python` or `table`. */
  readonly title: string;
  /** The text Copy and Download use. */
  readonly text: string;
  /** The file name Download suggests. */
  readonly filename: string;
}

export type Waiting =
  | { readonly kind: "send"; readonly turnKey: number }
  | { readonly kind: "cancel"; readonly turnKey: number | null }
  | { readonly kind: "status" }
  | { readonly kind: "openUrl" }
  | { readonly kind: "open"; readonly mid: string }
  /** `forSend`: asked because a send was refused (its failure gets a notice). */
  | { readonly kind: "info"; readonly forSend: boolean }
  | { readonly kind: "approve"; readonly approvalId: string };

/** What the tab knows of ternd's model setting (daemon.info). */
export type InfoState =
  /**
   * `remoteModel`: the setting was "remote" (that model) before it became unknown; the banner
   * stays, marked re-checking, until a fresh answer says local (#9: persistent).
   */
  | { readonly kind: "unknown"; readonly remoteModel?: string }
  /** The session negotiated minor 0: no daemon.info (ternd refuses remote-model chat itself). */
  | { readonly kind: "legacy" }
  | { readonly kind: "known"; readonly remoteLlm: boolean; readonly chatModel: string };

export interface Notice {
  readonly id: number;
  readonly level: "info" | "error";
  readonly text: string;
}

export interface ChatState {
  readonly sessionId: string;
  readonly turns: readonly Turn[];
  readonly waiting: ReadonlyMap<number, Waiting>;
  readonly nextReq: number;
  readonly nextKey: number;
  readonly nextNotice: number;
  readonly status: IndexStatus | null;
  /** Why the last status request failed (error name), null after a good one. */
  readonly statusError: string | null;
  readonly notices: readonly Notice[];
  /** The http(s) URL waiting for the user's confirm (P9), or null. */
  readonly confirm: string | null;
  readonly info: InfoState;
  /** The shown chat's last-activity order (Chat.seq). */
  readonly seq: number;
  /** The shown chat's turns the assistant may no longer remember (Chat.forgotten). */
  readonly forgotten: number;
  /** Every other chat of the tab, any order. */
  readonly others: readonly Chat[];
  readonly nextSeq: number;
  /** The sidebar is flipped from its default (shown on wide pages, hidden on narrow ones). */
  readonly sidebarToggled: boolean;
  /** The chat whose delete button waits for a second click, or null. */
  readonly deleting: string | null;
  /** The artifact shown in the side panel, or null. */
  readonly artifact: Artifact | null;
  /** The key of the copy button that just copied (it says "Copied"), or null. */
  readonly copied: string | null;
  /** The shown chat's permission mode (decisions #39). */
  readonly mode: ChatMode;
}

export type Action =
  | { readonly type: "submit"; readonly text: string }
  /** The shown chat's mode, from the header's switch (applies from its next message). */
  | { readonly type: "set_mode"; readonly mode: ChatMode }
  /** A button on an approval card of the running turn. */
  | {
      readonly type: "decide";
      readonly approvalId: string;
      readonly decision: "approve" | "deny";
      readonly restOfTurn: boolean;
    }
  /** Show a card's long arguments in full. */
  | { readonly type: "expand_args"; readonly approvalId: string }
  | { readonly type: "stop" }
  | { readonly type: "refresh" }
  | { readonly type: "new_chat"; readonly sessionId: string }
  | { readonly type: "switch_chat"; readonly id: string }
  /** First click on a chat's delete button (null: never mind). */
  | { readonly type: "ask_delete"; readonly id: string | null }
  /** Delete chat `id`; when it is the shown one, a new chat with `sessionId` replaces it. */
  | { readonly type: "delete_chat"; readonly id: string; readonly sessionId: string }
  /** Saved chats read at startup; `unreadable` saved entries were left out. */
  | { readonly type: "restore"; readonly chats: readonly SavedChat[]; readonly unreadable: number }
  | { readonly type: "toggle_sidebar" }
  | { readonly type: "open_artifact"; readonly artifact: Artifact }
  | { readonly type: "close_artifact" }
  | { readonly type: "copied"; readonly key: string | null }
  /** Effects main.ts runs (the clipboard, a file download); the reducer leaves state as is. */
  | { readonly type: "copy"; readonly key: string; readonly text: string }
  | { readonly type: "download"; readonly artifact: Artifact }
  /** A notice from main.ts (a copy, download or save that failed). */
  | { readonly type: "notice"; readonly level: Notice["level"]; readonly text: string }
  | { readonly type: "chip"; readonly mid: string }
  | { readonly type: "link"; readonly href: string }
  | { readonly type: "confirm_open" }
  | { readonly type: "confirm_cancel" }
  | { readonly type: "dismiss"; readonly id: number }
  | { readonly type: "message"; readonly msg: PageMessage }
  /** The page's runtime port to the background disconnected (main.ts reconnects). */
  | { readonly type: "port_closed" }
  /** A message that failed validation but names a `req` (replyReq): that request fails. */
  | { readonly type: "malformed_reply"; readonly req: number };

export interface Update {
  readonly state: ChatState;
  readonly out: readonly PageRequest[];
}

/** A chip click on a session at bridge minor 0 (the router answered method_not_available). */
export const CHIP_NOTICE =
  "Opening from the tab needs tern-bridge 1.1, which this ternd does not speak; you can ask the " +
  "assistant to open it";

export const INFO_PENDING_NOTICE =
  "Checking ternd's model setting before sending; nothing was sent. Send again in a moment.";

/** #9's persistent banner while ternd's config allows a remote model. */
export function remoteBannerText(chatModel: string): string {
  return (
    `Remote model: allow_remote_llm is set, so your questions and the mail text tern retrieves ` +
    `are sent to the model ${chatModel === "" ? "configured" : chatModel} off this computer.`
  );
}

/**
 * The banner text to show now, or null: a known remote_llm=true shows it, and so does an unknown
 * setting that was remote before (re-checking), so the banner never blinks off while ternd may
 * still use the remote model.
 */
export function remoteBanner(state: ChatState): string | null {
  const info = state.info;
  if (info.kind === "known" && info.remoteLlm) return remoteBannerText(info.chatModel);
  if (info.kind === "unknown" && info.remoteModel !== undefined) {
    return `${remoteBannerText(info.remoteModel)} (Re-checking ternd's setting.)`;
  }
  return null;
}

/** The unknown setting that follows `info` (keeping a remote model for the banner). */
function forgotten(info: InfoState): InfoState {
  if (info.kind === "known" && info.remoteLlm)
    return { kind: "unknown", remoteModel: info.chatModel };
  if (info.kind === "unknown") return info;
  return { kind: "unknown" };
}

export const SESSION_END_NOTICE =
  "The connection to ternd was reset: a running answer stopped, and the assistant may have lost " +
  "this conversation's context. Start a new chat if its answers lose track.";

export const PORT_CLOSED_NOTICE =
  "Lost the connection to the tern background page; reconnecting. A running answer stopped.";

/** Shown below turns the assistant may no longer remember (module comment). */
export const FORGOTTEN_NOTE =
  "The assistant may not remember the messages above (saved history, or ternd restarted). " +
  "Start a new chat if its answers lose track.";

export function initialState(sessionId: string): ChatState {
  return {
    sessionId,
    seq: 1,
    forgotten: 0,
    others: [],
    nextSeq: 2,
    sidebarToggled: false,
    deleting: null,
    artifact: null,
    copied: null,
    turns: [],
    waiting: new Map(),
    nextReq: 1,
    nextKey: 1,
    nextNotice: 1,
    status: null,
    statusError: null,
    notices: [],
    confirm: null,
    info: { kind: "unknown" },
    mode: DEFAULT_MODE,
  };
}

/** A manual send to a ternd that cannot ask (bridge below 1.2): nothing was sent. */
export const MANUAL_UNAVAILABLE_NOTICE =
  "Manual mode needs a newer ternd (tern-bridge 1.2), so nothing was sent: tools would run " +
  "without asking. Switch this chat to Bypass to send anyway.";

/** A mid as shown on a chip and in notices: its first 8 characters after any `x-` prefix. */
export function shortMid(mid: string): string {
  const body = mid.startsWith("x-") ? mid.slice(2) : mid;
  return body.length > 8 ? `${body.slice(0, 8)}…` : body;
}

const RUNNING: ReadonlySet<Phase> = new Set(["pending", "streaming"]);

export function isRunning(turn: Turn): boolean {
  return RUNNING.has(turn.phase);
}

/** The running turn, if any (at most one). */
export function runningTurn(state: ChatState): Turn | null {
  return state.turns.find(isRunning) ?? null;
}

// --- building blocks ------------------------------------------------------------------------

class Draft {
  state: ChatState;
  readonly out: PageRequest[] = [];
  constructor(state: ChatState) {
    this.state = state;
  }

  /** Allocate a `req`, remember what it is for, and queue the request. */
  request(w: Waiting, make: (req: number) => PageRequest): void {
    const req = this.state.nextReq;
    const waiting = new Map(this.state.waiting);
    waiting.set(req, w);
    this.state = { ...this.state, nextReq: req + 1, waiting };
    this.out.push(make(req));
  }

  /** Forget a request; returns what it was for (undefined when unknown). */
  settle(req: number): Waiting | undefined {
    const w = this.state.waiting.get(req);
    if (w === undefined) return undefined;
    const waiting = new Map(this.state.waiting);
    waiting.delete(req);
    this.state = { ...this.state, waiting };
    return w;
  }

  notice(level: Notice["level"], text: string): void {
    const id = this.state.nextNotice;
    this.state = {
      ...this.state,
      nextNotice: id + 1,
      notices: [...this.state.notices, { id, level, text }],
    };
  }

  patchTurn(pred: (t: Turn) => boolean, patch: (t: Turn) => Partial<Turn>): boolean {
    let hit = false;
    const turns = this.state.turns.map((t) => {
      if (hit || !pred(t)) return t;
      hit = true;
      return { ...t, ...patch(t) };
    });
    if (hit) this.state = { ...this.state, turns };
    return hit;
  }

  /** A status request, plus an info request while ternd's model setting is unknown (each at
   *  most once in flight). */
  status(): void {
    let asked = false;
    for (const w of this.state.waiting.values()) if (w.kind === "status") asked = true;
    if (!asked) this.request({ kind: "status" }, (req) => ({ type: "status", req }));
    if (this.state.info.kind === "unknown") this.info();
  }

  info(forSend = false): void {
    for (const w of this.state.waiting.values()) if (w.kind === "info") return;
    this.request({ kind: "info", forSend }, (req) => ({ type: "info", req }));
  }

  /** A notice, unless the same text is already shown. */
  noticeOnce(level: Notice["level"], text: string): void {
    if (this.state.notices.some((n) => n.text === text)) return;
    this.notice(level, text);
  }

  cancel(turnKey: number | null, turnId: string): void {
    this.request({ kind: "cancel", turnKey }, (req) => ({ type: "cancel", req, turn_id: turnId }));
  }

  /**
   * Leave the shown chat: a running turn is cancelled and kept as stopped, and a chat with turns
   * moves to `others`. The caller then shows another chat.
   */
  leave(): void {
    const run = runningTurn(this.state);
    if (run?.turnId) this.cancel(null, run.turnId);
    // A send not answered yet keeps its waiting entry: its `turn` reply finds no shown turn and
    // is cancelled then (onTurn).
    const s = this.state;
    const turns = s.turns.map((t) =>
      isRunning(t)
        ? closeCards({ ...t, phase: "cancelled" as const, reason: null, stopRequested: false })
        : t,
    );
    const others =
      turns.length > 0
        ? [
            ...s.others,
            { id: s.sessionId, seq: s.seq, turns, forgotten: s.forgotten, mode: s.mode },
          ]
        : s.others;
    this.state = { ...s, turns: [], others };
  }

  /** Patch the card `approvalId` of whichever turn has it; false when no turn does. */
  patchCard(approvalId: string, patch: (c: ApprovalCard) => Partial<ApprovalCard>): boolean {
    return this.patchTurn(
      (t) => (t.approvals ?? []).some((c) => c.approvalId === approvalId),
      (t) => ({
        approvals: (t.approvals ?? []).map((c) =>
          c.approvalId === approvalId ? { ...c, ...patch(c) } : c,
        ),
      }),
    );
  }

  /** Show `chat`, closing what belonged to the chat left. */
  show(chat: Chat): void {
    this.state = {
      ...this.state,
      sessionId: chat.id,
      seq: chat.seq,
      turns: chat.turns,
      forgotten: chat.forgotten,
      mode: chat.mode ?? DEFAULT_MODE,
      others: this.state.others.filter((c) => c.id !== chat.id),
      confirm: null,
      notices: [],
      deleting: null,
      artifact: null,
      copied: null,
      sidebarToggled: false,
    };
  }

  /** A new empty chat with `sessionId`, newest of all. */
  fresh(sessionId: string): void {
    const seq = this.state.nextSeq;
    this.state = { ...this.state, nextSeq: seq + 1 };
    this.show({ id: sessionId, seq, turns: [], forgotten: 0 });
  }

  /** Keep the MAX_CHATS - 1 newest other chats. */
  trim(): void {
    const others = this.state.others;
    if (others.length < MAX_CHATS) return;
    const kept = [...others].sort((a, b) => b.seq - a.seq).slice(0, MAX_CHATS - 1);
    this.state = { ...this.state, others: kept };
  }

  /** Every waiting request fails and every running turn ends `error` with `reason`. */
  failAll(reason: string): void {
    const failedSends = new Set<number>();
    for (const w of this.state.waiting.values()) if (w.kind === "send") failedSends.add(w.turnKey);
    const hadStatus = [...this.state.waiting.values()].some((w) => w.kind === "status");
    const turns = this.state.turns.map((t) =>
      isRunning(t) || failedSends.has(t.key)
        ? closeCards({ ...t, phase: "error" as const, reason, detail: null, stopRequested: false })
        : t,
    );
    this.state = {
      ...this.state,
      turns,
      waiting: new Map(),
      statusError: hadStatus ? reason : this.state.statusError,
      // ternd may come back with another config: ask again before the next send.
      info: forgotten(this.state.info),
    };
  }

  done(): Update {
    return { state: this.state, out: this.out };
  }
}

/** `turn` with every card still waiting for a decision closed (its turn ended). */
function closeCards(turn: Turn): Turn {
  if (turn.approvals === undefined) return turn;
  const live: ReadonlySet<CardState> = new Set<CardState>(["open", "sending"]);
  return {
    ...turn,
    approvals: turn.approvals.map((c) => (live.has(c.state) ? { ...c, state: "closed" } : c)),
  };
}

// --- the reducer ----------------------------------------------------------------------------

export function update(state: ChatState, action: Action): Update {
  const d = new Draft(state);
  switch (action.type) {
    case "submit":
      submit(d, action.text);
      break;
    case "set_mode":
      d.state = { ...d.state, mode: action.mode };
      break;
    case "decide":
      decide(d, action.approvalId, action.decision, action.restOfTurn);
      break;
    case "expand_args":
      d.patchCard(action.approvalId, () => ({ expanded: true }));
      break;
    case "stop":
      stop(d);
      break;
    case "refresh":
      d.status();
      break;
    case "new_chat":
      d.leave();
      d.fresh(action.sessionId);
      d.trim();
      break;
    case "switch_chat": {
      if (action.id === d.state.sessionId) {
        d.state = { ...d.state, sidebarToggled: false, deleting: null };
        break;
      }
      const target = d.state.others.find((c) => c.id === action.id);
      if (target === undefined) break;
      d.leave();
      d.show(target);
      break;
    }
    case "ask_delete":
      d.state = { ...d.state, deleting: action.id };
      break;
    case "delete_chat":
      if (action.id === d.state.sessionId) {
        const run = runningTurn(d.state);
        if (run?.turnId) d.cancel(null, run.turnId);
        d.fresh(action.sessionId);
      } else {
        d.state = { ...d.state, others: d.state.others.filter((c) => c.id !== action.id) };
      }
      d.state = { ...d.state, deleting: null };
      break;
    case "restore":
      restore(d, action.chats, action.unreadable);
      break;
    case "toggle_sidebar":
      d.state = { ...d.state, sidebarToggled: !d.state.sidebarToggled, deleting: null };
      break;
    case "open_artifact":
      d.state = { ...d.state, artifact: action.artifact };
      break;
    case "close_artifact":
      d.state = { ...d.state, artifact: null };
      break;
    case "copied":
      d.state = { ...d.state, copied: action.key };
      break;
    case "notice":
      d.notice(action.level, action.text);
      break;
    case "copy":
    case "download":
      break; // main.ts runs them; their outcome comes back as `copied` or `notice`
    case "chip": {
      const mid = action.mid;
      // A double click opens once: no second request while one for this mid waits.
      for (const w of d.state.waiting.values())
        if (w.kind === "open" && w.mid === mid) return d.done();
      d.request({ kind: "open", mid }, (req) => ({ type: "open", req, mid }));
      break;
    }
    case "link":
      d.state = { ...d.state, confirm: action.href };
      break;
    case "confirm_open": {
      const href = d.state.confirm;
      if (href !== null) {
        d.state = { ...d.state, confirm: null };
        d.request({ kind: "openUrl" }, (req) => ({ type: "openUrl", req, url: href }));
      }
      break;
    }
    case "confirm_cancel":
      d.state = { ...d.state, confirm: null };
      break;
    case "dismiss":
      d.state = { ...d.state, notices: d.state.notices.filter((n) => n.id !== action.id) };
      break;
    case "message":
      onMessage(d, action.msg);
      break;
    case "port_closed":
      d.failAll("disconnected");
      d.notice("error", PORT_CLOSED_NOTICE);
      break;
    case "malformed_reply": {
      const w = d.settle(action.req);
      if (w !== undefined) onError(d, w, "internal", "malformed reply");
      break;
    }
  }
  return d.done();
}

function submit(d: Draft, text: string): void {
  if (text.trim() === "") return;
  if (runningTurn(d.state) !== null) {
    d.notice("info", "Wait for the answer, or press Stop.");
    return;
  }
  if (d.state.info.kind === "unknown") {
    d.noticeOnce("info", INFO_PENDING_NOTICE);
    d.info(true);
    return;
  }
  if (text.length > MAX_CHAT_TEXT) {
    d.notice(
      "error",
      `Message too long: ${text.length} characters (at most ${MAX_CHAT_TEXT}). Nothing was sent.`,
    );
    return;
  }
  const key = d.state.nextKey;
  const seq = d.state.nextSeq;
  d.state = { ...d.state, seq, nextSeq: seq + 1 };
  const turn: Turn = {
    key,
    user: text,
    turnId: null,
    text: "",
    phase: "pending",
    reason: null,
    detail: null,
    citations: null,
    stopRequested: false,
  };
  d.state = { ...d.state, nextKey: key + 1, turns: [...d.state.turns, turn] };
  const sessionId = d.state.sessionId;
  const mode = d.state.mode;
  d.request({ kind: "send", turnKey: key }, (req) => ({
    type: "send",
    req,
    session_id: sessionId,
    text,
    mode,
  }));
}

/** A card button: one `approve` request; the card waits (disabled) until its answer. */
function decide(
  d: Draft,
  approvalId: string,
  decision: "approve" | "deny",
  restOfTurn: boolean,
): void {
  const run = runningTurn(d.state);
  const card = run?.approvals?.find((c) => c.approvalId === approvalId);
  const live = card?.state === "open" || card?.state === "failed"; // failed: try again
  if (run === null || run.turnId === null || card === undefined || !live) return;
  const turnId = run.turnId;
  const rest = restOfTurn && decision === "approve";
  d.patchCard(approvalId, () => ({ state: "sending", restOfTurn: rest }));
  d.request({ kind: "approve", approvalId }, (req) => ({
    type: "approve",
    req,
    turn_id: turnId,
    approval_id: approvalId,
    nonce: card.nonce,
    decision,
    rest_of_turn: rest,
  }));
}

/** Saved chats join `others` (never replacing a chat the tab already has). */
function restore(d: Draft, chats: readonly SavedChat[], unreadable: number): void {
  const known = new Set([d.state.sessionId, ...d.state.others.map((c) => c.id)]);
  let nextKey = d.state.nextKey;
  let nextSeq = d.state.nextSeq;
  const added: Chat[] = [];
  for (const c of chats) {
    if (known.has(c.id)) continue;
    known.add(c.id);
    const turns = c.turns.map(
      (t): Turn => ({
        key: nextKey++,
        user: t.user,
        turnId: null,
        text: t.text,
        phase: t.phase,
        reason: t.reason,
        detail: t.detail,
        citations: t.citations === null ? null : [...t.citations],
        stopRequested: false,
      }),
    );
    added.push({
      id: c.id,
      seq: c.seq,
      turns,
      forgotten: turns.length,
      ...(c.mode === undefined ? {} : { mode: c.mode }),
    });
    nextSeq = Math.max(nextSeq, c.seq + 1);
  }
  // The shown chat stays the newest: it was opened after every saved one.
  d.state = {
    ...d.state,
    others: [...d.state.others, ...added],
    nextKey,
    seq: nextSeq,
    nextSeq: nextSeq + 1,
  };
  d.trim();
  if (unreadable > 0) {
    d.notice(
      "error",
      `${unreadable} saved chat${unreadable === 1 ? "" : "s"} could not be read and ` +
        `${unreadable === 1 ? "was" : "were"} left out.`,
    );
  }
}

/** A chat's title: its first message, on one line, cut at TITLE_MAX characters. */
export const TITLE_MAX = 48;

export function chatTitle(turns: readonly Turn[]): string {
  const first = turns[0];
  if (first === undefined) return "New chat";
  const line = first.user.replace(/\s+/g, " ").trim();
  return line.length > TITLE_MAX ? `${line.slice(0, TITLE_MAX - 1)}…` : line;
}

export interface ChatEntry {
  readonly id: string;
  readonly title: string;
  readonly shown: boolean;
  readonly running: boolean;
}

/** The sidebar's chats, newest first; the shown chat is first while it is empty. */
export function chatList(state: ChatState): ChatEntry[] {
  const shown: Chat = {
    id: state.sessionId,
    seq: state.turns.length === 0 ? Number.POSITIVE_INFINITY : state.seq,
    turns: state.turns,
    forgotten: state.forgotten,
  };
  return [shown, ...state.others]
    .sort((a, b) => b.seq - a.seq)
    .map((c) => ({
      id: c.id,
      title: chatTitle(c.turns),
      shown: c.id === state.sessionId,
      running: c.turns.some(isRunning),
    }));
}

/** Every chat of the tab (the shown one first). */
export function allChats(state: ChatState): Chat[] {
  return [
    {
      id: state.sessionId,
      seq: state.seq,
      turns: state.turns,
      forgotten: state.forgotten,
      mode: state.mode,
    },
    ...state.others,
  ];
}

function stop(d: Draft): void {
  const run = runningTurn(d.state);
  if (run === null || run.stopRequested) return;
  d.patchTurn(
    (t) => t.key === run.key,
    () => ({ stopRequested: true }),
  );
  // Without a turn id yet, the cancel goes out when the `turn` reply arrives (onTurn).
  if (run.turnId !== null) d.cancel(run.key, run.turnId);
}

function onMessage(d: Draft, msg: PageMessage): void {
  switch (msg.type) {
    case "turn":
    case "status_result":
    case "info_result":
    case "approve_result":
    case "ok":
    case "error":
      onReply(d, msg);
      return;
    case "approval": {
      // Only a running turn of the shown chat takes a card; anything else is stale.
      const card: ApprovalCard = {
        approvalId: msg.approval_id,
        nonce: msg.nonce,
        tool: msg.tool,
        tier: msg.tier,
        argsText: argsText(msg.args),
        state: "open",
        restOfTurn: false,
        expanded: false,
      };
      d.patchTurn(
        (t) =>
          t.turnId === msg.turn_id &&
          isRunning(t) &&
          !(t.approvals ?? []).some((c) => c.approvalId === msg.approval_id),
        (t) => ({ approvals: [...(t.approvals ?? []), card] }),
      );
      return;
    }
    case "delta":
      d.patchTurn(
        (t) => t.turnId === msg.turn_id && isRunning(t),
        (t) => ({ text: t.text + msg.text, phase: "streaming" }),
      );
      return;
    case "done":
      if (
        d.patchTurn(
          (t) => t.turnId === msg.turn_id && isRunning(t),
          (t) => ({
            ...closeCards(t),
            phase: "done",
            citations: [...msg.citations],
            stopRequested: false,
          }),
        )
      ) {
        d.status();
      }
      return;
    case "chat_error": {
      const cancelled = msg.reason === "cancelled";
      if (
        d.patchTurn(
          (t) => t.turnId === msg.turn_id && isRunning(t),
          (t) => ({
            ...closeCards(t),
            phase: cancelled ? "cancelled" : "error",
            reason: cancelled ? null : msg.reason,
            stopRequested: false,
          }),
        )
      ) {
        d.status();
      }
      return;
    }
    case "session_end":
      d.failAll("session_end");
      // ternd's sessions are gone: no chat's earlier turns are known to the assistant now.
      d.state = {
        ...d.state,
        forgotten: d.state.turns.length,
        others: d.state.others.map((c) => ({ ...c, forgotten: c.turns.length })),
      };
      d.notice("error", SESSION_END_NOTICE);
      return;
  }
}

type Reply = Extract<PageMessage, { req: number }>;

function onReply(d: Draft, msg: Reply): void {
  const w = d.settle(msg.req);
  if (w === undefined) return; // failed at session_end, or never ours: nothing waits for it
  if (msg.type === "error") {
    onError(d, w, msg.name, msg.message);
    return;
  }
  switch (w.kind) {
    case "send":
      if (msg.type !== "turn") break;
      onTurn(d, w.turnKey, msg.turn_id);
      return;
    case "status":
      if (msg.type !== "status_result") break;
      d.state = { ...d.state, status: msg.status, statusError: null };
      return;
    case "info":
      if (msg.type !== "info_result") break;
      d.state = {
        ...d.state,
        info: { kind: "known", remoteLlm: msg.info.remote_llm, chatModel: msg.info.chat_model },
      };
      return;
    case "approve": {
      if (msg.type !== "approve_result") break;
      const status = msg.status;
      // A card its turn already closed stays closed; otherwise it shows the daemon's answer.
      d.patchCard(w.approvalId, (c) => (c.state === "sending" ? { state: status } : {}));
      return;
    }
    case "cancel":
    case "openUrl":
    case "open":
      if (msg.type !== "ok") break;
      return; // a cancelled turn ends with chat_error reason=cancelled; an opened message shows in TB
  }
  // A reply of the wrong kind for its request (a background bug): fail that request visibly.
  onError(d, w, "internal", `unexpected ${msg.type} reply`);
}

function onTurn(d: Draft, turnKey: number, turnId: string): void {
  const turn = d.state.turns.find((t) => t.key === turnKey);
  if (turn === undefined || !isRunning(turn)) {
    d.cancel(null, turnId); // its chat was cleared ("New chat") before ternd answered
    return;
  }
  d.patchTurn(
    (t) => t.key === turnKey,
    () => ({ turnId }),
  );
  if (turn.stopRequested) d.cancel(turnKey, turnId);
}

function onError(d: Draft, w: Waiting, name: ErrorName | "internal", message: string): void {
  switch (w.kind) {
    case "send":
      d.patchTurn(
        (t) => t.key === w.turnKey && isRunning(t),
        () => ({ phase: "error", reason: name, detail: message, stopRequested: false }),
      );
      // A manual send the router refused because ternd cannot ask (bridge below 1.2).
      if (name === "method_not_available") d.noticeOnce("error", MANUAL_UNAVAILABLE_NOTICE);
      return;
    case "approve":
      // ternd refused it (e.g. the call already ended) or never got it: the card shows that it
      // failed; the call still waits for a decision until it expires or its turn ends.
      d.patchCard(w.approvalId, (c) => (c.state === "sending" ? { state: "failed" } : {}));
      d.notice("error", `The decision on a tool call could not be sent (${name}).`);
      return;
    case "status":
      d.state = { ...d.state, statusError: name };
      return;
    case "cancel":
      if (w.turnKey !== null) {
        d.patchTurn(
          (t) => t.key === w.turnKey,
          () => ({ stopRequested: false }),
        );
      }
      d.notice("error", `Stop failed (${name}).`);
      return;
    case "openUrl":
      d.notice("error", `Could not open the link (${name}).`);
      return;
    case "open":
      if (name === "method_not_available") {
        d.notice("info", `${CHIP_NOTICE} (message ${shortMid(w.mid)}).`);
      } else {
        d.notice("error", `Could not open message ${shortMid(w.mid)} (${name}).`);
      }
      return;
    case "info":
      // A session at minor 0 has no daemon.info; any other failure leaves it unknown (asked again
      // with the next status request or send).
      if (name === "method_not_available") {
        d.state = { ...d.state, info: { kind: "legacy" } };
      } else if (w.forSend) {
        d.notice("error", `Could not check ternd's model setting (${name}); nothing was sent.`);
      }
      return;
  }
}

// --- incoming messages ----------------------------------------------------------------------

// From protocol.ts's complete record, so a name added there is never dropped here.
const ERROR_NAMES: ReadonlySet<string> = new Set(Object.keys(ERROR_CODES));

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

const isStr = (x: unknown): x is string => typeof x === "string";
const isNum = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x);
const isReq = (x: unknown): x is number => typeof x === "number" && Number.isInteger(x);
const isNullableStr = (x: unknown): boolean => x === null || isStr(x);

function isIndexStatus(x: unknown): x is IndexStatus {
  if (!isObject(x)) return false;
  const nums = ["messages", "locations", "conflicted", "parse_failed", "headers_only"] as const;
  if (!nums.every((k) => isNum(x[k]))) return false;
  if (!isNum(x.embed_pending) || !isNum(x.lag_s) || !isNullableStr(x.last_reconcile)) return false;
  const m = x.models;
  if (!isObject(m) || !isStr(m.chat) || !isStr(m.embed) || !isStr(m.runtime_version)) return false;
  return (
    Array.isArray(x.accounts) &&
    x.accounts.every(
      (a) => isObject(a) && isStr(a.account_id) && isStr(a.name) && isNullableStr(a.last_sync),
    )
  );
}

/** One message from the background, validated; null when it is not a PageMessage. */
export function parsePageMessage(x: unknown): PageMessage | null {
  if (!isObject(x)) return null;
  switch (x.type) {
    case "turn":
      return isReq(x.req) && isStr(x.turn_id) && x.turn_id !== ""
        ? { type: "turn", req: x.req, turn_id: x.turn_id }
        : null;
    case "status_result":
      return isReq(x.req) && isIndexStatus(x.status)
        ? { type: "status_result", req: x.req, status: x.status }
        : null;
    case "info_result":
      return isReq(x.req) &&
        isObject(x.info) &&
        typeof x.info.remote_llm === "boolean" &&
        isStr(x.info.chat_model)
        ? {
            type: "info_result",
            req: x.req,
            info: { remote_llm: x.info.remote_llm, chat_model: x.info.chat_model },
          }
        : null;
    case "ok":
      return isReq(x.req) ? { type: "ok", req: x.req } : null;
    case "error":
      return isReq(x.req) && isStr(x.name) && ERROR_NAMES.has(x.name) && isStr(x.message)
        ? { type: "error", req: x.req, name: x.name as ErrorName, message: x.message }
        : null;
    case "delta":
      return isStr(x.turn_id) && isStr(x.text)
        ? { type: "delta", turn_id: x.turn_id, text: x.text }
        : null;
    case "done":
      return isStr(x.turn_id) && Array.isArray(x.citations) && x.citations.every(isStr)
        ? { type: "done", turn_id: x.turn_id, citations: [...x.citations] }
        : null;
    case "chat_error":
      return isStr(x.turn_id) && isStr(x.reason)
        ? { type: "chat_error", turn_id: x.turn_id, reason: x.reason }
        : null;
    case "session_end":
      return { type: "session_end" };
    case "approve_result":
      return isReq(x.req) && isStr(x.status) && STATUSES.has(x.status)
        ? { type: "approve_result", req: x.req, status: x.status as ApprovalStatus }
        : null;
    case "approval":
      return isStr(x.turn_id) &&
        isStr(x.approval_id) &&
        x.approval_id !== "" &&
        isStr(x.nonce) &&
        x.nonce !== "" &&
        isStr(x.tool) &&
        x.tool !== "" &&
        isStr(x.tier) &&
        TIERS.has(x.tier) &&
        isObject(x.args) &&
        isStr(x.expires_at)
        ? {
            type: "approval",
            turn_id: x.turn_id,
            approval_id: x.approval_id,
            nonce: x.nonce,
            tool: x.tool,
            tier: x.tier as Tier,
            args: x.args,
            expires_at: x.expires_at,
          }
        : null;
    default:
      return null;
  }
}

const STATUSES: ReadonlySet<string> = new Set<ApprovalStatus>([
  "approved",
  "denied",
  "expired",
  "targets_changed",
]);
const TIERS: ReadonlySet<string> = new Set<Tier>(["R0", "R1", "W1", "W2", "W3"]);

/**
 * The `req` of a message that failed parsePageMessage, when it names one (an integer): only
 * replies carry a `req`, so the request it answers can be failed instead of waiting until a reset.
 */
export function replyReq(x: unknown): number | null {
  return isObject(x) && isReq(x.req) ? x.req : null;
}

// --- what a turn shows ----------------------------------------------------------------------

/**
 * The answer of `turn` as the sanitizer's tree, with chips the daemon did not confirm demoted to
 * their raw text: a `done` turn keeps chips whose mid is in its citations; a running turn keeps
 * all (shown as pending by the view); a failed or cancelled turn keeps none.
 */
export function displayBlocks(turn: Turn): Block[] {
  const keep = (mid: string): boolean =>
    turn.phase === "done" ? (turn.citations?.includes(mid) ?? false) : RUNNING.has(turn.phase);
  const inl = (xs: readonly Inline[]): Inline[] =>
    xs.map((n): Inline => {
      if (n.t === "cite") return keep(n.mid) ? n : { t: "text", text: n.raw };
      if (n.t === "strong" || n.t === "em") return { t: n.t, children: inl(n.children) };
      return n;
    });
  const items = (xs: readonly ListItem[]): ListItem[] =>
    xs.map((li) => ({ t: "li", children: inl(li.children) }));
  const cells = (xs: readonly TableCell[]): TableCell[] =>
    xs.map((c) => ({ t: "td", children: inl(c.children) }));
  return sanitize(turn.text).map((b): Block => {
    if (b.t === "p" || b.t === "h") return { ...b, children: inl(b.children) };
    if (b.t === "ul" || b.t === "ol") return { ...b, items: items(b.items) };
    if (b.t === "table") return { ...b, head: cells(b.head), rows: b.rows.map(cells) };
    return b;
  });
}

/** A short text for how a turn ended (or why it failed); null while it runs or after `done`. */
export function outcomeText(turn: Turn): string | null {
  switch (turn.phase) {
    case "pending":
    case "streaming":
    case "done":
      return null;
    case "cancelled":
      return "stopped";
    case "error":
      return errorText(turn.reason ?? "internal", turn.detail);
  }
}

const OUTCOMES: Readonly<Record<string, string>> = {
  max_tool_calls: "stopped: too many tool calls in one answer",
  timeout: "stopped: the answer took too long",
  llm_unavailable: "the local model is not reachable (is LM Studio running with the model loaded?)",
  llm_error: "the local model returned an error",
  malformed_think: "the model's output was malformed; the text above may be incomplete",
  truncated: "the answer was cut off at the length limit",
  internal: "tern hit an internal error (see ternd.log)",
  session_end: "ternd connection reset",
  disconnected: "lost the connection to the background page",
  interrupted: "the tab closed before the answer ended",
};

/** The text for a failure code: a known outcome, else the code itself, plus the detail if any. */
export function errorText(code: string, detail: string | null): string {
  const base = OUTCOMES[code] ?? `failed (${code})`;
  return detail !== null && detail !== "" ? `${base}: ${detail}` : base;
}
