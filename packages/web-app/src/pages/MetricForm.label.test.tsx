// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * UpdateMetric is a full replace, so a save that leaves `label` out clears it.
 * The edit form keeps no label input of its own, so it must send the existing
 * label back unchanged, and omit the field for a metric without one: an empty
 * string would make every save of an unlabelled metric fail with 400.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";
import { MetricForm } from "./MetricForm";

interface MockState {
  data?: { metric: Record<string, unknown> };
  update: Mock;
  create: Mock;
}

// `data` must keep its identity across renders: the form copies it into state
// in an effect keyed on it, so a fresh object per render would loop forever.
const mocks = vi.hoisted(
  (): MockState => ({
    data: undefined,
    update: vi.fn(),
    create: vi.fn(),
  }),
);

vi.mock("@api-hooks/use-metrics", () => ({
  useGetMetric: () => ({
    data: mocks.data,
    isLoading: false,
    error: null,
  }),
  useCreateMetric: () => ({ mutate: mocks.create, isPending: false }),
  useUpdateMetric: () => ({ mutate: mocks.update, isPending: false }),
}));

vi.mock("@api-hooks/use-list-sources", () => ({
  useListSources: () => ({
    data: { pages: [{ items: [] }] },
    isLoading: false,
    error: null,
  }),
}));

vi.mock("@api-hooks/use-list-source-tables", () => ({
  useListSourceTables: () => ({
    data: { items: [] },
    isFetching: false,
    error: null,
  }),
}));

vi.mock("@components/ApiClientProvider", () => ({
  useApiClient: () => ({ get: vi.fn() }),
}));

vi.mock("../services/ontology-engine", () => ({
  searchEntities: vi.fn().mockResolvedValue([]),
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

function renderEditForm() {
  return render(
    <MemoryRouter
      initialEntries={["/namespaces/ns/metrics/revenue_total/edit"]}
    >
      <Routes>
        <Route
          path="/namespaces/:namespaceId/metrics/:metricName/edit"
          element={<MetricForm />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function savedBody(): Record<string, unknown> {
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  expect(mocks.update).toHaveBeenCalledTimes(1);
  return mocks.update.mock.calls[0][0];
}

describe("MetricForm label pass-through", () => {
  beforeEach(() => {
    mocks.update.mockReset();
    mocks.create.mockReset();
    mocks.data = undefined;
  });

  it("sends the existing label back unchanged on save", () => {
    mocks.data = { metric: storedMetric({ label: " 売上合計 \u200d" }) };
    renderEditForm();

    expect(savedBody().label).toBe(" 売上合計 \u200d");
  });

  it("omits label for a metric that has none", () => {
    mocks.data = { metric: storedMetric() };
    renderEditForm();

    expect(savedBody()).not.toHaveProperty("label");
  });

  it("omits label rather than sending an empty string", () => {
    mocks.data = { metric: storedMetric({ label: "" }) };
    renderEditForm();

    expect(savedBody()).not.toHaveProperty("label");
  });
});
