// Plan 08b step 4: the chat tab's sanitizer (threat T3; plan 08b invariant "egress"). Model text
// is parsed into a small typed tree of a Markdown subset; the view (step 5) builds DOM nodes from
// the tree with textContent only. Nothing here produces or accepts an HTML string:
// - raw HTML in the model text stays literal text (`<` has no meaning here);
// - every image (`![…](…)`, any URL, `data:` included) becomes `image_removed`, its URL dropped;
// - bare URLs, autolinks `<https://…>` and reference-style links (`[x][1]` + `[1]: url`) stay text;
// - an inline link `[label](url)` keeps its href only for an absolute `http:`/`https:` URL (the
//   view renders it inert, opened only after an in-page confirm, P9); otherwise only its label
//   is kept, as text;
// - `[citation removed]` (the daemon's marker) is `removed`, also when `(url)` follows (that
//   stays text);
// - citations are found first, over the whole text, by ui/chat/citations.ts (the daemon's exact
//   grammar, A8), so a citation that spans lines is still one `cite`; inside a code span or a
//   code block a citation is shown as its raw text.
// Subset: paragraphs (a single newline is a line break), `#`–`######` headings (levels above 3
// render as 3), `-`/`*`/`+` and `1.`/`1)` list items (flat: indentation is not nesting; blank
// lines between items of one kind keep one list; an ordered list keeps its first number as
// `start`), fenced code blocks (``` or ~~~; an info string that is a short language tag is kept
// as the block's `lang`, lower-cased, any other info string as the block's first line), GFM
// pipe tables (a header row, a delimiter row of `---`/`:--`/`--:`/`:-:` cells with as many cells,
// then rows up to a blank line or a line without `|`; at most MAX_COLS columns; cells are inline
// text, a row's missing cells are empty and extra ones dropped), code spans, `**`/`__` strong,
// `*`/`_` em (an intraword `_` is text), backslash escapes of ASCII punctuation. Everything else is
// text. Pure TS, no DOM.

import { REMOVED_MARKER, tokenizeCitations } from "./chat/citations";

export type Inline =
  | { t: "text"; text: string }
  | { t: "strong"; children: Inline[] }
  | { t: "em"; children: Inline[] }
  | { t: "code"; text: string }
  | { t: "br" }
  | { t: "link"; href: string; label: string }
  | { t: "cite"; mid: string; raw: string }
  | { t: "removed" }
  | { t: "image_removed" };

export interface ListItem {
  t: "li";
  children: Inline[];
}

export type HeadingLevel = 1 | 2 | 3;

/** A table cell: inline text. */
export interface TableCell {
  t: "td";
  children: Inline[];
}

/** A table column's alignment from its delimiter cell (`:--`, `--:`, `:-:`), or null. */
export type Align = "left" | "center" | "right" | null;

export type Block =
  | { t: "p"; children: Inline[] }
  | { t: "h"; level: HeadingLevel; children: Inline[] }
  // `lang` is the fence's language tag (lower-cased), present only when there was one.
  | { t: "pre"; text: string; lang?: string }
  | { t: "ul"; items: ListItem[] }
  // `start` is the first item's number, present only when it is not 1 (P29).
  | { t: "ol"; items: ListItem[]; start?: number }
  // Every row (`head` included) has exactly `align.length` cells.
  | { t: "table"; align: Align[]; head: TableCell[]; rows: TableCell[][] };

export type Node = Block | ListItem | TableCell | Inline;

// Citations are replaced by `\ue000<index>\ue001` before the Markdown pass; those two private-use
// characters, if the model wrote them, are shown as U+FFFD (they cannot be told apart otherwise).
const PH_OPEN = "\ue000";
const PH_CLOSE = "\ue001";
const PH_AT = /\ue000(\d+)\ue001/y;
const PH_ALL = /\ue000(\d+)\ue001/g;
const PUA_INPUT = /[\ue000\ue001]/g;

interface Cite {
  mid: string;
  raw: string;
}

/** The model's text as a typed tree (module comment). */
export function sanitize(text: string): Block[] {
  targetReads = 0;
  const cites: Cite[] = [];
  let src = "";
  for (const tok of tokenizeCitations(text)) {
    if (tok.kind === "text") {
      src += tok.text.replace(PUA_INPUT, "\ufffd");
    } else {
      src += `${PH_OPEN}${cites.length}${PH_CLOSE}`;
      cites.push({ mid: tok.mid, raw: tok.raw });
    }
  }
  return parseBlocks(src.replace(/\r\n?/g, "\n"), cites);
}

function restore(s: string, cites: readonly Cite[]): string {
  return s.replace(PH_ALL, (_m, n: string) => citeAt(cites, n).raw);
}

function citeAt(cites: readonly Cite[], n: string): Cite {
  const c = cites[Number(n)];
  if (c === undefined) {
    throw new Error(`sanitize: citation placeholder ${n} out of range`);
  }
  return c;
}

// ---- blocks ----

const FENCE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
// An info string dropped as a language tag (`ts`, `c++`, `objective-c`); any other is kept as the
// code block's first line, so model text cannot hide words behind a fence (P30).
const LANG_TAG = /^[A-Za-z0-9_+.#-]{1,20}$/;
const HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
const UL_ITEM = /^[ \t]*[-*+][ \t]+(.*)$/;
const OL_ITEM = /^[ \t]*(\d{1,9})[.)][ \t]+(.*)$/;
const BLANK = /^[ \t]*$/;
// After a blank line inside a list, an indented line that is no other block joins the last item.
const INDENTED = /^(?: {2,}|\t)/;
// A table has at most this many columns; a wider one is text.
const MAX_COLS = 64;
const DELIM_CELL = /^:?-+:?$/;

/**
 * A table row's cells as raw text: one leading and one trailing `|` dropped, split on every `|`
 * not escaped by a backslash, each cell trimmed. Null when the line has no unescaped `|`.
 */
function rowCells(line: string): string[] | null {
  const cells: string[] = [];
  let cell = "";
  let pipes = 0;
  for (let k = 0; k < line.length; k += 1) {
    const ch = line[k];
    if (ch === "\\" && k + 1 < line.length) {
      cell += ch + line[k + 1];
      k += 1;
    } else if (ch === "|") {
      pipes += 1;
      cells.push(cell);
      cell = "";
    } else {
      cell += ch;
    }
  }
  if (pipes === 0) {
    return null;
  }
  cells.push(cell);
  const trimmed = cells.map((c) => c.trim());
  if (trimmed[0] === "" && /^\s*\|/.test(line)) {
    trimmed.shift();
  }
  // An escaped trailing `\|` stays in its cell, so an empty last cell means an unescaped `|`.
  if (trimmed.length > 0 && trimmed[trimmed.length - 1] === "" && /\|\s*$/.test(line)) {
    trimmed.pop();
  }
  return trimmed;
}

/** The column alignments when `line` is a delimiter row of exactly `n` cells, else null. */
function delimiterRow(line: string, n: number): Align[] | null {
  const cells = rowCells(line);
  if (cells === null || cells.length !== n || !cells.every((c) => DELIM_CELL.test(c))) {
    return null;
  }
  return cells.map((c): Align => {
    const left = c.startsWith(":");
    const right = c.endsWith(":");
    if (left && right) {
      return "center";
    }
    if (right) {
      return "right";
    }
    return left ? "left" : null;
  });
}

/** The list kind `line` starts an item of, or null. */
function itemKind(line: string): "ul" | "ol" | null {
  if (UL_ITEM.test(line)) {
    return "ul";
  }
  return OL_ITEM.test(line) ? "ol" : null;
}

function parseBlocks(src: string, cites: readonly Cite[]): Block[] {
  const lines = src.split("\n");
  const blocks: Block[] = [];
  let para: string[] | null = null;
  let list: { kind: "ul" | "ol"; start: number; items: string[][] } | null = null;

  const closePara = (): void => {
    if (para !== null) {
      blocks.push({ t: "p", children: parseInline(para.join("\n"), cites) });
      para = null;
    }
  };
  const closeList = (): void => {
    if (list !== null) {
      const items = list.items.map(
        (lines): ListItem => ({ t: "li", children: parseInline(lines.join("\n"), cites) }),
      );
      if (list.kind === "ul") {
        blocks.push({ t: "ul", items });
      } else {
        blocks.push(list.start === 1 ? { t: "ol", items } : { t: "ol", items, start: list.start });
      }
      list = null;
    }
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    const fence = FENCE.exec(line);
    if (fence !== null) {
      closePara();
      closeList();
      const marker = fence[1] ?? "```";
      const closing = new RegExp(
        `^ {0,3}${marker[0] === "`" ? "`" : "~"}{${marker.length},}[ \\t]*$`,
      );
      const info = (fence[2] ?? "").trim();
      const body: string[] = info === "" || LANG_TAG.test(info) ? [] : [info];
      i += 1;
      while (i < lines.length && !closing.test(lines[i] ?? "")) {
        body.push(lines[i] ?? "");
        i += 1;
      }
      i += 1; // the closing fence, or past the end
      const text = restore(body.join("\n"), cites);
      blocks.push(
        info !== "" && LANG_TAG.test(info)
          ? { t: "pre", text, lang: info.toLowerCase() }
          : { t: "pre", text },
      );
      continue;
    }
    if (BLANK.test(line)) {
      closePara();
      if (list !== null) {
        // A blank line inside a list (P29): the list goes on when the next non-blank line is an
        // item of the same kind, or an indented line that is no other block (it joins the last
        // item after a line break pair); anything else closes it.
        let j = i + 1;
        while (j < lines.length && BLANK.test(lines[j] ?? "")) {
          j += 1;
        }
        const next = lines[j];
        if (next !== undefined) {
          const kind = itemKind(next);
          if (kind === list.kind) {
            i = j;
            continue;
          }
          if (kind === null && INDENTED.test(next) && !FENCE.test(next) && !HEADING.test(next)) {
            const last = list.items[list.items.length - 1];
            if (last === undefined) {
              throw new Error("sanitize: open list without an item");
            }
            last.push("");
            i = j;
            continue;
          }
        }
      }
      closeList();
      i += 1;
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading !== null) {
      closePara();
      closeList();
      const level = Math.min((heading[1] ?? "#").length, 3) as HeadingLevel;
      blocks.push({ t: "h", level, children: parseInline(heading[2] ?? "", cites) });
      i += 1;
      continue;
    }
    const head = rowCells(line);
    const align =
      head !== null && head.length > 0 && head.length <= MAX_COLS && i + 1 < lines.length
        ? delimiterRow(lines[i + 1] ?? "", head.length)
        : null;
    if (head !== null && align !== null) {
      closePara();
      closeList();
      const cellsOf = (raw: readonly string[]): TableCell[] =>
        align.map((_a, c) => ({ t: "td", children: parseInline(raw[c] ?? "", cites) }));
      const rows: TableCell[][] = [];
      i += 2;
      while (i < lines.length) {
        const row = lines[i] ?? "";
        if (BLANK.test(row) || FENCE.test(row) || HEADING.test(row)) {
          break;
        }
        const raw = rowCells(row);
        if (raw === null) {
          break;
        }
        rows.push(cellsOf(raw));
        i += 1;
      }
      blocks.push({ t: "table", align, head: cellsOf(head), rows });
      continue;
    }
    const ul = UL_ITEM.exec(line);
    const ol = ul === null ? OL_ITEM.exec(line) : null;
    const item = ul ?? ol;
    if (item !== null) {
      closePara();
      const kind = ul !== null ? "ul" : "ol";
      if (list !== null && list.kind !== kind) {
        closeList();
      }
      if (list === null) {
        list = { kind, start: ol !== null ? Number(ol[1]) : 1, items: [] };
      }
      list.items.push([(ul !== null ? ul[1] : ol?.[2]) ?? ""]);
      i += 1;
      continue;
    }
    // A continuation line: part of the open list item, else of the paragraph.
    if (list !== null) {
      const last = list.items[list.items.length - 1];
      if (last === undefined) {
        throw new Error("sanitize: open list without an item");
      }
      last.push(line.trimStart());
    } else {
      para = para ?? [];
      para.push(line);
    }
    i += 1;
  }
  closePara();
  closeList();
  return blocks;
}

// ---- inlines ----

const ASCII_PUNCT = /[!-/:-@[-`{-~]/;
const WORD = /[\p{L}\p{N}]/u;
const SPACE = /\s/;

function isSpaceAt(s: string, k: number): boolean {
  const ch = s[k];
  return ch === undefined || SPACE.test(ch);
}

function isWordAt(s: string, k: number): boolean {
  const ch = s[k];
  return ch !== undefined && WORD.test(ch);
}

function runLength(s: string, k: number, ch: string): number {
  let n = 0;
  while (s[k + n] === ch) {
    n += 1;
  }
  return n;
}

/** Backslash escapes of ASCII punctuation removed (labels, link targets). */
function unescapePunct(s: string): string {
  return s.replace(/\\([!-/:-@[-`{-~])/g, "$1");
}

// A link target (from `(` to `)`) longer than this, or a bare destination nesting parentheses
// deeper than this, is not a link (it stays text). Both bound the work of one target scan, so
// a text of unclosed targets such as `[a](x[a](x…` stays linear (P31).
const MAX_TARGET = 2048;
const MAX_PAREN_DEPTH = 32;

// Characters the link-target scans of the last `sanitize` call reached (each scan's furthest
// index, summed). A work count instead of a time: tests pin P31's linear bound with it, since a
// wall-clock budget does not belong in the fast check.
let targetReads = 0;

/** Characters read by link-target scans during the last `sanitize` call (P31; for tests). */
export function targetScanReads(): number {
  return targetReads;
}

/** Only absolute http(s) URLs survive as a link target (P9). */
function safeHref(dest: string): string | null {
  if (!URL.canParse(dest)) {
    return null;
  }
  const url = new URL(dest);
  return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
}

class InlineParser {
  private readonly out: Inline[] = [];
  private buf = "";
  // Closer searches that failed: no closer at or after this index (keeps a paragraph of
  // unmatched delimiters linear instead of quadratic).
  private readonly noCodeCloser = new Map<number, number>();
  private readonly noEmCloser = new Map<string, number>();

  constructor(
    private readonly s: string,
    private readonly cites: readonly Cite[],
  ) {}

  parse(): Inline[] {
    const s = this.s;
    let i = 0;
    while (i < s.length) {
      const ch = s[i] ?? "";
      if (ch === "\\" && i + 1 < s.length && ASCII_PUNCT.test(s[i + 1] ?? "")) {
        this.buf += s[i + 1];
        i += 2;
      } else if (ch === "\n") {
        this.push({ t: "br" });
        i += 1;
      } else if (ch === PH_OPEN) {
        i = this.placeholder(i);
      } else if (ch === "`") {
        i = this.codeSpan(i);
      } else if (ch === "!" && s[i + 1] === "[") {
        i = this.image(i);
      } else if (ch === "[") {
        i = this.bracket(i);
      } else if (ch === "*" || ch === "_") {
        i = this.emphasis(i, ch);
      } else {
        this.buf += ch;
        i += 1;
      }
    }
    this.flush();
    return this.out;
  }

  private flush(): void {
    if (this.buf !== "") {
      this.out.push({ t: "text", text: this.buf });
      this.buf = "";
    }
  }

  private push(node: Inline): void {
    this.flush();
    this.out.push(node);
  }

  private placeholder(i: number): number {
    PH_AT.lastIndex = i;
    const m = PH_AT.exec(this.s);
    if (m === null) {
      throw new Error("sanitize: malformed citation placeholder");
    }
    const c = citeAt(this.cites, m[1] ?? "");
    this.push({ t: "cite", mid: c.mid, raw: c.raw });
    return PH_AT.lastIndex;
  }

  /** The index of the closing run of exactly `n` backticks after `from`, or -1. */
  private codeCloser(from: number, n: number): number {
    const failed = this.noCodeCloser.get(n);
    if (failed !== undefined && from >= failed) {
      return -1;
    }
    const s = this.s;
    let k = from;
    while (k < s.length) {
      if (s[k] === "`") {
        const r = runLength(s, k, "`");
        if (r === n) {
          return k;
        }
        k += r;
      } else {
        k += 1;
      }
    }
    this.noCodeCloser.set(n, Math.min(from, failed ?? from));
    return -1;
  }

  private codeSpan(i: number): number {
    const s = this.s;
    const n = runLength(s, i, "`");
    const close = this.codeCloser(i + n, n);
    if (close < 0) {
      this.buf += "`".repeat(n);
      return i + n;
    }
    let text = restore(s.slice(i + n, close), this.cites).replace(/\n/g, " ");
    if (text.length > 1 && text.startsWith(" ") && text.endsWith(" ") && text.trim() !== "") {
      text = text.slice(1, -1);
    }
    this.push({ t: "code", text });
    return close + n;
  }

  /** `[` at `i`: the end index of its label (the `]`), or -1. No nested `[`, no newline. */
  private labelEnd(i: number): number {
    const s = this.s;
    let k = i + 1;
    while (k < s.length) {
      const ch = s[k];
      if (ch === "\\" && k + 1 < s.length) {
        k += 2;
      } else if (ch === "]") {
        return k;
      } else if (ch === "[" || ch === "\n") {
        return -1;
      } else {
        k += 1;
      }
    }
    return -1;
  }

  /** `(` at `i`: the link target and the index after the `)`, or null. Reads at most
   * `MAX_TARGET` characters from `i` (P31), counted in `targetReads`. */
  private target(i: number): { dest: string; end: number } | null {
    const s = this.s;
    if (s[i] !== "(") {
      return null;
    }
    const limit = Math.min(s.length, i + MAX_TARGET);
    let k = i + 1;
    let close = i;
    try {
      while (k < limit && (s[k] === " " || s[k] === "\t")) {
        k += 1;
      }
      let dest = "";
      if (s[k] === "<") {
        close = k + 1;
        while (close < limit && s[close] !== ">" && s[close] !== "<" && s[close] !== "\n") {
          close += 1;
        }
        if (close >= limit || s[close] !== ">") {
          return null;
        }
        dest = s.slice(k + 1, close);
        k = close + 1;
      } else {
        const start = k;
        let depth = 0;
        while (k < limit && !isSpaceAt(s, k)) {
          const ch = s[k];
          if (ch === "\\" && k + 1 < s.length) {
            k += 2;
            continue;
          }
          if (ch === "(") {
            depth += 1;
            if (depth > MAX_PAREN_DEPTH) {
              return null;
            }
          } else if (ch === ")") {
            if (depth === 0) {
              break;
            }
            depth -= 1;
          }
          k += 1;
        }
        if (depth !== 0 || k >= limit) {
          return null;
        }
        dest = s.slice(start, k);
      }
      while (k < limit && (s[k] === " " || s[k] === "\t")) {
        k += 1;
      }
      const quote = k < limit ? s[k] : undefined;
      if (quote === '"' || quote === "'" || quote === "(") {
        const closeCh = quote === "(" ? ")" : quote;
        close = k + 1;
        while (close < limit && s[close] !== closeCh && s[close] !== "\n") {
          close += 1;
        }
        if (close >= limit || s[close] !== closeCh) {
          return null;
        }
        k = close + 1;
        while (k < limit && (s[k] === " " || s[k] === "\t")) {
          k += 1;
        }
      }
      if (k >= limit || s[k] !== ")") {
        return null;
      }
      return { dest: unescapePunct(dest), end: k + 1 };
    } finally {
      // Every scan above moves forward from `i`, so the furthest index read is max(k, close).
      targetReads += Math.min(limit, Math.max(k, close) + 1) - i;
    }
  }

  private image(i: number): number {
    const end = this.labelEnd(i + 1);
    const target = end < 0 ? null : this.target(end + 1);
    if (target === null) {
      this.buf += "!";
      return i + 1;
    }
    this.push({ t: "image_removed" });
    return target.end;
  }

  private bracket(i: number): number {
    if (this.s.startsWith(REMOVED_MARKER, i)) {
      this.push({ t: "removed" });
      return i + REMOVED_MARKER.length;
    }
    const end = this.labelEnd(i);
    const target = end < 0 ? null : this.target(end + 1);
    if (target === null) {
      this.buf += "[";
      return i + 1;
    }
    const label = unescapePunct(restore(this.s.slice(i + 1, end), this.cites));
    const href = safeHref(target.dest);
    if (href === null) {
      this.buf += label;
    } else {
      this.push({ t: "link", href, label: label === "" ? href : label });
    }
    return target.end;
  }

  /** The start of the closing delimiter (`need` chars of `ch`) after `from`, or -1. */
  private emCloser(from: number, ch: string, need: number): number {
    const key = `${ch}${need}`;
    const failed = this.noEmCloser.get(key);
    if (failed !== undefined && from >= failed) {
      return -1;
    }
    const s = this.s;
    let k = from;
    while (k < s.length) {
      const c = s[k];
      if (c === "`") {
        const n = runLength(s, k, "`");
        const close = this.codeCloser(k + n, n);
        k = close < 0 ? k + n : close + n;
        continue;
      }
      if (c !== ch) {
        k += 1;
        continue;
      }
      const r = runLength(s, k, ch);
      const fits = need === 2 ? r >= 2 : r === 1 || r >= 3;
      if (fits && k > from && !isSpaceAt(s, k - 1) && (ch !== "_" || !isWordAt(s, k + r))) {
        return k + r - need;
      }
      k += r;
    }
    this.noEmCloser.set(key, Math.min(from, failed ?? from));
    return -1;
  }

  private emphasis(i: number, ch: string): number {
    const s = this.s;
    const r = runLength(s, i, ch);
    // An opener is followed by a non-space after the whole run (`** x*` is text) and, for `_`,
    // not preceded by a letter or digit.
    if (isSpaceAt(s, i + r) || (ch === "_" && isWordAt(s, i - 1))) {
      this.buf += ch.repeat(r);
      return i + r;
    }
    for (const need of r >= 2 ? [2, 1] : [1]) {
      const close = this.emCloser(i + need, ch, need);
      if (close < 0) {
        continue;
      }
      const children = new InlineParser(s.slice(i + need, close), this.cites).parse();
      this.push(need === 2 ? { t: "strong", children } : { t: "em", children });
      return close + need;
    }
    this.buf += ch.repeat(r);
    return i + r;
  }
}

function parseInline(s: string, cites: readonly Cite[]): Inline[] {
  return new InlineParser(s, cites).parse();
}
