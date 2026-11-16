// L17: no mail-API call outside src/tb/api.ts. The acceptance grep only sees `messenger.messages`
// literally; this also catches aliased access (`const m = messenger; m.messages...`) by allowing
// the `messenger`/`browser` identifiers only in src/tb/api.ts and src/background/main.ts, and in
// main.ts only to hand the object to platformFrom/tbApiFrom.

import { describe, expect, it } from "vitest";

declare global {
  interface ImportMeta {
    glob(
      pattern: string,
      opts: { query: "?raw"; import: "default"; eager: true },
    ): Record<string, string>;
  }
}

const sources = import.meta.glob("../src/**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
});

function code(text: string): string {
  // Drop comments and string contents so prose like "messenger.* mail APIs" does not count.
  return text
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*$/gm, "")
    .replace(/(["'`])(?:\\.|(?!\1)[^\\])*\1/g, '""');
}

const IDENT = /\b(messenger|browser)\b/; // no g flag: .test() must not keep state

describe("mail-API boundary (L17)", () => {
  it("finds the extension sources", () => {
    expect(Object.keys(sources)).toContain("../src/tb/api.ts");
    expect(Object.keys(sources)).toContain("../src/background/main.ts");
    expect(Object.keys(sources).length).toBeGreaterThanOrEqual(6);
  });

  it("no file but api.ts and main.ts names messenger or browser", () => {
    const offenders = Object.entries(sources)
      .filter(
        ([path]) => !path.endsWith("/src/tb/api.ts") && !path.endsWith("/src/background/main.ts"),
      )
      .filter(([, text]) => IDENT.test(code(text)))
      .map(([path]) => path);
    expect(offenders).toEqual([]);
  });

  it("main.ts only declares messenger and hands it to the two factories", () => {
    const main = code(sources["../src/background/main.ts"] ?? "");
    const lines = main.split("\n").filter((l) => /\b(messenger|browser)\b/.test(l));
    const allowed = [
      /^declare const messenger: [^;]+;$/,
      /^if \(typeof messenger !== ""\) \{$/,
      /^const platform = platformFrom\(messenger\);$/,
      /^const api = tbApiFrom\(messenger\);$/,
      /^events\.register\(tbEventsFrom\(messenger\)\);$/,
    ];
    for (const l of lines)
      expect(
        allowed.some((re) => re.test(l.trim())),
        l.trim(),
      ).toBe(true);
    expect(main).not.toMatch(/\b(messenger|browser)\s*\./);
  });

  it("the check catches an aliased call", () => {
    const aliased = "const m = messenger;\nawait m.messages.list(id);";
    expect(/\b(messenger|browser)\b/.test(code(aliased))).toBe(true);
    expect(/\b(messenger|browser)\b/.test(code('log("messenger.* is off limits");'))).toBe(false);
  });
});
