# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Source database query executor — QueryExecutor implementation.

Executes authorized SQL against source databases on the direct-JDBC path.
- Single-source queries → direct JDBC (per-engine adapter)
- Cross-source queries → Athena federation (handled by the composite executor)

Engine support is pluggable: every engine-specific decision (driver, sqlglot
dialect, session settings, row-limit idiom) lives in a per-engine adapter under
``clients/db_adapters/``. This module owns only the engine-agnostic
orchestration — credential resolution, connection cache, firewall gating, and
result assembly. It resolves ONE adapter via ``get_adapter(engine_type)`` and
delegates; there are no engine ``if`` branches here. Supported engines today:
PostgreSQL + Redshift (asyncpg), MySQL (aiomysql), SQL Server (python-tds).

Credentials resolved from Secrets Manager via data source config in DynamoDB.

Error handling: Protocol methods raise on failure. Only health_check()
is exception-safe and returns a status dict.

Security:
- Only SELECT statements allowed (DDL/DML rejected via sqlglot AST check).
- Dangerous functions blocked by the SQL firewall.
- Secret ARN validated against expected pattern before fetching.
- Statement timeout validated (1-300s max, default 10s) and enforced per engine.
- Namespace validation enforced at entry point.

Authorization contract:
- Cedar authorization / namespace access checks are the responsibility of the
  orchestration layer BEFORE calling execute() (see step_ids T1_AUTHORIZE /
  T2_SQL_AUTHORIZE). This module enforces SQL-level safety only.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
import structlog
from coa_common import resolve_region, sync_boto_config

from ..tier2.sql_firewall import SQLFirewall
from .base import QueryResult, instrumented
from .db_adapters import DBCredentials, EngineAdapter, get_adapter
from .db_adapters.base import ConnHandle
from .sources_registry import SourcesRegistry

logger = structlog.get_logger(__name__)


# Upper bound for per-query statement_timeout (seconds); prevents runaway queries
MAX_TIMEOUT_SECONDS = 300
# Max cached connection handles (LRU eviction); one per namespace:data_source pair.
# The value is an opaque handle (a SQLAlchemy engine for asyncpg/aiomysql, resolved
# credentials for python-tds), so it is a connection cache, not strictly engines.
MAX_CONN_CACHE_SIZE = 20
# Thread pool size for running synchronous DB/secrets calls off the async event loop
_DB_THREAD_POOL_SIZE = 5

_EXECUTOR = ThreadPoolExecutor(max_workers=_DB_THREAD_POOL_SIZE, thread_name_prefix="source-db")

# Validates Secrets Manager ARN format before fetching credentials
_SECRET_ARN_PATTERN = re.compile(r"^arn:aws:secretsmanager:[a-z0-9-]+:\d{12}:secret:.+")

# Allowed characters for namespace identifiers (alphanumeric, hyphens, underscores)
_NAMESPACE_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# Valid unquoted PostgreSQL/Redshift identifier, used to validate a schema token
# before it is interpolated into the search_path resolution below (defense in
# depth — the identifier is interpolated into a non-parameterizable SET inside the
# PostgreSQL adapter).
_SCHEMA_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

_firewall = SQLFirewall()


def _resolve_search_path(item: dict[str, Any], config: dict[str, Any], engine_type: str) -> str:
    """Resolve the search_path schema(s) for a JDBC source (PostgreSQL/Redshift).

    Engines without a search_path concept receive an empty value and do not
    apply PostgreSQL identifier rules to their metadata.

    Priority:
      1. ``discoveredSchemas`` (authoritative — the schemas the scan found and
         ingested; a plain identifier list). Multiple schemas are joined so
         unqualified names resolve across all of them.
      2. ``schemaFilter`` — the onboarding glob, usable only when it is a single
         plain identifier (a glob is rejected downstream and skipped).
      3. ``public`` — default.

    Every discovered schema must be a valid identifier. Reject corrupted or
    partially valid metadata instead of silently changing the authoritative
    search path.
    """
    if engine_type not in {"POSTGRESQL", "REDSHIFT"}:
        return ""

    discovered = item.get("discoveredSchemas")
    if discovered is not None and not isinstance(discovered, list):
        raise ValueError("discoveredSchemas must be a list of schema identifiers")
    discovered = discovered or []
    if discovered:
        if any(not isinstance(schema, str) or not _SCHEMA_IDENTIFIER_PATTERN.match(schema) for schema in discovered):
            raise ValueError("discoveredSchemas contains an invalid schema identifier")
        return ", ".join(discovered)
    fallback = config.get("schemaFilter") or config.get("schemaName") or "public"
    if not isinstance(fallback, str):
        raise ValueError("configured schema must be a string")
    return fallback


class SourceDBQueryExecutor:
    """Direct-JDBC query executor: engine-agnostic orchestration over adapters.

    Requires a DynamoDB table (data_sources_table) that stores connection configs
    (JDBC endpoint, secret ARN) keyed by namespace and data source ID. Per-engine
    connection/dialect/session behavior is delegated to an ``EngineAdapter``.
    """

    def __init__(
        self,
        data_sources_table: str | None = None,
        region: str | None = None,
        sources_registry: SourcesRegistry | None = None,
    ):
        """Configure the data-sources table, region, and sources registry.

        Args:
            data_sources_table: DynamoDB table with per-namespace connection
                configs. Defaults to the ``DATA_SOURCES_TABLE`` env var.
            region: AWS region. Defaults to the resolved region.
            sources_registry: Pre-built registry; created from the table name when omitted.

        Raises:
            ValueError: If no data-sources table is provided via arg or env var.
        """
        self._table_name = data_sources_table or os.environ.get("DATA_SOURCES_TABLE", "")
        self._region = region or resolve_region()
        if not self._table_name:
            raise ValueError("data_sources_table must be provided via constructor arg or DATA_SOURCES_TABLE env var")
        self._sources = sources_registry or SourcesRegistry(
            table_name=self._table_name, region=self._region, executor=_EXECUTOR
        )
        self._secrets = boto3.client("secretsmanager", region_name=self._region, config=sync_boto_config())
        # Cached connection handle per "namespace:data_source" key. The value is
        # whatever the engine's adapter returns from open_connection (a SQLAlchemy
        # AsyncEngine for asyncpg/aiomysql, a pytds handle for SQL Server); the
        # executor treats it as opaque and only ever hands it back to the same
        # adapter (tracked in _adapter_cache).
        self._conn_cache: OrderedDict[str, ConnHandle] = OrderedDict()
        # Adapter per cache key, in lockstep with _conn_cache — decides how the
        # handle is used and disposed.
        self._adapter_cache: dict[str, EngineAdapter] = {}
        # search_path schema per cache key (in lockstep with _conn_cache).
        self._schema_cache: dict[str, str] = {}
        # sqlglot read-dialect per cache key (in lockstep with _conn_cache). Used to
        # parse the already-transpiled SQL for firewall re-validation and limit
        # injection with the engine's own dialect.
        self._dialect_cache: dict[str, str] = {}
        self._cache_lock = asyncio.Lock()

    async def close(self) -> None:
        """Dispose all cached connections and release resources."""
        async with self._cache_lock:
            for key, conn in self._conn_cache.items():
                adapter = self._adapter_cache.get(key)
                if adapter is not None:
                    # Continue closing every connection even if one close() raises,
                    # so a single failure can't leak the remaining pooled engines.
                    try:
                        await adapter.close(conn)
                    except Exception as exc:
                        logger.warning("source_db_close_failed", cache_key=key, error=str(exc))
            self._conn_cache.clear()
            self._adapter_cache.clear()
            self._schema_cache.clear()
            self._dialect_cache.clear()

    @instrumented("source_db")
    async def execute(
        self,
        sql: str,
        *,
        namespace: str,
        data_source_id: str,
        params: dict[str, Any] | None = None,
        max_rows: int = 1000,
        timeout_seconds: int = 10,
    ) -> QueryResult:
        """Execute read-only SQL against the source's direct-JDBC connection.

        Validates the namespace and SQL, resolves the engine's adapter, injects a
        dialect-aware row cap, and runs the query via that adapter (asyncpg for
        PostgreSQL/Redshift, aiomysql for MySQL, python-tds for SQL Server).

        Args:
            sql: The SQL to execute (write statements are rejected by the firewall).
            namespace: Namespace owning the data source.
            data_source_id: Identifier of the target data source.
            params: Optional bound query parameters.
            max_rows: Maximum rows to return; drives the injected LIMIT.
            timeout_seconds: Statement timeout (1..MAX_TIMEOUT_SECONDS).

        Returns:
            QueryResult with rows, columns, and execution metadata.

        Raises:
            ValueError: If ``timeout_seconds`` is out of range or namespace is invalid.
            UnsafeSQLError: If the SQL fails firewall validation.
        """
        start = time.perf_counter()

        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds must be 1-{MAX_TIMEOUT_SECONDS}, got {timeout_seconds}")

        self._validate_namespace(namespace)

        # Fast, dialect-independent safety gate FIRST: reject non-SELECT / blocked
        # statement types before any DDB/engine work, so a dangerous statement never
        # triggers a source lookup (preserves fail-fast + avoids wasted round-trips).
        _firewall.check_statement_type(sql)

        # Resolve the connection + adapter + dialect atomically so a concurrent
        # close()/eviction can't leave us with a connection but a missing
        # schema/dialect. The SQL arriving here was already transpiled to the
        # engine's dialect by the composite executor, so the deeper parse-based
        # firewall checks and limit injection parse with that same dialect.
        adapter, conn, schema, dialect = await self._get_connection(namespace, data_source_id)

        _firewall.validate(sql, dialect=dialect)

        # Cap at max_rows + 1 so a result of EXACTLY max_rows is distinguishable
        # from a truly truncated one: only > max_rows means rows were dropped. A
        # query returning exactly max_rows rows is no longer mis-flagged truncated.
        limited_sql = adapter.inject_limit(sql, max_rows + 1)

        rows, columns = await adapter.run(conn, limited_sql, params, timeout_seconds, schema)

        truncated = len(rows) > max_rows
        capped_rows = rows[:max_rows]
        duration_ms = int((time.perf_counter() - start) * 1000)

        logger.info(
            "query_executed",
            namespace=namespace,
            data_source_id=data_source_id,
            engine=adapter.engine_type,
            duration_ms=duration_ms,
            row_count=len(capped_rows),
            truncated=truncated,
        )

        return QueryResult(
            rows=capped_rows,
            columns=columns,
            row_count=len(capped_rows),
            truncated=truncated,
            duration_ms=duration_ms,
        )

    async def health_check(self) -> dict[str, Any]:
        """Probe DynamoDB sources-table connectivity; returns a status dict, never raises."""
        if not self._sources.available:
            return {"status": "error", "detail": "Sources table not configured"}
        try:
            await self._sources.get_source("health-check", "ping")
            return {"status": "ok", "table": self._table_name}
        except Exception:
            return {"status": "error", "detail": "DDB connectivity check failed"}

    def _validate_namespace(self, namespace: str) -> None:
        if not _NAMESPACE_PATTERN.match(namespace):
            raise ValueError(f"Invalid namespace format: {namespace!r}")

    async def _get_connection(self, namespace: str, data_source_id: str) -> tuple[EngineAdapter, ConnHandle, str, str]:
        """Return the cached ``(adapter, connection, schema, sqlglot_dialect)``.

        All four are returned together (never via a second cache lookup) so a
        concurrent close()/eviction cannot leave the caller with a connection but
        a missing adapter/schema/dialect. The engine's adapter owns connection
        creation, so this method is engine-agnostic.
        """
        cache_key = f"{namespace}:{data_source_id}"

        if cache_key in self._conn_cache:
            self._conn_cache.move_to_end(cache_key)
            return (
                self._adapter_cache[cache_key],
                self._conn_cache[cache_key],
                self._schema_cache.get(cache_key, ""),
                self._dialect_cache.get(cache_key, "postgres"),
            )

        async with self._cache_lock:
            if cache_key in self._conn_cache:
                self._conn_cache.move_to_end(cache_key)
                return (
                    self._adapter_cache[cache_key],
                    self._conn_cache[cache_key],
                    self._schema_cache.get(cache_key, ""),
                    self._dialect_cache.get(cache_key, "postgres"),
                )

            creds = await self._resolve_credentials(namespace, data_source_id)
            adapter = get_adapter(creds.engine_type)
            conn = await adapter.open_connection(creds)

            self._adapter_cache[cache_key] = adapter
            self._conn_cache[cache_key] = conn
            self._schema_cache[cache_key] = creds.schema
            self._dialect_cache[cache_key] = adapter.sqlglot_dialect

            if len(self._conn_cache) > MAX_CONN_CACHE_SIZE:
                evicted_key, evicted_conn = self._conn_cache.popitem(last=False)
                evicted_adapter = self._adapter_cache.pop(evicted_key, None)
                self._schema_cache.pop(evicted_key, None)
                self._dialect_cache.pop(evicted_key, None)
                if evicted_adapter is not None:
                    # A failure disposing the evicted connection must not propagate to
                    # the caller opening a NEW connection — that would fail an unrelated
                    # query. Log and continue; the handle is already out of the cache.
                    try:
                        await evicted_adapter.close(evicted_conn)
                    except Exception as exc:
                        logger.warning("source_db_evict_close_failed", cache_key=evicted_key, error=str(exc))

        return adapter, conn, creds.schema, adapter.sqlglot_dialect

    async def _resolve_credentials(self, namespace: str, data_source_id: str) -> DBCredentials:
        loop = asyncio.get_running_loop()

        item = await self._sources.get_source(namespace, data_source_id)
        if not item:
            raise ValueError(f"Data source not found: {namespace}/{data_source_id}")

        config = SourcesRegistry.parse_configuration(item)
        engine_type = str(config.get("engine", "POSTGRESQL")).upper()

        # Connection metadata comes from the DDB configuration blob (set at
        # onboarding time). The secret is only for authentication credentials.
        host = config.get("host")
        # Port falls back to the engine's default (3306 MySQL, 1433 SQL Server,
        # 5432 PostgreSQL, 5439 Redshift) when the config omits it.
        port = config.get("port") or get_adapter(engine_type).default_port
        database = config.get("databaseName")
        if not host or not port or not database:
            raise ValueError(
                f"Incomplete connection config for {namespace}/{data_source_id}: "
                f"host={bool(host)}, port={bool(port)}, databaseName={bool(database)}"
            )

        secret_arn = config.get("credentialSecretArn")
        if not secret_arn:
            raise ValueError(f"Missing credentialSecretArn for {namespace}/{data_source_id}")
        if not _SECRET_ARN_PATTERN.match(secret_arn):
            raise ValueError(f"Invalid secret ARN format for {namespace}/{data_source_id}")

        schema = _resolve_search_path(item, config, engine_type)
        secret_response = await loop.run_in_executor(
            _EXECUTOR,
            lambda arn=secret_arn: self._secrets.get_secret_value(SecretId=arn),
        )

        try:
            raw = json.loads(secret_response["SecretString"])
            return DBCredentials(
                username=raw["username"],
                password=raw["password"],
                host=host,
                port=int(port),
                database=database,
                schema=schema,
                engine_type=engine_type,
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            raise ValueError(f"Failed to parse credentials for {namespace}/{data_source_id}") from None
