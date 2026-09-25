# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the CompositeQueryExecutor (single-vs-cross dispatch)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import sqlglot
import sqlglot.errors
from coa_serve.clients.base import QueryResult
from coa_serve.clients.composite_executor import CompositeQueryExecutor
from coa_serve.clients.sources_registry import SourcesRegistry


def _make_executor(label: str):
    ex = AsyncMock()
    ex.execute.return_value = QueryResult(
        rows=[{"engine": label}], columns=["engine"], row_count=1, truncated=False, duration_ms=1
    )
    ex.health_check.return_value = {"status": "ok", "backend": label}
    return ex


def _jdbc_registry(engine: str = "POSTGRESQL"):
    """Registry mock returning a REAL-shaped direct-JDBC source record.

    Real records set sourceType=DATABASE for BOTH Glue and JDBC; the
    discriminator is queryEngine (JDBC vs ATHENA) + queryable. The source
    engine (POSTGRESQL/REDSHIFT) lives in the ``configuration`` JSON blob under
    the ``engine`` key — the same key the sources package reads. ``engine=""``
    omits the blob to exercise the missing-config fail-safe path.
    """
    import json

    reg = AsyncMock()
    record = {
        "sourceType": "DATABASE",
        "sourceSubType": "JDBC_DATABASE",
        "queryEngine": "JDBC",
        "queryable": True,
        "credentialSecretArn": "arn:secret",
    }
    if engine:
        record["configuration"] = json.dumps({"engine": engine})
    reg.get_source.return_value = record
    # parse_configuration is a synchronous @staticmethod — use the real impl so
    # dialect resolution parses the blob exactly as production does (an
    # AsyncMock would return an unawaited coroutine and break the JSON parse).
    reg.parse_configuration = SourcesRegistry.parse_configuration
    # The composite now scope-authorizes the JDBC route (mirrors Athena/Redshift).
    # Return a scope that authorizes the schemas the dispatch tests reference
    # (bare-db + the two-part sales/crm/public schemas) so routing tests still
    # exercise routing, not authorization. The deny path has its own tests.
    from coa_serve.clients.sources_registry import SQLNamespaceScope

    reg.sql_namespace_scope.return_value = SQLNamespaceScope(
        native_databases=frozenset({"mydb", "public", "sales", "crm", "orders", "customers"}),
        federated_catalog_schemas=frozenset({("mydb", "public"), ("mydb", "sales"), ("mydb", "crm")}),
    )
    return reg


def _glue_registry(queryable=True):
    """Registry mock returning a REAL-shaped Glue source (sourceType=DATABASE, queryEngine=ATHENA)."""
    reg = AsyncMock()
    reg.get_source.return_value = {
        "sourceType": "DATABASE",
        "sourceSubType": "GLUE_DATABASE",
        "queryEngine": "ATHENA",
        "queryable": queryable,
    }
    return reg


class _RealSignatureAthena:
    """Fake Athena executor with the REAL execute() signature (NO ``params`` kwarg).

    Mirrors AthenaQueryExecutor.execute so a regression where the composite
    forwards ``params`` to Athena raises TypeError here (an AsyncMock would
    silently accept it and hide the deployed-path break codex caught).
    """

    def __init__(self):
        self.calls: list[dict] = []

    async def execute(self, sql, *, namespace, data_source_id="", database="", max_rows=1000, timeout_seconds=120):
        self.calls.append({"sql": sql, "namespace": namespace, "data_source_id": data_source_id})
        return QueryResult(rows=[{"engine": "athena"}], columns=["engine"], row_count=1, truncated=False, duration_ms=1)

    async def health_check(self):
        return {"status": "ok", "backend": "athena"}


@pytest.mark.unit
class TestCompositeDispatch:
    async def test_no_jdbc_executor_always_athena(self):
        """Without a JDBC executor, every query (even single-source) → Athena."""
        athena = _make_executor("athena")
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=None)
        await comp.execute("SELECT * FROM mydb.public.orders", namespace="ns", data_source_id="mydb")
        athena.execute.assert_awaited_once()

    async def test_single_source_with_id_routes_jdbc(self):
        """Single catalog + explicit id + JDBC executor + DATABASE source → direct JDBC."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry()
        )
        result = await comp.execute("SELECT id FROM mydb.public.orders", namespace="ns", data_source_id="mydb")
        jdbc.execute.assert_awaited_once()
        athena.execute.assert_not_awaited()
        assert result.rows[0]["engine"] == "jdbc"

    async def test_two_part_single_source_multi_schema_routes_jdbc(self):
        """Code review finding: two-part ``schema.table`` names are a SINGLE-source,
        one-implicit-catalog query — the form the schema-restore pass emits — and
        must route to direct JDBC, not Athena federation. Two DIFFERENT schemas of
        the one source must not be mistaken for two catalogs."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry()
        )
        result = await comp.execute(
            'SELECT a.id FROM "sales"."orders" a JOIN "crm"."customers" b ON a.cid = b.cid',
            namespace="ns",
            data_source_id="mydb",
        )
        jdbc.execute.assert_awaited_once()
        athena.execute.assert_not_awaited()
        assert result.rows[0]["engine"] == "jdbc"

    async def test_three_part_two_catalog_still_routes_athena(self):
        """The complement of the above: genuinely cross-CATALOG (3-part, two
        distinct catalogs) still routes to Athena federation even with a JDBC
        executor and an explicit source id — the two-part fix must not widen this."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry()
        )
        await comp.execute(
            'SELECT a.id FROM "pg_cat"."public"."orders" a JOIN "awsdatacatalog"."crm"."customers" b ON a.cid = b.cid',
            namespace="ns",
            data_source_id="mydb",
        )
        athena.execute.assert_awaited_once()
        jdbc.execute.assert_not_awaited()

    @pytest.mark.parametrize("engine", ["SNOWFLAKE", "ORACLE"])
    async def test_no_direct_adapter_routes_athena_not_jdbc(self, engine):
        """Engines without a direct-query adapter route through Athena federation.

        Snowflake and Oracle have no adapter in db_adapters, so get_adapter hands
        back the PostgreSQL fail-safe — taking the direct route would transpile
        Trino->postgres and run it against the wrong engine. Athena federation speaks
        Trino natively and is the route these sources already get from their ATHENA
        queryEngine; this asserts the guard holds even with a stale queryEngine=JDBC
        on the record.
        """
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry(engine=engine)
        )
        await comp.execute("SELECT id FROM mydb.public.orders", namespace="ns", data_source_id="mydb")
        athena.execute.assert_awaited_once()
        jdbc.execute.assert_not_awaited()

    async def test_glue_source_routes_athena_even_if_single(self):
        """endpoint gate: a single-source query against a GLUE source → Athena."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_glue_registry()
        )
        await comp.execute("SELECT id FROM mydb.public.orders", namespace="ns", data_source_id="mydb")
        athena.execute.assert_awaited_once()
        jdbc.execute.assert_not_awaited()

    async def test_no_registry_fails_safe_to_athena(self):
        """No registry to confirm endpoint → fail safe to Athena (never unconfirmed JDBC)."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc)
        await comp.execute("SELECT id FROM mydb.public.orders", namespace="ns", data_source_id="mydb")
        athena.execute.assert_awaited_once()
        jdbc.execute.assert_not_awaited()

    async def test_real_glue_shape_routes_athena(self):
        """codex BLOCK: a Glue source is sourceType=DATABASE but queryEngine=ATHENA → Athena
        (the sourceType==DATABASE gate would have wrongly sent it to JDBC → 500)."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_glue_registry()
        )
        await comp.execute("SELECT id FROM mydb.public.orders", namespace="ns", data_source_id="mydb")
        athena.execute.assert_awaited_once()
        jdbc.execute.assert_not_awaited()

    async def test_jdbc_not_yet_queryable_routes_athena(self):
        """A JDBC source not yet provisioned (queryable=False) → Athena until ready."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        reg = AsyncMock()
        reg.get_source.return_value = {"sourceType": "DATABASE", "queryEngine": "JDBC", "queryable": False}
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc, sources_registry=reg)
        await comp.execute("SELECT id FROM mydb.public.orders", namespace="ns", data_source_id="mydb")
        athena.execute.assert_awaited_once()
        jdbc.execute.assert_not_awaited()

    async def test_cross_source_routes_athena(self):
        """Multiple catalogs → Athena federation even with a JDBC executor present."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc)
        await comp.execute(
            "SELECT a.id FROM db1.s.t a JOIN db2.s.t b ON a.id = b.id",
            namespace="ns",
            data_source_id="db1",
        )
        athena.execute.assert_awaited_once()
        jdbc.execute.assert_not_awaited()

    async def test_no_data_source_id_routes_athena(self):
        """Single catalog but no resolvable source id → Athena (JDBC can't get creds)."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc)
        await comp.execute("SELECT id FROM mydb.public.orders", namespace="ns", data_source_id="")
        athena.execute.assert_awaited_once()
        jdbc.execute.assert_not_awaited()

    async def test_default_sentinel_routes_athena(self):
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc)
        await comp.execute("SELECT id FROM mydb.public.orders", namespace="ns", data_source_id="default")
        athena.execute.assert_awaited_once()
        jdbc.execute.assert_not_awaited()

    async def test_bare_table_with_explicit_source_routes_jdbc(self):
        """Bare tables + explicit data_source_id → JDBC (NL-to-SQL retrieval-based resolution)."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry()
        )
        result = await comp.execute("SELECT id FROM orders", namespace="ns", data_source_id="src-uuid")
        jdbc.execute.assert_awaited_once()
        athena.execute.assert_not_awaited()
        assert result.rows[0]["engine"] == "jdbc"

    async def test_bare_table_no_source_id_no_registry_routes_athena(self):
        """Bare table SQL + no source ID + no registry → Athena."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc)
        await comp.execute("SELECT id FROM orders", namespace="ns", data_source_id="")
        athena.execute.assert_awaited_once()
        jdbc.execute.assert_not_awaited()


@pytest.mark.unit
class TestJdbcRouteNamespaceScopeAuthorization:
    """Review finding: the direct-JDBC route (source_db.execute) enforced only
    SELECT-only + dangerous-function rules — NOT namespace-scope authorization,
    which lived solely in the Athena and Redshift executors. A statement the
    router sends to JDBC could therefore reference a schema outside the
    namespace's authorized scope and execute unauthorized on the source
    credential. The composite now runs _authorize_qualified_references before the
    JDBC dispatch. Routing is asserted elsewhere; these assert AUTHORIZATION."""

    def _registry_scoped_to(self, *databases: str):
        from coa_serve.clients.sources_registry import SourcesRegistry, SQLNamespaceScope

        reg = _jdbc_registry()  # base record for routing + dialect
        reg.sql_namespace_scope.return_value = SQLNamespaceScope(
            native_databases=frozenset(databases),
            federated_catalog_schemas=frozenset(),
        )
        reg.parse_configuration = SourcesRegistry.parse_configuration
        return reg

    async def test_jdbc_route_invokes_namespace_scope_check(self):
        """A qualified statement routed to JDBC whose schema IS in scope executes,
        and the scope check was consulted (not skipped)."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        reg = self._registry_scoped_to("sales", "crm")
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc, sources_registry=reg)
        await comp.execute(
            'SELECT a.id FROM "sales"."orders" a JOIN "crm"."customers" b ON a.cid = b.cid',
            namespace="ns",
            data_source_id="mydb",
        )
        reg.sql_namespace_scope.assert_awaited()  # authorization actually ran
        jdbc.execute.assert_awaited_once()

    async def test_out_of_scope_schema_on_jdbc_is_denied(self):
        """A qualified reference to a schema OUTSIDE the namespace's scope, routed
        to JDBC, is denied — the JDBC executor is never reached."""
        from coa_serve.tier2.sql_firewall import NamespaceSQLScopeError

        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        # Scope authorizes only "sales"; the query also references "secret_hr".
        reg = self._registry_scoped_to("sales")
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc, sources_registry=reg)
        with pytest.raises(NamespaceSQLScopeError):
            await comp.execute(
                'SELECT a.id FROM "sales"."orders" a JOIN "secret_hr"."salaries" b ON a.cid = b.cid',
                namespace="ns",
                data_source_id="mydb",
            )
        jdbc.execute.assert_not_awaited()
        athena.execute.assert_not_awaited()

    async def test_scope_unavailable_fails_closed_on_jdbc(self):
        """If the source inventory cannot be resolved, a qualified JDBC statement
        is denied rather than executed unauthorized."""
        from coa_serve.tier2.sql_firewall import NamespaceSQLScopeError

        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        reg = _jdbc_registry()
        reg.sql_namespace_scope.return_value = None  # inventory unavailable
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc, sources_registry=reg)
        with pytest.raises(NamespaceSQLScopeError):
            await comp.execute('SELECT id FROM "sales"."orders"', namespace="ns", data_source_id="mydb")
        jdbc.execute.assert_not_awaited()

    async def test_mixed_qualified_and_bare_table_routes_athena(self):
        """codex BLOCK: one qualified catalog + a BARE table is ambiguous → Athena,
        NOT single-source JDBC (the bare table could live in another source)."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc)
        await comp.execute(
            "SELECT o.id FROM catalog_a.public.orders o JOIN customers c ON o.cid = c.id",
            namespace="ns",
            data_source_id="catalog_a",
        )
        athena.execute.assert_awaited_once()
        jdbc.execute.assert_not_awaited()

    async def test_all_qualified_single_catalog_routes_jdbc(self):
        """All tables qualified to one catalog, no bare table, DATABASE source → direct JDBC."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry()
        )
        await comp.execute(
            "SELECT o.id FROM mydb.public.orders o JOIN mydb.public.customers c ON o.cid = c.id",
            namespace="ns",
            data_source_id="mydb",
        )
        jdbc.execute.assert_awaited_once()
        athena.execute.assert_not_awaited()

    async def test_athena_dispatch_omits_params_real_signature(self):
        """codex BLOCK: composite must NOT forward params to Athena (real sig has none)."""
        athena = _RealSignatureAthena()
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=None)
        # Would raise TypeError if the composite forwarded params= to Athena.
        result = await comp.execute(
            "SELECT id FROM mydb.public.orders", namespace="ns", data_source_id="mydb", params={"x": 1}
        )
        assert result.rows == [{"engine": "athena"}]
        assert len(athena.calls) == 1

    async def test_jdbc_dispatch_receives_params(self):
        """JDBC path still receives bind params (its execute accepts them)."""
        athena = _RealSignatureAthena()
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry()
        )
        await comp.execute("SELECT id FROM mydb.public.orders", namespace="ns", data_source_id="mydb", params={"x": 1})
        jdbc.execute.assert_awaited_once()
        assert jdbc.execute.call_args.kwargs["params"] == {"x": 1}

    async def test_sole_source_fallback_routes_jdbc(self):
        """Bare SQL + no explicit source + sole JDBC source in namespace → JDBC."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        reg = AsyncMock()
        reg.get_source.return_value = {
            "sourceType": "DATABASE",
            "queryEngine": "JDBC",
            "queryable": True,
            "sourceId": "only-source",
        }
        reg.find_sole_database_source.return_value = {
            "sourceType": "DATABASE",
            "queryEngine": "JDBC",
            "queryable": True,
            "sourceId": "only-source",
        }
        reg.parse_configuration = SourcesRegistry.parse_configuration
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc, sources_registry=reg)
        await comp.execute("SELECT id FROM orders", namespace="ns", data_source_id="")
        jdbc.execute.assert_awaited_once()
        athena.execute.assert_not_awaited()
        assert jdbc.execute.call_args.kwargs["data_source_id"] == "only-source"

    async def test_sole_source_fallback_multiple_sources_routes_athena(self):
        """Bare SQL + no explicit source + multiple DB sources → Athena (ambiguous)."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        reg = AsyncMock()
        reg.find_sole_database_source.return_value = None  # multiple sources
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc, sources_registry=reg)
        await comp.execute("SELECT id FROM orders", namespace="ns", data_source_id="")
        athena.execute.assert_awaited_once()
        jdbc.execute.assert_not_awaited()

    async def test_jdbc_path_retranspiles_trino_to_postgres(self):
        """VKG produces Trino SQL; JDBC path must re-transpile to Postgres before execution."""
        athena = _RealSignatureAthena()
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry("POSTGRESQL")
        )
        # SUBSTR is Trino syntax; Postgres uses SUBSTRING(x FROM n FOR m)
        trino_sql = "SELECT SUBSTR(name, 1, 3) FROM mydb.public.orders"
        await comp.execute(trino_sql, namespace="ns", data_source_id="mydb")
        jdbc.execute.assert_awaited_once()
        received_sql = jdbc.execute.call_args.args[0]
        # sqlglot rewrites SUBSTR → SUBSTRING(... FROM ... FOR ...) for postgres
        assert "SUBSTRING" in received_sql.upper()
        assert "SUBSTR(" not in received_sql.upper()

    async def test_jdbc_path_transpiles_trino_to_redshift_for_redshift_source(self):
        """a REDSHIFT-engine source must transpile to the redshift dialect.

        DATE_ADD is the clearest divergence: postgres emits interval arithmetic
        (``ts + INTERVAL '1 DAY'``, which Redshift rejects) while redshift emits
        ``DATEADD(DAY, 1, ts)``. Asserting DATEADD proves the write-dialect was
        redshift, not the hardcoded postgres of the original bug.
        """
        athena = _RealSignatureAthena()
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry("REDSHIFT")
        )
        trino_sql = "SELECT DATE_ADD('day', 1, ts) FROM mydb.public.orders"
        await comp.execute(trino_sql, namespace="ns", data_source_id="mydb")
        jdbc.execute.assert_awaited_once()
        received_sql = jdbc.execute.call_args.args[0]
        assert "DATEADD" in received_sql.upper()
        # The postgres form (INTERVAL arithmetic) must NOT be produced.
        assert "INTERVAL" not in received_sql.upper()

    async def test_jdbc_path_transpiles_trino_to_mysql_for_mysql_source(self):
        """a MYSQL-engine source must transpile to the mysql dialect.

        DATE_ADD is the clearest divergence: mysql emits
        ``DATE_ADD(ts, INTERVAL 1 DAY)`` while postgres emits interval arithmetic
        (``ts + INTERVAL '1 DAY'``). Asserting the mysql form proves the
        write-dialect was mysql (via the shared adapter registry), not postgres.
        """
        athena = _RealSignatureAthena()
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry("MYSQL")
        )
        trino_sql = "SELECT DATE_ADD('day', 1, ts) FROM mydb.public.orders"
        await comp.execute(trino_sql, namespace="ns", data_source_id="mydb")
        jdbc.execute.assert_awaited_once()
        received_sql = jdbc.execute.call_args.args[0]
        assert "DATE_ADD" in received_sql.upper()
        assert "INTERVAL" in received_sql.upper()

    async def test_jdbc_path_transpiles_trino_to_tsql_for_sqlserver_source(self):
        """a SQLSERVER-engine source must transpile to the tsql dialect.

        Trino ``STRPOS`` becomes T-SQL ``CHARINDEX`` — a construct unique to the
        tsql write-dialect — proving the composite resolved SQLSERVER -> tsql via
        the shared adapter registry, not the postgres fallback.
        """
        athena = _RealSignatureAthena()
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry("SQLSERVER")
        )
        trino_sql = "SELECT STRPOS(name, 'x') FROM mydb.public.orders"
        await comp.execute(trino_sql, namespace="ns", data_source_id="mydb")
        jdbc.execute.assert_awaited_once()
        received_sql = jdbc.execute.call_args.args[0]
        assert "CHARINDEX" in received_sql.upper()

    async def test_jdbc_explicit_source_does_single_get_source_lookup(self):
        """perf guard: the explicit-source JDBC path fetches the source record
        exactly ONCE (the JDBC-capability gate and the dialect resolution share it),
        not twice. Guards against the doubled-DDB-lookup regression flagged in review.
        """
        athena = _RealSignatureAthena()
        jdbc = _make_executor("jdbc")
        reg = _jdbc_registry("REDSHIFT")
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc, sources_registry=reg)
        await comp.execute(
            "SELECT DATE_ADD('day', 1, ts) FROM mydb.public.orders",
            namespace="ns",
            data_source_id="mydb",
        )
        jdbc.execute.assert_awaited_once()
        # Exactly one get_source round-trip for the whole dispatch (gate + dialect reuse it).
        assert reg.get_source.await_count == 1

    async def test_jdbc_sole_source_does_no_extra_get_source_lookup(self):
        """perf guard: the sole-source fallback resolves via
        find_sole_database_source and reuses that record for dialect — it must NOT
        call get_source at all on the JDBC path.
        """
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        reg = AsyncMock()
        sole = {
            "sourceType": "DATABASE",
            "queryEngine": "JDBC",
            "queryable": True,
            "sourceId": "only-source",
            "configuration": '{"engine": "REDSHIFT"}',
        }
        reg.find_sole_database_source.return_value = sole
        reg.parse_configuration = SourcesRegistry.parse_configuration
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc, sources_registry=reg)
        await comp.execute("SELECT DATE_ADD('day', 1, ts) FROM orders", namespace="ns", data_source_id="")
        jdbc.execute.assert_awaited_once()
        # Sole-source path reuses find_sole_database_source's record — no get_source.
        reg.get_source.assert_not_awaited()
        # And the reused record still drives redshift dialect selection.
        assert "DATEADD" in jdbc.execute.call_args.args[0].upper()

    async def test_jdbc_path_postgres_source_keeps_postgres_dialect(self):
        """regression guard: a POSTGRESQL source keeps postgres interval arithmetic."""
        athena = _RealSignatureAthena()
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry("POSTGRESQL")
        )
        trino_sql = "SELECT DATE_ADD('day', 1, ts) FROM mydb.public.orders"
        await comp.execute(trino_sql, namespace="ns", data_source_id="mydb")
        received_sql = jdbc.execute.call_args.args[0]
        assert "INTERVAL" in received_sql.upper()
        assert "DATEADD" not in received_sql.upper()

    async def test_jdbc_path_missing_engine_config_fails_safe_to_postgres(self):
        """a source with no engine in its config falls back to postgres (previous behavior)."""
        athena = _RealSignatureAthena()
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry(engine="")
        )
        trino_sql = "SELECT DATE_ADD('day', 1, ts) FROM mydb.public.orders"
        await comp.execute(trino_sql, namespace="ns", data_source_id="mydb")
        received_sql = jdbc.execute.call_args.args[0]
        assert "INTERVAL" in received_sql.upper()
        assert "DATEADD" not in received_sql.upper()

    async def test_athena_path_does_not_retranspile(self):
        """Athena runs Trino SQL natively — SQL reaches Athena unchanged."""
        athena = _RealSignatureAthena()
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=None)
        trino_sql = "SELECT SUBSTR(name, 1, 3) FROM mydb.public.orders"
        await comp.execute(trino_sql, namespace="ns", data_source_id="mydb")
        received_sql = athena.calls[0]["sql"]
        # SQL passed to Athena unchanged — Athena/Trino handles SUBSTR natively
        assert "SUBSTR(" in received_sql

    async def test_jdbc_transpile_failure_falls_back_to_athena(self):
        """If sqlglot transpile fails, the query falls back to Athena (which runs
        Trino SQL natively) rather than forwarding untranspiled Trino SQL to the
        JDBC engine, where it would run with wrong semantics or be rejected."""
        from unittest.mock import patch

        athena = _RealSignatureAthena()
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry()
        )
        bad_sql = "SELECT @@INVALID_TRINO FROM mydb.public.t"
        patch_target = "coa_serve.clients.composite_executor.sqlglot.transpile"
        # transpile raises SqlglotError on parse/unsupported/optimize failures — the
        # executor catches THAT specifically (a generic bug must propagate, not reroute).
        with patch(patch_target, side_effect=sqlglot.errors.ParseError("parse error")):
            result = await comp.execute(bad_sql, namespace="ns", data_source_id="mydb")
        # JDBC executor must NOT be called; Athena handles it with the ORIGINAL SQL.
        jdbc.execute.assert_not_awaited()
        assert len(athena.calls) == 1
        assert athena.calls[0]["sql"] == bad_sql
        assert result.rows == [{"engine": "athena"}]

    async def test_jdbc_transpile_unexpected_error_propagates(self):
        """A non-sqlglot error during transpile (a real bug) must PROPAGATE, not be
        silently rerouted to Athena — the except is narrowed to SqlglotError."""
        from unittest.mock import patch

        athena = _RealSignatureAthena()
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(
            athena_executor=athena, source_db_executor=jdbc, sources_registry=_jdbc_registry()
        )
        patch_target = "coa_serve.clients.composite_executor.sqlglot.transpile"
        with (
            patch(patch_target, side_effect=RuntimeError("unexpected bug")),
            pytest.raises(RuntimeError, match="unexpected bug"),
        ):
            await comp.execute("SELECT 1 FROM mydb.public.t", namespace="ns", data_source_id="mydb")
        # Neither executor should have been used — the bug surfaced instead.
        jdbc.execute.assert_not_awaited()
        assert len(athena.calls) == 0

    async def test_health_check_includes_both(self):
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        comp = CompositeQueryExecutor(athena_executor=athena, source_db_executor=jdbc)
        health = await comp.health_check()
        assert health["status"] == "ok"
        assert "athena" in health and "source_db" in health


@pytest.mark.unit
class TestAthenaLimitInjection:
    """dialect-aware LIMIT injection on the Athena/Trino path."""

    @staticmethod
    def _inject(sql: str, max_rows: int = 1000) -> str:
        from coa_serve.clients.athena import AthenaQueryExecutor

        return AthenaQueryExecutor._inject_limit(sql, max_rows)

    def test_appends_limit_when_absent(self):
        out = self._inject("SELECT id FROM orders", 1000)
        assert "LIMIT 1000" in out.upper()

    def test_caps_larger_existing_limit(self):
        out = self._inject("SELECT id FROM orders LIMIT 100000", 1000)
        assert "100000" not in out
        assert "LIMIT 1000" in out.upper()

    def test_keeps_smaller_existing_limit(self):
        out = self._inject("SELECT id FROM orders LIMIT 10", 1000)
        assert "LIMIT 10" in out.upper()
        assert "1000" not in out

    def test_unparseable_sql_falls_back_to_textual_append(self):
        # Garbage that sqlglot can't parse → still gets a textual LIMIT appended.
        out = self._inject(")(*&^ not sql", 50)
        assert out.rstrip().upper().endswith("LIMIT 50")

    def test_does_not_mutate_inner_subquery_limit(self):
        """codex BLOCK: capping a descendant LIMIT changes results (e.g. COUNT).
        Inner LIMIT 2000 must NOT be lowered; an outer LIMIT IS added instead."""
        out = self._inject("SELECT count(*) FROM (SELECT * FROM orders LIMIT 2000) t", 1000)
        assert "2000" in out  # inner limit preserved
        assert out.rstrip().upper().endswith("LIMIT 1000")  # outer cap appended

    def test_outer_query_with_only_inner_limit_still_capped(self):
        """codex BLOCK: a small subquery LIMIT must not be mistaken for an outer cap."""
        sql = "SELECT a.x FROM (SELECT x FROM orders LIMIT 5) a JOIN other_tbl o ON a.x = o.x"
        out = self._inject(sql, 1000)
        assert "LIMIT 5" in out  # inner preserved
        assert out.rstrip().upper().endswith("LIMIT 1000")  # outer cap added

    def test_outer_limit_within_cap_unchanged(self):
        out = self._inject("SELECT id FROM orders LIMIT 10", 1000)
        assert out == "SELECT id FROM orders LIMIT 10"


def _redshift_glue_registry(queryable=True, source_id="src-redshift"):
    """Registry mock returning a Glue source that opted into Redshift execution
    (sourceType=DATABASE, queryEngine=REDSHIFT)."""
    reg = AsyncMock()
    record = {
        "sourceId": source_id,
        "sourceType": "DATABASE",
        "sourceSubType": "GLUE_DATABASE",
        "queryEngine": "REDSHIFT",
        "queryable": queryable,
        "redshiftWorkgroup": "my-wg",
        "athenaDatabase": "insurance_lake",
    }
    reg.get_source.return_value = record
    reg.find_sole_database_source.return_value = record
    return reg


@pytest.mark.unit
class TestRedshiftDispatch:
    """Glue sources with queryEngine=REDSHIFT route to the Redshift executor."""

    async def test_redshift_glue_source_routes_redshift(self):
        athena = _make_executor("athena")
        redshift = _make_executor("redshift")
        comp = CompositeQueryExecutor(
            athena_executor=athena,
            redshift_executor=redshift,
            sources_registry=_redshift_glue_registry(),
        )
        result = await comp.execute(
            "SELECT id FROM awsdatacatalog.insurance_lake.claims",
            namespace="ns",
            data_source_id="src-redshift",
        )
        redshift.execute.assert_awaited_once()
        athena.execute.assert_not_awaited()
        assert result.rows[0]["engine"] == "redshift"

    async def test_redshift_sole_source_no_id_routes_redshift(self):
        """No explicit data_source_id: the namespace's SOLE Glue source with
        queryEngine=REDSHIFT is resolved via find_sole_database_source → Redshift
        (mirrors the JDBC sole-source path)."""
        athena = _make_executor("athena")
        redshift = _make_executor("redshift")
        comp = CompositeQueryExecutor(
            athena_executor=athena,
            redshift_executor=redshift,
            sources_registry=_redshift_glue_registry(),
        )
        await comp.execute(
            "SELECT id FROM awsdatacatalog.insurance_lake.claims",
            namespace="ns",
            data_source_id="",
        )
        redshift.execute.assert_awaited_once()
        athena.execute.assert_not_awaited()

    async def test_no_redshift_executor_falls_back_to_athena(self):
        """Without a Redshift executor wired, a REDSHIFT-engine Glue source → Athena
        (deployed behaviour never regresses)."""
        athena = _make_executor("athena")
        comp = CompositeQueryExecutor(
            athena_executor=athena,
            redshift_executor=None,
            sources_registry=_redshift_glue_registry(),
        )
        await comp.execute(
            "SELECT id FROM awsdatacatalog.insurance_lake.claims",
            namespace="ns",
            data_source_id="src-redshift",
        )
        athena.execute.assert_awaited_once()

    async def test_athena_glue_source_never_routes_redshift(self):
        """A normal ATHENA Glue source is untouched by the Redshift path."""
        athena = _make_executor("athena")
        redshift = _make_executor("redshift")
        comp = CompositeQueryExecutor(
            athena_executor=athena,
            redshift_executor=redshift,
            sources_registry=_glue_registry(),
        )
        await comp.execute("SELECT id FROM mydb.public.orders", namespace="ns", data_source_id="mydb")
        athena.execute.assert_awaited_once()
        redshift.execute.assert_not_awaited()

    async def test_redshift_not_yet_queryable_falls_back_to_athena(self):
        athena = _make_executor("athena")
        redshift = _make_executor("redshift")
        comp = CompositeQueryExecutor(
            athena_executor=athena,
            redshift_executor=redshift,
            sources_registry=_redshift_glue_registry(queryable=False),
        )
        await comp.execute(
            "SELECT id FROM awsdatacatalog.insurance_lake.claims",
            namespace="ns",
            data_source_id="src-redshift",
        )
        athena.execute.assert_awaited_once()
        redshift.execute.assert_not_awaited()

    async def test_redshift_registry_error_fails_safe_to_athena(self):
        athena = _make_executor("athena")
        redshift = _make_executor("redshift")
        reg = AsyncMock()
        reg.get_source.side_effect = RuntimeError("ddb throttled")
        comp = CompositeQueryExecutor(athena_executor=athena, redshift_executor=redshift, sources_registry=reg)
        await comp.execute(
            "SELECT id FROM awsdatacatalog.insurance_lake.claims",
            namespace="ns",
            data_source_id="src-redshift",
        )
        athena.execute.assert_awaited_once()
        redshift.execute.assert_not_awaited()

    async def test_cross_catalog_sql_never_routes_redshift(self):
        """Qualified cross-catalog SQL must go to Athena, not Redshift.

        Redshift reaches Glue via Spectrum (``awsdatacatalog`` only) and cannot
        address an Athena federated connector catalog at all. The sole DATABASE
        source can still be Redshift-routed while the query's OTHER source is
        Glue/S3 (not a DATABASE source), so the sole-source resolution succeeds
        and only the catalog count can stop the misroute.
        """
        athena = _make_executor("athena")
        redshift = _make_executor("redshift")
        comp = CompositeQueryExecutor(
            athena_executor=athena,
            redshift_executor=redshift,
            sources_registry=_redshift_glue_registry(),
        )
        await comp.execute(
            'SELECT a.id FROM "awsdatacatalog"."insurance_lake"."claims" a '
            'JOIN "pg_cat"."public"."policies" b ON a.id = b.claim_id',
            namespace="ns",
            data_source_id="",
        )
        athena.execute.assert_awaited_once()
        redshift.execute.assert_not_awaited()

    async def test_jdbc_still_wins_over_redshift_for_jdbc_source(self):
        """A JDBC source routes to JDBC; the Redshift arm only handles REDSHIFT sources."""
        athena = _make_executor("athena")
        jdbc = _make_executor("jdbc")
        redshift = _make_executor("redshift")
        comp = CompositeQueryExecutor(
            athena_executor=athena,
            source_db_executor=jdbc,
            redshift_executor=redshift,
            sources_registry=_jdbc_registry(),
        )
        await comp.execute("SELECT id FROM mydb.public.orders", namespace="ns", data_source_id="mydb")
        jdbc.execute.assert_awaited_once()
        redshift.execute.assert_not_awaited()
        athena.execute.assert_not_awaited()


@pytest.mark.unit
class TestResolveTargetDialect:
    """Tests for resolve_target_dialect (used by NL→SQL to generate in target dialect)."""

    async def test_explicit_postgresql_source_returns_postgres(self):
        """When a PostgreSQL source ID is provided, returns 'postgres'."""
        comp = CompositeQueryExecutor(
            athena_executor=_make_executor("athena"),
            sources_registry=_jdbc_registry(engine="POSTGRESQL"),
        )
        dialect = await comp.resolve_target_dialect("ns", data_source_id="mydb")
        assert dialect == "postgres"

    async def test_explicit_redshift_source_returns_redshift(self):
        """When a Redshift source ID is provided, returns 'redshift'."""
        comp = CompositeQueryExecutor(
            athena_executor=_make_executor("athena"),
            sources_registry=_jdbc_registry(engine="REDSHIFT"),
        )
        dialect = await comp.resolve_target_dialect("ns", data_source_id="mydb")
        assert dialect == "redshift"

    async def test_sole_jdbc_source_returns_its_dialect(self):
        """When no source ID provided, but namespace has a sole JDBC source, returns its dialect."""
        reg = _jdbc_registry(engine="POSTGRESQL")
        reg.find_sole_database_source = AsyncMock(return_value=reg.get_source.return_value)
        comp = CompositeQueryExecutor(
            athena_executor=_make_executor("athena"),
            sources_registry=reg,
        )
        dialect = await comp.resolve_target_dialect("ns", data_source_id="")
        assert dialect == "postgres"

    async def test_no_sources_returns_athena(self):
        """When sources registry is not configured, returns 'athena'."""
        comp = CompositeQueryExecutor(
            athena_executor=_make_executor("athena"),
        )
        dialect = await comp.resolve_target_dialect("ns", data_source_id="")
        assert dialect == "athena"

    async def test_multiple_sources_returns_athena(self):
        """When namespace has multiple sources, returns 'athena' for cross-source queries."""
        reg = _jdbc_registry()
        reg.find_sole_database_source = AsyncMock(return_value=None)  # multiple sources
        comp = CompositeQueryExecutor(
            athena_executor=_make_executor("athena"),
            sources_registry=reg,
        )
        dialect = await comp.resolve_target_dialect("ns", data_source_id="")
        assert dialect == "athena"

    async def test_glue_source_returns_athena(self):
        """Glue sources (queryEngine=ATHENA) return 'athena'."""
        comp = CompositeQueryExecutor(
            athena_executor=_make_executor("athena"),
            sources_registry=_glue_registry(),
        )
        dialect = await comp.resolve_target_dialect("ns", data_source_id="glue-src")
        assert dialect == "athena"

    async def test_two_catalog_sql_not_dispatched_to_redshift(self):
        """A genuinely cross-CATALOG statement (>=2 distinct three-part
        catalogs) must NOT go to Redshift even with a Redshift source wired —
        Redshift cannot address a federated Athena catalog, so it must fall through
        to Athena. Guards the len(catalogs) < 2 condition at the redshift branch."""
        athena = _make_executor("athena")
        redshift = _make_executor("redshift")
        comp = CompositeQueryExecutor(
            athena_executor=athena,
            redshift_executor=redshift,
            sources_registry=_redshift_glue_registry(),
        )
        await comp.execute(
            'SELECT a.id FROM "pg_cat"."public"."orders" a JOIN "awsdatacatalog"."crm"."customers" b ON a.cid = b.cid',
            namespace="ns",
            data_source_id="src-redshift",
        )
        athena.execute.assert_awaited_once()
        redshift.execute.assert_not_awaited()

    async def test_bare_mixed_with_catalog_not_dispatched_to_redshift(self):
        """Code review finding: a bare table mixed with a catalog-qualified one is
        ambiguous — the bare ref could belong to a different source — so it must
        fall through to Athena, not run on Redshift against a guessed catalog. This
        mirrors _select_jdbc_candidate and closes the redshift-guard asymmetry (the
        old guard checked only len(catalogs) < 2 and ignored the bare mix)."""
        athena = _make_executor("athena")
        redshift = _make_executor("redshift")
        comp = CompositeQueryExecutor(
            athena_executor=athena,
            redshift_executor=redshift,
            sources_registry=_redshift_glue_registry(),
        )
        await comp.execute(
            'SELECT a.id FROM "pg_cat"."public"."orders" a JOIN customers b ON a.cid = b.cid',
            namespace="ns",
            data_source_id="src-redshift",
        )
        athena.execute.assert_awaited_once()
        redshift.execute.assert_not_awaited()


@pytest.mark.unit
class TestCatalogAnalysis:
    """Directly exercise CompositeQueryExecutor._catalog_analysis — the routing
    predicate the code review corrected: only a THREE-part name carries a
    catalog; a two-part schema.table is neither a catalog nor bare."""

    def _comp(self):
        return CompositeQueryExecutor(athena_executor=_make_executor("athena"))

    def test_three_part_counts_the_catalog(self):
        catalogs, bare = self._comp()._catalog_analysis('SELECT * FROM "cat"."sch"."t"')
        assert catalogs == {"cat"} and bare is False

    def test_two_part_is_neither_catalog_nor_bare(self):
        catalogs, bare = self._comp()._catalog_analysis('SELECT * FROM "sch"."t"')
        assert catalogs == set() and bare is False

    def test_one_part_is_bare(self):
        catalogs, bare = self._comp()._catalog_analysis("SELECT * FROM t")
        assert catalogs == set() and bare is True

    def test_two_distinct_three_part_catalogs(self):
        catalogs, bare = self._comp()._catalog_analysis(
            'SELECT a.id FROM "c1"."s"."t" a JOIN "c2"."s"."u" b ON a.id = b.id'
        )
        assert catalogs == {"c1", "c2"} and bare is False

    def test_two_part_join_stays_single_source(self):
        """Two schemas of one source (2-part each) must NOT look like two catalogs."""
        catalogs, bare = self._comp()._catalog_analysis(
            'SELECT a.id FROM "sales"."orders" a JOIN "crm"."customers" b ON a.cid = b.cid'
        )
        assert catalogs == set() and bare is False

    def test_bare_mixed_with_qualified(self):
        catalogs, bare = self._comp()._catalog_analysis('SELECT a.id FROM "cat"."s"."t" a JOIN u b ON a.id = b.id')
        assert catalogs == {"cat"} and bare is True
