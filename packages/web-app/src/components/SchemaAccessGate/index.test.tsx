// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { NoSchemaAccess, SchemaAccessGate } from "./index";

const mockSend = vi.fn();
const mockListOntologies = vi.fn();
const mockBreadcrumbTrail = vi.fn();
const apiClientStub = {};

vi.mock("@api-hooks/use-breadcrumb-trail", () => ({
  useBreadcrumbTrail: (trail: unknown) => mockBreadcrumbTrail(trail),
}));

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: mockSend }),
}));
vi.mock("@components/ApiClientProvider", () => ({
  useApiClient: () => apiClientStub,
}));
vi.mock("../../services/ontology-engine", () => ({
  listOntologies: (...args: unknown[]) => mockListOntologies(...args),
}));

function renderGate(kind: "source" | "ontology") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/namespaces/ns-1/page"]}>
        <Routes>
          <Route
            path="/namespaces/:namespaceId/page"
            element={
              <SchemaAccessGate kind={kind}>
                <div>schema page</div>
              </SchemaAccessGate>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const forbidden = Object.assign(new Error("Forbidden"), {
  $metadata: { httpStatusCode: 403 },
});

describe("SchemaAccessGate", () => {
  beforeEach(() => {
    mockSend.mockReset();
    mockListOntologies.mockReset();
    mockBreadcrumbTrail.mockReset();
  });

  it("renders the page when the caller can read sources", async () => {
    mockSend.mockResolvedValue({ items: [] });
    renderGate("source");
    expect(await screen.findByText("schema page")).toBeTruthy();
  });

  it("renders the no-access notice instead of the page on a 403", async () => {
    mockSend.mockRejectedValue(forbidden);
    renderGate("source");
    expect(await screen.findByTestId("no-schema-access")).toBeTruthy();
    expect(screen.queryByText("schema page")).toBeNull();
  });

  it("leaves other errors to the page", async () => {
    mockSend.mockRejectedValue(
      Object.assign(new Error("boom"), { $metadata: { httpStatusCode: 500 } }),
    );
    renderGate("source");
    expect(await screen.findByText("schema page")).toBeTruthy();
  });

  it("gates ontology pages on the ontology list (ApiError status 403)", async () => {
    mockListOntologies.mockRejectedValue(
      Object.assign(new Error("Forbidden"), { status: 403 }),
    );
    renderGate("ontology");
    expect(await screen.findByTestId("no-schema-access")).toBeTruthy();
    expect(mockSend).not.toHaveBeenCalled();
  });
});

describe("SchemaAccessGate caching", () => {
  it("re-checks a 'no access' answer on the next visit (a 403 can be transient)", async () => {
    mockSend.mockReset();
    mockSend
      .mockRejectedValueOnce(forbidden)
      .mockResolvedValueOnce({ items: [] });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const tree = (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/namespaces/ns-1/page"]}>
          <Routes>
            <Route
              path="/namespaces/:namespaceId/page"
              element={
                <SchemaAccessGate kind="source">
                  <div>schema page</div>
                </SchemaAccessGate>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const first = render(tree);
    expect(await screen.findByTestId("no-schema-access")).toBeTruthy();
    first.unmount();
    render(tree);
    expect(await screen.findByText("schema page")).toBeTruthy();
    expect(mockSend).toHaveBeenCalledTimes(2);
  });
});

describe("SchemaAccessGate probes", () => {
  beforeEach(() => {
    mockSend.mockReset();
    mockListOntologies.mockReset();
  });

  it("asks the ontology list for a single item", async () => {
    mockListOntologies.mockResolvedValue([]);
    renderGate("ontology");
    expect(await screen.findByText("schema page")).toBeTruthy();
    expect(mockListOntologies).toHaveBeenCalledTimes(1);
    expect(mockListOntologies).toHaveBeenCalledWith(apiClientStub, "ns-1", {
      limit: 1,
    });
  });

  it("asks the source list for a single item", async () => {
    mockSend.mockResolvedValue({ items: [] });
    renderGate("source");
    expect(await screen.findByText("schema page")).toBeTruthy();
    expect(mockSend).toHaveBeenCalledTimes(1);
    expect(mockSend.mock.calls[0][0].input).toEqual({
      namespaceId: "ns-1",
      maxResults: 1,
    });
  });
});

describe("SchemaAccessGate route-level notice", () => {
  beforeEach(() => {
    mockSend.mockReset();
    mockListOntologies.mockReset();
    mockBreadcrumbTrail.mockReset();
  });

  it.each([
    ["source" as const, "Sources"],
    ["ontology" as const, "Ontology"],
  ])(
    "gives a denied %s page an h1, breadcrumbs and a Playground link",
    async (kind, title) => {
      const denied = Object.assign(new Error("Forbidden"), { status: 403 });
      mockSend.mockRejectedValue(denied);
      mockListOntologies.mockRejectedValue(denied);
      renderGate(kind);
      expect(await screen.findByTestId("no-schema-access")).toBeTruthy();
      expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(title);
      const link = screen.getByRole("link", { name: "Playground" });
      expect(link.getAttribute("href")).toBe("/playground");
      expect(screen.getByTestId("no-schema-access").textContent).toContain(
        "You can still query this namespace in the Playground.",
      );
      expect(mockBreadcrumbTrail).toHaveBeenCalledWith([
        { text: "ns-1", href: "/namespaces/ns-1", namespaceId: "ns-1" },
        { text: title },
      ]);
    },
  );

  it("publishes no trail and renders no heading while the page is allowed", async () => {
    mockSend.mockResolvedValue({ items: [] });
    renderGate("source");
    expect(await screen.findByText("schema page")).toBeTruthy();
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
    expect(mockBreadcrumbTrail).not.toHaveBeenCalled();
  });

  it("keeps the compact notice heading-free for in-page slots", () => {
    render(<NoSchemaAccess kind="ontology" />);
    expect(screen.getByTestId("no-schema-access")).toBeTruthy();
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
    expect(mockBreadcrumbTrail).not.toHaveBeenCalled();
  });
});
