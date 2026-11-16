// The extension end of tern-bridge/1 (architecture §3; plan 02 step 7).
//
// - Opens the native port ("tern"), sends hello with profile_id (a UUID kept in storage.local;
//   S1 measured that it survives a TB restart for the temporary load and the XPI).
// - Correlates requests/responses, serves daemon→extension requests (bridge.ping built in),
//   and reassembles bridge.chunk notifications from the host.
// - Port lost: pending requests fail with `unavailable`, and a 1-minute alarm exists only while
//   unconnected (timers alone do not keep an event page alive; an open port does, S1 F12). Only
//   the loss of a working connection is retried at once; a port that dies before it ever got
//   ready waits for the alarm, so a broken host install cannot spin.
// - bridge.reconnected from the host (ternd came back): pending requests fail with `unavailable`
//   (they belonged to a ternd that is gone), then hello is re-sent.
// - minor: the session's negotiated tern-bridge minor, min(ours, the daemon hello's); 0 until a
//   hello succeeds and after the session ends. A daemon hello without a valid minor is a
//   rejection (never a guessed minor). Ours is MINOR unless the constructor is given a lower one
//   (the golden replay of the frozen 1.0 transcripts).
// - A request whose postMessage throws (a port dying before onDisconnect fires) rejects with
//   `unavailable` and leaves nothing pending.
// - Port lost or bridge.reconnected: onSessionEnd() (mail cursors die with the session).
// - After every successful hello: onReady (a throw is logged by error type), then
//   approvals.pending, handed to onResync (a failure is logged and does not undo the hello).
// - A chunked message that cannot be reassembled fails every pending request (its id is unknown).
// - hello reports the union of the capabilities of the handler providers registered with
//   register() (plan 03 step 5); bridge.ping alone gives none. Register before connect().
// - Alarm API failures are logged, never thrown out of a listener. TB 153 (Firefox ESR 153
//   ext-alarms.js) has no minimum period and does not clamp, so the 1-minute period is read
//   back after creation; a mismatch or a failed create is logged, a failed create is retried
//   once, and after that the bridge reconnects only on TB events (onStartup, onInstalled).

import {
  BridgeError,
  type Capability,
  type Chunk,
  type Hello,
  INBOUND_MAX,
  MINOR,
  PROTOCOL,
  parseMessage,
  type RpcId,
  type RpcMessage,
} from "../protocol";
import type { NativePort, Platform } from "./platform";

export const HOST_NAME = "tern";
export const PROFILE_KEY = "profile_id";
export const RECONNECT_ALARM = "tern-bridge-reconnect";
export const RECONNECT_PERIOD_MIN = 1;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type BridgeState = "disconnected" | "connecting" | "ready" | "rejected";
export type RequestHandler = (params: Record<string, unknown>) => Promise<unknown>;
export type NotificationHandler = (params: Record<string, unknown>) => void;

/** A set of request handlers and the capabilities they give the daemon (e.g. MailHandlers). */
export interface HandlerProvider {
  readonly capabilities: readonly Capability[];
  methods(): Map<string, RequestHandler>;
}

interface Pending {
  resolve(result: unknown): void;
  reject(err: BridgeError): void;
}

/** Rebuilds a message from bridge.chunk params (mirror of framing.Reassembler). */
export class Reassembler {
  private id: string | null = null;
  private next = 0;
  private parts: Uint8Array[] = [];
  private size = 0;

  /** On any error the partial message is discarded, so the next chunk_id starts clean. */
  feed(c: Chunk): string | null {
    try {
      return this.add(c);
    } catch (err) {
      this.reset();
      throw err;
    }
  }

  private reset(): void {
    this.id = null;
    this.next = 0;
    this.parts = [];
    this.size = 0;
  }

  private add(c: Chunk): string | null {
    if (this.id === null) {
      if (c.seq !== 0) throw new Error(`chunk sequence starts at ${c.seq}, not 0`);
      this.id = c.chunk_id;
    } else if (c.chunk_id !== this.id) {
      throw new Error("a new chunk_id started before the previous message completed");
    }
    if (c.seq !== this.next) throw new Error(`expected chunk ${this.next}, got ${c.seq}`);
    const bin = atob(c.data_b64);
    const piece = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) piece[i] = bin.charCodeAt(i);
    this.size += piece.length;
    if (this.size > INBOUND_MAX) throw new Error("reassembled message too large");
    this.parts.push(piece);
    this.next += 1;
    if (!c.last) return null;
    const all = new Uint8Array(this.size);
    let off = 0;
    for (const p of this.parts) {
      all.set(p, off);
      off += p.length;
    }
    this.reset();
    return new TextDecoder("utf-8", { fatal: true }).decode(all);
  }
}

/** The `minor` of a daemon hello result, or null when it has none (a non-negative integer). */
export function daemonMinor(reply: unknown): number | null {
  if (typeof reply !== "object" || reply === null) return null;
  const m = (reply as { minor?: unknown }).minor;
  return typeof m === "number" && Number.isInteger(m) && m >= 0 ? m : null;
}

export class Bridge {
  state: BridgeState = "disconnected";
  private negotiated = 0;

  /** The negotiated minor of the current session (0 until a hello succeeds). */
  get minor(): number {
    return this.negotiated;
  }
  rejection: BridgeError | null = null;
  onResync: (pending: unknown) => void = () => {};
  /** The daemon session ended (port lost, or ternd reconnected): session-scoped ids are void. */
  onSessionEnd: () => void = () => {};
  /** A hello just succeeded: buffered notifications can go out. */
  onReady: () => void = () => {};
  readonly handlers = new Map<string, RequestHandler>();
  readonly notifications = new Map<string, NotificationHandler>();

  private port: NativePort | null = null;
  private nextId = 1;
  private pending = new Map<RpcId, Pending>();
  private reassembler = new Reassembler();
  private helloInFlight = false;
  private readonly provided = new Set<Capability>();

  constructor(
    private readonly platform: Platform,
    private readonly log: (msg: string) => void = (m) => console.info(`tern bridge: ${m}`),
    private readonly localMinor: number = MINOR,
  ) {
    if (!Number.isInteger(localMinor) || localMinor < 0 || localMinor > MINOR) {
      throw new Error(`local minor must be 0..${MINOR}`);
    }
    this.handlers.set("bridge.ping", async () => ({}));
  }

  /** Serve a provider's methods; its capabilities go into every later hello. */
  register(provider: HandlerProvider): void {
    for (const [method, handler] of provider.methods()) {
      if (this.handlers.has(method)) throw new Error(`${method} is already served`);
      this.handlers.set(method, handler);
    }
    for (const c of provider.capabilities) this.provided.add(c);
  }

  /** What hello reports: the registered providers' capabilities, sorted, without repeats. */
  get capabilities(): Capability[] {
    return [...this.provided].sort();
  }

  /** Open the port (if needed) and say hello. Safe to call repeatedly. */
  async connect(): Promise<void> {
    if (this.port === null) {
      const port = this.platform.connectNative(HOST_NAME);
      this.port = port;
      port.onMessage.addListener((m) => this.onPortMessage(port, m));
      port.onDisconnect.addListener(() => this.onPortDisconnect(port));
    }
    if (this.state !== "ready") await this.hello();
  }

  async request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    if (this.state !== "ready" && method !== "hello") {
      throw new BridgeError("unavailable", `bridge not ready (${this.state})`);
    }
    return this.send(method, params);
  }

  /**
   * Send a notification to the daemon if ready; false (nothing sent) otherwise. A postMessage that
   * throws (a port that is dying before onDisconnect fires) also counts as not sent: the caller
   * keeps the payload and the next hello flushes it. Logged by error type only.
   */
  notify(method: string, params: Record<string, unknown>): boolean {
    if (this.state !== "ready" || this.port === null) return false;
    try {
      this.port.postMessage({ jsonrpc: "2.0", method, params });
    } catch (err) {
      this.log(`notify not delivered: ${err instanceof Error ? err.name : typeof err}`);
      return false;
    }
    return true;
  }

  private send(method: string, params: Record<string, unknown>): Promise<unknown> {
    const port = this.port;
    if (port === null) return Promise.reject(new BridgeError("unavailable", "no native port"));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      try {
        port.postMessage({ jsonrpc: "2.0", id, method, params });
      } catch (err) {
        this.pending.delete(id);
        this.log(`${method} not sent: ${err instanceof Error ? err.name : typeof err}`);
        reject(new BridgeError("unavailable", "native port not writable"));
      }
    });
  }

  private async profileId(): Promise<string> {
    const stored = await this.platform.storageGet(PROFILE_KEY);
    if (stored === undefined) {
      const id = this.platform.randomUUID();
      await this.platform.storageSet(PROFILE_KEY, id);
      return id;
    }
    if (typeof stored !== "string" || !UUID_RE.test(stored)) {
      // Never regenerate silently: a new id would unbind the index (profile_mismatch).
      throw new Error("storage.local profile_id is not a UUID");
    }
    return stored;
  }

  private async hello(): Promise<void> {
    if (this.helloInFlight) return;
    this.helloInFlight = true;
    this.state = "connecting";
    try {
      const hello: Hello = {
        protocol: PROTOCOL,
        minor: this.localMinor,
        role: "extension",
        version: this.platform.version,
        capabilities: this.capabilities,
        profile_id: await this.profileId(),
      };
      const reply = await this.send("hello", { ...hello });
      const theirs = daemonMinor(reply);
      if (theirs === null) {
        throw new BridgeError("invalid_params", "daemon hello without a valid minor");
      }
      this.negotiated = Math.min(this.localMinor, theirs);
      this.state = "ready";
      this.rejection = null;
    } catch (err) {
      if (!(err instanceof BridgeError)) throw err;
      if (err.code === "unavailable") {
        // ternd not reachable yet: the host sends bridge.reconnected when it is. If the port
        // itself is gone, stay disconnected (the alarm reconnects).
        this.state = this.port === null ? "disconnected" : "connecting";
      } else {
        this.state = "rejected";
        this.rejection = err;
        this.log(`hello rejected: ${err.code}`);
      }
      return;
    } finally {
      this.helloInFlight = false;
    }
    // The hello outcome is final here: a failed resync must not undo "ready".
    await this.clearReconnectAlarm();
    this.log("connected");
    try {
      this.onReady();
    } catch (err) {
      // E.g. a buffered notification hitting a dying port: the port's disconnect handles the
      // rest; resync still runs and connect() still resolves.
      this.log(`onReady failed: ${err instanceof Error ? err.name : typeof err}`);
    }
    await this.resync();
  }

  /** approvals.pending after a hello; a failure is logged and leaves the connection ready. */
  private async resync(): Promise<void> {
    let pending: unknown;
    try {
      pending = await this.request("approvals.pending");
    } catch (err) {
      if (!(err instanceof BridgeError)) throw err;
      this.log(`approvals resync failed: ${err.code}`);
      return;
    }
    this.onResync(pending);
  }

  private onPortMessage(port: NativePort, raw: unknown): void {
    if (port !== this.port) return;
    let msg: RpcMessage;
    try {
      msg = parseMessage(raw);
    } catch (err) {
      this.log(`malformed message dropped (${(err as Error).message})`);
      return;
    }
    if ("method" in msg && msg.method === "bridge.chunk") {
      try {
        const text = this.reassembler.feed(msg.params as unknown as Chunk);
        if (text === null) return;
        msg = parseMessage(JSON.parse(text));
      } catch (err) {
        // The lost message's id is unknown: fail every pending request now rather than leave
        // one waiting forever (requests have no timeout).
        this.log(`chunked message dropped (${(err as Error).message})`);
        this.failPending("a chunked message was lost");
        return;
      }
    }
    this.dispatch(port, msg);
  }

  private dispatch(port: NativePort, msg: RpcMessage): void {
    if ("result" in msg || "error" in msg) {
      const p = msg.id === null ? undefined : this.pending.get(msg.id);
      if (p === undefined) {
        this.log("response for an unknown request id");
        return;
      }
      this.pending.delete(msg.id as RpcId);
      if ("error" in msg) p.reject(BridgeError.fromRpc(msg.error));
      else p.resolve(msg.result);
      return;
    }
    if ("id" in msg) {
      void this.serve(port, msg.id, msg.method, msg.params);
      return;
    }
    if (msg.method === "bridge.reconnected") {
      this.state = "connecting";
      this.negotiated = 0;
      this.failPending("ternd restarted");
      this.onSessionEnd();
      void this.hello();
      return;
    }
    const h = this.notifications.get(msg.method);
    if (h === undefined) this.log(`notification not handled: ${msg.method}`);
    else h(msg.params);
  }

  private async serve(
    port: NativePort,
    id: RpcId,
    method: string,
    params: Record<string, unknown>,
  ): Promise<void> {
    const h = this.handlers.get(method);
    try {
      if (h === undefined) throw new BridgeError("method_not_available", `${method} not served`);
      port.postMessage({ jsonrpc: "2.0", id, result: await h(params) });
    } catch (err) {
      const e =
        err instanceof BridgeError ? err : new BridgeError("unavailable", "extension error");
      if (!(err instanceof BridgeError)) this.log(`handler ${method} failed: ${String(err)}`);
      port.postMessage({ jsonrpc: "2.0", id, error: e.toRpc() });
    }
  }

  private failPending(why: string): void {
    const failed = [...this.pending.values()];
    this.pending.clear();
    for (const p of failed) p.reject(new BridgeError("unavailable", why));
  }

  private onPortDisconnect(port: NativePort): void {
    if (port !== this.port) return;
    const wasReady = this.state === "ready";
    this.port = null;
    this.state = "disconnected";
    this.negotiated = 0;
    this.reassembler = new Reassembler();
    this.failPending("native port closed");
    this.onSessionEnd();
    this.log(`port closed${port.error ? `: ${port.error.message}` : ""}`);
    void this.armReconnectAlarm();
    if (wasReady) void this.connect();
  }

  /** The 1-minute backstop while unconnected (plan 02). Failures are logged, retried once. */
  private async armReconnectAlarm(): Promise<void> {
    for (let attempt = 1; attempt <= 2; attempt++) {
      let period: number | null;
      try {
        period = await this.platform.alarmCreate(RECONNECT_ALARM, RECONNECT_PERIOD_MIN);
      } catch (err) {
        this.log(`reconnect alarm create failed (attempt ${attempt}): ${(err as Error).message}`);
        continue;
      }
      if (period !== RECONNECT_PERIOD_MIN) {
        this.log(`reconnect alarm period is ${period}, asked for ${RECONNECT_PERIOD_MIN} min`);
      }
      return;
    }
    this.log("reconnect alarm unavailable: reconnecting only on Thunderbird startup or install");
  }

  private async clearReconnectAlarm(): Promise<void> {
    try {
      await this.platform.alarmClear(RECONNECT_ALARM);
    } catch (err) {
      // A stale alarm only causes a harmless connect() while ready (it is a no-op then).
      this.log(`reconnect alarm clear failed: ${(err as Error).message}`);
    }
  }
}

/**
 * Top-level listener registration (must run synchronously at module load: TB primes persisted
 * listeners and only starts the event page at app startup if one is runtime.onStartup, S1 F14).
 */
export function registerListeners(platform: Platform, bridge: Bridge): void {
  const connect = (): void => {
    void bridge.connect();
  };
  platform.onStartup(connect);
  platform.onInstalled(connect);
  platform.onAlarm((name) => {
    if (name === RECONNECT_ALARM) connect();
  });
}
