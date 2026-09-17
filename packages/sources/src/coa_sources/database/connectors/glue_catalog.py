# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Glue Data Catalog connector — discovers metadata from existing Glue databases.

Reads tables and columns directly from the Glue Data Catalog via GetTables
paginator. No Glue Crawler provisioning needed.

Supports:
  - Table filtering via Glue Expression syntax
  - Table exclusion via client-side regex
  - Partition key extraction
  - Cross-account access via STS AssumeRole
"""

from __future__ import annotations

import logging
import os

import boto3
from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    DiscoveredMetadata,
    EnrichmentSource,
    ReviewStatus,
    Table,
    TechnicalMetadata,
    compute_schema_hash,
)

from . import lf_grant
from .base import ConnectionCheck, ConnectionTestResult, MetadataConnector
from .filters import compile_filter
from .sts_assume import assume_datasource_session

logger = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


class GlueCatalogConnector(MetadataConnector):
    """Read metadata directly from an existing Glue Data Catalog database."""

    def test_connection(self, config: dict) -> ConnectionTestResult:
        """Verify the Glue database exists and is readable."""
        checks: list[ConnectionCheck] = []
        database_name = config.get("database_name", "")
        catalog_id = config.get("catalog_id", "")
        region = config.get("region", AWS_REGION)
        cross_account_role = config.get("cross_account_role_arn")
        external_id = config.get("external_id")
        # Set by the discovery handler once it has verified that the requesting
        # namespace owns this database. Absent means "not verified" — the Lake
        # Formation self-grant then stays off, because it can unlock SELECT on any
        # database in the account for the shared serve role.
        lf_self_grant_allowed = bool(config.get("lf_self_grant_allowed"))

        if not database_name:
            return ConnectionTestResult(
                success=False,
                message="database_name is required",
                checks=[
                    ConnectionCheck(
                        check="config",
                        status="failed",
                        message="database_name not provided",
                    )
                ],
            )

        glue = self._get_glue_client(region, cross_account_role, external_id, config.get("namespace_id", ""))
        get_db_kwargs: dict = {"Name": database_name}
        if catalog_id:
            get_db_kwargs["CatalogId"] = catalog_id

        # For same-account catalogs, retry once after a Lake Formation self-grant
        # if the first read is denied (strict-LF mode). Cross-account catalogs
        # cannot be self-granted; we surface an actionable hint instead.
        attempted_self_grant = bool(cross_account_role)
        while True:
            try:
                resp = glue.get_database(**get_db_kwargs)
                db_info = resp.get("Database", {})
                desc = db_info.get("Description", "no description")
                checks.append(
                    ConnectionCheck(
                        check="catalog_access",
                        status="ok",
                        message=f"Database '{database_name}' accessible ({desc})",
                    )
                )
                break
            except glue.exceptions.EntityNotFoundException:
                checks.append(
                    ConnectionCheck(
                        check="catalog_access",
                        status="failed",
                        message=f"Database '{database_name}' not found",
                    )
                )
                return ConnectionTestResult(
                    success=False,
                    message=f"Glue database '{database_name}' does not exist",
                    checks=checks,
                )
            except Exception as e:
                # Strict-LF denial on a same-account catalog: try a one-shot
                # self-grant via the LF-admin grantor role, then retry.
                if not attempted_self_grant and lf_grant.is_access_denied(e):
                    attempted_self_grant = True
                    if lf_grant.attempt_self_grant(
                        database_name, catalog_id, region, owner_verified=lf_self_grant_allowed
                    ):
                        continue
                    # Self-heal unavailable (no grantor / cross-account) — actionable hint.
                    msg = lf_grant.lf_onboarding_hint(database_name, catalog_id)
                    checks.append(ConnectionCheck(check="catalog_access", status="failed", message=msg))
                    return ConnectionTestResult(success=False, message=msg, checks=checks)
                checks.append(
                    ConnectionCheck(
                        check="catalog_access",
                        status="failed",
                        message=f"Cannot access Glue catalog: {str(e)[:300]}",
                    )
                )
                return ConnectionTestResult(
                    success=False,
                    message=f"Glue catalog access failed: {str(e)[:300]}",
                    checks=checks,
                )

        try:
            tables_kwargs: dict = {"DatabaseName": database_name, "MaxResults": 5}
            if catalog_id:
                tables_kwargs["CatalogId"] = catalog_id
            tables_resp = glue.get_tables(**tables_kwargs)
            table_count = len(tables_resp.get("TableList", []))
            if table_count > 0:
                checks.append(
                    ConnectionCheck(
                        check="tables",
                        status="ok",
                        message=f"Found {table_count}+ tables in catalog",
                    )
                )
            else:
                checks.append(
                    ConnectionCheck(
                        check="tables",
                        status="warning",
                        message="Database exists but contains no tables",
                    )
                )
        except Exception as e:
            checks.append(
                ConnectionCheck(
                    check="tables",
                    status="warning",
                    message=f"Could not list tables: {str(e)[:200]}",
                )
            )

        return ConnectionTestResult(
            success=True,
            message=f"Glue catalog '{database_name}' is accessible",
            checks=checks,
        )

    def discover_metadata(self, config: dict) -> DiscoveredMetadata:
        """Read all tables and columns from a Glue Data Catalog database."""
        database_name = config["database_name"]
        catalog_id = config.get("catalog_id", "")
        data_source_id = config.get("data_source_id", "")
        namespace_id = config.get("namespace_id", "")
        region = config.get("region", AWS_REGION)
        cross_account_role = config.get("cross_account_role_arn")
        external_id = config.get("external_id")
        table_filter = config.get("table_filter")
        table_exclude_filter = config.get("table_exclude_filter")

        glue = self._get_glue_client(region, cross_account_role, external_id, config.get("namespace_id", ""))
        # Reuse the shared filter compiler (same as JDBC) so a pipe/comma list
        # like ``staging_*|temp_*`` excludes ANY matching glob. A bare
        # ``fnmatch.translate`` treated the whole string as one glob and silently
        # matched nothing, so multi-pattern excludes were ignored.
        exclude_re = compile_filter(table_exclude_filter, "table_exclude_filter")

        # Pull the database-level Description once. Every Table from this
        # discovery shares the same database, so this is the value of
        # ``Table.database_description`` for all of them. Best-effort: if the
        # GetDatabase call fails (e.g., transient permissions), we continue
        # discovery with an empty database description.
        database_description = ""
        try:
            get_db_kwargs: dict = {"Name": database_name}
            if catalog_id:
                get_db_kwargs["CatalogId"] = catalog_id
            db_resp = glue.get_database(**get_db_kwargs)
            database_description = db_resp.get("Database", {}).get("Description", "") or ""
        except Exception:
            logger.warning("glue_get_database_failed", extra={"database": database_name}, exc_info=True)

        tables: list[Table] = []

        paginator = glue.get_paginator("get_tables")
        paginate_kwargs: dict = {"DatabaseName": database_name}
        if catalog_id:
            paginate_kwargs["CatalogId"] = catalog_id
        if table_filter:
            paginate_kwargs["Expression"] = table_filter

        for page in paginator.paginate(**paginate_kwargs):
            for glue_table in page.get("TableList", []):
                table_name = glue_table["Name"]

                if exclude_re and exclude_re.match(table_name):
                    continue

                table = self._build_table(
                    glue_table,
                    database_name,
                    data_source_id,
                    namespace_id,
                    database_description,
                )
                tables.append(table)

        # Sample distinct enum values for low-cardinality categorical columns via
        # Athena (Glue tables have no live JDBC connection). Best-effort: any
        # Athena/LF/permission failure leaves columns unsampled and never aborts
        # discovery. Uses the Athena database name (Glue database) + catalog.
        self._sample_enum_values(tables, database_name, catalog_id, region)

        logger.info(
            "Glue Catalog discovery: %d tables, %d columns from %s",
            len(tables),
            sum(len(t.columns) for t in tables),
            database_name,
        )
        return DiscoveredMetadata(tables=tables)

    @staticmethod
    def _sample_enum_values(tables: list[Table], database_name: str, catalog_id: str, region: str) -> None:
        """Assign ``col.distinct_values`` for enum-like string columns, in place.

        Mirrors the JDBC path (``JdbcConnector._safe_distinct_values``) but issues
        the sampling queries through Athena, fanned out in parallel across every
        table's columns via a bounded thread pool (see ``AthenaSampler.
        sample_all``) so the query-poll waits overlap and the scan fits the
        Lambda timeout at scale. Fully non-fatal — a disabled sampler (no
        workgroup/output location) or any per-column error simply leaves
        ``distinct_values`` empty.
        """
        from .athena_sampler import AthenaSampler

        sampler = AthenaSampler(region=region)
        if not sampler.enabled:
            logger.info("glue_enum_sampling_disabled", extra={"database": database_name})
            return

        table_cols = [
            (table.name, [(c.name, c.data_type) for c in table.columns if not c.is_partition_key]) for table in tables
        ]
        try:
            sampled_by_table = sampler.sample_all(database_name, table_cols)
        except Exception:
            # A whole-batch failure (pool/setup error) must not abort discovery;
            # per-column errors are already swallowed inside the fan-out.
            logger.warning("glue_enum_sampling_failed", extra={"database": database_name}, exc_info=True)
            return

        for table in tables:
            sampled = sampled_by_table.get(table.name, {})
            for col in table.columns:
                if col.name in sampled:
                    col.distinct_values = sampled[col.name]

    def _build_table(
        self,
        glue_table: dict,
        database_name: str,
        data_source_id: str,
        namespace_id: str,
        database_description: str,
    ) -> Table:
        """Convert a Glue table response into a typed Table model."""
        table_name = glue_table["Name"]
        storage = glue_table.get("StorageDescriptor", {})
        params = glue_table.get("Parameters", {})

        # Build columns. Each column may carry a Glue Comment, which we treat
        # as an authoritative source description: surfaced on
        # ``business_metadata.description`` (the single canonical home) tagged
        # ``DETERMINISTIC`` so downstream AI enrichment doesn't overwrite it.
        columns: list[Column] = []
        for col in storage.get("Columns", []):
            comment = col.get("Comment", "") or ""
            columns.append(
                Column(
                    name=col["Name"],
                    data_type=col.get("Type", "unknown"),
                    nullable=True,  # Glue doesn't expose nullability
                    is_partition_key=False,
                    business_metadata=(
                        BusinessMetadata(
                            description=comment,
                            enrichment_source=EnrichmentSource.DETERMINISTIC,
                            review_status=ReviewStatus.APPROVED,
                            confidence=1.0,
                        )
                        if comment
                        else BusinessMetadata()
                    ),
                )
            )
        partition_key_names: list[str] = []
        for col in glue_table.get("PartitionKeys", []):
            partition_key_names.append(col["Name"])
            comment = col.get("Comment", "") or ""
            columns.append(
                Column(
                    name=col["Name"],
                    data_type=col.get("Type", "unknown"),
                    nullable=False,
                    is_partition_key=True,
                    business_metadata=(
                        BusinessMetadata(
                            description=comment,
                            enrichment_source=EnrichmentSource.DETERMINISTIC,
                            review_status=ReviewStatus.APPROVED,
                            confidence=1.0,
                        )
                        if comment
                        else BusinessMetadata()
                    ),
                )
            )

        # Technical metadata
        fmt = params.get("classification", "") or glue_table.get("TableType", "")
        location = storage.get("Location", "")
        tech = TechnicalMetadata(
            column_count=len(columns),
            partition_keys=partition_key_names,
            format=fmt,
            location=location,
        )

        # Pull the Glue table-level Description into business_metadata so it
        # is the single authoritative description for the table, preserved
        # across AI enrichment via the DETERMINISTIC tag.
        # NOTE: Table review_status stays PENDING_REVIEW at discovery time
        # regardless of enrichment source. A table must not be APPROVED until
        # all child columns reach a terminal state (enforced by review_logic).
        table_description = glue_table.get("Description", "") or ""
        table_business = (
            BusinessMetadata(
                description=table_description,
                enrichment_source=EnrichmentSource.DETERMINISTIC,
                confidence=1.0,
            )
            if table_description
            else BusinessMetadata()
        )

        # Compute hash for re-scan change detection
        tech_hash = compute_schema_hash(columns)

        return Table(
            name=table_name,
            database=database_name,
            data_source_id=data_source_id,
            namespace_id=namespace_id,
            # Database-level description (from glue.get_database()), shared by
            # every table in this discovery and used by the enricher as
            # "External evidence" extra context — distinct from the table's
            # own description above.
            database_description=database_description,
            technical_metadata=tech,
            business_metadata=table_business,
            columns=columns,
            technical_metadata_hash=tech_hash,
        )

    def _get_glue_client(
        self,
        region: str,
        cross_account_role: str | None = None,
        external_id: str | None = None,
        namespace_id: str = "",
    ):
        """Get a Glue client, optionally assuming a cross-account role.

        A cross-account assume REQUIRES ``external_id``; it is derived from the
        requesting namespace by ``discovery_handler``, never taken from the API
        request. See ``sts_assume.assume_datasource_session``.
        """
        if cross_account_role:
            session = assume_datasource_session(
                role_arn=cross_account_role,
                external_id=external_id or "",
                region=region,
                session_name=f"coa-glue-{namespace_id}",
            )
            return session.client("glue", region_name=region)
        return boto3.client("glue", region_name=region)
