// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { GraphSearchPage } from "./GraphSearch";
import { BreadcrumbProvider } from "@components/BreadcrumbProvider";
import {
  CLASS_FETCH_CHUNK_SIZE,
  CLASS_DISPLAY_PAGE_SIZE,
} from "@api-hooks/use-graph-classes";
import type {
  GraphSearchHit,
  GraphSearchResult,
  GraphVertex,
  OntologyOverviewResult,
  OntologyRecord,
} from "../../services/ontology-engine";

// Two classes in one ontology: Policy is a sub-class of Claim. The taxonomy
// comes from the ontology-overview (subClassOf), NOT a per-class getClass
// fan-out.
const CLAIM = "http://ex.org/o#Claim";
const POLICY = "http://ex.org/o#Policy";
const CLASSES: GraphSearchHit[] = [
  { uri: CLAIM, label: "Claim", kind: "class", graph_uris: ["wb/ns/o"] },
  { uri: POLICY, label: "Policy", kind: "class", graph_uris: ["wb/ns/o"] },
];
const CLASS_PAGE: GraphSearchResult = { hits: CLASSES, total_count: 2 };
// The List view + graph seed are now keyed off the ontology registry (complete
// set of ontology ids), not a capped wildcard class fetch.
const ONTOLOGY_RECORDS: OntologyRecord[] = [
  {
    ontologyId: "o",
    title: "Insurance",
    ontologyType: "induced",
  } as OntologyRecord,
];
const FILED_BY = "http://ex.org/o#filedBy";
const OVERVIEW: OntologyOverviewResult = {
  ontology_id: "o",
  classes: [
    { uri: CLAIM, label: "Claim", comment: "A claim.", subClassOf: [] },
    { uri: POLICY, label: "Policy", comment: null, subClassOf: [CLAIM] },
  ],
  // One object property so the graph seed shows a relationship edge in
  // addition to the class taxonomy — matches what the proposal-detail graph
  // renders on the same overview payload.
  objectProperties: [
    { uri: FILED_BY, label: "filed by", domain: POLICY, range: CLAIM },
  ],
};
// The bounded seed the backend returns for sample=true: the root + its
// (capped) descendants, no properties. Here Claim (root) + Policy (child).
const SAMPLE_OVERVIEW: OntologyOverviewResult = {
  ontology_id: "o",
  classes: [
    { uri: CLAIM, label: "Claim", comment: "A claim.", subClassOf: [] },
    { uri: POLICY, label: "Policy", comment: null, subClassOf: [CLAIM] },
  ],
};
const CLAIM_FULL: GraphVertex = {
  uri: CLAIM,
  kind: "class",
  labels: ["Claim"],
  comments: ["A claim."],
  alt_labels: [],
  graph_uris: ["wb/ns/o"],
  edges: [],
};

const searchEntities = vi.fn();
const searchEntitiesPaged = vi.fn();
const getClass = vi.fn();
const getOntologyOverview = vi.fn();
const listOntologies = vi.fn();

vi.mock("../../services/ontology-engine", () => ({
  searchEntities: (...args: unknown[]) => searchEntities(...args),
  searchEntitiesPaged: (...args: unknown[]) => searchEntitiesPaged(...args),
  getClass: (...args: unknown[]) => getClass(...args),
  getObjectProperty: vi.fn(),
  getDatatypeProperty: vi.fn(),
  getOntologyOverview: (...args: unknown[]) => getOntologyOverview(...args),
  listOntologies: (...args: unknown[]) => listOntologies(...args),
}));

vi.mock("@components/ontology/PendingReviewBanner", () => ({
  PendingReviewBanner: () => null,
}));

// Stub the React Flow canvas but surface the nodes it receives so the test can
// assert what the graph renders.
vi.mock("../../components/graph/OntologyGraphFlow", () => ({
  OntologyGraphFlow: (props: {
    nodes: Array<{ id: string; label: string }>;
    edges: Array<{ id: string; source: string; target: string; label: string }>;
    onNodeSelect?: (n: { id: string; label: string }) => void;
  }) => (
    <div data-testid="graph-flow">
      {props.nodes.map((n) => (
        <button
          key={n.id}
          data-testid="graph-node"
          onClick={() => props.onNodeSelect?.(n)}
        >
          {n.label}
        </button>
      ))}
      {props.edges.map((e) => (
        <span
          key={e.id}
          data-testid="graph-edge"
          data-source={e.source}
          data-target={e.target}
        >
          {e.label}
        </span>
      ))}
    </div>
  ),
}));

// A STABLE client instance — the real useApiClient is useMemo-memoized, so its
// reference is stable across renders. A fresh object per render would re-trigger
// every apiClient-dependent effect and mask the behavior under test.
const stableApiClient = { get: vi.fn(), getText: vi.fn() };
vi.mock("@components/ApiClientProvider", () => ({
  useApiClient: () => stableApiClient,
}));

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/namespaces/ns/ontology/graph"]}>
        <BreadcrumbProvider>
          <Routes>
            <Route
              path="/namespaces/:namespaceId/ontology/graph"
              element={<GraphSearchPage />}
            />
          </Routes>
        </BreadcrumbProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("GraphSearchPage — initial Graph view builds from ontology-overview", () => {
  beforeEach(() => {
    searchEntities.mockReset();
    searchEntitiesPaged.mockReset();
    getClass.mockReset();
    getOntologyOverview.mockReset();
    listOntologies.mockReset();
    searchEntities.mockResolvedValue(CLASSES);
    // The List view pages classes via searchEntitiesPaged.
    searchEntitiesPaged.mockResolvedValue(CLASS_PAGE);
    // Both the List view's description enrichment and the Graph view's seed
    // now fetch the FULL (un-sampled) overview per ontology — the seed only
    // falls back to sample=true when the combined class count exceeds
    // MAX_SEED_CLASSES, which this small fixture (2 classes) never does.
    getOntologyOverview.mockImplementation(
      (
        _client: unknown,
        _ns: string,
        _id: string,
        opts?: { sample?: boolean },
      ) => Promise.resolve(opts?.sample ? SAMPLE_OVERVIEW : OVERVIEW),
    );
    getClass.mockResolvedValue(CLAIM_FULL);
    // The registry drives both description enrichment and the graph seed.
    listOntologies.mockResolvedValue(ONTOLOGY_RECORDS);
  });

  it("seeds the graph from the full overview when under the seed cap, without a per-class getClass fan-out", async () => {
    renderPage();

    // Mount loads the registry, then a FULL overview per ontology for the
    // List view's descriptions.
    await waitFor(() => expect(listOntologies).toHaveBeenCalled());
    await waitFor(() => expect(getOntologyOverview).toHaveBeenCalled());

    // Toggle to Graph — under MAX_SEED_CLASSES it fetches the FULL class set
    // (no sample flag) and builds the taxonomy from all of it.
    await userEvent.click(screen.getByText("Graph"));
    await waitFor(() =>
      expect(getOntologyOverview).toHaveBeenCalledWith(
        expect.anything(),
        "ns",
        expect.any(String),
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("graph-flow")).toBeInTheDocument(),
    );
    expect(screen.getByText("Claim")).toBeInTheDocument();

    // The key win: opening the Graph view did NOT fan out getClass over every
    // class (that was the ~100-request / deadlocking path). No getClass yet.
    expect(getClass).not.toHaveBeenCalled();
  });

  it("seeds object-property relationships alongside the taxonomy on the full path", async () => {
    // The proposal-detail graph renders domain → range edges for every
    // object property in the ontology; the Explorer seed used to drop them,
    // leaving only class hierarchy. Under the seed cap, the seed now
    // materializes the same relationships so the two views agree in shape.
    renderPage();
    await waitFor(() => expect(listOntologies).toHaveBeenCalled());
    await userEvent.click(screen.getByText("Graph"));
    await waitFor(() =>
      expect(screen.getByTestId("graph-flow")).toBeInTheDocument(),
    );

    const edges = screen.getAllByTestId("graph-edge");
    const rel = edges.find((e) => e.textContent === "filed by");
    expect(rel).toBeTruthy();
    expect(rel?.getAttribute("data-source")).toBe(POLICY);
    expect(rel?.getAttribute("data-target")).toBe(CLAIM);
  });

  it("lazily fetches the full vertex when a graph node is selected", async () => {
    renderPage();
    await waitFor(() => expect(getOntologyOverview).toHaveBeenCalled());
    await userEvent.click(screen.getByText("Graph"));
    await waitFor(() =>
      expect(screen.getByTestId("graph-flow")).toBeInTheDocument(),
    );

    // Selecting a node upgrades its partial (taxonomy-only) vertex to the full
    // one via getClass, so the detail panel gets relationships/attributes.
    await userEvent.click(screen.getByText("Claim"));
    await waitFor(() =>
      expect(getClass).toHaveBeenCalledWith(expect.anything(), "ns", CLAIM),
    );
  });

  it("keeps a small induced ontology in full (with relationships) beside an oversized reference", async () => {
    // The realistic namespace shape, and the one a combined-total cap got
    // wrong: a small induced ontology plus a large foundational reference
    // loaded for grounding. The reference alone blows the cap, but it must
    // degrade only ITSELF — the induced ontology has to keep every class and
    // its relationship edges, or the user is back to the "tiny graph with no
    // relationships" they complained about.
    const BIG = "big";
    listOntologies.mockResolvedValue([
      { ontologyId: "o", title: "Insurance", ontologyType: "induced" },
      { ontologyId: BIG, title: "Schema.org", ontologyType: "foundational" },
    ] as OntologyRecord[]);
    const bigFull: OntologyOverviewResult = {
      ontology_id: BIG,
      classes: Array.from({ length: 501 }, (_, i) => ({
        uri: `https://example.com/big#B${i}`,
        label: `B${i}`,
        comment: null,
        subClassOf: [],
      })),
    };
    const bigSample: OntologyOverviewResult = {
      ontology_id: BIG,
      classes: [
        {
          uri: "https://example.com/big#B0",
          label: "B0",
          comment: null,
          subClassOf: [],
        },
      ],
    };
    getOntologyOverview.mockImplementation(
      (
        _client: unknown,
        _ns: string,
        id: string,
        opts?: { sample?: boolean },
      ) => {
        if (id === BIG)
          return Promise.resolve(opts?.sample ? bigSample : bigFull);
        return Promise.resolve(opts?.sample ? SAMPLE_OVERVIEW : OVERVIEW);
      },
    );

    renderPage();
    await waitFor(() => expect(listOntologies).toHaveBeenCalled());
    await userEvent.click(screen.getByText("Graph"));
    await waitFor(() =>
      expect(screen.getByTestId("graph-flow")).toBeInTheDocument(),
    );

    // Only the oversized ontology is re-fetched sampled.
    await waitFor(() =>
      expect(getOntologyOverview).toHaveBeenCalledWith(
        expect.anything(),
        "ns",
        BIG,
        { sample: true },
      ),
    );
    expect(getOntologyOverview).not.toHaveBeenCalledWith(
      expect.anything(),
      "ns",
      "o",
      { sample: true },
    );

    // The induced ontology kept BOTH its classes and its relationship edge...
    expect(screen.getByText("Claim")).toBeInTheDocument();
    expect(screen.getByText("Policy")).toBeInTheDocument();
    const rel = screen
      .getAllByTestId("graph-edge")
      .find((e) => e.textContent === "filed by");
    expect(rel).toBeTruthy();
    expect(rel?.getAttribute("data-source")).toBe(POLICY);
    // ...while the oversized reference contributed only its bounded sample.
    expect(screen.getByText("B0")).toBeInTheDocument();
    expect(screen.queryByText("B500")).not.toBeInTheDocument();
  });

  it("falls back to the bounded sample when the full class count exceeds the seed cap", async () => {
    // Simulate a namespace bigger than MAX_SEED_CLASSES: the full overview
    // returns more classes than the seed will render in full.
    const bigOverview: OntologyOverviewResult = {
      ontology_id: "o",
      classes: Array.from({ length: 501 }, (_, i) => ({
        uri: `https://example.com/onto#C${i}`,
        label: `C${i}`,
        comment: null,
        subClassOf: [],
      })),
    };
    getOntologyOverview.mockImplementation(
      (
        _client: unknown,
        _ns: string,
        _id: string,
        opts?: { sample?: boolean },
      ) => Promise.resolve(opts?.sample ? SAMPLE_OVERVIEW : bigOverview),
    );

    renderPage();
    await waitFor(() => expect(listOntologies).toHaveBeenCalled());
    await userEvent.click(screen.getByText("Graph"));

    // Over the cap: the seed re-fetches with sample=true and renders that
    // bounded set (Claim + Policy from SAMPLE_OVERVIEW) instead of all 501.
    await waitFor(() =>
      expect(getOntologyOverview).toHaveBeenCalledWith(
        expect.anything(),
        "ns",
        expect.any(String),
        { sample: true },
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("graph-flow")).toBeInTheDocument(),
    );
    expect(screen.getByText("Claim")).toBeInTheDocument();
    expect(screen.queryByText("C500")).not.toBeInTheDocument();
  });
});

describe("GraphSearchPage — List view ontology filter", () => {
  beforeEach(() => {
    searchEntities.mockReset();
    searchEntitiesPaged.mockReset();
    getClass.mockReset();
    getOntologyOverview.mockReset();
    listOntologies.mockReset();
    searchEntitiesPaged.mockResolvedValue(CLASS_PAGE);
    getOntologyOverview.mockResolvedValue(OVERVIEW);
    getClass.mockResolvedValue(CLAIM_FULL);
    listOntologies.mockResolvedValue(ONTOLOGY_RECORDS);
  });

  it("defaults the class query to the induced ontology, and clears the filter when 'All ontologies' is chosen", async () => {
    renderPage();

    // The List view is the default; once the registry loads, the paged class
    // query is scoped to the induced ontology ("o").
    await waitFor(() =>
      expect(searchEntitiesPaged).toHaveBeenCalledWith(
        expect.anything(),
        "ns",
        "*",
        expect.objectContaining({ kind: "class", ontology_id: "o" }),
      ),
    );

    // Target the filter trigger by its aria label (unique in the DOM) rather
    // than by visible text — "Insurance" also appears in the results table.
    const filterTrigger = screen.getByRole("button", {
      name: /Restrict classes by ontology/i,
    });
    // The selector shows the induced ontology's title as the default.
    expect(filterTrigger).toHaveTextContent("Insurance");

    // Switch to "All ontologies" → the query re-runs without an ontologyId.
    searchEntitiesPaged.mockClear();
    await userEvent.click(filterTrigger);
    await userEvent.click(screen.getByText("All ontologies"));

    await waitFor(() =>
      expect(searchEntitiesPaged).toHaveBeenCalledWith(
        expect.anything(),
        "ns",
        "*",
        expect.objectContaining({ kind: "class", ontology_id: undefined }),
      ),
    );
  });
});

describe("GraphSearchPage — List view paging across chunks", () => {
  // Chunk 1 is a FULL fetch chunk so its rows back several display pages; the
  // server reports more remain (total_count > chunk size). Ontology "o" is the
  // induced default, so the List view queries it without extra setup.
  const CHUNK1_ROWS: GraphSearchHit[] = Array.from(
    { length: CLASS_FETCH_CHUNK_SIZE },
    (_, i) => ({
      uri: `http://ex.org/o#C${i}`,
      label: `C${String(i).padStart(3, "0")}`,
      kind: "class",
      graph_uris: ["wb/ns/o"],
    }),
  );
  // Total spans exactly one extra display page (chunk1 + 20 → 6 pages of 20).
  const TOTAL = CLASS_FETCH_CHUNK_SIZE + CLASS_DISPLAY_PAGE_SIZE;
  const LAST_PAGE = TOTAL / CLASS_DISPLAY_PAGE_SIZE; // 6
  const CHUNK2_ROWS: GraphSearchHit[] = Array.from(
    { length: CLASS_DISPLAY_PAGE_SIZE },
    (_, i) => ({
      uri: `http://ex.org/o#C${CLASS_FETCH_CHUNK_SIZE + i}`,
      label: `C${String(CLASS_FETCH_CHUNK_SIZE + i).padStart(3, "0")}`,
      kind: "class",
      graph_uris: ["wb/ns/o"],
    }),
  );
  const CHUNK1: GraphSearchResult = { hits: CHUNK1_ROWS, total_count: TOTAL };
  const CHUNK2: GraphSearchResult = { hits: CHUNK2_ROWS, total_count: TOTAL };
  const PAGED_RECORDS: OntologyRecord[] = [
    { ontologyId: "o", title: "Insurance", ontologyType: "induced" },
  ] as OntologyRecord[];
  const PAGED_OVERVIEW: OntologyOverviewResult = {
    ontology_id: "o",
    classes: [],
  };

  /** Narrow the paged-search opts to its numeric offset without a type assertion. */
  function offsetOf(opts: unknown): number {
    if (typeof opts === "object" && opts !== null && "offset" in opts) {
      const { offset }: { offset?: unknown } = opts;
      return typeof offset === "number" ? offset : 0;
    }
    return 0;
  }

  let releaseChunk2: (r: GraphSearchResult) => void = () => {};

  beforeEach(() => {
    searchEntities.mockReset();
    searchEntitiesPaged.mockReset();
    getClass.mockReset();
    getOntologyOverview.mockReset();
    listOntologies.mockReset();
    getOntologyOverview.mockResolvedValue(PAGED_OVERVIEW);
    listOntologies.mockResolvedValue(PAGED_RECORDS);
    // Chunk 1 resolves immediately; chunk 2 (offset 100) is deferred so the test
    // controls when it lands — this is the ~10s stall seen in production.
    const chunk2 = new Promise<GraphSearchResult>((res) => {
      releaseChunk2 = res;
    });
    searchEntitiesPaged.mockImplementation(
      (_c: unknown, _n: unknown, _q: unknown, opts: unknown) =>
        offsetOf(opts) === 0 ? Promise.resolve(CHUNK1) : chunk2,
    );
  });

  it("shows a loading state, not a false empty, on a page whose chunk is still fetching", async () => {
    const { container } = renderPage();
    const view = within(container);
    // Chunk 1 fully rendered: its last row is on the last chunk-1-backed page.
    await view.findByText("C000");
    // The known total gives 6 display pages even before chunk 2 loads.
    const lastPageButton = await view.findByRole("button", {
      name: String(LAST_PAGE),
    });

    // Jump to the last display page — its rows (chunk 2) have not arrived.
    await userEvent.click(lastPageButton);

    // The regression: the table must read as loading, and must NOT claim the
    // namespace is empty.
    expect(await view.findByText("Loading classes…")).toBeInTheDocument();
    expect(
      view.queryByText("No classes found in this namespace yet."),
    ).toBeNull();

    // When the deferred chunk lands, the real rows appear.
    releaseChunk2(CHUNK2);
    await view.findByText(`C${String(TOTAL - 1).padStart(3, "0")}`);
  });

  it("prefetches the next chunk one page early without blanking visible rows", async () => {
    const { container } = renderPage();
    const view = within(container);

    // Gate on the SECOND query phase before interacting. renderPage() first
    // queries unfiltered; when listOntologies resolves the page defaults the
    // filter to the induced ontology, and GraphSearch.tsx resets listPageIndex
    // to 1 on every ontologyIdFilter change (correctly — a new filter's first
    // chunk cannot back a deep page). A page-5 click landing before that reset
    // is therefore discarded, taking the prefetch with it: the effect recomputes
    // rowsNeeded from page 1 (40 rows) which the loaded 100 already cover, so no
    // offset-100 fetch is ever issued and the poll below fails on a genuinely
    // absent call rather than a slow one. Waiting for the filtered call removes
    // the ordering entirely; re-finding the page button after it also waits for
    // the restarted chunk-1 to land, since the changed query key drops the rows.
    await waitFor(() =>
      expect(searchEntitiesPaged).toHaveBeenCalledWith(
        expect.anything(),
        "ns",
        "*",
        expect.objectContaining({ kind: "class", ontology_id: "o" }),
      ),
    );
    await view.findByText("C000");
    const page5 = await view.findByRole("button", { name: "5" });

    // Page 5 is the last page backed by chunk 1 (rows 80..99). Landing here must
    // kick off the offset-100 fetch while page-5 rows stay on screen.
    await userEvent.click(page5);

    // Deliberately NOT preceded by `mockClear()`. The invariant under test is
    // "offset-100 is requested while page-5 rows are still on screen" — it does
    // not matter whether that request was issued on this click or already in
    // flight from the initial load. Clearing first made the test depend on
    // which side of the clear the request landed on: under parallel-suite load
    // it could fire BEFORE the clear, and since the chunk is then cached
    // nothing re-requests it, so the assertion failed on a prefetch that had
    // demonstrably worked. The rows-visible assertions below are what keep this
    // honest about the "one page early, no blanking" behaviour.
    //
    // Explicit timeout: this polls for a state transition driven by React effect
    // scheduling (click -> setState -> effect -> fetchNextPage), which the
    // default 1000ms waitFor budget misses under full-suite parallel load. It
    // must also stay strictly UNDER the per-test timeout set on this `it` below:
    // both were previously 5000ms — vitest's default — so a slow poll burned the
    // whole test budget and died as "Test timed out in 5000ms" before waitFor
    // could report the assertion it was polling. 12000 under a 15000 per-test
    // budget keeps that ordering with room for the click and row assertions.
    await waitFor(
      () =>
        expect(
          searchEntitiesPaged.mock.calls.some(
            (c) => offsetOf(c[3]) === CLASS_FETCH_CHUNK_SIZE,
          ),
        ).toBe(true),
      { timeout: 12000 },
    );
    // Rows are NOT replaced by a spinner: a page-5 row is visible, no loading text.
    expect(view.getByText("C099")).toBeInTheDocument();
    expect(view.queryByText("Loading classes…")).toBeNull();
  }, 15000);

  it("keeps the empty state for a genuinely empty namespace", async () => {
    searchEntitiesPaged.mockReset();
    searchEntitiesPaged.mockResolvedValue({ hits: [], total_count: 0 });
    const { container } = renderPage();
    const view = within(container);
    // The page runs TWO query phases: an initial unfiltered load, then a
    // re-query once listOntologies resolves and defaults the filter to the
    // induced ontology. Between them the empty text legitimately blinks back to
    // "Loading classes…". Wait for phase 2 to have been issued first, so the
    // assertions below cannot sample the gap — the previous version asserted the
    // empty text, then absence of loading, then re-asserted the empty text
    // synchronously, and that last re-check failed (in ~94ms, nowhere near its
    // timeout) whenever the phase-2 spinner had landed in between.
    await waitFor(() =>
      expect(searchEntitiesPaged).toHaveBeenCalledWith(
        expect.anything(),
        "ns",
        "*",
        expect.objectContaining({ kind: "class", ontology_id: "o" }),
      ),
    );

    // Now poll for a single settled frame satisfying BOTH halves of the
    // regression: the namespace reads as empty, and NOT as the false-empty
    // spinner. Explicit timeout because the shared CI runner exceeds the 1000ms
    // default on first paint (the suite takes ~2m there vs ~12s locally), and it
    // must stay under the per-test budget on this `it` — both were 5000ms
    // (vitest's default) before, so a slow poll aborted the test as "Test timed
    // out in 5000ms" before it could report the assertion it was polling.
    await waitFor(
      () => {
        expect(
          view.getByText("No classes found in this namespace yet."),
        ).toBeInTheDocument();
        expect(view.queryByText("Loading classes…")).toBeNull();
      },
      { timeout: 12000 },
    );
  }, 20000);
});
