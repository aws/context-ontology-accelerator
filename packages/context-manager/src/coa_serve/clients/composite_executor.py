# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Composite Query Executor — single-vs-cross-source dispatch.

Implements the ``QueryExecutor`` protocol by routing each query to the
cheapest correct engine, per Serve LLD §2.2.1 Option D:

- **single-source, JDBC-backed** → ``SourceDBQueryExecutor`` (direct asyncpg,
  ~20-50ms p50)
- **cross-source / Glue-only / ambiguous** → ``AthenaQueryExecutor`` (federation,
  ~500-800ms p50)

The routing decision reuses the SQL Firewall's AST table extraction
(``SQLFirewall.extract_tables``) rather than re-parsing — exactly the
``unique_catalogs(...)`` heuristic the architecture note describes.

Graceful degradation (MVP safety): the direct-JDBC executor is OPTIONAL. When
it is not configured (no ``DATA_SOURCES_TABLE``, or it failed to build), the
composite delegates EVERY query to Athena — i.e. it behaves exactly like the
deployed Athena-only path. Wiring the composite therefore never regresses the
current behaviour; it only ADDS the direct-JDBC fast path when that executor is
available. Authorization is unchanged — both underlying executors run the SQL
Firewall ``validate()`` themselves, and data-level ``evaluate()`` already ran in
the caller (Tier-1 orchestrator / Tier-2 VKGTranslator) before dispatch.
"""

from __future__ import annotations

from typing import Any

import sqlglot
import sqlglot.errors
import structlog

from ..tier2.sql_firewall import NamespaceSQLScopeError, SQLFirewall
from .base import QueryExecutor, QueryResult
from .db_adapters import get_adapter, is_direct_query_engine

logger = structlog.get_logger(__name__)


class CompositeQueryExecutor:
    """Routes SQL to a direct-JDBC or Athena executor by source cardinality.

    Args:
        athena_executor: Cross-source / federation executor. Always required —
            it is the fallback for every case the JDBC path cannot serve.
        source_db_executor: Optional single-source direct-JDBC executor. When
            ``None``, every query goes to Athena (deployed Athena-only behaviour).
        firewall: SQL Firewall used only for its AST ``extract_tables`` helper
            (table/catalog extraction for the routing decision). Defaults to a
            fresh instance; no authorization state is held here.
    """

    def __init__(
        self,
        athena_executor: QueryExecutor,
        source_db_executor: QueryExecutor | None = None,
        firewall: SQLFirewall | None = None,
        sources_registry=None,
        redshift_executor: QueryExecutor | None = None,
    ):
        """Wire the Athena and optional JDBC executors and the routing helpers.

        Args:
            athena_executor: Cross-source / federation executor; always the fallback.
            source_db_executor: Optional single-source direct-JDBC executor.
            firewall: SQLFirewall used only for its ``extract_tables`` AST helper;
                defaults to a fresh instance.
            sources_registry: Optional registry used solely for the
                ``has_jdbc_endpoint`` routing gate.
            redshift_executor: Optional Redshift Serverless executor for Glue/Iceberg
                sources that opt into ``queryEngine=REDSHIFT``. When
                ``None``, such sources fall through to Athena.
        """
        self._athena = athena_executor
        self._source_db = source_db_executor
        self._firewall = firewall or SQLFirewall()
        # Optional registry used ONLY for the has_jdbc_endpoint gate:
        # confirm a candidate single source is direct-JDBC capable before routing
        # to the JDBC executor. NOTE: get_source() is NOT cached today, so this
        # adds one DDB GetItem on the JDBC-candidate path (single-source + explicit
        # id only; cross-source/no-id queries skip it). A cached source-metadata
        # read is a tracked follow-up if this lookup shows up in latency.
        self._sources = sources_registry
        # Optional Redshift Serverless executor for Glue/Iceberg sources that opt
        # into Redshift execution (queryEngine=REDSHIFT). When None, such
        # sources fall through to Athena — i.e. the deployed Athena-only behaviour
        # is never regressed; this only ADDS the Redshift path when wired.
        self._redshift = redshift_executor

    async def execute(
        self,
        sql: str,
        *,
        namespace: str,
        data_source_id: str = "",
        params: dict[str, Any] | None = None,
        max_rows: int = 1000,
        timeout_seconds: int = 10,
    ) -> QueryResult:
        """Dispatch to the JDBC or Athena executor based on source cardinality.

        Direct JDBC (fast path) is chosen when ALL hold: a JDBC executor exists,
        an explicit single ``data_source_id`` is supplied, the SQL targets a
        single source (either fully qualified or all-bare-table), AND that
        source is a JDBC-capable DATABASE source (``has_jdbc_endpoint``).

        NL-to-SQL provides ``data_source_id`` resolved from OpenSearch class
        metadata (all retrieved hits agree on one source) — its SQL uses bare
        table names, which are unambiguous when the source is known.

        Fallback: when no data_source_id is provided but the namespace has
        exactly one DATABASE source with queryEngine=JDBC, bare-table SQL is
        routed to JDBC (covers namespaces not yet re-ingested with the
        data_source_id field in OpenSearch).
        """
        # JDBC fast path requires BOTH: a structurally single-source query (cheap
        # AST check) AND a confirmed JDBC-capable source (registry lookup — only
        # performed when the cheap check passes).
        resolved_source_id = data_source_id
        is_jdbc_candidate = self._source_db is not None and self._select_jdbc_candidate(sql, data_source_id)

        # The source record backing the JDBC decision, fetched ONCE and reused for
        # dialect resolution so the fast path does a single get_source DDB lookup.
        jdbc_source: dict[str, Any] | None = None

        # Fallback: for bare-table SQL with no explicit data_source_id, try to
        # resolve the namespace's sole DATABASE source for JDBC routing.
        # Guard: only attempt if SQL is actually bare-table — qualified
        # catalog.schema.table SQL cannot run on a single Postgres source.
        sole_source_resolved = False
        if (
            not is_jdbc_candidate
            and self._source_db is not None
            and self._sources
            and (not data_source_id or data_source_id == "default")
        ):
            catalogs_check, _ = self._catalog_analysis(sql)
            if not catalogs_check:
                sole_source, sole_source_record = await self._resolve_sole_jdbc_source(namespace)
                if sole_source:
                    resolved_source_id = sole_source
                    is_jdbc_candidate = True
                    sole_source_resolved = True
                    jdbc_source = sole_source_record

        # _resolve_sole_jdbc_source already validated queryEngine=JDBC + queryable=True
        # AND handed back the source record, so skip the redundant _fetch_jdbc_source
        # DDB round-trip for that path. Otherwise fetch (and reuse) the record once.
        if is_jdbc_candidate and not sole_source_resolved:
            jdbc_source = await self._fetch_jdbc_source(namespace, resolved_source_id)
        # A source whose engine has no direct adapter (Snowflake, Oracle) must NOT
        # take the direct route: get_adapter would hand back the PostgreSQL fail-safe
        # and we would transpile Trino->postgres and execute it against the wrong
        # engine. Athena federation speaks Trino natively and is the route these
        # sources already take (queryEngine is ATHENA for them), so this is a
        # belt-and-braces guard against a stale queryEngine value on the record.
        use_jdbc = is_jdbc_candidate and jdbc_source is not None and self._engine_has_direct_route(jdbc_source)
        if use_jdbc:
            # VKG translates to Trino dialect; re-transpile to the source engine's
            # dialect for direct JDBC. Redshift and PostgreSQL diverge on
            # constructs sqlglot rewrites differently (date arithmetic ->
            # DATEADD vs INTERVAL, TRY_CAST, type-name normalization); a
            # postgres-target transpile produces SQL Redshift rejects.
            # Dialect is derived from the ALREADY-fetched source record (no 2nd lookup).
            # sqlglot.transpile is fast (~μs per statement) — no need for run_in_executor.
            target_dialect = self._target_dialect_for_source(jdbc_source)
            try:
                transpiled_sql = sqlglot.transpile(sql, read="trino", write=target_dialect, identify=False)[0]
            except sqlglot.errors.SqlglotError as exc:
                # Catch transpilation failures specifically (parse/unsupported/optimize)
                # — a genuine bug (AttributeError, etc.) must surface, not be silently
                # rerouted to Athena. Mirrors base.inject_limit's SqlglotError handling.
                #
                # `sql` here is post-substitution (contains caller-supplied
                # dimension values — potential PII). Log length only, not the
                # statement; mirrors the orchestrator's SQL-log discipline.
                #
                # Do NOT forward the untranspiled Trino SQL to the JDBC engine:
                # Trino-only constructs run with different (silently wrong) semantics
                # on MySQL/Postgres, and a Trino `LIMIT` is invalid T-SQL. Fall back
                # to Athena, which executes Trino SQL natively — the same safe path
                # taken when no JDBC executor is configured.
                logger.warning(
                    "jdbc_trino_transpile_failed_fallback_athena",
                    target_dialect=target_dialect,
                    sql_len=len(sql),
                    error=str(exc),
                )
                logger.info("composite_dispatch", route="athena_federation", data_source_id=data_source_id or "")
                return await self._athena.execute(
                    sql,
                    namespace=namespace,
                    data_source_id=data_source_id,
                    max_rows=max_rows,
                    timeout_seconds=timeout_seconds,
                )
            logger.info(
                "composite_dispatch",
                route="jdbc_direct",
                data_source_id=resolved_source_id,
                target_dialect=target_dialect,
            )
            # Namespace-scope authorization for the direct-JDBC route. The Athena
            # and Redshift executors each run this check internally, but the
            # direct-JDBC executor (source_db.execute) does not — it only enforces
            # SELECT-only + dangerous-function rules. Without this, a statement that
            # the router sends to JDBC (a 2-part schema.table, or a mixed
            # bare/qualified join) could reference a schema OUTSIDE the namespace's
            # authorized scope and execute unauthorized on the source credential.
            # Checked on the pre-transpile Trino SQL, which carries the qualifiers.
            discovered_schemas = jdbc_source.get("discoveredSchemas")
            selected_source_schemas = frozenset(
                str(schema).lower()
                for schema in (discovered_schemas if isinstance(discovered_schemas, list) else [])
                if isinstance(schema, str)
            )
            await self._authorize_qualified_references(sql, namespace, selected_source_schemas)
            return await self._source_db.execute(  # type: ignore[union-attr]
                transpiled_sql,
                namespace=namespace,
                data_source_id=resolved_source_id,
                params=params,
                max_rows=max_rows,
                timeout_seconds=timeout_seconds,
            )

        # Glue/Iceberg sources that opted into Redshift execution
        # (queryEngine=REDSHIFT) route to the Redshift Serverless executor instead
        # of Athena. Only attempted when a Redshift executor is wired AND the
        # target source is confirmed Redshift-capable — otherwise falls through to
        # Athena (deployed default). Uses the same data_source_id the Athena path
        # would; the Redshift executor rewrites to `awsdatacatalog` 3-part names.
        #
        # Guard (mirrors the sole-JDBC guard above): SQL spanning MORE THAN ONE
        # catalog cannot run on Redshift. Redshift reaches Glue via Spectrum
        # (`awsdatacatalog` only) and cannot address an Athena federated connector
        # catalog at all, so a cross-catalog statement must go to Athena — even
        # when the namespace's sole DATABASE source is Redshift-routed (possible
        # when the query's other source is Glue/S3, which is not a DATABASE source).
        catalogs, has_bare_table = self._catalog_analysis(sql)
        # Keep off Redshift anything it cannot address: >=2 distinct catalogs (a
        # federated Athena catalog is unreachable via Spectrum, which sees only
        # awsdatacatalog), AND a bare table mixed with a catalog-qualified one —
        # that mix is ambiguous exactly as it is for the JDBC path
        # (_select_jdbc_candidate), so it must fall through to Athena rather than
        # run on Redshift against a guessed catalog. Both go to Athena federation.
        redshift_addressable = len(catalogs) < 2 and not (has_bare_table and catalogs)
        if self._redshift is not None and redshift_addressable:
            redshift_source_id = await self._resolve_redshift_source(namespace, data_source_id)
            if redshift_source_id:
                logger.info("composite_dispatch", route="redshift_serverless", data_source_id=redshift_source_id)
                return await self._redshift.execute(
                    sql,
                    namespace=namespace,
                    data_source_id=redshift_source_id,
                    params=params,
                    max_rows=max_rows,
                    timeout_seconds=timeout_seconds,
                )
        # AthenaQueryExecutor.execute() takes NO ``params`` kwarg (it never used
        # bind params — federation SQL is fully materialized). Forwarding params
        # here would TypeError, so omit it — this keeps the Athena call shape
        # bit-for-bit identical to the previously-deployed direct-Athena wiring.
        logger.info("composite_dispatch", route="athena_federation", data_source_id=data_source_id or "")
        return await self._athena.execute(
            sql,
            namespace=namespace,
            data_source_id=data_source_id,
            max_rows=max_rows,
            timeout_seconds=timeout_seconds,
        )

    async def _authorize_qualified_references(
        self,
        sql: str,
        namespace: str,
        selected_source_schemas: frozenset[str],
    ) -> None:
        """Deny qualified references outside ``namespace`` before the JDBC route runs.

        Mirrors ``AthenaQueryExecutor._authorize_qualified_references`` /
        ``RedshiftDataQueryExecutor._authorize_qualified_references`` so all three
        executor routes enforce the same namespace-scope boundary. Bare-table SQL
        stays on the source-resolved context and needs no extra lookup; any
        dot-qualified reference can escape that context, so it is fail-closed on an
        unavailable source inventory. Default catalog is ``awsdatacatalog`` — the
        same normalization the Redshift route uses — because a direct-JDBC single
        source carries one implicit catalog.
        """
        if self._sources is None:
            return
        if not any("." in ref for ref in self._firewall.extract_tables(sql)):
            return
        scope = await self._sources.sql_namespace_scope(namespace)
        if scope is None:
            raise NamespaceSQLScopeError("Unable to verify SQL references for the requested namespace")
        try:
            self._firewall.validate_namespace_sql_scope(
                sql,
                native_databases=scope.native_databases,
                federated_catalog_schemas=scope.federated_catalog_schemas,
                default_catalog="awsdatacatalog",
                # The direct-JDBC connection supplies the catalog, so a bare
                # "schema.table" is authorized only when that schema belongs to
                # the selected source whose credentials will execute the query.
                # An explicit 3-part name is still checked catalog-strict.
                schema_only=True,
                selected_source_schemas=selected_source_schemas,
            )
        except NamespaceSQLScopeError as exc:
            logger.warning(
                "namespace_scope_denied",
                route="jdbc_direct",
                namespace=namespace,
                reason=str(exc),
                native_databases=sorted(scope.native_databases),
                federated_catalog_schemas=sorted(f"{c}.{d}" for c, d in scope.federated_catalog_schemas),
            )
            raise

    def _select_jdbc_candidate(self, sql: str, data_source_id: str) -> bool:
        """Return True if ``sql`` is a single-source candidate for the JDBC path.

        True when:
        1. an explicit, resolvable ``data_source_id`` is supplied (the JDBC
           executor needs it to locate credentials); AND
        2. the statement does not span more than one catalog and carries no bare
           reference that could belong to a different source. Concretely, with an
           explicit source, ALL of these route to JDBC:
           a) every table qualified to the SAME single catalog (3-part,
              one catalog) — unambiguous catalog routing;
           b) every table two-part ``schema.table`` — one implicit (default)
              catalog, possibly several schemas of the one source; this is the
              form the schema-restore pass emits for a single-source query and it
              MUST stay on JDBC (its whole reason to exist);
           c) all tables bare — bare tables belong to the explicit source (the
              NL-to-SQL path, where retrieval provides the source externally);
           d) any mix of the above that still resolves to ≤1 catalog and no bare
              table alongside a catalog-qualified one.

        Only genuinely ambiguous SQL — two or more distinct catalogs, or a bare
        table mixed with catalog-qualified ones — routes to Athena federation.
        """
        if not data_source_id or data_source_id == "default":
            return False
        catalogs, has_bare_table = self._catalog_analysis(sql)
        # ≥2 distinct catalogs is genuinely cross-catalog → Athena.
        if len(catalogs) >= 2:
            return False
        # A bare table is unambiguous ONLY when nothing is catalog-qualified
        # (all-bare, single source). Bare mixed with a catalog is ambiguous.
        if has_bare_table:
            return len(catalogs) == 0
        # No bare tables and ≤1 catalog: single-source, JDBC-eligible. This is the
        # 3-part-single-catalog case AND the 2-part schema.table case (catalogs is
        # empty for the latter, which must NOT disqualify it — see the cross-source review).
        return True

    async def _fetch_jdbc_source(self, namespace: str, data_source_id: str) -> dict[str, Any] | None:
        """Return the source record IFF it is direct-JDBC capable AND queryable, else None.

        Source records set ``sourceType=DATABASE`` for BOTH Glue-federated and
        direct-JDBC sources — so sourceType does NOT discriminate. The authoritative
        signal is ``queryEngine`` (set by the control plane's _resolve_query_engine:
        ``JDBC`` only when a direct dialect exists — PostgreSQL, Redshift, MySQL,
        and SQL Server today (``jdbc.py:DIRECT_QUERY_ENGINES``) —
        else ``ATHENA``). We additionally require ``queryable is True`` (a JDBC
        source is only queryable after federation provisioning). A Glue/S3 source,
        an Athena-engine source, or a not-yet-queryable source → None (Athena).

        Returns the fetched source dict so the caller can reuse it (e.g. to resolve
        the transpile dialect) WITHOUT a second DDB ``get_source`` round-trip on the
        JDBC fast path. With no registry wired, or any lookup error, we FAIL SAFE to
        None (never route an unconfirmed source to direct JDBC).
        """
        if self._sources is None:
            return None
        try:
            source = await self._sources.get_source(namespace, data_source_id)
        except Exception as exc:
            # Fail-safe to Athena, but keep transient registry errors (throttling,
            # network) distinguishable from a genuinely missing source in the logs.
            logger.warning(
                "jdbc_endpoint_check_failed",
                namespace=namespace,
                data_source_id=data_source_id,
                error=type(exc).__name__,
                error_msg=str(exc),
            )
            return None
        if not source:
            return None
        # queryEngine is a str-enum serialized as "JDBC"/"ATHENA"; compare uppercased.
        query_engine = str(source.get("queryEngine", "")).upper()
        if query_engine == "JDBC" and source.get("queryable") is True:
            return source
        return None

    def _engine_has_direct_route(self, source: dict[str, Any] | None) -> bool:
        """True when the source's engine may be executed over direct JDBC.

        Reads the engine from the ``configuration`` JSON blob (same place
        :meth:`_target_dialect_for_source` reads it) and defers the decision to
        ``db_adapters.is_direct_query_engine`` — so the route choice and the
        engine->dialect mapping can never disagree about which engines are enabled.
        A source with no parseable engine keeps the historical behavior (the
        PostgreSQL default), matching ``get_adapter(None)``.
        """
        if not source or self._sources is None:
            return False
        config = self._sources.parse_configuration(source)
        engine = str(config.get("engine", "")).upper()
        # Absent engine == legacy PostgreSQL source; get_adapter defaults the same way.
        return is_direct_query_engine(engine or "POSTGRESQL")

    def _target_dialect_for_source(self, source: dict[str, Any] | None) -> str:
        """Return the sqlglot write-dialect for an already-fetched JDBC source.

        VKG emits Trino SQL that we re-transpile to the source engine's dialect
        before direct JDBC execution. Engines diverge on constructs sqlglot
        rewrites per target (e.g. ``DATE_ADD`` -> ``ts + INTERVAL '1 DAY'`` for
        postgres, ``DATEADD(DAY, 1, ts)`` for redshift, ``DATE_ADD(ts, INTERVAL 1
        DAY)`` for mysql; ``LIMIT`` -> ``TOP`` for tsql), so a wrong-target
        transpile raises at execution time.

        The dialect comes from the per-engine adapter registry (the SAME
        ``get_adapter`` the direct executor uses), so the composite and executor
        never drift on the engine->dialect mapping. The source engine lives in the
        ``configuration`` JSON blob under the ``engine`` key. FAILS SAFE to
        ``"postgres"`` (the PostgreSQL adapter) for a missing source or an unknown
        engine — preserving the previous behavior for every non-mapped source.

        Takes the source dict the caller ALREADY fetched (via _fetch_jdbc_source or
        _resolve_sole_jdbc_source) so the JDBC fast path performs exactly ONE
        ``get_source`` DDB lookup, not two.
        """
        if not source or self._sources is None:
            return "postgres"
        config = self._sources.parse_configuration(source)
        engine = str(config.get("engine", "")).upper()
        return get_adapter(engine).sqlglot_dialect

    @staticmethod
    def _is_redshift_routed(source: dict[str, Any]) -> bool:
        """A source routes to Redshift only when it opted in AND is queryable.

        The defining gate of ``_resolve_redshift_source``: ``queryEngine=REDSHIFT``
        (the opt-in) plus ``queryable is True`` (approved for serving). Anything
        else stays on Athena.
        """
        return str(source.get("queryEngine", "")).upper() == "REDSHIFT" and source.get("queryable") is True

    async def _resolve_redshift_source(self, namespace: str, data_source_id: str) -> str:
        """Resolve a Redshift-execution source id, or "" if none applies.

        Returns the source id when the target Glue source is confirmed
        ``queryEngine=REDSHIFT`` AND ``queryable is True`` — either the explicit
        ``data_source_id``, or the namespace's sole DATABASE source when no id was
        supplied. Returns "" (→ Athena fallback) for any other case, a missing
        registry, or a lookup error (fail-safe to Athena, never mis-route).
        """
        if self._sources is None:
            return ""
        try:
            if data_source_id and data_source_id != "default":
                source = await self._sources.get_source(namespace, data_source_id)
            else:
                source = await self._sources.find_sole_database_source(namespace)
        except Exception as exc:
            logger.warning(
                "redshift_endpoint_check_failed",
                namespace=namespace,
                data_source_id=data_source_id,
                error=type(exc).__name__,
                error_msg=str(exc),
            )
            return ""
        # The Redshift-routing gate (opted-in + queryable). Anything else → "" → Athena.
        if not source or not self._is_redshift_routed(source):
            return ""
        # Return the resolved sourceId; never echo back a "default"/empty caller
        # value (that would re-trigger the executor's weaker sole-source lookup).
        return source.get("sourceId", "")

    async def _resolve_sole_jdbc_source(self, namespace: str) -> tuple[str, dict[str, Any] | None]:
        """Resolve the namespace's sole JDBC source for bare-table SQL routing.

        Returns ``(source_id, source_record)`` only when exactly one DATABASE source
        exists in the namespace AND it has queryEngine=JDBC and queryable=True. Returns
        ``("", None)`` if zero, multiple, or non-JDBC sources — Athena handles it. The
        source record is returned alongside the id so the caller can reuse it for
        dialect resolution WITHOUT a second DDB lookup on the JDBC fast path.
        """
        try:
            source = await self._sources.find_sole_database_source(namespace)
        except Exception as exc:
            logger.warning(
                "sole_jdbc_resolve_failed",
                namespace=namespace,
                error=type(exc).__name__,
                error_msg=str(exc),
            )
            return "", None
        if not source:
            return "", None
        if str(source.get("queryEngine", "")).upper() != "JDBC":
            return "", None
        if source.get("queryable") is not True:
            return "", None
        source_id = source.get("sourceId", "")
        if source_id:
            logger.info(
                "composite_sole_jdbc_resolved",
                namespace=namespace,
                source_id=source_id,
            )
        return source_id, source

    def _catalog_analysis(self, sql: str) -> tuple[set[str], bool]:
        """Return (distinct catalog prefixes, any-bare-table-present) for ``sql``.

        Reuses the firewall's AST table extraction (qualified ``catalog.db.table``
        / ``db.table`` / bare ``table`` names) — never re-parses.

        Only a THREE-part ``catalog.schema.table`` name carries a catalog: its
        first segment is the Athena catalog and is counted. A TWO-part
        ``schema.table`` name does NOT — its first segment is a schema and the
        catalog is the pinned default, so counting it as a catalog would make a
        single-source, multi-schema query look cross-catalog and misroute it off
        the direct-JDBC path onto Athena federation (which is exactly what the
        two-part form exists to avoid). A bare one-part name cannot be attributed
        to a catalog, so its presence makes catalog routing ambiguous and forces
        the Athena path. A two-part name is therefore neither: it counts toward no
        catalog and is not bare.
        """
        catalogs: set[str] = set()
        has_bare_table = False
        for qualified in self._firewall.extract_tables(sql):
            parts = qualified.split(".")
            if len(parts) >= 3:
                catalogs.add(parts[0])
            elif len(parts) == 1:
                has_bare_table = True
            # len(parts) == 2 → schema.table, single implicit catalog: neither.
        return catalogs, has_bare_table

    async def resolve_target_dialect(self, namespace: str, data_source_id: str = "") -> str:
        """Resolve the sqlglot dialect for NL→SQL generation from the namespace sources.

        Returns the target SQL dialect that should be used for LLM SQL generation,
        matching the dialect the composite executor will use for execution. This
        ensures the SQL the LLM generates is in the correct dialect for the target
        engine, avoiding transpilation errors and dialect mismatches.

        Args:
            namespace: The namespace to resolve sources for.
            data_source_id: Optional explicit source ID; when provided and resolvable,
                its engine's dialect is returned. When absent or unresolvable, falls
                back to sole-source resolution.

        Returns:
            A sqlglot dialect string (e.g. "postgresql", "athena"). Returns "athena"
            for cross-source/Glue/ambiguous cases, and the source engine's dialect
            when a single JDBC source is resolved.
        """
        if not self._sources:
            return "athena"

        # Explicit source ID provided — fetch it and derive dialect
        if data_source_id and data_source_id != "default":
            source = await self._fetch_jdbc_source(namespace, data_source_id)
            if source:
                return self._target_dialect_for_source(source)

        # No explicit source or it didn't resolve — try sole-source resolution
        _, sole_source = await self._resolve_sole_jdbc_source(namespace)
        if sole_source:
            return self._target_dialect_for_source(sole_source)

        # Multiple sources or no JDBC sources → federated Athena/Trino
        return "athena"

    async def health_check(self) -> dict[str, Any]:
        """Aggregate the Athena (and JDBC, if present) executor health into one dict."""
        athena_health = await self._athena.health_check()
        health: dict[str, Any] = {"status": athena_health.get("status", "unknown"), "athena": athena_health}
        if self._source_db is not None:
            health["source_db"] = await self._source_db.health_check()
        if self._redshift is not None:
            health["redshift"] = await self._redshift.health_check()
        return health

    async def close(self) -> None:
        """Close both wrapped executors, logging (not raising) any close errors."""
        for executor in (self._athena, self._source_db, self._redshift):
            close_fn = getattr(executor, "close", None)
            if close_fn and callable(close_fn):
                try:
                    await close_fn()
                except Exception:
                    logger.warning("composite_close_error", executor=type(executor).__name__)
