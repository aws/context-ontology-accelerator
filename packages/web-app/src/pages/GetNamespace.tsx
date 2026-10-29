// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Namespace detail page — displays summary info plus Data Sources, Ontologies,
 * and Metrics for a single namespace. Permissions (roles + members) live on a
 * dedicated page at /namespaces/:namespaceId/permissions.
 *
 * Route: /namespaces/:namespaceId
 */
import React, { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import Popover from "@cloudscape-design/components/popover";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import Textarea from "@cloudscape-design/components/textarea";
import type { TableProps } from "@cloudscape-design/components/table";
import { useGetNamespace } from "@api-hooks/use-get-namespace";
import { useUpdateNamespace } from "@api-hooks/use-update-namespace";
import { useListSources } from "@api-hooks/use-list-sources";
import { useListMetrics } from "@api-hooks/use-metrics";
import type { MetricDefinition } from "@api-hooks/use-metrics";
import type { SourceSummary } from "@coa/control-plane-client";
import {
  NamespaceStatus,
  SourceStatus,
  SourceType,
  VkgHealthStatus,
} from "@coa/control-plane-client";
import { useApiClient } from "@components/ApiClientProvider";
import { localizeNamespaceLanguage } from "@utils";
import {
  localizeNamespaceStatus,
  localizeVkgHealth,
} from "@utils/namespace-status";
import { localizeOntologyType } from "@utils/ontology-display";
import { listOntologies } from "../services/ontology-engine";
import type { OntologyRecord } from "../services/ontology-engine";
import { useAppI18n } from "../i18n/LocaleProvider";
import type { AppMessage } from "../i18n/appMessages";

/** Formats an ISO timestamp into a localized short date+time string, or "—" if absent. */
function formatDate(iso: string | undefined, locale: string): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(locale, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

/**
 * Human-readable meaning of each VKG health status, surfaced as a tooltip on
 * the namespace detail page. Mirrors the VkgHealthStatus enum docs in
 * models/src/main/smithy/namespace.smithy.
 */
const VKG_HEALTH_DESCRIPTIONS: Record<VkgHealthStatus, AppMessage> = {
  [VkgHealthStatus.HEALTHY]:
    "The VKG translation service is running and able to translate queries.",
  [VkgHealthStatus.DEGRADED]:
    "A VKG task is running but its health check is failing, so it cannot translate queries (for example, the published mapping could not be loaded). Queries will fail — check the container's health status.",
  [VkgHealthStatus.UNAVAILABLE]:
    "The VKG service exists but is not serving — queries will fail.",
  [VkgHealthStatus.PROVISIONING]:
    "The VKG service is starting up (deployment in progress) and is expected to become healthy shortly.",
  [VkgHealthStatus.UNKNOWN]:
    "No VKG service is deployed yet for this namespace, or its state could not be determined.",
};

export const GetNamespace: React.FC = () => {
  const navigate = useNavigate();
  const { locale, t } = useAppI18n();
  const { namespaceId } = useParams<{ namespaceId: string }>();
  const { data, isLoading, error } = useGetNamespace(namespaceId ?? "");
  const [editVisible, setEditVisible] = useState(false);

  const nsName =
    data?.namespace?.displayName ?? data?.namespace?.name ?? namespaceId ?? "";
  useBreadcrumbTrail([
    { text: t("Namespaces"), href: "/namespaces" },
    { text: nsName },
    ...(editVisible ? [{ text: t("Edit") }] : []),
  ]);

  if (isLoading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (error) {
    return (
      <Box textAlign="center" padding="xxl">
        <StatusIndicator type="error">
          {t("Failed to load namespace: {message}", {
            message: error.message,
          })}
        </StatusIndicator>
      </Box>
    );
  }

  const ns = data?.namespace;
  if (!ns)
    return (
      <Box textAlign="center" padding="xxl">
        <StatusIndicator type="warning">
          {t("Namespace not found")}
        </StatusIndicator>
      </Box>
    );

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => setEditVisible(true)}>{t("Edit")}</Button>
            <Button
              iconName="lock-private"
              onClick={() => navigate(`/namespaces/${namespaceId}/permissions`)}
            >
              {t("Permissions")}
            </Button>
          </SpaceBetween>
        }
      >
        {ns.displayName ?? ns.name}
      </Header>

      <Container header={<Header variant="h2">{t("Summary")}</Header>}>
        <KeyValuePairs
          columns={3}
          items={[
            { label: t("Namespace ID"), value: ns.namespaceId },
            { label: t("Name"), value: ns.name },
            { label: t("Owner"), value: ns.owner },
            {
              label: t("Language"),
              info: (
                <Popover
                  dismissButton={false}
                  position="top"
                  triggerType="text"
                  header={t("Namespace language")}
                  content={t(
                    "Set when the namespace was created and cannot be changed.",
                  )}
                >
                  {t("Info")}
                </Popover>
              ),
              value: localizeNamespaceLanguage(ns.language, t),
            },
            {
              label: t("Status"),
              value: (
                <StatusIndicator
                  type={
                    ns.status === NamespaceStatus.ACTIVE
                      ? "success"
                      : ns.status === NamespaceStatus.DELETE_FAILED ||
                          ns.status === NamespaceStatus.DELETED
                        ? "error"
                        : ns.status === NamespaceStatus.DELETING
                          ? "in-progress"
                          : "stopped"
                  }
                >
                  {localizeNamespaceStatus(ns.status, t)}
                </StatusIndicator>
              ),
            },
            {
              label: t("VKG health"),
              value: (() => {
                const vkgHealth = ns.vkgHealth ?? VkgHealthStatus.UNKNOWN;
                // Prefer the server-provided reason (the live cause) when
                // present; fall back to the static per-status description.
                const vkgHealthContent =
                  ns.vkgHealthReason ?? t(VKG_HEALTH_DESCRIPTIONS[vkgHealth]);
                return (
                  <Popover
                    dismissButton={false}
                    position="top"
                    triggerType="text"
                    header={t("VKG health")}
                    content={vkgHealthContent}
                  >
                    <StatusIndicator
                      type={
                        vkgHealth === VkgHealthStatus.HEALTHY
                          ? "success"
                          : vkgHealth === VkgHealthStatus.UNAVAILABLE ||
                              vkgHealth === VkgHealthStatus.DEGRADED
                            ? "error"
                            : vkgHealth === VkgHealthStatus.PROVISIONING
                              ? "in-progress"
                              : "stopped"
                      }
                    >
                      {localizeVkgHealth(vkgHealth, t)}
                    </StatusIndicator>
                  </Popover>
                );
              })(),
            },
            {
              label: t("Sources"),
              value: String(ns.sourceCount ?? 0),
            },
            { label: t("Created"), value: formatDate(ns.createdAt, locale) },
            {
              label: t("Updated"),
              value: formatDate(ns.updatedAt, locale),
            },
            {
              label: t("Description"),
              value: ns.description ?? "—",
            },
          ]}
        />
      </Container>

      <NamespaceTabs namespaceId={namespaceId ?? ""} />

      <EditNamespaceModal
        namespaceId={namespaceId ?? ""}
        displayName={ns.displayName ?? ""}
        description={ns.description ?? ""}
        visible={editVisible}
        onDismiss={() => setEditVisible(false)}
      />
    </SpaceBetween>
  );
};

// ── Tabs ─────────────────────────────────────────────────────────────

const NamespaceTabs: React.FC<{ namespaceId: string }> = ({ namespaceId }) => {
  const { t } = useAppI18n();
  return (
    <Tabs
      tabs={[
        {
          id: "sources",
          label: t("Sources"),
          content: <SourcesTab namespaceId={namespaceId} />,
        },
        {
          id: "ontologies",
          label: t("Ontologies"),
          content: <OntologiesTab namespaceId={namespaceId} />,
        },
        {
          id: "metrics",
          label: t("Metrics"),
          content: <MetricsTab namespaceId={namespaceId} />,
        },
      ]}
    />
  );
};

// ── Sources tab ──────────────────────────────────────────────────────

const SourcesTab: React.FC<{ namespaceId: string }> = ({ namespaceId }) => {
  const navigate = useNavigate();
  const { t } = useAppI18n();
  const { data, isLoading, error, refetch, isFetching } =
    useListSources(namespaceId);

  const sources: SourceSummary[] = useMemo(
    () => data?.pages.flatMap((p) => p.items ?? []) ?? [],
    [data],
  );

  const columns: TableProps.ColumnDefinition<SourceSummary>[] = [
    {
      id: "name",
      header: t("Source name"),
      isRowHeader: true,
      cell: (item) => (
        <Link
          href={`/namespaces/${namespaceId}/sources/${item.sourceId}`}
          onFollow={(e) => {
            e.preventDefault();
            navigate(`/namespaces/${namespaceId}/sources/${item.sourceId}`);
          }}
        >
          {item.name}
        </Link>
      ),
    },
    {
      id: "type",
      header: t("Type"),
      cell: (item) =>
        item.sourceType === SourceType.DATABASE
          ? t("Database")
          : t("Documents"),
    },
    {
      id: "status",
      header: t("Status"),
      cell: (item) => (
        <StatusIndicator
          type={item.status === SourceStatus.APPROVED ? "success" : "info"}
        >
          {item.status}
        </StatusIndicator>
      ),
    },
    {
      id: "tables",
      header: t("Tables"),
      cell: (item) =>
        item.sourceType === SourceType.DATABASE
          ? (item.tablesDiscovered ?? 0)
          : "—",
    },
  ];

  return (
    <Table<SourceSummary>
      variant="container"
      items={sources}
      trackBy="sourceId"
      loading={isLoading}
      loadingText={t("Loading sources…")}
      columnDefinitions={columns}
      header={
        <Header
          variant="h2"
          counter={`(${sources.length})`}
          actions={
            <Button
              iconName="refresh"
              loading={isFetching}
              onClick={() => refetch()}
            >
              {t("Refresh")}
            </Button>
          }
        >
          {t("Sources")}
        </Header>
      }
      empty={
        error ? (
          <StatusIndicator type="error">
            {t("Failed to load sources: {message}", {
              message: error.message,
            })}
          </StatusIndicator>
        ) : (
          <Box textAlign="center" color="text-body-secondary">
            {t("No sources in this namespace.")}
          </Box>
        )
      }
    />
  );
};

// ── Ontologies tab ───────────────────────────────────────────────────

const OntologiesTab: React.FC<{ namespaceId: string }> = ({ namespaceId }) => {
  const navigate = useNavigate();
  const { locale, t } = useAppI18n();
  const apiClient = useApiClient();
  const [items, setItems] = useState<OntologyRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchOntologies = React.useCallback(() => {
    if (!namespaceId) return;
    setLoading(true);
    setError(null);
    listOntologies(apiClient, namespaceId)
      .then((res) => {
        setItems(res);
      })
      .catch((e: unknown) => {
        setError(
          e instanceof Error ? e.message : t("Failed to load ontologies"),
        );
      })
      .finally(() => {
        setLoading(false);
      });
  }, [apiClient, namespaceId, t]);

  useEffect(() => {
    fetchOntologies();
  }, [fetchOntologies]);

  const columns: TableProps.ColumnDefinition<OntologyRecord>[] = [
    {
      id: "title",
      header: t("Ontology"),
      isRowHeader: true,
      cell: (o) => (
        <Link
          href={`/namespaces/${namespaceId}/ontology`}
          onFollow={(e) => {
            e.preventDefault();
            navigate(`/namespaces/${namespaceId}/ontology`);
          }}
        >
          {o.title || o.ontologyId}
        </Link>
      ),
    },
    {
      id: "type",
      header: t("Type"),
      cell: (o) => localizeOntologyType(o.ontologyType, t),
    },
    { id: "classes", header: t("Classes"), cell: (o) => o.classCount },
    {
      id: "properties",
      header: t("Properties"),
      cell: (o) => o.propertyCount,
    },
    {
      id: "created",
      header: t("Created"),
      cell: (o) => formatDate(o.createdAt, locale),
    },
  ];

  return (
    <Table<OntologyRecord>
      variant="container"
      items={items}
      trackBy="ontologyId"
      loading={loading}
      loadingText={t("Loading ontologies…")}
      columnDefinitions={columns}
      header={
        <Header
          variant="h2"
          counter={`(${items.length})`}
          actions={
            <Button
              iconName="refresh"
              loading={loading}
              onClick={() => fetchOntologies()}
            >
              {t("Refresh")}
            </Button>
          }
        >
          {t("Ontologies")}
        </Header>
      }
      empty={
        error ? (
          <StatusIndicator type="error">
            {t("Failed to load ontologies: {message}", { message: error })}
          </StatusIndicator>
        ) : (
          <Box textAlign="center" color="text-body-secondary">
            {t("No ontologies in this namespace yet.")}
          </Box>
        )
      }
    />
  );
};

// ── Metrics tab ──────────────────────────────────────────────────────

const MetricsTab: React.FC<{ namespaceId: string }> = ({ namespaceId }) => {
  const navigate = useNavigate();
  const { t } = useAppI18n();
  const { data, isLoading, error, refetch, isFetching } =
    useListMetrics(namespaceId);

  const metrics: MetricDefinition[] = useMemo(
    () => data?.pages.flatMap((p) => p.metrics ?? []) ?? [],
    [data],
  );

  const columns: TableProps.ColumnDefinition<MetricDefinition>[] = [
    {
      id: "name",
      header: t("Metric"),
      isRowHeader: true,
      cell: (m) => (
        <Link
          href={`/namespaces/${namespaceId}/metrics/${encodeURIComponent(m.name ?? "")}`}
          onFollow={(e) => {
            e.preventDefault();
            navigate(
              `/namespaces/${namespaceId}/metrics/${encodeURIComponent(m.name ?? "")}`,
            );
          }}
        >
          {m.name}
        </Link>
      ),
    },
    {
      id: "description",
      header: t("Description"),
      cell: (m) => m.description || "—",
    },
    {
      id: "dataSourceId",
      header: t("Data source"),
      cell: (m) => m.dataSourceId,
    },
    { id: "sourceTable", header: t("Table"), cell: (m) => m.sourceTable },
  ];

  return (
    <Table<MetricDefinition>
      variant="container"
      items={metrics}
      trackBy="name"
      loading={isLoading}
      loadingText={t("Loading metrics…")}
      columnDefinitions={columns}
      header={
        <Header
          variant="h2"
          counter={`(${metrics.length})`}
          actions={
            <Button
              iconName="refresh"
              loading={isFetching}
              onClick={() => refetch()}
            >
              {t("Refresh")}
            </Button>
          }
        >
          {t("Metrics")}
        </Header>
      }
      empty={
        error ? (
          <StatusIndicator type="error">
            {t("Failed to load metrics: {message}", {
              message: error.message,
            })}
          </StatusIndicator>
        ) : (
          <Box textAlign="center" color="text-body-secondary">
            {t("No metrics in this namespace yet.")}
          </Box>
        )
      }
    />
  );
};

// ── Edit Namespace Modal ─────────────────────────────────────────────

interface EditNamespaceModalProps {
  namespaceId: string;
  displayName: string;
  description: string;
  visible: boolean;
  onDismiss: () => void;
}

const EditNamespaceModal: React.FC<EditNamespaceModalProps> = ({
  namespaceId,
  displayName: initialDisplayName,
  description: initialDescription,
  visible,
  onDismiss,
}) => {
  const { t } = useAppI18n();
  const [displayName, setDisplayName] = useState(initialDisplayName);
  const [description, setDescription] = useState(initialDescription);
  const updateNamespace = useUpdateNamespace();
  const [error, setError] = useState<string | null>(null);

  // Reset form when modal opens
  React.useEffect(() => {
    if (visible) {
      setDisplayName(initialDisplayName);
      setDescription(initialDescription);
      setError(null);
    }
  }, [visible, initialDisplayName, initialDescription]);

  const handleSubmit = () => {
    setError(null);
    updateNamespace.mutate(
      { namespaceId, displayName, description },
      {
        onSuccess: () => onDismiss(),
        onError: (e) => setError(e.message ?? t("Failed to update namespace")),
      },
    );
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={t("Edit namespace")}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              {t("Cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={handleSubmit}
              loading={updateNamespace.isPending}
            >
              {t("Save")}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {error && <StatusIndicator type="error">{error}</StatusIndicator>}
        <FormField label={t("Display name")}>
          <Input
            value={displayName}
            onChange={({ detail }) => setDisplayName(detail.value)}
          />
        </FormField>
        <FormField label={t("Description")}>
          <Textarea
            value={description}
            onChange={({ detail }) => setDescription(detail.value)}
            rows={4}
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};
