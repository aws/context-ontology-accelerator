// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A metric whose SQL and source table were withheld for the caller (a role
 * without source-schema visibility, or a transient authorizer error) must not
 * be saved: the save would write the blanks over the stored definition and
 * drop its ontology concepts.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MetricForm } from "./MetricForm";

const mockUpdate = vi.fn();
// Stable reference, like React Query's cached data: a fresh object per render
// would re-run the form's load effect forever.
let mockData: { metric: Record<string, unknown> } | undefined;

vi.mock("../api-hooks/use-metrics", () => ({
  useGetMetric: () => ({
    data: mockData,
    isLoading: false,
    error: null,
  }),
  useCreateMetric: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateMetric: () => ({ mutate: mockUpdate, isPending: false }),
}));
vi.mock("../api-hooks/use-list-sources", () => ({
  useListSources: () => ({
    data: { pages: [{ items: [] }] },
    isLoading: false,
    error: null,
  }),
}));
vi.mock("../api-hooks/use-list-source-tables", () => ({
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

function renderEditForm() {
  return render(
    <MemoryRouter initialEntries={["/namespaces/ns/metrics/revenue"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/metrics/:metricName"
          element={<MetricForm />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("MetricForm withheld definition", () => {
  beforeEach(() => {
    mockUpdate.mockReset();
  });

  it("blocks saving a definition the server withheld", () => {
    mockData = {
      metric: {
        name: "revenue",
        description: "Total revenue",
        expression: { dialects: [] },
        dataSourceId: "ds-1",
        sourceTable: "",
      },
    };
    renderEditForm();
    expect(screen.getByTestId("metric-definition-withheld")).toBeTruthy();
    const save = screen.getByRole("button", { name: "Save changes" });
    fireEvent.click(save);
    expect(mockUpdate).not.toHaveBeenCalled();
  });

  it("allows editing a full definition", () => {
    mockData = {
      metric: {
        name: "revenue",
        description: "Total revenue",
        expression: {
          dialects: [
            { dialect: "TRINO", expression: "SELECT SUM(x) FROM orders" },
          ],
        },
        dataSourceId: "ds-1",
        sourceTable: "orders",
      },
    };
    renderEditForm();
    expect(screen.queryByTestId("metric-definition-withheld")).toBeNull();
  });
});

describe("MetricForm ontologyConcepts in the update body", () => {
  beforeEach(() => {
    mockUpdate.mockReset();
  });

  function fullMetric(extra: Record<string, unknown> = {}) {
    return {
      metric: {
        name: "revenue",
        description: "Total revenue",
        expression: {
          dialects: [
            { dialect: "TRINO", expression: "SELECT SUM(x) FROM orders" },
          ],
        },
        dataSourceId: "ds-1",
        sourceTable: "orders",
        ...extra,
      },
    };
  }

  function save(): Record<string, unknown> {
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(mockUpdate).toHaveBeenCalledTimes(1);
    return mockUpdate.mock.calls[0][0] as Record<string, unknown>;
  }

  it("omits the field when the loaded metric had none and the user left it alone", () => {
    mockData = fullMetric();
    renderEditForm();
    const body = save();
    expect(body.name).toBe("revenue");
    expect(body).not.toHaveProperty("ontologyConcepts");
  });

  it("sends the loaded concepts back when the user left them alone", () => {
    mockData = fullMetric({ ontologyConcepts: ["http://ex.org/o#Order"] });
    renderEditForm();
    expect(save().ontologyConcepts).toEqual(["http://ex.org/o#Order"]);
  });

  it("sends an empty list when the user removed every concept", () => {
    mockData = fullMetric({ ontologyConcepts: ["http://ex.org/o/Order"] });
    renderEditForm();
    fireEvent.click(screen.getByRole("button", { name: /Order ✕/ }));
    expect(save().ontologyConcepts).toEqual([]);
  });
});
