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
  useKeepRescanRemoval: () => ({ mutate: vi.fn(), isPending: false }),
  // The settings panel this test renders also hosts the rescan controls.
  usePutSourceRescanSchedule: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: null,
    isSuccess: false,
  }),
  usePutSourceEventRescan: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: null,
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

const DATABRICKS_CONFIG = {
  workspaceHostname: "dbc-a1b2345c-d6e7.cloud.databricks.com",
  httpPath: "/sql/1.0/warehouses/abc123def456",
  databricksCatalog: "main",
  databaseName: "sales",
  credentialSecretArn:
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:databricks-abc123",
  crossAccountRoleArn:
    "arn:aws:iam::222222222222:role/coa-dev-datasource-access-databricks",
  tableFilter: "orders|customers",
  tableExcludeFilter: "tmp_*",
};

function setSource(overrides: Record<string, unknown> = {}) {
  mockGetSource.mockReturnValue({
    data: {
      body: {
        sourceId: "src-1",
        name: "Test Warehouse",
        sourceType: "DATABASE",
        sourceSubType: "DATABRICKS_SQL_WAREHOUSE",
        status: "PENDING_REVIEW",
        createdAt: "2026-01-01T00:00:00Z",
        databaseDetails: {
          tablesDiscovered: 5,
          tablesApproved: 0,
          metadataEnrichmentEnabled: true,
          lastScanAt: "2026-01-02T00:00:00Z",
          lastScanJobId: "2026-01-02T00:00:00Z",
          databricksSqlWarehouseConfiguration: DATABRICKS_CONFIG,
        },
        ...overrides,
      },
    },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  });
}

describe("SourceDetail — DATABRICKS_SQL_WAREHOUSE sub-type", () => {
  beforeEach(() => {
    mockGetSource.mockReset();
    mockGetSourceScanJob.mockReset();
    mockGetSourceScanJob.mockReturnValue({ data: undefined });
  });

  it("labels the sub-type by its own name rather than as another sub-type", () => {
    setSource();
    render(<SourceDetail />, { wrapper });

    expect(screen.getByText("Databricks SQL Warehouse")).toBeInTheDocument();
    expect(screen.queryByText("JDBC database")).not.toBeInTheDocument();
    expect(screen.queryByText("Custom connector")).not.toBeInTheDocument();
  });

  it("renders every configured member in the settings panel", async () => {
    setSource();
    render(<SourceDetail />, { wrapper });

    await user.click(screen.getByRole("tab", { name: /settings/i }));

    expect(
      screen.getByText(DATABRICKS_CONFIG.workspaceHostname),
    ).toBeInTheDocument();
    expect(screen.getByText(DATABRICKS_CONFIG.httpPath)).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();
    expect(screen.getByText("sales")).toBeInTheDocument();
    expect(
      screen.getByText(DATABRICKS_CONFIG.credentialSecretArn),
    ).toBeInTheDocument();
    expect(
      screen.getByText(DATABRICKS_CONFIG.crossAccountRoleArn),
    ).toBeInTheDocument();
    expect(screen.getByText("orders|customers")).toBeInTheDocument();
    expect(screen.getByText("tmp_*")).toBeInTheDocument();
  });

  it("presents the secret ARN as a pointer read through the role, not as a standalone fact", async () => {
    setSource();
    render(<SourceDetail />, { wrapper });

    await user.click(screen.getByRole("tab", { name: /settings/i }));

    // The credential is read only as a session assumed from the role, so neither
    // ARN means anything on its own.
    expect(screen.getByText(/assumed to read the secret/i)).toBeInTheDocument();
    expect(screen.getByText(/read through that role/i)).toBeInTheDocument();
  });

  it("omits an unset optional filter rather than showing a blank row", async () => {
    setSource({
      databaseDetails: {
        tablesDiscovered: 5,
        tablesApproved: 0,
        databricksSqlWarehouseConfiguration: {
          ...DATABRICKS_CONFIG,
          tableFilter: undefined,
          tableExcludeFilter: undefined,
        },
      },
    });
    render(<SourceDetail />, { wrapper });

    await user.click(screen.getByRole("tab", { name: /settings/i }));

    expect(screen.getAllByText("(none)").length).toBe(2);
  });

  it("warns that metadata is incomplete when the scan reports failed tables", () => {
    // Unity Catalog privilege-filters results, so a listed-but-unreadable table is
    // the expected shape of a scan under an under-granted credential.
    setSource();
    mockGetSourceScanJob.mockReturnValue({
      data: {
        status: "COMPLETED",
        tablesDiscovered: 5,
        tablesFailed: 2,
        failedTables: ["sales.orders", "sales.customers"],
      },
    });
    render(<SourceDetail />, { wrapper });

    expect(
      screen.getByText(/Scan completed, but metadata is incomplete/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText("sales.orders, sales.customers"),
    ).toBeInTheDocument();
  });
});
