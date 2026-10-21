// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import type { TableProps } from "@cloudscape-design/components/table";
import {
  GetNamespaceRoleCommand,
  type GrantSummary,
  type RoleDetail,
  type NamespaceSummary,
} from "@coa/control-plane-client";
import { useUser } from "@auth/index";
import { useListPrincipalGrants, useListPlatformRoles } from "@api-hooks/index";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useCurrentNamespace } from "@components/NamespaceProvider";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";
import { formatTimestamp } from "@utils/helpers";
import { useAppI18n, type AppTranslate } from "../i18n/LocaleProvider";

function namespaceColumns(
  t: AppTranslate,
): TableProps.ColumnDefinition<NamespaceSummary>[] {
  return [
    {
      id: "name",
      header: t("Namespace"),
      cell: (n) => n.displayName ?? n.name ?? "",
      isRowHeader: true,
    },
    { id: "status", header: t("Status"), cell: (n) => n.status ?? "—" },
    {
      id: "sources",
      header: t("Sources"),
      cell: (n) => n.sourceCount ?? 0,
    },
  ];
}

export const ProfilePage: React.FC = () => {
  const { t } = useAppI18n();
  const { user } = useUser();
  useBreadcrumbTrail([{ text: t("Profile") }]);
  const client = useControlPlaneClient();
  const { namespaces } = useCurrentNamespace();
  const {
    data: grantsData,
    isLoading: grantsLoading,
    error: grantsError,
    refetch: refetchGrants,
    isFetching: isFetchingGrants,
  } = useListPrincipalGrants();
  const { data: platformRolesData } = useListPlatformRoles();
  const [roleDetail, setRoleDetail] = useState<RoleDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const grants: GrantSummary[] = (grantsData?.grants as GrantSummary[]) ?? [];

  if (!user) return null;

  async function handleRoleClick(grant: GrantSummary) {
    setDetailLoading(true);
    setRoleDetail({ name: grant.role } as RoleDetail);
    try {
      if (grant.namespaceId && grant.namespaceId !== "GLOBAL") {
        const res = await client.send(
          new GetNamespaceRoleCommand({
            namespaceId: grant.namespaceId,
            roleId: grant.role,
          }),
        );
        setRoleDetail(res.role ?? null);
      } else {
        const match = platformRolesData?.roles?.find(
          (r) => r.roleId === grant.role || r.name === grant.role,
        );
        setRoleDetail(
          match
            ? ({ ...match, cedarPolicy: undefined } as unknown as RoleDetail)
            : ({
                name: grant.role,
                scope: "PLATFORM",
              } as unknown as RoleDetail),
        );
      }
    } catch (err) {
      console.error("Failed to load role details", { grant, error: err });
      setRoleDetail({
        name: grant.role,
        description: t("Failed to load role details."),
      } as unknown as RoleDetail);
    } finally {
      setDetailLoading(false);
    }
  }

  const grantColumns: TableProps.ColumnDefinition<GrantSummary>[] = [
    {
      id: "role",
      header: t("Role"),
      isRowHeader: true,
      cell: (g) => <Link onFollow={() => handleRoleClick(g)}>{g.role}</Link>,
    },
    {
      id: "principalType",
      header: t("Principal type"),
      cell: (g) => g.principalType ?? "—",
    },
    {
      id: "namespace",
      header: t("Namespace"),
      cell: (g) => g.namespaceId ?? "—",
    },
    {
      id: "grantedAt",
      header: t("Granted at"),
      cell: (g) => formatTimestamp(g.grantedAt),
    },
    {
      id: "grantedBy",
      header: t("Granted by"),
      cell: (g) => g.grantedBy ?? "—",
    },
  ];

  return (
    <SpaceBetween size="l">
      <Header variant="h1">{t("Profile")}</Header>

      <Container header={<Header variant="h2">{t("Identity")}</Header>}>
        <KeyValuePairs
          columns={3}
          items={[
            { label: t("Name"), value: user.name || "—" },
            { label: t("Email"), value: user.email || "—" },
            {
              label: t("Group / Team"),
              value:
                user.groups.length > 0 ? (
                  <SpaceBetween direction="horizontal" size="xs">
                    {user.groups.map((g) => (
                      <Badge key={g}>{g}</Badge>
                    ))}
                  </SpaceBetween>
                ) : (
                  "—"
                ),
            },
          ]}
        />
      </Container>

      <Tabs
        tabs={[
          {
            id: "roles",
            label: t("Roles ({count})", { count: grants.length }),
            content: (
              <SpaceBetween size="s">
                {/* A failed grants query must not read as "you have no roles":
                    an empty table is indistinguishable from a genuinely
                    unprivileged account, which is the same silent-wrong-view
                    failure the handler's fan-out fix removes server-side. */}
                {grantsError && (
                  <Alert type="error" header={t("Failed to load your roles")}>
                    {grantsError.message}
                  </Alert>
                )}
                <Table<GrantSummary>
                  variant="container"
                  items={grants}
                  columnDefinitions={grantColumns}
                  trackBy="grantId"
                  loading={grantsLoading}
                  loadingText={t("Loading roles...")}
                  header={
                    <Header
                      variant="h2"
                      counter={`(${grants.length})`}
                      actions={
                        <Button
                          iconName="refresh"
                          loading={isFetchingGrants}
                          onClick={() => refetchGrants()}
                        >
                          {t("Refresh")}
                        </Button>
                      }
                    >
                      {t("Your roles")}
                    </Header>
                  }
                  empty={
                    <Box textAlign="center" color="text-body-secondary">
                      {grantsError
                        ? t("Roles could not be loaded.")
                        : t("No roles assigned.")}
                    </Box>
                  }
                />
              </SpaceBetween>
            ),
          },
          {
            id: "namespaces",
            label: t("Namespaces ({count})", { count: namespaces.length }),
            content: (
              <Table<NamespaceSummary>
                variant="container"
                items={namespaces}
                columnDefinitions={namespaceColumns(t)}
                trackBy="namespaceId"
                header={
                  <Header variant="h2" counter={`(${namespaces.length})`}>
                    {t("Namespace memberships")}
                  </Header>
                }
                empty={
                  <Box textAlign="center" color="text-body-secondary">
                    {t("No namespace memberships.")}
                  </Box>
                }
              />
            ),
          },
        ]}
      />

      <Modal
        visible={roleDetail !== null}
        onDismiss={() => setRoleDetail(null)}
        header={roleDetail?.name ?? t("Role details")}
        size="large"
      >
        {detailLoading ? (
          <Spinner />
        ) : roleDetail ? (
          <SpaceBetween size="m">
            <KeyValuePairs
              columns={2}
              items={[
                { label: t("Role"), value: roleDetail.name ?? "—" },
                { label: t("Scope"), value: roleDetail.scope ?? "—" },
                {
                  label: t("Description"),
                  value: roleDetail.description ?? "—",
                },
                {
                  label: t("Built-in"),
                  value: roleDetail.isBuiltIn ? t("Yes") : t("No"),
                },
              ]}
            />
            {roleDetail.cedarPolicy && (
              <Container
                header={<Header variant="h3">{t("Cedar policy")}</Header>}
              >
                <Box variant="code">
                  <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>
                    {roleDetail.cedarPolicy}
                  </pre>
                </Box>
              </Container>
            )}
          </SpaceBetween>
        ) : null}
      </Modal>
    </SpaceBetween>
  );
};
