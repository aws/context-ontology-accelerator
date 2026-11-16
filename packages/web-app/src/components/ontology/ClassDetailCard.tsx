// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  Badge,
  Box,
  Button,
  Container,
  ExpandableSection,
  Header,
  Link,
  SpaceBetween,
} from "@cloudscape-design/components";
import { useMemo } from "react";
import {
  HAS_KEY_LIST_PRED,
  OWL_DATATYPE_PROPERTY,
  OWL_OBJECT_PROPERTY,
  RDF_TYPE,
  RDFS_DOMAIN,
  RDFS_LABEL,
  RDFS_RANGE,
  RDFS_SUBCLASS_OF,
  SCL_DATASOURCE_ID,
  SCL_FK_PROVENANCE,
  SCL_SOURCE_SCHEMA,
  SCL_SUBCLASS_PROVENANCE,
  SCL_SUGGESTED_SUBCLASS_OF,
  literalText,
  localName,
  parseTriples,
} from "../../utils/turtleLexer";
import { SuggestionPanel } from "./SuggestionPanel";
import { useAppI18n } from "../../i18n/LocaleProvider";

type ParsedOntology = {
  labels: Record<string, string>;
  parents: Record<string, string[]>;
  childrenOf: Record<string, string[]>;
  // Child classes carrying a suggestedSubClassOf annotation to the parent.
  suggestedParents: Record<string, string[]>;
  objectProps: {
    iri: string;
    domain: string;
    range: string;
    fkProvenance?: string;
  }[];
  datatypeProps: {
    iri: string;
    domain: string;
    range: string;
  }[];
  // Per-class COA annotations emitted by the inducer.
  datasourceId: Record<string, string>;
  sourceSchema: Record<string, string>;
  subClassProvenance: Record<string, string>;
  // owl:hasKey list members, in declaration order, as full property IRIs.
  pkColumns: Record<string, string[]>;
};

function parseOntology(turtle: string): ParsedOntology {
  const { triples } = parseTriples(turtle);
  const labels: Record<string, string> = {};
  const parents: Record<string, string[]> = {};
  const childrenOf: Record<string, string[]> = {};
  const suggestedParents: Record<string, string[]> = {};
  const propType: Record<string, "object" | "datatype"> = {};
  const propDomain: Record<string, string> = {};
  const propRange: Record<string, string> = {};
  const datasourceId: Record<string, string> = {};
  const sourceSchema: Record<string, string> = {};
  const fkProvenance: Record<string, string> = {};
  const subClassProvenance: Record<string, string> = {};
  const pkColumns: Record<string, string[]> = {};

  for (const { s, p, o } of triples) {
    if (p === RDF_TYPE && o === OWL_OBJECT_PROPERTY) propType[s] = "object";
    else if (p === RDF_TYPE && o === OWL_DATATYPE_PROPERTY)
      propType[s] = "datatype";
    else if (p === RDFS_SUBCLASS_OF) {
      parents[s] = parents[s] ? [...parents[s], o] : [o];
      childrenOf[o] = childrenOf[o] ? [...childrenOf[o], s] : [s];
    } else if (p === SCL_SUGGESTED_SUBCLASS_OF) {
      suggestedParents[s] = suggestedParents[s]
        ? [...suggestedParents[s], o]
        : [o];
    } else if (p === RDFS_DOMAIN) propDomain[s] = o;
    else if (p === RDFS_RANGE) propRange[s] = o;
    else if (p === RDFS_LABEL && o.startsWith('"')) labels[s] = literalText(o);
    else if (p === SCL_DATASOURCE_ID && o.startsWith('"'))
      datasourceId[s] = literalText(o);
    else if (p === SCL_SOURCE_SCHEMA && o.startsWith('"'))
      sourceSchema[s] = literalText(o);
    else if (p === SCL_FK_PROVENANCE && o.startsWith('"'))
      fkProvenance[s] = literalText(o);
    else if (p === SCL_SUBCLASS_PROVENANCE && o.startsWith('"'))
      subClassProvenance[s] = literalText(o);
    else if (p === HAS_KEY_LIST_PRED) {
      pkColumns[s] = pkColumns[s] ? [...pkColumns[s], o] : [o];
    }
  }

  const objectProps: ParsedOntology["objectProps"] = [];
  const datatypeProps: ParsedOntology["datatypeProps"] = [];
  for (const [prop, kind] of Object.entries(propType)) {
    const domain = propDomain[prop];
    const range = propRange[prop];
    if (!domain || !range) continue;
    if (kind === "object") {
      objectProps.push({
        iri: prop,
        domain,
        range,
        fkProvenance: fkProvenance[prop],
      });
    } else {
      datatypeProps.push({ iri: prop, domain, range });
    }
  }

  return {
    labels,
    parents,
    childrenOf,
    suggestedParents,
    objectProps,
    datatypeProps,
    datasourceId,
    sourceSchema,
    subClassProvenance,
    pkColumns,
  };
}

function ancestorChain(
  iri: string,
  parents: Record<string, string[]>,
  max = 10,
): string[] {
  const chain: string[] = [];
  const seen = new Set<string>([iri]);
  let cursor: string | undefined = parents[iri]?.[0];
  while (cursor && !seen.has(cursor) && chain.length < max) {
    chain.push(cursor);
    seen.add(cursor);
    cursor = parents[cursor]?.[0];
  }
  return chain;
}

type ClassLinkProps = {
  iri: string;
  label: string;
  onSelect: (iri: string) => void;
};

function ClassLink({ iri, label, onSelect }: ClassLinkProps) {
  return (
    <Link
      onFollow={(e) => {
        e.preventDefault();
        onSelect(iri);
      }}
      href={`#${iri}`}
    >
      {label}
    </Link>
  );
}

type Props = {
  iri: string;
  turtle: string;
  onSelectClass: (iri: string) => void;
  // Receives the rewritten Turtle when a suggestion is confirmed or rejected.
  // The suggestion panel is hidden when omitted (e.g. read-only contexts).
  onTurtleEdit?: (turtle: string) => void;
  /** When provided, renders a close (✕) button in the card header. */
  onClose?: () => void;
};

/** Render a green/amber pill for an FK / subClass provenance value. */
function ProvenanceBadge({ value }: { value: string }) {
  const { t } = useAppI18n();
  const ok = value === "DETERMINISTIC" || value === "STEWARD_SPECIFIED";
  const label =
    value === "DETERMINISTIC"
      ? t("Deterministic")
      : value === "AI_INFERRED"
        ? t("AI-inferred")
        : value === "STEWARD_SPECIFIED"
          ? t("Steward-specified")
          : value === "PK_SHARING"
            ? t("Auto-detected (PK-sharing)")
            : value;
  return <Badge color={ok ? "green" : "severity-medium"}>{label}</Badge>;
}

export function ClassDetailCard({
  iri,
  turtle,
  onSelectClass,
  onTurtleEdit,
  onClose,
}: Props) {
  const { t } = useAppI18n();
  const onto = useMemo(() => parseOntology(turtle), [turtle]);
  const labelOf = (i: string) => onto.labels[i] ?? localName(i);
  const directParents = onto.parents[iri] ?? [];
  const subclasses = onto.childrenOf[iri] ?? [];
  const hasSuggestions = (onto.suggestedParents[iri] ?? []).length > 0;
  const ancestors = useMemo(
    () => ancestorChain(iri, onto.parents),
    [iri, onto.parents],
  );

  const ownDataProps = onto.datatypeProps.filter((p) => p.domain === iri);
  const ownObjectProps = onto.objectProps.filter((p) => p.domain === iri);
  const ds = onto.datasourceId[iri];
  const sch = onto.sourceSchema[iri];
  const subProv = onto.subClassProvenance[iri];
  const pkCols = onto.pkColumns[iri]?.map(localName);

  return (
    <Container
      header={
        <Header
          variant="h2"
          actions={
            onClose && (
              <Button
                iconName="close"
                variant="icon"
                ariaLabel={t("Close class details")}
                onClick={onClose}
              />
            )
          }
        >
          {labelOf(iri)}
        </Header>
      }
    >
      <SpaceBetween size="m">
        <Box>
          <Box variant="awsui-key-label">IRI</Box>
          <Box variant="code">{iri}</Box>
        </Box>

        {(ds || sch) && (
          <Box>
            <Box variant="awsui-key-label">{t("Source")}</Box>
            <SpaceBetween direction="horizontal" size="xxs">
              {ds && <Badge>{ds}</Badge>}
              {sch && <Badge color="blue">{sch}</Badge>}
            </SpaceBetween>
          </Box>
        )}

        {pkCols && pkCols.length > 0 && (
          <Box>
            <Box variant="awsui-key-label">{t("Primary key")}</Box>
            <Box variant="small">{pkCols.join(", ")}</Box>
          </Box>
        )}

        {hasSuggestions && onTurtleEdit && (
          <SuggestionPanel
            iri={iri}
            turtle={turtle}
            onTurtleEdit={onTurtleEdit}
            labelOf={labelOf}
          />
        )}

        {directParents.length > 0 && (
          <Box>
            <Box variant="awsui-key-label">
              {t("Subclass of")}{" "}
              {subProv && <ProvenanceBadge value={subProv} />}
            </Box>
            <SpaceBetween size="xxs">
              {directParents.map((parentIri) => (
                <ClassLink
                  key={parentIri}
                  iri={parentIri}
                  label={labelOf(parentIri)}
                  onSelect={onSelectClass}
                />
              ))}
            </SpaceBetween>
          </Box>
        )}

        {subclasses.length > 0 && (
          <Box>
            <Box variant="awsui-key-label">{t("Subclasses")}</Box>
            <SpaceBetween size="xxs">
              {subclasses.map((childIri) => (
                <ClassLink
                  key={childIri}
                  iri={childIri}
                  label={labelOf(childIri)}
                  onSelect={onSelectClass}
                />
              ))}
            </SpaceBetween>
          </Box>
        )}

        {ownDataProps.length > 0 && (
          <Box>
            <Box variant="awsui-key-label">{t("Datatype properties")}</Box>
            <SpaceBetween size="xxs">
              {ownDataProps.map((p) => (
                <Box key={p.iri} variant="small">
                  <strong>{labelOf(p.iri)}</strong> · {localName(p.range)}
                </Box>
              ))}
            </SpaceBetween>
          </Box>
        )}

        {ownObjectProps.length > 0 && (
          <Box>
            <Box variant="awsui-key-label">{t("Object properties")}</Box>
            <SpaceBetween size="xxs">
              {ownObjectProps.map((p) => (
                <Box key={p.iri} variant="small">
                  <strong>{labelOf(p.iri)}</strong> →{" "}
                  <ClassLink
                    iri={p.range}
                    label={labelOf(p.range)}
                    onSelect={onSelectClass}
                  />{" "}
                  {p.fkProvenance && <ProvenanceBadge value={p.fkProvenance} />}
                </Box>
              ))}
            </SpaceBetween>
          </Box>
        )}

        {ancestors.map((ancestor) => {
          const inheritedData = onto.datatypeProps.filter(
            (p) => p.domain === ancestor,
          );
          const inheritedObj = onto.objectProps.filter(
            (p) => p.domain === ancestor,
          );
          if (inheritedData.length === 0 && inheritedObj.length === 0)
            return null;
          return (
            <ExpandableSection
              key={ancestor}
              headerText={t("Inherited from {name}", {
                name: labelOf(ancestor),
              })}
              variant="footer"
            >
              <SpaceBetween size="xxs">
                {inheritedData.map((p) => (
                  <Box key={p.iri} variant="small">
                    <strong>{labelOf(p.iri)}</strong> · {localName(p.range)}
                  </Box>
                ))}
                {inheritedObj.map((p) => (
                  <Box key={p.iri} variant="small">
                    <strong>{labelOf(p.iri)}</strong> →{" "}
                    <ClassLink
                      iri={p.range}
                      label={labelOf(p.range)}
                      onSelect={onSelectClass}
                    />{" "}
                    {p.fkProvenance && (
                      <ProvenanceBadge value={p.fkProvenance} />
                    )}
                  </Box>
                ))}
              </SpaceBetween>
            </ExpandableSection>
          );
        })}
      </SpaceBetween>
    </Container>
  );
}
