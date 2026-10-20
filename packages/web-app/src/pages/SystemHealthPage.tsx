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

function statusLabel(status: ComponentStatus): string {
  if (status === "ok") return "Healthy";
  if (status === "loading") return "Checking...";
  if (status === "degraded") return "Degraded";
  if (status.startsWith("error:")) return status.replace("error: ", "");
  return status || "Unknown";
}

interface HealthCardProps {
  title: string;
  description: string;
  status: ComponentStatus;
}

function HealthCard({ title, description, status }: HealthCardProps) {
  return (
    <Container>
      <SpaceBetween size="xs">
        <Box variant="h3">{title}</Box>
        <Box variant="small" color="text-body-secondary">
          {description}
        </Box>
        <StatusIndicator type={statusType(status)}>
          {statusLabel(status)}
        </StatusIndicator>
      </SpaceBetween>
    </Container>
  );
}

export function SystemHealthPage() {
  useBreadcrumbTrail([{ text: "Administration" }, { text: "System Health" }]);
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
        e instanceof Error ? e.message : "Failed to fetch system health",
      );
      setHealth(null);
    } finally {
      setLoading(false);
    }
  }, [apiClient]);

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
        description="Real-time health status of all backend storage and AI services"
        actions={
          <Button iconName="refresh" onClick={fetchHealth} loading={loading}>
            Refresh
          </Button>
        }
      >
        System Health
      </Header>

      <Container
        header={
          <Header
            variant="h2"
            description={
              lastChecked
                ? `Last checked: ${lastChecked.toLocaleTimeString()}`
                : undefined
            }
          >
            Overall Status
          </Header>
        }
      >
        <StatusIndicator type={statusType(overallStatus)}>
          {overallStatus === "ok"
            ? "All systems operational"
            : overallStatus === "loading"
              ? "Checking backend health..."
              : error
                ? `Unreachable: ${error}`
                : "One or more systems degraded"}
        </StatusIndicator>
      </Container>

      <Header variant="h2">Storage Backends</Header>

      <ColumnLayout columns={3}>
        <HealthCard
          title="DynamoDB"
          description="Sources and metadata table"
          status={loading ? "loading" : (health?.dynamodb ?? "unknown")}
        />
        <HealthCard
          title="Neptune KG"
          description="GraphRAG knowledge graph (Gremlin/openCypher)"
          status={loading ? "loading" : (health?.neptuneKg ?? "unknown")}
        />
        <HealthCard
          title="OpenSearch Serverless"
          description="Vector embeddings collection (AOSS)"
          status={loading ? "loading" : (health?.opensearch ?? "unknown")}
        />
      </ColumnLayout>

      <Header variant="h2">Ontology Induction</Header>

      <ColumnLayout columns={2}>
        <HealthCard
          title="Ontology Graph Store"
          description="Neptune DB — RDF/OWL ontology triples"
          status={
            loading
              ? "loading"
              : (health?.ontologyInduction?.graphStore ?? "unknown")
          }
        />
        <HealthCard
          title="Ontology Vector Store"
          description="OpenSearch — ontology embeddings and semantic search"
          status={
            loading
              ? "loading"
              : (health?.ontologyInduction?.vectorStore ?? "unknown")
          }
        />
        <HealthCard
          title="Bedrock Embeddings"
          description="Amazon Bedrock — text embedding model for vector generation"
          status={
            loading
              ? "loading"
              : (health?.ontologyInduction?.bedrockEmbeddings ?? "unknown")
          }
        />
        <HealthCard
          title="Description LLM"
          description="Amazon Bedrock Converse — LLM for entity descriptions and induction"
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
