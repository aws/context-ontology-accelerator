// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SourceDetail } from "./SourceDetail";

// RESCAN_REVIEW is terminal, so the source stops polling once a review opens and
// the tables list / per-table diff never poll. The Refresh button must re-pull
// all three query families so a change made elsewhere shows without a full reload.
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
    COMPLETED: "COMPLETED",
    DELETING: "DELETING",
    RESCAN_REVIEW: "RESCAN_REVIEW",
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

const user = userEvent.setup({ delay: null });

function renderWithClient() {
  const client = new QueryClient();
  const invalidateSpy = vi.spyOn(client, "invalidateQueries");
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/namespaces/ns-1/sources/src-1"]}>
        <Routes>
          <Route
            path="/namespaces/:namespaceId/sources/:sourceId"
            element={<SourceDetail />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return invalidateSpy;
}

describe("SourceDetail — refresh", () => {
  beforeEach(() => {
    mockGetSource.mockReset();
    mockGetSource.mockReturnValue({
      data: {
        body: {
          sourceId: "src-1",
          name: "Sales warehouse",
          sourceType: "DATABASE",
          status: "RESCAN_REVIEW",
          createdAt: "2026-01-01T00:00:00Z",
          databaseDetails: {
            tablesDiscovered: 5,
            tablesApproved: 5,
            metadataEnrichmentEnabled: true,
          },
        },
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
  });

  it("re-pulls the source, tables list, and per-table queries for this source", async () => {
    const invalidateSpy = renderWithClient();

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["source", "ns-1", "src-1"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["sourceTables", "ns-1", "src-1"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["sourceTable", "ns-1", "src-1"],
    });
  });
});
