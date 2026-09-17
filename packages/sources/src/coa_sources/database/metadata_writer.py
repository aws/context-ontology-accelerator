# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metadata writer — persists discovered metadata to SageMaker Catalog (DataZone).

Creates DataZone assets for each discovered table, with column metadata
stored as forms. Supports idempotent upsert via CreateAssetRevision.

Scalability (see COE for the all-schemas bird-DB scan timeout):
  * Existing assets are pre-fetched ONCE via a paginated search scoped to the
    data source, instead of a per-table search (the old N+1 that doubled the
    DataZone call count and made 1k+ table scans exceed the 15-min Lambda
    timeout). Absence from the pre-fetched map is authoritative — the search is
    fully paginated — so a missing entry means "create", with no per-table
    lookup.
  * Asset writes are parallelized with a bounded ThreadPoolExecutor
    (DATAZONE_WRITE_PARALLELISM, default 10) — mirrors the bulk-review worker.
    Each table needs a single DataZone write (create OR revise).
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from coa_common.datazone_forms import (
    ASSET_TYPE_NAME,
    build_forms_input,
)
from coa_common.domain_models import DiscoveredMetadata
from coa_common.metadata_store import SMUSClient
from coa_common.metadata_store.exceptions import MetadataStoreError

from coa_sources.database.metrics import emit_metric

logger = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Parallel DataZone writes per discovery invocation. Tunable per environment.
# DataZone account-wide rate limits are protected by the scan queue's
# maxConcurrency (one discovery runs at a time per source).
DEFAULT_WRITE_PARALLELISM = 10

# DataZone search page size (service hard limit is 50).
_SEARCH_PAGE_SIZE = 50


def _write_parallelism() -> int:
    """Resolve the configured write parallelism (>= 1)."""
    try:
        return max(1, int(os.environ.get("DATAZONE_WRITE_PARALLELISM", str(DEFAULT_WRITE_PARALLELISM))))
    except (TypeError, ValueError):
        return DEFAULT_WRITE_PARALLELISM


def _build_existing_asset_map(client: SMUSClient, project_id: str, data_source_id: str) -> dict[str, str]:
    """Page through all existing assets for the data source.

    Returns a ``{asset_name: asset_id}`` map. Scoped by searching for the
    ``DS#<id>`` key and keeping only names prefixed with ``DS#<id>:`` (mirrors
    the bulk-review worker's loader). The search is fully paginated, so the
    returned map is authoritative for create-vs-revise decisions.
    """
    prefix = f"{data_source_id}:"
    asset_map: dict[str, str] = {}
    next_token: str | None = None
    while True:
        result = client.search_assets(
            project_id=project_id,
            search_text=data_source_id,
            max_results=_SEARCH_PAGE_SIZE,
            next_token=next_token,
        )
        for item in result.items:
            if item.name and item.name.startswith(prefix):
                asset_map[item.name] = item.asset_id
        next_token = result.next_token
        if not next_token:
            break
    return asset_map


def _is_access_denied(exc: Exception) -> bool:
    """Heuristic: does this error represent a DataZone authorization denial?"""
    text = str(exc)
    return "AccessDenied" in text or "not permitted to perform operation" in text


def write_to_datazone(
    *,
    domain_id: str,
    project_id: str,
    metadata: DiscoveredMetadata,
    data_source_id: str,
) -> dict[str, Any]:
    """Write discovered tables as DataZone assets in the namespace project.

    Returns:
        Dict with counts: assets_created, assets_revised.
    """
    if not data_source_id:
        raise ValueError("data_source_id is required")

    client = SMUSClient(
        domain_id=domain_id,
        region_name=AWS_REGION,
        assume_role_arn=os.environ.get("PROJECT_ACCESS_ROLE_ARN"),
        session_name="connector-service",
    )

    tables = list(metadata.tables)
    if not tables:
        return {"assets_created": 0, "assets_revised": 0}

    # One paginated read instead of a per-table search (de-N+1). On a fresh
    # source this returns an empty map and every table is a create.
    try:
        existing = _build_existing_asset_map(client, project_id, data_source_id)
    except MetadataStoreError as exc:
        if _is_access_denied(exc):
            logger.error(
                "DataZone Search denied: scan role %s is not a member of project %s",
                os.environ.get("PROJECT_ACCESS_ROLE_ARN", "unknown"),
                project_id,
            )
            raise MetadataStoreError(
                "DataZone denied access to this namespace's project. The scan role is not a "
                "member of the namespace's DataZone project. This is a project-membership "
                "problem, not a Glue/IAM one, and it cannot be fixed from the scan: only the "
                "project owner can add members. Reconcile the project membership and re-scan."
            ) from exc
        raise
    logger.info(
        "DataZone write starting: tables=%d existing_assets=%d datasource=%s",
        len(tables),
        len(existing),
        data_source_id,
    )

    def _write_one(table: Any) -> tuple[str, str | None]:
        """Create or revise a single asset. Returns (outcome, failed_name)."""
        asset_name = f"{data_source_id}:{table.table_id}"
        description = table.business_metadata.description or table.technical_metadata.format or f"Table: {table.name}"
        forms = build_forms_input(table)
        existing_asset_id = existing.get(asset_name)
        try:
            if existing_asset_id:
                client.create_asset_revision(
                    asset_id=existing_asset_id,
                    name=asset_name,
                    description=description,
                    forms_input=forms,
                )
                return "revised", None
            client.create_asset(
                project_id=project_id,
                name=asset_name,
                description=description,
                forms_input=forms,
                type_identifier=ASSET_TYPE_NAME,
            )
            return "created", None
        except Exception as exc:
            logger.error("Failed to write asset %s: %s", asset_name, exc)
            return "failed", asset_name

    assets_created = 0
    assets_revised = 0
    failures: list[str] = []

    with ThreadPoolExecutor(max_workers=_write_parallelism()) as pool:
        futures = [pool.submit(_write_one, t) for t in tables]
        for future in as_completed(futures):
            outcome, failed_name = future.result()
            if outcome == "created":
                assets_created += 1
            elif outcome == "revised":
                assets_revised += 1
            elif failed_name is not None:
                failures.append(failed_name)

    logger.info(
        "DataZone write: created=%d revised=%d failures=%d for datasource=%s",
        assets_created,
        assets_revised,
        len(failures),
        data_source_id,
    )

    # Aggregate counts, emitted once per discovery — NOT per asset. A 1k-table
    # scan would otherwise write 1k EMF lines for the same two numbers.
    # Create/Update only: the writer has no delete path.
    emit_metric("CatalogAssetWrites", assets_created, "Count", Operation="Create")
    emit_metric("CatalogAssetWrites", assets_revised, "Count", Operation="Update")

    if failures:
        raise RuntimeError(f"Failed to write {len(failures)} asset(s): {failures}")

    return {"assets_created": assets_created, "assets_revised": assets_revised}


def _find_existing_asset(client: SMUSClient, project_id: str, asset_name: str) -> str | None:
    """Search for a single existing asset by exact name in the project.

    Retained as a targeted single-asset lookup (e.g. for callers outside the
    bulk discovery path). The discovery write path uses
    :func:`_build_existing_asset_map` to avoid a per-table search.
    """
    try:
        # find_asset_by_name rather than a top-1 search: search scores every
        # searchable attribute, so a full asset name ranks siblings of the same
        # source alongside the exact one and the wanted asset is not reliably
        # first. A miss here re-creates an asset that already exists.
        asset = client.find_asset_by_name(project_id=project_id, name=asset_name)
        if asset is not None:
            return asset.asset_id
    except Exception:
        logger.debug("Asset search failed for %s", asset_name, exc_info=True)
    return None
