// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { useListPrincipalGrants } from "./use-list-principal-grants";

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

describe("useListPrincipalGrants", () => {
  it("follows nextToken across short and empty pages and merges the grants", async () => {
    mockSend
      .mockResolvedValueOnce({ grants: [{ grantId: "g1" }], nextToken: "a" })
      .mockResolvedValueOnce({ grants: [], nextToken: "b" })
      .mockResolvedValueOnce({ grants: [{ grantId: "g2" }] });

    const { result } = renderHook(() => useListPrincipalGrants(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({
      grants: [{ grantId: "g1" }, { grantId: "g2" }],
    });
    expect(mockSend.mock.calls.map((c) => c[0].input)).toEqual([
      { principalId: "me", nextToken: undefined },
      { principalId: "me", nextToken: "a" },
      { principalId: "me", nextToken: "b" },
    ]);
  });
});
