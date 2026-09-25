# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Athena query executor — cross-source SQL execution via AWS Athena.

Executes SQL via Athena (Trino engine) against data sources registered in
the AWS Glue Data Catalog. Used for cross-source federation when VKG-translated
SQL references tables from multiple catalogs.

Security:
- Only SELECT statements allowed (validated via sqlglot AST).
- SQL comments stripped before validation to prevent bypass.
- Workgroup: coa-{namespace} (isolation per namespace).
- Timeout: 120s max poll.
"""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import boto3
import sqlglot
import structlog
from coa_common import resolve_region, sync_boto_config

from ..query_utils import validate_namespace
from ..tier2.sql_firewall import NamespaceSQLScopeError, SQLFirewall
from ..tier2.table_qualifier import is_fully_catalog_qualified_ast
from .base import QueryResult, instrumented
from .sources_registry import SourcesRegistry

logger = structlog.get_logger(__name__)


def _append_limit(sql: str, max_rows: int) -> str:
    """Textual-append fallback for ``_inject_limit``.

    The LIMIT goes on its OWN line: LLM-generated SQL frequently ends with a
    ``-- comment`` line (e.g. a stated assumption), and a same-line append
    would land inside that comment — silently removing the scan cap while the
    query still executes.
    """
    return f"{sql.rstrip().rstrip(';')}\nLIMIT {int(max_rows)}"


_POOL_SIZE = int(os.environ.get("ATHENA_THREAD_POOL_SIZE", "3"))
_EXECUTOR = ThreadPoolExecutor(max_workers=_POOL_SIZE, thread_name_prefix="athena")

_POLL_INITIAL_DELAY = 0.5
_POLL_MAX_DELAY = 5.0
_POLL_BACKOFF = 2.0
_DEFAULT_TIMEOUT = 120


# Sub-type of a source backed by a customer-authored Athena federation connector.
# Compared as a string rather than importing the control-plane enum: serve does not
# depend on that package, and the value is the DynamoDB attribute's own contract.
_CUSTOM_CONNECTOR_SUB_TYPE = "CUSTOM_CONNECTOR"

# Sub-type of a source reached through a managed Glue federated catalog. Compared
# as a string for the same reason as above.
_JDBC_SUB_TYPE = "JDBC_DATABASE"

# Athena's name for the account's root Glue Data Catalog, and the value it assumes
# when QueryExecutionContext.Catalog is omitted. A source recording this as its
# catalog is saying "the root catalog", which is the absence of a nested one.
_ROOT_CATALOG = "AwsDataCatalog"


@dataclass(frozen=True)
class _CatalogContext:
    """Where a query runs, and whether the crawled-name rewrite applies to it."""

    catalog: str
    """Athena ``QueryExecutionContext.Catalog``; empty for the Glue-native path."""
    database: str
    """Athena ``QueryExecutionContext.Database``."""
    rewrite_crawled_names: bool = True
    """Whether to apply :meth:`AthenaQueryExecutor._rewrite_table_names_for_federation`.

    True for the federated-JDBC path, whose R2RML names come from a Glue crawler
    and carry a ``{schema}_`` prefix the connector does not know. False for a
    custom connector, where the same substring strip would corrupt a legitimate
    table name that happens to start with its database name.
    """


class AthenaQueryError(RuntimeError):
    """Raised when Athena query execution fails."""


class AthenaTimeoutError(TimeoutError):
    """Raised when Athena query exceeds timeout."""


_firewall = SQLFirewall()


class AthenaQueryExecutor:
    """Executes SQL via Athena against Glue-cataloged data sources.

    Used for cross-source queries when VKG translation produces SQL
    referencing tables from multiple data sources.

    Database resolution (in priority order):
    1. Explicit ``database`` param passed to execute()
    2. DDB source record lookup (reads glueDatabaseName from the source's row)
    3. ATHENA_DATABASE env var fallback
    """

    def __init__(
        self,
        region: str | None = None,
        workgroup_prefix: str | None = None,
        workgroup: str | None = None,
        output_s3: str | None = None,
        sources_table: str | None = None,
        sources_registry: SourcesRegistry | None = None,
    ):
        """Configure Athena region, workgroup, output location, and sources registry.

        Args:
            region: AWS region. Defaults to the resolved region.
            workgroup_prefix: Prefix for per-namespace workgroups. Defaults to
                ``ATHENA_WORKGROUP_PREFIX`` then ``"coa-"``.
            workgroup: Fixed workgroup that overrides per-namespace resolution.
                Defaults to the ``ATHENA_WORKGROUP`` env var.
            output_s3: Default S3 output location. Defaults to ``ATHENA_OUTPUT_S3``.
            sources_table: DynamoDB sources table name for database lookups.
            sources_registry: Pre-built registry; created from ``sources_table`` when omitted.
        """
        self._region = region or resolve_region()
        self._fixed_workgroup = workgroup or os.environ.get("ATHENA_WORKGROUP")
        self._workgroup_prefix = workgroup_prefix or os.environ.get("ATHENA_WORKGROUP_PREFIX", "coa-")
        self._default_output_s3 = output_s3 or os.environ.get("ATHENA_OUTPUT_S3", "")
        self._default_database = os.environ.get("ATHENA_DATABASE", "default")
        self._athena = boto3.client("athena", region_name=self._region, config=sync_boto_config())
        self._sources = sources_registry or SourcesRegistry(
            table_name=sources_table, region=self._region, executor=_EXECUTOR
        )

    async def close(self) -> None:
        """Release boto3 client resources."""
        self._athena.close()

    @instrumented("athena")
    async def execute(
        self,
        sql: str,
        *,
        namespace: str,
        data_source_id: str = "",
        database: str = "",
        max_rows: int = 1000,
        timeout_seconds: int = _DEFAULT_TIMEOUT,
    ) -> QueryResult:
        """Execute SQL via Athena.

        Args:
            sql: The SQL to execute.
            namespace: Namespace (used for workgroup: coa-{namespace}).
            data_source_id: Data source identifier (used for DDB database lookup).
            database: Glue database name (explicit override; skips DDB lookup).
            max_rows: Maximum rows to return.
            timeout_seconds: Max time to wait for completion (default 120s).

        Returns:
            QueryResult with rows, columns, and metadata.

        Raises:
            AthenaQueryError: On execution failure.
            AthenaTimeoutError: If query exceeds timeout.
            UnsafeSQLError: If SQL contains non-SELECT statements.
        """
        start = time.perf_counter()

        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError(f"timeout_seconds must be 1-300, got {timeout_seconds}")

        validate_namespace(namespace)
        _firewall.validate(sql)

        # inject a dialect-aware LIMIT into the SQL itself (not just the
        # result-fetch cap) so large scans don't execute fully on the Athena side.
        # Mirrors the JDBC path's sqlglot LIMIT injection (source_db._inject_limit).
        #
        # Parsed once and shared with the qualification check below: both need the
        # AST, and injecting a LIMIT cannot change the qualification answer (a
        # LIMIT clause adds no table references), so the pre-injection AST is the
        # right input for both. Re-parsing the same statement costs ~1.7 ms per KB,
        # synchronously, on every Tier-2 query.
        parsed = self._try_parse(sql)
        sql = self._inject_limit(sql, max_rows, parsed)

        # Applied to every dialect-Trino query, not just federated ones: the alias
        # collision is a property of the SQL Ontop generates, not of the catalog
        # it runs against.
        sql = self._disambiguate_table_aliases(sql)

        # SQL whose every table reference already carries a catalog is
        # self-describing: Athena resolves each name against its own qualifier and
        # ignores the QueryExecutionContext entirely (verified — a qualified query
        # succeeds with a context Database that does not exist, and with no
        # context at all). Resolving a single catalog for it would be meaningless,
        # and _rewrite_table_names_for_federation — which strips ONE source's
        # schema prefix — would corrupt the names it did not expect. So skip both.
        # Unparseable SQL is treated as unqualified, as before.
        if parsed is not None and is_fully_catalog_qualified_ast(parsed):
            logger.info("athena_qualified_sql_neutral_context", namespace=namespace)
            resolved_catalog, resolved_db = "", self._default_database
            # Still fail-closed on out-of-namespace references: fully-qualified SQL
            # is exactly what _authorize_qualified_references guards against, so it
            # MUST run here too. No single default catalog applies, so pass "".
            await self._authorize_qualified_references(sql, namespace, resolved_catalog)
        else:
            context = await self._resolve_catalog_and_database(namespace, data_source_id, database)
            resolved_catalog, resolved_db = context.catalog, context.database
            await self._authorize_qualified_references(sql, namespace, resolved_catalog)
            if resolved_catalog and context.rewrite_crawled_names:
                original_sql = sql
                sql = self._rewrite_table_names_for_federation(sql, resolved_db)
                logger.info(
                    "athena_federation_resolved",
                    catalog=resolved_catalog,
                    database=resolved_db,
                    rewritten=sql != original_sql,
                )
        workgroup = self._fixed_workgroup or await self._resolve_workgroup(namespace)
        query_id = await self._start_query(sql, resolved_db, workgroup, catalog=resolved_catalog)
        try:
            await self._wait_for_completion(query_id, timeout_seconds)
        except AthenaQueryError as exc:
            raise self._explain_federation_failure(exc, resolved_catalog) from exc
        rows, columns, has_more = await self._get_results(query_id, max_rows)

        duration_ms = int((time.perf_counter() - start) * 1000)

        logger.info(
            "athena_query_executed",
            namespace=namespace,
            query_id=query_id,
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
            engine="athena",
        )

    async def health_check(self) -> dict[str, Any]:
        """Probe Athena reachability; returns a status dict, never raises."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                _EXECUTOR,
                lambda: self._athena.list_work_groups(MaxResults=1),
            )
            return {"status": "ok", "backend": "athena"}
        except Exception:
            return {"status": "error", "detail": "Athena health check failed"}

    async def _authorize_qualified_references(self, sql: str, namespace: str, default_catalog: str) -> None:
        """Deny qualified references outside ``namespace`` before Athena sees SQL.

        Bare-table SQL remains on the resolved namespace context and avoids an
        extra source lookup. Any dot-qualified table can override that context, so
        it is fail-closed on an unavailable source inventory.
        """
        if not any("." in ref for ref in _firewall.extract_tables(sql)):
            return

        scope = await self._sources.sql_namespace_scope(namespace)
        if scope is None:
            raise AthenaQueryError("Unable to verify SQL references for the requested namespace")
        try:
            _firewall.validate_namespace_sql_scope(
                sql,
                native_databases=scope.native_databases,
                federated_catalog_schemas=scope.federated_catalog_schemas,
                default_catalog=default_catalog,
            )
        except NamespaceSQLScopeError as exc:
            # Log the rejected reference AND the scope that was authorized, so a
            # distinct-catalog denial is diagnosable from CloudWatch (the client
            # message stays generic). Common cause: a native Glue source in a
            # non-root catalog whose (catalog, database) is not in scope.
            logger.warning(
                "namespace_scope_denied",
                namespace=namespace,
                reason=str(exc),
                default_catalog=default_catalog,
                native_databases=sorted(scope.native_databases),
                federated_catalog_schemas=sorted(f"{c}.{d}" for c, d in scope.federated_catalog_schemas),
            )
            raise AthenaQueryError("Access denied: SQL reference is outside the requested namespace") from exc

    async def _start_query(self, sql: str, database: str, workgroup: str, catalog: str = "") -> str:
        loop = asyncio.get_running_loop()
        context: dict[str, str] = {"Database": database, "Catalog": catalog or _ROOT_CATALOG}
        kwargs: dict[str, Any] = {
            "QueryString": sql,
            "QueryExecutionContext": context,
            "WorkGroup": workgroup,
        }
        if self._default_output_s3:
            kwargs["ResultConfiguration"] = {"OutputLocation": self._default_output_s3}

        response = await loop.run_in_executor(
            _EXECUTOR,
            lambda: self._athena.start_query_execution(**kwargs),
        )
        return response["QueryExecutionId"]

    async def _wait_for_completion(self, query_id: str, timeout_seconds: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = time.perf_counter() + timeout_seconds
        delay = _POLL_INITIAL_DELAY

        while True:
            response = await loop.run_in_executor(
                _EXECUTOR,
                lambda qid=query_id: self._athena.get_query_execution(QueryExecutionId=qid),
            )
            state = response["QueryExecution"]["Status"]["State"]

            if state == "SUCCEEDED":
                return
            if state in ("FAILED", "CANCELLED"):
                reason = response["QueryExecution"]["Status"].get("StateChangeReason", "Unknown error")
                raise AthenaQueryError(f"Athena query {state}: {reason}")

            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise AthenaTimeoutError(f"Athena query {query_id} timed out after {timeout_seconds}s")

            await asyncio.sleep(min(delay, max(0, remaining)))
            delay = min(delay * _POLL_BACKOFF, _POLL_MAX_DELAY)

    _ATHENA_PAGE_MAX = 1000
    """Athena GetQueryResults hard limit on rows per call."""

    async def _get_results(self, query_id: str, max_rows: int) -> tuple[list[dict[str, Any]], list[str], bool]:
        """Fetch up to ``max_rows`` result rows, following NextToken across pages.

        Athena caps GetQueryResults at 1000 rows per call and returns a
        ``NextToken`` for the remainder. The previous implementation issued a
        single call and discarded the token, so every result set was silently
        truncated to 999 rows (1000 minus the header) no matter how large
        ``max_rows`` was — ``has_more`` was computed and then ignored by callers.

        Note the Athena quirk this loop depends on: the header row is present
        ONLY on the first page. Stripping ``rows[1:]`` unconditionally would eat
        one real data row per subsequent page.
        """
        loop = asyncio.get_running_loop()
        rows: list[dict[str, Any]] = []
        columns: list[str] = []
        next_token: str | None = None
        first_page = True
        truncated = False

        while True:
            # Request only what is still needed; +1 on the first page for the header.
            want = max_rows - len(rows) + (1 if first_page else 0)
            page_size = max(1, min(want, self._ATHENA_PAGE_MAX))
            kwargs: dict[str, Any] = {"QueryExecutionId": query_id, "MaxResults": page_size}
            if next_token:
                kwargs["NextToken"] = next_token

            # Bind kwargs as a default arg: a bare closure would late-bind and
            # every iteration would re-request the same page.
            response = await loop.run_in_executor(
                _EXECUTOR,
                lambda kw=kwargs: self._athena.get_query_results(**kw),
            )

            result_set = response["ResultSet"]
            page_rows = result_set.get("Rows", [])
            if first_page:
                columns = [col["Name"] for col in result_set["ResultSetMetadata"]["ColumnInfo"]]
                page_rows = page_rows[1:] if page_rows else []
                first_page = False

            for row in page_rows:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                row_dict = {}
                for i, datum in enumerate(row.get("Data", [])):
                    if i < len(columns):
                        row_dict[columns[i]] = datum.get("VarCharValue")
                rows.append(row_dict)

            next_token = response.get("NextToken")
            if not next_token or len(rows) >= max_rows:
                break

        return rows, columns, bool(next_token) or truncated

    @staticmethod
    def _try_parse(sql: str) -> sqlglot.exp.Expression | None:
        """Parse ``sql`` for the Trino dialect, or None when it will not parse.

        One shared parse for the callers in :meth:`execute` that each need the AST.
        """
        try:
            return sqlglot.parse_one(sql, dialect="trino")
        except Exception:
            return None

    @staticmethod
    def _inject_limit(sql: str, max_rows: int, parsed: sqlglot.exp.Expression | None = None) -> str:
        """Inject/cap the OUTER-query LIMIT for the Trino/Athena dialect.

        the JDBC path injects LIMIT via sqlglot; the Athena path
        previously relied only on the result-fetch cap, so a query without a
        LIMIT still scanned the full table server-side. This caps the scan.

        Operates ONLY on the top-level SELECT's own LIMIT (``parsed.args["limit"]``)
        — NEVER a descendant/subquery LIMIT. Mutating an inner LIMIT would change
        results (e.g. capping ``(SELECT * FROM t LIMIT 2000)`` feeding a COUNT),
        and a small inner LIMIT must not be mistaken for an outer cap.

        - Outer query has no LIMIT → append ``LIMIT {max_rows}``.
        - Outer integer LIMIT > max_rows → lower it to the cap.
        - Outer non-integer LIMIT (param/expr) → replace with the cap.
        - Outer integer LIMIT ≤ max_rows → unchanged.
        Falls back to a textual append if parsing fails or the parsed root is
        not a simple SELECT we can reason about (never raises).

        Args:
            sql: Statement to cap.
            max_rows: Row cap for the outer LIMIT.
            parsed: Pre-parsed AST for ``sql``, when the caller already has one.
                Mutated in place, as the internally-parsed tree would be. Pass
                None (the default) to parse here.
        """
        if parsed is None:
            parsed = AthenaQueryExecutor._try_parse(sql)
        if parsed is None:
            return _append_limit(sql, max_rows)

        # Only the OUTER query's own limit — args.get("limit"), not find() which
        # descends into subqueries. Set-ops (UNION/EXCEPT) and non-Select roots
        # don't carry an outer .args["limit"] we can safely edit → textual append.
        if not isinstance(parsed, sqlglot.exp.Select):
            return _append_limit(sql, max_rows)

        limit_node = parsed.args.get("limit")
        if limit_node is None:
            return _append_limit(sql, max_rows)

        existing = limit_node.expression
        # Replace the outer LIMIT when it is non-integer (param/expr) OR an integer
        # above the cap; leave an integer LIMIT already within the cap unchanged.
        non_integer = not existing or not getattr(existing, "is_int", False)
        if non_integer or int(existing.this) > max_rows:
            limit_node.set("expression", sqlglot.exp.Literal.number(max_rows))
        else:
            return sql  # outer LIMIT already within cap — unchanged

        try:
            return parsed.sql(dialect="trino")
        except Exception:
            return _append_limit(sql, max_rows)

    @staticmethod
    def _explain_federation_failure(exc: AthenaQueryError, catalog: str) -> AthenaQueryError:
        """Append the actionable cause to Athena's opaque federation failures.

        Athena collapses both grants a connector needs into messages that name
        neither the grant nor the principal. A denied ``lambda:InvokeFunction``
        surfaces only as ``Insufficient permissions to execute the query``, and a
        denied spill read as a bare S3 ``403``. Both are customer-side resource
        policies in the connector's own account, so the operator reading this
        message is the only person who can act on it — and without the principal
        ARN they cannot.

        This matters more than a usual error-message tidy-up because these two
        failures are invisible until first query. Registration probes the
        connector with ``SHOW DATABASES`` as the *discovery* role, so a source
        granted discovery but not serve passes registration, approval and
        induction cleanly, then fails here.

        Returns a new error rather than raising so the caller keeps the original
        as ``__cause__``; the untouched Athena text stays in the traceback.
        """
        if not catalog:
            return exc

        message = str(exc)
        hint = ""
        if "Insufficient permissions to execute the query" in message:
            hint = (
                "The connector Lambda most likely does not allow this deployment to invoke it. "
                "Add a lambda:InvokeFunction resource-policy statement on the connector naming "
                "the serve runtime role (see the runtime-role-arn SSM parameter). Also check the "
                "function carries the tag 'coa:connector=true' — the identity policy on this side "
                "is scoped to that tag, so an untagged connector is not invocable."
            )
        elif "Access Denied" in message and "Amazon S3" in message:
            hint = (
                "The connector's spill bucket most likely does not allow this deployment to read it. "
                "The bucket must use SSE-KMS with a customer-managed key tagged "
                "'coa:connector-spill=true', its key policy must grant kms:Decrypt to the serve "
                "runtime role with kms:ViaService, and its bucket policy must grant that role "
                "s3:GetObject. The connector must also spill under "
                "'connectors/<connectorId>/spills/' — the grant is scoped to that key prefix."
            )
        if not hint:
            return exc

        return AthenaQueryError(f"{message} — catalog {catalog!r}. {hint}")

    @staticmethod
    def _disambiguate_table_aliases(sql: str) -> str:
        """Rename table aliases that collide with a projection alias.

        Ontop emits two independent alias families into one query: ``V1``, ``V2``
        for tables and ``v0``, ``v1`` for computed projections. When the numbers
        meet — a table aliased ``V1`` alongside a projection aliased ``v1`` —
        Trino fails the query with::

            TYPE_MISMATCH: Expression V1 is not of type ROW

        because ``ORDER BY`` resolves output aliases *before* FROM-clause aliases,
        so ``V1."total_amount"`` is read as dereferencing the ``v1`` output column
        (a scalar) rather than the table. ``SELECT`` and ``WHERE`` are unaffected —
        output aliases are not in scope there — which is why the same alias works
        in every clause except the one that sorts.

        Quoting cannot fix this. Trino folds identifiers to lower case whether or
        not they are quoted, so ``"V1"`` and ``"v1"`` stay the same name; verified
        against a live LAMBDA catalog, where the quoted form failed identically.
        Renaming the table alias is the repair that holds, and it is invisible
        outside the query because the alias is local to it.

        Not specific to custom connectors: any Athena-backed VKG query that sorts
        on a computed value can hit this. A no-op when nothing collides, so
        queries that work today pass through unchanged.
        """
        try:
            parsed = sqlglot.parse_one(sql, dialect="trino")
        except Exception:
            return sql

        # Output aliases from the whole tree, folded the way Trino folds them.
        # Tree-wide rather than per-scope: a rename is safe either way, and scope
        # analysis would add failure modes for no benefit.
        projection_aliases = {
            alias.lower()
            for select in parsed.find_all(sqlglot.exp.Select)
            for projection in select.expressions
            if (alias := projection.alias_or_name)
        }
        if not projection_aliases:
            return sql

        tables = [t for t in parsed.find_all(sqlglot.exp.Table) if t.alias]
        colliding = [t for t in tables if t.alias.lower() in projection_aliases]
        if not colliding:
            return sql

        taken = {t.alias.lower() for t in tables} | projection_aliases
        renamed: dict[str, str] = {}
        for table in colliding:
            old = table.alias
            candidate = f"{old}_t"
            suffix = 0
            while candidate.lower() in taken:
                suffix += 1
                candidate = f"{old}_t{suffix}"
            taken.add(candidate.lower())
            renamed[old.lower()] = candidate
            table.set("alias", sqlglot.exp.TableAlias(this=sqlglot.exp.to_identifier(candidate)))

        # Requalify every column that pointed at a renamed alias. Matching is
        # case-insensitive because the collision itself is: the qualifier may be
        # spelled ``V1`` while the projection spells it ``v1``.
        for column in parsed.find_all(sqlglot.exp.Column):
            qualifier = column.args.get("table")
            if qualifier is None:
                continue
            new_name = renamed.get(qualifier.name.lower())
            if new_name:
                column.set("table", sqlglot.exp.to_identifier(new_name))

        try:
            return parsed.sql(dialect="trino")
        except Exception:
            return sql

    @staticmethod
    def _rewrite_table_names_for_federation(sql: str, schema: str) -> str:
        """Rewrite VKG-generated table names for federated catalog queries.

        Fallback for when R2RML uses Glue-crawled names (e.g.,
        BIRD_PUBLIC_INCOME) but the federated PostgreSQL connector expects
        the actual PG table name (income). When VKG provides physical names
        via datasourceRouting, this becomes a no-op.

        Only called for federated JDBC sources. Custom-connector sources are
        excluded (see ``_CatalogContext.rewrite_crawled_names``) because for them
        this is not a no-op but a corruption: a real table named ``sales_orders``
        in database ``sales`` would be rewritten to ``orders``, a name its
        connector has never heard of.
        """
        if not schema:
            return sql

        try:
            parsed = sqlglot.parse_one(sql, dialect="trino")
        except Exception:
            return sql

        schema_upper = schema.upper() + "_"
        for table in parsed.find_all(sqlglot.exp.Table):
            name = table.name
            upper_name = name.upper()
            idx = upper_name.find(schema_upper)
            if idx >= 0:
                raw_table = name[idx + len(schema_upper) :]
                table.set("this", sqlglot.exp.to_identifier(raw_table.lower(), quoted=True))

        return parsed.sql(dialect="trino")

    async def _resolve_workgroup(self, namespace: str) -> str:
        """Resolve workgroup: DDB namespace record > prefix-based fallback."""
        workgroup = await self._sources.resolve_workgroup(namespace)
        if workgroup:
            return workgroup
        return f"{self._workgroup_prefix}{namespace}"

    async def _resolve_catalog_and_database(
        self, namespace: str, data_source_id: str, explicit_database: str
    ) -> _CatalogContext:
        """Resolve the Athena catalog and database for query execution.

        The catalog is whichever nested catalog under AwsDataCatalog the source
        lives in — a JDBC source's managed federated catalog, a custom connector's
        LAMBDA catalog, or the non-root catalog a native Glue source's database
        sits in — and the database is that catalog's schema. A Glue source in the
        account's root catalog needs no nested catalog, so it resolves to an empty
        one (which :meth:`_start_query` sends as ``AwsDataCatalog``) plus its Glue
        database name.

        Skips sources where queryable is explicitly False.

        ``table_qualifier.catalog_for_source`` / ``schema_for_source`` mirror this
        branching for the cross-source path, which must name the SAME catalog and
        schema per source; keep the three in step.
        """
        if explicit_database:
            return _CatalogContext(catalog="", database=explicit_database)

        if not self._sources.available:
            return _CatalogContext(catalog="", database=self._default_database)

        if not data_source_id or data_source_id == "default":
            # No explicit routing id. Pinning "any DATABASE source" is sound ONLY
            # when the namespace has exactly one — with two or more, the first-seen
            # source silently answers for same-named tables that belong to a
            # DIFFERENT source, running the query against the wrong physical
            # database. So resolve the SOLE database source when there
            # is one, and otherwise refuse rather than guess.
            #
            # Fully-qualified SQL never reaches this branch — execute() routes it
            # to the neutral-context path above — so a legitimately cross-source
            # statement is unaffected; only a BARE statement with no routing id and
            # an ambiguous (>=2) namespace is refused here.
            db_count = await self._sources.database_source_count(namespace)
            if db_count >= 2:
                logger.warning(
                    "ambiguous_database_source_no_id",
                    namespace=namespace,
                    database_source_count=db_count,
                )
                raise AthenaQueryError(
                    f"ambiguous source: the namespace has {db_count} DATABASE sources and no "
                    "data_source_id was resolved for an unqualified statement. Qualify each table as "
                    '"catalog"."schema"."table" or pass an explicit data_source_id so it does not run '
                    "against the wrong source."
                )
            source = await self._sources.find_database_source(namespace)
        else:
            # Targeted lookup by source ID (from VKG datasourceRouting or caller).
            source = await self._sources.get_source(namespace, data_source_id)

        if not source:
            return _CatalogContext(catalog="", database=self._default_database)

        if source.get("queryable") is False:
            logger.warning("source_not_queryable", namespace=namespace, source_id=data_source_id)
            return _CatalogContext(catalog="", database=self._default_database)

        # Which nested catalog under AwsDataCatalog this source lives in, if any.
        # Three kinds reach here and all three are addressed the same way, via
        # QueryExecutionContext.Catalog:
        #
        #   * a JDBC source's managed federated catalog, and a custom connector's
        #     LAMBDA catalog — both system-provisioned, both recorded in
        #     `athenaDataCatalogName`;
        #   * a native Glue source whose database sits in a non-root catalog (a
        #     customer's own federated catalog, or a cross-account one), which the
        #     caller declares at create and which is recorded in `athenaCatalog`.
        #
        # The Glue case is read from `athenaCatalog` rather than
        # `athenaDataCatalogName` deliberately: `athenaDataCatalogName` is the
        # system-managed attribute that DELETE keys its Lake-Formation-admin
        # teardown off (see sources_handler._handle_delete), so a caller-supplied
        # value must not be stored there. `athenaCatalog` carries the same value
        # with none of that authority.
        federated_catalog = source.get("athenaDataCatalogName") or ""
        # Whether the catalog came from the caller's declaration rather than from a
        # catalog this service provisioned. Load-bearing for the crawled-name
        # rewrite below, which must not reach a native Glue source.
        caller_declared = False
        if not federated_catalog:
            declared_catalog = source.get("athenaCatalog") or ""
            # Every DATABASE source records `athenaCatalog`, most of them as the
            # root catalog — which means "no nested catalog", not "a catalog named
            # AwsDataCatalog", so it must not be sent as one.
            if declared_catalog and declared_catalog != _ROOT_CATALOG:
                federated_catalog = declared_catalog
                caller_declared = True
        if federated_catalog:
            sub_type = source.get("sourceSubType") or ""
            is_custom_connector = sub_type == _CUSTOM_CONNECTOR_SUB_TYPE
            is_jdbc = sub_type == _JDBC_SUB_TYPE
            discovered = source.get("discoveredSchemas") or []
            if discovered:
                schema = discovered[0]
                schema_source = "discoveredSchemas"
            elif not is_jdbc and (
                configured_database := (
                    source.get("athenaDatabase") or self._sources.parse_configuration(source).get("databaseName", "")
                )
            ):
                # A custom-connector source, and a Glue source in a nested catalog,
                # are each scoped to exactly one database and record it at
                # onboarding, so use that rather than a hardcoded default that has
                # nothing to do with either. Reachable when a scan discovered zero
                # tables (an over-narrow table filter, or a connector exposing
                # none).
                #
                # Excluded for federated JDBC, where the configured value is the
                # wrong kind of name: a JDBC source's `databaseName` is its
                # database, while the federated catalog is keyed by SCHEMA, so
                # `postgres` would be sent where `public` belongs.
                schema = configured_database
                schema_source = "configuredDatabase"
            else:
                # `public` is the default schema of the engines the federated-JDBC
                # path serves (PostgreSQL, Redshift). It is not a name the other two
                # kinds would answer to, which is why they resolve above.
                schema = "public"
                schema_source = "default"
            logger.info(
                "catalog_resolution",
                path=("custom_connector" if is_custom_connector else "federated" if is_jdbc else "glue_nested"),
                catalog=federated_catalog,
                schema=schema,
                schema_source=schema_source,
                namespace=namespace,
            )
            return _CatalogContext(
                catalog=federated_catalog,
                database=schema,
                # The crawled-name rewrite exists for R2RML names produced by a
                # Glue crawler on the federated-JDBC path. It substring-strips a
                # `{schema}_` prefix from every table name, which for a custom
                # connector is not a no-op but a corruption: a genuine table named
                # `sales_orders` in database `sales` would be rewritten to
                # `orders`, a table its connector has never heard of.
                #
                # A caller-declared nested catalog is excluded for exactly that
                # reason. It is a NATIVE Glue source: its R2RML is generated from
                # Glue table metadata, so the names already ARE the catalog's own
                # names and there is no crawler-added prefix to strip. Applying the
                # strip would corrupt any table whose name begins with its database
                # name. Excluding it also keeps this flag's value unchanged for
                # every row that reaches here via `athenaDataCatalogName`, which is
                # every row that predates the caller-declared path.
                rewrite_crawled_names=not is_custom_connector and not caller_declared,
            )

        # Glue-native path: use athenaCatalog/athenaDatabase if available
        athena_db = source.get("athenaDatabase") or source.get("glueDatabaseName") or ""
        if athena_db:
            logger.info("catalog_resolution", path="glue_native", database=athena_db, namespace=namespace)
            return _CatalogContext(catalog="", database=athena_db)

        config = self._sources.parse_configuration(source)
        db = config.get("databaseName", "") or self._default_database
        logger.info("catalog_resolution", path="config_fallback", database=db, namespace=namespace)
        return _CatalogContext(catalog="", database=db)
