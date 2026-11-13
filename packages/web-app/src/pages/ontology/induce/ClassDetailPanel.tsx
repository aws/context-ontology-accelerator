// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * ClassDetailPanel — side panel content for a proposed class.
 *
 * Shows structured fields (label, description, grounding) by default with
 * inline editing. Toggle to "Edit raw" for full Turtle editing.
 * Properties are clickable for editing. R2RML shown as a readable table.
 */
import { useMemo, useState } from "react";
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
import { VocabularyReviewPanel } from "./VocabularyReviewPanel";
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
  /** Parsed mapping for this class, built from the whole R2RML document. */
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
  // Foundational grounding editor (in the Grounding tab)
  /** This class's foundational ConceptMatch (grounded class + candidates), or
   *  null when the class has no table-level match. */
  groundingMatch?: ConceptMatch | null;
  /** Staged-but-unsaved override for this class's grounding match. */
  pendingGroundingOverride?: string | null;
  /** Stage a chosen candidate URI for a grounding match key. */
  onSelectGroundingCandidate?: (matchKey: string, uri: string) => void;
  /** Stage "keep novel" for a grounding match key. */
  onMarkGroundingNovel?: (matchKey: string) => void;
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
  const prefixes = useMemo(() => parsePrefixes(prelude ?? ""), [prelude]);
  const iriOf = (entity: TurtleEntity) =>
    entity.iri ?? expandIri(entity.fullSubject, prefixes) ?? entity.fullSubject;
  const classIri = iriOf(g.classEntity);
  // The vocabulary section covers this class and its own properties.
  const vocabularyTermIris = [
    g.classEntity,
    ...g.attributes,
    ...g.relationships,
  ].map(iriOf);
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
            {/* Grounding comes from skos exact/close/relatedMatch; ``g.groundedTo``
                is the foundational target's local name. */}
            {g.matchType === "exact" || g.matchType === "close" ? (
              <Box>
                <Box variant="awsui-key-label">{t("Grounded to")}</Box>
                <Badge color="blue">{g.groundedTo}</Badge>
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

            {/* Explains why a subclass edge exists. */}
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
            {/* SuggestionPanel renders null without a suggestion; read-only
                contexts fall back to the static badge. */}
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

          {fullTurtle && (
            <SpaceBetween size="xs">
              {/* A vocabulary edit rewrites the class, which remounts this
                  panel and would drop the unapplied fields above. */}
              {!isReadOnly && onUpdateTurtle && dirty && (
                <Box variant="small" color="text-status-inactive">
                  {t("Apply the changes above before editing the vocabulary.")}
                </Box>
              )}
              <VocabularyReviewPanel
                turtle={fullTurtle}
                termIris={vocabularyTermIris}
                isReadOnly={isReadOnly || !onUpdateTurtle || dirty}
                onChange={(updatedTurtle) => onUpdateTurtle?.(updatedTurtle)}
              />
            </SpaceBetween>
          )}

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
                // Counts the foundational candidate options, distinct from the
                // "Matches" tab, which counts sibling classes.
                label: t("Grounding ({count})", {
                  count: groundingCandidateCount,
                }),
                content: (
                  <SpaceBetween size="l">
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

      {r2rmlMapping && (
        <ExpandableSection
          headerText={t("Mapping: {table}", {
            table: r2rmlMapping.sourceTable,
          })}
          defaultExpanded
        >
          <SpaceBetween size="s">
            <SortableTable
              items={r2rmlMapping.columnMappings}
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

      {!editRaw && (
        <ExpandableSection headerText={t("Source (Turtle)")}>
          <TurtleHighlight>{g.classEntity.body}</TurtleHighlight>
        </ExpandableSection>
      )}
    </SpaceBetween>
  );
}
