# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Factory for building the SMUS-backed data source lookup for metric validation.

Resolves namespace → DataZone project → SmusCatalogDataSourceLookup.

Required env vars (set by MetricServiceStack):
  DATA_SOURCES_TABLE      — unified sources table (PK=NS#, SK=SRC#)
  NAMESPACES_TABLE        — namespace registry (resolves dataZoneProjectId)
  SMUS_DOMAIN_ID          — DataZone domain ID
  PROJECT_ACCESS_ROLE_ARN — role assumed by the catalog reader for DataZone

Returns None when config is missing or the namespace has no provisioned
DataZone project — callers skip Checks 2-5 gracefully. Registry read failures
propagate so enforcement callers can return an operational error instead of
silently accepting unverified references.
"""

from __future__ import annotations

import os

import structlog
from botocore.exceptions import ClientError
from coa_common.dao import DynamoDBDAO

from coa_metrics.lookups import DataSourceLookup, SmusCatalogDataSourceLookup

logger = structlog.get_logger(__name__)


def _resolve_project_id(namespace_id: str, namespaces_table: str, region: str) -> str | None:
    """Resolve the DataZone project ID for a namespace.

    Uses DynamoDBDAO from common library. A missing row or project ID is a
    legitimate unprovisioned namespace and returns None. DynamoDB failures
    propagate so callers can distinguish an outage from that state.
    """
    try:
        dao = DynamoDBDAO(namespaces_table, region=region)
        item = dao.get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "namespace_lookup_failed",
            namespace=namespace_id,
            error_code=error_code,
            error=str(exc),
        )
        raise
    except Exception as exc:
        logger.warning("namespace_lookup_failed", namespace=namespace_id, error=str(exc))
        raise

    if not item:
        logger.warning("namespace_not_found", namespace=namespace_id, table=namespaces_table)
        return None

    project_id = item.get("dataZoneProjectId")
    if not project_id:
        logger.warning("namespace_missing_project", namespace=namespace_id)
        return None
    return project_id


def build_data_source_lookup(namespace_id: str) -> DataSourceLookup | None:
    """Build the SMUS-backed data source lookup for a namespace.

    Returns None if required configuration is missing or the namespace has
    no DataZone project.
    """
    sources_table = os.environ.get("DATA_SOURCES_TABLE", "")
    namespaces_table = os.environ.get("NAMESPACES_TABLE", "")
    domain_id = os.environ.get("SMUS_DOMAIN_ID", "")
    region = os.environ.get("AWS_REGION", "us-east-1")

    missing = [
        name
        for name, val in (
            ("DATA_SOURCES_TABLE", sources_table),
            ("NAMESPACES_TABLE", namespaces_table),
            ("SMUS_DOMAIN_ID", domain_id),
        )
        if not val
    ]
    if missing:
        logger.warning("data_source_lookup_not_configured", missing=missing)
        return None

    project_id = _resolve_project_id(namespace_id, namespaces_table, region)
    if not project_id:
        return None

    return SmusCatalogDataSourceLookup(
        domain_id=domain_id,
        project_id=project_id,
        namespace_id=namespace_id,
        datasources_table=sources_table,
        region=region,
    )
