// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  PLAYGROUND_UNAVAILABLE_MESSAGE,
  usePlaygroundStream,
} from "./use-playground-stream";

const OIDC = { authority: "https://idp.example.test", clientId: "c" };

/**
 * With no query endpoint, submit returned silently, so the
 * Playground showed "Resolving query" forever. It must end the request with a visible error.
 */
describe("usePlaygroundStream without a query service", () => {
  it.each([
    ["no query endpoint", undefined, OIDC, "r1", "r1"],
    [
      "no OIDC configuration",
      "https://query.example.test",
      undefined,
      "r2",
      "r2",
    ],
    ["no request id", undefined, OIDC, undefined, ""],
  ])(
    "ends the request with an unavailable error when there is %s",
    async (_label, queryEndpoint, oidcConfig, requestId, expectedId) => {
      const onEvent = vi.fn();
      const { result } = renderHook(() =>
        usePlaygroundStream({ queryEndpoint, oidcConfig, onEvent }),
      );
      await act(async () => {
        result.current.submit({ query: "q", namespace: "ns", requestId });
      });
      expect(onEvent).toHaveBeenCalledTimes(1);
      const event = onEvent.mock.calls[0][0];
      expect(event.type).toBe("error");
      expect(event.requestId).toBe(expectedId);
      expect(event.payload).toEqual({
        error: "PlaygroundUnavailable",
        message: PLAYGROUND_UNAVAILABLE_MESSAGE,
        statusCode: 503,
      });
      expect(result.current.isStreaming).toBe(false);
    },
  );
});
