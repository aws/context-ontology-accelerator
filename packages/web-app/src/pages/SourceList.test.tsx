// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SourceList } from "./SourceList";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const NS_ID = "550e8400-e29b-41d4-a716-446655440000";

function makeSource(overrides: Record<string, unknown> = {}) {
  return {
    sourceId: "src-001",
    namespaceId: NS_ID,
    name: "test-source",
    sourceType: "DATABASE",
    sourceSubType: "GLUE_DATABASE",
    status: "APPROVED",
    createdAt: new Date("2026-05-01T00:00:00Z"),
    ...overrides,
  };
}

function makePage(items: ReturnType<typeof makeSource>[]) {
  return { items, nextToken: undefined };
}

const defaultMock = {
  data: { pages: [makePage([])] },
  isLoading: false,
  error: null,
  refetch: vi.fn(),
  isFetching: false,
};

let mockUseListSources = { ...defaultMock };

vi.mock("@api-hooks", () => ({
  useListSources: () => mockUseListSources,
}));

vi.mock("@coa/control-plane-client", () => ({
  SourceType: { DATABASE: "DATABASE", DOCUMENTS: "DOCUMENTS" },
}));

vi.mock("@utils/helpers", () => ({
  formatTimestamp: (ts: unknown) => (ts ? "2026-05-01" : "—"),
}));

// ---------------------------------------------------------------------------
// Wrapper
// ---------------------------------------------------------------------------

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter initialEntries={[`/namespaces/${NS_ID}/sources`]}>
      <Routes>
        <Route path="/namespaces/:namespaceId/sources" element={children} />
      </Routes>
    </MemoryRouter>
  </QueryClientProvider>
);

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("SourceList", () => {
  beforeEach(() => {
    mockUseListSources = { ...defaultMock };
  });

  // ── Loading / empty states ────────────────────────────────────────────────

  it("renders the Sources header", () => {
    render(<SourceList />, { wrapper });
    expect(screen.getByText("Sources")).toBeInTheDocument();
  });

  it("shows empty state when no sources exist", () => {
    render(<SourceList />, { wrapper });
    expect(screen.getByText("No sources")).toBeInTheDocument();
  });

  it("shows loading state", () => {
    mockUseListSources = {
      ...defaultMock,
      isLoading: true,
      data: undefined as unknown as typeof defaultMock.data,
    };
    render(<SourceList />, { wrapper });
    expect(screen.getByText("Loading sources")).toBeInTheDocument();
  });

  it("shows error alert when fetch fails", () => {
    mockUseListSources = { ...defaultMock, error: new Error("Network error") };
    render(<SourceList />, { wrapper });
    expect(screen.getByText("Failed to load sources")).toBeInTheDocument();
  });

  // ── Summary dashboard ─────────────────────────────────────────────────────

  it("renders summary tiles only for statuses that have items", () => {
    mockUseListSources = {
      ...defaultMock,
      data: {
        pages: [
          makePage([
            makeSource({ status: "APPROVED" }),
            makeSource({ sourceId: "src-002", status: "PENDING_REVIEW" }),
          ]),
        ],
      },
    };
    render(<SourceList />, { wrapper });
    // Labels use sourceStatusLabel() — lowercase "r" in "Pending review"
    expect(screen.getAllByText("Approved").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Pending review").length).toBeGreaterThan(0);
    // SCANNING has no items — should not appear in summary
    expect(screen.queryByText("Scanning")).not.toBeInTheDocument();
  });

  it("shows no summary tiles when list is empty", () => {
    render(<SourceList />, { wrapper });
    // No items → no summary dashboard rendered
    expect(screen.queryByText("Approved")).not.toBeInTheDocument();
    expect(screen.queryByText("Pending review")).not.toBeInTheDocument();
  });

  it("shows correct counts in summary tiles", () => {
    mockUseListSources = {
      ...defaultMock,
      data: {
        pages: [
          makePage([
            makeSource({ status: "APPROVED" }),
            makeSource({ sourceId: "src-002", status: "APPROVED" }),
            makeSource({ sourceId: "src-003", status: "PENDING_REVIEW" }),
            makeSource({ sourceId: "src-004", status: "SCAN_FAILED" }),
          ]),
        ],
      },
    };
    render(<SourceList />, { wrapper });

    // Summary counts appear as link text in the KeyValuePairs tiles
    const countLinks = screen.getAllByText(/^\d+$/);
    const values = countLinks.map((el) => Number(el.textContent));
    expect(values).toContain(2); // APPROVED
    expect(values).toContain(1); // PENDING_REVIEW
    // SCANNING has no items — not shown, so no 0 count
    expect(values).not.toContain(0);
  });

  it("shows both sources when no status filter is active", () => {
    mockUseListSources = {
      ...defaultMock,
      data: {
        pages: [
          makePage([
            makeSource({
              sourceId: "s1",
              name: "approved-source",
              status: "APPROVED",
            }),
            makeSource({
              sourceId: "s2",
              name: "failed-source",
              status: "SCAN_FAILED",
            }),
          ]),
        ],
      },
    };
    render(<SourceList />, { wrapper });

    // Both items visible initially
    expect(screen.getByText("approved-source")).toBeInTheDocument();
    expect(screen.getByText("failed-source")).toBeInTheDocument();

    // Summary tiles show the correct labels (may appear multiple times due to status column)
    expect(screen.getAllByText("Approved").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Scan failed").length).toBeGreaterThan(0);
  });

  // ── Table rendering ───────────────────────────────────────────────────────

  it("renders source rows with correct columns", () => {
    mockUseListSources = {
      ...defaultMock,
      data: {
        pages: [
          makePage([
            makeSource({
              name: "my-glue-db",
              sourceType: "DATABASE",
              sourceSubType: "GLUE_DATABASE",
              status: "APPROVED",
            }),
          ]),
        ],
      },
    };
    render(<SourceList />, { wrapper });

    expect(screen.getByText("my-glue-db")).toBeInTheDocument();
    // Status uses sourceStatusLabel() — "Approved" not "APPROVED"
    expect(screen.getAllByText("Approved").length).toBeGreaterThan(0);
    expect(screen.getByText("Database · Glue")).toBeInTheDocument();
  });

  it("renders the Last updated column header", () => {
    mockUseListSources = {
      ...defaultMock,
      data: {
        pages: [
          makePage([
            makeSource({
              name: "my-glue-db",
              updatedAt: new Date("2026-05-02T00:00:00Z"),
            }),
          ]),
        ],
      },
    };
    render(<SourceList />, { wrapper });

    // stickyHeader renders a header clone, so the label appears more than once
    expect(screen.getAllByText("Last updated").length).toBeGreaterThan(0);
  });

  it("renders correct type labels for all source sub-types", () => {
    mockUseListSources = {
      ...defaultMock,
      data: {
        pages: [
          makePage([
            makeSource({
              sourceId: "s1",
              name: "a",
              sourceType: "DATABASE",
              sourceSubType: "GLUE_DATABASE",
            }),
            makeSource({
              sourceId: "s2",
              name: "b",
              sourceType: "DATABASE",
              sourceSubType: "JDBC_DATABASE",
            }),
            makeSource({
              sourceId: "s3",
              name: "c",
              sourceType: "DOCUMENTS",
              sourceSubType: "S3",
            }),
            makeSource({
              sourceId: "s4",
              name: "d",
              sourceType: "DOCUMENTS",
              sourceSubType: "LOCAL_UPLOAD",
            }),
            makeSource({
              sourceId: "s5",
              name: "e",
              sourceType: "DATABASE",
              sourceSubType: "CUSTOM_CONNECTOR",
            }),
          ]),
        ],
      },
    };
    render(<SourceList />, { wrapper });

    expect(screen.getByText("Database · Glue")).toBeInTheDocument();
    expect(screen.getByText("Database · JDBC")).toBeInTheDocument();
    expect(screen.getByText("Documents · S3")).toBeInTheDocument();
    expect(screen.getByText("Documents · Upload")).toBeInTheDocument();
    // Must be a human label, not the raw enum ("Database · CUSTOM_CONNECTOR").
    expect(screen.getByText("Database · Custom connector")).toBeInTheDocument();
  });

  it("shows item count in header", () => {
    mockUseListSources = {
      ...defaultMock,
      data: {
        pages: [makePage([makeSource(), makeSource({ sourceId: "src-002" })])],
      },
    };
    render(<SourceList />, { wrapper });
    expect(screen.getByText("(2)")).toBeInTheDocument();
  });

  // ── Filtering ─────────────────────────────────────────────────────────────

  it("filters items by name text", () => {
    mockUseListSources = {
      ...defaultMock,
      data: {
        pages: [
          makePage([
            makeSource({ sourceId: "s1", name: "postgres-prod" }),
            makeSource({ sourceId: "s2", name: "glue-catalog" }),
          ]),
        ],
      },
    };
    render(<SourceList />, { wrapper });

    // Placeholder is "Find sources" in the actual component
    const textFilter = screen.getByPlaceholderText("Find sources");
    fireEvent.change(textFilter, { target: { value: "postgres" } });

    expect(screen.getByText("postgres-prod")).toBeInTheDocument();
    expect(screen.queryByText("glue-catalog")).not.toBeInTheDocument();
  });

  it("filters items by source type via segmented control", () => {
    mockUseListSources = {
      ...defaultMock,
      data: {
        pages: [
          makePage([
            makeSource({
              sourceId: "s1",
              name: "db-source",
              sourceType: "DATABASE",
            }),
            makeSource({
              sourceId: "s2",
              name: "doc-source",
              sourceType: "DOCUMENTS",
              sourceSubType: "S3",
            }),
          ]),
        ],
      },
    };
    render(<SourceList />, { wrapper });

    fireEvent.click(screen.getByText("Database"));

    expect(screen.getByText("db-source")).toBeInTheDocument();
    expect(screen.queryByText("doc-source")).not.toBeInTheDocument();
  });

  // ── Sorting ───────────────────────────────────────────────────────────────

  it("sorts by createdAt descending by default (newest first)", () => {
    mockUseListSources = {
      ...defaultMock,
      data: {
        pages: [
          makePage([
            makeSource({
              sourceId: "s1",
              name: "older",
              createdAt: new Date("2026-01-01T00:00:00Z"),
            }),
            makeSource({
              sourceId: "s2",
              name: "newer",
              createdAt: new Date("2026-05-01T00:00:00Z"),
            }),
          ]),
        ],
      },
    };
    render(<SourceList />, { wrapper });

    // Both names should be visible; verify "newer" appears before "older" in the DOM
    const newerEl = screen.getByText("newer");
    const olderEl = screen.getByText("older");
    expect(newerEl).toBeInTheDocument();
    expect(olderEl).toBeInTheDocument();
    // "newer" should come before "older" in document order
    expect(
      newerEl.compareDocumentPosition(olderEl) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  // ── Preferences ───────────────────────────────────────────────────────────

  it("renders the preferences gear icon", () => {
    render(<SourceList />, { wrapper });
    expect(screen.getByTitle("Preferences")).toBeInTheDocument();
  });

  // ── Pagination ────────────────────────────────────────────────────────────

  it("paginates items — default page size is 20", () => {
    const items = Array.from({ length: 25 }, (_, i) =>
      makeSource({
        sourceId: `src-${i}`,
        name: `source-${i}`,
        createdAt: new Date(
          `2026-01-${String(i + 1).padStart(2, "0")}T00:00:00Z`,
        ),
      }),
    );
    mockUseListSources = { ...defaultMock, data: { pages: [makePage(items)] } };
    render(<SourceList />, { wrapper });

    // Default page size 20: header row + 20 data rows = 21 rows total
    expect(screen.getAllByRole("row")).toHaveLength(21);
  });
});
