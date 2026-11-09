// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { InducedOntologyDetailPage } from "./InducedOntologyDetail";

const OVERVIEW = {
  ontologyId: "http://ex.org/o#",
  namespace: "ns",
  graphUri: "https://ontology-workbench.local/ns/o",
  classes: [
    {
      uri: "http://ex.org/o#Claim",
      label: "Claim",
      comment: "A claim.",
      groundedTo: "http://customer.example/onto#Party",
      matchType: "exact",
    },
    { uri: "http://ex.org/o#Policy", label: "Policy", comment: undefined },
  ],
  objectProperties: [
    {
      uri: "http://ex.org/o#hasPolicy",
      label: "has policy",
      domain: "http://ex.org/o#Claim",
      range: "http://ex.org/o#Policy",
    },
  ],
  datatypeProperties: [
    {
      uri: "http://ex.org/o#amount",
      label: "amount",
      domain: "http://ex.org/o#Claim",
      range: "http://www.w3.org/2001/XMLSchema#decimal",
    },
  ],
};

// Mutable so a test can exercise the ungrounded ("all classes novel") branch.
const overviewState: { data: typeof OVERVIEW } = { data: OVERVIEW };
function resetOverview() {
  overviewState.data = OVERVIEW;
}
vi.mock("@api-hooks/use-ontology-overview", () => ({
  useOntologyOverview: () => ({
    data: overviewState.data,
    isLoading: false,
    error: null,
  }),
}));

// Mutable so a test can flip the induced row to status "deleting".
const registryRows: Record<string, unknown>[] = [];
function resetRegistry() {
  registryRows.length = 0;
  registryRows.push(
    {
      ontologyId: "http://ex.org/o#",
      title: "Claims Ontology",
      uri: "http://ex.org/o#",
    },
    // The ontology the sample class is grounded to — lets the rollup attribute
    // the groundedTo IRI to a named ontology instead of a bare IRI prefix.
    {
      ontologyId: "customer-onto",
      title: "Customer Ontology",
      uri: "http://customer.example/onto#",
      ontologyType: "foundational",
    },
  );
}
resetRegistry();
vi.mock("@api-hooks/use-list-ontologies", () => ({
  useListOntologies: () => ({ data: registryRows }),
}));

const downloadOntology = vi.fn();
const deleteOntology = vi.fn();
vi.mock("../../services/ontology-engine", () => ({
  downloadOntology: (...args: unknown[]) => downloadOntology(...args),
  deleteOntology: (...args: unknown[]) => deleteOntology(...args),
}));

vi.mock("@components/ontology/PendingReviewBanner", () => ({
  PendingReviewBanner: () => null,
}));

// One client for every render, as the real provider's memoized context gives.
const { apiClient } = vi.hoisted(() => ({
  apiClient: { get: vi.fn(), getText: vi.fn() },
}));

vi.mock("@components/ApiClientProvider", () => ({
  useApiClient: () => apiClient,
}));

function renderAt(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/namespaces/ns/ontology/induced${search}`]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/ontology/induced"
          element={<InducedOntologyDetailPage />}
        />
        <Route
          path="/namespaces/:namespaceId/ontology/induced/class"
          element={<div>CLASS_PAGE</div>}
        />
      </Routes>
    </MemoryRouter>,
  );
}

/**
 * Find the "Delete ontology" trigger. Cloudscape appends a `disabledReason` into
 * the button's own textContent when it renders the disabled state, and marks it
 * `aria-disabled="true"` rather than setting the native `disabled` attribute —
 * so match on prefix, and assert on aria-disabled (not `toBeDisabled()`).
 */
function findDeleteButton(
  container: HTMLElement,
): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find((b) =>
    b.textContent?.startsWith("Delete ontology"),
  );
}

const search = "?ontology_id=http%3A%2F%2Fex.org%2Fo%23";

describe("InducedOntologyDetailPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetRegistry();
    resetOverview();
    downloadOntology.mockResolvedValue("@prefix ex: <http://ex.org/o#> .");
    deleteOntology.mockResolvedValue(undefined);
  });

  it("renders tab counts and origin badges from the hook data", () => {
    const { container } = renderAt(search);
    expect(container.textContent).toContain("Induced Ontology");
    expect(container.textContent).toContain("Classes (2)");
    expect(container.textContent).toContain("Relationships (1)");
    expect(container.textContent).toContain("Attributes (1)");
    expect(container.textContent).toContain("Grounded (exact)");
    expect(container.textContent).toContain("Novel");
  });

  it("navigates to the class drill-down when a class is clicked", async () => {
    const { container } = renderAt(search);
    const claimLink = Array.from(container.querySelectorAll("a")).find(
      (a) => a.textContent === "Claim",
    );
    expect(claimLink).toBeTruthy();
    fireEvent.click(claimLink!);
    expect(container.textContent).toContain("CLASS_PAGE");
  });

  it("loads the Turtle source when the Source tab is opened", async () => {
    const { container } = renderAt(search);
    await userEvent.click(screen.getByText("Source (Turtle)"));
    await vi.waitFor(() => expect(downloadOntology).toHaveBeenCalled());
    await vi.waitFor(() =>
      expect(container.textContent).toContain("@prefix ex:"),
    );
  });

  it("fetches the Turtle when Download .ttl is clicked", async () => {
    const createObjectURL = vi.fn().mockReturnValue("blob:mock");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
    renderAt(search);
    await userEvent.click(screen.getByText("Download .ttl"));
    await vi.waitFor(() => expect(downloadOntology).toHaveBeenCalled());
    expect(createObjectURL).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("shows an error when ontology_id is missing", () => {
    renderAt("");
    expect(screen.getByText("Missing ontology")).toBeInTheDocument();
  });

  // ── "Grounded against" rollup ──

  it("rolls up grounded-to targets to the owning ontology with a class count", () => {
    const { container } = renderAt(search);
    expect(container.textContent).toContain("Grounded against");
    // Attributed by longest-URI-prefix match against the registry, not by the
    // raw IRI, so the user sees a name they recognise.
    expect(container.textContent).toContain("Customer Ontology");
    expect(container.textContent).toContain("(1 class)");
    expect(container.textContent).toContain("1 of 2 classes grounded");
  });

  it("says all classes are novel when nothing is grounded", () => {
    // Strip the grounding from the fixture so the empty branch renders.
    overviewState.data = {
      ...OVERVIEW,
      classes: OVERVIEW.classes.map((c) => ({
        ...c,
        groundedTo: undefined,
        matchType: undefined,
      })),
    };

    const { container } = renderAt(search);
    expect(container.textContent).toContain("None (all classes novel)");
    // And no per-ontology rollup row is emitted.
    expect(container.textContent).not.toContain("Customer Ontology");
  });

  // ── Delete affordance (regression guard) ──
  // The refactor that split Explorer from Induction removed the only induced
  // delete trigger and nothing failed, because the sole coverage was a
  // copy-string unit test. These assert the affordance itself.

  it("exposes a Delete ontology action", () => {
    const { container } = renderAt(search);
    const btn = findDeleteButton(container);
    expect(btn).toBeTruthy();
    expect(btn?.getAttribute("aria-disabled")).not.toBe("true");
  });

  it("deletes only after the user types 'delete' to confirm", async () => {
    const { container } = renderAt(search);
    const view = within(container);
    await userEvent.click(view.getByText("Delete ontology"));

    // Modal is portaled outside the container, so query it via screen.
    const confirm = await screen.findByPlaceholderText("delete");
    const confirmBtn = screen
      .getAllByRole("button", { name: "Delete" })
      .find((b) => !b.textContent?.includes("ontology"));
    expect(confirmBtn).toBeDisabled();

    fireEvent.change(confirm, { target: { value: "delete" } });
    expect(confirmBtn).toBeEnabled();
    fireEvent.click(confirmBtn!);

    await vi.waitFor(() =>
      expect(deleteOntology).toHaveBeenCalledWith(
        expect.anything(),
        "ns",
        "http://ex.org/o#",
      ),
    );
  });

  it("disables Delete and shows progress while a delete is already running", () => {
    registryRows[0].status = "deleting";
    const { container } = renderAt(search);
    expect(container.textContent).toContain("Delete in progress");
    expect(findDeleteButton(container)?.getAttribute("aria-disabled")).toBe(
      "true",
    );
  });

  it("surfaces deleteError so a stuck delete is distinguishable from an in-flight one", () => {
    registryRows[0].status = "deleting";
    registryRows[0].deleteError = "Neptune DROP timed out";
    const { container } = renderAt(search);
    expect(container.textContent).toContain("Delete failed");
    expect(container.textContent).toContain("Neptune DROP timed out");
  });
});
