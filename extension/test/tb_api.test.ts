import { describe, expect, it } from "vitest";
import { PAGE_SIZE, tbApiFrom, tbErrorKind } from "../src/tb/api";
import { syntheticMessenger } from "./fake_messenger";

describe("tbApiFrom (the only messenger.* mail caller)", () => {
  it("queries with an explicit page size", async () => {
    const fm = syntheticMessenger();
    const api = tbApiFrom(fm);
    const list = await api.query({ folderId: "account1:/INBOX" });
    expect(fm.queries).toEqual([{ folderId: "account1:/INBOX", messagesPerPage: PAGE_SIZE }]);
    expect(list.messages).toHaveLength(PAGE_SIZE);
    expect(list.id).not.toBeNull();
  });

  it("lists accounts without folders and gets one account with its folder tree", async () => {
    const fm = syntheticMessenger();
    const api = tbApiFrom(fm);
    const accounts = await api.listAccounts();
    expect(accounts.every((a) => a.rootFolder === undefined)).toBe(true);
    const one = await api.getAccountWithFolders("account1");
    expect(one?.rootFolder?.subFolders?.map((f) => f.path)).toEqual([
      "INBOX",
      "Archive",
      "Unread",
      "Tags/Important",
    ]);
    expect(await api.getAccountWithFolders("nope")).toBeNull();
  });

  it("reads getRaw's File into bytes", async () => {
    const fm = syntheticMessenger();
    const bytes = await tbApiFrom(fm).getRaw(1);
    expect(new TextDecoder().decode(bytes)).toContain("Message-ID: <m1@example.com>");
  });

  it("opens a message in a tab", async () => {
    const fm = syntheticMessenger();
    await tbApiFrom(fm).openMessage(2);
    expect(fm.opened).toEqual([{ messageId: 2, location: "tab" }]);
  });

  it("lists tags", async () => {
    const fm = syntheticMessenger();
    expect((await tbApiFrom(fm).listTags()).map((t) => t.key)).toEqual(["$label1", "invoice"]);
  });
});

describe("tbErrorKind (TB 153 error texts)", () => {
  it("classifies TB's exact messages", async () => {
    const fm = syntheticMessenger();
    const api = tbApiFrom(fm);
    const kind = async (p: Promise<unknown>): Promise<string> => {
      try {
        await p;
      } catch (err) {
        return tbErrorKind(err);
      }
      throw new Error("expected a rejection");
    };
    expect(await kind(api.getMessage(99_999))).toBe("message_not_found");
    expect(await kind(api.continueList("list-404"))).toBe("list_not_found");
    expect(await kind(api.getFolderInfo("account1:/SENTINEL-FOLDER"))).toBe("folder_not_found");
    expect(tbErrorKind(new Error("moveFolder failed, cannot delete source folder X"))).toBe(
      "other",
    );
    expect(tbErrorKind("not an Error")).toBe("other");
  });
});
