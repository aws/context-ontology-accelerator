// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { renderHook, act, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";

vi.mock("@auth", () => ({
  OIDCProvider: {
    build: () => ({
      getIdToken: vi.fn().mockResolvedValue("mock-token"),
      getUser: vi.fn().mockResolvedValue({
        profile: { sub: "12345678-abcd-1234-abcd-1234567890ab" },
      }),
    }),
  },
}));

const mockFetch = vi.fn();
global.fetch = mockFetch;

import { useSessions } from "./use-sessions";
import type { PlaygroundAction } from "./playground-reducer";

function createMockResponse(data: unknown) {
  const body = `data: ${JSON.stringify(data)}\n\n`;
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(body));
      controller.close();
    },
  });
  return { ok: true, body: stream };
}

describe("useSessions", () => {
  const mockDispatch = vi.fn<(action: PlaygroundAction) => void>();
  const defaultProps = {
    queryEndpoint: "https://test.endpoint/invocations",
    oidcConfig: { authority: "https://auth.test", clientId: "client-id" },
    namespace: "test-namespace",
    dispatch: mockDispatch,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockReset();
  });

  it("restores recent session on mount", async () => {
    mockFetch.mockResolvedValueOnce(
      createMockResponse({
        sessionId: "session-123",
        namespace: "test-namespace",
        messages: [
          { role: "user", content: "hello" },
          { role: "assistant", content: "hi there" },
        ],
        hasMore: false,
        cursor: null,
      }),
    );

    const { result } = renderHook(() => useSessions(defaultProps));

    await waitFor(() => {
      expect(result.current.isRestoring).toBe(false);
    });

    expect(result.current.sessionId).toBe("session-123");
    expect(mockDispatch).toHaveBeenCalledWith({
      type: "RESTORE_HISTORY",
      messages: [
        { role: "user", content: "hello" },
        { role: "assistant", content: "hi there" },
      ],
    });
  });

  it("sets hasMoreHistory from response", async () => {
    mockFetch.mockResolvedValueOnce(
      createMockResponse({
        sessionId: "session-123",
        namespace: "test-namespace",
        messages: [{ role: "user", content: "hello" }],
        hasMore: true,
        cursor: 5,
      }),
    );

    const { result } = renderHook(() => useSessions(defaultProps));

    await waitFor(() => {
      expect(result.current.isRestoring).toBe(false);
    });

    expect(result.current.hasMoreHistory).toBe(true);
  });

  it("handles no existing session gracefully", async () => {
    mockFetch.mockResolvedValueOnce(
      createMockResponse({
        sessionId: null,
        namespace: "test-namespace",
        messages: [],
      }),
    );

    const { result } = renderHook(() => useSessions(defaultProps));

    await waitFor(() => {
      expect(result.current.isRestoring).toBe(false);
    });

    expect(result.current.sessionId).toBeNull();
    expect(mockDispatch).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: "RESTORE_HISTORY" }),
    );
  });

  it("createSession updates sessionId and dispatches RESET", async () => {
    mockFetch.mockResolvedValueOnce(
      createMockResponse({ sessionId: null, messages: [] }),
    );

    const { result } = renderHook(() => useSessions(defaultProps));

    await waitFor(() => {
      expect(result.current.isRestoring).toBe(false);
    });

    mockFetch.mockResolvedValueOnce(
      createMockResponse({
        action: "sessionCreated",
        sessionId: "new-session-456",
        namespace: "test-namespace",
      }),
    );

    await act(async () => {
      await result.current.createSession();
    });

    expect(result.current.sessionId).toBe("new-session-456");
    expect(mockDispatch).toHaveBeenCalledWith({ type: "RESET" });
  });

  it("captureSessionId only sets if no session exists", async () => {
    mockFetch.mockResolvedValueOnce(
      createMockResponse({ sessionId: null, messages: [] }),
    );

    const { result } = renderHook(() => useSessions(defaultProps));

    await waitFor(() => {
      expect(result.current.isRestoring).toBe(false);
    });

    act(() => {
      result.current.captureSessionId("captured-session");
    });

    expect(result.current.sessionId).toBe("captured-session");

    act(() => {
      result.current.captureSessionId("another-session");
    });

    expect(result.current.sessionId).toBe("captured-session");
  });

  it("sends runtime session ID header with user sub", async () => {
    mockFetch.mockResolvedValueOnce(
      createMockResponse({ sessionId: null, messages: [] }),
    );

    renderHook(() => useSessions(defaultProps));

    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalled();
    });

    const headers = mockFetch.mock.calls[0][1].headers;
    // Length-prefixed (see deriveRuntimeSessionId): the prefix is what keeps
    // two distinct subs from deriving the same session id.
    expect(headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]).toBe(
      "036-12345678-abcd-1234-abcd-1234567890ab",
    );
    expect(headers["Authorization"]).toBe("Bearer mock-token");
  });

  it("resets state on namespace change", async () => {
    mockFetch.mockResolvedValue(
      createMockResponse({ sessionId: "s1", messages: [], hasMore: false }),
    );

    const { result, rerender } = renderHook((props) => useSessions(props), {
      initialProps: defaultProps,
    });

    await waitFor(() => {
      expect(result.current.isRestoring).toBe(false);
    });

    expect(result.current.sessionId).toBe("s1");

    rerender({ ...defaultProps, namespace: "other-namespace" });

    await waitFor(() => {
      expect(mockDispatch).toHaveBeenCalledWith({ type: "RESET" });
    });
  });
});
