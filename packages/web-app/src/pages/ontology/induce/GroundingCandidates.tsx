// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * GroundingCandidates — the grounding review/override control for a single
 * table match, rendered inside the class detail split panel's Grounding tab.
 *
 * Renders the foundational-grounding candidates as a Cloudscape embedded
 * ``<Table>`` — consistent with the Relationships / Attributes tabs — with a
 * single-select column. The selected row is the currently-chosen grounding
 * (the staged override if any, else the persisted match). Selecting a row
 * stages that candidate; the "Mark novel" header action stages "keep novel".
 * Selection is staged upward (``onSelectCandidate`` / ``onMarkNovel``) — this
 * component never persists; the ProposalDetail Save-changes flow does.
 *
 * !418 funnel invariant: render ALL persisted candidates (no ``slice``).
 */
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { ButtonWithHint } from "@components/ButtonWithHint";
import { InfoPopover } from "@components/InfoPopover";
import { SortableTable } from "@components/SortableTable";
import {
  conceptMatchKey,
  type ConceptMatch,
  type MatchCandidate,
  localName,
  ontologyLabel,
} from "./grounding";
import { useAppI18n } from "../../../i18n/LocaleProvider";

/** Numeric score for a candidate: fused score when present, else lexical. */
function candidateScore(candidate: MatchCandidate): number {
  return Number(candidate.fused_score ?? candidate.lexical_sim) || 0;
}

/** Horizontal score bar — shows absolute similarity as a filled bar (0–1). */
function ScoreBar({ score, chosen }: { score: number; chosen: boolean }) {
  const pct = Math.min(Math.max(score * 100, 0), 100);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <div
        style={{
          height: 6,
          borderRadius: 3,
          background: "#e2e8f0",
          width: 96,
          flexShrink: 0,
        }}
      >
        <div
          style={{
            height: 6,
            borderRadius: 3,
            background: chosen ? "#037f0c" : "#5f6b7a",
            width: `${pct}%`,
            transition: "width 0.2s",
          }}
        />
      </div>
      <Box variant="small" color="text-status-inactive">
        {score.toFixed(3)}
      </Box>
    </div>
  );
}

interface Props {
  match: ConceptMatch;
  /**
   * Staged-but-unsaved override for this table (optimistic). A string is a
   * chosen candidate URI; ``null`` means "keep novel" (mirrors the backend
   * ``grounding_overrides`` contract where ``null`` clears the grounding).
   * ``undefined`` means nothing is staged.
   */
  pendingOverride?: string | null;
  /** Stage a chosen candidate URI for this stable grounding match key. */
  onSelectCandidate?: (matchKey: string, uri: string) => void;
  /** Stage "keep novel" for this stable grounding match key. */
  onMarkNovel?: (matchKey: string) => void;
}

export function GroundingCandidates({
  match,
  pendingOverride,
  onSelectCandidate,
  onMarkNovel,
}: Props) {
  const { t } = useAppI18n();
  const candidates = match.candidates ?? [];
  const matchKey = conceptMatchKey(match);
  // Optimistic chosen URI: a staged (unsaved) override wins over the persisted
  // one. A ``null`` override means the steward staged "keep novel".
  const hasPending = pendingOverride !== undefined;
  const stagedNovel = pendingOverride === null;
  const chosenUri = hasPending ? pendingOverride : match.matched_class_uri;
  const isGrounded =
    match.match_type === "exact" || match.match_type === "high_confidence";
  const isSuggested = match.match_type === "ambiguous";
  const isExactName = isGrounded && match.similarity === 1;
  const editable = !!onSelectCandidate && candidates.length > 0;

  // Persisted-chosen URI, used to keep the originally-selected candidate at the
  // top of the table (the funnel's "winner" row) regardless of staged edits.
  const persistedUri = match.matched_class_uri ?? null;
  const sortedCandidates = [...candidates].sort((a, b) => {
    const aChosen = persistedUri === a.entity_uri ? 1 : 0;
    const bChosen = persistedUri === b.entity_uri ? 1 : 0;
    if (aChosen !== bChosen) return bChosen - aChosen;
    return candidateScore(b) - candidateScore(a);
  });

  const selectedItems = chosenUri
    ? sortedCandidates.filter((c) => c.entity_uri === chosenUri)
    : [];

  return (
    <SpaceBetween size="s">
      {/* Status line — current grounding state, unsaved indicator. */}
      {isSuggested && chosenUri ? (
        <StatusIndicator type={hasPending ? "pending" : "warning"}>
          {t("Ambiguous match to {name}", { name: localName(chosenUri) })}
          {hasPending && t(" (unsaved — click Save changes to apply)")}
          {match.matched_ontology_id && !hasPending && (
            <>
              {t(" from {ontology}", {
                ontology: ontologyLabel(match.matched_ontology_id),
              })}
            </>
          )}
          <Box variant="small" display="inline" color="text-status-inactive">
            {t(" — low confidence, review before accepting")}
          </Box>
        </StatusIndicator>
      ) : chosenUri ? (
        <StatusIndicator type={hasPending ? "pending" : "success"}>
          {t("Grounded to {name}", { name: localName(chosenUri) })}
          {hasPending && t(" (unsaved — click Save changes to apply)")}
          {match.matched_ontology_id && !hasPending && (
            <>
              {t(" from {ontology}", {
                ontology: ontologyLabel(match.matched_ontology_id),
              })}
            </>
          )}
          {isExactName && !hasPending && (
            <Box variant="small" display="inline" color="text-status-inactive">
              {t(" — exact name identity")}
            </Box>
          )}
        </StatusIndicator>
      ) : stagedNovel ? (
        <StatusIndicator type="pending">
          {t("Kept novel (unsaved — click Save changes to apply)")}
        </StatusIndicator>
      ) : isSuggested ? (
        <StatusIndicator type="warning">
          {t(
            candidates.length === 1
              ? "No confident match — {count} suggestion below"
              : "No confident match — {count} suggestions below",
            { count: candidates.length },
          )}
        </StatusIndicator>
      ) : (
        <StatusIndicator type="stopped">
          {t("Novel concept — no foundational match")}
        </StatusIndicator>
      )}

      {match.reason && (
        <Box variant="small" color="text-status-inactive">
          <em>{t("Reasoning: {reason}", { reason: match.reason })}</em>
        </Box>
      )}

      {isExactName && candidates.length > 0 && (
        <Box variant="small" color="text-status-inactive">
          {t(
            "Scores below are embedding similarity (semantic closeness of the table description to each class definition). The match above was decided by name identity, not embedding score.",
          )}
        </Box>
      )}

      <SortableTable
        items={sortedCandidates}
        trackBy="entity_uri"
        variant="embedded"
        defaultSortingColumnId="score"
        defaultSortingDescending
        selectionType={editable ? "single" : undefined}
        selectedItems={editable ? selectedItems : undefined}
        onSelectionChange={
          editable
            ? ({ detail }) => {
                const picked = detail.selectedItems[0];
                if (picked && picked.entity_uri !== chosenUri) {
                  onSelectCandidate?.(matchKey, picked.entity_uri);
                }
              }
            : undefined
        }
        ariaLabels={{
          selectionGroupLabel: t("Grounding candidate"),
          itemSelectionLabel: (_data, row) =>
            t("Ground to {name}", { name: localName(row.entity_uri) }),
        }}
        columnDefinitions={[
          {
            id: "candidate",
            header: t("Candidate class"),
            isRowHeader: true,
            sortingComparator: (a, b) =>
              localName(a.entity_uri).localeCompare(localName(b.entity_uri)),
            cell: (c) => (
              <Box fontWeight={chosenUri === c.entity_uri ? "bold" : "normal"}>
                {localName(c.entity_uri)}
              </Box>
            ),
          },
          {
            id: "ontology",
            header: t("Ontology"),
            sortingComparator: (a, b) =>
              (ontologyLabel(a.ontology_id) ?? "").localeCompare(
                ontologyLabel(b.ontology_id) ?? "",
              ),
            cell: (c) => {
              const source = ontologyLabel(c.ontology_id);
              return source ? <Badge color="blue">{source}</Badge> : "—";
            },
          },
          {
            id: "score",
            header: (
              <SpaceBetween
                direction="horizontal"
                size="xxs"
                alignItems="center"
              >
                <span>{t("Score")}</span>
                <InfoPopover
                  header={t("Match confidence")}
                  ariaLabel={t("About the match confidence score")}
                >
                  <SpaceBetween size="xs">
                    <Box variant="p">
                      {t(
                        "How closely the table matches each candidate class, from 0 to 1 (higher is stronger).",
                      )}
                    </Box>
                    <Box variant="p">
                      {t(
                        "Enhanced mode blends embedding similarity with LLM confidence; Standard mode uses similarity alone.",
                      )}
                    </Box>
                  </SpaceBetween>
                </InfoPopover>
              </SpaceBetween>
            ),
            sortingComparator: (a, b) => candidateScore(a) - candidateScore(b),
            cell: (c) => (
              <ScoreBar
                score={candidateScore(c)}
                chosen={chosenUri === c.entity_uri}
              />
            ),
          },
        ]}
        empty={
          <Box color="text-body-secondary">
            {t("No foundational grounding candidates.")}
          </Box>
        }
      />

      {/* "Mark novel" affordance — stages "keep grounding cleared". Only shown
          when editing is possible and the class is not already staged-novel. */}
      {editable && onMarkNovel && !stagedNovel && (
        <ButtonWithHint
          hint={t(
            "Clears grounding so this class becomes a new concept with no foundational link. Use when no candidate is a real match. Staged until you Save changes.",
          )}
          variant="inline-link"
          onClick={() => onMarkNovel(matchKey)}
        >
          {t("Mark novel (no grounding)")}
        </ButtonWithHint>
      )}
    </SpaceBetween>
  );
}
