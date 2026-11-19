// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { useGetNamespace } from "../../../src/api-hooks/use-get-namespace";
import { serviceError } from "../../helpers/service-error";

const mockSend = vi.fn();
vi.mock("../../../src/components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: mockSend }),
}));

// Keep the real exception class: the retry rule reads its status code.
vi.mock("@coa/control-plane-client", async (importOriginal) => {
  const actual = await importOriginal<object>();
  return {
    ...actual,
    GetNamespaceCommand: class {
      input: Record<string, unknown>;
      constructor(input: Record<string, unknown>) {
        this.input = input;
      }
    },
  };
});

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({
    // The hook sets its own retry rule; make its retries immediate here.
    defaultOptions: { queries: { retry: false, retryDelay: 0 } },
  });
  return React.createElement(
    QueryClientProvider,
    { client: queryClient },
    children,
  );
}

describe("useGetNamespace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns namespace on success", async () => {
    mockSend.mockResolvedValueOnce({
      namespace: {
        namespaceId: "ns-1",
        name: "test-ns",
        displayName: "Test NS",
        status: "ACTIVE",
      },
    });

    const { result } = renderHook(() => useGetNamespace("ns-1"), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.namespace?.namespaceId).toBe("ns-1");
  });

  it("sends namespaceId in command", async () => {
    mockSend.mockResolvedValueOnce({ namespace: {} });

    renderHook(() => useGetNamespace("ns-42"), { wrapper });

    await waitFor(() => expect(mockSend).toHaveBeenCalled());
    const command = mockSend.mock.calls[0][0];
    expect(command.input.namespaceId).toBe("ns-42");
  });

  it("does not fetch when namespaceId is empty", async () => {
    const { result } = renderHook(() => useGetNamespace(""), { wrapper });

    await new Promise((r) => setTimeout(r, 50));
    expect(result.current.fetchStatus).toBe("idle");
    expect(mockSend).not.toHaveBeenCalled();
  });

  it("handles error state", async () => {
    // A plain Error may pass, so it is retried: fail every attempt.
    mockSend.mockRejectedValue(new Error("Not found"));

    const { result } = renderHook(() => useGetNamespace("ns-bad"), { wrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("Not found");
  });
});

// The app's own QueryClient (main.tsx) has no options, so the hook runs with
// React Query's default retry delays (1 s, 2 s, 4 s). Use the same here.
function renderWithAppDefaults() {
  const client = new QueryClient();
  return renderHook(() => useGetNamespace("ns-1"), {
    wrapper: ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client }, children),
  });
}

const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));

describe("useGetNamespace retries (app defaults)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockSend.mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it.each([400, 404])(
    "gives up at once on a %i, which will not change",
    async (status) => {
      mockSend.mockRejectedValue(serviceError(status));
      const { result } = renderWithAppDefaults();
      await advance(50);
      expect(result.current.isError).toBe(true);
      expect(mockSend).toHaveBeenCalledTimes(1);
      await advance(10_000);
      expect(mockSend).toHaveBeenCalledTimes(1);
    },
  );

  it.each([
    ["a 500", () => serviceError(500)],
    ["a 503", () => serviceError(503)],
    ["a 401", () => serviceError(401)],
    // The authorizer's 403 for an expired token and for no role look alike.
    ["a 403", () => serviceError(403, "Signature has expired")],
    ["a network failure", () => new TypeError("Failed to fetch")],
  ])("still retries %s three times", async (_label, makeError) => {
    mockSend.mockImplementation(() => Promise.reject(makeError()));
    const { result } = renderWithAppDefaults();
    await advance(50);
    expect(result.current.isError).toBe(false);
    expect(result.current.isLoading).toBe(true);
    expect(mockSend).toHaveBeenCalledTimes(1);
    await advance(10_000);
    expect(result.current.isError).toBe(true);
    expect(mockSend).toHaveBeenCalledTimes(4);
  });

  it("returns the namespace when a retried failure passes", async () => {
    mockSend
      .mockRejectedValueOnce(serviceError(502))
      .mockResolvedValueOnce({ namespace: { namespaceId: "ns-1" } });
    const { result } = renderWithAppDefaults();
    await advance(1_500);
    expect(result.current.isSuccess).toBe(true);
    expect(mockSend).toHaveBeenCalledTimes(2);
  });

  it("returns the namespace when a 403 passes on retry (token refreshed)", async () => {
    mockSend
      .mockRejectedValueOnce(serviceError(403, "Signature has expired"))
      .mockResolvedValueOnce({ namespace: { namespaceId: "ns-1" } });
    const { result } = renderWithAppDefaults();
    await advance(1_500);
    expect(result.current.isSuccess).toBe(true);
    expect(result.current.data?.namespace?.namespaceId).toBe("ns-1");
    expect(mockSend).toHaveBeenCalledTimes(2);
  });
});
