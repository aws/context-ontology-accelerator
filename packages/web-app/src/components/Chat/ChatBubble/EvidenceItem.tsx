// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Single evidence source item with expandable text preview.
 */
import React, { useState } from "react";
import { Badge, Box } from "@cloudscape-design/components";
import type { SupportingContentItem } from "@app-types";
import { useAppI18n } from "../../../i18n/LocaleProvider";

export interface EvidenceItemProps {
  item: SupportingContentItem;
}

export const EvidenceItem: React.FC<EvidenceItemProps> = ({ item }) => {
  const { t } = useAppI18n();
  const [expanded, setExpanded] = useState(false);
  const label =
    [
      item.sourceDocumentName,
      item.sourceDoc,
      item.label,
      item.sourceDocumentId,
    ].find((value) => typeof value === "string" && value.trim()) ??
    t("Unknown source");
  const text = item.text ?? "";
  const preview = text.length > 200 ? `${text.slice(0, 200)}…` : text;

  return (
    <div
      style={{
        borderLeft: "3px solid #0073bb",
        paddingLeft: "12px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
        <Box variant="span" fontSize="body-s" fontWeight="bold">
          {label}
        </Box>
        {item.relevanceScore !== undefined && (
          <Badge color="blue">{item.relevanceScore.toFixed(2)}</Badge>
        )}
      </div>
      {text && (
        <div style={{ marginTop: "4px" }}>
          <Box variant="span" fontSize="body-s" color="text-body-secondary">
            {expanded ? text : preview}
          </Box>
          {text.length > 200 && (
            <button
              onClick={() => setExpanded(!expanded)}
              style={{
                background: "none",
                border: "none",
                color: "#0073bb",
                cursor: "pointer",
                fontSize: "12px",
                padding: "0 4px",
              }}
              aria-label={expanded ? t("Show less") : t("Show more")}
            >
              {expanded ? t("Show less") : t("Show more")}
            </button>
          )}
        </div>
      )}
    </div>
  );
};
