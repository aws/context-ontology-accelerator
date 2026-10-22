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
import { useAppI18n } from "../i18n/LocaleProvider";

export const MetricDetail: React.FC = () => {
  const { t } = useAppI18n();
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
    { text: t("Ontology") },
    {
      text: t("Metrics"),
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
              content: t("Metric “{name}” deleted.", { name: metricName }),
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
    return (
      <Alert type="error">
        {t("Failed to load metric: {message}", { message: error.message })}
      </Alert>
    );
  }

  if (!metric) {
    return <Alert type="warning">{t("Metric not found.")}</Alert>;
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
                  {t("Edit")}
                </Button>
                <Button
                  variant="normal"
                  onClick={() => setDeleteModalVisible(true)}
                >
                  {t("Delete")}
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
            <Box variant="awsui-key-label">{t("Description")}</Box>
            <div>{metric.description}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">{t("Unit")}</Box>
            <div>{metric.unit ?? "—"}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">{t("Return type")}</Box>
            <div>{metric.returnType ?? "—"}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">{t("Defined by")}</Box>
            <div>{metric.definedBy ?? "—"}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">{t("Effective from")}</Box>
            <div>{metric.effectiveFrom ?? "—"}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">{t("Default time grain")}</Box>
            <div>{metric.defaultTimeGrain ?? "—"}</div>
          </div>
        </ColumnLayout>
      </Container>

      {/* Source */}
      <Container header={<Header variant="h2">{t("Source")}</Header>}>
        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">{t("Data source")}</Box>
            <div>
              {metric.dataSourceId
                ? (sourceNameById.get(metric.dataSourceId) ??
                  metric.dataSourceId)
                : "—"}
            </div>
          </div>
          <div>
            <Box variant="awsui-key-label">{t("Source table")}</Box>
            <div>{metric.sourceTable}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">{t("Time grain")}</Box>
            <div>{metric.defaultTimeGrain ?? "—"}</div>
          </div>
        </ColumnLayout>
      </Container>

      {/* Expressions */}
      <Container header={<Header variant="h2">{t("Expressions")}</Header>}>
        <Table
          items={metric.expression?.dialects ?? []}
          columnDefinitions={[
            {
              id: "dialect",
              header: t("Dialect"),
              cell: (item) => <Badge>{item.dialect}</Badge>,
              width: 150,
            },
            {
              id: "expression",
              header: t("SQL Expression"),
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
        <Container header={<Header variant="h2">{t("AI Context")}</Header>}>
          <SpaceBetween size="m">
            {metric.aiContext.synonyms &&
              metric.aiContext.synonyms.length > 0 && (
                <div>
                  <Box variant="awsui-key-label">{t("Synonyms")}</Box>
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
                <Box variant="awsui-key-label">{t("Instructions")}</Box>
                <div>{metric.aiContext.instructions}</div>
              </div>
            )}
            {metric.aiContext.examples &&
              metric.aiContext.examples.length > 0 && (
                <div>
                  <Box variant="awsui-key-label">{t("Examples")}</Box>
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
      <Container header={<Header variant="h2">{t("Ontology Links")}</Header>}>
        {metric.ontologyConcepts && metric.ontologyConcepts.length > 0 ? (
          <SpaceBetween direction="horizontal" size="xs">
            {metric.ontologyConcepts.map((c) => (
              <Badge key={c}>{c}</Badge>
            ))}
          </SpaceBetween>
        ) : (
          <Box color="text-status-inactive">
            {t(
              "No ontology classes linked. Edit this metric to associate it with classes from your ontology.",
            )}
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
        header={t("Delete metric")}
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
                {t("Cancel")}
              </Button>
              <Button
                variant="primary"
                onClick={handleDelete}
                loading={deleteMutation.isPending}
                disabled={deleteConfirmText !== metricName}
              >
                {t("Delete")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            {t("Permanently delete metric {name}?", {
              name: metricName ?? "",
            })}{" "}
            {t(
              "This action cannot be undone. The metric will be removed from the ontology graph and will no longer be discoverable.",
            )}
          </Box>
          <FormField
            label={t("Type “{name}” to confirm", { name: metricName ?? "" })}
          >
            <Input
              value={deleteConfirmText}
              onChange={({ detail }) => setDeleteConfirmText(detail.value)}
              placeholder={metricName}
            />
          </FormField>
          {deleteMutation.isError && (
            <Alert type="error">
              {deleteMutation.error?.message ?? t("Delete failed")}
            </Alert>
          )}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};
