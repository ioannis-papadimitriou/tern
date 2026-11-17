// The chat tab's model picker (tern-bridge 1.3): the reducer (state.ts: the `models` event,
// toggle_model_menu, select_model and their replies) and the header's dropdown (view.ts
// modelView). Synthetic ids only.

import { describe, expect, it } from "vitest";
import type { ChatModels, PageMessage, PageRequest } from "../src/protocol";
import {
  type Action,
  type ChatState,
  initialState,
  parsePageMessage,
  selectedModel,
  showsModelPicker,
  update,
} from "../src/ui/chat/state";
import { type Child, childrenOf, type VNode } from "../src/ui/chat/vdom";
import { headerView, modelView } from "../src/ui/chat/view";

const STRATA = "http://127.0.0.1:47831/v1#iq3_xxs";
const LMS = "http://127.0.0.1:1234/v1#openai/gpt-oss-20b";

const Q = { key: STRATA, model: "iq3_xxs", server: "Strata", label: "Qwen3.8-Flash-Next IQ3_XXS" };
const G = {
  key: LMS,
  model: "openai/gpt-oss-20b",
  server: "LM Studio",
  label: "openai/gpt-oss-20b",
};
const BOTH: ChatModels = { models: [Q, G], selected: STRATA };

interface Run {
  state: ChatState;
  out: PageRequest[];
}

function run(state: ChatState, ...actions: Action[]): Run {
  let s = state;
  const out: PageRequest[] = [];
  for (const a of actions) {
    const r = update(s, a);
    s = r.state;
    out.push(...r.out);
  }
  return { state: s, out };
}

const msg = (m: PageMessage): Action => ({ type: "message", msg: m });

/** A tab that knows ternd's model setting (iq3_xxs) and got the `models` event. */
function withModels(models: ChatModels = BOTH): Run {
  return run(
    initialState("s-1"),
    { type: "refresh" }, // status req 1, info req 2
    msg({ type: "info_result", req: 2, info: { remote_llm: false, chat_model: "iq3_xxs" } }),
    msg({ type: "models", models }),
  );
}

function all(c: Child): VNode[] {
  const out: VNode[] = [];
  const walk = (x: Child): void => {
    for (const n of childrenOf([x])) {
      if (typeof n === "string") continue;
      out.push(n);
      walk(n.props.children as Child[]);
    }
  };
  walk(c);
  return out;
}

function text(c: Child): string {
  return childrenOf([c])
    .map((n) => (typeof n === "string" ? n : text(n.props.children as Child[])))
    .join("");
}

function click(v: VNode | undefined): void {
  const h = v?.props.onClick;
  if (typeof h !== "function") throw new Error("not clickable");
  (h as (e: Event) => void)({} as Event);
}

function recorder(): { actions: Action[]; dispatch: (a: Action) => void } {
  const actions: Action[] = [];
  return { actions, dispatch: (a) => actions.push(a) };
}

describe("model picker: state", () => {
  it("a new tab knows no models and shows no picker; nothing extra is asked at refresh", () => {
    const r = run(initialState("s-1"), { type: "refresh" });
    expect(r.state.models).toEqual({ kind: "unknown" });
    expect(showsModelPicker(r.state)).toBe(false);
    expect(r.out.map((o) => o.type)).toEqual(["status", "info"]);
  });

  it("the models event fills the list; two models show the picker with the selected one", () => {
    const { state } = withModels();
    expect(state.models).toEqual({
      kind: "known",
      models: BOTH.models,
      selected: STRATA,
      selecting: null,
    });
    expect(showsModelPicker(state)).toBe(true);
    expect(selectedModel(state)?.server).toBe("Strata");
  });

  it("one model that is the selected one: no picker; one that is not: the picker shows", () => {
    const only = { models: [Q], selected: STRATA };
    expect(showsModelPicker(withModels(only).state)).toBe(false);
    const gone = { models: [G], selected: null };
    expect(showsModelPicker(withModels(gone).state)).toBe(true);
  });

  it("opening the menu asks models again; closing does not", () => {
    const opened = run(withModels().state, { type: "toggle_model_menu" });
    expect(opened.state.modelMenu).toBe(true);
    expect(opened.out).toEqual([{ type: "models", req: 3 }]);
    const closed = run(opened.state, { type: "toggle_model_menu" });
    expect(closed.state.modelMenu).toBe(false);
    expect(closed.out).toEqual([]);
  });

  it("picking a model sends select_model once, shows it as switching, then the answer", () => {
    const base = withModels();
    const picked = run(
      base.state,
      { type: "toggle_model_menu" },
      { type: "select_model", key: LMS },
    );
    expect(picked.out).toEqual([
      { type: "models", req: 3 },
      { type: "select_model", req: 4, key: LMS },
    ]);
    expect(picked.state.modelMenu).toBe(false);
    expect(selectedModel(picked.state)?.key).toBe(LMS); // shown while switching
    // A second pick while one is on its way is ignored.
    expect(run(picked.state, { type: "select_model", key: STRATA }).out).toEqual([]);
    const done = run(
      picked.state,
      msg({ type: "models_result", req: 4, models: { ...BOTH, selected: LMS } }),
    );
    expect(done.state.models).toMatchObject({ selected: LMS, selecting: null });
    // daemon.info's model follows the pick (the remote-model banner names it).
    expect(done.state.info).toEqual({
      kind: "known",
      remoteLlm: false,
      chatModel: "openai/gpt-oss-20b",
    });
  });

  it("picking the selected model or an unknown key sends nothing", () => {
    const base = withModels();
    expect(run(base.state, { type: "select_model", key: STRATA }).out).toEqual([]);
    expect(run(base.state, { type: "select_model", key: "nope" }).out).toEqual([]);
  });

  it("a failed pick keeps the previous model, says so and asks the list again", () => {
    const picked = run(withModels().state, { type: "select_model", key: LMS });
    const failed = run(
      picked.state,
      msg({ type: "error", req: 3, name: "invalid_params", message: "gone" }),
    );
    expect(failed.state.models).toMatchObject({ selected: STRATA, selecting: null });
    expect(failed.state.notices.at(-1)?.text).toContain("Could not switch the model");
    expect(failed.out).toEqual([{ type: "models", req: 4 }]);
  });

  it("method_not_available for models (ternd below 1.3) hides the picker for good", () => {
    const asked = run(withModels().state, { type: "toggle_model_menu" });
    const old = run(
      asked.state,
      msg({ type: "error", req: 3, name: "method_not_available", message: "1.3" }),
    );
    expect(old.state.models).toEqual({ kind: "unsupported" });
    expect(run(old.state, { type: "toggle_model_menu" }).out).toEqual([]);
  });

  it("session_end forgets the list (ternd may come back with other models)", () => {
    const ended = run(withModels().state, msg({ type: "session_end" }));
    expect(ended.state.models).toEqual({ kind: "unknown" });
    expect(showsModelPicker(ended.state)).toBe(false);
  });

  it("parsePageMessage accepts well-formed models messages and drops malformed ones", () => {
    expect(parsePageMessage({ type: "models", models: BOTH })).toEqual({
      type: "models",
      models: BOTH,
    });
    expect(parsePageMessage({ type: "models_result", req: 1, models: BOTH })).toEqual({
      type: "models_result",
      req: 1,
      models: BOTH,
    });
    for (const bad of [
      { type: "models" },
      { type: "models", models: { models: [{ key: "k" }], selected: null } },
      { type: "models", models: { models: [], selected: 1 } },
      { type: "models_result", models: BOTH },
    ]) {
      expect(parsePageMessage(bad)).toBeNull();
    }
  });
});

describe("model picker: view", () => {
  it("no picker without a choice; the header carries it when there is one", () => {
    const { dispatch } = recorder();
    expect(modelView(initialState("s-1"), dispatch)).toBeNull();
    const head = headerView(withModels().state, dispatch, 0, () => "s-2");
    expect(all(head).some((n) => n.props.class === "model-picker")).toBe(true);
  });

  it("the button names the selected model; clicking it toggles the menu", () => {
    const { actions, dispatch } = recorder();
    const v = modelView(withModels().state, dispatch);
    const button = all(v).find((n) => n.props.class === "model-button");
    expect(text(button as VNode)).toContain("Qwen3.8-Flash-Next IQ3_XXS");
    expect(button?.props["aria-expanded"]).toBe(false);
    expect(all(v).some((n) => n.props.role === "listbox")).toBe(false);
    click(button);
    expect(actions).toEqual([{ type: "toggle_model_menu" }]);
  });

  it("the open menu lists every model with its server; the chosen one is marked", () => {
    const { actions, dispatch } = recorder();
    const open = run(withModels().state, { type: "toggle_model_menu" }).state;
    const options = all(modelView(open, dispatch)).filter((n) => n.props.role === "option");
    expect(options.map(text)).toEqual([
      "Qwen3.8-Flash-Next IQ3_XXSStrata",
      "openai/gpt-oss-20bLM Studio",
    ]);
    expect(options.map((o) => o.props["aria-selected"])).toEqual([true, false]);
    click(options[1]);
    expect(actions).toEqual([{ type: "select_model", key: LMS }]);
  });

  it("no selected model reads Choose a model", () => {
    const { dispatch } = recorder();
    const v = modelView(withModels({ models: BOTH.models, selected: null }).state, dispatch);
    expect(text(v as VNode)).toContain("Choose a model");
  });
});
