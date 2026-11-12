// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, it, expect, vi } from "vitest";
import { listAllProposals } from "../../services/ontology-engine";
import { PendingReviewBanner } from "./PendingReviewBanner";

const { apiClient } = vi.hoisted(() => ({ apiClient: {} }));

vi.mock("@components/ApiClientProvider", () => ({
  useApiClient: () => apiClient,
}));

vi.mock("../../services/ontology-engine", () => ({
  listAllProposals: vi
    .fn()
    .mockResolvedValue([{ proposal_id: "p1", status: "pending" }]),
}));

function renderWithRouter() {
  return render(
    <MemoryRouter initialEntries={["/namespaces/ns1/ontology"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/ontology"
          element={<PendingReviewBanner />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("PendingReviewBanner", () => {
  beforeEach(() => {
    vi.mocked(listAllProposals).mockClear();
  });

  it("renders banner when there are pending proposals", async () => {
    renderWithRouter();
    expect(await screen.findByText(/awaiting review/)).toBeTruthy();
    expect(screen.getByText("Review proposal")).toBeTruthy();
    expect(listAllProposals).toHaveBeenCalledTimes(1);
  });
});
