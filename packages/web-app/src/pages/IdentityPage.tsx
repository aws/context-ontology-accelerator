// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Identity page — platform-level access administration.
 *
 * Route: /identity
 *
 * - Members tab: principals granted a PLATFORM-scoped role, with the ability
 *   to grant a new platform role (platform-admin / platform-viewer) or revoke
 *   an existing grant.
 * - Roles tab: the catalogue of PLATFORM-scoped roles.
 */
import React, { useEffect, useMemo, useState } from "react";
import { useCollection } from "@cloudscape-design/collection-hooks";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import Pagination from "@cloudscape-design/components/pagination";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import TextFilter from "@cloudscape-design/components/text-filter";
import type { TableProps } from "@cloudscape-design/components/table";
import type { SelectProps } from "@cloudscape-design/components/select";

import { useListPlatformRoles } from "@api-hooks/use-list-platform-roles";
import { useListPlatformGrants } from "@api-hooks/use-list-platform-grants";
import { useCreatePlatformGrant } from "@api-hooks/use-create-platform-grant";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useDeletePlatformGrant } from "@api-hooks/use-delete-platform-grant";
import type { PlatformGrantSummary } from "@api-hooks/use-list-platform-grants";
import type { ListPlatformRolesOutput } from "@coa/control-plane-client";
import { PrincipalType, RoleScope } from "@coa/control-plane-client";
import { useAppI18n } from "../i18n/LocaleProvider";

type RoleSummary = NonNullable<ListPlatformRolesOutput["roles"]>[number];

/** Formats an ISO timestamp into a localized short date+time string, or "—". */
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

export function IdentityPage() {
  const { t } = useAppI18n();
  useBreadcrumbTrail([{ text: t("Administration") }, { text: t("Identity") }]);
  const { data: rolesData, isLoading: rolesLoading } = useListPlatformRoles();

  // Only PLATFORM-scoped roles are managed from this page.
  const platformRoles = useMemo<RoleSummary[]>(
    () =>
      (rolesData?.roles ?? []).filter((r) => r.scope === RoleScope.PLATFORM),
    [rolesData],
  );

  return (
    <SpaceBetween size="m">
      <Header variant="h1">{t("Identity")}</Header>
      <Tabs
        tabs={[
          {
            id: "members",
            label: t("Members"),
            content: <MembersTab roles={platformRoles} />,
          },
          {
            id: "roles",
            label: t("Roles"),
            content: (
              <RolesTab roles={platformRoles} isLoading={rolesLoading} />
            ),
          },
        ]}
      />
    </SpaceBetween>
  );
}

// ── Members tab ────────────────────────────────────────────────────────

function MembersTab({ roles }: { roles: RoleSummary[] }) {
  const { locale, t } = useAppI18n();
  const {
    data: grantsData,
    isLoading,
    error,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    refetch,
    isFetching,
  } = useListPlatformGrants();
  const deleteGrant = useDeletePlatformGrant();

  const [grantModalVisible, setGrantModalVisible] = useState(false);
  const [confirmRevokeVisible, setConfirmRevokeVisible] = useState(false);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const [selected, setSelected] = useState<PlatformGrantSummary[]>([]);

  const grants: PlatformGrantSummary[] = useMemo(
    () => grantsData?.pages.flatMap((p) => p.grants ?? []) ?? [],
    [grantsData],
  );

  // Map roleId → display name so the table shows "Platform Admin" not the id.
  const roleNameMap = useMemo(
    () =>
      new Map(
        roles
          .filter((r) => r.roleId && r.name)
          .map((r) => [r.roleId!, r.name!]),
      ),
    [roles],
  );

  // useCollection handles client-side filtering + pagination over the loaded
  // pages; selection is managed as controlled state below.
  const {
    items,
    collectionProps,
    filterProps,
    filteredItemsCount,
    paginationProps,
  } = useCollection(grants, {
    filtering: {
      empty: error ? (
        <StatusIndicator type="error">
          {t("Failed to load members: {error}", { error: error.message })}
        </StatusIndicator>
      ) : (
        <Box textAlign="center" color="text-body-secondary">
          {t("No platform-level grants yet.")}
        </Box>
      ),
      noMatch: (
        <Box textAlign="center" color="text-body-secondary">
          {t("No members match the filter.")}
        </Box>
      ),
    },
    pagination: { pageSize: 10 },
  });

  // Fetch the next page from the API as the user approaches the end of the
  // locally loaded set, mirroring the namespace list table.
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

  const columns: TableProps.ColumnDefinition<PlatformGrantSummary>[] = [
    {
      id: "principalId",
      header: t("Principal"),
      isRowHeader: true,
      cell: (g) => g.principalId,
    },
    {
      id: "principalType",
      header: t("Type"),
      cell: (g) => (
        <StatusIndicator
          type={g.principalType === PrincipalType.GROUP ? "info" : "success"}
        >
          {g.principalType === PrincipalType.GROUP
            ? t("Group")
            : g.principalType === PrincipalType.USER
              ? t("User")
              : g.principalType}
        </StatusIndicator>
      ),
    },
    {
      id: "role",
      header: t("Role"),
      cell: (g) => (g.role ? (roleNameMap.get(g.role) ?? g.role) : "—"),
    },
    {
      id: "grantedBy",
      header: t("Granted by"),
      cell: (g) => g.grantedBy,
    },
    {
      id: "grantedAt",
      header: t("Granted at"),
      cell: (g) => formatDate(g.grantedAt, locale),
    },
  ];

  const handleRevoke = () => {
    // mutateAsync per call: a single mutation observer only keeps the last
    // mutate call's callbacks, so concurrent mutate() calls would leave
    // earlier promises unresolved and the confirm modal would never close.
    Promise.allSettled(
      selected.map((g) => deleteGrant.mutateAsync({ grantId: g.grantId! })),
    ).then((results) => {
      // allSettled preserves order, so a rejected result maps back to the
      // grant at the same index — surface which principals failed.
      const failed = results
        .map((r, i) => (r.status === "rejected" ? selected[i]! : null))
        .filter((g): g is PlatformGrantSummary => g !== null);
      if (failed.length > 0) {
        setRevokeError(
          t("Failed to revoke {failed} of {total}: {principals}", {
            failed: failed.length,
            total: results.length,
            principals: failed.map((g) => g.principalId).join(", "),
          }),
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
      <Table<PlatformGrantSummary>
        {...collectionProps}
        items={items}
        trackBy="grantId"
        variant="container"
        loading={isLoading}
        loadingText={t("Loading members…")}
        selectionType="multi"
        selectedItems={selected}
        onSelectionChange={({ detail }) =>
          setSelected(detail.selectedItems as PlatformGrantSummary[])
        }
        columnDefinitions={columns}
        ariaLabels={{
          tableLabel: t("Platform members"),
          selectionGroupLabel: t("Member selection"),
          allItemsSelectionLabel: ({ selectedItems }) =>
            t("{count} members selected", {
              count: selectedItems.length,
            }),
          itemSelectionLabel: (_s, g) =>
            t("Select grant for {principal}", {
              principal: g.principalId ?? "",
            }),
        }}
        header={
          <Header
            variant="h2"
            counter={`(${grants.length})`}
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
                  disabled={selected.length === 0}
                  loading={deleteGrant.isPending}
                  onClick={() => setConfirmRevokeVisible(true)}
                >
                  {t("Revoke")}
                </Button>
                <Button
                  variant="primary"
                  onClick={() => setGrantModalVisible(true)}
                >
                  {t("Grant access")}
                </Button>
              </SpaceBetween>
            }
          >
            {t("Members")}
          </Header>
        }
        filter={
          <TextFilter
            {...filterProps}
            filteringAriaLabel={t("Filter members")}
            filteringPlaceholder={t("Find members")}
            countText={t(
              (filteredItemsCount ?? 0) === 1
                ? "{count} match"
                : "{count} matches",
              { count: filteredItemsCount ?? 0 },
            )}
          />
        }
        pagination={<Pagination {...paginationProps} openEnd={hasNextPage} />}
      />

      <GrantPlatformRoleModal
        roles={roles}
        visible={grantModalVisible}
        onDismiss={() => setGrantModalVisible(false)}
      />

      <Modal
        visible={confirmRevokeVisible}
        onDismiss={() => setConfirmRevokeVisible(false)}
        header={t("Confirm revoke")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => setConfirmRevokeVisible(false)}
              >
                {t("Cancel")}
              </Button>
              <Button
                variant="primary"
                loading={deleteGrant.isPending}
                onClick={handleRevoke}
              >
                {t("Revoke")}
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
            {t(
              selected.length === 1
                ? "Are you sure you want to revoke platform access for {count} principal?"
                : "Are you sure you want to revoke platform access for {count} principals?",
              { count: selected.length },
            )}
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
}

// ── Roles tab ──────────────────────────────────────────────────────────

function RolesTab({
  roles,
  isLoading,
}: {
  roles: RoleSummary[];
  isLoading: boolean;
}) {
  const { t } = useAppI18n();
  const [filterText, setFilterText] = useState("");

  const filtered = useMemo(() => {
    if (!filterText) return roles;
    const lower = filterText.toLowerCase();
    return roles.filter(
      (r) =>
        (r.name ?? "").toLowerCase().includes(lower) ||
        (r.description ?? "").toLowerCase().includes(lower),
    );
  }, [roles, filterText]);

  const columns: TableProps.ColumnDefinition<RoleSummary>[] = [
    {
      id: "name",
      header: t("Role"),
      isRowHeader: true,
      cell: (r) => r.name,
    },
    { id: "scope", header: t("Scope"), cell: (r) => r.scope },
    {
      id: "builtIn",
      header: t("Built-in"),
      cell: (r) => (r.isBuiltIn ? t("Yes") : t("No")),
    },
    {
      id: "description",
      header: t("Description"),
      cell: (r) => r.description ?? "—",
    },
  ];

  return (
    <Table<RoleSummary>
      variant="container"
      items={filtered}
      trackBy="roleId"
      columnDefinitions={columns}
      loading={isLoading}
      loadingText={t("Loading roles…")}
      header={
        <Header variant="h2" counter={`(${filtered.length})`}>
          {t("Platform roles")}
        </Header>
      }
      filter={
        <TextFilter
          filteringText={filterText}
          onChange={({ detail }) => setFilterText(detail.filteringText)}
          filteringAriaLabel={t("Filter roles")}
          filteringPlaceholder={t("Find roles")}
        />
      }
      empty={
        <Box textAlign="center" color="text-body-secondary">
          {t("No platform roles defined.")}
        </Box>
      }
    />
  );
}

// ── Grant platform role modal ────────────────────────────────────────────

function GrantPlatformRoleModal({
  roles,
  visible,
  onDismiss,
}: {
  roles: RoleSummary[];
  visible: boolean;
  onDismiss: () => void;
}) {
  const { t } = useAppI18n();
  const createGrant = useCreatePlatformGrant();
  const principalTypeOptions: SelectProps.Option[] = [
    { value: PrincipalType.USER, label: t("User") },
    { value: PrincipalType.GROUP, label: t("Group") },
  ];
  const [principalType, setPrincipalType] = useState<PrincipalType>(
    PrincipalType.USER,
  );
  const [principalId, setPrincipalId] = useState("");
  const [selectedRole, setSelectedRole] = useState<SelectProps.Option | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  // Role options are the PLATFORM-scoped roles (e.g. Platform Admin / Viewer).
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

  // Reset the form whenever the modal is (re)opened.
  React.useEffect(() => {
    if (visible) {
      setPrincipalType(PrincipalType.USER);
      setPrincipalId("");
      setSelectedRole(null);
      setError(null);
    }
  }, [visible]);

  const validatePrincipalId = (): string | null => {
    if (!principalId.trim()) return t("Principal ID is required");
    if (principalType === PrincipalType.USER) {
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(principalId))
        return t("Please enter a valid email address");
    } else if (!/^[\w.-]+$/.test(principalId)) {
      return t(
        "Name may only contain letters, numbers, hyphens, underscores, and dots",
      );
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
    createGrant.mutate(
      {
        body: {
          principalType,
          principalId,
          role: selectedRole.value!,
        },
      },
      {
        onSuccess: () => onDismiss(),
        onError: (e) => setError(e.message ?? t("Failed to create grant")),
      },
    );
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={t("Grant platform access")}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              {t("Cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={handleSubmit}
              loading={createGrant.isPending}
              disabled={!principalId || !selectedRole}
            >
              {t("Grant")}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {error && <StatusIndicator type="error">{error}</StatusIndicator>}
        <FormField label={t("Type")}>
          <Select
            selectedOption={
              principalTypeOptions.find((o) => o.value === principalType) ??
              principalTypeOptions[0]!
            }
            onChange={({ detail }) =>
              setPrincipalType(detail.selectedOption.value as PrincipalType)
            }
            options={principalTypeOptions}
          />
        </FormField>
        <FormField
          label={principalType === PrincipalType.USER ? t("Email") : t("Name")}
        >
          <Input
            value={principalId}
            onChange={({ detail }) => setPrincipalId(detail.value)}
            placeholder={
              principalType === PrincipalType.USER
                ? "user@example.com"
                : "platform-operators"
            }
          />
        </FormField>
        <FormField label={t("Platform role")}>
          <Select
            selectedOption={selectedRole}
            onChange={({ detail }) => setSelectedRole(detail.selectedOption)}
            options={roleOptions}
            placeholder={t("Select a platform role")}
            empty={t("No platform roles available")}
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
}
