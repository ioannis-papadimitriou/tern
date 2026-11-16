// Plan 08b step 5, P2 fallback: a tiny `h()` + `render()` with preact's call shapes, used because
// the npm registry was unreachable from the session that built the tab (preact is approved, #18;
// switching means importing `h`/`render` from "preact" instead of this file). Views build plain
// vnode objects with `h()`, so tests inspect them without a DOM; `render()` turns them into DOM
// nodes with createElement/createTextNode and a positional diff (existing nodes are kept, so a
// streaming answer does not lose the reader's text selection or scroll position).
//
// Never an HTML string: text becomes Text nodes. Defence in depth for the tab's CSP (T3): only the
// element types and attributes below can be created; anything else (an `a`, an `img`, `href`,
// `src`, `style`, a string `on*` handler) throws instead of rendering.

/** Element types the chat views may create. No `a`, `img`, `iframe`, `script`, `style`, `form`. */
const TAGS = new Set([
  "div",
  "span",
  "p",
  "strong",
  "em",
  "code",
  "pre",
  "ul",
  "ol",
  "li",
  "br",
  "button",
  "section",
  "header",
  "small",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
]);

/** Attribute names the views may set (values: string, number or boolean). */
const ATTR = /^(class|title|type|disabled|start|role|tabindex|aria-[a-z]+|data-[a-z-]+)$/;

/** Event props and the DOM event each one listens to. */
const EVENTS: Readonly<Record<string, string>> = { onClick: "click" };

export type AttrValue = string | number | boolean | undefined;
export type Handler = (ev: Event) => void;
export type Props = Record<string, AttrValue | Handler>;
export type Child = VNode | string | number | boolean | null | undefined | Child[];

export interface VNode {
  readonly type: string;
  readonly props: {
    readonly [key: string]: AttrValue | Handler | readonly Child[];
    readonly children: readonly Child[];
  };
}

/** preact's `h(type, props, ...children)`; the children always land in `props.children`. */
export function h(type: string, props: Props | null, ...children: Child[]): VNode {
  if (!TAGS.has(type)) throw new Error(`vdom: element type not allowed: ${type}`);
  for (const key of Object.keys(props ?? {})) checkProp(key);
  return { type, props: { ...(props ?? {}), children } };
}

function checkProp(key: string): void {
  if (Object.hasOwn(EVENTS, key) || ATTR.test(key)) return;
  throw new Error(`vdom: prop not allowed: ${key}`);
}

/** The flattened children of a vnode: vnodes and strings; null/undefined/booleans dropped. */
export function childrenOf(children: readonly Child[]): (VNode | string)[] {
  const out: (VNode | string)[] = [];
  const add = (c: Child): void => {
    if (c === null || c === undefined || typeof c === "boolean") return;
    if (Array.isArray(c)) {
      for (const x of c) add(x);
    } else if (typeof c === "number") {
      out.push(String(c));
    } else {
      out.push(c);
    }
  };
  for (const c of children) add(c);
  return out;
}

// Previous props and the live handler of each event, per rendered element.
const lastProps = new WeakMap<Element, Readonly<Props>>();
// A null handler keeps its event's one listener registered, idle.
const handlers = new WeakMap<Element, Map<string, Handler | null>>();

/** Render `vnode` as the only child of `parent` (preact's `render(vnode, parent)` shape). */
export function render(vnode: Child, parent: Element): void {
  patchChildren(parent, childrenOf([vnode]));
}

function patchChildren(parent: Element, kids: readonly (VNode | string)[]): void {
  const existing = Array.from(parent.childNodes);
  kids.forEach((v, i) => {
    const node = existing[i];
    if (node !== undefined && sameKind(node, v)) {
      patchNode(node, v);
    } else {
      const created = create(parent.ownerDocument, v);
      if (node === undefined) parent.appendChild(created);
      else parent.replaceChild(created, node);
    }
  });
  for (const extra of existing.slice(kids.length)) parent.removeChild(extra);
}

const TEXT_NODE = 3;
const ELEMENT_NODE = 1;

function sameKind(node: ChildNode, v: VNode | string): boolean {
  if (typeof v === "string") return node.nodeType === TEXT_NODE;
  return node.nodeType === ELEMENT_NODE && (node as Element).localName === v.type;
}

function create(doc: Document, v: VNode | string): ChildNode {
  if (typeof v === "string") return doc.createTextNode(v);
  if (!TAGS.has(v.type)) throw new Error(`vdom: element type not allowed: ${v.type}`);
  const el = doc.createElement(v.type);
  patchNode(el, v);
  return el;
}

function patchNode(node: ChildNode, v: VNode | string): void {
  if (typeof v === "string") {
    const text = node as Text;
    if (text.data !== v) text.data = v;
    return;
  }
  const el = node as Element;
  const prev = lastProps.get(el) ?? {};
  const next: Props = {};
  for (const [key, value] of Object.entries(v.props)) {
    if (key !== "children") next[key] = value as AttrValue | Handler;
  }
  for (const key of Object.keys(prev)) {
    if (!(key in next)) setProp(el, key, undefined);
  }
  for (const [key, value] of Object.entries(next)) {
    if (prev[key] !== value) setProp(el, key, value);
  }
  lastProps.set(el, next);
  patchChildren(el, childrenOf(v.props.children));
}

function setProp(el: Element, key: string, value: AttrValue | Handler): void {
  checkProp(key);
  const event = Object.hasOwn(EVENTS, key) ? EVENTS[key] : undefined;
  if (event !== undefined) {
    if (value !== undefined && typeof value !== "function") {
      throw new Error(`vdom: ${key} must be a function`);
    }
    setHandler(el, event, value);
    return;
  }
  if (typeof value === "function") throw new Error(`vdom: ${key} must not be a function`);
  // preact's rule: `aria-*`/`data-*` booleans are written as "true"/"false" (aria-busy="" would
  // read as false); other booleans are present ("") or absent.
  const dashed = key.startsWith("aria-") || key.startsWith("data-");
  if (dashed && typeof value === "boolean") el.setAttribute(key, String(value));
  else if (value === undefined || value === false) el.removeAttribute(key);
  else el.setAttribute(key, value === true ? "" : String(value));
}

function setHandler(el: Element, event: string, fn: Handler | undefined): void {
  let map = handlers.get(el);
  if (map === undefined) {
    map = new Map();
    handlers.set(el, map);
  }
  if (!map.has(event)) {
    const live = map;
    el.addEventListener(event, (ev) => live.get(event)?.(ev));
  }
  map.set(event, fn ?? null);
}
