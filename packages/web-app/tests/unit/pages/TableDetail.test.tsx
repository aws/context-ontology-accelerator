// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { vi, describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import React from "react";
import { TableDetail } from "../../../src/pages/TableDetail";

const mockUseGetSourceTable = vi.fn();
const mockUseGetSource = vi.fn();
const mockUseReviewSourceTable = vi.fn();
const mockUseReviewSourceColumn = vi.fn();
const mockUseUpdateSourceTableMetadata = vi.fn();
const mockUseUpdateSourceColumnMetadata = vi.fn();
const mockUseUpdateSourceTableKeys = vi.fn();

vi.mock("../../../src/api-hooks", () => ({
  useGetSourceTable: (...args: unknown[]) => mockUseGetSourceTable(...args),
  useGetSource: (...args: unknown[]) => mockUseGetSource(...args),
  useReviewSourceTable: (...args: unknown[]) =>
    mockUseReviewSourceTable(...args),
  useReviewSourceColumn: (...args: unknown[]) =>
    mockUseReviewSourceColumn(...args),
  useUpdateSourceTableMetadata: (...args: unknown[]) =>
    mockUseUpdateSourceTableMetadata(...args),
  useUpdateSourceColumnMetadata: (...args: unknown[]) =>
    mockUseUpdateSourceColumnMetadata(...args),
  useUpdateSourceTableKeys: (...args: unknown[]) =>
    mockUseUpdateSourceTableKeys(...args),
  useKeepRescanRemoval: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@coa/control-plane-client", () => ({
  ReviewStatus: {
    APPROVED: "APPROVED",
    PENDING_REVIEW: "PENDING_REVIEW",
    REJECTED: "REJECTED",
  },
  ReviewDecision: { APPROVE: "APPROVE", REJECT: "REJECT" },
}));

function renderPage() {
  return render(
    <MemoryRouter
      initialEntries={["/namespaces/ns-1/data-sources/ds-1/tables/t1"]}
    >
      <Routes>
        <Route
          path="/namespaces/:namespaceId/data-sources/:dataSourceId/tables/:tableId"
          element={<TableDetail />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("TableDetail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    const mockMutation = { mutate: vi.fn(), isPending: false };
    mockUseGetSource.mockReturnValue({
      data: { body: { databaseDetails: { metadataEnrichmentEnabled: true } } },
      isLoading: false,
      error: null,
    });
    mockUseReviewSourceTable.mockReturnValue(mockMutation);
    mockUseReviewSourceColumn.mockReturnValue(mockMutation);
    mockUseUpdateSourceTableMetadata.mockReturnValue(mockMutation);
    mockUseUpdateSourceColumnMetadata.mockReturnValue(mockMutation);
    mockUseUpdateSourceTableKeys.mockReturnValue(mockMutation);
  });

  it("renders without crashing when loading", () => {
    mockUseGetSourceTable.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    });
    const { container } = renderPage();
    expect(container).toBeTruthy();
  });

  it("shows error alert on failure", () => {
    mockUseGetSourceTable.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: { message: "Not found" },
    });
    renderPage();
    expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
  });

  it("returns null when data is undefined and not loading", () => {
    mockUseGetSourceTable.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: null,
    });
    const { container } = renderPage();
    expect(container.textContent).toBe("");
  });
});
