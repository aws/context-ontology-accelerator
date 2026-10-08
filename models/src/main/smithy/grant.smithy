// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Grant CRUD operations
$version: "2"

namespace com.amazon.semanticcontext.grant

use com.amazon.semanticcontext.common#AccessDeniedError
use com.amazon.semanticcontext.common#ConflictError
use com.amazon.semanticcontext.common#NotFoundError
use com.amazon.semanticcontext.common#PaginatedInput
use com.amazon.semanticcontext.common#PaginatedOutput
use com.amazon.semanticcontext.common#Uuid

// ──────────────────────────────────────────────
// Constrained primitives
// ──────────────────────────────────────────────
/// Principal identifier: an email, IdP subject, group name, or agent ID.
/// Markup/quote characters (< > " ` \), control characters and the reserved key
/// delimiters # and | are rejected; `::` is also rejected server-side.
/// Keep in sync with PRINCIPAL_ID_RE in libs/common/src/coa_common/constants.py.
@pattern("^[^<>\"`\\\\#|\\x00-\\x1F\\x7F]+$")
@length(min: 1, max: 256)
string PrincipalId

// ──────────────────────────────────────────────
// Enums
// ──────────────────────────────────────────────
/// The kind of principal a grant is bound to.
enum PrincipalType {
    /// An individual user identity.
    USER = "User"

    /// A group of users; the grant applies to all members.
    GROUP = "Group"

    /// A non-human agent identity (e.g. a service or automation).
    AGENT = "Agent"
}

// ──────────────────────────────────────────────
// Structures
// ──────────────────────────────────────────────
/// A namespace grant record returned in responses, describing the principal, the
/// role assigned to it, and any table allowlist, column denylist, or allowed-metrics scoping within the namespace.
structure GrantSummary {
    /// Unique identifier of the grant.
    @required
    grantId: String

    /// Identifier of the namespace the grant applies to.
    @required
    namespaceId: String

    /// Whether the principal is a user, group, or agent.
    @required
    principalType: PrincipalType

    /// Identifier of the principal the grant is bound to.
    @required
    principalId: String

    /// Name of the role assigned to the principal.
    @required
    role: String

    /// Identifier of the principal that created the grant.
    @required
    grantedBy: String

    /// Timestamp when the grant was created.
    @required
    grantedAt: String

    /// Optional list of tables the grant restricts data access to.
    tableAllowlist: TableAllowlist

    /// Optional per-table columns the grant denies access to.
    columnDenylist: ColumnDenylist

    /// Optional list of metrics the grant permits access to.
    allowedMetrics: MetricsList
}

/// The request body for creating a namespace-scoped grant, binding a principal to
/// a role and optionally scoping data access via a table allowlist, column denylist, and allowed-metrics list.
structure CreateGrantRequest {
    /// Whether the principal is a user, group, or agent.
    @required
    principalType: PrincipalType

    /// Identifier of the principal to bind to the role.
    @required
    principalId: PrincipalId

    /// Name of the role to assign to the principal.
    @required
    role: String

    /// Optional list of tables to restrict data access to.
    tableAllowlist: TableAllowlist

    /// Optional per-table columns to deny access to.
    columnDenylist: ColumnDenylist

    /// Optional list of metrics to permit access to.
    allowedMetrics: MetricsList
}

list TableAllowlist {
    member: String
}

list MetricsList {
    member: String
}

map ColumnDenylist {
    key: String
    value: ColumnList
}

list ColumnList {
    member: String
}

list GrantSummaryList {
    member: GrantSummary
}

// ──────────────────────────────────────────────
// Platform-scoped grant structures
// ──────────────────────────────────────────────
// Platform grants bind a principal to a PLATFORM-scoped role (e.g.
// platform-admin, platform-viewer) that applies across all namespaces.
// They carry no namespaceId and no per-namespace data-access overrides.
/// A platform-scoped grant record returned in responses, binding a principal to a
/// platform role that applies across all namespaces (no namespaceId or per-namespace data-access scoping).
structure PlatformGrantSummary {
    /// Unique identifier of the platform grant.
    @required
    grantId: String

    /// Whether the principal is a user, group, or agent.
    @required
    principalType: PrincipalType

    /// Identifier of the principal the grant is bound to.
    @required
    principalId: String

    /// Name of the platform role assigned to the principal.
    @required
    role: String

    /// Identifier of the principal that created the grant.
    @required
    grantedBy: String

    /// Timestamp when the grant was created.
    @required
    grantedAt: String
}

/// The request body for creating a platform-scoped grant, binding a principal to a
/// platform role that applies across all namespaces.
structure CreatePlatformGrantRequest {
    /// Whether the principal is a user, group, or agent.
    @required
    principalType: PrincipalType

    /// Identifier of the principal to bind to the platform role.
    @required
    principalId: PrincipalId

    /// Name of the platform role to assign to the principal.
    @required
    role: String
}

list PlatformGrantSummaryList {
    member: PlatformGrantSummary
}

// ──────────────────────────────────────────────
// CreateGrant
// ──────────────────────────────────────────────
/// Grants a principal a role within a namespace, optionally scoping data access
/// via a table allowlist, column denylist, and allowed-metrics list.
@http(method: "POST", uri: "/namespaces/{namespaceId}/grants")
operation CreateGrant {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpPayload
        body: CreateGrantRequest
    }

    output := {
        @required
        grant: GrantSummary
    }

    errors: [
        AccessDeniedError
        NotFoundError
        ConflictError
    ]
}

// ──────────────────────────────────────────────
// ListNamespaceGrants
// ──────────────────────────────────────────────
/// Lists the grants defined within a namespace, optionally filtered to a single
/// principal, with cursor-based pagination.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/grants")
operation ListNamespaceGrants {
    input := with [PaginatedInput] {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Optional principal identifier to filter the grants by.
        @httpQuery("principal")
        principal: String

        /// Pagination cursor from a previous response.
        @httpQuery("nextToken")
        nextToken: String

        /// Maximum number of grants to return per page.
        @httpQuery("maxResults")
        maxResults: Integer
    }

    output := with [PaginatedOutput] {
        /// The page of namespace grants.
        @required
        grants: GrantSummaryList
    }

    errors: [
        AccessDeniedError
        NotFoundError
    ]
}

// ──────────────────────────────────────────────
// DeleteGrant
// ──────────────────────────────────────────────
/// Revokes a namespace grant by id, removing the principal's role and any
/// associated data-access scoping within the namespace.
@idempotent
@http(method: "DELETE", uri: "/namespaces/{namespaceId}/grants/{grantId}")
operation DeleteGrant {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the grant to revoke.
        @required
        @httpLabel
        grantId: String
    }

    output := {}

    errors: [
        AccessDeniedError
        NotFoundError
    ]
}

// ──────────────────────────────────────────────
// ListPrincipalGrants
// ──────────────────────────────────────────────
/// Lists every namespace grant held by a single principal across all namespaces.
@readonly
@http(method: "GET", uri: "/principals/{principalId}/grants")
operation ListPrincipalGrants {
    input := {
        /// Identifier of the principal whose grants to list.
        @required
        @httpLabel
        principalId: String
    }

    output := {
        /// The namespace grants held by the principal.
        @required
        grants: GrantSummaryList
    }

    errors: [
        AccessDeniedError
    ]
}

// ──────────────────────────────────────────────
// CreatePlatformGrant
// ──────────────────────────────────────────────
// Binds a principal to a PLATFORM-scoped role. The role must exist as a
// platform role (PK=GLOBAL in the roles table). Stored under the synthetic
// "GLOBAL" platform resource so it applies across all namespaces.
/// Binds a principal to a platform-scoped role that applies across all
/// namespaces. The role must already exist as a platform role.
@http(method: "POST", uri: "/grants")
operation CreatePlatformGrant {
    input := {
        @required
        @httpPayload
        body: CreatePlatformGrantRequest
    }

    output := {
        @required
        grant: PlatformGrantSummary
    }

    errors: [
        AccessDeniedError
        NotFoundError
        ConflictError
    ]
}

// ──────────────────────────────────────────────
// ListPlatformGrants
// ──────────────────────────────────────────────
/// Lists platform-scoped grants, optionally filtered to a single principal,
/// with cursor-based pagination.
@readonly
@http(method: "GET", uri: "/grants")
operation ListPlatformGrants {
    input := with [PaginatedInput] {
        /// Optional principal identifier to filter the grants by.
        @httpQuery("principal")
        principal: String

        /// Pagination cursor from a previous response.
        @httpQuery("nextToken")
        nextToken: String

        /// Maximum number of grants to return per page.
        @httpQuery("maxResults")
        maxResults: Integer
    }

    output := with [PaginatedOutput] {
        /// The page of platform grants.
        @required
        grants: PlatformGrantSummaryList
    }

    errors: [
        AccessDeniedError
    ]
}

// ──────────────────────────────────────────────
// DeletePlatformGrant
// ──────────────────────────────────────────────
/// Revokes a platform-scoped grant by id, removing the principal's platform
/// role across all namespaces.
@idempotent
@http(method: "DELETE", uri: "/grants/{grantId}")
operation DeletePlatformGrant {
    input := {
        /// Identifier of the platform grant to revoke.
        @required
        @httpLabel
        grantId: String
    }

    output := {}

    errors: [
        AccessDeniedError
        NotFoundError
    ]
}
