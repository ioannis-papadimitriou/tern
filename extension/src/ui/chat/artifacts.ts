// The chat tab's "artifacts": a fenced code block, a table or a long list of an answer, as plain
// text the user can copy, open in the side panel or download as a file. Built from the
// sanitizer's tree only (never from raw model text), so a table's or list's text is what the tab
// shows: links keep their label only, a removed image or citation keeps its fixed label, a chip
// its raw citation text. Nothing is ever rendered as HTML or run. Pure TS, no DOM.

import type { Block, Inline, TableCell } from "../sanitize";
import { REMOVED_MARKER } from "./citations";
import type { Artifact } from "./state";

/** A list with at least this many items gets the artifact header too. */
export const LONG_LIST = 15;

export const IMAGE_REMOVED_LABEL = "[image removed]";

/** A file extension per fence language tag (lower case); anything else is `txt`. */
const EXT: Readonly<Record<string, string>> = {
  bash: "sh",
  c: "c",
  "c++": "cpp",
  cpp: "cpp",
  "c#": "cs",
  cs: "cs",
  csharp: "cs",
  css: "css",
  csv: "csv",
  diff: "diff",
  go: "go",
  html: "html",
  ics: "ics",
  ini: "ini",
  java: "java",
  javascript: "js",
  js: "js",
  json: "json",
  jsx: "jsx",
  kotlin: "kt",
  latex: "tex",
  lua: "lua",
  markdown: "md",
  md: "md",
  perl: "pl",
  php: "php",
  py: "py",
  python: "py",
  rb: "rb",
  ruby: "rb",
  rs: "rs",
  rust: "rs",
  sh: "sh",
  shell: "sh",
  sql: "sql",
  swift: "swift",
  tex: "tex",
  toml: "toml",
  ts: "ts",
  tsx: "tsx",
  tsv: "tsv",
  typescript: "ts",
  vcard: "vcf",
  xml: "xml",
  yaml: "yaml",
  yml: "yaml",
  zsh: "sh",
};

export function extFor(lang: string | undefined): string {
  return (lang !== undefined && EXT[lang]) || "txt";
}

/** An inline tree as plain text (labels, not URLs; line breaks as `br`). */
export function plainInline(xs: readonly Inline[], br = "\n"): string {
  return xs.map((n) => plainNode(n, br)).join("");
}

function plainNode(n: Inline, br: string): string {
  switch (n.t) {
    case "text":
    case "code":
      return n.text;
    case "strong":
    case "em":
      return plainInline(n.children, br);
    case "br":
      return br;
    case "link":
      return n.label;
    case "cite":
      return n.raw;
    case "removed":
      return REMOVED_MARKER;
    case "image_removed":
      return IMAGE_REMOVED_LABEL;
  }
}

function cellText(c: TableCell): string {
  return plainInline(c.children, " ").replace(/\|/g, "\\|");
}

/** A table as a GFM pipe table. */
export function tableMarkdown(b: Extract<Block, { t: "table" }>): string {
  const row = (cells: readonly TableCell[]): string => `| ${cells.map(cellText).join(" | ")} |`;
  const delim = b.align.map((a) => {
    if (a === "center") return ":---:";
    if (a === "right") return "---:";
    return a === "left" ? ":---" : "---";
  });
  return [row(b.head), `| ${delim.join(" | ")} |`, ...b.rows.map(row)].join("\n");
}

/** A list as Markdown (`-` items, or numbers from its start). */
export function listMarkdown(b: Extract<Block, { t: "ul" | "ol" }>): string {
  const start = b.t === "ol" ? (b.start ?? 1) : 0;
  return b.items
    .map((li, i) => {
      const mark = b.t === "ol" ? `${start + i}.` : "-";
      return `${mark} ${plainInline(li.children, "\n  ")}`;
    })
    .join("\n");
}

/** The artifact of a block that gets the artifact header, else null. */
export function artifactOf(b: Block): Artifact | null {
  switch (b.t) {
    case "pre":
      return {
        title: b.lang ?? "code",
        text: b.text,
        filename: `tern-snippet.${extFor(b.lang)}`,
      };
    case "table":
      return { title: "table", text: tableMarkdown(b), filename: "tern-table.md" };
    case "ul":
    case "ol":
      return b.items.length >= LONG_LIST
        ? { title: "list", text: listMarkdown(b), filename: "tern-list.md" }
        : null;
    default:
      return null;
  }
}

/** How many lines `text` has (the panel's title shows it). */
export function lineCount(text: string): number {
  return text === "" ? 0 : text.split("\n").length;
}
