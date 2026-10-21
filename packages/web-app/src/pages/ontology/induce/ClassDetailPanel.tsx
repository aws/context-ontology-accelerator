// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * ClassDetailPanel — side panel content for a proposed class.
 *
 * Shows structured fields (label, description, grounding) by default with
 * inline editing. Toggle to "Edit raw" for full Turtle editing.
 * Properties are clickable for editing. R2RML shown as a readable table.
 */
import { useState } from "react";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { SortableTable } from "@components/SortableTable";
import { InfoPopover } from "@components/InfoPopover";
import Tabs from "@cloudscape-design/components/tabs";
import Textarea from "@cloudscape-design/components/textarea";
import Toggle from "@cloudscape-design/components/toggle";
import { TurtleHighlight } from "@components/TurtleHighlight";
import { SuggestionPanel } from "@components/ontology/SuggestionPanel";
import { expandIri, parsePrefixes } from "@utils/turtleLexer";
import type { TurtleEntity } from "./turtle";
import { GroundingCandidates } from "./GroundingCandidates";
import {
  extractQuoted,
  extractUri,
  localName,
  type ConceptMatch,
  type R2rmlMapping,
} from "./grounding";
import { useAppI18n } from "../../../i18n/LocaleProvider";

/** Replace a quoted literal value in a Turtle body. */
function replaceQuoted(body: string, pred: string, newValue: string): string {
  const re = new RegExp(`(${pred}\\s+)"[^"]*"`);
  if (re.test(body)) {
    return body.replace(re, `$1"${newValue}"`);
  }
  const trimmed = body.trimEnd();
  const endsWithDot = trimmed.endsWith(".");
  const base = endsWithDot ? trimmed.slice(0, -1).trimEnd() : trimmed;
  return `${base} ;\n    ${pred} "${newValue}" .`;
}

// R2RML parsing moved to ``grounding.ts::parseR2rmlByClass`` (Item 4). The
// old block-slice ``parseR2rml`` here matched ``rr:tableName`` over a per-class
// slice that never actually carried it (it lives on a separate rr:logicalTable
// blank node), so it always fell back to "Unknown" with no columns. The parsed
// ``R2rmlMapping`` is now derived once from the whole R2RML document upstream
// and passed in via the ``r2rmlMapping`` prop.

// ─── Property editor ────────────────────────────────────────────────

function PropertyEditor({
  entity,
  onSave,
  onBack,
  isReadOnly,
}: {
  entity: TurtleEntity;
  onSave: (updated: string) => void;
  onBack: () => void;
  isReadOnly: boolean;
}) {
  const { t } = useAppI18n();
  const [label, setLabel] = useState(
    extractQuoted(entity.body, "rdfs:label") ?? entity.subject,
  );
  const [editRaw, setEditRaw] = useState(false);
  const [rawTurtle, setRawTurtle] = useState(entity.body);
  const [dirty, setDirty] = useState(false);

  const domain = extractUri(entity.body, "rdfs:domain");
  const range = extractUri(entity.body, "rdfs:range");
  const comment = extractQuoted(entity.body, "rdfs:comment");

  return (
    <SpaceBetween size="m">
      <Button variant="link" iconName="arrow-left" onClick={onBack}>
        {t("Back to class")}
      </Button>

      {!isReadOnly && (
        <Box float="right">
          <Toggle
            checked={editRaw}
            onChange={({ detail }) => setEditRaw(detail.checked)}
          >
            {t("Edit raw")}
          </Toggle>
        </Box>
      )}

      {editRaw ? (
        <SpaceBetween size="s">
          <Textarea
            value={rawTurtle}
            onChange={({ detail }) => {
              setRawTurtle(detail.value);
              setDirty(true);
            }}
            rows={10}
            disabled={isReadOnly}
          />
          {!isReadOnly && (
            <Button
              variant="primary"
              disabled={!dirty}
              onClick={() => {
                onSave(rawTurtle);
                setDirty(false);
              }}
            >
              {t("Apply")}
            </Button>
          )}
        </SpaceBetween>
      ) : (
        <SpaceBetween size="s">
          <FormField label={t("Label")}>
            {isReadOnly ? (
              <Box>{label}</Box>
            ) : (
              <Input
                value={label}
                onChange={({ detail }) => {
                  setLabel(detail.value);
                  setDirty(true);
                }}
              />
            )}
          </FormField>
          <FormField label={t("Domain (class)")}>
            <Box>{domain ?? "—"}</Box>
          </FormField>
          <FormField label={t("Range")}>
            <Box>{range ?? "—"}</Box>
          </FormField>
          {comment && (
            <FormField label={t("Description")}>
              <Box>{comment}</Box>
            </FormField>
          )}
          {!isReadOnly && dirty && (
            <Button
              variant="primary"
              onClick={() => {
                const updated = replaceQuoted(entity.body, "rdfs:label", label);
                onSave(updated);
                setDirty(false);
              }}
            >
              {t("Apply")}
            </Button>
          )}
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}

// ─── Main panel ─────────────────────────────────────────────────────

export interface ClassGroup {
  classEntity: TurtleEntity;
  label: string;
  description: string | null;
  groundedTo: string | null;
  matchType: string | null;
  /** owl:hasKey columns (bare names). */
  pkColumns: string[];
  /** coa:subClassProvenance literal, e.g. "PK_SHARING". */
  subClassProvenance: string | null;
  /** True when the class carries a coa:suggestedSubClassOf (unconfirmed). */
  hasSuggestedParent: boolean;
  relationships: TurtleEntity[];
  attributes: TurtleEntity[];
  matches?: { matchType: "close" | "exact"; label: string; classUri: string }[];
}

interface Props {
  group: ClassGroup;
  /** Raw per-class R2RML slice — for the "View raw R2RML" pane only. */
  r2rml: string | null;
  /** Correctly-parsed mapping (source table + column mappings) from the whole
   *  R2RML document (Item 4). Used for the readable mapping table. */
  r2rmlMapping?: R2rmlMapping | null;
  /** The whole proposal Turtle (prelude + all entities), possibly with unsaved
   *  edits. Needed by the SuggestionPanel, which re-parses the full document to
   *  find + rewrite this class's ``coa:suggestedSubClassOf`` annotations. */
  fullTurtle?: string | null;
  /** Prefix declarations for the proposal Turtle — used to expand the class's
   *  compact ``fullSubject`` (e.g. ``ind:Claim``) to the absolute IRI the
   *  SuggestionPanel matches triples on. */
  prelude?: string;
  isReadOnly: boolean;
  onSave: (updatedTurtle: string) => void;
  onSaveProperty?: (entity: TurtleEntity, updatedBody: string) => void;
  /** Called when the SuggestionPanel rewrites the WHOLE proposal Turtle
   *  (confirm/reject a suggested subclass). Persisted via ProposalDetail's
   *  dirty-state + Save flow. Omitted in read-only contexts. */
  onUpdateTurtle?: (updatedTurtle: string) => void;
  // ─── Foundational grounding editor (folded into the Matches tab, Item 1) ──
  /** This class's foundational ConceptMatch (grounded class + candidates), or
   *  null when the class has no table-level match. */
  groundingMatch?: ConceptMatch | null;
  /** Staged-but-unsaved override for this class's source table. */
  pendingGroundingOverride?: string | null;
  /** Stage a chosen candidate URI for a source table. */
  onSelectGroundingCandidate?: (table: string, uri: string) => void;
  /** Stage "keep novel" for a source table. */
  onMarkGroundingNovel?: (table: string) => void;
}

export function ClassDetailPanel({
  group,
  r2rml,
  r2rmlMapping,
  fullTurtle,
  prelude,
  isReadOnly,
  onSave,
  onSaveProperty,
  groundingMatch,
  pendingGroundingOverride,
  onSelectGroundingCandidate,
  onMarkGroundingNovel,
  onUpdateTurtle,
}: Props) {
  const { t } = useAppI18n();
  const g = group;
  // Absolute IRI for this class — the SuggestionPanel matches ``coa:``
  // annotation triples on the expanded subject, but ``fullSubject`` is the
  // compact ``prefix:Local`` form. Expand it through the proposal's prefix
  // table (falls back to the compact token if the prefix is unknown).
  const classIri =
    expandIri(g.classEntity.fullSubject, parsePrefixes(prelude ?? "")) ??
    g.classEntity.fullSubject;
  // Label lookup for the SuggestionPanel headers. This class's own label comes
  // from the group; other classes (the suggested parent) fall back to their
  // local name — the panel's copy reads fine with a local name.
  const labelOf = (iri: string): string =>
    iri === classIri ? g.label : localName(iri);
  const [editRaw, setEditRaw] = useState(false);
  const [rawTurtle, setRawTurtle] = useState(g.classEntity.body);
  const [label, setLabel] = useState(g.label);
  const [description, setDescription] = useState(g.description ?? "");
  const [dirty, setDirty] = useState(false);
  const [editingProperty, setEditingProperty] = useState<TurtleEntity | null>(
    null,
  );

  // If editing a property, show the property editor instead
  if (editingProperty) {
    return (
      <PropertyEditor
        entity={editingProperty}
        isReadOnly={isReadOnly}
        onBack={() => setEditingProperty(null)}
        onSave={(updated) => {
          onSaveProperty?.(editingProperty, updated);
          setEditingProperty(null);
        }}
      />
    );
  }

  function handleStructuredSave() {
    let updated = g.classEntity.body;
    if (label !== g.label)
      updated = replaceQuoted(updated, "rdfs:label", label);
    if (description !== (g.description ?? ""))
      updated = replaceQuoted(updated, "rdfs:comment", description);
    onSave(updated);
    setDirty(false);
  }

  const parsedR2rml = r2rmlMapping ?? null;
  // Candidate count drives the Grounding tab label — the number of options the
  // steward actually sees in the table, not "1" (which conflated "a match
  // exists" with the option count). 0 / undefined → no count on the label.
  const groundingCandidateCount = groundingMatch?.candidates?.length ?? 0;

  return (
    <SpaceBetween size="m">
      {!isReadOnly && (
        <Box float="right">
          <Toggle
            checked={editRaw}
            onChange={({ detail }) => setEditRaw(detail.checked)}
          >
            {t("Edit raw Turtle")}
          </Toggle>
        </Box>
      )}

      {editRaw ? (
        <SpaceBetween size="s">
          <Textarea
            value={rawTurtle}
            onChange={({ detail }) => {
              setRawTurtle(detail.value);
              setDirty(true);
            }}
            rows={15}
            disabled={isReadOnly}
          />
          {!isReadOnly && (
            <Button
              variant="primary"
              disabled={!dirty}
              onClick={() => {
                onSave(rawTurtle);
                setDirty(false);
              }}
            >
              {t("Apply")}
            </Button>
          )}
        </SpaceBetween>
      ) : (
        <SpaceBetween size="m">
          <SpaceBetween size="s">
            <FormField label={t("Label")}>
              {isReadOnly ? (
                <Box>{label}</Box>
              ) : (
                <Input
                  value={label}
                  onChange={({ detail }) => {
                    setLabel(detail.value);
                    setDirty(true);
                  }}
                />
              )}
            </FormField>
            <FormField label={t("Description")}>
              {isReadOnly ? (
                <Box>{description || "—"}</Box>
              ) : (
                <Textarea
                  value={description}
                  onChange={({ detail }) => {
                    setDescription(detail.value);
                    setDirty(true);
                  }}
                  rows={2}
                />
              )}
            </FormField>
            {/* Origin — grounding is carried by skos:exactMatch/closeMatch/relatedMatch
                (coa:groundedTo retired, Item 8); ``g.groundedTo`` is the
                foundational target's local name. */}
            {g.matchType === "exact" || g.matchType === "close" ? (
              <Box>
                <Box variant="awsui-key-label">{t("Grounded to")}</Box>
                <Badge color="blue">{g.groundedTo}</Badge>
                {g.matchType && (
                  <>
                    <Box variant="small" display="inline">
                      {t(" ({type} match) ", { type: g.matchType })}
                    </Box>
                    <InfoPopover
                      header={t("Exact vs close match")}
                      ariaLabel={t("About exact vs close match")}
                    >
                      <SpaceBetween size="xs">
                        <Box variant="p">
                          {t(
                            "Exact: the same concept as the foundational class (skos:exactMatch).",
                          )}
                        </Box>
                        <Box variant="p">
                          {t(
                            "Close: closely related but not identical (skos:closeMatch).",
                          )}
                        </Box>
                      </SpaceBetween>
                    </InfoPopover>
                  </>
                )}
              </Box>
            ) : g.matchType === "related" ? (
              <Box>
                <Box variant="awsui-key-label">{t("Origin")}</Box>
                <Badge>
                  {t("Ambiguous match to {name}", {
                    name: g.groundedTo ?? "",
                  })}
                </Badge>
                <Box variant="small" display="inline">
                  {t(" (low confidence, review Grounding tab)")}
                </Box>
              </Box>
            ) : (
              <Box>
                <Box variant="awsui-key-label">{t("Origin")}</Box>
                <Badge color="green">{t("Novel")}</Badge>
              </Box>
            )}

            {/* Primary key + inheritance provenance (Item 2). Main's card did
                not surface these; they explain WHY a subclass edge exists. */}
            {g.pkColumns.length > 0 && (
              <Box>
                <Box variant="awsui-key-label">
                  {t("Primary key (owl:hasKey)")}
                </Box>
                <SpaceBetween direction="horizontal" size="xs">
                  {g.pkColumns.map((col) => (
                    <Badge key={col} color="grey">
                      {col}
                    </Badge>
                  ))}
                </SpaceBetween>
              </Box>
            )}
            {g.subClassProvenance === "PK_SHARING" && (
              <Box>
                <Box variant="awsui-key-label">{t("Inheritance")}</Box>
                <Badge color="blue">{t("PK-sharing")}</Badge>
                <Box variant="small" display="inline">
                  {t(" subclass detected from shared primary key")}
                </Box>
              </Box>
            )}
            {/* Suggested subclass (Item 2 → now actionable). When we have the
                whole Turtle + a whole-Turtle edit callback, render the real
                SuggestionPanel so the steward can Confirm (promote to a real
                rdfs:subClassOf) or Reject. The panel returns null when the
                class has no coa:suggestedSubClassOf, so it's safe to always
                render here. In read-only / turtle-less contexts we keep the
                original static badge as an informational fallback. */}
            {fullTurtle && onUpdateTurtle && !isReadOnly ? (
              <SuggestionPanel
                iri={classIri}
                turtle={fullTurtle}
                onTurtleEdit={onUpdateTurtle}
                labelOf={labelOf}
              />
            ) : (
              g.hasSuggestedParent && (
                <Box>
                  <Box variant="awsui-key-label">{t("Suggested parent")}</Box>
                  <Badge color="blue">{t("Suggested subclass")}</Badge>
                  <Box variant="small" display="inline">
                    {t(" unconfirmed — review before accepting")}
                  </Box>
                </Box>
              )
            )}
          </SpaceBetween>

          {!isReadOnly && dirty && (
            <Button variant="primary" onClick={handleStructuredSave}>
              {t("Apply")}
            </Button>
          )}

          {/* Properties — clickable for editing */}
          <Tabs
            tabs={[
              {
                id: "relationships",
                label: t("Relationships ({count})", {
                  count: g.relationships.length,
                }),
                content: (
                  <SortableTable
                    items={g.relationships}
                    trackBy="fullSubject"
                    variant="embedded"
                    defaultSortingColumnId="name"
                    columnDefinitions={[
                      {
                        id: "name",
                        header: t("Property"),
                        isRowHeader: true,
                        sortingComparator: (a, b) =>
                          (
                            extractQuoted(a.body, "rdfs:label") ?? a.subject
                          ).localeCompare(
                            extractQuoted(b.body, "rdfs:label") ?? b.subject,
                          ),
                        cell: (p) => (
                          <Link
                            onFollow={(ev) => {
                              ev.preventDefault();
                              setEditingProperty(p);
                            }}
                          >
                            {extractQuoted(p.body, "rdfs:label") ?? p.subject}
                          </Link>
                        ),
                      },
                      {
                        id: "range",
                        header: t("Target class"),
                        sortingComparator: (a, b) =>
                          (
                            extractUri(a.body, "rdfs:range") ?? "—"
                          ).localeCompare(
                            extractUri(b.body, "rdfs:range") ?? "—",
                          ),
                        cell: (p) => extractUri(p.body, "rdfs:range") ?? "—",
                      },
                    ]}
                    empty={
                      <Box color="text-body-secondary">
                        {t("No relationships.")}
                      </Box>
                    }
                  />
                ),
              },
              {
                id: "attributes",
                label: t("Attributes ({count})", {
                  count: g.attributes.length,
                }),
                content: (
                  <SortableTable
                    items={g.attributes}
                    trackBy="fullSubject"
                    variant="embedded"
                    defaultSortingColumnId="name"
                    columnDefinitions={[
                      {
                        id: "name",
                        header: t("Property"),
                        isRowHeader: true,
                        sortingComparator: (a, b) =>
                          (
                            extractQuoted(a.body, "rdfs:label") ?? a.subject
                          ).localeCompare(
                            extractQuoted(b.body, "rdfs:label") ?? b.subject,
                          ),
                        cell: (p) => (
                          <Link
                            onFollow={(ev) => {
                              ev.preventDefault();
                              setEditingProperty(p);
                            }}
                          >
                            {extractQuoted(p.body, "rdfs:label") ?? p.subject}
                          </Link>
                        ),
                      },
                      {
                        id: "range",
                        header: t("Datatype"),
                        sortingComparator: (a, b) =>
                          (
                            extractUri(a.body, "rdfs:range") ?? "—"
                          ).localeCompare(
                            extractUri(b.body, "rdfs:range") ?? "—",
                          ),
                        cell: (p) => extractUri(p.body, "rdfs:range") ?? "—",
                      },
                    ]}
                    empty={
                      <Box color="text-body-secondary">
                        {t("No attributes.")}
                      </Box>
                    }
                  />
                ),
              },
              {
                id: "grounding",
                // EXTERNAL foundational grounding candidates (embedding recall +
                // LLM rerank against foundational ontologies). The count is the
                // number of candidate OPTIONS shown (~10), NOT "1" — always shown
                // incl. "(0)" for consistency with the sibling tabs. Distinct
                // from the "Matches" tab below, which counts SIBLING proposal
                // classes (intra-proposal skos dedup).
                label: t("Grounding ({count})", {
                  count: groundingCandidateCount,
                }),
                content: (
                  <SpaceBetween size="l">
                    {/* Foundational grounding editor (Item 1) — folded out of
                        the old standalone GroundingPanel. Editable Cloudscape
                        Table (consistent with the other tabs): pick a different
                        candidate / mark novel; staged upward and persisted via
                        the ProposalDetail Save-changes flow (grounding_overrides). */}
                    {groundingMatch ? (
                      <GroundingCandidates
                        match={groundingMatch}
                        pendingOverride={pendingGroundingOverride}
                        onSelectCandidate={
                          isReadOnly ? undefined : onSelectGroundingCandidate
                        }
                        onMarkNovel={
                          isReadOnly ? undefined : onMarkGroundingNovel
                        }
                      />
                    ) : (
                      <Box color="text-body-secondary">
                        {t(
                          "No foundational grounding evaluated for this class.",
                        )}
                      </Box>
                    )}
                  </SpaceBetween>
                ),
              },
              {
                id: "matches",
                // SIBLING (intra-proposal) skos:closeMatch/exactMatch — near-
                // duplicate classes the unstructured/RIGOR normalizer collapsed
                // against each other. Populated for unstructured proposals; 0 for
                // the structured path (which emits no intra-proposal matches).
                label: t("Matches ({count})", {
                  count: g.matches?.length ?? 0,
                }),
                content: (
                  <SortableTable
                    items={g.matches ?? []}
                    trackBy="classUri"
                    variant="embedded"
                    defaultSortingColumnId="class"
                    columnDefinitions={[
                      {
                        id: "type",
                        header: t("Match"),
                        isRowHeader: true,
                        sortingComparator: (a, b) =>
                          a.matchType.localeCompare(b.matchType),
                        cell: (m) => <Badge color="blue">{m.matchType}</Badge>,
                      },
                      {
                        id: "class",
                        header: t("Class"),
                        sortingComparator: (a, b) =>
                          a.label.localeCompare(b.label),
                        cell: (m) => m.label,
                      },
                    ]}
                    empty={
                      <Box color="text-body-secondary">
                        {t("No sibling matches to other proposal classes.")}
                      </Box>
                    }
                  />
                ),
              },
            ]}
          />
        </SpaceBetween>
      )}

      {/* R2RML — human-readable table with raw toggle */}
      {parsedR2rml && (
        <ExpandableSection
          headerText={t("Mapping: {table}", {
            table: parsedR2rml.sourceTable,
          })}
          defaultExpanded
        >
          <SpaceBetween size="s">
            <SortableTable
              items={parsedR2rml.columnMappings}
              trackBy="column"
              variant="embedded"
              defaultSortingColumnId="column"
              columnDefinitions={[
                {
                  id: "column",
                  header: t("Source column"),
                  isRowHeader: true,
                  sortingComparator: (a, b) => a.column.localeCompare(b.column),
                  cell: (m) => m.column,
                },
                {
                  id: "property",
                  header: t("Property"),
                  sortingComparator: (a, b) =>
                    a.property.localeCompare(b.property),
                  cell: (m) => m.property,
                },
                {
                  id: "type",
                  header: t("Type"),
                  sortingComparator: (a, b) => a.type.localeCompare(b.type),
                  cell: (m) =>
                    m.type === "relationship" ? (
                      <Badge color="green">
                        {t("FK → {target}", { target: m.target })}
                      </Badge>
                    ) : (
                      <Box variant="small">{m.target}</Box>
                    ),
                },
              ]}
              empty={
                <Box color="text-body-secondary">
                  {t("No column mappings parsed.")}
                </Box>
              }
            />
            {r2rml && (
              <ExpandableSection headerText={t("View raw R2RML")}>
                <TurtleHighlight>{r2rml}</TurtleHighlight>
              </ExpandableSection>
            )}
          </SpaceBetween>
        </ExpandableSection>
      )}

      {/* Collapsed raw class source */}
      {!editRaw && (
        <ExpandableSection headerText={t("Source (Turtle)")}>
          <TurtleHighlight>{g.classEntity.body}</TurtleHighlight>
        </ExpandableSection>
      )}
    </SpaceBetween>
  );
}
