// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * State reducer for the Playground chat — handles streaming events
 * and manages message lifecycle from query submission through resolution.
 */
import type {
  ChatMessage,
  HistoryMessage,
  PlaygroundResponse,
  TraceStep,
} from "@app-types/playground";
import { normalizeResponse } from "./normalize-response";
import { STEP_LABELS, humanizeStepId } from "@components/Chat/step-labels";
import { normalizeGraphContext } from "@utils/graph-context";

// ── Actions ───────────────────────────────────────────────────────────

export type PlaygroundAction =
  | { type: "SEND_QUERY"; id: string; requestId: string; query: string }
  | { type: "RETRY"; messageId: string; requestId: string }
  | { type: "STEP"; requestId: string; step: TraceStep }
  | { type: "TOKEN"; requestId: string; text: string }
  | { type: "DONE"; requestId: string; response: PlaygroundResponse }
  | { type: "ERROR"; requestId: string; error: string; message: string }
  | { type: "RESET" }
  | { type: "RESTORE_HISTORY"; messages: HistoryMessage[] }
  | { type: "PREPEND_HISTORY"; messages: HistoryMessage[] };

// ── State ─────────────────────────────────────────────────────────────

export interface PlaygroundState {
  messages: ChatMessage[];
}

export const INITIAL_STATE: PlaygroundState = { messages: [] };

// ── Reducer ───────────────────────────────────────────────────────────

export function playgroundReducer(
  state: PlaygroundState,
  action: PlaygroundAction,
): PlaygroundState {
  switch (action.type) {
    case "SEND_QUERY": {
      const userMsgId = crypto.randomUUID();
      return {
        messages: [
          ...state.messages,
          {
            id: userMsgId,
            role: "user",
            content: action.query,
            timestamp: new Date(),
          },
          {
            id: action.id,
            role: "assistant",
            content: "",
            timestamp: new Date(),
            isLoading: true,
            requestId: action.requestId,
            replyToId: userMsgId,
          },
        ],
      };
    }

    case "RETRY":
      return {
        messages: state.messages.map((msg) =>
          msg.id === action.messageId
            ? {
                ...msg,
                isLoading: true,
                error: undefined,
                response: undefined,
                content: "",
                stage: undefined,
                streamingTokens: undefined,
                liveTraceSteps: undefined,
                requestId: action.requestId,
              }
            : msg,
        ),
      };

    case "STEP": {
      return {
        messages: state.messages.map((msg) =>
          msg.isLoading && msg.requestId === action.requestId
            ? {
                ...msg,
                liveTraceSteps: [...(msg.liveTraceSteps ?? []), action.step],
                stage: formatStepAsStage(action.step.step),
              }
            : msg,
        ),
      };
    }

    case "TOKEN":
      return {
        messages: state.messages.map((msg) =>
          msg.isLoading && msg.requestId === action.requestId
            ? {
                ...msg,
                streamingTokens: (msg.streamingTokens ?? "") + action.text,
              }
            : msg,
        ),
      };

    case "DONE": {
      const normalized = normalizeResponse(action.response);
      if (!normalized) {
        return {
          messages: state.messages.map((msg) =>
            msg.isLoading && msg.requestId === action.requestId
              ? {
                  ...msg,
                  isLoading: false,
                  error: {
                    error: "InvalidResponse",
                    message: "Received malformed response from server",
                    statusCode: 0,
                  },
                  content: "Received malformed response from server",
                  stage: undefined,
                  streamingTokens: undefined,
                }
              : msg,
          ),
        };
      }
      const result = normalized.result;
      const isGuardrailBlocked = result.guardrailBlocked ?? false;
      const content = isGuardrailBlocked
        ? (result.synthesizedAnswer ?? "Response blocked by content guardrail.")
        : formatResponseContent(normalized);
      return {
        messages: state.messages.map((msg) =>
          msg.isLoading && msg.requestId === action.requestId
            ? {
                ...msg,
                isLoading: false,
                response: normalized,
                content,
                // Clear streaming state
                stage: undefined,
                streamingTokens: isGuardrailBlocked
                  ? undefined
                  : msg.streamingTokens,
              }
            : msg,
        ),
      };
    }

    case "ERROR":
      return {
        messages: state.messages.map((msg) =>
          msg.isLoading && msg.requestId === action.requestId
            ? {
                ...msg,
                isLoading: false,
                error: {
                  error: action.error,
                  message: action.message,
                  statusCode: 0,
                },
                content: action.message,
                stage: undefined,
                streamingTokens: undefined,
              }
            : msg,
        ),
      };

    case "RESET":
      return INITIAL_STATE;

    case "RESTORE_HISTORY":
      return { messages: historyMessagesToChatMessages(action.messages) };

    case "PREPEND_HISTORY":
      return {
        messages: [
          ...historyMessagesToChatMessages(action.messages),
          ...state.messages,
        ],
      };

    default:
      return state;
  }
}

// ── Helpers ───────────────────────────────────────────────────────────

/** Convert HistoryMessage[] from session restore into ChatMessage[]. */
function historyMessagesToChatMessages(
  messages: HistoryMessage[],
): ChatMessage[] {
  let lastUserId: string | undefined;
  return messages.map((msg) => {
    const role: "user" | "assistant" =
      msg.role === "user" ? "user" : "assistant";
    const id = crypto.randomUUID();
    const base = {
      id,
      role,
      content: msg.content,
      timestamp: new Date(),
      isRestored: true,
    };
    if (role === "user") {
      lastUserId = id;
      return base;
    }
    const meta = msg.metadata;
    const { graphContext: rawGraphContext, ...canonicalMeta } = meta ?? {};
    const graphContext = normalizeGraphContext(rawGraphContext);
    return {
      ...base,
      replyToId: lastUserId,
      requestId: canonicalMeta.requestId,
      response: {
        result: {
          tier: 0,
          confidence: { score: 0, rationale: "" },
          trace: [],
          partial: false,
          synthesizedAnswer: msg.content,
          ...canonicalMeta,
          ...(graphContext && { graphContext }),
        },
        requestId: canonicalMeta.requestId,
      },
      liveTraceSteps: canonicalMeta.trace,
    };
  });
}

export function formatResponseContent(response: PlaygroundResponse): string {
  const { result } = response;
  if (result.synthesizedAnswer) return result.synthesizedAnswer;
  if (result.resultRows && result.resultRows.length > 0) {
    return `${result.resultRows.length} row(s) returned`;
  }
  return "Response received";
}

/** Map internal step names to user-friendly progress labels (shared map + ellipsis). */
function formatStepAsStage(stepName: string): string {
  return `${STEP_LABELS[stepName] ?? humanizeStepId(stepName)}…`;
}
