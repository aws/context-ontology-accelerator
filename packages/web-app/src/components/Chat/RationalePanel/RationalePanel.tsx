// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { useNavigate } from "react-router-dom";
import {
  Badge,
  Box,
  ExpandableSection,
  KeyValuePairs,
  Link,
  SpaceBetween,
  StatusIndicator,
  Steps,
} from "@cloudscape-design/components";
import type { StepsProps } from "@cloudscape-design/components/steps";
import type { TraceStep, ChatMessage } from "@app-types";
import { formatStepDetail } from "./format-step-detail";
import {
  STEP_DISPLAY_NAMES,
  CEDAR_STEP_IDS,
  FIREWALL_STEP_IDS,
  GUARDRAIL_STEP_IDS,
  humanizeStepId,
  mapTraceStatus,
} from "@components/Chat/step-labels";

export interface RationalePanelProps {
  message: ChatMessage | undefined;
  sessionId?: string | null;
  userQuery?: string;
}

const TIER_LABELS: Record<number, string> = {
  1: "Tier 1 — Metric Match",
  2: "Tier 2 — Structured Query",
  2.5: "Tier 2.5 — Structured Query",
  3: "Tier 3 — Knowledge Retrieval",
};

// Only surface a parallel-savings hint when it's material — for fast sources
// the delta is just the quicker source's time and adds noise, not insight.
const MIN_NOTABLE_SAVINGS_MS = 500;

/**
 * Resolution label shown in the metadata header. For Tier 2 it delegates to
 * tier2Title so the header and the execution-plan section title never disagree
 * about which strategy/strategies ran.
 */
function resolutionLabel(tier: number, trace: TraceStep[]): string {
  if (tier === 2) {
    return tier2Title(trace);
  }
  return TIER_LABELS[tier] ?? `Tier ${tier}`;
}

function formatStepName(name: string): string {
  return STEP_DISPLAY_NAMES[name] ?? humanizeStepId(name);
}

function formatDuration(ms: number): string {
  if (ms === 0) return "0ms";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function confidenceBadgeColor(
  score: number,
): "green" | "severity-medium" | "red" {
  if (score >= 0.8) return "green";
  if (score >= 0.5) return "severity-medium";
  return "red";
}

const STATUS_LABELS: Record<string, string> = {
  success: "Resolved",
  ok: "Resolved",
  allow: "Resolved",
  completed: "Resolved",
  skipped: "Skipped",
  miss: "Skipped",
  empty: "Empty",
  error: "Error",
  denied: "Denied",
  failed: "Failed",
  retry: "Retried",
  retrying: "Retrying",
  timeout: "Timed out",
  degraded: "Degraded",
  multi_metric_bypass: "Skipped",
  residual_qualifier_bypass: "Skipped",
};

function getStepDetail(step: TraceStep): React.ReactNode {
  const detail = formatStepDetail(step.step, step.detail);
  const statusLabel = STATUS_LABELS[step.status] ?? step.status;
  // Parallel-group wrappers (e.g. t3.retrieve) carry their elapsed time in
  // wallMs, not durationMs — prefer it and label it as wall-clock time.
  const timeLabel =
    step.wallMs != null
      ? `${formatDuration(step.wallMs)} wall`
      : formatDuration(step.durationMs);

  return (
    <Box variant="small" color="text-body-secondary">
      {detail && <>{detail}</>}
      <Box
        variant="small"
        display="inline"
        padding={{ left: "s" }}
        color="text-status-inactive"
      >
        {statusLabel} · {timeLabel}
      </Box>
    </Box>
  );
}

export const RationalePanel: React.FC<RationalePanelProps> = ({
  message,
  sessionId,
  userQuery,
}) => {
  const navigate = useNavigate();
  if (!message || !message.response) {
    return (
      <Box padding="l" textAlign="center" color="text-body-secondary">
        Select a response to view its rationale.
      </Box>
    );
  }

  const { result, requestId } = message.response;
  const trace = result.trace ?? message.liveTraceSteps ?? [];
  const metadata = result.metadata ?? {};
  const { namespace, modelId, principal } = metadata;
  const latencyMs =
    typeof metadata.totalMs === "number"
      ? metadata.totalMs
      : trace.reduce((sum, t) => sum + t.durationMs, 0);

  return (
    <SpaceBetween size="l">
      {/* Metadata key-value pairs */}
      <KeyValuePairs
        columns={2}
        items={[
          {
            label: "Resolution",
            value: resolutionLabel(result.tier, trace),
          },
          { label: "Latency", value: `${latencyMs}ms` },
          {
            label: "Model",
            value: modelId ?? "—",
          },
          {
            label: "Principal",
            value: principal ?? "—",
          },
          {
            label: "Namespace",
            value: namespace ?? "—",
          },
          {
            label: "Request ID",
            value: requestId ?? "—",
          },
        ]}
      />

      {/* Confidence */}
      <SpaceBetween size="xxs">
        <Box variant="h5">Confidence</Box>
        <SpaceBetween size="xs" direction="horizontal" alignItems="center">
          <Badge color={confidenceBadgeColor(result.confidence.score)}>
            {Math.round(result.confidence.score * 100)}%
          </Badge>
          <Box variant="span" color="text-body-secondary" fontSize="body-s">
            {result.confidence.rationale}
          </Box>
        </SpaceBetween>
      </SpaceBetween>

      {/* Boundary controls */}
      <SpaceBetween size="xxs">
        <Box variant="h5">Boundary controls</Box>
        <SpaceBetween size="xs" direction="horizontal">
          <BoundaryBadge label="Cedar" status={getCedarStatus(trace)} />
          <BoundaryBadge
            label="SQL Firewall"
            status={getFirewallStatus(trace)}
          />
          <BoundaryBadge
            label="Guardrails"
            status={getGuardrailStatus(trace, result.guardrailBlocked ?? false)}
          />
        </SpaceBetween>
      </SpaceBetween>

      {/* Data sources */}
      {result.dataSources && result.dataSources.length > 0 && (
        <SpaceBetween size="xxs">
          <Box variant="h5">Data sources</Box>
          <SpaceBetween size="xs" direction="horizontal">
            {result.dataSources.map((ds) => (
              <Badge key={ds} color="blue">
                {ds}
              </Badge>
            ))}
          </SpaceBetween>
        </SpaceBetween>
      )}

      {/* Supporting content */}
      {result.supportingContent && result.supportingContent.length > 0 && (
        <ExpandableSection
          headerText={`Supporting content (${result.supportingContent.length})`}
          variant="footer"
        >
          <SpaceBetween size="s">
            {result.supportingContent.map((item, idx) => {
              const sourceLabel =
                [
                  item.sourceDocumentName,
                  item.sourceDoc,
                  item.label,
                  item.sourceDocumentId,
                ].find((value) => typeof value === "string" && value.trim()) ??
                undefined;
              return (
                <Box
                  key={item.chunkId ?? `evidence-${idx}`}
                  padding={{ vertical: "xs" }}
                >
                  <SpaceBetween size="xxs">
                    {sourceLabel && (
                      <Box variant="span" fontWeight="bold" fontSize="body-s">
                        {sourceLabel}
                      </Box>
                    )}
                    {item.text && (
                      <Box
                        variant="span"
                        fontSize="body-s"
                        color="text-body-secondary"
                      >
                        {item.text.slice(0, 200)}
                        {item.text.length > 200 ? "…" : ""}
                      </Box>
                    )}
                    {item.relevanceScore != null && (
                      <Box
                        variant="span"
                        fontSize="body-s"
                        color="text-body-secondary"
                      >
                        Relevance: {Math.round(item.relevanceScore * 100)}%
                      </Box>
                    )}
                  </SpaceBetween>
                </Box>
              );
            })}
          </SpaceBetween>
        </ExpandableSection>
      )}

      {/* Execution plan — grouped into tier sections */}
      {trace.length > 0 && (
        <SpaceBetween size="s">
          <Box variant="h3">Execution plan</Box>
          {buildTierSections(trace).map((section) => (
            <SpaceBetween size="xxs" key={section.key}>
              <Box variant="h5" color="text-body-secondary">
                {section.title}
              </Box>
              {section.steps.length > 0 && (
                <Steps ariaLabel={section.title} steps={section.steps} />
              )}
              {section.subgroups?.map((sub) => (
                <SpaceBetween size="xxs" key={sub.label}>
                  <Box
                    variant="small"
                    fontWeight="bold"
                    color="text-body-secondary"
                    padding={{ left: "s" }}
                  >
                    {sub.label}
                  </Box>
                  <Box padding={{ left: "s" }}>
                    <Steps
                      ariaLabel={`${section.title} — ${sub.label}`}
                      steps={sub.steps}
                    />
                  </Box>
                </SpaceBetween>
              ))}
            </SpaceBetween>
          ))}
          <Link
            fontSize="body-s"
            onFollow={() => {
              const rid = message.response?.requestId ?? message.requestId;
              if (!sessionId || !rid) return;
              navigate(`/playground/trace/${sessionId}/${rid}`, {
                state: { message, query: userQuery },
              });
            }}
          >
            Open full trace
          </Link>
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
};

// ── Helpers ─────────────────────────────────────────────────────────

interface StrategySubgroup {
  label: string;
  steps: StepsProps.Step[];
}

interface TierSection {
  key: string;
  title: string;
  steps: StepsProps.Step[];
  // When set, the section's strategy-specific steps are split into labeled
  // subgroups (e.g. NL→SQL vs VKG) so each maps to a name in the title.
  subgroups?: StrategySubgroup[];
}

const SECTION_TITLES: Record<string, string> = {
  t1: "Tier 1 — Metric Match",
  t2: "Tier 2 — Structured Query",
  t3: "Tier 3 — Knowledge Retrieval",
  other: "Other",
};

// Stable display order for sections.
const SECTION_ORDER = ["t1", "t2", "t3", "other"];

// Setup steps (embedding/routing) run once Tier 1 misses, as a lead-in to the
// retrieval tiers — they are folded into the first retrieval section that ran.
const SETUP_STEP_IDS = new Set(["query.embed", "routing.select"]);

/**
 * Classify a step ID into a tier-section key. Setup steps are routed to the
 * first retrieval tier that ran (Tier 2, else Tier 3) so they appear as a
 * lead-in rather than a free-floating section.
 */
function sectionKeyFor(stepId: string, setupTarget: string): string {
  if (SETUP_STEP_IDS.has(stepId)) return setupTarget;
  if (stepId.startsWith("t1.")) return "t1";
  if (stepId.startsWith("t2.")) return "t2";
  if (stepId.startsWith("t3.")) return "t3";
  // The graphrag lexical retriever (TIER3_STRATEGY=lexical-baseline) emits
  // "lexical_baseline_retrieve" without a "t3." prefix; it IS a Tier-3 step, so
  // classify it there — otherwise it lands in a stray "other" section and the
  // Tier-3 execution plan looks incomplete.
  if (stepId === "lexical_baseline_retrieve") return "t3";
  return "other";
}

/**
 * Title for the Tier 2 section, reflecting which strategy/strategies ran.
 * Both strategies are structured-query strategies; name them in parallel:
 * NL→SQL (generates SQL) and VKG (Ontop) (generates SPARQL → SQL).
 */
function tier2Title(trace: TraceStep[]): string {
  const hasSql = trace.some((s) => s.step.startsWith("t2.sql."));
  const hasVkg = trace.some((s) => s.step.startsWith("t2.vkg."));
  const strategies: string[] = [];
  if (hasSql) strategies.push("NL→SQL");
  if (hasVkg) strategies.push("VKG (Ontop)");
  return strategies.length
    ? `Tier 2 — Structured Query (${strategies.join(" + ")})`
    : "Tier 2 — Structured Query";
}

/**
 * Group the trace into tier sections for display. Parallel children (those
 * tagged with a parallelGroup) are pulled out and rendered nested inside their
 * group wrapper's detail, so each section reflects the true execution shape.
 */
function buildTierSections(trace: TraceStep[]): TierSection[] {
  const childrenByGroup = new Map<string, TraceStep[]>();
  for (const step of trace) {
    if (step.parallelGroup) {
      const list = childrenByGroup.get(step.parallelGroup) ?? [];
      list.push(step);
      childrenByGroup.set(step.parallelGroup, list);
    }
  }

  // Fold setup steps into the first retrieval tier that actually ran.
  const setupTarget = trace.some((s) => s.step.startsWith("t2.")) ? "t2" : "t3";

  const toRow = (step: TraceStep): StepsProps.Step => {
    const children = childrenByGroup.get(step.step);
    return {
      status: mapTraceStatus(step.status),
      header: formatStepName(step.step),
      details: children ? getGroupDetail(step, children) : getStepDetail(step),
    };
  };

  const stepsByKey = new Map<string, StepsProps.Step[]>();
  for (const step of trace) {
    if (step.parallelGroup) continue; // rendered nested under its group wrapper
    const key = sectionKeyFor(step.step, setupTarget);
    const list = stepsByKey.get(key) ?? [];
    list.push(toRow(step));
    stepsByKey.set(key, list);
  }

  // When both Tier 2 strategies ran, split the t2 section's steps into labeled
  // subgroups so each maps to a name in the title. Setup steps folded into t2
  // (embed/routing) stay as the section's lead-in steps.
  const hasSql = trace.some((s) => s.step.startsWith("t2.sql."));
  const hasVkg = trace.some((s) => s.step.startsWith("t2.vkg."));
  const splitTier2 = hasSql && hasVkg;

  return SECTION_ORDER.filter((key) => stepsByKey.has(key)).map((key) => {
    if (key === "t2" && splitTier2) {
      const leadIn = trace.filter(
        (s) => !s.parallelGroup && SETUP_STEP_IDS.has(s.step),
      );
      const sqlSteps = trace.filter(
        (s) => !s.parallelGroup && s.step.startsWith("t2.sql."),
      );
      const vkgSteps = trace.filter(
        (s) => !s.parallelGroup && s.step.startsWith("t2.vkg."),
      );
      return {
        key,
        title: SECTION_TITLES.t2,
        steps: leadIn.map(toRow),
        subgroups: [
          { label: "NL→SQL", steps: sqlSteps.map(toRow) },
          { label: "VKG (Ontop)", steps: vkgSteps.map(toRow) },
        ],
      };
    }
    return {
      key,
      title: key === "t2" ? tier2Title(trace) : SECTION_TITLES[key],
      steps: stepsByKey.get(key) ?? [],
    };
  });
}

/** Detail renderer for a parallel-group wrapper: nested children + speedup hint. */
function getGroupDetail(
  group: TraceStep,
  children: TraceStep[],
): React.ReactNode {
  const wall = group.wallMs ?? group.durationMs;
  const sum = children.reduce((acc, c) => acc + c.durationMs, 0);
  const saved = sum - wall;
  const statusLabel = STATUS_LABELS[group.status] ?? group.status;

  return (
    <Box variant="small" color="text-body-secondary">
      {`${children.length} sources in parallel`}
      <Box
        variant="small"
        display="inline"
        padding={{ left: "s" }}
        color="text-status-inactive"
      >
        {statusLabel} · {formatDuration(wall)} wall
      </Box>
      <SpaceBetween size="xxs">
        {children.map((child, i) => {
          const childDetail = formatStepDetail(child.step, child.detail);
          return (
            <Box
              key={`${child.step}-${i}`}
              variant="small"
              padding={{ left: "l" }}
            >
              <StatusIndicator type={mapTraceStatus(child.status)}>
                {formatStepName(child.step)}
              </StatusIndicator>
              <Box
                variant="small"
                display="inline"
                padding={{ left: "xs" }}
                color="text-status-inactive"
              >
                {childDetail ? `${childDetail} · ` : ""}
                {formatDuration(child.durationMs)}
                {child.toolUsed ? ` · ${child.toolUsed}` : ""}
              </Box>
            </Box>
          );
        })}
      </SpaceBetween>
      {saved >= MIN_NOTABLE_SAVINGS_MS && (
        <Box variant="small" color="text-status-success">
          {`~${formatDuration(saved)} saved by running in parallel`}
        </Box>
      )}
    </Box>
  );
}

type BoundaryStatus = "allow" | "ok" | "passed" | "blocked" | "n/a";

function getCedarStatus(trace: TraceStep[]): BoundaryStatus {
  const step = trace.find((s) => CEDAR_STEP_IDS.has(s.step));
  if (!step) return "n/a";
  if (step.status === "allow" || step.status === "success") return "allow";
  if (step.status === "denied" || step.status === "error") return "blocked";
  return "n/a";
}

function getFirewallStatus(trace: TraceStep[]): BoundaryStatus {
  const step = trace.find((s) => FIREWALL_STEP_IDS.has(s.step));
  if (!step) return "n/a";
  if (step.status === "ok" || step.status === "success") return "ok";
  if (step.status === "denied" || step.status === "error") return "blocked";
  return "n/a";
}

// Guardrails render "passed" when a t3.guardrail step was recorded and
// succeeded, "blocked" when it was recorded and denied, and "n/a" when no
// guardrail ran. Fall back to the guardrailBlocked boolean for traces
// recorded before this step existed so the block path stays visible.
function getGuardrailStatus(
  trace: TraceStep[],
  guardrailBlocked: boolean,
): BoundaryStatus {
  const step = trace.find((s) => GUARDRAIL_STEP_IDS.has(s.step));
  if (step) {
    if (step.status === "success" || step.status === "ok") return "passed";
    if (step.status === "denied" || step.status === "error") return "blocked";
    return "n/a";
  }
  return guardrailBlocked ? "blocked" : "n/a";
}

const BOUNDARY_COLORS: Record<BoundaryStatus, "green" | "red" | "grey"> = {
  allow: "green",
  ok: "green",
  passed: "green",
  blocked: "red",
  "n/a": "grey",
};

function BoundaryBadge({
  label,
  status,
}: {
  label: string;
  status: BoundaryStatus;
}) {
  return (
    <Badge color={BOUNDARY_COLORS[status]}>
      {label}: {status}
    </Badge>
  );
}
