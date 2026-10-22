// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * InductedSourcesView — the data sources that have fed an induction in this
 * namespace. Lives in the Explorer ("what exists") rather than on the Induction
 * page ("start/review a run"), since it is a read-only inventory.
 */
import { useEffect, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { SortableTable } from "@components/SortableTable";
import type { ApiClient } from "@components/ApiClientProvider";
import {
  listInducedDatasources,
  type InducedDatasource,
} from "../../../services/ontology-engine";
import { useAppI18n } from "../../../i18n/LocaleProvider";

export function InductedSourcesView({
  apiClient,
  namespace,
}: {
  apiClient: ApiClient;
  namespace?: string;
}) {
  const { t } = useAppI18n();
  const [items, setItems] = useState<InducedDatasource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    if (!namespace) return;
    setLoading(true);
    setError(null);
    listInducedDatasources(apiClient, namespace)
      .then(setItems)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [apiClient, namespace, refreshKey]);

  return (
    <SpaceBetween size="s">
      {error && (
        <Alert
          type="warning"
          dismissible
          onDismiss={() => setError(null)}
          header={t("Could not load inducted sources")}
        >
          {error}
        </Alert>
      )}
      <SortableTable
        loading={loading}
        items={items}
        trackBy="datasourceId"
        defaultSortingColumnId="id"
        columnDefinitions={[
          {
            id: "id",
            header: t("Datasource"),
            sortingComparator: (a, b) =>
              (a.name || a.datasourceId).localeCompare(
                b.name || b.datasourceId,
              ),
            cell: (e) => e.name || e.datasourceId,
          },
          {
            id: "label",
            header: t("Label"),
            sortingField: "label",
            cell: (e) => e.label,
          },
          {
            id: "tables",
            header: t("Tables"),
            // Sort numerically; UNSTRUCTURED rows (rendered "N/A") sort as -1 so
            // they group together below the numeric table counts.
            sortingComparator: (a, b) => {
              const av =
                a.source_type === "UNSTRUCTURED" ? -1 : a.tables_processed;
              const bv =
                b.source_type === "UNSTRUCTURED" ? -1 : b.tables_processed;
              return av - bv;
            },
            cell: (e) =>
              e.source_type === "UNSTRUCTURED" ? t("N/A") : e.tables_processed,
          },
          {
            id: "classes",
            header: t("Classes"),
            sortingComparator: (a, b) =>
              a.novel_classes_created - b.novel_classes_created,
            cell: (e) =>
              e.source_type === "UNSTRUCTURED"
                ? e.novel_classes_created
                : t("{count} (novel)", {
                    count: e.novel_classes_created,
                  }),
          },
        ]}
        empty={
          <Box textAlign="center" color="text-status-inactive" padding="l">
            {t(
              "No inducted sources yet. Run an induction to see the sources it used.",
            )}
          </Box>
        }
        header={
          <Header
            counter={`(${items.length})`}
            description={t(
              "Data sources that have fed an induction in this namespace.",
            )}
            actions={
              <Button
                iconName="refresh"
                loading={loading}
                onClick={() => setRefreshKey((k) => k + 1)}
              >
                {t("Refresh")}
              </Button>
            }
          >
            {t("Inducted sources")}
          </Header>
        }
      />
    </SpaceBetween>
  );
}
