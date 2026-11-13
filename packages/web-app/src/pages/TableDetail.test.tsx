// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { TableDetail } from "./TableDetail";

const mockUseGetSource = vi.fn();
const mockUseGetSourceTable = vi.fn();
const mockUpdateKeysMutate = vi.fn();
const mockUpdateTableMetaMutate = vi.fn();

vi.mock("@api-hooks", () => ({
  useGetSourceTable: () => mockUseGetSourceTable(),
  useGetSource: () => mockUseGetSource(),
  useReviewSourceTable: () => ({ mutate: vi.fn(), isPending: false }),
  useReviewSourceColumn: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateSourceTableMetadata: () => ({
    mutate: mockUpdateTableMetaMutate,
    isPending: false,
  }),
  useUpdateSourceColumnMetadata: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateSourceTableKeys: () => ({
    mutate: mockUpdateKeysMutate,
    isPending: false,
    reset: vi.fn(),
    error: null,
  }),
  useKeepRescanRemoval: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@coa/control-plane-client", () => ({
  ReviewStatus: {
    APPROVED: "APPROVED",
    PENDING_REVIEW: "PENDING_REVIEW",
    REJECTED: "REJECTED",
  },
  ReviewDecision: { APPROVED: "APPROVED", REJECTED: "REJECTED" },
}));

const baseTableData = {
  tableId: "sales.orders",
  name: "orders",
  database: "sales",
  reviewStatus: "PENDING_REVIEW",
  businessMetadata: {
    description: "Transactional order records (S3-backed CSV)",
    enrichmentSource: "DETERMINISTIC",
    tags: ["transactional", "s3-backed"],
  },
  primaryKey: {
    columns: ["order_id"],
    source: "DETERMINISTIC",
    confidence: 0,
  },
  foreignKeys: [
    {
      column: "customer_id",
      targetTable: "customers",
      targetColumn: "id",
      source: "AI_INFERRED",
      confidence: 0.92,
    },
  ],
  columns: [
    {
      name: "order_id",
      dataType: "bigint",
      nullable: false,
      businessMetadata: {
        description: "Order id",
        enrichmentSource: "AI_GENERATED",
        confidence: 0.77,
      },
    },
    {
      name: "status",
      dataType: "varchar",
      nullable: false,
      businessMetadata: {
        description: "Order status, curated by data steward",
        enrichmentSource: "STEWARD_EDITED",
        tags: ["lifecycle"],
      },
    },
  ],
  technicalMetadata: {},
};

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter
    initialEntries={["/namespaces/ns-1/sources/src-1/tables/sales.orders"]}
  >
    <Routes>
      <Route
        path="/namespaces/:namespaceId/sources/:dataSourceId/tables/:tableId"
        element={children}
      />
    </Routes>
  </MemoryRouter>
);

beforeEach(() => {
  mockUpdateKeysMutate.mockReset();
  mockUpdateTableMetaMutate.mockReset();
  mockUseGetSourceTable.mockReset();
  mockUseGetSourceTable.mockReturnValue({
    data: baseTableData,
    isLoading: false,
    error: null,
  });
  mockUseGetSource.mockReset();
  mockUseGetSource.mockReturnValue({
    data: { body: { databaseDetails: { metadataEnrichmentEnabled: true } } },
    isLoading: false,
    error: null,
  });
});

describe("TableDetail keys & relationships", () => {
  it("renders the primary key columns and source", () => {
    render(<TableDetail />, { wrapper });
    expect(screen.getByText("Keys & relationships")).toBeInTheDocument();
    expect(screen.getAllByText("order_id").length).toBeGreaterThan(0);
    expect(screen.getByText("DETERMINISTIC")).toBeInTheDocument();
  });

  it("renders foreign keys with their reference and confidence score", () => {
    render(<TableDetail />, { wrapper });
    expect(screen.getByText("customer_id")).toBeInTheDocument();
    expect(screen.getByText("customers.id")).toBeInTheDocument();
    expect(screen.getByText("AI_INFERRED")).toBeInTheDocument();
    expect(screen.getByText("92%")).toBeInTheDocument();
  });

  it("shows confidence for AI-generated column metadata", () => {
    render(<TableDetail />, { wrapper });
    expect(screen.getByText("77%")).toBeInTheDocument();
  });

  it("opens the edit keys modal", () => {
    render(<TableDetail />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /edit keys/i }));
    expect(screen.getByText("Primary key columns")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /add foreign key/i }),
    ).toBeInTheDocument();
  });

  it("surfaces a pending cross-source relationship and approves it", () => {
    mockUseGetSourceTable.mockReturnValue({
      data: {
        ...baseTableData,
        foreignKeys: [
          {
            column: "customer_id",
            targetTable: "customers",
            targetColumn: "id",
            source: "AI_INFERRED",
            confidence: 0.9,
            reviewStatus: "PENDING_REVIEW",
            targetDatasourceId: "DS#crm",
            provenance: "maps a customer via account_xref",
          },
        ],
      },
      isLoading: false,
      error: null,
    });
    render(<TableDetail />, { wrapper });

    expect(screen.getByText("PENDING_REVIEW")).toBeInTheDocument();
    expect(screen.getByText("cross-source")).toBeInTheDocument();
    expect(
      screen.getByText("maps a customer via account_xref"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(mockUpdateKeysMutate).toHaveBeenCalledTimes(1);
    const [payload] = mockUpdateKeysMutate.mock.calls[0];
    const fk = payload.foreignKeys.find(
      (f: { column: string }) => f.column === "customer_id",
    );
    expect(fk.reviewStatus).toBe("APPROVED");
  });

  it("keeps a long inference note to one line with the full text on demand", () => {
    const long =
      "entitlement.tenant_id description states it maps to a customer via account_xref; " +
      "mart_customer_360 is the customer master keyed by ba_no, making it the target for resolution.";
    mockUseGetSourceTable.mockReturnValue({
      data: {
        ...baseTableData,
        foreignKeys: [
          {
            column: "customer_id",
            targetTable: "customers",
            targetColumn: "id",
            source: "AI_INFERRED",
            confidence: 0.7,
            reviewStatus: "PENDING_REVIEW",
            targetDatasourceId: "DS#crm",
            provenance: long,
          },
        ],
      },
      isLoading: false,
      error: null,
    });
    render(<TableDetail />, { wrapper });

    expect(screen.queryByText(long)).not.toBeInTheDocument();
    const clipped = screen.getByText(
      /^entitlement\.tenant_id description states.*…$/,
    );
    expect(clipped.textContent!.length).toBeLessThan(long.length);

    fireEvent.click(clipped);
    expect(screen.getByText(long)).toBeInTheDocument();
  });
});

// enrichmentSource describes only the description; tags may still be AI-generated.
describe("TableDetail enrichment source provenance", () => {
  it("labels the field as 'Description source', not 'Enrichment source'", () => {
    render(<TableDetail />, { wrapper });
    expect(screen.getAllByText("Description source").length).toBeGreaterThan(0);
    expect(screen.queryByText("Enrichment source")).not.toBeInTheDocument();
  });

  it("renders a human-readable provenance label instead of the raw enum", () => {
    render(<TableDetail />, { wrapper });
    expect(screen.getByText("Source catalog")).toBeInTheDocument();
    expect(screen.getByText("Steward-edited")).toBeInTheDocument();
  });

  it("shows an AI-enriched hint icon next to the description source badge when the description is DETERMINISTIC but tags are present", () => {
    render(<TableDetail />, { wrapper });
    // Table-level summary + the "status" column row both qualify — assert at least one hint renders.
    expect(
      screen.getAllByRole("img", { name: "AI-enriched hint" }).length,
    ).toBeGreaterThan(0);
    // Must not render as a plain Badge indistinguishable from real tags.
    expect(screen.queryByText("AI-enriched")).not.toBeInTheDocument();
  });

  it("shows the hint next to the steward-edited column's description source badge, not inside the Tags cell or the name cell", () => {
    render(<TableDetail />, { wrapper });
    const statusRow = screen.getByText("status").closest("tr");
    expect(statusRow).not.toBeNull();
    if (!statusRow) return;

    const hintIcon = statusRow.querySelector(
      '[role="img"][aria-label="AI-enriched hint"]',
    );
    expect(hintIcon).not.toBeNull();

    // The hint must sit in the same cell as the "Steward-edited"
    // description-source badge — not the row's name cell (isRowHeader) or
    // the Tags cell.
    const hintCell = hintIcon?.closest("td, th");
    expect(hintCell?.textContent).toContain("Steward-edited");

    const rowHeaderCell = statusRow.querySelector("th");
    expect(rowHeaderCell?.textContent).toBe("status");
    expect(
      rowHeaderCell?.querySelector(
        '[role="img"][aria-label="AI-enriched hint"]',
      ),
    ).toBeNull();
  });
});

// With enrichment disabled the scan never runs it, so no hint.
describe("TableDetail AI-enriched hint respects metadataEnrichmentEnabled", () => {
  it("suppresses the hint when the source has metadata enrichment disabled", () => {
    mockUseGetSource.mockReturnValue({
      data: {
        body: { databaseDetails: { metadataEnrichmentEnabled: false } },
      },
      isLoading: false,
      error: null,
    });
    render(<TableDetail />, { wrapper });
    expect(
      screen.queryByRole("img", { name: "AI-enriched hint" }),
    ).not.toBeInTheDocument();
  });

  it("defaults to showing the hint when metadataEnrichmentEnabled is undefined (legacy sources)", () => {
    mockUseGetSource.mockReturnValue({
      data: { body: { databaseDetails: {} } },
      isLoading: false,
      error: null,
    });
    render(<TableDetail />, { wrapper });
    expect(
      screen.getAllByRole("img", { name: "AI-enriched hint" }).length,
    ).toBeGreaterThan(0);
  });
});

// rescanDiff is present only while the source is in RESCAN_REVIEW.
describe("TableDetail re-scan diff (old vs new)", () => {
  it("renders the changes panel (table-level and column groups) with old → new detail and a modified-column badge on the row", () => {
    mockUseGetSourceTable.mockReturnValue({
      data: {
        ...baseTableData,
        rescanDiff: {
          tableFields: [
            {
              field: "description",
              kind: "DESCRIPTION",
              old: "Old order records",
              new: "New order records",
            },
          ],
          columns: [
            {
              name: "status",
              status: "modified",
              fields: [
                {
                  field: "data_type",
                  kind: "SCHEMA",
                  old: "varchar",
                  new: "text",
                },
              ],
            },
          ],
        },
      },
      isLoading: false,
      error: null,
    });
    render(<TableDetail />, { wrapper });

    // Expand the foldable section to read the detail.
    expect(
      screen.getByText("Changes since last approved scan"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByText("1 table-level change, 1 column change"));
    expect(screen.getByText("Table-level changes")).toBeInTheDocument();
    expect(screen.getByText("Column changes")).toBeInTheDocument();

    // Field rows concatenate several text nodes, so match by substring.
    expect(screen.getByText(/Old order records/)).toBeInTheDocument();
    expect(screen.getByText(/New order records/)).toBeInTheDocument();
    // "data_type" is unique to the diff row; "varchar" also renders in the table.
    expect(screen.getByText(/data_type/)).toBeInTheDocument();

    // "status" and "Changed" appear in the panel and on the row; scope to the row.
    const statusRow =
      screen
        .getAllByText("status")
        .map((el) => el.closest("tr"))
        .find((tr) => tr !== null) ?? null;
    expect(statusRow).not.toBeNull();
    expect(statusRow?.textContent).toContain("Changed");
  });

  it("does not render the changes panel when rescanDiff is absent", () => {
    render(<TableDetail />, { wrapper });
    expect(
      screen.queryByText("Changes since last approved scan"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Changed")).not.toBeInTheDocument();
  });

  it("shows a 'New table' note for a re-scan-added table (added, no diff)", () => {
    mockUseGetSourceTable.mockReturnValue({
      data: { ...baseTableData, added: true },
      isLoading: false,
      error: null,
    });
    render(<TableDetail />, { wrapper });
    expect(screen.getByText("New table")).toBeInTheDocument();
    expect(
      screen.getByText(/discovered during the latest re-scan/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Changes since last approved scan"),
    ).not.toBeInTheDocument();
  });

  it("shows a 'Dropped table' note for a re-scan-dropped table (pendingDeletion)", () => {
    mockUseGetSourceTable.mockReturnValue({
      data: { ...baseTableData, pendingDeletion: true },
      isLoading: false,
      error: null,
    });
    render(<TableDetail />, { wrapper });
    expect(screen.getByText("Dropped table")).toBeInTheDocument();
    expect(
      screen.getByText(/not found in the source during the latest re-scan/),
    ).toBeInTheDocument();
  });

  it("shows only Pending deletion (not a duplicate Removed badge) for a re-scan-dropped column", () => {
    mockUseGetSourceTable.mockReturnValue({
      data: {
        ...baseTableData,
        columns: [
          baseTableData.columns[0],
          { ...baseTableData.columns[1], pendingDeletion: true },
        ],
        rescanDiff: {
          tableFields: [],
          columns: [{ name: "status", status: "removed" }],
        },
      },
      isLoading: false,
      error: null,
    });
    render(<TableDetail />, { wrapper });

    // "status" also appears in the panel; scope to the row.
    const statusRow =
      screen
        .getAllByText("status")
        .map((el) => el.closest("tr"))
        .find((tr) => tr !== null) ?? null;
    expect(statusRow).not.toBeNull();
    expect(statusRow?.textContent).toContain("Pending deletion");
    expect(statusRow?.textContent).toContain("Keep");
    expect(statusRow?.textContent).not.toContain("Removed");
  });
});

describe("TableDetail synonyms editor", () => {
  it("pre-fills stored synonyms, 'Clear all' empties them, and Save sends an empty list", () => {
    mockUseGetSourceTable.mockReturnValue({
      data: {
        ...baseTableData,
        businessMetadata: {
          description: "AI: order records",
          enrichmentSource: "AI_GENERATED",
          synonyms: ["orders_table", "order_dimension"],
        },
      },
      isLoading: false,
      error: null,
    });
    render(<TableDetail />, { wrapper });
    // The table-level "Edit" is the first one; each column row has its own.
    fireEvent.click(screen.getAllByRole("button", { name: /^edit$/i })[0]);
    expect(screen.getByText("Edit table: orders")).toBeInTheDocument();

    expect(screen.getByText("orders_table")).toBeInTheDocument();
    expect(screen.getByText("order_dimension")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: /clear all synonyms/i }),
    );
    expect(screen.queryByText("orders_table")).not.toBeInTheDocument();
    expect(screen.queryByText("order_dimension")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /clear all synonyms/i }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(mockUpdateTableMetaMutate).toHaveBeenCalledTimes(1);
    const [payload] = mockUpdateTableMetaMutate.mock.calls[0];
    expect(payload.overrides.synonyms).toEqual([]);
    expect(payload.overrides.description).toBe("AI: order records");
  });

  it("does not offer 'Clear all' when there are no synonyms", () => {
    render(<TableDetail />, { wrapper });
    fireEvent.click(screen.getAllByRole("button", { name: /^edit$/i })[0]);
    expect(screen.getByText("Edit table: orders")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /clear all synonyms/i }),
    ).not.toBeInTheDocument();
  });

  it("moves keyboard focus to the synonyms input when 'Clear all' unmounts itself", async () => {
    mockUseGetSourceTable.mockReturnValue({
      data: {
        ...baseTableData,
        businessMetadata: {
          description: "AI: order records",
          enrichmentSource: "AI_GENERATED",
          synonyms: ["orders_table"],
        },
      },
      isLoading: false,
      error: null,
    });
    render(<TableDetail />, { wrapper });
    fireEvent.click(screen.getAllByRole("button", { name: /^edit$/i })[0]);
    const inputBefore = document.querySelector(
      'input[placeholder="e.g. customers_table"]',
    ) as HTMLInputElement | null;
    expect(inputBefore).not.toBeNull();

    fireEvent.click(
      screen.getByRole("button", { name: /clear all synonyms/i }),
    );

    // Wait one animation frame for handleClear.
    await new Promise((r) => requestAnimationFrame(() => r(undefined)));

    const inputAfter = document.querySelector(
      'input[placeholder="e.g. customers_table"]',
    ) as HTMLInputElement | null;
    expect(inputAfter).not.toBeNull();
    expect(document.activeElement).toBe(inputAfter);
  });
});
