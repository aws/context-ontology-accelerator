// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { useListOntologies } from "./use-list-ontologies";

const mockSend = vi.fn();

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: mockSend }),
}));

beforeEach(() => {
  mockSend.mockReset();
});

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return React.createElement(
    QueryClientProvider,
    { client: queryClient },
    children,
  );
}

describe("useListOntologies", () => {
  it("returns the ontologies from every page, dropping rows without an id", async () => {
    mockSend
      .mockResolvedValueOnce({
        ontologies: [{ ontologyId: "o1" }, { uri: "no-id" }],
        nextToken: "a",
      })
      .mockResolvedValueOnce({ ontologies: [], nextToken: "b" })
      .mockResolvedValueOnce({ ontologies: [{ ontologyId: "o2" }] });

    const { result } = renderHook(() => useListOntologies("ns-1"), {
      wrapper,
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.map((o) => o.ontologyId)).toEqual(["o1", "o2"]);
    expect(mockSend.mock.calls.map((c) => c[0].input.nextToken)).toEqual([
      undefined,
      "a",
      "b",
    ]);
  });
});
