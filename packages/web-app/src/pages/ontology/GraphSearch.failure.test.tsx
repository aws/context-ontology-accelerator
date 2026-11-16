// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Partial-load failure surfacing in the Explorer.
 *
 * The Explorer renders a `partial` taxonomy vertex immediately and then fetches
 * the full class. When that fetch fails, the detail panel shows a warning +
 * Retry affordance, and a successful Retry clears the warning. These tests
 * drive that path with a rejecting `getClass`.
 */
import { render, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { GraphSearchPage } from "./GraphSearch";
import { BreadcrumbProvider } from "@components/BreadcrumbProvider";
import type {
  GraphSearchHit,
  GraphSearchResult,
  GraphVertex,
  OntologyOverviewResult,
  OntologyRecord,
} from "../../services/ontology-engine";

const CLAIM = "http://ex.org/o#Claim";
const POLICY = "http://ex.org/o#Policy";

const CLASSES: GraphSearchHit[] = [
  { uri: CLAIM, label: "Claim", kind: "class", graph_uris: ["wb/ns/o"] },
];
const CLASS_PAGE: GraphSearchResult = { hits: CLASSES, total_count: 1 };
const ONTOLOGY_RECORDS: OntologyRecord[] = [
  {
    ontologyId: "o",
    uri: "o",
    title: "Insurance",
    ontologyType: "induced",
    format: "turtle",
    domainTags: [],
    classCount: 1,
    propertyCount: 0,
    axiomCount: 0,
    embeddingCount: 1,
    createdAt: "2026-07-01T00:00:00Z",
    updatedAt: "2026-07-01T00:00:00Z",
  },
];
// The overview seeds a PARTIAL vertex for Claim — this is what makes the page
// attempt the full-class hydration that these tests fail.
const OVERVIEW: OntologyOverviewResult = {
  ontology_id: "o",
  classes: [
    { uri: CLAIM, label: "Claim", comment: "A claim.", subClassOf: [] },
    { uri: POLICY, label: "Policy", comment: "A policy.", subClassOf: [] },
  ],
};
const FULL_CLAIM: GraphVertex = {
  uri: CLAIM,
  kind: "class",
  labels: ["Claim"],
  comments: ["A claim."],
  edges: [],
};

const searchEntitiesPaged = vi.fn();
const getOntologyOverview = vi.fn();
const listOntologies = vi.fn();
const getClass = vi.fn();

vi.mock("../../services/ontology-engine", () => ({
  searchEntities: vi.fn(),
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

// Stub the graph but keep the onNodeSelect seam: clicking this button invokes
// the page's real `handleNodeSelect` (graph node select runs its hydration;
// the List-row path surfaces errors via setError, so it would not exercise
// this).
vi.mock("../../components/graph/OntologyGraphFlow", () => ({
  OntologyGraphFlow: ({
    onNodeSelect,
  }: {
    onNodeSelect?: (n: { id: string; label: string; kind: string }) => void;
  }) => (
    <>
      <button
        data-testid="graph-node"
        onClick={() =>
          onNodeSelect?.({
            id: "http://ex.org/o#Claim",
            label: "Claim",
            kind: "class",
          })
        }
      >
        select-node
      </button>
      <button
        data-testid="graph-node-b"
        onClick={() =>
          onNodeSelect?.({
            id: "http://ex.org/o#Policy",
            label: "Policy",
            kind: "class",
          })
        }
      >
        select-node-b
      </button>
    </>
  ),
}));

vi.mock("./explorer/OntologyInventoryView", () => ({
  OntologyInventoryView: () => <div data-testid="ontology-inventory" />,
}));
vi.mock("./explorer/InductedSourcesView", () => ({
  InductedSourcesView: () => <div data-testid="inducted-sources" />,
}));

// A STABLE client instance — the real useApiClient is useMemo-memoized. A fresh
// object per render would re-trigger every apiClient-dependent effect.
const stableApiClient = { get: vi.fn(), getText: vi.fn() };
vi.mock("@components/ApiClientProvider", () => ({
  useApiClient: () => stableApiClient,
}));

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
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
  // Scope queries to THIS render's container — a prior test's tree can linger
  // briefly before cleanup(), and the page re-renders as its fetches resolve.
  return within(result.container);
}

async function selectClaimNodeInGraph(view: ReturnType<typeof renderPage>) {
  const user = userEvent.setup();
  // Switch to the Graph view, then click the stubbed node so the page runs
  // handleNodeSelect -> getClass(...) for a `partial` vertex.
  await user.click(await view.findByText("Graph"));
  await user.click(await view.findByTestId("graph-node"));
  return user;
}

describe("Explorer — partial-load failure surfacing", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    searchEntitiesPaged.mockResolvedValue(CLASS_PAGE);
    getOntologyOverview.mockResolvedValue(OVERVIEW);
    listOntologies.mockResolvedValue(ONTOLOGY_RECORDS);
  });

  it("warns instead of silently showing a truncated class when hydration fails", async () => {
    getClass.mockRejectedValue(new Error("500 upstream"));

    const view = renderPage();
    await selectClaimNodeInGraph(view);

    // The failed hydration must be visible — not swallowed to console.warn.
    await waitFor(() =>
      expect(getClass).toHaveBeenCalledWith(
        expect.anything(),
        "ns",
        expect.stringContaining("Claim"),
      ),
    );
    await view.findByText(/couldn't be loaded/i);
    expect(view.getByText("Retry")).toBeInTheDocument();
  });

  it("clears the warning when Retry succeeds", async () => {
    getClass.mockRejectedValueOnce(new Error("500 upstream"));

    const view = renderPage();
    const user = await selectClaimNodeInGraph(view);
    await view.findByText(/couldn't be loaded/i);

    // Second attempt succeeds → the marker goes away.
    getClass.mockResolvedValue(FULL_CLAIM);
    await user.click(view.getByText("Retry"));

    await waitFor(() =>
      expect(view.queryByText(/couldn't be loaded/i)).not.toBeInTheDocument(),
    );
  });

  it("a slow success for a deselected node must not wipe the selected node's warning", async () => {
    // An unguarded setHydrationFailedUri(null) in the .then() would let A's late
    // success clear B's legitimate warning, showing B's partial vertex as if it
    // were complete.
    //   1. select A (Claim)  -> getClass hangs
    //   2. select B (Policy) -> getClass rejects fast, marker = B
    //   3. A resolves        -> must NOT clear B's marker
    let resolveClaim: ((v: GraphVertex) => void) | undefined;
    getClass.mockImplementation((_c: unknown, _ns: unknown, uri: string) => {
      if (uri === CLAIM) {
        return new Promise<GraphVertex>((res) => {
          resolveClaim = res;
        });
      }
      return Promise.reject(new Error("500 policy"));
    });

    const view = renderPage();
    const user = userEvent.setup();
    await user.click(await view.findByText("Graph"));

    // 1. A: hydration in flight (never settles yet).
    await user.click(await view.findByTestId("graph-node"));
    // 2. B: fails fast -> its warning is the one on screen.
    await user.click(await view.findByTestId("graph-node-b"));
    await view.findByText(/couldn't be loaded/i);

    // 3. A finally resolves, for a node the user is no longer looking at.
    resolveClaim?.({
      uri: CLAIM,
      kind: "class",
      labels: ["Claim"],
      comments: [],
      edges: [],
    });

    // B is still selected and still failed -> the warning must survive.
    await new Promise((r) => setTimeout(r, 50));
    expect(view.getByText(/couldn't be loaded/i)).toBeInTheDocument();
  });
});
