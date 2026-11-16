// The ONLY module that calls Thunderbird's mail APIs (CLAUDE.md; A-15: the one file to port on an
// ESR upgrade). One function per TB call; values come back as TB returns them, with no protocol
// logic. Facts (TB 153 ESR, comm-esr153 schemas and parent/ext-*.js; plans/03-tb-adapter.md):
// - messages.query has no sort option; messagesPerPage defaults to 100 and
//   autoPaginationTimeout to 1000 ms, so a page can be empty and still continue (S1 NOTES F3).
// - getRaw returns a DOM File in MV3 (F4).
// - Unknown message ids throw `Message not found: <id>.`; a finished or unknown list throws
//   `No message list for id <id>. ...`; unknown folders throw `Folder not found: <id>`.
//   Other TB error texts can contain folder names, so callers never forward them.
// - messages.tags.list needs the messagesTagsList permission; the rest needs messagesRead and
//   accountsRead.
// - MailAccount.identities lists the default identity first.
// It also holds chatPagePort(), the chat tab page's runtime port (plan 08b A5): the tab page's
// code names `messenger` nowhere else (extension/test/api_boundary.test.ts).

import type { RuntimePort } from "../background/platform";

export interface TbIdentity {
  id: string;
  email: string;
  name: string;
}

export interface TbFolder {
  id: string;
  accountId?: string;
  name: string;
  path: string;
  specialUse?: string[];
  isRoot?: boolean;
  isTag?: boolean;
  isUnified?: boolean;
  isVirtual?: boolean;
  subFolders?: TbFolder[];
}

export interface TbAccount {
  id: string;
  name: string;
  type: string;
  identities: TbIdentity[];
  rootFolder?: TbFolder;
}

export interface TbMessageHeader {
  id: number;
  headerMessageId: string;
  date: Date;
  size: number;
  read?: boolean;
  flagged: boolean;
  junk: boolean;
  tags: string[];
  headersOnly: boolean;
  folder?: TbFolder;
}

export interface TbMessageList {
  id: string | null;
  messages: TbMessageHeader[];
}

export interface TbFolderInfo {
  totalMessageCount: number;
  unreadMessageCount: number;
}

export interface TbMessagePart {
  headers?: Record<string, string[]>;
}

export interface TbInlineTextPart {
  contentType: string;
  content: string;
}

export interface TbAttachment {
  name: string;
  contentType: string;
  size: number;
}

export interface TbTag {
  key: string;
  tag: string;
  color: string;
}

export interface TbQuery {
  folderId?: string;
  fromDate?: Date;
  toDate?: Date;
  headerMessageId?: string;
}

interface Listener<A extends unknown[]> {
  addListener(cb: (...args: A) => void): void;
}

/** Structural type of the `messenger` mail namespaces used here (TB 153 MV3). */
export interface MessengerMail {
  accounts: {
    list(includeSubFolders?: boolean): Promise<TbAccount[]>;
    get(accountId: string, includeSubFolders?: boolean): Promise<TbAccount | null>;
  };
  folders: {
    getFolderInfo(folderId: string): Promise<TbFolderInfo>;
    onCreated: Listener<[TbFolder]>;
    onRenamed: Listener<[TbFolder, TbFolder]>;
    onMoved: Listener<[TbFolder, TbFolder]>;
    onCopied: Listener<[TbFolder, TbFolder]>;
    onDeleted: Listener<[TbFolder]>;
    onUpdated: Listener<[TbFolder, TbFolder]>;
    onFolderInfoChanged: Listener<[TbFolder, TbFolderInfo]>;
  };
  messages: {
    query(queryInfo: TbQuery & { messagesPerPage?: number }): Promise<TbMessageList>;
    continueList(messageListId: string): Promise<TbMessageList>;
    abortList(messageListId: string): Promise<void>;
    get(messageId: number): Promise<TbMessageHeader>;
    getRaw(messageId: number): Promise<{ arrayBuffer(): Promise<ArrayBuffer> }>;
    getFull(messageId: number): Promise<TbMessagePart>;
    listInlineTextParts(messageId: number): Promise<TbInlineTextPart[]>;
    listAttachments(messageId: number): Promise<TbAttachment[]>;
    tags: { list(): Promise<TbTag[]> };
    onNewMailReceived: {
      addListener(
        cb: (folder: TbFolder, messages: TbMessageList) => void,
        monitorAllFolders: boolean,
      ): void;
    };
    onMoved: Listener<[TbMessageList, TbMessageList]>;
    onCopied: Listener<[TbMessageList, TbMessageList]>;
    onDeleted: Listener<[TbMessageList]>;
    onUpdated: Listener<[TbMessageHeader, Record<string, unknown>, Record<string, unknown>]>;
  };
  messageDisplay: {
    open(openProperties: { messageId: number; location: "tab" }): Promise<unknown>;
  };
}

export const PAGE_SIZE = 100; // TB's default messagesPerPage, stated so pages are predictable

export interface TbApi {
  listAccounts(): Promise<TbAccount[]>;
  /** The account with its folder tree, or null if TB has no such account. */
  getAccountWithFolders(accountId: string): Promise<TbAccount | null>;
  getFolderInfo(folderId: string): Promise<TbFolderInfo>;
  query(q: TbQuery): Promise<TbMessageList>;
  continueList(listId: string): Promise<TbMessageList>;
  abortList(listId: string): Promise<void>;
  getMessage(tbId: number): Promise<TbMessageHeader>;
  getRaw(tbId: number): Promise<Uint8Array>;
  getFull(tbId: number): Promise<TbMessagePart>;
  listInlineTextParts(tbId: number): Promise<TbInlineTextPart[]>;
  listAttachments(tbId: number): Promise<TbAttachment[]>;
  openMessage(tbId: number): Promise<void>;
  listTags(): Promise<TbTag[]>;
}

export function tbApiFrom(m: MessengerMail): TbApi {
  return {
    listAccounts: () => m.accounts.list(false),
    getAccountWithFolders: (accountId) => m.accounts.get(accountId, true),
    getFolderInfo: (folderId) => m.folders.getFolderInfo(folderId),
    query: (q) => m.messages.query({ ...q, messagesPerPage: PAGE_SIZE }),
    continueList: (listId) => m.messages.continueList(listId),
    abortList: (listId) => m.messages.abortList(listId),
    getMessage: (tbId) => m.messages.get(tbId),
    getRaw: async (tbId) => new Uint8Array(await (await m.messages.getRaw(tbId)).arrayBuffer()),
    getFull: (tbId) => m.messages.getFull(tbId),
    listInlineTextParts: (tbId) => m.messages.listInlineTextParts(tbId),
    listAttachments: (tbId) => m.messages.listAttachments(tbId),
    openMessage: async (tbId) => {
      await m.messageDisplay.open({ messageId: tbId, location: "tab" });
    },
    listTags: () => m.messages.tags.list(),
  };
}

/**
 * TB's mail and folder events. Register synchronously at module load (S1 F14: TB primes
 * persisted listeners and wakes the event page for them). Event MessageLists can be paged:
 * drain them with `continueList`. Folder events pass every folder whose state changed: a
 * rename or move passes the old and the new folder (their ids differ).
 */
export interface TbEvents {
  onNewMail(cb: (folder: TbFolder, messages: TbMessageList) => void): void;
  onMoved(cb: (original: TbMessageList, moved: TbMessageList) => void): void;
  onCopied(cb: (original: TbMessageList, copied: TbMessageList) => void): void;
  onDeleted(cb: (messages: TbMessageList) => void): void;
  onUpdated(cb: (message: TbMessageHeader) => void): void;
  onFolderChanged(cb: (folders: TbFolder[]) => void): void;
}

export function tbEventsFrom(m: MessengerMail): TbEvents {
  return {
    // monitorAllFolders: without it TB reports new mail only for inbox-like and normal folders.
    onNewMail: (cb) => m.messages.onNewMailReceived.addListener(cb, true),
    onMoved: (cb) => m.messages.onMoved.addListener(cb),
    onCopied: (cb) => m.messages.onCopied.addListener(cb),
    onDeleted: (cb) => m.messages.onDeleted.addListener(cb),
    onUpdated: (cb) => m.messages.onUpdated.addListener((message) => cb(message)),
    onFolderChanged: (cb) => {
      m.folders.onCreated.addListener((f) => cb([f]));
      m.folders.onRenamed.addListener((a, b) => cb([a, b]));
      m.folders.onMoved.addListener((a, b) => cb([a, b]));
      m.folders.onCopied.addListener((_a, b) => cb([b]));
      m.folders.onDeleted.addListener((f) => cb([f]));
      m.folders.onUpdated.addListener((_a, b) => cb([b]));
      m.folders.onFolderInfoChanged.addListener((f) => cb([f]));
    },
  };
}

export type TbErrorKind = "message_not_found" | "list_not_found" | "folder_not_found" | "other";

/** Classify a TB API rejection by its (TB 153) message text; the text itself is never kept. */
export function tbErrorKind(err: unknown): TbErrorKind {
  const msg = err instanceof Error ? err.message : "";
  if (/^Message not found: \d+\.$/.test(msg)) return "message_not_found";
  if (msg.startsWith("No message list for id ")) return "list_not_found";
  if (msg.startsWith("Folder not found: ")) return "folder_not_found";
  return "other";
}

// ---------------------------------------------------------------------------------------------
// The chat tab page's runtime port to the background (plan 08b step 3). The background's
// ChatRouter accepts ports with this name only.

export const CHAT_PORT_NAME = "tern-chat";

export type PagePort = RuntimePort;

interface MessengerPage {
  runtime: { connect(connectInfo: { name: string }): PagePort };
  storage: {
    local: {
      get(keys: null): Promise<Record<string, unknown>>;
      set(items: Record<string, unknown>): Promise<void>;
      remove(keys: string[]): Promise<void>;
    };
  };
}

declare const messenger: MessengerPage | undefined;

/** Connect the chat tab page to the background. Throws outside an extension page. */
export function chatPagePort(): PagePort {
  if (typeof messenger === "undefined") throw new Error("no messenger global (not a TB page)");
  return messenger.runtime.connect({ name: CHAT_PORT_NAME });
}

// ---------------------------------------------------------------------------------------------
// The chat tab's saved chats (ui/chat/history.ts): the extension's `storage.local`, which lives
// in the user's TB profile and is never synced or sent anywhere. The page's own modules name no
// storage API (extension/test/manifest.test.ts); they reach this one only through chatPageStore().

/** The extension's local key-value store, as the chat page uses it. */
export interface PageStore {
  /** Every stored key and its value. */
  getAll(): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
  remove(keys: string[]): Promise<void>;
}

/** The chat page's store (`storage.local`). Throws outside an extension page. */
export function chatPageStore(): PageStore {
  if (typeof messenger === "undefined") throw new Error("no messenger global (not a TB page)");
  const local = messenger.storage.local;
  return {
    getAll: () => local.get(null),
    set: (items) => local.set(items),
    remove: (keys) => local.remove(keys),
  };
}
