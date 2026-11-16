// Plan 08b step 4: the chat tab's citation tokenizer. It accepts exactly the grammar of
// `tern.agent.citations` (src/tern/agent/citations.py, plan 08a step 4; #35 note), so the tab
// renders a chip only for text the daemon's filter would treat as a citation:
// - a matched pair `[`…`]` or `【`…`】` (`[…】` and `【…]` are not citations);
// - inside: optional whitespace, an optional lower-case `mid:` prefix followed by optional
//   whitespace, the bridge `Mid` pattern `(x-)?[0-9a-f]{64}`, optional whitespace;
// - whitespace is Python's `str.isspace` set (= `\s` in a Python `str` regex), which is NOT
//   JavaScript's `\s` (JS adds U+FEFF and lacks U+001C–U+001F and U+0085), so the set is spelled
//   out below; at most MAX_WS characters in each of the three places;
// - anything else is text. A bare mid is not a citation.
// The daemon's filter is a streaming state machine; over a whole text it is equivalent to a
// left-to-right scan that tries the pattern at each opener (a dead span holds no other opener
// before the character that killed it). extension/test/fixtures/citation_grammar.json is checked
// by extension/test/citations.test.ts here and by tests/agent/test_citation_grammar_parity.py
// against `find_citations`, so the two grammars cannot drift (plan 08b A8).
// Pure TS, no DOM.

/** What the daemon puts in place of an invalid citation (Python `REMOVED_MARKER`). Not a citation. */
export const REMOVED_MARKER = "[citation removed]";

/** The most whitespace characters accepted in each place inside a citation (Python `MAX_WS`). */
export const MAX_WS = 8;

/** Python's `str.isspace` code points, as a regex character-class body. */
const WS_CLASS =
  "\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";

const WS_ONE = new RegExp(`^[${WS_CLASS}]$`, "u");

/** True when `ch` (one code point) is whitespace for the citation grammar (Python `isspace`). */
export function isCiteSpace(ch: string): boolean {
  return WS_ONE.test(ch);
}

const WS = `[${WS_CLASS}]{0,${MAX_WS}}`;
const BODY = `${WS}(?:mid:${WS})?((?:x-)?[0-9a-f]{64})${WS}`;
// Sticky: tried only at an opener's index, never searched forward.
const CITE_AT = new RegExp(`\\[${BODY}\\]|【${BODY}】`, "y");

export type CiteToken = { kind: "text"; text: string } | { kind: "cite"; mid: string; raw: string };

/** Splits `text` into text runs and citations, in order; adjacent text is one token. */
export function tokenizeCitations(text: string): CiteToken[] {
  const out: CiteToken[] = [];
  let textStart = 0;
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch !== "[" && ch !== "【") {
      i += 1;
      continue;
    }
    CITE_AT.lastIndex = i;
    const m = CITE_AT.exec(text);
    if (m === null) {
      i += 1;
      continue;
    }
    // Group 1 is the `[` alternative's mid, group 2 the `【` one's.
    const mid = m[1] ?? m[2];
    if (mid === undefined) {
      throw new Error("citation pattern matched without a mid");
    }
    if (i > textStart) {
      out.push({ kind: "text", text: text.slice(textStart, i) });
    }
    out.push({ kind: "cite", mid, raw: m[0] });
    i = CITE_AT.lastIndex;
    textStart = i;
  }
  if (textStart < text.length) {
    out.push({ kind: "text", text: text.slice(textStart) });
  }
  return out;
}

/** The mids of every citation in `text`, per occurrence, in order (Python `find_citations`). */
export function findCitations(text: string): string[] {
  const mids: string[] = [];
  for (const t of tokenizeCitations(text)) {
    if (t.kind === "cite") {
      mids.push(t.mid);
    }
  }
  return mids;
}
