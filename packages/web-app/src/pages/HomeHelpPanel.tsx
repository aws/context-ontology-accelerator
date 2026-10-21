// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import HelpPanel from "@cloudscape-design/components/help-panel";
import Box from "@cloudscape-design/components/box";
import Icon from "@cloudscape-design/components/icon";
import { useAppI18n } from "../i18n/LocaleProvider";
import type { AppMessage } from "../i18n/appMessages";

interface Step {
  title: AppMessage;
  description: AppMessage;
  status: "active" | "pending" | "complete";
}

const STEPS: Step[] = [
  {
    title: "Choose your domain",
    description:
      "Pick the business area you want to model first and what you intend to use it for. You can always expand scope later.",
    status: "active",
  },
  {
    title: "Connect a data source",
    description:
      "Point Context Ontology Accelerator at a Glue catalog, database, or upload a file. It scans your schema and generates a draft set of concepts automatically.",
    status: "pending",
  },
  {
    title: "Curate induced concepts",
    description:
      "Review what Context Ontology Accelerator found — accept, reject, merge, or refine proposed concepts before they go live. Work through a few at a time.",
    status: "pending",
  },
  {
    title: "Test with a question",
    description:
      "Ask a natural-language question and see how your ontology grounds the answer. This is your proof of value — even a few concepts make a difference.",
    status: "complete",
  },
];

const StepIndicator: React.FC<{ status: Step["status"] }> = ({ status }) => {
  if (status === "complete") {
    return <Icon name="status-positive" variant="success" />;
  }
  return (
    <div
      style={{
        width: 10,
        height: 10,
        borderRadius: "50%",
        border: "2px solid var(--color-border-control-default, #7d8998)",
      }}
    />
  );
};

export const HomeHelpPanel: React.FC = () => {
  const { t } = useAppI18n();
  return (
    <HelpPanel header={<h2>{t("Get started")}</h2>}>
      <p>{t("Set up your governed context layer in a few steps.")}</p>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "20px 1fr",
          gap: "0 12px",
          marginTop: 16,
        }}
      >
        {STEPS.map((step, i) => {
          const isLast = i === STEPS.length - 1;
          const dimmed = step.status === "pending";
          return (
            <React.Fragment key={step.title}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  height: 20,
                  opacity: dimmed ? 0.45 : undefined,
                }}
              >
                <StepIndicator status={step.status} />
              </div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  height: 20,
                  opacity: dimmed ? 0.45 : undefined,
                }}
              >
                <Box fontWeight="bold">{t(step.title)}</Box>
              </div>

              {!isLast && (
                <div
                  style={{
                    display: "flex",
                    justifyContent: "center",
                    opacity: dimmed ? 0.45 : undefined,
                  }}
                >
                  <div
                    style={{
                      width: 2,
                      height: "100%",
                      backgroundColor:
                        "var(--color-border-divider-default, #e9ebed)",
                    }}
                  />
                </div>
              )}
              {isLast && <div />}

              <div
                style={{
                  padding: isLast ? "4px 0 0" : "4px 0 20px",
                  opacity: dimmed ? 0.45 : undefined,
                }}
              >
                <Box variant="small" color="text-body-secondary">
                  {t(step.description)}
                </Box>
              </div>
            </React.Fragment>
          );
        })}
      </div>
    </HelpPanel>
  );
};
