// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LocaleProvider } from "../i18n/LocaleProvider";
import { SourceDetail } from "./SourceDetail";

// The Glue settings panel's empty-filter placeholder and the "filter had no
// effect" alert header go through the catalogue.

const mockGetSource = vi.hoisted(() => vi.fn());
const mockGetSourceScanJob = vi.hoisted(() => vi.fn());

vi.mock("@api-hooks", () => ({
  useGetSource: mockGetSource,
  useDeleteSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRescanSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useListSourceTables: () => ({ data: { items: [] }, isLoading: false }),
  useApproveSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRejectSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useGetSourceScanJob: mockGetSourceScanJob,
  useKeepRescanRemoval: () => ({ mutate: vi.fn(), isPending: false }),
  // The settings panel this test renders also hosts the rescan controls.
  usePutSourceRescanSchedule: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: null,
    isSuccess: false,
  }),
  usePutSourceEventRescan: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: null,
  }),
}));

vi.mock("@coa/control-plane-client", () => ({
  ReviewDecision: { APPROVED: "APPROVED", REJECTED: "REJECTED" },
  ReviewStatus: {
    APPROVED: "APPROVED",
    PENDING_REVIEW: "PENDING_REVIEW",
    REJECTED: "REJECTED",
  },
  SourceStatus: {
    PENDING_REVIEW: "PENDING_REVIEW",
    APPROVED: "APPROVED",
    APPROVING: "APPROVING",
    REJECTING: "REJECTING",
    SCANNING: "SCANNING",
    ENRICHING: "ENRICHING",
    SCAN_FAILED: "SCAN_FAILED",
    APPROVAL_FAILED: "APPROVAL_FAILED",
    REJECTION_FAILED: "REJECTION_FAILED",
  },
  ReviewSourceTableCommand: class {
    input: Record<string, unknown>;
    constructor(input: Record<string, unknown>) {
      this.input = input;
    }
  },
}));

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: vi.fn() }),
}));

function renderIn(locale: "en" | "ja") {
  window.localStorage.setItem("scl.locale", locale);
  return render(
    <LocaleProvider>
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/namespaces/ns-1/sources/src-1"]}>
          <Routes>
            <Route
              path="/namespaces/:namespaceId/sources/:sourceId"
              element={<SourceDetail />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </LocaleProvider>,
  );
}

const user = userEvent.setup({ delay: null });

function setGlueSource() {
  mockGetSource.mockReturnValue({
    data: {
      body: {
        sourceId: "src-1",
        name: "Sales Glue",
        sourceType: "DATABASE",
        sourceSubType: "GLUE_DATABASE",
        status: "PENDING_REVIEW",
        createdAt: "2026-01-01T00:00:00Z",
        databaseDetails: {
          tablesDiscovered: 5,
          tablesApproved: 0,
          lastScanAt: "2026-01-02T00:00:00Z",
          lastScanJobId: "2026-01-02T00:00:00Z",
          glueConfiguration: { databaseName: "sales" },
        },
      },
    },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  });
}

describe("SourceDetail — localized filter text", () => {
  beforeEach(() => {
    mockGetSource.mockReset();
    mockGetSourceScanJob.mockReset();
    mockGetSourceScanJob.mockReturnValue({
      data: {
        status: "COMPLETED",
        filterWarnings: ["The table filter 'orders.*' matched no tables."],
      },
    });
  });

  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = "";
  });

  it("shows the empty Glue table filter and the filter alert in Japanese", async () => {
    setGlueSource();
    renderIn("ja");
    expect(
      await screen.findByText(
        "前回のスキャンで効果がなかったフィルターがあります",
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "設定" }));
    expect(screen.getByText("（なし）")).toBeInTheDocument();
    expect(screen.queryByText("(none)")).not.toBeInTheDocument();
  });

  it("keeps the English text in English", async () => {
    setGlueSource();
    renderIn("en");
    expect(
      await screen.findByText("A filter had no effect on the last scan"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /settings/i }));
    expect(screen.getByText("(none)")).toBeInTheDocument();
  });
});
