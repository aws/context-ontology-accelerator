// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useNamespace } from "@api-hooks/use-namespace";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import Box from "@cloudscape-design/components/box";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Table from "@cloudscape-design/components/table";
import Badge from "@cloudscape-design/components/badge";
import Alert from "@cloudscape-design/components/alert";
import Modal from "@cloudscape-design/components/modal";
import Input from "@cloudscape-design/components/input";
import FormField from "@cloudscape-design/components/form-field";
import Spinner from "@cloudscape-design/components/spinner";
import { useGetMetric, useDeleteMetric } from "../api-hooks/use-metrics";
import { useSourceNameById } from "../api-hooks/use-source-name-by-id";

export const MetricDetail: React.FC = () => {
  const navigate = useNavigate();
  const { namespaceId, metricName } = useParams<{
    namespaceId: string;
    metricName: string;
  }>();

  const { data, isLoading, error } = useGetMetric(
    namespaceId ?? "",
    metricName ?? "",
  );
  const deleteMutation = useDeleteMetric(namespaceId ?? "");

  const { name: nsName } = useNamespace(namespaceId);
  useBreadcrumbTrail([
    { text: nsName, href: `/namespaces/${namespaceId}`, namespaceId },
    { text: "Ontology" },
    {
      text: "Metrics",
      href: `/namespaces/${namespaceId}/metrics`,
      namespaceId,
    },
    { text: metricName ?? "" },
  ]);

  // Resolve dataSourceId -> human-readable source name
  const sourceNameById = useSourceNameById(namespaceId ?? "");

  const [deleteModalVisible, setDeleteModalVisible] = useState(false);
  const [deleteConfirmText, setDeleteConfirmText] = useState("");

  const metric = data?.metric;

  const handleDelete = () => {
    if (!metricName) return;
    deleteMutation.mutate(metricName, {
      onSuccess: () => {
        navigate(`/namespaces/${namespaceId}/metrics`, {
          state: {
            flash: {
              type: "success",
              content: `Metric "${metricName}" deleted.`,
            },
          },
        });
      },
    });
  };

  if (isLoading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (error) {
    return <Alert type="error">Failed to load metric: {error.message}</Alert>;
  }

  if (!metric) {
    return <Alert type="warning">Metric not found.</Alert>;
  }

  return (
    <SpaceBetween size="l">
      {/* Overview */}
      <Container
        header={
          <Header
            variant="h1"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  onClick={() =>
                    navigate(
                      `/namespaces/${namespaceId}/metrics/${metricName}/edit`,
                    )
                  }
                >
                  Edit
                </Button>
                <Button
                  variant="normal"
                  onClick={() => setDeleteModalVisible(true)}
                >
                  Delete
                </Button>
              </SpaceBetween>
            }
          >
            {metric.name}
          </Header>
        }
      >
        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Description</Box>
            <div>{metric.description}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Unit</Box>
            <div>{metric.unit ?? "—"}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Return type</Box>
            <div>{metric.returnType ?? "—"}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Defined by</Box>
            <div>{metric.definedBy ?? "—"}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Effective from</Box>
            <div>{metric.effectiveFrom ?? "—"}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Default time grain</Box>
            <div>{metric.defaultTimeGrain ?? "—"}</div>
          </div>
        </ColumnLayout>
      </Container>

      {/* Source */}
      <Container header={<Header variant="h2">Source</Header>}>
        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Data source</Box>
            <div>
              {metric.dataSourceId
                ? (sourceNameById.get(metric.dataSourceId) ??
                  metric.dataSourceId)
                : "—"}
            </div>
          </div>
          <div>
            <Box variant="awsui-key-label">Source table</Box>
            <div>{metric.sourceTable}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Time grain</Box>
            <div>{metric.defaultTimeGrain ?? "—"}</div>
          </div>
        </ColumnLayout>
      </Container>

      {/* Expressions */}
      <Container header={<Header variant="h2">Expressions</Header>}>
        <Table
          items={metric.expression?.dialects ?? []}
          columnDefinitions={[
            {
              id: "dialect",
              header: "Dialect",
              cell: (item) => <Badge>{item.dialect}</Badge>,
              width: 150,
            },
            {
              id: "expression",
              header: "SQL Expression",
              cell: (item) => (
                <code
                  style={{
                    fontFamily: "monospace",
                    whiteSpace: "pre-wrap",
                  }}
                >
                  {item.expression}
                </code>
              ),
            },
          ]}
          variant="embedded"
        />
      </Container>

      {/* AI Context */}
      {metric.aiContext && (
        <Container header={<Header variant="h2">AI Context</Header>}>
          <SpaceBetween size="m">
            {metric.aiContext.synonyms &&
              metric.aiContext.synonyms.length > 0 && (
                <div>
                  <Box variant="awsui-key-label">Synonyms</Box>
                  <SpaceBetween direction="horizontal" size="xs">
                    {metric.aiContext.synonyms.map((s) => (
                      <Badge key={s} color="blue">
                        {s}
                      </Badge>
                    ))}
                  </SpaceBetween>
                </div>
              )}
            {metric.aiContext.instructions && (
              <div>
                <Box variant="awsui-key-label">Instructions</Box>
                <div>{metric.aiContext.instructions}</div>
              </div>
            )}
            {metric.aiContext.examples &&
              metric.aiContext.examples.length > 0 && (
                <div>
                  <Box variant="awsui-key-label">Examples</Box>
                  <ul>
                    {metric.aiContext.examples.map((e, i) => (
                      <li key={i}>{e}</li>
                    ))}
                  </ul>
                </div>
              )}
          </SpaceBetween>
        </Container>
      )}

      {/* Ontology Links */}
      <Container header={<Header variant="h2">Ontology Links</Header>}>
        {metric.ontologyConcepts && metric.ontologyConcepts.length > 0 ? (
          <SpaceBetween direction="horizontal" size="xs">
            {metric.ontologyConcepts.map((c) => (
              <Badge key={c}>{c}</Badge>
            ))}
          </SpaceBetween>
        ) : (
          <Box color="text-status-inactive">
            No ontology classes linked. Edit this metric to associate it with
            classes from your ontology.
          </Box>
        )}
      </Container>

      {/* Delete Modal */}
      <Modal
        visible={deleteModalVisible}
        onDismiss={() => {
          setDeleteModalVisible(false);
          setDeleteConfirmText("");
          deleteMutation.reset();
        }}
        header="Delete metric"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setDeleteModalVisible(false);
                  setDeleteConfirmText("");
                }}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleDelete}
                loading={deleteMutation.isPending}
                disabled={deleteConfirmText !== metricName}
              >
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            Permanently delete metric <b>{metricName}</b>? This action cannot be
            undone. The metric will be removed from the ontology graph and will
            no longer be discoverable.
          </Box>
          <FormField label={`Type "${metricName}" to confirm`}>
            <Input
              value={deleteConfirmText}
              onChange={({ detail }) => setDeleteConfirmText(detail.value)}
              placeholder={metricName}
            />
          </FormField>
          {deleteMutation.isError && (
            <Alert type="error">
              {deleteMutation.error?.message ?? "Delete failed"}
            </Alert>
          )}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};
