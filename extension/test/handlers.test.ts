import { describe, expect, it } from "vitest";
import type { RequestHandler } from "../src/background/bridge";
import { MAX_OPEN_CURSORS, MailHandlers } from "../src/background/handlers";
import { BridgeError, RAW_MAX } from "../src/protocol";
import { tbApiFrom } from "../src/tb/api";
import { resultErrors } from "./contract";
import { type FakeMessenger, folder, syntheticMessenger } from "./fake_messenger";

const INBOX = "account1:/INBOX";
const SENTINEL = "SENTINEL-FOLDER-handlers";

function setup(fm: FakeMessenger = syntheticMessenger()): {
  fm: FakeMessenger;
  h: MailHandlers;
  call: (method: string, params?: Record<string, unknown>) => Promise<unknown>;
  logs: string[];
} {
  const logs: string[] = [];
  let n = 0;
  const h = new MailHandlers(
    tbApiFrom(fm),
    (m) => logs.push(m),
    () => `c${++n}`,
  );
  const methods = h.methods();
  const call = async (method: string, params: Record<string, unknown> = {}): Promise<unknown> => {
    const fn: RequestHandler | undefined = methods.get(method);
    if (fn === undefined) throw new Error(`no handler ${method}`);
    const result = await fn(params);
    expect(resultErrors(method, result)).toEqual([]); // every result conforms to frozen 1.0
    return result;
  };
  return { fm, h, call, logs };
}

async function bridgeError(p: Promise<unknown>): Promise<BridgeError> {
  try {
    await p;
  } catch (err) {
    if (err instanceof BridgeError) return err;
    throw err;
  }
  throw new Error("expected a BridgeError");
}

describe("served methods", () => {
  it("serves exactly the R1 methods of 1.0 and reports mail.read", () => {
    const { h } = setup();
    expect([...h.methods().keys()].sort()).toEqual(
      [
        "mail.accounts.list",
        "mail.folders.list",
        "mail.messages.find",
        "mail.messages.full",
        "mail.messages.page",
        "mail.messages.raw",
        "mail.open",
        "mail.tags.list",
      ].sort(),
    );
    expect(h.capabilities).toEqual(["mail.read"]);
  });
});

describe("mail.accounts.list / mail.folders.list", () => {
  it("returns accounts with identities, the default first", async () => {
    const { call } = setup();
    expect(await call("mail.accounts.list")).toEqual([
      {
        account_id: "account1",
        name: "alex@example.com",
        type: "imap",
        identities: [
          { identity_id: "id1", email: "alex@example.com", name: "Alex Example", is_default: true },
          {
            identity_id: "id2",
            email: "alex.work@example.org",
            name: "Alex (work)",
            is_default: false,
          },
        ],
      },
      { account_id: "account2", name: "Local Folders", type: "local", identities: [] },
    ]);
  });

  it("flattens nested folders, skips root, virtual and tag folders, adds counts", async () => {
    const { call } = setup();
    expect(await call("mail.folders.list", { account_id: "account1" })).toEqual([
      {
        folder_id: INBOX,
        path: "INBOX",
        name: "INBOX",
        special_use: ["inbox"],
        total: 250,
        unread: 167,
      },
      {
        folder_id: "account1:/Archive",
        path: "Archive",
        name: "Archive",
        special_use: ["archives"],
        total: 2,
        unread: 1,
      },
      {
        folder_id: "account1:/Archive/2026",
        path: "Archive/2026",
        name: "2026",
        special_use: [],
        total: 3,
        unread: 2,
      },
    ]);
  });

  it("an unknown account is invalid_params", async () => {
    const { call } = setup();
    const err = await bridgeError(call("mail.folders.list", { account_id: "nope" }));
    expect(err.code).toBe("invalid_params");
  });

  const ARCHIVE = "account1:/Archive";
  const YEAR = "account1:/Archive/2026";

  function withSleeps(fm: FakeMessenger = syntheticMessenger()): {
    fm: FakeMessenger;
    call: (method: string, params?: Record<string, unknown>) => Promise<unknown>;
    logs: string[];
    slept: number[];
  } {
    const logs: string[] = [];
    const slept: number[] = [];
    const h = new MailHandlers(
      tbApiFrom(fm),
      (m) => logs.push(m),
      () => "c",
      async (ms) => {
        slept.push(ms);
      },
    );
    const methods = h.methods();
    const call = async (method: string, params: Record<string, unknown> = {}) => {
      const fn = methods.get(method);
      if (fn === undefined) throw new Error(`no handler ${method}`);
      const result = await fn(params);
      expect(resultErrors(method, result)).toEqual([]);
      return result;
    };
    return { fm, call, logs, slept };
  }

  const ids = (r: unknown): string[] => (r as { folder_id: string }[]).map((f) => f.folder_id);

  it("a folder whose summary is not built gets it built by a query, then its real counts", async () => {
    const { fm, call, logs, slept } = withSleeps();
    fm.unbuiltSummaries.add(ARCHIVE);
    const out = (await call("mail.folders.list", { account_id: "account1" })) as {
      folder_id: string;
      total: number;
      unread: number;
    }[];
    expect(ids(out)).toEqual([INBOX, ARCHIVE, YEAR]); // TB's order kept
    expect(out[1]).toMatchObject({ total: 2, unread: 1 }); // TB's counts, not made up
    expect(fm.queries.map((q) => q.folderId)).toEqual([ARCHIVE]);
    expect(slept).toEqual([250]);
    expect(fm.lists.size).toBe(0); // no list left open by the summary query
    expect(logs).toEqual([]);
  });

  it("a folder whose counts stay unavailable is left out, the rest served, logged count-only", async () => {
    const { fm, call, logs, slept } = withSleeps();
    fm.accountsData[0]?.rootFolder?.subFolders?.push(folder("account1", SENTINEL));
    const sentinelId = `account1:/${SENTINEL}`;
    fm.brokenFolderInfo.add(sentinelId);
    fm.brokenFolderInfo.add(YEAR);
    const out = await call("mail.folders.list", { account_id: "account1" });
    expect(ids(out)).toEqual([INBOX, ARCHIVE]);
    expect(slept).toEqual([250, 1000, 3000]);
    expect(fm.getFolderInfoCalls.filter((id) => id === YEAR)).toHaveLength(4);
    expect(logs).toEqual([
      "mail.folders.list: 2 folder(s) left out, counts unavailable (folder_info_failed); " +
        "0 left out, no longer found (folder_not_found)",
    ]);
    expect(logs.join("\n")).not.toContain(SENTINEL);
  });

  it("a summary query that fails is counted; the retries still decide", async () => {
    const { fm, call, logs } = withSleeps();
    fm.unbuiltSummaries.add(ARCHIVE);
    fm.failNext.set("messages.query", new Error(`busy ${SENTINEL}`));
    const out = await call("mail.folders.list", { account_id: "account1" });
    // ARCHIVE's summary query failed, so its summary is never built: left out after the retries
    expect(ids(out)).toEqual([INBOX, YEAR]);
    expect(fm.getFolderInfoCalls.filter((id) => id === ARCHIVE)).toHaveLength(4);
    expect(logs).toEqual([
      "mail.folders.list: summary build query failed for 1 folder(s)",
      "mail.folders.list: 1 folder(s) left out, counts unavailable (folder_info_failed); " +
        "0 left out, no longer found (folder_not_found)",
    ]);
    expect(logs.join("\n")).not.toContain(SENTINEL);
  });

  it("a folder TB no longer finds is left out at once, without retries", async () => {
    const { fm, call, logs, slept } = withSleeps();
    fm.failNext.set("folders.getFolderInfo", new Error(`Folder not found: ${INBOX}`));
    const out = await call("mail.folders.list", { account_id: "account1" });
    expect(ids(out)).toEqual([ARCHIVE, YEAR]);
    expect(slept).toEqual([]);
    expect(fm.queries).toEqual([]);
    expect(logs).toEqual([
      "mail.folders.list: 0 folder(s) left out, counts unavailable (folder_info_failed); " +
        "1 left out, no longer found (folder_not_found)",
    ]);
  });
});

describe("mail.messages.page", () => {
  it("pages a folder through opaque cursors; the last page has none", async () => {
    const { call, fm } = setup();
    const p1 = (await call("mail.messages.page", { folder_id: INBOX })) as {
      items: { tb_id: number }[];
      next_cursor?: string;
    };
    expect(p1.items).toHaveLength(100);
    expect(p1.next_cursor).toBe("c1"); // our token, never TB's list id
    expect(fm.lists.has("c1")).toBe(false);
    const p2 = (await call("mail.messages.page", {
      folder_id: INBOX,
      cursor: p1.next_cursor,
    })) as typeof p1;
    const p3 = (await call("mail.messages.page", {
      folder_id: INBOX,
      cursor: p2.next_cursor,
    })) as typeof p1;
    expect(p2.items).toHaveLength(100);
    expect(p3.items).toHaveLength(50);
    expect(p3.next_cursor).toBeUndefined();
    const ids = [...p1.items, ...p2.items, ...p3.items].map((i) => i.tb_id);
    expect(new Set(ids).size).toBe(250);
  });

  it("maps a header to a 1.0 page item", async () => {
    const { call } = setup();
    const page = (await call("mail.messages.page", {
      folder_id: "account1:/Archive",
    })) as { items: unknown[] };
    expect(page.items[0]).toEqual({
      tb_id: 254,
      header_message_id: "m254@example.com",
      date: new Date(Date.UTC(2026, 9, 1) + 254 * 3_600_000).toISOString(),
      size: expect.any(Number),
      flags: { read: false, flagged: false, junk: false },
      tags: [],
      headers_only: true,
    });
  });

  it("passes the date window to TB as inclusive bounds", async () => {
    const { call, fm } = setup();
    const from = new Date(Date.UTC(2026, 9, 1) + 10 * 3_600_000).toISOString();
    const to = new Date(Date.UTC(2026, 9, 1) + 20 * 3_600_000).toISOString();
    const page = (await call("mail.messages.page", {
      folder_id: INBOX,
      from_date: from,
      to_date: to,
    })) as { items: { tb_id: number }[] };
    expect(page.items.map((i) => i.tb_id)).toEqual([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]);
    expect(fm.queries.at(-1)?.fromDate?.toISOString()).toBe(from);
  });

  it("a cursor is single-use", async () => {
    const { call } = setup();
    const p1 = (await call("mail.messages.page", { folder_id: INBOX })) as { next_cursor: string };
    await call("mail.messages.page", { folder_id: INBOX, cursor: p1.next_cursor });
    const err = await bridgeError(
      call("mail.messages.page", { folder_id: INBOX, cursor: p1.next_cursor }),
    );
    expect(err.code).toBe("stale_id");
  });

  it("the session end makes every cursor stale and aborts TB's lists", async () => {
    const { call, h, fm } = setup();
    const p1 = (await call("mail.messages.page", { folder_id: INBOX })) as { next_cursor: string };
    expect(fm.lists.size).toBe(1);
    await h.endSession();
    expect(fm.lists.size).toBe(0);
    expect(fm.aborted).toHaveLength(1);
    const err = await bridgeError(
      call("mail.messages.page", { folder_id: INBOX, cursor: p1.next_cursor }),
    );
    expect(err.code).toBe("stale_id");
  });

  it("a list TB dropped on its own is stale_id", async () => {
    const { call, fm } = setup();
    const p1 = (await call("mail.messages.page", { folder_id: INBOX })) as { next_cursor: string };
    fm.lists.clear();
    const err = await bridgeError(
      call("mail.messages.page", { folder_id: INBOX, cursor: p1.next_cursor }),
    );
    expect(err.code).toBe("stale_id");
  });

  it(`keeps at most ${MAX_OPEN_CURSORS} lists open, aborting the oldest`, async () => {
    const { call, h, fm } = setup();
    const first = (await call("mail.messages.page", { folder_id: INBOX })) as {
      next_cursor: string;
    };
    for (let i = 0; i < MAX_OPEN_CURSORS; i++) {
      await call("mail.messages.page", { folder_id: INBOX });
    }
    expect(h.openCursors).toBe(MAX_OPEN_CURSORS);
    expect(fm.lists.size).toBe(MAX_OPEN_CURSORS);
    expect(fm.aborted).toEqual(["list-1"]);
    const err = await bridgeError(
      call("mail.messages.page", { folder_id: INBOX, cursor: first.next_cursor }),
    );
    expect(err.code).toBe("stale_id");
  });

  it("a session end during a cursor eviction still aborts the new page's list", async () => {
    const { call, h, fm } = setup();
    for (let i = 0; i < MAX_OPEN_CURSORS; i++) {
      await call("mail.messages.page", { folder_id: INBOX });
    }
    const abortList = fm.messages.abortList;
    let release: () => void = () => {
      throw new Error("eviction not reached");
    };
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let reached: () => void = () => undefined;
    const atEviction = new Promise<void>((resolve) => {
      reached = resolve;
    });
    let first = true;
    fm.messages.abortList = async (id) => {
      if (first) {
        first = false;
        reached();
        await gate;
      }
      return abortList(id);
    };
    const pending = call("mail.messages.page", { folder_id: INBOX });
    await atEviction;
    await h.endSession();
    release();
    await pending;
    expect(h.openCursors).toBe(0);
    expect(fm.lists.size).toBe(0);
  });

  it("an empty page can still continue (TB's autoPaginationTimeout)", async () => {
    const { call, fm } = setup();
    fm.messages.query = async () => {
      fm.lists.set("list-slow", []);
      return { id: "list-slow", messages: [] };
    };
    const page = (await call("mail.messages.page", { folder_id: INBOX })) as {
      items: unknown[];
      next_cursor?: string;
    };
    expect(page.items).toEqual([]);
    expect(page.next_cursor).toBe("c1");
  });

  it("an unmappable header fails the page and aborts its list (no leaked cursor)", async () => {
    const { call, fm, h } = setup();
    const m = fm.msgs.find((x) => x.id === 1);
    if (m === undefined) throw new Error("fixture");
    m.date = new Date(Number.NaN);
    const err = await bridgeError(call("mail.messages.page", { folder_id: INBOX }));
    expect(err.code).toBe("tb_error");
    expect(err.tbCode).toBe("page_failed");
    expect(h.openCursors).toBe(0);
    expect(fm.lists.size).toBe(0);
  });

  it("an unknown folder is tb_error folder_not_found, without the folder name", async () => {
    const { call, logs } = setup();
    const err = await bridgeError(
      call("mail.messages.page", { folder_id: `account1:/${SENTINEL}` }),
    );
    expect(err.code).toBe("tb_error");
    expect(err.tbCode).toBe("folder_not_found");
    expect(JSON.stringify(err.toRpc())).not.toContain(SENTINEL);
    expect(logs.join("\n")).not.toContain(SENTINEL);
  });

  it("bad params are invalid_params", async () => {
    const { call } = setup();
    for (const params of [{}, { folder_id: INBOX, from_date: "yesterday" }, { folder_id: 3 }]) {
      expect((await bridgeError(call("mail.messages.page", params))).code).toBe("invalid_params");
    }
  });

  it("a date without a UTC offset is invalid_params and never reaches TB", async () => {
    const { call, fm } = setup();
    for (const params of [
      { folder_id: INBOX, from_date: "2026-09-21T09:00:00" },
      { folder_id: INBOX, to_date: "2026-09-26T09:00:00.500" },
      { folder_id: INBOX, from_date: "2026-09-21T09:00:00z" }, // ternd sends uppercase Z only
      { folder_id: INBOX, from_date: "2026-09-21" },
    ]) {
      const err = await bridgeError(call("mail.messages.page", params));
      expect(err.code).toBe("invalid_params");
      expect(err.message).toMatch(/expected an RFC 3339 date-time with a UTC offset$/);
    }
    expect(fm.queries).toHaveLength(0);
  });

  it("dates with Z or a numeric offset are taken as instants", async () => {
    const { call, fm } = setup();
    await call("mail.messages.page", {
      folder_id: INBOX,
      from_date: "2026-10-01T10:00:00Z",
      to_date: "2026-10-01T23:00:00.000+03:00",
    });
    expect(fm.queries.at(-1)?.fromDate?.toISOString()).toBe("2026-10-01T10:00:00.000Z");
    expect(fm.queries.at(-1)?.toDate?.toISOString()).toBe("2026-10-01T20:00:00.000Z");
  });

  it("an impossible calendar day is invalid_params, not rolled over", async () => {
    const { call, fm } = setup();
    for (const from_date of [
      "2026-02-30T00:00:00Z",
      "2026-09-21T24:00:00Z", // V8 would read this as Sep 22 00:00
      "2026-09-21T09:60:00Z",
      "2026-09-21T09:00:60Z",
    ]) {
      const err = await bridgeError(call("mail.messages.page", { folder_id: INBOX, from_date }));
      expect(err.code).toBe("invalid_params");
    }
    expect(fm.queries).toHaveLength(0);
  });

  it("a cursor used with another folder is invalid_params and stays valid", async () => {
    const { call, fm, h } = setup();
    const p1 = (await call("mail.messages.page", { folder_id: INBOX })) as { next_cursor: string };
    const continueList = fm.messages.continueList;
    let continued = 0;
    fm.messages.continueList = async (id) => {
      continued += 1;
      return continueList(id);
    };
    const err = await bridgeError(
      call("mail.messages.page", { folder_id: "account1:/Archive", cursor: p1.next_cursor }),
    );
    expect(err.code).toBe("invalid_params");
    expect(err.message).toMatch(/cursor belongs to another folder$/);
    expect(continued).toBe(0);
    expect(h.openCursors).toBe(1);
    expect(fm.aborted).toEqual([]);
    const p2 = (await call("mail.messages.page", {
      folder_id: INBOX,
      cursor: p1.next_cursor,
    })) as { items: unknown[] };
    expect(p2.items).toHaveLength(100);
    expect(continued).toBe(1);
  });

  for (const via of ["query", "continueList"] as const) {
    it(`a page whose TB ${via} spans a session end aborts its list and is stale_id`, async () => {
      const { call, fm, h } = setup();
      const p1 =
        via === "continueList"
          ? ((await call("mail.messages.page", { folder_id: INBOX })) as { next_cursor: string })
          : undefined;
      let release: () => void = () => {
        throw new Error("TB call not reached");
      };
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      let reached: () => void = () => undefined;
      const atTb = new Promise<void>((resolve) => {
        reached = resolve;
      });
      const returned: (string | null)[] = [];
      if (via === "query") {
        const query = fm.messages.query;
        fm.messages.query = async (q) => {
          const list = await query(q);
          returned.push(list.id);
          reached();
          await gate;
          return list;
        };
      } else {
        const continueList = fm.messages.continueList;
        fm.messages.continueList = async (id) => {
          const list = await continueList(id);
          returned.push(list.id);
          reached();
          await gate;
          return list;
        };
      }
      const pending = bridgeError(
        call("mail.messages.page", {
          folder_id: INBOX,
          ...(p1 === undefined ? {} : { cursor: p1.next_cursor }),
        }),
      );
      await atTb;
      await h.endSession();
      release();
      const err = await pending;
      expect(err.code).toBe("stale_id");
      const listId = returned[0];
      expect(typeof listId).toBe("string");
      expect(fm.aborted).toContain(listId);
      expect(h.openCursors).toBe(0);
      expect(fm.lists.size).toBe(0);
    });
  }
});

describe("mail.messages.raw / full", () => {
  it("returns the raw bytes as base64", async () => {
    const { call, fm } = setup();
    const raw = (await call("mail.messages.raw", { tb_id: 7 })) as { raw_b64: string };
    const bin = atob(raw.raw_b64);
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    expect(bytes).toEqual(fm.msgs.find((m) => m.id === 7)?.raw);
  });

  it("over RAW_MAX by TB's size: too_large without fetching", async () => {
    const { call, fm } = setup();
    const m = fm.msgs.find((x) => x.id === 7);
    if (m === undefined) throw new Error("fixture");
    m.size = RAW_MAX + 1;
    const err = await bridgeError(call("mail.messages.raw", { tb_id: 7 }));
    expect(err.code).toBe("too_large");
    expect(fm.getRawCalls).toBe(0);
  });

  it("over RAW_MAX by the bytes TB returns: too_large", async () => {
    const { call, fm } = setup();
    const m = fm.msgs.find((x) => x.id === 7);
    if (m === undefined) throw new Error("fixture");
    m.raw = new Uint8Array(RAW_MAX + 1);
    expect((await bridgeError(call("mail.messages.raw", { tb_id: 7 }))).code).toBe("too_large");
  });

  it("an unknown tb_id is stale_id", async () => {
    const { call } = setup();
    for (const method of ["mail.messages.raw", "mail.messages.full", "mail.open"]) {
      expect((await bridgeError(call(method, { tb_id: 99_999 }))).code).toBe("stale_id");
    }
  });

  it("another TB failure is tb_error with a fixed code; TB's text is not forwarded or logged", async () => {
    const { call, fm, logs } = setup();
    fm.failNext.set("messages.getRaw", new Error(`read failed in ${SENTINEL}`));
    const err = await bridgeError(call("mail.messages.raw", { tb_id: 7 }));
    expect(err.code).toBe("tb_error");
    expect(err.tbCode).toBe("raw_failed");
    expect(JSON.stringify(err.toRpc())).not.toContain(SENTINEL);
    expect(logs).toEqual(["mail.messages.raw: Thunderbird call failed (raw_failed)"]);
  });

  it("full: headers, inline text parts and attachment metadata", async () => {
    const { call, fm } = setup();
    const m = fm.msgs.find((x) => x.id === 9);
    if (m === undefined) throw new Error("fixture");
    m.textParts = [
      { contentType: "text/plain", content: "hello" },
      { contentType: "text/html", content: "<p>hello</p>" },
    ];
    m.attachments = [{ name: "invoice.pdf", contentType: "application/pdf", size: 1234 }];
    expect(await call("mail.messages.full", { tb_id: 9 })).toEqual({
      headers: { "message-id": ["<m9@example.com>"], subject: ["synthetic 9"] },
      text_parts: [
        { mime: "text/plain", text: "hello" },
        { mime: "text/html", text: "<p>hello</p>" },
      ],
      attachments: [{ name: "invoice.pdf", mime: "application/pdf", size: 1234 }],
    });
  });

  it("bad tb_id params are invalid_params", async () => {
    const { call } = setup();
    for (const tb_id of [-1, 1.5, "7", undefined]) {
      expect((await bridgeError(call("mail.messages.raw", { tb_id }))).code).toBe("invalid_params");
    }
  });
});

describe("mail.messages.find / mail.open / mail.tags.list", () => {
  it("finds every location of a Message-ID, across folders and pages", async () => {
    const fm = syntheticMessenger();
    const copy = fm.msgs.find((m) => m.id === 3);
    if (copy === undefined) throw new Error("fixture");
    fm.msgs.push({ ...copy, id: 9001, folderId: "account1:/Archive/2026", tags: ["invoice"] });
    fm.pageSize = 1; // continueList pages of one: find must follow them
    const { call } = setup(fm);
    expect(await call("mail.messages.find", { header_message_id: "m3@example.com" })).toEqual([
      {
        tb_id: 3,
        folder_id: INBOX,
        flags: { read: true, flagged: false, junk: false },
        tags: [],
      },
      {
        tb_id: 9001,
        folder_id: "account1:/Archive/2026",
        flags: { read: true, flagged: false, junk: false },
        tags: ["invoice"],
      },
    ]);
  });

  it("find of an unknown Message-ID is empty", async () => {
    const { call } = setup();
    expect(await call("mail.messages.find", { header_message_id: "none@example.com" })).toEqual([]);
  });

  it("open shows the message in a tab", async () => {
    const { call, fm } = setup();
    expect(await call("mail.open", { tb_id: 4 })).toEqual({});
    expect(fm.opened).toEqual([{ messageId: 4, location: "tab" }]);
  });

  it("tags.list", async () => {
    const { call } = setup();
    expect(await call("mail.tags.list")).toEqual([
      { key: "$label1", tag: "Important", color: "#FF0000" },
      { key: "invoice", tag: "invoice", color: "#3366CC" },
    ]);
  });
});

describe("the contract check itself", () => {
  const item = {
    tb_id: 1,
    header_message_id: "m1@example.com",
    date: "2026-10-01T01:00:00.000Z",
    size: 10,
    flags: { read: false, flagged: false, junk: false },
    tags: [],
    headers_only: false,
  };
  it("accepts a valid page and rejects broken ones", () => {
    expect(resultErrors("mail.messages.page", { items: [item] })).toEqual([]);
    expect(resultErrors("mail.messages.page", { items: [{ ...item, tb_id: -1 }] })).not.toEqual([]);
    expect(resultErrors("mail.messages.page", { items: [{ ...item, offline: true }] })).not.toEqual(
      [],
    );
    expect(resultErrors("mail.messages.page", { items: [{ ...item, date: "Oct 1" }] })).not.toEqual(
      [],
    );
    const { headers_only: _h, ...missing } = item;
    expect(resultErrors("mail.messages.page", { items: [missing] })).not.toEqual([]);
    expect(resultErrors("mail.messages.page", { items: [item], next_cursor: 5 })).not.toEqual([]);
  });
});
