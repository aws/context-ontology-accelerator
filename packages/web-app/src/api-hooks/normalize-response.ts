// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Validates and normalizes raw backend responses into a well-typed
 * PlaygroundResponse with safe defaults for all required fields.
 */
import type { PlaygroundResponse, TraceStep } from "@app-types/playground";
import { normalizeGraphContext } from "@utils/graph-context";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isTraceStep(value: unknown): value is TraceStep {
  return (
    isRecord(value) &&
    typeof value.step === "string" &&
    typeof value.status === "string" &&
    typeof value.durationMs === "number"
  );
}

/**
 * Normalize a raw response from the backend, filling in safe defaults
 * for any missing or malformed fields. Returns null if the response
 * is fundamentally unusable.
 */
export function normalizeResponse(raw: unknown): PlaygroundResponse | null {
  if (!raw || !isRecord(raw)) {
    return null;
  }

  const rawResult = raw.result;
  if (!rawResult || !isRecord(rawResult)) {
    return null;
  }

  const tier = typeof rawResult.tier === "number" ? rawResult.tier : 0;

  let confidence: { score: number; rationale: string };
  if (isRecord(rawResult.confidence)) {
    const rawConf = rawResult.confidence;
    confidence = {
      score: typeof rawConf.score === "number" ? rawConf.score : 0,
      rationale: typeof rawConf.rationale === "string" ? rawConf.rationale : "",
    };
  } else {
    confidence = { score: 0, rationale: "" };
  }

  const trace = Array.isArray(rawResult.trace)
    ? rawResult.trace.filter(isTraceStep)
    : [];

  const partial =
    typeof rawResult.partial === "boolean" ? rawResult.partial : false;
  const graphContext = normalizeGraphContext(rawResult.graphContext);

  const result = {
    tier,
    confidence,
    trace,
    partial,
    ...(typeof rawResult.synthesizedAnswer === "string" && {
      synthesizedAnswer: rawResult.synthesizedAnswer,
    }),
    ...(Array.isArray(rawResult.resultRows) && {
      resultRows: rawResult.resultRows as Record<string, unknown>[],
    }),
    ...(typeof rawResult.queryUsed === "string" && {
      queryUsed: rawResult.queryUsed,
    }),
    ...(typeof rawResult.sparqlGenerated === "string" && {
      sparqlGenerated: rawResult.sparqlGenerated,
    }),
    ...(Array.isArray(rawResult.supportingContent) && {
      supportingContent:
        rawResult.supportingContent as PlaygroundResponse["result"]["supportingContent"],
    }),
    ...(graphContext && { graphContext }),
    ...(Array.isArray(rawResult.dataSources) && {
      dataSources: rawResult.dataSources as string[],
    }),
    ...(typeof rawResult.guardrailBlocked === "boolean" && {
      guardrailBlocked: rawResult.guardrailBlocked,
    }),
    ...(typeof rawResult.ontologyVersion === "string" && {
      ontologyVersion: rawResult.ontologyVersion,
    }),
    ...(isRecord(rawResult.metadata) && {
      metadata: rawResult.metadata as PlaygroundResponse["result"]["metadata"],
    }),
  };

  return {
    result,
    ...(typeof raw.requestId === "string" && { requestId: raw.requestId }),
    ...(typeof raw.sessionId === "string" && { sessionId: raw.sessionId }),
  };
}
