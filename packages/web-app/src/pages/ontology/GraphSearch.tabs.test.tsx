// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Tab navigation for the Explorer page (Classes / Ontologies / Inducted
 * sources), including `?tab=` deep-linking. The two inventory views are stubbed
 * to a marker element — their contents have dedicated tests in
 * `explorer/*.test.tsx`; here we only prove which tab body the page mounts.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { GraphSearchPage } from "./GraphSearch";
import { BreadcrumbProvider } from "@components/BreadcrumbProvider";
import type {
  GraphSearchHit,
  GraphSearchResult,
  OntologyOverviewResult,
  OntologyRecord,
} from "../../services/ontology-engine";

const CLAIM = "http://ex.org/o#Claim";
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
const OVERVIEW: OntologyOverviewResult = {
  ontology_id: "o",
  classes: [
    { uri: CLAIM, label: "Claim", comment: "A claim.", subClassOf: [] },
  ],
};

const searchEntitiesPaged = vi.fn();
const getOntologyOverview = vi.fn();
const listOntologies = vi.fn();

vi.mock("../../services/ontology-engine", () => ({
  searchEntities: vi.fn(),
  searchEntitiesPaged: (...args: unknown[]) => searchEntitiesPaged(...args),
  getClass: vi.fn(),
  getObjectProperty: vi.fn(),
  getDatatypeProperty: vi.fn(),
  getOntologyOverview: (...args: unknown[]) => getOntologyOverview(...args),
  listOntologies: (...args: unknown[]) => listOntologies(...args),
}));

vi.mock("@components/ontology/PendingReviewBanner", () => ({
  PendingReviewBanner: () => null,
}));

vi.mock("../../components/graph/OntologyGraphFlow", () => ({
  OntologyGraphFlow: () => <div data-testid="graph-flow" />,
}));

// Stub the two inventory views: this file asserts tab WIRING, not their content
// (each has its own test file). A marker element per view makes "which tab body
// is mounted" unambiguous — the real views' headers would collide with the tab
// labels themselves.
vi.mock("./explorer/OntologyInventoryView", () => ({
  OntologyInventoryView: ({ namespace }: { namespace?: string }) => (
    <div data-testid="ontology-inventory">inventory:{namespace}</div>
  ),
}));
vi.mock("./explorer/InductedSourcesView", () => ({
  InductedSourcesView: ({ namespace }: { namespace?: string }) => (
    <div data-testid="inducted-sources">sources:{namespace}</div>
  ),
}));

// A STABLE client instance — the real useApiClient is useMemo-memoized, so its
// reference is stable across renders. A fresh object per render would re-trigger
// every apiClient-dependent effect.
const stableApiClient = { get: vi.fn(), getText: vi.fn() };
vi.mock("@components/ApiClientProvider", () => ({
  useApiClient: () => stableApiClient,
}));

function renderPage(search = "") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/namespaces/ns/ontology/graph${search}`]}>
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

/** The Cloudscape tab whose panel is currently active (aria-selected). */
function activeTabLabel(view: ReturnType<typeof renderPage>): string | null {
  const selected = view
    .getAllByRole("tab")
    .find((t) => t.getAttribute("aria-selected") === "true");
  return selected?.textContent?.trim() ?? null;
}

describe("GraphSearchPage — Explorer tabs", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    searchEntitiesPaged.mockResolvedValue(CLASS_PAGE);
    getOntologyOverview.mockResolvedValue(OVERVIEW);
    listOntologies.mockResolvedValue(ONTOLOGY_RECORDS);
  });

  it("defaults to the Classes tab when no ?tab= is given", async () => {
    const view = renderPage();

    await waitFor(() => expect(activeTabLabel(view)).toBe("Classes"));
    // The Classes body is the graph/list browser, identified by its List/Graph
    // toggle. Cloudscape gives each SegmentedControl segment a data-testid equal
    // to its id; the visible "List" text is repeated by its narrow-viewport Select.
    // Neither inventory view is mounted.
    expect(view.getByTestId("list")).toBeInTheDocument();
    expect(view.queryByTestId("ontology-inventory")).toBeNull();
    expect(view.queryByTestId("inducted-sources")).toBeNull();
  });

  it("opens the Ontologies tab from a ?tab=ontologies deep link", async () => {
    const view = renderPage("?tab=ontologies");

    await waitFor(() => expect(activeTabLabel(view)).toBe("Ontologies"));
    // Cloudscape mounts only the active panel, so the inventory view is the
    // rendered body and the Classes browser is absent.
    expect(view.getByTestId("ontology-inventory")).toBeInTheDocument();
    expect(view.queryByTestId("list")).toBeNull();
    // The namespace route param is threaded through to the view.
    expect(view.getByTestId("ontology-inventory")).toHaveTextContent(
      "inventory:ns",
    );
  });

  it("opens the Inducted sources tab from a ?tab=sources deep link", async () => {
    const view = renderPage("?tab=sources");

    await waitFor(() => expect(activeTabLabel(view)).toBe("Inducted sources"));
    expect(view.getByTestId("inducted-sources")).toHaveTextContent(
      "sources:ns",
    );
  });

  it("falls back to Classes for an unknown ?tab= value", async () => {
    // An unrecognized id must not leave the Tabs with an activeTabId that
    // matches no panel (which renders an empty page).
    const view = renderPage("?tab=bogus");

    await waitFor(() => expect(activeTabLabel(view)).toBe("Classes"));
    expect(view.getByTestId("list")).toBeInTheDocument();
    expect(view.queryByTestId("ontology-inventory")).toBeNull();
  });

  it("falls back to Classes for an empty ?tab= value", async () => {
    const view = renderPage("?tab=");
    await waitFor(() => expect(activeTabLabel(view)).toBe("Classes"));
    expect(view.getByTestId("list")).toBeInTheDocument();
  });

  it("switches the rendered body when the user clicks through the tabs", async () => {
    const view = renderPage();
    await waitFor(() => expect(activeTabLabel(view)).toBe("Classes"));

    // Click the TAB (not just any node with that text) so the assertion can't
    // pass off some other element carrying the same label.
    await userEvent.click(view.getByRole("tab", { name: "Ontologies" }));
    await waitFor(() =>
      expect(view.getByTestId("ontology-inventory")).toBeInTheDocument(),
    );
    expect(activeTabLabel(view)).toBe("Ontologies");
    expect(view.queryByTestId("inducted-sources")).toBeNull();

    await userEvent.click(view.getByRole("tab", { name: "Inducted sources" }));
    await waitFor(() =>
      expect(view.getByTestId("inducted-sources")).toBeInTheDocument(),
    );
    expect(activeTabLabel(view)).toBe("Inducted sources");
    expect(view.queryByTestId("ontology-inventory")).toBeNull();

    // …and back to Classes, which re-mounts the graph/list browser.
    await userEvent.click(view.getByRole("tab", { name: "Classes" }));
    await waitFor(() => expect(view.getByTestId("list")).toBeInTheDocument());
    expect(activeTabLabel(view)).toBe("Classes");
    expect(view.queryByTestId("ontology-inventory")).toBeNull();
    expect(view.queryByTestId("inducted-sources")).toBeNull();
  });

  it("always renders all three tabs, whichever one is active", async () => {
    const view = renderPage("?tab=sources");
    await waitFor(() => expect(activeTabLabel(view)).toBe("Inducted sources"));
    expect(view.getAllByRole("tab").map((t) => t.textContent?.trim())).toEqual([
      "Classes",
      "Ontologies",
      "Inducted sources",
    ]);
    // The page header is tab-independent.
    expect(screen.getByText("Explorer")).toBeInTheDocument();
  });
});
