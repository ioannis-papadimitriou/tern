import { describe, expect, it } from "vitest";
import manifestJson from "../manifest.json" with { type: "json" };
import { EXTENSION_ID, startupMessage } from "../src/background/main";

interface Manifest {
  manifest_version: number;
  permissions: string[];
  browser_specific_settings: { gecko: { id: string; strict_min_version: string } };
}

const manifest: Manifest = manifestJson;

describe("manifest", () => {
  it("is MV3 for TB 153+", () => {
    expect(manifest.manifest_version).toBe(3);
    expect(manifest.browser_specific_settings.gecko.strict_min_version).toBe("153.0");
  });

  it("has the fixed gecko id the native host allow-list depends on", () => {
    expect(manifest.browser_specific_settings.gecko.id).toBe(EXTENSION_ID);
  });

  it("requests exactly the read-only permissions (no compose/send, decision #8)", () => {
    // storage (profile_id) and alarms (reconnect backstop) added by plan 02 step 7;
    // messagesTagsList (read-only, needed by messages.tags.list) by plan 03 step 2.
    expect([...manifest.permissions].sort()).toEqual(
      [
        "accountsRead",
        "alarms",
        "messagesRead",
        "messagesTagsList",
        "nativeMessaging",
        "storage",
      ].sort(),
    );
  });
});

describe("background", () => {
  it("formats the startup message", () => {
    expect(startupMessage("0.1.0")).toBe("tern 0.1.0 background started");
  });
});
