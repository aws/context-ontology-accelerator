// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useNamespace } from "@api-hooks/use-namespace";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import TextFilter from "@cloudscape-design/components/text-filter";
import Select from "@cloudscape-design/components/select";
import type { SelectProps } from "@cloudscape-design/components/select";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import CollectionPreferences from "@cloudscape-design/components/collection-preferences";
import type { CollectionPreferencesProps } from "@cloudscape-design/components/collection-preferences";
import Pagination from "@cloudscape-design/components/pagination";
import Alert from "@cloudscape-design/components/alert";
import Container from "@cloudscape-design/components/container";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import { useListSources, SourceSummary } from "@api-hooks";
import { formatTimestamp } from "@utils/helpers";
import { sourceStatusType, sourceStatusLabel } from "@utils/source-status";
import { useAppI18n } from "../i18n/LocaleProvider";
import type { AppMessage } from "../i18n/appMessages";

// ── Type labels ──────────────────────────────────────────────────────────────

const SOURCE_TYPE_LABELS: Record<string, AppMessage> = {
  DATABASE: "Database",
  DOCUMENTS: "Documents",
};
const SOURCE_SUBTYPE_LABELS: Record<string, AppMessage> = {
  GLUE_DATABASE: "Glue",
  JDBC_DATABASE: "JDBC",
  CUSTOM_CONNECTOR: "Custom connector",
  DATABRICKS_SQL_WAREHOUSE: "Databricks",
  S3: "S3",
  LOCAL_UPLOAD: "Upload",
};

function sourceTypeLabel(
  sourceType: string,
  sourceSubType: string,
  t: ReturnType<typeof useAppI18n>["t"],
): string {
  const typeKey = SOURCE_TYPE_LABELS[sourceType];
  const subtypeKey = SOURCE_SUBTYPE_LABELS[sourceSubType];
  return `${typeKey ? t(typeKey) : sourceType} · ${
    subtypeKey ? t(subtypeKey) : sourceSubType
  }`;
}

const LOCALIZED_SOURCE_STATUS_LABELS: Readonly<Record<string, AppMessage>> = {
  Approved: "Approved",
  Completed: "Completed",
  "Pending review": "Pending review",
  "Re-scan review": "Re-scan review",
  Registered: "Registered",
  Scanning: "Scanning",
  Enriching: "Enriching",
  "Approving…": "Approving…",
  "Rejecting…": "Rejecting…",
  "Approval failed": "Approval failed",
  "Rejection failed": "Rejection failed",
  "Scan failed": "Scan failed",
  "Delete failed": "Delete failed",
  Deleting: "Deleting",
  Deleted: "Deleted",
  Rejected: "Rejected",
};

function localizedSourceStatus(
  status: string | undefined,
  t: ReturnType<typeof useAppI18n>["t"],
): string {
  const label = sourceStatusLabel(status);
  const message = LOCALIZED_SOURCE_STATUS_LABELS[label];
  return message ? t(message) : label;
}

// ── Type filter options ───────────────────────────────────────────────────────

// Priority order for summary dashboard
const SUMMARY_STATUS_PRIORITY = [
  "PENDING_REVIEW",
  "SCAN_FAILED",
  "SCANNING",
  "APPROVED",
  "REGISTERED",
  "ENRICHING",
  "COMPLETED",
];

// ── Preferences ───────────────────────────────────────────────────────────────

const PAGE_SIZE_OPTIONS: Array<
  CollectionPreferencesProps.PageSizeOption & { label: AppMessage }
> = [
  { value: 10, label: "10 sources" },
  { value: 20, label: "20 sources" },
  { value: 50, label: "50 sources" },
];

const COLUMN_DISPLAY_OPTIONS: Array<{
  label: AppMessage;
  options: Array<{ id: string; label: AppMessage; editable?: boolean }>;
}> = [
  {
    label: "Source properties",
    options: [
      { id: "name", label: "Name", editable: false },
      { id: "status", label: "Status" },
      { id: "sourceType", label: "Type" },
      { id: "createdAt", label: "Created" },
      { id: "updatedAt", label: "Last updated" },
    ],
  },
];

const DEFAULT_PREFERENCES: CollectionPreferencesProps.Preferences = {
  pageSize: 20,
  visibleContent: ["name", "status", "sourceType", "createdAt", "updatedAt"],
  wrapLines: false,
  stripedRows: true,
};

// ── Sorting ───────────────────────────────────────────────────────────────────

type SortingColumn = { sortingField: string };

function sortItems(
  items: SourceSummary[],
  sortingColumn: SortingColumn | undefined,
  isDescending: boolean,
): SourceSummary[] {
  if (!sortingColumn) return items;
  const field = sortingColumn.sortingField as keyof SourceSummary;
  return [...items].sort((a, b) => {
    const av = a[field] ?? "";
    const bv = b[field] ?? "";
    let cmp: number;
    if (av instanceof Date && bv instanceof Date) {
      cmp = av.getTime() - bv.getTime();
    } else if (typeof av === "number" && typeof bv === "number") {
      cmp = av - bv;
    } else {
      cmp = String(av).localeCompare(String(bv));
    }
    return isDescending ? -cmp : cmp;
  });
}

// ── Component ─────────────────────────────────────────────────────────────────

export const SourceList: React.FC = () => {
  const { namespaceId } = useParams<{ namespaceId: string }>();
  const navigate = useNavigate();
  const { t } = useAppI18n();
  const sourceTypeSegments = [
    { id: "", text: t("All") },
    { id: "DATABASE", text: t("Database") },
    { id: "DOCUMENTS", text: t("Documents") },
  ];

  const { name: nsName } = useNamespace(namespaceId);
  useBreadcrumbTrail([
    { text: nsName, href: `/namespaces/${namespaceId}`, namespaceId },
    { text: t("Sources") },
  ]);

  const [filterText, setFilterText] = useState("");
  const [typeFilter, setTypeFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<SelectProps.Option>({
    label: "All statuses",
    value: "",
  });
  const [sortingColumn, setSortingColumn] = useState<SortingColumn | undefined>(
    { sortingField: "createdAt" },
  );
  const [isDescending, setIsDescending] = useState(true);
  const [currentPageIndex, setCurrentPageIndex] = useState(1);
  const [preferences, setPreferences] =
    useState<CollectionPreferencesProps.Preferences>(DEFAULT_PREFERENCES);

  const { data, isLoading, error, refetch, isFetching } = useListSources(
    namespaceId ?? "",
  );

  const allItems = useMemo(
    () => data?.pages.flatMap((p) => p.items ?? []) ?? [],
    [data],
  );

  // Summary: show only statuses that have items, in priority order, max 4
  const statusCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const item of allItems) {
      const s = item.status ?? "";
      counts.set(s, (counts.get(s) ?? 0) + 1);
    }
    return SUMMARY_STATUS_PRIORITY.filter((s) => (counts.get(s) ?? 0) > 0)
      .slice(0, 4)
      .map((s) => ({
        status: s,
        label: localizedSourceStatus(s, t),
        count: counts.get(s)!,
      }));
  }, [allItems, t]);

  // Dynamic status filter options based on what's actually in the data
  const statusFilterOptions = useMemo<SelectProps.Option[]>(() => {
    const seen = new Set<string>();
    for (const item of allItems) {
      if (item.status) seen.add(item.status);
    }
    const opts: SelectProps.Option[] = [
      { label: t("All statuses"), value: "" },
    ];
    for (const s of SUMMARY_STATUS_PRIORITY) {
      if (seen.has(s))
        opts.push({ label: localizedSourceStatus(s, t), value: s });
      seen.delete(s);
    }
    // Any remaining statuses not in priority list
    for (const s of seen) {
      opts.push({ label: localizedSourceStatus(s, t), value: s });
    }
    return opts;
  }, [allItems, t]);

  // Client-side filtering
  const filteredItems = useMemo(
    () =>
      allItems
        .filter((item) => !typeFilter || item.sourceType === typeFilter)
        .filter(
          (item) => !statusFilter.value || item.status === statusFilter.value,
        )
        .filter(
          (item) =>
            !filterText ||
            (item.name ?? "").toLowerCase().includes(filterText.toLowerCase()),
        ),
    [allItems, typeFilter, statusFilter, filterText],
  );

  const sortedItems = useMemo(
    () => sortItems(filteredItems, sortingColumn, isDescending),
    [filteredItems, sortingColumn, isDescending],
  );

  const pageSize = preferences.pageSize ?? DEFAULT_PREFERENCES.pageSize!;
  const pagesCount = Math.max(1, Math.ceil(sortedItems.length / pageSize));
  const pagedItems = sortedItems.slice(
    (currentPageIndex - 1) * pageSize,
    currentPageIndex * pageSize,
  );

  const resetPage = () => setCurrentPageIndex(1);

  const visibleContent = new Set(preferences.visibleContent ?? []);

  const columnDefinitions = [
    {
      id: "name",
      header: t("Name"),
      sortingField: "name",
      isRowHeader: true,
      cell: (item: SourceSummary) => (
        <Link
          onFollow={(e) => {
            e.preventDefault();
            navigate(`/namespaces/${namespaceId}/sources/${item.sourceId}`);
          }}
          href={`/namespaces/${namespaceId}/sources/${item.sourceId}`}
        >
          {item.name}
        </Link>
      ),
      minWidth: 160,
    },
    {
      id: "status",
      header: t("Status"),
      sortingField: "status",
      cell: (item: SourceSummary) => (
        <StatusIndicator type={sourceStatusType(item.status)}>
          {localizedSourceStatus(item.status, t)}
        </StatusIndicator>
      ),
    },
    {
      id: "sourceType",
      header: t("Type"),
      cell: (item: SourceSummary) =>
        sourceTypeLabel(item.sourceType ?? "", item.sourceSubType ?? "", t),
    },
    {
      id: "createdAt",
      header: t("Created"),
      sortingField: "createdAt",
      cell: (item: SourceSummary) => formatTimestamp(item.createdAt),
    },
    {
      id: "updatedAt",
      header: t("Last updated"),
      sortingField: "updatedAt",
      cell: (item: SourceSummary) => formatTimestamp(item.updatedAt),
    },
  ].filter((col) => col.id === "name" || visibleContent.has(col.id));

  const emptyState = (
    <Box textAlign="center" padding="xxl">
      <SpaceBetween size="m">
        <b>{t("No sources")}</b>
        <Box variant="p" color="text-body-secondary">
          {t("Connect a database or document source to get started.")}
        </Box>
        <Button
          variant="primary"
          onClick={() => navigate(`/namespaces/${namespaceId}/sources/connect`)}
        >
          {t("Connect source")}
        </Button>
      </SpaceBetween>
    </Box>
  );

  const counterText =
    filteredItems.length < allItems.length
      ? `(${filteredItems.length}/${allItems.length})`
      : `(${allItems.length})`;

  return (
    <>
      {error && (
        <Alert type="error" header={t("Failed to load sources")}>
          {error.message}
        </Alert>
      )}
      <Table
        variant="full-page"
        stickyHeader
        resizableColumns
        enableKeyboardNavigation
        wrapLines={preferences.wrapLines}
        stripedRows={preferences.stripedRows}
        loading={isLoading}
        loadingText={t("Loading sources")}
        sortingColumn={sortingColumn}
        sortingDescending={isDescending}
        onSortingChange={({ detail }) => {
          setSortingColumn(detail.sortingColumn as SortingColumn);
          setIsDescending(detail.isDescending ?? false);
          resetPage();
        }}
        header={
          <SpaceBetween size="s">
            <Header
              variant="awsui-h1-sticky"
              counter={counterText}
              description={t("All sources registered in this namespace.")}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    iconName="refresh"
                    loading={isFetching}
                    onClick={() => refetch()}
                  >
                    {t("Refresh")}
                  </Button>
                  <Button
                    variant="primary"
                    onClick={() =>
                      navigate(`/namespaces/${namespaceId}/sources/connect`)
                    }
                  >
                    {t("Connect source")}
                  </Button>
                </SpaceBetween>
              }
            >
              {t("Sources")}
            </Header>
            {statusCounts.length > 0 && (
              <Container>
                <KeyValuePairs
                  columns={4}
                  items={statusCounts.map(({ status, label, count }) => ({
                    label,
                    value: (
                      <Link
                        variant="awsui-value-large"
                        href="#"
                        onFollow={(e) => {
                          e.preventDefault();
                          const next =
                            statusFilter.value === status ? "" : status;
                          setStatusFilter(
                            next
                              ? {
                                  label: localizedSourceStatus(next, t),
                                  value: next,
                                }
                              : { label: t("All statuses"), value: "" },
                          );
                          resetPage();
                        }}
                        ariaLabel={`${label} (${count})`}
                      >
                        {count}
                      </Link>
                    ),
                  }))}
                />
              </Container>
            )}
          </SpaceBetween>
        }
        filter={
          <SpaceBetween direction="horizontal" size="s" alignItems="center">
            <TextFilter
              filteringPlaceholder={t("Find sources")}
              filteringText={filterText}
              onChange={({ detail }) => {
                setFilterText(detail.filteringText);
                resetPage();
              }}
            />
            <SegmentedControl
              selectedId={typeFilter}
              onChange={({ detail }) => {
                setTypeFilter(detail.selectedId);
                resetPage();
              }}
              options={sourceTypeSegments}
            />
            <Select
              selectedOption={
                statusFilterOptions.find(
                  (option) => option.value === statusFilter.value,
                ) ?? statusFilterOptions[0]!
              }
              onChange={({ detail }) => {
                setStatusFilter(detail.selectedOption);
                resetPage();
              }}
              options={statusFilterOptions}
              ariaLabel={t("Filter by status")}
              expandToViewport
            />
          </SpaceBetween>
        }
        pagination={
          <Pagination
            currentPageIndex={currentPageIndex}
            pagesCount={pagesCount}
            onChange={({ detail }) =>
              setCurrentPageIndex(detail.currentPageIndex)
            }
          />
        }
        preferences={
          <CollectionPreferences
            title={t("Preferences")}
            confirmLabel={t("Confirm")}
            cancelLabel={t("Cancel")}
            preferences={preferences}
            onConfirm={({ detail }) => {
              setPreferences(detail);
              resetPage();
            }}
            pageSizePreference={{
              title: t("Page size"),
              options: PAGE_SIZE_OPTIONS.map((option) => ({
                ...option,
                label: t(option.label),
              })),
            }}
            visibleContentPreference={{
              title: t("Select visible columns"),
              options: COLUMN_DISPLAY_OPTIONS.map((group) => ({
                ...group,
                label: t(group.label),
                options: group.options.map((option) => ({
                  ...option,
                  label: t(option.label),
                })),
              })),
            }}
            wrapLinesPreference={{
              label: t("Wrap lines"),
              description: t("Wrap long text in table cells"),
            }}
            stripedRowsPreference={{
              label: t("Striped rows"),
              description: t("Alternate row background colors"),
            }}
          />
        }
        columnDefinitions={columnDefinitions}
        items={pagedItems}
        empty={emptyState}
        ariaLabels={{
          tableLabel: t("Sources"),
          selectionGroupLabel: t("Source selection"),
        }}
      />
    </>
  );
};
