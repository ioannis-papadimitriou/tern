// Background event page (MV3): wires the bridge to ternd (plan 02), the mail handlers (plan 03)
// and the chat router with its space (plan 08b). Rule (CLAUDE.md): only src/tb/api.ts calls
// messenger.* mail APIs; this file only hands the `messenger` object to platformFrom (runtime,
// storage, alarms, and the chat APIs: onConnect, spaces, windows) and tbApiFrom.

import { type MessengerMail, tbApiFrom, tbEventsFrom } from "../tb/api";
import { Bridge, registerListeners } from "./bridge";
import { startChat } from "./chat";
import { EventForwarder } from "./events";
import { MailHandlers } from "./handlers";
import { type MessengerRuntime, platformFrom } from "./platform";

export const EXTENSION_ID = "tern@tern.invalid";

export function startupMessage(version: string): string {
  return `tern ${version} background started`;
}

declare const messenger: (MessengerRuntime & MessengerMail) | undefined;

// Tests import this module without a `messenger` global; in TB it always exists.
if (typeof messenger !== "undefined") {
  const platform = platformFrom(messenger);
  const bridge = new Bridge(platform);
  const api = tbApiFrom(messenger);
  const mail = new MailHandlers(api, (m) => console.info(`tern mail: ${m}`));
  bridge.register(mail); // hello reports mail.read from here on
  // Synchronously, at top level (S1 F14): runtime.onConnect wakes the page for a chat tab.
  const chat = startChat(platform.chat, bridge);
  bridge.onSessionEnd = () => {
    void mail.endSession();
    chat?.sessionEnded();
  };
  const events = new EventForwarder(
    api,
    (method, params) => bridge.notify(method, params),
    (m) => console.info(`tern events: ${m}`),
  );
  bridge.onReady = () => events.flush();
  // Synchronously, at top level (S1 F14): TB wakes the page for these listeners.
  events.register(tbEventsFrom(messenger));
  registerListeners(platform, bridge);
  console.info(startupMessage(platform.version));
  void bridge.connect(); // the page started for some reason (load, install, event): connect now
}
