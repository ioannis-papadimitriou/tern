// Decisions #39: per-chat permission modes in the chat tab. The mode switch and its saving
// (state.ts, history.ts), approval cards (state.ts reducer, view.ts approvalCardView) and the
// router's chat.start / chat.approval / chat.approve paths (background/chat.ts). No snapshots;
// synthetic ids and text only.

import { describe, expect, it } from "vitest";
import type { NotificationHandler } from "../src/background/bridge";
import {
  CHAT_SPACE,
  type ChatBridge,
  ChatRouter,
  MANUAL_NEEDS_MINOR,
  parsePageRequest,
} from "../src/background/chat";
import type { RuntimePort } from "../src/background/platform";
import type {
  ApprovalDecided,
  ChatApproval,
  ChatApproveParams,
  ChatStartParams,
  PageMessage,
  PageRequest,
} from "../src/protocol";
import { CHAT_PORT_NAME } from "../src/tb/api";
import { entryKey, entryOf, parseEntry, readEntries } from "../src/ui/chat/history";
import {
  type Action,
  allChats,
  argsText,
  type ChatState,
  DEFAULT_MODE,
  initialState,
  MANUAL_UNAVAILABLE_NOTICE,
  parsePageMessage,
  update,
} from "../src/ui/chat/state";
import { type Child, childrenOf, type VNode } from "../src/ui/chat/vdom";
import { ARGS_FOLD_LINES, approvalCardView, headerView, logView } from "../src/ui/chat/view";
import { paramsErrors, resultErrors } from "./contract";

const SID = "session-1";

// --- reducer helpers --------------------------------------------------------------------------

interface Run {
  state: ChatState;
  out: PageRequest[];
}

function run(state: ChatState, ...actions: Action[]): Run {
  let s = state;
  const out: PageRequest[] = [];
  for (const a of actions) {
    const r = update(s, a);
    s = r.state;
    out.push(...r.out);
  }
  return { state: s, out };
}

const msg = (m: PageMessage): Action => ({ type: "message", msg: m });

/** A tab whose model setting is known (sends go out), plus `actions`. */
function ready(...actions: Action[]): Run {
  // refresh asks status (req 1) and info (req 2); the info answer makes the setting known.
  const base = run(
    initialState(SID),
    { type: "refresh" },
    msg({ type: "info_result", req: 2, info: { remote_llm: false, chat_model: "m" } }),
  );
  return run(base.state, ...actions);
}

/** A running turn `t-1` (sent, answered) in the given mode. */
function runningTurn(mode: "manual" | "bypass" = "manual"): Run {
  const r = ready({ type: "set_mode", mode }, { type: "submit", text: "brief me" });
  const send = r.out.find((o) => o.type === "send");
  if (send === undefined) throw new Error("no send");
  return run(r.state, msg({ type: "turn", req: send.req, turn_id: "t-1" }));
}

const APPROVAL = {
  type: "approval" as const,
  turn_id: "t-1",
  approval_id: "ap-1",
  nonce: "nonce-1",
  tool: "tb_search",
  tier: "R0" as const,
  args: { query: "synthetic invoices", limit: 5 },
  expires_at: "2026-11-16T10:10:00Z",
};

function withCard(args: Record<string, unknown> = APPROVAL.args): Run {
  const r = runningTurn();
  return run(r.state, msg({ ...APPROVAL, args }));
}

const cardOf = (s: ChatState) => {
  const c = s.turns.at(-1)?.approvals?.[0];
  if (c === undefined) throw new Error("no card");
  return c;
};

// --- vnode helpers ----------------------------------------------------------------------------

function all(c: Child): VNode[] {
  const out: VNode[] = [];
  const walk = (x: Child): void => {
    for (const n of childrenOf([x])) {
      if (typeof n === "string") continue;
      out.push(n);
      walk(n.props.children as Child[]);
    }
  };
  walk(c);
  return out;
}

function text(c: Child): string {
  return childrenOf([c])
    .map((n) => (typeof n === "string" ? n : text(n.props.children as Child[])))
    .join("");
}

function buttons(v: VNode): Map<string, VNode> {
  return new Map(
    all(v)
      .filter((n) => n.type === "button")
      .map((b) => [text(b), b]),
  );
}

function click(v: VNode | undefined): void {
  const h = v?.props.onClick;
  if (typeof h !== "function") throw new Error("not clickable");
  (h as (e: Event) => void)({} as Event);
}

function recorder(): { actions: Action[]; dispatch: (a: Action) => void } {
  const actions: Action[] = [];
  return { actions, dispatch: (a) => actions.push(a) };
}

// --- the mode switch --------------------------------------------------------------------------

describe("mode per chat", () => {
  it("a new chat is manual and every send carries the chat's mode", () => {
    expect(DEFAULT_MODE).toBe("manual");
    const r = ready({ type: "submit", text: "q" });
    expect(r.out).toContainEqual({
      type: "send",
      req: 3,
      session_id: SID,
      text: "q",
      mode: "manual",
    });
    const b = ready({ type: "set_mode", mode: "bypass" }, { type: "submit", text: "q" });
    expect(b.out.find((o) => o.type === "send")).toMatchObject({ mode: "bypass" });
  });

  it("the header switch shows the chosen mode and dispatches set_mode", () => {
    const { actions, dispatch } = recorder();
    const r = ready({ type: "set_mode", mode: "bypass" });
    const tree = headerView(r.state, dispatch, 0, () => "s-new");
    const opts = all(tree).filter((n) => String(n.props.class).startsWith("mode-option"));
    expect(opts.map((o) => [text(o), o.props["aria-pressed"]])).toEqual([
      ["Manual", false],
      ["Bypass", true],
    ]);
    for (const o of opts) click(o);
    expect(actions).toEqual([
      { type: "set_mode", mode: "manual" },
      { type: "set_mode", mode: "bypass" },
    ]);
  });

  it("each chat keeps its own mode across switching and a new chat", () => {
    let r = ready({ type: "set_mode", mode: "bypass" }, { type: "submit", text: "first" });
    r = run(r.state, { type: "new_chat", sessionId: "session-2" });
    expect(r.state.mode).toBe("manual");
    r = run(r.state, { type: "submit", text: "second" });
    expect(r.out.find((o) => o.type === "send")).toMatchObject({
      session_id: "session-2",
      mode: "manual",
    });
    r = run(r.state, { type: "switch_chat", id: SID });
    expect(r.state.mode).toBe("bypass");
    expect(allChats(r.state).map((c) => [c.id, c.mode ?? DEFAULT_MODE])).toEqual([
      [SID, "bypass"],
      ["session-2", "manual"],
    ]);
  });

  it("the mode is saved with the chat (bypass only; absent = manual) and restored", () => {
    const r = ready(
      { type: "set_mode", mode: "bypass" },
      { type: "submit", text: "q" },
      msg({ type: "turn", req: 3, turn_id: "t-1" }),
      msg({ type: "done", turn_id: "t-1", citations: [] }),
    );
    const [shown] = allChats(r.state);
    const entry = entryOf(shown as NonNullable<typeof shown>);
    expect(entry).toMatchObject({ v: 1, id: SID, mode: "bypass" });
    const back = parseEntry(entryKey(SID), JSON.parse(JSON.stringify(entry)));
    expect(back?.mode).toBe("bypass");
    // A manual chat writes no mode, exactly like an entry saved before modes existed.
    const manual = entryOf({ ...(shown as NonNullable<typeof shown>), mode: "manual" });
    expect(manual !== null && "mode" in manual).toBe(false);
    // Anything else is unreadable, never guessed.
    const bad = { ...JSON.parse(JSON.stringify(entry)), mode: "auto" };
    expect(readEntries({ [entryKey(SID)]: bad }).unreadable).toBe(1);
    // Restored chats keep it; switching to one shows it.
    const restored = run(
      initialState("session-9"),
      { type: "restore", chats: back === null ? [] : [back], unreadable: 0 },
      { type: "switch_chat", id: SID },
    );
    expect(restored.state.mode).toBe("bypass");
  });

  it("a manual send refused for an old ternd says how to go on", () => {
    const r = ready({ type: "submit", text: "q" });
    const next = run(
      r.state,
      msg({ type: "error", req: 3, name: "method_not_available", message: MANUAL_NEEDS_MINOR }),
    );
    expect(next.state.turns.at(-1)?.phase).toBe("error");
    expect(next.state.notices.map((n) => n.text)).toContain(MANUAL_UNAVAILABLE_NOTICE);
  });
});

// --- approval cards ---------------------------------------------------------------------------

describe("approval cards", () => {
  it("a chat.approval adds a card to its running turn; others are ignored", () => {
    const r = withCard();
    expect(cardOf(r.state)).toMatchObject({
      approvalId: "ap-1",
      nonce: "nonce-1",
      tool: "tb_search",
      tier: "R0",
      state: "open",
    });
    const stale = run(r.state, msg({ ...APPROVAL, turn_id: "t-other", approval_id: "ap-x" }));
    expect(stale.state.turns.at(-1)?.approvals).toHaveLength(1);
    const dup = run(r.state, msg(APPROVAL));
    expect(dup.state.turns.at(-1)?.approvals).toHaveLength(1);
  });

  it("the card shows the tool and the arguments as escaped text, never markup", () => {
    const hostile = {
      query: '<img src=x onerror="fetch(1)"><a href="https://evil.invalid">x</a>',
      // A right-to-left override and a zero-width space would hide or reorder what is shown.
      name: "invoice‮gpj.exe​",
    };
    const r = withCard(hostile);
    const { dispatch } = recorder();
    const v = approvalCardView(cardOf(r.state), dispatch);
    const nodes = all(v);
    expect(nodes.some((n) => n.type === "img" || n.type === "a")).toBe(false);
    expect(nodes.some((n) => "href" in n.props || "src" in n.props)).toBe(false);
    const shown = text(v);
    expect(shown).toContain("tb_search");
    expect(shown).toContain('"query": "<img src=x onerror=\\"fetch(1)\\">');
    expect(shown).toContain("invoice\\u202egpj.exe\\u200b");
    expect(shown).not.toMatch(/[‮​]/);
    expect(argsText({ a: "⁦x⁩" })).toBe('{\n  "a": "\\u2066x\\u2069"\n}');
    // The args are one text node inside pre > code.
    const pre = nodes.find((n) => n.type === "pre");
    const code = pre === undefined ? [] : childrenOf(pre.props.children as Child[]);
    expect(code).toHaveLength(1);
    const codeKids = childrenOf((code[0] as VNode).props.children as Child[]);
    expect(codeKids.every((k) => typeof k === "string")).toBe(true);
  });

  it("Allow sends one approve with the card's nonce; the card is disabled from then on", () => {
    const r = withCard();
    const { actions, dispatch } = recorder();
    click(buttons(approvalCardView(cardOf(r.state), dispatch)).get("Allow"));
    expect(actions).toEqual([
      { type: "decide", approvalId: "ap-1", decision: "approve", restOfTurn: false },
    ]);
    const sent = run(r.state, ...actions);
    expect(sent.out).toEqual([
      {
        type: "approve",
        req: 4,
        turn_id: "t-1",
        approval_id: "ap-1",
        nonce: "nonce-1",
        decision: "approve",
        rest_of_turn: false,
      },
    ]);
    expect(cardOf(sent.state).state).toBe("sending");
    const disabled = (s: ChatState) =>
      [...buttons(approvalCardView(cardOf(s), dispatch)).values()].map((b) => b.props.disabled);
    expect(disabled(sent.state)).toEqual([true, true, true]);
    // A second click while it waits sends nothing.
    expect(run(sent.state, ...actions).out).toEqual([]);
    const answered = run(sent.state, msg({ type: "approve_result", req: 4, status: "approved" }));
    expect(cardOf(answered.state).state).toBe("approved");
    expect(disabled(answered.state)).toEqual([true, true, true]);
    expect(text(approvalCardView(cardOf(answered.state), dispatch))).toContain("Allowed");
  });

  it("Deny and Allow all for this turn send their decisions", () => {
    const r = withCard();
    const { actions, dispatch } = recorder();
    const btns = buttons(approvalCardView(cardOf(r.state), dispatch));
    click(btns.get("Deny"));
    click(btns.get("Allow all for this turn"));
    const denied = run(r.state, actions[0] as Action);
    expect(denied.out[0]).toMatchObject({ type: "approve", decision: "deny", rest_of_turn: false });
    const all = run(r.state, actions[1] as Action);
    expect(all.out[0]).toMatchObject({ type: "approve", decision: "approve", rest_of_turn: true });
    const answered = run(all.state, msg({ type: "approve_result", req: 4, status: "approved" }));
    expect(text(approvalCardView(cardOf(answered.state), dispatch))).toContain("rest of this turn");
  });

  it("an open card is closed (disabled) when its turn ends", () => {
    const r = withCard();
    for (const end of [
      msg({ type: "done", turn_id: "t-1", citations: [] }),
      msg({ type: "chat_error", turn_id: "t-1", reason: "cancelled" }),
      msg({ type: "session_end" }),
    ]) {
      const ended = run(r.state, end);
      expect(cardOf(ended.state).state).toBe("closed");
      expect(
        run(ended.state, {
          type: "decide",
          approvalId: "ap-1",
          decision: "approve",
          restOfTurn: false,
        }).out,
      ).toEqual([]);
    }
  });

  it("a decision that fails can be tried again", () => {
    const r = withCard();
    const sent = run(r.state, {
      type: "decide",
      approvalId: "ap-1",
      decision: "approve",
      restOfTurn: false,
    });
    const failed = run(
      sent.state,
      msg({ type: "error", req: 4, name: "unavailable", message: "x" }),
    );
    expect(cardOf(failed.state).state).toBe("failed");
    expect(failed.state.notices).toHaveLength(1);
    const again = run(failed.state, {
      type: "decide",
      approvalId: "ap-1",
      decision: "deny",
      restOfTurn: false,
    });
    expect(again.out[0]).toMatchObject({ type: "approve", decision: "deny" });
  });

  it("long arguments are folded until Show all", () => {
    const long: Record<string, unknown> = {};
    for (let i = 0; i < ARGS_FOLD_LINES * 2; i++) long[`k${i}`] = i;
    const r = withCard(long);
    const { actions, dispatch } = recorder();
    const folded = approvalCardView(cardOf(r.state), dispatch);
    expect(text(folded)).not.toContain(`"k${ARGS_FOLD_LINES * 2 - 1}"`);
    click(buttons(folded).get(`Show all (${ARGS_FOLD_LINES * 2 + 2} lines)`));
    const open = run(r.state, ...actions);
    expect(text(approvalCardView(cardOf(open.state), dispatch))).toContain(
      `"k${ARGS_FOLD_LINES * 2 - 1}"`,
    );
  });

  it("the log shows the cards of a turn above its answer", () => {
    const r = withCard();
    const { dispatch } = recorder();
    const tree = logView(r.state, dispatch);
    expect(all(tree).filter((n) => String(n.props.class).startsWith("approval "))).toHaveLength(1);
  });

  it("malformed approval messages are refused", () => {
    expect(parsePageMessage(APPROVAL)).toEqual(APPROVAL);
    expect(parsePageMessage({ ...APPROVAL, tier: "W9" })).toBeNull();
    expect(parsePageMessage({ ...APPROVAL, args: [] })).toBeNull();
    expect(parsePageMessage({ ...APPROVAL, nonce: "" })).toBeNull();
    expect(parsePageMessage({ type: "approve_result", req: 1, status: "maybe" })).toBeNull();
  });
});

// --- the router -------------------------------------------------------------------------------

const PAGE_URL = `moz-extension://0d9c7a52-synthetic/${CHAT_SPACE.url}`;
const tick = (): Promise<void> => new Promise((r) => setTimeout(r, 0));

class TabPort implements RuntimePort {
  readonly name = CHAT_PORT_NAME;
  readonly sender = { url: PAGE_URL };
  posted: PageMessage[] = [];
  private cbs: ((m: unknown) => void)[] = [];
  onMessage = { addListener: (cb: (m: unknown) => void) => void this.cbs.push(cb) };
  onDisconnect = { addListener: (_cb: (p: RuntimePort) => void) => {} };
  postMessage(m: unknown): void {
    this.posted.push(m as PageMessage);
  }
  disconnect(): void {}
  send(m: unknown): void {
    for (const cb of this.cbs) cb(m);
  }
}

interface Call {
  method: string;
  params: Record<string, unknown>;
  resolve(r: unknown): void;
}

class Bridge implements ChatBridge {
  state = "ready" as const;
  readonly notifications = new Map<string, NotificationHandler>();
  calls: Call[] = [];
  constructor(public minor: number) {}
  request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    return new Promise((resolve) => this.calls.push({ method, params, resolve }));
  }
  emit(method: string, params: Record<string, unknown>): void {
    this.notifications.get(method)?.(params);
  }
}

async function routed(minor: number): Promise<{ bridge: Bridge; tab: () => Promise<TabPort> }> {
  const bridge = new Bridge(minor);
  const router = new ChatRouter(
    bridge,
    async () => {},
    PAGE_URL,
    () => {},
  );
  const tab = async (): Promise<TabPort> => {
    const p = new TabPort();
    router.attach(p);
    p.send({ type: "info", req: 0 });
    await tick();
    bridge.calls
      .find((c) => c.method === "daemon.info")
      ?.resolve({ remote_llm: false, chat_model: "m" });
    await tick();
    bridge.calls = [];
    p.posted = [];
    return p;
  };
  return { bridge, tab };
}

async function turnOn(bridge: Bridge, port: TabPort, mode?: "manual" | "bypass"): Promise<void> {
  port.send({ type: "send", req: 1, session_id: "s-1", text: "q", ...(mode ? { mode } : {}) });
  await tick();
  bridge.calls.at(-1)?.resolve({ turn_id: "t-1" });
  await tick();
}

describe("router: modes over the bridge", () => {
  it("parses send with or without a valid mode, and approve", () => {
    expect(
      parsePageRequest({ type: "send", req: 1, session_id: "s", text: "t", mode: "bypass" }),
    ).toMatchObject({ ok: true });
    expect(
      parsePageRequest({ type: "send", req: 1, session_id: "s", text: "t", mode: "auto" }),
    ).toEqual({ ok: false, req: 1 });
    const ok = {
      type: "approve",
      req: 2,
      turn_id: "t",
      approval_id: "a",
      nonce: "n",
      decision: "deny",
      rest_of_turn: false,
    };
    expect(parsePageRequest(ok)).toMatchObject({ ok: true });
    expect(parsePageRequest({ ...ok, rest_of_turn: true })).toEqual({ ok: false, req: 2 });
  });

  it("on a 1.2 session a send with a mode is chat.start; without one it is chat.send", async () => {
    const { bridge, tab } = await routed(2);
    const p = await tab();
    p.send({ type: "send", req: 1, session_id: "s-1", text: "q", mode: "manual" });
    p.send({ type: "send", req: 2, session_id: "s-2", text: "q" });
    await tick();
    expect(bridge.calls.map((c) => [c.method, c.params])).toEqual([
      ["chat.start", { session_id: "s-1", text: "q", mode: "manual" }],
      ["chat.send", { session_id: "s-2", text: "q" }],
    ]);
  });

  it("below 1.2 a manual send is refused unsent and a bypass send is chat.send", async () => {
    const { bridge, tab } = await routed(1);
    const p = await tab();
    p.send({ type: "send", req: 1, session_id: "s-1", text: "q", mode: "manual" });
    await tick();
    expect(bridge.calls).toEqual([]);
    expect(p.posted).toEqual([
      { type: "error", req: 1, name: "method_not_available", message: MANUAL_NEEDS_MINOR },
    ]);
    p.send({ type: "send", req: 2, session_id: "s-1", text: "q", mode: "bypass" });
    await tick();
    expect(bridge.calls.map((c) => [c.method, c.params])).toEqual([
      ["chat.send", { session_id: "s-1", text: "q" }],
    ]);
  });

  it("chat.approval reaches the owning tab only; a malformed one is dropped", async () => {
    const { bridge, tab } = await routed(2);
    const owner = await tab();
    const other = await tab();
    await turnOn(bridge, owner, "manual");
    owner.posted = [];
    const { type: _t, ...note } = APPROVAL;
    bridge.emit("chat.approval", note);
    bridge.emit("chat.approval", { ...note, approval_id: "ap-2", tier: "X" });
    expect(owner.posted).toEqual([APPROVAL]);
    expect(other.posted).toEqual([]);
  });

  it("approve goes out as chat.approve for the owner's turn and answers approve_result", async () => {
    const { bridge, tab } = await routed(2);
    const owner = await tab();
    const other = await tab();
    await turnOn(bridge, owner, "manual");
    bridge.calls = [];
    const req = {
      type: "approve",
      req: 5,
      turn_id: "t-1",
      approval_id: "ap-1",
      nonce: "nonce-1",
      decision: "approve",
      rest_of_turn: true,
    };
    other.send(req);
    await tick();
    expect(bridge.calls).toEqual([]);
    expect(other.posted.at(-1)).toMatchObject({ type: "error", req: 5, name: "invalid_params" });
    owner.send(req);
    await tick();
    const call = bridge.calls.at(-1);
    expect([call?.method, call?.params]).toEqual([
      "chat.approve",
      {
        turn_id: "t-1",
        approval_id: "ap-1",
        nonce: "nonce-1",
        decision: "approve",
        rest_of_turn: true,
      },
    ]);
    call?.resolve({ status: "approved" });
    await tick();
    expect(owner.posted.at(-1)).toEqual({ type: "approve_result", req: 5, status: "approved" });
  });

  it("below 1.2 approve is method_not_available and nothing is sent", async () => {
    const { bridge, tab } = await routed(1);
    const p = await tab();
    await turnOn(bridge, p, "bypass");
    bridge.calls = [];
    p.send({
      type: "approve",
      req: 7,
      turn_id: "t-1",
      approval_id: "a",
      nonce: "n",
      decision: "deny",
      rest_of_turn: false,
    });
    await tick();
    expect(bridge.calls).toEqual([]);
    expect(p.posted.at(-1)).toMatchObject({ type: "error", req: 7, name: "method_not_available" });
  });
});

describe("protocol.ts 1.2 mirrors vs the frozen tern-bridge-1.2 contract", () => {
  it("samples typed with the TS mirrors validate; malformed ones do not", () => {
    const start: ChatStartParams = { session_id: "s-1", text: "q", mode: "manual" };
    const approve: ChatApproveParams = {
      turn_id: "t-1",
      approval_id: "ap-1",
      nonce: "n",
      decision: "approve",
      rest_of_turn: true,
    };
    const decided: ApprovalDecided = { status: "approved" };
    const { type: _t, ...note } = APPROVAL;
    const approval: ChatApproval = note;
    expect(paramsErrors("chat.start", start, "1.2")).toEqual([]);
    expect(resultErrors("chat.start", { turn_id: "t-1" }, "1.2")).toEqual([]);
    expect(paramsErrors("chat.approve", approve, "1.2")).toEqual([]);
    expect(resultErrors("chat.approve", decided, "1.2")).toEqual([]);
    expect(paramsErrors("chat.approval", approval, "1.2")).toEqual([]);
    expect(paramsErrors("chat.start", { ...start, mode: "auto" }, "1.2")).not.toEqual([]);
    expect(paramsErrors("chat.send", { ...start }, "1.2")).not.toEqual([]); // no mode on chat.send
  });
});
