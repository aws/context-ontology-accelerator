// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Evidence panel — shows cited sources and graph context for Tier 3 responses.
 */
import React from "react";
import { ExpandableSection, SpaceBetween } from "@cloudscape-design/components";
import type {
  GraphContextEntity,
  GraphContextItem,
  SupportingContentItem,
} from "@app-types";
import { normalizeGraphContext } from "@utils/graph-context";
import { EvidenceItem } from "./EvidenceItem";
import { GraphContextSection } from "./GraphContextSection";

export interface EvidencePanelProps {
  supportingContent?: SupportingContentItem[];
  graphContext?: GraphContextItem | GraphContextEntity[];
}

/**
 * Collapsible evidence panel showing source attribution with relevance scores.
 * Renders when document citations or graph evidence are present.
 */
export const EvidencePanel: React.FC<EvidencePanelProps> = ({
  supportingContent,
  graphContext,
}) => {
  const normalizedGraphContext = normalizeGraphContext(graphContext);
  const sorted = [...(supportingContent ?? [])].sort(
    (a, b) => (b.relevanceScore ?? 0) - (a.relevanceScore ?? 0),
  );
  const hasGraphContext = Boolean(
    normalizedGraphContext?.entities?.length ||
    normalizedGraphContext?.relationships?.length,
  );

  if (sorted.length === 0 && !hasGraphContext) return null;

  const headerText =
    sorted.length > 0
      ? `${sorted.length} source${sorted.length !== 1 ? "s" : ""} cited`
      : "Graph evidence";

  return (
    <ExpandableSection
      headerText={headerText}
      variant="footer"
      defaultExpanded={false}
    >
      <SpaceBetween size="s">
        {sorted.map((item, index) => (
          <EvidenceItem key={item.chunkId ?? `evidence-${index}`} item={item} />
        ))}
        {hasGraphContext && normalizedGraphContext && (
          <GraphContextSection context={normalizedGraphContext} />
        )}
      </SpaceBetween>
    </ExpandableSection>
  );
};
