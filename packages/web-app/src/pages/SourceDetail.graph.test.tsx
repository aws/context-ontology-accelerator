// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { RuntimeConfigContext } from "@components/RuntimeContext";
import type { RuntimeContext } from "@components/RuntimeContext";
import { SourceDetail } from "./SourceDetail";

const mockGetSource = vi.hoisted(() => vi.fn());
const mockGetSourceScanJob = vi.hoisted(() => vi.fn());
const mockListScanJobs = vi.hoisted(() => vi.fn());
const mockListTables = vi.hoisted(() => vi.fn());
const mockRescan = vi.hoisted(() => vi.fn());

vi.mock("@api-hooks", () => ({
  useGetSource: mockGetSource,
  useDeleteSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRescanSource: () => ({
    mutate: mockRescan,
    isPending: false,
    error: null,
  }),
  useListSourceTables: mockListTables,
  useListSourceScanJobs: mockListScanJobs,
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
    REGISTERED: "REGISTERED",
    RESCAN_REVIEW: "RESCAN_REVIEW",
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

const BASE_RUNTIME_CONFIG: RuntimeContext = {
  region: "us-east-1",
  authority: "https://issuer.example.com",
  clientId: "client-abc",
  oidcConfig: {
    authority: "https://issuer.example.com",
    clientId: "client-abc",
  },
};

function wrapperWithRuntimeConfig(ctx: RuntimeContext | undefined) {
  return ({ children }: { children: React.ReactNode }) => (
    <RuntimeConfigContext.Provider value={ctx}>
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
    </RuntimeConfigContext.Provider>
  );
}

// The graph-source views below are the switch-on deployment.
const wrapper = wrapperWithRuntimeConfig({
  ...BASE_RUNTIME_CONFIG,
  graphSourcesEnabled: true,
});

const user = userEvent.setup({ delay: null });

const ENDPOINT = "db.cluster-abc.us-east-1.neptune.amazonaws.com";

function setGraphSource(overrides: Record<string, unknown> = {}) {
  mockGetSource.mockReturnValue({
    data: {
      body: {
        sourceId: "src-1",
        name: "support-graph",
        sourceType: "DATABASE",
        sourceSubType: "RDF_SPARQL",
        status: "APPROVED",
        createdAt: "2026-01-01T00:00:00Z",
        databaseDetails: {
          lastScanAt: "2026-01-02T00:00:00Z",
          lastScanJobId: "2026-01-02T00:00:00.000000Z",
          sparqlConfiguration: {
            store: "neptune",
            dataEndpoint: ENDPOINT,
            region: "us-east-1",
            dataNamedGraph: "https://coa.example.com/graphs/support",
            authMode: "sigv4",
          },
        },
        ...overrides,
      },
    },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  });
}

describe("SourceDetail — RDF_SPARQL graph source", () => {
  beforeEach(() => {
    mockGetSource.mockReset();
    mockRescan.mockReset();
    mockGetSourceScanJob.mockReset();
    mockGetSourceScanJob.mockReturnValue({ data: undefined });
    mockListTables.mockReset();
    mockListTables.mockReturnValue({ data: { items: [] }, isLoading: false });
    mockListScanJobs.mockReset();
    mockListScanJobs.mockReturnValue({ data: { items: [] }, isLoading: false });
  });

  it("labels the sub-type and shows the endpoint instead of table counts", () => {
    setGraphSource();
    render(<SourceDetail />, { wrapper });
    expect(screen.getByText("Graph database (SPARQL)")).toBeInTheDocument();
    expect(screen.getAllByText(ENDPOINT).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("Tables discovered")).not.toBeInTheDocument();
    expect(screen.queryByText("Metadata enrichment")).not.toBeInTheDocument();
    expect(screen.getByText("Last verified")).toBeInTheDocument();
  });

  it("has no Tables tab and never lists tables", () => {
    setGraphSource();
    render(<SourceDetail />, { wrapper });
    expect(
      screen.queryByRole("tab", { name: /tables/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: /verification history/i }),
    ).toBeInTheDocument();
    // The tables hook is called with an empty source id, i.e. disabled.
    expect(mockListTables.mock.calls.every((c) => c[1] === "")).toBe(true);
  });

  it("offers Re-verify instead of Re-scan, and no approve/reject", async () => {
    setGraphSource();
    render(<SourceDetail />, { wrapper });
    expect(
      screen.queryByRole("button", { name: /^re-scan$/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /approve source/i }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^re-verify$/i }));
    expect(mockRescan).toHaveBeenCalledTimes(1);
  });

  it("disables Re-verify while the check is running", () => {
    setGraphSource({ status: "REGISTERED" });
    render(<SourceDetail />, { wrapper });
    expect(screen.getByRole("button", { name: /^re-verify$/i })).toBeDisabled();
  });

  it("explains the ontology import once the endpoint is verified", () => {
    setGraphSource();
    render(<SourceDetail />, { wrapper });
    expect(screen.getByText("Endpoint verified")).toBeInTheDocument();
    expect(screen.getByText(/coa_import:dataSourceName/)).toBeInTheDocument();
  });

  it("shows why the reachability check failed", () => {
    setGraphSource({ status: "SCAN_FAILED" });
    mockGetSourceScanJob.mockReturnValue({
      data: {
        status: "FAILED",
        errorMessage: "Endpoint is unreachable from the service VPC",
      },
    });
    render(<SourceDetail />, { wrapper });
    expect(screen.getByText("Reachability check failed")).toBeInTheDocument();
    expect(
      screen.getAllByText(/unreachable from the service VPC/).length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("renders the connection settings without scan schedule controls", async () => {
    setGraphSource();
    render(<SourceDetail />, { wrapper });
    await user.click(screen.getByRole("tab", { name: /settings/i }));
    expect(screen.getByText("Amazon Neptune")).toBeInTheDocument();
    expect(
      screen.getAllByText("https://coa.example.com/graphs/support").length,
    ).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("SigV4 / IAM")).toBeInTheDocument();
    expect(screen.getByText("8182")).toBeInTheDocument();
    expect(screen.queryByText(/re-scan schedule/i)).not.toBeInTheDocument();
  });

  it("labels reachability rows in the verification history", async () => {
    setGraphSource();
    mockListScanJobs.mockReturnValue({
      data: {
        items: [
          {
            at: "2026-01-02T00:00:00Z",
            eventType: "SCAN",
            status: "FAILED",
            scanType: "reachability",
            errorMessage: "Access denied calling the endpoint",
          },
          {
            at: "2026-01-03T00:00:00Z",
            eventType: "SCAN",
            status: "COMPLETED",
            scanType: "reachability",
          },
        ],
      },
      isLoading: false,
    });
    render(<SourceDetail />, { wrapper });
    await user.click(
      screen.getByRole("tab", { name: /verification history/i }),
    );
    expect(
      screen.getAllByText("Reachability check failed").length,
    ).toBeGreaterThanOrEqual(1);
    expect(
      screen.getByText(/Access denied calling the endpoint/),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText("Endpoint verified").length,
    ).toBeGreaterThanOrEqual(1);
  });
});

describe("SourceDetail — graph sources switch", () => {
  beforeEach(() => {
    mockGetSource.mockReset();
    mockGetSourceScanJob.mockReset();
    mockGetSourceScanJob.mockReturnValue({ data: undefined });
    mockListTables.mockReset();
    mockListTables.mockReturnValue({ data: { items: [] }, isLoading: false });
    mockListScanJobs.mockReset();
    mockListScanJobs.mockReturnValue({ data: { items: [] }, isLoading: false });
  });

  it.each([
    ["no runtime config", undefined],
    [
      "graphSourcesEnabled false",
      { ...BASE_RUNTIME_CONFIG, graphSourcesEnabled: false },
    ],
  ])(
    "with %s, hides Re-verify but keeps Delete and the read-only view",
    (_label, ctx) => {
      setGraphSource();
      render(<SourceDetail />, { wrapper: wrapperWithRuntimeConfig(ctx) });
      expect(
        screen.queryByRole("button", { name: /^re-verify$/i }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /^re-scan$/i }),
      ).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /^delete$/i }),
      ).toBeInTheDocument();
      expect(screen.getAllByText(ENDPOINT).length).toBeGreaterThanOrEqual(1);
    },
  );

  it("says a verified graph source answers nothing while graph sources are off", () => {
    setGraphSource();
    render(<SourceDetail />, { wrapper: wrapperWithRuntimeConfig(undefined) });
    expect(
      screen.getByText(/Graph sources are turned off in this deployment/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Questions are answered from it/),
    ).not.toBeInTheDocument();
  });

  it("keeps the endpoint-verified note when graph sources are on", () => {
    setGraphSource();
    render(<SourceDetail />, { wrapper });
    expect(
      screen.getByText(/Questions are answered from it/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Graph sources are turned off in this deployment/),
    ).not.toBeInTheDocument();
  });

  it("enables Delete for a graph source still waiting for its verify while graph sources are off", () => {
    setGraphSource({ status: "REGISTERED" });
    render(<SourceDetail />, { wrapper: wrapperWithRuntimeConfig(undefined) });
    expect(screen.getByRole("button", { name: /^delete$/i })).toBeEnabled();
  });

  it("keeps Delete disabled for a graph source waiting for its verify while graph sources are on", () => {
    setGraphSource({ status: "REGISTERED" });
    render(<SourceDetail />, { wrapper });
    expect(screen.getByRole("button", { name: /^delete$/i })).toBeDisabled();
  });

  const minutesAgo = (m: number) =>
    new Date(Date.now() - m * 60_000).toISOString();

  it("offers Re-verify for a graph source left waiting for more than 15 minutes while graph sources are on", async () => {
    mockRescan.mockReset();
    setGraphSource({ status: "REGISTERED", updatedAt: minutesAgo(60) });
    render(<SourceDetail />, { wrapper });
    const reverify = screen.getByRole("button", { name: /^re-verify$/i });
    expect(reverify).toBeEnabled();
    await user.click(reverify);
    expect(mockRescan).toHaveBeenCalledTimes(1);
  });

  it("keeps Re-verify disabled for a graph source registered a few minutes ago", () => {
    setGraphSource({ status: "REGISTERED", updatedAt: minutesAgo(5) });
    render(<SourceDetail />, { wrapper });
    expect(screen.getByRole("button", { name: /^re-verify$/i })).toBeDisabled();
  });

  it("keeps Delete disabled for a graph source left waiting while graph sources are on, as the API does", () => {
    setGraphSource({ status: "REGISTERED", updatedAt: minutesAgo(60) });
    render(<SourceDetail />, { wrapper });
    expect(screen.getByRole("button", { name: /^delete$/i })).toBeDisabled();
  });

  it("shows Re-verify when graphSourcesEnabled is true", () => {
    setGraphSource();
    render(<SourceDetail />, { wrapper });
    expect(
      screen.getByRole("button", { name: /^re-verify$/i }),
    ).toBeInTheDocument();
  });
});
