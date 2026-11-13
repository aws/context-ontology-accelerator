// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { OntologyInventoryView } from "./OntologyInventoryView";
import type { ApiClient } from "@components/ApiClientProvider";
import type { OntologyRecord } from "../../../services/ontology-engine";

const listOntologies = vi.fn();
vi.mock("../../../services/ontology-engine", () => ({
  listOntologies: (...args: unknown[]) => listOntologies(...args),
}));

// Capture navigations so the induced-row Link's onFollow can be asserted
// without a real router history assertion.
const navigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>(
      "react-router-dom",
    );
  return { ...actual, useNavigate: () => navigate };
});

function record(overrides: Partial<OntologyRecord>): OntologyRecord {
  return {
    ontologyId: "http://ex.org/o",
    uri: "http://ex.org/o",
    title: "Onto",
    ontologyType: "foundational",
    format: "turtle",
    domainTags: [],
    classCount: 0,
    propertyCount: 0,
    axiomCount: 0,
    embeddingCount: 0,
    createdAt: "2026-07-01T00:00:00Z",
    updatedAt: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

const INDUCED = record({
  ontologyId: "http://ex.org/induced/sales",
  uri: "http://ex.org/induced/sales",
  title: "Sales (induced)",
  ontologyType: "induced",
  classCount: 11,
  propertyCount: 22,
  axiomCount: 33,
});
const FOUNDATIONAL = record({
  ontologyId: "http://ex.org/fibo",
  uri: "http://ex.org/fibo",
  title: "FIBO",
  ontologyType: "foundational",
  classCount: 400,
});
const UPLOADED = record({
  ontologyId: "http://ex.org/mine",
  uri: "http://ex.org/mine",
  title: "My Upload",
  ontologyType: "user_uploaded",
  classCount: 3,
});

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
    <MemoryRouter initialEntries={["/namespaces/ns/ontology/graph"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/ontology/graph"
          element={
            <OntologyInventoryView
              apiClient={apiClient}
              namespace={props.namespace}
            />
          }
        />
      </Routes>
    </MemoryRouter>,
  );
  // Scope every query to THIS render's container: a prior test's tree can
  // linger briefly before cleanup(), and Cloudscape re-renders the table as the
  // fetch resolves, so a page-global `screen` query could match stale nodes.
  return within(result.container);
}

/**
 * The type-filter segment button for a given filter id. Cloudscape's
 * SegmentedControl renders BOTH a button toolbar and a (narrow-viewport) Select
 * with the same option labels, so a by-text lookup is ambiguous; each segment
 * button carries `data-testid={id}`, which is unique.
 */
function segment(view: ReturnType<typeof renderView>, id: string) {
  return view.getByTestId(id);
}

/** Click a type-filter segment by its filter id. */
function selectFilter(view: ReturnType<typeof renderView>, id: string) {
  fireEvent.click(segment(view, id));
}

describe("OntologyInventoryView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders every registered ontology with its type Badge and counts", async () => {
    listOntologies.mockResolvedValue([INDUCED, FOUNDATIONAL, UPLOADED]);

    const view = renderView();

    await waitFor(() =>
      expect(listOntologies).toHaveBeenCalledWith(apiClient, "ns"),
    );
    // All three groups are listed together — this is the namespace inventory.
    await view.findByText("Sales (induced)");
    expect(view.getByText("FIBO")).toBeInTheDocument();
    expect(view.getByText("My Upload")).toBeInTheDocument();
    // Type badges use the shared display mapping.
    expect(view.getByText("Induced")).toBeInTheDocument();
    expect(view.getByText("Foundational")).toBeInTheDocument();
    expect(view.getByText("Uploaded")).toBeInTheDocument();
    // Per-row counts (classes / properties / axioms) come straight off the row.
    expect(view.getByText("11")).toBeInTheDocument();
    expect(view.getByText("22")).toBeInTheDocument();
    expect(view.getByText("33")).toBeInTheDocument();
    // Header counter reflects the visible (unfiltered) row count.
    expect(view.getByText("(3)")).toBeInTheDocument();
  });

  it("labels each filter segment with its group count", async () => {
    listOntologies.mockResolvedValue([INDUCED, FOUNDATIONAL, UPLOADED]);
    const view = renderView();

    // Counts are derived from ontologyTypeGroup, so user_uploaded lands in
    // "Uploaded" and the "All" segment counts every row.
    await waitFor(() =>
      expect(segment(view, "all")).toHaveTextContent("All (3)"),
    );
    expect(segment(view, "induced")).toHaveTextContent("Induced (1)");
    expect(segment(view, "foundational")).toHaveTextContent("Foundational (1)");
    expect(segment(view, "uploaded")).toHaveTextContent("Uploaded (1)");
  });

  it("groups user_created and user_uploaded together under Uploaded", async () => {
    listOntologies.mockResolvedValue([
      UPLOADED,
      record({
        ontologyId: "http://ex.org/mine2",
        uri: "http://ex.org/mine2",
        title: "My Other Upload",
        ontologyType: "user_created",
      }),
    ]);
    const view = renderView();
    await waitFor(() =>
      expect(segment(view, "uploaded")).toHaveTextContent("Uploaded (2)"),
    );
    expect(segment(view, "induced")).toHaveTextContent("Induced (0)");
  });

  it("narrows the visible rows to the selected type", async () => {
    listOntologies.mockResolvedValue([INDUCED, FOUNDATIONAL, UPLOADED]);
    const view = renderView();
    await view.findByText("Sales (induced)");

    selectFilter(view, "induced");

    // Only the induced row survives the filter; the header counter follows.
    await waitFor(() => expect(view.queryByText("FIBO")).toBeNull());
    expect(view.queryByText("My Upload")).toBeNull();
    expect(view.getByText("Sales (induced)")).toBeInTheDocument();
    expect(view.getByText("(1)")).toBeInTheDocument();

    // Switching to Foundational swaps which row is shown.
    selectFilter(view, "foundational");
    await waitFor(() => expect(view.queryByText("Sales (induced)")).toBeNull());
    expect(view.getByText("FIBO")).toBeInTheDocument();
  });

  it("links ONLY induced ontologies to the induced-ontology page", async () => {
    listOntologies.mockResolvedValue([INDUCED, FOUNDATIONAL, UPLOADED]);
    const view = renderView();

    // The induced row's title is an anchor to the graph-contents page, with the
    // ontologyId URL-encoded into the query string.
    const inducedLink = await view.findByText("Sales (induced)");
    const anchor = inducedLink.closest("a");
    expect(anchor).not.toBeNull();
    expect(anchor).toHaveAttribute(
      "href",
      `/namespaces/ns/ontology/induced?ontology_id=${encodeURIComponent(
        INDUCED.ontologyId,
      )}`,
    );

    // Foundational and uploaded rows have no browsable graph page → plain text.
    expect(view.getByText("FIBO").closest("a")).toBeNull();
    expect(view.getByText("My Upload").closest("a")).toBeNull();
  });

  it("navigates client-side (no full page load) when an induced row is opened", async () => {
    listOntologies.mockResolvedValue([INDUCED]);
    const view = renderView();

    // The table re-renders as the fetch resolves, so re-query and click inside
    // waitFor until the handler actually fires against the live node.
    await waitFor(() => {
      fireEvent.click(view.getByText("Sales (induced)"));
      expect(navigate).toHaveBeenCalledWith(
        `/namespaces/ns/ontology/induced?ontology_id=${encodeURIComponent(
          INDUCED.ontologyId,
        )}`,
      );
    });
  });

  it("shows 'Delete in progress' instead of a type Badge while a row is deleting", async () => {
    listOntologies.mockResolvedValue([
      record({
        ontologyId: "http://ex.org/going",
        uri: "http://ex.org/going",
        title: "Going Away",
        ontologyType: "user_uploaded",
        status: "deleting",
      }),
    ]);

    const view = renderView();

    await view.findByText("Delete in progress");
    // The Type cell is taken over by the status indicator — no "Uploaded" badge
    // in the row (the filter segment label is "Uploaded (1)", not "Uploaded").
    expect(view.queryByText("Uploaded")).toBeNull();
  });

  it("polls while a row is deleting so the indicator clears itself", async () => {
    listOntologies.mockResolvedValue([
      record({
        ontologyId: "http://ex.org/going",
        uri: "http://ex.org/going",
        title: "Going Away",
        ontologyType: "induced",
        status: "deleting",
      }),
    ]);

    // Install fake timers BEFORE render so they own the component's interval;
    // `shouldAdvanceTime` keeps promises/RTL queries working.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const view = renderView();
      await view.findByText("Delete in progress");
      const callsBefore = listOntologies.mock.calls.length;

      await vi.advanceTimersByTimeAsync(4100);

      // The poll must re-fetch on its own; without it the row would sit at
      // "Delete in progress" until the user pressed Refresh.
      await waitFor(() =>
        expect(listOntologies.mock.calls.length).toBeGreaterThan(callsBefore),
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not link a row that is being deleted", async () => {
    listOntologies.mockResolvedValue([
      record({
        ontologyId: "http://ex.org/going",
        uri: "http://ex.org/going",
        title: "Going Away",
        // Induced rows normally link to their detail page; a mid-delete one must
        // not, since its graph is being torn down underneath.
        ontologyType: "induced",
        status: "deleting",
      }),
    ]);

    const view = renderView();
    // The title still renders (as plain text) — only the anchor is withheld.
    await view.findByText("Going Away");
    expect(view.queryByRole("link", { name: "Going Away" })).toBeNull();
  });

  it("renders the all-types empty state pointing at the Induction page", async () => {
    listOntologies.mockResolvedValue([]);
    const view = renderView();
    await view.findByText(/No ontologies yet/);
    expect(view.getByText("(0)")).toBeInTheDocument();
  });

  it("renders a type-specific empty state when a filter matches nothing", async () => {
    listOntologies.mockResolvedValue([FOUNDATIONAL]);
    const view = renderView();
    await view.findByText("FIBO");

    selectFilter(view, "induced");

    await view.findByText("No ontologies of this type.");
    expect(view.queryByText(/No ontologies yet/)).toBeNull();
  });

  it("surfaces a fetch failure in a dismissible warning Alert", async () => {
    listOntologies.mockRejectedValue(new Error("registry unavailable"));
    const view = renderView();
    await view.findByText("Could not load ontologies");
    expect(view.getByText("registry unavailable")).toBeInTheDocument();
  });

  it("does not fetch at all until a namespace is known", async () => {
    const view = renderView({});
    // The route param can be undefined on the first paint; the effect must bail
    // rather than request `/namespaces/undefined/ontologies`.
    expect(listOntologies).not.toHaveBeenCalled();
    expect(view.queryByText("Could not load ontologies")).toBeNull();
  });

  it("re-fetches the registry when Refresh is clicked", async () => {
    listOntologies.mockResolvedValue([FOUNDATIONAL]);
    const view = renderView();
    await view.findByText("FIBO");
    const callsBefore = listOntologies.mock.calls.length;

    await waitFor(() => {
      fireEvent.click(view.getByText("Refresh"));
      expect(listOntologies.mock.calls.length).toBeGreaterThan(callsBefore);
    });
  });
});
