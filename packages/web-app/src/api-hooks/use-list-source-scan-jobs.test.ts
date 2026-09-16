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
