// The small slice of the WebExtension runtime the bridge and the chat router need, injected so
// tests use a fake. Mail APIs (messenger.messages/folders/...) are NOT here: only src/tb/api.ts
// calls those.

export interface NativePort {
  postMessage(message: unknown): void;
  disconnect(): void;
  onMessage: { addListener(cb: (message: unknown) => void): void };
  onDisconnect: { addListener(cb: (port: NativePort) => void): void };
  error?: { message: string } | null;
}

/** A runtime port between extension pages (runtime.connect / runtime.onConnect). */
export interface RuntimePort {
  readonly name: string;
  /** The connecting context (runtime.onConnect side); `url` is the page that called connect. */
  readonly sender?: { readonly url?: string };
  postMessage(message: unknown): void;
  disconnect(): void;
  onMessage: { addListener(cb: (message: unknown) => void): void };
  onDisconnect: { addListener(cb: (port: RuntimePort) => void): void };
  error?: { message: string } | null;
}

/**
 * What the chat tab needs from TB (plan 08b step 3). Real TB unverified (J1): the spaces API
 * shape, windows.openDefaultBrowser from an event page, and onConnect waking the event page.
 */
export interface ChatPlatform {
  /** Register a runtime.onConnect listener. Call synchronously at module load (S1 F14). */
  onConnect(cb: (port: RuntimePort) => void): void;
  /**
   * The extension's space `name` exists afterwards: spaces.query first, spaces.create only if
   * absent, so a restarted event page does not fail on a space TB kept.
   */
  spaceEnsure(name: string, url: string, title: string): Promise<"created" | "present">;
  /** Open an http(s) URL in the system's default browser (the caller checks the scheme). */
  openDefaultBrowser(url: string): Promise<void>;
  /** The absolute URL of an extension file (runtime.getURL), e.g. the chat page's. */
  extensionUrl(path: string): string;
}

export interface Platform {
  version: string;
  connectNative(application: string): NativePort;
  storageGet(key: string): Promise<unknown>;
  storageSet(key: string, value: unknown): Promise<void>;
  /** Creates the alarm and returns the period TB reports for it afterwards (null: not set). */
  alarmCreate(name: string, periodInMinutes: number): Promise<number | null>;
  alarmClear(name: string): Promise<void>;
  onAlarm(cb: (name: string) => void): void;
  onStartup(cb: () => void): void;
  onInstalled(cb: () => void): void;
  randomUUID(): string;
}

// Structural types for the parts of `messenger` used below (TB 153 MV3).
export interface MessengerRuntime {
  runtime: {
    getManifest(): { version: string };
    getURL(path: string): string;
    connectNative(application: string): NativePort;
    onStartup: { addListener(cb: () => void): void };
    onInstalled: { addListener(cb: () => void): void };
    // Optional here (TB 153 always has them) so that a runtime without the chat APIs disables
    // the chat tab with an error log instead of breaking the mail bridge (chatPlatformFrom).
    onConnect?: { addListener(cb: (port: RuntimePort) => void): void };
  };
  spaces?: {
    query(queryInfo: { name: string; isSelfOwned: boolean }): Promise<{ name: string }[]>;
    create(name: string, defaultUrl: string, buttonProperties: { title: string }): Promise<unknown>;
  };
  windows?: {
    openDefaultBrowser(url: string): Promise<void>;
  };
  storage: {
    local: {
      get(key: string): Promise<Record<string, unknown>>;
      set(items: Record<string, unknown>): Promise<void>;
    };
  };
  alarms: {
    create(name: string, info: { periodInMinutes: number }): Promise<void> | void;
    clear(name: string): Promise<boolean>;
    get(name: string): Promise<{ periodInMinutes?: number } | undefined>;
    onAlarm: { addListener(cb: (alarm: { name: string }) => void): void };
  };
}

/** The chat APIs of `m`, or null (and nothing registered) when any of them is missing. */
export function chatPlatformFrom(m: MessengerRuntime): ChatPlatform | null {
  const onConnect = m.runtime.onConnect;
  const spaces = m.spaces;
  const windows = m.windows;
  if (onConnect === undefined || spaces === undefined || windows === undefined) return null;
  return {
    onConnect: (cb) => onConnect.addListener(cb),
    spaceEnsure: async (name, url, title) => {
      const found = await spaces.query({ name, isSelfOwned: true });
      if (found.some((s) => s.name === name)) return "present";
      await spaces.create(name, url, { title });
      return "created";
    },
    openDefaultBrowser: (url) => windows.openDefaultBrowser(url),
    extensionUrl: (path) => m.runtime.getURL(path),
  };
}

/** The bridge's Platform plus `chat` (null: the chat APIs are missing, the tab is disabled). */
export function platformFrom(m: MessengerRuntime): Platform & { chat: ChatPlatform | null } {
  return {
    chat: chatPlatformFrom(m),
    version: m.runtime.getManifest().version,
    connectNative: (app) => m.runtime.connectNative(app),
    storageGet: async (key) => (await m.storage.local.get(key))[key],
    storageSet: (key, value) => m.storage.local.set({ [key]: value }),
    alarmCreate: async (name, periodInMinutes) => {
      await m.alarms.create(name, { periodInMinutes });
      return (await m.alarms.get(name))?.periodInMinutes ?? null;
    },
    alarmClear: async (name) => {
      await m.alarms.clear(name);
    },
    onAlarm: (cb) => m.alarms.onAlarm.addListener((a) => cb(a.name)),
    onStartup: (cb) => m.runtime.onStartup.addListener(cb),
    onInstalled: (cb) => m.runtime.onInstalled.addListener(cb),
    randomUUID: () => crypto.randomUUID(),
  };
}
