// The chat tab's saved chats (ui/chat/history.ts), the page's store in tb/api.ts, the artifact
// texts (ui/chat/artifacts.ts) and the download helper (ui/chat/main.ts saveTextFile). Synthetic
// ids and text only.

import { afterEach, describe, expect, it, vi } from "vitest";
import { chatPageStore } from "../src/tb/api";
import { artifactOf, extFor, LONG_LIST, lineCount, plainInline } from "../src/ui/chat/artifacts";
import {
  diff,
  ENTRY_PREFIX,
  type Entry,
  entryKey,
  entryOf,
  parseEntry,
  readEntries,
} from "../src/ui/chat/history";
import { REVOKE_MS, saveTextFile } from "../src/ui/chat/main";
import { type Chat, MAX_CHATS, type Turn } from "../src/ui/chat/state";
import { type Block, sanitize } from "../src/ui/sanitize";

const A = "a".repeat(64);
const X = `x-${"b".repeat(64)}`;

const turn = (patch: Partial<Turn> = {}): Turn => ({
  key: 7,
  user: "what is new?",
  turnId: "t-9",
  text: `See [${A}].`,
  phase: "done",
  reason: null,
  detail: null,
  citations: [A],
  stopRequested: false,
  ...patch,
});

const chat = (id: string, seq: number, turns: Turn[] = [turn()]): Chat => ({
  id,
  seq,
  turns,
  forgotten: 0,
});

describe("entries", () => {
  it("a chat is saved without keys or turn ids; a running turn as interrupted", () => {
    const e = entryOf(
      chat("s-1", 4, [turn(), turn({ phase: "streaming", text: "half", citations: null })]),
    );
    expect(e).toEqual({
      v: 1,
      id: "s-1",
      seq: 4,
      turns: [
        {
          user: "what is new?",
          text: `See [${A}].`,
          phase: "done",
          reason: null,
          detail: null,
          citations: [A],
        },
        {
          user: "what is new?",
          text: "half",
          phase: "error",
          reason: "interrupted",
          detail: null,
          citations: null,
        },
      ],
    });
    expect(JSON.stringify(e)).not.toContain("t-9");
    expect(entryOf(chat("s-2", 1, []))).toBeNull();
  });

  it("an entry reads back as the chat it was saved from", () => {
    const e = entryOf(chat("s-1", 4)) as Entry;
    const back = parseEntry(entryKey("s-1"), JSON.parse(JSON.stringify(e)));
    expect(back).toEqual({ id: "s-1", seq: 4, turns: e.turns });
  });

  it("anything off makes an entry unreadable", () => {
    const good = JSON.parse(JSON.stringify(entryOf(chat("s-1", 4)))) as Record<string, unknown>;
    const t0 = (good.turns as Record<string, unknown>[])[0] as Record<string, unknown>;
    const bad: [string, unknown][] = [
      ["wrong key", null],
      ["v2", { ...good, v: 2 }],
      ["id with a slash", { ...good, id: "a/b" }],
      ["negative seq", { ...good, seq: -1 }],
      ["no turns", { ...good, turns: [] }],
      ["bad phase", { ...good, turns: [{ ...t0, phase: "streaming" }] }],
      ["bad mid", { ...good, turns: [{ ...t0, citations: ["not-a-mid"] }] }],
      ["empty user", { ...good, turns: [{ ...t0, user: "" }] }],
      ["long user", { ...good, turns: [{ ...t0, user: "x".repeat(8001) }] }],
      ["text not a string", { ...good, turns: [{ ...t0, text: 5 }] }],
      ["not an object", "x"],
    ];
    for (const [why, value] of bad) {
      const key = why === "wrong key" ? entryKey("other") : entryKey("s-1");
      expect(parseEntry(key, why === "wrong key" ? good : value), why).toBeNull();
    }
    expect(parseEntry(entryKey("s-1"), { ...good, turns: [{ ...t0, citations: [X] }] })).not.toBe(
      null,
    );
  });

  it("readEntries: only the tab's keys, newest first, unreadable counted, beyond the bound excess", () => {
    const all: Record<string, unknown> = { profile_id: "0000", other: { v: 1 } };
    for (let i = 0; i < MAX_CHATS + 2; i += 1) {
      all[entryKey(`c-${i}`)] = entryOf(chat(`c-${i}`, i + 1));
    }
    all[`${ENTRY_PREFIX}broken`] = { v: 1, id: "broken" };
    const r = readEntries(all);
    expect(r.unreadable).toBe(1);
    expect(r.chats).toHaveLength(MAX_CHATS);
    expect(r.chats[0]?.id).toBe(`c-${MAX_CHATS + 1}`);
    expect(r.excess).toEqual([entryKey("c-1"), entryKey("c-0")]);
  });

  it("diff writes only changed chats and removes the ones gone", () => {
    const one = chat("s-1", 1);
    const two = chat("s-2", 2);
    const first = diff(new Map(), [one, two, chat("empty", 3, [])]);
    expect(Object.keys(first.set)).toEqual([entryKey("s-1"), entryKey("s-2")]);
    expect(first.remove).toEqual([]);
    const changed = { ...two, turns: [turn({ text: "edited" })] };
    const second = diff(first.next, [one, changed]);
    expect(Object.keys(second.set)).toEqual([entryKey("s-2")]);
    const third = diff(second.next, [changed]);
    expect(third.set).toEqual({});
    expect(third.remove).toEqual([entryKey("s-1")]);
  });
});

describe("chatPageStore (tb/api.ts)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("is the extension's storage.local: get(null), set, remove", async () => {
    const calls: unknown[] = [];
    vi.stubGlobal("messenger", {
      runtime: { connect: () => null },
      storage: {
        local: {
          get: (k: unknown) => {
            calls.push(["get", k]);
            return Promise.resolve({ a: 1 });
          },
          set: (items: unknown) => {
            calls.push(["set", items]);
            return Promise.resolve();
          },
          remove: (keys: unknown) => {
            calls.push(["remove", keys]);
            return Promise.resolve();
          },
        },
      },
    });
    const store = chatPageStore();
    expect(await store.getAll()).toEqual({ a: 1 });
    await store.set({ k: 2 });
    await store.remove(["k"]);
    expect(calls).toEqual([
      ["get", null],
      ["set", { k: 2 }],
      ["remove", ["k"]],
    ]);
  });

  it("throws outside an extension page", () => {
    expect(() => chatPageStore()).toThrow("no messenger global");
  });
});

describe("artifacts", () => {
  const block = (md: string): Block => {
    const [b] = sanitize(md);
    if (b === undefined) throw new Error("no block");
    return b;
  };

  it("a code block keeps its text; the file extension follows the fence language", () => {
    expect(artifactOf(block("```python\nprint(1)\n```"))).toEqual({
      title: "python",
      text: "print(1)",
      filename: "tern-snippet.py",
    });
    expect(artifactOf(block("```\nplain\n```"))?.filename).toBe("tern-snippet.txt");
    expect(extFor("ts")).toBe("ts");
    expect(extFor("c++")).toBe("cpp");
    expect(extFor("unheard-of")).toBe("txt");
    expect(extFor(undefined)).toBe("txt");
    // HTML is saved as a file, never rendered.
    expect(artifactOf(block("```html\n<b>x</b>\n```"))).toEqual({
      title: "html",
      text: "<b>x</b>",
      filename: "tern-snippet.html",
    });
  });

  it("a table becomes a pipe table: labels not URLs, removed images labelled, pipes escaped", () => {
    const a = artifactOf(
      block(
        `| Who | Link |\n|:--|--:|\n| a\\|b | [site](https://example.test/x) |\n` +
          `| ![i](https://evil.test/p) | [${A}] |`,
      ),
    );
    expect(a?.filename).toBe("tern-table.md");
    expect(a?.text).toBe(
      `| Who | Link |\n| :--- | ---: |\n| a\\|b | site |\n| [image removed] | [${A}] |`,
    );
    expect(a?.text).not.toContain("evil.test");
    expect(a?.text).not.toContain("example.test");
  });

  it(`a list gets an artifact from ${LONG_LIST} items on`, () => {
    const short = Array.from({ length: LONG_LIST - 1 }, (_, i) => `- item ${i}`).join("\n");
    expect(artifactOf(block(short))).toBeNull();
    const long = Array.from({ length: LONG_LIST }, (_, i) => `${i + 3}. **item** ${i}`).join("\n");
    const a = artifactOf(block(long));
    expect(a?.title).toBe("list");
    expect(a?.text.split("\n")[0]).toBe("3. item 0");
    expect(lineCount(a?.text ?? "")).toBe(LONG_LIST);
    expect(artifactOf(block("just a paragraph"))).toBeNull();
  });

  it("plainInline keeps link labels and citation text", () => {
    const [p] = sanitize(`a [b](https://example.test/) **c** \`d\` [${A}]`);
    if (p?.t !== "p") throw new Error("not a paragraph");
    expect(plainInline(p.children)).toBe(`a b c d [${A}]`);
  });
});

describe("saveTextFile", () => {
  it("offers the text as a plain-text Blob behind a temporary link, then revokes the URL", async () => {
    const blobs: Blob[] = [];
    const events: string[] = [];
    const timers: { cb: () => void; ms: number }[] = [];
    const link = {
      href: "",
      download: "",
      click: () => events.push(`click ${link.href} ${link.download}`),
      remove: () => events.push("removed"),
    };
    const doc = {
      createElement: (tag: string) => {
        events.push(`create ${tag}`);
        return link;
      },
      body: { appendChild: () => events.push("appended") },
    };
    saveTextFile(
      {
        doc: doc as unknown as Pick<Document, "createElement" | "body">,
        createObjectURL: (b) => {
          blobs.push(b);
          return "blob:tern/1";
        },
        revokeObjectURL: (u) => events.push(`revoked ${u}`),
        later: (cb, ms) => void timers.push({ cb, ms }),
      },
      "tern-snippet.py",
      "print('hi')\n<script>x</script>",
    );
    expect(events).toEqual([
      "create a",
      "appended",
      "click blob:tern/1 tern-snippet.py",
      "removed",
    ]);
    expect(blobs).toHaveLength(1);
    expect(blobs[0]?.type).toBe("text/plain;charset=utf-8");
    expect(await blobs[0]?.text()).toBe("print('hi')\n<script>x</script>");
    expect(timers.map((t) => t.ms)).toEqual([REVOKE_MS]);
    timers[0]?.cb();
    expect(events.at(-1)).toBe("revoked blob:tern/1");
  });

  it("revokes the URL even when the click throws", () => {
    const revoked: string[] = [];
    const timers: (() => void)[] = [];
    const doc = {
      createElement: () => {
        throw new TypeError("no a");
      },
      body: {},
    };
    expect(() =>
      saveTextFile(
        {
          doc: doc as unknown as Pick<Document, "createElement" | "body">,
          createObjectURL: () => "blob:tern/2",
          revokeObjectURL: (u) => void revoked.push(u),
          later: (cb) => void timers.push(cb),
        },
        "f.txt",
        "x",
      ),
    ).toThrow("no a");
    for (const t of timers) t();
    expect(revoked).toEqual(["blob:tern/2"]);
  });
});
