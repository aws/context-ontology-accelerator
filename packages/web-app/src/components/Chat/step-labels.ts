// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { AppMessage } from "../../i18n/appMessages";
import type { AppTranslate } from "../../i18n/LocaleProvider";

/**
 * Canonical trace step taxonomy → human-readable labels.
 *
 * Single source of truth shared by every trace UI surface:
 *  - LiveSteps (streaming spinner)        — uses STEP_LABELS
 *  - playground-reducer (stage string)    — uses STEP_LABELS (with ellipsis)
 *  - RationalePanel (execution plan)       — uses STEP_DISPLAY_NAMES
 *  - TracePanel (inline summary)           — uses HIDDEN_UNLESS_DENIED
 *
 * Step IDs follow the `<tier>.<action>` taxonomy emitted by the Python
 * serve layer. Keep this in sync with the backend trace.record() call sites.
 */

/** Short, present-tense labels shown while a step is the current/live step. */
export const STEP_LABELS: Readonly<Record<string, AppMessage>> = {
  "t1.metric_match": "Searching metric catalog",
  "t1.execute": "Running query",
  "query.embed": "Embedding query",
  "routing.select": "Routing query",
  "t2.sql.generate": "Generating SQL",
  "t2.sql.execute": "Running query",
  "t2.vkg.context": "Loading ontology context",
  "t2.vkg.translate": "Translating to SPARQL",
  "t2.vkg.validate": "Validating query",
  "t2.vkg.compile": "Translating SPARQL to SQL",
  "t2.vkg.retry": "Retrying translation",
  "t2.vkg.execute": "Running knowledge graph query",
  "t3.retrieve": "Searching knowledge sources",
  "t3.vector_search": "Searching knowledge base",
  "t3.graph_traverse": "Traversing knowledge graph",
  "t3.synthesize": "Synthesizing answer",
  "t3.guardrail": "Applying guardrail",
  "response.assemble": "Assembling response",
  // Deep-reasoning mode (options.mode="deep-reasoning"). These IDs are emitted bare
  // — without a `t1./t2./t3.` prefix — by the reasoning loop, so they must be listed
  // explicitly or the whole deep-reasoning trace renders unlabeled. The IDs keep
  // their `agentic_` prefix: they are the backend's own step taxonomy, not a
  // user-facing label.
  agentic_session_start: "Planning approach",
  decompose: "Breaking down the question",
  subquestion_start: "Working on a sub-question",
  next_step_select: "Choosing the next tool",
  tools_gated: "Selecting applicable tools",
  ontology_resolved: "Loading ontology",
  ontology_edge_filter: "Focusing the graph traversal",
  present_edges_discovered: "Discovering relationships",
  exploration_branch: "Following up a lead",
  no_progress_step: "Trying a different approach",
  ad_hoc_query: "Running a custom query",
  agentic_working_answer: "Drafting an answer",
  agentic_final_semantic_search: "Final knowledge-base sweep",
  agentic_retrieval_stop: "Finishing retrieval",
  llm_synthesize: "Synthesizing answer",
};

/** Noun-phrase labels shown in the detailed execution plan (RationalePanel). */
export const STEP_DISPLAY_NAMES: Readonly<Record<string, AppMessage>> = {
  "t1.metric_match": "Match metric",
  "t1.authorize": "Cedar authorize",
  "t1.firewall": "SQL Firewall",
  "t1.execute": "Execute metric query",
  "query.embed": "Embed query",
  "routing.select": "Route intent",
  "t2.sql.generate": "Generate SQL",
  "t2.sql.authorize": "Cedar authorize",
  "t2.sql.firewall": "SQL Firewall",
  "t2.sql.execute": "Execute SQL",
  "t2.vkg.context": "Assemble ontology context",
  "t2.vkg.translate": "Generate SPARQL",
  "t2.vkg.validate": "Validate SPARQL",
  "t2.vkg.compile": "Translate SPARQL → SQL",
  "t2.vkg.retry": "Retry SPARQL translation",
  "t2.vkg.authorize": "Cedar authorize",
  "t2.vkg.firewall": "SQL Firewall",
  "t2.vkg.execute": "Execute SQL via VKG",
  "t2.sql.failed": "NL→SQL strategy failed",
  "t2.vkg.failed": "VKG strategy failed",
  "t2.strategy_selected": "Strategy selected",
  "t3.retrieve": "Parallel retrieval",
  "t3.vector_search": "Vector search",
  "t3.graph_traverse": "Graph traversal",
  "t3.catalog_context": "Catalog context",
  "t3.synthesize": "Synthesize response",
  "t3.guardrail": "Bedrock guardrail",
  // Client-side only — emitted by use-playground-chat.ts, not the Python backend.
  "response.assemble": "Assemble response",
  // Legacy ID from the graphrag lexical-baseline retrieval path (not migrated
  // to the t3.* taxonomy); labeled explicitly so it doesn't fall back to raw.
  lexical_baseline_retrieve: "Lexical retrieval",
};

/**
 * Labels historically produced by humanizeStepId and used by the compact trace
 * panel/table. Keep their English copy stable while making known backend step
 * IDs translatable. Unknown extension step IDs still use the generic fallback.
 */
const TRACE_STEP_NAMES = {
  "t1.metric_match": "Metric Match",
  "t1.authorize": "Authorize",
  "t1.firewall": "Firewall",
  "t1.execute": "Execute",
  "query.embed": "Embed",
  "routing.select": "Select",
  "t2.sql.generate": "Sql Generate",
  "t2.sql.authorize": "Sql Authorize",
  "t2.sql.firewall": "Sql Firewall",
  "t2.sql.execute": "Sql Execute",
  "t2.sql.failed": "Sql Failed",
  "t2.vkg.context": "Vkg Context",
  "t2.vkg.translate": "Vkg Translate",
  "t2.vkg.validate": "Vkg Validate",
  "t2.vkg.compile": "Vkg Compile",
  "t2.vkg.retry": "Vkg Retry",
  "t2.vkg.authorize": "Vkg Authorize",
  "t2.vkg.firewall": "Vkg Firewall",
  "t2.vkg.execute": "Vkg Execute",
  "t2.vkg.failed": "Vkg Failed",
  "t2.strategy_selected": "Strategy Selected",
  "t3.retrieve": "Retrieve",
  "t3.vector_search": "Vector Search",
  "t3.graph_traverse": "Graph Traverse",
  "t3.catalog_context": "Catalog Context",
  "t3.synthesize": "Synthesize",
  "t3.guardrail": "Guardrail",
  "response.assemble": "Response Assemble",
  lexical_baseline_retrieve: "Lexical Baseline Retrieve",
  agentic_session_start: "Agentic Session Start",
  decompose: "Decompose",
  subquestion_start: "Subquestion Start",
  next_step_select: "Next Step Select",
  tools_gated: "Tools Gated",
  ontology_resolved: "Ontology Resolved",
  ontology_edge_filter: "Ontology Edge Filter",
  present_edges_discovered: "Present Edges Discovered",
  exploration_branch: "Exploration Branch",
  no_progress_step: "No Progress Step",
  ad_hoc_query: "Ad Hoc Query",
  agentic_working_answer: "Agentic Working Answer",
  agentic_final_semantic_search: "Agentic Final Semantic Search",
  agentic_retrieval_stop: "Agentic Retrieval Stop",
  llm_synthesize: "Llm Synthesize",
} satisfies Record<string, AppMessage>;

export function localizeLiveStep(t: AppTranslate, stepId: string): string {
  const message = STEP_LABELS[stepId as keyof typeof STEP_LABELS];
  return message ? t(message) : humanizeStepId(stepId);
}

export function localizeDisplayStep(t: AppTranslate, stepId: string): string {
  const message = STEP_DISPLAY_NAMES[stepId as keyof typeof STEP_DISPLAY_NAMES];
  return message ? t(message) : humanizeStepId(stepId);
}

export function localizeTraceStep(t: AppTranslate, stepId: string): string {
  const message = TRACE_STEP_NAMES[stepId as keyof typeof TRACE_STEP_NAMES];
  return message ? t(message) : humanizeStepId(stepId);
}

/**
 * Steps hidden from the inline TracePanel summary unless they failed/denied —
 * internal plumbing not useful to end users on the happy path.
 */
export const HIDDEN_UNLESS_DENIED = new Set<string>([
  "query.embed",
  "routing.select",
  "t2.vkg.context",
  "t2.vkg.validate",
  "t3.catalog_context",
  "t1.authorize",
  "t1.firewall",
  "t2.sql.authorize",
  "t2.sql.firewall",
  "t2.vkg.authorize",
  "t2.vkg.firewall",
  "t2.strategy_selected",
  "t3.guardrail",
]);

/** Authorization (Cedar) step IDs across all tiers — for boundary controls. */
export const CEDAR_STEP_IDS = new Set<string>([
  "t1.authorize",
  "t2.sql.authorize",
  "t2.vkg.authorize",
]);

/** SQL Firewall step IDs across all tiers — for boundary controls. */
export const FIREWALL_STEP_IDS = new Set<string>([
  "t1.firewall",
  "t2.sql.firewall",
  "t2.vkg.firewall",
]);

/** Bedrock Guardrail step IDs — for boundary controls. Only Tier-3 today. */
export const GUARDRAIL_STEP_IDS = new Set<string>(["t3.guardrail"]);

/** Parallel retrieval group ID — children have parallelGroup set to this value. */
export const PARALLEL_RETRIEVE_GROUP = "t3.retrieve";

/** Step IDs rendered as indented children under the t3.retrieve group. */
export const RETRIEVE_CHILD_STEPS = new Set<string>([
  "t3.vector_search",
  "t3.graph_traverse",
]);

/** Title-case a raw step ID when no explicit label exists. */
export function humanizeStepId(stepId: string): string {
  return stepId
    .replace(/^t[123]\./i, "")
    .replace(/\./g, " ")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Title for a Tier 2 result, reflecting which strategy/strategies ran.
 * Both strategies are structured-query strategies; name them in parallel:
 * NL→SQL (generates SQL) and VKG (Ontop) (generates SPARQL → SQL).
 * Shared by the Rationale panel and the Trace detail page so both name the
 * strategy the same way.
 */
export function tier2Title(
  t: AppTranslate,
  trace: readonly { step: string }[],
): string {
  const hasSql = trace.some((s) => s.step.startsWith("t2.sql."));
  const hasVkg = trace.some((s) => s.step.startsWith("t2.vkg."));
  const strategies: string[] = [];
  if (hasSql) strategies.push("NL→SQL");
  if (hasVkg) strategies.push("VKG (Ontop)");
  return strategies.length
    ? t("Tier 2 — Structured Query ({strategies})", {
        strategies: strategies.join(" + "),
      })
    : t("Tier 2 — Structured Query");
}

/**
 * Visual indicator type for a trace step's status. Single source of truth for
 * every trace surface (LiveSteps, TracePanel, RationalePanel) so a given status
 * always renders the same color. The return type matches both Cloudscape's
 * `StatusIndicator` `type` and `StepsProps.Status` (same union).
 *
 * - success: completed OK / authorized / allowed
 * - error:   hard failure / denied
 * - warning: degraded-but-continued (retry, timeout, partial)
 * - stopped: routine "not applicable, moved on" (miss / skipped / empty)
 * - info:    anything else
 */
export type TraceStatusType =
  | "success"
  | "error"
  | "warning"
  | "stopped"
  | "info";

export function mapTraceStatus(status: string): TraceStatusType {
  switch (status) {
    // "succeeded" is what the agentic controller/retriever record on the two paths
    // that report a tool actually working; without it every successful agentic tool
    // call fell through to `default` and rendered as a neutral info dot.
    case "success":
    case "succeeded":
    case "completed":
    case "ok":
    case "allow":
      return "success";
    case "error":
    case "denied":
    case "failed":
      return "error";
    // unknown_tool / invalid_input are agentic per-step degradations: the planner
    // named an unregistered tool, or its inputs failed the tool's declared
    // contract. Neither is fatal (the loop continues) but both should read as
    // degradations rather than neutral info.
    case "retry":
    case "retrying":
    case "timeout":
    case "degraded":
    case "unknown_tool":
    case "invalid_input":
      return "warning";
    // Both bypasses are Tier 1 declining a question it cannot fully answer — more
    // than one metric matched, or the question carried a qualifier the matched
    // metric's fixed SQL cannot express — so Tier 2 answered instead. Routine
    // re-routing, not a degradation.
    case "skipped":
    case "miss":
    case "empty":
    case "multi_metric_bypass":
    case "residual_qualifier_bypass":
      return "stopped";
    default:
      return "info";
  }
}
