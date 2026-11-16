// Plan 08b step 5: the chat tab page's security surface (T3, invariants "egress", "no send",
// "tb_id never stored"): the manifest's CSP and permissions, index.html with nothing inline and
// nothing remote, the build entry for the page, and a source scan of the tab and the router for
// HTML sinks, dynamic code and persistence APIs.

import { describe, expect, it } from "vitest";
import { MAX_CHAT_TEXT } from "../src/ui/chat/state";

declare global {
  interface ImportMeta {
    glob(
      pattern: string | string[],
      opts: { query: "?raw"; import: "default"; eager: true },
    ): Record<string, string>;
  }
}

const files = import.meta.glob(
  ["../manifest.json", "../esbuild.mjs", "../src/ui/chat/index.html", "../src/ui/chat/chat.css"],
  { query: "?raw", import: "default", eager: true },
);

function file(path: string): string {
  const text = files[path];
  if (text === undefined) throw new Error(`${path} not found`);
  return text;
}

const manifest = JSON.parse(file("../manifest.json")) as Record<string, unknown>;
const esbuildConfig = file("../esbuild.mjs");
const indexHtml = file("../src/ui/chat/index.html");
const chatCss = file("../src/ui/chat/chat.css");

const CSP = "default-src 'self'; img-src 'self' data:";

describe("manifest", () => {
  it("sets exactly the chat page CSP", () => {
    expect(manifest.content_security_policy).toEqual({ extension_pages: CSP });
  });

  it("asks for no new permission (spaces need none; nothing that can send)", () => {
    expect(manifest.permissions).toEqual([
      "nativeMessaging",
      "messagesRead",
      "messagesTagsList",
      "accountsRead",
      "storage",
      "alarms",
    ]);
    expect(Object.keys(manifest)).not.toContain("host_permissions");
    expect(Object.keys(manifest)).not.toContain("optional_permissions");
    expect(JSON.stringify(manifest)).not.toMatch(/compose|messagesModify|\.send/);
  });
});

describe("chat page index.html", () => {
  it("loads its script and stylesheet as files next to it", () => {
    expect(indexHtml).toMatch(/<script type="module" src="main\.js"><\/script>/);
    expect(indexHtml).toMatch(/<link rel="stylesheet" href="chat\.css" \/>/);
  });

  it("has no inline script or style, no style= or on*= attribute, no remote URL", () => {
    for (const m of indexHtml.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
      expect(m[1], "script without src").toMatch(/\bsrc="[^"]+"/);
      expect(m[2]?.trim(), "inline script body").toBe("");
    }
    expect(indexHtml).not.toMatch(/<style\b/i);
    expect(indexHtml).not.toMatch(/\sstyle\s*=/i);
    expect(indexHtml).not.toMatch(/\son[a-z]+\s*=/i);
    expect(indexHtml).not.toMatch(/https?:/i);
    expect(indexHtml).not.toMatch(/javascript:/i);
  });

  it("has the four regions, the form and the input main.ts looks up", () => {
    for (const id of ["header", "log", "overlay", "composer", "input", "actions"]) {
      expect(indexHtml).toContain(`id="${id}"`);
    }
    expect(indexHtml).toContain('maxlength="8000"');
  });

  it("the textarea's maxlength is state.ts's MAX_CHAT_TEXT (ternd's own copy: test_chat.py)", () => {
    const m = [...indexHtml.matchAll(/\bmaxlength="(\d+)"/g)];
    expect(m).toHaveLength(1);
    expect(Number(m[0]?.[1])).toBe(MAX_CHAT_TEXT);
  });

  it("chat.css loads nothing (no url(), no @import)", () => {
    expect(chatCss).not.toMatch(/url\s*\(/i);
    expect(chatCss).not.toMatch(/@import/i);
  });

  it("esbuild builds the page entry and copies index.html and chat.css next to it", () => {
    expect(esbuildConfig).toContain('"ui/chat/main": "src/ui/chat/main.ts"');
    expect(esbuildConfig).toMatch(/\["index\.html", "chat\.css"\]/);
    expect(esbuildConfig).toContain("/ui/chat/$" + "{file}`"); // the copy's target dir
  });
});

// The tab page and the router: no HTML sinks, no dynamic code, and no storage API of their own.
// The tab saves its chats (ui/chat/history.ts) only through tb/api.ts's chatPageStore, which is
// the extension's storage.local (the TB profile; never synced): see "chat page store" below.
// Raw text (comments included), so these words cannot hide in a string or a comment either.
const scanned = import.meta.glob(["../src/ui/**/*.ts", "../src/background/chat.ts"], {
  query: "?raw",
  import: "default",
  eager: true,
});

const FORBIDDEN = [
  "innerHTML",
  "outerHTML",
  "insertAdjacentHTML",
  "dangerouslySetInnerHTML",
  "document.write",
  "eval(",
  "new Function",
  "storage",
  "localStorage",
  "sessionStorage",
  "indexedDB",
];

/** The FORBIDDEN words in `text`, in list order. */
function hitsIn(text: string): string[] {
  return FORBIDDEN.filter((w) => text.includes(w));
}

describe("chat sources", () => {
  it("scans the tab's modules and the router", () => {
    const paths = Object.keys(scanned);
    for (const f of [
      "../src/ui/sanitize.ts",
      "../src/ui/chat/citations.ts",
      "../src/ui/chat/history.ts",
      "../src/ui/chat/artifacts.ts",
      "../src/ui/chat/main.ts",
      "../src/ui/chat/state.ts",
      "../src/ui/chat/view.ts",
      "../src/ui/chat/vdom.ts",
      "../src/background/chat.ts",
    ]) {
      expect(paths).toContain(f);
    }
  });

  it("none names an HTML sink, dynamic code or a storage API", () => {
    const hits = Object.entries(scanned).flatMap(([path, text]) =>
      hitsIn(text).map((w) => `${path}: ${w}`),
    );
    expect(hits).toEqual([]);
  });

  it("the scan reports each word in a synthetic source, and nothing in a clean one", () => {
    for (const w of FORBIDDEN) {
      expect(hitsIn(`// synthetic\nconst el = x;\n${w}\n`), w).toContain(w);
    }
    expect(hitsIn("const el = document.createElement('p');\nel.textContent = t;\n")).toEqual([]);
  });
});

const apiSource = import.meta.glob("../src/tb/api.ts", {
  query: "?raw",
  import: "default",
  eager: true,
});

describe("chat page store", () => {
  it("tb/api.ts gives the page storage.local only (never sync or session storage)", () => {
    const api = apiSource["../src/tb/api.ts"] ?? "";
    expect(api).toContain("messenger.storage.local");
    expect(api).not.toMatch(/storage\s*\.\s*(sync|session|managed)/);
  });
});
