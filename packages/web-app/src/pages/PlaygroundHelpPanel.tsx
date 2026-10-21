// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import HelpPanel from "@cloudscape-design/components/help-panel";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Link from "@cloudscape-design/components/link";
import { useAppI18n } from "../i18n/LocaleProvider";

export const PlaygroundHelpPanel: React.FC = () => {
  const { t } = useAppI18n();
  return (
    <HelpPanel
      header={<h2>{t("Playground")}</h2>}
      footer={
        <>
          <h3>{t("Learn more")}</h3>
          <SpaceBetween size="xxs">
            <Link
              external
              href="https://github.com/aws/context-ontology-accelerator"
            >
              {t("Context Ontology Accelerator on GitHub")}
            </Link>
          </SpaceBetween>
        </>
      }
    >
      <p>
        {t(
          "Ask a business question in plain language. Context Ontology Accelerator routes through three tiers — cheapest first — and falls through gracefully when a tier can't answer.",
        )}
      </p>

      <h3>{t("Three-tier resolution")}</h3>
      <ul>
        <li>
          <strong>{t("Tier 1 — metric.")}</strong>{" "}
          {t(
            "Synonym table → pre-compiled SQL → Athena. Zero LLM calls. Deterministic; the number a CFO sees in a dashboard equals the number an agent returns over MCP.",
          )}
        </li>
        <li>
          <strong>{t("Tier 2 — ontology.")}</strong>{" "}
          {t(
            "NL → SPARQL (LLM) → VKG → SQL → Athena. Grounded in the formal data model, not table-name guessing.",
          )}
        </li>
        <li>
          <strong>{t("Tier 3 — knowledge.")}</strong>{" "}
          {t(
            "Parallel vector + graph retrieval → LLM synthesis. Always cites which documents informed the answer.",
          )}
        </li>
      </ul>

      <h3>{t("Boundary controls (cross-cutting)")}</h3>
      <ul>
        <li>
          {t(
            "Cedar authorizes each request on principal, action, and resource before any tier runs.",
          )}
        </li>
        <li>
          {t(
            "The SQL Firewall rewrites compiled SQL with Cedar-derived row filters.",
          )}
        </li>
        <li>{t("Bedrock Guardrails filter prompts and outputs.")}</li>
      </ul>

      <h3>{t("Demos to try")}</h3>
      <ul>
        <li>
          {t(
            "Demo 1. Ask “total claims by region” for deterministic Tier 1, then “claims count” to reach the same metric through a synonym.",
          )}
        </li>
        <li>
          {t(
            "Demo 2. Ask “show me all claims with amount greater than 10000” to see Tier 2 with compiled SPARQL and SQL.",
          )}
        </li>
        <li>
          {t(
            "Demo 2 fallback. Ask “list all customers in northeast region”; when the Athena table is absent, the system falls through to Tier 3 without hiding the failure.",
          )}
        </li>
        <li>
          {t(
            "Demo 3. Ask “what is the claims filing process” for a cited Tier 3 answer, then “explain underwriting guidelines” to see an honest low-confidence refusal.",
          )}
        </li>
        <li>
          {t(
            "Demo 4. Choose Re-run as Tier 3 on a Tier 1 answer and compare the structurally different result.",
          )}
        </li>
        <li>
          {t(
            "Demo 5. Submit an empty query or a namespace with special characters to see fail-fast validation in under 100 ms.",
          )}
        </li>
        <li>
          {t(
            "Demo 6. Copy the request-ID chip on any answer to follow it through the rationale drawer and CloudWatch.",
          )}
        </li>
      </ul>
    </HelpPanel>
  );
};
