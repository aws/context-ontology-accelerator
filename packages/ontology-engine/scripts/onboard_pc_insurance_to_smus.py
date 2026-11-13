#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Register P&C Insurance tables as approved assets in SMUS (DataZone).

Creates DataZone assets from the pc_insurance.json fixture so the
benchmark can run end-to-end against SMUS without the mock sidecar.

Usage:
    export AWS_PROFILE=<profile>
    export AWS_REGION=us-east-1
    export SMUS_DOMAIN_ID=<domain-id>
    export SMUS_PROJECT_ID=<project-id>
    export DATASOURCES_TABLE=<datasource-connectors-table>

    uv run python scripts/onboard_pc_insurance_to_smus.py

Prereqs:
    - SMUS domain and project must already exist
    - DynamoDB datasources table must exist
    - Caller must have DataZone CreateAsset + CreateAssetRevision permissions
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make the workspace importable
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent / "libs" / "common" / "src"))

from coa_common.datazone_forms import (  # noqa: E402
    ASSET_TYPE_NAME,
    build_forms_input,
)
from coa_common.domain_models import (  # noqa: E402
    BusinessMetadata,
    Column,
    ForeignKey,
    PrimaryKey,
    Table,
    TechnicalMetadata,
)
from coa_common.metadata_store.smus import SMUSClient  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "data-catalog" / "sample_data" / "pc_insurance.json"

DOMAIN_ID = os.environ.get("SMUS_DOMAIN_ID", "")
PROJECT_ID = os.environ.get("SMUS_PROJECT_ID", "")
DATASOURCES_TABLE = os.environ.get("DATASOURCES_TABLE", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
NAMESPACE_ID = os.environ.get("SMUS_NAMESPACE_ID", "")


def _fixture_table_to_domain_model(
    tbl: dict,
    *,
    database: str,
    data_source_id: str,
    namespace_id: str,
) -> Table:
    """Convert pc_insurance.json table entry to a domain Table model."""
    bm = tbl.get("businessMetadata", {})
    tech = tbl.get("technicalMetadata", {})
    pk_data = tbl.get("primaryKey") or {}
    fk_data = tbl.get("foreignKeys", [])

    columns = []
    for col in tbl.get("columns", []):
        col_bm = col.get("businessMetadata", {})
        # The pc_insurance.json fixture stores the source description on a
        # top-level ``description`` field on each column. Map it into
        # business_metadata.description (the single canonical home), preferring
        # an explicit businessMetadata.description if the fixture provides one.
        col_description = col_bm.get("description", col.get("description", ""))
        columns.append(
            Column(
                name=col["name"],
                data_type=col.get("dataType", col.get("type", "")),
                nullable=col.get("nullable", True),
                business_metadata=BusinessMetadata(
                    description=col_description,
                    review_status="APPROVED",
                    enrichment_source=col_bm.get("enrichmentSource", "DETERMINISTIC"),
                ),
            )
        )

    return Table(
        name=tbl["name"],
        database=database,
        data_source_id=data_source_id,
        namespace_id=namespace_id,
        database_description="",
        technical_metadata=TechnicalMetadata(
            column_count=tech.get("columnCount", len(columns)),
            partition_keys=tech.get("partitionKeys", []),
            format=tech.get("format", ""),
            location=tech.get("location", ""),
        ),
        business_metadata=BusinessMetadata(
            description=bm.get("description", ""),
            synonyms=bm.get("synonyms", []),
            glossary_terms=bm.get("glossaryTerms", []),
            tags=bm.get("tags", []),
            enrichment_source=bm.get("enrichmentSource", "DETERMINISTIC"),
            review_status="APPROVED",
        ),
        primary_key=PrimaryKey(
            columns=pk_data.get("columns", []),
            source=pk_data.get("source", "DETERMINISTIC"),
            confidence=pk_data.get("confidence", 1.0),
        ),
        foreign_keys=[
            ForeignKey(
                column=fk["column"],
                target_table=fk["targetTable"],
                target_column=fk["targetColumn"],
                source=fk.get("source", "DETERMINISTIC"),
                confidence=fk.get("confidence", 1.0),
            )
            for fk in fk_data
        ],
        columns=columns,
    )


def _register_datasource_in_dynamo(data_source_id: str) -> None:
    """Ensure a datasource record exists in DynamoDB."""
    import boto3

    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(DATASOURCES_TABLE)
    pk = f"DS#{data_source_id}" if not data_source_id.startswith("DS#") else data_source_id
    table.put_item(
        Item={
            "PK": pk,
            "SK": "METADATA",
            "sourceType": "JDBC_DATABASE",
            "datasourceId": data_source_id,
            "status": "CONNECTED",
        },
        ConditionExpression="attribute_not_exists(PK)",
    )


def main() -> int:
    if not DOMAIN_ID or not PROJECT_ID or not DATASOURCES_TABLE:
        print("ERROR: Set SMUS_DOMAIN_ID, SMUS_PROJECT_ID, DATASOURCES_TABLE env vars")
        return 1

    if not FIXTURE_PATH.exists():
        print(f"ERROR: Fixture not found: {FIXTURE_PATH}")
        return 1

    fixture = json.loads(FIXTURE_PATH.read_text())
    sources = fixture.get("sources", [])
    if not sources:
        print("ERROR: No sources in fixture")
        return 1

    source = sources[0]
    data_source_id = source["datasourceId"]
    namespace_id = NAMESPACE_ID or fixture.get("namespaceId", "ns-pc-insurance")

    print(f"SMUS Domain:      {DOMAIN_ID}")
    print(f"SMUS Project:     {PROJECT_ID}")
    print(f"DataSource ID:    {data_source_id}")
    print(f"Namespace ID:     {namespace_id}")
    print(f"DDB Table:        {DATASOURCES_TABLE}")
    print(f"Region:           {REGION}")
    print()

    # Step 1: Register datasource in DynamoDB
    print("Step 1: Registering datasource in DynamoDB...")
    try:
        _register_datasource_in_dynamo(data_source_id)
        print(f"  Created DS#{data_source_id} in {DATASOURCES_TABLE}")
    except Exception as e:
        error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if error_code == "ConditionalCheckFailedException":
            print(f"  Already exists: DS#{data_source_id}")
        else:
            print(f"  WARNING: {e}")

    # Step 2: Create assets in SMUS
    client = SMUSClient(domain_id=DOMAIN_ID, region_name=REGION)

    databases = source.get("databases", [])
    total_tables = sum(len(db.get("tables", [])) for db in databases)
    print(f"\nStep 2: Creating {total_tables} assets in SMUS...")

    created = 0
    skipped = 0
    for db in databases:
        db_name = db["name"]
        for tbl in db.get("tables", []):
            table_model = _fixture_table_to_domain_model(
                tbl,
                database=db_name,
                data_source_id=data_source_id,
                namespace_id=namespace_id,
            )
            asset_name = f"DS#{data_source_id}:{db_name}.{tbl['name']}"
            forms = build_forms_input(table_model)

            try:
                result = client.create_asset(
                    project_id=PROJECT_ID,
                    name=asset_name,
                    type_identifier=ASSET_TYPE_NAME,
                    description=tbl.get("businessMetadata", {}).get("description", ""),
                    forms_input=forms,
                )
                print(f"  + {asset_name} → {result.asset_id}")
                created += 1
            except Exception as e:
                if "ConflictException" in str(type(e).__name__) or "already exists" in str(e).lower():
                    print(f"  = {asset_name} (already exists)")
                    skipped += 1
                else:
                    print(f"  ! {asset_name} FAILED: {e}")

    print(f"\nDone: {created} created, {skipped} skipped")
    print(f"\n{'═' * 60}")
    print("To run induction against this data:")
    print(f"{'═' * 60}")
    print("  export CATALOG_SOURCE=smus")
    print(f"  export SMUS_DOMAIN_ID={DOMAIN_ID}")
    print(f"  export SMUS_PROJECT_ID={PROJECT_ID}")
    print(f"  export SMUS_NAMESPACE_ID={namespace_id}")
    print(f"  export DATASOURCES_TABLE={DATASOURCES_TABLE}")
    print(f"  # In DATASOURCES list: ('{data_source_id}', 'P&C Insurance')")
    print("  python3 scripts/live_induction_run.py --clean")

    return 0


if __name__ == "__main__":
    sys.exit(main())
