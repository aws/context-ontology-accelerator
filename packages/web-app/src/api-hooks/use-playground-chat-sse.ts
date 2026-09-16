// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Orchestrates the Playground chat via SSE streaming.
 *
 * Replaces the WebSocket-based use-playground-chat.ts:
 * - Queries via HTTP POST (use-playground-stream)
 * - Sessions via HTTP POST actions (use-sessions)
 * - Token buffering preserved (use-token-buffer)
 * - No row buffering needed (full rows in single done event)
 * - No connection state (no heartbeat, no reconnection)
 */
import { useCallback, useEffect, useReducer, useRef } from "react";
import type {
  ChatMessage,
  ExecutionMode,
  QueryStrategy,
  StreamingEvent,
} from "@app-types/playground";
import { usePlaygroundStream } from "./use-playground-stream";
import { INITIAL_STATE, playgroundReducer } from "./playground-reducer";
import type { PlaygroundState } from "./playground-reducer";
import { useSessions } from "./use-sessions";
import { useTokenBuffer } from "./use-token-buffer";

/**
 * Build the request's `options` object, omitting it entirely when nothing is set.
 *
 * Written as one helper rather than two conditional spreads because
 * `...(mode && { options: { mode } })` followed by the same for `strategy` would
 * have the second overwrite the first — both assign the whole `options` key.
 * Omitting the object when empty keeps the request identical to before for a
 * caller that sets neither.
 *
 * Exported for test: a silent regression here would drop `mode` as well as
 * `strategy`, and the Playground page test mocks this whole hook, so there is no
 * other coverage of the request shape.
 */
export function buildOptions(
  mode?: ExecutionMode,
  strategy?: QueryStrategy,
): { options?: { mode?: ExecutionMode; strategy?: QueryStrategy } } {
  const options = {
    ...(mode && { mode }),
    ...(strategy && { strategy }),
  };
  return Object.keys(options).length > 0 ? { options } : {};
}

export interface UsePlaygroundChatSSEOptions {
  queryEndpoint: string | undefined;
  oidcConfig: { authority: string; clientId: string } | undefined;
  namespaceId: string;
}

export interface UsePlaygroundChatSSEReturn {
  state: PlaygroundState;
  sessionId: string | null;
  isRestoring: boolean;
  showRestoreSpinner: boolean;
  restoreTimedOut: boolean;
  dismissRestoreWarning: () => void;
  isStreaming: boolean;
  sendQuery: (
    query: string,
    mode?: ExecutionMode,
    strategy?: QueryStrategy,
  ) => void;
  retryQuery: (
    message: ChatMessage,
    mode?: ExecutionMode,
    strategy?: QueryStrategy,
  ) => void;
  newChat: () => void;
  hasMoreHistory: boolean;
  isLoadingOlderHistory: boolean;
  requestOlderHistory: () => void;
}

export function usePlaygroundChatSSE({
  queryEndpoint,
  oidcConfig,
  namespaceId,
}: UsePlaygroundChatSSEOptions): UsePlaygroundChatSSEReturn {
  const [state, dispatch] = useReducer(playgroundReducer, INITIAL_STATE);

  const messagesRef = useRef(state.messages);
  useEffect(() => {
    messagesRef.current = state.messages;
  }, [state.messages]);

  // ── Token buffering (75ms flush, same as WS version) ──
  const tokenBuffer = useTokenBuffer(dispatch);

  // Delayed "response.assemble" step for UX polish (same as WS version)
  const pendingTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(
    new Set(),
  );
  useEffect(() => {
    const timers = pendingTimersRef.current;
    return () => {
      timers.forEach((id) => clearTimeout(id));
      timers.clear();
    };
  }, []);

  // ── Sessions (REST-based) ──
  const sessions = useSessions({
    queryEndpoint,
    oidcConfig,
    namespace: namespaceId,
    dispatch,
  });

  // ── Streaming event handler ──
  const handleEvent = useCallback(
    (event: StreamingEvent) => {
      switch (event.type) {
        case "step":
          dispatch({
            type: "STEP",
            requestId: event.requestId,
            step: {
              step: event.payload.stepName,
              status: event.payload.status,
              durationMs: event.payload.durationMs,
              detail: event.payload.detail,
              toolUsed: event.payload.toolUsed,
            },
          });
          break;

        case "token":
          tokenBuffer.append(event.requestId, event.payload.text);
          break;

        case "done": {
          // Capture session ID from streaming response
          if (event.sessionId) {
            sessions.captureSessionId(event.sessionId);
          }
          // Flush any buffered tokens
          const buffered = tokenBuffer.flush(event.requestId);
          if (buffered) {
            dispatch({
              type: "TOKEN",
              requestId: event.requestId,
              text: buffered,
            });
          }
          // Delayed "response.assemble" step for UX polish
          const t1 = setTimeout(() => {
            pendingTimersRef.current.delete(t1);
            dispatch({
              type: "STEP",
              requestId: event.requestId,
              step: {
                step: "response.assemble",
                status: "active",
                durationMs: 0,
              },
            });
            const t2 = setTimeout(() => {
              pendingTimersRef.current.delete(t2);
              dispatch({
                type: "DONE",
                requestId: event.requestId,
                response: {
                  result: event.payload.result,
                  requestId: event.requestId,
                },
              });
            }, 1500);
            pendingTimersRef.current.add(t2);
          }, 200);
          pendingTimersRef.current.add(t1);
          break;
        }

        case "error":
          tokenBuffer.clear(event.requestId);
          dispatch({
            type: "ERROR",
            requestId: event.requestId,
            error: event.payload.error,
            message: event.payload.message,
          });
          break;

        default:
          // Unknown event type, skip
          break;
      }
    },
    [tokenBuffer, sessions],
  );

  // ── SSE stream hook ──
  const { submit, isStreaming, cancel } = usePlaygroundStream({
    queryEndpoint,
    oidcConfig,
    onEvent: handleEvent,
  });

  // ── Public API ──

  const sendQuery = useCallback(
    (query: string, mode?: ExecutionMode, strategy?: QueryStrategy) => {
      if (!namespaceId || isStreaming || sessions.isRestoring) return;

      const requestId = crypto.randomUUID();
      const assistantMsgId = crypto.randomUUID();

      dispatch({ type: "SEND_QUERY", id: assistantMsgId, requestId, query });

      void submit({
        query,
        namespace: namespaceId,
        requestId,
        sessionId: sessions.getSessionId() ?? undefined,
        ...buildOptions(mode, strategy),
      });
    },
    [namespaceId, submit, sessions, isStreaming],
  );

  const retryQuery = useCallback(
    (message: ChatMessage, mode?: ExecutionMode, strategy?: QueryStrategy) => {
      if (!namespaceId || message.role !== "assistant") return;
      if (message.isLoading || isStreaming) return;
      if (!message.replyToId) return;

      const userMsg = messagesRef.current.find(
        (m) => m.id === message.replyToId,
      );
      if (!userMsg || userMsg.role !== "user") return;

      const requestId = crypto.randomUUID();
      dispatch({ type: "RETRY", messageId: message.id, requestId });

      void submit({
        query: userMsg.content,
        namespace: namespaceId,
        requestId,
        sessionId: sessions.getSessionId() ?? undefined,
        ...buildOptions(mode, strategy),
      });
    },
    [namespaceId, submit, sessions, isStreaming],
  );

  const newChat = useCallback(() => {
    cancel(); // Cancel any in-flight stream
    void sessions.createSession();
  }, [cancel, sessions]);

  return {
    state,
    sessionId: sessions.sessionId,
    isRestoring: sessions.isRestoring,
    showRestoreSpinner: sessions.showRestoreSpinner,
    restoreTimedOut: sessions.restoreTimedOut,
    dismissRestoreWarning: sessions.dismissRestoreWarning,
    isStreaming,
    sendQuery,
    newChat,
    retryQuery,
    hasMoreHistory: sessions.hasMoreHistory,
    isLoadingOlderHistory: sessions.isLoadingOlderHistory,
    requestOlderHistory: sessions.loadOlderHistory,
  };
}
