# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""JDBC connector — schema discovery + connectivity verification for relational databases.

Schema discovery (``discover_metadata``) supports every engine with a dialect
(see ``dialects.py``): PostgreSQL, Redshift, MySQL, SQL Server, Snowflake, and
Oracle. The connector owns the discovery flow; the dialect supplies the driver
connection and the catalog SQL.

Discovery flow:
  1. Resolve the engine's dialect and open one connection (creds from Secrets Manager).
  2. List schemas (dialect catalog query) and apply schema include/exclude filters
     (defaulting to the dialect's system-schema exclude when none is provided).
  3. For each matching schema: list tables/views, apply table filters, then fetch
     all columns + key constraints for the kept tables in batched queries.
  4. Build ``Table`` objects with ``database = schema_name`` so the canonical
     ``table_id`` is ``{schema}.{table}`` (the design's JDBC identifier format).

All SQL parameters are bound via the driver's parameterized API — schema and
table names from configuration are NEVER string-interpolated into the SQL text.
"""

from __future__ import annotations

import json
import logging
import re
import socket
from typing import Any

import boto3
from botocore.exceptions import ClientError
from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    DiscoveredMetadata,
    EnrichmentSource,
    ForeignKey,
    PrimaryKey,
    ReviewStatus,
    Table,
    TechnicalMetadata,
    compute_schema_hash,
)

from .base import ConnectionCheck, ConnectionTestResult
from .dialects import MAX_ENUM_DISTINCT, Dialect, get_dialect
from .filters import compile_filter, split_glob_list
from .sts_assume import assume_datasource_session

logger = logging.getLogger(__name__)

# Engines for which serve may execute single-source queries DIRECTLY (driver,
# low latency) rather than via Athena/federation. This is the serve-side
# execution capability that drives a source's `queryEngine` and is intentionally
# narrower than discovery support (see dialects.DISCOVERY_ENGINES): every engine
# can be discovered + queried via Athena, but only these have a direct path.
# Each engine here has a matching adapter in the serve package
# (coa_serve.clients.db_adapters): PostgreSQL/Redshift over asyncpg,
# MySQL over aiomysql, SQL Server over python-tds.
DIRECT_QUERY_ENGINES = {"POSTGRESQL", "REDSHIFT", "MYSQL", "SQLSERVER"}


class JdbcConnector:
    """Connector for JDBC-compatible relational databases."""

    def test_connection(self, config: dict) -> ConnectionTestResult:
        """Verify connectivity: network, credentials, and database query."""
        checks: list[ConnectionCheck] = []
        host = config.get("host", "")
        if not host:
            checks.append(
                ConnectionCheck(check="network", status="failed", message="Missing or empty 'host' configuration")
            )
            return ConnectionTestResult(success=False, message="Invalid host", checks=checks)
        engine = config.get("engine", "POSTGRESQL").upper()
        dialect = get_dialect(engine)
        # No dialect for this engine token. Fail fast with a clear message —
        # otherwise the connect below dereferences None and reports the far less
        # useful "Database connection failed".
        if dialect is None:
            checks.append(
                ConnectionCheck(check="engine", status="failed", message=f"Unsupported database engine: {engine}")
            )
            return ConnectionTestResult(success=False, message=f"Unsupported database engine: {engine}", checks=checks)
        try:
            port = int(config.get("port", dialect.default_port))
            if not (1 <= port <= 65535):
                raise ValueError("out of range")
        except (ValueError, TypeError):
            checks.append(
                ConnectionCheck(
                    check="network",
                    status="failed",
                    message="Invalid port: must be an integer between 1 and 65535",
                )
            )
            return ConnectionTestResult(success=False, message="Invalid port", checks=checks)
        credential_secret_arn = config.get("credential_secret_arn", "")
        if not credential_secret_arn:
            checks.append(
                ConnectionCheck(check="auth", status="failed", message="Missing 'credential_secret_arn' configuration")
            )
            return ConnectionTestResult(success=False, message="Invalid credentials configuration", checks=checks)
        database_name = config.get("database_name", "")

        # Check 1: TCP connectivity
        try:
            with socket.create_connection((host, port), timeout=10):
                pass
            checks.append(
                ConnectionCheck(check="network", status="ok", message=f"TCP connection to {host}:{port} succeeded")
            )
        except OSError as e:
            checks.append(
                ConnectionCheck(check="network", status="failed", message=f"Network: cannot reach {host}:{port} — {e}")
            )
            return ConnectionTestResult(success=False, message=f"Cannot reach {host}:{port}", checks=checks)

        # Check 2: Fetch credentials from Secrets Manager
        try:
            username, password = self._fetch_credentials(
                config.get("credential_secret_arn", ""),
                config.get("region", "us-east-1"),
                config.get("cross_account_role_arn"),
                config.get("external_id"),
                config.get("namespace_id", ""),
            )
            checks.append(
                ConnectionCheck(check="auth", status="ok", message="Credentials retrieved from Secrets Manager")
            )
        except Exception:
            logger.exception("Credential fetch failed")
            checks.append(
                ConnectionCheck(check="auth", status="failed", message="Auth: failed to retrieve credentials")
            )
            return ConnectionTestResult(success=False, message="Failed to retrieve credentials", checks=checks)

        # Check 3: Database connectivity. The engine is guaranteed to have an
        # enabled dialect — an unsupported one returned above, before any network
        # I/O, rather than reporting success on the strength of network+auth alone
        # (which let a source be created that could never be discovered).
        try:
            conn = dialect.connect(
                host=host, port=port, database=database_name, user=username, password=password, options=config
            )
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM DUAL" if engine == "ORACLE" else "SELECT 1")
                cursor.close()
            finally:
                conn.close()
            checks.append(
                ConnectionCheck(check="database", status="ok", message=f"connectivity check succeeded on {engine}")
            )
        except Exception:
            logger.exception("Database connection failed")
            checks.append(
                ConnectionCheck(
                    check="database", status="failed", message="Database: authentication or connection failed"
                )
            )
            return ConnectionTestResult(success=False, message="Database connection failed", checks=checks)

        # Check 4: Schema access — verify at least one schema matches the filters.
        try:
            schema_check = self._check_schema_access(
                dialect=dialect,
                host=host,
                port=port,
                username=username,
                password=password,
                database_name=database_name,
                options=config,
                schema_filter=config.get("schema_filter"),
                schema_exclude_filter=config.get("schema_exclude_filter"),
            )
            checks.append(schema_check)
            if schema_check.status == "failed":
                return ConnectionTestResult(success=False, message=schema_check.message, checks=checks)
        except Exception:
            logger.exception("Schema access probe failed")
            checks.append(
                ConnectionCheck(
                    check="schema_access",
                    status="warning",
                    message="Could not enumerate schemas; review may proceed but discovery may be empty",
                )
            )

        return ConnectionTestResult(success=True, message="Connection test passed", checks=checks)

    def discover_metadata(self, config: dict) -> DiscoveredMetadata:
        """Connect and discover all schemas/tables/columns for the engine's dialect.

        Raises:
            NotImplementedError: For engines without a registered dialect.
            ValueError: For invalid configuration (bad regex, missing creds).
            RuntimeError: For unrecoverable database errors during discovery.
        """
        engine = (config.get("engine") or "POSTGRESQL").upper()
        dialect = get_dialect(engine)
        if dialect is None:
            raise NotImplementedError(f"Schema discovery for engine '{engine}' is not supported.")

        host, port = self._validate_endpoint(config, dialect.default_port)
        database_name = config.get("database_name", "")
        if not database_name:
            raise ValueError("database_name is required")

        # Compile filter regexes up front so we fail fast on invalid input.
        schema_include = self._compile_filter(config.get("schema_filter"), "schema_filter")
        schema_exclude = self._compile_filter(
            config.get("schema_exclude_filter"), "schema_exclude_filter"
        ) or re.compile(dialect.system_schema_pattern)
        table_include = self._compile_filter(config.get("table_filter"), "table_filter")
        table_exclude = self._compile_filter(config.get("table_exclude_filter"), "table_exclude_filter")

        username, password = self._fetch_credentials(
            config.get("credential_secret_arn", ""),
            config.get("region", "us-east-1"),
            config.get("cross_account_role_arn"),
            config.get("external_id"),
            config.get("namespace_id", ""),
        )

        try:
            conn = dialect.connect(
                host=host, port=port, database=database_name, user=username, password=password, options=config
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to open database connection: {exc}") from exc

        try:
            schemas = self._list_filtered_schemas(conn, dialect, schema_include, schema_exclude)
            data_source_id = config.get("data_source_id", "")
            namespace_id = config.get("namespace_id", "")
            tables: list[Table] = []
            for schema_name in schemas:
                tables.extend(
                    self._discover_schema(
                        conn,
                        dialect,
                        schema_name=schema_name,
                        table_include=table_include,
                        table_exclude=table_exclude,
                        data_source_id=data_source_id,
                        namespace_id=namespace_id,
                    )
                )
        finally:
            try:
                conn.close()
            except Exception:
                logger.warning("jdbc_close_connection_failed", exc_info=True)

        logger.info(
            "JDBC discovery (%s): %d tables, %d columns from database=%s schemas=%d",
            engine,
            len(tables),
            sum(len(t.columns) for t in tables),
            database_name,
            len(schemas),
        )
        return DiscoveredMetadata(tables=tables)

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_endpoint(config: dict, default_port: int) -> tuple[str, int]:
        """Validate host/port from config; raise ValueError on invalid input."""
        host = config.get("host", "")
        if not host:
            raise ValueError("host is required")
        try:
            port = int(config.get("port", default_port))
        except (TypeError, ValueError) as exc:
            raise ValueError("port must be an integer") from exc
        if not (1 <= port <= 65535):
            raise ValueError("port must be in range 1-65535")
        return host, port

    # Filter compilation lives in ``connectors.filters`` so the JDBC and Glue
    # connectors share one implementation (they previously drifted — Glue treated
    # the whole string as a single glob). Thin static wrappers preserve the
    # existing call sites/tests.
    _split_glob_list = staticmethod(split_glob_list)
    _compile_filter = staticmethod(compile_filter)

    @staticmethod
    def _list_filtered_schemas(
        conn: Any,
        dialect: Dialect,
        include: re.Pattern[str] | None,
        exclude: re.Pattern[str] | None,
    ) -> list[str]:
        """List schemas via the dialect and apply include/exclude filters."""
        schemas: list[str] = []
        for schema_name in dialect.list_schemas(conn):
            if include is not None and not include.match(schema_name):
                continue
            if exclude is not None and exclude.match(schema_name):
                continue
            schemas.append(schema_name)
        return schemas

    @staticmethod
    def _discover_schema(
        conn: Any,
        dialect: Dialect,
        *,
        schema_name: str,
        table_include: re.Pattern[str] | None,
        table_exclude: re.Pattern[str] | None,
        data_source_id: str,
        namespace_id: str,
    ) -> list[Table]:
        """Discover tables + columns + constraints within a single schema."""
        kept_table_names: list[str] = []
        for table_name in dialect.list_tables(conn, schema_name):
            if table_include is not None and not table_include.match(table_name):
                continue
            if table_exclude is not None and table_exclude.match(table_name):
                continue
            kept_table_names.append(table_name)

        if not kept_table_names:
            return []

        columns_by_table: dict[str, list[Column]] = {n: [] for n in kept_table_names}
        for table_name, col_name, data_type, nullable in dialect.fetch_columns(conn, schema_name, kept_table_names):
            columns_by_table.setdefault(table_name, []).append(
                Column(name=col_name, data_type=data_type, nullable=nullable, is_partition_key=False)
            )

        pks, fks = JdbcConnector._safe_constraints(dialect, conn, schema_name, kept_table_names)
        table_desc, column_desc = JdbcConnector._safe_descriptions(dialect, conn, schema_name, kept_table_names)
        # Schema-level COMMENT — only PG/Redshift/Snowflake expose this; the
        # default Dialect implementation returns "". Used as the LLM
        # "External evidence" extra context, distinct from the table's own
        # description.
        schema_description = JdbcConnector._safe_database_description(dialect, conn, schema_name)

        tables: list[Table] = []
        for table_name in kept_table_names:
            cols = columns_by_table.get(table_name, [])
            # Apply per-column source descriptions to the single canonical
            # home (``business_metadata.description``) tagged DETERMINISTIC
            # mirror of how PK/FK constraints from information_schema are
            # tagged — both come straight from the source catalog.
            col_desc_map = column_desc.get(table_name, {})
            for col in cols:
                source_comment = col_desc_map.get(col.name, "")
                if source_comment:
                    col.business_metadata = BusinessMetadata(
                        description=source_comment,
                        enrichment_source=EnrichmentSource.DETERMINISTIC,
                        review_status=ReviewStatus.APPROVED,
                        confidence=1.0,
                    )

            # Sample distinct values for low-cardinality string columns (in place).
            # Best-effort: never aborts discovery. Feeds serve NL→SQL enum hints.
            JdbcConnector._safe_distinct_values(dialect, conn, schema_name, table_name, cols)

            # Table's own description goes on business_metadata only —
            # ``database_description`` is reserved for the schema/database-
            # level description (above) used as LLM evidence.
            # NOTE: Table review_status stays PENDING_REVIEW at discovery time
            # regardless of enrichment source. A table must not be APPROVED until
            # all child columns reach a terminal state (enforced by review_logic).
            table_comment = table_desc.get(table_name, "")
            table_business = (
                BusinessMetadata(
                    description=table_comment,
                    enrichment_source=EnrichmentSource.DETERMINISTIC,
                    confidence=1.0,
                )
                if table_comment
                else BusinessMetadata()
            )

            tables.append(
                Table(
                    name=table_name,
                    # `database` is the grouping level (Glue semantics); for JDBC we
                    # group by schema, making the canonical id `schema.table_name`.
                    database=schema_name,
                    data_source_id=data_source_id,
                    namespace_id=namespace_id,
                    database_description=schema_description,
                    technical_metadata=TechnicalMetadata(
                        column_count=len(cols), partition_keys=[], format="", location=""
                    ),
                    business_metadata=table_business,
                    primary_key=pks.get(table_name, PrimaryKey()),
                    foreign_keys=fks.get(table_name, []),
                    columns=cols,
                    technical_metadata_hash=compute_schema_hash(cols),
                )
            )
        return tables

    @staticmethod
    def _safe_descriptions(
        dialect: Dialect, conn: Any, schema_name: str, table_names: list[str]
    ) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
        """Fetch table/column comments via the dialect, swallowing errors.

        Comments are best-effort metadata: if the engine doesn't expose them,
        the user lacks the catalog grants, or the query fails, discovery
        continues without them rather than aborting.
        """
        try:
            return dialect.fetch_descriptions(conn, schema_name, table_names)
        except Exception:
            logger.warning("description_discovery_failed", extra={"schema": schema_name}, exc_info=True)
            return {}, {}

    @staticmethod
    def _safe_database_description(dialect: Dialect, conn: Any, schema_name: str) -> str:
        """Fetch the schema-level COMMENT via the dialect, swallowing errors.

        Best-effort: engines without a schema-level COMMENT (MySQL, SQL Server,
        Oracle) return "" by default; transient failures fall back to "".
        """
        try:
            return dialect.fetch_database_description(conn, schema_name)
        except Exception:
            logger.warning("database_description_discovery_failed", extra={"schema": schema_name}, exc_info=True)
            return ""

    # Column data-types eligible for distinct-value sampling: string/categorical
    # only. Numeric, temporal, and binary types are excluded (their values are
    # not useful enum hints and are more likely PII/high-cardinality).
    _SAMPLEABLE_TYPE_PREFIXES = ("char", "varchar", "text", "enum", "nvarchar", "nchar", "string", "citext")

    @staticmethod
    def _safe_distinct_values(
        dialect: Dialect,
        conn: Any,
        schema_name: str,
        table_name: str,
        cols: list[Column],
        *,
        max_distinct: int = MAX_ENUM_DISTINCT,
    ) -> None:
        """Sample distinct values for low-cardinality STRING columns, in place.

        Assigns ``col.distinct_values`` for eligible columns. Best-effort: any
        sampling failure leaves ``distinct_values`` empty and never aborts
        discovery. Only string/categorical columns are sampled (see
        ``_SAMPLEABLE_TYPE_PREFIXES``); high-cardinality columns get ``[]``.

        Before sampling, applies any session-level timeout statements the
        dialect provides (e.g. ``SET statement_timeout``) so that a single
        pathological column cannot consume the Lambda budget. If those
        statements cannot be applied, sampling is SKIPPED rather than run
        unbounded (GH-131).
        """
        candidates = [
            c.name
            for c in cols
            if any(c.data_type.lower().startswith(p) for p in JdbcConnector._SAMPLEABLE_TYPE_PREFIXES)
        ]
        if not candidates:
            return

        # Apply the probe timeout bound BEFORE issuing any cardinality queries.
        # Fail CLOSED: when a dialect declares timeout statements it is telling us
        # its probe needs bounding, so if we cannot apply that bound we skip
        # sampling entirely rather than issuing the unbounded COUNT(DISTINCT …)
        # this guard exists to prevent (GH-131). Sampling is best-effort
        # enrichment — losing enum detection for one table is strictly cheaper
        # than burning the whole 15-minute discovery Lambda on one wide column.
        for stmt in dialect.probe_timeout_statements():
            try:
                cursor = conn.cursor()
                try:
                    cursor.execute(stmt)
                finally:
                    cursor.close()
            except Exception:
                logger.warning(
                    "probe_timeout_setup_failed",
                    extra={"schema": schema_name, "table": table_name, "statement": stmt},
                    exc_info=True,
                )
                return

        try:
            sampled = dialect.fetch_distinct_values(
                conn, schema_name, table_name, candidates, max_distinct=max_distinct
            )
        except Exception:
            logger.warning(
                "sample_value_discovery_failed",
                extra={"schema": schema_name, "table": table_name},
                exc_info=True,
            )
            return
        for col in cols:
            if col.name in sampled:
                col.distinct_values = sampled[col.name]

    @staticmethod
    def _safe_constraints(
        dialect: Dialect, conn: Any, schema_name: str, table_names: list[str]
    ) -> tuple[dict[str, PrimaryKey], dict[str, list[ForeignKey]]]:
        """Fetch PK/FK via the dialect and map to domain models.

        Constraints are tagged ``DETERMINISTIC`` (confidence 1.0). On any query
        failure (e.g. an engine that doesn't expose constraint metadata) returns
        empty dicts so discovery continues without deterministic constraints.
        """
        try:
            pk_cols, fk_refs = dialect.fetch_constraints(conn, schema_name, table_names)
        except Exception:
            logger.warning("constraint_discovery_failed", extra={"schema": schema_name}, exc_info=True)
            return {}, {}

        pks = {
            t: PrimaryKey(columns=cols, source=EnrichmentSource.DETERMINISTIC, confidence=1.0)
            for t, cols in pk_cols.items()
        }
        fks = {
            t: [
                ForeignKey(
                    column=col,
                    target_table=ref_table,
                    target_column=ref_col,
                    source=EnrichmentSource.DETERMINISTIC,
                    confidence=1.0,
                )
                for (col, ref_table, ref_col) in refs
            ]
            for t, refs in fk_refs.items()
        }
        return pks, fks

    def _check_schema_access(
        self,
        *,
        dialect: Dialect,
        host: str,
        port: int,
        username: str,
        password: str,
        database_name: str,
        options: dict,
        schema_filter: str | None,
        schema_exclude_filter: str | None,
    ) -> ConnectionCheck:
        """Verify at least one schema matches the configured filters."""
        try:
            conn = dialect.connect(
                host=host, port=port, database=database_name, user=username, password=password, options=options
            )
        except Exception as exc:
            return ConnectionCheck(
                check="schema_access", status="failed", message=f"Could not reopen connection for schema probe: {exc}"
            )
        try:
            include = self._compile_filter(schema_filter, "schema_filter")
            exclude = self._compile_filter(schema_exclude_filter, "schema_exclude_filter") or re.compile(
                dialect.system_schema_pattern
            )
            schemas = self._list_filtered_schemas(conn, dialect, include, exclude)
        except ValueError as exc:
            return ConnectionCheck(check="schema_access", status="failed", message=str(exc))
        finally:
            try:
                conn.close()
            except Exception:
                logger.warning("schema_access_close_connection_failed", exc_info=True)

        if not schemas:
            return ConnectionCheck(
                check="schema_access",
                status="failed",
                message="No schemas matched the configured filters; refine schema_filter or schema_exclude_filter",
            )
        return ConnectionCheck(
            check="schema_access", status="ok", message=f"{len(schemas)} schema(s) match the filters"
        )

    def _fetch_credentials(
        self,
        secret_arn: str,
        region: str,
        cross_account_role_arn: str | None,
        external_id: str | None = None,
        namespace_id: str = "",
    ) -> tuple[str, str]:
        """Retrieve username/password from Secrets Manager."""
        session = self._get_session(region, cross_account_role_arn, external_id, namespace_id)
        client = session.client("secretsmanager", region_name=region)
        try:
            resp = client.get_secret_value(SecretId=secret_arn)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                raise ValueError("Secret not found") from e
            if code == "AccessDeniedException":
                raise ValueError("Access denied to secret") from e
            raise
        if "SecretString" not in resp:
            raise ValueError("Secret must be stored as a string, not binary")
        secret = json.loads(resp["SecretString"])
        if "username" not in secret or "password" not in secret:
            raise ValueError("Secret must contain 'username' and 'password' keys")
        return secret["username"], secret["password"]

    def _get_session(
        self,
        region: str,
        cross_account_role_arn: str | None,
        external_id: str | None = None,
        namespace_id: str = "",
    ) -> boto3.Session:
        """Get a boto3 session, optionally assuming a cross-account role.

        A cross-account assume REQUIRES ``external_id``; it is derived from the
        requesting namespace by ``discovery_handler``, never taken from the API
        request. See ``sts_assume.assume_datasource_session``.
        """
        if cross_account_role_arn:
            return assume_datasource_session(
                role_arn=cross_account_role_arn,
                external_id=external_id or "",
                region=region,
                session_name=f"coa-jdbc-{namespace_id}",
            )
        return boto3.Session(region_name=region)
