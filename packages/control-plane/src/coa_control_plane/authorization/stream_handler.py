# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""DynamoDB Streams handler that bumps the cache version counter.

Triggered by changes to Roles and ResourceRoleMappings tables.
The authorizer Lambda checks this version on each invocation and
clears its local role cache when a mismatch is detected.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.dao import DynamoDBDAO

logger = structlog.get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> None:  # noqa: ARG001
    """Process DynamoDB Stream records and bump the cache version counter."""
    table_name = os.environ.get("CACHE_INVALIDATION_TABLE_NAME", "")
    region = os.environ["AWS_REGION"]

    if not table_name:
        logger.error("CACHE_INVALIDATION_TABLE_NAME not set")
        return

    records = event.get("Records", [])
    if not records:
        return

    logger.info("Processing stream records", count=len(records))

    dao = DynamoDBDAO(table_name, region=region)

    dao.atomic_increment(
        key={"PK": "CACHE_VERSION", "SK": "CURRENT"},
        attribute="version",
    )
    logger.info("Cache version bumped")
