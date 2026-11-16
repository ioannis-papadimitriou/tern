// The chat tab's saved chats. Each chat with at least one turn is one entry in the extension's
// local key-value store (tb/api.ts chatPageStore: it lives in the user's TB profile, is never
// synced, and nothing here sends it anywhere). Key `tern.chat.v1.<session id>`, value:
//
//   { "v": 1, "id": "<session id>", "seq": <last-activity order, higher is newer>,
//     "turns": [ { "user": "<the user's message>", "text": "<the answer as streamed>",
//                  "phase": "done" | "error" | "cancelled", "reason": <code> | null,
//                  "detail": <bridge error message> | null, "citations": [<mid>, …] | null } ] }
//
// A chat in `bypass` mode also has `"mode": "bypass"` (decisions #39); without it a chat is
// `manual`, the default, which is also what every entry saved before modes means. An entry with
// any other mode is unreadable. Approval cards are not saved.
//
// A turn still running when its chat is saved is saved as `error` with reason `interrupted` (the
// router cancels it when the tab closes). No ternd turn id and no TB id is saved: chips keep only
// mids (the daemon's `chat.done` list), which stay valid (architecture §4). At most MAX_CHATS
// entries are kept: main.ts removes the oldest beyond that and every deleted chat's entry.
// An entry that fails validation is left as it is and counted (`unreadable`), never guessed at.

import type { Chat, SavedChat, SavedTurn } from "./state";
import { isRunning, MAX_CHAT_TEXT, MAX_CHATS } from "./state";

export const ENTRY_PREFIX = "tern.chat.v1.";

/** Session ids the tab makes (crypto.randomUUID) and accepts back. */
const SESSION_ID = /^[A-Za-z0-9-]{1,64}$/;
const MID = /^(?:x-)?[0-9a-f]{64}$/;
const PHASES: ReadonlySet<string> = new Set(["done", "error", "cancelled"]);

export interface Entry {
  readonly v: 1;
  readonly id: string;
  readonly seq: number;
  readonly turns: readonly SavedTurn[];
  /** Only a bypass chat carries it (module comment). */
  readonly mode?: "bypass";
}

export function entryKey(id: string): string {
  return `${ENTRY_PREFIX}${id}`;
}

/** What `chat` is saved as; null for a chat without turns (nothing to keep). */
export function entryOf(chat: Chat): Entry | null {
  if (chat.turns.length === 0) return null;
  return {
    v: 1,
    id: chat.id,
    seq: chat.seq,
    ...(chat.mode === "bypass" ? { mode: "bypass" as const } : {}),
    turns: chat.turns.map((t): SavedTurn => {
      if (isRunning(t)) {
        return {
          user: t.user,
          text: t.text,
          phase: "error",
          reason: "interrupted",
          detail: null,
          citations: null,
        };
      }
      return {
        user: t.user,
        text: t.text,
        phase: t.phase as SavedTurn["phase"],
        reason: t.reason,
        detail: t.detail,
        citations: t.citations === null ? null : [...t.citations],
      };
    }),
  };
}

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

const isNullableStr = (x: unknown): x is string | null => x === null || typeof x === "string";

function parseTurn(x: unknown): SavedTurn | null {
  if (!isObject(x)) return null;
  const { user, text, phase, reason, detail, citations } = x;
  if (typeof user !== "string" || user === "" || user.length > MAX_CHAT_TEXT) return null;
  if (typeof text !== "string" || typeof phase !== "string" || !PHASES.has(phase)) return null;
  if (!isNullableStr(reason) || !isNullableStr(detail)) return null;
  if (
    citations !== null &&
    !(Array.isArray(citations) && citations.every((m) => typeof m === "string" && MID.test(m)))
  ) {
    return null;
  }
  return {
    user,
    text,
    phase: phase as SavedTurn["phase"],
    reason,
    detail,
    citations: citations === null ? null : [...(citations as string[])],
  };
}

/** One saved entry, validated; null when anything about it is off. */
export function parseEntry(key: string, x: unknown): SavedChat | null {
  if (!isObject(x) || x.v !== 1) return null;
  const { id, seq, turns, mode } = x;
  if (typeof id !== "string" || !SESSION_ID.test(id) || key !== entryKey(id)) return null;
  if (typeof seq !== "number" || !Number.isSafeInteger(seq) || seq < 0) return null;
  if (!Array.isArray(turns) || turns.length === 0) return null;
  if (mode !== undefined && mode !== "bypass") return null;
  const parsed: SavedTurn[] = [];
  for (const t of turns) {
    const turn = parseTurn(t);
    if (turn === null) return null;
    parsed.push(turn);
  }
  return mode === "bypass" ? { id, seq, turns: parsed, mode } : { id, seq, turns: parsed };
}

export interface ReadResult {
  /** The newest MAX_CHATS readable chats, newest first. */
  readonly chats: SavedChat[];
  /** Entries that failed validation (left in place). */
  readonly unreadable: number;
  /** Keys of readable entries beyond MAX_CHATS (to remove). */
  readonly excess: string[];
}

/** The saved chats among every stored key (other keys are not the tab's and are ignored). */
export function readEntries(all: Readonly<Record<string, unknown>>): ReadResult {
  const chats: SavedChat[] = [];
  let unreadable = 0;
  for (const [key, value] of Object.entries(all)) {
    if (!key.startsWith(ENTRY_PREFIX)) continue;
    const chat = parseEntry(key, value);
    if (chat === null) unreadable += 1;
    else chats.push(chat);
  }
  chats.sort((a, b) => b.seq - a.seq);
  return {
    chats: chats.slice(0, MAX_CHATS),
    unreadable,
    excess: chats.slice(MAX_CHATS).map((c) => entryKey(c.id)),
  };
}

export interface Diff {
  /** Entries to write (new or changed). */
  readonly set: Record<string, Entry>;
  /** Keys to remove (chats deleted or dropped). */
  readonly remove: string[];
  /** What is saved once both are done: key → the entry's JSON. */
  readonly next: Map<string, string>;
}

/** What to write so the saved entries match `chats`, given what is saved now (`saved`). */
export function diff(saved: ReadonlyMap<string, string>, chats: readonly Chat[]): Diff {
  const set: Record<string, Entry> = {};
  const next = new Map<string, string>();
  for (const chat of chats) {
    const entry = entryOf(chat);
    if (entry === null) continue;
    const key = entryKey(chat.id);
    const json = JSON.stringify(entry);
    next.set(key, json);
    if (saved.get(key) !== json) set[key] = entry;
  }
  const remove = [...saved.keys()].filter((k) => !next.has(k));
  return { set, remove, next };
}
