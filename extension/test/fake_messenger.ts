// A fake of the TB 153 mail APIs that src/tb/api.ts calls, with TB's paging and error texts
// (comm-esr153 parent/ext-messages.js, ExtensionMessages.sys.mjs, ExtensionAccounts.sys.mjs).
// Synthetic data only: reserved domains (#11).

import type {
  MessengerMail,
  TbAccount,
  TbAttachment,
  TbFolder,
  TbFolderInfo,
  TbInlineTextPart,
  TbMessageHeader,
  TbMessageList,
  TbQuery,
  TbTag,
} from "../src/tb/api";

export interface FakeMsg {
  id: number;
  headerMessageId: string;
  date: Date;
  size: number;
  read: boolean;
  flagged: boolean;
  junk: boolean;
  tags: string[];
  headersOnly: boolean;
  folderId: string;
  raw: Uint8Array;
  headers: Record<string, string[]>;
  textParts: TbInlineTextPart[];
  attachments: TbAttachment[];
}

/** A TB event: listeners and the extra arguments they were registered with. */
export class FakeEvent<A extends unknown[]> {
  listeners: { cb: (...args: A) => void; extra: unknown[] }[] = [];
  addListener(cb: (...args: A) => void, ...extra: unknown[]): void {
    this.listeners.push({ cb, extra });
  }
  emit(...args: A): void {
    for (const l of this.listeners) l.cb(...args);
  }
}

const notFound = (id: number): Error => new Error(`Message not found: ${id}.`);
const noList = (id: string): Error =>
  new Error(`No message list for id ${id}. Have you reached the end of a list?`);

export function folder(
  accountId: string,
  path: string,
  extra: Partial<TbFolder> = {},
  subFolders: TbFolder[] = [],
): TbFolder {
  const name = path.split("/").at(-1) ?? path;
  return { id: `${accountId}:/${path}`, accountId, name, path, subFolders, ...extra };
}

export class FakeMessenger implements MessengerMail {
  accountsData: TbAccount[] = [];
  msgs: FakeMsg[] = [];
  tagsData: TbTag[] = [];
  lists = new Map<string, TbMessageHeader[]>();
  aborted: string[] = [];
  opened: unknown[] = [];
  queries: (TbQuery & { messagesPerPage?: number })[] = [];
  /** method name → error the next call of it throws */
  failNext = new Map<string, Error>();
  /** Local folders whose summary (.msf) is not built: getFolderInfo throws TB's generic error
   * until a messages.query on the folder builds it (TB opens the folder's database). */
  unbuiltSummaries = new Set<string>();
  /** Folders whose getFolderInfo always throws TB's generic error, queried or not. */
  brokenFolderInfo = new Set<string>();
  getFolderInfoCalls: string[] = [];
  getRawCalls = 0;
  private listSeq = 0;

  private maybeFail(name: string): void {
    const err = this.failNext.get(name);
    if (err !== undefined) {
      this.failNext.delete(name);
      throw err;
    }
  }

  private header(m: FakeMsg): TbMessageHeader {
    const f = this.findFolder(m.folderId);
    return {
      id: m.id,
      headerMessageId: m.headerMessageId,
      date: new Date(m.date.getTime()),
      size: m.size,
      read: m.read,
      flagged: m.flagged,
      junk: m.junk,
      tags: [...m.tags],
      headersOnly: m.headersOnly,
      ...(f === undefined ? {} : { folder: { ...f, subFolders: [] } }),
    };
  }

  private allFolders(): TbFolder[] {
    const out: TbFolder[] = [];
    const walk = (fs: TbFolder[] | undefined): void => {
      for (const f of fs ?? []) {
        out.push(f);
        walk(f.subFolders);
      }
    };
    for (const a of this.accountsData) walk(a.rootFolder ? [a.rootFolder] : []);
    return out;
  }

  private findFolder(id: string): TbFolder | undefined {
    return this.allFolders().find((f) => f.id === id);
  }

  private msg(id: number): FakeMsg {
    const m = this.msgs.find((x) => x.id === id);
    if (m === undefined) throw notFound(id);
    return m;
  }

  private page(all: TbMessageHeader[], size: number): TbMessageList {
    const n = Math.min(size, this.cutAfter ?? size);
    const first = all.slice(0, n);
    const rest = all.slice(n);
    if (rest.length === 0) return { id: null, messages: first };
    this.listSeq += 1;
    const id = `list-${this.listSeq}`;
    this.lists.set(id, rest);
    return { id, messages: first };
  }

  pageSize = 100;
  /** TB's autoPaginationTimeout: a page closes after this many items, whatever messagesPerPage. */
  cutAfter: number | null = null;

  accounts = {
    list: async (includeSubFolders?: boolean): Promise<TbAccount[]> => {
      this.maybeFail("accounts.list");
      return this.accountsData.map((a) => {
        if (includeSubFolders) return a;
        const { rootFolder: _root, ...rest } = a;
        return rest;
      });
    },
    get: async (accountId: string, _includeSubFolders?: boolean): Promise<TbAccount | null> => {
      this.maybeFail("accounts.get");
      return this.accountsData.find((a) => a.id === accountId) ?? null;
    },
  };

  folders = {
    onCreated: new FakeEvent<[TbFolder]>(),
    onRenamed: new FakeEvent<[TbFolder, TbFolder]>(),
    onMoved: new FakeEvent<[TbFolder, TbFolder]>(),
    onCopied: new FakeEvent<[TbFolder, TbFolder]>(),
    onDeleted: new FakeEvent<[TbFolder]>(),
    onUpdated: new FakeEvent<[TbFolder, TbFolder]>(),
    onFolderInfoChanged: new FakeEvent<[TbFolder, TbFolderInfo]>(),
    getFolderInfo: async (folderId: string): Promise<TbFolderInfo> => {
      this.maybeFail("folders.getFolderInfo");
      this.getFolderInfoCalls.push(folderId);
      if (this.findFolder(folderId) === undefined) throw new Error(`Folder not found: ${folderId}`);
      if (this.unbuiltSummaries.has(folderId) || this.brokenFolderInfo.has(folderId)) {
        throw new Error("An unexpected error occurred");
      }
      const inFolder = this.msgs.filter((m) => m.folderId === folderId);
      return {
        totalMessageCount: inFolder.length,
        unreadMessageCount: inFolder.filter((m) => !m.read).length,
      };
    },
  };

  messages = {
    query: async (q: TbQuery & { messagesPerPage?: number }): Promise<TbMessageList> => {
      this.maybeFail("messages.query");
      this.queries.push(q);
      if (q.folderId !== undefined && this.findFolder(q.folderId) === undefined) {
        throw new Error(`Folder not found: ${q.folderId}`);
      }
      if (q.folderId !== undefined) this.unbuiltSummaries.delete(q.folderId);
      const hits = this.msgs.filter(
        (m) =>
          (q.folderId === undefined || m.folderId === q.folderId) &&
          (q.headerMessageId === undefined || m.headerMessageId === q.headerMessageId) &&
          (q.fromDate === undefined || m.date.getTime() >= q.fromDate.getTime()) &&
          (q.toDate === undefined || m.date.getTime() <= q.toDate.getTime()),
      );
      return this.page(
        hits.map((m) => this.header(m)),
        q.messagesPerPage ?? this.pageSize,
      );
    },
    continueList: async (messageListId: string): Promise<TbMessageList> => {
      this.maybeFail("messages.continueList");
      const rest = this.lists.get(messageListId);
      if (rest === undefined) throw noList(messageListId);
      this.lists.delete(messageListId);
      const next = this.page(rest, this.pageSize);
      return next;
    },
    abortList: async (messageListId: string): Promise<void> => {
      if (!this.lists.delete(messageListId)) throw noList(messageListId);
      this.aborted.push(messageListId);
    },
    get: async (messageId: number): Promise<TbMessageHeader> => {
      this.maybeFail("messages.get");
      return this.header(this.msg(messageId));
    },
    getRaw: async (messageId: number): Promise<{ arrayBuffer(): Promise<ArrayBuffer> }> => {
      this.getRawCalls += 1;
      this.maybeFail("messages.getRaw");
      const raw = this.msg(messageId).raw;
      return { arrayBuffer: async () => raw.slice().buffer };
    },
    getFull: async (messageId: number) => {
      this.maybeFail("messages.getFull");
      return { headers: this.msg(messageId).headers };
    },
    listInlineTextParts: async (messageId: number): Promise<TbInlineTextPart[]> =>
      this.msg(messageId).textParts,
    listAttachments: async (messageId: number): Promise<TbAttachment[]> =>
      this.msg(messageId).attachments,
    tags: {
      list: async (): Promise<TbTag[]> => {
        this.maybeFail("messages.tags.list");
        return this.tagsData;
      },
    },
    onNewMailReceived: new FakeEvent<[TbFolder, TbMessageList]>(),
    onMoved: new FakeEvent<[TbMessageList, TbMessageList]>(),
    onCopied: new FakeEvent<[TbMessageList, TbMessageList]>(),
    onDeleted: new FakeEvent<[TbMessageList]>(),
    onUpdated: new FakeEvent<[TbMessageHeader, Record<string, unknown>, Record<string, unknown>]>(),
  };

  /** A header as TB would pass it in an event (public for event tests). */
  headerOf(id: number): TbMessageHeader {
    return this.header(this.msg(id));
  }

  /** A MessageList for an event, paged like TB's (pageSize per page). */
  listOf(headers: TbMessageHeader[]): TbMessageList {
    return this.page(headers, this.pageSize);
  }

  messageDisplay = {
    open: async (p: { messageId: number; location: "tab" }): Promise<unknown> => {
      this.msg(p.messageId);
      this.opened.push(p);
      return {};
    },
  };
}

const enc = new TextEncoder();

/** A small synthetic mailbox: one account, nested folders, a virtual and a tag folder. */
export function syntheticMessenger(inboxCount = 250): FakeMessenger {
  const fm = new FakeMessenger();
  const acc = "account1";
  const inbox = folder(acc, "INBOX", { specialUse: ["inbox"] });
  const year = folder(acc, "Archive/2026");
  const archive = folder(acc, "Archive", { specialUse: ["archives"] }, [year]);
  const virtual = folder(acc, "Unread", { isVirtual: true });
  const tagFolder = folder(acc, "Tags/Important", { isTag: true });
  const root = folder(acc, "", { isRoot: true, name: "Root" }, [
    inbox,
    archive,
    virtual,
    tagFolder,
  ]);
  fm.accountsData = [
    {
      id: acc,
      name: "alex@example.com",
      type: "imap",
      identities: [
        { id: "id1", email: "alex@example.com", name: "Alex Example" },
        { id: "id2", email: "alex.work@example.org", name: "Alex (work)" },
      ],
      rootFolder: root,
    },
    {
      id: "account2",
      name: "Local Folders",
      type: "local",
      identities: [],
      rootFolder: folder("account2", "", { isRoot: true }),
    },
  ];
  fm.tagsData = [
    { key: "$label1", tag: "Important", color: "#FF0000" },
    { key: "invoice", tag: "invoice", color: "#3366CC" },
  ];
  const base = Date.UTC(2026, 9, 1);
  let id = 0;
  const add = (folderId: string, n: number, extra: Partial<FakeMsg> = {}): void => {
    for (let i = 0; i < n; i++) {
      id += 1;
      const raw = enc.encode(
        `Message-ID: <m${id}@example.com>\r\nFrom: sender${id}@example.com\r\nSubject: synthetic ${id}\r\n\r\nbody ${id}\r\n`,
      );
      fm.msgs.push({
        id,
        headerMessageId: `m${id}@example.com`,
        date: new Date(base + id * 3_600_000),
        size: raw.length,
        read: id % 3 === 0,
        flagged: id % 7 === 0,
        junk: false,
        tags: id % 5 === 0 ? ["invoice"] : [],
        headersOnly: false,
        folderId,
        raw,
        headers: { "message-id": [`<m${id}@example.com>`], subject: [`synthetic ${id}`] },
        textParts: [{ contentType: "text/plain", content: `body ${id}` }],
        attachments: [],
        ...extra,
      });
    }
  };
  add(inbox.id, inboxCount);
  add(year.id, 3);
  add(archive.id, 2, { headersOnly: true });
  return fm;
}

/** A message of a golden transcript's `tb` block (tests/golden/bridge/mail_read.jsonl). */
export interface GoldenTbMessage {
  id: number;
  folderId: string;
  headerMessageId: string;
  date: string;
  size: number;
  read: boolean;
  flagged: boolean;
  junk: boolean;
  tags: string[];
  headersOnly: boolean;
  raw_b64?: string;
  headers?: Record<string, string[]>;
  textParts?: TbInlineTextPart[];
  attachments?: TbAttachment[];
}

/** A golden transcript's `tb` block: a synthetic profile as TB's APIs show it. */
export interface GoldenTb {
  page_size: number;
  accounts: TbAccount[];
  messages: GoldenTbMessage[];
  tags: TbTag[];
}

/** The messenger a golden's `tb` block describes. Parts it leaves out stay empty. */
export function messengerFromGolden(tb: GoldenTb): FakeMessenger {
  const fm = new FakeMessenger();
  fm.cutAfter = tb.page_size;
  fm.accountsData = structuredClone(tb.accounts);
  fm.tagsData = structuredClone(tb.tags);
  fm.msgs = tb.messages.map((m) => {
    const date = new Date(m.date);
    if (Number.isNaN(date.getTime())) throw new Error(`tb message ${m.id}: bad date`);
    const bin = atob(m.raw_b64 ?? "");
    const raw = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) raw[i] = bin.charCodeAt(i);
    return {
      id: m.id,
      headerMessageId: m.headerMessageId,
      date,
      size: m.size,
      read: m.read,
      flagged: m.flagged,
      junk: m.junk,
      tags: [...m.tags],
      headersOnly: m.headersOnly,
      folderId: m.folderId,
      raw,
      headers: structuredClone(m.headers ?? {}),
      textParts: structuredClone(m.textParts ?? []),
      attachments: structuredClone(m.attachments ?? []),
    };
  });
  return fm;
}
