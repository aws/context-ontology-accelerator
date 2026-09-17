// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { GetNamespace } from "./GetNamespace";

const baseNamespace = {
  namespaceId: "ns-1",
  name: "test-ns",
  displayName: "Test Namespace",
  status: "ACTIVE",
  owner: "admin@example.com",
  sourceCount: 4,
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-05-01T00:00:00Z",
  description: "A test namespace",
};

// Mutable so individual tests can set vkgHealth; reset in beforeEach.
let mockNamespace: Record<string, unknown> = { ...baseNamespace };

vi.mock("../api-hooks/use-get-namespace", () => ({
  useGetNamespace: () => ({
    data: {
      namespace: mockNamespace,
    },
    isLoading: false,
    error: null,
  }),
}));

vi.mock("../api-hooks/use-update-namespace", () => ({
  useUpdateNamespace: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("../api-hooks/use-list-sources", () => ({
  useListSources: () => ({
    data: {
      pages: [
        {
          items: [
            {
              sourceId: "src-1",
              name: "My Database",
              status: "APPROVED",
              tablesDiscovered: 5,
            },
          ],
        },
      ],
    },
    isLoading: false,
    error: null,
  }),
}));

vi.mock("../api-hooks/use-metrics", () => ({
  useListMetrics: () => ({
    data: { pages: [] },
    isLoading: false,
    error: null,
  }),
}));

vi.mock("../components/ApiClientProvider", () => ({
  useApiClient: () => ({ get: vi.fn().mockResolvedValue([]) }),
}));

vi.mock("../services/ontology-engine", () => ({
  listOntologies: vi.fn().mockResolvedValue([]),
}));

vi.mock("@coa/control-plane-client", () => ({
  NamespaceStatus: {
    ACTIVE: "ACTIVE",
    ARCHIVED: "ARCHIVED",
    DELETING: "DELETING",
    DELETED: "DELETED",
    DELETE_FAILED: "DELETE_FAILED",
  },
  SourceStatus: { APPROVED: "APPROVED" },
  SourceType: { DATABASE: "DATABASE", DOCUMENTS: "DOCUMENTS" },
  VkgHealthStatus: {
    HEALTHY: "HEALTHY",
    DEGRADED: "DEGRADED",
    UNAVAILABLE: "UNAVAILABLE",
    PROVISIONING: "PROVISIONING",
    UNKNOWN: "UNKNOWN",
  },
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter initialEntries={["/namespaces/ns-1"]}>
      <Routes>
        <Route path="/namespaces/:namespaceId" element={children} />
      </Routes>
    </MemoryRouter>
  </QueryClientProvider>
);

describe("GetNamespace", () => {
  beforeEach(() => {
    mockNamespace = { ...baseNamespace };
  });

  it("renders the namespace display name as header", () => {
    render(<GetNamespace />, { wrapper });
    expect(
      screen.getByRole("heading", { name: "Test Namespace" }),
    ).toBeInTheDocument();
  });

  it("renders the summary section", () => {
    render(<GetNamespace />, { wrapper });
    expect(screen.getByText("Summary")).toBeInTheDocument();
    expect(screen.getByText("ns-1")).toBeInTheDocument();
    expect(screen.getByText("admin@example.com")).toBeInTheDocument();
  });

  it("renders VKG health, defaulting to UNKNOWN with an explanatory tooltip trigger", () => {
    render(<GetNamespace />, { wrapper });
    expect(screen.getByText("VKG health")).toBeInTheDocument();
    // Namespace mock has no vkgHealth -> defaults to UNKNOWN.
    const status = screen.getByText("UNKNOWN");
    expect(status).toBeInTheDocument();
    // Popover wraps the indicator as a text trigger (button role).
    expect(
      screen.getByRole("button", { name: /UNKNOWN/i }),
    ).toBeInTheDocument();
  });

  it("renders DEGRADED VKG health as an error indicator (running but cannot translate)", () => {
    mockNamespace = { ...baseNamespace, vkgHealth: "DEGRADED" };
    render(<GetNamespace />, { wrapper });
    expect(screen.getByText("VKG health")).toBeInTheDocument();
    const status = screen.getByText("DEGRADED");
    expect(status).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /DEGRADED/i }),
    ).toBeInTheDocument();
  });

  it("surfaces the server-provided vkgHealthReason in the health popover", async () => {
    const userEvent = (await import("@testing-library/user-event")).default;
    mockNamespace = {
      ...baseNamespace,
      vkgHealth: "DEGRADED",
      vkgHealthReason: "container health check failing — cannot answer queries",
    };
    render(<GetNamespace />, { wrapper });
    // Open the popover on the health indicator.
    await userEvent.click(screen.getByRole("button", { name: /DEGRADED/i }));
    expect(
      screen.getByText(/container health check failing/i),
    ).toBeInTheDocument();
  });

  it("renders Edit and Permissions action buttons", () => {
    render(<GetNamespace />, { wrapper });
    expect(screen.getByRole("button", { name: /edit/i })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /permissions/i }),
    ).toBeInTheDocument();
  });

  it("does not render a Data Sources action button (removed)", () => {
    render(<GetNamespace />, { wrapper });
    expect(
      screen.queryByRole("button", { name: /^data sources$/i }),
    ).not.toBeInTheDocument();
  });

  it("renders Sources, Ontologies, and Metrics tabs but not Members", () => {
    render(<GetNamespace />, { wrapper });
    expect(screen.getByRole("tab", { name: /sources/i })).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: /ontologies/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /metrics/i })).toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: /members/i }),
    ).not.toBeInTheDocument();
  });

  it("links each data source name to its detail page", () => {
    render(<GetNamespace />, { wrapper });
    const link = screen.getByRole("link", { name: "My Database" });
    expect(link).toHaveAttribute("href", "/namespaces/ns-1/sources/src-1");
  });
});
