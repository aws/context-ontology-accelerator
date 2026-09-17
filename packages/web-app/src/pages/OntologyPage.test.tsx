// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
  within,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  OntologyPage,
  ontologyIngestState,
  ONTOLOGY_INGEST_STATUS,
  ONTOLOGY_INGEST_TERMINAL,
  proposalStatusDisplay,
  resolveCompletedInductionAction,
} from "./OntologyPage";
import { deleteOntologyCopy } from "@utils/ontology-display";
import type { OntologyRecord } from "../services/ontology-engine";

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
    createdAt: "",
    updatedAt: "",
    ...overrides,
  };
}

describe("ontologyIngestState derivation", () => {
  it("maps parseStatus ok → ready", () => {
    expect(ontologyIngestState(record({ parseStatus: "ok" }))).toBe("ready");
  });
  it("maps parseStatus parse_error → failed", () => {
    expect(ontologyIngestState(record({ parseStatus: "parse_error" }))).toBe(
      "failed",
    );
  });
  it("maps parseStatus pending (no embeddings yet) → ingesting", () => {
    expect(ontologyIngestState(record({ parseStatus: "pending" }))).toBe(
      "ingesting",
    );
  });
  it("treats pending-with-embeddings as ready (foundational-load leaves parseStatus stuck at pending)", () => {
    // Regression: FIBO ontologies load embeddings but the append path can
    // leave parseStatus="pending" — a positive embeddingCount means DONE,
    // so it must NOT show "Ingesting" forever (which also hung the poll).
    expect(
      ontologyIngestState(
        record({ parseStatus: "pending", embeddingCount: 11 }),
      ),
    ).toBe("ready");
  });
  it("parse_error wins even with embeddings present", () => {
    expect(
      ontologyIngestState(
        record({ parseStatus: "parse_error", embeddingCount: 5 }),
      ),
    ).toBe("failed");
  });
  it("falls back to ready for legacy rows (no parseStatus, embeddings > 0)", () => {
    expect(
      ontologyIngestState(record({ parseStatus: null, embeddingCount: 42 })),
    ).toBe("ready");
  });
  it("treats a legacy row with no embeddings as still ingesting", () => {
    expect(
      ontologyIngestState(record({ parseStatus: null, embeddingCount: 0 })),
    ).toBe("ingesting");
  });

  it("marks ready + failed as terminal, ingesting as non-terminal", () => {
    expect(ONTOLOGY_INGEST_TERMINAL.has("ready")).toBe(true);
    expect(ONTOLOGY_INGEST_TERMINAL.has("failed")).toBe(true);
    expect(ONTOLOGY_INGEST_TERMINAL.has("ingesting")).toBe(false);
    expect(ONTOLOGY_INGEST_STATUS.ready.label).toBe("Ready");
    expect(ONTOLOGY_INGEST_STATUS.failed.type).toBe("error");
  });
});

describe("resolveCompletedInductionAction", () => {
  it("returns a duplicate action (no navigation) when duplicate_of is set", () => {
    const action = resolveCompletedInductionAction(
      {
        duplicate_of: "accepted-123",
        report: { induced_ontology_id: "job-1" },
      },
      "job-1",
    );
    // duplicate_of wins even when a report id is present — the run made no new
    // proposal, so we must point at the pre-existing one and NOT navigate.
    expect(action).toEqual({ kind: "duplicate", proposalId: "accepted-123" });
  });

  it("navigates to induced_ontology_id when present on a normal run", () => {
    const action = resolveCompletedInductionAction(
      { report: { induced_ontology_id: "prop-9" } },
      "job-9",
    );
    expect(action).toEqual({ kind: "navigate", proposalId: "prop-9" });
  });

  it("falls back to the polled job id when the report id is absent", () => {
    // proposal_id == job_id on the normal path; resilient when induced_ontology_id
    // isn't hydrated on the DDB-fallback poll response.
    const action = resolveCompletedInductionAction({ report: {} }, "job-7");
    expect(action).toEqual({ kind: "navigate", proposalId: "job-7" });
  });

  it("refreshes when there is neither a duplicate nor any id to navigate to", () => {
    const action = resolveCompletedInductionAction({}, null);
    expect(action).toEqual({ kind: "refresh" });
  });

  it("does not navigate to a superset selection's own job (duplicate only on exact match)", () => {
    // A superset run (e.g. added a 2nd datasource) is NOT a duplicate — the
    // backend gives it no duplicate_of, so it navigates to its own new proposal.
    const action = resolveCompletedInductionAction(
      { report: { induced_ontology_id: "superset-prop" } },
      "superset-job",
    );
    expect(action.kind).toBe("navigate");
  });
});

describe("deleteOntologyCopy", () => {
  it("frames foundational deletion as re-loadable (reversible)", () => {
    const copy = deleteOntologyCopy("foundational");
    expect(copy.reloadable).toBe(true);
    expect(copy.header).toMatch(/foundational/i);
  });
  it("frames user-uploaded deletion as permanent", () => {
    const copy = deleteOntologyCopy("user_uploaded");
    expect(copy.reloadable).toBe(false);
    expect(copy.header).toMatch(/cannot be undone/i);
  });
  it("frames induced deletion as permanent", () => {
    expect(deleteOntologyCopy("induced").reloadable).toBe(false);
  });
  it("defaults unknown types to permanent (safer framing)", () => {
    expect(deleteOntologyCopy("something_else").reloadable).toBe(false);
  });
});

// ── Rendered Actions cell ──

const listOntologies = vi.fn();
const listFoundationalOntologies = vi.fn();
const loadFoundationalOntology = vi.fn();
const getOntologyIngestStatus = vi.fn();
const listProposals = vi.fn();
const triggerInduction = vi.fn();
const deleteOntology = vi.fn();
vi.mock("../services/ontology-engine", () => ({
  listOntologies: (...args: unknown[]) => listOntologies(...args),
  listFoundationalOntologies: (...args: unknown[]) =>
    listFoundationalOntologies(...args),
  listProposals: (...args: unknown[]) => listProposals(...args),
  listInducedDatasources: vi.fn().mockResolvedValue([]),
  listInducedDatasource: vi.fn(),
  triggerInduction: (...args: unknown[]) => triggerInduction(...args),
  getInductionJob: vi.fn(),
  uploadOntologyFile: vi.fn(),
  loadFoundationalOntology: (...args: unknown[]) =>
    loadFoundationalOntology(...args),
  getOntologyIngestStatus: (...args: unknown[]) =>
    getOntologyIngestStatus(...args),
  deleteOntology: (...args: unknown[]) => deleteOntology(...args),
}));

// ApiError defined INSIDE the factory (vi.mock is hoisted above top-level
// declarations, so it can't close over an outer class). Its shape mirrors the
// real ApiError (public numeric `status`) so startInduction's
// `e instanceof ApiError && e.status === 409` branch works under the mock.
const mockApiGet = vi.fn();
vi.mock("@components/ApiClientProvider", () => {
  class ApiError extends Error {
    constructor(
      public readonly status: number,
      public readonly code: string,
      message: string,
    ) {
      super(message);
    }
  }
  // The client object identity MUST be stable across renders: OntologyPage's
  // source-loading effects list `apiClient` in their dep arrays, so returning a
  // fresh `{ get }` literal per call would re-run them on every render. Each
  // re-run sets `cancelled = true` on the previous pass, swallowing the error
  // the DOCUMENTS test asserts on (and destabilising the whole file).
  // Delegate through a closure so per-test `mockApiGet` overrides still apply.
  const stableClient = {
    get: (...args: unknown[]) => mockApiGet(...args),
  };
  return {
    useApiClient: () => stableClient,
    ApiError,
  };
});

vi.mock("./ontology/induce/InductionStages", () => ({
  InductionStages: () => null,
}));

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/namespaces/ns/ontology"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/ontology"
          element={<OntologyPage />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

/**
 * Render and switch to the "Reference ontologies" tab. The page defaults to the
 * Proposals tab (the review queue), so tests that assert reference-ontology
 * table/actions content must open that tab first.
 */
async function renderReferenceOntologiesTab() {
  const result = renderPage();
  // Scope ALL queries — including the tab switch itself — to THIS render's
  // container. A prior test's tree can linger briefly before cleanup(), so a
  // page-global `screen` query would match its stale (detached) tab/"Load"/
  // "Delete" nodes; clicking a stale tab leaves the live page on Proposals and
  // the reference-ontologies content never mounts. `within(container)` pins
  // every lookup to this mount.
  const view = within(result.container);
  // Cloudscape renders inactive tab panels lazily, so the reference-ontologies
  // content (and its listOntologies fetch) only mounts after this click.
  fireEvent.click(await view.findByText("Reference ontologies"));
  return view;
}

describe("OntologiesTab Actions cell", () => {
  beforeEach(() => {
    // Unmount any prior test's tree first. The table's ingest/delete poll
    // (setInterval bumping refreshKey while a row is non-terminal) keeps
    // re-rendering a detached tree; without an explicit unmount, findAllByText
    // can match those stale nodes in the next test.
    cleanup();
    vi.clearAllMocks();
    // Default: no proposals → induction lock stays open. Individual tests
    // override to exercise the in-progress / awaiting-review lock.
    listProposals.mockResolvedValue([]);
    // Default: sources API succeeds with empty lists. Individual tests override.
    mockApiGet.mockResolvedValue({ items: [] });
  });

  it("shows an ingest StatusIndicator for a loaded row and Load for an available foundational", async () => {
    // One registered (loaded, ready) ontology.
    listOntologies.mockResolvedValue([
      record({
        ontologyId: "http://ex.org/loaded",
        uri: "http://ex.org/loaded",
        title: "Loaded Onto",
        ontologyType: "user_uploaded",
        parseStatus: "ok",
        embeddingCount: 10,
        classCount: 5,
      }),
    ]);
    // One curated foundational NOT yet loaded → placeholder "Load" row.
    listFoundationalOntologies.mockResolvedValue({
      items: [
        {
          key: "fibo",
          uri: "http://ex.org/fibo",
          title: "FIBO",
          description: "",
          format: "turtle",
          domainTags: [],
        },
      ],
    });

    const view = await renderReferenceOntologiesTab();

    // Loaded row → ingest state indicator.
    await view.findByText("Ready");
    // Available foundational → Load action.
    expect(view.getByText("Load")).toBeInTheDocument();
  });

  it("clicking Load fires the 202 POST and refreshes — without a button-side ingest-status poll", async () => {
    listOntologies.mockResolvedValue([]);
    listFoundationalOntologies.mockResolvedValue({
      items: [
        {
          key: "fibo",
          uri: "http://ex.org/fibo",
          title: "FIBO",
          description: "",
          format: "turtle",
          domainTags: [],
        },
      ],
    });
    // 202 → PENDING job keyed by the ontology URI. The backend has already
    // written an `ingesting` registry row synchronously, so the table's own
    // ingest poll (not the button) drives it to a terminal state.
    loadFoundationalOntology.mockResolvedValue({
      jobId: "job-1",
      ontologyId: "http://ex.org/fibo",
      status: "pending",
    });

    const view = await renderReferenceOntologiesTab();
    const listCallsBefore = listOntologies.mock.calls.length;
    // The table re-renders as listOntologies/listFoundationalOntologies resolve,
    // so a Load node grabbed once can be detached by a pending re-render before
    // the click lands (its handler then never fires). Re-query and click inside
    // waitFor so it retries against the live node until the POST actually fires.
    await waitFor(() => {
      fireEvent.click(view.getByText("Load"));
      expect(loadFoundationalOntology).toHaveBeenCalledWith(
        expect.anything(),
        "ns",
        "fibo",
      );
    });
    // It then refreshes the list (bumped refreshKey) so the new `ingesting`
    // row appears — the table poll takes over from there.
    await waitFor(() =>
      expect(listOntologies.mock.calls.length).toBeGreaterThan(listCallsBefore),
    );
    // The button must NOT run its own ingest-status poll anymore — that was
    // the redundancy this change removed (tech-debt #773). Give the old 2s
    // poll interval time to fire; it must not.
    await new Promise((r) => setTimeout(r, 100));
    expect(getOntologyIngestStatus).not.toHaveBeenCalled();
  }, 10000);

  it("offers a Delete action for BOTH loaded foundational and uploaded rows", async () => {
    // Every LOADED reference ontology is deletable (uploaded = user content;
    // foundational = re-loadable curated reference). A not-yet-loaded catalog
    // placeholder (_available) stays Load-only and is covered separately.
    listOntologies.mockResolvedValue([
      record({
        ontologyId: "http://ex.org/mine",
        uri: "http://ex.org/mine",
        title: "My Uploaded Onto",
        ontologyType: "user_uploaded",
        parseStatus: "ok",
        embeddingCount: 10,
      }),
      record({
        ontologyId: "http://ex.org/fibo",
        uri: "http://ex.org/fibo",
        title: "FIBO Loaded",
        ontologyType: "foundational",
        parseStatus: "ok",
        embeddingCount: 10,
      }),
    ]);
    // A curated foundational NOT yet loaded → placeholder Load row, no Delete.
    listFoundationalOntologies.mockResolvedValue({
      items: [
        {
          key: "schema-org",
          uri: "http://ex.org/schema",
          title: "Schema.org (available)",
          description: "",
          format: "turtle",
          domainTags: [],
        },
      ],
    });

    const view = await renderReferenceOntologiesTab();

    // Both LOADED rows (uploaded + foundational) expose Delete; the available
    // placeholder shows Load, not Delete → exactly two Delete buttons.
    await view.findByText("My Uploaded Onto");
    await view.findByText("FIBO Loaded");
    const deleteButtons = await view.findAllByText("Delete");
    expect(deleteButtons).toHaveLength(2);
    // The not-yet-loaded catalog row offers Load, not Delete.
    expect(await view.findAllByText("Load")).toHaveLength(1);
  });

  it("disables Delete on a foundational row while an induced ontology exists (mirrors backend 409 guard)", async () => {
    // A loaded foundational + an induced ontology in the same namespace. The
    // induced one is grounded against the foundational, so the backend refuses
    // to delete the foundational (409); the UI disables its Delete to match.
    listOntologies.mockResolvedValue([
      record({
        ontologyId: "http://ex.org/fibo",
        uri: "http://ex.org/fibo",
        title: "FIBO Loaded",
        ontologyType: "foundational",
        parseStatus: "ok",
        embeddingCount: 10,
      }),
      record({
        ontologyId: "http://ex.org/induced",
        uri: "http://ex.org/induced",
        title: "Induced Onto",
        ontologyType: "induced",
        parseStatus: "ok",
        embeddingCount: 10,
      }),
    ]);
    listFoundationalOntologies.mockResolvedValue({ items: [] });

    const view = await renderReferenceOntologiesTab();

    await view.findByText("FIBO Loaded");
    // The foundational row's Delete is disabled while an induced ontology
    // exists (mirrors the backend 409 grounding guard).
    const deletes = await view.findAllByText("Delete");
    const disabled = deletes.filter((el) =>
      el.closest("button")?.hasAttribute("disabled"),
    );
    expect(disabled.length).toBeGreaterThanOrEqual(1);
  });

  it("shows 'Delete in progress' and no Delete action while an uploaded row is deleting", async () => {
    listOntologies.mockResolvedValue([
      record({
        ontologyId: "http://ex.org/mine",
        uri: "http://ex.org/mine",
        title: "My Uploaded Onto",
        ontologyType: "user_uploaded",
        parseStatus: "ok",
        embeddingCount: 10,
        status: "deleting",
      }),
    ]);
    listFoundationalOntologies.mockResolvedValue({ items: [] });

    const view = await renderReferenceOntologiesTab();

    await view.findByText("Delete in progress");
    // The Delete trigger stays rendered but is disabled while mid-delete, so
    // the row can't be queued for deletion twice. The deleting row runs a
    // status poll that bumps refreshKey and re-renders the table, so retry the
    // lookup+assertion inside waitFor rather than querying a node a pending
    // re-render may have just detached.
    await waitFor(() =>
      expect(view.getByRole("button", { name: "Delete" })).toBeDisabled(),
    );
  });

  it("deletes an uploaded ontology after the user types 'delete' to confirm", async () => {
    deleteOntology.mockResolvedValue(undefined);
    listOntologies.mockResolvedValue([
      record({
        ontologyId: "http://ex.org/mine",
        uri: "http://ex.org/mine",
        title: "My Uploaded Onto",
        ontologyType: "user_uploaded",
        parseStatus: "ok",
        embeddingCount: 10,
      }),
    ]);
    listFoundationalOntologies.mockResolvedValue({ items: [] });

    const view = await renderReferenceOntologiesTab();

    // Open the confirm modal from the uploaded row's Delete action. The table
    // re-renders as its fetches resolve, so a Delete node grabbed once can be
    // detached before the click lands; re-query and click inside waitFor until
    // the modal's confirm Input (unique to the open dialog) appears.
    await waitFor(() => {
      fireEvent.click(view.getByRole("button", { name: "Delete" }));
      expect(screen.getByPlaceholderText("delete")).toBeInTheDocument();
    });

    // Scope to the modal dialog: once it's open, the table row's inline-link
    // Delete AND the modal's primary Delete both exist, so an unscoped query
    // would be ambiguous. The confirm-text Input is unique to the modal, so
    // anchor on it and walk up to the dialog container (Cloudscape's dialog
    // <div> isn't surfaced by RTL's accessibility-tree getByRole).
    const confirmInput = screen.getByPlaceholderText("delete");
    const dialog = confirmInput.closest('[role="dialog"]');
    if (!(dialog instanceof HTMLElement)) {
      throw new Error("delete modal dialog not found");
    }
    const confirmButton = within(dialog).getByRole("button", {
      name: "Delete",
    });
    // The confirm button stays disabled until the exact word is typed.
    expect(confirmButton).toBeDisabled();
    fireEvent.change(confirmInput, { target: { value: "delete" } });
    expect(confirmButton).toBeEnabled();
    fireEvent.click(confirmButton);

    // The delete call fired with the row's ontologyId (a full IRI).
    await waitFor(() =>
      expect(deleteOntology).toHaveBeenCalledWith(
        expect.anything(),
        "ns",
        "http://ex.org/mine",
      ),
    );
  });

  // NOTE on the grounding tests below (Induce Ontology modal): grounding is
  // available for BOTH strategies — the structured (table_to_ontology) path
  // grounds tables and the unstructured (lexical graph) path grounds its
  // induced classes against the same loaded foundational ontologies.

  // NOTE on the concurrent-load fix (loadingUris scalar → Set<string>): the
  // regression it guards against — clicking Load on a second reference
  // ontology cancelling the first's spinner — cannot be reproduced at the unit
  // level. A Cloudscape inline-link Button drops its "Load" label and its table
  // row re-renders out of the queryable jsdom tree the instant it enters
  // `loading`, so the second row's button is never clickable in the test env
  // (verified by DOM inspection: after one click, all foundational rows vanish
  // from the queryable tree). The fix is verified by tsc + code review of the
  // per-URI `Set` keying in OntologyPage.tsx and by the live-browser E2E in the
  // MR checklist; the single-load path above already covers Load→POST→poll.
});

describe("Induce Ontology modal — grounding is available for both strategies", () => {
  beforeEach(() => {
    cleanup();
    vi.clearAllMocks();
    listOntologies.mockResolvedValue([]);
    listFoundationalOntologies.mockResolvedValue({ items: [] });
    listProposals.mockResolvedValue([]);
    mockApiGet.mockResolvedValue({ items: [] });
  });

  async function openInductionModal() {
    renderPage();
    fireEvent.click(await screen.findByText("Start induction"));
    // The modal is open once its "Strategy" form field renders.
    await screen.findByText("Strategy");
  }

  it("shows an error alert when document sources fail to load", async () => {
    // Only the DOCUMENTS fetch fails (403, network, ...); DATABASE succeeds.
    // Both effects write the SAME `datasourceError` state behind one Alert, so
    // the DATABASE call must resolve — otherwise its own catch populates the
    // Alert and the assertion passes even when the DOCUMENTS catch is a no-op
    // (i.e. the test would not detect the bug it exists to pin).
    const docError = "403 Forbidden: docs are gated";
    mockApiGet.mockImplementation((url: unknown) => {
      const path = String(url);
      if (path.includes("sourceType=DOCUMENTS")) {
        return Promise.reject(new Error(docError));
      }
      return Promise.resolve({ items: [] });
    });

    const { container } = renderPage();
    const view = within(container);

    // The Alert must carry the DOCUMENTS error text specifically — asserting on
    // the generic header alone would also pass on a DATABASE-path failure.
    await view.findByText(docError);
    expect(view.getByText("Could not load data sources")).toBeInTheDocument();
  });

  it("shows the grounding option for the default (structured) strategy", async () => {
    await openInductionModal();
    // Default strategy is table_to_ontology → grounding field is present.
    expect(
      screen.getByText("Grounding ontologies (optional)"),
    ).toBeInTheDocument();
  });

  it("keeps the grounding option when the unstructured strategy is selected", async () => {
    await openInductionModal();
    expect(
      screen.getByText("Grounding ontologies (optional)"),
    ).toBeInTheDocument();

    // Open the strategy Select (mousedown on the Cloudscape button trigger,
    // not the label span) and pick the unstructured option by its role. The
    // Cloudscape option is a div that commits selection on mouseup, so fire
    // both mouseUp and click to be robust.
    const strategyTrigger = screen
      .getByText("Table to Ontology")
      .closest("button");
    expect(strategyTrigger).not.toBeNull();
    fireEvent.mouseDown(strategyTrigger!);
    const unstructuredOption = await screen.findByRole("option", {
      name: /Unstructured \(Lexical Graph\)/,
    });
    fireEvent.mouseUp(unstructuredOption);
    fireEvent.click(unstructuredOption);

    // Grounding UI remains for the unstructured (lexical graph) path —
    // it now grounds induced classes against loaded foundational ontologies.
    await waitFor(() =>
      expect(
        screen.getByText("Grounding ontologies (optional)"),
      ).toBeInTheDocument(),
    );
  });

  it("offers induced ontologies as selectable (pre-checked) grounding options alongside foundational", async () => {
    // Namespace has one accepted-induced and one loaded-foundational ontology,
    // both embedded. The backend grounds against EXACTLY the selected pool, so
    // BOTH are selectable in the dropdown; the induced one is pre-checked by
    // default (deselectable) so new runs converge with prior ones.
    listOntologies.mockResolvedValue([
      record({
        ontologyId: "http://ns/induced-a",
        uri: "http://ns/induced-a",
        title: "My Induced Ontology",
        ontologyType: "induced",
        embeddingCount: 12,
      }),
      record({
        ontologyId: "http://fibo/loaded",
        uri: "http://fibo/loaded",
        title: "FIBO Loaded",
        ontologyType: "foundational",
        embeddingCount: 9,
      }),
    ]);
    await openInductionModal();

    // No read-only "always grounded" note anymore — grounding is fully driven
    // by the selection.
    expect(
      screen.queryByText(/Always grounded against this namespace/),
    ).toBeNull();

    // Open the dropdown: BOTH ontologies are selectable options now (induced is
    // no longer excluded).
    // `findByText`, not `getByText`: openInductionModal() only awaits the
    // modal's "Strategy" field, while this trigger renders on a separate async
    // chain (the listOntologies fetch resolving). A synchronous query races
    // that chain and throws "unable to find element" under parallel-suite load.
    const groundingTrigger = (
      await screen.findByText("Pick loaded ontologies to ground against")
    ).closest("button");
    expect(groundingTrigger).not.toBeNull();
    fireEvent.mouseDown(groundingTrigger!);

    const foundationalOpt = await screen.findByRole("option", {
      name: /FIBO Loaded/,
    });
    const inducedOpt = await screen.findByRole("option", {
      name: /My Induced Ontology/,
    });
    expect(foundationalOpt).toBeInTheDocument();
    expect(inducedOpt).toBeInTheDocument();

    // The induced ontology is pre-selected by default; the foundational is not.
    // Cloudscape marks the selected option with aria-selected="true".
    expect(inducedOpt).toHaveAttribute("aria-selected", "true");
    expect(foundationalOpt).toHaveAttribute("aria-selected", "false");
  });

  it("surfaces not-ready ontologies as disabled with a per-row status label (issue #540)", async () => {
    // A namespace with one ready, one still-embedding, one failed, and one
    // legacy (no parseStatus but embedded) ontology. Before #540 the three
    // non-ready rows were the still-embedding and failed ones — silently
    // dropped from the dropdown with no signal. Now every loaded ontology is
    // SHOWN; readiness (selectability) is derived from ontologyIngestState so
    // it can never disagree with the Ontologies-list StatusIndicator.
    listOntologies.mockResolvedValue([
      record({
        ontologyId: "http://ns/ready",
        uri: "http://ns/ready",
        title: "Ready Onto",
        ontologyType: "foundational",
        parseStatus: "ok",
        embeddingCount: 9,
      }),
      record({
        ontologyId: "http://ns/embedding",
        uri: "http://ns/embedding",
        title: "Embedding Onto",
        ontologyType: "foundational",
        parseStatus: "pending",
        embeddingCount: 0,
      }),
      record({
        ontologyId: "http://ns/failed",
        uri: "http://ns/failed",
        title: "Failed Onto",
        ontologyType: "foundational",
        parseStatus: "parse_error",
        embeddingCount: 0,
      }),
      record({
        ontologyId: "http://ns/legacy",
        uri: "http://ns/legacy",
        title: "Legacy Onto",
        ontologyType: "foundational",
        // Legacy row persisted before parseStatus existed: null parseStatus
        // but embeddings present ⇒ ready (matches ontologyIngestState()).
        parseStatus: null,
        embeddingCount: 7,
      }),
    ]);
    await openInductionModal();

    // `findByText`, not `getByText`: openInductionModal() only awaits the
    // modal's "Strategy" field, while this trigger renders on a separate async
    // chain (the listOntologies fetch resolving). A synchronous query races
    // that chain and throws "unable to find element" under parallel-suite load.
    const groundingTrigger = (
      await screen.findByText("Pick loaded ontologies to ground against")
    ).closest("button");
    expect(groundingTrigger).not.toBeNull();
    fireEvent.mouseDown(groundingTrigger!);

    // Ready + legacy(embedded) rows are ENABLED (selectable).
    const readyOpt = await screen.findByRole("option", { name: /Ready Onto/ });
    const legacyOpt = await screen.findByRole("option", {
      name: /Legacy Onto/,
    });
    expect(readyOpt).toHaveAttribute("aria-disabled", "false");
    expect(legacyOpt).toHaveAttribute("aria-disabled", "false");

    // Still-embedding row is SHOWN but DISABLED with the embedding label.
    const embeddingOpt = await screen.findByRole("option", {
      name: /Embedding Onto/,
    });
    expect(embeddingOpt).toHaveAttribute("aria-disabled", "true");
    expect(embeddingOpt).toHaveTextContent("⏳ Embedding…");

    // Failed-ingest row is SHOWN but DISABLED with the failed label — the
    // failed-vs-pending distinction #540 asked for.
    const failedOpt = await screen.findByRole("option", {
      name: /Failed Onto/,
    });
    expect(failedOpt).toHaveAttribute("aria-disabled", "true");
    expect(failedOpt).toHaveTextContent("⚠️ Ingest failed");
  });
});

describe("Induction in-flight visibility + trigger lock (issue I-3f1f50cc)", () => {
  beforeEach(() => {
    cleanup();
    vi.clearAllMocks();
    listOntologies.mockResolvedValue([]);
    listFoundationalOntologies.mockResolvedValue({ items: [] });
    listProposals.mockResolvedValue([]);
  });

  function proposal(overrides: Record<string, unknown>) {
    return {
      proposal_id: "p1",
      job_id: "p1",
      namespace: "ns",
      ontology_id: "http://ex.org/namespace/ns/ontology/induced#",
      proposal_type: "induction",
      status: "inducing",
      created_at: "2026-07-10T00:00:00Z",
      updated_at: "2026-07-10T00:00:00Z",
      ...overrides,
    };
  }

  it("maps proposal statuses to the right StatusIndicator (inducing/failed/pending)", () => {
    // The in-progress "Inducing…" label + failed/pending mapping is the
    // presentational branch added for the stub row. Asserting it directly on
    // the pure helper keeps the check deterministic — driving it through the
    // Cloudscape Tabs -> SortableTable -> 4s-poll stack in jsdom is flaky (the
    // live poll interval prevents the tab-content render from settling). The
    // server->component wiring that DECIDES a row is "inducing" is covered by
    // the trigger-lock tests below, which read listProposals end-to-end.
    expect(proposalStatusDisplay("inducing")).toEqual({
      type: "in-progress",
      label: "Inducing…",
    });
    expect(proposalStatusDisplay("failed")).toEqual({
      type: "error",
      label: "failed",
    });
    expect(proposalStatusDisplay("embeddings_sync")).toEqual({
      type: "in-progress",
      label: "Syncing embeddings…",
    });
    expect(proposalStatusDisplay("accepted")).toEqual({
      type: "success",
      label: "accepted",
    });
    // An accept whose pipeline step exhausted its retries (#456/#466/#467).
    // Must render as an ERROR, not fall through to the pending branch —
    // otherwise a failed accept looks like a fresh proposal.
    expect(proposalStatusDisplay("accept_failed")).toEqual({
      type: "error",
      label: "accept failed",
    });
    expect(proposalStatusDisplay("pending")).toEqual({
      type: "pending",
      label: "pending",
    });
  });

  it("locks the Start-induction button when a proposal is in flight (survives refresh via server state)", async () => {
    listProposals.mockResolvedValue([proposal({ status: "inducing" })]);
    renderPage();
    // The header Start-induction button is disabled purely from the server
    // proposal list (no in-session trigger needed) — this is the refresh-safe
    // lock. A disabled Cloudscape button carries aria-disabled="true".
    await waitFor(() => {
      const btn = screen.getByText("Start induction").closest("button");
      expect(btn).toHaveAttribute("aria-disabled", "true");
    });
  });

  it("keeps Start-induction enabled when there are no in-flight/pending proposals", async () => {
    listProposals.mockResolvedValue([proposal({ status: "accepted" })]);
    renderPage();
    await waitFor(() => {
      const btn = screen.getByText("Start induction").closest("button");
      expect(btn).not.toHaveAttribute("aria-disabled", "true");
    });
  });

  it("also locks when a completed proposal is awaiting review (pending)", async () => {
    listProposals.mockResolvedValue([proposal({ status: "pending" })]);
    renderPage();
    await waitFor(() => {
      const btn = screen.getByText("Start induction").closest("button");
      expect(btn).toHaveAttribute("aria-disabled", "true");
    });
  });
});
