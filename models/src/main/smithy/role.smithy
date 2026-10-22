// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Role CRUDL operations
$version: "2"

namespace com.amazon.semanticcontext.role

use com.amazon.semanticcontext.common#AccessDeniedError
use com.amazon.semanticcontext.common#NotFoundError
use com.amazon.semanticcontext.common#Uuid

/// Human-readable role name (lowercase alphanumeric and hyphens).
@pattern("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
@length(min: 3, max: 64)
string RoleName

/// Role description.
@length(min: 1, max: 1024)
string RoleDescription

// ──────────────────────────────────────────────
// Enums
// ──────────────────────────────────────────────
/// Scope at which a role applies: platform-wide or within a single namespace.
enum RoleScope {
    /// The role applies platform-wide, across all namespaces.
    PLATFORM

    /// The role applies within a single namespace.
    NAMESPACE
}

// ──────────────────────────────────────────────
// Structures
// ──────────────────────────────────────────────
/// Summary view of a role, returned in role listings.
structure RoleSummary {
    /// Unique identifier of the role.
    @required
    roleId: String

    @required
    name: RoleName

    description: RoleDescription

    @required
    scope: RoleScope

    /// Whether this is a built-in role (true) or a custom role (false).
    @required
    isBuiltIn: Boolean
}

/// Full detail of a role, including its Cedar authorization policy.
structure RoleDetail {
    /// Unique identifier of the role.
    @required
    roleId: String

    @required
    name: RoleName

    description: RoleDescription

    @required
    scope: RoleScope

    /// Whether this is a built-in role (true) or a custom role (false).
    @required
    isBuiltIn: Boolean

    /// The Cedar policy document defining the role's permissions.
    cedarPolicy: String
}

/// Roles are a small fixed set: the built-in roles seeded at deployment and
/// copied into each namespace when it is created. No operation creates roles,
/// so the role listings are returned whole rather than paginated; the bound
/// documents that guarantee.
@length(max: 100)
list RoleSummaryList {
    member: RoleSummary
}

// ──────────────────────────────────────────────
// ListPlatformRoles
// ──────────────────────────────────────────────
/// List the platform-scoped roles available across the deployment. Not
/// paginated: the role set is small and fixed (see `RoleSummaryList`).
@readonly
@http(method: "GET", uri: "/roles")
operation ListPlatformRoles {
    input := {}
    output: ListPlatformRolesOutput
    errors: [
        AccessDeniedError
    ]
}

/// Output containing the list of platform-scoped roles.
structure ListPlatformRolesOutput {
    /// The platform-scoped roles.
    @required
    roles: RoleSummaryList
}

// ──────────────────────────────────────────────
// ListNamespaceRoles
// ──────────────────────────────────────────────
/// List the roles available within a specific namespace. Not paginated: the
/// role set is small and fixed (see `RoleSummaryList`).
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/roles")
operation ListNamespaceRoles {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid
    }

    output: ListNamespaceRolesOutput

    errors: [
        NotFoundError
        AccessDeniedError
    ]
}

/// Output containing the list of roles within a namespace.
structure ListNamespaceRolesOutput {
    /// The roles available within the namespace.
    @required
    roles: RoleSummaryList
}

// ──────────────────────────────────────────────
// GetNamespaceRole
// ──────────────────────────────────────────────
/// Retrieve the full detail of a single role within a namespace.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/roles/{roleId}")
operation GetNamespaceRole {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Unique identifier of the role to retrieve.
        @required
        @httpLabel
        @length(min: 1, max: 128)
        @pattern("^[A-Za-z0-9_-]+$")
        roleId: String
    }

    output: GetNamespaceRoleOutput

    errors: [
        NotFoundError
        AccessDeniedError
    ]
}

/// Output containing the requested role's detail.
structure GetNamespaceRoleOutput {
    @required
    role: RoleDetail
}
