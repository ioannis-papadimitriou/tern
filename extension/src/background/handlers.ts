// R1 daemon→extension mail methods of tern-bridge/1.0 (architecture §3; plan 03 step 2).
//
// Maps bridge params → src/tb/api.ts calls → 1.0 result shapes. No messenger.* here.
// - Cursors: `next_cursor` is an opaque random token mapped in memory to TB's message-list id.
//   The table belongs to one bridge session: it is cleared, and the lists aborted, when the
//   session ends (port lost or ternd reconnected). An unknown token → `stale_id`. TB's list id
//   never leaves the extension (M8). At most MAX_OPEN_CURSORS lists stay open; the oldest is
//   aborted when a new one would exceed that. A cursor is tied to the folder it pages: used with
//   another folder_id it is `invalid_params` and stays valid. A page whose TB call spans a session
//   end aborts its list and is `stale_id`.
// - Dates must carry an explicit UTC offset (an offset-less one would be read as local time).
// - Order inside a page is TB's (messages.query cannot sort); the daemon picks date windows
//   newest-first (#16). A page can be empty and still carry a cursor (autoPaginationTimeout).
// - Errors: unknown tb_id or dead list → `stale_id`; any other TB rejection → `tb_error` with
//   a fixed per-method `tb_code`. TB's error text is never forwarded or logged (it can contain
//   folder names).
// - Folder counts: TB's getFolderInfo throws for a local folder whose summary (.msf) is not built
//   yet (a fresh mbox import, or a folder TB has not opened since its summary went). The frozen
//   1.0 `Folder` needs integer counts and allows no extra field, so such a folder is never
//   listed with made-up counts: its summary build is started with a messages.query on it (the
//   list aborted at once), and getFolderInfo is retried after each of FOLDER_INFO_RETRY_MS. A
//   folder still failing then, or one TB no longer finds (deleted since the tree was read), is
//   left out of that listing and counted in one log line; the rest of the list is served. ternd
//   never takes an unlisted folder for a deleted one without TB saying `folder_not_found` for
//   it (EventIngest.sync_folders).

import {
  type Account,
  BridgeError,
  type Capability,
  type Flags,
  type Folder,
  type FoundLocation,
  type FullMessage,
  type Page,
  type PageItem,
  RAW_MAX,
  type Raw,
  type Tag,
} from "../protocol";
import type { TbApi, TbFolder, TbFolderInfo, TbMessageHeader } from "../tb/api";
import { tbErrorKind } from "../tb/api";
import type { HandlerProvider, RequestHandler } from "./bridge";

export const MAX_OPEN_CURSORS = 64;
/** Waits before each getFolderInfo retry of a folder whose summary TB is still building. */
export const FOLDER_INFO_RETRY_MS: readonly number[] = [250, 1000, 3000];

export type Sleep = (ms: number) => Promise<void>;
const realSleep: Sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

type Params = Record<string, unknown>;

// -- param checks (the daemon validates too; a bad param here is a daemon bug) ------------------

function str(p: Params, key: string, opts: { optional: true }): string | undefined;
function str(p: Params, key: string): string;
function str(p: Params, key: string, opts?: { optional: true }): string | undefined {
  const v = p[key];
  if ((v === undefined || v === null) && opts?.optional) return undefined;
  if (typeof v !== "string" || v.length === 0) {
    throw new BridgeError("invalid_params", `${key}: expected a non-empty string`);
  }
  return v;
}

function tbId(p: Params): number {
  const v = p.tb_id;
  if (typeof v !== "number" || !Number.isSafeInteger(v) || v < 0) {
    throw new BridgeError("invalid_params", "tb_id: expected a non-negative integer");
  }
  return v;
}

// RFC 3339 date-time with an explicit offset. `new Date()` reads an offset-less string as local
// time, which would silently shift the window, so one is required (uppercase Z only, as ternd
// sends it).
const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/;

function date(p: Params, key: string): Date | undefined {
  const v = str(p, key, { optional: true });
  if (v === undefined) return undefined;
  const m = DATE_RE.exec(v);
  if (m === null)
    throw new BridgeError(
      "invalid_params",
      `${key}: expected an RFC 3339 date-time with a UTC offset`,
    );
  const d = new Date(v);
  // V8 rolls an impossible day over (Feb 30 → Mar 2) and accepts T24:00 as the next day's 00:00:
  // reject both instead of shifting the window (Python's datetime rejects them too).
  const [y, mo, day] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const [hh, mm, ss] = [Number(m[4]), Number(m[5]), Number(m[6] ?? "0")];
  const calendar = new Date(Date.UTC(y, mo - 1, day));
  if (
    Number.isNaN(d.getTime()) ||
    calendar.getUTCMonth() !== mo - 1 ||
    calendar.getUTCDate() !== day ||
    hh > 23 ||
    mm > 59 ||
    ss > 59
  ) {
    throw new BridgeError("invalid_params", `${key}: not a date`);
  }
  return d;
}

// -- result mapping ------------------------------------------------------------------------------

export function flagsOf(h: TbMessageHeader): Flags {
  return { read: h.read === true, flagged: h.flagged === true, junk: h.junk === true };
}

export function pageItemOf(h: TbMessageHeader): PageItem {
  return {
    tb_id: h.id,
    header_message_id: h.headerMessageId,
    date: h.date.toISOString(), // throws on an invalid date: a tb_error, never a made-up date
    size: h.size,
    flags: flagsOf(h),
    tags: [...h.tags],
    headers_only: h.headersOnly === true,
  };
}

function flatten(folders: TbFolder[] | undefined, out: TbFolder[] = []): TbFolder[] {
  for (const f of folders ?? []) {
    if (!(f.isVirtual || f.isTag || f.isUnified || f.isRoot)) out.push(f);
    flatten(f.subFolders, out);
  }
  return out;
}

function base64(bytes: Uint8Array): string {
  let bin = "";
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    bin += String.fromCharCode(...bytes.subarray(i, i + step));
  }
  return btoa(bin);
}

// -- handlers ------------------------------------------------------------------------------------

export class MailHandlers implements HandlerProvider {
  /** What these handlers let the daemon do (Bridge.register puts it into hello). */
  readonly capabilities: readonly Capability[] = ["mail.read"];

  // token → TB list id and the folder it pages, insertion-ordered
  private cursors = new Map<string, { listId: string; folderId: string }>();
  // Bumped by endSession(): a page() whose TB call spans a session end must not file its list
  // under the new session (no daemon holds that token).
  private generation = 0;

  constructor(
    private readonly api: TbApi,
    private readonly log: (msg: string) => void,
    private readonly newToken: () => string = () => crypto.randomUUID(),
    private readonly sleep: Sleep = realSleep,
  ) {}

  /** Method name → handler, for `Bridge.handlers`. */
  methods(): Map<string, RequestHandler> {
    const wrap =
      (method: string, tbCode: string, fn: (p: Params) => Promise<unknown>): RequestHandler =>
      async (p) => {
        try {
          return await fn(p);
        } catch (err) {
          throw this.toBridgeError(method, tbCode, err);
        }
      };
    return new Map<string, RequestHandler>([
      ["mail.accounts.list", wrap("mail.accounts.list", "accounts_failed", () => this.accounts())],
      ["mail.folders.list", wrap("mail.folders.list", "folders_failed", (p) => this.folders(p))],
      ["mail.messages.page", wrap("mail.messages.page", "page_failed", (p) => this.page(p))],
      ["mail.messages.raw", wrap("mail.messages.raw", "raw_failed", (p) => this.raw(p))],
      ["mail.messages.full", wrap("mail.messages.full", "full_failed", (p) => this.full(p))],
      ["mail.messages.find", wrap("mail.messages.find", "find_failed", (p) => this.find(p))],
      ["mail.open", wrap("mail.open", "open_failed", (p) => this.open(p))],
      ["mail.tags.list", wrap("mail.tags.list", "tags_failed", () => this.tags())],
    ]);
  }

  /** The bridge session ended: every cursor is now stale and its TB list is aborted. */
  async endSession(): Promise<void> {
    this.generation += 1;
    const lists = [...this.cursors.values()].map((c) => c.listId);
    this.cursors.clear();
    for (const id of lists) await this.abort(id);
  }

  get openCursors(): number {
    return this.cursors.size;
  }

  private toBridgeError(method: string, tbCode: string, err: unknown): BridgeError {
    if (err instanceof BridgeError) return err;
    const kind = tbErrorKind(err);
    if (kind === "message_not_found") {
      return new BridgeError("stale_id", "tb_id is not valid in this session");
    }
    if (kind === "list_not_found") return new BridgeError("stale_id", "cursor is no longer valid");
    const code = kind === "folder_not_found" ? "folder_not_found" : tbCode;
    this.log(`${method}: Thunderbird call failed (${code})`);
    return new BridgeError("tb_error", "Thunderbird call failed", code);
  }

  private async abort(listId: string): Promise<void> {
    try {
      await this.api.abortList(listId);
    } catch (err) {
      // Finished lists are already gone in TB; anything else is logged, by kind only.
      if (tbErrorKind(err) !== "list_not_found") this.log("abortList failed");
    }
  }

  private async remember(listId: string | null, folderId: string): Promise<string | undefined> {
    if (listId === null) return undefined;
    let evicted: string | undefined;
    if (this.cursors.size >= MAX_OPEN_CURSORS) {
      const [oldest] = this.cursors;
      if (oldest !== undefined) {
        this.cursors.delete(oldest[0]);
        evicted = oldest[1].listId;
      }
    }
    // Stored before any await, so an endSession() during the eviction below still aborts it.
    const token = this.newToken();
    this.cursors.set(token, { listId, folderId });
    if (evicted !== undefined) await this.abort(evicted);
    return token;
  }

  private async accounts(): Promise<Account[]> {
    const accounts = await this.api.listAccounts();
    return accounts.map((a) => ({
      account_id: a.id,
      name: a.name,
      type: a.type,
      identities: a.identities.map((i, n) => ({
        identity_id: i.id,
        email: i.email,
        name: i.name,
        is_default: n === 0,
      })),
    }));
  }

  private async folders(p: Params): Promise<Folder[]> {
    const account = await this.api.getAccountWithFolders(str(p, "account_id"));
    if (account === null) throw new BridgeError("invalid_params", "account_id: unknown account");
    const flat = flatten(account.rootFolder?.subFolders);
    const infos = new Map<string, TbFolderInfo>();
    let pending: TbFolder[] = [];
    let gone = 0;
    for (const f of flat) {
      const r = await this.folderInfo(f.id);
      if (r === "gone") gone += 1;
      else if (r === "failed") pending.push(f);
      else infos.set(f.id, r);
    }
    if (pending.length > 0) {
      await this.startSummaryBuilds(pending);
      for (const ms of FOLDER_INFO_RETRY_MS) {
        if (pending.length === 0) break;
        await this.sleep(ms);
        const still: TbFolder[] = [];
        for (const f of pending) {
          const r = await this.folderInfo(f.id);
          if (r === "gone") gone += 1;
          else if (r === "failed") still.push(f);
          else infos.set(f.id, r);
        }
        pending = still;
      }
    }
    if (pending.length > 0 || gone > 0) {
      // counts only: folder ids and names stay out of the log
      this.log(
        `mail.folders.list: ${pending.length} folder(s) left out, counts unavailable ` +
          `(folder_info_failed); ${gone} left out, no longer found (folder_not_found)`,
      );
    }
    const out: Folder[] = [];
    for (const f of flat) {
      const info = infos.get(f.id);
      if (info === undefined) continue;
      out.push({
        folder_id: f.id,
        path: f.path,
        name: f.name,
        special_use: [...(f.specialUse ?? [])],
        total: info.totalMessageCount,
        unread: info.unreadMessageCount,
      });
    }
    return out;
  }

  /** TB's counts for one folder; "gone" when TB no longer finds it, "failed" on any other
   * rejection (TB's text is not kept). */
  private async folderInfo(folderId: string): Promise<TbFolderInfo | "gone" | "failed"> {
    try {
      return await this.api.getFolderInfo(folderId);
    } catch (err) {
      return tbErrorKind(err) === "folder_not_found" ? "gone" : "failed";
    }
  }

  /** Start TB's summary build of each folder: a messages.query opens the folder's database
   * (the same query a page() makes); its list is aborted at once. A query that fails is
   * counted in the log only: the getFolderInfo retries decide the folder's fate. */
  private async startSummaryBuilds(folders: TbFolder[]): Promise<void> {
    let failed = 0;
    for (const f of folders) {
      let list: Awaited<ReturnType<TbApi["query"]>>;
      try {
        list = await this.api.query({ folderId: f.id });
      } catch {
        // counted and logged below; the retries decide (a folder TB lost turns "gone" there)
        failed += 1;
        continue;
      }
      if (list.id !== null) await this.abort(list.id);
    }
    if (failed > 0)
      this.log(`mail.folders.list: summary build query failed for ${failed} folder(s)`);
  }

  private async page(p: Params): Promise<Page> {
    const folderId = str(p, "folder_id");
    const fromDate = date(p, "from_date");
    const toDate = date(p, "to_date");
    const cursor = str(p, "cursor", { optional: true });
    const generation = this.generation;
    let list: Awaited<ReturnType<TbApi["query"]>>;
    if (cursor !== undefined) {
      const entry = this.cursors.get(cursor);
      if (entry === undefined) throw new BridgeError("stale_id", "cursor is not valid");
      // A mixed-up cursor would file one folder's messages under another. Nothing is consumed:
      // the token stays valid for its own folder.
      if (entry.folderId !== folderId) {
        throw new BridgeError("invalid_params", "cursor belongs to another folder");
      }
      this.cursors.delete(cursor);
      list = await this.api.continueList(entry.listId);
    } else {
      list = await this.api.query({
        folderId,
        ...(fromDate === undefined ? {} : { fromDate }),
        ...(toDate === undefined ? {} : { toDate }),
      });
    }
    if (this.generation !== generation) {
      // The session ended while TB answered: the cursor table now belongs to another daemon.
      if (list.id !== null) await this.abort(list.id);
      throw new BridgeError("stale_id", "the bridge session ended");
    }
    let items: PageItem[];
    try {
      items = list.messages.map(pageItemOf);
    } catch (err) {
      if (list.id !== null) await this.abort(list.id); // no cursor will ever reach it
      throw err;
    }
    const page: Page = { items };
    const next = await this.remember(list.id, folderId);
    if (next !== undefined) page.next_cursor = next;
    return page;
  }

  private async raw(p: Params): Promise<Raw> {
    const id = tbId(p);
    const header = await this.api.getMessage(id);
    if (header.size > RAW_MAX) throw new BridgeError("too_large", `message over ${RAW_MAX} bytes`);
    const bytes = await this.api.getRaw(id);
    // TB's size can differ from the stored bytes; the limit applies to what would be sent.
    if (bytes.length > RAW_MAX) throw new BridgeError("too_large", `message over ${RAW_MAX} bytes`);
    return { raw_b64: base64(bytes) };
  }

  private async full(p: Params): Promise<FullMessage> {
    const id = tbId(p);
    const part = await this.api.getFull(id);
    const texts = await this.api.listInlineTextParts(id);
    const attachments = await this.api.listAttachments(id);
    const headers: Record<string, string[]> = {};
    for (const [k, v] of Object.entries(part.headers ?? {})) headers[k] = [...v];
    return {
      headers,
      text_parts: texts.map((t) => ({ mime: t.contentType, text: t.content })),
      attachments: attachments.map((a) => ({ name: a.name, mime: a.contentType, size: a.size })),
    };
  }

  private async find(p: Params): Promise<FoundLocation[]> {
    const headerMessageId = str(p, "header_message_id");
    const out: FoundLocation[] = [];
    let list = await this.api.query({ headerMessageId });
    try {
      for (;;) {
        for (const h of list.messages) {
          if (h.folder === undefined) throw new Error("message header without folder");
          out.push({ tb_id: h.id, folder_id: h.folder.id, flags: flagsOf(h), tags: [...h.tags] });
        }
        if (list.id === null) return out;
        list = await this.api.continueList(list.id);
      }
    } catch (err) {
      if (list.id !== null) await this.abort(list.id);
      throw err;
    }
  }

  private async open(p: Params): Promise<Record<string, never>> {
    await this.api.openMessage(tbId(p));
    return {};
  }

  private async tags(): Promise<Tag[]> {
    const tags = await this.api.listTags();
    return tags.map((t) => ({ key: t.key, tag: t.tag, color: t.color }));
  }
}
