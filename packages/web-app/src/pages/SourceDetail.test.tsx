// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, act, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SourceDetail } from "./SourceDetail";

// Captures every ReviewSourceTableCommand the page dispatches so we can assert
// that batch review issues one call per selected table with the right input.
const sendMock = vi.hoisted(() => vi.fn());
const invalidateQueriesMock = vi.hoisted(() => vi.fn());
const keepMutateMock = vi.hoisted(() => vi.fn());
const listSourceTablesMock = vi.hoisted(() =>
  vi.fn(() => ({
    data: { items: TABLES, skippedAssets: 0 },
    isLoading: false,
  })),
);

const PENDING_SOURCE = {
  sourceId: "src-1",
  sourceType: "DATABASE",
  status: "PENDING_REVIEW",
  createdAt: "2026-01-01T00:00:00Z",
  databaseDetails: {
    tablesDiscovered: 2,
    tablesApproved: 0,
    metadataEnrichmentEnabled: true,
  },
};

const TABLES = [
  {
    tableId: "sales.orders",
    name: "orders",
    database: "sales",
    reviewStatus: "PENDING_REVIEW",
    columnCount: 3,
    columnsApproved: 0,
  },
  {
    tableId: "sales.customers",
    name: "customers",
    database: "sales",
    reviewStatus: "PENDING_REVIEW",
    columnCount: 2,
    columnsApproved: 0,
  },
];

vi.mock("@api-hooks", () => ({
  useGetSource: () => ({
    data: { body: PENDING_SOURCE },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
  useDeleteSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRescanSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useListSourceTables: listSourceTablesMock,
  useApproveSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRejectSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useGetSourceScanJob: () => ({ data: undefined }),
  useListSourceScanJobs: () => ({ data: undefined }),
  useKeepRescanRemoval: () => ({ mutate: keepMutateMock, isPending: false }),
}));

// ReviewSourceTableCommand is mocked as a simple input-carrying class so the
// send() spy can introspect what was dispatched.
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
    SCAN_FAILED: "SCAN_FAILED",
    APPROVAL_FAILED: "APPROVAL_FAILED",
  },
  ReviewSourceTableCommand: class {
    input: Record<string, unknown>;
    constructor(input: Record<string, unknown>) {
      this.input = input;
    }
  },
}));

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: sendMock }),
}));

vi.mock("@tanstack/react-query", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-query")>();
  return {
    ...actual,
    useQueryClient: () => ({ invalidateQueries: invalidateQueriesMock }),
  };
});

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

const selectAllTables = async () => {
  // Index 0 is the header "select all" checkbox; clicking it selects every row.
  const checkboxes = screen.getAllByRole("checkbox");
  await act(async () => {
    checkboxes[0]!.click();
  });
};

describe("SourceDetail batch review", () => {
  beforeEach(() => {
    sendMock.mockReset();
    sendMock.mockResolvedValue({});
    invalidateQueriesMock.mockReset();
    listSourceTablesMock.mockReturnValue({
      data: { items: TABLES, skippedAssets: 0 },
      isLoading: false,
    });
  });

  it("disables the batch review buttons until tables are selected", () => {
    render(<SourceDetail />, { wrapper });
    expect(
      screen.getByRole("button", { name: /approve selected/i }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /reject selected/i }),
    ).toBeDisabled();
  });

  it("sends a ReviewSourceTableCommand per selected table on approve", async () => {
    render(<SourceDetail />, { wrapper });
    await selectAllTables();

    const approve = screen.getByRole("button", { name: /approve selected/i });
    await waitFor(() => expect(approve).toBeEnabled());
    await act(async () => {
      approve.click();
    });

    // One command per table, each with the APPROVED decision and the table id.
    await waitFor(() => expect(sendMock).toHaveBeenCalledTimes(TABLES.length));
    const dispatchedInputs = sendMock.mock.calls.map((c) => c[0].input);
    expect(dispatchedInputs.map((i) => i.tableId).sort()).toEqual(
      ["sales.customers", "sales.orders"].sort(),
    );
    for (const input of dispatchedInputs) {
      expect(input.decision).toBe("APPROVED");
      expect(input.namespaceId).toBe("ns-1");
      expect(input.sourceId).toBe("src-1");
    }
  });

  it("sends the REJECTED decision when rejecting selected tables", async () => {
    render(<SourceDetail />, { wrapper });
    await selectAllTables();

    const reject = screen.getByRole("button", { name: /reject selected/i });
    await waitFor(() => expect(reject).toBeEnabled());
    await act(async () => {
      reject.click();
    });

    await waitFor(() => expect(sendMock).toHaveBeenCalledTimes(TABLES.length));
    for (const call of sendMock.mock.calls) {
      expect(call[0].input.decision).toBe("REJECTED");
    }
  });

  it("shows a success flash and clears the selection after all reviews succeed", async () => {
    render(<SourceDetail />, { wrapper });
    await selectAllTables();

    const approve = screen.getByRole("button", { name: /approve selected/i });
    await act(async () => {
      approve.click();
    });

    // Success message reports the number of tables approved.
    await waitFor(() =>
      expect(screen.getByText(/2 table\(s\) approved\./i)).toBeInTheDocument(),
    );
    // Selection cleared → the action is disabled again.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /approve selected/i }),
      ).toBeDisabled(),
    );
    // Tables + source queries are invalidated so the UI refreshes.
    expect(invalidateQueriesMock).toHaveBeenCalled();
  });

  it("reports partial failure when some table reviews reject", async () => {
    // First table succeeds, second rejects → partial-failure flash.
    sendMock.mockResolvedValueOnce({}).mockRejectedValueOnce(new Error("boom"));

    render(<SourceDetail />, { wrapper });
    await selectAllTables();

    const approve = screen.getByRole("button", { name: /approve selected/i });
    await act(async () => {
      approve.click();
    });

    await waitFor(() =>
      expect(
        screen.getByText(/1 of 2 table\(s\) approved\. 1 failed\./i),
      ).toBeInTheDocument(),
    );
  });
});

describe("SourceDetail skipped assets warning", () => {
  beforeEach(() => {
    sendMock.mockReset();
    sendMock.mockResolvedValue({});
    invalidateQueriesMock.mockReset();
  });

  it("does not show a warning when skippedAssets is 0", () => {
    listSourceTablesMock.mockReturnValue({
      data: { items: TABLES, skippedAssets: 0 },
      isLoading: false,
    });
    render(<SourceDetail />, { wrapper });
    expect(screen.queryByText(/could not be loaded/i)).not.toBeInTheDocument();
  });

  it("shows a warning when skippedAssets is greater than 0", () => {
    listSourceTablesMock.mockReturnValue({
      data: { items: TABLES, skippedAssets: 3 },
      isLoading: false,
    });
    render(<SourceDetail />, { wrapper });
    expect(
      screen.getByText(/3 tables could not be loaded due to data issues/i),
    ).toBeInTheDocument();
  });

  it("uses singular wording when exactly 1 asset is skipped", () => {
    listSourceTablesMock.mockReturnValue({
      data: { items: TABLES, skippedAssets: 1 },
      isLoading: false,
    });
    render(<SourceDetail />, { wrapper });
    expect(
      screen.getByText(/1 table could not be loaded due to a data issue/i),
    ).toBeInTheDocument();
  });
});

describe("SourceDetail re-scan pending-deletion surfacing", () => {
  beforeEach(() => {
    keepMutateMock.mockReset();
    listSourceTablesMock.mockReturnValue({
      data: {
        items: [
          {
            tableId: "sales.orders",
            name: "orders",
            database: "sales",
            reviewStatus: "PENDING_REVIEW",
            columnCount: 3,
            columnsApproved: 0,
            // The re-scan no longer found this table in the source.
            pendingDeletion: true,
          },
          {
            tableId: "sales.customers",
            name: "customers",
            database: "sales",
            reviewStatus: "PENDING_REVIEW",
            columnCount: 2,
            columnsApproved: 0,
          },
        ],
        skippedAssets: 0,
      },
      isLoading: false,
    });
  });

  it("flags a re-scan-removed table with a Pending deletion badge and a Keep button", () => {
    render(<SourceDetail />, { wrapper });
    // Exactly the flagged table surfaces the badge + Keep control.
    expect(screen.getByText(/pending deletion/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^keep$/i })).toBeInTheDocument();
  });

  it("keeps the flagged table (by id) when Keep is clicked", async () => {
    render(<SourceDetail />, { wrapper });
    const keep = screen.getByRole("button", { name: /^keep$/i });
    await act(async () => {
      keep.click();
    });
    expect(keepMutateMock).toHaveBeenCalledWith(
      { tableId: "sales.orders" },
      expect.anything(),
    );
  });
});

describe("SourceDetail re-scan added-table surfacing", () => {
  it("flags a re-scan-added table with a New badge", () => {
    listSourceTablesMock.mockReturnValue({
      data: {
        items: [
          {
            tableId: "sales.orders",
            name: "orders",
            database: "sales",
            reviewStatus: "PENDING_REVIEW",
            columnCount: 3,
            columnsApproved: 0,
            // The re-scan created this table fresh (net-new since last scan).
            added: true,
          },
        ],
        skippedAssets: 0,
      },
      isLoading: false,
    });
    render(<SourceDetail />, { wrapper });
    expect(screen.getByText(/^new$/i)).toBeInTheDocument();
  });

  it("shows no New badge when no table is added", () => {
    listSourceTablesMock.mockReturnValue({
      data: { items: TABLES, skippedAssets: 0 },
      isLoading: false,
    });
    render(<SourceDetail />, { wrapper });
    expect(screen.queryByText(/^new$/i)).not.toBeInTheDocument();
  });
});

describe("SourceDetail column-level pending-deletion surfacing", () => {
  beforeEach(() => {
    listSourceTablesMock.mockReturnValue({
      data: {
        items: [
          {
            tableId: "sales.orders",
            name: "orders",
            database: "sales",
            reviewStatus: "PENDING_REVIEW",
            // Honest denominator: 3 merged columns (one retained pending
            // deletion), 1 approved.
            columnCount: 3,
            columnsApproved: 1,
            columnsPendingDeletion: 1,
          },
        ],
        skippedAssets: 0,
      },
      isLoading: false,
    });
  });

  it("surfaces a column-level pending-deletion badge in the Review status column", () => {
    render(<SourceDetail />, { wrapper });
    expect(screen.getByText(/1 column pending deletion/i)).toBeInTheDocument();
  });

  it("shows the honest X/Y denominator in the Columns cell", () => {
    render(<SourceDetail />, { wrapper });
    // Denominator counts the retained pending-deletion column → 1/3, not 1/2.
    expect(screen.getByText("1/3")).toBeInTheDocument();
  });
});
