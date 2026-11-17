// Plan 08b step 5: the chat tab's views. Pure functions from ChatState to vnodes built with `h()`
// (vdom.ts, preact's shape; no JSX, A7); main.ts renders them into the page's regions. The
// user's text is plain text; the answer goes through the sanitizer's tree (state.displayBlocks),
// whose nodes map to elements one to one:
// - a link is an inert button showing its label; a click opens an in-page confirm panel with the
//   full URL (P9), and only "Open" there asks the background to open it in the default browser;
// - a citation is a chip (button) showing a shortened mid; a click asks the background to open the
//   message in Thunderbird (bridge 1.1 ui.open_mid; on a 1.0 session the notice of A1 instead);
// - while ternd allows a remote model (daemon.info remote_llm), the header carries #9's banner;
// - images were removed by the sanitizer and show a fixed "[image removed]" label (P27);
// - a code block, a table and a long list get a small header with Copy and "Open as artifact"
//   (artifacts.ts): the side panel shows the block's text, never HTML, with Copy and Download.
// The sidebar lists the tab's chats (state.chatList): click to switch, × then "Delete?" to delete.
// While a turn runs, the answer shows "Thinking" until its first text arrives and "Writing" while
// it streams: the only two things the tab is told besides approval cards.
// Permission modes (decisions #39): the header's Manual/Bypass switch sets the shown chat's mode.
// In a manual turn each tool call shows an approval card: the tool, its tier and its arguments as
// plain text (state.argsText: pretty JSON, hidden characters escaped; long ones folded behind
// "Show all"), with Allow, Deny and "Allow all for this turn", disabled once decided.
//
// Model picker (bridge 1.3): when ternd lists a choice of chat models, the header shows a
// dropdown button naming the selected model; its menu lists every loaded model with its server.

import type { ChatMode, IndexStatus } from "../../protocol";
import type { Block, Inline, ListItem, TableCell } from "../sanitize";
import { artifactOf, IMAGE_REMOVED_LABEL, lineCount } from "./artifacts";
import { REMOVED_MARKER } from "./citations";
import {
  type Action,
  type ApprovalCard,
  type CardState,
  type ChatState,
  chatList,
  displayBlocks,
  FORGOTTEN_NOTE,
  isRunning,
  outcomeText,
  remoteBanner,
  runningTurn,
  selectedModel,
  shortMid,
  showsModelPicker,
  type Turn,
} from "./state";
import { type Child, h, type VNode } from "./vdom";

export { IMAGE_REMOVED_LABEL };

export type Dispatch = (action: Action) => void;

export const EMPTY_TEXT = "Ask about your mail. Answers cite the messages they use.";

// --- answer tree ----------------------------------------------------------------------------

export function inlineNode(n: Inline, pending: boolean, dispatch: Dispatch): Child {
  switch (n.t) {
    case "text":
      return n.text;
    case "strong":
      return h(
        "strong",
        null,
        n.children.map((c) => inlineNode(c, pending, dispatch)),
      );
    case "em":
      return h(
        "em",
        null,
        n.children.map((c) => inlineNode(c, pending, dispatch)),
      );
    case "code":
      return h("code", null, n.text);
    case "br":
      return h("br", null);
    case "link":
      return h(
        "button",
        {
          type: "button",
          class: "link",
          title: n.href,
          onClick: () => dispatch({ type: "link", href: n.href }),
        },
        n.label,
      );
    case "cite":
      return h(
        "button",
        {
          type: "button",
          class: pending ? "chip pending" : "chip",
          title: `message ${shortMid(n.mid)}`,
          // A chip opens its message only once chat.done confirmed the mid (step 9b review):
          // while the turn streams it is shown but inert.
          ...(pending
            ? { disabled: true }
            : { onClick: () => dispatch({ type: "chip", mid: n.mid }) }),
        },
        shortMid(n.mid),
      );
    case "removed":
      return h("span", { class: "removed" }, REMOVED_MARKER);
    case "image_removed":
      return h("span", { class: "image-removed" }, IMAGE_REMOVED_LABEL);
  }
}

function items(xs: readonly ListItem[], pending: boolean, dispatch: Dispatch): VNode[] {
  return xs.map((li) =>
    h(
      "li",
      null,
      li.children.map((c) => inlineNode(c, pending, dispatch)),
    ),
  );
}

function cells(
  tag: "th" | "td",
  xs: readonly TableCell[],
  align: readonly (string | null)[],
  pending: boolean,
  dispatch: Dispatch,
): VNode[] {
  return xs.map((c, i) => {
    const a = align[i] ?? null;
    return h(
      tag,
      a === null ? null : { class: `align-${a}` },
      c.children.map((n) => inlineNode(n, pending, dispatch)),
    );
  });
}

export function blockNode(b: Block, pending: boolean, dispatch: Dispatch): VNode {
  const inl = (xs: readonly Inline[]): Child[] => xs.map((c) => inlineNode(c, pending, dispatch));
  switch (b.t) {
    case "p":
      return h("p", null, inl(b.children));
    case "h":
      // Headings render as bold lines (plan 08b step 4), sized by level in chat.css.
      return h("p", { class: `heading h${b.level}` }, h("strong", null, inl(b.children)));
    case "pre":
      return h("pre", null, h("code", null, b.text));
    case "ul":
      return h("ul", null, items(b.items, pending, dispatch));
    case "ol":
      return h(
        "ol",
        b.start === undefined ? null : { start: b.start },
        items(b.items, pending, dispatch),
      );
    case "table":
      return h(
        "div",
        { class: "table-scroll" },
        h(
          "table",
          null,
          h("thead", null, h("tr", null, cells("th", b.head, b.align, pending, dispatch))),
          h(
            "tbody",
            null,
            b.rows.map((r) => h("tr", null, cells("td", r, b.align, pending, dispatch))),
          ),
        ),
      );
  }
}

/** A copy button: "Copy", or "Copied" right after it copied. */
function copyButton(key: string, text: string, state: ChatState, dispatch: Dispatch, off = false) {
  return h(
    "button",
    {
      type: "button",
      class: "tool copy",
      disabled: off,
      onClick: () => dispatch({ type: "copy", key, text }),
    },
    state.copied === key ? "Copied" : "Copy",
  );
}

/**
 * A block with its artifact header when it has one (artifacts.ts). While its turn runs the
 * buttons are disabled: the block may still grow.
 */
export function blockWithTools(
  b: Block,
  key: string,
  running: boolean,
  state: ChatState,
  dispatch: Dispatch,
): VNode {
  const node = blockNode(b, running, dispatch);
  const artifact = artifactOf(b);
  if (artifact === null) return node;
  return h(
    "div",
    { class: `block-tools ${b.t}` },
    h(
      "div",
      { class: "block-head" },
      h("span", { class: "block-title" }, artifact.title),
      copyButton(key, artifact.text, state, dispatch, running),
      h(
        "button",
        {
          type: "button",
          class: "tool open-artifact",
          disabled: running,
          onClick: () => dispatch({ type: "open_artifact", artifact }),
        },
        "Open as artifact",
      ),
    ),
    node,
  );
}

// --- regions --------------------------------------------------------------------------------

function indicator(label: string): VNode {
  return h(
    "p",
    { class: "thinking", role: "status" },
    h(
      "span",
      { class: "dots", "aria-hidden": "true" },
      h("span", null),
      h("span", null),
      h("span", null),
    ),
    label,
  );
}

/** Arguments longer than this (lines or characters) are folded until "Show all". */
export const ARGS_FOLD_LINES = 12;
export const ARGS_FOLD_CHARS = 1200;

const CARD_STATUS: Readonly<Record<CardState, string | null>> = {
  open: null,
  sending: "Sending…",
  approved: "Allowed",
  denied: "Denied",
  expired: "Expired: not run",
  targets_changed: "Not run: the messages changed",
  failed: "Could not send the decision; try again",
  closed: "Not run: the answer ended first",
};

/** The text shown for a card's arguments: whole, or its first lines while folded. */
function shownArgs(card: ApprovalCard): { text: string; folded: boolean; lines: number } {
  const lines = card.argsText.split("\n");
  const long = lines.length > ARGS_FOLD_LINES || card.argsText.length > ARGS_FOLD_CHARS;
  if (!long || card.expanded) return { text: card.argsText, folded: false, lines: lines.length };
  const head = lines.slice(0, ARGS_FOLD_LINES).join("\n").slice(0, ARGS_FOLD_CHARS);
  return { text: `${head}\n…`, folded: true, lines: lines.length };
}

/** One approval card (a tool call of a manual turn). Every value is a text node. */
export function approvalCardView(card: ApprovalCard, dispatch: Dispatch): VNode {
  const live = card.state === "open" || card.state === "failed";
  const args = shownArgs(card);
  const status = CARD_STATUS[card.state];
  const decide = (decision: "approve" | "deny", restOfTurn: boolean) => () =>
    dispatch({ type: "decide", approvalId: card.approvalId, decision, restOfTurn });
  return h(
    "section",
    {
      class: `approval ${card.state}`,
      role: "group",
      "aria-label": `Run tool ${card.tool}?`,
      "data-approval": card.approvalId,
    },
    h(
      "p",
      { class: "approval-head" },
      "Run tool ",
      h("code", { class: "tool-name" }, card.tool),
      h("small", { class: "tier", title: "permission tier" }, ` ${card.tier}`),
      "?",
    ),
    h("pre", { class: "approval-args" }, h("code", null, args.text)),
    args.folded
      ? h(
          "button",
          {
            type: "button",
            class: "ghost show-args",
            onClick: () => dispatch({ type: "expand_args", approvalId: card.approvalId }),
          },
          `Show all (${args.lines} lines)`,
        )
      : null,
    h(
      "div",
      { class: "row approval-actions" },
      h(
        "button",
        {
          type: "button",
          class: "primary allow",
          disabled: !live,
          onClick: decide("approve", false),
        },
        "Allow",
      ),
      h(
        "button",
        { type: "button", class: "ghost deny", disabled: !live, onClick: decide("deny", false) },
        "Deny",
      ),
      h(
        "button",
        {
          type: "button",
          class: "ghost allow-all",
          title: "Allow this call and every later tool call of this answer",
          disabled: !live,
          onClick: decide("approve", true),
        },
        "Allow all for this turn",
      ),
    ),
    status === null
      ? null
      : h(
          "p",
          { class: "approval-status", role: "status" },
          card.restOfTurn && card.state === "approved"
            ? `${status} (rest of this turn too)`
            : status,
        ),
  );
}

function turnNodes(turn: Turn, state: ChatState, dispatch: Dispatch): VNode[] {
  const running = isRunning(turn);
  const outcome = outcomeText(turn);
  const answer: Child[] = (turn.approvals ?? []).map((c) => approvalCardView(c, dispatch));
  answer.push(
    ...displayBlocks(turn).map((b, i) =>
      blockWithTools(b, `${turn.key}:${i}`, running, state, dispatch),
    ),
  );
  if (turn.phase === "pending") answer.push(indicator("Thinking"));
  if (turn.phase === "streaming") answer.push(indicator("Writing"));
  if (outcome !== null) {
    answer.push(
      h("p", { class: turn.phase === "cancelled" ? "outcome" : "outcome error" }, outcome),
    );
  }
  return [
    h("div", { class: "msg user" }, turn.user),
    h("div", { class: "msg assistant", "aria-busy": running }, answer),
  ];
}

/** The message list. */
export function logView(state: ChatState, dispatch: Dispatch): VNode {
  if (state.turns.length === 0) {
    return h(
      "div",
      { class: "turns" },
      h(
        "div",
        { class: "welcome" },
        h("p", { class: "welcome-mark", "aria-hidden": "true" }, "tern"),
        h("p", { class: "empty" }, EMPTY_TEXT),
        h("p", { class: "hint" }, "Enter sends · Shift+Enter adds a line"),
      ),
    );
  }
  const nodes: VNode[] = [];
  state.turns.forEach((t, i) => {
    nodes.push(...turnNodes(t, state, dispatch));
    if (i === state.forgotten - 1) {
      nodes.push(h("p", { class: "forgotten", role: "note" }, FORGOTTEN_NOTE));
    }
  });
  return h("div", { class: "turns" }, nodes);
}

function ago(iso: string | null, now: number): string {
  if (iso === null) return "never";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "unknown";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 90) return `${s} s ago`;
  if (s < 90 * 60) return `${Math.round(s / 60)} min ago`;
  if (s < 36 * 3600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/** The status line's text from `index.status` (counts only). */
export function statusText(status: IndexStatus | null, error: string | null, now: number): string {
  if (error !== null) return `status unavailable (${error})`;
  if (status === null) return "status: loading…";
  const parts = [
    `${status.messages} messages`,
    status.models.embed === "" ? "embedding off" : `${status.embed_pending} to embed`,
    `lag ${Math.round(status.lag_s)} s`,
    `last sync ${ago(status.last_reconcile, now)}`,
  ];
  if (status.conflicted > 0) parts.push(`${status.conflicted} conflicted`);
  if (status.models.chat !== "") parts.push(`model ${status.models.chat}`);
  return parts.join(" · ");
}

/**
 * The header: the chat-list toggle, the status line, Refresh and New chat, and below them #9's
 * remote-model banner while ternd allows a remote model. `newSessionId` makes the new chat's
 * session id (crypto.randomUUID() in main.ts), so the view stays pure.
 */
export function headerView(
  state: ChatState,
  dispatch: Dispatch,
  now: number,
  newSessionId: () => string,
): VNode {
  const banner = remoteBanner(state);
  return h(
    "div",
    { class: "head" },
    barView(state, dispatch, now, newSessionId),
    banner === null ? null : h("div", { class: "banner", role: "alert" }, banner),
  );
}

function barView(
  state: ChatState,
  dispatch: Dispatch,
  now: number,
  newSessionId: () => string,
): VNode {
  const status = statusText(state.status, state.statusError, now);
  return h(
    "div",
    { class: "bar" },
    h(
      "button",
      {
        type: "button",
        class: "icon toggle-sidebar",
        title: "Show or hide the chat list",
        "aria-label": "Chats",
        onClick: () => dispatch({ type: "toggle_sidebar" }),
      },
      "☰",
    ),
    h(
      "span",
      {
        class: state.statusError === null ? "status" : "status error",
        role: "status",
        title: status,
      },
      status,
    ),
    modelView(state, dispatch),
    modeView(state.mode, dispatch),
    h(
      "button",
      { type: "button", class: "ghost", onClick: () => dispatch({ type: "refresh" }) },
      "Refresh",
    ),
    h(
      "button",
      {
        type: "button",
        class: "ghost",
        onClick: () => dispatch({ type: "new_chat", sessionId: newSessionId() }),
      },
      "New chat",
    ),
  );
}

/**
 * The model picker: a dropdown button naming the selected model and, while open, a menu of every
 * loaded chat model ternd offers (label, then server). null while there is no choice to make.
 */
export function modelView(state: ChatState, dispatch: Dispatch): VNode | null {
  const models = state.models;
  if (models.kind !== "known" || !showsModelPicker(state)) return null;
  const current = selectedModel(state);
  const switching = models.selecting !== null;
  const label = current === null ? "Choose a model" : current.label;
  return h(
    "div",
    { class: "model-picker" },
    h(
      "button",
      {
        type: "button",
        class: "model-button",
        "aria-haspopup": "listbox",
        "aria-expanded": state.modelMenu,
        "aria-label": `Chat model: ${label}`,
        "aria-busy": switching,
        title:
          current === null
            ? "Choose the chat model for your next messages"
            : `${current.label} on ${current.server}; applies from your next message`,
        onClick: () => dispatch({ type: "toggle_model_menu" }),
      },
      h("span", { class: "model-name" }, switching ? `${label}…` : label),
      h("span", { class: "model-caret", "aria-hidden": "true" }, "▾"),
    ),
    state.modelMenu
      ? h(
          "div",
          { class: "model-menu", role: "listbox", "aria-label": "Chat models" },
          models.models.map((m) =>
            h(
              "button",
              {
                type: "button",
                role: "option",
                class: m.key === current?.key ? "model-option chosen" : "model-option",
                "aria-selected": m.key === current?.key,
                title: m.model,
                onClick: () => dispatch({ type: "select_model", key: m.key }),
              },
              h("span", { class: "model-label" }, m.label),
              h("small", { class: "model-server" }, m.server),
            ),
          ),
        )
      : null,
  );
}

const MODES: readonly { mode: ChatMode; label: string; title: string }[] = [
  {
    mode: "manual",
    label: "Manual",
    title: "Ask before every tool call this chat makes (reads included)",
  },
  {
    mode: "bypass",
    label: "Bypass",
    title: "Run tool calls without asking, as the permission tiers allow (W2/W3 still ask)",
  },
];

/** The shown chat's permission mode: a two-button switch (aria-pressed on the chosen one). */
export function modeView(mode: ChatMode, dispatch: Dispatch): VNode {
  return h(
    "div",
    { class: "mode", role: "group", "aria-label": "Tool permissions for this chat" },
    MODES.map((m) =>
      h(
        "button",
        {
          type: "button",
          class: m.mode === mode ? "mode-option chosen" : "mode-option",
          title: m.title,
          "aria-pressed": m.mode === mode,
          onClick: () => dispatch({ type: "set_mode", mode: m.mode }),
        },
        m.label,
      ),
    ),
  );
}

/** The chat list: New chat, then every chat newest first, each with its delete button. */
export function sidebarView(
  state: ChatState,
  dispatch: Dispatch,
  newSessionId: () => string,
): VNode {
  const entries = chatList(state).map((c) => {
    const confirming = state.deleting === c.id;
    return h(
      "div",
      { class: c.shown ? "chat-item shown" : "chat-item", role: "listitem" },
      h(
        "button",
        {
          type: "button",
          class: "chat-open",
          title: c.title,
          "aria-current": c.shown ? "true" : undefined,
          onClick: () => dispatch({ type: "switch_chat", id: c.id }),
        },
        c.running ? h("span", { class: "running", "aria-label": "answering" }) : null,
        h("span", { class: "chat-title" }, c.title),
      ),
      h(
        "button",
        {
          type: "button",
          class: confirming ? "chat-delete confirming" : "chat-delete",
          title: confirming ? "Click again to delete this chat" : "Delete chat",
          "aria-label": confirming ? "Confirm delete" : "Delete chat",
          onClick: () =>
            dispatch(
              confirming
                ? { type: "delete_chat", id: c.id, sessionId: newSessionId() }
                : { type: "ask_delete", id: c.id },
            ),
        },
        confirming ? "Delete?" : "×",
      ),
    );
  });
  return h(
    "div",
    { class: "sidebar" },
    h(
      "div",
      { class: "sidebar-head" },
      h("span", { class: "brand" }, "tern"),
      h(
        "button",
        {
          type: "button",
          class: "new-chat",
          onClick: () => dispatch({ type: "new_chat", sessionId: newSessionId() }),
        },
        "+ New chat",
      ),
      // Shown on narrow pages only (chat.css), where the list floats over the chat.
      h(
        "button",
        {
          type: "button",
          class: "icon sidebar-close",
          title: "Hide the chat list",
          "aria-label": "Hide chats",
          onClick: () => dispatch({ type: "toggle_sidebar" }),
        },
        "×",
      ),
    ),
    h("div", { class: "chat-list", role: "list", "aria-label": "Chats" }, entries),
    h("p", { class: "sidebar-note" }, "Chats are saved in this Thunderbird profile only."),
  );
}

/** The side panel with the open artifact (its text only), or nothing. */
export function artifactView(state: ChatState, dispatch: Dispatch): VNode | null {
  const a = state.artifact;
  if (a === null) return null;
  const lines = lineCount(a.text);
  return h(
    "div",
    { class: "artifact-panel", role: "dialog", "aria-label": `Artifact: ${a.title}` },
    h(
      "div",
      { class: "artifact-head" },
      h(
        "span",
        { class: "artifact-title" },
        a.title,
        h("small", null, ` · ${lines} line${lines === 1 ? "" : "s"}`),
      ),
      copyButton("artifact", a.text, state, dispatch),
      h(
        "button",
        {
          type: "button",
          class: "tool download",
          title: `Save as ${a.filename}`,
          onClick: () => dispatch({ type: "download", artifact: a }),
        },
        "Download",
      ),
      h(
        "button",
        {
          type: "button",
          class: "icon close",
          "aria-label": "Close",
          title: "Close",
          onClick: () => dispatch({ type: "close_artifact" }),
        },
        "×",
      ),
    ),
    h("pre", { class: "artifact-body" }, h("code", null, a.text)),
  );
}

/** Notices and the link confirm panel. */
export function overlayView(state: ChatState, dispatch: Dispatch): VNode {
  const notices = state.notices.map((n) =>
    h(
      "div",
      { class: `notice ${n.level}`, role: n.level === "error" ? "alert" : "status" },
      h("span", null, n.text),
      h(
        "button",
        {
          type: "button",
          class: "dismiss",
          "aria-label": "Dismiss",
          onClick: () => dispatch({ type: "dismiss", id: n.id }),
        },
        "×",
      ),
    ),
  );
  const confirm =
    state.confirm === null
      ? null
      : h(
          "section",
          { class: "confirm", role: "dialog", "aria-label": "Open link" },
          h("p", null, "Open this link in your default browser?"),
          h("code", { class: "url" }, state.confirm),
          h(
            "div",
            { class: "row" },
            h(
              "button",
              {
                type: "button",
                class: "primary",
                onClick: () => dispatch({ type: "confirm_open" }),
              },
              "Open",
            ),
            h(
              "button",
              {
                type: "button",
                class: "ghost",
                onClick: () => dispatch({ type: "confirm_cancel" }),
              },
              "Cancel",
            ),
          ),
        );
  return h("div", { class: "overlay" }, notices, confirm);
}

/** Send, or Stop while a turn runs. */
export function actionsView(state: ChatState, dispatch: Dispatch): VNode {
  const run = runningTurn(state);
  if (run !== null) {
    return h(
      "div",
      { class: "actions" },
      h(
        "button",
        {
          type: "button",
          class: "stop",
          disabled: run.stopRequested,
          onClick: () => dispatch({ type: "stop" }),
        },
        run.stopRequested ? "Stopping…" : "Stop",
      ),
    );
  }
  return h("div", { class: "actions" }, h("button", { type: "submit", class: "send" }, "Send"));
}
