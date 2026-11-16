// Plan 08b step 4: the chat tab's citation tokenizer over the fixture shared with
// tests/agent/test_citation_grammar_parity.py (A8). Synthetic mids only.

import { describe, expect, it } from "vitest";
import {
  findCitations,
  isCiteSpace,
  MAX_WS,
  REMOVED_MARKER,
  tokenizeCitations,
} from "../src/ui/chat/citations";
import fixtureJson from "./fixtures/citation_grammar.json" with { type: "json" };

interface GrammarCase {
  name: string;
  text: string;
  mids: string[];
}

const fixture = fixtureJson as unknown as {
  max_ws: number;
  removed_marker: string;
  whitespace: number[];
  cases: GrammarCase[];
};

const A = "a".repeat(64);
const X = `x-${"b".repeat(64)}`;

describe("shared citation grammar fixture", () => {
  it.each(fixture.cases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    expect(findCitations(c.text)).toEqual(c.mids);
  });

  it("holds citations and non-citations", () => {
    expect(fixture.cases.filter((c) => c.mids.length > 0).length).toBeGreaterThanOrEqual(10);
    expect(fixture.cases.filter((c) => c.mids.length === 0).length).toBeGreaterThanOrEqual(10);
  });

  it("carries the constants the tab uses", () => {
    expect(MAX_WS).toBe(fixture.max_ws);
    expect(REMOVED_MARKER).toBe(fixture.removed_marker);
  });

  it("whitespace is exactly the fixture's str.isspace set over every code point", () => {
    const ours: number[] = [];
    for (let cp = 0; cp <= 0x10ffff; cp += 1) {
      if (cp >= 0xd800 && cp <= 0xdfff) {
        continue; // lone surrogates are not code points of a well-formed string
      }
      if (isCiteSpace(String.fromCodePoint(cp))) {
        ours.push(cp);
      }
    }
    expect(ours).toEqual(fixture.whitespace);
    // JavaScript's own \s differs: it has U+FEFF and lacks U+001C and U+0085.
    expect(/\s/.test("\ufeff")).toBe(true);
    expect(isCiteSpace("\ufeff")).toBe(false);
    expect(isCiteSpace("\x1c")).toBe(true);
    expect(isCiteSpace("\x85")).toBe(true);
  });
});

describe("tokenizeCitations", () => {
  it("keeps the raw text of each citation and the text around it", () => {
    const raw = `【 mid: ${X} 】`;
    expect(tokenizeCitations(`One [${A}] two ${raw}.`)).toEqual([
      { kind: "text", text: "One " },
      { kind: "cite", mid: A, raw: `[${A}]` },
      { kind: "text", text: " two " },
      { kind: "cite", mid: X, raw },
      { kind: "text", text: "." },
    ]);
  });

  it("returns one text token when there is no citation, and nothing for empty text", () => {
    expect(tokenizeCitations(`[${A}】 [citation removed]`)).toEqual([
      { kind: "text", text: `[${A}】 [citation removed]` },
    ]);
    expect(tokenizeCitations("")).toEqual([]);
  });

  it("joins the tokens back into the input", () => {
    for (const c of fixture.cases) {
      const joined = tokenizeCitations(c.text)
        .map((t) => (t.kind === "text" ? t.text : t.raw))
        .join("");
      expect(joined).toBe(c.text);
    }
  });

  it("is not confused by a lone surrogate or astral text around a citation", () => {
    expect(findCitations(`\ud83d[${A}]\u{1f600}`)).toEqual([A]);
  });
});
