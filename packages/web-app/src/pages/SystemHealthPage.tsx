// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * System Health page — displays backend service health status.
 *
 * Route: /system-health
 *
 * Calls GET /system-health (proxied to ontology-engine /readiness) and
 * renders the status of each backend component with visual indicators.
 */
import React, { useCallback, useEffect, useState } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import type { StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";

import { useApiClient } from "@components";
import { useAppI18n, type AppTranslate } from "../i18n/LocaleProvider";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";

interface OntologyInductionHealth {
  graphStore?: string;
  vectorStore?: string;
  bedrockEmbeddings?: string;
  descriptionLlm?: string;
}

interface SystemHealthResponse {
  status: string;
  ontologyInduction?: OntologyInductionHealth;
  dynamodb?: string;
  neptuneKg?: string;
  opensearch?: string;
}

type ComponentStatus = "ok" | "degraded" | "error" | "loading" | string;

function statusType(status: ComponentStatus): StatusIndicatorProps.Type {
  if (status === "ok") return "success";
  if (status === "loading") return "loading";
  if (status === "degraded") return "warning";
  return "error";
}

function statusLabel(status: ComponentStatus, t: AppTranslate): string {
  if (status === "ok") return t("Healthy");
  if (status === "loading") return t("Checking...");
  if (status === "degraded") return t("Degraded");
  if (status.startsWith("error:")) return status.replace("error: ", "");
  return status || t("Unknown");
}

interface HealthCardProps {
  title: string;
  description: string;
  status: ComponentStatus;
}

function HealthCard({ title, description, status }: HealthCardProps) {
  const { t } = useAppI18n();
  return (
    <Container>
      <SpaceBetween size="xs">
        <Box variant="h3">{title}</Box>
        <Box variant="small" color="text-body-secondary">
          {description}
        </Box>
        <StatusIndicator type={statusType(status)}>
          {statusLabel(status, t)}
        </StatusIndicator>
      </SpaceBetween>
    </Container>
  );
}

export function SystemHealthPage() {
  const { locale, t } = useAppI18n();
  useBreadcrumbTrail([
    { text: t("Administration") },
    { text: t("System Health") },
  ]);
  const apiClient = useApiClient();
  const [health, setHealth] = useState<SystemHealthResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);

  const fetchHealth = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiClient.get<SystemHealthResponse>("/system-health");
      setHealth(data);
      setLastChecked(new Date());
    } catch (e) {
      setError(
        e instanceof Error ? e.message : t("Failed to fetch system health"),
      );
      setHealth(null);
    } finally {
      setLoading(false);
    }
  }, [apiClient, t]);

  useEffect(() => {
    fetchHealth();
  }, [fetchHealth]);

  const overallStatus: ComponentStatus = loading
    ? "loading"
    : error
      ? "error"
      : (health?.status ?? "error");

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={t(
          "Real-time health status of all backend storage and AI services",
        )}
        actions={
          <Button iconName="refresh" onClick={fetchHealth} loading={loading}>
            {t("Refresh")}
          </Button>
        }
      >
        {t("System Health")}
      </Header>

      <Container
        header={
          <Header
            variant="h2"
            description={
              lastChecked
                ? t("Last checked: {time}", {
                    time: lastChecked.toLocaleTimeString(locale),
                  })
                : undefined
            }
          >
            {t("Overall Status")}
          </Header>
        }
      >
        <StatusIndicator type={statusType(overallStatus)}>
          {overallStatus === "ok"
            ? t("All systems operational")
            : overallStatus === "loading"
              ? t("Checking backend health...")
              : error
                ? t("Unreachable: {error}", { error })
                : t("One or more systems degraded")}
        </StatusIndicator>
      </Container>

      <Header variant="h2">{t("Storage Backends")}</Header>

      <ColumnLayout columns={3}>
        <HealthCard
          title="DynamoDB"
          description={t("Sources and metadata table")}
          status={loading ? "loading" : (health?.dynamodb ?? "unknown")}
        />
        <HealthCard
          title="Neptune KG"
          description={t("GraphRAG knowledge graph (Gremlin/openCypher)")}
          status={loading ? "loading" : (health?.neptuneKg ?? "unknown")}
        />
        <HealthCard
          title="OpenSearch Serverless"
          description={t("Vector embeddings collection (AOSS)")}
          status={loading ? "loading" : (health?.opensearch ?? "unknown")}
        />
      </ColumnLayout>

      <Header variant="h2">{t("Ontology Induction")}</Header>

      <ColumnLayout columns={2}>
        <HealthCard
          title={t("Ontology Graph Store")}
          description={t("Neptune DB — RDF/OWL ontology triples")}
          status={
            loading
              ? "loading"
              : (health?.ontologyInduction?.graphStore ?? "unknown")
          }
        />
        <HealthCard
          title={t("Ontology Vector Store")}
          description={t(
            "OpenSearch — ontology embeddings and semantic search",
          )}
          status={
            loading
              ? "loading"
              : (health?.ontologyInduction?.vectorStore ?? "unknown")
          }
        />
        <HealthCard
          title={t("Bedrock Embeddings")}
          description={t(
            "Amazon Bedrock — text embedding model for vector generation",
          )}
          status={
            loading
              ? "loading"
              : (health?.ontologyInduction?.bedrockEmbeddings ?? "unknown")
          }
        />
        <HealthCard
          title={t("Description LLM")}
          description={t(
            "Amazon Bedrock Converse — LLM for entity descriptions and induction",
          )}
          status={
            loading
              ? "loading"
              : (health?.ontologyInduction?.descriptionLlm ?? "unknown")
          }
        />
      </ColumnLayout>
    </SpaceBetween>
  );
}
