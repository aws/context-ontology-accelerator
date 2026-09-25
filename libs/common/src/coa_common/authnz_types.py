# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Canonical ResourceRoleMapping schema — single source of truth."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class PrincipalType(StrEnum):
    """Kind of principal a role grant is issued to."""

    USER = "User"
    AGENT = "Agent"
    GROUP = "Group"


class ResourceType(StrEnum):
    """Kind of resource a role grant applies to."""

    NAMESPACE = "Namespace"
    DATA_SOURCE = "DataSource"
    TABLE = "Table"
    METRIC = "Metric"
    DOC_SOURCE = "DocSource"
    PLATFORM = "Platform"


# The seeded ``default`` row in the roles table (PK=GLOBAL, SK=ROLE#default) is
# the baseline Cedar policy bundle that the authorizer loads for EVERY
# authenticated principal (policy_loader always includes it). It is not a role
# anyone can be granted: a GLOBAL grant carrying it makes the principal look
# like a cross-namespace role holder to filtering code (#988). Keep it out of
# the assignable platform-role surface.
NON_ASSIGNABLE_ROLE_IDS: frozenset[str] = frozenset({"default"})


class ResourceRoleMapping(BaseModel):
    """DynamoDB record for ResourceRoleMappings table.

    PK: `<resourceType>::<resourceId>#<principalType>::<principalId>`
    SK: `ROLE#<roleId>`

    GSI PrincipalIndex:        PK=principalKey, SK=resourceRoleKey
    GSI NamespaceGrantsIndex:  PK=namespaceKey, SK=principalRoleKey
    """

    # Keys
    PK: str
    SK: str

    # Core grant fields
    resourceType: ResourceType
    resourceId: str
    principalType: PrincipalType
    principalId: str
    role: str

    # GSI attributes
    principalKey: str
    resourceRoleKey: str
    namespaceKey: str
    principalRoleKey: str

    # Provenance
    grantedBy: str
    grantedAt: str

    # Optional overrides
    tableAllowlist: list[str] | None = None
    columnDenylist: dict[str, list[str]] | None = None
    rowFilters: dict[str, str] | None = None
    allowedMetrics: list[str] | None = None
    cedarPolicy: str | None = None
