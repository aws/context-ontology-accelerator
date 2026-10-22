// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * TableDetail's breadcrumb trail conditionally appends an `Edit` crumb when
 * the page is in any of its three editing states (column edit, table-level
 * edit, or keys edit). This is a branch producing a different-length crumb
 * array, the same shape of hazard the Explorer/Induction branch on
 * OntologyClassDetail has a dedicated test for, so it gets one here too
 * rather than only being verified by reading the source.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { TableDetail } from "./TableDetail";
import type { BreadcrumbItem } from "@components/BreadcrumbProvider";

// This is a breadcrumb-trail test, so it opts out of the global
// vitest-setup mocks that no-op useBreadcrumbTrail and stub useNamespace.
vi.unmock("@api-hooks/use-breadcrumb-trail");
vi.unmock("@api-hooks/use-namespace");

const TABLE_DATA = {
  tableId: "sales.orders",
  name: "orders",
  database: "sales",
  reviewStatus: "APPROVED",
  businessMetadata: { description: "Orders" },
  primaryKey: { columns: ["order_id"], source: "DETERMINISTIC" },
  foreignKeys: [],
  columns: [],
  technicalMetadata: {},
};

vi.mock("@api-hooks", () => ({
  useGetSourceTable: () => ({
    data: TABLE_DATA,
    isLoading: false,
    error: null,
  }),
  useGetSource: () => ({
    data: { body: { name: "Sales source" } },
    isLoading: false,
    error: null,
  }),
  useReviewSourceTable: () => ({ mutate: vi.fn(), isPending: false }),
  useReviewSourceColumn: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateSourceTableMetadata: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateSourceColumnMetadata: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateSourceTableKeys: () => ({
    mutate: vi.fn(),
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

vi.mock("@api-hooks/use-get-namespace", () => ({
  useGetNamespace: () => ({
    data: { namespace: { displayName: "My Namespace" } },
    isSuccess: true,
    isLoading: false,
  }),
}));

let captured: BreadcrumbItem[] = [];
vi.mock("@components/BreadcrumbProvider", () => ({
  useBreadcrumbs: () => ({
    setBreadcrumbs: (items: BreadcrumbItem[]) => {
      captured = items;
    },
    clearBreadcrumbs: () => {
      captured = [];
    },
  }),
}));

function renderTable() {
  return render(
    <MemoryRouter
      initialEntries={["/namespaces/ns/sources/src-1/tables/sales.orders"]}
    >
      <Routes>
        <Route
          path="/namespaces/:namespaceId/sources/:dataSourceId/tables/:tableId"
          element={<TableDetail />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  captured = [];
});

describe("TableDetail breadcrumb trail", () => {
  it("publishes COA > {namespace} > Sources > {source} > {table} with no Edit crumb by default", async () => {
    renderTable();

    await waitFor(() => expect(captured.length).toBeGreaterThan(1));
    expect(captured.map((c) => c.text)).toEqual([
      "COA",
      "My Namespace",
      "Sources",
      "Sales source",
      "orders",
    ]);
    expect(captured.at(-1)?.onClick).toBeUndefined();
  });

  it("appends an Edit crumb while the table-level edit modal is open", async () => {
    renderTable();
    await waitFor(() => expect(captured.length).toBeGreaterThan(1));
    expect(captured.map((c) => c.text)).not.toContain("Edit");

    fireEvent.click(screen.getAllByRole("button", { name: /^edit$/i })[0]);

    await waitFor(() => expect(captured.map((c) => c.text)).toContain("Edit"));
    expect(captured.map((c) => c.text)).toEqual([
      "COA",
      "My Namespace",
      "Sources",
      "Sales source",
      "orders",
      "Edit",
    ]);
    expect(captured.at(-1)?.onClick).toBeUndefined();
  });
});
