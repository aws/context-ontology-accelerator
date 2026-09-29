// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { vi } from "vitest";
import {
  playgroundReducer,
  INITIAL_STATE,
  type PlaygroundState,
  type PlaygroundAction,
} from "./playground-reducer";
import type { PlaygroundResponse } from "@app-types/playground";

// Mock crypto.randomUUID for deterministic IDs
let uuidCounter = 0;
vi.stubGlobal("crypto", {
  randomUUID: () => `uuid-${++uuidCounter}`,
});

describe("playgroundReducer", () => {
  beforeEach(() => {
    uuidCounter = 0;
  });

  describe("SEND_QUERY", () => {
    it("creates user + assistant messages with replyToId", () => {
      const action: PlaygroundAction = {
        type: "SEND_QUERY",
        id: "assistant-1",
        requestId: "req-1",
        query: "What is the loss ratio?",
      };

      const state = playgroundReducer(INITIAL_STATE, action);

      expect(state.messages).toHaveLength(2);

      const userMsg = state.messages[0];
      expect(userMsg.role).toBe("user");
      expect(userMsg.content).toBe("What is the loss ratio?");
      expect(userMsg.id).toBe("uuid-1");

      const assistantMsg = state.messages[1];
      expect(assistantMsg.role).toBe("assistant");
      expect(assistantMsg.id).toBe("assistant-1");
      expect(assistantMsg.isLoading).toBe(true);
      expect(assistantMsg.requestId).toBe("req-1");
      expect(assistantMsg.replyToId).toBe("uuid-1");
      expect(assistantMsg.content).toBe("");
    });
  });

  describe("RETRY", () => {
    it("resets assistant message (clears error, sets isLoading, new requestId)", () => {
      const initial: PlaygroundState = {
        messages: [
          {
            id: "user-1",
            role: "user",
            content: "Hello",
            timestamp: new Date(),
          },
          {
            id: "assistant-1",
            role: "assistant",
            content: "Error occurred",
            timestamp: new Date(),
            isLoading: false,
            requestId: "old-req",
            error: { error: "Timeout", message: "Timed out", statusCode: 0 },
          },
        ],
      };

      const action: PlaygroundAction = {
        type: "RETRY",
        messageId: "assistant-1",
        requestId: "new-req",
      };

      const state = playgroundReducer(initial, action);

      const retried = state.messages[1];
      expect(retried.isLoading).toBe(true);
      expect(retried.error).toBeUndefined();
      expect(retried.response).toBeUndefined();
      expect(retried.content).toBe("");
      expect(retried.stage).toBeUndefined();
      expect(retried.streamingTokens).toBeUndefined();
      expect(retried.liveTraceSteps).toBeUndefined();
      expect(retried.requestId).toBe("new-req");
    });

    it("preserves other messages unchanged", () => {
      const initial: PlaygroundState = {
        messages: [
          {
            id: "user-1",
            role: "user",
            content: "First question",
            timestamp: new Date(),
          },
          {
            id: "assistant-1",
            role: "assistant",
            content: "First answer",
            timestamp: new Date(),
            isLoading: false,
            requestId: "req-1",
          },
          {
            id: "user-2",
            role: "user",
            content: "Second question",
            timestamp: new Date(),
          },
          {
            id: "assistant-2",
            role: "assistant",
            content: "Error",
            timestamp: new Date(),
            isLoading: false,
            requestId: "req-2",
            error: { error: "Fail", message: "Failed", statusCode: 500 },
          },
        ],
      };

      const action: PlaygroundAction = {
        type: "RETRY",
        messageId: "assistant-2",
        requestId: "req-3",
      };

      const state = playgroundReducer(initial, action);

      // First messages unchanged
      expect(state.messages[0].content).toBe("First question");
      expect(state.messages[1].content).toBe("First answer");
      expect(state.messages[1].requestId).toBe("req-1");
      expect(state.messages[2].content).toBe("Second question");

      // Only the target was retried
      expect(state.messages[3].requestId).toBe("req-3");
      expect(state.messages[3].isLoading).toBe(true);
    });
  });

  describe("RESTORE_HISTORY", () => {
    it("replaces all messages with isRestored flag", () => {
      const initial: PlaygroundState = {
        messages: [
          {
            id: "existing",
            role: "user",
            content: "old msg",
            timestamp: new Date(),
          },
        ],
      };

      const action: PlaygroundAction = {
        type: "RESTORE_HISTORY",
        messages: [
          { role: "user", content: "Restored question" },
          { role: "assistant", content: "Restored answer" },
        ],
      };

      const state = playgroundReducer(initial, action);

      expect(state.messages).toHaveLength(2);
      expect(state.messages[0].content).toBe("Restored question");
      expect(state.messages[0].role).toBe("user");
      expect(state.messages[0].isRestored).toBe(true);

      expect(state.messages[1].content).toBe("Restored answer");
      expect(state.messages[1].role).toBe("assistant");
      expect(state.messages[1].isRestored).toBe(true);
      expect(state.messages[1].response).toBeDefined();
      expect(state.messages[1].response!.result.synthesizedAnswer).toBe(
        "Restored answer",
      );
    });

    it("restores resultRows in response for table rendering", () => {
      const rows = [
        { claim_id: "CL-44201", status: "Open", amount: "$14,200" },
        { claim_id: "CL-44318", status: "Open", amount: "$6,800" },
      ];

      const action: PlaygroundAction = {
        type: "RESTORE_HISTORY",
        messages: [
          { role: "user", content: "list open claims" },
          {
            role: "assistant",
            content: "",
            metadata: {
              tier: 1,
              confidence: { score: 0.92, rationale: "Exact match" },
              resultRows: rows,
              queryUsed: "SELECT * FROM claims WHERE status = 'Open'",
              trace: [
                { step: "metric_match", status: "success", durationMs: 50 },
              ],
            },
          },
        ],
      };

      const state = playgroundReducer(INITIAL_STATE, action);

      const assistantMsg = state.messages[1];
      expect(assistantMsg.role).toBe("assistant");
      expect(assistantMsg.isRestored).toBe(true);
      expect(assistantMsg.response).toBeDefined();
      expect(assistantMsg.response!.result.resultRows).toEqual(rows);
      expect(assistantMsg.response!.result.resultRows).toHaveLength(2);
      expect(assistantMsg.response!.result.queryUsed).toBe(
        "SELECT * FROM claims WHERE status = 'Open'",
      );
      expect(assistantMsg.response!.result.tier).toBe(1);
    });

    it("normalizes a restored legacy top-level graph entity array", () => {
      const action: PlaygroundAction = {
        type: "RESTORE_HISTORY",
        messages: [
          { role: "user", content: "show graph evidence" },
          {
            role: "assistant",
            content: "Restored graph answer",
            metadata: {
              graphContext: [
                {
                  uri: "e:legacy",
                  label: "Legacy entity",
                  relationships: [
                    {
                      predicate: "RELATED_TO",
                      target_uri: "e:target",
                      target_label: "Target",
                    },
                  ],
                },
              ],
            },
          },
        ],
      };

      const state = playgroundReducer(INITIAL_STATE, action);

      expect(state.messages[1].response!.result.graphContext).toEqual({
        entities: [
          {
            uri: "e:legacy",
            label: "Legacy entity",
            relationships: [
              {
                predicate: "RELATED_TO",
                target_uri: "e:target",
                target_label: "Target",
              },
            ],
          },
        ],
      });
    });

    it("does not show raw array string as content for table results", () => {
      const action: PlaygroundAction = {
        type: "RESTORE_HISTORY",
        messages: [
          { role: "user", content: "total accounts" },
          {
            role: "assistant",
            content: "",
            metadata: {
              tier: 1,
              confidence: { score: 0.95, rationale: "Metric match" },
              resultRows: [{ total_accounts: "47" }],
              trace: [],
            },
          },
        ],
      };

      const state = playgroundReducer(INITIAL_STATE, action);

      const msg = state.messages[1];
      // Content should NOT be a raw array representation
      expect(msg.content).not.toContain("[{");
      expect(msg.content).not.toContain("'total_accounts'");
      // resultRows should be available for table rendering
      expect(msg.response!.result.resultRows).toHaveLength(1);
      expect(msg.response!.result.resultRows![0]).toEqual({
        total_accounts: "47",
      });
    });
  });

  describe("RESET", () => {
    it("returns initial state", () => {
      const populated: PlaygroundState = {
        messages: [
          {
            id: "msg-1",
            role: "user",
            content: "Hello",
            timestamp: new Date(),
          },
        ],
      };

      const state = playgroundReducer(populated, { type: "RESET" });

      expect(state).toEqual(INITIAL_STATE);
      expect(state.messages).toHaveLength(0);
    });
  });

  describe("DONE", () => {
    it("with valid response sets content and clears loading", () => {
      const initial: PlaygroundState = {
        messages: [
          {
            id: "assistant-1",
            role: "assistant",
            content: "",
            timestamp: new Date(),
            isLoading: true,
            requestId: "req-1",
            stage: "Executing...",
            streamingTokens: "partial text",
          },
        ],
      };

      const response: PlaygroundResponse = {
        result: {
          tier: 1,
          confidence: { score: 0.95, rationale: "High" },
          synthesizedAnswer: "The loss ratio is 71.3%",
          trace: [],
          partial: false,
        },
      };

      const action: PlaygroundAction = {
        type: "DONE",
        requestId: "req-1",
        response,
      };

      const state = playgroundReducer(initial, action);
      const msg = state.messages[0];

      expect(msg.isLoading).toBe(false);
      expect(msg.content).toBe("The loss ratio is 71.3%");
      expect(msg.response).toBeDefined();
      expect(msg.stage).toBeUndefined();
    });

    it("with null response (normalizer returns null) sets error state", () => {
      const initial: PlaygroundState = {
        messages: [
          {
            id: "assistant-1",
            role: "assistant",
            content: "",
            timestamp: new Date(),
            isLoading: true,
            requestId: "req-1",
          },
        ],
      };

      // Pass something that normalizeResponse will reject
      const action: PlaygroundAction = {
        type: "DONE",
        requestId: "req-1",
        response: "not-an-object" as unknown as PlaygroundResponse,
      };

      const state = playgroundReducer(initial, action);
      const msg = state.messages[0];

      expect(msg.isLoading).toBe(false);
      expect(msg.error).toBeDefined();
      expect(msg.error!.error).toBe("InvalidResponse");
      expect(msg.content).toBe("Received malformed response from server");
    });
  });

  describe("ERROR", () => {
    it("sets error and clears loading", () => {
      const initial: PlaygroundState = {
        messages: [
          {
            id: "assistant-1",
            role: "assistant",
            content: "",
            timestamp: new Date(),
            isLoading: true,
            requestId: "req-1",
            stage: "Translating...",
            streamingTokens: "partial",
          },
        ],
      };

      const action: PlaygroundAction = {
        type: "ERROR",
        requestId: "req-1",
        error: "InternalError",
        message: "Something went wrong",
      };

      const state = playgroundReducer(initial, action);
      const msg = state.messages[0];

      expect(msg.isLoading).toBe(false);
      expect(msg.error).toEqual({
        error: "InternalError",
        message: "Something went wrong",
        statusCode: 0,
      });
      expect(msg.content).toBe("Something went wrong");
      expect(msg.stage).toBeUndefined();
      expect(msg.streamingTokens).toBeUndefined();
    });
  });

  describe("PREPEND_HISTORY", () => {
    it("inserts older messages before existing ones", () => {
      const initial: PlaygroundState = {
        messages: [
          {
            id: "existing-1",
            role: "user",
            content: "Recent question",
            timestamp: new Date(),
          },
        ],
      };

      const action: PlaygroundAction = {
        type: "PREPEND_HISTORY",
        messages: [
          { role: "user", content: "Old question" },
          { role: "assistant", content: "Old answer" },
        ],
      };

      const state = playgroundReducer(initial, action);
      expect(state.messages).toHaveLength(3);
      expect(state.messages[0].content).toBe("Old question");
      expect(state.messages[1].content).toBe("Old answer");
      expect(state.messages[2].content).toBe("Recent question");
      expect(state.messages[2].id).toBe("existing-1");
    });

    it("marks prepended messages as restored", () => {
      const initial: PlaygroundState = { messages: [] };
      const action: PlaygroundAction = {
        type: "PREPEND_HISTORY",
        messages: [{ role: "user", content: "Old msg" }],
      };

      const state = playgroundReducer(initial, action);
      expect(state.messages[0].isRestored).toBe(true);
    });
  });
});
