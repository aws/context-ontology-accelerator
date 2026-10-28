// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The synonym field adds the typed word on Enter. An Enter that confirms an
 * IME conversion (Japanese, Chinese, Korean input) must not add it: the user
 * is still typing, and a half-finished word would land in the list (#1374).
 * The page-level call sites have no shared component, so this pins one of
 * them to `isSubmitEnter` against a revert to `detail.key === "Enter"`.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi } from "vitest";
import { MetricForm } from "./MetricForm";

vi.mock("../api-hooks/use-metrics", () => ({
  useGetMetric: () => ({ data: undefined, isLoading: false, error: null }),
  useCreateMetric: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateMetric: () => ({ mutate: vi.fn(), isPending: false }),
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

function renderCreateForm() {
  return render(
    <MemoryRouter initialEntries={["/namespaces/ns/metrics/create"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/metrics/:metricName"
          element={<MetricForm />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("MetricForm synonym input", () => {
  it("does not add the synonym on the Enter that confirms an IME conversion", () => {
    renderCreateForm();
    const input = screen.getByPlaceholderText("Add synonym and press Enter");

    fireEvent.change(input, { target: { value: "総売上" } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(screen.queryByRole("button", { name: /総売上/ })).toBeNull();

    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByRole("button", { name: /総売上/ })).toBeTruthy();
    expect(input).toHaveProperty("value", "");
  });
});
