import { describe, expect, it } from "vitest";
import {
  Bridge,
  PROFILE_KEY,
  RECONNECT_ALARM,
  Reassembler,
  registerListeners,
} from "../src/background/bridge";
import { MailHandlers } from "../src/background/handlers";
import type { NativePort, Platform } from "../src/background/platform";
import {
  BridgeError,
  type Capability,
  type Chunk,
  ERROR_CODES,
  type ErrorName,
  HOST_TO_EXT_MAX,
  MINOR,
  parseMessage,
  type RpcId,
} from "../src/protocol";
import { tbApiFrom } from "../src/tb/api";
import { resultErrors } from "./contract";
import { FakeMessenger, type GoldenTb, messengerFromGolden } from "./fake_messenger";

const UUID = "123e4567-e89b-42d3-a456-426614174000";

type Msg = Record<string, unknown> & { id?: number; method?: string };

class FakePort implements NativePort {
  posted: Msg[] = [];
  error: { message: string } | null = null;
  private msgCbs: ((m: unknown) => void)[] = [];
  private discCbs: ((p: NativePort) => void)[] = [];
  onMessage = { addListener: (cb: (m: unknown) => void) => this.msgCbs.push(cb) };
  onDisconnect = { addListener: (cb: (p: NativePort) => void) => this.discCbs.push(cb) };
  postMessage(m: unknown): void {
    this.posted.push(m as Msg);
  }
  disconnect(): void {}
  emit(m: unknown): void {
    for (const cb of this.msgCbs) cb(m);
  }
  drop(): void {
    for (const cb of this.discCbs) cb(this);
  }
  last(method: string): Msg {
    const m = [...this.posted].reverse().find((x) => x.method === method);
    if (m === undefined) throw new Error(`no ${method} posted`);
    return m;
  }
}

class FakePlatform implements Platform {
  version = "0.1.0";
  ports: FakePort[] = [];
  store = new Map<string, unknown>();
  alarms = new Set<string>();
  uuids = 0;
  uuid = UUID;
  listeners: string[] = [];
  startup: (() => void)[] = [];
  alarmCbs: ((n: string) => void)[] = [];
  connectNative(app: string): NativePort {
    expect(app).toBe("tern");
    const p = new FakePort();
    this.ports.push(p);
    return p;
  }
  async storageGet(key: string): Promise<unknown> {
    return this.store.get(key);
  }
  async storageSet(key: string, value: unknown): Promise<void> {
    this.store.set(key, value);
  }
  alarmCreateFailures = 0; // the next N alarmCreate calls reject
  alarmClearFails = false;
  alarmPeriodOverride: number | null | undefined = undefined; // what alarms.get reports back
  async alarmCreate(name: string, periodInMinutes: number): Promise<number | null> {
    if (this.alarmCreateFailures > 0) {
      this.alarmCreateFailures -= 1;
      throw new Error("alarms.create rejected");
    }
    this.alarms.add(name);
    return this.alarmPeriodOverride === undefined ? periodInMinutes : this.alarmPeriodOverride;
  }
  async alarmClear(name: string): Promise<void> {
    if (this.alarmClearFails) throw new Error("alarms.clear rejected");
    this.alarms.delete(name);
  }
  onAlarm(cb: (n: string) => void): void {
    this.listeners.push("alarm");
    this.alarmCbs.push(cb);
  }
  onStartup(cb: () => void): void {
    this.listeners.push("startup");
    this.startup.push(cb);
  }
  onInstalled(): void {
    this.listeners.push("installed");
  }
  randomUUID(): string {
    this.uuids += 1;
    return this.uuid;
  }
  get port(): FakePort {
    const p = this.ports.at(-1);
    if (p === undefined) throw new Error("no port");
    return p;
  }
}

const tick = (): Promise<void> => new Promise((r) => setTimeout(r, 0));

async function answerHello(p: FakePlatform, pending: unknown[] = []): Promise<void> {
  await tick();
  const hello = p.port.last("hello");
  p.port.emit({
    jsonrpc: "2.0",
    id: hello.id,
    result: {
      protocol: "tern-bridge/1",
      minor: 1,
      role: "daemon",
      version: "test",
      capabilities: [],
    },
  });
  await tick();
  const ap = p.port.last("approvals.pending");
  p.port.emit({ jsonrpc: "2.0", id: ap.id, result: pending });
  await tick();
}

async function ready(): Promise<{ p: FakePlatform; b: Bridge; resyncs: unknown[] }> {
  const p = new FakePlatform();
  const b = new Bridge(p, () => {});
  const resyncs: unknown[] = [];
  b.onResync = (x) => resyncs.push(x);
  const c = b.connect();
  await answerHello(p, ["a1"]);
  await c;
  return { p, b, resyncs };
}

describe("BridgeError", () => {
  it("puts the bare message on the wire, as Python's to_rpc does", () => {
    const e = new BridgeError("stale_id", "cursor is not valid");
    expect(e.message).toBe("stale_id: cursor is not valid");
    expect(e.toRpc()).toStrictEqual({
      code: ERROR_CODES.stale_id,
      message: "cursor is not valid",
      data: { name: "stale_id" },
    });
    const tb = new BridgeError("tb_error", "Thunderbird call failed", "raw_failed").toRpc();
    expect(BridgeError.fromRpc(tb).toRpc()).toStrictEqual(tb);
  });
});

describe("capabilities in hello (plan 03 step 5)", () => {
  const helloCaps = async (serve: (b: Bridge) => void): Promise<unknown> => {
    const p = new FakePlatform();
    const b = new Bridge(p, () => {});
    serve(b);
    void b.connect();
    await tick();
    return (p.port.last("hello").params as Record<string, unknown>).capabilities;
  };
  const provider = (capabilities: Capability[], methods: string[]) => ({
    capabilities,
    methods: () => new Map(methods.map((m) => [m, async () => ({})])),
  });

  it("a bridge that serves only bridge.ping reports none", async () => {
    expect(await helloCaps(() => {})).toStrictEqual([]);
  });

  it("serving the mail handlers reports mail.read", async () => {
    const mail = new MailHandlers(tbApiFrom(new FakeMessenger()), () => {});
    expect(await helloCaps((b) => b.register(mail))).toStrictEqual(["mail.read"]);
  });

  it("reports the union of the providers, sorted, without repeats", async () => {
    const caps = await helloCaps((b) => {
      b.register(provider(["mail.write", "mail.read"], ["x.a"]));
      b.register(provider(["drafts", "mail.read"], ["x.b"]));
    });
    expect(caps).toStrictEqual(["drafts", "mail.read", "mail.write"]);
  });

  it("serves the providers' methods; one served twice is an error", () => {
    const b = new Bridge(new FakePlatform(), () => {});
    b.register(provider([], ["x.a"]));
    expect(b.handlers.has("x.a")).toBe(true);
    expect(() => b.register(provider([], ["x.a"]))).toThrow("x.a is already served");
    expect(() => b.register(provider([], ["bridge.ping"]))).toThrow(
      "bridge.ping is already served",
    );
  });
});

describe("listeners (S1 F14)", () => {
  it("registers onStartup, onInstalled and the alarm synchronously", () => {
    const p = new FakePlatform();
    registerListeners(p, new Bridge(p, () => {}));
    expect(p.listeners.sort()).toEqual(["alarm", "installed", "startup"]);
    expect(p.ports).toHaveLength(0); // registering alone does not connect
  });

  it("onStartup connects", async () => {
    const p = new FakePlatform();
    registerListeners(p, new Bridge(p, () => {}));
    for (const cb of p.startup) cb();
    await tick();
    expect(p.port.last("hello").method).toBe("hello");
  });
});

describe("hello and profile_id", () => {
  it("creates profile_id once and reuses it", async () => {
    const { p } = await ready();
    expect(p.store.get(PROFILE_KEY)).toBe(UUID);
    const hello = p.port.last("hello") as { params: Record<string, unknown> };
    expect(hello.params).toMatchObject({
      protocol: "tern-bridge/1",
      minor: 3, // tern-bridge/1.3 (the model picker)
      role: "extension",
      profile_id: UUID,
    });
    const b2 = new Bridge(p, () => {});
    void b2.connect();
    await tick();
    expect(p.uuids).toBe(1);
  });

  it("refuses a corrupt stored profile_id instead of replacing it", async () => {
    const p = new FakePlatform();
    p.store.set(PROFILE_KEY, "not-a-uuid");
    await expect(new Bridge(p, () => {}).connect()).rejects.toThrow("not a UUID");
    expect(p.store.get(PROFILE_KEY)).toBe("not-a-uuid");
  });

  it("resyncs approvals after hello", async () => {
    const { b, resyncs } = await ready();
    expect(b.state).toBe("ready");
    expect(resyncs).toEqual([["a1"]]);
  });

  it("onReady throwing is logged by type; resync still runs and connect() resolves", async () => {
    const p = new FakePlatform();
    const logs: string[] = [];
    const b = new Bridge(p, (m) => logs.push(m));
    const resyncs: unknown[] = [];
    b.onResync = (x) => resyncs.push(x);
    b.onReady = () => {
      throw new TypeError("postMessage on a dying port: Subject m1@example.com");
    };
    const outcome = b.connect().then(
      () => "resolved",
      (err: unknown) => `rejected: ${String(err)}`,
    );
    await tick();
    p.port.emit({
      jsonrpc: "2.0",
      id: p.port.last("hello").id,
      result: {
        protocol: "tern-bridge/1",
        minor: 1,
        role: "daemon",
        version: "test",
        capabilities: [],
      },
    });
    await tick();
    expect(p.port.posted.map((m) => m.method)).toContain("approvals.pending");
    p.port.emit({ jsonrpc: "2.0", id: p.port.last("approvals.pending").id, result: ["a1"] });
    expect(await outcome).toBe("resolved");
    expect(b.state).toBe("ready");
    expect(resyncs).toEqual([["a1"]]);
    expect(logs).toContain("onReady failed: TypeError");
    expect(logs.join("\n")).not.toMatch(/@|Subject|dying/);
  });

  it("notify on a port whose postMessage throws → false, logged by type only", async () => {
    const p = new FakePlatform();
    const logs: string[] = [];
    const b = new Bridge(p, (m) => logs.push(m));
    const c = b.connect();
    await answerHello(p, []);
    await c;
    logs.length = 0;
    p.port.postMessage = () => {
      throw new TypeError("Attempt to postMessage on disconnected port: m1@example.com");
    };
    expect(b.notify("event.mail.new", { folder_id: "f1", items: [] })).toBe(false);
    expect(logs).toEqual(["notify not delivered: TypeError"]);
  });

  it("a failed approvals resync leaves the bridge ready", async () => {
    // Plan 02's daemon answers approvals.pending with unavailable ("not wired yet", plan 06b).
    const p = new FakePlatform();
    const logs: string[] = [];
    const b = new Bridge(p, (m) => logs.push(m));
    const resyncs: unknown[] = [];
    b.onResync = (x) => resyncs.push(x);
    p.alarms.add(RECONNECT_ALARM);
    const c = b.connect();
    await tick();
    p.port.emit({
      jsonrpc: "2.0",
      id: p.port.last("hello").id,
      result: {
        protocol: "tern-bridge/1",
        minor: 1,
        role: "daemon",
        version: "test",
        capabilities: [],
      },
    });
    await tick();
    const err = new BridgeError("unavailable", "approvals.pending is not wired yet").toRpc();
    p.port.emit({ jsonrpc: "2.0", id: p.port.last("approvals.pending").id, error: err });
    await c;
    expect(b.state).toBe("ready");
    expect(b.rejection).toBeNull();
    expect(p.alarms.has(RECONNECT_ALARM)).toBe(false);
    expect(resyncs).toEqual([]);
    expect(logs).toContain("approvals resync failed: unavailable");
    const r = b.request("index.status");
    expect(p.port.posted.at(-1)?.method).toBe("index.status");
    p.port.emit({ jsonrpc: "2.0", id: p.port.posted.at(-1)?.id, result: { messages: 0 } });
    await expect(r).resolves.toEqual({ messages: 0 });
  });

  it("a rejected hello is visible, not retried", async () => {
    const p = new FakePlatform();
    const b = new Bridge(p, () => {});
    const c = b.connect();
    await tick();
    const err = new BridgeError("profile_mismatch", "other profile").toRpc();
    p.port.emit({ jsonrpc: "2.0", id: p.port.last("hello").id, error: err });
    await c;
    expect(b.state).toBe("rejected");
    expect(b.rejection?.code).toBe("profile_mismatch");
  });
});

describe("requests", () => {
  it("correlates responses and maps errors", async () => {
    const { p, b } = await ready();
    const r1 = b.request("index.status");
    const r2 = b.request("search.query", { query: "x" });
    const [m1, m2] = p.port.posted.slice(-2);
    p.port.emit({
      jsonrpc: "2.0",
      id: m2?.id,
      error: { code: ERROR_CODES.invalid_params, message: "m", data: { name: "invalid_params" } },
    });
    p.port.emit({ jsonrpc: "2.0", id: m1?.id, result: { messages: 1 } });
    await expect(r1).resolves.toEqual({ messages: 1 });
    await expect(r2).rejects.toMatchObject({ code: "invalid_params" });
  });

  it("answers bridge.ping from the daemon", async () => {
    const { p } = await ready();
    p.port.emit({ jsonrpc: "2.0", id: 77, method: "bridge.ping", params: {} });
    await tick();
    expect(p.port.posted.at(-1)).toEqual({ jsonrpc: "2.0", id: 77, result: {} });
  });

  it("an unknown daemon request gets method_not_available", async () => {
    const { p } = await ready();
    p.port.emit({ jsonrpc: "2.0", id: 5, method: "mail.accounts.list", params: {} });
    await tick();
    const last = p.port.posted.at(-1) as { error: { data: { name: string } } };
    expect(last.error.data.name).toBe("method_not_available");
  });

  it("refuses requests before hello", async () => {
    const p = new FakePlatform();
    await expect(new Bridge(p, () => {}).request("index.status")).rejects.toMatchObject({
      code: "unavailable",
    });
  });
});

describe("reconnect", () => {
  it("port disconnect → pending unavailable → alarm → reconnect → re-hello → resync", async () => {
    const { p, b, resyncs } = await ready();
    const pending = b.request("index.status");
    const first = p.port;
    first.drop();
    await expect(pending).rejects.toMatchObject({ code: "unavailable" });
    expect(p.alarms.has(RECONNECT_ALARM)).toBe(true);
    expect(p.ports).toHaveLength(2); // immediate reconnect attempt
    expect(p.port).not.toBe(first);
    await answerHello(p, ["a2"]);
    expect(b.state).toBe("ready");
    expect(p.alarms.has(RECONNECT_ALARM)).toBe(false);
    expect(resyncs).toEqual([["a1"], ["a2"]]);
  });

  it("the alarm reconnects when the immediate attempt did not", async () => {
    const { p, b } = await ready();
    registerListeners(p, b);
    p.port.drop(); // a working connection lost: one immediate retry
    expect(p.ports).toHaveLength(2);
    await tick();
    p.port.drop(); // the retry's host died before hello: no further immediate retry
    await tick();
    expect(p.ports).toHaveLength(2);
    expect(p.alarms.has(RECONNECT_ALARM)).toBe(true);
    for (const cb of p.alarmCbs) cb(RECONNECT_ALARM);
    await tick();
    expect(p.ports).toHaveLength(3);
    await answerHello(p);
    expect(b.state).toBe("ready");
  });

  it("a host that never comes up does not spin", async () => {
    const p = new FakePlatform();
    const b = new Bridge(p, () => {});
    void b.connect();
    await tick();
    p.port.drop();
    await tick();
    expect(p.ports).toHaveLength(1);
    expect(b.state).toBe("disconnected");
    expect(p.alarms.has(RECONNECT_ALARM)).toBe(true);
  });

  it("bridge.reconnected from the host triggers a new hello", async () => {
    const { p, b } = await ready();
    const hellos = p.port.posted.filter((m) => m.method === "hello").length;
    p.port.emit({ jsonrpc: "2.0", method: "bridge.reconnected", params: {} });
    expect(b.state).toBe("connecting");
    await answerHello(p);
    expect(p.port.posted.filter((m) => m.method === "hello").length).toBe(hellos + 1);
    expect(b.state).toBe("ready");
  });
});

describe("tern-bridge 1.1: negotiated minor and the step 3 review fixes (plan 07 step 9b)", () => {
  async function helloWith(result: unknown, localMinor?: number) {
    const p = new FakePlatform();
    const logs: string[] = [];
    const b = new Bridge(p, (m) => logs.push(m), localMinor);
    const c = b.connect();
    await tick();
    const hello = p.port.last("hello") as Msg & { params: Record<string, unknown> };
    p.port.emit({ jsonrpc: "2.0", id: hello.id, result });
    await tick();
    const ap = p.port.posted.find((m) => m.method === "approvals.pending");
    if (ap !== undefined) p.port.emit({ jsonrpc: "2.0", id: ap.id, result: [] });
    await c;
    return { p, b, hello, logs };
  }
  const daemon = (minor: unknown) => ({
    protocol: "tern-bridge/1",
    minor,
    role: "daemon",
    version: "test",
    capabilities: [],
  });

  it("the session uses the lower minor of ours and the daemon's", async () => {
    expect(MINOR).toBe(3); // tern-bridge/1.3 (the model picker)
    const newer = await helloWith(daemon(4));
    expect([newer.b.minor, newer.hello.params.minor, newer.b.state]).toEqual([3, 3, "ready"]);
    const older = await helloWith(daemon(0));
    expect([older.b.minor, older.b.state]).toEqual([0, "ready"]);
    const legacy = await helloWith(daemon(1), 0);
    expect([legacy.b.minor, legacy.hello.params.minor]).toEqual([0, 0]);
  });

  it.each([[undefined], [-1], [1.5], ["1"], [null]])(
    "a daemon hello with minor %s is a rejection, never a guessed minor",
    async (minor) => {
      const { b, logs } = await helloWith(daemon(minor));
      expect(b.state).toBe("rejected");
      expect(b.minor).toBe(0);
      expect(b.rejection?.code).toBe("invalid_params");
      expect(logs).toContain("hello rejected: invalid_params");
    },
  );

  it("a local minor outside 0..MINOR is refused", () => {
    expect(() => new Bridge(new FakePlatform(), () => {}, MINOR + 1)).toThrow("local minor");
    expect(() => new Bridge(new FakePlatform(), () => {}, -1)).toThrow("local minor");
  });

  it("the minor resets when the port closes and when ternd restarts", async () => {
    const { p, b } = await ready();
    expect(b.minor).toBe(1);
    p.port.emit({ jsonrpc: "2.0", method: "bridge.reconnected", params: {} });
    expect(b.minor).toBe(0);
    await answerHello(p);
    expect(b.minor).toBe(1);
    p.port.drop();
    expect(b.minor).toBe(0);
  });

  it("a request whose postMessage throws rejects unavailable and leaves nothing pending", async () => {
    const p = new FakePlatform();
    const logs: string[] = [];
    const b = new Bridge(p, (m) => logs.push(m));
    const c = b.connect();
    await answerHello(p, []);
    await c;
    const port = p.port;
    port.postMessage = () => {
      throw new TypeError("Attempt to postMessage on disconnected port: m1@example.com");
    };
    const err = await b.request("index.status").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(BridgeError);
    expect((err as BridgeError).code).toBe("unavailable");
    expect((b as unknown as { pending: Map<unknown, unknown> }).pending.size).toBe(0);
    expect(logs).toContain("index.status not sent: TypeError");
    expect(logs.join("\n")).not.toContain("example.com"); // the error's text is never logged
  });

  it("bridge.reconnected fails the requests still pending (they belonged to the old ternd)", async () => {
    const { p, b } = await ready();
    const out = b.request("index.status").catch((e: unknown) => e);
    await tick();
    p.port.emit({ jsonrpc: "2.0", method: "bridge.reconnected", params: {} });
    const err = await out;
    expect(err).toBeInstanceOf(BridgeError);
    expect((err as BridgeError).code).toBe("unavailable");
    expect((err as BridgeError).rpcMessage).toBe("ternd restarted");
  });
});

function chunksOf(text: string, id: string, size: number): Chunk[] {
  const bytes = new TextEncoder().encode(text);
  const out: Chunk[] = [];
  for (let off = 0, seq = 0; off < bytes.length; off += size, seq++) {
    let bin = "";
    for (const b of bytes.slice(off, off + size)) bin += String.fromCharCode(b);
    out.push({ chunk_id: id, seq, last: off + size >= bytes.length, data_b64: btoa(bin) });
  }
  return out;
}

describe("reconnect alarm failures (plan 02 review, deferred to plan 03)", () => {
  async function dropAfterReady(setup: (p: FakePlatform) => void): Promise<{
    p: FakePlatform;
    b: Bridge;
    logs: string[];
  }> {
    const p = new FakePlatform();
    const logs: string[] = [];
    const b = new Bridge(p, (m) => logs.push(m));
    const c = b.connect();
    await answerHello(p);
    await c;
    setup(p);
    const first = p.port;
    first.drop();
    await tick();
    p.port.drop(); // the immediate retry dies too: only the alarm is left
    await tick();
    return { p, b, logs };
  }

  it("a rejected create is logged and retried once", async () => {
    const { p, logs } = await dropAfterReady((x) => {
      x.alarmCreateFailures = 1;
    });
    expect(logs.some((m) => m.includes("create failed (attempt 1)"))).toBe(true);
    expect(p.alarms.has(RECONNECT_ALARM)).toBe(true);
  });

  it("a create that keeps failing is logged as unavailable, never thrown", async () => {
    const { p, b, logs } = await dropAfterReady((x) => {
      x.alarmCreateFailures = 10;
    });
    expect(logs.some((m) => m.includes("reconnect alarm unavailable"))).toBe(true);
    expect(p.alarms.has(RECONNECT_ALARM)).toBe(false);
    expect(b.state).toBe("disconnected");
  });

  it("a period other than 1 min read back after create is logged", async () => {
    const { logs } = await dropAfterReady((x) => {
      x.alarmPeriodOverride = null;
    });
    expect(logs.some((m) => m.includes("reconnect alarm period is null"))).toBe(true);
  });

  it("a rejected clear after hello is logged and the bridge stays ready", async () => {
    const p = new FakePlatform();
    p.alarmClearFails = true;
    const logs: string[] = [];
    const b = new Bridge(p, (m) => logs.push(m));
    const c = b.connect();
    await answerHello(p);
    await c;
    expect(b.state).toBe("ready");
    expect(logs.some((m) => m.includes("reconnect alarm clear failed"))).toBe(true);
  });
});

describe("session end (mail cursors, plan 03)", () => {
  it("fires when the port is lost", async () => {
    const { p, b } = await ready();
    let ends = 0;
    b.onSessionEnd = () => {
      ends += 1;
    };
    p.port.drop();
    expect(ends).toBe(1);
  });

  it("fires when the host reports bridge.reconnected (a new ternd)", async () => {
    const { p, b } = await ready();
    let ends = 0;
    b.onSessionEnd = () => {
      ends += 1;
    };
    p.port.emit({ jsonrpc: "2.0", method: "bridge.reconnected", params: {} });
    expect(ends).toBe(1);
  });
});

describe("chunks", () => {
  it("a chunked reply is reassembled (multi-byte characters split across chunks)", async () => {
    const { p, b } = await ready();
    const r = b.request("index.status");
    const id = p.port.posted.at(-1)?.id;
    const reply = JSON.stringify({ jsonrpc: "2.0", id, result: { s: "ü€".repeat(5000) } });
    for (const c of chunksOf(reply, "c1", 1001)) {
      p.port.emit({ jsonrpc: "2.0", method: "bridge.chunk", params: c });
    }
    await expect(r).resolves.toEqual({ s: "ü€".repeat(5000) });
  });

  it("rejects out-of-order and interleaved chunks", () => {
    const cs = chunksOf("x".repeat(30), "a", 10);
    const r = new Reassembler();
    r.feed(cs[0] as Chunk);
    expect(() => r.feed(cs[2] as Chunk)).toThrow("expected chunk 1");
    const r2 = new Reassembler();
    r2.feed(cs[0] as Chunk);
    expect(() => r2.feed({ ...(cs[1] as Chunk), chunk_id: "b" })).toThrow("new chunk_id");
  });

  it("the reassembler starts clean after an error", () => {
    const cs = chunksOf("x".repeat(30), "a", 10);
    const r = new Reassembler();
    r.feed(cs[0] as Chunk);
    expect(() => r.feed(cs[2] as Chunk)).toThrow("expected chunk 1");
    const next = chunksOf("yz", "b", 10);
    expect(r.feed(next[0] as Chunk)).toBe("yz");
  });

  it("a lost chunked reply fails pending requests, and later chunked replies still work", async () => {
    const { p, b } = await ready();
    const lost = b.request("index.status");
    const lostId = p.port.posted.at(-1)?.id;
    const bad = chunksOf(JSON.stringify({ jsonrpc: "2.0", id: lostId, result: {} }), "c1", 10);
    p.port.emit({ jsonrpc: "2.0", method: "bridge.chunk", params: bad[0] });
    p.port.emit({ jsonrpc: "2.0", method: "bridge.chunk", params: bad[2] }); // gap
    await expect(lost).rejects.toMatchObject({ code: "unavailable" });
    expect(b.state).toBe("ready");
    const r = b.request("index.status");
    const id = p.port.posted.at(-1)?.id;
    const reply = JSON.stringify({ jsonrpc: "2.0", id, result: { messages: 2 } });
    for (const c of chunksOf(reply, "c2", 10)) {
      p.port.emit({ jsonrpc: "2.0", method: "bridge.chunk", params: c });
    }
    await expect(r).resolves.toEqual({ messages: 2 });
  });
});

// ---------------------------------------------------------------------------------------------
// Golden transcripts (plan 02 step 9): tests/golden/bridge/*.jsonl, also replayed against
// tern-nmhost + ternd by tests/bridge/test_golden.py (format in that file's docstring).
// "extension" transcripts are replayed here against Bridge: tern lines are emitted on the fake
// port, client lines must be what Bridge posts (on its own, or via request() when it would not
// send them unprompted). Every transcript's messages must parse as 1.0 messages.

interface FsLike {
  readdirSync(path: URL): string[];
  readFileSync(path: URL, encoding: "utf8"): string;
}
// node:fs without adding Node's types to the whole program (tsconfig "types": []).
const NODE_FS = "node:fs";
const fs = (await import(/* @vite-ignore */ NODE_FS)) as FsLike;
const GOLDEN_DIR = new URL("../../tests/golden/bridge/", import.meta.url);
const REQUIRED_GOLDENS = [
  "chunk",
  "error_hello_invalid",
  "error_role_rejected",
  "errors_cli",
  "hello_cli",
  "hello_extension",
  "mail_read",
  "ping",
  "reconnected",
];
// Transcripts of the extension as main.ts wires it (Bridge + MailHandlers); the others pin the
// bare bridge protocol. A `tb` header block is the profile the mail handlers see (else empty).
const SERVES_MAIL = new Set(["hello_extension", "mail_read"]);

interface GMsg extends Record<string, unknown> {
  id?: RpcId | null;
  method?: string;
  params?: Record<string, unknown>;
  result?: unknown;
  error?: { code: number; message: string; data: { name: ErrorName } };
}
type Step = { from: "client" | "tern"; msg: GMsg } | { event: string };
interface Transcript {
  name: string;
  client: "extension" | "socket";
  tb?: GoldenTb;
  steps: Step[];
}

function loadGoldens(): Transcript[] {
  return fs
    .readdirSync(GOLDEN_DIR)
    .filter((f) => f.endsWith(".jsonl"))
    .sort()
    .map((f) => {
      const lines = fs
        .readFileSync(new URL(f, GOLDEN_DIR), "utf8")
        .split("\n")
        .filter((l) => l !== "")
        .map((l) => JSON.parse(l) as unknown);
      const [header, ...steps] = lines as [
        { transcript: string; client: string; tb?: GoldenTb },
        ...Step[],
      ];
      expect(header.transcript).toBe(f.replace(/\.jsonl$/, ""));
      expect(["extension", "socket"]).toContain(header.client);
      return {
        name: header.transcript,
        client: header.client as Transcript["client"],
        ...(header.tb === undefined ? {} : { tb: header.tb }),
        steps,
      };
    });
}

const GOLDENS = loadGoldens();

function golden(name: string): Transcript {
  const t = GOLDENS.find((x) => x.name === name);
  if (t === undefined) throw new Error(`no golden ${name}`);
  return t;
}

/** Messages with each bridge.chunk run decoded (atob + TextDecoder, not Bridge's Reassembler). */
function logical(steps: Step[]): { from: "client" | "tern"; msg: GMsg }[] {
  const out: { from: "client" | "tern"; msg: GMsg }[] = [];
  let parts: number[] = [];
  for (const s of steps) {
    if (!("msg" in s)) continue;
    if (s.msg.method !== "bridge.chunk") {
      out.push(s);
      continue;
    }
    const c = s.msg.params as unknown as Chunk;
    for (const ch of atob(c.data_b64)) parts.push(ch.charCodeAt(0));
    if (c.last) {
      const text = new TextDecoder("utf-8", { fatal: true }).decode(new Uint8Array(parts));
      out.push({ from: s.from, msg: JSON.parse(text) as GMsg });
      parts = [];
    }
  }
  expect(parts).toHaveLength(0);
  return out;
}

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/**
 * `got` with the values that may differ between implementations replaced by the golden's:
 * a `date` naming the same instant (Python keeps the UTC offset, toISOString writes Z with
 * milliseconds) and a `next_cursor` (tokens are per implementation; golden → actual is recorded
 * in `cursors` for the requests that reuse it). Anything else is left for toStrictEqual.
 */
function aligned(got: unknown, want: unknown, cursors: Map<string, string>, key = ""): unknown {
  if (typeof got === "string" && typeof want === "string") {
    if (key === "next_cursor") {
      cursors.set(want, got);
      return want;
    }
    if (key === "date" && !Number.isNaN(Date.parse(got)) && Date.parse(got) === Date.parse(want)) {
      return want;
    }
    return got;
  }
  if (Array.isArray(got) && Array.isArray(want)) {
    return got.map((x, i) => aligned(x, want[i], cursors));
  }
  if (isObj(got) && isObj(want)) {
    return Object.fromEntries(
      Object.entries(got).map(([k, v]) => [k, aligned(v, want[k], cursors, k)]),
    );
  }
  return got;
}

/** A daemon request with its golden cursor swapped for the token the handlers issued. */
function withCursor(msg: GMsg, cursors: Map<string, string>): GMsg {
  const c = msg.params?.cursor;
  if (typeof c !== "string" || !cursors.has(c)) return msg;
  return { ...msg, params: { ...msg.params, cursor: cursors.get(c) } };
}

const settle = async (): Promise<void> => {
  for (let i = 0; i < 5; i++) await tick();
};

type Outcome = { ok: unknown } | { err: ErrorName };
const outcome = (pr: Promise<unknown>): Promise<Outcome> =>
  pr.then(
    (ok) => ({ ok }),
    (e: unknown) => {
      if (e instanceof BridgeError) return { err: e.code };
      throw e;
    },
  );

async function replayExtension(t: Transcript): Promise<{
  b: Bridge;
  resyncs: unknown[];
  triggered: Map<RpcId, Promise<Outcome>>;
}> {
  const hello = logical(t.steps).find((m) => m.from === "client" && m.msg.method === "hello");
  if (hello?.msg.params === undefined) throw new Error(`${t.name}: no client hello`);
  const p = new FakePlatform();
  p.version = hello.msg.params.version as string;
  p.uuid = hello.msg.params.profile_id as string;
  // The transcripts are frozen 1.0 conversations: replayed by a Bridge speaking 1.0 (as
  // tests/bridge/test_golden.py replays them against a 1.0 daemon; plan 07 amendment B2).
  const b = new Bridge(p, () => {}, 0);
  if (SERVES_MAIL.has(t.name)) {
    const fm = t.tb === undefined ? new FakeMessenger() : messengerFromGolden(t.tb);
    const mail = new MailHandlers(tbApiFrom(fm), () => {});
    b.register(mail); // as main.ts does
    b.onSessionEnd = () => void mail.endSession();
  }
  const resyncs: unknown[] = [];
  b.onResync = (x) => resyncs.push(x);
  const triggered = new Map<RpcId, Promise<Outcome>>();
  const cursors = new Map<string, string>();
  const daemonRequests = new Map<RpcId, string>(); // id → method of the daemon's requests
  void b.connect();
  let consumed = 0;
  for (const [i, s] of t.steps.entries()) {
    if (!("msg" in s)) continue; // daemon_down / daemon_up happen behind the host
    await settle();
    const where = `${t.name}.jsonl line ${i + 2}`;
    if (s.from === "tern") {
      const { id, method } = s.msg;
      if (method !== undefined && id !== undefined && id !== null) daemonRequests.set(id, method);
      p.port.emit(withCursor(s.msg, cursors));
      continue;
    }
    if (p.port.posted[consumed] === undefined) {
      // Bridge does not send this on its own: drive it through the public API.
      const { id, method, params } = s.msg;
      if (method === undefined || id === undefined || id === null) throw new Error(where);
      triggered.set(id, outcome(b.request(method, params ?? {})));
      await settle();
    }
    const got = p.port.posted[consumed];
    const answered = got?.method === undefined ? daemonRequests.get(got?.id as RpcId) : undefined;
    if (answered !== undefined && got !== undefined && "result" in got) {
      expect(resultErrors(answered, got.result), `${where}: 1.0 result`).toEqual([]);
    }
    expect(aligned(got, s.msg, cursors), where).toStrictEqual(s.msg);
    consumed++;
  }
  await settle();
  expect(p.port.posted.slice(consumed), `${t.name}: messages after the transcript`).toEqual([]);
  expect(p.ports).toHaveLength(1);
  return { b, resyncs, triggered };
}

describe("golden transcripts", () => {
  it("the required transcripts exist", () => {
    expect(GOLDENS.map((t) => t.name)).toEqual(expect.arrayContaining(REQUIRED_GOLDENS));
  });

  it.each(GOLDENS.map((t) => [t.name, t] as const))("%s: messages parse as 1.0", (_, t) => {
    for (const s of t.steps) {
      if (!("msg" in s)) continue;
      expect(parseMessage(s.msg)).toStrictEqual(s.msg);
      if (s.msg.error !== undefined) {
        expect(s.msg.error.code).toBe(ERROR_CODES[s.msg.error.data.name]);
      }
      if (s.msg.method === "bridge.chunk") {
        const size = new TextEncoder().encode(JSON.stringify(s.msg)).length;
        expect(size).toBeLessThan(HOST_TO_EXT_MAX);
      }
    }
  });

  it.each(GOLDENS.filter((t) => t.client === "extension").map((t) => [t.name, t] as const))(
    "%s: replays against Bridge",
    async (_, t) => {
      const { b, resyncs, triggered } = await replayExtension(t);
      const msgs = logical(t.steps);
      const replies = new Map<RpcId, GMsg>();
      for (const m of msgs) {
        if (m.from === "tern" && m.msg.method === undefined && m.msg.id != null) {
          replies.set(m.msg.id, m.msg);
        }
      }
      const replyTo = (id: RpcId | null | undefined): GMsg => {
        const r = id == null ? undefined : replies.get(id);
        if (r === undefined) throw new Error(`${t.name}: no reply to ${String(id)}`);
        return r;
      };
      // Requests driven through request() settle with the golden reply.
      for (const [id, out] of triggered) {
        const r = replyTo(id);
        const want = r.error === undefined ? { ok: r.result } : { err: r.error.data.name };
        expect(await out).toStrictEqual(want);
      }
      const sent = msgs.filter((m) => m.from === "client").map((m) => m.msg);
      // Every approvals.pending result reaches onResync, in order (chunked ones reassembled).
      const pending = sent.filter((m) => m.method === "approvals.pending");
      expect(resyncs).toStrictEqual(pending.map((m) => replyTo(m.id).result));
      // The state follows the last hello reply.
      const last = replyTo(sent.filter((m) => m.method === "hello").at(-1)?.id);
      if (last.error === undefined) {
        expect(b.state).toBe("ready");
      } else {
        expect(b.state).toBe("rejected");
        expect(b.rejection?.code).toBe(last.error.data.name);
      }
    },
  );

  it("the replay fails when a golden line differs from what Bridge sends", async () => {
    const t = structuredClone(golden("ping"));
    const answer = t.steps.find(
      (s) => "msg" in s && s.from === "client" && s.msg.method === undefined,
    );
    if (answer === undefined || !("msg" in answer)) throw new Error("ping golden changed");
    answer.msg.result = { unexpected: true };
    await expect(replayExtension(t)).rejects.toThrow("ping.jsonl line");
  });

  it("the mail_read replay fails when the handlers' answer differs", async () => {
    const t = structuredClone(golden("mail_read"));
    const msg = t.tb?.messages.find((m) => m.id === 24);
    if (msg === undefined) throw new Error("mail_read golden changed");
    msg.date = "2026-09-23T13:10:01Z"; // one second later than the golden's item
    await expect(replayExtension(t)).rejects.toThrow("mail_read.jsonl line");
  });

  it("hello_extension fails against a bridge without the mail handlers", async () => {
    const t = { ...structuredClone(golden("hello_extension")), name: "bare" };
    await expect(replayExtension(t)).rejects.toThrow("bare.jsonl line 2");
  });
});
