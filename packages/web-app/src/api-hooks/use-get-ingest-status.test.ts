// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { useGetIngestStatus } from "./use-get-ingest-status";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockSend = vi.fn();

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: mockSend }),
}));

vi.mock("@coa/control-plane-client", () => ({
  GetIngestStatusCommand: vi.fn().mockImplementation(function (input) {
    return { input };
  }),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const NS_ID = "550e8400-e29b-41d4-a716-446655440000";
const ONT_ID = "https://example.org/e2e-async";
const JOB_ID = "job-abc-123";

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

describe("useGetIngestStatus", () => {
  beforeEach(() => {
    mockSend.mockReset();
  });

  it("is disabled until a jobId is provided", () => {
    const { result } = renderHook(
      () => useGetIngestStatus(NS_ID, ONT_ID, null),
      { wrapper: makeWrapper() },
    );
    expect(result.current.fetchStatus).toBe("idle");
    expect(mockSend).not.toHaveBeenCalled();
  });

  it("is disabled when namespace or ontology id is empty", () => {
    const { result } = renderHook(
      () => useGetIngestStatus("", ONT_ID, JOB_ID),
      {
        wrapper: makeWrapper(),
      },
    );
    expect(result.current.fetchStatus).toBe("idle");
    expect(mockSend).not.toHaveBeenCalled();
  });

  it("unwraps the job document from the result envelope", async () => {
    mockSend.mockResolvedValueOnce({
      result: {
        jobId: JOB_ID,
        ontologyId: ONT_ID,
        status: "completed",
        result: { ontologyId: ONT_ID, classCount: 3, embeddingCount: 4 },
      },
    });

    const { result } = renderHook(
      () => useGetIngestStatus(NS_ID, ONT_ID, JOB_ID),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.status).toBe("completed");
    expect(result.current.data?.result?.embeddingCount).toBe(4);
  });

  it("passes namespace, ontology, and job ids to the command", async () => {
    mockSend.mockResolvedValueOnce({
      result: { jobId: JOB_ID, ontologyId: ONT_ID, status: "running" },
    });

    const { result } = renderHook(
      () => useGetIngestStatus(NS_ID, ONT_ID, JOB_ID),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(mockSend).toHaveBeenCalledWith(
      expect.objectContaining({
        input: expect.objectContaining({
          namespaceId: NS_ID,
          ontologyId: ONT_ID,
          jobId: JOB_ID,
        }),
      }),
    );
  });

  it("stops polling once the job reaches a terminal state", async () => {
    // Terminal on the first response → refetchInterval must return false, so
    // no further polls fire. (If the predicate kept polling, mockSend would be
    // called repeatedly.)
    mockSend.mockResolvedValue({
      result: { jobId: JOB_ID, ontologyId: ONT_ID, status: "completed" },
    });

    const { result } = renderHook(
      () => useGetIngestStatus(NS_ID, ONT_ID, JOB_ID),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => expect(result.current.data?.status).toBe("completed"));

    // Give any erroneously-scheduled refetch a chance to fire, then assert it
    // didn't: a terminal status means exactly one send.
    await new Promise((r) => setTimeout(r, 150));
    expect(mockSend).toHaveBeenCalledTimes(1);
  });

  it("surfaces a poll error to the caller", async () => {
    mockSend.mockRejectedValueOnce(new Error("boom"));

    const { result } = renderHook(
      () => useGetIngestStatus(NS_ID, ONT_ID, JOB_ID),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("boom");
  });
});
