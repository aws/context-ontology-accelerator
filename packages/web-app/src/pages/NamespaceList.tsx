// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useCollection } from "@cloudscape-design/collection-hooks";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import ButtonDropdown from "@cloudscape-design/components/button-dropdown";
import Link from "@cloudscape-design/components/link";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Alert from "@cloudscape-design/components/alert";
import Modal from "@cloudscape-design/components/modal";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import TextFilter from "@cloudscape-design/components/text-filter";
import Pagination from "@cloudscape-design/components/pagination";
import CollectionPreferences from "@cloudscape-design/components/collection-preferences";
import type { TableProps } from "@cloudscape-design/components/table";
import type { SelectProps } from "@cloudscape-design/components/select";
import type { CollectionPreferencesProps } from "@cloudscape-design/components/collection-preferences";
import {
  useListNamespaces,
  useUpdateNamespaceStatus,
  useDeleteNamespace,
  parseBlockers,
  NamespaceSummary,
} from "@api-hooks";
import { NamespaceStatus } from "@coa/control-plane-client";
import { localizeNamespaceLanguage } from "@utils";
import { useAppI18n } from "../i18n/LocaleProvider";
import type { AppMessage } from "../i18n/appMessages";

const PAGE_SIZE_OPTIONS: Array<{ value: number; label: AppMessage }> = [
  { value: 10, label: "10 namespaces" },
  { value: 20, label: "20 namespaces" },
  { value: 50, label: "50 namespaces" },
];

const COLUMNS: Array<{
  id: string;
  label: AppMessage;
  alwaysVisible?: boolean;
}> = [
  { id: "name", label: "Namespace", alwaysVisible: true },
  { id: "language", label: "Language" },
  { id: "sources", label: "Sources" },
  { id: "status", label: "Status" },
  { id: "createdAt", label: "Created" },
  { id: "updatedAt", label: "Updated" },
];

const STATUS_MAP: Record<
  string,
  "success" | "stopped" | "error" | "in-progress"
> = {
  [NamespaceStatus.ACTIVE]: "success",
  [NamespaceStatus.ARCHIVED]: "stopped",
  [NamespaceStatus.DELETING]: "in-progress",
  [NamespaceStatus.DELETED]: "error",
  [NamespaceStatus.DELETE_FAILED]: "error",
};

const STATUS_FILTER_OPTIONS: Array<
  Omit<SelectProps.Option, "label"> & { label: AppMessage }
> = [
  { value: "all", label: "All statuses" },
  { value: NamespaceStatus.ACTIVE, label: "Active" },
  { value: NamespaceStatus.ARCHIVED, label: "Archived" },
  { value: NamespaceStatus.DELETING, label: "Deleting" },
  { value: NamespaceStatus.DELETED, label: "Deleted" },
  { value: NamespaceStatus.DELETE_FAILED, label: "Delete failed" },
];

// All lifecycle actions shown in the "Change status" dropdown. They are always
// rendered; invalid transitions for the selected namespace are disabled rather
// than hidden so the full set of actions is discoverable.
const STATUS_ACTIONS: { id: NamespaceStatus; text: AppMessage }[] = [
  { id: NamespaceStatus.ACTIVE, text: "Activate" },
  { id: NamespaceStatus.ARCHIVED, text: "Archive" },
  { id: NamespaceStatus.DELETED, text: "Delete" },
];

// Allowed target statuses keyed by the namespace's current status.
// "DELETED" here is a UI sentinel for the delete action (see onItemClick
// below) — clicking it actually starts the async deletion pipeline, which
// transitions ARCHIVED/DELETE_FAILED -> DELETING -> DELETED/DELETE_FAILED.
// DELETING itself is system-managed and intentionally not a selectable
// target (no user-initiated transition leads into or out of it directly).
const VALID_TRANSITIONS: Partial<Record<NamespaceStatus, NamespaceStatus[]>> = {
  [NamespaceStatus.ACTIVE]: [NamespaceStatus.ARCHIVED],
  [NamespaceStatus.ARCHIVED]: [NamespaceStatus.ACTIVE, NamespaceStatus.DELETED],
  [NamespaceStatus.DELETE_FAILED]: [NamespaceStatus.DELETED],
};

/**
 * Builds the "Change status" dropdown items for a namespace's current status.
 * Every action is always returned; transitions that are not valid from the
 * current status are disabled (greyed out) with an explanatory reason rather
 * than hidden.
 */
export function getStatusActionItems(status: NamespaceStatus | undefined) {
  const allowed = new Set(status ? (VALID_TRANSITIONS[status] ?? []) : []);
  return STATUS_ACTIONS.map((action) => {
    const enabled = allowed.has(action.id);
    return {
      ...action,
      disabled: !enabled,
      disabledReason: enabled
        ? undefined
        : `Cannot ${action.text.toLowerCase()} a namespace in ${status ?? "this"} status.`,
    };
  });
}

/**
 * Header counter text for the namespace table. Shows the total count, and when
 * rows are selected, renders "selected/total selected" (e.g. "(1/2 selected)").
 */
export function getHeaderCounterText(total: number, selected: number): string {
  return selected ? `(${selected}/${total} selected)` : `(${total})`;
}

function buildColumnDefinitions(
  locale: string,
  t: (message: AppMessage) => string,
): Array<
  TableProps.ColumnDefinition<NamespaceSummary> & { header: AppMessage }
> {
  return [
    {
      id: "name",
      header: "Namespace",
      sortingField: "displayName",
      isRowHeader: true,
      cell: (item) => item.displayName ?? item.name ?? "-",
    },
    {
      id: "language",
      header: "Language",
      sortingField: "language",
      cell: (item) => localizeNamespaceLanguage(item.language, t),
    },
    {
      id: "sources",
      header: "Sources",
      sortingField: "sourceCount",
      cell: (item) => item.sourceCount ?? 0,
    },
    {
      id: "status",
      header: "Status",
      sortingField: "status",
      cell: (item) => (
        <StatusIndicator type={STATUS_MAP[item.status as string] ?? "info"}>
          {item.status}
        </StatusIndicator>
      ),
    },
    {
      id: "createdAt",
      header: "Created",
      sortingField: "createdAt",
      cell: (item) =>
        item.createdAt
          ? new Date(item.createdAt).toLocaleDateString(locale)
          : "-",
    },
    {
      id: "updatedAt",
      header: "Updated",
      sortingField: "updatedAt",
      cell: (item) =>
        item.updatedAt
          ? new Date(item.updatedAt).toLocaleDateString(locale)
          : "-",
    },
  ];
}

export const NamespaceList: React.FC = () => {
  const navigate = useNavigate();
  const { locale, t } = useAppI18n();
  const baseColumnDefinitions = useMemo(
    () => buildColumnDefinitions(locale, t),
    [locale, t],
  );
  useBreadcrumbTrail([
    { text: t("Administration") },
    { text: t("Namespaces") },
  ]);
  const updateStatus = useUpdateNamespaceStatus();
  const deleteNamespace = useDeleteNamespace();
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [deleteConfirmText, setDeleteConfirmText] = useState("");

  const [selectedStatus, setSelectedStatus] = useState<SelectProps.Option>(
    STATUS_FILTER_OPTIONS[0]!,
  );
  const statusFilterOptions = useMemo(
    () =>
      STATUS_FILTER_OPTIONS.map((option) => ({
        ...option,
        label: t(option.label),
      })),
    [t],
  );

  const [preferences, setPreferences] =
    useState<CollectionPreferencesProps.Preferences>({
      pageSize: 20,
      wrapLines: false,
      stripedRows: true,
      contentDensity: "comfortable",
      contentDisplay: COLUMNS.map((c) => ({ id: c.id, visible: true })),
    });

  const {
    data,
    isLoading,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    refetch,
    isFetching,
  } = useListNamespaces();

  const allItems = useMemo(
    () => data?.pages.flatMap((p) => p.namespaces ?? []) ?? [],
    [data],
  );

  const visibleItems = useMemo(() => {
    const s = selectedStatus.value;
    if (!s || s === "all") return allItems;
    return allItems.filter((n) => n.status === s);
  }, [allItems, selectedStatus]);

  const {
    items,
    collectionProps,
    filterProps,
    paginationProps,
    filteredItemsCount,
  } = useCollection(visibleItems, {
    filtering: {
      empty: (
        <Box textAlign="center" padding="xxl">
          <SpaceBetween size="m">
            <b>{t("No namespaces")}</b>
            <Box variant="p" color="text-body-secondary">
              {t(
                "Create your first namespace to start building a semantic context.",
              )}
            </Box>
            <Button
              variant="primary"
              onClick={() => navigate("/namespaces/create")}
            >
              {t("Create namespace")}
            </Button>
          </SpaceBetween>
        </Box>
      ),
      noMatch: (
        <Box textAlign="center" padding="xxl">
          <SpaceBetween size="m">
            <b>{t("No matches")}</b>
            <Box variant="p" color="text-body-secondary">
              {t("No namespaces matched the filter criteria.")}
            </Box>
          </SpaceBetween>
        </Box>
      ),
    },
    pagination: { pageSize: preferences.pageSize ?? 20 },
    sorting: { defaultState: { sortingColumn: baseColumnDefinitions[0]! } },
    selection: {},
  });

  // Fetch next page when approaching the end
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

  const selected = collectionProps.selectedItems ?? [];

  // Inject Link navigation into the name column
  const columnDefinitions = useMemo<
    TableProps.ColumnDefinition<NamespaceSummary>[]
  >(
    () =>
      baseColumnDefinitions.map((col) =>
        col.id === "name"
          ? {
              ...col,
              header: t(col.header),
              cell: (item: NamespaceSummary) => (
                <Link
                  href={`/namespaces/${item.namespaceId}`}
                  onFollow={(e) => {
                    e.preventDefault();
                    navigate(`/namespaces/${item.namespaceId}`);
                  }}
                >
                  {item.displayName ?? item.name}
                </Link>
              ),
            }
          : {
              ...col,
              header: t(col.header),
            },
      ),
    [baseColumnDefinitions, navigate, t],
  );

  return (
    <SpaceBetween size="m">
      {deleteError && (
        <Alert
          type="error"
          dismissible
          onDismiss={() => setDeleteError(null)}
          header={t("Namespace deletion blocked")}
        >
          {deleteError}
        </Alert>
      )}
      <Table<NamespaceSummary>
        {...collectionProps}
        items={items}
        trackBy="namespaceId"
        variant="full-page"
        stickyHeader
        resizableColumns
        enableKeyboardNavigation
        loading={isLoading}
        loadingText={t("Loading namespaces")}
        selectionType="multi"
        columnDefinitions={columnDefinitions}
        columnDisplay={preferences.contentDisplay}
        wrapLines={preferences.wrapLines}
        stripedRows={preferences.stripedRows}
        contentDensity={preferences.contentDensity}
        ariaLabels={{
          tableLabel: t("Namespaces"),
          selectionGroupLabel: t("Namespace selection"),
          allItemsSelectionLabel: ({ selectedItems }) =>
            t("{count} namespaces selected", {
              count: selectedItems.length,
            }),
          itemSelectionLabel: (_state, row) =>
            t("Select {name}", {
              name: row.displayName ?? row.name ?? "—",
            }),
        }}
        renderAriaLive={({ firstIndex, lastIndex, totalItemsCount }) =>
          t("Displaying namespaces {first} to {last} of {total}", {
            first: firstIndex,
            last: lastIndex,
            total: totalItemsCount ?? 0,
          })
        }
        header={
          <Header
            variant="h1"
            counter={getHeaderCounterText(visibleItems.length, selected.length)}
            description={t(
              "Namespaces partition the semantic graph into isolated scopes. Each namespace has its own data sources, ontology, and access policies.",
            )}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  iconName="refresh"
                  loading={isFetching}
                  onClick={() => refetch()}
                >
                  {t("Refresh")}
                </Button>
                <ButtonDropdown
                  disabled={selected.length !== 1}
                  items={getStatusActionItems(
                    selected[0]?.status as NamespaceStatus | undefined,
                  ).map((item) => ({
                    ...item,
                    text: t(item.text),
                    disabledReason: item.disabled
                      ? t("This action is unavailable for the current status.")
                      : undefined,
                  }))}
                  onItemClick={({ detail }) => {
                    const ns = selected[0];
                    if (!ns?.namespaceId) return;
                    setDeleteError(null);

                    if (detail.id === NamespaceStatus.DELETED) {
                      setShowDeleteConfirm(true);
                    } else {
                      updateStatus.mutate(
                        {
                          namespaceId: ns.namespaceId,
                          status: detail.id as NamespaceStatus,
                        },
                        {
                          onError: (e) => {
                            setDeleteError(
                              t("Status update failed: {message}", {
                                message: e.message,
                              }),
                            );
                          },
                        },
                      );
                    }
                  }}
                >
                  {t("Change status")}
                </ButtonDropdown>
                <Button
                  variant="primary"
                  onClick={() => navigate("/namespaces/create")}
                >
                  {t("Create namespace")}
                </Button>
              </SpaceBetween>
            }
          >
            {t("Namespaces")}
          </Header>
        }
        filter={
          <SpaceBetween direction="horizontal" size="s">
            <TextFilter
              {...filterProps}
              filteringAriaLabel={t("Filter namespaces")}
              filteringPlaceholder={t("Find namespaces")}
              countText={t("{count} matches", {
                count: filteredItemsCount ?? 0,
              })}
            />
            <Select
              selectedOption={
                statusFilterOptions.find(
                  (option) => option.value === selectedStatus.value,
                ) ?? statusFilterOptions[0]!
              }
              onChange={({ detail }) =>
                setSelectedStatus(detail.selectedOption)
              }
              options={statusFilterOptions}
              ariaLabel={t("Filter by status")}
              expandToViewport
            />
          </SpaceBetween>
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
              options: PAGE_SIZE_OPTIONS.map((option) => ({
                ...option,
                label: t(option.label),
              })),
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
      {showDeleteConfirm && selected[0] && (
        <Modal
          visible={showDeleteConfirm}
          onDismiss={() => {
            setShowDeleteConfirm(false);
            setDeleteConfirmText("");
          }}
          header={t("Delete namespace “{name}”?", {
            name: selected[0].name ?? "—",
          })}
          footer={
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                onClick={() => {
                  setShowDeleteConfirm(false);
                  setDeleteConfirmText("");
                }}
              >
                {t("Cancel")}
              </Button>
              <Button
                variant="primary"
                loading={deleteNamespace.isPending}
                disabled={
                  deleteNamespace.isPending ||
                  deleteConfirmText !== selected[0].name
                }
                onClick={() => {
                  const ns = selected[0];
                  if (!ns?.namespaceId) return;
                  deleteNamespace.mutate(
                    { namespaceId: ns.namespaceId },
                    {
                      onSuccess: () => {
                        setShowDeleteConfirm(false);
                        setDeleteConfirmText("");
                      },
                      onError: (e) => {
                        setShowDeleteConfirm(false);
                        setDeleteConfirmText("");
                        const blocked = parseBlockers(e);
                        if (blocked) {
                          setDeleteError(
                            blocked.blockers.length > 0
                              ? `Cannot delete "${ns.name}": ${blocked.blockers.join(", ")}. Wait for these operations to finish and try again.`
                              : blocked.message,
                          );
                        } else {
                          setDeleteError(
                            `Delete failed: ${e instanceof Error ? e.message : String(e)}`,
                          );
                        }
                      },
                    },
                  );
                }}
              >
                {t("Delete")}
              </Button>
            </SpaceBetween>
          }
        >
          <SpaceBetween size="m">
            <Alert type="warning" header={t("This action cannot be undone")}>
              <b>{selected[0].name}</b>:{" "}
              {t(
                "Deleting this namespace permanently removes all of its resources:",
              )}
              <ul>
                <li>{t("All data sources and their discovered metadata")}</li>
                <li>{t("All metric definitions")}</li>
                <li>
                  {t("All ontology data (classes, properties, induced graphs)")}
                </li>
                <li>
                  {t("Role and permission grants scoped to this namespace")}
                </li>
                <li>{t("The Athena workgroup and DataZone project")}</li>
              </ul>
              {t(
                "Deletion runs in the background. The namespace remains in Deleting status until it completes, and deleted data cannot be recovered.",
              )}
            </Alert>
            <FormField
              label={t("Type “{name}” to confirm", {
                name: selected[0].name ?? "—",
              })}
            >
              <Input
                value={deleteConfirmText}
                onChange={({ detail }) => setDeleteConfirmText(detail.value)}
                placeholder={selected[0].name}
              />
            </FormField>
          </SpaceBetween>
        </Modal>
      )}
    </SpaceBetween>
  );
};
