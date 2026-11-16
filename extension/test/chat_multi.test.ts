// The chat tab's chats (ui/chat/state.ts): several chats per tab, each its own ternd session;
// switching, deleting, restoring saved chats, the MAX_CHATS bound and the artifact panel state.
// Pure reducer: actions in, state and requests out. Synthetic ids and text only.

import { describe, expect, it } from "vitest";
import type { PageMessage, PageRequest } from "../src/protocol";
import {
  type Action,
  type ChatState,
  chatList,
  chatTitle,
  initialState,
  MAX_CHATS,
  type SavedChat,
  TITLE_MAX,
  type Turn,
  update,
} from "../src/ui/chat/state";

const A = "a".repeat(64);

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

const fresh = (id = "s-1"): ChatState => ({
  ...initialState(id),
  info: { kind: "known", remoteLlm: false, chatModel: "synthetic-chat" },
});

/** A chat with one answered turn: sent as `req`, turn `turnId`, answer `answer`. */
function answered(s: ChatState, q: string, req: number, turnId: string, answer = "ok"): ChatState {
  return run(
    s,
    { type: "submit", text: q },
    msg({ type: "turn", req, turn_id: turnId }),
    msg({ type: "delta", turn_id: turnId, text: answer }),
    msg({ type: "done", turn_id: turnId, citations: [] }),
  ).state;
}

const saved = (id: string, seq: number, user = `question ${id}`): SavedChat => ({
  id,
  seq,
  turns: [
    {
      user,
      text: `answer [${A}]`,
      phase: "done",
      reason: null,
      detail: null,
      citations: [A],
    },
  ],
});

describe("several chats in one tab", () => {
  it("New chat keeps the chat with its turns in the list, newest first, titled by its question", () => {
    let s = answered(fresh(), "first question", 1, "t-1");
    s = run(s, { type: "new_chat", sessionId: "s-2" }).state;
    expect(s.sessionId).toBe("s-2");
    expect(s.turns).toEqual([]);
    expect(chatList(s).map((c) => [c.id, c.title, c.shown])).toEqual([
      ["s-2", "New chat", true],
      ["s-1", "first question", false],
    ]);
    s = answered(s, "second question", 3, "t-2");
    expect(chatList(s).map((c) => c.title)).toEqual(["second question", "first question"]);
  });

  it("an empty chat is not kept when the user leaves it", () => {
    let s = run(fresh(), { type: "new_chat", sessionId: "s-2" }).state;
    s = run(s, { type: "new_chat", sessionId: "s-3" }).state;
    expect(chatList(s).map((c) => c.id)).toEqual(["s-3"]);
  });

  it("switching shows the other chat's turns; the next send uses its session id", () => {
    let s = answered(fresh(), "in chat one", 1, "t-1", "answer one");
    s = run(s, { type: "new_chat", sessionId: "s-2" }).state;
    s = answered(s, "in chat two", 3, "t-2", "answer two");
    const r = run(s, { type: "switch_chat", id: "s-1" }, { type: "submit", text: "follow-up" });
    expect(r.state.sessionId).toBe("s-1");
    expect(r.state.turns.map((t) => t.user)).toEqual(["in chat one", "follow-up"]);
    expect(r.state.turns[0]?.text).toBe("answer one");
    expect(r.out).toEqual([
      { type: "send", req: 4, session_id: "s-1", text: "follow-up", mode: "manual" },
    ]);
    // Activity moves the chat to the top.
    expect(chatList(r.state).map((c) => c.id)).toEqual(["s-1", "s-2"]);
  });

  it("leaving a running chat cancels its turn and keeps it as stopped; a late delta is dropped", () => {
    let r = run(
      fresh(),
      { type: "submit", text: "long one" },
      msg({ type: "turn", req: 1, turn_id: "t-1" }),
      msg({ type: "delta", turn_id: "t-1", text: "part" }),
    );
    r = run(r.state, { type: "new_chat", sessionId: "s-2" });
    expect(r.out).toEqual([{ type: "cancel", req: 2, turn_id: "t-1" }]);
    r = run(r.state, msg({ type: "delta", turn_id: "t-1", text: " more" }));
    r = run(r.state, { type: "switch_chat", id: "s-1" });
    const t = r.state.turns[0] as Turn;
    expect([t.phase, t.text]).toEqual(["cancelled", "part"]);
  });

  it("a chat left before ternd named its turn gets that turn cancelled when the reply comes", () => {
    let r = run(fresh(), { type: "submit", text: "q" }, { type: "new_chat", sessionId: "s-2" });
    expect(r.out.map((o) => o.type)).toEqual(["send"]);
    r = run(r.state, msg({ type: "turn", req: 1, turn_id: "t-late" }));
    expect(r.out).toEqual([{ type: "cancel", req: 2, turn_id: "t-late" }]);
  });

  it("switching to the shown chat only closes the narrow-page list; an unknown id does nothing", () => {
    const s = run(fresh(), { type: "toggle_sidebar" }).state;
    expect(s.sidebarToggled).toBe(true);
    expect(run(s, { type: "switch_chat", id: "s-1" }).state.sidebarToggled).toBe(false);
    expect(run(s, { type: "switch_chat", id: "nope" }).state).toBe(s);
  });

  it("delete: the first click asks, the second deletes; deleting the shown chat starts a new one", () => {
    let s = answered(fresh(), "keep?", 1, "t-1");
    s = run(s, { type: "new_chat", sessionId: "s-2" }).state;
    s = answered(s, "other", 3, "t-2");
    s = run(s, { type: "ask_delete", id: "s-1" }).state;
    expect(s.deleting).toBe("s-1");
    s = run(s, { type: "delete_chat", id: "s-1", sessionId: "unused" }).state;
    expect(chatList(s).map((c) => c.id)).toEqual(["s-2"]);
    expect(s.deleting).toBeNull();
    const r = run(s, { type: "delete_chat", id: "s-2", sessionId: "s-3" });
    expect(r.state.sessionId).toBe("s-3");
    expect(r.state.turns).toEqual([]);
    expect(chatList(r.state).map((c) => c.id)).toEqual(["s-3"]);
  });

  it("deleting the shown chat while it answers cancels the turn", () => {
    const r = run(
      fresh(),
      { type: "submit", text: "q" },
      msg({ type: "turn", req: 1, turn_id: "t-1" }),
      { type: "delete_chat", id: "s-1", sessionId: "s-2" },
    );
    expect(r.out.at(-1)).toEqual({ type: "cancel", req: 2, turn_id: "t-1" });
    expect(r.state.others).toEqual([]);
  });

  it("session_end marks every chat's earlier turns as possibly forgotten", () => {
    let s = answered(fresh(), "one", 1, "t-1");
    s = run(s, { type: "new_chat", sessionId: "s-2" }).state;
    s = answered(s, "two", 3, "t-2");
    s = run(s, msg({ type: "session_end" })).state;
    expect(s.forgotten).toBe(1);
    expect(s.others.map((c) => c.forgotten)).toEqual([1]);
  });

  it("chatTitle: the first message on one line, cut with an ellipsis", () => {
    const t = (user: string): Turn[] => [
      {
        key: 1,
        user,
        turnId: null,
        text: "",
        phase: "done",
        reason: null,
        detail: null,
        citations: [],
        stopRequested: false,
      },
    ];
    expect(chatTitle([])).toBe("New chat");
    expect(chatTitle(t("  two\n lines  "))).toBe("two lines");
    const long = chatTitle(t("x".repeat(100)));
    expect(long).toHaveLength(TITLE_MAX);
    expect(long.endsWith("…")).toBe(true);
  });
});

describe("restore (saved chats)", () => {
  it("saved chats join the list as forgotten, newest first, below the shown chat", () => {
    const r = run(fresh(), {
      type: "restore",
      chats: [saved("old", 3), saved("older", 1)],
      unreadable: 0,
    });
    expect(chatList(r.state).map((c) => [c.id, c.title])).toEqual([
      ["s-1", "New chat"],
      ["old", "question old"],
      ["older", "question older"],
    ]);
    const s = run(r.state, { type: "switch_chat", id: "old" }).state;
    expect(s.forgotten).toBe(1);
    const t = s.turns[0] as Turn;
    expect([t.phase, t.citations, t.turnId]).toEqual(["done", [A], null]);
    expect(r.out).toEqual([]);
    // The shown chat, once it has turns, is newer than every saved one.
    const s2 = answered(r.state, "now", 1, "t-1");
    expect(chatList(s2).map((c) => c.id)).toEqual(["s-1", "old", "older"]);
  });

  it("restored turns get keys of their own; a chat the tab already has is not replaced", () => {
    let s = answered(fresh(), "mine", 1, "t-1");
    s = run(s, {
      type: "restore",
      chats: [saved("s-1", 9, "stale"), saved("x", 2)],
      unreadable: 0,
    }).state;
    expect(s.turns.map((t) => t.user)).toEqual(["mine"]);
    const x = s.others.find((c) => c.id === "x");
    expect(x?.turns[0]?.key).toBeGreaterThan(s.turns[0]?.key ?? 0);
  });

  it("unreadable entries are counted in a notice", () => {
    const one = run(fresh(), { type: "restore", chats: [], unreadable: 1 }).state;
    expect(one.notices.map((n) => n.text)).toEqual([
      "1 saved chat could not be read and was left out.",
    ]);
    const two = run(fresh(), { type: "restore", chats: [], unreadable: 2 }).state;
    expect(two.notices[0]?.text).toBe("2 saved chats could not be read and were left out.");
  });

  it(`at most ${MAX_CHATS} chats are kept: the oldest go first`, () => {
    const many = Array.from({ length: MAX_CHATS + 5 }, (_, i) => saved(`c-${i}`, i + 1));
    let s = run(fresh(), { type: "restore", chats: many, unreadable: 0 }).state;
    s = answered(s, "shown", 1, "t-1");
    const ids = chatList(s).map((c) => c.id);
    expect(ids).toHaveLength(MAX_CHATS);
    expect(ids[0]).toBe("s-1");
    expect(ids).not.toContain("c-0");
    expect(ids).toContain(`c-${MAX_CHATS + 4}`);
    s = run(s, { type: "new_chat", sessionId: "s-2" }).state;
    expect(chatList(s)).toHaveLength(MAX_CHATS);
    expect(chatList(s).map((c) => c.id)).toContain("s-1");
  });
});

describe("artifact panel and copy state", () => {
  it("open, close, copied; switching chats closes the panel", () => {
    const artifact = { title: "py", text: "print(1)", filename: "tern-snippet.py" };
    let s = run(fresh(), { type: "open_artifact", artifact }, { type: "copied", key: "1:0" }).state;
    expect(s.artifact).toEqual(artifact);
    expect(s.copied).toBe("1:0");
    // copy and download are effects of main.ts: the reducer leaves the state as it is.
    expect(run(s, { type: "copy", key: "k", text: "t" }).state).toBe(s);
    expect(run(s, { type: "download", artifact }).state).toBe(s);
    expect(run(s, { type: "close_artifact" }).state.artifact).toBeNull();
    s = run(s, { type: "new_chat", sessionId: "s-2" }).state;
    expect([s.artifact, s.copied]).toEqual([null, null]);
  });
});
