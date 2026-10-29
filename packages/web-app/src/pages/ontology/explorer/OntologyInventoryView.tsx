// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * OntologyInventoryView — the Explorer's "what ontologies exist here" table.
 *
 * Covers every registered ontology in the namespace (induced, foundational, and
 * uploaded) with a type filter, so the Explorer is the single inventory of the
 * namespace's graph. Read-only: loading/uploading and grounding-input curation
 * stay on the Induction page, which owns those actions.
 */
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { SortableTable } from "@components/SortableTable";
import type { ApiClient } from "@components/ApiClientProvider";
import { formatTimestamp } from "@utils/helpers";
import {
  listOntologies,
  type ListedOntology,
} from "../../../services/ontology-engine";
import {
  localizeOntologyType,
  ontologyTypeDisplay,
  ontologyTypeGroup,
} from "@utils/ontology-display";
import { useAppI18n } from "../../../i18n/LocaleProvider";

type TypeFilter = "all" | "induced" | "foundational" | "uploaded";

export function OntologyInventoryView({
  apiClient,
  namespace,
}: {
  apiClient: ApiClient;
  namespace?: string;
}) {
  const { t } = useAppI18n();
  const navigate = useNavigate();
  const [items, setItems] = useState<ListedOntology[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");

  useEffect(() => {
    if (!namespace) return;
    setLoading(true);
    setError(null);
    listOntologies(apiClient, namespace)
      .then(setItems)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [apiClient, namespace, refreshKey]);

  // Poll while a row is mid-delete so "Delete in progress" clears itself instead
  // of looking hung. Same 4s cadence as the Induction page's poll.
  //
  // A row whose teardown failed keeps `status: "deleting"` and gains a
  // `deleteError` (see _mark_delete_error in the catalog router), so polling on
  // status alone never terminates. Wedged rows are excluded: the row is
  // finished changing, and the error is surfaced in the status cell instead.
  const hasDeleting = items.some(
    (o) => o.status === "deleting" && !o.deleteError,
  );
  useEffect(() => {
    if (!namespace || !hasDeleting) return;
    const id = window.setInterval(() => setRefreshKey((k) => k + 1), 4000);
    return () => window.clearInterval(id);
  }, [namespace, hasDeleting]);

  const counts = useMemo(() => {
    const c = { induced: 0, foundational: 0, uploaded: 0 };
    for (const o of items) {
      const g = ontologyTypeGroup(o.ontologyType);
      if (g === "induced" || g === "foundational" || g === "uploaded")
        c[g] += 1;
    }
    return c;
  }, [items]);

  const visible = useMemo(
    () =>
      typeFilter === "all"
        ? items
        : items.filter((o) => ontologyTypeGroup(o.ontologyType) === typeFilter),
    [items, typeFilter],
  );

  return (
    <SpaceBetween size="s">
      {error && (
        <Alert
          type="warning"
          dismissible
          onDismiss={() => setError(null)}
          header={t("Could not load ontologies")}
        >
          {error}
        </Alert>
      )}
      <SortableTable
        loading={loading}
        items={visible}
        trackBy="ontologyId"
        defaultSortingColumnId="title"
        columnDefinitions={[
          {
            id: "title",
            header: t("Ontology"),
            isRowHeader: true,
            sortingComparator: (a, b) =>
              (a.title || a.ontologyId || "").localeCompare(
                b.title || b.ontologyId || "",
              ),
            cell: (o) => {
              // Only induced ontologies have a detail page, and not while their
              // graph is being torn down.
              const href =
                ontologyTypeGroup(o.ontologyType) === "induced" &&
                o.status !== "deleting"
                  ? `/namespaces/${namespace}/ontology/induced?ontology_id=${encodeURIComponent(
                      o.ontologyId,
                    )}`
                  : null;
              return (
                <SpaceBetween size="xxxs">
                  {href ? (
                    <Link
                      href={href}
                      onFollow={(ev) => {
                        ev.preventDefault();
                        navigate(href);
                      }}
                    >
                      {o.title || o.ontologyId}
                    </Link>
                  ) : (
                    <Box>{o.title || o.ontologyId}</Box>
                  )}
                  <Box variant="small" color="text-status-inactive">
                    {o.uri}
                  </Box>
                </SpaceBetween>
              );
            },
          },
          {
            id: "type",
            header: t("Type"),
            sortingComparator: (a, b) =>
              ontologyTypeGroup(a.ontologyType).localeCompare(
                ontologyTypeGroup(b.ontologyType),
              ),
            cell: (o) => {
              const { color } = ontologyTypeDisplay(o.ontologyType);
              if (o.status !== "deleting")
                return (
                  <Badge color={color}>
                    {localizeOntologyType(o.ontologyType, t)}
                  </Badge>
                );
              // A wedged teardown stops the poll, so say so here rather than
              // showing "in progress" forever. Retry = re-issue the delete.
              return o.deleteError ? (
                <StatusIndicator type="error">
                  {t("Delete failed")}
                </StatusIndicator>
              ) : (
                <StatusIndicator type="in-progress">
                  {t("Delete in progress")}
                </StatusIndicator>
              );
            },
          },
          {
            id: "classes",
            header: t("Classes"),
            sortingComparator: (a, b) =>
              (a.classCount ?? 0) - (b.classCount ?? 0),
            cell: (o) => o.classCount ?? 0,
          },
          {
            id: "properties",
            header: t("Properties"),
            sortingComparator: (a, b) =>
              (a.propertyCount ?? 0) - (b.propertyCount ?? 0),
            cell: (o) => o.propertyCount ?? 0,
          },
          {
            id: "axioms",
            header: t("Axioms"),
            sortingComparator: (a, b) =>
              (a.axiomCount ?? 0) - (b.axiomCount ?? 0),
            cell: (o) => o.axiomCount ?? 0,
          },
          {
            id: "created",
            header: t("Created"),
            sortingComparator: (a, b) =>
              (a.createdAt ?? "").localeCompare(b.createdAt ?? ""),
            cell: (o) => formatTimestamp(o.createdAt),
          },
        ]}
        empty={
          <Box textAlign="center" color="text-status-inactive" padding="l">
            {typeFilter === "all"
              ? t(
                  "No ontologies yet. Run an induction, or load a reference ontology from the Induction page.",
                )
              : t("No ontologies of this type.")}
          </Box>
        }
        header={
          <Header
            counter={`(${visible.length})`}
            description={t(
              "Every ontology registered in this namespace. Open an induced ontology to browse its classes and relationships.",
            )}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <SegmentedControl
                  selectedId={typeFilter}
                  onChange={({ detail }) => {
                    const id = detail.selectedId;
                    if (
                      id === "all" ||
                      id === "induced" ||
                      id === "foundational" ||
                      id === "uploaded"
                    ) {
                      setTypeFilter(id);
                    }
                  }}
                  label={t("Filter by ontology type")}
                  options={[
                    {
                      id: "all",
                      text: t("All ({count})", { count: items.length }),
                    },
                    {
                      id: "induced",
                      text: t("Induced ({count})", {
                        count: counts.induced,
                      }),
                    },
                    {
                      id: "foundational",
                      text: t("Foundational ({count})", {
                        count: counts.foundational,
                      }),
                    },
                    {
                      id: "uploaded",
                      text: t("Uploaded ({count})", {
                        count: counts.uploaded,
                      }),
                    },
                  ]}
                />
                <Button
                  iconName="refresh"
                  loading={loading}
                  onClick={() => setRefreshKey((k) => k + 1)}
                >
                  {t("Refresh")}
                </Button>
              </SpaceBetween>
            }
          >
            {t("Ontologies")}
          </Header>
        }
      />
    </SpaceBetween>
  );
}
