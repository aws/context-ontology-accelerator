// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The detail page's header names a metric by its display label when it has
 * one, and by its name otherwise. The overview always lists the name the
 * metric is addressed by, and shows "—" for a metric with no label.
 */
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MetricDetail } from "./MetricDetail";

const mocks = vi.hoisted(() => ({
  metric: undefined as Record<string, unknown> | undefined,
}));

vi.mock("../api-hooks/use-metrics", () => ({
  useGetMetric: () => ({
    data: mocks.metric ? { metric: mocks.metric } : undefined,
    isLoading: false,
    error: null,
  }),
  useDeleteMetric: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("../api-hooks/use-source-name-by-id", () => ({
  useSourceNameById: () => new Map<string, string>(),
}));

function storedMetric(extra: Record<string, unknown> = {}) {
  return {
    name: "revenue_total",
    description: "Total revenue",
    dataSourceId: "ds-1",
    sourceTable: "orders",
    expression: {
      dialects: [{ dialect: "TRINO", expression: "SELECT SUM(x) FROM orders" }],
    },
    ...extra,
  };
}

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={["/namespaces/ns/metrics/revenue_total"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/metrics/:metricName"
          element={<MetricDetail />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

// An overview field is its key label followed by its value in one element.
function fieldText(key: string): string {
  const keyLabel = screen.getByText(key, { exact: true });
  return keyLabel.parentElement?.textContent ?? "";
}

describe("MetricDetail display label", () => {
  beforeEach(() => {
    mocks.metric = undefined;
  });

  it("names a labelled metric by its label and still lists its name", () => {
    mocks.metric = storedMetric({ label: "売上合計" });
    renderDetail();

    expect(
      screen.getByRole("heading", { level: 1, name: "売上合計" }),
    ).toBeInTheDocument();
    expect(fieldText("Name")).toBe("Namerevenue_total");
    expect(fieldText("Display label")).toBe("Display label売上合計");
  });

  it("names an unlabelled metric by its name and shows no label", () => {
    mocks.metric = storedMetric();
    renderDetail();

    expect(
      screen.getByRole("heading", { level: 1, name: "revenue_total" }),
    ).toBeInTheDocument();
    expect(fieldText("Display label")).toBe("Display label—");
  });
});
