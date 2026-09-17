// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, act, fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi } from "vitest";
import { SourceDetail } from "./SourceDetail";

// The list the Scan-history tab renders from. Each test overrides its return.
const listScanJobsMock = vi.hoisted(() => vi.fn(() => ({ data: undefined })));

const APPROVED_SOURCE = {
  sourceId: "src-1",
  sourceType: "DATABASE",
  status: "APPROVED",
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-01-05T00:00:00Z",
  databaseDetails: {
    tablesDiscovered: 3,
    tablesApproved: 3,
    lastScanAt: "2026-01-02T00:00:00Z",
    metadataEnrichmentEnabled: true,
  },
};

vi.mock("@api-hooks", () => ({
  useGetSource: () => ({
    data: { body: APPROVED_SOURCE },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
  useDeleteSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRescanSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useListSourceTables: () => ({
    data: { items: [], skippedAssets: 0 },
    isLoading: false,
  }),
  useApproveSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRejectSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useGetSourceScanJob: () => ({ data: undefined }),
  useListSourceScanJobs: listScanJobsMock,
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

const openScanHistory = async () => {
  await act(async () => {
    fireEvent.click(screen.getByText("Scan history"));
  });
};

describe("SourceDetail scan history (store-backed)", () => {
  it("renders REVIEW rows from the list: an approval and a re-scan rejection", async () => {
    listScanJobsMock.mockReturnValue({
      data: {
        items: [
          {
            at: "2026-01-06T00:00:00Z",
            eventType: "REVIEW",
            status: "APPROVED",
            decision: "APPROVED",
            isRescan: false,
            tablesApproved: 3,
          },
          {
            at: "2026-01-08T00:00:00Z",
            eventType: "REVIEW",
            status: "APPROVED",
            decision: "REJECTED",
            isRescan: true,
            tablesApproved: 3,
          },
          {
            at: "2026-01-02T00:00:00Z",
            eventType: "SCAN",
            status: "COMPLETED",
            scanType: "full",
            tablesDiscovered: 3,
          },
        ],
      },
    });

    render(<SourceDetail />, { wrapper });
    await openScanHistory();

    expect(screen.getByText("Source approved")).toBeInTheDocument();
    expect(screen.getByText("Re-scan rejected")).toBeInTheDocument();
    expect(screen.getByText("Scan completed")).toBeInTheDocument();
    // A store-derived approval carries the tablesApproved count in its detail.
    expect(screen.getByText("3 tables approved.")).toBeInTheDocument();
  });

  it("falls back to derived rows when the list is empty", async () => {
    listScanJobsMock.mockReturnValue({ data: { items: [] } });

    render(<SourceDetail />, { wrapper });
    await openScanHistory();

    // "All tables approved" is emitted only by the derived fallback path, never
    // by the store path — its presence proves the fallback ran.
    expect(screen.getByText("All tables approved")).toBeInTheDocument();
    // The store-only review label must NOT appear when falling back.
    expect(screen.queryByText("Re-scan rejected")).not.toBeInTheDocument();
  });
});
