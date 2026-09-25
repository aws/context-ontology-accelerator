// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { SourceType } from "@coa/control-plane-client";
import { useListSources } from "./use-list-sources";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockSend = vi.fn();

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: mockSend }),
}));

vi.mock("@coa/control-plane-client", () => ({
  ListSourcesCommand: vi.fn().mockImplementation(function (input) {
    return { input };
  }),
  SourceType: { DATABASE: "DATABASE", DOCUMENTS: "DOCUMENTS" },
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const NS_ID = "550e8400-e29b-41d4-a716-446655440000";

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("useListSources", () => {
  beforeEach(() => {
    mockSend.mockReset();
  });

  it("fetches sources for a namespace", async () => {
    mockSend.mockResolvedValueOnce({
      items: [{ sourceId: "src-001" }],
      nextToken: undefined,
    });

    const { result } = renderHook(() => useListSources(NS_ID), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.pages[0].items).toHaveLength(1);
    expect(result.current.data?.pages[0].items[0].sourceId).toBe("src-001");
  });

  it("is disabled when namespaceId is empty", () => {
    const { result } = renderHook(() => useListSources(""), {
      wrapper: makeWrapper(),
    });
    expect(result.current.fetchStatus).toBe("idle");
    expect(mockSend).not.toHaveBeenCalled();
  });

  it("passes sourceType to the command when provided", async () => {
    mockSend.mockResolvedValueOnce({ items: [], nextToken: undefined });

    const { result } = renderHook(
      () => useListSources(NS_ID, "DATABASE" as SourceType),
      {
        wrapper: makeWrapper(),
      },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(mockSend).toHaveBeenCalledWith(
      expect.objectContaining({
        input: expect.objectContaining({ sourceType: "DATABASE" }),
      }),
    );
  });

  it("passes maxResults=100 to the command", async () => {
    mockSend.mockResolvedValueOnce({ items: [], nextToken: undefined });

    const { result } = renderHook(() => useListSources(NS_ID), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(mockSend).toHaveBeenCalledWith(
      expect.objectContaining({
        input: expect.objectContaining({ maxResults: 100 }),
      }),
    );
  });

  it("auto-fetches next page when available", async () => {
    mockSend
      .mockResolvedValueOnce({
        items: [{ sourceId: "src-001" }],
        nextToken: "token-1",
      })
      .mockResolvedValueOnce({
        items: [{ sourceId: "src-002" }],
        nextToken: undefined,
      });

    const { result } = renderHook(() => useListSources(NS_ID), {
      wrapper: makeWrapper(),
    });

    // Wait for both pages to be fetched
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2), {
      timeout: 3000,
    });

    expect(mockSend).toHaveBeenCalledTimes(2);
    const allItems = result.current.data?.pages.flatMap((p) => p.items ?? []);
    expect(allItems).toHaveLength(2);
  });

  it("uses different query keys for different sourceType values", async () => {
    mockSend.mockResolvedValue({ items: [], nextToken: undefined });

    const { result: r1 } = renderHook(() => useListSources(NS_ID), {
      wrapper: makeWrapper(),
    });
    const { result: r2 } = renderHook(
      () => useListSources(NS_ID, "DATABASE" as SourceType),
      {
        wrapper: makeWrapper(),
      },
    );

    await waitFor(() => r1.current.isSuccess && r2.current.isSuccess);

    // Both hooks fetched independently
    expect(mockSend).toHaveBeenCalledTimes(2);
  });
});
