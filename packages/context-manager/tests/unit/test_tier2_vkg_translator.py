# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Tier 2 VKG translator (orchestration layer)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_common.constants import RESOURCE_PREFIX
from coa_serve.clients.base import QueryResult as ExecQueryResult
from coa_serve.clients.vkg import SparqlProjection, VKGClient, VKGResult, VKGTranslationError
from coa_serve.tier2.ontop.vkg_translator import (
    Tier2Status,
    Tier2Step,
    VKGTranslator,
)
from coa_serve.tier2.sql_firewall import FirewallResult, SQLFirewall

_TEST_VKG_ENDPOINT = "http://vkg.test-services.local:8080"


def _make_vkg_client(sql="SELECT 1", tables=None, error=None, projection=None):
    client = AsyncMock(spec=VKGClient)
    if error:
        client.translate.side_effect = error
    else:
        client.translate.return_value = VKGResult(
            sql=sql,
            dialect="postgresql",
            ontology_version="v2.1",
            source_table_refs=tables or ["catalog.schema.orders"],
            projection=projection,
        )
    return client


def _make_translator(vkg_client=None, firewall=None, executor=None, sources_registry=None):
    """Create a VKGTranslator with a mocked VKG client seeded for test namespaces."""
    translator = VKGTranslator(
        vkg_endpoint=_TEST_VKG_ENDPOINT,
        firewall=firewall or _make_firewall(),
        query_executor=executor or _make_executor(),
        sources_registry=sources_registry,
    )
    if vkg_client:
        translator._clients["demo"] = vkg_client
        translator._clients["target-ns-123"] = vkg_client
    return translator


def _make_firewall(denied=False, reason=None):
    fw = MagicMock(spec=SQLFirewall)
    fw.evaluate.return_value = FirewallResult(
        denied=denied,
        authorized_sql="SELECT 1" if not denied else "",
        reason=reason,
    )
    return fw


def _make_executor(rows=None, error=None, engine=""):
    executor = AsyncMock()
    if error:
        executor.execute.side_effect = error
    else:
        effective_rows = rows or [{"id": 1, "amount": 100}]
        # Derive columns from the rows, as the real executors do — hardcoding
        # them lets a test assert a projection over columns that the result
        # set does not actually contain.
        executor.execute.return_value = ExecQueryResult(
            rows=effective_rows,
            columns=list(effective_rows[0].keys()) if effective_rows else [],
            row_count=len(effective_rows),
            truncated=False,
            duration_ms=30,
            engine=engine,
        )
    return executor


@pytest.mark.unit
class TestVKGTranslatorSuccess:
    async def test_full_pipeline_success(self):
        vkg = _make_vkg_client(sql="SELECT id, amount FROM orders")
        fw = _make_firewall()
        executor = _make_executor()

        translator = _make_translator(vkg_client=vkg, firewall=fw, executor=executor)
        result = await translator.resolve("SELECT ?o WHERE { ?o a :Order }", namespace="demo")

        assert result.error is None
        assert result.query_result is not None
        assert result.query_result.rows == [{"id": 1, "amount": 100}]
        assert result.vkg_result is not None
        assert result.vkg_result.sql == "SELECT id, amount FROM orders"
        assert len(result.trace_steps) == 4  # vkg_translate + authorize + firewall + execute

    async def test_execute_trace_step_records_engine(self):
        """The ontop QUERY_EXECUTE trace step must carry detail.engine (athena |
        redshift | jdbc). Without it the integration-level Redshift-dispatch guard
        cannot verify which engine ran on the ontop path — it silently no-ops. The
        nl_to_sql path already records engine; this keeps the two strategies in
        step."""
        vkg = _make_vkg_client(sql="SELECT id, amount FROM orders")
        fw = _make_firewall()
        executor = _make_executor(engine="athena")

        translator = _make_translator(vkg_client=vkg, firewall=fw, executor=executor)
        result = await translator.resolve("SELECT ?o WHERE { ?o a :Order }", namespace="demo")

        exec_steps = [s for s in result.trace_steps if s.step == Tier2Step.QUERY_EXECUTE]
        assert exec_steps, "no QUERY_EXECUTE trace step recorded"
        detail = exec_steps[0].detail or {}
        assert detail.get("engine") == "athena", f"execute step must record detail.engine; got {detail!r}"

    async def test_trace_steps_recorded(self):
        translator = _make_translator(
            vkg_client=_make_vkg_client(),
            firewall=_make_firewall(),
            executor=_make_executor(),
        )
        result = await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        step_names = [s.step for s in result.trace_steps]
        assert Tier2Step.VKG_COMPILE in step_names
        assert Tier2Step.VKG_AUTHORIZE in step_names
        assert Tier2Step.SQL_FIREWALL in step_names
        assert Tier2Step.QUERY_EXECUTE in step_names

        for step in result.trace_steps:
            assert step.status == Tier2Status.OK
            assert step.duration_ms >= 0


@pytest.mark.unit
class TestVKGTranslatorErrors:
    async def test_vkg_translation_error(self):
        vkg = _make_vkg_client(error=VKGTranslationError("VKG returned 500"))
        translator = _make_translator(
            vkg_client=vkg,
            firewall=_make_firewall(),
            executor=_make_executor(),
        )
        result = await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        assert result.error == "vkg_translation_error"
        assert result.query_result is None
        assert len(result.trace_steps) == 1
        assert result.trace_steps[0].status == Tier2Status.ERROR

    async def test_firewall_denied(self):
        translator = _make_translator(
            vkg_client=_make_vkg_client(),
            firewall=_make_firewall(denied=True, reason="Table not allowed"),
            executor=_make_executor(),
        )
        result = await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        assert result.error is not None
        assert "Firewall denied" in result.error
        assert result.query_result is None
        assert result.vkg_result is not None
        # Executor should not have been called
        assert len(result.trace_steps) == 3  # vkg_translate + authorize + firewall

    async def test_firewall_denied_is_terminal_before_datasource_resolution(self):
        # Rebase guard: a firewall DENY must stay
        # terminal BEFORE Kun's _resolve_data_source/40cd2b8 runs. Even when VKG
        # returns datasource_routing (which would drive resolution on the allowed
        # path), a deny must short-circuit so the executor is never reached.
        vkg = AsyncMock(spec=VKGClient)
        vkg.translate.return_value = VKGResult(
            sql="SELECT 1",
            dialect="postgresql",
            ontology_version="v2.1",
            source_table_refs=["restricted.public.orders"],
            datasource_routing={"orders": {"datasourceId": "pg_main"}},
        )
        executor = _make_executor()
        translator = _make_translator(
            vkg_client=vkg,
            firewall=_make_firewall(denied=True, reason="Table not allowed"),
            executor=executor,
        )
        result = await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        assert result.error is not None and "Firewall denied" in result.error
        assert result.firewall_result is not None and result.firewall_result.denied
        # The whole point: resolution/execution never ran on a deny.
        executor.execute.assert_not_awaited()
        assert len(result.trace_steps) == 3  # vkg_translate + authorize + firewall only

    async def test_execution_error(self):
        translator = _make_translator(
            vkg_client=_make_vkg_client(),
            firewall=_make_firewall(),
            executor=_make_executor(error=RuntimeError("DB connection failed")),
        )
        result = await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        assert result.error == "query_execution_error"
        assert result.query_result is None
        assert result.vkg_result is not None
        assert result.firewall_result is not None
        assert len(result.trace_steps) == 4  # vkg_translate + authorize + firewall + execute
        assert result.trace_steps[3].status == Tier2Status.ERROR

    async def test_firewall_error_exception(self):
        fw = MagicMock(spec=SQLFirewall)
        fw.evaluate.side_effect = RuntimeError("Firewall crash")

        translator = _make_translator(
            vkg_client=_make_vkg_client(),
            firewall=fw,
            executor=_make_executor(),
        )
        result = await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        assert result.error == "firewall_error"
        assert result.query_result is None


@pytest.mark.unit
class TestVKGTranslatorDataSourceInference:
    async def test_single_catalog_inferred(self):
        vkg = _make_vkg_client(tables=["mydb.public.orders", "mydb.public.customers"])
        executor = _make_executor()

        translator = _make_translator(vkg_client=vkg, firewall=_make_firewall(), executor=executor)
        await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        # Check that executor was called with inferred data_source_id
        executor.execute.assert_called_once()
        call_kwargs = executor.execute.call_args.kwargs
        assert call_kwargs["data_source_id"] == "mydb"

    async def test_multiple_catalogs_returns_empty(self):
        vkg = _make_vkg_client(tables=["db1.schema.t1", "db2.schema.t2"])
        executor = _make_executor()

        translator = _make_translator(vkg_client=vkg, firewall=_make_firewall(), executor=executor)
        await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        call_kwargs = executor.execute.call_args.kwargs
        assert call_kwargs["data_source_id"] == ""

    async def test_explicit_data_source_overrides_inference(self):
        vkg = _make_vkg_client(tables=["mydb.public.orders"])
        executor = _make_executor()

        translator = _make_translator(vkg_client=vkg, firewall=_make_firewall(), executor=executor)
        await translator.resolve(
            "SELECT ?s WHERE { ?s ?p ?o }",
            namespace="demo",
            data_source_id="override-ds",
        )

        call_kwargs = executor.execute.call_args.kwargs
        assert call_kwargs["data_source_id"] == "override-ds"

    async def test_datasource_routing_used_over_prefix_heuristic(self):
        """datasourceRouting provides deterministic resolution over prefix parsing."""
        vkg_client = AsyncMock(spec=VKGClient)
        vkg_client.translate.return_value = VKGResult(
            sql="SELECT 1",
            dialect="trino",
            ontology_version="v1",
            source_table_refs=["BIRD_PUBLIC_INCOME"],
            datasource_routing={"BIRD_PUBLIC_INCOME": {"datasourceId": "ds-abc-123", "sourceSchema": "public"}},
        )
        executor = _make_executor()

        translator = _make_translator(vkg_client=vkg_client, firewall=_make_firewall(), executor=executor)
        await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        call_kwargs = executor.execute.call_args.kwargs
        assert call_kwargs["data_source_id"] == "ds-abc-123"

    async def test_default_data_source_id_triggers_inference(self):
        """Passing "default" as data_source_id should not short-circuit inference."""
        vkg_client = AsyncMock(spec=VKGClient)
        vkg_client.translate.return_value = VKGResult(
            sql="SELECT 1",
            dialect="trino",
            ontology_version="v1",
            source_table_refs=["loan"],
            datasource_routing={"loan": {"datasourceId": "ds-from-routing", "sourceSchema": "public"}},
        )
        executor = _make_executor()

        translator = _make_translator(vkg_client=vkg_client, firewall=_make_firewall(), executor=executor)
        await translator.resolve(
            "SELECT ?s WHERE { ?s ?p ?o }",
            namespace="demo",
            data_source_id="default",
        )

        call_kwargs = executor.execute.call_args.kwargs
        assert call_kwargs["data_source_id"] == "ds-from-routing"

    async def test_empty_data_source_id_triggers_inference(self):
        """Empty string data_source_id should trigger inference."""
        vkg_client = AsyncMock(spec=VKGClient)
        vkg_client.translate.return_value = VKGResult(
            sql="SELECT 1",
            dialect="trino",
            ontology_version="v1",
            source_table_refs=["account"],
            datasource_routing={"account": {"datasourceId": "ds-inferred", "sourceSchema": "public"}},
        )
        executor = _make_executor()

        translator = _make_translator(vkg_client=vkg_client, firewall=_make_firewall(), executor=executor)
        await translator.resolve(
            "SELECT ?s WHERE { ?s ?p ?o }",
            namespace="demo",
            data_source_id="",
        )

        call_kwargs = executor.execute.call_args.kwargs
        assert call_kwargs["data_source_id"] == "ds-inferred"

    async def test_datasource_routing_multiple_sources_returns_empty(self):
        """Multiple distinct datasourceIds in routing → empty (cross-source)."""
        vkg_client = AsyncMock(spec=VKGClient)
        vkg_client.translate.return_value = VKGResult(
            sql="SELECT 1",
            dialect="trino",
            ontology_version="v1",
            source_table_refs=["T1", "T2"],
            datasource_routing={
                "T1": {"datasourceId": "ds-1", "sourceSchema": "public"},
                "T2": {"datasourceId": "ds-2", "sourceSchema": "analytics"},
            },
        )
        executor = _make_executor()

        translator = _make_translator(vkg_client=vkg_client, firewall=_make_firewall(), executor=executor)
        await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        call_kwargs = executor.execute.call_args.kwargs
        assert call_kwargs["data_source_id"] == ""

    def test_infer_data_source_empty_means_cross_source_audit(self):
        # (c) DISPATCHER characterization, explicit audit:
        # `_infer_data_source` returning "" is the SINGLE-vs-CROSS-source signal.
        # - a single resolvable catalog/datasource id -> that id; the injected
        # CompositeQueryExecutor may then route it to direct JDBC (if the
        # has_jdbc_endpoint gate passes) or Athena
        # - "" -> cross-source / ambiguous -> always Athena federation
        translator = _make_translator()
        single = VKGResult(sql="SELECT 1", dialect="trino", ontology_version="v1", source_table_refs=["mydb.s.t"])
        multi = VKGResult(
            sql="SELECT 1", dialect="trino", ontology_version="v1", source_table_refs=["db1.s.t", "db2.s.t"]
        )
        # single resolvable catalog -> the catalog id (single-source signal)
        assert translator._infer_data_source(single) == "mydb"
        # multiple/ambiguous catalogs -> "" (cross-source signal)
        assert translator._infer_data_source(multi) == ""


@pytest.mark.unit
class TestVKGTranslatorCrossSourceQualification:
    """Cross-source SQL must be catalog-qualified before execution.

    Bare table names execute against Athena's single pinned (Catalog, Database)
    pair, so every table outside that pair fails TABLE_NOT_FOUND. The translator
    rewrites such SQL to catalog.schema.table AFTER authorization.
    """

    _SQL = "SELECT a.id FROM claims a JOIN policies b ON a.id = b.claim_id"

    def _firewall(self):
        fw = MagicMock(spec=SQLFirewall)
        fw.evaluate.return_value = FirewallResult(denied=False, authorized_sql=self._SQL, reason=None)
        return fw

    def _vkg(self, routing):
        client = AsyncMock(spec=VKGClient)
        client.translate.return_value = VKGResult(
            sql=self._SQL,
            dialect="trino",
            ontology_version="v1",
            source_table_refs=list(routing),
            datasource_routing=routing,
        )
        return client

    def _registry(self, sources):
        reg = AsyncMock()
        reg.get_source.side_effect = lambda namespace, data_source_id: sources.get(data_source_id, {})
        return reg

    async def test_firewall_sees_the_unqualified_sql(self):
        """The rewrite MUST run downstream of authorization.

        The firewall normalises table names to their bare component for the
        allowlist/denylist, so prefixes do not affect those — but Cedar receives
        ``SQLFirewall.extract_tables()`` output verbatim. Qualifying first would
        silently change the strings a table-scoped policy matches on, which is an
        authorization change disguised as a formatting change. Asserting the
        firewall's actual argument makes the ordering executable rather than a
        comment.
        """
        routing = {
            "claims": {"datasourceId": "ds-pg", "sourceSchema": "public"},
            "policies": {"datasourceId": "ds-glue", "sourceSchema": "insurance"},
        }
        fw = self._firewall()
        executor = _make_executor()
        translator = _make_translator(
            vkg_client=self._vkg(routing),
            firewall=fw,
            executor=executor,
            sources_registry=self._registry(
                {
                    "ds-pg": {"athenaDataCatalogName": "pg_cat"},
                    "ds-glue": {"glueDatabaseName": "insurance"},
                }
            ),
        )
        await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        assert fw.evaluate.call_args.args[0] == self._SQL
        # ...and the executor got the rewritten form, so the two really did differ.
        assert executor.execute.call_args.args[0] != self._SQL

    async def test_cross_source_sql_is_qualified_before_execute(self):
        routing = {
            "claims": {"datasourceId": "ds-pg", "sourceSchema": "public"},
            "policies": {"datasourceId": "ds-glue", "sourceSchema": "insurance"},
        }
        executor = _make_executor()
        translator = _make_translator(
            vkg_client=self._vkg(routing),
            firewall=self._firewall(),
            executor=executor,
            sources_registry=self._registry(
                {
                    "ds-pg": {"athenaDataCatalogName": "pg_cat"},
                    "ds-glue": {"glueDatabaseName": "insurance"},
                }
            ),
        )
        await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        executed = executor.execute.call_args.args[0].replace('"', "")
        assert "pg_cat.public.claims" in executed
        assert "awsdatacatalog.insurance.policies" in executed
        # Cross-source → no single source pinned; the qualified names route it.
        assert executor.execute.call_args.kwargs["data_source_id"] == ""

    async def test_reported_executed_sql_is_the_statement_the_executor_ran(self):
        """``queryUsed`` must show the statement that RAN, not Ontop's translate output.

        These diverge exactly when cross-source qualification applies. Reporting the raw form hands the
        operator SQL whose bare names resolve under no ``QueryExecutionContext`` — it
        cannot be copied into the Athena console, and it reads as evidence the fix
        never fired. Asserting against ``executor.execute``'s own argument (rather than
        a literal) keeps the two from drifting apart again.
        """
        routing = {
            "claims": {"datasourceId": "ds-pg", "sourceSchema": "public"},
            "policies": {"datasourceId": "ds-glue", "sourceSchema": "insurance"},
        }
        executor = _make_executor()
        translator = _make_translator(
            vkg_client=self._vkg(routing),
            firewall=self._firewall(),
            executor=executor,
            sources_registry=self._registry(
                {
                    "ds-pg": {"athenaDataCatalogName": "pg_cat"},
                    "ds-glue": {"glueDatabaseName": "insurance"},
                }
            ),
        )
        result = await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        assert result.executed_sql == executor.execute.call_args.args[0]
        assert result.executed_sql != self._SQL, "executed_sql still holds the un-qualified translate output"

    async def test_single_source_sql_reaches_executor_verbatim(self):
        """No regression on the path that already works."""
        routing = {
            "claims": {"datasourceId": "ds-pg", "sourceSchema": "public"},
            "policies": {"datasourceId": "ds-pg", "sourceSchema": "public"},
        }
        executor = _make_executor()
        translator = _make_translator(
            vkg_client=self._vkg(routing),
            firewall=self._firewall(),
            executor=executor,
            sources_registry=self._registry({"ds-pg": {"athenaDataCatalogName": "pg_cat"}}),
        )
        await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        assert executor.execute.call_args.args[0] == self._SQL
        assert executor.execute.call_args.kwargs["data_source_id"] == "ds-pg"

    async def test_single_source_synthetic_schema_is_restored_before_execute(self):
        """A SINGLE-source query over a shared table name still needs a rewrite.

        When two datasources both expose ``public.customers``, the inducer mints a
        synthetic schema token for the mapping and the H2 validation schema so the
        two logical tables stay distinct. That token names nothing in the real
        database. A one-table query never spans sources, so it never reaches the
        cross-source qualifier — restoration therefore runs unconditionally, or the
        mapping change breaks every single-source query over the shared table.
        """
        routing = {"public__2f77729f.customers": {"datasourceId": "ds-pg", "sourceSchema": "public"}}
        translated = 'SELECT id FROM "public__2f77729f"."customers"'
        fw = MagicMock(spec=SQLFirewall)
        fw.evaluate.return_value = FirewallResult(denied=False, authorized_sql=translated, reason=None)
        executor = _make_executor()
        translator = _make_translator(
            vkg_client=self._vkg(routing),
            firewall=fw,
            executor=executor,
            sources_registry=self._registry({"ds-pg": {"athenaDataCatalogName": "pg_cat"}}),
        )
        await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        executed = executor.execute.call_args.args[0]
        assert '"public"."customers"' in executed
        assert "public__2f77729f" not in executed
        # Single-source: still pinned to its own source, no catalog added.
        assert executor.execute.call_args.kwargs["data_source_id"] == "ds-pg"

    async def test_colliding_table_name_fails_loudly_without_executing(self):
        """A BARE reference to a name two sources own cannot be attributed — never guess.

        The SQL must actually name the ambiguous table: ambiguity is judged per
        reference, not per routing map, so a query that happens to avoid the shared
        name is still qualified normally.
        """
        routing = {
            "public.customers": {"datasourceId": "ds-pg", "sourceSchema": "public"},
            "crm.customers": {"datasourceId": "ds-glue", "sourceSchema": "crm"},
        }
        fw = MagicMock(spec=SQLFirewall)
        fw.evaluate.return_value = FirewallResult(denied=False, authorized_sql="SELECT 1 FROM customers", reason=None)
        executor = _make_executor()
        translator = _make_translator(
            vkg_client=self._vkg(routing),
            firewall=fw,
            executor=executor,
            sources_registry=self._registry(
                {
                    "ds-pg": {"athenaDataCatalogName": "pg_cat"},
                    "ds-glue": {"glueDatabaseName": "crm"},
                }
            ),
        )
        result = await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        assert result.error == "query_qualification_error"
        executor.execute.assert_not_called()

    async def test_colliding_table_name_resolves_when_the_sql_carries_the_schema(self):
        """Same two-source collision, but each reference says which schema it means.

        This is the shape a re-induced namespace produces (the mapping emits
        ``"schema"."table"`` for a shared name), and it must execute rather than
        raise — otherwise the fix would make every such query fail.
        """
        routing = {
            "public.customers": {"datasourceId": "ds-pg", "sourceSchema": "public"},
            "crm.customers": {"datasourceId": "ds-glue", "sourceSchema": "crm"},
        }
        sql = "SELECT a.id FROM public.customers a JOIN crm.customers b ON a.id = b.id"
        fw = MagicMock(spec=SQLFirewall)
        fw.evaluate.return_value = FirewallResult(denied=False, authorized_sql=sql, reason=None)
        executor = _make_executor()
        translator = _make_translator(
            vkg_client=self._vkg(routing),
            firewall=fw,
            executor=executor,
            sources_registry=self._registry(
                {
                    "ds-pg": {"athenaDataCatalogName": "pg_cat"},
                    "ds-glue": {"glueDatabaseName": "crm"},
                }
            ),
        )
        result = await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        assert result.error is None
        executed = executor.execute.call_args.args[0].replace('"', "")
        assert "pg_cat.public.customers" in executed
        assert "awsdatacatalog.crm.customers" in executed

    async def test_no_registry_fails_closed_without_executing(self):
        """Detected cross-source SQL without registry metadata must not execute bare."""
        routing = {
            "claims": {"datasourceId": "ds-pg", "sourceSchema": "public"},
            "policies": {"datasourceId": "ds-glue", "sourceSchema": "insurance"},
        }
        executor = _make_executor()
        translator = _make_translator(vkg_client=self._vkg(routing), firewall=self._firewall(), executor=executor)
        result = await translator.resolve("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

        assert result.error == "query_qualification_error"
        executor.execute.assert_not_awaited()


@pytest.mark.unit
class TestVKGTranslatorDynamicRouting:
    """Tests for per-namespace VKG client resolution via endpoint URL rewriting."""

    async def test_resolves_namespace_to_per_namespace_endpoint(self):
        translator = VKGTranslator(
            vkg_endpoint=f"http://vkg.{RESOURCE_PREFIX}-dev-services.local:8080",
            firewall=_make_firewall(),
            query_executor=_make_executor(),
        )
        client = translator._resolve_client("ns-abc")
        assert client._base_url == f"http://vkg-ns-abc.{RESOURCE_PREFIX}-dev-services.local:8080"

    async def test_different_namespaces_resolve_different_endpoints(self):
        translator = VKGTranslator(
            vkg_endpoint=f"http://vkg.{RESOURCE_PREFIX}-dev-services.local:8080",
            firewall=_make_firewall(),
            query_executor=_make_executor(),
        )
        client_a = translator._resolve_client("ns-a")
        client_b = translator._resolve_client("ns-b")
        assert client_a is not client_b
        assert "vkg-ns-a" in client_a._base_url
        assert "vkg-ns-b" in client_b._base_url

    async def test_caches_client_per_namespace(self):
        translator = VKGTranslator(
            vkg_endpoint=f"http://vkg.{RESOURCE_PREFIX}-dev-services.local:8080",
            firewall=_make_firewall(),
            query_executor=_make_executor(),
        )
        client_1 = translator._resolve_client("ns-a")
        client_2 = translator._resolve_client("ns-a")
        assert client_1 is client_2

    async def test_invalid_endpoint_raises(self):
        translator = VKGTranslator(
            vkg_endpoint="http://localhost:8080",
            firewall=_make_firewall(),
            query_executor=_make_executor(),
        )
        with pytest.raises(ValueError, match="does not contain"):
            translator._resolve_client("ns-abc")

    async def test_resolve_routes_to_namespace_endpoint(self):
        """Full resolve() call uses the per-namespace VKG client."""
        mock_client = _make_vkg_client(sql="SELECT ns_specific")
        translator = _make_translator(vkg_client=mock_client)
        result = await translator.resolve(
            "SELECT ?s WHERE { ?s ?p ?o }",
            namespace="target-ns-123",
        )
        mock_client.translate.assert_called_once()
        assert result.vkg_result.sql == "SELECT ns_specific"


@pytest.mark.unit
class TestSparqlProjection:
    """Tests for _project_to_sparql — mapping SQL columns to SPARQL variables."""

    async def test_project_to_sparql_renames_columns(self):
        """Projection renames SQL aliases to SPARQL variable names."""
        projection = SparqlProjection(
            select_vars=["employeeName"],
            var_to_column={"employeeName": "full_name1m54"},
            distinct=False,
        )
        raw_result = ExecQueryResult(
            rows=[
                {"full_name1m54": "Mel Brooks", "id1m52": "nm0000316", "title1m52": "Spaceballs"},
                {"full_name1m54": "Bill Pullman", "id1m52": "nm0000597", "title1m52": "Spaceballs"},
            ],
            columns=["full_name1m54", "id1m52", "title1m52"],
            row_count=2,
            truncated=False,
        )
        translator = _make_translator()
        projected = translator._project_to_sparql(raw_result, projection)
        assert projected.columns == ["employeeName"]
        assert projected.rows == [{"employeeName": "Mel Brooks"}, {"employeeName": "Bill Pullman"}]
        assert projected.row_count == 2

    async def test_project_to_sparql_preserves_duplicates_when_not_distinct(self):
        """Plain SELECT keeps duplicate rows (bag semantics)."""
        projection = SparqlProjection(
            select_vars=["name"],
            var_to_column={"name": "name_col"},
            distinct=False,
        )
        raw_result = ExecQueryResult(
            rows=[{"name_col": "Mel Brooks"}, {"name_col": "Mel Brooks"}, {"name_col": "Bill Pullman"}],
            columns=["name_col"],
            row_count=3,
            truncated=False,
        )
        translator = _make_translator()
        projected = translator._project_to_sparql(raw_result, projection)
        assert len(projected.rows) == 3
        assert projected.rows[0] == {"name": "Mel Brooks"}
        assert projected.rows[1] == {"name": "Mel Brooks"}

    async def test_project_to_sparql_deduplicates_when_distinct(self):
        """SELECT DISTINCT collapses duplicate rows."""
        projection = SparqlProjection(
            select_vars=["name"],
            var_to_column={"name": "name_col"},
            distinct=True,
        )
        raw_result = ExecQueryResult(
            rows=[{"name_col": "Mel Brooks"}, {"name_col": "Mel Brooks"}, {"name_col": "Bill Pullman"}],
            columns=["name_col"],
            row_count=3,
            truncated=False,
        )
        translator = _make_translator()
        projected = translator._project_to_sparql(raw_result, projection)
        assert len(projected.rows) == 2
        assert {"name": "Mel Brooks"} in projected.rows
        assert {"name": "Bill Pullman"} in projected.rows

    async def test_project_to_sparql_multi_var(self):
        """Multiple SELECT variables are projected in order."""
        projection = SparqlProjection(
            select_vars=["name", "age"],
            var_to_column={"name": "n_alias", "age": "a_alias"},
            distinct=False,
        )
        raw_result = ExecQueryResult(
            rows=[{"n_alias": "Alice", "a_alias": 30, "extra_col": "ignored"}],
            columns=["n_alias", "a_alias", "extra_col"],
            row_count=1,
            truncated=False,
        )
        translator = _make_translator()
        projected = translator._project_to_sparql(raw_result, projection)
        assert projected.columns == ["name", "age"]
        assert projected.rows == [{"name": "Alice", "age": 30}]

    async def test_resolve_applies_projection_when_present(self):
        """End-to-end: resolve() projects results if VKG returns projection metadata."""
        projection = SparqlProjection(
            select_vars=["employeeName"],
            var_to_column={"employeeName": "full_name1m54"},
            distinct=False,
        )
        vkg = _make_vkg_client(sql="SELECT ...", projection=projection)
        # Executor returns raw SQL columns
        executor = _make_executor(
            rows=[
                {"full_name1m54": "Mel Brooks", "id1m52": "nm0000316"},
            ]
        )
        translator = _make_translator(vkg_client=vkg, executor=executor)
        result = await translator.resolve("SELECT ?employeeName WHERE {...}", namespace="demo")
        assert result.error is None
        assert result.query_result.columns == ["employeeName"]
        assert result.query_result.rows == [{"employeeName": "Mel Brooks"}]

    async def test_resolve_skips_projection_when_absent(self):
        """Fallback: if VKG returns no projection, raw SQL columns are kept."""
        vkg = _make_vkg_client(sql="SELECT id FROM orders", projection=None)
        executor = _make_executor(rows=[{"id": 123, "amount": 999}])
        translator = _make_translator(vkg_client=vkg, executor=executor)
        result = await translator.resolve("SELECT ?o WHERE {...}", namespace="demo")
        assert result.error is None
        # No projection → raw SQL columns
        assert result.query_result.columns == ["id", "amount"]
        assert result.query_result.rows == [{"id": 123, "amount": 999}]

    async def test_project_to_sparql_returns_raw_when_alias_missing(self):
        """An alias absent from the result degrades to the raw result.

        Reading a missing key would yield an all-None column under a
        correct-looking header, which is worse than showing SQL aliases.
        """
        projection = SparqlProjection(
            select_vars=["employeeName"],
            var_to_column={"employeeName": "not_in_result"},
            distinct=False,
        )
        raw_result = ExecQueryResult(
            rows=[{"full_name1m3": "Mel Brooks"}],
            columns=["full_name1m3"],
            row_count=1,
            truncated=False,
        )
        translator = _make_translator()
        projected = translator._project_to_sparql(raw_result, projection)
        assert projected is raw_result

    async def test_project_to_sparql_matches_alias_case_insensitively(self):
        """Drivers that upper/lower-case column labels still project correctly."""
        projection = SparqlProjection(
            select_vars=["employeeName"],
            var_to_column={"employeeName": "FULL_NAME1m3"},
            distinct=False,
        )
        raw_result = ExecQueryResult(
            rows=[{"full_name1m3": "Mel Brooks"}],
            columns=["full_name1m3"],
            row_count=1,
            truncated=False,
        )
        translator = _make_translator()
        projected = translator._project_to_sparql(raw_result, projection)
        assert projected.columns == ["employeeName"]
        assert projected.rows == [{"employeeName": "Mel Brooks"}]

    async def test_project_to_sparql_reversed_case_mismatch_also_matches(self):
        """The fallback works in both directions: lower alias, upper columns."""
        projection = SparqlProjection(
            select_vars=["employeeName"],
            var_to_column={"employeeName": "full_name1m3"},
            distinct=False,
        )
        raw_result = ExecQueryResult(
            rows=[{"FULL_NAME1M3": "Mel Brooks"}],
            columns=["FULL_NAME1M3"],
            row_count=1,
            truncated=False,
        )
        translator = _make_translator()
        projected = translator._project_to_sparql(raw_result, projection)
        assert projected.columns == ["employeeName"]
        assert projected.rows == [{"employeeName": "Mel Brooks"}]

    async def test_project_to_sparql_prefers_exact_alias_over_case_folded(self):
        """Exact match wins when columns differ only by case.

        ``by_lower`` keeps one entry per lower-cased label, so a case-folded
        lookup alone could resolve "NAME" to "name" and read the wrong value —
        a correct-looking header over wrong data, which is the failure class
        this projection exists to prevent. Pins the branch ordering.
        """
        projection = SparqlProjection(
            select_vars=["v"],
            var_to_column={"v": "NAME"},
            distinct=False,
        )
        raw_result = ExecQueryResult(
            rows=[{"NAME": "right-one", "name": "wrong-one"}],
            columns=["NAME", "name"],
            row_count=1,
            truncated=False,
        )
        translator = _make_translator()
        projected = translator._project_to_sparql(raw_result, projection)
        assert projected.rows == [{"v": "right-one"}]
