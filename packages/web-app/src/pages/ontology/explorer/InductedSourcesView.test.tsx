// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, waitFor, within } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { InductedSourcesView } from "./InductedSourcesView";
import type { ApiClient } from "@components/ApiClientProvider";
import type { InducedDatasource } from "../../../services/ontology-engine";

const listInducedDatasources = vi.fn();
vi.mock("../../../services/ontology-engine", () => ({
  listInducedDatasources: (...args: unknown[]) =>
    listInducedDatasources(...args),
}));

function datasource(
  overrides: Partial<InducedDatasource> & { datasourceId: string },
): InducedDatasource {
  return {
    proposal_id: "p1",
    ontology_id: "ont:sales",
    label: "Sales DB",
    tables_processed: 0,
    columns_processed: 0,
    novel_classes_created: 0,
    ...overrides,
  };
}

// Minimal stub client — the component only forwards it to the (mocked) service
// call, so nothing is ever invoked on it.
const apiClient: ApiClient = {
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
};

function renderView(props: { namespace?: string } = { namespace: "ns" }) {
  const result = render(
    <InductedSourcesView apiClient={apiClient} namespace={props.namespace} />,
  );
  // Scope every query to THIS render's container: a prior test's tree can
  // linger briefly before cleanup(), and Cloudscape re-renders the table as the
  // fetch resolves, so a page-global `screen` query could match stale nodes.
  return within(result.container);
}

describe("InductedSourcesView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders a row per inducted datasource with its label, tables and novel classes", async () => {
    listInducedDatasources.mockResolvedValue([
      datasource({
        datasourceId: "ds-sales",
        name: "sales_warehouse",
        label: "Sales DB",
        source_type: "JDBC",
        tables_processed: 7,
        novel_classes_created: 12,
      }),
    ]);

    const view = renderView();

    await waitFor(() =>
      expect(listInducedDatasources).toHaveBeenCalledWith(apiClient, "ns"),
    );
    // Display name prefers `name` over the raw datasource id.
    await view.findByText("sales_warehouse");
    expect(view.getByText("Sales DB")).toBeInTheDocument();
    expect(view.getByText("7")).toBeInTheDocument();
    // Structured sources annotate the class count as novel.
    expect(view.getByText("12 (novel)")).toBeInTheDocument();
  });

  it("falls back to the datasource id when the source has no name", async () => {
    listInducedDatasources.mockResolvedValue([
      datasource({ datasourceId: "ds-no-name", source_type: "JDBC" }),
    ]);
    const view = renderView();
    await view.findByText("ds-no-name");
  });

  it("shows 'N/A' tables for an UNSTRUCTURED source (documents have no tables)", async () => {
    listInducedDatasources.mockResolvedValue([
      datasource({
        datasourceId: "ds-docs",
        name: "contracts",
        label: "Contract corpus",
        source_type: "UNSTRUCTURED",
        // The backend still reports 0 here; the UI must not render it as a
        // table count, since "0 tables" reads as a failed run.
        tables_processed: 0,
        novel_classes_created: 4,
      }),
    ]);

    const view = renderView();

    await view.findByText("contracts");
    expect(view.getByText("N/A")).toBeInTheDocument();
    // Unstructured rows show the bare class count (no "(novel)" qualifier).
    expect(view.getByText("4")).toBeInTheDocument();
    expect(view.queryByText("4 (novel)")).toBeNull();
  });

  it("renders the empty state when no source has fed an induction yet", async () => {
    listInducedDatasources.mockResolvedValue([]);
    const view = renderView();
    await view.findByText(/No inducted sources yet/);
    expect(view.getByText("(0)")).toBeInTheDocument();
  });

  it("surfaces a fetch failure in a dismissible warning Alert", async () => {
    listInducedDatasources.mockRejectedValue(new Error("neptune unreachable"));

    const view = renderView();

    await view.findByText("Could not load inducted sources");
    expect(view.getByText("neptune unreachable")).toBeInTheDocument();
    // The table still renders (empty) rather than the whole view erroring out.
    expect(view.getByText(/No inducted sources yet/)).toBeInTheDocument();
  });

  it("does not fetch at all until a namespace is known", async () => {
    const view = renderView({});
    // The route param can be undefined on the first paint; the effect must
    // bail rather than request `/namespaces/undefined/...`.
    expect(listInducedDatasources).not.toHaveBeenCalled();
    // It stays in the loading state (no error, no empty-state claim of "none").
    expect(view.queryByText("Could not load inducted sources")).toBeNull();
  });
});
