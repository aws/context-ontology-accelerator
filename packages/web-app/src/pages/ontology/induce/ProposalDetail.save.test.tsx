// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@components/ApiClientProvider";
import type { Proposal } from "../../../services/ontology-engine";
import { ProposalDetailPage } from "./ProposalDetail";

const serviceMocks = vi.hoisted(() => ({
  getProposal: vi.fn(),
  getProposalUploadUrls: vi.fn(),
  putToPresignedUrl: vi.fn(),
  updateProposal: vi.fn(),
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
  },
}));

vi.mock("../../../services/ontology-engine", () => ({
  acceptProposal: vi.fn(),
  cancelProposal: vi.fn(),
  compileConstraints: vi.fn(),
  fetchJsonFromUrl: vi.fn(),
  fetchTurtleFromUrl: vi.fn(),
  getInferConstraintsJob: vi.fn(),
  getProposal: serviceMocks.getProposal,
  getProposalUploadUrls: serviceMocks.getProposalUploadUrls,
  getProposalValidationJob: vi.fn(),
  inferConstraints: vi.fn(),
  putToPresignedUrl: serviceMocks.putToPresignedUrl,
  repairProposalDatatypes: vi.fn(),
  updateProposal: serviceMocks.updateProposal,
  validateProposal: vi.fn(),
}));

// The page narrows save failures with `instanceof ApiError`, so the mock has to
// export a class rather than a stub — otherwise every failure falls through to
// the generic branch and the status-specific assertions below pass vacuously.
vi.mock("@components/ApiClientProvider", () => {
  class ApiError extends Error {
    constructor(
      public readonly status: number,
      public readonly code: string,
      message: string,
    ) {
      super(message);
      this.name = "ApiError";
    }
  }
  return { ApiError, useApiClient: () => serviceMocks.apiClient };
});

vi.mock("@components/NamespaceProvider", () => ({
  useCurrentNamespace: () => ({ current: undefined }),
}));

vi.mock("@components/graph/OntologyGraphFlow", () => ({
  OntologyGraphFlow: () => null,
}));

vi.mock("@components/graph/turtleToGraph", () => ({
  turtleToGraph: () => ({ nodes: [], edges: [] }),
}));

vi.mock("./ConstraintPanel", () => ({
  ConstraintPanel: () => null,
}));

interface ProposalEntitiesPanelStubProps {
  readonly fullTurtle?: string | null;
  readonly onUpdateTurtle?: (updatedTurtle: string) => void;
  readonly onMarkGroundingNovel?: (table: string) => void;
}

vi.mock("./ProposalEntitiesPanel", () => ({
  ProposalEntitiesPanel: ({
    fullTurtle,
    onUpdateTurtle,
    onMarkGroundingNovel,
  }: ProposalEntitiesPanelStubProps) => (
    <div>
      <button type="button" onClick={() => onMarkGroundingNovel?.("orders")}>
        Mark grounding novel
      </button>
      <button
        type="button"
        onClick={() => onUpdateTurtle?.(`${fullTurtle ?? ""}\n# edited`)}
      >
        Edit Turtle
      </button>
    </div>
  ),
}));

const ONTOLOGY_TURTLE = `@prefix ind: <https://example.org/ind#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

ind:Order a owl:Class ;
    rdfs:label "Order" .
`;

const R2RML_TURTLE = `@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ind: <https://example.org/ind#> .

<#OrdersMap> a rr:TriplesMap ;
    rr:subjectMap [ rr:class ind:Order ] .
`;

const PROPOSAL: Proposal = {
  proposal_id: "p-edit",
  job_id: "j-edit",
  namespace: "ns",
  ontology_id: "https://example.org/ind#",
  proposal_type: "induction",
  status: "pending",
  created_at: "2026-08-18T00:00:00Z",
  updated_at: "2026-08-18T00:00:00Z",
  metadata: { matches: [] },
  ontology_turtle: ONTOLOGY_TURTLE,
};

function renderProposalDetail() {
  return render(
    <MemoryRouter initialEntries={["/namespaces/ns/proposals/p-edit"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/proposals/:proposalId"
          element={<ProposalDetailPage />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ProposalDetailPage save safety", () => {
  const uploadUrls = {
    revisionId: "d3bc58e9-52a4-43cf-b8b2-44a2f05b43f8",
    ontologyPutUrl: "https://s3.test/ontology",
    r2rmlPutUrl: "https://s3.test/r2rml",
  };

  beforeEach(() => {
    vi.clearAllMocks();
    serviceMocks.getProposal.mockResolvedValue(PROPOSAL);
    serviceMocks.getProposalUploadUrls.mockResolvedValue(uploadUrls);
    serviceMocks.putToPresignedUrl.mockResolvedValue(undefined);
    serviceMocks.updateProposal.mockResolvedValue({
      proposal_id: "p-edit",
      status: "updated",
    });
  });

  it("serializes mark-novel as novel_tables without a null map value", async () => {
    const user = userEvent.setup();
    renderProposalDetail();

    await user.click(
      await screen.findByRole("button", { name: "Mark grounding novel" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(serviceMocks.getProposalUploadUrls).not.toHaveBeenCalled();
    expect(serviceMocks.updateProposal).toHaveBeenCalledWith(
      serviceMocks.apiClient,
      "ns",
      "p-edit",
      { novel_tables: ["orders"] },
    );
  });

  it("commits a Turtle upload with the immutable revision id", async () => {
    const user = userEvent.setup();
    renderProposalDetail();

    await user.click(
      await screen.findByRole("button", { name: "Edit Turtle" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(serviceMocks.putToPresignedUrl).toHaveBeenCalledWith(
      "https://s3.test/ontology",
      `${ONTOLOGY_TURTLE}\n# edited`,
    );
    expect(serviceMocks.updateProposal).toHaveBeenCalledWith(
      serviceMocks.apiClient,
      "ns",
      "p-edit",
      {
        ontology_uploaded: true,
        upload_revision_id: "d3bc58e9-52a4-43cf-b8b2-44a2f05b43f8",
      },
    );
  });

  it("rejects mixed grounding and Turtle edits before requesting upload URLs", async () => {
    const user = userEvent.setup();
    renderProposalDetail();

    await user.click(
      await screen.findByRole("button", { name: "Mark grounding novel" }),
    );
    await user.click(screen.getByRole("button", { name: "Edit Turtle" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText(
        "Grounding overrides and Turtle edits must be saved separately",
      ),
    ).toBeInTheDocument();
    expect(serviceMocks.getProposalUploadUrls).not.toHaveBeenCalled();
    expect(serviceMocks.putToPresignedUrl).not.toHaveBeenCalled();
    expect(serviceMocks.updateProposal).not.toHaveBeenCalled();
  });

  it("can discard grounding choices and then save the Turtle edit", async () => {
    const user = userEvent.setup();
    renderProposalDetail();

    await user.click(
      await screen.findByRole("button", { name: "Mark grounding novel" }),
    );
    await user.click(screen.getByRole("button", { name: "Edit Turtle" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(
      await screen.findByText(
        "Grounding overrides and Turtle edits must be saved separately",
      ),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Discard grounding choices" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(serviceMocks.putToPresignedUrl).toHaveBeenCalledWith(
      "https://s3.test/ontology",
      `${ONTOLOGY_TURTLE}\n# edited`,
    );
    expect(serviceMocks.updateProposal).toHaveBeenCalledWith(
      serviceMocks.apiClient,
      "ns",
      "p-edit",
      {
        ontology_uploaded: true,
        upload_revision_id: "d3bc58e9-52a4-43cf-b8b2-44a2f05b43f8",
      },
    );
  });

  it("can discard Turtle edits and then save the grounding choice", async () => {
    const user = userEvent.setup();
    renderProposalDetail();

    await user.click(
      await screen.findByRole("button", { name: "Mark grounding novel" }),
    );
    await user.click(screen.getByRole("button", { name: "Edit Turtle" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(
      await screen.findByText(
        "Grounding overrides and Turtle edits must be saved separately",
      ),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Discard Turtle edits" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(serviceMocks.getProposalUploadUrls).not.toHaveBeenCalled();
    expect(serviceMocks.putToPresignedUrl).not.toHaveBeenCalled();
    expect(serviceMocks.updateProposal).toHaveBeenCalledWith(
      serviceMocks.apiClient,
      "ns",
      "p-edit",
      { novel_tables: ["orders"] },
    );
  });

  it("tells the user a 503 save is safe to repeat", async () => {
    const user = userEvent.setup();
    serviceMocks.updateProposal.mockRejectedValue(
      new ApiError(
        503,
        "SERVICE_UNAVAILABLE",
        "Ontology store is temporarily unavailable.",
      ),
    );
    renderProposalDetail();

    await user.click(
      await screen.findByRole("button", { name: "Mark grounding novel" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText(
        "Ontology store is temporarily unavailable. Nothing was saved, so you can retry this save as-is.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps the class name from a 409 and says a plain retry will not help", async () => {
    const user = userEvent.setup();
    serviceMocks.updateProposal.mockRejectedValue(
      new ApiError(
        409,
        "CONFLICT",
        "Grounding override could not be applied: no class matching '請求明細' in ontology Turtle",
      ),
    );
    renderProposalDetail();

    await user.click(
      await screen.findByRole("button", { name: "Mark grounding novel" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText(
        "The server refused this save: Grounding override could not be applied: " +
          "no class matching '請求明細' in ontology Turtle " +
          "Retrying unchanged will fail the same way.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps an upload failure visible and stops before UpdateProposal", async () => {
    const user = userEvent.setup();
    const uploadError =
      "The proposal revision already has an upload or the write outcome conflicted (412 Precondition Failed). " +
      "Save again to create a fresh revision and re-upload.";
    serviceMocks.putToPresignedUrl.mockRejectedValue(new Error(uploadError));
    renderProposalDetail();

    await user.click(
      await screen.findByRole("button", { name: "Edit Turtle" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText(uploadError)).toBeInTheDocument();
    expect(serviceMocks.updateProposal).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
  });

  it("retries both changed artifacts with a fresh revision after a partial upload", async () => {
    const user = userEvent.setup();
    const firstUploadUrls = {
      revisionId: "d3bc58e9-52a4-43cf-b8b2-44a2f05b43f8",
      ontologyPutUrl: "https://s3.test/revision-one/ontology",
      r2rmlPutUrl: "https://s3.test/revision-one/r2rml",
    };
    const retryUploadUrls = {
      revisionId: "7f3aa47f-1c97-482a-8a9d-a798ec37584e",
      ontologyPutUrl: "https://s3.test/revision-two/ontology",
      r2rmlPutUrl: "https://s3.test/revision-two/r2rml",
    };
    const uploadError = "Network error while uploading the R2RML artifact";
    serviceMocks.getProposal.mockResolvedValue({
      ...PROPOSAL,
      r2rml_turtle: R2RML_TURTLE,
    });
    serviceMocks.getProposalUploadUrls
      .mockResolvedValueOnce(firstUploadUrls)
      .mockResolvedValueOnce(retryUploadUrls);
    serviceMocks.putToPresignedUrl
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error(uploadError))
      .mockResolvedValueOnce(undefined)
      .mockResolvedValueOnce(undefined);
    renderProposalDetail();

    await user.click(
      await screen.findByRole("button", { name: "Edit Turtle" }),
    );
    const editButtons = await screen.findAllByRole("button", { name: "Edit" });
    await user.click(editButtons[0]);
    await user.type(await screen.findByLabelText("R2RML Turtle"), "\n# edited");
    const editedOntology = `${ONTOLOGY_TURTLE}\n# edited`;
    const editedR2rml = `${R2RML_TURTLE}\n# edited`;

    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText(uploadError)).toBeInTheDocument();
    expect(serviceMocks.getProposalUploadUrls).toHaveBeenCalledTimes(1);
    expect(serviceMocks.putToPresignedUrl).toHaveBeenNthCalledWith(
      1,
      firstUploadUrls.ontologyPutUrl,
      editedOntology,
    );
    expect(serviceMocks.putToPresignedUrl).toHaveBeenNthCalledWith(
      2,
      firstUploadUrls.r2rmlPutUrl,
      editedR2rml,
    );
    expect(serviceMocks.updateProposal).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() =>
      expect(serviceMocks.updateProposal).toHaveBeenCalledTimes(1),
    );
    expect(serviceMocks.getProposalUploadUrls).toHaveBeenCalledTimes(2);
    expect(serviceMocks.putToPresignedUrl).toHaveBeenNthCalledWith(
      3,
      retryUploadUrls.ontologyPutUrl,
      editedOntology,
    );
    expect(serviceMocks.putToPresignedUrl).toHaveBeenNthCalledWith(
      4,
      retryUploadUrls.r2rmlPutUrl,
      editedR2rml,
    );
    expect(serviceMocks.updateProposal).toHaveBeenCalledWith(
      serviceMocks.apiClient,
      "ns",
      "p-edit",
      {
        ontology_uploaded: true,
        r2rml_uploaded: true,
        upload_revision_id: retryUploadUrls.revisionId,
      },
    );
  });

  it("leaves a non-API failure message untouched", async () => {
    const user = userEvent.setup();
    serviceMocks.updateProposal.mockRejectedValue(
      new Error("Failed to PUT the ontology artifact"),
    );
    renderProposalDetail();

    await user.click(
      await screen.findByRole("button", { name: "Mark grounding novel" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText("Failed to PUT the ontology artifact"),
    ).toBeInTheDocument();
  });
});
