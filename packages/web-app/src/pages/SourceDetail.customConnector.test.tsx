// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
  // Re-scan review hooks the page calls unconditionally. Not exercised by these
  // Athena/degraded-scan cases, but the mock must expose every hook the
  // component imports or the render throws before reaching the assertion.
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

const user = userEvent.setup({ delay: null });

const CUSTOM_CONNECTOR_CONFIG = {
  connectorFunctionArn:
    "arn:aws:lambda:us-east-1:123456789012:function:my-connector",
  databaseName: "my_connector_db",
  tableFilter: "orders|customers",
};

function setSource(overrides: Record<string, unknown> = {}) {
  mockGetSource.mockReturnValue({
    data: {
      body: {
        sourceId: "src-1",
        name: "Test Connector",
        sourceType: "DATABASE",
        sourceSubType: "CUSTOM_CONNECTOR",
        status: "PENDING_REVIEW",
        createdAt: "2026-01-01T00:00:00Z",
        databaseDetails: {
          tablesDiscovered: 5,
          tablesApproved: 0,
          metadataEnrichmentEnabled: true,
          lastScanAt: "2026-01-02T00:00:00Z",
          lastScanJobId: "2026-01-02T00:00:00Z",
          customConnectorConfiguration: CUSTOM_CONNECTOR_CONFIG,
        },
        ...overrides,
      },
    },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  });
}

describe("SourceDetail — CUSTOM_CONNECTOR sub-type", () => {
  beforeEach(() => {
    mockGetSource.mockReset();
    mockGetSourceScanJob.mockReset();
    mockGetSourceScanJob.mockReturnValue({ data: undefined });
  });

  it("labels the sub-type as a custom connector rather than a JDBC database", () => {
    setSource();
    render(<SourceDetail />, { wrapper });

    expect(screen.getByText("Custom connector")).toBeInTheDocument();
    expect(screen.queryByText("JDBC database")).not.toBeInTheDocument();
  });

  it("renders the connector settings panel instead of an empty container", async () => {
    setSource();
    render(<SourceDetail />, { wrapper });

    await user.click(screen.getByRole("tab", { name: /settings/i }));

    expect(screen.getByText("Connector function ARN")).toBeInTheDocument();
    expect(
      screen.getByText(CUSTOM_CONNECTOR_CONFIG.connectorFunctionArn),
    ).toBeInTheDocument();
    expect(screen.getByText("my_connector_db")).toBeInTheDocument();
    expect(screen.getByText("orders|customers")).toBeInTheDocument();
  });

  it("shows exactly one connector ARN, with no metadata/record split", async () => {
    setSource();
    render(<SourceDetail />, { wrapper });

    await user.click(screen.getByRole("tab", { name: /settings/i }));

    // A connector is one Lambda serving both paths. Athena supports a split
    // metadata/record pair and CustomConnectorConfiguration deliberately does not model it,
    // so no row here may imply the customer chose between them.
    expect(screen.queryByText("Metadata function ARN")).not.toBeInTheDocument();
    expect(screen.queryByText("Record function ARN")).not.toBeInTheDocument();
    expect(screen.queryByText(/composite handler/i)).not.toBeInTheDocument();
  });
});

// Discovery for this sub-type reads each table with its own DESCRIBE, so a scan
// can succeed while individual tables are lost. The steward must be told before
// reviewing metadata that is silently incomplete.
describe("SourceDetail — degraded scan annotation", () => {
  beforeEach(() => {
    mockGetSource.mockReset();
    mockGetSourceScanJob.mockReset();
    mockGetSourceScanJob.mockReturnValue({ data: undefined });
  });

  it("warns that metadata is incomplete when the scan reports failed tables", () => {
    setSource();
    mockGetSourceScanJob.mockReturnValue({
      data: {
        status: "COMPLETED",
        tablesDiscovered: 5,
        tablesFailed: 2,
        failedTables: ["my_connector_db.orders", "my_connector_db.customers"],
      },
    });
    render(<SourceDetail />, { wrapper });

    expect(
      screen.getByText(/Scan completed, but metadata is incomplete/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/could not read 2 of the tables/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText("my_connector_db.orders, my_connector_db.customers"),
    ).toBeInTheDocument();
  });

  it("says how many of the failed tables are listed when the sample is capped", () => {
    setSource();
    mockGetSourceScanJob.mockReturnValue({
      data: { tablesFailed: 40, failedTables: ["db.a", "db.b"] },
    });
    render(<SourceDetail />, { wrapper });

    // The list is a capped diagnostic sample — the count must come from
    // tablesFailed, never from the list's length.
    expect(
      screen.getByText(/could not read 40 of the tables/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/2 of 40 shown/i)).toBeInTheDocument();
  });

  it("uses the singular form for a single failed table", () => {
    setSource();
    mockGetSourceScanJob.mockReturnValue({
      data: { tablesFailed: 1, failedTables: ["db.orders"] },
    });
    render(<SourceDetail />, { wrapper });

    expect(
      screen.getByText(/could not read 1 of the tables/i),
    ).toBeInTheDocument();
  });

  it("stays silent when the scan reported no failed tables", () => {
    setSource();
    mockGetSourceScanJob.mockReturnValue({
      data: { status: "COMPLETED", tablesDiscovered: 5 },
    });
    render(<SourceDetail />, { wrapper });

    expect(
      screen.queryByText(/metadata is incomplete/i),
    ).not.toBeInTheDocument();
  });

  it("ignores a malformed tablesFailed value", () => {
    setSource();
    mockGetSourceScanJob.mockReturnValue({
      data: { tablesFailed: "lots", failedTables: "nope" },
    });
    render(<SourceDetail />, { wrapper });

    expect(
      screen.queryByText(/metadata is incomplete/i),
    ).not.toBeInTheDocument();
  });

  it("flags the completed scan in the activity log instead of a clean success", async () => {
    setSource();
    mockGetSourceScanJob.mockReturnValue({
      data: { tablesFailed: 2, failedTables: ["db.a", "db.b"] },
    });
    render(<SourceDetail />, { wrapper });

    await user.click(screen.getByRole("tab", { name: /scan history/i }));

    expect(
      screen.getByText(
        /Scanned 5 tables\. 2 tables could not be read and were skipped\./i,
      ),
    ).toBeInTheDocument();
  });
});
