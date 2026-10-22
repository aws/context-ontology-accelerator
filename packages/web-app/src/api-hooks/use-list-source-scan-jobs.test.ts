// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { useListSourceScanJobs } from "./use-list-source-scan-jobs";

const mockSend = vi.fn();

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: mockSend }),
}));

beforeEach(() => {
  mockSend.mockReset();
});

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    wrapper: ({ children }: { children: React.ReactNode }) =>
      React.createElement(
        QueryClientProvider,
        { client: queryClient },
        children,
      ),
    queryClient,
  };
}

describe("useListSourceScanJobs", () => {
  it("requests the history for the given source and returns it", async () => {
    mockSend.mockResolvedValueOnce({
      items: [
        { jobId: "2026-01-02T00:00:00Z", status: "COMPLETED" },
        { jobId: "2026-01-01T00:00:00Z", status: "COMPLETED", isRescan: true },
      ],
    });
    const { wrapper } = createWrapper();

    const { result } = renderHook(
      () => useListSourceScanJobs("ns-1", "src-1"),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockSend).toHaveBeenCalledOnce();
    expect(mockSend.mock.calls[0][0].input).toEqual({
      namespaceId: "ns-1",
      sourceId: "src-1",
    });
    expect(result.current.data?.items).toHaveLength(2);
  });

  it("follows nextToken and returns every page, newest first", async () => {
    // The server caps a page (1000 rows); a long-lived source's older scans
    // are on later pages and must not silently drop off the tab.
    mockSend
      .mockResolvedValueOnce({
        items: [
          { jobId: "2026-01-04T00:00:00Z", status: "COMPLETED" },
          { jobId: "2026-01-03T00:00:00Z", status: "FAILED" },
        ],
        nextToken: "page-2",
      })
      .mockResolvedValueOnce({ items: [], nextToken: "page-3" })
      .mockResolvedValueOnce({
        items: [{ jobId: "2026-01-01T00:00:00Z", status: "COMPLETED" }],
      });
    const { wrapper } = createWrapper();

    const { result } = renderHook(
      () => useListSourceScanJobs("ns-1", "src-1"),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockSend).toHaveBeenCalledTimes(3);
    expect(
      mockSend.mock.calls.map(
        (c) => (c[0] as { input: { nextToken?: string } }).input.nextToken,
      ),
    ).toEqual([undefined, "page-2", "page-3"]);
    expect(result.current.data?.items?.map((i) => i.jobId)).toEqual([
      "2026-01-04T00:00:00Z",
      "2026-01-03T00:00:00Z",
      "2026-01-01T00:00:00Z",
    ]);
  });

  it("surfaces a later-page failure instead of a truncated history", async () => {
    mockSend
      .mockResolvedValueOnce({
        items: [{ jobId: "2026-01-02T00:00:00Z" }],
        nextToken: "page-2",
      })
      .mockRejectedValueOnce(new Error("page 2 failed"));
    const { wrapper } = createWrapper();

    const { result } = renderHook(
      () => useListSourceScanJobs("ns-1", "src-1"),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("page 2 failed");
  });

  it("keys the cache per source so two sources do not share history", async () => {
    // The Scan-history tab is rendered per source; a shared key would show one
    // source's scans under another.
    mockSend.mockResolvedValue({ items: [] });
    const { wrapper, queryClient } = createWrapper();

    const { result: a } = renderHook(
      () => useListSourceScanJobs("ns-1", "src-1"),
      { wrapper },
    );
    await waitFor(() => expect(a.current.isSuccess).toBe(true));

    const { result: b } = renderHook(
      () => useListSourceScanJobs("ns-1", "src-2"),
      { wrapper },
    );
    await waitFor(() => expect(b.current.isSuccess).toBe(true));

    const keys = queryClient
      .getQueryCache()
      .getAll()
      .map((q) => q.queryKey);
    expect(keys).toContainEqual(["sourceScanJobs", "ns-1", "src-1"]);
    expect(keys).toContainEqual(["sourceScanJobs", "ns-1", "src-2"]);
    expect(mockSend).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["missing sourceId", "ns-1", ""],
    ["missing namespaceId", "", "src-1"],
    ["both missing", "", ""],
  ])("does not fire with %s", async (_label, namespaceId, sourceId) => {
    // SourceDetail calls this hook unconditionally, before the route params are
    // resolved, so an unguarded query would send a request with an empty id.
    const { wrapper } = createWrapper();

    const { result } = renderHook(
      () => useListSourceScanJobs(namespaceId, sourceId),
      { wrapper },
    );

    expect(result.current.fetchStatus).toBe("idle");
    expect(mockSend).not.toHaveBeenCalled();
  });

  it("surfaces a query error rather than reporting success with no rows", async () => {
    // The Scan-history tab falls back to a derived view when this errors, so the
    // error has to reach it instead of looking like an empty history.
    mockSend.mockRejectedValueOnce(new Error("ListSourceScanJobs failed"));
    const { wrapper } = createWrapper();

    const { result } = renderHook(
      () => useListSourceScanJobs("ns-1", "src-1"),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("ListSourceScanJobs failed");
    expect(result.current.data).toBeUndefined();
  });
});
