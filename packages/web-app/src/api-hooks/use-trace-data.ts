// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Hook that resolves trace data for the Trace Detail page.
 *
 * Data sources (in priority order):
 * 1. Router state (passed from Playground via "Open full trace")
 * 2. REST fetch from session history (direct URL access or page reload)
 */
import { useMemo } from "react";
import { useLocation, useParams } from "react-router-dom";
import type {
  ChatMessage,
  ExecutionMode,
  HistoryMessage,
  TraceStep,
} from "@app-types";
import { useSessionHistory } from "./use-session-history";

export interface TraceData {
  trace: TraceStep[];
  tier: number;
  content: string;
  queryUsed?: string;
  sparqlGenerated?: string;
  requestId: string;
  guardrailBlocked: boolean;
  metadata: Record<string, unknown>;
  confidence?: { score: number; rationale: string };
  userQuery: string;
  /**
   * Resolved execution mode ("standard" / "deep-reasoning"). Threaded so the
   * full-trace "Tier outcome" badge can follow the same mode-wins rule as the
   * Rationale panel: a Deep Reasoning answer resolves at tier 3 but must not
   * read "Tier 3 — knowledge" (#1166). Absent on responses that omit it, in
   * which case the badge falls back to the tier label.
   */
  executionMode?: ExecutionMode;
}

export interface UseTraceDataReturn {
  data: TraceData | null;
  loading: boolean;
  error: string | null;
  retry: () => void;
}

interface LocationState {
  message?: ChatMessage;
  query?: string;
}

function isLocationState(value: unknown): value is LocationState {
  if (typeof value !== "object" || value === null) return false;
  return "message" in value || Object.keys(value).length === 0;
}

function toRecord(
  value: Record<string, unknown> | undefined | null,
): Record<string, unknown> {
  return value ?? {};
}

function resolveFromRouterState(
  state: LocationState,
  requestIdParam: string | undefined,
): TraceData | null {
  const { message, query } = state;
  if (!message?.response) return null;
  const { result } = message.response;
  return {
    trace: result.trace ?? message.liveTraceSteps ?? [],
    tier: result.tier,
    content: message.content,
    queryUsed: result.queryUsed,
    sparqlGenerated: result.sparqlGenerated,
    requestId: requestIdParam ?? message.requestId ?? "unknown",
    guardrailBlocked: result.guardrailBlocked ?? false,
    metadata: toRecord(result.metadata),
    confidence: result.confidence,
    userQuery: query ?? "—",
    executionMode: result.executionMode,
  };
}

function resolveFromHistory(
  msg: HistoryMessage,
  userQuery: string,
  requestId: string,
): TraceData {
  const meta = msg.metadata ?? {};
  // The typed history metadata doesn't expose executionMode, but the backend
  // stamps the resolved mode into the free-form inner metadata as `mode`
  // (orchestrator sets result.metadata["mode"] = "deep-reasoning"). Read it and
  // coerce to ExecutionMode so the badge can apply the same mode-wins rule.
  const rawMode = meta.metadata?.mode;
  const executionMode: ExecutionMode | undefined =
    rawMode === "deep-reasoning" || rawMode === "standard"
      ? rawMode
      : undefined;
  return {
    trace: meta.trace ?? [],
    tier: meta.tier ?? 0,
    content: msg.content,
    queryUsed: meta.queryUsed,
    sparqlGenerated: meta.sparqlGenerated,
    requestId,
    guardrailBlocked: meta.guardrailBlocked ?? false,
    metadata: toRecord(meta.metadata),
    confidence: meta.confidence,
    userQuery: userQuery !== "—" ? userQuery : (meta.query ?? "—"),
    executionMode,
  };
}

export function useTraceData(): UseTraceDataReturn {
  const { sessionId, requestId } = useParams<{
    sessionId: string;
    requestId: string;
  }>();
  const location = useLocation();

  const locationState: LocationState = isLocationState(location.state)
    ? location.state
    : {};

  const fromRouterState = useMemo(
    () => resolveFromRouterState(locationState, requestId),
    // location.state is stable per navigation
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [location.key],
  );

  const needsFetch = !fromRouterState;
  const {
    data: historyResult,
    loading,
    error,
    retry,
  } = useSessionHistory(
    needsFetch ? sessionId : undefined,
    needsFetch ? requestId : undefined,
  );

  const fromHistory = useMemo(() => {
    if (!historyResult?.message || !requestId) return null;
    return resolveFromHistory(
      historyResult.message,
      historyResult.userQuery,
      requestId,
    );
  }, [historyResult, requestId]);

  return {
    data: fromRouterState ?? fromHistory,
    loading,
    error,
    retry,
  };
}
