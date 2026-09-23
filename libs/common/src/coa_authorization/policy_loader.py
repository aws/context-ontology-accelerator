# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Load Cedar policies from the Roles DynamoDB table.

Centralizes the DDB lookup pattern used by both the control-plane
authorizer Lambda and the MCP server grant resolver. Single source
of truth for partition key conventions and the cedarPolicy attribute.
"""

from __future__ import annotations

from collections.abc import Iterable

from coa_common.aws_config import sync_boto_config
from coa_common.dao import DynamoDBDAO

# Partition keys where role definitions live in the Roles table.
# GLOBAL: built-in roles seeded at deploy time (platform-admin, default, etc.)
# NAMESPACE_TEMPLATE: namespace-scoped role templates (namespace-owner, data-steward, etc.)
_ROLE_PARTITION_KEYS = ("GLOBAL", "NAMESPACE_TEMPLATE")

_CEDAR_POLICY_ATTR = "cedarPolicy"


def load_policies_for_roles(
    role_ids: Iterable[str],
    table_name: str,
    region: str,
) -> str:
    """Load and concatenate Cedar policy text for the given role IDs.

    For each role, checks both GLOBAL and NAMESPACE_TEMPLATE partitions
    (first match wins). Returns the concatenated policy text, or an empty
    string if no policies were found.

    Args:
        role_ids: Set of role identifiers to load (e.g., {"default", "data-analyst"}).
        table_name: DynamoDB Roles table name.
        region: AWS region for DynamoDB access.

    Returns:
        Concatenated Cedar policy text (newline-separated), or empty string.
    """
    # Both known callers (the control-plane authorizer Lambda and the MCP
    # server grant resolver) are on a synchronous request path. Fail fast
    # instead of inheriting the DAO's background 30-second/three-attempt
    # default (matches the Cedar policy loader's #1092 fix).
    dao = DynamoDBDAO(table_name, region=region, config=sync_boto_config())
    policies = ""

    for role_id in role_ids:
        for pk in _ROLE_PARTITION_KEYS:
            item = dao.get({"PK": pk, "SK": f"ROLE#{role_id}"})
            if item and item.get(_CEDAR_POLICY_ATTR):
                policy_text: str = item[_CEDAR_POLICY_ATTR]
                policies = f"{policies}\n{policy_text}" if policies else policy_text
                break

    return policies
