// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * MetricForm's breadcrumb trail is create/edit-aware: in edit mode (route
 * param is an existing metric name) it links the metric and appends an
 * `Edit` crumb; in create mode it appends a plain `Create` crumb instead.
 * These are different-length, different-shaped crumb arrays produced by the
 * same `isEdit` branch, the same hazard class the Explorer/Induction branch
 * on OntologyClassDetail has a dedicated test for, so it gets one here too.
 */
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MetricForm } from "./MetricForm";
import { LocaleProvider } from "../i18n/LocaleProvider";
import type { BreadcrumbItem } from "@components/BreadcrumbProvider";

// This is a breadcrumb-trail test, so it opts out of the global
// vitest-setup mocks that no-op useBreadcrumbTrail and stub useNamespace.
vi.unmock("@api-hooks/use-breadcrumb-trail");
vi.unmock("@api-hooks/use-namespace");

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

function renderForm(metricName: string) {
  return render(
    <MemoryRouter initialEntries={[`/namespaces/ns/metrics/${metricName}`]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/metrics/:metricName"
          element={<MetricForm />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  captured = [];
});

describe("MetricForm breadcrumb trail", () => {
  it("publishes a Create crumb (no metric name, no Edit) in create mode", async () => {
    renderForm("create");

    await waitFor(() => expect(captured.length).toBeGreaterThan(1));
    expect(captured.map((c) => c.text)).toEqual([
      "COA",
      "My Namespace",
      "Ontology",
      "Metrics",
      "Create",
    ]);
    expect(captured.at(-1)?.onClick).toBeUndefined();
  });

  it("publishes the metric name + Edit crumb in edit mode", async () => {
    renderForm("monthly_revenue");

    await waitFor(() => expect(captured.length).toBeGreaterThan(1));
    expect(captured.map((c) => c.text)).toEqual([
      "COA",
      "My Namespace",
      "Ontology",
      "Metrics",
      "monthly_revenue",
      "Edit",
    ]);
    expect(captured.at(-1)?.onClick).toBeUndefined();
    // The linked metric-name crumb (not the trailing Edit) must navigate.
    const metricCrumb = captured.find((c) => c.text === "monthly_revenue");
    expect(metricCrumb?.onClick).toBeTypeOf("function");
  });

  it("translates the fixed crumbs in Japanese and leaves names verbatim", async () => {
    window.localStorage.setItem("scl.locale", "ja");
    try {
      render(
        <LocaleProvider>
          <MemoryRouter initialEntries={["/namespaces/ns/metrics/create"]}>
            <Routes>
              <Route
                path="/namespaces/:namespaceId/metrics/:metricName"
                element={<MetricForm />}
              />
            </Routes>
          </MemoryRouter>
        </LocaleProvider>,
      );

      await waitFor(() =>
        expect(captured.map((c) => c.text)).toEqual([
          "COA",
          "My Namespace",
          "オントロジー",
          "メトリクス",
          "作成",
        ]),
      );
    } finally {
      window.localStorage.removeItem("scl.locale");
    }
  });
});
