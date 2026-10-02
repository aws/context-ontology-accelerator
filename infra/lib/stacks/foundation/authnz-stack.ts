// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as cr from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";
import * as fs from "fs";
import * as path from "path";

import { SCLStack } from "../../constructs/scl-stack";
import { DynamoDBTable } from "../../constructs/dynamodb-table";
import { Paths } from "../../paths";
import { ClaimMapping } from "../../types";
import {
  PrincipalType,
  ResourceType,
  TABLE_NAMES,
  sanitizePrincipalKey,
} from "@coa/shared";

export interface AuthnzStackProps extends cdk.StackProps {
  /** Group → role claim mappings to seed into ResourceRoleMappings. */
  readonly claimsMappings?: ClaimMapping[];
}

/**
 * Foundation stack: DynamoDB tables and GSIs for the AuthNZ subsystem.
 *
 * Tables provisioned:
 *   - Roles                — namespace-scoped and global role definitions
 *   - ResourceRoleMappings — principal ↔ resource ↔ role grants with
 *                            PrincipalIndex and NamespaceGrantsIndex GSIs
 *   - CacheInvalidation    — single-row version counter for auth cache busting
 *
 * IdP group → role mappings are handled via ResourceRoleMappings with
 * principalType "Group" and the IdP group name as principalId. The
 * authorizer extracts group claims from the JWT and queries the
 * PrincipalIndex GSI directly — no separate claims mapping table needed.
 *
 * All tables use PAY_PER_REQUEST billing, AWS-managed encryption, PITR,
 * and env-aware removal policy (RETAIN in prod, DESTROY elsewhere).
 *
 * DynamoDB Streams (NEW_AND_OLD_IMAGES) are enabled on Roles and
 * ResourceRoleMappings to drive cache invalidation.
 */
export class AuthnzStack extends SCLStack {
  /** Roles table. */
  public readonly rolesTable: dynamodb.Table;

  /** ResourceRoleMappings table. */
  public readonly resourceRoleMappingsTable: dynamodb.Table;

  /** CacheInvalidation table. */
  public readonly cacheInvalidationTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props?: AuthnzStackProps) {
    super(scope, id, props);
    this.addComponentTag("authnz");

    const PK = { name: "PK", type: dynamodb.AttributeType.STRING };
    const SK = { name: "SK", type: dynamodb.AttributeType.STRING };

    // ── 1. Roles ─────────────────────────────────────────────────────
    const roles = new DynamoDBTable(this, "RolesTable", {
      tableName: TABLE_NAMES.ROLES,
      partitionKey: PK,
      sortKey: SK,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
    });
    this.rolesTable = roles.table;

    // ── 3. ResourceRoleMappings ──────────────────────────────────────
    const rrm = new DynamoDBTable(this, "ResourceRoleMappingsTable", {
      tableName: TABLE_NAMES.RESOURCE_ROLE_MAPPINGS,
      partitionKey: PK,
      sortKey: SK,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
    });

    rrm.addGlobalSecondaryIndex({
      indexName: "PrincipalIndex",
      partitionKey: {
        name: "principalKey",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "resourceRoleKey",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    rrm.addGlobalSecondaryIndex({
      indexName: "NamespaceGrantsIndex",
      partitionKey: {
        name: "namespaceKey",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "principalRoleKey",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.resourceRoleMappingsTable = rrm.table;

    // ── 4. CacheInvalidation ─────────────────────────────────────────
    const cache = new DynamoDBTable(this, "CacheInvalidationTable", {
      tableName: TABLE_NAMES.CACHE_INVALIDATION,
      partitionKey: PK,
      sortKey: SK,
    });
    this.cacheInvalidationTable = cache.table;

    // ── 5. Seed built-in roles ───────────────────────────────────────
    this.seedBuiltInRoles(roles.table);

    // ── 6. Seed group → role mappings from IdP claims ────────────────
    for (const mapping of props?.claimsMappings ?? []) {
      for (const roleId of mapping.mappedRoles) {
        const group = mapping.groupValue;
        // Key attributes carry the encoded form; principalId stays raw, and the
        // construct/physical ids stay raw to avoid logical-id churn on upgrade.
        const groupKey = sanitizePrincipalKey(group);
        new cr.AwsCustomResource(this, `SeedGroupMapping-${group}-${roleId}`, {
          onUpdate: {
            service: "DynamoDB",
            action: "putItem",
            parameters: {
              TableName: rrm.table.tableName,
              Item: {
                PK: {
                  S: `${ResourceType.PLATFORM}::GLOBAL#${PrincipalType.GROUP}::${groupKey}`,
                },
                SK: { S: `ROLE#${roleId}` },
                resourceType: { S: ResourceType.PLATFORM },
                resourceId: { S: "GLOBAL" },
                principalType: { S: PrincipalType.GROUP },
                principalId: { S: group },
                role: { S: roleId },
                principalKey: { S: `${PrincipalType.GROUP}::${groupKey}` },
                resourceRoleKey: {
                  S: `${ResourceType.PLATFORM}::GLOBAL#ROLE#${roleId}`,
                },
                namespaceKey: { S: "NS#GLOBAL" },
                principalRoleKey: {
                  S: `${PrincipalType.GROUP}::${groupKey}#ROLE#${roleId}`,
                },
                grantedBy: { S: "system" },
                grantedAt: { S: new Date().toISOString() },
              },
            },
            physicalResourceId: cr.PhysicalResourceId.of(
              `seed-group-mapping-${group}-${roleId}`,
            ),
          },
          policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
            resources: [rrm.table.tableArn],
          }),
        });
      }
    }
  }

  /**
   * Seeds built-in role definitions into the Roles table at deploy time.
   * Uses AwsCustomResource (SDK call) so roles are created/updated on
   * every deployment without a Lambda function.
   */
  private seedBuiltInRoles(rolesTable: dynamodb.Table): void {
    const seedDir = Paths.authorizationSeed;

    const builtInRoles: {
      roleId: string;
      name: string;
      scope: string;
      description: string;
      file: string;
    }[] = [
      {
        roleId: "platform-admin",
        name: "Platform Admin",
        scope: "GLOBAL",
        description: "Full access to all resources across all namespaces",
        file: "global_admin.cedar",
      },
      {
        roleId: "platform-viewer",
        name: "Platform Viewer",
        scope: "GLOBAL",
        description: "Read-only access across all namespaces",
        file: "global_viewer.cedar",
      },
      {
        roleId: "namespace-owner",
        name: "Namespace Owner",
        scope: "NAMESPACE",
        description: "Full control within a namespace",
        file: "namespace_owner.cedar",
      },
      {
        roleId: "data-steward",
        name: "Data Steward",
        scope: "NAMESPACE",
        description:
          "Manage data sources, ontologies, metrics, and documents within a namespace",
        file: "namespace_data_steward.cedar",
      },
      {
        roleId: "data-analyst",
        name: "Data Analyst",
        scope: "NAMESPACE",
        description: "Query and read access within a namespace",
        file: "namespace_data_analyst.cedar",
      },
      {
        roleId: "namespace-maintainer",
        name: "Namespace Maintainer",
        scope: "NAMESPACE",
        description: "Full control within a namespace",
        file: "namespace_maintainer.cedar",
      },
      {
        roleId: "default",
        name: "Default",
        scope: "GLOBAL",
        description: "Default policy applied to all authenticated users",
        file: "default.cedar",
      },
    ];

    for (const role of builtInRoles) {
      const cedarPolicy = fs.readFileSync(
        path.join(seedDir, role.file),
        "utf-8",
      );
      const pk = role.scope === "GLOBAL" ? "GLOBAL" : "NAMESPACE_TEMPLATE";

      new cr.AwsCustomResource(this, `SeedRole-${role.roleId}`, {
        onCreate: {
          service: "DynamoDB",
          action: "putItem",
          parameters: {
            TableName: rolesTable.tableName,
            Item: {
              PK: { S: pk },
              SK: { S: `ROLE#${role.roleId}` },
              name: { S: role.name },
              description: { S: role.description },
              isBuiltIn: { BOOL: true },
              cedarPolicy: { S: cedarPolicy },
              scope: { S: role.scope },
              createdAt: { S: new Date().toISOString() },
              updatedAt: { S: new Date().toISOString() },
            },
          },
          physicalResourceId: cr.PhysicalResourceId.of(
            `seed-role-${role.roleId}`,
          ),
        },
        onUpdate: {
          service: "DynamoDB",
          action: "putItem",
          parameters: {
            TableName: rolesTable.tableName,
            Item: {
              PK: { S: pk },
              SK: { S: `ROLE#${role.roleId}` },
              name: { S: role.name },
              description: { S: role.description },
              isBuiltIn: { BOOL: true },
              cedarPolicy: { S: cedarPolicy },
              scope: { S: role.scope },
              createdAt: { S: new Date().toISOString() },
              updatedAt: { S: new Date().toISOString() },
            },
          },
          physicalResourceId: cr.PhysicalResourceId.of(
            `seed-role-${role.roleId}`,
          ),
        },
        policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
          resources: [rolesTable.tableArn],
        }),
      });
    }
  }
}
