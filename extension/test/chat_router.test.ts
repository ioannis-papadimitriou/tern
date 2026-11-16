// Plan 08b step 3: the chat router between chat tab ports and the bridge, the chat platform
// (spaces, onConnect, openDefaultBrowser), chatPagePort() and the main.ts wiring. Synthetic ids
// and text only; SENTINEL- strings must never reach a log line.

import { afterEach, describe, expect, it, vi } from "vitest";
import { Bridge, type BridgeState, type NotificationHandler } from "../src/background/bridge";
import {
  CHAT_SPACE,
  type ChatBridge,
  ChatRouter,
  isOpenableUrl,
  parsePageRequest,
  schemeForLog,
  startChat,
} from "../src/background/chat";
import {
  type ChatPlatform,
  chatPlatformFrom,
  type MessengerRuntime,
  type NativePort,
  type Platform,
  platformFrom,
  type RuntimePort,
} from "../src/background/platform";
import {
  BridgeError,
  type ChatDelta,
  type ChatDone,
  type ChatErrorNotif,
  type ChatSendParams,
  type DaemonInfo,
  type IndexStatus,
  type OpenMidParams,
  type OpenMidResult,
  type PageMessage,
  type TurnId,
} from "../src/protocol";
import { CHAT_PORT_NAME, chatPagePort } from "../src/tb/api";
import { paramsErrors, resultErrors } from "./contract";

const MID = "a".repeat(64);
const MID_X = `x-${"b".repeat(64)}`;
const SECRET = "SENTINEL-chat-text-5b1e";
/** runtime.getURL("") of the fake extension, and the chat page's URL under it. */
const EXT_BASE = "moz-extension://0d9c7a52-synthetic/";
const PAGE_URL = `${EXT_BASE}${CHAT_SPACE.url}`;

const tick = (): Promise<void> => new Promise((r) => setTimeout(r, 0));

class FakeTabPort implements RuntimePort {
  posted: PageMessage[] = [];
  error: { message: string } | null = null;
  failPost = false;
  private msgCbs: ((m: unknown) => void)[] = [];
  private discCbs: ((p: RuntimePort) => void)[] = [];
  onMessage = { addListener: (cb: (m: unknown) => void) => void this.msgCbs.push(cb) };
  onDisconnect = { addListener: (cb: (p: RuntimePort) => void) => void this.discCbs.push(cb) };
  readonly sender?: { url?: string };
  constructor(
    readonly name: string = CHAT_PORT_NAME,
    senderUrl: string | null = PAGE_URL,
  ) {
    if (senderUrl !== null) this.sender = { url: senderUrl };
  }
  postMessage(m: unknown): void {
    if (this.failPost) throw new Error("Attempt to postMessage on disconnected port");
    this.posted.push(m as PageMessage);
  }
  disconnect(): void {}
  send(m: unknown): void {
    for (const cb of this.msgCbs) cb(m);
  }
  close(): void {
    for (const cb of this.discCbs) cb(this);
  }
  of<T extends PageMessage["type"]>(type: T): Extract<PageMessage, { type: T }>[] {
    return this.posted.filter((m) => m.type === type) as Extract<PageMessage, { type: T }>[];
  }
}

interface Call {
  method: string;
  params: Record<string, unknown>;
  resolve(result: unknown): void;
  reject(err: unknown): void;
}

class FakeBridge implements ChatBridge {
  state: BridgeState = "ready";
  minor = 1;
  readonly notifications = new Map<string, NotificationHandler>();
  calls: Call[] = [];
  request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    return new Promise((resolve, reject) => this.calls.push({ method, params, resolve, reject }));
  }
  emit(method: string, params: Record<string, unknown>): void {
    const h = this.notifications.get(method);
    if (h === undefined) throw new Error(`${method} not handled`);
    h(params);
  }
  call(method: string): Call {
    const c = [...this.calls].reverse().find((x) => x.method === method);
    if (c === undefined) throw new Error(`no ${method} request`);
    return c;
  }
  methods(): string[] {
    return this.calls.map((c) => c.method);
  }
}

function setup(): {
  bridge: FakeBridge;
  router: ChatRouter;
  logs: string[];
  opened: string[];
  tab: (opts?: { checked?: boolean }) => Promise<FakeTabPort>;
} {
  const bridge = new FakeBridge();
  const logs: string[] = [];
  const opened: string[] = [];
  const router = new ChatRouter(
    bridge,
    async (url) => {
      opened.push(url);
    },
    PAGE_URL,
    (m) => logs.push(m),
  );
  /**
   * Attach a chat tab. Unless `checked` is false, the tab first learns the model setting (info →
   * daemon.info → info_result, as the real tab does before any send; the #9 send gate), then the
   * exchange is cleared from the records so tests see only their own traffic.
   */
  const tab = async (opts: { checked?: boolean } = {}): Promise<FakeTabPort> => {
    const p = new FakeTabPort();
    router.attach(p);
    if (opts.checked !== false) {
      p.send({ type: "info", req: 0 });
      const info = bridge.calls.find((c) => c.method === "daemon.info");
      info?.resolve({ remote_llm: false, chat_model: "synthetic-chat" });
      await tick();
      bridge.calls = bridge.calls.filter((c) => c.method !== "daemon.info");
      p.posted.length = 0;
    }
    return p;
  };
  return { bridge, router, logs, opened, tab };
}

/** Send from `port`, answer chat.send with `turnId`, and return it once the tab has it. */
async function startTurn(
  bridge: FakeBridge,
  port: FakeTabPort,
  turnId: string,
  req = 1,
): Promise<void> {
  port.send({ type: "send", req, session_id: "s-1", text: "what did the synthetic team say?" });
  await tick();
  bridge.call("chat.send").resolve({ turn_id: turnId } satisfies TurnId);
  await tick();
  expect(port.of("turn")).toContainEqual({ type: "turn", req, turn_id: turnId });
}

const sampleStatus: IndexStatus = {
  messages: 12,
  locations: 14,
  conflicted: 0,
  parse_failed: 1,
  headers_only: 2,
  embed_pending: 0,
  lag_s: 0.25,
  last_reconcile: "2026-11-16T08:00:00Z",
  models: { chat: "synthetic-chat", embed: "", runtime_version: "" },
  accounts: [{ account_id: "account1", name: "", last_sync: null }],
};

describe("protocol.ts chat/status types vs the frozen 1.0 contract", () => {
  it("samples typed with the TS mirrors validate against tern-bridge-1.0", () => {
    const send: ChatSendParams = { session_id: "s-1", text: "hello" };
    const turn: TurnId = { turn_id: "t-1" };
    const delta: ChatDelta = { turn_id: "t-1", text: "Hi" };
    const done: ChatDone = { turn_id: "t-1", citations: [MID, MID_X] };
    const error: ChatErrorNotif = { turn_id: "t-1", reason: "cancelled" };
    expect(paramsErrors("chat.send", send)).toEqual([]);
    expect(resultErrors("chat.send", turn)).toEqual([]);
    expect(paramsErrors("chat.cancel", turn)).toEqual([]);
    expect(resultErrors("chat.cancel", {})).toEqual([]);
    expect(paramsErrors("chat.delta", delta)).toEqual([]);
    expect(paramsErrors("chat.done", done)).toEqual([]);
    expect(paramsErrors("chat.error", error)).toEqual([]);
    expect(paramsErrors("index.status", {})).toEqual([]);
    expect(resultErrors("index.status", sampleStatus)).toEqual([]);
  });

  it("the contract check is not vacuous", () => {
    expect(paramsErrors("chat.send", { session_id: "", text: "x" })).not.toEqual([]);
    expect(paramsErrors("chat.done", { turn_id: "t", citations: ["NOT-A-MID"] })).not.toEqual([]);
    const { last_reconcile: _, ...noReconcile } = sampleStatus;
    expect(resultErrors("index.status", noReconcile)).not.toEqual([]);
  });
});

describe("protocol.ts 1.1 types vs the frozen 1.1 contract (plan 07 part B, #37)", () => {
  it("samples typed with the TS mirrors validate against tern-bridge-1.1", () => {
    const open: OpenMidParams = { mid: MID };
    const opened: OpenMidResult = { opened: true, folder_id: "account1://INBOX" };
    const info: DaemonInfo = { remote_llm: true, chat_model: "synthetic-chat" };
    expect(paramsErrors("ui.open_mid", open, "1.1")).toEqual([]);
    expect(resultErrors("ui.open_mid", opened, "1.1")).toEqual([]);
    expect(paramsErrors("daemon.info", {}, "1.1")).toEqual([]);
    expect(resultErrors("daemon.info", info, "1.1")).toEqual([]);
    // 1.0 methods are unchanged in 1.1
    expect(resultErrors("index.status", sampleStatus, "1.1")).toEqual([]);
  });

  it("the 1.1 check is not vacuous, and the 1.1 methods are not in 1.0", () => {
    expect(resultErrors("ui.open_mid", { opened: false, folder_id: "f" }, "1.1")).not.toEqual([]);
    expect(resultErrors("ui.open_mid", { opened: true }, "1.1")).not.toEqual([]);
    expect(resultErrors("daemon.info", { remote_llm: true }, "1.1")).not.toEqual([]);
    expect(paramsErrors("ui.open_mid", { mid: "NOT-A-MID" }, "1.1")).not.toEqual([]);
    expect(() => paramsErrors("ui.open_mid", { mid: MID })).toThrow("not in tern-bridge/1.0");
  });
});

describe("ChatRouter: tab → bridge", () => {
  it("send → chat.send with params valid against the frozen schema → turn reply", async () => {
    const { bridge, tab } = setup();
    const p = await tab();
    p.send({ type: "send", req: 7, session_id: "s-1", text: "list synthetic invoices" });
    await tick();
    const call = bridge.call("chat.send");
    expect(call.params).toEqual({ session_id: "s-1", text: "list synthetic invoices" });
    expect(paramsErrors("chat.send", call.params)).toEqual([]);
    expect(p.posted).toEqual([]); // nothing until ternd answers
    call.resolve({ turn_id: "t-1" });
    await tick();
    expect(p.posted).toEqual([{ type: "turn", req: 7, turn_id: "t-1" }]);
  });

  it("bridge not ready → error unavailable 'ternd not connected', nothing sent", async () => {
    const { bridge, tab } = setup();
    const p = await tab();
    for (const state of ["disconnected", "connecting", "rejected"] as const) {
      bridge.state = state;
      p.send({ type: "send", req: 1, session_id: "s-1", text: "hi" });
      p.send({ type: "status", req: 2 });
      p.send({ type: "cancel", req: 3, turn_id: "t-9" });
      await tick();
      expect(bridge.calls).toEqual([]);
      expect(p.posted).toEqual([
        { type: "error", req: 1, name: "unavailable", message: "ternd not connected" },
        { type: "error", req: 2, name: "unavailable", message: "ternd not connected" },
        { type: "error", req: 3, name: "unavailable", message: "ternd not connected" },
      ]);
      p.posted = [];
    }
  });

  it("a bridge error on chat.send reaches the tab with its code and message", async () => {
    const { bridge, tab, logs } = setup();
    const p = await tab();
    p.send({ type: "send", req: 4, session_id: "s-1", text: SECRET });
    await tick();
    bridge.call("chat.send").reject(new BridgeError("invalid_params", "session busy"));
    await tick();
    expect(p.posted).toEqual([
      { type: "error", req: 4, name: "invalid_params", message: "session busy" },
    ]);
    expect(logs).toEqual(["send failed: invalid_params"]);
  });

  it("status → index.status {} → status_result with the daemon's IndexStatus", async () => {
    const { bridge, tab } = setup();
    const p = await tab();
    p.send({ type: "status", req: 5 });
    await tick();
    const call = bridge.call("index.status");
    expect(call.params).toEqual({});
    call.resolve(sampleStatus);
    await tick();
    expect(p.posted).toEqual([{ type: "status_result", req: 5, status: sampleStatus }]);
  });

  it("cancel of the tab's own turn → chat.cancel → ok", async () => {
    const { bridge, tab } = setup();
    const p = await tab();
    await startTurn(bridge, p, "t-1");
    p.send({ type: "cancel", req: 2, turn_id: "t-1" });
    await tick();
    const call = bridge.call("chat.cancel");
    expect(call.params).toEqual({ turn_id: "t-1" });
    expect(paramsErrors("chat.cancel", call.params)).toEqual([]);
    call.resolve({});
    await tick();
    expect(p.of("ok")).toEqual([{ type: "ok", req: 2 }]);
  });

  it("cancel of a turn another open tab owns is refused locally (one bridge peer for all tabs)", async () => {
    const { bridge, tab } = setup();
    const a = await tab();
    const b = await tab();
    await startTurn(bridge, a, "t-a");
    b.send({ type: "cancel", req: 9, turn_id: "t-a" });
    await tick();
    expect(bridge.methods()).toEqual(["chat.send"]);
    expect(b.posted).toEqual([
      { type: "error", req: 9, name: "invalid_params", message: "turn belongs to another tab" },
    ]);
  });

  it("malformed tab messages: answered invalid_params when req is known, else dropped", async () => {
    const { bridge, tab, logs } = setup();
    const p = await tab();
    p.send({ type: "send", req: 1, session_id: "s-1", text: "" });
    p.send({ type: "send", req: 2, text: "no session" });
    p.send({ type: "nope", req: 3 });
    p.send({ type: "status" });
    p.send("not an object");
    await tick();
    expect(bridge.calls).toEqual([]);
    expect(p.posted).toEqual(
      [1, 2, 3].map((req) => ({
        type: "error",
        req,
        name: "invalid_params",
        message: "malformed request",
      })),
    );
    expect(logs).toEqual(["malformed tab message dropped", "malformed tab message dropped"]);
  });

  it("a runtime port with another name is ignored", async () => {
    const { bridge, router, logs } = setup();
    const p = new FakeTabPort("something-else");
    router.attach(p);
    p.send({ type: "status", req: 1 });
    await tick();
    expect(bridge.calls).toEqual([]);
    expect(router.openTabs).toBe(0);
    expect(logs).toEqual(["runtime port ignored (not a chat port)"]);
  });

  it("a tern-chat port not opened by the chat page is ignored (sender URL not logged)", async () => {
    const { bridge, router, logs, tab } = setup();
    const others = [
      new FakeTabPort(CHAT_PORT_NAME, null),
      new FakeTabPort(CHAT_PORT_NAME, `${EXT_BASE}content/${SECRET}.html`),
      new FakeTabPort(CHAT_PORT_NAME, `https://example.org/${CHAT_SPACE.url}`),
      new FakeTabPort(CHAT_PORT_NAME, `moz-extension://other-uuid/${CHAT_SPACE.url}`),
    ];
    for (const p of others) {
      router.attach(p);
      p.send({ type: "status", req: 1 });
    }
    await tick();
    expect(bridge.calls).toEqual([]);
    expect(router.openTabs).toBe(0);
    expect(others.map((p) => p.posted)).toEqual([[], [], [], []]);
    expect(logs).toEqual(Array(4).fill("runtime port ignored (not from the chat page)"));
    // The chat page itself (also with a fragment or query) is served.
    const withHash = new FakeTabPort(CHAT_PORT_NAME, `${PAGE_URL}#s`);
    router.attach(withHash);
    await tab();
    expect(router.openTabs).toBe(2);
  });
});

describe("ChatRouter: open and info (tern-bridge 1.1, plan 07 step 9b)", () => {
  it("open {mid} → ui.open_mid {mid} → ok", async () => {
    const { bridge, tab } = setup();
    const p = await tab();
    p.send({ type: "open", req: 3, mid: MID });
    await tick();
    const call = bridge.call("ui.open_mid");
    expect(call.params).toEqual({ mid: MID });
    call.resolve({ opened: true, folder_id: "f1" });
    await tick();
    expect(p.posted).toEqual([{ type: "ok", req: 3 }]);
  });

  it("a refused open answers the bridge error (code and ternd's message)", async () => {
    const { bridge, tab, logs } = setup();
    const p = await tab();
    p.send({ type: "open", req: 4, mid: MID_X });
    await tick();
    bridge
      .call("ui.open_mid")
      .reject(new BridgeError("state_changed", "conflicted [mid_conflicted]"));
    await tick();
    expect(p.posted).toEqual([
      { type: "error", req: 4, name: "state_changed", message: "conflicted [mid_conflicted]" },
    ]);
    expect(logs).toEqual(["open failed: state_changed"]);
  });

  it("info → daemon.info {} → info_result", async () => {
    const { bridge, tab } = setup();
    const p = await tab();
    p.send({ type: "info", req: 6 });
    await tick();
    const call = bridge.call("daemon.info");
    expect(call.params).toEqual({});
    call.resolve({ remote_llm: true, chat_model: "synthetic-chat" });
    await tick();
    expect(p.posted).toEqual([
      { type: "info_result", req: 6, info: { remote_llm: true, chat_model: "synthetic-chat" } },
    ]);
  });

  it.each([[null], [{ remote_llm: "yes", chat_model: "m" }], [{ remote_llm: true }]])(
    "a malformed daemon.info reply %j fails the request (never a guessed banner)",
    async (reply) => {
      const { bridge, tab } = setup();
      const p = await tab();
      p.send({ type: "info", req: 7 });
      await tick();
      bridge.call("daemon.info").resolve(reply);
      await tick();
      expect(p.posted).toEqual([
        { type: "error", req: 7, name: "unavailable", message: "ternd sent an invalid reply" },
      ]);
    },
  );

  it.each([["open"], ["info"]])(
    "%s on a session that negotiated minor 0 is method_not_available and sends nothing",
    async (type) => {
      const { bridge, tab } = setup();
      bridge.minor = 0;
      const p = await tab();
      p.send({ type, req: 8, mid: MID });
      await tick();
      expect(bridge.methods()).toEqual([]);
      expect(p.posted).toEqual([
        {
          type: "error",
          req: 8,
          name: "method_not_available",
          message: "needs tern-bridge 1.1 (ternd is older)",
        },
      ]);
    },
  );

  it.each([
    [{ type: "open", req: 9 }],
    [{ type: "open", req: 9, mid: "NOT-A-MID" }],
    [{ type: "open", req: 9, mid: 7 }],
  ])("a malformed open %j is invalid_params and sends nothing", async (msg) => {
    const { bridge, tab } = setup();
    const p = await tab();
    p.send(msg);
    await tick();
    expect(bridge.methods()).toEqual([]);
    expect(p.posted).toEqual([
      { type: "error", req: 9, name: "invalid_params", message: "malformed request" },
    ]);
  });

  it("open while the bridge is not ready is unavailable", async () => {
    const { bridge, tab } = setup();
    bridge.state = "connecting";
    const p = await tab();
    p.send({ type: "open", req: 10, mid: MID });
    await tick();
    expect(bridge.methods()).toEqual([]);
    expect(p.of("error")[0]?.name).toBe("unavailable");
  });
});

describe("ChatRouter: the #9 send gate (plan 07 step 9b review)", () => {
  it("a tab that has not learned the model setting cannot send", async () => {
    const { bridge, tab, logs } = setup();
    const p = await tab({ checked: false });
    p.send({ type: "send", req: 1, session_id: "s-1", text: "hi" });
    await tick();
    expect(bridge.methods()).toEqual([]);
    expect(p.posted).toEqual([
      {
        type: "error",
        req: 1,
        name: "unavailable",
        message: "model setting not checked; ask info first",
      },
    ]);
    expect(logs).toEqual(["send refused: model setting not checked in this session"]);
  });

  it("a send posted before the tab processed session_end never reaches the new session", async () => {
    const { bridge, router, tab } = setup();
    const p = await tab();
    router.sessionEnded(); // ternd restarted (maybe with another config)
    p.send({ type: "send", req: 1, session_id: "s-1", text: "stale" });
    await tick();
    expect(bridge.methods()).toEqual([]);
    expect(p.of("error")[0]?.message).toBe("model setting not checked; ask info first");
    // After a new info answer, sends go out again.
    p.send({ type: "info", req: 2 });
    await tick();
    bridge.call("daemon.info").resolve({ remote_llm: true, chat_model: "big-remote" });
    await tick();
    p.send({ type: "send", req: 3, session_id: "s-1", text: "fresh" });
    await tick();
    expect(bridge.methods()).toEqual(["daemon.info", "chat.send"]);
  });

  it("an info answer from the ended session does not unlock sending", async () => {
    const { bridge, router, tab } = setup();
    const p = await tab({ checked: false });
    p.send({ type: "info", req: 1 });
    await tick();
    router.sessionEnded();
    bridge.call("daemon.info").resolve({ remote_llm: false, chat_model: "old" });
    await tick();
    p.send({ type: "send", req: 2, session_id: "s-1", text: "q" });
    await tick();
    expect(bridge.methods()).toEqual(["daemon.info"]);
    expect(p.of("error").at(-1)?.name).toBe("unavailable");
  });

  it("method_not_available for info (a 1.0 session) lets the tab send: ternd refuses remote chat itself", async () => {
    const { bridge, tab } = setup();
    bridge.minor = 0;
    const p = await tab({ checked: false });
    p.send({ type: "info", req: 1 });
    p.send({ type: "send", req: 2, session_id: "s-1", text: "q" });
    await tick();
    expect(bridge.methods()).toEqual(["chat.send"]);
  });
});

describe("ChatRouter: bridge → the owning tab", () => {
  it("a turn's deltas reach only the owning tab; chat.done ends the turn", async () => {
    const { bridge, tab, logs } = setup();
    const a = await tab();
    const b = await tab();
    await startTurn(bridge, a, "t-a");
    bridge.emit("chat.delta", { turn_id: "t-a", text: "Two synthetic " });
    bridge.emit("chat.delta", { turn_id: "t-a", text: `invoices [${MID}].` });
    bridge.emit("chat.done", { turn_id: "t-a", citations: [MID] });
    expect(a.posted.slice(1)).toEqual([
      { type: "delta", turn_id: "t-a", text: "Two synthetic " },
      { type: "delta", turn_id: "t-a", text: `invoices [${MID}].` },
      { type: "done", turn_id: "t-a", citations: [MID] },
    ]);
    expect(b.posted).toEqual([]);
    // Ended: a late delta is dropped with a log line naming the turn id only.
    bridge.emit("chat.delta", { turn_id: "t-a", text: SECRET });
    expect(a.posted).toHaveLength(4);
    expect(logs).toEqual(["chat.delta for unknown turn t-a dropped"]);
  });

  it("chat.error ends the turn with its reason", async () => {
    const { bridge, tab } = setup();
    const p = await tab();
    await startTurn(bridge, p, "t-1");
    bridge.emit("chat.delta", { turn_id: "t-1", text: "partial" });
    bridge.emit("chat.error", { turn_id: "t-1", reason: "max_tool_calls" });
    bridge.emit("chat.done", { turn_id: "t-1", citations: [] });
    expect(p.posted.slice(1)).toEqual([
      { type: "delta", turn_id: "t-1", text: "partial" },
      { type: "chat_error", turn_id: "t-1", reason: "max_tool_calls" },
    ]);
  });

  it("a delta for an unknown turn is dropped with a log line (turn id only)", async () => {
    const { bridge, tab, logs } = setup();
    const p = await tab();
    bridge.emit("chat.delta", { turn_id: "t-unknown", text: SECRET });
    bridge.emit("chat.done", { turn_id: "t-unknown", citations: [] });
    bridge.emit("chat.delta", { text: SECRET });
    expect(p.posted).toEqual([]);
    expect(logs).toEqual([
      "chat.delta for unknown turn t-unknown dropped",
      "chat.done for unknown turn t-unknown dropped",
      "chat.delta without a turn id dropped",
    ]);
  });

  it("chat.error without a string reason ends the turn as chat_error internal, logged", async () => {
    const { bridge, tab, logs } = setup();
    const p = await tab();
    await startTurn(bridge, p, "t-1");
    bridge.emit("chat.error", { turn_id: "t-1" });
    bridge.emit("chat.error", { turn_id: "t-1", reason: 7 });
    bridge.emit("chat.delta", { turn_id: "t-1", text: SECRET });
    expect(p.posted.slice(1)).toEqual([{ type: "chat_error", turn_id: "t-1", reason: "internal" }]);
    expect(logs).toEqual([
      "chat.error without a reason (turn t-1)",
      "chat.error for unknown turn t-1 dropped",
      "chat.delta for unknown turn t-1 dropped",
    ]);
  });

  it("chat.delta without string text is dropped (turn id only logged); the turn stays live", async () => {
    const { bridge, tab, logs } = setup();
    const p = await tab();
    await startTurn(bridge, p, "t-1");
    bridge.emit("chat.delta", { turn_id: "t-1" });
    bridge.emit("chat.delta", { turn_id: "t-1", text: 5, note: SECRET });
    expect(p.posted).toHaveLength(1);
    bridge.emit("chat.delta", { turn_id: "t-1", text: "still here" });
    expect(p.posted.slice(1)).toEqual([{ type: "delta", turn_id: "t-1", text: "still here" }]);
    expect(logs).toEqual([
      "chat.delta without text dropped (turn t-1)",
      "chat.delta without text dropped (turn t-1)",
    ]);
    expect(logs.join("\n")).not.toContain("SENTINEL");
  });

  it("chat.done without a citation list fails the turn instead of showing unchecked chips", async () => {
    const { bridge, tab } = setup();
    const p = await tab();
    await startTurn(bridge, p, "t-1");
    bridge.emit("chat.done", { turn_id: "t-1" });
    expect(p.posted.at(-1)).toEqual({ type: "chat_error", turn_id: "t-1", reason: "internal" });
  });

  it("two tabs, two turns: each tab sees only its own stream", async () => {
    const { bridge, tab } = setup();
    const a = await tab();
    const b = await tab();
    await startTurn(bridge, a, "t-a", 1);
    await startTurn(bridge, b, "t-b", 1);
    bridge.emit("chat.delta", { turn_id: "t-b", text: "B" });
    bridge.emit("chat.delta", { turn_id: "t-a", text: "A" });
    expect(a.of("delta").map((d) => d.text)).toEqual(["A"]);
    expect(b.of("delta").map((d) => d.text)).toEqual(["B"]);
  });
});

describe("ChatRouter: tabs going away and the session ending", () => {
  it("a tab port that disconnects → chat.cancel for each of its running turns", async () => {
    const { bridge, router, tab, logs } = setup();
    const p = await tab();
    await startTurn(bridge, p, "t-1", 1);
    await startTurn(bridge, p, "t-2", 2);
    p.close();
    expect(router.openTabs).toBe(0);
    const cancels = bridge.calls.filter((c) => c.method === "chat.cancel").map((c) => c.params);
    expect(cancels).toEqual([{ turn_id: "t-1" }, { turn_id: "t-2" }]);
    for (const c of cancels) expect(paramsErrors("chat.cancel", c)).toEqual([]);
    bridge.emit("chat.error", { turn_id: "t-1", reason: "cancelled" });
    expect(p.posted.filter((m) => m.type === "chat_error")).toEqual([]);
    expect(logs).toContain("chat.error for unknown turn t-1 dropped");
  });

  it("a chat.send answered after its tab closed → chat.cancel for that turn", async () => {
    const { bridge, tab } = setup();
    const p = await tab();
    p.send({ type: "send", req: 1, session_id: "s-1", text: "hi" });
    await tick();
    p.close();
    bridge.call("chat.send").resolve({ turn_id: "t-late" });
    await tick();
    expect(bridge.call("chat.cancel").params).toEqual({ turn_id: "t-late" });
    expect(p.posted).toEqual([]);
  });

  it("a failed cancel is logged by code; the router does not cancel when the bridge is down", async () => {
    const { bridge, tab, logs } = setup();
    const a = await tab();
    await startTurn(bridge, a, "t-1");
    a.close();
    bridge.call("chat.cancel").reject(new BridgeError("unavailable", "native port closed"));
    await tick();
    expect(logs).toEqual([
      "cancelling turn t-1 (its tab closed)",
      "cancel of turn t-1 failed: unavailable",
    ]);
    const b = await tab();
    await startTurn(bridge, b, "t-2");
    bridge.state = "disconnected";
    b.close();
    expect(bridge.calls.filter((c) => c.method === "chat.cancel")).toHaveLength(1);
  });

  it("a tab port whose postMessage throws is treated as closed", async () => {
    const { bridge, router, tab, logs } = setup();
    const p = await tab();
    await startTurn(bridge, p, "t-1");
    p.failPost = true;
    bridge.emit("chat.delta", { turn_id: "t-1", text: "x" });
    expect(router.openTabs).toBe(0);
    expect(bridge.call("chat.cancel").params).toEqual({ turn_id: "t-1" });
    expect(logs[0]).toBe("tab port post failed: Error");
  });

  it("session end → every open tab gets session_end; old turns are forgotten", async () => {
    const { bridge, router, tab, logs } = setup();
    const a = await tab();
    const b = await tab();
    await startTurn(bridge, a, "t-1");
    router.sessionEnded();
    expect(a.posted.at(-1)).toEqual({ type: "session_end" });
    expect(b.posted).toEqual([{ type: "session_end" }]);
    expect(logs).toEqual(["session ended with 1 running turn(s)"]);
    bridge.emit("chat.delta", { turn_id: "t-1", text: "stale" });
    expect(a.of("delta")).toEqual([]);
    a.close(); // the forgotten turn is not cancelled on a new session
    expect(bridge.methods()).toEqual(["chat.send"]);
  });
});

describe("ChatRouter: openUrl (links from model text)", () => {
  it("javascript:, data:, file: and other non-http(s) URLs are refused, logged by scheme only", async () => {
    const { tab, opened, logs } = setup();
    const p = await tab();
    const bad = [
      `javascript:alert("${SECRET}")`,
      `data:text/html,${SECRET}`,
      `file:///home/${SECRET}`,
      `${SECRET.toLowerCase()}:payload`,
      `//evil.test/${SECRET}`,
      "",
    ];
    for (const [i, url] of bad.entries()) p.send({ type: "openUrl", req: i, url });
    await tick();
    expect(opened).toEqual([]);
    expect(p.posted).toEqual(
      bad.map((_, req) => ({
        type: "error",
        req,
        name: "invalid_params",
        message: "only http and https links open",
      })),
    );
    expect(logs).toEqual([
      "openUrl refused: scheme javascript:",
      "openUrl refused: scheme data:",
      "openUrl refused: scheme file:",
      "openUrl refused: scheme other",
      "openUrl refused: scheme unparseable",
      "openUrl refused: scheme unparseable",
    ]);
  });

  it("https: and http: URLs are forwarded to the default browser", async () => {
    const { bridge, tab, opened } = setup();
    const p = await tab();
    p.send({ type: "openUrl", req: 1, url: "https://example.org/a?b=c" });
    p.send({ type: "openUrl", req: 2, url: "http://example.net/" });
    await tick();
    expect(opened).toEqual(["https://example.org/a?b=c", "http://example.net/"]);
    expect(p.posted).toEqual([
      { type: "ok", req: 1 },
      { type: "ok", req: 2 },
    ]);
    expect(bridge.calls).toEqual([]); // links never touch the bridge
  });

  it("a failing openDefaultBrowser is answered unavailable and logged by error type", async () => {
    const logs: string[] = [];
    const router = new ChatRouter(
      new FakeBridge(),
      async () => {
        throw new Error(`cannot open ${SECRET}`);
      },
      PAGE_URL,
      (m) => logs.push(m),
    );
    const p = new FakeTabPort();
    router.attach(p);
    p.send({ type: "openUrl", req: 1, url: "https://example.org/" });
    await tick();
    expect(p.posted).toEqual([
      { type: "error", req: 1, name: "unavailable", message: "could not open the default browser" },
    ]);
    expect(logs).toEqual(["openUrl failed: Error"]);
  });

  it("isOpenableUrl / schemeForLog / parsePageRequest edge cases", () => {
    expect(isOpenableUrl("HTTPS://EXAMPLE.ORG/")).toBe(true);
    expect(isOpenableUrl(" javascript:alert(1)")).toBe(false);
    expect(isOpenableUrl("java\tscript:alert(1)")).toBe(false);
    expect(schemeForLog("mailto:someone@example.org")).toBe("mailto:");
    expect(parsePageRequest({ type: "status", req: 1.5 })).toEqual({ ok: false, req: null });
    expect(parsePageRequest({ type: "openUrl", req: 1, url: 5 })).toEqual({ ok: false, req: 1 });
  });
});

describe("no chat content in the router's logs", () => {
  it("sentinels in user text, model text, URLs and daemon messages never reach a log line", async () => {
    const { bridge, tab, logs } = setup();
    const p = await tab();
    p.send({ type: "send", req: 1, session_id: `${SECRET}-session`, text: SECRET });
    await tick();
    bridge.call("chat.send").resolve({ turn_id: "t-1" });
    await tick();
    bridge.emit("chat.delta", { turn_id: "t-1", text: SECRET });
    bridge.emit("chat.delta", { turn_id: "t-gone", text: SECRET });
    bridge.emit("chat.error", { turn_id: "t-1", reason: "llm_unavailable" });
    p.send({ type: "send", req: 2, session_id: "s", text: SECRET });
    await tick();
    bridge.call("chat.send").reject(new BridgeError("unavailable", `refused ${SECRET}`));
    p.send({ type: "openUrl", req: 3, url: `ftp://${SECRET}.test/` });
    p.send({ type: "bogus", req: 4, text: SECRET });
    await tick();
    expect(logs.length).toBeGreaterThan(0);
    for (const l of logs) expect(l).not.toContain("SENTINEL");
  });
});

describe("the real Bridge carries the router's traffic", () => {
  class Port implements NativePort {
    posted: { id?: number; method?: string; params?: unknown }[] = [];
    private cbs: ((m: unknown) => void)[] = [];
    onMessage = { addListener: (cb: (m: unknown) => void) => void this.cbs.push(cb) };
    onDisconnect = { addListener: () => {} };
    postMessage(m: unknown): void {
      this.posted.push(m as { id?: number; method?: string });
    }
    disconnect(): void {}
    emit(m: unknown): void {
      for (const cb of this.cbs) cb(m);
    }
    last(method: string): { id?: number; params?: unknown } {
      const m = [...this.posted].reverse().find((x) => x.method === method);
      if (m === undefined) throw new Error(`no ${method}`);
      return m;
    }
  }

  it("chat.send goes out on the native port; native chat.delta/done reach the tab", async () => {
    const port = new Port();
    const platform: Platform = {
      version: "0.1.0",
      connectNative: () => port,
      storageGet: async () => "123e4567-e89b-42d3-a456-426614174000",
      storageSet: async () => {},
      alarmCreate: async (_n, p) => p,
      alarmClear: async () => {},
      onAlarm: () => {},
      onStartup: () => {},
      onInstalled: () => {},
      randomUUID: () => "123e4567-e89b-42d3-a456-426614174000",
    };
    const bridge = new Bridge(platform, () => {});
    const router = new ChatRouter(
      bridge,
      async () => {},
      PAGE_URL,
      () => {},
    );
    const tabPort = new FakeTabPort();
    router.attach(tabPort);
    const connected = bridge.connect();
    await tick();
    port.emit({
      jsonrpc: "2.0",
      id: port.last("hello").id,
      result: {
        protocol: "tern-bridge/1",
        minor: 1,
        role: "daemon",
        version: "test",
        capabilities: [],
      },
    });
    await tick();
    port.emit({ jsonrpc: "2.0", id: port.last("approvals.pending").id, result: [] });
    await connected;
    expect(bridge.state).toBe("ready");

    // The tab learns the model setting first (the #9 send gate), over the real bridge too.
    tabPort.send({ type: "info", req: 0 });
    await tick();
    const info = port.last("daemon.info");
    expect(info.params).toEqual({});
    port.emit({
      jsonrpc: "2.0",
      id: info.id,
      result: { remote_llm: false, chat_model: "synthetic-chat" },
    });
    await tick();
    expect(tabPort.posted.shift()).toEqual({
      type: "info_result",
      req: 0,
      info: { remote_llm: false, chat_model: "synthetic-chat" },
    });

    tabPort.send({ type: "send", req: 1, session_id: "s-1", text: "hello" });
    await tick();
    const send = port.last("chat.send");
    expect(paramsErrors("chat.send", send.params)).toEqual([]);
    // P3: ternd writes the response before the first delta; they arrive as separate messages.
    port.emit({ jsonrpc: "2.0", id: send.id, result: { turn_id: "t-1" } });
    await tick();
    port.emit({ jsonrpc: "2.0", method: "chat.delta", params: { turn_id: "t-1", text: "Hi" } });
    port.emit({ jsonrpc: "2.0", method: "chat.done", params: { turn_id: "t-1", citations: [] } });
    expect(tabPort.posted).toEqual([
      { type: "turn", req: 1, turn_id: "t-1" },
      { type: "delta", turn_id: "t-1", text: "Hi" },
      { type: "done", turn_id: "t-1", citations: [] },
    ]);
  });

  it("the router refuses a bridge whose chat notifications are already taken", () => {
    const bridge = new FakeBridge();
    bridge.notifications.set("chat.done", () => {});
    expect(() => new ChatRouter(bridge, async () => {}, PAGE_URL)).toThrow(
      "chat.done is already handled",
    );
  });
});

// ---------------------------------------------------------------------------------------------
// platform.ts chat APIs, startChat, chatPagePort, main.ts wiring.

interface FakeChatMessenger {
  onConnect: ((p: RuntimePort) => void)[];
  spaces: { name: string }[];
  created: { name: string; url: string; props: { title: string } }[];
  queries: unknown[];
  browser: string[];
  failCreate: boolean;
  m: MessengerRuntime;
}

function chatMessenger(): FakeChatMessenger {
  const f: FakeChatMessenger = {
    onConnect: [],
    spaces: [],
    created: [],
    queries: [],
    browser: [],
    failCreate: false,
    m: undefined as unknown as MessengerRuntime,
  };
  f.m = {
    runtime: {
      getManifest: () => ({ version: "0.1.0" }),
      getURL: (path) => `${EXT_BASE}${path}`,
      connectNative: () => {
        throw new Error("not in these tests");
      },
      onStartup: { addListener: () => {} },
      onInstalled: { addListener: () => {} },
      onConnect: { addListener: (cb) => void f.onConnect.push(cb) },
    },
    storage: { local: { get: async () => ({}), set: async () => {} } },
    alarms: {
      create: async () => {},
      clear: async () => true,
      get: async () => undefined,
      onAlarm: { addListener: () => {} },
    },
    spaces: {
      query: async (q) => {
        f.queries.push(q);
        return f.spaces.filter((s) => s.name === q.name);
      },
      create: async (name, url, props) => {
        if (f.failCreate) throw new Error(`Failed to create space ${SECRET}`);
        f.created.push({ name, url, props });
        f.spaces.push({ name });
        return { id: 1, name };
      },
    },
    windows: {
      openDefaultBrowser: async (url) => {
        f.browser.push(url);
      },
    },
  };
  return f;
}

describe("platform.ts chat APIs", () => {
  it("spaceEnsure queries first and creates the space only when absent", async () => {
    const f = chatMessenger();
    const chat = chatPlatformFrom(f.m) as ChatPlatform;
    expect(await chat.spaceEnsure("tern", "ui/chat/index.html", "tern")).toBe("created");
    expect(await chat.spaceEnsure("tern", "ui/chat/index.html", "tern")).toBe("present");
    expect(f.created).toEqual([
      { name: "tern", url: "ui/chat/index.html", props: { title: "tern" } },
    ]);
    expect(f.queries).toEqual([
      { name: "tern", isSelfOwned: true },
      { name: "tern", isSelfOwned: true },
    ]);
  });

  it("onConnect and openDefaultBrowser go to runtime.onConnect and windows.openDefaultBrowser", async () => {
    const f = chatMessenger();
    const chat = platformFrom(f.m).chat as ChatPlatform;
    const seen: string[] = [];
    chat.onConnect((p) => seen.push(p.name));
    f.onConnect[0]?.(new FakeTabPort());
    await chat.openDefaultBrowser("https://example.org/");
    expect(seen).toEqual([CHAT_PORT_NAME]);
    expect(f.browser).toEqual(["https://example.org/"]);
  });

  it("chat is null when runtime.onConnect, spaces or windows is missing", () => {
    for (const drop of ["onConnect", "spaces", "windows"] as const) {
      const f = chatMessenger();
      if (drop === "onConnect") delete f.m.runtime.onConnect;
      else delete f.m[drop];
      expect(chatPlatformFrom(f.m), drop).toBeNull();
      expect(platformFrom(f.m).chat, drop).toBeNull();
    }
  });
});

describe("startChat", () => {
  it("registers onConnect synchronously and ensures the space; tabs reach the router", async () => {
    const f = chatMessenger();
    const logs: string[] = [];
    const bridge = new FakeBridge();
    const router = startChat(
      chatPlatformFrom(f.m),
      bridge,
      (m) => logs.push(m),
      (m) => logs.push(m),
    );
    expect(router).not.toBeNull();
    expect(f.onConnect).toHaveLength(1); // before any await
    await tick();
    expect(f.created.map((c) => [c.name, c.url, c.props.title])).toEqual([
      [CHAT_SPACE.name, CHAT_SPACE.url, CHAT_SPACE.title],
    ]);
    expect(logs).toEqual(["space created"]);
    const p = new FakeTabPort();
    f.onConnect[0]?.(p);
    p.send({ type: "openUrl", req: 1, url: "https://example.org/x" });
    await tick();
    expect(f.browser).toEqual(["https://example.org/x"]);
    expect(router?.openTabs).toBe(1);
  });

  it("a failed spaces.create is logged by error type only", async () => {
    const f = chatMessenger();
    f.failCreate = true;
    const errors: string[] = [];
    startChat(
      chatPlatformFrom(f.m),
      new FakeBridge(),
      () => {},
      (m) => errors.push(m),
    );
    await tick();
    expect(errors).toEqual(["space not created: Error"]);
  });

  it("without the chat APIs nothing is registered and the error is logged", () => {
    const errors: string[] = [];
    const bridge = new FakeBridge();
    expect(
      startChat(
        null,
        bridge,
        () => {},
        (m) => errors.push(m),
      ),
    ).toBeNull();
    expect(errors).toEqual(["runtime.onConnect, spaces or windows missing: chat tab disabled"]);
    expect(bridge.notifications.size).toBe(0);
  });
});

describe("chatPagePort (tb/api.ts, the tab page's only messenger use)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("connects a runtime port named tern-chat", () => {
    const names: string[] = [];
    const port = new FakeTabPort();
    vi.stubGlobal("messenger", {
      runtime: {
        connect: (info: { name: string }) => {
          names.push(info.name);
          return port;
        },
      },
    });
    expect(chatPagePort()).toBe(port);
    expect(names).toEqual(["tern-chat"]);
    expect(CHAT_PORT_NAME).toBe("tern-chat");
  });

  it("throws outside an extension page", () => {
    expect(() => chatPagePort()).toThrow("no messenger global");
  });
});

describe("main.ts wiring", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.resetModules();
  });

  it("registers runtime.onConnect at load, creates the space, and serves a chat tab", async () => {
    const f = chatMessenger();
    const native: NativePort = {
      postMessage: () => {},
      disconnect: () => {},
      onMessage: { addListener: () => {} },
      onDisconnect: { addListener: () => {} },
    };
    f.m.runtime.connectNative = () => native;
    const mail = {
      accounts: { list: async () => [], get: async () => null },
      folders: new Proxy({}, { get: () => ({ addListener: () => {} }) }),
      messages: new Proxy(
        {},
        { get: (_t, k) => (k === "tags" ? { list: async () => [] } : { addListener: () => {} }) },
      ),
      messageDisplay: { open: async () => {} },
    };
    vi.stubGlobal("messenger", Object.assign(f.m, mail));
    vi.spyOn(console, "info").mockImplementation(() => {});
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.resetModules();
    await import("../src/background/main");
    expect(f.onConnect).toHaveLength(1);
    await tick();
    expect(f.created.map((c) => c.name)).toEqual([CHAT_SPACE.name]);
    // The bridge is not ready (no hello answer): a tab's request is answered, nothing hangs.
    const p = new FakeTabPort();
    f.onConnect[0]?.(p);
    p.send({ type: "status", req: 1 });
    await tick();
    expect(p.posted).toEqual([
      { type: "error", req: 1, name: "unavailable", message: "ternd not connected" },
    ]);
    expect(errors).not.toHaveBeenCalled();
  });
});
