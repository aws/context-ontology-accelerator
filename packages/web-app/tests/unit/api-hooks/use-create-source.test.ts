// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { useCreateSource } from "../../../src/api-hooks/use-create-source";

const mockSend = vi.fn();
vi.mock("../../../src/components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: mockSend }),
}));

function setup() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
  return { invalidate, wrapper };
}

describe("useCreateSource", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("sends CreateSourceCommand with the caller's input", async () => {
    mockSend.mockResolvedValueOnce({});
    const { wrapper } = setup();
    const { result } = renderHook(() => useCreateSource(), { wrapper });

    act(() => {
      result.current.mutate({ namespaceId: "ns-1", name: "orders" });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockSend.mock.calls[0][0].input).toEqual({
      namespaceId: "ns-1",
      name: "orders",
    });
  });

  it("refreshes the sources and the namespace counts even when the caller passes onSuccess", async () => {
    mockSend.mockResolvedValueOnce({});
    const onSuccess = vi.fn();
    const { invalidate, wrapper } = setup();
    const { result } = renderHook(() => useCreateSource({ onSuccess }), {
      wrapper,
    });

    act(() => {
      result.current.mutate({ namespaceId: "ns-1", name: "orders" });
    });

    await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce());
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["sources"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["namespaces"] });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["namespace", "ns-1"],
    });
  });

  it("waits for a promise the caller's onSuccess returns", async () => {
    mockSend.mockResolvedValueOnce({});
    let release: () => void = () => {};
    const onSuccess = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          release = resolve;
        }),
    );
    const { wrapper } = setup();
    const { result } = renderHook(() => useCreateSource({ onSuccess }), {
      wrapper,
    });
    let settled = false;

    act(() => {
      void result.current
        .mutateAsync({ namespaceId: "ns-1", name: "orders" })
        .then(() => {
          settled = true;
        });
    });

    await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(settled).toBe(false);
    expect(result.current.isPending).toBe(true);

    act(() => release());
    await waitFor(() => expect(settled).toBe(true));
    expect(result.current.isSuccess).toBe(true);
  });
});
