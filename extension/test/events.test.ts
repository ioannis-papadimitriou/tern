import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEBOUNCE_MS,
  EventForwarder,
  MAX_BUFFERED_ITEMS,
  MAX_WAIT_MS,
  type Timers,
} from "../src/background/events";
import { type TbFolder, type TbMessageHeader, tbApiFrom, tbEventsFrom } from "../src/tb/api";
import { paramsErrors } from "./contract";
import { type FakeMessenger, folder, syntheticMessenger } from "./fake_messenger";

const INBOX = "account1:/INBOX";
const ARCHIVE = "account1:/Archive";

class FakeTimers implements Timers {
  t = 0;
  private seq = 0;
  private due = new Map<number, { at: number; cb: () => void }>();
  set(cb: () => void, ms: number): unknown {
    this.seq += 1;
    this.due.set(this.seq, { at: this.t + ms, cb });
    return this.seq;
  }
  clear(h: unknown): void {
    this.due.delete(h as number);
  }
  now(): number {
    return this.t;
  }
  advance(ms: number): void {
    const end = this.t + ms;
    for (;;) {
      const next = [...this.due.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (next === undefined || next[1].at > end) break;
      this.t = next[1].at;
      this.due.delete(next[0]);
      next[1].cb();
    }
    this.t = end;
  }
}

const tick = (): Promise<void> => new Promise((r) => setTimeout(r, 0));

function setup(fm: FakeMessenger = syntheticMessenger()): {
  fm: FakeMessenger;
  fwd: EventForwarder;
  timers: FakeTimers;
  sent: [string, Record<string, unknown>][];
  logs: string[];
  connected: { ready: boolean };
} {
  const sent: [string, Record<string, unknown>][] = [];
  const logs: string[] = [];
  const connected = { ready: true };
  const timers = new FakeTimers();
  const fwd = new EventForwarder(
    tbApiFrom(fm),
    (method, params) => {
      if (!connected.ready) return false;
      expect(paramsErrors(method, params)).toEqual([]); // conforms to frozen 1.0
      sent.push([method, params]);
      return true;
    },
    (m) => logs.push(m),
    timers,
  );
  fwd.register(tbEventsFrom(fm));
  return { fm, fwd, timers, sent, logs, connected };
}

function hdr(fm: FakeMessenger, id: number): TbMessageHeader {
  return fm.headerOf(id);
}

function inFolder(h: TbMessageHeader, f: TbFolder, newId?: number): TbMessageHeader {
  return { ...h, id: newId ?? h.id, folder: f };
}

/** The params of the i-th sent notification (fails the test if there is none). */
function nth<T>(sent: [string, Record<string, unknown>][], i: number): T {
  const s = sent[i];
  if (s === undefined) throw new Error(`no notification #${i}`);
  return s[1] as T;
}

const inbox = folder("account1", "INBOX", { specialUse: ["inbox"] });

describe("listener registration", () => {
  it("registers every mail and folder listener; new mail for all folders", () => {
    const { fm } = setup();
    for (const ev of [
      fm.messages.onNewMailReceived,
      fm.messages.onMoved,
      fm.messages.onCopied,
      fm.messages.onDeleted,
      fm.messages.onUpdated,
      fm.folders.onCreated,
      fm.folders.onRenamed,
      fm.folders.onMoved,
      fm.folders.onCopied,
      fm.folders.onDeleted,
      fm.folders.onUpdated,
      fm.folders.onFolderInfoChanged,
    ]) {
      expect(ev.listeners).toHaveLength(1);
    }
    expect(fm.messages.onNewMailReceived.listeners[0]?.extra).toEqual([true]);
  });
});

describe("main.ts at module load (S1 F14)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("registers the mail listeners and runtime.onStartup when the module is evaluated", async () => {
    const fm = syntheticMessenger();
    const runtimeListeners: string[] = [];
    const port = {
      postMessage: () => {},
      disconnect: () => {},
      onMessage: { addListener: () => {} },
      onDisconnect: { addListener: () => {} },
    };
    vi.stubGlobal(
      "messenger",
      Object.assign(fm, {
        runtime: {
          getManifest: () => ({ version: "0.0.0-test" }),
          connectNative: () => port,
          onStartup: { addListener: () => runtimeListeners.push("startup") },
          onInstalled: { addListener: () => runtimeListeners.push("installed") },
        },
        storage: { local: { get: async () => ({}), set: async () => {} } },
        alarms: {
          create: async () => {},
          clear: async () => true,
          get: async () => undefined,
          onAlarm: { addListener: () => runtimeListeners.push("alarm") },
        },
      }),
    );
    vi.spyOn(console, "info").mockImplementation(() => {});
    await import("../src/background/main");
    expect(fm.messages.onNewMailReceived.listeners).toHaveLength(1);
    expect(fm.folders.onFolderInfoChanged.listeners).toHaveLength(1);
    expect(runtimeListeners.sort()).toEqual(["alarm", "installed", "startup"]);
  });
});

describe("mail events", () => {
  it("new mail → one event.mail.new after the debounce, paged lists drained", async () => {
    const { fm, timers, sent } = setup();
    fm.pageSize = 2;
    fm.messages.onNewMailReceived.emit(inbox, fm.listOf([1, 2, 3, 4, 5].map((i) => hdr(fm, i))));
    await tick();
    timers.advance(DEBOUNCE_MS - 1);
    expect(sent).toEqual([]);
    timers.advance(1);
    expect(sent).toHaveLength(1);
    expect(sent[0]?.[0]).toBe("event.mail.new");
    const params = nth<{ folder_id: string; items: { tb_id: number }[] }>(sent, 0);
    expect(params.folder_id).toBe(INBOX);
    expect(params.items.map((i) => i.tb_id)).toEqual([1, 2, 3, 4, 5]);
  });

  it("a move across 3 source folders → 3 event.mail.moved, one per pair", async () => {
    const { fm, timers, sent } = setup();
    const a = folder("account1", "A");
    const b = folder("account1", "B");
    const c = folder("account1", "C");
    const dst = folder("account1", "Archive", { specialUse: ["archives"] });
    const orig = [inFolder(hdr(fm, 1), a), inFolder(hdr(fm, 2), b), inFolder(hdr(fm, 3), c)];
    const moved = orig.map((h, i) => inFolder(h, dst, 500 + i));
    fm.messages.onMoved.emit(fm.listOf(orig), fm.listOf(moved));
    await tick();
    timers.advance(DEBOUNCE_MS);
    expect(sent.map(([m, p]) => [m, p.src_folder_id, p.dst_folder_id])).toEqual([
      ["event.mail.moved", a.id, ARCHIVE],
      ["event.mail.moved", b.id, ARCHIVE],
      ["event.mail.moved", c.id, ARCHIVE],
    ]);
    expect(nth<{ items: { tb_id: number }[] }>(sent, 1).items[0]?.tb_id).toBe(501);
  });

  it("one move with interleaved sources (A,B,A,B → X) → one notification per pair", async () => {
    const { fm, timers, sent } = setup();
    const a = folder("account1", "A");
    const b = folder("account1", "B");
    const dst = folder("account1", "Archive", { specialUse: ["archives"] });
    const orig = [1, 2, 3, 4].map((n) => inFolder(hdr(fm, n), n % 2 === 1 ? a : b));
    const moved = orig.map((h, i) => inFolder(h, dst, 600 + i));
    fm.messages.onMoved.emit(fm.listOf(orig), fm.listOf(moved));
    await tick();
    timers.advance(DEBOUNCE_MS);
    expect(
      sent.map(([m, p]) => [
        m,
        p.src_folder_id,
        p.dst_folder_id,
        (p.items as { tb_id: number }[]).map((i) => i.tb_id),
      ]),
    ).toEqual([
      ["event.mail.moved", a.id, ARCHIVE, [600, 602]],
      ["event.mail.moved", b.id, ARCHIVE, [601, 603]],
    ]);
  });

  it("a copy → event.mail.new in the destination", async () => {
    const { fm, timers, sent } = setup();
    const dst = folder("account1", "Archive");
    fm.messages.onCopied.emit(fm.listOf([hdr(fm, 1)]), fm.listOf([inFolder(hdr(fm, 1), dst, 900)]));
    await tick();
    timers.advance(DEBOUNCE_MS);
    expect(sent).toEqual([
      ["event.mail.new", { folder_id: ARCHIVE, items: [expect.objectContaining({ tb_id: 900 })] }],
    ]);
  });

  it("a permanent delete → event.mail.deleted with Message-IDs", async () => {
    const { fm, timers, sent } = setup();
    fm.messages.onDeleted.emit(fm.listOf([hdr(fm, 1), hdr(fm, 2), hdr(fm, 1)]));
    await tick();
    timers.advance(DEBOUNCE_MS);
    expect(sent).toEqual([
      [
        "event.mail.deleted",
        { folder_id: INBOX, header_message_ids: ["m1@example.com", "m2@example.com"] },
      ],
    ]);
  });

  it("10 updates within 200 ms → 1 event.mail.updated, merged by tb_id (latest wins)", async () => {
    const { fm, timers, sent } = setup();
    for (let i = 0; i < 10; i++) {
      const h = { ...hdr(fm, (i % 4) + 1), flagged: i >= 6 };
      fm.messages.onUpdated.emit(h, {}, {});
      timers.advance(20);
    }
    expect(sent).toEqual([]);
    timers.advance(DEBOUNCE_MS);
    expect(sent).toHaveLength(1);
    const items = nth<{ items: { tb_id: number; flags: { flagged: boolean } }[] }>(sent, 0).items;
    expect(items.map((i) => i.tb_id)).toEqual([1, 2, 3, 4]);
    expect(items.map((i) => i.flags.flagged)).toEqual([true, true, true, true]);
  });

  it(`a steady stream still flushes after ${MAX_WAIT_MS} ms`, () => {
    const { fm, timers, sent } = setup();
    for (let t = 0; t < MAX_WAIT_MS; t += DEBOUNCE_MS - 100) {
      fm.messages.onUpdated.emit(hdr(fm, 1), {}, {});
      timers.advance(DEBOUNCE_MS - 100);
    }
    expect(sent).toHaveLength(1);
  });
});

describe("folder events", () => {
  it("rename → event.folder.changed for the old and the new id", () => {
    const { fm, timers, sent } = setup();
    fm.folders.onRenamed.emit(folder("account1", "Old"), folder("account1", "New"));
    timers.advance(DEBOUNCE_MS);
    expect(sent).toEqual([
      ["event.folder.changed", { folder_id: "account1:/Old" }],
      ["event.folder.changed", { folder_id: "account1:/New" }],
    ]);
  });

  it("bursts of folder-info changes collapse; virtual and tag folders are skipped", () => {
    const { fm, timers, sent } = setup();
    for (let i = 0; i < 5; i++) {
      fm.folders.onFolderInfoChanged.emit(inbox, { totalMessageCount: i, unreadMessageCount: 0 });
    }
    fm.folders.onCreated.emit(folder("account1", "Unread", { isVirtual: true }));
    fm.folders.onCreated.emit(folder("account1", "Tags/X", { isTag: true }));
    timers.advance(DEBOUNCE_MS);
    expect(sent).toEqual([["event.folder.changed", { folder_id: INBOX }]]);
  });
});

describe("before the bridge is ready", () => {
  it("events are buffered and flushed, in first-seen order, once ready", async () => {
    const { fm, fwd, timers, sent, connected } = setup();
    connected.ready = false;
    fm.messages.onNewMailReceived.emit(inbox, fm.listOf([hdr(fm, 1)]));
    await tick();
    fm.messages.onDeleted.emit(fm.listOf([hdr(fm, 2)]));
    await tick();
    timers.advance(MAX_WAIT_MS);
    expect(sent).toEqual([]);
    expect(fwd.bufferedItems).toBe(2);
    connected.ready = true;
    fwd.flush(); // Bridge.onReady
    expect(sent.map(([m]) => m)).toEqual(["event.mail.new", "event.mail.deleted"]);
    expect(fwd.bufferedItems).toBe(0);
  });

  it(`beyond ${MAX_BUFFERED_ITEMS} items the oldest are dropped, and only the count is logged`, async () => {
    const fm = syntheticMessenger(1200);
    const { fwd, sent, connected, logs } = setup(fm);
    connected.ready = false;
    fm.pageSize = 1200;
    const all = fm.msgs.filter((m) => m.folderId === INBOX).map((m) => hdr(fm, m.id));
    fm.messages.onNewMailReceived.emit(inbox, fm.listOf(all));
    await tick();
    expect(fwd.bufferedItems).toBe(MAX_BUFFERED_ITEMS);
    connected.ready = true;
    fwd.flush();
    const items = nth<{ items: { tb_id: number }[] }>(sent, 0).items;
    expect(items).toHaveLength(MAX_BUFFERED_ITEMS);
    expect(items[0]?.tb_id).toBe(201); // the oldest 200 went
    expect(logs).toEqual(["200 event items dropped (reconcile repairs them)"]);
    expect(logs.join("\n")).not.toMatch(/@|synthetic|INBOX|account1/);
  });
});

describe("the buffer bound while the bridge is ready (review item 1)", () => {
  const delivered = (sent: [string, Record<string, unknown>][]): number[] =>
    sent.flatMap(([, p]) => (p as { items: { tb_id: number }[] }).items.map((i) => i.tb_id));

  it(`one move of ${MAX_BUFFERED_ITEMS + 200} messages is delivered whole, nothing dropped`, async () => {
    const fm = syntheticMessenger(1200);
    const { fwd, timers, sent, logs } = setup(fm);
    const dst = folder("account1", "Archive");
    const orig = fm.msgs.filter((m) => m.folderId === INBOX).map((m) => hdr(fm, m.id));
    expect(orig).toHaveLength(1200);
    fm.messages.onMoved.emit(
      fm.listOf(orig),
      fm.listOf(orig.map((h, i) => inFolder(h, dst, 10_000 + i))),
    );
    await tick();
    timers.advance(DEBOUNCE_MS);
    expect(sent.every(([m]) => m === "event.mail.moved")).toBe(true);
    expect(delivered(sent)).toEqual(orig.map((_, i) => 10_000 + i));
    expect(fwd.bufferedItems).toBe(0);
    expect(logs).toEqual([]);
  });

  it("several moves summing over the bound are all delivered, nothing dropped", async () => {
    const fm = syntheticMessenger(1200);
    const { fwd, timers, sent, logs } = setup(fm);
    const dst = folder("account1", "Archive");
    const orig = fm.msgs.filter((m) => m.folderId === INBOX).map((m) => hdr(fm, m.id));
    for (let k = 0; k < 4; k++) {
      const part = orig.slice(k * 300, (k + 1) * 300);
      fm.messages.onMoved.emit(
        fm.listOf(part),
        fm.listOf(part.map((h, i) => inFolder(h, dst, 10_000 + k * 300 + i))),
      );
      await tick();
      timers.advance(100);
    }
    timers.advance(DEBOUNCE_MS);
    expect(delivered(sent)).toEqual(orig.map((_, i) => 10_000 + i));
    expect(fwd.bufferedItems).toBe(0);
    expect(logs).toEqual([]);
  });
});

describe("notification size and resend safety (review follow-up)", () => {
  it(`a move of ${2 * MAX_BUFFERED_ITEMS + 500} messages → 3 event.mail.moved of at most ${MAX_BUFFERED_ITEMS}, in order`, async () => {
    const fm = syntheticMessenger(2500);
    const { fwd, timers, sent, logs } = setup(fm);
    const dst = folder("account1", "Archive");
    const orig = fm.msgs.filter((m) => m.folderId === INBOX).map((m) => hdr(fm, m.id));
    expect(orig).toHaveLength(2500);
    fm.messages.onMoved.emit(
      fm.listOf(orig),
      fm.listOf(orig.map((h, i) => inFolder(h, dst, 10_000 + i))),
    );
    await tick();
    timers.advance(DEBOUNCE_MS);
    expect(sent.map(([m, p]) => [m, p.src_folder_id, p.dst_folder_id])).toEqual([
      ["event.mail.moved", INBOX, ARCHIVE],
      ["event.mail.moved", INBOX, ARCHIVE],
      ["event.mail.moved", INBOX, ARCHIVE],
    ]);
    const ids = sent.map(([, p]) =>
      (p as { items: { tb_id: number }[] }).items.map((i) => i.tb_id),
    );
    expect(ids.map((x) => x.length)).toEqual([MAX_BUFFERED_ITEMS, MAX_BUFFERED_ITEMS, 500]);
    expect(ids.flat()).toEqual(orig.map((_, i) => 10_000 + i));
    expect(fwd.bufferedItems).toBe(0);
    expect(logs).toEqual([]);
  });

  it("a send that throws after one success never re-sends what was already sent", () => {
    const fm = syntheticMessenger();
    const sent: string[] = [];
    let ready = false;
    let throwOnce = true;
    const timers = new FakeTimers();
    const fwd = new EventForwarder(
      tbApiFrom(fm),
      (method) => {
        if (!ready) return false;
        if (sent.length === 1 && throwOnce) {
          throwOnce = false;
          throw new Error("port gone");
        }
        sent.push(method);
        return true;
      },
      () => {},
      timers,
    );
    fwd.register(tbEventsFrom(fm));
    fm.messages.onUpdated.emit(hdr(fm, 1), {}, {});
    fm.folders.onCreated.emit(folder("account1", "New"));
    ready = true;
    expect(() => fwd.flush()).toThrow("port gone");
    fwd.flush();
    expect(sent).toEqual(["event.mail.updated", "event.folder.changed"]);
    expect(fwd.bufferedItems).toBe(0);
  });

  it("a log call that throws does not wedge the events that arrive after it", async () => {
    const fm = syntheticMessenger();
    const sent: string[] = [];
    const logs: string[] = [];
    let logThrows = true;
    const timers = new FakeTimers();
    const fwd = new EventForwarder(
      tbApiFrom(fm),
      (method) => {
        sent.push(method);
        return true;
      },
      (m) => {
        if (logThrows) {
          logThrows = false;
          throw new Error("log down");
        }
        logs.push(m);
      },
      timers,
    );
    fwd.register(tbEventsFrom(fm));
    const broken = { ...hdr(fm, 1), tags: undefined as unknown as string[] }; // [...tags] throws
    fm.messages.onNewMailReceived.emit(inbox, fm.listOf([broken]));
    fm.messages.onNewMailReceived.emit(inbox, fm.listOf([hdr(fm, 2)]));
    fm.messages.onUpdated.emit(hdr(fm, 3), {}, {});
    await tick();
    timers.advance(DEBOUNCE_MS);
    expect(sent).toEqual(["event.mail.new", "event.mail.updated"]);
    expect(logs).toEqual(["new mail event lost: Error"]);
  });
});

describe("causal order per folder (review item 2)", () => {
  const a = folder("account1", "A");
  const b = folder("account1", "B");

  it("A→B, B→A, A→B within one debounce window → three moves, in that order", async () => {
    const { fm, timers, sent } = setup();
    const x = hdr(fm, 1);
    const steps: [TbMessageHeader, TbMessageHeader][] = [
      [inFolder(x, a, 11), inFolder(x, b, 12)],
      [inFolder(x, b, 12), inFolder(x, a, 13)],
      [inFolder(x, a, 13), inFolder(x, b, 14)],
    ];
    for (const [from, to] of steps) {
      fm.messages.onMoved.emit(fm.listOf([from]), fm.listOf([to]));
      await tick();
      timers.advance(100);
    }
    timers.advance(DEBOUNCE_MS);
    expect(
      sent.map(([m, p]) => [
        m,
        p.src_folder_id,
        p.dst_folder_id,
        (p as { items: { tb_id: number }[] }).items.map((i) => i.tb_id),
      ]),
    ).toEqual([
      ["event.mail.moved", a.id, b.id, [12]],
      ["event.mail.moved", b.id, a.id, [13]],
      ["event.mail.moved", a.id, b.id, [14]],
    ]);
  });

  it("delete → copy back → delete in one folder keeps that order", async () => {
    const { fm, timers, sent } = setup();
    const x = hdr(fm, 1);
    const elsewhere = folder("account1", "Archive");
    fm.messages.onDeleted.emit(fm.listOf([x]));
    await tick();
    fm.messages.onCopied.emit(
      fm.listOf([inFolder(x, elsewhere, 700)]),
      fm.listOf([inFolder(x, inbox, 701)]),
    );
    await tick();
    fm.messages.onDeleted.emit(fm.listOf([inFolder(x, inbox, 701)]));
    await tick();
    timers.advance(DEBOUNCE_MS);
    expect(sent.map(([m, p]) => [m, p.folder_id])).toEqual([
      ["event.mail.deleted", INBOX],
      ["event.mail.new", INBOX],
      ["event.mail.deleted", INBOX],
    ]);
  });

  it("an update arriving while an earlier move is still draining is sent after it", async () => {
    const { fm, timers, sent } = setup();
    fm.pageSize = 1; // both move lists need continueList: the drain is pending
    const orig = [inFolder(hdr(fm, 1), a), inFolder(hdr(fm, 2), a)];
    fm.messages.onMoved.emit(
      fm.listOf(orig),
      fm.listOf(orig.map((h, i) => inFolder(h, b, 40 + i))),
    );
    fm.messages.onUpdated.emit({ ...inFolder(hdr(fm, 1), b, 40), flagged: true }, {}, {});
    await tick();
    timers.advance(DEBOUNCE_MS);
    expect(sent.map(([m]) => m)).toEqual(["event.mail.moved", "event.mail.updated"]);
  });
});

describe("failures", () => {
  it("a listener whose list cannot be drained logs the loss, by error type only", async () => {
    const { fm, timers, sent, logs } = setup();
    fm.messages.onNewMailReceived.emit(inbox, { id: "list-gone", messages: [hdr(fm, 1)] });
    await tick();
    timers.advance(DEBOUNCE_MS);
    expect(sent).toEqual([]);
    expect(logs).toEqual(["new mail event lost: Error"]);
  });

  it("an item TB cannot date is dropped and counted, never sent with a made-up date", async () => {
    const { fm, timers, sent, logs } = setup();
    const bad = { ...hdr(fm, 1), date: new Date(Number.NaN) };
    fm.messages.onNewMailReceived.emit(inbox, fm.listOf([bad, hdr(fm, 2)]));
    await tick();
    timers.advance(DEBOUNCE_MS);
    expect(nth<{ items: { tb_id: number }[] }>(sent, 0).items.map((i) => i.tb_id)).toEqual([2]);
    expect(logs).toEqual(["1 event items dropped (reconcile repairs them)"]);
  });

  it("a header whose mapping throws (not a bad date) loses the event, logged by type, not counted", async () => {
    const { fm, timers, sent, logs } = setup();
    const broken = { ...hdr(fm, 1), tags: undefined as unknown as string[] }; // [...tags] throws
    fm.messages.onNewMailReceived.emit(inbox, fm.listOf([broken, hdr(fm, 2)]));
    await tick();
    timers.advance(DEBOUNCE_MS);
    expect(sent).toEqual([]);
    expect(logs).toEqual(["new mail event lost: TypeError"]);
  });

  it("events whose every item is invalid send nothing (no empty batches)", async () => {
    const { fm, timers, sent, logs } = setup();
    const bad = (id: number): TbMessageHeader => ({ ...hdr(fm, id), date: new Date(Number.NaN) });
    const dst = folder("account1", "Archive");
    fm.messages.onMoved.emit(fm.listOf([bad(1)]), fm.listOf([inFolder(bad(1), dst, 800)]));
    await tick();
    fm.messages.onNewMailReceived.emit(inbox, fm.listOf([bad(2)]));
    await tick();
    timers.advance(MAX_WAIT_MS);
    expect(sent).toEqual([]);
    expect(logs).toEqual([
      "1 event items dropped (reconcile repairs them)",
      "1 event items dropped (reconcile repairs them)",
    ]);
  });
});
