// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Guards Cloudscape `trackBy` and `sortingField` string literals against field
 * renames.
 *
 * Both are typed as plain strings and resolved by index (`item[key]`), so
 * TypeScript cannot tell that `trackBy="ontology_id"` no longer names a field
 * once `OntologyRecord` moved to camelCase. A stale literal yields `undefined`
 * for every row: `trackBy` then gives every `<tr>` the same React key, and
 * `sortingField` silently sorts on nothing. Neither throws.
 *
 * The tables in question are mocked to zero rows and React only *warns* on
 * duplicate keys, so this is asserted structurally rather than through a
 * rendered test.
 *
 * Two scopes, deliberately:
 *   - the *stale-name* rule sweeps every page, since a snake_case
 *     `OntologyRecord` field is wrong wherever it appears and needs no allowlist;
 *   - the *known-key* rule covers only the OntologyRecord-backed tables. An
 *     allowlist spanning all pages would have to enumerate every unrelated row
 *     type (grantId, tableId, dataType, …) and would drift into noise.
 */
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { describe, it, expect } from "vitest";

/**
 * `packages/web-app/src/pages`, located relative to this file. `fileURLToPath`
 * is given the URL *string*, not a `new URL(...)`: happy-dom replaces the
 * global `URL`, and its instances are rejected by Node's path helpers.
 */
const PAGES_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");

/** Tables whose rows are `OntologyRecord`s. */
const ONTOLOGY_TABLES = [
  "GetNamespace.tsx",
  join("ontology", "explorer", "OntologyInventoryView.tsx"),
  "OntologyPage.tsx",
];

/**
 * Keys on the generated `OntologyRecord`, plus the non-ontology keys those same
 * files legitimately track or sort by. Kept as a literal list so adding a
 * column forces a conscious update rather than silently passing.
 */
const VALID_KEYS = new Set([
  // OntologyRecord (generated, camelCase)
  "ontologyId",
  "title",
  "ontologyType",
  "classCount",
  "propertyCount",
  "createdAt",
  // other row types in the same files: the sources table (sourceId), the
  // metrics table (name, a required MetricDefinition field), and the proposals
  // table, which is still snake_case on the wire.
  "sourceId",
  "name",
  "status",
  "proposal_id",
  "created_at",
]);

/** Snake_case names that `OntologyRecord` no longer uses. */
const STALE_KEYS = [
  "ontology_id",
  "ontology_type",
  "class_count",
  "property_count",
  "domain_tags",
  "embedding_count",
];

/** Every non-test `.tsx` under src/pages, so a new table cannot dodge the guard. */
function pageFiles(dir: string = PAGES_DIR): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const full = join(dir, e.name);
    if (e.isDirectory()) return pageFiles(full);
    return e.isFile() &&
      e.name.endsWith(".tsx") &&
      !e.name.endsWith(".test.tsx")
      ? [full]
      : [];
  });
}

/** `trackBy="x"` and `sortingField: "x"` literals in a source file. */
function tableKeys(src: string): string[] {
  return [
    ...[...src.matchAll(/trackBy="([^"]+)"/g)],
    ...[...src.matchAll(/sortingField:\s*"([^"]+)"/g)],
  ].map((m) => m[1]);
}

describe("Cloudscape table keys stay in sync with their row type", () => {
  const allPages = pageFiles();

  it("finds page sources to scan", () => {
    // Sanity: if the walk breaks, every assertion below passes vacuously.
    expect(allPages.length).toBeGreaterThan(10);
    expect(allPages.some((f) => f.endsWith("GetNamespace.tsx"))).toBe(true);
  });

  it.each(ONTOLOGY_TABLES)("%s uses only known row keys", (rel) => {
    const keys = tableKeys(readFileSync(join(PAGES_DIR, rel), "utf8"));
    expect(keys.length).toBeGreaterThan(0);
    for (const key of keys) {
      expect(VALID_KEYS, `${rel}: "${key}"`).toContain(key);
    }
  });

  it("carries no unused allowlist entries", () => {
    // An allowlist with unused keys stops constraining anything: it would
    // permit a stale name if one reappeared.
    const used = new Set(
      ONTOLOGY_TABLES.flatMap((rel) =>
        tableKeys(readFileSync(join(PAGES_DIR, rel), "utf8")),
      ),
    );
    expect([...VALID_KEYS].filter((k) => !used.has(k))).toEqual([]);
  });

  it("no page keys a table on a snake_case OntologyRecord field", () => {
    const offenders: string[] = [];
    for (const file of allPages) {
      const src = readFileSync(file, "utf8");
      for (const key of STALE_KEYS) {
        if (src.includes(`trackBy="${key}"`))
          offenders.push(`${file}: trackBy="${key}"`);
        if (src.includes(`sortingField: "${key}"`))
          offenders.push(`${file}: sortingField: "${key}"`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
