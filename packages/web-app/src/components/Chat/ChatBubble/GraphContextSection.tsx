// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Graph context section showing entity relationships from the knowledge graph.
 */
import React from "react";
import { Box } from "@cloudscape-design/components";
import type {
  GraphContextItem,
  LegacyGraphContextRelationship,
} from "@app-types";

export interface GraphContextSectionProps {
  context: GraphContextItem;
}

interface RenderedRelationship {
  sourceUri: string;
  predicate: string;
  targetUri: string;
  targetLabel?: string;
}

const MAX_RENDERED_STATEMENTS = 20;

const graphSupportingStatements = (
  properties: Record<string, unknown> | undefined,
): string[] => {
  const raw =
    properties?.supportingStatements ?? properties?.supporting_statements;
  if (Array.isArray(raw)) {
    return raw
      .filter(
        (statement): statement is string =>
          typeof statement === "string" && statement.trim().length > 0,
      )
      .slice(0, MAX_RENDERED_STATEMENTS);
  }
  if (typeof raw !== "string" || raw.trim().length === 0) return [];

  try {
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return parsed
        .filter(
          (statement): statement is string =>
            typeof statement === "string" && statement.trim().length > 0,
        )
        .slice(0, MAX_RENDERED_STATEMENTS);
    }
  } catch {
    // Restored legacy sessions may store one plain-text statement.
  }
  return [raw];
};

const legacyRelationship = (
  entityUri: string,
  relationship: LegacyGraphContextRelationship,
): RenderedRelationship | null => {
  const sourceUri =
    relationship.sourceUri ?? relationship.source_uri ?? entityUri;
  const predicate =
    relationship.predicateLabel ??
    relationship.predicate_label ??
    relationship.predicateUri ??
    relationship.predicate_uri ??
    relationship.predicate;
  const targetUri =
    relationship.targetUri ?? relationship.target_uri ?? relationship.target;
  if (!sourceUri || !predicate || !targetUri) return null;
  return {
    sourceUri,
    predicate,
    targetUri,
    targetLabel: relationship.targetLabel ?? relationship.target_label,
  };
};

export const GraphContextSection: React.FC<GraphContextSectionProps> = ({
  context,
}) => {
  const entities = context.entities ?? [];
  const relationships: RenderedRelationship[] = [
    ...(context.relationships ?? []).map((relationship) => ({
      sourceUri: relationship.sourceUri,
      predicate: relationship.predicateLabel ?? relationship.predicateUri,
      targetUri: relationship.targetUri,
    })),
    ...entities.flatMap((entity) =>
      (entity.relationships ?? [])
        .map((relationship) => legacyRelationship(entity.uri, relationship))
        .filter(
          (relationship): relationship is RenderedRelationship =>
            relationship !== null,
        ),
    ),
  ];
  if (entities.length === 0 && relationships.length === 0) return null;

  const entityLabels = new Map(
    entities.map((entity) => [entity.uri, entity.label || entity.uri]),
  );

  return (
    <div style={{ borderTop: "1px solid #e9ebed", paddingTop: "8px" }}>
      <Box variant="span" fontSize="body-s" fontWeight="bold">
        Graph context
      </Box>
      {entities.length > 0 && (
        <div style={{ marginTop: "4px" }}>
          {entities.map((entity) => {
            const statements = graphSupportingStatements(entity.properties);
            return (
              <React.Fragment key={entity.uri}>
                <Box variant="span" fontSize="body-s" display="block">
                  {entity.label || entity.uri}
                  {entity.type && (
                    <Box variant="span" color="text-body-secondary">
                      {" "}
                      ({entity.type})
                    </Box>
                  )}
                </Box>
                {statements.map((statement, index) => (
                  <Box
                    key={`${entity.uri}|statement|${index}`}
                    variant="span"
                    fontSize="body-s"
                    color="text-body-secondary"
                    display="block"
                  >
                    ↳ {statement}
                  </Box>
                ))}
              </React.Fragment>
            );
          })}
        </div>
      )}
      {relationships.length > 0 && (
        <div style={{ marginTop: "4px" }}>
          {relationships.map((relationship, index) => (
            <Box
              key={`${relationship.sourceUri}|${relationship.predicate}|${relationship.targetUri}|${index}`}
              variant="span"
              fontSize="body-s"
              display="block"
            >
              {entityLabels.get(relationship.sourceUri) ??
                relationship.sourceUri}{" "}
              → {relationship.predicate} →{" "}
              {entityLabels.get(relationship.targetUri) ??
                relationship.targetLabel ??
                relationship.targetUri}
            </Box>
          ))}
        </div>
      )}
    </div>
  );
};
