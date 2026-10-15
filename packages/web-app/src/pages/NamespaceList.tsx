// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
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
import { formatNamespaceLanguage } from "@utils";

const PAGE_SIZE_OPTIONS = [
  { value: 10, label: "10 namespaces" },
  { value: 20, label: "20 namespaces" },
  { value: 50, label: "50 namespaces" },
];

const COLUMNS = [
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

const STATUS_FILTER_OPTIONS: SelectProps.Option[] = [
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
const STATUS_ACTIONS: { id: NamespaceStatus; text: string }[] = [
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

const COLUMN_DEFINITIONS: TableProps.ColumnDefinition<NamespaceSummary>[] = [
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
    cell: (item) => formatNamespaceLanguage(item.language),
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
      item.createdAt ? new Date(item.createdAt).toLocaleDateString() : "-",
  },
  {
    id: "updatedAt",
    header: "Updated",
    sortingField: "updatedAt",
    cell: (item) =>
      item.updatedAt ? new Date(item.updatedAt).toLocaleDateString() : "-",
  },
];

export const NamespaceList: React.FC = () => {
  const navigate = useNavigate();
  const updateStatus = useUpdateNamespaceStatus();
  const deleteNamespace = useDeleteNamespace();
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [deleteConfirmText, setDeleteConfirmText] = useState("");

  const [selectedStatus, setSelectedStatus] = useState<SelectProps.Option>(
    STATUS_FILTER_OPTIONS[0]!,
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
            <b>No namespaces</b>
            <Box variant="p" color="text-body-secondary">
              Create your first namespace to start building a semantic context.
            </Box>
            <Button
              variant="primary"
              onClick={() => navigate("/namespaces/create")}
            >
              Create namespace
            </Button>
          </SpaceBetween>
        </Box>
      ),
      noMatch: (
        <Box textAlign="center" padding="xxl">
          <SpaceBetween size="m">
            <b>No matches</b>
            <Box variant="p" color="text-body-secondary">
              No namespaces matched the filter criteria.
            </Box>
          </SpaceBetween>
        </Box>
      ),
    },
    pagination: { pageSize: preferences.pageSize ?? 20 },
    sorting: { defaultState: { sortingColumn: COLUMN_DEFINITIONS[0]! } },
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
      COLUMN_DEFINITIONS.map((col) =>
        col.id === "name"
          ? {
              ...col,
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
          : col,
      ),
    [navigate],
  );

  return (
    <SpaceBetween size="m">
      {deleteError && (
        <Alert
          type="error"
          dismissible
          onDismiss={() => setDeleteError(null)}
          header="Namespace deletion blocked"
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
        loadingText="Loading namespaces"
        selectionType="multi"
        columnDefinitions={columnDefinitions}
        columnDisplay={preferences.contentDisplay}
        wrapLines={preferences.wrapLines}
        stripedRows={preferences.stripedRows}
        contentDensity={preferences.contentDensity}
        ariaLabels={{
          tableLabel: "Namespaces",
          selectionGroupLabel: "Namespace selection",
          allItemsSelectionLabel: ({ selectedItems }) =>
            `${selectedItems.length} namespaces selected`,
          itemSelectionLabel: (_state, row) =>
            `Select ${row.displayName ?? row.name}`,
        }}
        renderAriaLive={({ firstIndex, lastIndex, totalItemsCount }) =>
          `Displaying namespaces ${firstIndex} to ${lastIndex} of ${totalItemsCount}`
        }
        header={
          <Header
            variant="h1"
            counter={getHeaderCounterText(visibleItems.length, selected.length)}
            description="Namespaces partition the semantic graph into isolated scopes. Each namespace has its own data sources, ontology, and access policies."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  iconName="refresh"
                  loading={isFetching}
                  onClick={() => refetch()}
                >
                  Refresh
                </Button>
                <ButtonDropdown
                  disabled={selected.length !== 1}
                  items={getStatusActionItems(
                    selected[0]?.status as NamespaceStatus | undefined,
                  )}
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
                              `Status update failed: ${e.message}`,
                            );
                          },
                        },
                      );
                    }
                  }}
                >
                  Change status
                </ButtonDropdown>
                <Button
                  variant="primary"
                  onClick={() => navigate("/namespaces/create")}
                >
                  Create namespace
                </Button>
              </SpaceBetween>
            }
          >
            Namespaces
          </Header>
        }
        filter={
          <SpaceBetween direction="horizontal" size="s">
            <TextFilter
              {...filterProps}
              filteringAriaLabel="Filter namespaces"
              filteringPlaceholder="Find namespaces"
              countText={`${filteredItemsCount ?? 0} match${(filteredItemsCount ?? 0) === 1 ? "" : "es"}`}
            />
            <Select
              selectedOption={selectedStatus}
              onChange={({ detail }) =>
                setSelectedStatus(detail.selectedOption)
              }
              options={STATUS_FILTER_OPTIONS}
              ariaLabel="Filter by status"
              expandToViewport
            />
          </SpaceBetween>
        }
        pagination={<Pagination {...paginationProps} openEnd={hasNextPage} />}
        preferences={
          <CollectionPreferences
            title="Preferences"
            confirmLabel="Confirm"
            cancelLabel="Cancel"
            preferences={preferences}
            onConfirm={({ detail }) => setPreferences(detail)}
            pageSizePreference={{
              title: "Page size",
              options: PAGE_SIZE_OPTIONS,
            }}
            wrapLinesPreference={{
              label: "Wrap lines",
              description: "Wrap long text to multiple lines.",
            }}
            stripedRowsPreference={{
              label: "Striped rows",
              description: "Alternate row background.",
            }}
            contentDensityPreference={{
              label: "Density",
              description: "Use compact density for scanning.",
            }}
            contentDisplayPreference={{
              title: "Column preferences",
              description: "Customize column visibility and order.",
              options: COLUMNS.map((c) => ({
                id: c.id,
                label: c.label,
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
          header={`Delete namespace "${selected[0].name}"?`}
          footer={
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                onClick={() => {
                  setShowDeleteConfirm(false);
                  setDeleteConfirmText("");
                }}
              >
                Cancel
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
                Delete
              </Button>
            </SpaceBetween>
          }
        >
          <SpaceBetween size="m">
            <Alert type="warning" header="This action cannot be undone">
              Deleting <b>{selected[0].name}</b> will permanently delete ALL of
              its resources, including:
              <ul>
                <li>All data sources and their discovered metadata</li>
                <li>All metric definitions</li>
                <li>All ontology data (classes, properties, induced graphs)</li>
                <li>Role and permission grants scoped to this namespace</li>
                <li>The Athena workgroup and DataZone project</li>
              </ul>
              This runs as a background deletion job — the namespace will show
              status <b>Deleting</b> until it completes. There is no way to
              recover a deleted namespace or its data once this starts.
            </Alert>
            <FormField label={`Type "${selected[0].name}" to confirm`}>
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
