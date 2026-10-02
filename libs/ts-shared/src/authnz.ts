// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Type of principal that can be granted a role. */
export enum PrincipalType {
  /** End-user authenticated via IdP. */
  USER = "User",
  /** Service or machine identity. */
  AGENT = "Agent",
  /** IdP group containing multiple users. */
  GROUP = "Group",
}

/**
 * URL-encode a principal identifier for use in a DynamoDB principal key.
 *
 * Must stay byte-identical to `sanitize_principal_key` in
 * `libs/common/src/coa_common/principal_keys.py`, because every reader encodes
 * before querying the PrincipalIndex GSI: a key written under a different
 * encoding is silently unresolvable rather than an error. `encodeURIComponent`
 * alone is not equivalent, it leaves `!'()*` raw and escapes `@` where Python
 * does the opposite.
 */
export function sanitizePrincipalKey(value: string): string {
  const normalized = value.includes("@") ? value.trim().toLowerCase() : value;
  return encodeURIComponent(normalized)
    .replace(
      /[!'()*]/g,
      (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`,
    )
    .replace(/%40/g, "@");
}

/** Type of resource a role can be scoped to. */
export enum ResourceType {
  NAMESPACE = "Namespace",
  DATA_SOURCE = "DataSource",
  TABLE = "Table",
  METRIC = "Metric",
  DOC_SOURCE = "DocSource",
  /** Platform-wide scope; use resourceId "GLOBAL" for global roles. */
  PLATFORM = "Platform",
}

/**
 * DynamoDB record for the ResourceRoleMappings table.
 *
 * PK format: `<resourceType>::<resourceId>#<principalType>::<principalId>`
 * SK format: `ROLE#<roleId>`
 *
 * GSI PrincipalIndex:       PK=principalKey, SK=resourceRoleKey
 * GSI NamespaceGrantsIndex: PK=namespaceKey, SK=principalRoleKey
 */
export interface ResourceRoleMapping {
  PK: string;
  SK: string;
  resourceType: ResourceType;
  resourceId: string;
  principalType: PrincipalType;
  principalId: string;
  role: string;
  /** Format: `<PrincipalType>::<principalId>` */
  principalKey: string;
  /** Format: `<ResourceType>::<resourceId>#ROLE#<roleId>` */
  resourceRoleKey: string;
  /** Format: `NS#<namespaceId>` (or `NS#GLOBAL` for platform roles) */
  namespaceKey: string;
  /** Format: `<PrincipalType>::<principalId>#ROLE#<roleId>` */
  principalRoleKey: string;
  grantedBy: string;
  grantedAt: string;
  /** Allowed tables for fine-grained access. */
  tableAllowlist?: string[];
  /** Denied columns per table. */
  columnDenylist?: Record<string, string[]>;
  /** Row-level filter expressions per table. */
  rowFilters?: Record<string, string>;
  /** Allowed metric identifiers. */
  allowedMetrics?: string[];
  /** Cedar policy override for this grant. */
  cedarPolicy?: string;
}
