// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useNamespace } from "@api-hooks/use-namespace";
import { useQueryClient } from "@tanstack/react-query";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import Flashbar from "@cloudscape-design/components/flashbar";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import Pagination from "@cloudscape-design/components/pagination";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import Select from "@cloudscape-design/components/select";
import Toggle from "@cloudscape-design/components/toggle";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import TextFilter from "@cloudscape-design/components/text-filter";
import type { StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";
import { useCollection } from "@cloudscape-design/collection-hooks";
import {
  useGetSource,
  useDeleteSource,
  useRescanSource,
  useListSourceTables,
  useApproveSource,
  useRejectSource,
  useGetSourceScanJob,
  useListSourceScanJobs,
  useKeepRescanRemoval,
  usePutSourceRescanSchedule,
  usePutSourceEventRescan,
} from "@api-hooks";
import {
  ReviewDecision,
  ReviewSourceTableCommand,
  ReviewStatus,
  SourceStatus,
} from "@coa/control-plane-client";
import type {
  EventRescanConfig,
  RescanSchedule,
  ScanJobEntry,
  TableSummary,
} from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";
import { ButtonWithHint } from "@components/ButtonWithHint";
import { sourceStatusType, sourceStatusLabel } from "@utils/source-status";
import { formatTimestamp } from "@utils/helpers";
import { tableDetailPath } from "@utils/routes";
import { useAppI18n, type AppTranslate } from "../i18n/LocaleProvider";
import type { AppMessage } from "../i18n/appMessages";

const TABLES_PAGE_SIZE = 25;
const METADATA_FRESHNESS_THRESHOLD_DAYS = 30;
// Matches the enricher's publish throttle; polling faster only re-reads the same row.
const SCAN_PROGRESS_POLL_MS = 5000;

// ── Status helpers ────────────────────────────────────────────────────────────

const ACTIVE_STATUSES = new Set([
  "REGISTERED",
  "SCANNING",
  "ENRICHING",
  "DELETING",
  "APPROVING",
  "REJECTING",
]);

// Human labels for the DATABASE sub-types. A lookup rather than a ternary so a
// sub-type without an entry shows its own name instead of being mislabelled as
// one of the others.
const DATABASE_SUBTYPE_LABELS: Record<string, AppMessage> = {
  GLUE_DATABASE: "Glue database",
  JDBC_DATABASE: "JDBC database",
  CUSTOM_CONNECTOR: "Custom connector",
  DATABRICKS_SQL_WAREHOUSE: "Databricks SQL Warehouse",
};

const SOURCE_STATUS_MESSAGES: Record<string, AppMessage> = {
  APPROVED: "Approved",
  COMPLETED: "Completed",
  PENDING_REVIEW: "Pending review",
  RESCAN_REVIEW: "Re-scan review",
  REGISTERED: "Registered",
  SCANNING: "Scanning",
  ENRICHING: "Enriching",
  APPROVING: "Approving…",
  REJECTING: "Rejecting…",
  APPROVAL_FAILED: "Approval failed",
  REJECTION_FAILED: "Rejection failed",
  SCAN_FAILED: "Scan failed",
  DELETE_FAILED: "Delete failed",
  DELETING: "Deleting",
  DELETED: "Deleted",
  REJECTED: "Rejected",
};

function localizedSourceStatus(
  status: string | undefined,
  t: AppTranslate,
): string {
  const message = status ? SOURCE_STATUS_MESSAGES[status] : undefined;
  return message ? t(message) : sourceStatusLabel(status);
}

// ── Degraded-scan annotation ──────────────────────────────────────────────────
//
// Discovery for the connector-backed sub-types — CUSTOM_CONNECTOR and
// DATABRICKS_SQL_WAREHOUSE — reads one table at a time (a DESCRIBE per table),
// so a single unreadable table is dropped while the scan as a whole still
// SUCCEEDS. On a Databricks source that is the common case rather than the
// exotic one: Unity Catalog privilege-filters results, so a table listed under
// BROWSE but not SELECT-able is listed and then not readable.
// The scan job then reports `tablesFailed` — the
// count of listed-but-unreadable tables, absent rather than zero when the scan
// was clean, so its presence alone marks the scan degraded — and
// `failedTables`, the affected tables as `database.table`.
//
// `failedTables` is a capped diagnostic sample and may be shorter than
// `tablesFailed`, so the count is always taken from `tablesFailed` and never
// derived from the list's length.
//
// Both members ARE modelled on `GetSourceScanJobOutput`, but they are read
// defensively off the response rather than by typed field access, mirroring
// `normalizeResponse` in `@api-hooks/normalize-response` — the package's idiom
// for values the API assembles as a raw dict rather than through a model.
//
// The `typeof === "number"` check is load-bearing, not decoration. DynamoDB
// numbers arrive as `Decimal` through boto3's resource interface and serialize to
// JSON as STRINGS unless the handler coerces them, so a regression on the API
// side must read here as "not degraded" rather than as a truthy count.

interface DegradedScan {
  tablesFailed: number;
  failedTables: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// A filter that looked like a regular expression and matched nothing — filters
// are globs — leaves the scan successful but unfiltered as intended. The scan
// job carries the plain-language reasons as `filterWarnings`, absent when none.
function readFilterWarnings(scanJob: unknown): string[] {
  if (!isRecord(scanJob) || !Array.isArray(scanJob.filterWarnings)) return [];
  return scanJob.filterWarnings.filter(
    (entry): entry is string => typeof entry === "string",
  );
}

function readDegradedScan(scanJob: unknown): DegradedScan | undefined {
  if (!isRecord(scanJob)) return undefined;
  const count = scanJob.tablesFailed;
  if (typeof count !== "number" || count <= 0) return undefined;
  const listed = scanJob.failedTables;
  return {
    tablesFailed: count,
    failedTables: Array.isArray(listed)
      ? listed.filter((entry): entry is string => typeof entry === "string")
      : [],
  };
}

interface ScanProgress {
  processed: number;
  total: number;
  percent: number;
}

// Read defensively for the same reason as readDegradedScan: the handler builds
// this response as a raw dict, so a missed int() coercion arrives as a string.
function readScanProgress(scanJob: unknown): ScanProgress | undefined {
  if (!isRecord(scanJob)) return undefined;
  const total = scanJob.tablesTotal;
  if (typeof total !== "number" || total <= 0) return undefined;
  const raw = scanJob.tablesProcessed;
  // Clamped: tablesTotal can come from an older scan while this one is early.
  const processed = Math.min(typeof raw === "number" ? raw : 0, total);
  return { processed, total, percent: Math.round((processed / total) * 100) };
}

const ValueWithLabel: React.FC<{
  label: string;
  children: React.ReactNode;
}> = ({ label, children }) => (
  <div>
    <Box variant="awsui-key-label">{label}</Box>
    <div>{children}</div>
  </div>
);

// ── Component ─────────────────────────────────────────────────────────────────

export const SourceDetail: React.FC = () => {
  const { t } = useAppI18n();
  const { namespaceId, sourceId } = useParams<{
    namespaceId: string;
    sourceId: string;
  }>();
  const navigate = useNavigate();
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  // Shown instead of starting the re-scan when a re-scan review is already
  // open, since starting a new one discards that review's decisions and edits.
  const [showRescanConfirm, setShowRescanConfirm] = useState(false);
  const [flash, setFlash] = useState<
    { id: string; type: "success" | "error"; content: string }[]
  >([]);

  const { data, isLoading, error, refetch } = useGetSource(
    namespaceId ?? "",
    sourceId ?? "",
  );

  const { name: nsName } = useNamespace(namespaceId);
  const sourceName = data?.body?.name ?? sourceId ?? "";
  useBreadcrumbTrail([
    { text: nsName, href: `/namespaces/${namespaceId}`, namespaceId },
    {
      text: t("Sources"),
      href: `/namespaces/${namespaceId}/sources`,
      namespaceId,
    },
    { text: sourceName },
  ]);

  const {
    mutate: deleteSource,
    isPending: isDeleting,
    error: deleteError,
  } = useDeleteSource(namespaceId ?? "", sourceId ?? "", {
    onSuccess: () => navigate(`/namespaces/${namespaceId}/sources`),
  });

  const {
    mutate: rescan,
    isPending: isRescanning,
    error: rescanError,
  } = useRescanSource(namespaceId ?? "", sourceId ?? "", {
    onSuccess: () => {
      setFlash([
        {
          id: String(Date.now()),
          type: "success",
          content: t("Re-scan triggered."),
        },
      ]);
      refetch();
    },
  });

  const source = data?.body;
  const isDatabase = source?.sourceType === "DATABASE";

  // Tables — only fetched for DATABASE sources via the new sources-api tables endpoint.
  // Pages render as they arrive; `tablesLoadingMore` means the list on screen is
  // a valid prefix, not the whole source, so the counts are marked provisional.
  const {
    data: tablesData,
    isLoading: tablesLoading,
    isLoadingMore: tablesLoadingMore,
    isError: tablesError,
    error: tablesErrorObj,
  } = useListSourceTables(
    namespaceId ?? "",
    isDatabase ? (sourceId ?? "") : "",
  );
  const approveMutation = useApproveSource(namespaceId ?? "", sourceId ?? "");
  const rejectMutation = useRejectSource(namespaceId ?? "", sourceId ?? "");
  const controlPlaneClient = useControlPlaneClient();
  const queryClient = useQueryClient();
  const [selectedTables, setSelectedTables] = useState<TableSummary[]>([]);
  const [isBatchReviewing, setIsBatchReviewing] = useState(false);
  // Re-scan review: keep (decline) a table the re-scan flagged for deletion.
  const keepMutation = useKeepRescanRemoval(namespaceId ?? "", sourceId ?? "");
  const [keepingTableId, setKeepingTableId] = useState<string | null>(null);

  // Refetch tables list when source transitions out of APPROVING/REJECTING
  const prevStatusRef = useRef(source?.status);
  useEffect(() => {
    const prev = prevStatusRef.current;
    const curr = source?.status;
    prevStatusRef.current = curr;
    if (
      (prev === SourceStatus.APPROVING || prev === SourceStatus.REJECTING) &&
      curr !== prev
    ) {
      queryClient.invalidateQueries({
        queryKey: ["sourceTables", namespaceId, sourceId],
      });
    }
  }, [source?.status, queryClient, namespaceId, sourceId]);

  // Scan-job record for the most recent scan of a database source. Fetched for
  // every scan, not just failed ones: a scan can succeed and still have dropped
  // individual tables, and that count lives only on the scan job.
  // Polled while enriching, which is where per-table progress advances.
  const isEnriching = source?.status === SourceStatus.ENRICHING;
  const { data: scanJobData } = useGetSourceScanJob(
    namespaceId ?? "",
    sourceId ?? "",
    isDatabase ? (source?.databaseDetails?.lastScanJobId ?? "") : "",
    isEnriching ? SCAN_PROGRESS_POLL_MS : undefined,
  );

  // useCollection must be called unconditionally (Rules of Hooks) — before any early returns.
  // Sort: tables with unapproved columns first, then fully approved, alphabetical within each group.
  const tables = React.useMemo(() => {
    const raw = tablesData?.items ?? [];
    return [...raw].sort((a, b) => {
      const aFull =
        (a.columnsApproved ?? 0) >= (a.columnCount ?? 0) &&
        (a.columnCount ?? 0) > 0;
      const bFull =
        (b.columnsApproved ?? 0) >= (b.columnCount ?? 0) &&
        (b.columnCount ?? 0) > 0;
      if (aFull !== bFull) return aFull ? 1 : -1;
      return (a.name ?? "").localeCompare(b.name ?? "");
    });
  }, [tablesData]);
  const [statusFilter, setStatusFilter] = useState<string | null>(null);
  const filteredTables = statusFilter
    ? tables.filter((t) => t.reviewStatus === statusFilter)
    : tables;
  const {
    items: pagedTables,
    collectionProps,
    filterProps,
    paginationProps,
  } = useCollection(filteredTables, {
    filtering: {
      empty: (
        <Box textAlign="center" color="text-status-inactive" padding="l">
          {t("No tables found.")}
        </Box>
      ),
      noMatch: (
        <Box textAlign="center" color="text-status-inactive" padding="l">
          {t("No tables match the filter.")}
        </Box>
      ),
    },
    pagination: { pageSize: TABLES_PAGE_SIZE },
    sorting: {
      defaultState: {
        sortingColumn: { sortingField: "reviewStatus" },
        isDescending: false,
      },
    },
  });

  // Warn when approved metadata is older than threshold — schema may have drifted.
  const isMetadataStale = useMemo(() => {
    const s = source?.status;
    const lastScanAt = source?.databaseDetails?.lastScanAt;
    if (s !== SourceStatus.APPROVED || !lastScanAt) return false;
    const lastScan = new Date(
      lastScanAt instanceof Date ? lastScanAt.toISOString() : lastScanAt,
    );
    const ageDays = Math.floor(
      (Date.now() - lastScan.getTime()) / (1000 * 60 * 60 * 24),
    );
    return ageDays > METADATA_FRESHNESS_THRESHOLD_DAYS;
  }, [source?.status, source?.databaseDetails?.lastScanAt]);

  if (isLoading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (error) {
    return (
      <Alert type="error" header={t("Failed to load source")}>
        {error.message}
      </Alert>
    );
  }

  if (!source) return null;

  const isDocuments = source.sourceType === "DOCUMENTS";
  const isActive = ACTIVE_STATUSES.has(source.status ?? "");
  const status = source.status ?? "";

  // Re-scan entry points mirror the backend's allowed statuses (RescanSource):
  // DATABASE from SCAN_FAILED (recovery), APPROVED / RESCAN_REVIEW (schema-drift
  // re-scan of a live source); DOCUMENTS from COMPLETED (re-ingest) or
  // SCAN_FAILED (retry). Enabling APPROVED is what makes the drift re-scan
  // review flow reachable from the UI.
  const canRescan = isDatabase
    ? status === SourceStatus.SCAN_FAILED ||
      status === SourceStatus.APPROVED ||
      status === SourceStatus.RESCAN_REVIEW
    : isDocuments
      ? status === SourceStatus.COMPLETED || status === SourceStatus.SCAN_FAILED
      : status === SourceStatus.SCAN_FAILED;

  // Type-specific detail objects
  const dbDetails = source.databaseDetails;
  const docDetails = source.documentDetails;

  // Present only when the last scan succeeded but lost individual tables.
  const degradedScan = readDegradedScan(scanJobData);
  const filterWarnings = readFilterWarnings(scanJobData);

  // Only while enriching: before that, lastScanJobId still points at the
  // previous (finished) job, whose counts would render as a full bar.
  const scanProgress = readScanProgress(isEnriching ? scanJobData : undefined);

  const preprocessingIssues = Array.isArray(docDetails?.preprocessingIssues)
    ? docDetails.preprocessingIssues
    : [];
  const extractionConfig = docDetails?.extractionConfig;

  const handleApproveAll = () => {
    approveMutation.mutate(
      {},
      {
        onSuccess: () =>
          setFlash([
            {
              id: String(Date.now()),
              type: "success",
              content: t(
                "Bulk approve started. Status will update when complete.",
              ),
            },
          ]),
        onError: (e) =>
          setFlash([
            { id: String(Date.now()), type: "error", content: e.message },
          ]),
      },
    );
  };

  const handleRejectAll = () => {
    rejectMutation.mutate(
      {},
      {
        onSuccess: () =>
          setFlash([
            {
              id: String(Date.now()),
              type: "success",
              content: t(
                "Bulk reject started. Status will update when complete.",
              ),
            },
          ]),
        onError: (e) =>
          setFlash([
            { id: String(Date.now()), type: "error", content: e.message },
          ]),
      },
    );
  };

  const handleBatchReviewTables = async (decision: ReviewDecision) => {
    if (selectedTables.length === 0) return;
    setIsBatchReviewing(true);
    const approved = decision === ReviewDecision.APPROVED;
    try {
      const results = await Promise.allSettled(
        selectedTables.map((t) =>
          controlPlaneClient.send(
            new ReviewSourceTableCommand({
              namespaceId: namespaceId!,
              sourceId: sourceId!,
              tableId: t.tableId!,
              decision,
            }),
          ),
        ),
      );
      const succeeded = results.filter((r) => r.status === "fulfilled").length;
      const failed = results.filter((r) => r.status === "rejected").length;
      if (failed === 0) {
        setFlash([
          {
            id: String(Date.now()),
            type: "success",
            content: t(
              approved
                ? "{count} table(s) approved."
                : "{count} table(s) rejected.",
              { count: succeeded },
            ),
          },
        ]);
      } else {
        setFlash([
          {
            id: String(Date.now()),
            type: "error",
            content: t(
              approved
                ? "{succeeded} of {total} table(s) approved. {failed} failed."
                : "{succeeded} of {total} table(s) rejected. {failed} failed.",
              {
                succeeded,
                total: selectedTables.length,
                failed,
              },
            ),
          },
        ]);
      }
      setSelectedTables([]);
      queryClient.invalidateQueries({
        queryKey: ["sourceTables", namespaceId, sourceId],
      });
      queryClient.invalidateQueries({
        queryKey: ["source", namespaceId, sourceId],
      });
    } finally {
      setIsBatchReviewing(false);
    }
  };

  const isTransient =
    status === SourceStatus.APPROVING || status === SourceStatus.REJECTING;

  const handleKeepTable = (tableId: string) => {
    setKeepingTableId(tableId);
    keepMutation.mutate(
      { tableId },
      {
        onSuccess: () =>
          setFlash([
            {
              id: String(Date.now()),
              type: "success",
              content: t(
                'Keeping "{name}". Approving the re-scan will no longer delete it.',
                { name: tableId },
              ),
            },
          ]),
        onError: (e) =>
          setFlash([
            { id: String(Date.now()), type: "error", content: e.message },
          ]),
        onSettled: () => setKeepingTableId(null),
      },
    );
  };

  return (
    <SpaceBetween size="l">
      {flash.length > 0 && (
        <Flashbar
          items={flash.map((f) => ({
            ...f,
            dismissible: true,
            onDismiss: () =>
              setFlash((prev) => prev.filter((x) => x.id !== f.id)),
          }))}
        />
      )}

      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              iconName="refresh"
              ariaLabel={t("Refresh")}
              onClick={() => {
                // RESCAN_REVIEW is terminal, so the source stops polling the
                // moment the review opens, and the tables list and per-table diff
                // never poll at all. Re-pull all three so a change made elsewhere
                // (another steward's keep, a late-finishing worker) appears
                // without a full-page reload.
                queryClient.invalidateQueries({
                  queryKey: ["source", namespaceId ?? "", sourceId ?? ""],
                });
                queryClient.invalidateQueries({
                  queryKey: ["sourceTables", namespaceId ?? "", sourceId ?? ""],
                });
                queryClient.invalidateQueries({
                  queryKey: ["sourceTable", namespaceId ?? "", sourceId ?? ""],
                });
              }}
            >
              {t("Refresh")}
            </Button>
            {isDatabase && (
              <>
                <ButtonWithHint
                  hint={
                    status === SourceStatus.RESCAN_REVIEW
                      ? t(
                          "Discards this re-scan: restores changed tables to their last approved version, removes newly-added tables, and returns the source to Approved.",
                        )
                      : t(
                          "Marks all pending tables and columns in this source as rejected. Already-approved or already-rejected items are not affected.",
                        )
                  }
                  onClick={handleRejectAll}
                  loading={rejectMutation.isPending}
                  disabled={
                    isTransient ||
                    status === SourceStatus.APPROVED ||
                    tables.length === 0
                  }
                >
                  {t("Reject source")}
                </ButtonWithHint>
                <ButtonWithHint
                  hint={
                    status === SourceStatus.RESCAN_REVIEW
                      ? t(
                          "Approves the re-scan's changes and deletes the tables and columns it found were removed from the source. Already-rejected items are not affected.",
                        )
                      : status === SourceStatus.APPROVAL_FAILED
                        ? t(
                            "Retries approving all pending tables and columns in this source. Already-rejected items are not affected.",
                          )
                        : t(
                            "Approves all pending tables and columns in this source, making them queryable. Already-rejected items are not affected.",
                          )
                  }
                  variant="primary"
                  onClick={handleApproveAll}
                  loading={approveMutation.isPending}
                  disabled={
                    isTransient ||
                    status === SourceStatus.APPROVED ||
                    tables.length === 0
                  }
                >
                  {status === SourceStatus.APPROVAL_FAILED
                    ? t("Retry approve source")
                    : t("Approve source")}
                </ButtonWithHint>
              </>
            )}
            <Button
              onClick={() => setShowDeleteModal(true)}
              disabled={isActive}
            >
              {t("Delete")}
            </Button>
            <Button
              onClick={() => {
                // An open re-scan review is the only state a re-scan destroys
                // work in, so it is the only one that asks first.
                if (status === SourceStatus.RESCAN_REVIEW) {
                  setShowRescanConfirm(true);
                  return;
                }
                rescan({});
              }}
              loading={isRescanning}
              disabled={!canRescan}
            >
              {t("Re-scan")}
            </Button>
            <Button
              onClick={() => navigate(`/namespaces/${namespaceId}/sources`)}
            >
              {t("Back to sources")}
            </Button>
          </SpaceBetween>
        }
      >
        {source.name}
      </Header>

      {rescanError && (
        <Alert type="error" header={t("Failed to start re-scan")}>
          {rescanError.message}
        </Alert>
      )}

      {status === SourceStatus.APPROVAL_FAILED && (
        <Alert type="error" header={t("Bulk approval failed")}>
          {t(
            'The background worker encountered an error. You can retry by clicking "Retry approve source".',
          )}
        </Alert>
      )}

      {status === SourceStatus.REJECTION_FAILED && (
        <Alert type="error" header={t("Bulk rejection failed")}>
          {t(
            'The background worker encountered an error. You can retry by clicking "Reject source".',
          )}
        </Alert>
      )}

      {isTransient && (
        <Alert type="info" header={t("Processing")}>
          {status === SourceStatus.APPROVING
            ? t(
                "Bulk approval is in progress. This page will update automatically.",
              )
            : t(
                "Bulk rejection is in progress. This page will update automatically.",
              )}
        </Alert>
      )}

      {docDetails?.errorMessage && (
        <Alert type="error" header={t("Error")}>
          {docDetails.errorMessage}
        </Alert>
      )}

      {isDatabase &&
        status === SourceStatus.SCAN_FAILED &&
        scanJobData?.errorMessage && (
          <Alert type="error" header={t("Scan failed")}>
            {scanJobData.errorMessage}
          </Alert>
        )}

      {isDatabase && scanProgress && (
        <ProgressBar
          value={scanProgress.percent}
          label={t("Enrichment progress")}
          description={t("{processed} of {total} tables processed", {
            processed: scanProgress.processed,
            total: scanProgress.total,
          })}
          additionalInfo={t(
            "Tables already reviewed count as processed without being regenerated.",
          )}
        />
      )}

      {isDatabase && filterWarnings.length > 0 && (
        <Alert type="warning" header="A filter had no effect on the last scan">
          <SpaceBetween size="xs">
            {filterWarnings.map((warning) => (
              <Box variant="p" key={warning}>
                {warning}
              </Box>
            ))}
          </SpaceBetween>
        </Alert>
      )}

      {isDatabase && degradedScan && (
        <Alert
          type="warning"
          header={t("Scan completed, but metadata is incomplete")}
        >
          <SpaceBetween size="xs">
            <Box variant="p">
              {t(
                "The last scan succeeded but could not read {failedTables} it listed. Those tables have no columns, no comments and no declared keys below, and any AI-generated descriptions were produced over that gap — do not review this source as complete. Re-scan once the tables are readable.",
                {
                  failedTables:
                    degradedScan.tablesFailed === 1
                      ? t("1 of the tables")
                      : t("{count} of the tables", {
                          count: degradedScan.tablesFailed,
                        }),
                },
              )}
            </Box>
            {degradedScan.failedTables.length > 0 && (
              <Box variant="p">
                {degradedScan.failedTables.length < degradedScan.tablesFailed
                  ? t("Affected tables ({shown} of {total} shown):", {
                      shown: degradedScan.failedTables.length,
                      total: degradedScan.tablesFailed,
                    })
                  : t("Affected tables:")}{" "}
                <Box variant="code">{degradedScan.failedTables.join(", ")}</Box>
              </Box>
            )}
          </SpaceBetween>
        </Alert>
      )}

      {isDatabase && isMetadataStale && (
        <Alert type="warning" header={t("Metadata may be stale")}>
          {t(
            "This source was last scanned more than 30 days ago. The underlying schema may have changed since approval. Consider refreshing the source to ensure ontology induction uses up-to-date metadata.",
          )}
        </Alert>
      )}

      {isDatabase && status === SourceStatus.RESCAN_REVIEW && (
        <Alert type="info" header={t("Re-scan ready for review")}>
          {t(
            "This source was re-scanned. New and changed tables and columns are marked Pending review — edit and approve them just like a first scan. Items the re-scan no longer found in the source are marked Pending deletion; approving the source deletes them. If a removal looks wrong (for example a partial scan or a changed include/exclude filter), use Keep to retain the table or column.",
          )}
        </Alert>
      )}

      {/* ── DATABASE layout — matches DataSourceDetail ── */}
      {isDatabase && (
        <>
          <Container
            header={<Header variant="h2">{t("Source summary")}</Header>}
          >
            <KeyValuePairs
              columns={4}
              items={[
                {
                  label: t("Source type"),
                  value:
                    (DATABASE_SUBTYPE_LABELS[source.sourceSubType ?? ""]
                      ? t(DATABASE_SUBTYPE_LABELS[source.sourceSubType ?? ""])
                      : source.sourceSubType) ?? "—",
                },
                {
                  label: t("Status"),
                  value: (
                    <StatusIndicator type={sourceStatusType(status)}>
                      {localizedSourceStatus(status, t)}
                    </StatusIndicator>
                  ),
                },
                {
                  label: t("Tables discovered"),
                  value: String(dbDetails?.tablesDiscovered ?? 0),
                },
                {
                  label: t("Tables approved"),
                  value: `${dbDetails?.tablesApproved ?? 0} / ${dbDetails?.tablesDiscovered ?? 0}`,
                },
                {
                  label: t("Metadata enrichment"),
                  value:
                    dbDetails?.metadataEnrichmentEnabled === false
                      ? t("Disabled")
                      : t("Enabled"),
                },
                {
                  label: t("Last scan"),
                  value: dbDetails?.lastScanAt
                    ? formatTimestamp(dbDetails.lastScanAt)
                    : "—",
                },
                {
                  label: t("Created"),
                  value: formatTimestamp(source.createdAt),
                },
                { label: t("Source ID"), value: source.sourceId },
              ]}
            />
          </Container>

          <Tabs
            tabs={[
              {
                id: "tables",
                label: t("Tables ({count})", {
                  count: `${tables.length}${tablesLoadingMore ? "+" : ""}`,
                }),
                content: (
                  <SpaceBetween size="m">
                    {tablesError && (
                      <Alert type="error" header={t("Failed to load tables")}>
                        {tablesErrorObj instanceof Error
                          ? tablesErrorObj.message
                          : t("The table list could not be retrieved.")}{" "}
                        {(tablesData?.items?.length ?? 0) > 0
                          ? t("The list below is partial.")
                          : ""}
                      </Alert>
                    )}
                    {(tablesData?.skippedAssets ?? 0) > 0 && (
                      <Alert type="warning">
                        {tablesData?.skippedAssets === 1
                          ? t(
                              "1 table could not be loaded due to a data issue. The list below may be incomplete.",
                            )
                          : t(
                              "{count} tables could not be loaded due to data issues. The list below may be incomplete.",
                              { count: tablesData?.skippedAssets ?? 0 },
                            )}
                      </Alert>
                    )}
                    <Table
                      {...collectionProps}
                      variant="container"
                      selectionType="multi"
                      selectedItems={selectedTables}
                      onSelectionChange={({ detail }) =>
                        setSelectedTables(detail.selectedItems)
                      }
                      loading={tablesLoading}
                      items={pagedTables}
                      trackBy="tableId"
                      columnDefinitions={[
                        {
                          id: "name",
                          header: t("Table"),
                          sortingField: "tableId",
                          cell: (item: TableSummary) => (
                            <Link
                              onFollow={(e) => {
                                e.preventDefault();
                                navigate(
                                  tableDetailPath(
                                    namespaceId!,
                                    sourceId!,
                                    item.tableId!,
                                  ),
                                );
                              }}
                            >
                              {item.tableId}
                            </Link>
                          ),
                        },
                        {
                          id: "database",
                          header: t("Database"),
                          sortingField: "database",
                          cell: (item: TableSummary) => item.database,
                        },
                        {
                          id: "columns",
                          header: t("Columns"),
                          // No scalar field for the approved/total ratio; sort by
                          // total column count.
                          sortingComparator: (
                            a: TableSummary,
                            b: TableSummary,
                          ) => (a.columnCount ?? 0) - (b.columnCount ?? 0),
                          cell: (item: TableSummary) => {
                            const total = item.columnCount ?? 0;
                            const approved = item.columnsApproved ?? 0;
                            const allApproved = approved >= total && total > 0;
                            return (
                              <StatusIndicator
                                type={allApproved ? "success" : "warning"}
                              >
                                {`${approved}/${total}`}
                              </StatusIndicator>
                            );
                          },
                        },
                        {
                          id: "enriched",
                          header: t("Enriched"),
                          sortingField: "enrichmentSource",
                          cell: (item: TableSummary) =>
                            item.enrichmentSource ? (
                              <Badge color="blue">
                                {item.enrichmentSource}
                              </Badge>
                            ) : (
                              "—"
                            ),
                        },
                        {
                          id: "status",
                          header: t("Review status"),
                          sortingField: "reviewStatus",
                          cell: (item: TableSummary) => {
                            const s = item.reviewStatus as string;
                            const type =
                              s === ReviewStatus.APPROVED
                                ? "success"
                                : s === ReviewStatus.REJECTED
                                  ? "error"
                                  : "warning";
                            const label =
                              s === ReviewStatus.APPROVED
                                ? t("Approved")
                                : s === ReviewStatus.REJECTED
                                  ? t("Rejected")
                                  : t("Pending review");
                            return (
                              <SpaceBetween size="xxs">
                                <StatusIndicator type={type}>
                                  {label}
                                </StatusIndicator>
                                {item.added && (
                                  <Badge color="green">{t("New")}</Badge>
                                )}
                                {item.pendingDeletion && (
                                  <SpaceBetween
                                    direction="horizontal"
                                    size="xs"
                                  >
                                    <Badge color="red">
                                      {t("Pending deletion")}
                                    </Badge>
                                    <Button
                                      variant="inline-link"
                                      loading={keepingTableId === item.tableId}
                                      disabled={keepMutation.isPending}
                                      onClick={() =>
                                        handleKeepTable(item.tableId ?? "")
                                      }
                                    >
                                      {t("Keep")}
                                    </Button>
                                  </SpaceBetween>
                                )}
                                {(item.columnsPendingDeletion ?? 0) > 0 && (
                                  <Badge color="red">
                                    {t(
                                      item.columnsPendingDeletion === 1
                                        ? "{count} column pending deletion"
                                        : "{count} columns pending deletion",
                                      {
                                        count: item.columnsPendingDeletion ?? 0,
                                      },
                                    )}
                                  </Badge>
                                )}
                                {(item.pendingRelationships ?? 0) > 0 && (
                                  <Badge color="blue">
                                    {t(
                                      item.pendingRelationships === 1
                                        ? "{count} relationship pending review"
                                        : "{count} relationships pending review",
                                      { count: item.pendingRelationships ?? 0 },
                                    )}
                                  </Badge>
                                )}
                              </SpaceBetween>
                            );
                          },
                        },
                      ]}
                      filter={
                        <SpaceBetween direction="horizontal" size="xs">
                          <TextFilter
                            {...filterProps}
                            filteringPlaceholder={t("Find tables")}
                            filteringAriaLabel={t("Filter tables")}
                          />
                          <Select
                            selectedOption={
                              statusFilter
                                ? { value: statusFilter, label: statusFilter }
                                : { value: "", label: t("All statuses") }
                            }
                            onChange={({ detail }) =>
                              setStatusFilter(
                                detail.selectedOption.value || null,
                              )
                            }
                            options={[
                              { value: "", label: t("All statuses") },
                              {
                                value: ReviewStatus.PENDING_REVIEW,
                                label: t("Pending review"),
                              },
                              {
                                value: ReviewStatus.APPROVED,
                                label: t("Approved"),
                              },
                              {
                                value: ReviewStatus.REJECTED,
                                label: t("Rejected"),
                              },
                            ]}
                            placeholder={t("Filter by status")}
                          />
                        </SpaceBetween>
                      }
                      pagination={<Pagination {...paginationProps} />}
                      header={
                        <Header
                          variant="h2"
                          counter={`(${tables.length}${tablesLoadingMore ? "+" : ""})`}
                          // Text, not just a spinner: a partial list that looks
                          // complete is worse than a slow one, and screen-reader
                          // users get no signal from a loading glyph alone.
                          description={
                            tablesLoadingMore
                              ? t(
                                  "Still loading tables — the list below is incomplete.",
                                )
                              : undefined
                          }
                          actions={
                            <SpaceBetween direction="horizontal" size="xs">
                              <Button
                                onClick={() =>
                                  handleBatchReviewTables(
                                    ReviewDecision.REJECTED,
                                  )
                                }
                                loading={isBatchReviewing}
                                disabled={selectedTables.length === 0}
                              >
                                {t("Reject selected")}
                              </Button>
                              <Button
                                onClick={() =>
                                  handleBatchReviewTables(
                                    ReviewDecision.APPROVED,
                                  )
                                }
                                loading={isBatchReviewing}
                                disabled={selectedTables.length === 0}
                              >
                                {t("Approve selected")}
                              </Button>
                            </SpaceBetween>
                          }
                        >
                          {t("Tables")}
                        </Header>
                      }
                      empty={
                        <Box
                          textAlign="center"
                          color="text-status-inactive"
                          padding="l"
                        >
                          {status === "SCANNING" || status === "ENRICHING"
                            ? t(
                                "Tables will appear here once the scan completes.",
                              )
                            : status === "REGISTERED"
                              ? t("Scan has not started yet.")
                              : t("No tables discovered.")}
                        </Box>
                      }
                    />
                  </SpaceBetween>
                ),
              },
              {
                id: "history",
                label: t("Scan history"),
                content: (
                  <ScanHistoryTable
                    createdAt={source.createdAt}
                    updatedAt={source.updatedAt}
                    lastScanAt={dbDetails?.lastScanAt}
                    status={status}
                    tablesDiscovered={dbDetails?.tablesDiscovered ?? 0}
                    tablesApproved={dbDetails?.tablesApproved ?? 0}
                    namespaceId={namespaceId ?? ""}
                    sourceId={sourceId ?? ""}
                    lastScanJobId={dbDetails?.lastScanJobId}
                  />
                ),
              },
              {
                id: "settings",
                label: t("Settings"),
                content: (
                  <SpaceBetween size="l">
                    <Container
                      header={
                        <Header variant="h2">{t("Connection settings")}</Header>
                      }
                    >
                      {dbDetails?.glueConfiguration && (
                        <KeyValuePairs
                          columns={2}
                          items={[
                            {
                              label: t("Catalog ID"),
                              value: String(
                                dbDetails.glueConfiguration.catalogId ?? "—",
                              ),
                            },
                            {
                              label: t("Region"),
                              value: String(
                                dbDetails.glueConfiguration.region ?? "—",
                              ),
                            },
                            {
                              label: t("Database"),
                              value: String(
                                dbDetails.glueConfiguration.databaseName ?? "—",
                              ),
                            },
                            {
                              label: t("Table filter"),
                              value: String(
                                dbDetails.glueConfiguration.tableFilter ??
                                  "(none)",
                              ),
                            },
                          ]}
                        />
                      )}
                      {dbDetails?.jdbcConfiguration && (
                        <KeyValuePairs
                          columns={2}
                          items={[
                            {
                              label: t("Engine"),
                              value: String(
                                dbDetails.jdbcConfiguration.engine ?? "—",
                              ),
                            },
                            {
                              label: t("Host"),
                              value: String(
                                dbDetails.jdbcConfiguration.host ?? "—",
                              ),
                            },
                            {
                              label: t("Port"),
                              value: String(
                                dbDetails.jdbcConfiguration.port ?? "—",
                              ),
                            },
                            {
                              label: t("Database"),
                              value: String(
                                dbDetails.jdbcConfiguration.databaseName ?? "—",
                              ),
                            },
                            {
                              label: t("Schema filter"),
                              value: String(
                                dbDetails.jdbcConfiguration.schemaFilter ??
                                  "(none)",
                              ),
                            },
                            {
                              label: t("Table filter"),
                              value: String(
                                dbDetails.jdbcConfiguration.tableFilter ??
                                  "(none)",
                              ),
                            },
                            {
                              label: t("Auth"),
                              value: "Secrets Manager",
                            },
                          ]}
                        />
                      )}
                      {dbDetails?.customConnectorConfiguration && (
                        <KeyValuePairs
                          columns={2}
                          items={[
                            {
                              // One ARN, because CustomConnectorConfiguration models one: the
                              // connector Lambda serves both the metadata and record
                              // paths. Athena's split metadata/record pair is not
                              // offered anywhere — see that Smithy shape for why.
                              label: t("Connector function ARN"),
                              value: String(
                                dbDetails.customConnectorConfiguration
                                  .connectorFunctionArn ?? "—",
                              ),
                            },
                            {
                              label: t("Database"),
                              value: String(
                                dbDetails.customConnectorConfiguration
                                  .databaseName ?? "—",
                              ),
                            },
                            {
                              label: t("Table filter"),
                              value: String(
                                dbDetails.customConnectorConfiguration
                                  .tableFilter ?? "(none)",
                              ),
                            },
                            {
                              label: t("Table exclude filter"),
                              value: String(
                                dbDetails.customConnectorConfiguration
                                  .tableExcludeFilter ?? "(none)",
                              ),
                            },
                          ]}
                        />
                      )}
                      {dbDetails?.databricksSqlWarehouseConfiguration && (
                        <KeyValuePairs
                          columns={2}
                          items={[
                            {
                              label: t("Workspace hostname"),
                              value: String(
                                dbDetails.databricksSqlWarehouseConfiguration
                                  .workspaceHostname ?? "—",
                              ),
                            },
                            {
                              label: t("HTTP path"),
                              value: String(
                                dbDetails.databricksSqlWarehouseConfiguration
                                  .httpPath ?? "—",
                              ),
                            },
                            {
                              label: t("Unity Catalog catalog"),
                              value: String(
                                dbDetails.databricksSqlWarehouseConfiguration
                                  .databricksCatalog ?? "—",
                              ),
                            },
                            {
                              label: t("Unity Catalog schema"),
                              value: String(
                                dbDetails.databricksSqlWarehouseConfiguration
                                  .databaseName ?? "—",
                              ),
                            },
                            // Role before secret: the credential is read only as a session
                            // assumed from the role. Neither ARN is itself a secret.
                            {
                              label: t(
                                "Credential access role (assumed to read the secret)",
                              ),
                              value: String(
                                dbDetails.databricksSqlWarehouseConfiguration
                                  .crossAccountRoleArn ?? "—",
                              ),
                            },
                            {
                              label: t(
                                "Credential secret (read through that role)",
                              ),
                              value: String(
                                dbDetails.databricksSqlWarehouseConfiguration
                                  .credentialSecretArn ?? "—",
                              ),
                            },
                            {
                              label: t("Table filter"),
                              value: String(
                                dbDetails.databricksSqlWarehouseConfiguration
                                  .tableFilter ?? t("(none)"),
                              ),
                            },
                            {
                              label: t("Table exclude filter"),
                              value: String(
                                dbDetails.databricksSqlWarehouseConfiguration
                                  .tableExcludeFilter ?? t("(none)"),
                              ),
                            },
                          ]}
                        />
                      )}
                    </Container>
                    <RescanScheduleControl
                      namespaceId={namespaceId ?? ""}
                      sourceId={sourceId ?? ""}
                      isGlue={source.sourceSubType === "GLUE_DATABASE"}
                      schedule={dbDetails?.rescanSchedule}
                      eventRescan={dbDetails?.eventRescan}
                    />
                  </SpaceBetween>
                ),
              },
            ]}
          />
        </>
      )}

      {/* ── DOCUMENTS layout — matches DocSourceDetail ── */}
      {isDocuments && (
        <>
          <Container header={<Header variant="h2">{t("Overview")}</Header>}>
            <ColumnLayout columns={3} variant="text-grid">
              <ValueWithLabel label={t("Status")}>
                <StatusIndicator type={sourceStatusType(status)}>
                  {localizedSourceStatus(status, t)}
                </StatusIndicator>
              </ValueWithLabel>
              <ValueWithLabel label={t("Source")}>
                {source.sourceSubType === "LOCAL_UPLOAD"
                  ? t("File upload")
                  : t("S3 bucket")}
              </ValueWithLabel>
              {source.sourceSubType !== "LOCAL_UPLOAD" && (
                <>
                  <ValueWithLabel label={t("S3 Prefixes")}>
                    {docDetails?.s3Prefixes &&
                    docDetails.s3Prefixes.length > 0 ? (
                      docDetails.s3Prefixes.map((p, i) => (
                        <div key={i}>
                          <Box variant="code">{p}</Box>
                        </div>
                      ))
                    ) : (
                      <span>{t("— (entire bucket)")}</span>
                    )}
                  </ValueWithLabel>
                  <ValueWithLabel label={t("Source Bucket ARN")}>
                    {String(docDetails?.sourceBucketArn ?? "—")}
                  </ValueWithLabel>
                  <ValueWithLabel label={t("Cross-account Role")}>
                    {String(docDetails?.roleArn ?? "—")}
                  </ValueWithLabel>
                </>
              )}
              <ValueWithLabel label={t("Created")}>
                {formatTimestamp(source.createdAt)}
              </ValueWithLabel>
              <ValueWithLabel label={t("Last Updated")}>
                {formatTimestamp(source.updatedAt)}
              </ValueWithLabel>
            </ColumnLayout>
          </Container>

          <Container
            header={<Header variant="h2">{t("Ingestion Statistics")}</Header>}
          >
            <ColumnLayout columns={3} variant="text-grid">
              <ValueWithLabel label={t("Total Files")}>
                {String(docDetails?.filesTotal ?? "—")}
              </ValueWithLabel>
              <ValueWithLabel label={t("Skipped")}>
                {String(docDetails?.filesSkipped ?? "—")}
              </ValueWithLabel>
              <ValueWithLabel label={t("Errored")}>
                {String(docDetails?.filesErrored ?? "—")}
              </ValueWithLabel>
              <ValueWithLabel label={t("Documents Processed")}>
                {String(docDetails?.documentsProcessed ?? "—")}
              </ValueWithLabel>
              <ValueWithLabel label={t("Text Chunks AI Processed")}>
                {String(docDetails?.chunksLLM ?? "—")}
              </ValueWithLabel>
              <ValueWithLabel label={t("Text Chunks Embeddings")}>
                {String(docDetails?.chunksEmbed ?? "—")}
              </ValueWithLabel>
              <ValueWithLabel label={t("Text Chunks Added To Graph")}>
                {String(docDetails?.chunksGraph ?? "—")}
              </ValueWithLabel>
            </ColumnLayout>
          </Container>

          {preprocessingIssues.length > 0 && (
            <Table
              header={
                <Header
                  variant="h2"
                  counter={`(${preprocessingIssues.length})`}
                >
                  {t("File Issues")}
                </Header>
              }
              columnDefinitions={[
                {
                  id: "filename",
                  header: t("Filename"),
                  cell: (item) => item.filename,
                },
                {
                  id: "type",
                  header: t("Type"),
                  cell: (item) => (
                    <StatusIndicator
                      type={item.type === "error" ? "error" : "warning"}
                    >
                      {item.type}
                    </StatusIndicator>
                  ),
                },
                {
                  id: "reason",
                  header: t("Reason"),
                  cell: (item) => item.reason,
                },
              ]}
              items={preprocessingIssues}
            />
          )}

          {extractionConfig && (
            <Container
              header={<Header variant="h2">{t("Advanced options")}</Header>}
            >
              <ColumnLayout columns={2} variant="text-grid">
                <ValueWithLabel label={t("Versioning")}>
                  {(extractionConfig.enableVersioning as boolean)
                    ? t("Enabled")
                    : t("Disabled")}
                </ValueWithLabel>
                <ValueWithLabel label={t("Delete previous versions")}>
                  {(extractionConfig.deletePrevVersions as boolean)
                    ? t("Yes")
                    : t("No")}
                </ValueWithLabel>
              </ColumnLayout>
            </Container>
          )}
        </>
      )}

      {deleteError && (
        <Alert type="error" header={t("Failed to delete source")}>
          {deleteError.message}
        </Alert>
      )}

      {showDeleteModal && (
        <Modal
          visible
          onDismiss={() => setShowDeleteModal(false)}
          header={
            isDocuments ? t("Delete document source") : t("Delete data source")
          }
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  onClick={() => setShowDeleteModal(false)}
                >
                  {t("Cancel")}
                </Button>
                <Button
                  variant="normal"
                  loading={isDeleting}
                  onClick={() => {
                    setShowDeleteModal(false);
                    deleteSource();
                  }}
                >
                  {t("Delete")}
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <Box>
              {isDocuments
                ? t(
                    "Permanently delete {name}? This will remove all associated files from S3, the DynamoDB record, and the knowledge graph data from Neptune and OpenSearch.",
                    { name: source.name ?? "" },
                  )
                : t(
                    "Permanently delete {name}? This will remove all associated data.",
                    { name: source.name ?? "" },
                  )}
            </Box>
            <Alert type="warning">{t("This action cannot be undone.")}</Alert>
          </SpaceBetween>
        </Modal>
      )}

      {showRescanConfirm && (
        <Modal
          visible
          onDismiss={() => setShowRescanConfirm(false)}
          header={t("Discard the open re-scan review?")}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  onClick={() => setShowRescanConfirm(false)}
                >
                  {t("Cancel")}
                </Button>
                <Button
                  variant="primary"
                  loading={isRescanning}
                  onClick={() => {
                    setShowRescanConfirm(false);
                    rescan({ confirmDiscardOpenReview: true });
                  }}
                >
                  {t("Discard and re-scan")}
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <Box>
              <b>{source.name}</b>
              {t(
                " has a re-scan waiting for your review. A new re-scan compares the database against the last version you approved, so anything you already approved, rejected, or edited inside this review is discarded.",
              )}
            </Box>
            <Alert type="info">
              {t(
                "Your approved tables and columns are not affected. To keep the decisions you have made in this review instead, cancel and finish reviewing it first.",
              )}
            </Alert>
          </SpaceBetween>
        </Modal>
      )}
    </SpaceBetween>
  );
};

// ── Scan history (database sources) ──────────────────────────────────────────
//
// Rows come from the ListSourceScanJobs event store: discovery/enrichment scan
// rows plus steward approve/reject (REVIEW) events, newest first. When the store
// is empty or the call errors we fall back to a lightweight view derived from
// the detail record (createdAt / updatedAt / lastScanAt + current status) so the
// tab never regresses to blank. The public ScanHistoryEntry shape and the
// Time/Status/Details columns are unchanged.

interface ScanHistoryEntry {
  id: string;
  at: string; // ISO timestamp (normalized from Date | string)
  kind: "registered" | "scan" | "review" | "deletion";
  type: StatusIndicatorProps["type"];
  label: string;
  detail: string;
  /** INITIAL / MANUAL / SCHEDULED / EVENT. Absent on review and legacy rows. */
  trigger?: string;
}

function toIso(value: Date | string | undefined): string | undefined {
  if (!value) return undefined;
  return value instanceof Date ? value.toISOString() : value;
}

// Map one ListSourceScanJobs SCAN row to a history entry. A row with no
// explicit eventType is treated as a SCAN (legacy rows predate review events).
function scanEntryToRow(
  entry: ScanJobEntry,
  index: number,
  t: AppTranslate,
): ScanHistoryEntry {
  const at = toIso(entry.at) ?? "";
  const id = `scan-${at}-${index}`;
  const tables = entry.tablesDiscovered ?? 0;
  const status = entry.status ?? "";
  const trigger = entry.triggerType ?? undefined;
  if (status === "FAILED") {
    return {
      id,
      at,
      kind: "scan",
      trigger,
      type: "error",
      label: t("Scan failed"),
      detail: entry.errorMessage
        ? t("Error: {message}", { message: entry.errorMessage })
        : t("The scan did not complete. Use Re-scan to try again."),
    };
  }
  if (
    status === "IN_PROGRESS" ||
    status === "DISCOVERING" ||
    status === "ENRICHING"
  ) {
    return {
      id,
      at,
      kind: "scan",
      trigger,
      type: "in-progress",
      label: t("Scan in progress"),
      detail: t("The scan or enrichment pipeline is still running."),
    };
  }
  if (status === "CANCELLED") {
    return {
      id,
      at,
      kind: "scan",
      trigger,
      type: "stopped",
      label: t("Scan cancelled"),
      detail: t("The scan was cancelled."),
    };
  }
  return {
    id,
    at,
    kind: "scan",
    trigger,
    type: "success",
    label: t("Scan completed"),
    detail: t(
      tables === 1 ? "Scanned {count} table." : "Scanned {count} tables.",
      { count: tables },
    ),
  };
}

// Map one ListSourceScanJobs REVIEW row (an approve/reject decision) to a
// history entry. The label distinguishes an initial approve/reject from a
// re-scan resolution; a *_FAILED terminal status renders as an error.
function reviewEntryToRow(
  entry: ScanJobEntry,
  index: number,
  t: AppTranslate,
): ScanHistoryEntry {
  const at = toIso(entry.at) ?? "";
  const id = `review-${at}-${index}`;
  const approved = entry.decision === "APPROVED";
  const isRescan = entry.isRescan === true;
  const failed = (entry.status ?? "").endsWith("_FAILED");
  const label = isRescan
    ? approved
      ? t("Re-scan approved")
      : t("Re-scan rejected")
    : approved
      ? t("Source approved")
      : t("Source rejected");
  const count = entry.tablesApproved ?? 0;
  let detail: string;
  if (approved) {
    detail = t(
      count === 1 ? "{count} table approved." : "{count} tables approved.",
      { count },
    );
  } else if (isRescan) {
    detail = t("Re-scan discarded; the previously approved tables were kept.");
  } else {
    detail = t("Source rejected; no tables were published.");
  }
  return {
    id,
    at,
    kind: "review",
    type: failed ? "error" : "success",
    label,
    detail,
  };
}

function buildScanHistory(args: {
  scanJobs?: ScanJobEntry[];
  createdAt?: Date | string;
  updatedAt?: Date | string;
  lastScanAt?: Date | string;
  status: string;
  tablesDiscovered: number;
  tablesApproved: number;
  t: AppTranslate;
  errorMessage?: string;
  /** Tables lost to a per-table read failure while the scan itself succeeded. */
  tablesFailed?: number;
}): ScanHistoryEntry[] {
  // Normalize Smithy ``Date`` timestamps to ISO strings so the rest of the
  // function can build stable ids and sort lexicographically.
  const createdAt = toIso(args.createdAt);
  const updatedAt = toIso(args.updatedAt);
  const lastScanAt = toIso(args.lastScanAt);

  // Preferred path: build rows from the real event store when it has any.
  if (args.scanJobs && args.scanJobs.length > 0) {
    const rows: ScanHistoryEntry[] = [];
    if (createdAt) {
      rows.push({
        id: `created-${createdAt}`,
        at: createdAt,
        kind: "registered",
        type: "info",
        label: args.t("Source registered"),
        detail: args.t("Source created and queued for initial scan."),
      });
    }
    args.scanJobs.forEach((entry, i) => {
      rows.push(
        entry.eventType === "REVIEW"
          ? reviewEntryToRow(entry, i, args.t)
          : scanEntryToRow(entry, i, args.t),
      );
    });
    // Most recent first so stewards see the latest state at the top.
    return rows.sort((a, b) => (a.at < b.at ? 1 : -1));
  }

  // Fallback: reconstruct a lightweight view from the detail record so the tab
  // never regresses to blank when the store is empty or the call errored.
  const events: ScanHistoryEntry[] = [];

  if (createdAt) {
    events.push({
      id: `created-${createdAt}`,
      at: createdAt,
      kind: "registered",
      type: "info",
      label: args.t("Source registered"),
      detail: args.t("Source created and queued for initial scan."),
    });
  }

  if (lastScanAt) {
    if (args.status === SourceStatus.SCAN_FAILED) {
      events.push({
        id: `scan-${lastScanAt}`,
        at: lastScanAt,
        kind: "scan",
        type: "error",
        label: args.t("Scan failed"),
        detail: args.errorMessage
          ? args.t("Error: {message}", { message: args.errorMessage })
          : args.t(
              "The most recent scan did not complete. Use Re-scan to try again.",
            ),
      });
    } else if (
      args.status === SourceStatus.PENDING_REVIEW ||
      args.status === SourceStatus.APPROVING ||
      args.status === SourceStatus.REJECTING ||
      args.status === SourceStatus.APPROVED ||
      args.status === SourceStatus.APPROVAL_FAILED ||
      args.status === SourceStatus.REJECTION_FAILED
    ) {
      const scanned = args.t(
        args.tablesDiscovered === 1
          ? "Scanned {count} table."
          : "Scanned {count} tables.",
        { count: args.tablesDiscovered },
      );
      const lost = args.tablesFailed ?? 0;
      events.push({
        id: `scan-${lastScanAt}`,
        at: lastScanAt,
        kind: "scan",
        // A scan that dropped tables completed, but not cleanly — flag it so
        // the activity log doesn't read as a clean success.
        type: lost > 0 ? "warning" : "success",
        label: args.t("Scan completed"),
        detail:
          lost > 0
            ? args.t(
                lost === 1
                  ? "{scanned} {count} table could not be read and was skipped."
                  : "{scanned} {count} tables could not be read and were skipped.",
                { scanned, count: lost },
              )
            : scanned,
      });
    } else if (
      args.status === SourceStatus.SCANNING ||
      args.status === SourceStatus.ENRICHING
    ) {
      events.push({
        id: `scan-${lastScanAt}`,
        at: lastScanAt,
        kind: "scan",
        type: "in-progress",
        label: args.t("Scan in progress"),
        detail: args.t("The scan or enrichment pipeline is still running."),
      });
    }
  }

  if (
    args.status === SourceStatus.APPROVED &&
    args.tablesApproved > 0 &&
    updatedAt
  ) {
    events.push({
      id: `review-${updatedAt}`,
      at: updatedAt,
      kind: "review",
      type: "success",
      label: args.t("All tables approved"),
      detail: args.t("{approved} of {discovered} tables approved.", {
        approved: args.tablesApproved,
        discovered: args.tablesDiscovered,
      }),
    });
  }

  if (args.status === SourceStatus.DELETING && updatedAt) {
    events.push({
      id: `delete-${updatedAt}`,
      at: updatedAt,
      kind: "deletion",
      type: "in-progress",
      label: args.t("Deletion in progress"),
      detail: args.t("The source and all associated data are being removed."),
    });
  }

  // Most recent first so stewards see the latest state at the top.
  return events.sort((a, b) => (a.at < b.at ? 1 : -1));
}

interface ScanHistoryTableProps {
  createdAt?: Date | string;
  updatedAt?: Date | string;
  lastScanAt?: Date | string;
  status: string;
  tablesDiscovered: number;
  tablesApproved: number;
  namespaceId: string;
  sourceId: string;
  lastScanJobId?: string;
}

const ScanHistoryTable: React.FC<ScanHistoryTableProps> = (props) => {
  const { t } = useAppI18n();
  // Real event store: scan + review rows, newest first.
  const { data: scanJobs } = useListSourceScanJobs(
    props.namespaceId,
    props.sourceId,
  );

  // Fetch the scan job for its errorMessage when the scan failed, and for its
  // per-table failure count when it succeeded — a scan that completed can still
  // have dropped tables, and only the scan job records that. It also feeds the
  // derived fallback used when the event store is empty or the list call errored.
  const { data: scanJob } = useGetSourceScanJob(
    props.namespaceId,
    props.sourceId,
    props.lastScanJobId ?? "",
  );
  const tablesFailed = readDegradedScan(scanJob)?.tablesFailed;

  const events = React.useMemo(
    () =>
      buildScanHistory({
        scanJobs: scanJobs?.items,
        createdAt: props.createdAt,
        updatedAt: props.updatedAt,
        lastScanAt: props.lastScanAt,
        status: props.status,
        tablesDiscovered: props.tablesDiscovered,
        tablesApproved: props.tablesApproved,
        t,
        errorMessage: scanJob?.errorMessage,
        tablesFailed,
      }),
    [
      scanJobs?.items,
      props.createdAt,
      props.updatedAt,
      props.lastScanAt,
      props.status,
      props.tablesDiscovered,
      props.tablesApproved,
      t,
      scanJob?.errorMessage,
      tablesFailed,
    ],
  );

  return (
    <Table<ScanHistoryEntry>
      variant="container"
      items={events}
      trackBy="id"
      columnDefinitions={[
        {
          id: "at",
          header: t("Time"),
          isRowHeader: true,
          cell: (e) => formatTimestamp(e.at),
          minWidth: 180,
        },
        {
          id: "status",
          header: t("Status"),
          cell: (e) => (
            <StatusIndicator type={e.type}>{e.label}</StatusIndicator>
          ),
          minWidth: 200,
        },
        {
          id: "trigger",
          header: t("Trigger"),
          cell: (e) => e.trigger ?? "-",
          minWidth: 120,
        },
        {
          id: "detail",
          header: t("Details"),
          cell: (e) => e.detail,
        },
      ]}
      header={
        <Header variant="h2" counter={`(${events.length})`}>
          {t("Activity")}
        </Header>
      }
      empty={
        <Box textAlign="center" color="text-status-inactive" padding="l">
          {t("No activity yet.")}
        </Box>
      }
    />
  );
};

interface RescanScheduleControlProps {
  namespaceId: string;
  sourceId: string;
  isGlue: boolean;
  schedule?: RescanSchedule;
  eventRescan?: EventRescanConfig;
}

/**
 * Configure a recurring rescan cadence (EventBridge Scheduler). When enabled,
 * the source is automatically re-scanned on the given schedule; the schema-diff
 * keeps it non-destructive.
 */
const RescanScheduleControl: React.FC<RescanScheduleControlProps> = ({
  namespaceId,
  sourceId,
  isGlue,
  schedule,
  eventRescan,
}) => {
  const { t } = useAppI18n();
  const [enabled, setEnabled] = useState<boolean>(schedule?.enabled ?? false);
  const [expression, setExpression] = useState<string>(
    schedule?.scheduleExpression ?? "rate(1 day)",
  );
  const [timezone, setTimezone] = useState<string>(schedule?.timezone ?? "UTC");
  const { mutate, isPending, error, isSuccess } = usePutSourceRescanSchedule(
    namespaceId,
    sourceId,
  );

  const [eventEnabled, setEventEnabled] = useState<boolean>(
    eventRescan?.enabled ?? false,
  );
  const {
    mutate: setEventRescan,
    isPending: eventPending,
    error: eventError,
  } = usePutSourceEventRescan(namespaceId, sourceId);
  // Set by every edit below. useGetSource polls every 10s, so a sync that
  // ignored this would overwrite an expression the user is still typing.
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (dirty) return;
    setEnabled(schedule?.enabled ?? false);
    setExpression(schedule?.scheduleExpression ?? "rate(1 day)");
    setTimezone(schedule?.timezone ?? "UTC");
  }, [
    dirty,
    schedule?.enabled,
    schedule?.scheduleExpression,
    schedule?.timezone,
  ]);

  // Hand ownership back to the server value once the save lands.
  useEffect(() => {
    if (isSuccess) setDirty(false);
  }, [isSuccess]);

  // No save button on this one, so it only needs holding still in flight.
  useEffect(() => {
    if (!eventPending) setEventEnabled(eventRescan?.enabled ?? false);
  }, [eventPending, eventRescan?.enabled]);

  const save = () =>
    mutate({
      enabled,
      scheduleExpression: enabled ? expression : undefined,
      timezone: enabled ? timezone : undefined,
    });

  return (
    <Container
      header={
        <Header
          variant="h2"
          description={t(
            "Automatically re-scan this source on a recurring cadence to pick up upstream schema changes. Unchanged tables keep their enrichment and review status.",
          )}
        >
          {t("Automatic rescans")}
        </Header>
      }
    >
      <SpaceBetween size="m">
        {error && <Alert type="error">{error.message}</Alert>}
        {isSuccess && (
          <Alert type="success">{t("Rescan schedule saved.")}</Alert>
        )}
        <Toggle
          checked={enabled}
          onChange={({ detail }) => {
            setEnabled(detail.checked);
            setDirty(true);
          }}
        >
          {t("Enable scheduled rescans")}
        </Toggle>
        {enabled && (
          <>
            <FormField
              label={t("Schedule expression")}
              description={t(
                "EventBridge expression, e.g. rate(1 day) or cron(0 3 * * ? *).",
              )}
            >
              <Input
                value={expression}
                onChange={({ detail }) => {
                  setExpression(detail.value);
                  setDirty(true);
                }}
                placeholder="rate(1 day)"
              />
            </FormField>
            <FormField
              label={t("Timezone")}
              description={t(
                "IANA timezone for cron evaluation (e.g. America/New_York).",
              )}
            >
              <Input
                value={timezone}
                onChange={({ detail }) => {
                  setTimezone(detail.value);
                  setDirty(true);
                }}
                placeholder="UTC"
              />
            </FormField>
          </>
        )}
        <Box>
          <Button variant="primary" loading={isPending} onClick={save}>
            {t("Save schedule")}
          </Button>
        </Box>
        {isGlue && eventError && (
          <Alert
            type="error"
            header={t("Could not change event-driven rescans")}
          >
            {eventError.message}
          </Alert>
        )}
        {isGlue && (
          <FormField
            label={t("Event-driven rescans")}
            description={t(
              "Re-scan automatically when this source's Glue Data Catalog changes (new/changed tables). Bursts are coalesced.",
            )}
          >
            <Toggle
              checked={eventEnabled}
              disabled={eventPending}
              onChange={({ detail }) => {
                setEventEnabled(detail.checked);
                setEventRescan(detail.checked);
              }}
            >
              {t("Re-scan on Glue catalog changes")}
            </Toggle>
          </FormField>
        )}
      </SpaceBetween>
    </Container>
  );
};
