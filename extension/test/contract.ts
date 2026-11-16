// Validate values against the frozen tern-bridge/1.N contract files (the pydantic exports):
// 1.0 by default, 1.1 or 1.2 on request (plan 07 part B, #39; supersets, older shapes identical).
// Supports exactly the JSON-schema keywords that file uses; an unknown keyword is an error, so a
// future export cannot silently pass unchecked.

import contractJson from "../../contracts/tern-bridge-1.0.schema.json";
import contract11Json from "../../contracts/tern-bridge-1.1.schema.json";
import contract12Json from "../../contracts/tern-bridge-1.2.schema.json";

type Schema = Record<string, unknown>;

interface Contract {
  $defs: Record<string, Schema>;
  methods: Record<string, { params: Schema; result?: Schema; kind: string; direction: string }>;
}

export type Version = "1.0" | "1.1" | "1.2";

const CONTRACTS: Record<Version, Contract> = {
  "1.0": contractJson as unknown as Contract,
  "1.1": contract11Json as unknown as Contract,
  "1.2": contract12Json as unknown as Contract,
};

/** The contract whose $defs `check` resolves (set for the duration of one check). */
let contract: Contract = CONTRACTS["1.0"];

const KNOWN = new Set([
  "$ref",
  "anyOf",
  "const",
  "enum",
  "type",
  "properties",
  "required",
  "additionalProperties",
  "items",
  "minItems",
  "minLength",
  "maxLength",
  "pattern",
  "format",
  "minimum",
  "maximum",
  "title",
  "description",
  "default",
]);

function typeOk(type: string, v: unknown): boolean {
  switch (type) {
    case "object":
      return typeof v === "object" && v !== null && !Array.isArray(v);
    case "array":
      return Array.isArray(v);
    case "string":
      return typeof v === "string";
    case "integer":
      return typeof v === "number" && Number.isInteger(v);
    case "number":
      return typeof v === "number";
    case "boolean":
      return typeof v === "boolean";
    case "null":
      return v === null;
    default:
      throw new Error(`unsupported type ${type}`);
  }
}

function check(s: Schema, v: unknown, path: string, errs: string[]): void {
  for (const k of Object.keys(s)) if (!KNOWN.has(k)) throw new Error(`unsupported keyword ${k}`);
  if (typeof s.$ref === "string") {
    const name = s.$ref.replace("#/$defs/", "");
    const def = contract.$defs[name];
    if (def === undefined) throw new Error(`unknown $ref ${s.$ref}`);
    check(def, v, path, errs);
    return;
  }
  if (Array.isArray(s.anyOf)) {
    const ok = (s.anyOf as Schema[]).some((b) => {
      const e: string[] = [];
      check(b, v, path, e);
      return e.length === 0;
    });
    if (!ok) errs.push(`${path}: matches no anyOf branch`);
    return;
  }
  if ("const" in s && v !== s.const) errs.push(`${path}: expected ${String(s.const)}`);
  if (Array.isArray(s.enum) && !s.enum.includes(v)) errs.push(`${path}: not in enum`);
  if (typeof s.type === "string" && !typeOk(s.type, v)) {
    errs.push(`${path}: expected ${s.type}`);
    return;
  }
  if (typeOk("object", v) && (s.properties !== undefined || s.additionalProperties !== undefined)) {
    const obj = v as Record<string, unknown>;
    const props = (s.properties ?? {}) as Record<string, Schema>;
    for (const r of (s.required ?? []) as string[]) {
      if (!(r in obj)) errs.push(`${path}.${r}: required`);
    }
    for (const [k, val] of Object.entries(obj)) {
      const ps = props[k];
      if (ps !== undefined) check(ps, val, `${path}.${k}`, errs);
      else if (s.additionalProperties === false) errs.push(`${path}.${k}: not allowed`);
      else if (typeof s.additionalProperties === "object") {
        check(s.additionalProperties as Schema, val, `${path}.${k}`, errs);
      }
    }
  }
  if (Array.isArray(v)) {
    if (typeof s.minItems === "number" && v.length < s.minItems) errs.push(`${path}: too short`);
    if (s.items !== undefined) {
      for (const [i, x] of v.entries()) check(s.items as Schema, x, `${path}[${i}]`, errs);
    }
  }
  if (typeof v === "string") {
    if (typeof s.minLength === "number" && v.length < s.minLength) errs.push(`${path}: too short`);
    if (typeof s.maxLength === "number" && v.length > s.maxLength) errs.push(`${path}: too long`);
    if (typeof s.pattern === "string" && !new RegExp(s.pattern).test(v)) {
      errs.push(`${path}: pattern`);
    }
    if (
      s.format === "date-time" &&
      !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)$/.test(v)
    ) {
      errs.push(`${path}: not an RFC 3339 date-time`);
    }
  }
  if (typeof v === "number") {
    if (typeof s.minimum === "number" && v < s.minimum) errs.push(`${path}: below minimum`);
    if (typeof s.maximum === "number" && v > s.maximum) errs.push(`${path}: above maximum`);
  }
}

function method(name: string, version: Version): { params: Schema; result?: Schema } {
  const m = CONTRACTS[version].methods[name];
  if (m === undefined) throw new Error(`${name} is not in tern-bridge/${version}`);
  return m;
}

function checked(version: Version, s: Schema, value: unknown, path: string): string[] {
  const errs: string[] = [];
  contract = CONTRACTS[version];
  try {
    check(s, value, path, errs);
  } finally {
    contract = CONTRACTS["1.0"];
  }
  return errs;
}

/** Violations of the result schema of `name` in `version` (default 1.0; empty: conforms). */
export function resultErrors(name: string, value: unknown, version: Version = "1.0"): string[] {
  const s = method(name, version).result;
  if (s === undefined) throw new Error(`${name} has no result`);
  return checked(version, s, value, "result");
}

/** Violations of the params schema of `name` in `version` (requests and notifications). */
export function paramsErrors(name: string, value: unknown, version: Version = "1.0"): string[] {
  return checked(version, method(name, version).params, value, "params");
}
