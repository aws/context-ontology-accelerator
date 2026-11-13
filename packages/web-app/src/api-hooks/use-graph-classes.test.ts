// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { useGraphClasses, CLASS_FETCH_CHUNK_SIZE } from "./use-graph-classes";
import type { GraphSearchResult } from "../services/ontology-engine";

const searchEntitiesPaged = vi.fn();

vi.mock("../services/ontology-engine", () => ({
  searchEntitiesPaged: (...args: unknown[]) => searchEntitiesPaged(...args),
}));

const stableApiClient = { get: vi.fn() };
vi.mock("@components/ApiClientProvider", () => ({
  useApiClient: () => stableApiClient,
}));

const NS = "ns-1";

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children);
}

/** A full chunk of `n` distinct hits with the given total_count. */
function chunk(n: number, total: number, base = 0): GraphSearchResult {
  return {
    hits: Array.from({ length: n }, (_, i) => ({
      uri: `http://ex/C${base + i}`,
      label: `C${base + i}`,
      kind: "class",
      graph_uris: ["g"],
    })),
    total_count: total,
  };
}

describe("useGraphClasses", () => {
  beforeEach(() => {
    searchEntitiesPaged.mockReset();
  });

  it("is disabled without a namespace id", () => {
    const { result } = renderHook(() => useGraphClasses(undefined, ""), {
      wrapper: makeWrapper(),
    });
    expect(result.current.fetchStatus).toBe("idle");
    expect(searchEntitiesPaged).not.toHaveBeenCalled();
  });

  it("is disabled when the caller passes enabled=false", () => {
    const { result } = renderHook(
      () => useGraphClasses(NS, "", undefined, false),
      { wrapper: makeWrapper() },
    );
    expect(result.current.fetchStatus).toBe("idle");
    expect(searchEntitiesPaged).not.toHaveBeenCalled();
  });

  it("sends an empty query as the '*' wildcard, kind=class, offset=0", async () => {
    searchEntitiesPaged.mockResolvedValueOnce(chunk(2, 2));
    const { result } = renderHook(() => useGraphClasses(NS, "  "), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(searchEntitiesPaged).toHaveBeenCalledWith(stableApiClient, NS, "*", {
      kind: "class",
      ontology_id: undefined,
      limit: CLASS_FETCH_CHUNK_SIZE,
      offset: 0,
    });
    expect(result.current.data?.pages[0].total_count).toBe(2);
  });

  it("forwards an ontology_id filter when provided", async () => {
    searchEntitiesPaged.mockResolvedValueOnce(chunk(1, 1));
    const { result } = renderHook(
      () => useGraphClasses(NS, "", "http://ex/onto#o1"),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(searchEntitiesPaged).toHaveBeenCalledWith(stableApiClient, NS, "*", {
      kind: "class",
      ontology_id: "http://ex/onto#o1",
      limit: CLASS_FETCH_CHUNK_SIZE,
      offset: 0,
    });
  });

  it("stops paging when a chunk comes back short of the fetch size", async () => {
    // total_count says more exist, but a short chunk means the query ran dry.
    searchEntitiesPaged.mockResolvedValueOnce(
      chunk(CLASS_FETCH_CHUNK_SIZE - 1, 999),
    );
    const { result } = renderHook(() => useGraphClasses(NS, ""), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.hasNextPage).toBe(false);
  });

  it("advances the offset by the loaded count for the next page", async () => {
    searchEntitiesPaged
      .mockResolvedValueOnce(
        chunk(CLASS_FETCH_CHUNK_SIZE, CLASS_FETCH_CHUNK_SIZE + 5),
      )
      .mockResolvedValueOnce(
        chunk(5, CLASS_FETCH_CHUNK_SIZE + 5, CLASS_FETCH_CHUNK_SIZE),
      );

    const { result } = renderHook(() => useGraphClasses(NS, ""), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.hasNextPage).toBe(true);

    await result.current.fetchNextPage();
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2));

    expect(searchEntitiesPaged).toHaveBeenLastCalledWith(
      stableApiClient,
      NS,
      "*",
      {
        kind: "class",
        limit: CLASS_FETCH_CHUNK_SIZE,
        offset: CLASS_FETCH_CHUNK_SIZE,
      },
    );
    expect(result.current.hasNextPage).toBe(false);
  });

  it("stops when the loaded count reaches total_count exactly", async () => {
    // Exactly one full chunk, and total equals that chunk — no more pages even
    // though the chunk was 'full'.
    searchEntitiesPaged.mockResolvedValueOnce(
      chunk(CLASS_FETCH_CHUNK_SIZE, CLASS_FETCH_CHUNK_SIZE),
    );
    const { result } = renderHook(() => useGraphClasses(NS, ""), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.hasNextPage).toBe(false);
  });
});
