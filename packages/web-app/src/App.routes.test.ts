// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Every source and ontology route must sit behind SchemaAccessGate, so a role
 * without schema visibility gets the neutral notice instead of a raw 403 panel
 * on any of them (including deep links). Rendering the whole app shell for
 * this would need every provider; the route table is checked from source
 * instead, the same way table-trackby.test.ts guards the ontology tables.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { describe, it, expect } from "vitest";

const APP = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "App.tsx"),
  "utf8",
);

/** `path -> element source` for every `<Route path=... element={...} />`. */
function routes(src: string): Map<string, string> {
  const out = new Map<string, string>();
  const re = /<Route\s+path="([^"]+)"\s+element=\{([\s\S]*?)\}\s*\/>/g;
  for (const m of src.matchAll(re)) out.set(m[1], m[2]);
  return out;
}

const ALL = routes(APP);

function gateKind(path: string): string | null {
  const element = ALL.get(path);
  if (element === undefined) throw new Error(`route not found: ${path}`);
  return (
    /<SchemaAccessGate kind="(source|ontology)">/.exec(element)?.[1] ?? null
  );
}

describe("App schema-access route gating", () => {
  it("finds the route table", () => {
    expect(ALL.size).toBeGreaterThan(10);
  });

  it("gates the ontology class detail route", () => {
    expect(gateKind("/namespaces/:namespaceId/ontology/induced/class")).toBe(
      "ontology",
    );
  });

  it("gates every ontology route on ontology visibility", () => {
    const ontology = [...ALL.keys()].filter((p) =>
      p.startsWith("/namespaces/:namespaceId/ontology"),
    );
    expect(ontology.length).toBeGreaterThanOrEqual(5);
    for (const p of ontology) expect(gateKind(p), p).toBe("ontology");
  });

  it("gates every source route except connect on source visibility", () => {
    const sources = [...ALL.keys()].filter(
      (p) =>
        p.startsWith("/namespaces/:namespaceId/sources") &&
        p !== "/namespaces/:namespaceId/sources/connect",
    );
    expect(sources.length).toBeGreaterThanOrEqual(3);
    for (const p of sources) expect(gateKind(p), p).toBe("source");
  });

  it("leaves metric routes ungated (fields are withheld server-side)", () => {
    expect(gateKind("/namespaces/:namespaceId/metrics")).toBeNull();
    expect(gateKind("/namespaces/:namespaceId/metrics/:metricName")).toBeNull();
  });
});
