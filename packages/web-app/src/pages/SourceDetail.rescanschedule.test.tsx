// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SourceDetail } from "./SourceDetail";

const mockGetSource = vi.hoisted(() => vi.fn());
const mockPutSchedule = vi.hoisted(() => vi.fn());
const mockEventError = vi.hoisted(() => vi.fn(() => null as Error | null));

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
  usePutSourceRescanSchedule: () => ({
    mutate: mockPutSchedule,
    isPending: false,
    error: null,
    isSuccess: false,
  }),
  usePutSourceEventRescan: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: mockEventError(),
  }),
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

const user = userEvent.setup({ delay: null });

function setSchedule(expression: string) {
  mockGetSource.mockReturnValue({
    data: {
      body: {
        sourceId: "src-1",
        name: "Test Source",
        sourceType: "DATABASE",
        sourceSubType: "GLUE_DATABASE",
        status: "APPROVED",
        createdAt: "2026-01-01T00:00:00Z",
        databaseDetails: {
          tablesDiscovered: 3,
          tablesApproved: 3,
          metadataEnrichmentEnabled: true,
          rescanSchedule: {
            enabled: true,
            scheduleExpression: expression,
            timezone: "UTC",
          },
        },
      },
    },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  });
}

const openSettings = () =>
  user.click(screen.getByRole("tab", { name: /settings/i }));

const expressionBox = () =>
  screen.getByPlaceholderText("rate(1 day)") as HTMLInputElement;

describe("SourceDetail — rescan schedule form state", () => {
  beforeEach(() => {
    mockGetSource.mockReset();
    mockPutSchedule.mockReset();
    mockEventError.mockReturnValue(null);
  });

  it("surfaces an event-rescan failure instead of just reverting the toggle", async () => {
    // Stewards hit this every time while the route was missing from the
    // authorizer, and saw only a toggle that flipped back.
    mockEventError.mockReturnValue(
      new Error("Event-driven rescans unavailable"),
    );
    setSchedule("rate(1 day)");
    render(<SourceDetail />, { wrapper });
    await openSettings();

    expect(
      screen.getByText("Could not change event-driven rescans"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Event-driven rescans unavailable"),
    ).toBeInTheDocument();
  });

  it("shows the schedule the server reports", async () => {
    setSchedule("rate(1 day)");
    render(<SourceDetail />, { wrapper });
    await openSettings();

    expect(expressionBox().value).toBe("rate(1 day)");
  });

  it("picks up a server-side change while the form is untouched", async () => {
    setSchedule("rate(1 day)");
    const { rerender } = render(<SourceDetail />, { wrapper });
    await openSettings();
    expect(expressionBox().value).toBe("rate(1 day)");

    // The value changed elsewhere and the 10s poll brought it in.
    setSchedule("rate(6 hours)");
    rerender(<SourceDetail />);

    expect(expressionBox().value).toBe("rate(6 hours)");
  });

  it("does not overwrite what the user is typing when a poll lands", async () => {
    // useGetSource polls every 10s, so an unconditional props-to-state sync
    // would wipe a half-typed expression under the user.
    setSchedule("rate(1 day)");
    const { rerender } = render(<SourceDetail />, { wrapper });
    await openSettings();

    await user.clear(expressionBox());
    await user.type(expressionBox(), "rate(12 hours)");

    setSchedule("rate(1 day)");
    rerender(<SourceDetail />);

    expect(expressionBox().value).toBe("rate(12 hours)");
  });

  it("sends the edited expression on save", async () => {
    setSchedule("rate(1 day)");
    render(<SourceDetail />, { wrapper });
    await openSettings();

    await user.clear(expressionBox());
    await user.type(expressionBox(), "rate(2 days)");
    await user.click(screen.getByRole("button", { name: /save schedule/i }));

    expect(mockPutSchedule).toHaveBeenCalledWith({
      enabled: true,
      scheduleExpression: "rate(2 days)",
      timezone: "UTC",
    });
  });
});
