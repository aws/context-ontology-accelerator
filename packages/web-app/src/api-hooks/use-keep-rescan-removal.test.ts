// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { useKeepRescanRemoval } from "./use-keep-rescan-removal";

const mockSend = vi.fn();

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: mockSend }),
}));

beforeEach(() => {
  mockSend.mockReset();
});

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
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

describe("useKeepRescanRemoval", () => {
  it("keeps a whole table and invalidates caches", async () => {
    mockSend.mockResolvedValueOnce({
      tableId: "sales.orders",
      pendingDeletion: false,
    });
    const { wrapper, queryClient } = createWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useKeepRescanRemoval("ns-1", "src-1"), {
      wrapper,
    });

    act(() => {
      result.current.mutate({ tableId: "sales.orders" });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockSend).toHaveBeenCalledOnce();
    // No columnName when keeping the whole table.
    expect(mockSend.mock.calls[0][0].input).toEqual({
      namespaceId: "ns-1",
      sourceId: "src-1",
      tableId: "sales.orders",
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["sourceTables", "ns-1", "src-1"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["source", "ns-1", "src-1"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["sourceTable", "ns-1", "src-1", "sales.orders"],
    });
  });

  it("keeps a single column, threading columnName through", async () => {
    mockSend.mockResolvedValueOnce({
      tableId: "sales.orders",
      columnName: "legacy_region",
      pendingDeletion: false,
    });
    const { wrapper } = createWrapper();

    const { result } = renderHook(() => useKeepRescanRemoval("ns-1", "src-1"), {
      wrapper,
    });

    act(() => {
      result.current.mutate({
        tableId: "sales.orders",
        columnName: "legacy_region",
      });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockSend.mock.calls[0][0].input).toEqual({
      namespaceId: "ns-1",
      sourceId: "src-1",
      tableId: "sales.orders",
      columnName: "legacy_region",
    });
  });

  it("surfaces mutation errors", async () => {
    mockSend.mockRejectedValueOnce(new Error("Keep failed"));
    const { wrapper } = createWrapper();

    const { result } = renderHook(() => useKeepRescanRemoval("ns-1", "src-1"), {
      wrapper,
    });

    act(() => {
      result.current.mutate({ tableId: "sales.orders" });
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("Keep failed");
  });
});
