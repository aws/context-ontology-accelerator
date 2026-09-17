// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import type {
  GraphSearchHit,
  GraphVertex,
  OntologyRecord,
} from "../../services/ontology-engine";
import {
  toKind,
  childUrisOf,
  parentUrisOf,
  buildGraphData,
  buildTaxonomyVertices,
  collectVisibleDescendants,
  computeRoots,
  filterClassHits,
  ontologyIdFromGraphUri,
  resolveOntologyInfo,
  resolveVertexSources,
  buildClassRows,
  buildOntologyFilterOptions,
  defaultOntologyFilterValue,
  isListPageLoading,
  planGraphSeed,
  ALL_ONTOLOGIES_VALUE,
} from "./GraphSearch";

const SUBCLASS = "http://www.w3.org/2000/01/rdf-schema#subClassOf";

type Edge = GraphVertex["edges"][number];

function subClassEdge(
  direction: "incoming" | "outgoing",
  neighborUri: string,
  kind = "class",
): Edge {
  return {
    predicate: SUBCLASS,
    predicate_label: "subClassOf",
    direction,
    neighbor: { uri: neighborUri, label: null, kind },
  };
}

function vertex(uri: string, edges: Edge[] = [], label?: string): GraphVertex {
  return {
    uri,
    kind: "class",
    labels: label ? [label] : [],
    comments: [],
    graph_uris: [],
    edges,
  };
}

// A taxonomy: A → B → C (A is the root). Each parent has an incoming
// subClassOf edge from its child; each child has an outgoing edge to its parent.
const A = "ex:A";
const B = "ex:B";
const C = "ex:C";

function taxonomy(): Map<string, GraphVertex> {
  return new Map([
    [A, vertex(A, [subClassEdge("incoming", B)], "A")],
    [
      B,
      vertex(
        B,
        [subClassEdge("outgoing", A), subClassEdge("incoming", C)],
        "B",
      ),
    ],
    [C, vertex(C, [subClassEdge("outgoing", B)], "C")],
  ]);
}

describe("toKind", () => {
  it("passes through known kinds", () => {
    expect(toKind("class")).toBe("class");
    expect(toKind("object-property")).toBe("object-property");
    expect(toKind("grounded")).toBe("grounded");
  });

  it("maps unknown/undefined to 'unknown'", () => {
    expect(toKind("literal")).toBe("unknown");
    expect(toKind(undefined)).toBe("unknown");
  });
});

describe("childUrisOf / parentUrisOf", () => {
  it("childUrisOf returns incoming subClassOf class neighbours", () => {
    expect(childUrisOf(taxonomy().get(A))).toEqual([B]);
    expect(childUrisOf(taxonomy().get(C))).toEqual([]);
  });

  it("parentUrisOf returns outgoing subClassOf class neighbours", () => {
    expect(parentUrisOf(taxonomy().get(B))).toEqual([A]);
    expect(parentUrisOf(taxonomy().get(A))).toEqual([]);
  });

  it("ignores non-class subClassOf neighbours", () => {
    const v = vertex("ex:X", [
      subClassEdge("incoming", "ex:bnode", "blank-node"),
    ]);
    expect(childUrisOf(v)).toEqual([]);
  });
});

describe("buildTaxonomyVertices", () => {
  it("builds bidirectional subClassOf edges from overview classes", () => {
    // A ← B (B subClassOf A). A must get an incoming edge (child B); B an
    // outgoing edge (parent A) — exactly what computeRoots/buildGraphData read.
    const details = buildTaxonomyVertices([
      { uri: A, label: "A", comment: "root" },
      { uri: B, label: "B", subClassOf: [A] },
    ]);
    expect(childUrisOf(details.get(A))).toEqual([B]);
    expect(parentUrisOf(details.get(B))).toEqual([A]);
    // Roots computed from the synthesized map match the taxonomy.
    expect(computeRoots(details)).toEqual([A]);
  });

  it("marks synthesized vertices partial and carries label + comment", () => {
    const details = buildTaxonomyVertices([
      { uri: A, label: "A", comment: "hi" },
    ]);
    const v = details.get(A);
    expect(v?.partial).toBe(true);
    expect(v?.labels).toEqual(["A"]);
    expect(v?.comments).toEqual(["hi"]);
  });

  it("materializes a parent not present in the overview (foundational super-class)", () => {
    const FOUNDATIONAL = "ex:Party";
    const details = buildTaxonomyVertices([
      { uri: A, subClassOf: [FOUNDATIONAL] },
    ]);
    // The parent gets a node so grounding parents still render, and A is its child.
    expect(details.has(FOUNDATIONAL)).toBe(true);
    expect(childUrisOf(details.get(FOUNDATIONAL))).toEqual([A]);
    // A is not a root (its parent is loaded); the foundational parent is.
    expect(computeRoots(details)).toEqual([FOUNDATIONAL]);
  });

  it("handles missing/empty subClassOf as a root", () => {
    const details = buildTaxonomyVertices([
      { uri: A, subClassOf: null },
      { uri: B, subClassOf: [] },
    ]);
    expect(computeRoots(details).sort()).toEqual([A, B]);
  });

  it("materializes object-property edges when domain and range are both in the class set", () => {
    // The proposal-detail graph shows domain → range edges for object
    // properties; the Explorer seed used to drop them. Now that relationships
    // are threaded through, buildTaxonomyVertices should emit an edge pair
    // (outgoing on domain, incoming on range) so buildGraphData renders the
    // relationship exactly as the proposal graph does.
    const REL = "ex:filedBy";
    const details = buildTaxonomyVertices(
      [
        { uri: A, label: "A" },
        { uri: B, label: "B" },
      ],
      [{ uri: REL, label: "filed by", domain: A, range: B }],
    );
    const outgoing = details
      .get(A)
      ?.edges.filter((e) => e.predicate === REL && e.direction === "outgoing");
    expect(outgoing).toHaveLength(1);
    expect(outgoing?.[0]?.predicate_label).toBe("filed by");
    expect(outgoing?.[0]?.neighbor.uri).toBe(B);
    const incoming = details
      .get(B)
      ?.edges.filter((e) => e.predicate === REL && e.direction === "incoming");
    expect(incoming).toHaveLength(1);
    expect(incoming?.[0]?.neighbor.uri).toBe(A);
  });

  it("drops object-property edges whose domain or range is not a class vertex", () => {
    // A property whose domain is unknown to the class set would dangle in the
    // graph — the render budget shouldn't pay for an edge with a missing
    // endpoint. Filter those out before the render-count cap is applied.
    const REL = "ex:orphan";
    const details = buildTaxonomyVertices(
      [{ uri: A, label: "A" }],
      [{ uri: REL, domain: A, range: "ex:Missing" }],
    );
    expect(details.get(A)?.edges).toEqual([]);
    // The missing range didn't get a synthesized vertex either — only
    // subClassOf parents trigger the materialize-a-missing-parent path.
    expect(details.has("ex:Missing")).toBe(false);
  });
});

describe("planGraphSeed", () => {
  /** N synthetic classes, enough to push an ontology over a cap. */
  const classes = (n: number) =>
    Array.from({ length: n }, (_, i) => ({ uri: `ex:C${i}` }));

  it("renders a small induced ontology in full even beside an oversized reference", () => {
    // The case the combined-total cap got wrong: a 40-class induced ontology
    // sitting next to Schema.org (~1010 classes) loaded for grounding. The
    // induced ontology must NOT be dragged onto the sampled path — that is the
    // "graph is tiny and has no relationships" complaint.
    const plan = planGraphSeed([
      { ontology_id: "schema.org", classes: classes(1010) },
      { ontology_id: "induced", induced: true, classes: classes(40) },
    ]);
    expect(plan.full).toEqual(["induced"]);
    expect(plan.sampled).toEqual(["schema.org"]);
  });

  it("samples only the ontologies that individually exceed the per-ontology cap", () => {
    const plan = planGraphSeed([
      { ontology_id: "big", classes: classes(501) },
      { ontology_id: "atCap", classes: classes(500) },
      { ontology_id: "small", classes: classes(10) },
    ]);
    // 500 is at the cap, so it still renders in full; only 501 is over.
    expect(plan.full).toEqual(["small", "atCap"]);
    expect(plan.sampled).toEqual(["big"]);
  });

  it("spends the total budget induced-first, then smallest-first", () => {
    // 4 × 400 = 1600 exceeds the 1500 total ceiling, so the LAST one considered
    // falls back — and the induced ontology must never be that one.
    const plan = planGraphSeed([
      { ontology_id: "c", classes: classes(400) },
      { ontology_id: "a", classes: classes(400) },
      { ontology_id: "b", classes: classes(400) },
      { ontology_id: "ind", induced: true, classes: classes(400) },
    ]);
    expect(plan.full[0]).toBe("ind");
    expect(plan.full).toHaveLength(3);
    expect(plan.sampled).toHaveLength(1);
    // Deterministic: ties break on ontology_id, so the dropped one is stable
    // rather than dependent on registry order.
    expect(plan.sampled).toEqual(["c"]);
  });

  it("is deterministic regardless of the order the registry returned", () => {
    const input = [
      { ontology_id: "z", classes: classes(300) },
      { ontology_id: "m", classes: classes(300) },
      { ontology_id: "a", classes: classes(300) },
    ];
    const forward = planGraphSeed(input);
    const reversed = planGraphSeed([...input].reverse());
    expect(reversed).toEqual(forward);
    expect(forward.full).toEqual(["a", "m", "z"]);
  });

  it("keeps an empty namespace on the full path with nothing to sample", () => {
    expect(planGraphSeed([])).toEqual({ full: [], sampled: [] });
    expect(planGraphSeed([{ ontology_id: "empty", classes: [] }])).toEqual({
      full: ["empty"],
      sampled: [],
    });
  });
});

describe("computeRoots", () => {
  it("returns only classes with no loaded parent", () => {
    expect(computeRoots(taxonomy())).toEqual([A]);
  });

  it("treats a class as a root when its parent is not loaded", () => {
    // Only B and C loaded; B's parent A is absent, so B becomes a root.
    const details = new Map<string, GraphVertex>([
      [
        B,
        vertex(
          B,
          [subClassEdge("outgoing", A), subClassEdge("incoming", C)],
          "B",
        ),
      ],
      [C, vertex(C, [subClassEdge("outgoing", B)], "C")],
    ]);
    expect(computeRoots(details)).toEqual([B]);
  });

  it("sorts expandable roots (with children) first", () => {
    const leaf = "ex:Leaf";
    const details = new Map<string, GraphVertex>([
      [leaf, vertex(leaf, [], "Leaf")],
      [A, vertex(A, [subClassEdge("incoming", B)], "A")],
    ]);
    expect(computeRoots(details)).toEqual([A, leaf]);
  });
});

describe("buildGraphData", () => {
  it("only includes visible nodes and edges between them", () => {
    const details = taxonomy();
    const { nodes, edges } = buildGraphData(new Set([A, B]), details);
    expect(nodes.map((n) => n.id).sort()).toEqual([A, B]);
    // The A–B subClassOf edge is present; C is not visible so no B–C edge.
    expect(edges).toHaveLength(1);
    expect(edges[0]).toMatchObject({ source: A, target: B });
  });

  it("draws subClassOf edges parent → child regardless of stored direction", () => {
    const details = taxonomy();
    // Visible B and C: C→B is stored 'outgoing' on C, must render B (parent) → C.
    const { edges } = buildGraphData(new Set([B, C]), details);
    expect(edges[0]).toMatchObject({ source: B, target: C });
  });

  it("annotates nodes with hasChildren / expanded flags", () => {
    const details = taxonomy();
    const collapsed = buildGraphData(new Set([A]), details).nodes[0];
    expect(collapsed.hasChildren).toBe(true);
    expect(collapsed.expanded).toBe(false);
    const expanded = buildGraphData(new Set([A, B]), details).nodes.find(
      (n) => n.id === A,
    );
    expect(expanded?.expanded).toBe(true);
  });
});

describe("collectVisibleDescendants", () => {
  it("collects all visible descendants transitively", () => {
    const details = taxonomy();
    const result = collectVisibleDescendants(A, details, new Set([A, B, C]));
    expect([...result].sort()).toEqual([B, C]);
  });

  it("stops at the boundary of what is visible", () => {
    const details = taxonomy();
    const result = collectVisibleDescendants(A, details, new Set([A, B]));
    expect([...result]).toEqual([B]);
  });
});

describe("filterClassHits", () => {
  const hits: GraphSearchHit[] = [
    {
      uri: "http://ex/Customer",
      label: "Customer",
      kind: "class",
      graph_uris: ["g"],
    },
    {
      uri: "http://ex/Order",
      label: "Order",
      kind: "class",
      graph_uris: ["g"],
    },
    {
      uri: "http://ex/order_line",
      label: "",
      kind: "class",
      graph_uris: ["g"],
    },
  ];

  it("returns all classes for an empty or whitespace query", () => {
    expect(filterClassHits(hits, "")).toHaveLength(3);
    expect(filterClassHits(hits, "   ")).toHaveLength(3);
  });

  it("matches on label case-insensitively", () => {
    const r = filterClassHits(hits, "cust");
    expect(r.map((h) => h.uri)).toEqual(["http://ex/Customer"]);
  });

  it("matches on URI when the label is empty", () => {
    const r = filterClassHits(hits, "order_line");
    expect(r.map((h) => h.uri)).toEqual(["http://ex/order_line"]);
  });

  it("matches multiple hits on a shared substring", () => {
    // "order" hits Order (label) and order_line (uri).
    const r = filterClassHits(hits, "order");
    expect(r.map((h) => h.uri).sort()).toEqual([
      "http://ex/Order",
      "http://ex/order_line",
    ]);
  });
});

describe("ontologyIdFromGraphUri", () => {
  it("decodes the last path segment as the ontologyId", () => {
    expect(
      ontologyIdFromGraphUri(
        "https://ontology-workbench.local/ns1/http%3A%2F%2Fex%2Fsales",
      ),
    ).toBe("http://ex/sales");
  });

  it("returns empty for undefined", () => {
    expect(ontologyIdFromGraphUri(undefined)).toBe("");
  });
});

describe("resolveOntologyInfo", () => {
  const rec = (id: string, title: string, type: string): OntologyRecord => ({
    ontologyId: id,
    uri: id,
    title,
    ontologyType: type,
    format: "turtle",
    domainTags: [],
    classCount: null,
    propertyCount: null,
    axiomCount: null,
    embeddingCount: null,
  });
  const gUri = (id: string) =>
    `https://ontology-workbench.local/ns1/${encodeURIComponent(id)}`;

  it("uses registry title and marks induced ontologies", () => {
    const byId = new Map([["ont:sales", rec("ont:sales", "Sales", "induced")]]);
    const info = resolveOntologyInfo(gUri("ont:sales"), byId);
    expect(info).toEqual({ id: "ont:sales", name: "Sales", induced: true });
  });

  it("non-induced type is not marked induced", () => {
    const byId = new Map([
      ["ont:fibo", rec("ont:fibo", "FIBO", "foundational")],
    ]);
    expect(resolveOntologyInfo(gUri("ont:fibo"), byId).induced).toBe(false);
  });

  it("falls back to the id local-name when not in the registry", () => {
    const info = resolveOntologyInfo(gUri("http://ex/unknown"), new Map());
    expect(info).toEqual({
      id: "http://ex/unknown",
      name: "unknown",
      induced: false,
    });
  });
});

describe("resolveVertexSources", () => {
  const rec = (id: string, title: string, type: string): OntologyRecord => ({
    ontologyId: id,
    uri: id,
    title,
    ontologyType: type,
    format: "turtle",
    domainTags: [],
    classCount: null,
    propertyCount: null,
    axiomCount: null,
    embeddingCount: null,
  });
  const gUri = (id: string) =>
    `https://ontology-workbench.local/ns1/${encodeURIComponent(id)}`;
  const byId = new Map([
    [
      "https://schema.org/",
      rec("https://schema.org/", "Schema.org", "foundational"),
    ],
    [
      "https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/FinancialProductsAndServices/",
      rec(
        "https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/FinancialProductsAndServices/",
        "FIBO Financial Products and Services",
        "foundational",
      ),
    ],
  ]);

  it("returns all source ontologies for a class in multiple graphs, sorted by name", () => {
    const sources = resolveVertexSources(
      [
        gUri("https://schema.org/"),
        gUri(
          "https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/FinancialProductsAndServices/",
        ),
      ],
      byId,
    );
    expect(sources.map((s) => s.name)).toEqual([
      "FIBO Financial Products and Services",
      "Schema.org",
    ]);
  });

  it("dedupes when the same ontology graph appears more than once", () => {
    const g = gUri("https://schema.org/");
    const sources = resolveVertexSources([g, g], byId);
    expect(sources).toHaveLength(1);
    expect(sources[0].name).toBe("Schema.org");
  });

  it("falls back to the id local-name for graphs not in the registry", () => {
    const sources = resolveVertexSources(
      [gUri("http://example.org/myonto/")],
      new Map(),
    );
    expect(sources).toHaveLength(1);
    expect(sources[0].id).toBe("http://example.org/myonto/");
  });

  it("returns an empty list for no graph_uris", () => {
    expect(resolveVertexSources(undefined, byId)).toEqual([]);
    expect(resolveVertexSources([], byId)).toEqual([]);
  });
});

describe("buildClassRows", () => {
  const hit = (uri: string, label: string, ontId: string): GraphSearchHit => ({
    uri,
    label,
    kind: "class",
    graph_uris: [
      `https://ontology-workbench.local/ns1/${encodeURIComponent(ontId)}`,
    ],
  });
  const rec = (id: string, title: string, type: string): OntologyRecord => ({
    ontologyId: id,
    uri: id,
    title,
    ontologyType: type,
    format: "turtle",
    domainTags: [],
    classCount: null,
    propertyCount: null,
    axiomCount: null,
    embeddingCount: null,
  });

  const classes = [
    hit("u:B", "Beta", "ont:fibo"), // foundational
    hit("u:A", "Alpha", "ont:sales"), // induced
    hit("u:C", "Gamma", "ont:sales"), // induced
  ];
  const byId = new Map([
    ["ont:sales", rec("ont:sales", "Sales", "induced")],
    ["ont:fibo", rec("ont:fibo", "FIBO", "foundational")],
  ]);
  // Descriptions come from the ontology-overview map (uri → comment).
  const descByUri = new Map<string, string>([["u:A", "The alpha class"]]);

  it("preserves the server's hit order and enriches each row", () => {
    // Server order must survive untouched: re-sorting client-side reshuffles
    // rows as later pages load, which duplicates and skips rows in the display
    // slice (the ordering key would differ from the backend's).
    const rows = buildClassRows(classes, byId, descByUri);
    expect(rows.map((r) => r.label)).toEqual(["Beta", "Alpha", "Gamma"]);
    expect(rows.map((r) => r.uri)).toEqual(classes.map((c) => c.uri));
    expect(rows[0]).toMatchObject({ ontologyName: "FIBO", induced: false });
    expect(rows[1]).toMatchObject({
      ontologyName: "Sales",
      induced: true,
      description: "The alpha class",
    });
  });

  it("keeps paged windows disjoint as later chunks arrive", () => {
    // Regression for the client-re-sort paging bug: build rows from chunk 1,
    // slice a display page, then rebuild from chunk 1 + chunk 2 and slice the
    // next display page. With a client-side re-sort the two slices overlap;
    // with server order preserved the first slice is a stable prefix.
    const DISPLAY = 2;
    const chunk1 = [
      hit("u:1", "Zeta", "ont:fibo"), // foundational, label sorts last
      hit("u:2", "Alpha", "ont:sales"), // induced, label sorts first
      hit("u:3", "Mu", "ont:fibo"),
      hit("u:4", "Beta", "ont:sales"),
    ];
    const chunk2 = [
      hit("u:5", "Aardvark", "ont:sales"), // would jump to the front if re-sorted
      hit("u:6", "Omega", "ont:fibo"),
    ];

    const page1 = buildClassRows(chunk1, byId, descByUri).slice(0, DISPLAY);
    const merged = buildClassRows([...chunk1, ...chunk2], byId, descByUri);
    const page2 = merged.slice(DISPLAY, DISPLAY * 2);
    const page3 = merged.slice(DISPLAY * 2, DISPLAY * 3);

    // The earlier page is unchanged by the new chunk, and pages are disjoint.
    expect(merged.slice(0, DISPLAY).map((r) => r.uri)).toEqual(
      page1.map((r) => r.uri),
    );
    const seen = [...page1, ...page2, ...page3].map((r) => r.uri);
    expect(new Set(seen).size).toBe(seen.length);
    expect(seen).toEqual(["u:1", "u:2", "u:3", "u:4", "u:5", "u:6"]);
  });

  it("defaults description to empty when none is provided", () => {
    const rows = buildClassRows(
      [hit("u:X", "Xi", "ont:sales")],
      byId,
      new Map(),
    );
    expect(rows[0].description).toBe("");
  });

  it("comma-joins all source ontologies for a class defined in multiple graphs", () => {
    const g = (id: string) =>
      `https://ontology-workbench.local/ns1/${encodeURIComponent(id)}`;
    const multi: GraphSearchHit = {
      uri: "u:Shared",
      label: "Shared",
      kind: "class",
      graph_uris: [g("ont:fibo"), g("ont:sales")],
    };
    const rows = buildClassRows([multi], byId, new Map());
    // Both source names shown (sorted by name: FIBO, then Sales).
    expect(rows[0].ontologyName).toBe("FIBO, Sales");
    // induced is true if ANY source is induced (Sales is).
    expect(rows[0].induced).toBe(true);
  });

  it("falls back to 'Unknown ontology' when a hit has no graph_uris", () => {
    const rows = buildClassRows(
      [{ uri: "u:Z", label: "Zeta", kind: "class", graph_uris: [] }],
      byId,
      new Map(),
    );
    expect(rows[0].ontologyName).toBe("Unknown ontology");
  });
});

describe("ontology filter selector", () => {
  const rec = (id: string, title: string, type: string): OntologyRecord => ({
    ontologyId: id,
    uri: id,
    title,
    ontologyType: type,
    format: "turtle",
    domainTags: [],
    classCount: null,
    propertyCount: null,
    axiomCount: null,
    embeddingCount: null,
  });

  describe("buildOntologyFilterOptions", () => {
    it("puts 'All ontologies' first, then ontologies sorted by title", () => {
      const byId = new Map([
        ["z", rec("z", "Zebra", "foundational")],
        ["a", rec("a", "Alpha", "induced")],
      ]);
      const opts = buildOntologyFilterOptions(byId);
      expect(opts.map((o) => o.label)).toEqual([
        "All ontologies",
        "Alpha",
        "Zebra",
      ]);
      expect(opts[0].value).toBe(ALL_ONTOLOGIES_VALUE);
      expect(opts[1].value).toBe("a");
    });

    it("marks induced ontologies with a description", () => {
      const byId = new Map([
        ["a", rec("a", "Alpha", "induced")],
        ["b", rec("b", "Beta", "foundational")],
      ]);
      const opts = buildOntologyFilterOptions(byId);
      expect(opts.find((o) => o.value === "a")?.description).toBe("Induced");
      expect(opts.find((o) => o.value === "b")?.description).toBeUndefined();
    });

    it("returns just 'All ontologies' for an empty registry", () => {
      const opts = buildOntologyFilterOptions(new Map());
      expect(opts).toHaveLength(1);
      expect(opts[0].value).toBe(ALL_ONTOLOGIES_VALUE);
    });
  });

  describe("defaultOntologyFilterValue", () => {
    it("defaults to the first induced ontology by title", () => {
      const byId = new Map([
        ["found", rec("found", "AAA Foundation", "foundational")],
        ["i2", rec("i2", "Sales", "induced")],
        ["i1", rec("i1", "Claims", "induced")],
      ]);
      // Claims sorts before Sales; the foundational one is ignored.
      expect(defaultOntologyFilterValue(byId)).toBe("i1");
    });

    it("falls back to All when there is no induced ontology", () => {
      const byId = new Map([["f", rec("f", "FIBO", "foundational")]]);
      expect(defaultOntologyFilterValue(byId)).toBe(ALL_ONTOLOGIES_VALUE);
    });

    it("falls back to All for an empty registry", () => {
      expect(defaultOntologyFilterValue(new Map())).toBe(ALL_ONTOLOGIES_VALUE);
    });
  });
});

describe("isListPageLoading", () => {
  const base = {
    initialLoading: false,
    fetching: false,
    loadedCount: 0,
    pageRowCount: 0,
    hasNextPage: false,
    isFetchingNextPage: false,
  };

  it("reports loading for a display page beyond the loaded chunk", () => {
    // The reported bug: chunk 1 (100 rows) loaded, user clicks to page 6 whose
    // rows (100..119) are not fetched yet. Zero rows + more coming = LOADING,
    // not the misleading "no classes" empty state.
    expect(
      isListPageLoading({
        ...base,
        loadedCount: 100,
        pageRowCount: 0,
        hasNextPage: true,
      }),
    ).toBe(true);
  });

  it("reports loading while the next chunk is in flight", () => {
    expect(
      isListPageLoading({
        ...base,
        loadedCount: 100,
        isFetchingNextPage: true,
        hasNextPage: false,
      }),
    ).toBe(true);
  });

  it("does not spin for an empty namespace or an exhausted result set", () => {
    // Empty namespace: no rows, nothing more coming → fall through to `empty`.
    expect(isListPageLoading(base)).toBe(false);
    // Over-run page past the last chunk: no more data → `empty`, not a spinner.
    expect(isListPageLoading({ ...base, loadedCount: 100 })).toBe(false);
  });

  it("keeps a backed page visible while a background chunk prefetches", () => {
    // A lookahead prefetch fires while a full page is on screen; Cloudscape's
    // `loading` hides rows, so a page WITH rows must never read as loading.
    expect(
      isListPageLoading({
        ...base,
        loadedCount: 100,
        pageRowCount: 20,
        hasNextPage: true,
        isFetchingNextPage: true,
        fetching: true,
      }),
    ).toBe(false);
  });

  it("reports loading before the first chunk resolves", () => {
    expect(isListPageLoading({ ...base, initialLoading: true })).toBe(true);
    expect(isListPageLoading({ ...base, fetching: true })).toBe(true);
  });
});

// NOTE on testing the partial-failure surfacing (#822): the failure state
// (`hydrationFailedUri`, `expandFailedUris`) lives in component `useState`, so
// it cannot be reached from the pure helpers exported here. The user-visible
// behaviour — the warning + Retry affordance when full-vertex hydration fails —
// is covered by a rendered test in `GraphSearch.failure.test.tsx`, which drives
// it through a rejecting `getClass` mock. Asserting on a hand-built Map here
// would only re-check the test's own fixture, not the failure path.
