// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SourceDetail } from "./SourceDetail";

const mockGetSource = vi.hoisted(() => vi.fn());
const mockGetSourceScanJob = vi.hoisted(() => vi.fn());

vi.mock("@api-hooks", () => ({
  useGetSource: mockGetSource,
  useDeleteSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRescanSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useListSourceTables: () => ({ data: { items: [] }, isLoading: false }),
  useApproveSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRejectSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useGetSourceScanJob: mockGetSourceScanJob,
  useListSourceScanJobs: () => ({ data: undefined }),
  useKeepRescanRemoval: () => ({ mutate: vi.fn(), isPending: false }),
  usePutSourceRescanSchedule: () => ({ mutate: vi.fn(), isPending: false }),
  usePutSourceEventRescan: () => ({ mutate: vi.fn(), isPending: false }),
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

function setSource(status: string) {
  mockGetSource.mockReturnValue({
    data: {
      body: {
        sourceId: "src-1",
        name: "Test Source",
        sourceType: "DATABASE",
        sourceSubType: "GLUE_DATABASE",
        status,
        createdAt: "2026-01-01T00:00:00Z",
        databaseDetails: {
          tablesDiscovered: 9,
          tablesApproved: 0,
          metadataEnrichmentEnabled: true,
          lastScanAt: "2026-01-02T00:00:00Z",
          lastScanJobId: "2026-01-02T00:00:00Z",
        },
      },
    },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  });
}

describe("SourceDetail — scan progress", () => {
  beforeEach(() => {
    mockGetSource.mockReset();
    mockGetSourceScanJob.mockReset();
    mockGetSourceScanJob.mockReturnValue({ data: undefined });
  });

  it("shows tables processed over total while enriching", () => {
    setSource("ENRICHING");
    mockGetSourceScanJob.mockReturnValue({
      data: { status: "IN_PROGRESS", tablesProcessed: 4, tablesTotal: 9 },
    });

    render(<SourceDetail />, { wrapper });

    expect(screen.getByText("Enrichment progress")).toBeInTheDocument();
    expect(screen.getByText("4 of 9 tables processed")).toBeInTheDocument();
  });

  it("polls the scan job only while enriching", () => {
    setSource("APPROVED");
    render(<SourceDetail />, { wrapper });
    expect(mockGetSourceScanJob.mock.calls[0][3]).toBeUndefined();

    mockGetSourceScanJob.mockClear();
    setSource("ENRICHING");
    render(<SourceDetail />, { wrapper });
    expect(mockGetSourceScanJob.mock.calls[0][3]).toBe(5000);
  });

  it("hides the bar once the source leaves ENRICHING", () => {
    // lastScanJobId then names a finished job whose counts would read as 100%,
    // so a stale full bar would sit on an approved source forever.
    setSource("APPROVED");
    mockGetSourceScanJob.mockReturnValue({
      data: { status: "COMPLETED", tablesProcessed: 9, tablesTotal: 9 },
    });

    render(<SourceDetail />, { wrapper });

    expect(screen.queryByText("Enrichment progress")).not.toBeInTheDocument();
  });

  it("hides the bar when the counts arrive as strings", () => {
    // An uncoerced DynamoDB Decimal serialises to a JSON string; that must read
    // as "no progress known" rather than as a truthy count.
    setSource("ENRICHING");
    mockGetSourceScanJob.mockReturnValue({
      data: { status: "IN_PROGRESS", tablesProcessed: "4", tablesTotal: "9" },
    });

    render(<SourceDetail />, { wrapper });

    expect(screen.queryByText("Enrichment progress")).not.toBeInTheDocument();
  });

  it("treats a missing processed count as zero against a known total", () => {
    setSource("ENRICHING");
    mockGetSourceScanJob.mockReturnValue({
      data: { status: "IN_PROGRESS", tablesTotal: 9 },
    });

    render(<SourceDetail />, { wrapper });

    expect(screen.getByText("0 of 9 tables processed")).toBeInTheDocument();
  });

  it("clamps a processed count that overshoots the total", () => {
    // The total can still be the previous scan's while this one is further along.
    setSource("ENRICHING");
    mockGetSourceScanJob.mockReturnValue({
      data: { status: "IN_PROGRESS", tablesProcessed: 12, tablesTotal: 9 },
    });

    render(<SourceDetail />, { wrapper });

    expect(screen.getByText("9 of 9 tables processed")).toBeInTheDocument();
  });
});
