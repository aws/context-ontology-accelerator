// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Orchestrates the Playground chat: SSE query stream, session actions and
 *  token buffering. */
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
 * Build the request's `options` object.
 *
 * `includeDebugInfo` is only a request: serve returns the SQL only to a caller
 * holding `viewSource` and the SPARQL only to one holding `viewOntology`.
 *
 * One helper rather than conditional spreads, because two
 * `...(x && { options: {...} })` spreads would overwrite each other.
 *
 * Exported for test: the Playground page test mocks this whole hook.
 */
export function buildOptions(
  mode?: ExecutionMode,
  strategy?: QueryStrategy,
): {
  options: {
    mode?: ExecutionMode;
    strategy?: QueryStrategy;
    includeDebugInfo: boolean;
  };
} {
  const options = {
    ...(mode && { mode }),
    ...(strategy && { strategy }),
    includeDebugInfo: true,
  };
  return { options };
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

  const tokenBuffer = useTokenBuffer(dispatch);

  // Delayed "response.assemble" step for UX polish.
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

  const sessions = useSessions({
    queryEndpoint,
    oidcConfig,
    namespace: namespaceId,
    dispatch,
  });

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
          if (event.sessionId) {
            sessions.captureSessionId(event.sessionId);
          }
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
          break;
      }
    },
    [tokenBuffer, sessions],
  );

  const { submit, isStreaming, cancel } = usePlaygroundStream({
    queryEndpoint,
    oidcConfig,
    onEvent: handleEvent,
  });

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
