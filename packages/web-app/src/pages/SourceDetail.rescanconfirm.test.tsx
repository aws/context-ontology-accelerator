// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SourceDetail } from "./SourceDetail";

// Re-scanning a source that already has an open re-scan review throws away the
// decisions and edits made inside that review, so the page must ask first and
// pass the acknowledgement the API requires. Every other status re-scans
// straight away — a prompt there would be pure friction.
const rescanMock = vi.hoisted(() => vi.fn());
const mockGetSource = vi.hoisted(() => vi.fn());

vi.mock("@api-hooks", () => ({
  useGetSource: mockGetSource,
  useDeleteSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRescanSource: () => ({
    mutate: rescanMock,
    isPending: false,
    error: null,
  }),
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

const user = userEvent.setup({ delay: null });

function setSource(status: string) {
  mockGetSource.mockReturnValue({
    data: {
      body: {
        sourceId: "src-1",
        name: "Sales warehouse",
        sourceType: "DATABASE",
        status,
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
}

describe("SourceDetail — re-scan confirmation", () => {
  beforeEach(() => {
    rescanMock.mockReset();
    mockGetSource.mockReset();
  });

  it("asks before discarding an open re-scan review instead of starting the scan", async () => {
    setSource("RESCAN_REVIEW");
    render(<SourceDetail />, { wrapper });

    await user.click(screen.getByRole("button", { name: "Re-scan" }));

    expect(
      screen.getByText(/Discard the open re-scan review\?/i),
    ).toBeInTheDocument();
    // The click must not have started anything yet.
    expect(rescanMock).not.toHaveBeenCalled();
  });

  it("names the source and says what is lost", async () => {
    setSource("RESCAN_REVIEW");
    render(<SourceDetail />, { wrapper });

    await user.click(screen.getByRole("button", { name: "Re-scan" }));

    // Scoped to the dialog: the page header carries the same name, so an
    // unscoped query would pass without the dialog naming anything.
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText("Sales warehouse")).toBeInTheDocument();
    expect(
      dialog.getByText(/anything you already approved, rejected, or edited/i),
    ).toBeInTheDocument();
  });

  it("sends the acknowledgement the API requires once confirmed", async () => {
    setSource("RESCAN_REVIEW");
    render(<SourceDetail />, { wrapper });

    await user.click(screen.getByRole("button", { name: "Re-scan" }));
    await user.click(
      screen.getByRole("button", { name: /Discard and re-scan/i }),
    );

    expect(rescanMock).toHaveBeenCalledTimes(1);
    expect(rescanMock).toHaveBeenCalledWith({ confirmDiscardOpenReview: true });
  });

  it("starts nothing when the prompt is cancelled", async () => {
    setSource("RESCAN_REVIEW");
    render(<SourceDetail />, { wrapper });

    await user.click(screen.getByRole("button", { name: "Re-scan" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(rescanMock).not.toHaveBeenCalled();
    expect(
      screen.queryByText(/Discard the open re-scan review\?/i),
    ).not.toBeInTheDocument();
  });

  it.each(["APPROVED", "SCAN_FAILED"])(
    "re-scans a %s source with no prompt and no acknowledgement",
    async (status) => {
      setSource(status);
      render(<SourceDetail />, { wrapper });

      await user.click(screen.getByRole("button", { name: "Re-scan" }));

      expect(
        screen.queryByText(/Discard the open re-scan review\?/i),
      ).not.toBeInTheDocument();
      expect(rescanMock).toHaveBeenCalledWith({});
    },
  );
});
