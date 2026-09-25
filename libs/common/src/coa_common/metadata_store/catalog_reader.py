# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Catalog reader — fetches approved metadata for ontology induction.

Aggregates enriched, steward-approved metadata from one or more data sources
into the nested JSON structure expected by the ontology induction pipeline.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from coa_common.dao import DynamoDBDAO
from coa_common.domain_models import ReviewStatus, Table
from coa_common.metadata_store.reader import read_assets_for_datasource

logger = logging.getLogger(__name__)


def read_approved_catalog(
    *,
    domain_id: str,
    project_id: str,
    namespace_id: str,
    data_source_ids: list[str],
    datasources_table: str,
    region: str | None = None,
) -> dict[str, Any]:
    """Fetch approved metadata for multiple data sources, shaped for induction.

    Looks up each datasource in the unified sources table keyed by
    ``PK=NS#{namespace_id}, SK=SRC#{data_source_id}``.

    Returns a dict matching the ontology induction input schema:
    {
      "namespaceId": "...",
      "sources": [
        {
          "datasourceId": "...",
          "sourceType": "GLUE_DATABASE" | "JDBC_DATABASE",
          "databases": [{...}]
        }
      ]
    }
    """
    resolved_region: str = region if region else os.environ.get("AWS_REGION", "us-east-1")
    ds_dao = DynamoDBDAO(datasources_table, region=resolved_region)

    sources: list[dict[str, Any]] = []

    for ds_id in data_source_ids:
        # Get source type from DynamoDB.
        # New sources table key schema: PK=NS#{namespaceId}, SK=SRC#{sourceId}
        bare_id = ds_id.removeprefix("DS#").removeprefix("SRC#")
        ds_item = ds_dao.get({"PK": f"NS#{namespace_id}", "SK": f"SRC#{bare_id}"})
        if not ds_item:
            logger.warning("Data source %s not found in DynamoDB (namespace=%s), skipping", ds_id, namespace_id)
            continue

        source_type = ds_item.get("sourceType", "UNKNOWN")

        # Only induct from fully-approved sources
        source_status = ds_item.get("status", "")
        if source_status != "APPROVED":
            logger.info("Source %s is in %s status (not APPROVED), skipping", ds_id, source_status)
            continue

        # Fetch all tables from SMUS
        tables = read_assets_for_datasource(domain_id, project_id, ds_id)

        # Filter to approved tables only
        approved_tables = [t for t in tables if t.business_metadata.review_status == ReviewStatus.APPROVED]

        if not approved_tables:
            logger.info("No approved tables for data source %s, skipping", ds_id)
            continue

        # Group tables by database
        databases: dict[str, list[Table]] = {}
        for table in approved_tables:
            db_name = table.database or "default"
            databases.setdefault(db_name, []).append(table)

        # Build database entries
        db_entries: list[dict[str, Any]] = []
        for db_name, db_tables in databases.items():
            db_desc = db_tables[0].database_description if db_tables else ""
            db_entries.append(
                {
                    "name": db_name,
                    "description": db_desc,
                    "tables": [_table_to_induction_format(t) for t in db_tables],
                }
            )

        sources.append(
            {
                "datasourceId": ds_id,
                "sourceType": source_type,
                "databases": db_entries,
            }
        )

    return {"namespaceId": namespace_id, "sources": sources}


def _table_to_induction_format(table: Table) -> dict[str, Any]:
    """Convert a Table to the induction-expected JSON shape."""
    tech = table.technical_metadata
    biz = table.business_metadata

    # Filter columns to approved only
    approved_cols = [c for c in table.columns if c.business_metadata.review_status == ReviewStatus.APPROVED]

    return {
        "name": table.name,
        "technicalMetadata": {
            "columnCount": tech.column_count,
            "partitionKeys": tech.partition_keys,
            "format": tech.format,
            "location": tech.location,
        },
        "businessMetadata": {
            "description": biz.description,
            "synonyms": biz.synonyms,
            "glossaryTerms": biz.glossary_terms,
            "tags": biz.tags,
            "enrichmentSource": biz.enrichment_source,
            "reviewStatus": biz.review_status,
        },
        "primaryKey": {
            "columns": table.primary_key.columns,
            "source": table.primary_key.source,
            "confidence": table.primary_key.confidence,
        }
        if table.primary_key.columns
        else None,
        "foreignKeys": [
            {
                "column": fk.column,
                "targetTable": fk.target_table,
                "targetColumn": fk.target_column,
                "source": fk.source,
                "confidence": fk.confidence,
                # Governance (#1088): carried through so the inducer can gate on
                # approval and resolve a cross-source target's datasource.
                "reviewStatus": fk.review_status,
                "targetDatasourceId": fk.target_datasource_id,
                "provenance": fk.provenance,
            }
            for fk in table.foreign_keys
        ],
        "columns": [
            {
                "name": col.name,
                "type": col.data_type,
                "nullable": col.nullable,
                # Sampled distinct values for low-cardinality categorical
                # columns — carried through to induction so the emitted
                # ontology can annotate the property with coa:distinctValues.
                "distinctValues": col.distinct_values or [],
                "businessMetadata": {
                    "description": col.business_metadata.description,
                    "synonyms": col.business_metadata.synonyms,
                    "glossaryTerms": col.business_metadata.glossary_terms,
                    "tags": col.business_metadata.tags,
                    "enrichmentSource": col.business_metadata.enrichment_source,
                    "reviewStatus": col.business_metadata.review_status,
                },
            }
            for col in approved_cols
        ],
    }
