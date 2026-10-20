// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Namespace permissions page — manage role grants for a single namespace.
 *
 * Route: /namespaces/:namespaceId/permissions
 *
 * Moved off the namespace detail page so granting/revoking access has its own
 * surface. Two tabs:
 *   - Roles: the namespace's available roles.
 *   - Members: principals granted a role, with grant + revoke actions.
 */
import React, { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useCollection } from "@cloudscape-design/collection-hooks";
import Badge, { type BadgeProps } from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import Pagination from "@cloudscape-design/components/pagination";
import Select from "@cloudscape-design/components/select";
import type { SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import TextFilter from "@cloudscape-design/components/text-filter";
import type { TableProps } from "@cloudscape-design/components/table";
import { useGetNamespace } from "@api-hooks/use-get-namespace";
import { useListNamespaceRoles } from "@api-hooks/use-list-namespace-roles";
import { useListNamespaceGrants } from "@api-hooks/use-list-namespace-grants";
import { useCreateGrant } from "@api-hooks/use-create-grant";
import { useDeleteGrant } from "@api-hooks/use-delete-grant";
import type { GrantSummary } from "@api-hooks/use-list-namespace-grants";
import type { RoleSummary } from "@coa/control-plane-client";
import { PrincipalType } from "@coa/control-plane-client";
import { TokenInput } from "@components/TokenInput";
import {
  ColumnDenylistEditor,
  type ColumnDenylistRow,
} from "@components/ColumnDenylistEditor";
import { columnDenylistRowsToMap } from "@utils/grant-overrides";

/** Formats an ISO timestamp into a localized short date+time string, or "—". */
function formatDate(iso: string | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
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

/** Muted "state" label used when a restriction is absent (e.g. "All", "None"). */
function unrestrictedLabel(label: string): React.ReactNode {
  return (
    <Box variant="small" color="text-status-inactive">
      {label}
    </Box>
  );
}

/**
 * Renders a list of values as wrapping Cloudscape badges (chips), mirroring
 * the token pills in the create-grant modal. Falls back to a muted state
 * label when the list is empty.
 */
function renderBadgeList(
  values: string[] | undefined,
  color: BadgeProps["color"],
  emptyLabel: string,
): React.ReactNode {
  // Absent = unrestricted. An explicitly EMPTY list is a deny-all restriction
  // (enforced by the SQL firewall) and must not render as the unrestricted
  // label — "All" for a grant that permits nothing would be actively wrong.
  if (!values) return unrestrictedLabel(emptyLabel);
  if (!values.length) return <Badge color="red">None</Badge>;
  return (
    <SpaceBetween direction="horizontal" size="xxs">
      {values.map((v) => (
        <Badge key={v} color={color}>
          {v}
        </Badge>
      ))}
    </SpaceBetween>
  );
}

/**
 * Renders the per-table column denylist as grouped rows — each table name
 * followed by its denied columns as red chips — instead of a flat string.
 */
function renderDeniedColumns(
  map: Record<string, string[]> | undefined,
): React.ReactNode {
  const entries = map ? Object.entries(map) : [];
  if (!entries.length) return unrestrictedLabel("None");
  return (
    <SpaceBetween size="xxs">
      {entries.map(([table, cols]) => (
        <SpaceBetween key={table} direction="horizontal" size="xxs">
          <Box variant="awsui-key-label">{table}</Box>
          {cols.map((c) => (
            <Badge key={`${table}.${c}`} color="red">
              {c}
            </Badge>
          ))}
        </SpaceBetween>
      ))}
    </SpaceBetween>
  );
}

export const NamespacePermissions: React.FC = () => {
  const navigate = useNavigate();
  const { namespaceId = "" } = useParams<{ namespaceId: string }>();
  const { data: nsData } = useGetNamespace(namespaceId);
  const ns = nsData?.namespace;
  const title = ns?.displayName ?? ns?.name ?? namespaceId;

  useBreadcrumbTrail([
    { text: title, href: `/namespaces/${namespaceId}`, namespaceId },
    { text: "Permissions" },
  ]);

  return (
    <SpaceBetween size="m">
      <Header
        variant="h1"
        actions={
          <Button onClick={() => navigate(`/namespaces/${namespaceId}`)}>
            Back to namespace
          </Button>
        }
      >
        {title} — Permissions
      </Header>
      <PermissionsTabs namespaceId={namespaceId} />
    </SpaceBetween>
  );
};

// ── Tabs ─────────────────────────────────────────────────────────────

const PermissionsTabs: React.FC<{ namespaceId: string }> = ({
  namespaceId,
}) => {
  const {
    data: rolesData,
    isLoading: rolesLoading,
    error: rolesError,
  } = useListNamespaceRoles(namespaceId);
  const {
    data: grantsData,
    isLoading: grantsLoading,
    error: grantsError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    refetch: refetchGrants,
    isFetching: isFetchingGrants,
  } = useListNamespaceGrants(namespaceId);
  const deleteGrant = useDeleteGrant();

  const [grantModalVisible, setGrantModalVisible] = useState(false);
  const [confirmRevokeVisible, setConfirmRevokeVisible] = useState(false);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const [selected, setSelected] = useState<GrantSummary[]>([]);

  const roles: RoleSummary[] = useMemo(
    () => rolesData?.roles ?? [],
    [rolesData],
  );
  const grants: GrantSummary[] = useMemo(
    () => grantsData?.pages.flatMap((p) => p.grants ?? []) ?? [],
    [grantsData],
  );

  const roleNameMap = useMemo(
    () =>
      new Map(
        roles
          .filter((r) => r.roleId && r.name)
          .map((r) => [r.roleId!, r.name!]),
      ),
    [roles],
  );

  const memberColumns = useMemo(
    () => makeMemberColumns(roleNameMap),
    [roleNameMap],
  );

  const {
    items: memberItems,
    collectionProps,
    filterProps,
    filteredItemsCount,
    paginationProps,
  } = useCollection(grants, {
    filtering: {
      empty: grantsError ? (
        <StatusIndicator type="error">
          Failed to load members: {grantsError.message}
        </StatusIndicator>
      ) : (
        <Box textAlign="center" color="text-body-secondary">
          No members in this namespace.
        </Box>
      ),
      noMatch: (
        <Box textAlign="center" color="text-body-secondary">
          No members match the filter.
        </Box>
      ),
    },
    pagination: { pageSize: 10 },
    selection: {},
  });

  // Fetch the next API page as the user approaches the end of the loaded set.
  const pagesCount = paginationProps.pagesCount ?? 1;
  useEffect(() => {
    if (
      paginationProps.currentPageIndex >= pagesCount &&
      hasNextPage &&
      !isFetchingNextPage
    ) {
      // Errors are surfaced via the query's `error` state (shown in the table
      // empty/error slot); swallow the rejection to avoid an unhandled promise.
      fetchNextPage().catch(() => undefined);
    }
  }, [
    paginationProps.currentPageIndex,
    pagesCount,
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
  ]);

  const roleColumns: TableProps.ColumnDefinition<RoleSummary>[] = [
    { id: "name", header: "Role", isRowHeader: true, cell: (r) => r.name },
    { id: "scope", header: "Scope", cell: (r) => r.scope ?? "—" },
    {
      id: "description",
      header: "Description",
      cell: (r) => r.description ?? "—",
    },
    {
      id: "builtIn",
      header: "Built-in",
      cell: (r) => (
        <StatusIndicator type={r.isBuiltIn ? "info" : "success"}>
          {r.isBuiltIn ? "Yes" : "No"}
        </StatusIndicator>
      ),
    },
  ];

  const handleRevoke = () => {
    // Use mutateAsync (not mutate + onSuccess callbacks): a single mutation
    // observer only retains the last mutate call's callbacks, so concurrent
    // mutate() calls would leave earlier promises unresolved and the modal
    // would never close. mutateAsync returns a promise per call instead.
    Promise.allSettled(
      selected.map((g) =>
        deleteGrant.mutateAsync({ namespaceId, grantId: g.grantId }),
      ),
    ).then((results) => {
      // allSettled preserves order, so a rejected result maps back to the
      // grant at the same index — surface which principals failed.
      const failed = results
        .map((r, i) => (r.status === "rejected" ? selected[i]! : null))
        .filter((g): g is GrantSummary => g !== null);
      if (failed.length > 0) {
        setRevokeError(
          `Failed to revoke ${failed.length} of ${results.length}: ${failed
            .map((g) => g.principalId)
            .join(", ")}`,
        );
      } else {
        setRevokeError(null);
        setSelected([]);
        setConfirmRevokeVisible(false);
      }
    });
  };

  return (
    <>
      <Tabs
        tabs={[
          {
            id: "members",
            label: `Members (${grants.length})`,
            content: (
              <Table<GrantSummary>
                {...collectionProps}
                items={memberItems}
                trackBy="grantId"
                loading={grantsLoading}
                loadingText="Loading members…"
                selectionType="multi"
                selectedItems={selected}
                onSelectionChange={({ detail }) =>
                  setSelected(detail.selectedItems as GrantSummary[])
                }
                columnDefinitions={memberColumns}
                variant="container"
                ariaLabels={{
                  tableLabel: "Namespace members",
                  selectionGroupLabel: "Member selection",
                  allItemsSelectionLabel: ({ selectedItems }) =>
                    `${selectedItems.length} members selected`,
                  itemSelectionLabel: (_s, g) =>
                    `Select grant for ${g.principalId}`,
                }}
                header={
                  <Header
                    variant="h2"
                    counter={`(${grants.length})`}
                    actions={
                      <SpaceBetween direction="horizontal" size="xs">
                        <Button
                          iconName="refresh"
                          loading={isFetchingGrants}
                          onClick={() => refetchGrants()}
                        >
                          Refresh
                        </Button>
                        <Button
                          disabled={selected.length === 0}
                          loading={deleteGrant.isPending}
                          onClick={() => setConfirmRevokeVisible(true)}
                        >
                          Revoke
                        </Button>
                        <Button
                          variant="primary"
                          onClick={() => setGrantModalVisible(true)}
                        >
                          Grant access
                        </Button>
                      </SpaceBetween>
                    }
                  >
                    Users with access to this namespace
                  </Header>
                }
                filter={
                  <TextFilter
                    {...filterProps}
                    filteringAriaLabel="Filter members"
                    filteringPlaceholder="Find members"
                    countText={`${filteredItemsCount ?? 0} match${(filteredItemsCount ?? 0) === 1 ? "" : "es"}`}
                  />
                }
                pagination={
                  <Pagination {...paginationProps} openEnd={hasNextPage} />
                }
              />
            ),
          },
          {
            id: "roles",
            label: `Roles (${roles.length})`,
            content: (
              <Table<RoleSummary>
                variant="container"
                items={roles}
                trackBy="roleId"
                loading={rolesLoading}
                loadingText="Loading roles…"
                columnDefinitions={roleColumns}
                header={
                  <Header variant="h2" counter={`(${roles.length})`}>
                    Namespace roles
                  </Header>
                }
                empty={
                  rolesError ? (
                    <StatusIndicator type="error">
                      Failed to load roles: {rolesError.message}
                    </StatusIndicator>
                  ) : (
                    <Box textAlign="center" color="text-body-secondary">
                      No roles defined for this namespace.
                    </Box>
                  )
                }
              />
            ),
          },
        ]}
      />

      <CreateGrantModal
        namespaceId={namespaceId}
        roles={roles}
        rolesError={rolesError}
        visible={grantModalVisible}
        onDismiss={() => setGrantModalVisible(false)}
      />

      <Modal
        visible={confirmRevokeVisible}
        onDismiss={() => setConfirmRevokeVisible(false)}
        header="Confirm revoke"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => setConfirmRevokeVisible(false)}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                loading={deleteGrant.isPending}
                onClick={handleRevoke}
              >
                Revoke
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          {revokeError && (
            <StatusIndicator type="error">{revokeError}</StatusIndicator>
          )}
          <Box>
            Are you sure you want to revoke access for{" "}
            <strong>{selected.length}</strong>{" "}
            {selected.length === 1 ? "principal" : "principals"}?
          </Box>
          {selected.map((g) => (
            <Box key={g.grantId} variant="code">
              {g.principalId} (
              {g.role ? (roleNameMap.get(g.role) ?? g.role) : "—"})
            </Box>
          ))}
        </SpaceBetween>
      </Modal>
    </>
  );
};

// ── Column definitions ───────────────────────────────────────────────

function makeMemberColumns(
  roleNameMap: Map<string, string>,
): TableProps.ColumnDefinition<GrantSummary>[] {
  return [
    {
      id: "principalId",
      header: "Principal",
      cell: (g) => g.principalId,
      isRowHeader: true,
    },
    { id: "principalType", header: "Type", cell: (g) => g.principalType },
    {
      id: "role",
      header: "Role",
      cell: (g) => (g.role ? (roleNameMap.get(g.role) ?? g.role) : "—"),
    },
    { id: "grantedBy", header: "Granted by", cell: (g) => g.grantedBy },
    {
      id: "grantedAt",
      header: "Granted at",
      cell: (g) => formatDate(g.grantedAt),
    },
    {
      id: "allowedTables",
      header: "Allowed tables",
      cell: (g) => renderBadgeList(g.tableAllowlist, "blue", "All"),
    },
    {
      id: "allowedMetrics",
      header: "Allowed metrics",
      cell: (g) => renderBadgeList(g.allowedMetrics, "blue", "All"),
    },
    {
      id: "deniedColumns",
      header: "Denied columns",
      cell: (g) => renderDeniedColumns(g.columnDenylist),
    },
  ];
}

// ── Create grant modal ───────────────────────────────────────────────

interface CreateGrantModalProps {
  namespaceId: string;
  roles: RoleSummary[];
  rolesError?: Error | null;
  visible: boolean;
  onDismiss: () => void;
}

const PRINCIPAL_TYPE_OPTIONS = [
  { value: PrincipalType.USER, label: "User" },
  { value: PrincipalType.GROUP, label: "Group" },
];

const CreateGrantModal: React.FC<CreateGrantModalProps> = ({
  namespaceId,
  roles,
  rolesError,
  visible,
  onDismiss,
}) => {
  const [principalType, setPrincipalType] = useState(
    PRINCIPAL_TYPE_OPTIONS[0]!,
  );
  const [principalId, setPrincipalId] = useState("");
  const [selectedRole, setSelectedRole] = useState<SelectProps.Option | null>(
    null,
  );
  const [tableAllowlist, setTableAllowlist] = useState<string[]>([]);
  const [columnDenylistRows, setColumnDenylistRows] = useState<
    ColumnDenylistRow[]
  >([]);
  const [allowedMetrics, setAllowedMetrics] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const createGrant = useCreateGrant();

  const roleOptions = useMemo<SelectProps.Option[]>(
    () =>
      roles
        .filter((r) => r.roleId && r.name)
        .map((r) => ({
          value: r.roleId!,
          label: r.name!,
          description: r.description ?? undefined,
        })),
    [roles],
  );

  React.useEffect(() => {
    if (visible) {
      setPrincipalType(PRINCIPAL_TYPE_OPTIONS[0]!);
      setPrincipalId("");
      setSelectedRole(null);
      setTableAllowlist([]);
      setColumnDenylistRows([]);
      setAllowedMetrics([]);
      setError(null);
    }
  }, [visible]);

  const validatePrincipalId = (): string | null => {
    if (!principalId.trim()) return "Principal ID is required";
    if (principalType.value === PrincipalType.USER) {
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(principalId))
        return "Please enter a valid email address";
    } else if (!/^[\w.-]+$/.test(principalId)) {
      return "Group name may only contain letters, numbers, hyphens, underscores, and dots";
    }
    return null;
  };

  const handleSubmit = () => {
    if (!principalId || !selectedRole) return;
    const validationError = validatePrincipalId();
    if (validationError) {
      setError(validationError);
      return;
    }
    setError(null);
    const tables = tableAllowlist;
    const denylist = columnDenylistRowsToMap(columnDenylistRows);
    const metrics = allowedMetrics;
    createGrant.mutate(
      {
        namespaceId,
        body: {
          principalType: principalType.value as PrincipalType,
          principalId,
          role: selectedRole.value!,
          ...(tables.length ? { tableAllowlist: tables } : {}),
          ...(Object.keys(denylist).length ? { columnDenylist: denylist } : {}),
          ...(metrics.length ? { allowedMetrics: metrics } : {}),
        },
      },
      {
        onSuccess: () => onDismiss(),
        onError: (e) => setError(e.message ?? "Failed to create grant"),
      },
    );
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Grant access"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={handleSubmit}
              loading={createGrant.isPending}
              disabled={!principalId || !selectedRole}
            >
              Grant
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {error && <StatusIndicator type="error">{error}</StatusIndicator>}
        <FormField label="Type">
          <Select
            selectedOption={principalType}
            onChange={({ detail }) =>
              setPrincipalType(detail.selectedOption as typeof principalType)
            }
            options={PRINCIPAL_TYPE_OPTIONS}
          />
        </FormField>
        <FormField
          label={
            principalType.value === PrincipalType.USER ? "Email" : "Group name"
          }
        >
          <Input
            value={principalId}
            onChange={({ detail }) => setPrincipalId(detail.value)}
            placeholder={
              principalType.value === PrincipalType.USER
                ? "user@example.com"
                : "engineering-team"
            }
          />
        </FormField>
        <FormField
          label="Role"
          errorText={rolesError ? "Failed to load roles" : undefined}
        >
          <Select
            selectedOption={selectedRole}
            onChange={({ detail }) => setSelectedRole(detail.selectedOption)}
            options={roleOptions}
            placeholder="Select a role"
            empty="No roles available"
          />
        </FormField>
        <FormField
          label="Table allowlist"
          description="Optional. Tables the principal may query. Add one per chip; blank means no table restriction."
        >
          <TokenInput
            value={tableAllowlist}
            onChange={setTableAllowlist}
            placeholder="orders"
            ariaLabel="Table allowlist"
          />
        </FormField>
        <FormField
          label="Column denylist"
          description="Optional. Per table, the columns the principal may not read."
        >
          <ColumnDenylistEditor
            value={columnDenylistRows}
            onChange={setColumnDenylistRows}
          />
        </FormField>
        <FormField
          label="Allowed metrics"
          description="Optional. Metrics the principal may read. Add one per chip."
        >
          <TokenInput
            value={allowedMetrics}
            onChange={setAllowedMetrics}
            placeholder="revenue"
            ariaLabel="Allowed metrics"
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};
