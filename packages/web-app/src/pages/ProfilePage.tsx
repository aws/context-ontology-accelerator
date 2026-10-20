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

const nsColumns: TableProps.ColumnDefinition<NamespaceSummary>[] = [
  {
    id: "name",
    header: "Namespace",
    cell: (n) => n.displayName ?? n.name ?? "",
    isRowHeader: true,
  },
  { id: "status", header: "Status", cell: (n) => n.status ?? "—" },
  {
    id: "sources",
    header: "Sources",
    cell: (n) => n.sourceCount ?? 0,
  },
];

export const ProfilePage: React.FC = () => {
  const { user } = useUser();
  useBreadcrumbTrail([{ text: "Profile" }]);
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
        description: "Failed to load role details.",
      } as unknown as RoleDetail);
    } finally {
      setDetailLoading(false);
    }
  }

  const grantColumns: TableProps.ColumnDefinition<GrantSummary>[] = [
    {
      id: "role",
      header: "Role",
      isRowHeader: true,
      cell: (g) => <Link onFollow={() => handleRoleClick(g)}>{g.role}</Link>,
    },
    {
      id: "principalType",
      header: "Principal type",
      cell: (g) => g.principalType ?? "—",
    },
    { id: "namespace", header: "Namespace", cell: (g) => g.namespaceId ?? "—" },
    {
      id: "grantedAt",
      header: "Granted at",
      cell: (g) => formatTimestamp(g.grantedAt),
    },
    { id: "grantedBy", header: "Granted by", cell: (g) => g.grantedBy ?? "—" },
  ];

  return (
    <SpaceBetween size="l">
      <Header variant="h1">Profile</Header>

      <Container header={<Header variant="h2">Identity</Header>}>
        <KeyValuePairs
          columns={3}
          items={[
            { label: "Name", value: user.name || "—" },
            { label: "Email", value: user.email || "—" },
            {
              label: "Group / Team",
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
            label: `Roles (${grants.length})`,
            content: (
              <SpaceBetween size="s">
                {/* A failed grants query must not read as "you have no roles":
                    an empty table is indistinguishable from a genuinely
                    unprivileged account, which is the same silent-wrong-view
                    failure the handler's fan-out fix removes server-side. */}
                {grantsError && (
                  <Alert type="error" header="Failed to load your roles">
                    {grantsError.message}
                  </Alert>
                )}
                <Table<GrantSummary>
                  variant="container"
                  items={grants}
                  columnDefinitions={grantColumns}
                  trackBy="grantId"
                  loading={grantsLoading}
                  loadingText="Loading roles..."
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
                          Refresh
                        </Button>
                      }
                    >
                      Your roles
                    </Header>
                  }
                  empty={
                    <Box textAlign="center" color="text-body-secondary">
                      {grantsError
                        ? "Roles could not be loaded."
                        : "No roles assigned."}
                    </Box>
                  }
                />
              </SpaceBetween>
            ),
          },
          {
            id: "namespaces",
            label: `Namespaces (${namespaces.length})`,
            content: (
              <Table<NamespaceSummary>
                variant="container"
                items={namespaces}
                columnDefinitions={nsColumns}
                trackBy="namespaceId"
                header={
                  <Header variant="h2" counter={`(${namespaces.length})`}>
                    Namespace memberships
                  </Header>
                }
                empty={
                  <Box textAlign="center" color="text-body-secondary">
                    No namespace memberships.
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
        header={roleDetail?.name ?? "Role details"}
        size="large"
      >
        {detailLoading ? (
          <Spinner />
        ) : roleDetail ? (
          <SpaceBetween size="m">
            <KeyValuePairs
              columns={2}
              items={[
                { label: "Role", value: roleDetail.name ?? "—" },
                { label: "Scope", value: roleDetail.scope ?? "—" },
                { label: "Description", value: roleDetail.description ?? "—" },
                {
                  label: "Built-in",
                  value: roleDetail.isBuiltIn ? "Yes" : "No",
                },
              ]}
            />
            {roleDetail.cedarPolicy && (
              <Container header={<Header variant="h3">Cedar policy</Header>}>
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
