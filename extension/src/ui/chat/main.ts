// Plan 08b step 5: the chat tab page's entry (dist/ui/chat/main.js, loaded by index.html). It
// connects to the background's chat router through tb/api.ts's chatPagePort() (A5: the only place
// this page's code reaches the runtime), feeds every validated message and user action through
// the reducer (state.ts), posts the requests it returns, and renders the views (view.ts) into the
// page's regions once per animation frame. Closing the tab ends its running turn (the router
// cancels it when the port closes).
//
// Saved chats (history.ts): at startup the page reads the saved entries through tb/api.ts's
// chatPageStore() and hands them to the reducer (`restore`); after that, a change to any chat is
// saved at most every SAVE_DELAY_MS (only entries that changed are written, deleted and dropped
// chats are removed). A save that fails shows one notice; the chats stay in the tab.
//
// Effects the views ask for: `copy` (the clipboard; the button says "Copied" for COPIED_MS) and
// `download` (the artifact's text as a file, through a Blob URL; saveTextFile).

import type { PageRequest } from "../../protocol";
import { chatPagePort, chatPageStore, type PagePort, type PageStore } from "../../tb/api";
import { diff, entryKey, entryOf, readEntries } from "./history";
import {
  type Action,
  type Artifact,
  allChats,
  type ChatState,
  initialState,
  parsePageMessage,
  replyReq,
  update,
} from "./state";
import { render } from "./vdom";
import { actionsView, artifactView, headerView, logView, overlayView, sidebarView } from "./view";

/** The page's elements main.ts uses (ids in index.html). */
export interface ChatElements {
  /** The page's frame: carries `data-sidebar` and `data-artifact` for the layout (chat.css). */
  readonly app: Pick<Element, "setAttribute">;
  readonly sidebar: Element;
  readonly header: Element;
  readonly log: Element;
  readonly overlay: Element;
  readonly actions: Element;
  readonly artifact: Element;
  readonly form: Pick<HTMLFormElement, "addEventListener">;
  readonly input: Pick<
    HTMLTextAreaElement,
    "value" | "addEventListener" | "focus" | "scrollHeight"
  > & { readonly style: { height: string } };
}

export interface ChatPageDeps {
  readonly elements: ChatElements;
  /** Open a runtime port to the background (chatPagePort in TB). */
  connect(): PagePort;
  /** The saved chats' store (chatPageStore in TB). */
  readonly store: PageStore;
  /** Put `text` on the clipboard (navigator.clipboard.writeText in TB). */
  copyText(text: string): Promise<void>;
  /** Offer `text` as a file named `filename` (saveTextFile in TB). Throws when it cannot. */
  saveFile(filename: string, text: string): void;
  randomUUID(): string;
  now(): number;
  /** Run `cb` before the next paint (requestAnimationFrame in TB). */
  schedule(cb: () => void): void;
  /** Run `cb` after `ms` milliseconds (setTimeout in TB). */
  later(cb: () => void, ms: number): void;
  log(msg: string): void;
}

/** How close (px) to the bottom the message list must be to follow a streaming answer. */
const FOLLOW_PX = 48;
/** The composer grows with its text up to this height (px), then scrolls. */
export const INPUT_MAX_PX = 240;
export const SAVE_DELAY_MS = 500;
export const COPIED_MS = 1500;

export const SAVE_FAILED_NOTICE =
  "Could not save this tab's chats; they stay here until the tab closes.";

function errName(err: unknown): string {
  return err instanceof Error ? err.name : typeof err;
}

export class ChatPage {
  private stateNow: ChatState;
  private port: PagePort | null = null;
  private renderQueued = false;
  private dispatching = false;
  private readonly queue: Action[] = [];
  /** Saved entries as last written (key → JSON); null until the saved chats were read. */
  private saved: Map<string, string> | null = null;
  private saveQueued = false;
  private saving = false;
  private saveAgain = false;
  private saveFailed = false;
  private shownSession: string | null = null;

  constructor(private readonly deps: ChatPageDeps) {
    this.stateNow = initialState(deps.randomUUID());
    const { form, input } = deps.elements;
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      this.submit();
    });
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        this.submit();
      }
    });
    input.addEventListener("input", () => this.grow());
    this.dispatch({ type: "refresh" }); // the status line, fetched when the tab opens
    this.paint();
    this.loadHistory();
  }

  get state(): ChatState {
    return this.stateNow;
  }

  /** Run one action through the reducer and post its requests (re-entrant calls are queued). */
  dispatch(action: Action): void {
    if (action.type === "copy") {
      this.copy(action.key, action.text);
      return;
    }
    if (action.type === "download") {
      this.download(action.artifact);
      return;
    }
    this.queue.push(action);
    if (this.dispatching) return;
    this.dispatching = true;
    try {
      for (let a = this.queue.shift(); a !== undefined; a = this.queue.shift()) {
        const { state, out } = update(this.stateNow, a);
        this.stateNow = state;
        for (const req of out) this.post(req);
      }
    } finally {
      this.dispatching = false;
    }
    this.requestPaint();
    this.requestSave();
  }

  private submit(): void {
    const { input } = this.deps.elements;
    const before = this.stateNow.turns.length;
    this.dispatch({ type: "submit", text: input.value });
    if (this.stateNow.turns.length > before) {
      input.value = "";
      this.grow();
    }
  }

  /** The composer's height follows its text, up to INPUT_MAX_PX. */
  private grow(): void {
    const { input } = this.deps.elements;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, INPUT_MAX_PX)}px`;
  }

  private copy(key: string, text: string): void {
    this.deps.copyText(text).then(
      () => {
        this.dispatch({ type: "copied", key });
        this.deps.later(() => {
          if (this.stateNow.copied === key) this.dispatch({ type: "copied", key: null });
        }, COPIED_MS);
      },
      (err: unknown) => {
        this.deps.log(`copy failed: ${errName(err)}`);
        this.dispatch({
          type: "notice",
          level: "error",
          text: `Could not copy (${errName(err)}).`,
        });
      },
    );
  }

  private download(artifact: Artifact): void {
    try {
      this.deps.saveFile(artifact.filename, artifact.text);
    } catch (err) {
      this.deps.log(`download failed: ${errName(err)}`);
      this.dispatch({
        type: "notice",
        level: "error",
        text: `Could not download ${artifact.filename} (${errName(err)}).`,
      });
    }
  }

  // --- saved chats --------------------------------------------------------------------------

  private loadHistory(): void {
    this.deps.store.getAll().then(
      (all) => {
        const read = readEntries(all);
        this.dispatch({ type: "restore", chats: read.chats, unreadable: read.unreadable });
        if (read.unreadable > 0) this.deps.log(`saved chats unreadable: ${read.unreadable}`);
        // What is saved now: each restored chat as the tab holds it. A readable entry the tab
        // does not hold (beyond MAX_CHATS) is marked with "" so the first save removes it.
        const held = new Map(allChats(this.stateNow).map((c) => [c.id, c]));
        const saved = new Map<string, string>();
        for (const c of read.chats) {
          const chat = held.get(c.id);
          const entry = chat === undefined ? null : entryOf(chat);
          saved.set(entryKey(c.id), entry === null ? "" : JSON.stringify(entry));
        }
        for (const key of read.excess) saved.set(key, "");
        this.saved = saved;
        this.requestSave();
      },
      (err: unknown) => {
        // Saving stays off: writing without knowing what is saved could drop saved chats.
        this.deps.log(`reading saved chats failed: ${errName(err)}`);
        this.dispatch({
          type: "notice",
          level: "error",
          text: `Could not read the saved chats (${errName(err)}); this tab's chats are not saved.`,
        });
      },
    );
  }

  private requestSave(): void {
    if (this.saved === null || this.saveQueued) return;
    this.saveQueued = true;
    this.deps.later(() => {
      this.saveQueued = false;
      this.save();
    }, SAVE_DELAY_MS);
  }

  private save(): void {
    const saved = this.saved;
    if (saved === null) return;
    if (this.saving) {
      this.saveAgain = true;
      return;
    }
    const d = diff(saved, allChats(this.stateNow));
    const keys = Object.keys(d.set);
    if (keys.length === 0 && d.remove.length === 0) return;
    this.saving = true;
    const write = keys.length === 0 ? Promise.resolve() : this.deps.store.set(d.set);
    write
      .then(() => (d.remove.length === 0 ? undefined : this.deps.store.remove(d.remove)))
      .then(
        () => {
          this.saved = d.next;
          this.saveFailed = false;
        },
        (err: unknown) => {
          this.deps.log(`saving chats failed: ${errName(err)}`);
          if (!this.saveFailed) {
            this.saveFailed = true;
            this.dispatch({ type: "notice", level: "error", text: SAVE_FAILED_NOTICE });
          }
        },
      )
      .finally(() => {
        this.saving = false;
        if (this.saveAgain) {
          this.saveAgain = false;
          this.requestSave();
        }
      });
  }

  // --- the port -----------------------------------------------------------------------------

  /**
   * Post to the background, connecting first when there is no port (lazily after a close). A
   * connect or post that throws is a closed port: the request fails like every other waiting one.
   */
  private post(req: PageRequest): void {
    let port = this.port;
    try {
      port ??= this.connect();
      port.postMessage(req);
    } catch (err) {
      const name = errName(err);
      if (port === null) {
        this.deps.log(`page port connect failed: ${name}`);
        this.dispatch({ type: "port_closed" });
        return;
      }
      this.deps.log(`page port post failed: ${name}`);
      // The port may still be open on the router's side: disconnect it, so the router sees the
      // close and cancels this tab's running turn (step 3) instead of ternd answering nobody.
      try {
        port.disconnect();
      } catch (dErr) {
        this.deps.log(`page port disconnect failed: ${errName(dErr)}`);
      }
      this.closed(port);
    }
  }

  private connect(): PagePort {
    const port = this.deps.connect();
    this.port = port;
    port.onMessage.addListener((m) => {
      if (this.port !== port) return; // a message from a port already given up
      const msg = parsePageMessage(m);
      if (msg === null) {
        this.deps.log("malformed message from the background dropped");
        // A malformed reply still answers its request: fail it, or it waits until a reset.
        const req = replyReq(m);
        if (req !== null) this.dispatch({ type: "malformed_reply", req });
        return;
      }
      this.dispatch({ type: "message", msg });
    });
    port.onDisconnect.addListener(() => this.closed(port));
    return port;
  }

  private closed(port: PagePort): void {
    if (this.port !== port) return;
    this.port = null; // the next request reconnects
    this.dispatch({ type: "port_closed" });
  }

  // --- painting -----------------------------------------------------------------------------

  private requestPaint(): void {
    if (this.renderQueued) return;
    this.renderQueued = true;
    this.deps.schedule(() => {
      this.renderQueued = false;
      this.paint();
    });
  }

  private paint(): void {
    const { app, sidebar, header, log, overlay, actions, artifact, input } = this.deps.elements;
    const s = this.stateNow;
    const dispatch = (a: Action): void => this.dispatch(a);
    const newId = this.deps.randomUUID;
    // Another chat is shown: start at its end. Otherwise follow a streaming answer only while the
    // reader is at the bottom.
    const switched = this.shownSession !== s.sessionId;
    const follow = switched || log.scrollHeight - log.scrollTop - log.clientHeight < FOLLOW_PX;
    app.setAttribute("data-sidebar", s.sidebarToggled ? "toggled" : "default");
    app.setAttribute("data-artifact", s.artifact === null ? "closed" : "open");
    render(sidebarView(s, dispatch, newId), sidebar);
    render(headerView(s, dispatch, this.deps.now(), newId), header);
    render(logView(s, dispatch), log);
    render(overlayView(s, dispatch), overlay);
    render(actionsView(s, dispatch), actions);
    render(artifactView(s, dispatch), artifact);
    if (follow) log.scrollTop = log.scrollHeight;
    if (switched && this.shownSession !== null) input.focus();
    this.shownSession = s.sessionId;
  }
}

/** Where saveTextFile reaches the page (the document and URL's Blob URL functions in TB). */
export interface FileEnv {
  readonly doc: Pick<Document, "createElement" | "body">;
  createObjectURL(blob: Blob): string;
  revokeObjectURL(url: string): void;
  later(cb: () => void, ms: number): void;
}

/** How long a download's Blob URL stays valid (the download has started long before). */
export const REVOKE_MS = 60_000;

/**
 * Offer `text` as a file: a plain-text Blob behind a temporary `blob:` URL, clicked through a
 * detached download link that is removed at once (never part of the rendered views). The text is
 * never parsed or shown as HTML, whatever the file's extension.
 */
export function saveTextFile(env: FileEnv, filename: string, text: string): void {
  const url = env.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  try {
    const link = env.doc.createElement("a");
    link.href = url;
    link.download = filename;
    env.doc.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    env.later(() => env.revokeObjectURL(url), REVOKE_MS);
  }
}

function byId(id: string): HTMLElement {
  const el = document.getElementById(id);
  if (el === null) throw new Error(`chat page: #${id} missing`);
  return el;
}

/** Start the page in TB: the elements of index.html, the real port, rAF painting. */
export function startChatPage(): ChatPage {
  const later = (cb: () => void, ms: number): void => void setTimeout(cb, ms);
  const page = new ChatPage({
    elements: {
      app: byId("app"),
      sidebar: byId("sidebar"),
      header: byId("header"),
      log: byId("log"),
      overlay: byId("overlay"),
      actions: byId("actions"),
      artifact: byId("artifact"),
      form: byId("composer") as HTMLFormElement,
      input: byId("input") as HTMLTextAreaElement,
    },
    connect: chatPagePort,
    store: chatPageStore(),
    copyText: (text) => navigator.clipboard.writeText(text),
    saveFile: (filename, text) =>
      saveTextFile(
        {
          doc: document,
          createObjectURL: (b) => URL.createObjectURL(b),
          revokeObjectURL: (u) => URL.revokeObjectURL(u),
          later,
        },
        filename,
        text,
      ),
    randomUUID: () => crypto.randomUUID(),
    now: () => Date.now(),
    schedule: (cb) => void requestAnimationFrame(() => cb()),
    later,
    log: (m) => console.info(`tern chat page: ${m}`),
  });
  byId("input").focus();
  return page;
}

// Tests import this module in Node (no document); in the tab it always exists.
if (typeof document !== "undefined") startChatPage();
