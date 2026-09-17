// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi } from "vitest";
import { SourceDetail } from "./SourceDetail";

const mockGetSource = vi.hoisted(() => vi.fn());

vi.mock("@api-hooks", () => ({
  useGetSource: mockGetSource,
  useDeleteSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRescanSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useListSourceTables: () => ({ data: { items: [] }, isLoading: false }),
  useApproveSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRejectSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useGetSourceScanJob: () => ({ data: undefined }),
  useListSourceScanJobs: () => ({ data: undefined }),
  useKeepRescanRemoval: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@coa/control-plane-client", () => ({
  ReviewDecision: { APPROVED: "APPROVED", REJECTED: "REJECTED" },
  ReviewStatus: {
    APPROVED: "APPROVED",
    PENDING_REVIEW: "PENDING_REVIEW",
    REJECTED: "REJECTED",
  },
  SourceStatus: {
    PENDING_REVIEW: "PENDING_REVIEW",
    APPROVED: "APPROVED",
    APPROVING: "APPROVING",
    REJECTING: "REJECTING",
    SCANNING: "SCANNING",
    ENRICHING: "ENRICHING",
    SCAN_FAILED: "SCAN_FAILED",
    APPROVAL_FAILED: "APPROVAL_FAILED",
    REJECTION_FAILED: "REJECTION_FAILED",
  },
  ReviewSourceTableCommand: class {
    input: Record<string, unknown>;
    constructor(input: Record<string, unknown>) {
      this.input = input;
    }
  },
}));

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: vi.fn() }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter initialEntries={["/namespaces/ns-1/sources/src-1"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/sources/:sourceId"
          element={children}
        />
      </Routes>
    </MemoryRouter>
  </QueryClientProvider>
);

function makeSource(overrides: Record<string, unknown> = {}) {
  return {
    sourceId: "src-1",
    name: "Test Source",
    sourceType: "DATABASE",
    sourceSubType: "GLUE_DATABASE",
    status: "APPROVED",
    createdAt: "2026-01-01T00:00:00Z",
    databaseDetails: {
      tablesDiscovered: 5,
      tablesApproved: 5,
      metadataEnrichmentEnabled: true,
      lastScanAt: "2026-01-01T00:00:00Z",
    },
    ...overrides,
  };
}

describe("SourceDetail — metadata staleness warning", () => {
  it("shows a stale metadata warning when source is APPROVED and last scan is older than 30 days", () => {
    const sixtyDaysAgo = new Date(
      Date.now() - 60 * 24 * 60 * 60 * 1000,
    ).toISOString();

    mockGetSource.mockReturnValue({
      data: {
        body: makeSource({
          databaseDetails: {
            tablesDiscovered: 5,
            tablesApproved: 5,
            metadataEnrichmentEnabled: true,
            lastScanAt: sixtyDaysAgo,
          },
        }),
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    render(<SourceDetail />, { wrapper });

    expect(screen.getByText(/Metadata may be stale/i)).toBeInTheDocument();
    expect(
      screen.getByText(/last scanned more than 30 days ago/i),
    ).toBeInTheDocument();
  });

  it("does not show the staleness warning when the last scan is recent", () => {
    const tenDaysAgo = new Date(
      Date.now() - 10 * 24 * 60 * 60 * 1000,
    ).toISOString();

    mockGetSource.mockReturnValue({
      data: {
        body: makeSource({
          databaseDetails: {
            tablesDiscovered: 5,
            tablesApproved: 5,
            metadataEnrichmentEnabled: true,
            lastScanAt: tenDaysAgo,
          },
        }),
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    render(<SourceDetail />, { wrapper });

    expect(
      screen.queryByText(/Metadata may be stale/i),
    ).not.toBeInTheDocument();
  });

  it("does not show the staleness warning when source is not APPROVED", () => {
    const sixtyDaysAgo = new Date(
      Date.now() - 60 * 24 * 60 * 60 * 1000,
    ).toISOString();

    mockGetSource.mockReturnValue({
      data: {
        body: makeSource({
          status: "PENDING_REVIEW",
          databaseDetails: {
            tablesDiscovered: 5,
            tablesApproved: 0,
            metadataEnrichmentEnabled: true,
            lastScanAt: sixtyDaysAgo,
          },
        }),
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    render(<SourceDetail />, { wrapper });

    expect(
      screen.queryByText(/Metadata may be stale/i),
    ).not.toBeInTheDocument();
  });

  it("does not show the staleness warning when lastScanAt is missing", () => {
    mockGetSource.mockReturnValue({
      data: {
        body: makeSource({
          databaseDetails: {
            tablesDiscovered: 5,
            tablesApproved: 5,
            metadataEnrichmentEnabled: true,
            // no lastScanAt
          },
        }),
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    render(<SourceDetail />, { wrapper });

    expect(
      screen.queryByText(/Metadata may be stale/i),
    ).not.toBeInTheDocument();
  });
});
