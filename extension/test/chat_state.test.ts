// Plan 08b step 5: the chat tab's reducer (ui/chat/state.ts). Pure: actions in, state and the
// requests to post out. Synthetic ids and text only.

import { describe, expect, it } from "vitest";
import { ERROR_CODES, type IndexStatus, type PageMessage, type PageRequest } from "../src/protocol";
import {
  type Action,
  CHIP_NOTICE,
  type ChatState,
  displayBlocks,
  errorText,
  INFO_PENDING_NOTICE,
  initialState,
  MAX_CHAT_TEXT,
  outcomeText,
  PORT_CLOSED_NOTICE,
  parsePageMessage,
  remoteBanner,
  remoteBannerText,
  replyReq,
  runningTurn,
  SESSION_END_NOTICE,
  shortMid,
  type Turn,
  update,
} from "../src/ui/chat/state";
import type { Block, Inline } from "../src/ui/sanitize";

const A = "a".repeat(64);
const B = "b".repeat(64);
const X = `x-${"c".repeat(64)}`;
const SID = "session-1";

const STATUS: IndexStatus = {
  messages: 12,
  locations: 14,
  conflicted: 0,
  parse_failed: 0,
  headers_only: 1,
  embed_pending: 3,
  lag_s: 0,
  last_reconcile: null,
  models: { chat: "synthetic-chat", embed: "synthetic-embed", runtime_version: "" },
  accounts: [{ account_id: "acct-1", name: "", last_sync: null }],
};

/** Run actions in order; return the final state and every request posted on the way. */
function run(state: ChatState, ...actions: Action[]): { state: ChatState; out: PageRequest[] } {
  const out: PageRequest[] = [];
  let s = state;
  for (const a of actions) {
    const u = update(s, a);
    s = u.state;
    out.push(...u.out);
  }
  return { state: s, out };
}

const msg = (m: PageMessage): Action => ({ type: "message", msg: m });

/**
 * A tab that already knows ternd's model setting (a local model): daemon.info answered. Sends are
 * gated on it since bridge 1.1 (plan 07 step 9b); the gate itself is tested below.
 */
const fresh = (): ChatState => ({
  ...initialState(SID),
  info: { kind: "known", remoteLlm: false, chatModel: "synthetic-chat" },
});

/** A tab with one sent message answered by ternd with turn id `t-1` (req 1). */
function started(text = "question one"): ChatState {
  const r = run(fresh(), { type: "submit", text }, msg({ type: "turn", req: 1, turn_id: "t-1" }));
  return r.state;
}

const turn = (s: ChatState, i = 0): Turn => {
  const t = s.turns[i];
  if (t === undefined) throw new Error(`no turn ${i}`);
  return t;
};

/** Every inline of a block list, depth first. */
function inlines(blocks: readonly Block[]): Inline[] {
  const out: Inline[] = [];
  const walk = (xs: readonly Inline[]): void => {
    for (const n of xs) {
      out.push(n);
      if (n.t === "strong" || n.t === "em") walk(n.children);
    }
  };
  for (const b of blocks) {
    if (b.t === "p" || b.t === "h") walk(b.children);
    if (b.t === "ul" || b.t === "ol") for (const li of b.items) walk(li.children);
  }
  return out;
}

const chips = (blocks: readonly Block[]): string[] =>
  inlines(blocks).flatMap((n) => (n.t === "cite" ? [n.mid] : []));

describe("sending", () => {
  it("submit posts chat.send with the tab's session id and makes a pending turn", () => {
    const { state, out } = run(fresh(), { type: "submit", text: "hello there" });
    expect(out).toEqual([
      { type: "send", req: 1, session_id: SID, text: "hello there", mode: "manual" },
    ]);
    expect(turn(state).phase).toBe("pending");
    expect(turn(state).user).toBe("hello there");
    expect(state.waiting.get(1)).toEqual({ kind: "send", turnKey: turn(state).key });
  });

  it("an empty or blank message sends nothing", () => {
    const { state, out } = run(fresh(), { type: "submit", text: "  \n\t" });
    expect(out).toEqual([]);
    expect(state.turns).toEqual([]);
  });

  it("a message over MAX_CHAT_TEXT characters is refused with a notice", () => {
    const { state, out } = run(fresh(), {
      type: "submit",
      text: "x".repeat(MAX_CHAT_TEXT + 1),
    });
    expect(out).toEqual([]);
    expect(state.turns).toEqual([]);
    expect(state.notices[0]?.text).toContain(`at most ${MAX_CHAT_TEXT}`);
    const ok = run(fresh(), { type: "submit", text: "x".repeat(MAX_CHAT_TEXT) });
    expect(ok.out).toHaveLength(1);
  });

  it("a second message while a turn runs is refused with a notice", () => {
    const { state, out } = run(started(), { type: "submit", text: "another" });
    expect(out).toEqual([]);
    expect(state.turns).toHaveLength(1);
    expect(state.notices.map((n) => n.text)).toEqual(["Wait for the answer, or press Stop."]);
  });

  it("each request gets the next req number", () => {
    const { out } = run(
      fresh(),
      { type: "refresh" },
      { type: "submit", text: "q" },
      { type: "link", href: "https://example.test/" },
      { type: "confirm_open" },
    );
    expect(out.map((r) => r.req)).toEqual([1, 2, 3]);
  });
});

describe("streaming", () => {
  it("the turn reply names the turn; deltas append to their own turn only", () => {
    let s = started();
    expect(turn(s).turnId).toBe("t-1");
    s = run(
      s,
      msg({ type: "delta", turn_id: "t-1", text: "Hel" }),
      msg({ type: "delta", turn_id: "t-other", text: "WRONG" }),
      msg({ type: "delta", turn_id: "t-1", text: "lo" }),
    ).state;
    expect(turn(s).text).toBe("Hello");
    expect(turn(s).phase).toBe("streaming");
  });

  it("two turns in a row keep their own text; a late delta of the first is dropped", () => {
    let s = run(
      started(),
      msg({ type: "delta", turn_id: "t-1", text: "first" }),
      msg({ type: "done", turn_id: "t-1", citations: [] }),
      { type: "submit", text: "question two" },
    ).state;
    const sendReq = [...s.waiting].find(([, w]) => w.kind === "send")?.[0];
    expect(sendReq).toBeDefined();
    s = run(
      s,
      msg({ type: "turn", req: sendReq as number, turn_id: "t-2" }),
      msg({ type: "delta", turn_id: "t-2", text: "second" }),
      msg({ type: "delta", turn_id: "t-1", text: " late" }),
    ).state;
    expect(turn(s, 0).text).toBe("first");
    expect(turn(s, 1).text).toBe("second");
  });

  it("done ends the turn, keeps the daemon's citations and refreshes the status", () => {
    const { state, out } = run(
      started(),
      msg({ type: "delta", turn_id: "t-1", text: `See [${A}].` }),
      msg({ type: "done", turn_id: "t-1", citations: [A] }),
    );
    expect(turn(state).phase).toBe("done");
    expect(turn(state).citations).toEqual([A]);
    expect(runningTurn(state)).toBeNull();
    expect(out).toEqual([{ type: "status", req: 2 }]);
  });

  it("chat_error ends the turn with its reason and refreshes the status", () => {
    const { state, out } = run(
      started(),
      msg({ type: "chat_error", turn_id: "t-1", reason: "llm_unavailable" }),
    );
    expect(turn(state).phase).toBe("error");
    expect(turn(state).reason).toBe("llm_unavailable");
    expect(outcomeText(turn(state))).toContain("local model is not reachable");
    expect(out).toEqual([{ type: "status", req: 2 }]);
  });

  it("every agent outcome code reaches the outcome text, unknown codes by name", () => {
    for (const code of ["max_tool_calls", "timeout", "llm_error", "malformed_think", "truncated"]) {
      const s = run(started(), msg({ type: "chat_error", turn_id: "t-1", reason: code })).state;
      expect(outcomeText(turn(s))).not.toContain(`(${code})`);
      expect(outcomeText(turn(s))).not.toBeNull();
    }
    expect(errorText("brand_new_code", null)).toBe("failed (brand_new_code)");
  });

  it("a bridge error on send fails the turn with its name and the router's message", () => {
    const { state } = run(
      fresh(),
      { type: "submit", text: "q" },
      msg({ type: "error", req: 1, name: "unavailable", message: "ternd not connected" }),
    );
    expect(turn(state).phase).toBe("error");
    expect(turn(state).reason).toBe("unavailable");
    expect(outcomeText(turn(state))).toBe("failed (unavailable): ternd not connected");
    expect(state.waiting.size).toBe(0);
    // The next message can be sent.
    expect(run(state, { type: "submit", text: "again" }).out).toHaveLength(1);
  });
});

describe("stop", () => {
  it("stop posts chat.cancel for the running turn; cancelled ends it as stopped", () => {
    let r = run(started(), msg({ type: "delta", turn_id: "t-1", text: "partial" }), {
      type: "stop",
    });
    expect(r.out).toEqual([{ type: "cancel", req: 2, turn_id: "t-1" }]);
    expect(turn(r.state).stopRequested).toBe(true);
    // A second click does not send another cancel.
    expect(run(r.state, { type: "stop" }).out).toEqual([]);
    r = run(
      r.state,
      msg({ type: "ok", req: 2 }),
      msg({ type: "chat_error", turn_id: "t-1", reason: "cancelled" }),
      msg({ type: "delta", turn_id: "t-1", text: " after" }),
    );
    expect(turn(r.state).phase).toBe("cancelled");
    expect(turn(r.state).text).toBe("partial");
    expect(outcomeText(turn(r.state))).toBe("stopped");
  });

  it("stop before the turn reply sends the cancel when the turn id arrives", () => {
    let r = run(fresh(), { type: "submit", text: "q" }, { type: "stop" });
    expect(r.out.map((o) => o.type)).toEqual(["send"]);
    r = run(r.state, msg({ type: "turn", req: 1, turn_id: "t-9" }));
    expect(r.out).toEqual([{ type: "cancel", req: 2, turn_id: "t-9" }]);
  });

  it("a failed cancel is a notice and Stop can be pressed again", () => {
    let r = run(started(), { type: "stop" });
    r = run(r.state, msg({ type: "error", req: 2, name: "invalid_params", message: "x" }));
    expect(r.state.notices.map((n) => n.text)).toEqual(["Stop failed (invalid_params)."]);
    expect(turn(r.state).stopRequested).toBe(false);
    expect(run(r.state, { type: "stop" }).out).toEqual([
      { type: "cancel", req: 3, turn_id: "t-1" },
    ]);
  });

  it("stop with nothing running does nothing", () => {
    expect(run(fresh(), { type: "stop" }).out).toEqual([]);
  });
});

describe("session_end and a closed page port fail everything still waiting", () => {
  /** A tab with one of each request waiting: a running turn's cancel, a status, an openUrl. */
  function busy(): ChatState {
    return run(
      started(),
      msg({ type: "delta", turn_id: "t-1", text: "part" }),
      { type: "stop" }, // req 2: cancel
      { type: "refresh" }, // req 3: status
      { type: "link", href: "https://example.test/x" },
      { type: "confirm_open" }, // req 4: openUrl
    ).state;
  }

  it("session_end: the running turn fails, every waiting request is forgotten, a notice says so", () => {
    const before = busy();
    expect([...before.waiting.values()].map((w) => w.kind)).toEqual([
      "cancel",
      "status",
      "openUrl",
    ]);
    const { state, out } = run(before, msg({ type: "session_end" }));
    expect(out).toEqual([]);
    expect(state.waiting.size).toBe(0);
    expect(turn(state).phase).toBe("error");
    expect(turn(state).reason).toBe("session_end");
    expect(turn(state).text).toBe("part");
    expect(state.statusError).toBe("session_end");
    expect(state.notices.map((n) => n.text)).toEqual([SESSION_END_NOTICE]);
    // Replies that arrive afterwards for the forgotten requests change nothing.
    const late = run(
      state,
      msg({ type: "ok", req: 2 }),
      msg({ type: "status_result", req: 3, status: STATUS }),
      msg({ type: "chat_error", turn_id: "t-1", reason: "cancelled" }),
    );
    expect(late.state).toEqual(state);
    expect(late.out).toEqual([]);
  });

  it("session_end fails a send that ternd never answered (no turn id yet)", () => {
    const s = run(fresh(), { type: "submit", text: "q" }).state;
    const { state } = run(s, msg({ type: "session_end" }));
    expect(turn(state).phase).toBe("error");
    expect(turn(state).reason).toBe("session_end");
    expect(state.waiting.size).toBe(0);
    // A turn reply for it after the reset is not taken up (its session is gone).
    const late = run(state, msg({ type: "turn", req: 1, turn_id: "t-late" }));
    expect(late.state).toEqual(state);
    expect(late.out).toEqual([]);
    expect(run(state, { type: "submit", text: "again" }).out).toHaveLength(1);
  });

  it("port_closed fails the same way with its own reason and notice", () => {
    const { state } = run(busy(), { type: "port_closed" });
    expect(state.waiting.size).toBe(0);
    expect(turn(state).reason).toBe("disconnected");
    expect(state.statusError).toBe("disconnected");
    expect(state.notices.map((n) => n.text)).toEqual([PORT_CLOSED_NOTICE]);
  });

  it("finished turns stay as they were", () => {
    const s = run(started(), msg({ type: "done", turn_id: "t-1", citations: [] })).state;
    const { state } = run(s, msg({ type: "session_end" }));
    expect(turn(state).phase).toBe("done");
  });
});

describe("citations shown", () => {
  const text = `One [${A}], two 【mid: ${B}】, three [${X}].`;
  const base = (): ChatState => run(started(), msg({ type: "delta", turn_id: "t-1", text })).state;

  it("while streaming every chip the text holds is shown (the daemon filtered the deltas)", () => {
    expect(chips(displayBlocks(turn(base())))).toEqual([A, B, X]);
  });

  it("done demotes a chip whose mid is not in chat.done.citations to its raw text", () => {
    const s = run(base(), msg({ type: "done", turn_id: "t-1", citations: [A, X] })).state;
    const blocks = displayBlocks(turn(s));
    expect(chips(blocks)).toEqual([A, X]);
    const texts = inlines(blocks).flatMap((n) => (n.t === "text" ? [n.text] : []));
    expect(texts.join("")).toContain(`【mid: ${B}】`);
  });

  it("done with no citations shows no chip", () => {
    const s = run(base(), msg({ type: "done", turn_id: "t-1", citations: [] })).state;
    expect(chips(displayBlocks(turn(s)))).toEqual([]);
  });

  it("a failed or cancelled turn shows no chip (no list from the daemon)", () => {
    const err = run(base(), msg({ type: "chat_error", turn_id: "t-1", reason: "timeout" })).state;
    expect(chips(displayBlocks(turn(err)))).toEqual([]);
    const cancelled = run(base(), msg({ type: "chat_error", turn_id: "t-1", reason: "cancelled" }));
    expect(chips(displayBlocks(turn(cancelled.state)))).toEqual([]);
  });

  it("chips inside emphasis and list items are demoted too", () => {
    const s = run(
      started(),
      msg({ type: "delta", turn_id: "t-1", text: `**bold [${B}]**\n\n- item [${B}]\n- ok [${A}]` }),
      msg({ type: "done", turn_id: "t-1", citations: [A] }),
    ).state;
    expect(chips(displayBlocks(turn(s)))).toEqual([A]);
  });

  it("chips inside table cells are demoted too", () => {
    const s = run(
      started(),
      msg({ type: "delta", turn_id: "t-1", text: `| a | b |\n|---|---|\n| [${B}] | [${A}] |` }),
      msg({ type: "done", turn_id: "t-1", citations: [A] }),
    ).state;
    const [table] = displayBlocks(turn(s));
    if (table?.t !== "table") throw new Error("no table");
    expect(table.rows[0]?.map((c) => c.children)).toEqual([
      [{ t: "text", text: `[${B}]` }],
      [{ t: "cite", mid: A, raw: `[${A}]` }],
    ]);
  });

  // Bridge 1.1 (plan 07 step 9b, #36): the chip asks the background to open the message; the
  // 1.0 notice is now the answer to a session at minor 0 (method_not_available).
  it("a chip click sends open {mid}; ok shows nothing more", () => {
    const { state, out } = run(fresh(), { type: "chip", mid: X });
    expect(out).toEqual([{ type: "open", req: 1, mid: X }]);
    expect(state.notices).toEqual([]);
    const opened = run(state, msg({ type: "ok", req: 1 }));
    expect(opened.state.notices).toEqual([]);
    expect(opened.state.waiting.size).toBe(0);
  });

  it("method_not_available on open shows the 1.0 notice with the mid shortened", () => {
    const { state } = run(
      fresh(),
      { type: "chip", mid: X },
      msg({ type: "error", req: 1, name: "method_not_available", message: "needs 1.1" }),
    );
    expect(state.notices).toHaveLength(1);
    expect(state.notices[0]?.text).toBe(`${CHIP_NOTICE} (message cccccccc…).`);
    expect(state.notices[0]?.text).not.toContain(X);
    expect(shortMid(A)).toBe("aaaaaaaa…");
    const dismissed = run(state, { type: "dismiss", id: state.notices[0]?.id ?? -1 });
    expect(dismissed.state.notices).toEqual([]);
  });

  it("any other open failure is an error notice naming the code, not the mid", () => {
    const { state } = run(
      fresh(),
      { type: "chip", mid: X },
      msg({ type: "error", req: 1, name: "state_changed", message: "x [mid_conflicted]" }),
    );
    expect(state.notices.map((n) => [n.level, n.text])).toEqual([
      ["error", "Could not open message cccccccc… (state_changed)."],
    ]);
  });
});

describe("links", () => {
  it("a link click asks first; Open posts openUrl with the full URL", () => {
    const href = "https://example.test/a/very/long/path?q=1#frag";
    let r = run(fresh(), { type: "link", href });
    expect(r.out).toEqual([]);
    expect(r.state.confirm).toBe(href);
    r = run(r.state, { type: "confirm_open" });
    expect(r.out).toEqual([{ type: "openUrl", req: 1, url: href }]);
    expect(r.state.confirm).toBeNull();
    // Opening twice needs a second click on the link.
    expect(run(r.state, { type: "confirm_open" }).out).toEqual([]);
  });

  it("Cancel closes the panel and sends nothing", () => {
    const r = run(
      fresh(),
      { type: "link", href: "https://example.test/" },
      {
        type: "confirm_cancel",
      },
    );
    expect(r.out).toEqual([]);
    expect(r.state.confirm).toBeNull();
  });

  it("a refused openUrl is a notice", () => {
    const r = run(
      fresh(),
      { type: "link", href: "https://example.test/" },
      { type: "confirm_open" },
      msg({ type: "error", req: 1, name: "unavailable", message: "Error" }),
    );
    expect(r.state.notices.map((n) => n.text)).toEqual(["Could not open the link (unavailable)."]);
  });
});

describe("status", () => {
  it("refresh posts index.status once while one is waiting; the result is kept", () => {
    let r = run(fresh(), { type: "refresh" }, { type: "refresh" });
    expect(r.out).toEqual([{ type: "status", req: 1 }]);
    r = run(r.state, msg({ type: "status_result", req: 1, status: STATUS }));
    expect(r.state.status).toEqual(STATUS);
    expect(r.state.statusError).toBeNull();
    expect(run(r.state, { type: "refresh" }).out).toEqual([{ type: "status", req: 2 }]);
  });

  it("a failed status request is shown by error name", () => {
    const r = run(
      fresh(),
      { type: "refresh" },
      msg({ type: "error", req: 1, name: "unavailable", message: "ternd not connected" }),
    );
    expect(r.state.statusError).toBe("unavailable");
  });

  it("a reply of the wrong kind fails its request visibly", () => {
    const r = run(fresh(), { type: "refresh" }, msg({ type: "ok", req: 1 }));
    expect(r.state.statusError).toBe("internal");
    expect(r.state.waiting.size).toBe(0);
    const s = run(fresh(), { type: "submit", text: "q" }, msg({ type: "ok", req: 1 }));
    expect(turn(s.state).phase).toBe("error");
    expect(turn(s.state).reason).toBe("internal");
  });
});

describe("new chat", () => {
  it("makes a new session, clears the turns and cancels a running turn", () => {
    const r = run(started(), { type: "new_chat", sessionId: "session-2" });
    expect(r.out).toEqual([{ type: "cancel", req: 2, turn_id: "t-1" }]);
    expect(r.state.turns).toEqual([]);
    expect(r.state.sessionId).toBe("session-2");
    const next = run(r.state, { type: "submit", text: "fresh" });
    expect(next.out).toEqual([
      { type: "send", req: 3, session_id: "session-2", text: "fresh", mode: "manual" },
    ]);
  });

  it("a turn reply for a send made before New chat is cancelled", () => {
    let r = run(
      fresh(),
      { type: "submit", text: "q" },
      {
        type: "new_chat",
        sessionId: "session-2",
      },
    );
    expect(r.out.map((o) => o.type)).toEqual(["send"]);
    r = run(r.state, msg({ type: "turn", req: 1, turn_id: "t-old" }));
    expect(r.out).toEqual([{ type: "cancel", req: 2, turn_id: "t-old" }]);
    expect(r.state.turns).toEqual([]);
  });
});

describe("parsePageMessage", () => {
  it("accepts every PageMessage shape", () => {
    const good: PageMessage[] = [
      { type: "turn", req: 1, turn_id: "t-1" },
      { type: "status_result", req: 2, status: STATUS },
      { type: "ok", req: 3 },
      { type: "error", req: 4, name: "unavailable", message: "m" },
      { type: "delta", turn_id: "t-1", text: "" },
      { type: "done", turn_id: "t-1", citations: [A] },
      { type: "chat_error", turn_id: "t-1", reason: "timeout" },
      { type: "session_end" },
    ];
    for (const m of good) expect(parsePageMessage(m)).toEqual(m);
  });

  it("refuses malformed messages", () => {
    const bad: unknown[] = [
      null,
      "turn",
      [],
      { type: "turn", req: 1 },
      { type: "turn", req: 1.5, turn_id: "t" },
      { type: "turn", req: 1, turn_id: "" },
      { type: "status_result", req: 1, status: { ...STATUS, messages: "12" } },
      { type: "status_result", req: 1, status: { ...STATUS, models: { chat: "c" } } },
      { type: "status_result", req: 1, status: { ...STATUS, accounts: [{ account_id: "a" }] } },
      { type: "ok" },
      { type: "error", req: 1, name: "not_a_code", message: "m" },
      { type: "error", req: 1, name: "unavailable" },
      { type: "delta", turn_id: "t" },
      { type: "done", turn_id: "t" },
      { type: "done", turn_id: "t", citations: [1] },
      { type: "chat_error", turn_id: "t", reason: 7 },
      { type: "what" },
    ];
    for (const m of bad) expect(parsePageMessage(m), JSON.stringify(m)).toBeNull();
  });

  it("accepts every bridge error name protocol.ts knows", () => {
    for (const name of Object.keys(ERROR_CODES)) {
      const m = { type: "error", req: 1, name, message: "m" };
      expect(parsePageMessage(m), name).toEqual(m);
    }
  });

  it("replyReq names the integer req of a message, else null", () => {
    expect(replyReq({ type: "status_result", req: 4, status: {} })).toBe(4);
    expect(replyReq({ type: "turn", req: 1.5 })).toBeNull();
    expect(replyReq({ type: "delta", turn_id: "t" })).toBeNull();
    expect(replyReq(null)).toBeNull();
  });
});

describe("daemon.info: the send gate and the remote-model banner (plan 07 step 9b, #9)", () => {
  const remote = { remote_llm: true, chat_model: "big-remote" };
  const local = { remote_llm: false, chat_model: "synthetic-chat" };

  it("a new tab's first refresh asks info and status; later refreshes only status", () => {
    const first = run(initialState(SID), { type: "refresh" });
    expect(first.out).toEqual([
      { type: "status", req: 1 },
      { type: "info", req: 2 },
    ]);
    const known = run(first.state, msg({ type: "info_result", req: 2, info: local }));
    expect(known.state.info).toEqual({
      kind: "known",
      remoteLlm: false,
      chatModel: "synthetic-chat",
    });
    const again = run(known.state, msg({ type: "status_result", req: 1, status: STATUS }), {
      type: "refresh",
    });
    expect(again.out).toEqual([{ type: "status", req: 3 }]);
  });

  it("a send while the setting is unknown is refused locally and asks info", () => {
    const { state, out } = run(initialState(SID), { type: "submit", text: "hello" });
    expect(out).toEqual([{ type: "info", req: 1 }]);
    expect(state.turns).toEqual([]);
    expect(state.notices.map((n) => n.text)).toEqual([INFO_PENDING_NOTICE]);
    // Asked once: a second send while info is in flight does not ask again.
    const twice = run(state, { type: "submit", text: "hello" });
    expect(twice.out).toEqual([]);
  });

  it("remote_llm true shows the banner, naming the model; false shows none", () => {
    const r = run(
      initialState(SID),
      { type: "refresh" },
      msg({ type: "info_result", req: 2, info: remote }),
    );
    expect(remoteBanner(r.state)).toBe(remoteBannerText("big-remote"));
    expect(remoteBannerText("big-remote")).toContain("big-remote");
    expect(remoteBannerText("big-remote")).toContain("off this computer");
    const l = run(
      initialState(SID),
      { type: "refresh" },
      msg({ type: "info_result", req: 2, info: local }),
    );
    expect(remoteBanner(l.state)).toBeNull();
    expect(remoteBanner(initialState(SID))).toBeNull();
    // With the banner up, sending works.
    expect(run(r.state, { type: "submit", text: "q" }).out).toEqual([
      { type: "send", req: 3, session_id: SID, text: "q", mode: "manual" },
    ]);
  });

  it("method_not_available (a session at minor 0) is legacy: sends go out, no banner", () => {
    const r = run(
      initialState(SID),
      { type: "submit", text: "q" },
      msg({ type: "error", req: 1, name: "method_not_available", message: "needs 1.1" }),
    );
    expect(r.state.info).toEqual({ kind: "legacy" });
    expect(remoteBanner(r.state)).toBeNull();
    expect(run(r.state, { type: "submit", text: "q" }).out).toEqual([
      { type: "send", req: 2, session_id: SID, text: "q", mode: "manual" },
    ]);
  });

  it("any other info failure leaves the setting unknown: sends stay refused", () => {
    const r = run(
      initialState(SID),
      { type: "refresh" },
      msg({ type: "error", req: 2, name: "unavailable", message: "ternd not connected" }),
    );
    expect(r.state.info).toEqual({ kind: "unknown" });
    const sent = run(r.state, { type: "submit", text: "q" });
    expect(sent.state.turns).toEqual([]);
    expect(sent.out).toEqual([{ type: "info", req: 3 }]);
  });

  it.each([["session_end"], ["port_closed"]])(
    "%s forgets the setting (ternd may restart with another config); the banner stays, re-checking",
    (how) => {
      let s = run(
        initialState(SID),
        { type: "refresh" },
        msg({ type: "info_result", req: 2, info: remote }),
      ).state;
      expect(remoteBanner(s)).not.toBeNull();
      s = run(
        s,
        how === "session_end" ? msg({ type: "session_end" }) : { type: "port_closed" },
      ).state;
      // Step 9b review: #9's banner is persistent, so it stays until ternd says "local".
      expect(s.info).toEqual({ kind: "unknown", remoteModel: "big-remote" });
      expect(remoteBanner(s)).toBe(
        `${remoteBannerText("big-remote")} (Re-checking ternd's setting.)`,
      );
      expect(run(s, { type: "submit", text: "q" }).state.turns).toEqual([]); // still gated
      const again = run(s, { type: "refresh" });
      expect(again.out.map((o) => o.type)).toEqual(["status", "info"]);
      const info = again.out.find((o) => o.type === "info");
      const local2 = run(
        again.state,
        msg({ type: "info_result", req: info?.req ?? -1, info: local }),
      );
      expect(remoteBanner(local2.state)).toBeNull();
    },
  );

  it("a local setting that becomes unknown shows no banner", () => {
    let s = run(
      initialState(SID),
      { type: "refresh" },
      msg({ type: "info_result", req: 2, info: local }),
    ).state;
    s = run(s, msg({ type: "session_end" })).state;
    expect(s.info).toEqual({ kind: "unknown" });
    expect(remoteBanner(s)).toBeNull();
  });

  it("a failed check after a refused send says so; a failed background check stays quiet", () => {
    const refused = run(
      initialState(SID),
      { type: "submit", text: "q" },
      { type: "submit", text: "q" },
    );
    expect(refused.state.notices.map((n) => n.text)).toEqual([INFO_PENDING_NOTICE]); // once
    const failed = run(
      refused.state,
      msg({ type: "error", req: 1, name: "unavailable", message: "ternd not connected" }),
    );
    expect(failed.state.notices.map((n) => [n.level, n.text]).at(-1)).toEqual([
      "error",
      "Could not check ternd's model setting (unavailable); nothing was sent.",
    ]);
    const quiet = run(
      initialState(SID),
      { type: "refresh" },
      msg({ type: "error", req: 2, name: "unavailable", message: "ternd not connected" }),
    );
    expect(quiet.state.notices).toEqual([]);
  });

  it("a double click on a chip opens once", () => {
    const r = run(
      fresh(),
      { type: "chip", mid: X },
      { type: "chip", mid: X },
      { type: "chip", mid: A },
    );
    expect(r.out).toEqual([
      { type: "open", req: 1, mid: X },
      { type: "open", req: 2, mid: A },
    ]);
    const after = run(r.state, msg({ type: "ok", req: 1 }), { type: "chip", mid: X });
    expect(after.out).toEqual([{ type: "open", req: 3, mid: X }]);
  });

  it("parsePageMessage accepts a well-formed info_result only", () => {
    expect(parsePageMessage({ type: "info_result", req: 4, info: remote })).toEqual({
      type: "info_result",
      req: 4,
      info: remote,
    });
    for (const bad of [
      { type: "info_result", req: 4 },
      { type: "info_result", req: 4, info: { remote_llm: "true", chat_model: "m" } },
      { type: "info_result", req: 4, info: { remote_llm: true } },
      { type: "info_result", req: "4", info: remote },
    ]) {
      expect(parsePageMessage(bad)).toBeNull();
    }
  });
});

describe("malformed_reply", () => {
  it("fails the request it answers as internal; unknown reqs change nothing", () => {
    let r = run({ ...initialState("s"), info: fresh().info }, { type: "submit", text: "q" });
    r = run(r.state, { type: "refresh" });
    expect(r.out).toEqual([{ type: "status", req: 2 }]);
    r = run(r.state, { type: "malformed_reply", req: 2 });
    expect(r.state.statusError).toBe("internal");
    r = run(r.state, { type: "malformed_reply", req: 1 });
    expect(r.state.turns[0]?.phase).toBe("error");
    expect(r.state.turns[0]?.reason).toBe("internal");
    expect(r.state.waiting.size).toBe(0);
    const again = run(r.state, { type: "malformed_reply", req: 1 });
    expect(again.state).toBe(r.state);
    expect(run(r.state, { type: "refresh" }).out).toEqual([{ type: "status", req: 3 }]);
  });
});
