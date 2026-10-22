// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Grant CRUD operations
$version: "2"

namespace com.amazon.semanticcontext.grant

use com.amazon.semanticcontext.common#AccessDeniedError
use com.amazon.semanticcontext.common#ClientToken
use com.amazon.semanticcontext.common#ConflictError
use com.amazon.semanticcontext.common#NotFoundError
use com.amazon.semanticcontext.common#PaginatedInput
use com.amazon.semanticcontext.common#PaginatedOutput
use com.amazon.semanticcontext.common#PaginationToken
use com.amazon.semanticcontext.common#Uuid

// ──────────────────────────────────────────────
// Constrained primitives
// ──────────────────────────────────────────────
/// Principal identifier: an email, IdP subject, group name, or agent ID.
/// Markup/quote characters (< > " ` \), control characters and the reserved key
/// delimiters # and | are rejected; `::` is also rejected server-side.
/// Keep in sync with PRINCIPAL_ID_RE in libs/common/src/coa_common/constants.py.
/// Usually an email address, so marked sensitive to keep it out of
/// generated-client logs.
@pattern("^[^<>\"`\\\\#|\\x00-\\x1F\\x7F]+$")
@length(min: 1, max: 256)
@sensitive
string PrincipalId

/// A principal identifier as stored on a grant record (the grantee, or the
/// caller that created the grant). Output only, so unconstrained; usually an
/// email address, so marked sensitive to keep it out of generated-client logs.
@sensitive
string GrantPrincipal

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
    principalId: GrantPrincipal

    /// Name of the role assigned to the principal.
    @required
    role: String

    /// Identifier of the principal that created the grant.
    @required
    grantedBy: GrantPrincipal

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
    @length(min: 1, max: 128)
    role: String

    /// Optional list of tables to restrict data access to.
    @length(max: 1000)
    tableAllowlist: TableAllowlist

    /// Optional per-table columns to deny access to.
    @length(max: 1000)
    columnDenylist: ColumnDenylist

    /// Optional list of metrics to permit access to.
    @length(max: 1000)
    allowedMetrics: MetricsList

    /// Optional idempotency token. A retry with the same token and the same
    /// parameters within 24 hours returns the original result instead of creating
    /// a duplicate; reusing the token with different parameters is rejected with
    /// ConflictError (409).
    // The operation binds this whole structure as its HTTP payload, so the
    // token has to live here to travel in the JSON body. Smithy only honours
    // @idempotencyToken on top-level input members (generated clients will not
    // auto-fill it here); it is kept to mark the member's semantics.
    @idempotencyToken
    @suppress(["IdempotencyTokenIgnored"])
    clientToken: ClientToken
}

list TableAllowlist {
    @length(max: 1024)
    member: String
}

list MetricsList {
    @length(max: 256)
    member: String
}

map ColumnDenylist {
    @length(max: 1024)
    key: String

    @length(max: 1000)
    value: ColumnList
}

list ColumnList {
    @length(max: 1024)
    member: String
}

list GrantSummaryList {
    member: GrantSummary
}

/// Opaque grant identifier as returned in `GrantSummary$grantId`: URL-safe
/// base64 without padding. The 1,024-character bound is the API's request-path
/// limit, so it cannot reject an identifier that is reachable today.
@length(min: 1, max: 1024)
@pattern("^[A-Za-z0-9_-]+$")
string GrantId

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
    principalId: GrantPrincipal

    /// Name of the platform role assigned to the principal.
    @required
    role: String

    /// Identifier of the principal that created the grant.
    @required
    grantedBy: GrantPrincipal

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
    @length(min: 1, max: 128)
    role: String

    /// Optional idempotency token. A retry with the same token and the same
    /// parameters within 24 hours returns the original result instead of creating
    /// a duplicate; reusing the token with different parameters is rejected with
    /// ConflictError (409).
    // The operation binds this whole structure as its HTTP payload, so the
    // token has to live here to travel in the JSON body. Smithy only honours
    // @idempotencyToken on top-level input members (generated clients will not
    // auto-fill it here); it is kept to mark the member's semantics.
    @idempotencyToken
    @suppress(["IdempotencyTokenIgnored"])
    clientToken: ClientToken
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
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "grants")
operation ListNamespaceGrants {
    input := with [PaginatedInput] {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Optional principal identifier to filter the grants by.
        @httpQuery("principal")
        @length(max: 1024)
        principal: String

        /// Pagination cursor from a previous response.
        @httpQuery("nextToken")
        nextToken: PaginationToken

        /// Maximum number of grants to return per page.
        @httpQuery("maxResults")
        @range(min: 1, max: 100)
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
        grantId: GrantId
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
/// Lists the namespace grants held by a single principal (and the groups it
/// belongs to) across all namespaces, with cursor-based pagination. Only the
/// `me` principal (the caller) is accepted. The default page size is the
/// maximum, so a caller holding up to 1,000 grants gets them all in one page.
@readonly
@http(method: "GET", uri: "/principals/{principalId}/grants")
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "grants")
operation ListPrincipalGrants {
    input := {
        /// Identifier of the principal whose grants to list (`me`).
        @required
        @httpLabel
        principalId: PrincipalId

        /// Pagination cursor from a previous response.
        @httpQuery("nextToken")
        nextToken: PaginationToken

        /// Maximum number of grants to return per page (default 1000).
        @httpQuery("maxResults")
        @range(min: 1, max: 1000)
        maxResults: Integer
    }

    output := with [PaginatedOutput] {
        /// The page of namespace grants held by the principal.
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
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "grants")
operation ListPlatformGrants {
    input := with [PaginatedInput] {
        /// Optional principal identifier to filter the grants by.
        @httpQuery("principal")
        @length(max: 1024)
        principal: String

        /// Pagination cursor from a previous response.
        @httpQuery("nextToken")
        nextToken: PaginationToken

        /// Maximum number of grants to return per page.
        @httpQuery("maxResults")
        @range(min: 1, max: 100)
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
        grantId: GrantId
    }

    output := {}

    errors: [
        AccessDeniedError
        NotFoundError
    ]
}
