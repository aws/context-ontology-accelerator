// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { normalizeResponse } from "./normalize-response";

describe("normalizeResponse", () => {
  it("normalizes a legacy top-level graph entity array", () => {
    const normalized = normalizeResponse({
      result: {
        tier: 3,
        confidence: { score: 0.8, rationale: "Graph evidence" },
        trace: [],
        partial: false,
        graphContext: [{ uri: "e:legacy", label: "Legacy entity" }],
      },
    });

    expect(normalized?.result.graphContext).toEqual({
      entities: [{ uri: "e:legacy", label: "Legacy entity" }],
    });
  });

  describe("returns null for unusable input", () => {
    it("returns null for undefined", () => {
      expect(normalizeResponse(undefined)).toBeNull();
    });

    it("returns null for null", () => {
      expect(normalizeResponse(null)).toBeNull();
    });

    it("returns null for non-object input (string)", () => {
      expect(normalizeResponse("hello")).toBeNull();
    });

    it("returns null for non-object input (number)", () => {
      expect(normalizeResponse(42)).toBeNull();
    });

    it("returns null for an array", () => {
      expect(normalizeResponse([1, 2, 3])).toBeNull();
    });

    it("returns null when result is missing", () => {
      expect(normalizeResponse({ requestId: "req-1" })).toBeNull();
    });

    it("returns null when result is not an object", () => {
      expect(normalizeResponse({ result: "not-an-object" })).toBeNull();
    });

    it("returns null when result is an array", () => {
      expect(normalizeResponse({ result: [1, 2] })).toBeNull();
    });

    it("returns null when result is null", () => {
      expect(normalizeResponse({ result: null })).toBeNull();
    });
  });

  describe("fills defaults for missing fields", () => {
    const minimalRaw = { result: {} };

    it("fills default tier (0) when tier is missing", () => {
      const normalized = normalizeResponse(minimalRaw);
      expect(normalized).not.toBeNull();
      expect(normalized!.result.tier).toBe(0);
    });

    it("fills default tier (0) when tier is not a number", () => {
      const normalized = normalizeResponse({ result: { tier: "high" } });
      expect(normalized!.result.tier).toBe(0);
    });

    it("fills default confidence when confidence is missing", () => {
      const normalized = normalizeResponse(minimalRaw);
      expect(normalized!.result.confidence).toEqual({
        score: 0,
        rationale: "",
      });
    });

    it("fills default confidence fields when confidence is partial", () => {
      const normalized = normalizeResponse({
        result: { confidence: { score: 0.8 } },
      });
      expect(normalized!.result.confidence).toEqual({
        score: 0.8,
        rationale: "",
      });
    });

    it("fills default confidence when confidence is not an object", () => {
      const normalized = normalizeResponse({
        result: { confidence: "high" },
      });
      expect(normalized!.result.confidence).toEqual({
        score: 0,
        rationale: "",
      });
    });

    it("fills default empty trace array when trace is missing", () => {
      const normalized = normalizeResponse(minimalRaw);
      expect(normalized!.result.trace).toEqual([]);
    });

    it("fills default trace when trace is not an array", () => {
      const normalized = normalizeResponse({
        result: { trace: "not-array" },
      });
      expect(normalized!.result.trace).toEqual([]);
    });

    it("fills default partial (false) when partial is missing", () => {
      const normalized = normalizeResponse(minimalRaw);
      expect(normalized!.result.partial).toBe(false);
    });
  });

  describe("preserves valid fields", () => {
    it("preserves executionMode 'deep-reasoning'", () => {
      const normalized = normalizeResponse({
        result: { executionMode: "deep-reasoning" },
      });
      expect(normalized!.result.executionMode).toBe("deep-reasoning");
    });

    it("preserves executionMode 'standard'", () => {
      const normalized = normalizeResponse({
        result: { executionMode: "standard" },
      });
      expect(normalized!.result.executionMode).toBe("standard");
    });

    it("omits executionMode when absent (Standard falls back to tier labels)", () => {
      const normalized = normalizeResponse({ result: { tier: 2 } });
      expect(normalized!.result.executionMode).toBeUndefined();
    });

    it("omits an unrecognized executionMode value", () => {
      const normalized = normalizeResponse({
        result: { executionMode: "agentic" },
      });
      expect(normalized!.result.executionMode).toBeUndefined();
    });

    it("preserves guardrailBlocked", () => {
      const normalized = normalizeResponse({
        result: { guardrailBlocked: true },
      });
      expect(normalized!.result.guardrailBlocked).toBe(true);
    });

    it("preserves trace when valid array", () => {
      const trace = [
        { step: "metric_match", status: "success", durationMs: 15 },
      ];
      const normalized = normalizeResponse({ result: { trace } });
      expect(normalized!.result.trace).toEqual(trace);
    });

    it("does not include requestId when it is not a string", () => {
      const normalized = normalizeResponse({
        requestId: 12345,
        result: { tier: 1 },
      });
      expect(normalized!.requestId).toBeUndefined();
    });

    it("does not include sessionId when it is not a string", () => {
      const normalized = normalizeResponse({
        sessionId: null,
        result: { tier: 1 },
      });
      expect(normalized!.sessionId).toBeUndefined();
    });
  });

  describe("full valid response", () => {
    it("handles a fully valid response correctly", () => {
      const raw = {
        requestId: "req-full",
        sessionId: "sess-full",
        result: {
          tier: 2,
          confidence: { score: 0.92, rationale: "High confidence match" },
          synthesizedAnswer: "The loss ratio is 71.3%",
          resultRows: [{ metric: "loss_ratio", value: 0.713 }],
          queryUsed: "SELECT loss_ratio FROM metrics",
          sparqlGenerated: "SELECT ?val WHERE { :loss_ratio :hasValue ?val }",
          trace: [
            { step: "metric_match", status: "success", durationMs: 15 },
            { step: "query_execute", status: "success", durationMs: 120 },
          ],
          dataSources: ["Athena"],
          partial: false,
          guardrailBlocked: false,
          ontologyVersion: "v2.3.1",
          metadata: { namespace: "insurance-prod", modelId: "claude-3-sonnet" },
        },
      };

      const normalized = normalizeResponse(raw);

      expect(normalized).not.toBeNull();
      expect(normalized!.requestId).toBe("req-full");
      expect(normalized!.sessionId).toBe("sess-full");
      expect(normalized!.result.tier).toBe(2);
      expect(normalized!.result.confidence).toEqual({
        score: 0.92,
        rationale: "High confidence match",
      });
      expect(normalized!.result.synthesizedAnswer).toBe(
        "The loss ratio is 71.3%",
      );
      expect(normalized!.result.resultRows).toEqual([
        { metric: "loss_ratio", value: 0.713 },
      ]);
      expect(normalized!.result.queryUsed).toBe(
        "SELECT loss_ratio FROM metrics",
      );
      expect(normalized!.result.sparqlGenerated).toBe(
        "SELECT ?val WHERE { :loss_ratio :hasValue ?val }",
      );
      expect(normalized!.result.trace).toHaveLength(2);
      expect(normalized!.result.dataSources).toEqual(["Athena"]);
      expect(normalized!.result.partial).toBe(false);
      expect(normalized!.result.guardrailBlocked).toBe(false);
      expect(normalized!.result.ontologyVersion).toBe("v2.3.1");
      expect(normalized!.result.metadata).toEqual({
        namespace: "insurance-prod",
        modelId: "claude-3-sonnet",
      });
    });
  });
});
