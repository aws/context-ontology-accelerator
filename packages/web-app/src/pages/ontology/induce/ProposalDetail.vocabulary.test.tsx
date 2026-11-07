// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  SplitPanelProvider,
  useSplitPanel,
} from "@components/SplitPanelProvider";
import type { Proposal } from "../../../services/ontology-engine";
import { ProposalDetailPage } from "./ProposalDetail";
import { parseVocabulary } from "./vocabulary";

const serviceMocks = vi.hoisted(() => ({
  getProposal: vi.fn(),
  getProposalUploadUrls: vi.fn(),
  putToPresignedUrl: vi.fn(),
  updateProposal: vi.fn(),
  apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn() },
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

vi.mock("@components/ApiClientProvider", () => ({
  ApiError: class ApiError extends Error {},
  useApiClient: () => serviceMocks.apiClient,
}));

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

const IND = "https://example.org/ind#";

const ONTOLOGY_TURTLE = `@prefix ind: <${IND}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

ind:Order a owl:Class ;
    rdfs:label "注文"@ja ;
    skos:altLabel "受注"@ja, "オーダー"@ja .
`;

const PROPOSAL: Proposal = {
  proposal_id: "p-vocab",
  job_id: "j-vocab",
  namespace: "ns",
  ontology_id: IND,
  proposal_type: "induction",
  status: "pending",
  created_at: "2026-11-05T00:00:00Z",
  updated_at: "2026-11-05T00:00:00Z",
  metadata: { matches: [] },
  ontology_turtle: ONTOLOGY_TURTLE,
};

/** Renders the split panel the way the app layout does. */
function SplitPanelOutlet() {
  const { state } = useSplitPanel();
  return state.isOpen ? (
    <aside aria-label="Side panel">{state.content}</aside>
  ) : null;
}

function renderPage() {
  return render(
    <SplitPanelProvider>
      <MemoryRouter initialEntries={["/namespaces/ns/proposals/p-vocab"]}>
        <Routes>
          <Route
            path="/namespaces/:namespaceId/proposals/:proposalId"
            element={<ProposalDetailPage />}
          />
        </Routes>
        <SplitPanelOutlet />
      </MemoryRouter>
    </SplitPanelProvider>,
  );
}

describe("ProposalDetailPage vocabulary review", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    serviceMocks.getProposal.mockResolvedValue(PROPOSAL);
    serviceMocks.getProposalUploadUrls.mockResolvedValue({
      revisionId: "0f1b8a4c-4a52-4f59-9c3b-0c3f62b3d0a1",
      ontologyPutUrl: "https://s3.test/ontology",
      r2rmlPutUrl: "https://s3.test/r2rml",
    });
    serviceMocks.putToPresignedUrl.mockResolvedValue(undefined);
    serviceMocks.updateProposal.mockResolvedValue({
      proposal_id: "p-vocab",
      status: "updated",
    });
  });

  it("reviews a class's vocabulary in its side panel, marks the page dirty, and saves", async () => {
    const user = userEvent.setup();
    renderPage();

    const classCell = (await screen.findAllByText("注文")).find((element) =>
      element.closest("tr"),
    );
    expect(classCell).toBeDefined();
    expect(screen.queryByText(/^Business vocabulary/)).toBeNull();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();

    await user.click(classCell!);
    const sidePanel = await screen.findByRole("complementary", {
      name: "Side panel",
    });
    expect(within(sidePanel).getByText("Business vocabulary (1)")).toBeTruthy();

    await user.click(
      within(sidePanel).getByRole("button", {
        name: "Reject variant 受注 @ja",
      }),
    );

    expect(
      await screen.findByText("Unsaved edits. Click Save changes to upload."),
    ).toBeTruthy();
    const save = screen.getByRole("button", { name: "Save changes" });
    expect(save).toBeEnabled();

    await user.click(save);

    await waitFor(() =>
      expect(serviceMocks.updateProposal).toHaveBeenCalledWith(
        serviceMocks.apiClient,
        "ns",
        "p-vocab",
        {
          ontology_uploaded: true,
          upload_revision_id: "0f1b8a4c-4a52-4f59-9c3b-0c3f62b3d0a1",
        },
      ),
    );
    expect(serviceMocks.putToPresignedUrl).toHaveBeenCalledOnce();
    const [url, saved] = serviceMocks.putToPresignedUrl.mock.calls[0];
    expect(url).toBe("https://s3.test/ontology");
    const order = parseVocabulary(saved as string).find(
      (concept) => concept.id === `${IND}Order`,
    );
    expect(
      order?.preferredLabels.map((l) => `${l.value}@${l.language}`),
    ).toEqual(["注文@ja"]);
    expect(
      order?.alternativeLabels.map((l) => `${l.value}@${l.language}`),
    ).toEqual(["オーダー@ja"]);
  });
});
