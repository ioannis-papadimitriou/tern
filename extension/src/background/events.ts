// TB mail and folder events → tern-bridge/1.0 notifications (architecture §3; plan 03 step 3).
//
// - onNewMailReceived (all folders) and onCopied (destination) → event.mail.new
// - onMoved → one event.mail.moved per (source folder, destination folder) pair
// - onDeleted (permanent deletes; a move to Trash is onMoved) → event.mail.deleted
// - onUpdated → event.mail.updated
// - every folder event → event.folder.changed, for the old and the new id on rename/move
//   (virtual, tag and unified folders are skipped, as in mail.folders.list)
//
// Order: events are applied in arrival order; an event whose message lists are still draining
// holds back the events that arrived after it. Batching: one trailing debounce (DEBOUNCE_MS
// after the last event, at most MAX_WAIT_MS after the first) collects events into batches,
// merged by tb_id (latest wins). An event merges into an earlier batch of the same kind and
// folders only if no batch opened after that one touches any of its folders; otherwise it opens
// a new batch, so the daemon sees the changes to each folder in causal order. A flush sends the
// batches in order, each as notifications of at most MAX_BUFFERED_ITEMS items (same method and
// folders), and a sent notification leaves the buffer at once, so it is never sent twice. Over
// MAX_BUFFERED_ITEMS buffered items, a flush is tried at once; only what stays buffered because
// the bridge is not ready is trimmed, oldest first, and only the dropped count is logged (the
// daemon's reconcile repairs what was dropped, #16). Nothing here logs a subject, address,
// folder name or id.

import type {
  FolderChanged,
  MailDeleted,
  MailMoved,
  MailNew,
  MailUpdated,
  PageItem,
  UpdatedItem,
} from "../protocol";
import type { TbApi, TbEvents, TbFolder, TbMessageHeader, TbMessageList } from "../tb/api";
import { flagsOf, pageItemOf } from "./handlers";

export const DEBOUNCE_MS = 500;
export const MAX_WAIT_MS = 2000;
export const MAX_BUFFERED_ITEMS = 1000;

/** Sends one notification; false if the bridge cannot send now (the batch stays buffered). */
export type Send = (method: string, params: Record<string, unknown>) => boolean;

export interface Timers {
  set(cb: () => void, ms: number): unknown;
  clear(handle: unknown): void;
  now(): number;
}

const realTimers: Timers = {
  set: (cb, ms) => setTimeout(cb, ms),
  clear: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  now: () => Date.now(),
};

type Body =
  | { method: "event.mail.new"; folder_id: string; items: Map<number, PageItem> }
  | {
      method: "event.mail.moved";
      src_folder_id: string;
      dst_folder_id: string;
      items: Map<number, PageItem>;
    }
  | { method: "event.mail.deleted"; folder_id: string; ids: Set<string> }
  | { method: "event.mail.updated"; folder_id: string; items: Map<number, UpdatedItem> }
  | { method: "event.folder.changed"; folder_id: string };

/** A pending notification: its merge key and every folder it touches. */
type Batch = Body & { key: string; folders: readonly string[] };

type Drained<T> = { ok: true; value: T } | { ok: false; err: unknown };

function size(b: Batch): number {
  if (b.method === "event.folder.changed") return 1;
  if (b.method === "event.mail.deleted") return b.ids.size;
  return b.items.size;
}

/** The first n values of an iterable. */
function firstN<T>(values: Iterable<T>, n: number): T[] {
  const out: T[] = [];
  for (const v of values) {
    if (out.length >= n) break;
    out.push(v);
  }
  return out;
}

function skipFolder(f: TbFolder): boolean {
  return f.isVirtual === true || f.isTag === true || f.isUnified === true;
}

export class EventForwarder {
  private batches: Batch[] = []; // in send order
  private timer: unknown = null;
  private firstAt = 0;
  private dropped = 0;
  private tail: Promise<void> = Promise.resolve(); // applies events in arrival order
  private waiting = 0; // events received but not applied yet

  constructor(
    private readonly api: Pick<TbApi, "continueList">,
    private readonly send: Send,
    private readonly log: (msg: string) => void,
    private readonly timers: Timers = realTimers,
  ) {}

  /** Register every TB listener. Call synchronously at module load (S1 F14). */
  register(events: TbEvents): void {
    events.onNewMail((folder, list) => this.onNew(folder.id, list));
    events.onCopied((_orig, copied) => this.onCopied(copied));
    events.onMoved((orig, moved) => this.onMoved(orig, moved));
    events.onDeleted((list) => this.onDeleted(list));
    events.onUpdated((msg) => this.onUpdated(msg));
    events.onFolderChanged((folders) => this.onFolders(folders));
  }

  get bufferedItems(): number {
    let n = 0;
    for (const b of this.batches) n += size(b);
    return n;
  }

  /** Send every batch now (timer expiry, or the bridge just became ready). */
  flush(): void {
    if (this.timer !== null) {
      this.timers.clear(this.timer);
      this.timer = null;
    }
    for (let b = this.batches[0]; b !== undefined; b = this.batches[0]) {
      // Not connected: keep this and the later batches (order matters); the next hello flushes.
      if (!this.sendBatch(b)) break;
      this.batches.shift();
    }
    this.reportDropped();
  }

  // -- arrival order --------------------------------------------------------------------------

  /** Apply an event once its lists are drained, after every event that arrived before it. */
  private later<T>(what: string, data: Promise<T>, apply: (value: T) => void): void {
    // Settle at once, so a rejected drain is never an unhandled rejection while it waits.
    const drained: Promise<Drained<T>> = data.then(
      (value) => ({ ok: true, value }),
      (err: unknown) => ({ ok: false, err }),
    );
    this.waiting += 1;
    const step = async (): Promise<void> => {
      try {
        const r = await drained;
        if (r.ok) this.apply(what, () => apply(r.value));
        else this.lost(what, r.err);
      } finally {
        this.waiting -= 1;
      }
    };
    // A link that fails (only if logging itself throws) is logged, and the next link runs
    // whatever the previous one did, so one failure never wedges every later event. The step's
    // onRejected argument is that already-reported link failure.
    this.tail = this.tail.then(step, step).catch((err: unknown) => this.lost(what, err));
  }

  /** A synchronous event: applied now, unless an earlier event is still draining. */
  private now(what: string, apply: () => void): void {
    if (this.waiting === 0) this.apply(what, apply);
    else this.later(what, Promise.resolve(), apply);
  }

  private apply(what: string, fn: () => void): void {
    try {
      fn();
    } catch (err) {
      this.lost(what, err);
    }
  }

  private lost(what: string, err: unknown): void {
    // A listener must not throw or reject unobserved; the event is lost and reconcile repairs it.
    this.log(`${what} event lost: ${err instanceof Error ? err.name : typeof err}`);
  }

  // -- TB event handlers ------------------------------------------------------------------------

  private async drain(list: TbMessageList): Promise<TbMessageHeader[]> {
    const out = [...list.messages];
    let id = list.id;
    while (id !== null) {
      const next = await this.api.continueList(id);
      out.push(...next.messages);
      id = next.id;
    }
    return out;
  }

  /** Map headers to items; one TB cannot date is dropped and counted, never given a made-up date. */
  private items(headers: TbMessageHeader[]): PageItem[] {
    const out: PageItem[] = [];
    for (const h of headers) {
      if (Number.isNaN(h.date.getTime())) {
        this.dropped += 1;
        continue;
      }
      out.push(pageItemOf(h)); // any other failure loses the whole event (logged by apply)
    }
    return out;
  }

  private onNew(folderId: string, list: TbMessageList): void {
    this.later("new mail", this.drain(list), (headers) => {
      this.addNew([[folderId, this.items(headers)]]);
      this.changed();
    });
  }

  private onCopied(copied: TbMessageList): void {
    this.later("copy", this.drain(copied), (headers) => {
      const added: [string, PageItem[]][] = [];
      for (const h of headers) {
        if (h.folder === undefined) continue;
        added.push([h.folder.id, this.items([h])]);
      }
      this.addNew(added);
      this.changed();
    });
  }

  private addNew(added: [string, PageItem[]][]): void {
    for (const [folderId, items] of added) {
      if (items.length === 0) continue;
      const b = this.batch(`new|${folderId}`, [folderId], () => ({
        method: "event.mail.new",
        folder_id: folderId,
        items: new Map(),
      }));
      if (b.method !== "event.mail.new") continue;
      for (const it of items) b.items.set(it.tb_id, it);
    }
  }

  private onMoved(original: TbMessageList, moved: TbMessageList): void {
    const lists = (async () => [await this.drain(original), await this.drain(moved)] as const)();
    this.later("move", lists, ([src, dst]) => {
      // TB passes both lists in the same order: original[i] became moved[i].
      // One TB move is one user action over distinct messages, so its items are grouped per
      // (source, destination) pair first, in first-seen order: interleaved sources (a move from a
      // search view: A,B,A,B → X) must not open one batch per message (batch() would, because
      // consecutive pairs share X).
      const pairs = new Map<string, { from: string; to: string; items: PageItem[] }>();
      dst.forEach((h, i) => {
        const from = src[i]?.folder?.id;
        const to = h.folder?.id;
        if (from === undefined || to === undefined) {
          this.dropped += 1;
          return;
        }
        const items = this.items([h]);
        if (items.length === 0) return;
        const key = `${from}|${to}`;
        let pair = pairs.get(key);
        if (pair === undefined) {
          pair = { from, to, items: [] };
          pairs.set(key, pair);
        }
        pair.items.push(...items);
      });
      for (const { from, to, items } of pairs.values()) {
        const b = this.batch(`moved|${from}|${to}`, [from, to], () => ({
          method: "event.mail.moved",
          src_folder_id: from,
          dst_folder_id: to,
          items: new Map(),
        }));
        if (b.method === "event.mail.moved") for (const it of items) b.items.set(it.tb_id, it);
      }
      this.changed();
    });
  }

  private onDeleted(list: TbMessageList): void {
    this.later("delete", this.drain(list), (headers) => {
      for (const h of headers) {
        const folderId = h.folder?.id;
        if (folderId === undefined) {
          this.dropped += 1;
          continue;
        }
        const b = this.batch(`deleted|${folderId}`, [folderId], () => ({
          method: "event.mail.deleted",
          folder_id: folderId,
          ids: new Set(),
        }));
        if (b.method === "event.mail.deleted") b.ids.add(h.headerMessageId);
      }
      this.changed();
    });
  }

  private onUpdated(h: TbMessageHeader): void {
    this.now("update", () => {
      const folderId = h.folder?.id;
      if (folderId === undefined) {
        this.dropped += 1;
      } else {
        const item: UpdatedItem = {
          tb_id: h.id,
          folder_id: folderId,
          header_message_id: h.headerMessageId,
          flags: flagsOf(h),
          tags: [...h.tags],
        };
        const b = this.batch(`updated|${folderId}`, [folderId], () => ({
          method: "event.mail.updated",
          folder_id: folderId,
          items: new Map(),
        }));
        if (b.method === "event.mail.updated") b.items.set(h.id, item);
      }
      this.changed();
    });
  }

  private onFolders(folders: TbFolder[]): void {
    this.now("folder", () => {
      for (const f of folders) {
        if (skipFolder(f)) continue;
        this.batch(`folder|${f.id}`, [f.id], () => ({
          method: "event.folder.changed",
          folder_id: f.id,
        }));
      }
      this.changed();
    });
  }

  // -- batching -------------------------------------------------------------------------------

  /**
   * The batch an event with this key and these folders goes into: the latest batch with the same
   * key, unless a later batch touches one of the folders (then a new batch, to keep causal order).
   */
  private batch(key: string, folders: readonly string[], make: () => Body): Batch {
    for (let i = this.batches.length - 1; i >= 0; i--) {
      const b = this.batches[i];
      if (b === undefined || !b.folders.some((f) => folders.includes(f))) continue;
      if (b.key === key) return b;
      break;
    }
    const b: Batch = { ...make(), key, folders };
    this.batches.push(b);
    return b;
  }

  /** Enforce the buffer bound, then (re)arm the debounce. */
  private changed(): void {
    if (this.bufferedItems > MAX_BUFFERED_ITEMS) {
      this.flush(); // sends everything if the bridge is ready
      this.trim(this.bufferedItems - MAX_BUFFERED_ITEMS);
    }
    if (this.batches.length === 0) {
      this.reportDropped();
      return;
    }
    const now = this.timers.now();
    if (this.timer === null) this.firstAt = now;
    else this.timers.clear(this.timer);
    const wait = Math.max(0, Math.min(DEBOUNCE_MS, this.firstAt + MAX_WAIT_MS - now));
    this.timer = this.timers.set(() => {
      this.timer = null;
      this.flush();
    }, wait);
  }

  /** Drop the `over` oldest buffered items (the bridge is not ready to take them). */
  private trim(over: number): void {
    const gone = new Set<Batch>();
    for (const b of this.batches) {
      if (over <= 0) break;
      if (b.method === "event.folder.changed") {
        gone.add(b);
        over -= 1;
        this.dropped += 1;
        continue;
      }
      const coll: Map<unknown, unknown> | Set<unknown> =
        b.method === "event.mail.deleted" ? b.ids : b.items;
      for (const k of [...coll.keys()]) {
        if (over <= 0) break;
        coll.delete(k);
        over -= 1;
        this.dropped += 1;
      }
    }
    this.batches = this.batches.filter((b) => !gone.has(b) && size(b) > 0);
  }

  private reportDropped(): void {
    if (this.dropped > 0 && this.batches.length === 0) {
      this.log(`${this.dropped} event items dropped (reconcile repairs them)`);
      this.dropped = 0;
    }
  }

  /**
   * Send one batch as notifications of at most MAX_BUFFERED_ITEMS items; each sent chunk leaves
   * the batch at once. False if the bridge cannot send now (the rest stays buffered).
   */
  private sendBatch(b: Batch): boolean {
    for (;;) {
      const chunk = this.nextChunk(b);
      if (!this.send(b.method, chunk.params)) return false;
      chunk.sent();
      if (b.method === "event.folder.changed" || size(b) === 0) return true;
    }
  }

  private nextChunk(b: Batch): { params: Record<string, unknown>; sent: () => void } {
    const n = MAX_BUFFERED_ITEMS;
    switch (b.method) {
      case "event.mail.new": {
        const e = firstN(b.items.entries(), n);
        return {
          params: { folder_id: b.folder_id, items: e.map(([, v]) => v) } satisfies MailNew,
          sent: () => {
            for (const [k] of e) b.items.delete(k);
          },
        };
      }
      case "event.mail.moved": {
        const e = firstN(b.items.entries(), n);
        return {
          params: {
            src_folder_id: b.src_folder_id,
            dst_folder_id: b.dst_folder_id,
            items: e.map(([, v]) => v),
          } satisfies MailMoved,
          sent: () => {
            for (const [k] of e) b.items.delete(k);
          },
        };
      }
      case "event.mail.deleted": {
        const ids = firstN(b.ids, n);
        return {
          params: { folder_id: b.folder_id, header_message_ids: ids } satisfies MailDeleted,
          sent: () => {
            for (const id of ids) b.ids.delete(id);
          },
        };
      }
      case "event.mail.updated": {
        const e = firstN(b.items.entries(), n);
        return {
          params: { items: e.map(([, v]) => v) } satisfies MailUpdated,
          sent: () => {
            for (const [k] of e) b.items.delete(k);
          },
        };
      }
      case "event.folder.changed":
        // One item: sendBatch drops the whole batch once it is sent.
        return {
          params: { folder_id: b.folder_id } satisfies FolderChanged,
          sent: () => undefined,
        };
    }
  }
}
