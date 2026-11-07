// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A metric whose SQL and source table were withheld for the caller's role must
 * say so in the Source and Expressions areas instead of rendering blanks that
 * look like a broken metric.
 */
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi } from "vitest";
import { MetricDetail } from "./MetricDetail";

const NOTICE =
  "Your role doesn't include access to this metric's source schema.";

// Stable reference, like React Query's cached data.
let mockData: { metric: Record<string, unknown> } | undefined;

vi.mock("../api-hooks/use-metrics", () => ({
  useGetMetric: () => ({ data: mockData, isLoading: false, error: null }),
  useDeleteMetric: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
  }),
}));
vi.mock("../api-hooks/use-source-name-by-id", () => ({
  useSourceNameById: () => new Map([["ds-1", "Orders DB"]]),
}));

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={["/namespaces/ns/metrics/revenue"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/metrics/:metricName"
          element={<MetricDetail />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("MetricDetail withheld definition", () => {
  it("shows the notice in Source and Expressions when the definition is withheld", () => {
    mockData = {
      metric: {
        name: "revenue",
        description: "Total revenue",
        expression: { dialects: [] },
        dataSourceId: "ds-1",
        sourceTable: "",
      },
    };
    renderDetail();
    const notices = screen.getAllByTestId("metric-source-withheld");
    expect(notices).toHaveLength(2);
    for (const n of notices) expect(n).toHaveTextContent(NOTICE);
    // The non-withheld source fields still render.
    expect(screen.getByText("Orders DB")).toBeInTheDocument();
    expect(screen.queryByText("SQL Expression")).toBeNull();
  });

  it("renders the definition and no notice for a full-visibility caller", () => {
    mockData = {
      metric: {
        name: "revenue",
        description: "Total revenue",
        expression: {
          dialects: [{ dialect: "TRINO", expression: "SELECT SUM(x) FROM t" }],
        },
        dataSourceId: "ds-1",
        sourceTable: "orders",
      },
    };
    renderDetail();
    expect(screen.queryByTestId("metric-source-withheld")).toBeNull();
    expect(document.body.textContent).not.toContain(NOTICE);
    expect(screen.getByText("orders")).toBeInTheDocument();
    expect(screen.getByText("SELECT SUM(x) FROM t")).toBeInTheDocument();
  });
});
