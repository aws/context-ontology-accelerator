# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared constants and helpers for DataZone CoaTableMetadata form type.

The Smithy model defines a flat structure — all complex values (arrays, nested
objects) are JSON-serialized into String fields. This module provides the single
source of truth for form/asset type names and the serialize/deserialize logic
so that writers and readers stay in sync.
"""

from __future__ import annotations

import json

# ── Constants ────────────────────────────────────────────────────────────
from typing import Any

from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    ForeignKey,
    PrimaryKey,
    Table,
    TechnicalMetadata,
    to_dict,
)

from .constants import DZ_TYPE_PREFIX as _DZ_TYPE_PREFIX

# ── Constants ────────────────────────────────────────────────────────────
FORM_TYPE_NAME = f"{_DZ_TYPE_PREFIX}TableMetadata"
ASSET_TYPE_NAME = f"{_DZ_TYPE_PREFIX}RelationalTable"


# ── Serialization (Table → flat form payload) ────────────────────────────


def serialize_form(table: Table) -> dict[str, Any]:
    """Convert a Table into a flat dict matching the CoaTableMetadata Smithy structure.

    Keys align 1:1 with the Smithy model members. Complex values are
    JSON-serialized into strings.
    """
    tech = table.technical_metadata
    biz = table.business_metadata
    pk = table.primary_key

    return {
        "tableName": table.name,
        "databaseName": table.database,
        "dataSourceId": table.data_source_id,
        "namespaceId": table.namespace_id,
        "databaseDescription": table.database_description,
        "description": biz.description or "",
        "columnCount": tech.column_count or len(table.columns),
        "partitionKeys": json.dumps(tech.partition_keys),
        "format": tech.format or "",
        "location": tech.location or "",
        "synonyms": json.dumps(biz.synonyms),
        "glossaryTerms": json.dumps(biz.glossary_terms),
        "tags": json.dumps(biz.tags),
        "enrichmentSource": biz.enrichment_source or "",
        "reviewStatus": biz.review_status or "",
        "primaryKeyColumns": json.dumps(pk.columns) if pk else "[]",
        "primaryKeySource": pk.source if pk else "",
        "primaryKeyConfidence": pk.confidence if pk else 0.0,
        "foreignKeys": json.dumps([to_dict(fk) for fk in table.foreign_keys]),
        "columns": json.dumps([to_dict(col) for col in table.columns]),
    }


def build_forms_input(table: Table) -> list[dict[str, str]]:
    """Build the formsOutput list for DataZone create_asset / create_asset_revision."""
    return [
        {
            "formName": FORM_TYPE_NAME,
            "content": json.dumps(serialize_form(table)),
            "typeIdentifier": FORM_TYPE_NAME,
        }
    ]


# ── Deserialization (flat form payload → Table) ──────────────────────────


def deserialize_form(payload: dict[str, Any], *, data_source_id: str = "") -> Table:
    """Parse a flat CoaTableMetadata form payload back into a Table object."""
    bm_fields = set(BusinessMetadata.__dataclass_fields__)

    # Parse JSON-encoded fields
    partition_keys = _parse_json_list(payload.get("partitionKeys", "[]"))
    synonyms = _parse_json_list(payload.get("synonyms", "[]"))
    glossary_terms = _parse_json_list(payload.get("glossaryTerms", "[]"))
    tags = _parse_json_list(payload.get("tags", "[]"))
    pk_columns = _parse_json_list(payload.get("primaryKeyColumns", "[]"))
    foreign_keys_raw = _parse_json_list(payload.get("foreignKeys", "[]"))
    columns_raw = _parse_json_list(payload.get("columns", "[]"))

    columns = [
        Column(
            name=c["name"],
            data_type=c.get("data_type", ""),
            nullable=c.get("nullable", True),
            is_partition_key=c.get("is_partition_key", False),
            business_metadata=BusinessMetadata(
                **{k: v for k, v in c.get("business_metadata", {}).items() if k in bm_fields}
            ),
            distinct_values=c.get("distinct_values", []),
        )
        for c in columns_raw
        if isinstance(c, dict) and "name" in c
    ]

    return Table(
        name=payload.get("tableName", ""),
        database=payload.get("databaseName", ""),
        data_source_id=data_source_id or payload.get("dataSourceId", ""),
        namespace_id=payload.get("namespaceId", ""),
        database_description=payload.get("databaseDescription", ""),
        technical_metadata=TechnicalMetadata(
            column_count=payload.get("columnCount", 0),
            partition_keys=partition_keys,
            format=payload.get("format", ""),
            location=payload.get("location", ""),
        ),
        business_metadata=BusinessMetadata(
            description=payload.get("description", ""),
            synonyms=synonyms,
            glossary_terms=glossary_terms,
            tags=tags,
            enrichment_source=payload.get("enrichmentSource", ""),
            review_status=payload.get("reviewStatus", ""),
        ),
        primary_key=PrimaryKey(
            columns=pk_columns,
            source=payload.get("primaryKeySource", ""),
            confidence=float(payload.get("primaryKeyConfidence", 0.0)),
        ),
        foreign_keys=[
            ForeignKey(
                column=fk.get("column", ""),
                target_table=fk.get("target_table", ""),
                target_column=fk.get("target_column", ""),
                source=fk.get("source", ""),
                confidence=float(fk.get("confidence", 0.0)),
                # Defaults keep FKs stored before these fields existed valid:
                # a missing reviewStatus deserialises to "" (grandfathered).
                review_status=fk.get("review_status", ""),
                target_datasource_id=fk.get("target_datasource_id", ""),
                provenance=fk.get("provenance", ""),
            )
            for fk in foreign_keys_raw
            if isinstance(fk, dict)
        ],
        columns=columns,
    )


def _parse_json_list(value: Any) -> list:
    """Safely parse a JSON-encoded list string, returning [] on failure."""
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
