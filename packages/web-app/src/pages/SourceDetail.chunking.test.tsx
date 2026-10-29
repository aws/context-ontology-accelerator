// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi } from "vitest";
import { SourceDetail } from "./SourceDetail";

const mockGetSource = vi.hoisted(() => vi.fn());

vi.mock("@api-hooks", () => ({
  useGetSource: mockGetSource,
  useDeleteSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRescanSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useListSourceTables: () => ({ data: { items: [] }, isLoading: false }),
  useApproveSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRejectSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useGetSourceScanJob: () => ({ data: undefined }),
  useListSourceScanJobs: () => ({ data: undefined }),
  useKeepRescanRemoval: () => ({ mutate: vi.fn(), isPending: false }),
  usePutSourceRescanSchedule: () => ({ mutate: vi.fn(), isPending: false }),
  usePutSourceEventRescan: () => ({ mutate: vi.fn(), isPending: false }),
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
    RESCAN_REVIEW: "RESCAN_REVIEW",
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

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter initialEntries={["/namespaces/ns-1/sources/src-1"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/sources/:sourceId"
          element={children}
        />
      </Routes>
    </MemoryRouter>
  </QueryClientProvider>
);

function renderWithExtractionConfig(extractionConfig: Record<string, unknown>) {
  mockGetSource.mockReturnValue({
    data: {
      body: {
        sourceId: "src-1",
        name: "Docs",
        sourceType: "DOCUMENTS",
        status: "APPROVED",
        createdAt: "2026-01-01T00:00:00Z",
        documentDetails: { extractionConfig },
      },
    },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  });
  render(<SourceDetail />, { wrapper });
}

// The Advanced options card is the last place a user can confirm the chunk
// size they picked when connecting the source (#1286). ValueWithLabel renders
// <div><label/><div>{value}</div></div>, so the value is the second child of the
// div whose first child is the label.
function valueOf(label: string): string {
  const field = Array.from(document.querySelectorAll("div")).find(
    (el) => el.children.length === 2 && el.children[0]?.textContent === label,
  );
  if (!field) throw new Error(`no field for ${label}`);
  return field.children[1]?.textContent ?? "";
}

describe("SourceDetail chunking", () => {
  it("shows the toolkit defaults when the source set no chunk size", () => {
    renderWithExtractionConfig({ enableVersioning: false });
    expect(valueOf("Chunk size (tokens)")).toBe("Default (256 tokens)");
    expect(valueOf("Chunk overlap (tokens)")).toBe("Default (25 tokens)");
  });

  it("shows the chunk size and overlap the source set", () => {
    renderWithExtractionConfig({ chunkSize: 1024, chunkOverlap: 100 });
    expect(valueOf("Chunk size (tokens)")).toBe("1024");
    expect(valueOf("Chunk overlap (tokens)")).toBe("100");
  });

  it("treats 0 as the toolkit default, as the model does", () => {
    renderWithExtractionConfig({ chunkSize: 0, chunkOverlap: 0 });
    expect(valueOf("Chunk size (tokens)")).toBe("Default (256 tokens)");
    expect(valueOf("Chunk overlap (tokens)")).toBe("Default (25 tokens)");
  });

  it("shows a set chunk size with the default overlap", () => {
    renderWithExtractionConfig({ chunkSize: 1024 });
    expect(valueOf("Chunk size (tokens)")).toBe("1024");
    expect(valueOf("Chunk overlap (tokens)")).toBe("Default (25 tokens)");
  });

  it("shows the default overlap when only the overlap is set, as the KG build ignores it", () => {
    renderWithExtractionConfig({ chunkOverlap: 100 });
    expect(valueOf("Chunk size (tokens)")).toBe("Default (256 tokens)");
    expect(valueOf("Chunk overlap (tokens)")).toBe("Default (25 tokens)");
  });

  it.each([
    { chunkSize: "1024", chunkOverlap: -5 },
    { chunkSize: 10.5, chunkOverlap: 2.5 },
    { chunkSize: -1, chunkOverlap: "25" },
  ])(
    "shows the defaults for a stored value that is not a positive whole number: %o",
    (config) => {
      renderWithExtractionConfig(config);
      expect(valueOf("Chunk size (tokens)")).toBe("Default (256 tokens)");
      expect(valueOf("Chunk overlap (tokens)")).toBe("Default (25 tokens)");
    },
  );

  it("shows the default overlap for an invalid overlap next to a set size", () => {
    renderWithExtractionConfig({ chunkSize: 1024, chunkOverlap: 2.5 });
    expect(valueOf("Chunk size (tokens)")).toBe("1024");
    expect(valueOf("Chunk overlap (tokens)")).toBe("Default (25 tokens)");
  });
});
