// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useMemo, useState } from "react";
import { useNavigate, useParams, useLocation } from "react-router-dom";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useNamespace } from "@api-hooks/use-namespace";
import { useCollection } from "@cloudscape-design/collection-hooks";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import PropertyFilter from "@cloudscape-design/components/property-filter";
import type { PropertyFilterProps } from "@cloudscape-design/components/property-filter";
import Pagination from "@cloudscape-design/components/pagination";
import CollectionPreferences from "@cloudscape-design/components/collection-preferences";
import Alert from "@cloudscape-design/components/alert";
import Modal from "@cloudscape-design/components/modal";
import RadioGroup from "@cloudscape-design/components/radio-group";
import FileUpload from "@cloudscape-design/components/file-upload";
import FormField from "@cloudscape-design/components/form-field";
import Link from "@cloudscape-design/components/link";
import type { CollectionPreferencesProps } from "@cloudscape-design/components/collection-preferences";
import {
  useListMetrics,
  useImportOsiFile,
  useExportOsi,
  useGetImportJob,
  useBulkDeleteMetrics,
} from "../api-hooks/use-metrics";
import type { MetricDefinition } from "../api-hooks/use-metrics";
import { useSourceNameById } from "../api-hooks/use-source-name-by-id";
import { useAppI18n } from "../i18n/LocaleProvider";
import type { AppMessage } from "../i18n/appMessages";

type MetricWithDataSource = MetricDefinition & { dataSourceName: string };

const PAGE_SIZE_OPTIONS: Array<{ value: number; label: AppMessage }> = [
  { value: 20, label: "20 metrics" },
  { value: 50, label: "50 metrics" },
  { value: 100, label: "100 metrics" },
];

const COLUMNS: Array<{
  id: string;
  label: AppMessage;
  alwaysVisible?: boolean;
}> = [
  { id: "name", label: "Name", alwaysVisible: true },
  { id: "description", label: "Description" },
  { id: "dataSourceId", label: "Data source" },
  { id: "sourceTable", label: "Table" },
  { id: "defaultTimeGrain", label: "Time grain" },
  { id: "unit", label: "Unit" },
  { id: "dialects", label: "Dialects" },
  { id: "definedBy", label: "Defined by" },
];

export const MetricList: React.FC = () => {
  const { t } = useAppI18n();
  const { namespaceId } = useParams<{ namespaceId: string }>();
  const navigate = useNavigate();
  const location = useLocation();

  const { name: nsName } = useNamespace(namespaceId);
  useBreadcrumbTrail([
    { text: nsName, href: `/namespaces/${namespaceId}`, namespaceId },
    { text: t("Ontology") },
    { text: t("Metrics") },
  ]);

  // Resolve dataSourceId -> human-readable source name
  const sourceNameById = useSourceNameById(namespaceId ?? "");
  const [importModalVisible, setImportModalVisible] = useState(false);
  const [importFiles, setImportFiles] = useState<File[]>([]);
  const [queryErrorDismissed, setQueryErrorDismissed] = useState(false);
  const [importResult, setImportResult] = useState<{
    type: "success" | "error";
    message: string;
  } | null>(() => {
    const state = location.state as {
      flash?: { type: "success"; content: string };
    } | null;
    if (state?.flash) {
      window.history.replaceState({}, "");
      return { type: state.flash.type, message: state.flash.content };
    }
    return null;
  });

  const [preferences, setPreferences] =
    useState<CollectionPreferencesProps.Preferences>({
      pageSize: 20,
      wrapLines: false,
      stripedRows: true,
      contentDensity: "comfortable",
      contentDisplay: COLUMNS.map((c) => ({ id: c.id, visible: true })),
    });
  const pageSizeOptions = useMemo(
    () =>
      PAGE_SIZE_OPTIONS.map((option) => ({
        ...option,
        label: t(option.label),
      })),
    [t],
  );
  const filteringProperties = useMemo<PropertyFilterProps.FilteringProperty[]>(
    () => [
      {
        key: "name",
        propertyLabel: t("Name"),
        groupValuesLabel: t("Name values"),
        operators: ["=", "!=", ":", "!:"],
      },
      {
        key: "dataSourceName",
        propertyLabel: t("Data source"),
        groupValuesLabel: t("Data source values"),
        operators: ["=", "!=", ":", "!:"],
      },
      {
        key: "sourceTable",
        propertyLabel: t("Source table"),
        groupValuesLabel: t("Source table values"),
        operators: ["=", "!=", ":", "!:"],
      },
      {
        key: "definedBy",
        propertyLabel: t("Defined by"),
        groupValuesLabel: t("Defined by values"),
        operators: ["=", "!=", ":", "!:"],
      },
    ],
    [t],
  );

  const {
    data,
    isLoading,
    error,
    refetch,
    isFetching,
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
  } = useListMetrics(namespaceId ?? "");
  const importMutation = useImportOsiFile(namespaceId ?? "");
  const exportMutation = useExportOsi(namespaceId ?? "");
  const bulkDeleteMutation = useBulkDeleteMetrics(namespaceId ?? "");

  // Selection state for bulk operations
  const [selectedMetrics, setSelectedMetrics] = useState<
    MetricWithDataSource[]
  >([]);
  const [bulkDeleteModalVisible, setBulkDeleteModalVisible] = useState(false);
  const [deleteMode, setDeleteMode] = useState<"selected" | "filtered">(
    "selected",
  );

  // Export scope modal — mirrors the bulk-delete selected/filtered/all
  // pattern so users can export a subset instead of always the full namespace.
  const [exportModalVisible, setExportModalVisible] = useState(false);
  // Client-side download failures (blob/anchor errors) and empty-selection
  // guards for export — kept separate from the page-level `importResult`
  // banner and from `exportMutation.error`, both of which have their own
  // rendering; this covers errors that happen outside the mutation itself.
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [exportMode, setExportMode] = useState<"all" | "selected" | "filtered">(
    "all",
  );

  // Track async import job (persisted in localStorage to survive page reload)
  const storageKey = `import-job-${namespaceId}`;
  const [activeJobId, setActiveJobId] = useState<string | null>(() => {
    const stored = localStorage.getItem(storageKey);
    return stored || null;
  });
  const { data: importJob } = useGetImportJob(namespaceId ?? "", activeJobId);

  // Auto-clear localStorage when job reaches terminal state
  React.useEffect(() => {
    if (
      importJob &&
      (importJob.status === "COMPLETED" || importJob.status === "FAILED")
    ) {
      localStorage.removeItem(storageKey);
    }
  }, [importJob, storageKey]);

  // Clear localStorage when job completes or is dismissed
  const clearActiveJob = () => {
    setActiveJobId(null);
    localStorage.removeItem(storageKey);
  };

  // Persist jobId to localStorage when set
  const startJobPolling = (jobId: string) => {
    setActiveJobId(jobId);
    localStorage.setItem(storageKey, jobId);
  };

  // Flatten all pages into a single array
  const allMetricsRaw = useMemo(
    () => data?.pages.flatMap((p) => p.metrics ?? []) ?? [],
    [data],
  );

  // Derive a dataSourceName field for filtering by resolved name instead of UUID
  const allMetrics: MetricWithDataSource[] = useMemo(
    () =>
      allMetricsRaw.map((m) => ({
        ...m,
        dataSourceName: m.dataSourceId
          ? (sourceNameById.get(m.dataSourceId) ?? m.dataSourceId)
          : "",
      })),
    [allMetricsRaw, sourceNameById],
  );

  // Keep the selection in sync with the underlying data. Without this, a
  // selection made against a stale table (e.g. right before a bulk-delete's
  // query invalidation resolves) can retain now-deleted metrics: the table
  // visually shows nothing checked (the rows are gone), but `selectedMetrics`
  // — and therefore the "Delete N selected" button — still holds the stale
  // count. Pruning to the intersection with `allMetrics` on every
  // data change (rather than clearing outright) preserves a still-valid
  // in-progress selection across unrelated refetches.
  React.useEffect(() => {
    setSelectedMetrics((prev) => {
      if (prev.length === 0) return prev;
      const next = prev.filter((s) =>
        allMetrics.some((m) => m.name === s.name),
      );
      return next.length === prev.length ? prev : next;
    });
  }, [allMetrics]);

  const {
    items,
    allPageItems,
    collectionProps,
    propertyFilterProps,
    paginationProps,
    filteredItemsCount,
  } = useCollection(allMetrics, {
    propertyFiltering: {
      filteringProperties,
      empty: (
        <Box textAlign="center" color="inherit">
          <b>{t("No metrics")}</b>
          <Box padding={{ bottom: "s" }} variant="p" color="inherit">
            {t(
              "No metrics have been defined yet. Import an OSI file to get started.",
            )}
          </Box>
          <Button onClick={() => setImportModalVisible(true)}>
            {t("Import OSI")}
          </Button>
        </Box>
      ),
      noMatch: (
        <Box textAlign="center" color="inherit">
          <SpaceBetween size="m">
            <b>{t("No matches")}</b>
            <Box variant="p" color="text-body-secondary">
              {t("No metrics matched the filter criteria.")}
            </Box>
          </SpaceBetween>
        </Box>
      ),
    },
    pagination: { pageSize: preferences.pageSize ?? 20 },
    sorting: { defaultState: { sortingColumn: { sortingField: "name" } } },
  });

  // Auto-fetch next server page when user reaches the end of loaded data
  const pagesCount = paginationProps.pagesCount ?? 1;
  React.useEffect(() => {
    if (
      paginationProps.currentPageIndex >= pagesCount &&
      hasNextPage &&
      !isFetchingNextPage
    ) {
      fetchNextPage();
    }
  }, [
    paginationProps.currentPageIndex,
    pagesCount,
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
  ]);

  // ── Filtered-scope operations over the COMPLETE namespace ─────
  // "Delete/Export all matching" must evaluate the filter against every
  // metric in the namespace, but the list is an infinite query loaded 50 at
  // a time. When a filtered operation is confirmed while more server pages
  // exist, we record it as pending, drain the remaining pages, and execute
  // once the dataset is complete — deriving names from `allPageItems` (the
  // full filtered set across client pages; `items` is only the CURRENT
  // client page and must never be used for scope decisions).
  const [pendingFilteredOp, setPendingFilteredOp] = useState<
    null | "export" | "delete"
  >(null);

  // Type guards for RadioGroup values (project convention: no `as` casts —
  // .claude/rules/frontend-typescript.md).
  const isDeleteMode = (value: string): value is "selected" | "filtered" =>
    value === "selected" || value === "filtered";
  const isExportMode = (
    value: string,
  ): value is "all" | "selected" | "filtered" =>
    value === "all" || value === "selected" || value === "filtered";

  // Drain remaining server pages while a filtered operation is pending.
  // A failed page fetch must cancel the pending operation and surface the
  // error — otherwise the confirm button spins forever with no way to
  // recover short of a page refresh.
  React.useEffect(() => {
    if (pendingFilteredOp && hasNextPage && !isFetchingNextPage) {
      Promise.resolve(fetchNextPage()).catch((err: unknown) => {
        setPendingFilteredOp(null);
        setImportResult({
          type: "error",
          message: t(
            "Failed to load all metrics for the filtered operation: {message}",
            {
              message: err instanceof Error ? err.message : t("Unknown error"),
            },
          ),
        });
      });
    }
  }, [pendingFilteredOp, hasNextPage, isFetchingNextPage, fetchNextPage, t]);

  const filteredNamesComplete = React.useMemo(
    () =>
      allPageItems
        .map((m) => m.name)
        .filter((name): name is string => Boolean(name)),
    [allPageItems],
  );

  // ── Import handler ──────────────────────────────────────────────────

  const openImportModal = () => {
    setImportFiles([]);
    importMutation.reset();
    setImportModalVisible(true);
  };

  const closeImportModal = () => {
    setImportModalVisible(false);
    setImportFiles([]);
    importMutation.reset();
  };

  const handleImport = async () => {
    if (importFiles.length === 0) return;

    const file = importFiles[0];

    try {
      importMutation.mutate(file, {
        onSuccess: (result) => {
          setImportModalVisible(false);
          setImportFiles([]);

          if (result.status === "IN_PROGRESS" && result.jobId) {
            // Async — start polling
            startJobPolling(result.jobId);
            setImportResult({
              type: "success",
              message: t("Import started. Processing in the background…"),
            });
          } else {
            // Sync — completed immediately
            const parts: string[] = [];
            if (result.metricsCreated > 0)
              parts.push(
                t("{count} created", { count: result.metricsCreated }),
              );
            if (result.metricsUpdated > 0)
              parts.push(
                t("{count} updated", { count: result.metricsUpdated }),
              );
            if (result.datasetsResolved > 0)
              parts.push(
                t("{count} datasets resolved", {
                  count: result.datasetsResolved,
                }),
              );
            setImportResult({
              type: "success",
              message: t("Import successful: {result}.{warnings}", {
                result: parts.join(", "),
                warnings: result.warnings?.length
                  ? t(" Warnings: {warnings}", {
                      warnings: result.warnings.join("; "),
                    })
                  : "",
              }),
            });
          }
        },
        onError: (err) => {
          setImportResult({
            type: "error",
            message: t("Import failed: {message}", { message: err.message }),
          });
        },
      });
    } catch (err) {
      setImportResult({
        type: "error",
        message: t("Failed to read file: {message}", {
          message: err instanceof Error ? err.message : t("Unknown error"),
        }),
      });
    }
  };

  // ── Export handler ──────────────────────────────────────────────────

  const openExportModal = () => {
    // Default to a scoped mode when one is available, mirroring the
    // bulk-delete modal's preference for the more targeted option.
    if (selectedMetrics.length > 0) {
      setExportMode("selected");
    } else if (propertyFilterProps.query.tokens.length > 0) {
      setExportMode("filtered");
    } else {
      setExportMode("all");
    }
    setDownloadError(null);
    setExportModalVisible(true);
  };

  const runExport = (names: string[] | undefined) => {
    // Client-side download errors (blob/anchor click failures) are unrelated
    // to the mutation itself, so they get their own inline state — the
    // mutation's own failures are already surfaced via `exportMutation.isError`
    // / `exportMutation.error` in the modal, and shouldn't also go to the
    // page-level `importResult` banner (that banner is for import/bulk-delete,
    // not export).
    setDownloadError(null);
    exportMutation.mutate(names ? { names } : undefined, {
      onSuccess: (result) => {
        try {
          const blob = new Blob([result.content], {
            type: "application/x-yaml",
          });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = `${namespaceId}-metrics.osi.yaml`;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(url);
          // Only close the modal once the download has actually succeeded —
          // if any of the steps above throw, we fall into the catch block
          // below and the modal stays open so the inline error is visible.
          setExportModalVisible(false);
        } catch (err) {
          setDownloadError(
            t("Download failed: {message}", {
              message: err instanceof Error ? err.message : t("Unknown error"),
            }),
          );
        }
      },
      // No onError here — `exportMutation.isError`/`exportMutation.error`
      // already drive the inline Alert in the export modal below.
    });
  };

  const runBulkDelete = (names: string[]) => {
    bulkDeleteMutation.mutate(
      { names },
      {
        onSuccess: (result) => {
          setBulkDeleteModalVisible(false);
          setSelectedMetrics([]);
          setImportResult({
            type: "success",
            message: t("Deleted {count} metrics.{notFound}", {
              count: result.deleted,
              notFound: result.notFound?.length
                ? t(" {count} not found.", {
                    count: result.notFound.length,
                  })
                : "",
            }),
          });
        },
        onError: (err) => {
          setImportResult({
            type: "error",
            message: t("Bulk delete failed: {message}", {
              message: err.message,
            }),
          });
        },
      },
    );
  };

  const handleConfirmExport = () => {
    if (exportMode === "all") {
      runExport(undefined);
      return;
    }
    if (exportMode === "selected") {
      const names = selectedMetrics
        .map((m) => m.name)
        .filter((name): name is string => Boolean(name));
      if (names.length === 0) {
        setDownloadError(t("No metrics selected to export."));
        return;
      }
      runExport(names);
      return;
    }
    // "filtered" — evaluate over the COMPLETE namespace. If more
    // server pages exist, defer to the pending-op effect below, which drains
    // them first; otherwise execute immediately from `allPageItems`.
    if (hasNextPage || isFetchingNextPage) {
      setPendingFilteredOp("export");
      return;
    }
    if (filteredNamesComplete.length === 0) {
      setDownloadError(t("No metrics match the current filter to export."));
      return;
    }
    runExport(filteredNamesComplete);
  };

  // Execute a pending filtered operation once every server page is loaded.
  // Runs render-driven (not inside the confirm handler) so the names are
  // derived from the complete, fresh `allPageItems` — an async handler would
  // capture a stale closure from before the drain.
  React.useEffect(() => {
    if (!pendingFilteredOp || hasNextPage || isFetchingNextPage) return;
    const op = pendingFilteredOp;
    setPendingFilteredOp(null);
    // If the infinite query errored during the drain, the loaded set may be
    // incomplete — cancel rather than run a partial export/delete.
    if (error) {
      setImportResult({
        type: "error",
        message: t(
          "Cancelled filtered {operation}: the metric list failed to load completely ({message}).",
          { operation: op, message: error.message },
        ),
      });
      return;
    }
    if (op === "export") {
      if (filteredNamesComplete.length === 0) {
        setDownloadError(t("No metrics match the current filter to export."));
        return;
      }
      runExport(filteredNamesComplete);
    } else {
      runBulkDelete(filteredNamesComplete);
    }
    // runExport/runBulkDelete are intentionally omitted from the deps:
    // they are recreated per render but stable in behavior, and the
    // `pendingFilteredOp` guard makes extra invocations impossible.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    pendingFilteredOp,
    hasNextPage,
    isFetchingNextPage,
    error,
    filteredNamesComplete,
  ]);

  return (
    <SpaceBetween size="l">
      {importResult && (
        <Alert
          type={importResult.type}
          dismissible
          onDismiss={() => setImportResult(null)}
        >
          {importResult.message}
        </Alert>
      )}

      {error && !queryErrorDismissed && (
        <Alert
          type="error"
          dismissible
          onDismiss={() => setQueryErrorDismissed(true)}
        >
          {t("Failed to load metrics: {message}", {
            message: error.message,
          })}
        </Alert>
      )}

      {importJob && importJob.status === "IN_PROGRESS" && (
        <Alert type="info" header={t("Import in progress")}>
          {t(
            "Processing metrics: {processed}/{total} ({created} created, {updated} updated). This page will update automatically.",
            {
              processed: importJob.metricsProcessed,
              total: importJob.metricsTotal,
              created: importJob.metricsCreated,
              updated: importJob.metricsUpdated,
            },
          )}
        </Alert>
      )}

      {importJob && importJob.status === "COMPLETED" && activeJobId && (
        <Alert type="success" dismissible onDismiss={() => clearActiveJob()}>
          {t("Import complete: {created} created, {updated} updated.", {
            created: importJob.metricsCreated,
            updated: importJob.metricsUpdated,
          })}
          {importJob.warnings &&
            importJob.warnings.length > 0 &&
            t(" Warnings: {warnings}", {
              warnings: importJob.warnings.join("; "),
            })}
        </Alert>
      )}

      {importJob && importJob.status === "FAILED" && activeJobId && (
        <Alert type="error" dismissible onDismiss={() => clearActiveJob()}>
          {t("Import failed after processing {processed}/{total} metrics.", {
            processed: importJob.metricsProcessed,
            total: importJob.metricsTotal,
          })}
          {importJob.errors &&
            importJob.errors.length > 0 &&
            t(" Errors: {errors}", {
              errors: importJob.errors.slice(0, 5).join("; "),
            })}
        </Alert>
      )}

      <Table
        {...collectionProps}
        loading={isLoading}
        loadingText={t("Loading metrics")}
        items={items}
        selectionType="multi"
        selectedItems={selectedMetrics}
        onSelectionChange={({ detail }) =>
          setSelectedMetrics(detail.selectedItems)
        }
        trackBy="name"
        resizableColumns
        stickyHeader
        wrapLines={preferences.wrapLines}
        stripedRows={preferences.stripedRows}
        contentDensity={preferences.contentDensity}
        columnDisplay={preferences.contentDisplay}
        columnDefinitions={[
          {
            id: "name",
            header: t("Name"),
            cell: (item: MetricDefinition) => (
              <Link
                href={`/namespaces/${namespaceId}/metrics/${item.name}`}
                onFollow={(e) => {
                  e.preventDefault();
                  navigate(`/namespaces/${namespaceId}/metrics/${item.name}`);
                }}
              >
                {item.name}
              </Link>
            ),
            sortingField: "name",
            minWidth: 180,
          },
          {
            id: "description",
            header: t("Description"),
            cell: (item: MetricDefinition) => item.description,
          },
          {
            id: "dataSourceId",
            header: t("Data source"),
            cell: (item: MetricDefinition) =>
              item.dataSourceId
                ? (sourceNameById.get(item.dataSourceId) ?? item.dataSourceId)
                : "—",
          },
          {
            id: "sourceTable",
            header: t("Table"),
            cell: (item: MetricDefinition) => item.sourceTable,
          },
          {
            id: "dialects",
            header: t("Dialects"),
            cell: (item: MetricDefinition) =>
              (item.expression?.dialects ?? [])
                .map((d: { dialect?: string }) => d.dialect ?? "")
                .join(", "),
          },
          {
            id: "defaultTimeGrain",
            header: t("Time grain"),
            cell: (item: MetricDefinition) => item.defaultTimeGrain ?? "—",
            sortingField: "defaultTimeGrain",
          },
          {
            id: "unit",
            header: t("Unit"),
            cell: (item: MetricDefinition) => item.unit ?? "—",
          },
          {
            id: "definedBy",
            header: t("Defined by"),
            cell: (item: MetricDefinition) => item.definedBy ?? "—",
            sortingField: "definedBy",
          },
        ]}
        header={
          <Header
            variant="h1"
            counter={`(${allMetrics.length}${hasNextPage ? "+" : ""})`}
            description={t(
              "Governed business metrics with pre-compiled SQL expressions.",
            )}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  iconName="refresh"
                  loading={isFetching}
                  onClick={() => refetch()}
                />
                <Button
                  disabled={selectedMetrics.length === 0}
                  onClick={() => setBulkDeleteModalVisible(true)}
                >
                  {selectedMetrics.length > 0
                    ? t("Delete {count} selected", {
                        count: selectedMetrics.length,
                      })
                    : t("Delete")}
                </Button>
                <Button iconName="upload" onClick={openImportModal}>
                  {t("Import OSI")}
                </Button>
                <Button
                  iconName="download"
                  onClick={openExportModal}
                  loading={exportMutation.isPending}
                  disabled={allMetrics.length === 0}
                >
                  {t("Export OSI")}
                </Button>
                <Button
                  variant="primary"
                  onClick={() =>
                    navigate(`/namespaces/${namespaceId}/metrics/create`)
                  }
                >
                  {t("Create metric")}
                </Button>
              </SpaceBetween>
            }
          >
            {t("Metrics")}
          </Header>
        }
        filter={
          <PropertyFilter
            {...propertyFilterProps}
            filteringAriaLabel={t("Filter metrics")}
            filteringPlaceholder={t("Find metrics")}
            i18nStrings={{
              // App copy the Cloudscape bundle does not carry (the free-text
              // token offer); everything else comes from the I18nProvider.
              enteredTextLabel: (text) => t('Use: "{value}"', { value: text }),
            }}
            countText={t("{count} matches", {
              count: filteredItemsCount ?? 0,
            })}
            expandToViewport
          />
        }
        pagination={<Pagination {...paginationProps} openEnd={hasNextPage} />}
        preferences={
          <CollectionPreferences
            title={t("Preferences")}
            confirmLabel={t("Confirm")}
            cancelLabel={t("Cancel")}
            preferences={preferences}
            onConfirm={({ detail }) => setPreferences(detail)}
            pageSizePreference={{
              title: t("Page size"),
              options: pageSizeOptions,
            }}
            wrapLinesPreference={{
              label: t("Wrap lines"),
              description: t("Wrap long text to multiple lines."),
            }}
            stripedRowsPreference={{
              label: t("Striped rows"),
              description: t("Alternate row background."),
            }}
            contentDensityPreference={{
              label: t("Density"),
              description: t("Use compact density for scanning."),
            }}
            contentDisplayPreference={{
              title: t("Column preferences"),
              description: t("Customize column visibility and order."),
              options: COLUMNS.map((c) => ({
                id: c.id,
                label: t(c.label),
                alwaysVisible: c.alwaysVisible,
              })),
            }}
          />
        }
      />

      {/* Bulk Delete Confirmation Modal */}
      <Modal
        visible={bulkDeleteModalVisible}
        onDismiss={() => {
          // Closing the modal is a cancel — a filtered delete pending on the
          // page drain must NOT execute after the user dismissed it.
          setBulkDeleteModalVisible(false);
          setPendingFilteredOp(null);
        }}
        header={t("Delete metrics")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setBulkDeleteModalVisible(false);
                  setPendingFilteredOp(null);
                }}
              >
                {t("Cancel")}
              </Button>
              <Button
                variant="primary"
                onClick={() => {
                  if (deleteMode === "filtered") {
                    // Filtered scope must cover the whole namespace:
                    // drain remaining server pages first when needed.
                    if (hasNextPage || isFetchingNextPage) {
                      setPendingFilteredOp("delete");
                      return;
                    }
                    runBulkDelete(filteredNamesComplete);
                    return;
                  }
                  runBulkDelete(
                    selectedMetrics
                      .map((m) => m.name)
                      .filter((name): name is string => Boolean(name)),
                  );
                }}
                loading={
                  bulkDeleteMutation.isPending || pendingFilteredOp === "delete"
                }
              >
                {t("Delete")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {propertyFilterProps.query.tokens.length > 0 &&
          selectedMetrics.length > 0 ? (
            <RadioGroup
              value={deleteMode}
              onChange={({ detail }) => {
                if (isDeleteMode(detail.value)) setDeleteMode(detail.value);
              }}
              items={[
                {
                  value: "selected",
                  label: t("Delete {count} selected metrics", {
                    count: selectedMetrics.length,
                  }),
                },
                {
                  value: "filtered",
                  label: t("Delete all {count} metrics matching: {filter}", {
                    count: `${filteredItemsCount ?? 0}${hasNextPage ? "+" : ""}`,
                    filter: propertyFilterProps.query.tokens
                      .map(
                        (token) =>
                          `${token.propertyKey} ${token.operator} ${token.value}`,
                      )
                      .join(", "),
                  }),
                },
              ]}
            />
          ) : (
            <Box>
              {t(
                "Permanently delete {count} selected metrics? This action cannot be undone.",
                { count: selectedMetrics.length },
              )}
            </Box>
          )}
          <Box color="text-body-secondary">
            {t("This action cannot be undone.")}
          </Box>
          {bulkDeleteMutation.isError && (
            <Alert type="error">
              {bulkDeleteMutation.error?.message ?? "Delete failed"}
            </Alert>
          )}
        </SpaceBetween>
      </Modal>

      {/* Export Scope Modal */}
      <Modal
        visible={exportModalVisible}
        onDismiss={() => {
          // Closing the modal is a cancel — a filtered export pending on the
          // page drain must NOT execute after the user dismissed it.
          setExportModalVisible(false);
          setPendingFilteredOp(null);
        }}
        header={t("Export OSI metrics")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setExportModalVisible(false);
                  setPendingFilteredOp(null);
                }}
              >
                {t("Cancel")}
              </Button>
              <Button
                variant="primary"
                onClick={handleConfirmExport}
                loading={
                  exportMutation.isPending || pendingFilteredOp === "export"
                }
              >
                {t("Export")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <RadioGroup
            value={exportMode}
            onChange={({ detail }) => {
              if (isExportMode(detail.value)) setExportMode(detail.value);
            }}
            items={[
              {
                value: "all",
                label: t("Export all {count} metrics in this namespace", {
                  count: allMetrics.length,
                }),
              },
              {
                value: "selected",
                label: t("Export {count} selected metrics", {
                  count: selectedMetrics.length,
                }),
                disabled: selectedMetrics.length === 0,
              },
              {
                value: "filtered",
                label:
                  propertyFilterProps.query.tokens.length > 0
                    ? t("Export all {count} metrics matching: {filter}", {
                        count: `${filteredItemsCount ?? 0}${hasNextPage ? "+" : ""}`,
                        filter: propertyFilterProps.query.tokens
                          .map(
                            (token) =>
                              `${token.propertyKey} ${token.operator} ${token.value}`,
                          )
                          .join(", "),
                      })
                    : t("Export filtered metrics (no filter applied)"),
                disabled: propertyFilterProps.query.tokens.length === 0,
              },
            ]}
          />
          {exportMutation.isError && (
            <Alert type="error">
              {exportMutation.error?.message ?? t("Export failed")}
            </Alert>
          )}
          {downloadError && <Alert type="error">{downloadError}</Alert>}
        </SpaceBetween>
      </Modal>

      {/* Import Modal */}
      <Modal
        visible={importModalVisible}
        onDismiss={closeImportModal}
        header={t("Import OSI metrics")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={closeImportModal}>
                {t("Cancel")}
              </Button>
              <Button
                variant="primary"
                onClick={handleImport}
                loading={importMutation.isPending}
                disabled={importFiles.length === 0}
              >
                {t("Import")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <FormField
            label={t("OSI YAML file")}
            description={t(
              "Select an OSI v1.0 YAML file containing metric definitions.",
            )}
          >
            <FileUpload
              value={importFiles}
              onChange={({ detail }) => setImportFiles(detail.value)}
              accept=".yaml,.yml"
              constraintText={t("Accepted format: .yaml, .yml (OSI v1.0)")}
            />
          </FormField>
          {importMutation.isError && (
            <Alert type="error">
              {importMutation.error?.message ?? t("Import failed")}
            </Alert>
          )}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};
