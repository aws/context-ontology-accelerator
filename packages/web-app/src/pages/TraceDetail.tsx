// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { useNavigate, useParams } from "react-router-dom";
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
import type { TraceStep } from "@app-types";
import {
  humanizeStepId,
  mapTraceStatus,
  tier2Title,
  CEDAR_STEP_IDS,
  FIREWALL_STEP_IDS,
  GUARDRAIL_STEP_IDS,
} from "@components/Chat/step-labels";
import { useTraceData } from "@api-hooks/use-trace-data";
import { useCurrentNamespace } from "@components/NamespaceProvider";

function tierBadgeColor(tier: number): "green" | "blue" | "grey" {
  if (tier === 1) return "green";
  if (tier === 2) return "blue";
  return "grey";
}

/**
 * Tier outcome badge text. Tier 2 names the strategy that ran (NL→SQL and/or
 * VKG (Ontop)) through the same tier2Title the Rationale panel uses, so the
 * two views of one answer agree.
 */
function tierOutcome(tier: number, trace: TraceStep[]): string {
  if (tier === 2) return tier2Title(trace);
  if (tier === 1) return "Tier 1 — metric";
  return `Tier ${tier} — knowledge`;
}

function formatDurationMs(ms: number): string {
  return `${ms} ms`;
}

export function TraceDetail() {
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

  if (!resolvedTrace) {
    return (
      <SpaceBetween size="l">
        <Header variant="h1">Trace detail</Header>
        <Container>
          <SpaceBetween size="m">
            {fetching ? (
              <StatusIndicator type="loading">
                Loading trace for request {requestId ?? "unknown"}...
              </StatusIndicator>
            ) : fetchError ? (
              <SpaceBetween size="s">
                <StatusIndicator type="error">{fetchError}</StatusIndicator>
                <SpaceBetween size="xs" direction="horizontal">
                  <Button onClick={fetchRetry} iconName="refresh">
                    Retry
                  </Button>
                  <Button
                    variant="link"
                    onClick={() => navigate("/playground")}
                  >
                    Back to Playground
                  </Button>
                </SpaceBetween>
              </SpaceBetween>
            ) : (
              <StatusIndicator type="loading">Loading trace...</StatusIndicator>
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
            Back to Playground
          </Button>
        }
      >
        Trace detail
      </Header>

      <Container header={<Header variant="h2">Resolution summary</Header>}>
        <SpaceBetween size="m">
          <ColumnLayout columns={3} variant="text-grid">
            <KeyValuePairs
              items={[
                { label: "Question", value: userQuery },
                {
                  label: "Answer",
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
                  label: "Total latency",
                  value: formatDurationMs(totalLatencyMs),
                },
                {
                  label: "Tier outcome",
                  value: (
                    <Badge color={tierBadgeColor(tier)}>
                      {tierOutcome(tier, trace)}
                    </Badge>
                  ),
                },
                {
                  label: "Request ID",
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
                  label: "Principal",
                  value: metadata.principal ? (
                    <>
                      <Badge color="green">human</Badge>{" "}
                      {String(metadata.principal)}
                    </>
                  ) : (
                    "—"
                  ),
                },
                {
                  label: "Namespace",
                  value: metadata.namespace ? String(metadata.namespace) : "—",
                },
                {
                  label: "Model",
                  value: metadata.modelId ? String(metadata.modelId) : "—",
                },
              ]}
            />
          </ColumnLayout>
          <SpaceBetween size="xs">
            <Box variant="small" fontWeight="bold">
              Boundary controls
            </Box>
            <SpaceBetween size="xxs" direction="horizontal">
              <Badge color={boundaryColor(cedarStatus)}>
                Cedar: {cedarStatus}
              </Badge>
              <Badge color={boundaryColor(firewallStatus)}>
                SQL Firewall: {firewallStatus}
              </Badge>
              <Badge color={boundaryColor(guardrailStatus)}>
                Guardrails: {guardrailStatus}
              </Badge>
            </SpaceBetween>
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h2">Compiled artifacts</Header>}>
        <ExpandableSection headerText="SPARQL & SQL" defaultExpanded={true}>
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
                No compiled artifacts for this request.
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
          description="Every tier the request passed through, with the MCP tool invoked and the per-step latency."
        >
          Tier trace
        </Header>
      }
      columnDefinitions={[
        {
          id: "step",
          header: "Step",
          cell: (item: IndexedTraceStep) => item._index + 1,
          width: 60,
        },
        {
          id: "tier",
          header: "Tier",
          cell: (item: IndexedTraceStep) => {
            const tierNum = extractTier(item.step);
            return tierNum ? (
              <Badge color={tierBadgeColor(tierNum)}>Tier {tierNum}</Badge>
            ) : (
              "—"
            );
          },
        },
        {
          id: "stage",
          header: "Stage",
          cell: (item: IndexedTraceStep) => humanizeStepId(item.step),
        },
        {
          id: "status",
          header: "Status",
          cell: (item: IndexedTraceStep) => (
            <StatusIndicator type={mapTraceStatus(item.status)}>
              {statusLabel(item.status)}
            </StatusIndicator>
          ),
        },
        {
          id: "duration",
          header: "Duration",
          cell: (item: IndexedTraceStep) => formatDurationMs(item.durationMs),
          width: 100,
        },
        {
          id: "detail",
          header: "Detail",
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

function extractTier(stepId: string): number | null {
  if (stepId.startsWith("t1.") || stepId.startsWith("tier1")) return 1;
  if (stepId.startsWith("t2.") || stepId.startsWith("tier2")) return 2;
  if (stepId.startsWith("t3.") || stepId.startsWith("tier3")) return 3;
  return null;
}

function statusLabel(status: string): string {
  switch (status) {
    case "success":
      return "Resolved";
    case "miss":
    case "skipped":
      return "Skipped";
    case "error":
      return "Failed";
    case "denied":
      return "Denied";
    default:
      return status;
  }
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
