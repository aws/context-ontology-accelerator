# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Redshift Data API query executor — Glue/Iceberg execution via Redshift Serverless.

Executes SQL against Glue Data Catalog / Iceberg tables through Amazon Redshift
Serverless using the ``awsdatacatalog`` auto-mount, as an alternative execution
engine to Athena for Glue-backed sources.

This is the serve-time counterpart to ``AthenaQueryExecutor``: it implements the
same ``QueryExecutor`` protocol (SELECT-only, returns a ``QueryResult``) but runs
the query on a Redshift Serverless workgroup via the Redshift Data API
(``redshift-data`` — HTTPS/IAM, no persistent connection or VPC egress to :5439).

A Glue source opts into this path by setting ``executionEngine=REDSHIFT`` +
``redshiftWorkgroup`` at onboarding (control plane persists ``queryEngine=REDSHIFT``
and the ``redshiftWorkgroup`` column); the ``CompositeQueryExecutor`` routes such
sources here instead of Athena.

SQL handling: VKG emits Trino-dialect SQL. This executor transpiles Trino→Redshift
and rewrites table references to the 3-part ``awsdatacatalog."<db>"."<table>"``
form that Redshift's Glue auto-mount requires.

Security:
- Only SELECT statements allowed (shared ``SQLFirewall.validate``).
- A dialect-aware LIMIT is injected so large scans don't fully materialize.
- Errors are classified: a poll timeout raises ``RedshiftTimeoutError`` (network/
  provisioning), a FAILED/ABORTED statement raises ``RedshiftQueryError`` (SQL/IAM).
"""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
import sqlglot
import sqlglot.errors
import structlog
from coa_common import resolve_region

from ..query_utils import validate_namespace
from ..tier2.sql_firewall import AWS_DATA_CATALOG, NamespaceSQLScopeError, SQLFirewall
from ..tier2.table_qualifier import QualificationError, qualifier_tables, real_tables
from .base import QueryResult, instrumented
from .sources_registry import SourcesRegistry, SQLNamespaceScope

logger = structlog.get_logger(__name__)

_POOL_SIZE = int(os.environ.get("REDSHIFT_DATA_THREAD_POOL_SIZE", "3"))
_EXECUTOR = ThreadPoolExecutor(max_workers=_POOL_SIZE, thread_name_prefix="redshift-data")

_POLL_INITIAL_DELAY = 0.5
_POLL_MAX_DELAY = 5.0
_POLL_BACKOFF = 2.0
_DEFAULT_TIMEOUT = 120

_TERMINAL_OK = "FINISHED"
_TERMINAL_FAIL = frozenset({"FAILED", "ABORTED"})

_firewall = SQLFirewall()


class RedshiftQueryError(RuntimeError):
    """Raised when a Redshift Data API statement FAILED or ABORTED (SQL/IAM error)."""


class RedshiftTimeoutError(TimeoutError):
    """Raised when a Redshift Data API statement exceeds the poll timeout (network/provisioning)."""


class RedshiftDataAPIExecutor:
    """Executes SELECT SQL against Glue/Iceberg tables via Redshift Serverless.

    Implements the ``QueryExecutor`` protocol. The target Redshift Serverless
    workgroup and Glue database are resolved per-source from the source registry
    (``redshiftWorkgroup`` column + Glue ``databaseName``), mirroring how
    ``AthenaQueryExecutor`` resolves ``athenaCatalog``/``athenaDatabase``.
    """

    def __init__(
        self,
        *,
        sources_registry: SourcesRegistry,
        region: str | None = None,
        database: str | None = None,
    ) -> None:
        """Wire the Redshift Data API client, sources registry, and target database.

        Args:
            sources_registry: Registry used to resolve the Redshift-execution source
                and its workgroup.
            region: AWS region for the ``redshift-data`` client; defaults to the
                resolved ambient region.
            database: Target database name; defaults to the ``REDSHIFT_SERVE_DATABASE``
                env var, falling back to ``"dev"``.
        """
        self._region = region or resolve_region()
        self._sources = sources_registry
        self._database = database or os.environ.get("REDSHIFT_SERVE_DATABASE", "dev")
        self._client = boto3.client("redshift-data", region_name=self._region)

    @instrumented("redshift_data")
    async def execute(
        self,
        sql: str,
        *,
        namespace: str,
        data_source_id: str,
        params: dict[str, Any] | None = None,
        max_rows: int = 1000,
        timeout_seconds: int = _DEFAULT_TIMEOUT,
    ) -> QueryResult:
        """Execute SELECT SQL against Glue/Iceberg tables via Redshift Serverless.

        Args:
            sql: Trino-dialect SELECT SQL (VKG output). Transpiled to Redshift and
                rewritten to ``awsdatacatalog`` 3-part table names before execution.
            namespace: Serving namespace (validated).
            data_source_id: Glue source id — used to resolve the workgroup + Glue db.
            params: Ignored (Redshift Data API SQL is fully materialized, like Athena).
            max_rows: Result cap; also injected as a SQL LIMIT.
            timeout_seconds: Max poll time (1-300).

        Raises:
            RedshiftQueryError: On a FAILED/ABORTED statement (SQL or IAM problem).
            RedshiftTimeoutError: If the statement exceeds ``timeout_seconds``.
            ValueError: On invalid timeout or when no workgroup can be resolved.
            UnsafeSQLError: If SQL contains non-SELECT statements.
        """
        start = time.perf_counter()

        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError(f"timeout_seconds must be an int in 1-300, got {timeout_seconds!r}")

        validate_namespace(namespace)
        _firewall.validate(sql)

        workgroup, glue_database = await self._resolve_workgroup_and_database(namespace, data_source_id)
        if not workgroup:
            # Fail loud rather than silently mis-execute: the composite only routes
            # here for queryEngine=REDSHIFT sources, which the control plane
            # guarantees carry a workgroup.
            raise ValueError(f"No Redshift workgroup configured for source {namespace}/{data_source_id}")

        scope = await self._authorize_qualified_references(sql, namespace)
        sql = self._prepare_sql(sql, glue_database, max_rows)
        await self._authorize_rewritten_sql(sql, namespace, glue_database, scope)

        statement_id = await self._start_statement(sql, workgroup)
        await self._wait_for_completion(statement_id, timeout_seconds)
        rows, columns, has_more = await self._get_results(statement_id, max_rows)

        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "redshift_data_query_executed",
            namespace=namespace,
            workgroup=workgroup,
            statement_id=statement_id,
            duration_ms=duration_ms,
            row_count=len(rows),
            truncated=has_more,
        )
        return QueryResult(
            rows=rows,
            columns=columns,
            row_count=len(rows),
            truncated=has_more,
            duration_ms=duration_ms,
            engine="redshift",
        )

    async def health_check(self) -> dict[str, Any]:
        """Probe the Redshift Data API; return an ``ok``/``error`` status dict."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(_EXECUTOR, lambda: self._client.list_statements(MaxResults=1))
            return {"status": "ok", "backend": "redshift-data"}
        except Exception:
            return {"status": "error", "detail": "Redshift Data API health check failed"}

    async def close(self) -> None:
        """No-op close for executor symmetry; the boto3 client is stateless."""
        return None

    async def _authorize_qualified_references(self, sql: str, namespace: str) -> SQLNamespaceScope | None:
        """Deny cross-namespace Glue references before Redshift rewrites them.

        The Redshift rewriter preserves an explicit database from generated SQL,
        so it must not run until the database is proven to belong to this
        namespace. Bare tables use the source-resolved Glue database and need no
        additional inventory lookup; a table function is refused by the scope check.

        Returns:
            The namespace scope that was fetched, so the rewritten statement can
            be re-checked without a second lookup; ``None`` for bare-table SQL.
        """
        if not _firewall.requires_namespace_scope_check(sql):
            return None

        scope = await self._sources.sql_namespace_scope(namespace)
        if scope is None:
            raise RedshiftQueryError("Unable to verify SQL references for the requested namespace")
        try:
            _firewall.validate_namespace_sql_scope(
                sql,
                native_databases=scope.native_databases,
                federated_catalog_schemas=scope.federated_catalog_schemas,
                default_catalog=AWS_DATA_CATALOG,
            )
        except NamespaceSQLScopeError as exc:
            # Log the rejected reference + authorized scope so a distinct-catalog
            # denial is diagnosable from CloudWatch (client message stays generic).
            logger.warning(
                "namespace_scope_denied",
                namespace=namespace,
                reason=str(exc),
                default_catalog=AWS_DATA_CATALOG,
                native_databases=sorted(scope.native_databases),
                federated_catalog_schemas=sorted(f"{c}.{d}" for c, d in scope.federated_catalog_schemas),
            )
            raise RedshiftQueryError("Access denied: SQL reference is outside the requested namespace") from exc
        return scope

    async def _authorize_rewritten_sql(
        self,
        sql: str,
        namespace: str,
        glue_database: str,
        scope: SQLNamespaceScope | None,
    ) -> None:
        """Re-check the final Redshift statement against the namespace's root Glue databases.

        The statement that runs is a Redshift-dialect rewrite of the authorized
        Trino SQL, so it is re-checked: the statement that runs is the statement
        that was authorized. Every table that is not a CTE reference (by the
        firewall's ``resolves_to_cte`` rule) must be ``awsdatacatalog``-qualified
        with a database, because a bare name here resolves through Redshift's
        ``search_path`` to local tables or system views. Only native databases
        apply; federated pairs are never valid on Redshift.

        Raises:
            RedshiftQueryError: If the statement cannot be parsed, keeps an
                unqualified table, or names a database outside the namespace.
        """
        try:
            parsed = sqlglot.parse_one(sql, dialect="redshift")
        except sqlglot.errors.SqlglotError as exc:
            # A parse failure is not a scope decision: report it as such, and skip
            # the scope lookup, which cannot make an unparseable statement pass.
            logger.warning("redshift_rewritten_sql_parse_failed", sql_len=len(sql), error_type=type(exc).__name__)
            raise RedshiftQueryError("Unable to verify SQL for Redshift") from exc
        for table in real_tables(parsed):
            if (table.catalog or "").lower() != AWS_DATA_CATALOG or not table.db:
                logger.warning(
                    "redshift_unqualified_reference",
                    namespace=namespace,
                    route="redshift_rewritten",
                    has_catalog=bool(table.catalog),
                    has_database=bool(table.db),
                )
                raise RedshiftQueryError("Access denied: SQL reference is outside the requested namespace")

        own_database = frozenset({glue_database.lower()}) if glue_database else frozenset()
        if scope is None:
            # Bare tables already run against the source-resolved database, so a
            # statement that reads only it needs no inventory lookup.
            try:
                _firewall.validate_namespace_sql_scope(
                    sql,
                    native_databases=own_database,
                    federated_catalog_schemas=frozenset(),
                    default_catalog=AWS_DATA_CATALOG,
                    dialect="redshift",
                )
                return
            except NamespaceSQLScopeError:
                pass
            scope = await self._sources.sql_namespace_scope(namespace)
            if scope is None:
                logger.warning(
                    "namespace_scope_unavailable",
                    namespace=namespace,
                    route="redshift_rewritten",
                    glue_database=glue_database,
                )
                raise RedshiftQueryError("Unable to verify SQL references for the requested namespace")
        try:
            _firewall.validate_namespace_sql_scope(
                sql,
                native_databases=scope.native_databases | own_database,
                federated_catalog_schemas=frozenset(),
                default_catalog=AWS_DATA_CATALOG,
                dialect="redshift",
            )
        except NamespaceSQLScopeError as exc:
            logger.warning(
                "namespace_scope_denied",
                namespace=namespace,
                route="redshift_rewritten",
                reason=str(exc),
                native_databases=sorted(scope.native_databases),
            )
            raise RedshiftQueryError("Access denied: SQL reference is outside the requested namespace") from exc

    def _prepare_sql(self, sql: str, glue_database: str, max_rows: int) -> str:
        """Transpile Trino→Redshift, rewrite to awsdatacatalog 3-part names, cap LIMIT."""
        sql = self._inject_limit(sql, max_rows)
        sql = self._transpile_to_redshift(sql)
        sql = self._qualify_awsdatacatalog(sql, glue_database)
        return sql

    @staticmethod
    def _transpile_to_redshift(sql: str) -> str:
        """Transpile VKG's Trino-dialect SQL to Redshift. Falls back to the input on error."""
        try:
            return sqlglot.transpile(sql, read="trino", write="redshift", identify=False)[0]
        except sqlglot.errors.SqlglotError as exc:
            # Catch transpilation failures only — a genuine bug (AttributeError,
            # etc.) must surface, not be masked by the fall-back-to-input path.
            # SQL may carry caller-supplied literal values (potential PII), and sqlglot
            # error messages echo a snippet of it. Log length + error type only.
            logger.warning("redshift_trino_transpile_failed", sql_len=len(sql), error_type=type(exc).__name__)
            return sql

    @staticmethod
    def _qualify_awsdatacatalog(sql: str, glue_database: str) -> str:
        """Rewrite table references to ``awsdatacatalog."<db>"."<table>"``.

        VKG-generated table names may be bare (``table``), 2-part (``db.table``),
        or already catalog-qualified. Redshift's Glue auto-mount requires the
        external catalog name ``awsdatacatalog`` as the leading part. This maps:

        - ``table``            → ``awsdatacatalog.<glue_database>.table``
        - ``db.table``         → ``awsdatacatalog.db.table``
        - ``awsdatacatalog.db.table`` (any case, e.g. Athena's ``AwsDataCatalog``)
                               → ``awsdatacatalog.db.table``

        Any other catalog (a federated connector catalog such as
        ``fedcat.sales.orders``) raises ``RedshiftQueryError``. Spectrum cannot
        address it, and rewriting it to ``awsdatacatalog`` would run a reference
        authorized as a federated ``(catalog, schema)`` pair against a root Glue
        database of the same name, which may belong to another namespace. The
        composite executor routes such SQL to Athena, so this is a fail-closed
        backstop.

        The database is lower-cased, matching how ``validate_namespace_sql_scope``
        authorized it (Glue database names are lower-case).

        CTE references (``WITH a AS (...) SELECT * FROM a``) are NOT Glue tables,
        so they are left untouched. They are resolved by the firewall's rule
        (``resolves_to_cte``): a name matching a CTE defined in some *other* scope
        is a real table and is qualified, and a name that differs from a CTE only
        in case or quoting is refused, because Redshift binds it to the CTE or to
        ``search_path`` depending on the workgroup's case-sensitivity setting.

        Raises:
            RedshiftQueryError: If the SQL cannot be parsed or rendered, a table
                names a catalog other than ``awsdatacatalog``, a bare table has no
                database to resolve against, or a reference differs from a CTE only
                in case or quoting. The rewrite never returns SQL it did not fully
                qualify.
        """
        try:
            parsed = sqlglot.parse_one(sql, dialect="redshift")
        except sqlglot.errors.SqlglotError as exc:
            logger.warning(
                "redshift_awsdatacatalog_rewrite_parse_failed", sql_len=len(sql), error_type=type(exc).__name__
            )
            raise RedshiftQueryError("Unable to rewrite SQL for Redshift") from exc
        try:
            tables = qualifier_tables(parsed)
        except QualificationError as exc:
            raise RedshiftQueryError(str(exc)) from exc

        for table in tables:
            table_name = table.name
            if table.catalog and table.catalog.lower() != AWS_DATA_CATALOG:
                # Never retarget a federated catalog at the root Glue catalog.
                logger.warning("redshift_non_awsdatacatalog_reference", catalog=table.catalog.lower())
                raise RedshiftQueryError("Redshift can only query awsdatacatalog tables; route this query to Athena")
            db_name = (table.db or glue_database or "").lower()
            if not db_name:
                # A bare table with no default database would resolve through
                # Redshift's search_path, not the namespace's Glue database.
                raise RedshiftQueryError("Unable to resolve a Glue database for a table reference")
            table.set("catalog", sqlglot.exp.to_identifier(AWS_DATA_CATALOG, quoted=True))
            table.set("db", sqlglot.exp.to_identifier(db_name, quoted=True))
            table.set("this", sqlglot.exp.to_identifier(table_name, quoted=True))

        try:
            return parsed.sql(dialect="redshift")
        except sqlglot.errors.SqlglotError as exc:
            # sqlglot error messages can echo SQL fragments (potential PII); log type only.
            logger.warning("redshift_awsdatacatalog_rewrite_render_failed", error_type=type(exc).__name__)
            raise RedshiftQueryError("Unable to rewrite SQL for Redshift") from exc

    @staticmethod
    def _inject_limit(sql: str, max_rows: int) -> str:
        """Inject/cap the OUTER-query LIMIT (Trino dialect, pre-transpile).

        Mirrors ``AthenaQueryExecutor._inject_limit`` semantics: only the top-level
        SELECT's own LIMIT is touched (never a subquery LIMIT). Falls back to a
        textual append if parsing fails or the root is not a simple SELECT.
        """
        try:
            parsed = sqlglot.parse_one(sql, dialect="trino")
        except Exception:
            return f"{sql.rstrip(';')} LIMIT {int(max_rows)}"

        if not isinstance(parsed, sqlglot.exp.Select):
            return f"{sql.rstrip(';')} LIMIT {int(max_rows)}"

        limit_node = parsed.args.get("limit")
        if limit_node is None:
            return f"{sql.rstrip(';')} LIMIT {int(max_rows)}"

        existing = limit_node.expression
        non_integer = not existing or not getattr(existing, "is_int", False)
        if non_integer or int(existing.this) > max_rows:
            limit_node.set("expression", sqlglot.exp.Literal.number(max_rows))
        else:
            return sql

        try:
            return parsed.sql(dialect="trino")
        except Exception:
            return f"{sql.rstrip(';')} LIMIT {int(max_rows)}"

    async def _resolve_workgroup_and_database(self, namespace: str, data_source_id: str) -> tuple[str, str]:
        """Resolve the Redshift workgroup + Glue database for a Glue source.

        Reads the ``redshiftWorkgroup`` column (persisted at onboarding) and the
        Glue database (``athenaDatabase`` / config ``databaseName``). Returns
        ("", "") when the source is missing/not-queryable so the caller fails loud.
        """
        if not self._sources.available:
            return "", ""

        if not data_source_id or data_source_id == "default":
            # Strict: only resolve when the namespace has exactly ONE DATABASE
            # source (mirrors the composite gate's find_sole_database_source) —
            # never guess a source in a multi-source namespace. In the wired flow
            # the composite always supplies an explicit id, so this is a safety net.
            source = await self._sources.find_sole_database_source(namespace)
        else:
            source = await self._sources.get_source(namespace, data_source_id)

        if not source or source.get("queryable") is False:
            return "", ""

        workgroup = source.get("redshiftWorkgroup") or ""
        glue_database = source.get("athenaDatabase") or source.get("glueDatabaseName") or ""
        if not glue_database:
            config = self._sources.parse_configuration(source)
            glue_database = config.get("databaseName", "")
        return workgroup, glue_database

    async def _start_statement(self, sql: str, workgroup: str) -> str:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            _EXECUTOR,
            lambda: self._client.execute_statement(
                WorkgroupName=workgroup,
                Database=self._database,
                Sql=sql,
            ),
        )
        return response["Id"]

    async def _wait_for_completion(self, statement_id: str, timeout_seconds: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = time.perf_counter() + timeout_seconds
        delay = _POLL_INITIAL_DELAY

        while True:
            response = await loop.run_in_executor(
                _EXECUTOR,
                lambda sid=statement_id: self._client.describe_statement(Id=sid),
            )
            status = response["Status"]
            if status == _TERMINAL_OK:
                return
            if status in _TERMINAL_FAIL:
                reason = response.get("Error", "Unknown error")
                raise RedshiftQueryError(f"Redshift statement {status}: {reason}")

            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise RedshiftTimeoutError(f"Redshift statement {statement_id} timed out after {timeout_seconds}s")
            await asyncio.sleep(min(delay, max(0, remaining)))
            delay = min(delay * _POLL_BACKOFF, _POLL_MAX_DELAY)

    async def _get_results(self, statement_id: str, max_rows: int) -> tuple[list[dict[str, Any]], list[str], bool]:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            _EXECUTOR,
            lambda: self._client.get_statement_result(Id=statement_id),
        )
        column_meta = response.get("ColumnMetadata", [])
        columns = [c.get("label") or c.get("name") or "" for c in column_meta]

        raw_records = response.get("Records", [])
        # has_more: one extra row beyond the cap, OR a NextToken (more pages exist).
        has_more = len(raw_records) > max_rows or bool(response.get("NextToken"))
        raw_records = raw_records[:max_rows]

        rows = [self._record_to_dict(columns, record) for record in raw_records]
        return rows, columns, has_more

    @staticmethod
    def _record_to_dict(columns: list[str], record: list[dict[str, Any]]) -> dict[str, Any]:
        """Convert a Redshift Data API record (list of typed-value cells) to a dict.

        Each cell is a single-key dict like ``{"stringValue": "x"}``,
        ``{"longValue": 3}``, or ``{"isNull": True}``. We take the sole value,
        mapping an explicit null cell to ``None``.
        """
        row: dict[str, Any] = {}
        for col, cell in zip(columns, record, strict=False):
            if cell.get("isNull"):
                row[col] = None
                continue
            row[col] = next(iter(cell.values()), None)
        return row
