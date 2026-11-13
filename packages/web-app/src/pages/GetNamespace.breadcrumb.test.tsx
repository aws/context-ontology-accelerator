// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, act } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Opts out of the vitest-setup no-op mocks to assert the published trail.
vi.unmock("@api-hooks/use-breadcrumb-trail");
vi.unmock("@api-hooks/use-namespace");

import { GetNamespace } from "./GetNamespace";
import type { BreadcrumbItem } from "@components/BreadcrumbProvider";

const mockNamespace = {
  namespaceId: "ns-1",
  name: "test-ns",
  displayName: "Test Namespace",
  status: "ACTIVE",
  owner: "admin@example.com",
  sourceCount: 0,
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-05-01T00:00:00Z",
  description: "A test namespace",
};

vi.mock("../api-hooks/use-get-namespace", () => ({
  useGetNamespace: () => ({
    data: { namespace: mockNamespace },
    isLoading: false,
    isSuccess: true,
    error: null,
  }),
}));

vi.mock("../api-hooks/use-update-namespace", () => ({
  useUpdateNamespace: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("../api-hooks/use-list-sources", () => ({
  useListSources: () => ({
    data: { pages: [{ items: [] }] },
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

const { apiClient } = vi.hoisted(() => ({
  apiClient: { get: vi.fn().mockResolvedValue([]) },
}));

vi.mock("../components/ApiClientProvider", () => ({
  useApiClient: () => apiClient,
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

// Capture whatever trail the page publishes through the real useBreadcrumbTrail.
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

const renderPage = () =>
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={["/namespaces/ns-1"]}>
        <Routes>
          <Route path="/namespaces/:namespaceId" element={<GetNamespace />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );

beforeEach(() => {
  captured = [];
});

describe("GetNamespace breadcrumb trail", () => {
  it("publishes COA > Namespaces > {displayName}", () => {
    renderPage();
    expect(captured.map((c) => c.text)).toEqual([
      "COA",
      "Namespaces",
      "Test Namespace",
    ]);
    // The current-page crumb is non-navigable.
    expect(captured.at(-1)?.onClick).toBeUndefined();
  });

  it("appends an Edit crumb when the edit modal is open", () => {
    renderPage();
    expect(captured.map((c) => c.text)).not.toContain("Edit");

    act(() => {
      screen.getByText("Edit").click();
    });

    expect(captured.map((c) => c.text)).toEqual([
      "COA",
      "Namespaces",
      "Test Namespace",
      "Edit",
    ]);
  });
});
