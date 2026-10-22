// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Processing trace panel — shows step-by-step pipeline trace with timing.
 */
import React, { useState } from "react";
import {
  Badge,
  Box,
  ExpandableSection,
  SpaceBetween,
  Spinner,
  StatusIndicator,
} from "@cloudscape-design/components";
import type { TraceStep } from "@app-types";
import { formatStepDetail } from "@components/Chat/RationalePanel/format-step-detail";
import {
  HIDDEN_UNLESS_DENIED,
  PARALLEL_RETRIEVE_GROUP,
  RETRIEVE_CHILD_STEPS,
  localizeTraceStep,
  mapTraceStatus,
} from "@components/Chat/step-labels";
import { useAppI18n } from "../../../i18n/LocaleProvider";
import "../chat.css";

export interface TracePanelProps {
  /** Trace steps to render. */
  steps: TraceStep[];
  /** Whether more steps may still arrive (live streaming mode). */
  isLive: boolean;
}

function shouldShowStep(step: TraceStep): boolean {
  if (RETRIEVE_CHILD_STEPS.has(step.step)) {
    return false;
  }
  if (HIDDEN_UNLESS_DENIED.has(step.step)) {
    return step.status === "denied" || step.status === "error";
  }
  return true;
}

/**
 * Expandable trace panel showing each pipeline step with status + timing.
 */
export const TracePanel: React.FC<TracePanelProps> = ({ steps, isLive }) => {
  const { t } = useAppI18n();
  if (steps.length === 0 && !isLive) return null;

  const visibleSteps = steps.filter(shouldShowStep);

  return (
    <ExpandableSection
      headerText={t(
        visibleSteps.length === 1
          ? "Processing trace ({count} step)"
          : "Processing trace ({count} steps)",
        { count: visibleSteps.length },
      )}
      variant="footer"
      defaultExpanded={false}
    >
      <SpaceBetween size="xs">
        {visibleSteps.map((step, index) => {
          const children =
            step.step === PARALLEL_RETRIEVE_GROUP
              ? steps.filter((s) => s.parallelGroup === PARALLEL_RETRIEVE_GROUP)
              : [];
          return (
            <TraceStepRow
              key={`${step.step}-${index}`}
              step={step}
              childSteps={children}
            />
          );
        })}
        {isLive && (
          <Box padding={{ top: "xs" }}>
            <Spinner size="normal" />
          </Box>
        )}
      </SpaceBetween>
    </ExpandableSection>
  );
};

function TraceStepRow({
  step,
  childSteps = [],
}: {
  step: TraceStep;
  childSteps?: TraceStep[];
}) {
  const { t } = useAppI18n();
  const [expanded, setExpanded] = useState(false);
  const hasDetail = Boolean(step.detail || step.toolUsed);
  const durationLabel =
    step.wallMs != null
      ? t("{duration}ms wall", { duration: step.wallMs })
      : t("{duration}ms", { duration: step.durationMs });
  const stepLabel = localizeTraceStep(t, step.step);

  return (
    <div>
      <div
        className="trace-step-row"
        style={{ cursor: hasDetail ? "pointer" : "default" }}
        onClick={() => hasDetail && setExpanded(!expanded)}
        onKeyDown={(e) => {
          if (hasDetail && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            setExpanded(!expanded);
          }
        }}
        role={hasDetail ? "button" : undefined}
        tabIndex={hasDetail ? 0 : undefined}
        aria-expanded={hasDetail ? expanded : undefined}
        aria-label={t("Trace step: {step}", { step: stepLabel })}
      >
        <StatusIndicator type={mapTraceStatus(step.status)}>
          {stepLabel}
        </StatusIndicator>
        <Badge color="blue">{durationLabel}</Badge>
        {hasDetail && (
          <Box variant="span" fontSize="body-s" color="text-body-secondary">
            {expanded ? "▾" : "▸"}
          </Box>
        )}
      </div>
      {expanded && hasDetail && (
        <div className="trace-step-detail">
          {step.toolUsed && (
            <Box variant="span" fontSize="body-s" color="text-body-secondary">
              {t("Tool: {tool}", { tool: step.toolUsed })}
            </Box>
          )}
          {step.detail && (
            <pre className="trace-step-code">
              {formatStepDetail(step.step, step.detail)}
            </pre>
          )}
        </div>
      )}
      {/* Render parallel children indented */}
      {childSteps.length > 0 && (
        <div className="trace-step-children">
          <SpaceBetween size="xxs">
            {childSteps.map((child, i) => (
              <div key={`${child.step}-${i}`} className="trace-step-row">
                <Box
                  variant="span"
                  fontSize="body-s"
                  color="text-body-secondary"
                >
                  ├
                </Box>
                <StatusIndicator type={mapTraceStatus(child.status)}>
                  {localizeTraceStep(t, child.step)}
                </StatusIndicator>
                <Badge color="grey">
                  {t("{duration}ms", { duration: child.durationMs })}
                </Badge>
                {child.toolUsed && (
                  <Box
                    variant="span"
                    fontSize="body-s"
                    color="text-body-secondary"
                  >
                    {child.toolUsed}
                  </Box>
                )}
              </div>
            ))}
          </SpaceBetween>
        </div>
      )}
    </div>
  );
}
