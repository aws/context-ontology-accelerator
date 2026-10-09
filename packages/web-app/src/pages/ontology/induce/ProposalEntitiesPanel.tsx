// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * ProposalEntitiesPanel — Class-centric table with side-panel detail.
 *
 * Groups properties by their domain class. Each class is a row showing
 * its relationship/attribute counts. Selecting a class opens the side
 * panel with the class info + a list of its properties.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { SortableTable } from "@components/SortableTable";
import { TurtleHighlight } from "@components/TurtleHighlight";
import { InfoPopover } from "@components/InfoPopover";
import { useSplitPanel } from "@components/SplitPanelProvider";
import { ClassDetailPanel } from "./ClassDetailPanel";
import type { TurtleEntity } from "./turtle";
import {
  buildClassMatchResolver,
  conceptMatchKey,
  extractQuoted,
  extractUri,
  localName,
  parseR2rmlByClass,
  type ConceptMatch,
} from "./grounding";

// ─── Helpers ────────────────────────────────────────────────────────

/**
 * All ``skos:exactMatch``/``skos:closeMatch``/``skos:relatedMatch`` object
 * tokens on a class body, with the match strength. rdflib serializes each match
 * as ``skos:exactMatch <iri>`` or ``skos:closeMatch prefix:Local`` (comma lists
 * possible), so scan globally rather than grabbing the first.
 */
function extractSkosMatches(
  body: string,
): { token: string; strength: "exact" | "close" | "related" }[] {
  const out: { token: string; strength: "exact" | "close" | "related" }[] = [];
  const re = /skos:(exactMatch|closeMatch|relatedMatch)\s+(<[^>]+>|[^\s;,]+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(body)) !== null) {
    out.push({
      token: m[2],
      strength:
        m[1] === "exactMatch"
          ? "exact"
          : m[1] === "closeMatch"
            ? "close"
            : "related",
    });
  }
  return out;
}

/** Bare column names from ``owl:hasKey ( ind:col1 ind:col2 )`` (ported from
 *  the !436 helper): strip the list delimiters + any ``prefix:`` on members. */
function extractPkColumns(body: string): string[] {
  const m = body.match(/owl:hasKey\s*\(([^)]*)\)/);
  if (!m) return [];
  return m[1]
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .map((tok) => {
      const bare = tok.replace(/[<>]/g, "").split(/[#/]/).pop() ?? tok;
      const colon = bare.lastIndexOf(":");
      return colon >= 0 ? bare.slice(colon + 1) : bare;
    });
}

/**
 * True when a property body's ``rdfs:domain`` object equals ``ind:<clsName>``
 * (or the class's full subject token). rdflib serializes the domain object as a
 * single token (prefixed name or ``<iri>``) terminated by whitespace, ``;``,
 * ``,``, ``.`` or end-of-string, so we compare the WHOLE captured token rather
 * than substring-testing — a bare ``includes("rdfs:domain ind:Claim")`` would
 * spuriously match ``rdfs:domain ind:ClaimAmount`` and let ``Claim`` absorb
 * ``ClaimAmount``'s properties.
 */
function propertyDomainMatches(
  propBody: string,
  clsName: string,
  fullSubject: string,
): boolean {
  const re = /rdfs:domain\s+(<[^>]+>|[^\s;,.]+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(propBody)) !== null) {
    const token = m[1];
    if (token === `ind:${clsName}` || token === fullSubject) return true;
  }
  return false;
}

// ─── Grouped model ──────────────────────────────────────────────────

interface ClassMatch {
  matchType: "close" | "exact";
  label: string;
  classUri: string;
}

interface ClassGroup {
  classEntity: TurtleEntity;
  label: string;
  description: string | null;
  /** Foundational grounding target (local name), or null when novel. Derived
   *  from skos:exactMatch/closeMatch/relatedMatch whose target is NOT a sibling
   *  proposal class (coa:groundedTo is retired — see Item 8 re-key). */
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
  matches?: ClassMatch[];
}

interface GroupResult {
  groups: ClassGroup[];
  ungrouped: TurtleEntity[];
}

export function groupByClass(entities: TurtleEntity[]): GroupResult {
  const classes = entities.filter((e) => e.kind.includes("Class"));
  const objProps = entities.filter((e) => e.kind.includes("ObjectProperty"));
  const dataProps = entities.filter((e) => e.kind.includes("DatatypeProperty"));

  const claimed = new Set<string>();

  // Bidirectional close/exact-match index (one pass, O(n)): a class is matched
  // whether it authors the skos:closeMatch/exactMatch or is the target of one
  // on another class. Keyed by local name so prefix vs full-URI targets resolve.
  //
  // These sibling (intra-proposal) skos matches are REAL for unstructured /
  // RIGOR inductions: the unstructured class normalizer runs a per-run embedding
  // dedup index and emits skos:closeMatch from a class to an earlier SIBLING
  // class it collapsed against (class_normalizer.normalize → induction_rules
  // Req 9.4). The structured (table_to_ontology) path does not emit these today,
  // so the "Matches" column is 0 for structured proposals but populated for
  // unstructured ones. The classByLocal filter below is also what lets grounding
  // detection exclude these sibling targets from the external-grounding origin.
  const matchLocal = (token: string) =>
    token.replace(/[<>]/g, "").split(/[#/]/).pop() ?? token;
  const classByLocal = new Map<string, TurtleEntity>();
  for (const c of classes) classByLocal.set(matchLocal(c.fullSubject), c);
  const matchesByLocal = new Map<string, ClassMatch[]>();
  const addMatch = (
    fromLocal: string,
    toLocal: string,
    matchType: "close" | "exact",
  ) => {
    const target = classByLocal.get(toLocal);
    if (!target) return;
    const list = matchesByLocal.get(fromLocal) ?? [];
    if (
      !list.some((m) => m.classUri === toLocal && m.matchType === matchType)
    ) {
      list.push({
        matchType,
        classUri: toLocal,
        label: extractQuoted(target.body, "rdfs:label") ?? target.subject,
      });
      matchesByLocal.set(fromLocal, list);
    }
  };
  const matchRe = /skos:(closeMatch|exactMatch)\s+(<[^>]+>|[^\s;,]+)/g;
  for (const c of classes) {
    const fromLocal = matchLocal(c.fullSubject);
    let m: RegExpExecArray | null;
    matchRe.lastIndex = 0;
    while ((m = matchRe.exec(c.body)) !== null) {
      const type = m[1] === "exactMatch" ? "exact" : "close";
      const toLocal = matchLocal(m[2]);
      addMatch(fromLocal, toLocal, type);
      addMatch(toLocal, fromLocal, type);
    }
  }

  const groups = classes.map((cls) => {
    const clsName = cls.subject;
    const rels = objProps.filter((p) =>
      propertyDomainMatches(p.body, clsName, cls.fullSubject),
    );
    const attrs = dataProps.filter((p) =>
      propertyDomainMatches(p.body, clsName, cls.fullSubject),
    );
    for (const p of [...rels, ...attrs]) claimed.add(p.fullSubject);

    // Foundational grounding target: a skos:exactMatch/closeMatch/relatedMatch
    // whose target is NOT another proposal class. (coa:groundedTo is retired —
    // Item 8.) A skos match to a SIBLING ind: class is a dedup/close-match
    // relationship surfaced separately via ``matches``, not grounding, so we
    // exclude those to avoid false "Grounded" origins. Exact wins over close,
    // close wins over related.
    const skosMatches = extractSkosMatches(cls.body);
    const foundational =
      skosMatches.find(
        (sm) =>
          sm.strength === "exact" && !classByLocal.has(matchLocal(sm.token)),
      ) ??
      skosMatches.find(
        (sm) =>
          sm.strength === "close" && !classByLocal.has(matchLocal(sm.token)),
      ) ??
      skosMatches.find(
        (sm) =>
          sm.strength === "related" && !classByLocal.has(matchLocal(sm.token)),
      ) ??
      null;

    // Match strength for the badge: ONLY the foundational match's strength, so
    // matchType and groundedTo are set together and can never disagree. A
    // sibling-only skos match (dedup relationship, no foundational target) is
    // NOT grounding — it leaves both null so the origin renders "Novel". A prior
    // body.includes(...) fallback set matchType here even with groundedTo null,
    // which made the origin gates render "Grounded"/"Ambiguous" with a blank
    // target for sibling-only classes (bug #910 follow-up).
    const matchType: "exact" | "close" | "related" | null = foundational
      ? foundational.strength
      : null;

    return {
      classEntity: cls,
      label: extractQuoted(cls.body, "rdfs:label") ?? clsName,
      description: extractQuoted(cls.body, "rdfs:comment"),
      groundedTo: foundational ? matchLocal(foundational.token) : null,
      matchType,
      pkColumns: extractPkColumns(cls.body),
      subClassProvenance: extractQuoted(cls.body, "coa:subClassProvenance"),
      hasSuggestedParent: cls.body.includes("coa:suggestedSubClassOf"),
      relationships: rels,
      attributes: attrs,
      matches: matchesByLocal.get(matchLocal(cls.fullSubject)) ?? [],
    };
  });

  const ungrouped = [...objProps, ...dataProps].filter(
    (p) => !claimed.has(p.fullSubject),
  );

  return { groups, ungrouped };
}

// ─── Props ──────────────────────────────────────────────────────────

interface Props {
  entities: TurtleEntity[];
  prelude: string;
  /** The whole proposal Turtle (prelude + all entities), possibly with unsaved
   *  edits. Needed by the class-card SuggestionPanel, which parses the full
   *  document to find/rewrite ``coa:suggestedSubClassOf`` annotations. */
  fullTurtle?: string | null;
  r2rmlTurtle?: string | null;
  isReadOnly: boolean;
  onEditEntity: (entity: TurtleEntity, index: number) => void;
  /** Called when the user saves edits to a class's Turtle in the side panel. */
  onUpdateEntityTurtle?: (entity: TurtleEntity, updatedBody: string) => void;
  /** Called when a control rewrites the WHOLE proposal Turtle (e.g. the
   *  SuggestionPanel Confirm/Reject promotes/drops a suggested subclass). */
  onUpdateTurtle?: (updatedTurtle: string) => void;
  // ─── Grounding editor (folded into the class-card Matches tab, Item 1) ──
  /** ConceptMatch records (proposal ``metadata.matches``), keyed per source
   *  table. The class→match link is recovered via ``buildClassMatchResolver``.
   *  Owned + persisted by ProposalDetail (``grounding_overrides``). */
  groundingMatches?: ConceptMatch[];
  /** Staged-but-unsaved overrides (stable table identity, or legacy bare table,
   *  → chosen foundational URI; ``null`` keeps the table novel). */
  pendingGroundingOverrides?: Record<string, string | null>;
  /** Stage a chosen candidate URI for a stable grounding match key. */
  onSelectGroundingCandidate?: (matchKey: string, uri: string) => void;
  /** Stage "keep novel" for a stable grounding match key. */
  onMarkGroundingNovel?: (matchKey: string) => void;
}

// ─── Component ──────────────────────────────────────────────────────

export function ProposalEntitiesPanel({
  entities,
  prelude,
  fullTurtle,
  r2rmlTurtle,
  isReadOnly,
  onEditEntity,
  onUpdateEntityTurtle,
  onUpdateTurtle,
  groundingMatches,
  pendingGroundingOverrides,
  onSelectGroundingCandidate,
  onMarkGroundingNovel,
}: Props) {
  const [selectedClassUri, setSelectedClassUri] = useState<string | null>(null);
  const { openPanel, closePanel, state: panelState } = useSplitPanel();

  const { groups, ungrouped } = useMemo(
    () => groupByClass(entities),
    [entities],
  );

  // Derive the selected group from the fresh groups array
  const selectedGroup = useMemo(
    () =>
      selectedClassUri
        ? (groups.find((g) => g.classEntity.fullSubject === selectedClassUri) ??
          null)
        : null,
    [selectedClassUri, groups],
  );

  // Clear the selection only when the panel is closed *externally* (the X
  // button) — i.e. a real open→closed transition. The ref guard is essential:
  // without it this effect also fired on the very first selection, before
  // ``openPanel()`` had flipped ``isOpen`` to true, which cleared the
  // selection and made the just-opened panel snap shut.
  const wasPanelOpenRef = useRef(false);
  useEffect(() => {
    if (wasPanelOpenRef.current && !panelState.isOpen && selectedClassUri) {
      setSelectedClassUri(null);
    }
    wasPanelOpenRef.current = panelState.isOpen;
  }, [panelState.isOpen, selectedClassUri]);

  // Raw R2RML slice per class — for the "View raw R2RML" pane only. This
  // block-slice is intentionally lossy (it captures only the block carrying
  // ``rr:class``), which is exactly why the OLD ``parseR2rml`` produced
  // "Mapping: Unknown". The parsed mapping below is derived from the WHOLE
  // document via the TriplesMap-id join instead (Item 4).
  const r2rmlRawByClass = useMemo(() => {
    if (!r2rmlTurtle) return new Map<string, string>();
    const map = new Map<string, string>();
    const blocks = r2rmlTurtle.split(/\n\s*\n/).filter((b) => b.trim());
    for (const block of blocks) {
      const classMatch = block.match(/rr:class\s+([^\s;,]+)/);
      if (classMatch) {
        const cls = localName(classMatch[1]);
        map.set(cls, (map.get(cls) ?? "") + block + "\n\n");
      }
    }
    return map;
  }, [r2rmlTurtle]);

  // Correctly-parsed R2RML mapping per class (source table + column mappings),
  // keyed by class local name. Layout-independent TriplesMap-id + POM-id join
  // over the whole document (fixes "Mapping: Unknown").
  const r2rmlMappingByClass = useMemo(
    () => parseR2rmlByClass(r2rmlTurtle),
    [r2rmlTurtle],
  );

  // Class → foundational ConceptMatch resolver (grounding candidates are keyed
  // per source table; the resolver inverts the class↔table link).
  const classMatchResolver = useMemo(
    () => buildClassMatchResolver(groundingMatches ?? [], r2rmlTurtle),
    [groundingMatches, r2rmlTurtle],
  );

  // Open/close the split panel when selection changes
  useEffect(() => {
    if (!selectedGroup) {
      closePanel();
      return;
    }
    const g = selectedGroup;
    const rawR2rml = r2rmlRawByClass.get(g.classEntity.subject) ?? null;
    const mapping = r2rmlMappingByClass.get(g.classEntity.subject) ?? null;
    const groundingMatch = classMatchResolver(g.classEntity.fullSubject);
    const pendingOverride = groundingMatch
      ? pendingGroundingOverrides?.[conceptMatchKey(groundingMatch)]
      : undefined;

    openPanel(
      g.label,
      <ClassDetailPanel
        key={g.classEntity.body}
        group={g}
        r2rml={rawR2rml}
        r2rmlMapping={mapping}
        fullTurtle={fullTurtle}
        prelude={prelude}
        isReadOnly={isReadOnly}
        groundingMatch={groundingMatch}
        pendingGroundingOverride={pendingOverride}
        onSelectGroundingCandidate={onSelectGroundingCandidate}
        onMarkGroundingNovel={onMarkGroundingNovel}
        onSave={(updatedTurtle) => {
          onUpdateEntityTurtle?.(g.classEntity, updatedTurtle);
        }}
        onSaveProperty={(propEntity, updatedBody) => {
          onUpdateEntityTurtle?.(propEntity, updatedBody);
        }}
        onUpdateTurtle={onUpdateTurtle}
      />,
    );
  }, [
    selectedGroup,
    r2rmlRawByClass,
    r2rmlMappingByClass,
    classMatchResolver,
    fullTurtle,
    prelude,
    pendingGroundingOverrides,
    onSelectGroundingCandidate,
    onMarkGroundingNovel,
    openPanel,
    closePanel,
    isReadOnly,
    onUpdateEntityTurtle,
    onUpdateTurtle,
  ]);

  // Close panel on unmount
  useEffect(() => () => closePanel(), [closePanel]);

  return (
    <SpaceBetween size="m">
      <ExpandableSection
        variant="container"
        defaultExpanded
        headerText="Proposed classes"
        headerCounter={`(${groups.length} classes)`}
        headerDescription="Select a class to view its relationships, attributes, and mappings."
        headerActions={
          !isReadOnly ? (
            <Button
              variant="inline-link"
              onClick={() => {
                if (selectedGroup) {
                  const idx = entities.indexOf(selectedGroup.classEntity);
                  onEditEntity(selectedGroup.classEntity, idx);
                }
              }}
              disabled={!selectedGroup}
            >
              Edit selected
            </Button>
          ) : undefined
        }
      >
        <SortableTable
          items={groups}
          trackBy={(g) => g.classEntity.fullSubject}
          defaultSortingColumnId="name"
          selectionType="single"
          selectedItems={selectedGroup ? [selectedGroup] : []}
          onSelectionChange={({ detail }) =>
            setSelectedClassUri(
              detail.selectedItems[0]?.classEntity.fullSubject ?? null,
            )
          }
          onRowClick={({ detail }) =>
            setSelectedClassUri(
              detail.item.classEntity.fullSubject === selectedClassUri
                ? null
                : detail.item.classEntity.fullSubject,
            )
          }
          columnDefinitions={[
            {
              id: "name",
              header: "Class",
              isRowHeader: true,
              sortingField: "label",
              cell: (g) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <Box fontWeight="bold">{g.label}</Box>
                  {g.hasSuggestedParent && (
                    <Badge color="blue">Suggested subclass</Badge>
                  )}
                </SpaceBetween>
              ),
            },
            {
              id: "origin",
              header: (
                <SpaceBetween
                  direction="horizontal"
                  size="xxs"
                  alignItems="center"
                >
                  <span>Origin</span>
                  <InfoPopover header="Origin" ariaLabel="About class origin">
                    <SpaceBetween size="xs">
                      <Box variant="p">
                        <strong>Grounded</strong>: matched to a foundational
                        ontology, reusing standard vocabulary.
                      </Box>
                      <Box variant="p">
                        <strong>Ambiguous</strong>: low-confidence match, review
                        before accepting.
                      </Box>
                      <Box variant="p">
                        <strong>Novel</strong>: created fresh, no confident
                        match.
                      </Box>
                      <Box variant="p">
                        Open a class and see its Grounding tab to change the
                        match.
                      </Box>
                    </SpaceBetween>
                  </InfoPopover>
                </SpaceBetween>
              ),
              // Grounded sorts before Ambiguous, Ambiguous before Novel.
              sortingComparator: (a, b) => {
                const aScore =
                  a.matchType === "exact" || a.matchType === "close"
                    ? 0
                    : a.matchType === "related"
                      ? 1
                      : 2;
                const bScore =
                  b.matchType === "exact" || b.matchType === "close"
                    ? 0
                    : b.matchType === "related"
                      ? 1
                      : 2;
                return aScore - bScore;
              },
              cell: (g) =>
                g.matchType === "exact" || g.matchType === "close" ? (
                  <Badge color="blue">Grounded</Badge>
                ) : g.matchType === "related" ? (
                  <Badge>Ambiguous</Badge>
                ) : (
                  <Badge color="green">Novel</Badge>
                ),
            },
            {
              id: "relationships",
              header: "Relationships",
              sortingComparator: (a, b) =>
                a.relationships.length - b.relationships.length,
              cell: (g) => g.relationships.length,
            },
            {
              id: "attributes",
              header: "Attributes",
              sortingComparator: (a, b) =>
                a.attributes.length - b.attributes.length,
              cell: (g) => g.attributes.length,
            },
            {
              // Sibling/near-duplicate proposal classes: skos:exactMatch/
              // closeMatch to OTHER classes in this proposal (surfaced in the
              // card's "Related classes" sub-section). Populated for unstructured
              // / RIGOR inductions (the class normalizer's per-run dedup emits
              // these); 0 for the structured table_to_ontology path, which does
              // not emit intra-proposal matches. Distinct from the card's
              // "Grounding" tab, which counts EXTERNAL foundational candidates.
              id: "matches",
              header: "Matches",
              sortingComparator: (a, b) =>
                (a.matches?.length ?? 0) - (b.matches?.length ?? 0),
              cell: (g) => g.matches?.length ?? 0,
            },
          ]}
          empty={
            <Box textAlign="center" color="text-body-secondary">
              No classes in this proposal.
            </Box>
          }
        />
      </ExpandableSection>
      {prelude && (
        <ExpandableSection
          variant="container"
          defaultExpanded={false}
          headerText="Prefixes (shared)"
        >
          <TurtleHighlight>{prelude}</TurtleHighlight>
        </ExpandableSection>
      )}

      {ungrouped.length > 0 && (
        <ExpandableSection
          variant="container"
          defaultExpanded={false}
          headerText="Ungrouped properties"
          headerCounter={`(${ungrouped.length})`}
          headerDescription="Properties whose domain class is not in this proposal."
        >
          <SortableTable
            items={ungrouped}
            trackBy="fullSubject"
            variant="embedded"
            defaultSortingColumnId="name"
            columnDefinitions={[
              {
                id: "name",
                header: "Property",
                isRowHeader: true,
                sortingComparator: (a, b) =>
                  (
                    extractQuoted(a.body, "rdfs:label") ?? a.subject
                  ).localeCompare(
                    extractQuoted(b.body, "rdfs:label") ?? b.subject,
                  ),
                cell: (p) => extractQuoted(p.body, "rdfs:label") ?? p.subject,
              },
              {
                id: "kind",
                header: "Type",
                sortingComparator: (a, b) =>
                  (a.kind.includes("Object") ? 0 : 1) -
                  (b.kind.includes("Object") ? 0 : 1),
                cell: (p) =>
                  p.kind.includes("Object") ? (
                    <Badge color="green">Relationship</Badge>
                  ) : (
                    <Badge color="grey">Attribute</Badge>
                  ),
              },
              {
                id: "domain",
                header: "Domain",
                sortingComparator: (a, b) =>
                  (extractUri(a.body, "rdfs:domain") ?? "—").localeCompare(
                    extractUri(b.body, "rdfs:domain") ?? "—",
                  ),
                cell: (p) => extractUri(p.body, "rdfs:domain") ?? "—",
              },
              {
                id: "range",
                header: "Range",
                sortingComparator: (a, b) =>
                  (extractUri(a.body, "rdfs:range") ?? "—").localeCompare(
                    extractUri(b.body, "rdfs:range") ?? "—",
                  ),
                cell: (p) => extractUri(p.body, "rdfs:range") ?? "—",
              },
            ]}
          />
        </ExpandableSection>
      )}
    </SpaceBetween>
  );
}
