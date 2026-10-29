// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import {
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  ExpandableSection,
  Header,
  KeyValuePairs,
  SpaceBetween,
  StatusIndicator,
  Table,
} from "@cloudscape-design/components";
import type { ExecutionMode, TraceStep } from "@app-types";
import {
  localizeTraceStatus,
  localizeTraceStep,
  mapTraceStatus,
  tier2Title,
  CEDAR_STEP_IDS,
  FIREWALL_STEP_IDS,
  GUARDRAIL_STEP_IDS,
} from "@components/Chat/step-labels";
import { useTraceData } from "@api-hooks/use-trace-data";
import { useCurrentNamespace } from "@components/NamespaceProvider";
import { useAppI18n, type AppTranslate } from "../i18n/LocaleProvider";

function tierBadgeColor(
  tier: number,
  executionMode?: ExecutionMode,
): "green" | "blue" | "grey" | "severity-low" {
  // Mode wins over tier (same rule as the Rationale panel): a Deep Reasoning
  // answer resolves at tier 3 but is labelled by its mode, so give it its own
  // badge colour rather than the tier-3 grey.
  if (executionMode === "deep-reasoning") return "severity-low";
  if (tier === 1) return "green";
  if (tier === 2) return "blue";
  return "grey";
}

/**
 * Tier outcome badge text. Mode wins over tier: a Deep Reasoning answer
 * resolves at tier 3 but must read "Deep Reasoning", not "Tier 3 — knowledge",
 * so the full-trace page agrees with the Rationale panel (#1166). Tier 2 names
 * the strategy that ran (NL→SQL and/or VKG (Ontop)) through the same tier2Title
 * the Rationale panel uses, so the two views of one answer agree.
 */
function tierOutcome(
  tier: number,
  trace: TraceStep[],
  t: AppTranslate,
  executionMode?: ExecutionMode,
): string {
  if (executionMode === "deep-reasoning") return t("Deep Reasoning");
  if (tier === 2) return tier2Title(t, trace);
  return t("Tier {tier} — {label}", {
    tier,
    label: tier === 1 ? t("metric") : t("knowledge"),
  });
}

function formatDurationMs(ms: number): string {
  return `${ms} ms`;
}

export function TraceDetail() {
  const { t } = useAppI18n();
  const { requestId } = useParams<{
    sessionId: string;
    requestId: string;
  }>();
  const navigate = useNavigate();
  const { setNamespace } = useCurrentNamespace();
  const {
    data: resolvedTrace,
    loading: fetching,
    error: fetchError,
    retry: fetchRetry,
  } = useTraceData();

  useBreadcrumbTrail([
    { text: t("Serve") },
    { text: t("Playground"), href: "/playground" },
    {
      text: requestId
        ? t("Trace {requestId}", { requestId })
        : t("Trace detail"),
    },
  ]);

  if (!resolvedTrace) {
    return (
      <SpaceBetween size="l">
        <Header variant="h1">{t("Trace detail")}</Header>
        <Container>
          <SpaceBetween size="m">
            {fetching ? (
              <StatusIndicator type="loading">
                {t("Loading trace for request {id}...", {
                  id: requestId ?? t("unknown"),
                })}
              </StatusIndicator>
            ) : fetchError ? (
              <SpaceBetween size="s">
                <StatusIndicator type="error">{fetchError}</StatusIndicator>
                <SpaceBetween size="xs" direction="horizontal">
                  <Button onClick={fetchRetry} iconName="refresh">
                    {t("Retry")}
                  </Button>
                  <Button
                    variant="link"
                    onClick={() => navigate("/playground")}
                  >
                    {t("Back to Playground")}
                  </Button>
                </SpaceBetween>
              </SpaceBetween>
            ) : (
              <StatusIndicator type="loading">
                {t("Loading trace...")}
              </StatusIndicator>
            )}
          </SpaceBetween>
        </Container>
      </SpaceBetween>
    );
  }

  const {
    trace,
    tier,
    content,
    queryUsed,
    sparqlGenerated,
    guardrailBlocked,
    metadata,
    userQuery,
    executionMode,
  } = resolvedTrace;

  const totalLatencyMs =
    typeof metadata.totalMs === "number"
      ? metadata.totalMs
      : trace.reduce((sum, t) => sum + t.durationMs, 0);

  const cedarStatus = getCedarStatus(trace);
  const firewallStatus = getFirewallStatus(trace);
  const guardrailStatus = getGuardrailStatus(trace, guardrailBlocked);

  const displaySparql = sparqlGenerated;
  const displaySql = queryUsed;

  // Preserve the namespace across the back-nav. NamespaceProvider only reads
  // namespace from /namespaces/:id/... paths, not from ?namespaceId query
  // strings, so on reload+return the provider has no in-memory selectedId
  // and would fall back to the first namespace. Restore it via setNamespace
  // (a no-op if the user never left the same-session in-memory state).
  const backNamespace =
    (typeof metadata.namespace === "string" && metadata.namespace) || undefined;
  const onBack = () => {
    if (backNamespace) setNamespace(backNamespace);
    navigate("/playground");
  };

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        actions={
          <Button
            iconName="arrow-left"
            onClick={onBack}
            data-testid="trace-detail-back"
          >
            {t("Back to Playground")}
          </Button>
        }
      >
        {t("Trace detail")}
      </Header>

      <Container
        header={<Header variant="h2">{t("Resolution summary")}</Header>}
      >
        <SpaceBetween size="m">
          <ColumnLayout columns={3} variant="text-grid">
            <KeyValuePairs
              items={[
                { label: t("Question"), value: userQuery },
                {
                  label: t("Answer"),
                  value: content
                    ? content.length > 120
                      ? `${content.slice(0, 120)}...`
                      : content
                    : "—",
                },
              ]}
            />
            <KeyValuePairs
              items={[
                {
                  label: t("Total latency"),
                  value: formatDurationMs(totalLatencyMs),
                },
                {
                  label: t("Tier outcome"),
                  value: (
                    <Badge color={tierBadgeColor(tier, executionMode)}>
                      {tierOutcome(tier, trace, t, executionMode)}
                    </Badge>
                  ),
                },
                {
                  label: t("Request ID"),
                  value: (
                    <button
                      type="button"
                      onClick={() =>
                        navigator.clipboard.writeText(requestId ?? "")
                      }
                      style={{
                        fontFamily:
                          "ui-monospace, SFMono-Regular, Menlo, monospace",
                        fontSize: 12,
                        padding: "2px 6px",
                        background: "transparent",
                        border:
                          "1px solid var(--color-border-input-default, #ccc)",
                        borderRadius: 3,
                        cursor: "pointer",
                        color: "inherit",
                      }}
                    >
                      {requestId ?? "—"}
                    </button>
                  ),
                },
              ]}
            />
            <KeyValuePairs
              items={[
                {
                  label: t("Principal"),
                  value: metadata.principal ? (
                    <>
                      <Badge color="green">{t("human")}</Badge>{" "}
                      {String(metadata.principal)}
                    </>
                  ) : (
                    "—"
                  ),
                },
                {
                  label: t("Namespace"),
                  value: metadata.namespace ? String(metadata.namespace) : "—",
                },
                {
                  label: t("Model"),
                  value: metadata.modelId ? String(metadata.modelId) : "—",
                },
              ]}
            />
          </ColumnLayout>
          <SpaceBetween size="xs">
            <Box variant="small" fontWeight="bold">
              {t("Boundary controls")}
            </Box>
            <SpaceBetween size="xxs" direction="horizontal">
              <Badge color={boundaryColor(cedarStatus)}>
                {t("Cedar: {status}", {
                  status: boundaryStatusLabel(cedarStatus, t),
                })}
              </Badge>
              <Badge color={boundaryColor(firewallStatus)}>
                {t("SQL Firewall: {status}", {
                  status: boundaryStatusLabel(firewallStatus, t),
                })}
              </Badge>
              <Badge color={boundaryColor(guardrailStatus)}>
                {t("Guardrails: {status}", {
                  status: boundaryStatusLabel(guardrailStatus, t),
                })}
              </Badge>
            </SpaceBetween>
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      <Container
        header={<Header variant="h2">{t("Compiled artifacts")}</Header>}
      >
        <ExpandableSection
          headerText={t("SPARQL & SQL")}
          defaultExpanded={true}
        >
          <SpaceBetween size="s">
            {displaySparql && (
              <Box>
                <Box
                  variant="small"
                  fontWeight="bold"
                  padding={{ bottom: "xxs" }}
                >
                  SPARQL
                </Box>
                <Box variant="code">
                  <pre className="coa-code-block">{displaySparql}</pre>
                </Box>
              </Box>
            )}
            {displaySql && (
              <Box>
                <Box
                  variant="small"
                  fontWeight="bold"
                  padding={{ bottom: "xxs" }}
                >
                  SQL
                </Box>
                <Box variant="code">
                  <pre className="coa-code-block">{displaySql}</pre>
                </Box>
              </Box>
            )}
            {!displaySparql && !displaySql && (
              <Box color="text-body-secondary">
                {t("No compiled artifacts for this request.")}
              </Box>
            )}
          </SpaceBetween>
        </ExpandableSection>
      </Container>

      <TraceTable trace={trace} />
    </SpaceBetween>
  );
}

// ── Trace Table ─────────────────────────────────────────────────────

interface IndexedTraceStep extends TraceStep {
  _index: number;
}

function TraceTable({ trace }: { trace: TraceStep[] }) {
  const { t } = useAppI18n();
  const items: IndexedTraceStep[] = trace.map((step, i) => ({
    ...step,
    _index: i,
  }));

  return (
    <Table
      variant="borderless"
      header={
        <Header
          variant="h2"
          description={t(
            "Every tier the request passed through, with the MCP tool invoked and the per-step latency.",
          )}
        >
          {t("Tier trace")}
        </Header>
      }
      columnDefinitions={[
        {
          id: "step",
          header: t("Step"),
          cell: (item: IndexedTraceStep) => item._index + 1,
          width: 60,
        },
        {
          id: "tier",
          header: t("Tier"),
          cell: (item: IndexedTraceStep) => {
            const tierNum = extractTier(item.step);
            return tierNum ? (
              <Badge color={tierBadgeColor(tierNum)}>
                {t("Tier {tier}", { tier: tierNum })}
              </Badge>
            ) : (
              "—"
            );
          },
        },
        {
          id: "stage",
          header: t("Stage"),
          cell: (item: IndexedTraceStep) => localizeTraceStep(t, item.step),
        },
        {
          id: "status",
          header: t("Status"),
          cell: (item: IndexedTraceStep) => (
            <StatusIndicator type={mapTraceStatus(item.status)}>
              {localizeTraceStatus(t, item.status)}
            </StatusIndicator>
          ),
        },
        {
          id: "duration",
          header: t("Duration"),
          cell: (item: IndexedTraceStep) => formatDurationMs(item.durationMs),
          width: 100,
        },
        {
          id: "detail",
          header: t("Detail"),
          cell: (item: IndexedTraceStep) => formatDetail(item.detail),
        },
      ]}
      items={items}
      trackBy={(item) => `${item.step}-${item._index}`}
    />
  );
}

// ── Helpers ─────────────────────────────────────────────────────────

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

// Guardrails read the same way as Cedar/Firewall — from a dedicated trace
// step — instead of from the guardrailBlocked boolean alone. The boolean is
// still consulted as a fallback so legacy traces (recorded before the
// t3.guardrail step existed) render "blocked" correctly on the block path.
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

function boundaryColor(
  status: BoundaryStatus | string,
): "green" | "red" | "grey" {
  if (status === "allow" || status === "ok" || status === "passed")
    return "green";
  if (status === "blocked") return "red";
  return "grey";
}

function boundaryStatusLabel(status: BoundaryStatus, t: AppTranslate): string {
  switch (status) {
    case "allow":
      return t("allow");
    case "ok":
      return t("ok");
    case "passed":
      return t("passed");
    case "blocked":
      return t("blocked");
    default:
      return t("n/a");
  }
}

function extractTier(stepId: string): number | null {
  if (stepId.startsWith("t1.") || stepId.startsWith("tier1")) return 1;
  if (stepId.startsWith("t2.") || stepId.startsWith("tier2")) return 2;
  if (stepId.startsWith("t3.") || stepId.startsWith("tier3")) return 3;
  return null;
}

function formatDetail(
  detail: string | Record<string, unknown> | undefined,
): string {
  if (!detail) return "—";
  if (typeof detail === "string") return detail;
  const entries = Object.entries(detail);
  if (entries.length === 0) return "—";
  return entries
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
}
