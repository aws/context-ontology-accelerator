// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { useListPlatformGrants } from "../../../src/api-hooks/use-list-platform-grants";

const mockSend = vi.fn();
vi.mock("../../../src/components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: mockSend }),
}));

vi.mock("@coa/control-plane-client", () => ({
  ListPlatformGrantsCommand: class {
    input: Record<string, unknown>;
    constructor(input: Record<string, unknown>) {
      this.input = input;
    }
  },
}));

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

describe("useListPlatformGrants", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns the first page of grants on success", async () => {
    mockSend.mockResolvedValueOnce({
      grants: [
        {
          grantId: "pg1",
          principalType: "User",
          principalId: "admin@example.com",
          role: "platform-admin",
          grantedBy: "system",
          grantedAt: "2026-05-01T00:00:00Z",
        },
      ],
    });

    const { result } = renderHook(() => useListPlatformGrants(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.pages[0].grants).toHaveLength(1);
    expect(result.current.data?.pages[0].grants?.[0].role).toBe(
      "platform-admin",
    );
    // First page sends an undefined token.
    expect(mockSend.mock.calls[0][0].input.nextToken).toBeUndefined();
  });

  it("forwards the principal filter onto the command input", async () => {
    mockSend.mockResolvedValueOnce({ grants: [] });

    renderHook(() => useListPlatformGrants("me"), { wrapper });

    await waitFor(() => expect(mockSend).toHaveBeenCalled());
    expect(mockSend.mock.calls[0][0].input.principal).toBe("me");
  });

  it("omits the principal filter when called with no argument", async () => {
    mockSend.mockResolvedValueOnce({ grants: [] });

    renderHook(() => useListPlatformGrants(), { wrapper });

    await waitFor(() => expect(mockSend).toHaveBeenCalled());
    expect(mockSend.mock.calls[0][0].input.principal).toBeUndefined();
  });

  it("derives hasNextPage from nextToken and fetches subsequent pages", async () => {
    mockSend
      .mockResolvedValueOnce({
        grants: [{ grantId: "pg1" }],
        nextToken: "tok-2",
      })
      .mockResolvedValueOnce({ grants: [{ grantId: "pg2" }] });

    const { result } = renderHook(() => useListPlatformGrants(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.hasNextPage).toBe(true);

    await act(async () => {
      await result.current.fetchNextPage();
    });

    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2));
    // Second page forwards the token returned by the first page.
    expect(mockSend.mock.calls[1][0].input.nextToken).toBe("tok-2");
    expect(result.current.hasNextPage).toBe(false);
  });

  it("handles error state", async () => {
    mockSend.mockRejectedValueOnce(new Error("Access denied"));

    const { result } = renderHook(() => useListPlatformGrants(), { wrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("Access denied");
  });
});
