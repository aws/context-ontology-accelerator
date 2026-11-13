// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * SSE streaming hook for the Playground chat: POSTs to the AgentCore Runtime
 * /invocations endpoint and parses the stream with `eventsource-parser`.
 * Session affinity uses the X-Amzn-Bedrock-AgentCore-Runtime-Session-Id header.
 * The caller does any token buffering.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { EventSourceParserStream } from "eventsource-parser/stream";
import { OIDCProvider } from "@auth";
import type {
  PlaygroundRequest,
  StreamingEvent,
  StreamingErrorEvent,
} from "@app-types/playground";
import { isStreamingEvent } from "@utils/sse-type-guards";
import { deriveRuntimeSessionId } from "@utils/runtime-session-id";

/** Shown when the Playground has no query service to send to. */
export const PLAYGROUND_UNAVAILABLE_MESSAGE =
  "The Playground's query service is not available in this environment yet, so questions cannot be sent.";

export interface UsePlaygroundStreamOptions {
  /** AgentCore Runtime invocations endpoint (public Bedrock endpoint, fetched cross-origin). */
  queryEndpoint: string | undefined;
  oidcConfig: { authority: string; clientId: string } | undefined;
  onEvent: (event: StreamingEvent) => void;
}

export interface UsePlaygroundStreamReturn {
  submit: (request: PlaygroundRequest) => void;
  isStreaming: boolean;
  cancel: () => void;
}

export function usePlaygroundStream({
  queryEndpoint,
  oidcConfig,
  onEvent,
}: UsePlaygroundStreamOptions): UsePlaygroundStreamReturn {
  const [isStreaming, setIsStreaming] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);
  const onEventRef = useRef(onEvent);

  useEffect(() => {
    onEventRef.current = onEvent;
  });

  const submit = useCallback(
    async (request: PlaygroundRequest) => {
      if (!queryEndpoint || !oidcConfig) {
        // Emit an error so the request ends instead of spinning forever.
        onEventRef.current(
          makeErrorEvent(
            request.requestId ?? "",
            503,
            PLAYGROUND_UNAVAILABLE_MESSAGE,
            "PlaygroundUnavailable",
          ),
        );
        return;
      }

      // Cancel any in-flight stream.
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      setIsStreaming(true);

      // Get ID token and user sub (carries email + groups for Cedar auth; AgentCore validates aud)
      // Sub feeds X-Amzn-Bedrock-AgentCore-Runtime-Session-Id (must be ≥33 chars,
      // sticky routing) — deriveRuntimeSessionId pads/normalizes since some IdPs
      // issue short subs (e.g. "jdoe") that fail that constraint as-is.
      let token: string;
      let runtimeSessionId: string | undefined;
      try {
        const provider = OIDCProvider.build(oidcConfig);
        token = await provider.getIdToken();
        const user = await provider.getUser();
        runtimeSessionId = deriveRuntimeSessionId(user?.profile?.sub);
        if (!token) {
          onEventRef.current(
            makeErrorEvent(
              request.requestId ?? "",
              401,
              "Authentication token is empty. Please sign in again.",
            ),
          );
          setIsStreaming(false);
          return;
        }
      } catch {
        onEventRef.current(
          makeErrorEvent(
            request.requestId ?? "",
            401,
            "Failed to retrieve authentication token. Please sign in again.",
          ),
        );
        setIsStreaming(false);
        return;
      }

      try {
        const response = await fetch(queryEndpoint, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
            ...(runtimeSessionId && {
              "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": runtimeSessionId,
            }),
          },
          body: JSON.stringify({ ...request, stream: true }),
          signal: controller.signal,
        });

        // Pre-stream error: HTTP 4xx/5xx before streaming starts
        if (!response.ok) {
          let errorBody = "";
          try {
            errorBody = await response.text();
          } catch {
            // ignore read failure
          }
          onEventRef.current(
            makeErrorEvent(
              request.requestId ?? "",
              response.status,
              errorBody || `HTTP ${response.status}`,
            ),
          );
          setIsStreaming(false);
          return;
        }

        if (!response.body) {
          onEventRef.current(
            makeErrorEvent(
              request.requestId ?? "",
              0,
              "Response body is empty",
            ),
          );
          setIsStreaming(false);
          return;
        }

        // Pipe response through eventsource-parser's TransformStream
        const eventStream = response.body
          .pipeThrough(new TextDecoderStream())
          .pipeThrough(new EventSourceParserStream());

        const reader = eventStream.getReader();

        while (true) {
          const { done, value: sseMessage } = await reader.read();
          if (done) break;

          // AgentCore sends each yielded dict as: data: {json}\n\n
          // eventsource-parser delivers { data: string, event?: string }
          // The event type lives inside the JSON payload as "type" field.
          const data = (sseMessage as { data?: string; event?: string }).data;
          if (!data) continue;

          try {
            const parsed: unknown = JSON.parse(data);
            if (isStreamingEvent(parsed)) {
              onEventRef.current(parsed);
            }
          } catch {
            // Non-JSON data line, skip (e.g., keepalive or comment)
          }
        }
      } catch (err: unknown) {
        // AbortError: user cancelled via new query or navigation, suppress
        if (err instanceof DOMException && err.name === "AbortError") {
          return;
        }
        onEventRef.current(
          makeErrorEvent(request.requestId ?? "", 0, "Stream reading failed"),
        );
      } finally {
        setIsStreaming(false);
      }
    },
    [queryEndpoint, oidcConfig],
  );

  const cancel = useCallback(() => {
    controllerRef.current?.abort();
  }, []);

  // Cleanup on unmount: abort any in-flight stream to prevent orphaned requests
  useEffect(() => {
    return () => {
      controllerRef.current?.abort();
    };
  }, []);

  return { submit, isStreaming, cancel };
}

/** Construct a typed error event for the reducer. */
function makeErrorEvent(
  requestId: string,
  statusCode: number,
  message: string,
  error = statusCode === 401 ? "AuthError" : "StreamError",
): StreamingErrorEvent {
  return {
    type: "error",
    requestId,
    timestamp: new Date().toISOString(),
    payload: {
      error,
      message,
      statusCode,
    },
  };
}
