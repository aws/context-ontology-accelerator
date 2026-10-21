// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * GraphSearch publishes its breadcrumb trail via the raw `useBreadcrumbs` slot
 * (not `useBreadcrumbTrail`) because its deeper crumbs run in-page actions
 * rather than route navigation. This test proves the one route up-link it does
 * render — the {namespace} crumb — is grant-trimmed the same way the shared
 * hook trims every other page: a link when the user has access, plain text
 * when not.
 */
import { render, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { GraphSearchPage } from "./GraphSearch";
import type { BreadcrumbItem } from "@components/BreadcrumbProvider";

vi.mock("../../services/ontology-engine", () => ({
  searchEntities: vi.fn().mockResolvedValue([]),
  searchEntitiesPaged: vi.fn().mockResolvedValue({ hits: [], total_count: 0 }),
  getClass: vi.fn(),
  getObjectProperty: vi.fn(),
  getDatatypeProperty: vi.fn(),
  getOntologyOverview: vi
    .fn()
    .mockResolvedValue({ ontology_id: "o", classes: [] }),
  listOntologies: vi.fn().mockResolvedValue([]),
}));

vi.mock("@components/ontology/PendingReviewBanner", () => ({
  PendingReviewBanner: () => null,
}));

vi.mock("../../components/graph/OntologyGraphFlow", () => ({
  OntologyGraphFlow: () => <div data-testid="graph-flow" />,
}));

vi.mock("./explorer/OntologyInventoryView", () => ({
  OntologyInventoryView: () => <div data-testid="ontology-inventory" />,
}));
vi.mock("./explorer/InductedSourcesView", () => ({
  InductedSourcesView: () => <div data-testid="inducted-sources" />,
}));

const stableApiClient = { get: vi.fn(), getText: vi.fn() };
vi.mock("@components/ApiClientProvider", () => ({
  useApiClient: () => stableApiClient,
}));

// Resolve the namespace name and access deterministically (the real hook is
// globally stubbed to echo the id with no access; this gives the crumb a
// readable label and lets tests drive access on/off).
const canAccess = vi.fn();
vi.mock("@api-hooks/use-namespace", () => ({
  useNamespace: () => ({
    name: "My Namespace",
    canAccess: canAccess(),
    isLoading: false,
  }),
}));

// Capture the trail GraphSearch publishes through the raw breadcrumb slot.
let captured: BreadcrumbItem[] = [];
vi.mock("@components/BreadcrumbProvider", () => ({
  useBreadcrumbs: () => ({
    setBreadcrumbs: (items: BreadcrumbItem[]) => {
      captured = items;
    },
    clearBreadcrumbs: () => {
      captured = [];
    },
  }),
}));

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/namespaces/ns/ontology/graph"]}>
        <Routes>
          <Route
            path="/namespaces/:namespaceId/ontology/graph"
            element={<GraphSearchPage />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The {namespace} crumb is the second entry (after COA). */
async function namespaceCrumb(): Promise<BreadcrumbItem> {
  await waitFor(() => expect(captured.length).toBeGreaterThan(1));
  const crumb = captured.find((c) => c.text === "My Namespace");
  if (!crumb) throw new Error("namespace crumb not published");
  return crumb;
}

beforeEach(() => {
  captured = [];
  canAccess.mockReset();
});

afterEach(() => cleanup());

describe("GraphSearch namespace breadcrumb — grant trimming", () => {
  it("links the namespace crumb when the user has access", async () => {
    canAccess.mockReturnValue(true);
    renderPage();

    const crumb = await namespaceCrumb();
    expect(crumb.onClick).toBeTypeOf("function");
  });

  it("renders the namespace crumb as plain text when the user has no access", async () => {
    canAccess.mockReturnValue(false);
    renderPage();

    const crumb = await namespaceCrumb();
    // Still shown (you are inside the namespace), but not navigable.
    expect(crumb.onClick).toBeUndefined();
  });
});
