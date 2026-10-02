// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { useListSourceTables } from "./use-list-source-tables";

const sendMock = vi.fn();

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: sendMock }),
}));

// The command is mocked as an input carrier so the send() spy can assert which
// cursor each page was requested with.
vi.mock("@coa/control-plane-client", () => ({
  ListSourceTablesCommand: class {
    input: Record<string, unknown>;
    constructor(input: Record<string, unknown>) {
      this.input = input;
    }
  },
  ReviewStatus: { PENDING_REVIEW: "PENDING_REVIEW" },
}));

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children);
}

/** A page of `n` tables named `t{base}`…, with an optional next cursor. */
function page(n: number, base: number, nextToken?: string) {
  return {
    items: Array.from({ length: n }, (_, i) => ({
      tableId: `db.t${base + i}`,
      name: `t${base + i}`,
    })),
    skippedAssets: 0,
    ...(nextToken && { nextToken }),
  };
}

describe("useListSourceTables — progressive page loading", () => {
  beforeEach(() => {
    sendMock.mockReset();
  });

  it("exposes the first page before later pages have arrived", async () => {
    // The regression this guards: the hook used to drain every page inside one
    // queryFn, so `data` stayed undefined — and the UI stayed on a spinner —
    // until the LAST page landed. Page 2 never resolves here, so a hook that
    // still waits for completion would never expose items at all.
    let releasePage2: (v: unknown) => void = () => {};
    const page2 = new Promise((resolve) => {
      releasePage2 = resolve;
    });
    sendMock
      .mockResolvedValueOnce(page(2, 0, "cursor-1"))
      .mockReturnValueOnce(page2);

    const { result } = renderHook(() => useListSourceTables("ns", "src"), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.data?.items).toHaveLength(2));
    // Renderable, and honest that it is not the whole list yet.
    expect(result.current.isLoading).toBe(false);
    expect(result.current.isLoadingMore).toBe(true);

    releasePage2(page(1, 2));
    await waitFor(() => expect(result.current.data?.items).toHaveLength(3));
    expect(result.current.isLoadingMore).toBe(false);
  });

  it("drains remaining pages without being asked and accumulates them in order", async () => {
    sendMock
      .mockResolvedValueOnce(page(2, 0, "cursor-1"))
      .mockResolvedValueOnce(page(2, 2, "cursor-2"))
      .mockResolvedValueOnce(page(1, 4));

    const { result } = renderHook(() => useListSourceTables("ns", "src"), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.data?.items).toHaveLength(5));
    expect(result.current.isLoadingMore).toBe(false);
    expect(result.current.data?.items.map((t) => t.name)).toEqual([
      "t0",
      "t1",
      "t2",
      "t3",
      "t4",
    ]);
    expect(result.current.pagesLoaded).toBe(3);
    // Page 1 carries no cursor; each later page carries the previous one's.
    const tokens = sendMock.mock.calls.map(
      (c) => (c[0] as { input: { nextToken?: string } }).input.nextToken,
    );
    expect(tokens).toEqual([undefined, "cursor-1", "cursor-2"]);
  });

  it("sums skippedAssets across pages", async () => {
    sendMock
      .mockResolvedValueOnce({ ...page(1, 0, "c1"), skippedAssets: 2 })
      .mockResolvedValueOnce({ ...page(1, 1), skippedAssets: 3 });

    const { result } = renderHook(() => useListSourceTables("ns", "src"), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.data?.items).toHaveLength(2));
    expect(result.current.isLoadingMore).toBe(false);
    expect(result.current.data?.skippedAssets).toBe(5);
  });

  it("issues no request until both ids are known", () => {
    renderHook(() => useListSourceTables("ns", ""), { wrapper: makeWrapper() });
    expect(sendMock).not.toHaveBeenCalled();
  });

  it("surfaces a mid-drain page failure and stops loading instead of stalling", async () => {
    // Regression guard: on a page-2 rejection React Query leaves hasNextPage
    // true (page 1's cursor) but stops fetching, and the drain effect will not
    // retry. Without the isError guards, isLoadingMore stayed true forever — a
    // perpetual spinner that never resolves and never errors. The partial
    // page-1 prefix must remain visible and the error must be exposed.
    sendMock
      .mockResolvedValueOnce(page(2, 0, "cursor-1"))
      .mockRejectedValueOnce(new Error("page 2 boom"));

    const { result } = renderHook(() => useListSourceTables("ns", "src"), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    // The already-loaded prefix survives the failure.
    expect(result.current.data?.items).toHaveLength(2);
    // No longer "loading" — it loaded a partial list and then failed.
    expect(result.current.isLoadingMore).toBe(false);
    expect(result.current.error).toBeInstanceOf(Error);
    // Drain halted: only page 1 and the failed page 2 were attempted, no retry.
    expect(sendMock).toHaveBeenCalledTimes(2);
  });
});
