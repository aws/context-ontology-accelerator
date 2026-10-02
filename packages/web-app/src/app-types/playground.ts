// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Types for the Playground chat UI, matching the Context Manager
 * SSE streaming request/response schema.
 */

/** A single message in a conversation history (user or assistant turn). */
export interface HistoryMessage {
  role: string;
  content: string;
  metadata?: {
    requestId?: string;
    query?: string;
    tier?: number;
    confidence?: { score: number; rationale: string };
    trace?: TraceStep[];
    queryUsed?: string;
    resultRows?: Record<string, unknown>[];
    sparqlGenerated?: string;
    guardrailBlocked?: boolean;
    graphContext?: GraphContextItem | GraphContextEntity[];
    metadata?: {
      modelId?: string;
      namespace?: string;
      principal?: string;
      suppressedRows?: number;
      [key: string]: unknown;
    };
  };
}

/**
 * Execution mode, sent as ``options.mode``. Orthogonal to the tier cascade:
 * ``deep-reasoning`` runs the multi-step reasoning loop; ``standard`` runs the
 * single-shot retriever. Absent lets serve use its deployment default, which
 * ships as standard (TIER3_STRATEGY=lexical-baseline) — so deep reasoning is
 * opt-in.
 */
export type ExecutionMode = "deep-reasoning" | "standard";

/**
 * Which Tier-2 engine answers a structured query, sent as `options.strategy`.
 *
 * A different axis from {@link ExecutionMode}: `mode` decides whether the whole
 * T1→T2→T3 cascade is replaced by the Tier-3 reasoning loop, this decides which
 * engine answers *within* Tier 2.
 *
 * Mirrors the Smithy `QueryStrategy` enum and `StrategyOption` in
 * `tier2/strategy.py`. Omitting it uses the serve default, `nl_to_sql_first`.
 */
export type QueryStrategy =
  | "best"
  | "ontop"
  | "nl_to_sql"
  | "ontop_first"
  | "nl_to_sql_first"
  | "deep-reasoning";

/** Session summary for history sidebar listing. */
export interface SessionSummary {
  sessionId: string;
  title: string;
  lastActiveAt: string;
  messageCount: number;
  namespaceId?: string;
}

export interface PlaygroundRequest {
  query: string;
  namespace: string;
  requestId?: string;
  sessionId?: string;
  profile?: Record<string, unknown>;
  options?: Record<string, unknown>;
}

export interface ConfidenceScore {
  score: number;
  rationale: string;
}

export interface TraceStep {
  step: string;
  status: string;
  durationMs: number;
  detail?: string | Record<string, unknown>;
  toolUsed?: string;
  parallelGroup?: string;
  wallMs?: number;
}

export interface QueryResult {
  tier: number;
  confidence: ConfidenceScore;
  resultRows?: Record<string, unknown>[];
  synthesizedAnswer?: string;
  queryUsed?: string;
  sparqlGenerated?: string;
  supportingContent?: SupportingContentItem[];
  graphContext?: GraphContextItem;
  trace: TraceStep[];
  ontologyVersion?: string;
  dataSources?: string[];
  guardrailBlocked?: boolean;
  partial: boolean;
  metadata?: {
    suppressedRows?: number;
    modelId?: string;
    namespace?: string;
    principal?: string;
    [key: string]: unknown;
  };
}

export interface SupportingContentItem {
  chunkId?: string;
  text?: string;
  sourceDocumentId?: string;
  sourceDocumentName?: string;
  // Legacy Tier-3 fields retained for rendering older responses.
  sourceDoc?: string;
  label?: string;
  relevanceScore?: number;
  [key: string]: unknown;
}

export interface GraphContextEntity {
  uri: string;
  label: string;
  type?: string;
  properties?: Record<string, unknown>;
  /** Legacy restored-session shape; new responses use top-level relationships. */
  relationships?: LegacyGraphContextRelationship[];
}

export interface GraphContextRelationship {
  sourceUri: string;
  predicateUri: string;
  targetUri: string;
  predicateLabel?: string;
}

export interface LegacyGraphContextRelationship {
  sourceUri?: string;
  source_uri?: string;
  predicateUri?: string;
  predicate_uri?: string;
  predicateLabel?: string;
  predicate_label?: string;
  predicate?: string;
  targetUri?: string;
  target_uri?: string;
  target?: string;
  targetLabel?: string;
  target_label?: string;
}

export interface GraphContextItem {
  entities?: GraphContextEntity[];
  relationships?: GraphContextRelationship[];
  [key: string]: unknown;
}

export interface PlaygroundResponse {
  result: QueryResult;
  requestId?: string;
  sessionId?: string;
}

export interface PlaygroundError {
  error: string;
  message: string;
  statusCode: number;
  requestId?: string;
  details?: { field: string; type: string }[];
}

// ── Streaming Event Protocol ──────────────────────────────────────────

export interface StreamingEventBase {
  requestId: string;
  timestamp: string;
  sessionId?: string;
}

export interface StepEvent extends StreamingEventBase {
  type: "step";
  payload: {
    stepName: string;
    status: "success" | "error" | "skipped" | "miss" | "degraded";
    durationMs: number;
    detail?: string;
    toolUsed?: string;
  };
}

export interface TokenEvent extends StreamingEventBase {
  type: "token";
  payload: { text: string };
}

export interface DoneEvent extends StreamingEventBase {
  type: "done";
  payload: { result: QueryResult };
}

export interface StreamingErrorEvent extends StreamingEventBase {
  type: "error";
  payload: { error: string; message: string; statusCode: number };
}

export interface RowsEvent extends StreamingEventBase {
  type: "rows";
  payload: {
    rows: Record<string, unknown>[];
    chunkIndex: number;
    totalChunks: number;
  };
}

export type StreamingEvent =
  | StepEvent
  | TokenEvent
  | DoneEvent
  | StreamingErrorEvent
  | RowsEvent;

// ── Chat Message (with streaming state) ───────────────────────────────

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
  response?: PlaygroundResponse;
  error?: PlaygroundError;
  isLoading?: boolean;
  /** Correlates assistant messages with their request for correct response matching. */
  requestId?: string;
  /** Links an assistant message to the user message it responds to. */
  replyToId?: string;

  // Streaming state (populated progressively by events)
  stage?: string;
  streamingTokens?: string;
  liveTraceSteps?: TraceStep[];
  /** True for messages restored from a previous session on page load. */
  isRestored?: boolean;
}
