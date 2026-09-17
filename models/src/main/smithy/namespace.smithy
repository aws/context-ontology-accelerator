// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Namespace CRUDL operations
$version: "2"

namespace com.amazon.semanticcontext.namespace

use com.amazon.semanticcontext.common#AccessDeniedError
use com.amazon.semanticcontext.common#ConflictError
use com.amazon.semanticcontext.common#NotFoundError
use com.amazon.semanticcontext.common#PaginatedInput
use com.amazon.semanticcontext.common#PaginatedOutput
use com.amazon.semanticcontext.common#Uuid
use com.amazon.semanticcontext.common#ValidationError

// ──────────────────────────────────────────────
// Constrained primitives
// ──────────────────────────────────────────────
/// Namespace name: 3-64 lowercase alphanumeric or hyphens, must start/end with alphanumeric.
@pattern("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
@length(min: 3, max: 64)
string NamespaceName

/// Display name for a namespace.
@length(min: 1, max: 256)
string NamespaceDisplayName

/// Namespace description.
@length(min: 1, max: 1024)
string NamespaceDescription

/// Owner email address.
@pattern("^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$")
string OwnerEmail

// ──────────────────────────────────────────────
// Enums
// ──────────────────────────────────────────────
/// Lifecycle state of a namespace, from active use through the asynchronous deletion pipeline.
enum NamespaceStatus {
    /// The namespace is active and accepting reads and writes.
    ACTIVE

    /// The namespace is archived and read-only; mutations are rejected.
    ARCHIVED

    /// Deletion pipeline is running (async cleanup of child resources in
    /// progress). Set immediately when DELETE /namespaces/{id} is accepted;
    /// cleared to DELETED on success or DELETE_FAILED on error.
    DELETING

    /// The namespace and its resources have been fully deleted.
    DELETED

    /// The deletion pipeline failed; the namespace may be partially deleted.
    DELETE_FAILED
}

/// Health of the namespace's VKG translation service. HEALTHY: running and able
/// to translate. UNAVAILABLE: exists but not serving. PROVISIONING: service is
/// starting up (deployment in progress, not yet serving) — expected to become
/// HEALTHY shortly. UNKNOWN: not deployed yet or state undetermined.
enum VkgHealthStatus {
    /// Running and able to translate queries.
    HEALTHY

    /// A task is running but its container health check is failing, so the
    /// service cannot answer queries (e.g. the published R2RML/schema could not
    /// be loaded). Distinct from UNAVAILABLE, which means no task is running.
    DEGRADED

    /// The service exists but is not currently serving (no running task).
    UNAVAILABLE

    /// The service is starting up (deployment in progress); expected to become HEALTHY shortly.
    PROVISIONING

    /// Not deployed yet, or the state could not be determined.
    UNKNOWN
}

// ──────────────────────────────────────────────
// Structures
// ──────────────────────────────────────────────
/// Condensed view of a namespace as returned in list responses.
structure NamespaceSummary {
    @required
    namespaceId: Uuid

    @required
    name: NamespaceName

    displayName: NamespaceDisplayName

    @required
    status: NamespaceStatus

    /// Number of data sources registered in the namespace.
    sourceCount: Integer

    /// Creation timestamp (RFC 3339).
    createdAt: String

    /// Last-update timestamp (RFC 3339).
    updatedAt: String
}

/// Full detail view of a namespace, including owner, description, and system-managed fields.
structure NamespaceDetail {
    @required
    namespaceId: Uuid

    @required
    name: NamespaceName

    displayName: NamespaceDisplayName

    description: NamespaceDescription

    @required
    owner: OwnerEmail

    @required
    status: NamespaceStatus

    /// Amazon DataZone project ID linked to this namespace, when applicable.
    dataZoneProjectId: String

    /// Number of data sources registered in the namespace.
    sourceCount: Integer

    /// Creation timestamp (RFC 3339).
    @required
    createdAt: String

    /// Last-update timestamp (RFC 3339).
    updatedAt: String

    /// Name of the Athena workgroup created for this namespace
    /// (read-only, system-managed). Use this workgroup when querying
    /// data sources via Athena federation. Format: `{resource-prefix}{namespaceId}`.
    /// Null when workgroup provisioning could not be confirmed
    /// (e.g., SSM parameter missing) — in that case Athena queries
    /// against this namespace's federated catalogs are not yet supported
    /// and a re-creation is required to enable them.
    athenaWorkgroupName: String

    /// STS ExternalId the platform presents when assuming a cross-account role
    /// onboarded into this namespace (read-only, system-derived). Format:
    /// `{resource-prefix}{namespaceId}`, the same scheme as
    /// `athenaWorkgroupName`.
    ///
    /// Any role supplied as `crossAccountRoleArn` MUST condition its trust policy
    /// on this exact value (`sts:ExternalId`). The platform derives it server-side
    /// and refuses to assume a role without presenting one, so this is what stops
    /// a caller in one namespace from pointing a source at a role onboarded for
    /// another. Surface it wherever a cross-account role is configured.
    datasourceExternalId: String

    /// Health of this namespace's VKG translation service (read-time).
    vkgHealth: VkgHealthStatus

    /// Human-readable explanation for a non-HEALTHY vkgHealth, when known
    /// (e.g. "container health check failing", "no running task"). Absent when
    /// HEALTHY. Coarse by design — the precise container-side cause is in the
    /// VKG service /health response and its CloudWatch logs.
    vkgHealthReason: String
}

list NamespaceSummaryList {
    member: NamespaceSummary
}

// ──────────────────────────────────────────────
// CreateNamespace
// ──────────────────────────────────────────────
/// Create a new namespace to hold data sources, ontologies, and grants.
@idempotent
@http(method: "POST", uri: "/namespaces", code: 201)
operation CreateNamespace {
    input := {
        @required
        name: NamespaceName

        displayName: NamespaceDisplayName

        description: NamespaceDescription

        @required
        owner: OwnerEmail
    }

    output: CreateNamespaceOutput

    errors: [
        ValidationError
        ConflictError
        AccessDeniedError
    ]
}

/// Output of CreateNamespace, wrapping the newly created namespace detail.
structure CreateNamespaceOutput {
    @required
    namespace: NamespaceDetail
}

// ──────────────────────────────────────────────
// GetNamespace
// ──────────────────────────────────────────────
/// Retrieve the full detail of a single namespace by its identifier.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}")
operation GetNamespace {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid
    }

    output: GetNamespaceOutput

    errors: [
        NotFoundError
        AccessDeniedError
    ]
}

/// Output of GetNamespace, wrapping the requested namespace detail.
structure GetNamespaceOutput {
    @required
    namespace: NamespaceDetail
}

// ──────────────────────────────────────────────
// ListNamespaces
// ──────────────────────────────────────────────
/// List namespaces the caller can access, with optional pagination.
@readonly
@http(method: "GET", uri: "/namespaces")
operation ListNamespaces {
    input := with [PaginatedInput] {
        /// Opaque token from a prior response; pass it to fetch the next page.
        @httpQuery("nextToken")
        nextToken: String

        /// Maximum number of namespaces to return in a single page.
        @httpQuery("maxResults")
        maxResults: Integer
    }

    output: ListNamespacesOutput

    errors: [
        AccessDeniedError
    ]
}

/// Output of ListNamespaces: a page of namespace summaries plus an optional pagination token.
structure ListNamespacesOutput with [PaginatedOutput] {
    /// The page of namespace summaries.
    @required
    namespaces: NamespaceSummaryList
}

// ──────────────────────────────────────────────
// UpdateNamespace
// ──────────────────────────────────────────────
/// Update the mutable fields (display name, description) of an existing namespace.
@idempotent
@http(method: "PUT", uri: "/namespaces/{namespaceId}")
operation UpdateNamespace {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// New human-readable display name for the namespace.
        displayName: String

        /// New description for the namespace.
        description: String
    }

    output: UpdateNamespaceOutput

    errors: [
        ValidationError
        NotFoundError
        ConflictError
        AccessDeniedError
    ]
}

/// Output of UpdateNamespace, wrapping the updated namespace detail.
structure UpdateNamespaceOutput {
    @required
    namespace: NamespaceDetail
}

// ──────────────────────────────────────────────
// DeleteNamespace
// ──────────────────────────────────────────────
/// Delete a namespace, triggering the asynchronous cleanup of its child resources.
@idempotent
@http(method: "DELETE", uri: "/namespaces/{namespaceId}", code: 202)
operation DeleteNamespace {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid
    }

    output := {
        @required
        namespaceId: Uuid

        @required
        status: NamespaceStatus
    }

    errors: [
        NotFoundError
        ConflictError
        AccessDeniedError
    ]
}

// ──────────────────────────────────────────────
// UpdateNamespaceStatus
// ──────────────────────────────────────────────
/// Change a namespace's lifecycle status (e.g., archive or reactivate it).
@http(method: "PATCH", uri: "/namespaces/{namespaceId}/status")
operation UpdateNamespaceStatus {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        status: NamespaceStatus
    }

    output: UpdateNamespaceStatusOutput

    errors: [
        ValidationError
        NotFoundError
        ConflictError
        AccessDeniedError
    ]
}

/// Output of UpdateNamespaceStatus, wrapping the namespace detail with its new status.
structure UpdateNamespaceStatusOutput {
    @required
    namespace: NamespaceDetail
}
