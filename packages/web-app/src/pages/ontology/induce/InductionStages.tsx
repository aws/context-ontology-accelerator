// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Box from "@cloudscape-design/components/box";
import Popover from "@cloudscape-design/components/popover";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { useAppI18n } from "../../../i18n/LocaleProvider";
import type { AppMessage } from "../../../i18n/appMessages";

/**
 * Induction job stages — mirrors the Smithy ``InductionJobStatus`` enum
 * order:
 *   PENDING → FETCHING_METADATA → GENERATING_EMBEDDINGS → MATCHING →
 *   BUILDING_ONTOLOGY → STORING → COMPLETED
 *
 * Renders a horizontal step strip with the current stage highlighted.
 * On ``failed`` status the stage at which we stopped is rendered with
 * an error icon. Uses Cloudscape's ``StatusIndicator`` for the per-step
 * icon to match the rest of the app's visual language.
 */

interface Stage {
  id: string;
  label: AppMessage;
  /** Plain-language explanation of what this stage does, shown on hover/click. */
  info: AppMessage;
}

const STAGES: Stage[] = [
  {
    id: "fetching_metadata",
    label: "Fetch metadata",
    info: "Reads the source schema (tables, columns, types, keys). No row data.",
  },
  // Conditional stage — only fires when the user picked foundational
  // ontologies in the modal. Skipped otherwise; the strip skips ahead
  // because the backend transitions directly from FETCHING_METADATA to
  // GENERATING_EMBEDDINGS when no grounding is requested.
  {
    id: "loading_grounding_ontologies",
    label: "Load grounding",
    info: "Loads your chosen grounding ontologies for comparison. Skipped when no grounding is selected.",
  },
  {
    id: "generating_embeddings",
    label: "Embeddings",
    info: "Embeds each table and column to drive the similarity search in the next step.",
  },
  {
    id: "matching",
    label: "Match concepts",
    info: "Matches concepts to the grounding ontologies (Enhanced mode uses an LLM; Standard uses similarity). Unmatched concepts become novel classes.",
  },
  {
    id: "building_ontology",
    label: "Build OWL",
    info: "Builds the OWL ontology (a class per table, a property per column) plus R2RML mappings back to SQL.",
  },
  {
    id: "storing",
    label: "Persist",
    info: "Saves the ontology as a proposal. Nothing goes live until you accept it.",
  },
];

interface Props {
  status: string;
  error?: string;
}

export function InductionStages({ status, error }: Props) {
  const { t } = useAppI18n();
  // Map the status string to its index in STAGES. ``pending`` comes
  // before any stage runs (index -1). ``completed`` is after the last
  // stage (length).
  const lower = status.toLowerCase();
  let activeIdx = STAGES.findIndex((s) => s.id === lower);
  if (lower === "pending") activeIdx = -1;
  if (lower === "completed") activeIdx = STAGES.length;
  const isFailed = lower === "failed";

  return (
    <Box>
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        {STAGES.map((stage, i) => {
          const state =
            isFailed && i === Math.max(activeIdx, 0)
              ? "error"
              : i < activeIdx || lower === "completed"
                ? "done"
                : i === activeIdx
                  ? "running"
                  : "upcoming";
          return (
            <SpaceBetween
              key={stage.id}
              direction="horizontal"
              size="xs"
              alignItems="center"
            >
              <StageDot
                label={t(stage.label)}
                info={t(stage.info)}
                state={state}
              />
              {i < STAGES.length - 1 && (
                <span
                  style={{
                    width: 24,
                    height: 1,
                    background:
                      state === "done"
                        ? "#16a34a"
                        : state === "running"
                          ? "#3b82f6"
                          : "#cbd5e1",
                  }}
                />
              )}
            </SpaceBetween>
          );
        })}
      </SpaceBetween>
      {isFailed && error && (
        <Box variant="small" color="text-status-error" padding={{ top: "xs" }}>
          {error}
        </Box>
      )}
    </Box>
  );
}

function StageDot({
  label,
  info,
  state,
}: {
  label: string;
  info: string;
  state: "done" | "running" | "upcoming" | "error";
}) {
  return (
    <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
      <StatusIndicator
        type={
          state === "done"
            ? "success"
            : state === "running"
              ? "in-progress"
              : state === "error"
                ? "error"
                : "pending"
        }
      >
        {/* Label doubles as the info trigger to keep the strip compact; a
            separate (i) icon per step would crowd the horizontal strip. */}
        <Popover
          triggerType="custom"
          size="large"
          position="top"
          dismissButton={false}
          header={label}
          content={<Box variant="p">{info}</Box>}
        >
          <span style={{ cursor: "help" }}>{label}</span>
        </Popover>
      </StatusIndicator>
    </SpaceBetween>
  );
}
