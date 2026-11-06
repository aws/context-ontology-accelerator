// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { usePutSourceRescanSchedule } from "../../../src/api-hooks/use-put-source-rescan-schedule";

const mockSend = vi.fn();
vi.mock("../../../src/components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: mockSend }),
}));

const SCHEDULE = {
  enabled: true,
  scheduleExpression: "cron(0 3 * * ? *)",
  timezone: "UTC",
};

function setup() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
  return { invalidate, wrapper };
}

describe("usePutSourceRescanSchedule", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("sends PutSourceRescanScheduleCommand with the schedule", async () => {
    mockSend.mockResolvedValueOnce({});
    const { wrapper } = setup();
    const { result } = renderHook(
      () => usePutSourceRescanSchedule("ns-1", "src-1"),
      { wrapper },
    );

    act(() => {
      result.current.mutate(SCHEDULE);
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockSend.mock.calls[0][0].input).toEqual({
      namespaceId: "ns-1",
      sourceId: "src-1",
      ...SCHEDULE,
    });
  });

  it("refreshes the source even when the caller passes onSuccess", async () => {
    mockSend.mockResolvedValueOnce({});
    const onSuccess = vi.fn();
    const { invalidate, wrapper } = setup();
    const { result } = renderHook(
      () => usePutSourceRescanSchedule("ns-1", "src-1", { onSuccess }),
      { wrapper },
    );

    act(() => {
      result.current.mutate(SCHEDULE);
    });

    await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce());
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["source", "ns-1", "src-1"],
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
    const { result } = renderHook(
      () => usePutSourceRescanSchedule("ns-1", "src-1", { onSuccess }),
      { wrapper },
    );
    let settled = false;

    act(() => {
      void result.current.mutateAsync(SCHEDULE).then(() => {
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
