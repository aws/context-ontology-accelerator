// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { vi, describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import React from "react";
import { SourceDetail } from "../../../src/pages/SourceDetail";

// ── Mock all the api-hooks the page touches ──────────────────────────────────

const mockUseGetSource = vi.fn();
const mockUseDeleteSource = vi.fn();
const mockUseRescanSource = vi.fn();
const mockUseListSourceTables = vi.fn();
const mockUseApproveSource = vi.fn();
const mockUseRejectSource = vi.fn();
const mockUseGetSourceScanJob = vi.fn();

vi.mock("@api-hooks", () => ({
  useGetSource: (...args: unknown[]) => mockUseGetSource(...args),
  useDeleteSource: (...args: unknown[]) => mockUseDeleteSource(...args),
  useRescanSource: (...args: unknown[]) => mockUseRescanSource(...args),
  useListSourceTables: (...args: unknown[]) => mockUseListSourceTables(...args),
  useApproveSource: (...args: unknown[]) => mockUseApproveSource(...args),
  useRejectSource: (...args: unknown[]) => mockUseRejectSource(...args),
  useGetSourceScanJob: (...args: unknown[]) => mockUseGetSourceScanJob(...args),
  useKeepRescanRemoval: () => ({ mutate: vi.fn(), isPending: false }),
}));

// Pin only the enum values the page reads. Avoids loading the full smithy
// client during unit tests.
vi.mock("@coa/control-plane-client", () => ({
  ReviewStatus: {
    PENDING_REVIEW: "PENDING_REVIEW",
    APPROVED: "APPROVED",
    REJECTED: "REJECTED",
  },
  SourceStatus: {
    REGISTERED: "REGISTERED",
    SCANNING: "SCANNING",
    ENRICHING: "ENRICHING",
    PENDING_REVIEW: "PENDING_REVIEW",
    APPROVING: "APPROVING",
    REJECTING: "REJECTING",
    APPROVED: "APPROVED",
    RESCAN_REVIEW: "RESCAN_REVIEW",
    COMPLETED: "COMPLETED",
    SCAN_FAILED: "SCAN_FAILED",
    APPROVAL_FAILED: "APPROVAL_FAILED",
    REJECTION_FAILED: "REJECTION_FAILED",
    DELETING: "DELETING",
  },
}));

vi.mock("@utils/helpers", () => ({
  formatTimestamp: (v: unknown) => String(v ?? "—"),
}));

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: vi.fn() }),
}));

vi.mock("@tanstack/react-query", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-query")>();
  return { ...actual, useQueryClient: () => ({ invalidateQueries: vi.fn() }) };
});

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={["/namespaces/ns-1/sources/src-1"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/sources/:sourceId"
          element={<SourceDetail />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function setupBaseMocks(
  overrides: {
    status?: string;
    tablesDiscovered?: number;
    tables?: { tableId: string; reviewStatus: string }[];
  } = {},
) {
  const status = overrides.status ?? "PENDING_REVIEW";
  const tablesDiscovered = overrides.tablesDiscovered ?? 0;
  const tables = overrides.tables ?? [];

  mockUseGetSource.mockReturnValue({
    data: {
      body: {
        sourceId: "src-1",
        name: "Sales DB",
        sourceType: "DATABASE",
        sourceSubType: "GLUE_DATABASE",
        status,
        createdAt: "2026-01-01T00:00:00Z",
        updatedAt: "2026-05-01T00:00:00Z",
        databaseDetails: {
          tablesDiscovered,
          tablesApproved: 0,
          lastScanAt: "2026-04-01T00:00:00Z",
          glueConfiguration: {
            catalogId: "cat",
            region: "us-east-1",
            databaseName: "db",
          },
        },
      },
    },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  });

  mockUseDeleteSource.mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    error: null,
  });
  mockUseRescanSource.mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    error: null,
  });
  mockUseListSourceTables.mockReturnValue({
    data: { items: tables },
    isLoading: false,
  });
  mockUseApproveSource.mockReturnValue({ mutate: vi.fn(), isPending: false });
  mockUseRejectSource.mockReturnValue({ mutate: vi.fn(), isPending: false });
  mockUseGetSourceScanJob.mockReturnValue({ data: undefined });
}

describe("SourceDetail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("disables Re-scan for non-re-scannable statuses (e.g. PENDING_REVIEW)", () => {
    setupBaseMocks({ status: "PENDING_REVIEW", tablesDiscovered: 5 });
    renderDetail();
    const rescan = screen.getByRole("button", { name: /re-scan/i });
    expect(rescan).toBeDisabled();
  });

  it("enables Re-scan when status is SCAN_FAILED", () => {
    setupBaseMocks({ status: "SCAN_FAILED", tablesDiscovered: 0 });
    renderDetail();
    const rescan = screen.getByRole("button", { name: /re-scan/i });
    expect(rescan).not.toBeDisabled();
  });

  it("enables Re-scan when an APPROVED database source can drift (drift re-scan)", () => {
    setupBaseMocks({ status: "APPROVED", tablesDiscovered: 5 });
    renderDetail();
    const rescan = screen.getByRole("button", { name: /re-scan/i });
    expect(rescan).not.toBeDisabled();
  });

  it("disables Approve source and Reject source when no tables are scanned", () => {
    setupBaseMocks({ status: "PENDING_REVIEW", tablesDiscovered: 0 });
    renderDetail();
    expect(
      screen.getByRole("button", { name: /approve source/i }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /reject source/i }),
    ).toBeDisabled();
  });

  it("enables Approve source when tables exist and status is reviewable", () => {
    setupBaseMocks({
      status: "PENDING_REVIEW",
      tablesDiscovered: 3,
      tables: [
        { tableId: "t1", reviewStatus: "PENDING_REVIEW" },
        { tableId: "t2", reviewStatus: "PENDING_REVIEW" },
        { tableId: "t3", reviewStatus: "PENDING_REVIEW" },
      ],
    });
    renderDetail();
    expect(
      screen.getByRole("button", { name: /approve source/i }),
    ).not.toBeDisabled();
    expect(
      screen.getByRole("button", { name: /reject source/i }),
    ).not.toBeDisabled();
  });

  it("shows a hint explaining the cascade to tables/columns on hover", async () => {
    setupBaseMocks({
      status: "PENDING_REVIEW",
      tablesDiscovered: 3,
      tables: [{ tableId: "t1", reviewStatus: "PENDING_REVIEW" }],
    });
    renderDetail();

    const approve = screen.getByRole("button", { name: /approve source/i });
    expect(
      screen.queryByText(/pending tables and columns/i),
    ).not.toBeInTheDocument();

    fireEvent.mouseEnter(approve.parentElement!);
    expect(
      screen.getByText(/approves all pending tables and columns/i),
    ).toBeInTheDocument();

    fireEvent.mouseLeave(approve.parentElement!);
    expect(
      screen.queryByText(/pending tables and columns/i),
    ).not.toBeInTheDocument();
  });

  it("renders the Scan history tab", () => {
    setupBaseMocks({ status: "PENDING_REVIEW", tablesDiscovered: 5 });
    renderDetail();
    // The tab itself is always rendered; clicking it surfaces the Activity
    // header from the history table. Searching by tab role keeps this from
    // being brittle to internal Cloudscape DOM changes.
    expect(
      screen.getByRole("tab", { name: /scan history/i }),
    ).toBeInTheDocument();
  });

  it("displays scan error message when status is SCAN_FAILED", () => {
    setupBaseMocks({ status: "SCAN_FAILED", tablesDiscovered: 0 });
    mockUseGetSourceScanJob.mockReturnValue({
      data: { errorMessage: "Connection timed out" },
    });
    renderDetail();
    expect(screen.getByText("Connection timed out")).toBeInTheDocument();
  });
});
