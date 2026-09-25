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

vi.mock("@api-hooks", () => ({
  useGetSourceTable: () => mockUseGetSourceTable(),
  useGetSource: () => mockUseGetSource(),
  useReviewSourceTable: () => ({ mutate: vi.fn(), isPending: false }),
  useReviewSourceColumn: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateSourceTableMetadata: () => ({ mutate: vi.fn(), isPending: false }),
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

// Default table payload used by most tests. Individual tests override
// mockUseGetSourceTable with a spread of this plus the field under test
// (e.g. rescanDiff) so the shared shape stays in one place.
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

  it("surfaces a pending cross-source relationship and approves it (#1088)", () => {
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

    // Review state, cross-source chip, and provenance are all shown.
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
});

// DETERMINISTIC only describes the *description* field's
// provenance. Tags/synonyms/glossary terms can still be AI-generated even
// when enrichmentSource is DETERMINISTIC — the UI must not imply the whole
// metadata block came from the source system.
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

// follow-up: when metadataEnrichmentEnabled is false, the scan
// pipeline never runs the enrichment step at all, so tags/synonyms/glossary
// terms can't be AI-generated regardless of enrichmentSource. The hint must
// not render in that case — showing it would itself be misleading.
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

  it("shows the hint when metadataEnrichmentEnabled is true", () => {
    mockUseGetSource.mockReturnValue({
      data: {
        body: { databaseDetails: { metadataEnrichmentEnabled: true } },
      },
      isLoading: false,
      error: null,
    });
    render(<TableDetail />, { wrapper });
    expect(
      screen.getAllByRole("img", { name: "AI-enriched hint" }).length,
    ).toBeGreaterThan(0);
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

// Re-scan review surfaces an old-vs-new breakdown: a table-level "Changes
// since last approved scan" panel listing each changed table field as
// old → new, plus a per-column Changed/Added/Removed badge. rescanDiff is
// present only while the source is in RESCAN_REVIEW.
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

    // The summary panel is present; its detail is behind a foldable section
    // (headered with the change counts) so a table with many column changes
    // doesn't produce a huge panel. Expand it to read the old → new detail.
    expect(
      screen.getByText("Changes since last approved scan"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByText("1 table-level change, 1 column change"));
    expect(screen.getByText("Table-level changes")).toBeInTheDocument();
    expect(screen.getByText("Column changes")).toBeInTheDocument();

    // Field rows concatenate several text nodes ("description (DESCRIPTION):
    // Old order records → New order records"), so match values with a substring
    // regex rather than an exact string. Table-level change:
    expect(screen.getByText(/Old order records/)).toBeInTheDocument();
    expect(screen.getByText(/New order records/)).toBeInTheDocument();
    // The column change is listed in the panel with its changed field. Use the
    // field name "data_type" (unique to the diff row) rather than the value
    // "varchar", which also renders as the column's own type in the table.
    expect(screen.getByText(/data_type/)).toBeInTheDocument();

    // The modified column also carries a "Changed" badge on its own row. "status"
    // and "Changed" now appear both in the panel and on the row, so scope the
    // lookup to the table row rather than asserting a single match.
    const statusRow =
      screen
        .getAllByText("status")
        .map((el) => el.closest("tr"))
        .find((tr) => tr !== null) ?? null;
    expect(statusRow).not.toBeNull();
    expect(statusRow?.textContent).toContain("Changed");
  });

  it("does not render the changes panel when rescanDiff is absent", () => {
    // baseTableData (set in beforeEach) has no rescanDiff.
    render(<TableDetail />, { wrapper });
    expect(
      screen.queryByText("Changes since last approved scan"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Changed")).not.toBeInTheDocument();
  });

  it("shows a 'New table' note for a re-scan-added table (added, no diff)", () => {
    // A table the re-scan discovered for the first time has no before/after, so
    // it carries `added` and no `rescanDiff`. We say it's new instead of showing
    // an empty changes panel.
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
    // Not the old-vs-new changes panel — there is nothing to diff.
    expect(
      screen.queryByText("Changes since last approved scan"),
    ).not.toBeInTheDocument();
  });

  it("shows a 'Dropped table' note for a re-scan-dropped table (pendingDeletion)", () => {
    // A table the re-scan found gone from the source carries pendingDeletion;
    // the top panel says it's dropped (deleted on approve unless kept).
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
    // A dropped column is surfaced twice by two data paths: the rescanDiff
    // (status "removed") and the column's own pendingDeletion soft-flag. They
    // mean the same thing, so the row must show the actionable "Pending
    // deletion" (+ Keep) and NOT also the "Removed" diff badge.
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

    // "status" now appears in the summary panel's Column changes group as well
    // as on the table row, so scope to the row. The row must show the actionable
    // "Pending deletion" (+ Keep) and NOT a duplicate "Removed" diff badge; the
    // panel legitimately lists the removed column and is not asserted here.
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
