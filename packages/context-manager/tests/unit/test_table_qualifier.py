# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for cross-source catalog qualification.

The defect: SQL joining tables from two data sources executes with BARE table
names against Athena's single pinned ``(Catalog, Database)`` context, so every
table outside the pinned pair fails with TABLE_NOT_FOUND. Verified live against
Athena — bare names fail, ``catalog.schema.table`` succeeds regardless of the
context. These tests pin the rewrite that produces the qualified form, and the
cases where it must deliberately do NOTHING.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import sqlglot
from coa_serve.tier2.table_qualifier import (
    AMBIGUOUS_KEY,
    QualificationError,
    ambiguous_reference,
    bare_names_shared_across_datasources,
    catalog_for_source,
    distinct_datasources,
    distinct_source_targets,
    is_fully_catalog_qualified,
    is_fully_catalog_qualified_ast,
    prepare_execution_sql,
    qualify_cross_source_sql,
    real_tables,
    restore_source_schemas,
    schema_for_source,
)

_CROSS_SOURCE_SQL = 'SELECT a.id, b.total FROM "claims" a JOIN "policies" b ON a.id = b.claim_id'

# A Glue-native source record: no federated catalog, so its tables live in the
# account's AwsDataCatalog.
_GLUE = {"glueDatabaseName": "insurance"}

# A bare "claims" plus a schema-qualified table in a second source.
_AMBIGUOUS_BARE_SQL = "SELECT 1 FROM claims JOIN sales.orders ON TRUE"


def _unquoted(sql: str) -> str:
    """Drop identifier quotes so an assertion reads like the logical name.

    The rewrite quotes the catalog and schema it ADDS (hyphens are legal in an
    Athena catalog name) but never re-quotes the table name the firewall
    authorized, so the emitted SQL is legitimately mixed-quoting.

    Only for assertions where quoting is not what is under test —
    ``TestIdentifierQuoting`` asserts the literal quoted text, because a helper
    that strips quotes cannot fail when the quotes go missing.
    """
    return sql.replace('"', "")


def _registry(sources: dict[str, dict]):
    """Registry stub resolving data_source_id -> record."""
    reg = AsyncMock()
    reg.get_source.side_effect = lambda namespace, data_source_id: sources.get(data_source_id, {})
    return reg


def _routing(*pairs: tuple[str, str, str]) -> dict[str, dict[str, str]]:
    """Build a datasourceRouting map from (table_ref, datasourceId, sourceSchema)."""
    return {ref: {"datasourceId": ds, "sourceSchema": schema} for ref, ds, schema in pairs}


@pytest.mark.unit
class TestDistinctDatasources:
    def test_ignores_blank_ids(self):
        routing = {"a": {"datasourceId": "src-1"}, "b": {"datasourceId": ""}, "c": {}}
        assert distinct_datasources(routing) == {"src-1"}

    def test_counts_each_source_once(self):
        routing = _routing(("t1", "src-1", "s"), ("t2", "src-1", "s"), ("t3", "src-2", "s"))
        assert distinct_datasources(routing) == {"src-1", "src-2"}


@pytest.mark.unit
class TestDistinctSourceTargets:
    def test_one_source_one_schema_is_one_target(self):
        routing = _routing(("t1", "src-1", "public"), ("t2", "src-1", "public"))
        assert distinct_source_targets(routing) == {("src-1", "public")}

    def test_one_source_two_schemas_is_two_targets(self):
        # The "adjacent limitation": a single source spanning two schemas
        # still needs qualification, and distinct_datasources cannot see it.
        routing = _routing(("orders", "src-1", "sales"), ("customers", "src-1", "analytics"))
        assert distinct_source_targets(routing) == {("src-1", "sales"), ("src-1", "analytics")}
        assert distinct_datasources(routing) == {"src-1"}

    def test_ignores_blank_ids(self):
        routing = {"a": {"datasourceId": "src-1", "sourceSchema": "s"}, "b": {"datasourceId": ""}, "c": {}}
        assert distinct_source_targets(routing) == {("src-1", "s")}

    def test_missing_schema_collapses_per_source(self):
        # NL->SQL entries carry no sourceSchema, so a single source stays one
        # target and that path keeps its datasource-count trigger.
        routing = {"orders": {"datasourceId": "src-1"}, "customers": {"datasourceId": "src-1"}}
        assert distinct_source_targets(routing) == {("src-1", "")}


@pytest.mark.unit
class TestBareNamesSharedAcrossDatasources:
    def test_empty_when_no_name_repeats(self):
        routing = _routing(("orders", "src-1", "sales"), ("customers", "src-2", "crm"))
        assert bare_names_shared_across_datasources(routing) == set()

    def test_name_owned_by_two_datasources_is_shared(self):
        # The same bare name in two DATASOURCES (keyed by schema-qualified refs) —
        # a caller pinned to one context cannot resolve the bare name correctly, so
        # it needs the catalog.
        routing = _routing(("db_a.customers", "src-a", "db_a"), ("db_b.customers", "src-b", "db_b"))
        assert bare_names_shared_across_datasources(routing) == {"customers"}

    def test_two_schemas_of_one_source_is_not_shared(self):
        # One datasource spanning two schemas: the schema separates them, so a
        # two-part schema.table is enough — NOT a cross-datasource share.
        routing = _routing(("sales.orders", "src-1", "sales"), ("analytics.orders", "src-1", "analytics"))
        assert bare_names_shared_across_datasources(routing) == set()

    def test_ambiguity_marker_contributes_no_owner(self):
        routing = {
            "db_a.customers": {"datasourceId": "src-a", "sourceSchema": "db_a"},
            "customers": {AMBIGUOUS_KEY: "DB_A.CUSTOMERS,DB_B.CUSTOMERS"},
        }
        # Only one real datasource owner here (the marker has no datasourceId), so
        # the bare name is not counted as shared from this map alone.
        assert bare_names_shared_across_datasources(routing) == set()


@pytest.mark.unit
class TestSingleSourceSharedNameQualifiesCatalog:
    """A single-source query over a name ALSO owned by another datasource must be
    3-part catalog-qualified, so a context pinned to the other same-named source
    cannot resolve the bare name against the wrong catalog. This is the ontop
    same-name case: NL->SPARQL picks ONE class, the SQL references one table, but
    the routing (with the sibling surfaced) shows the name spans datasources."""

    async def test_single_referenced_source_shared_name_gets_catalog(self):
        # SQL references only db_a.customers, but the routing carries db_b's sibling
        # too (as the VKG translator now surfaces it for a shared bare name).
        routing = _routing(("db_a.customers", "src-a", "db_a"), ("db_b.customers", "src-b", "db_b"))
        reg = _registry({"src-a": _GLUE, "src-b": _GLUE})
        sql = 'SELECT COUNT(*) FROM "db_a"."customers"'
        out = await qualify_cross_source_sql(sql, routing, reg, namespace="ns")
        # 3-part: catalog present even though only ONE source is referenced.
        assert "awsdatacatalog.db_a.customers" in _unquoted(out)
        # Only the referenced source is read from the registry.
        assert reg.get_source.await_count == 1

    async def test_single_source_unique_name_stays_untouched(self):
        # Control: the SAME shape but a name NOT shared across datasources is left
        # bare — the single-source fast path must not regress.
        routing = _routing(("db_a.orders", "src-a", "db_a"))
        reg = _registry({"src-a": _GLUE})
        sql = 'SELECT COUNT(*) FROM "db_a"."orders"'
        out = await qualify_cross_source_sql(sql, routing, reg, namespace="ns")
        assert out == sql
        reg.get_source.assert_not_awaited()

    def test_federated_catalog_preferred(self):
        assert catalog_for_source({"athenaDataCatalogName": "pg_cat", "athenaCatalog": "x"}) == "pg_cat"

    def test_glue_source_defaults_to_awsdatacatalog(self):
        assert catalog_for_source({"glueDatabaseName": "insurance"}) == "awsdatacatalog"

    def test_federated_schema_from_discovered(self):
        source = {"athenaDataCatalogName": "pg_cat", "discoveredSchemas": ["public"]}
        assert schema_for_source(source) == "public"

    def test_federated_schema_empty_when_no_discovered_schemas(self):
        """A federated source with no discoveredSchemas returns '' so the qualifier
        ABANDONS (leaves SQL bare) rather than inventing 'public'. sql_namespace_scope
        builds the federated authorized set only from discoveredSchemas, so an
        invented schema is guaranteed out of scope — and because the rewrite makes
        the statement qualified, that would DENY it rather than soft-fail. Returning
        '' takes the documented qualification_incomplete_no_schema abandon path."""
        assert schema_for_source({"athenaDataCatalogName": "pg_cat"}) == ""

    def test_glue_native_ignores_discovered_schemas(self):
        """Parity with athena.py, which reads discoveredSchemas ONLY when federated.

        A Glue-native record that happens to carry discoveredSchemas must resolve
        to its database, not to the discovered schema: the executor pins the
        database, and a qualifier that named the schema instead would address a
        different place than the single-source path does for the same record.
        """
        source = {"discoveredSchemas": ["public"], "glueDatabaseName": "insurance"}
        assert schema_for_source(source) == "insurance"

    def test_schema_falls_back_to_glue_database(self):
        assert schema_for_source({"glueDatabaseName": "insurance"}) == "insurance"

    def test_multi_schema_federated_source_logs_the_choice(self):
        """A per-source fallback cannot know which schema a bare hit came from."""
        source = {"athenaDataCatalogName": "pg_cat", "discoveredSchemas": ["public", "restricted"]}
        with patch("coa_serve.tier2.table_qualifier.logger") as log:
            assert schema_for_source(source) == "public"
        assert log.warning.call_args.args[0] == "qualification_schema_ambiguous_for_source"


@pytest.mark.unit
class TestQualifyCrossSourceSQL:
    async def test_single_source_left_untouched(self):
        """The working single-source path must not change at all."""
        routing = _routing(("claims", "src-1", "public"), ("policies", "src-1", "public"))
        reg = _registry({"src-1": {"athenaDataCatalogName": "pg_cat"}})
        out = await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")
        assert out == _CROSS_SOURCE_SQL
        reg.get_source.assert_not_awaited()

    async def test_cross_source_qualifies_each_table_to_its_own_catalog(self):
        """The fix: bare names become catalog.schema.table, per source."""
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-glue", "insurance"))
        reg = _registry(
            {
                "src-pg": {"athenaDataCatalogName": "pg_cat"},
                "src-glue": {"glueDatabaseName": "insurance"},
            }
        )
        out = await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")
        assert "pg_cat.public.claims" in _unquoted(out)
        assert "awsdatacatalog.insurance.policies" in _unquoted(out)

    async def test_federated_no_discovered_schemas_fails_closed_not_public(self):
        """A missing federated schema must error, never invent ``public`` or run bare."""
        routing = _routing(("claims", "src-pg", ""), ("policies", "src-glue", "insurance"))
        reg = _registry(
            {
                "src-pg": {"athenaDataCatalogName": "pg_cat"},  # no discoveredSchemas
                "src-glue": {"glueDatabaseName": "insurance"},
            }
        )
        with pytest.raises(QualificationError, match="claims.*source schema"):
            await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")

    async def test_render_preserves_table_multiset(self):
        """F7: make the all-or-nothing invariant executable. A cross-source rewrite
        of a statement with a CTE, a cast and a subquery must preserve the real
        (non-CTE) table set — the guard re-parses the rendered output and abandons
        (returns input unchanged) if the count changes. Here it must NOT false-trip:
        both real tables are qualified and the count is preserved."""
        sql = (
            "WITH recent AS (SELECT id FROM claims) "
            "SELECT c.id, CAST(p.premium AS DOUBLE) "
            "FROM claims c JOIN policies p ON c.pid = p.id "
            "WHERE c.id IN (SELECT id FROM recent)"
        )
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-glue", "insurance"))
        reg = _registry(
            {
                "src-pg": {"athenaDataCatalogName": "pg_cat", "discoveredSchemas": ["public"]},
                "src-glue": {"glueDatabaseName": "insurance"},
            }
        )
        out = await qualify_cross_source_sql(sql, routing, reg, namespace="ns")
        # Not abandoned (both real tables qualified), and the CTE ref is untouched.
        assert "pg_cat.public.claims" in _unquoted(out)
        assert "awsdatacatalog.insurance.policies" in _unquoted(out)
        # The CTE name "recent" must not have been qualified (it is not a real table).
        assert "recent" in _unquoted(out)

    async def test_single_source_two_schemas_qualifies_schema_only(self):
        """Adjacent limitation: one source, two schemas → SCHEMA-qualified.

        Both tables share a datasource, so distinct_datasources sees one source and
        the old gate skipped the rewrite; the context then pinned discoveredSchemas[0]
        and the other schema's table was TABLE_NOT_FOUND. Each table must carry its
        own schema — but NOT the catalog: a single catalog is supplied by the
        context/connection, and prepending the Athena catalog would break the
        direct-JDBC route.
        """
        routing = _routing(("claims", "src-1", "sales"), ("policies", "src-1", "analytics"))
        reg = _registry({"src-1": {"athenaDataCatalogName": "pg_cat"}})
        out = await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")
        assert "sales.claims" in _unquoted(out)
        assert "analytics.policies" in _unquoted(out)
        # Two-part, not three: the federated catalog name must not appear.
        assert "pg_cat" not in out
        # One source, so a single registry read despite two distinct targets.
        assert reg.get_source.await_count == 1

    async def test_single_source_single_schema_not_qualified(self):
        """A genuinely single-target query is still returned byte-for-byte."""
        routing = _routing(("claims", "src-1", "public"), ("policies", "src-1", "public"))
        reg = _registry({"src-1": {"athenaDataCatalogName": "pg_cat"}})
        out = await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")
        assert out == _CROSS_SOURCE_SQL
        reg.get_source.assert_not_awaited()

    async def test_one_registry_read_per_distinct_source(self):
        """Registry lookups scale with SOURCES, not with table references."""
        routing = _routing(
            ("claims", "src-pg", "public"),
            ("claim_lines", "src-pg", "public"),
            ("policies", "src-glue", "insurance"),
        )
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}, "src-glue": _GLUE})
        sql = "SELECT 1 FROM claims JOIN claim_lines ON TRUE JOIN policies ON TRUE"
        await qualify_cross_source_sql(sql, routing, reg, namespace="ns")
        assert reg.get_source.await_count == 2

    async def test_reads_only_the_sources_the_statement_references(self):
        """A namespace-wide routing map must not cost a read per unused source."""
        routing = _routing(
            ("claims", "src-pg", "public"),
            ("policies", "src-glue", "insurance"),
            ("unused", "src-third", "other"),
        )
        reg = _registry(
            {
                "src-pg": {"athenaDataCatalogName": "pg_cat"},
                "src-glue": _GLUE,
                "src-third": _GLUE,
            }
        )
        await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")
        assert {c.args[1] for c in reg.get_source.await_args_list} == {"src-pg", "src-glue"}

    async def test_unrouted_table_fails_before_registry_reads(self):
        """Attribution fails closed before any registry I/O."""
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-glue", "insurance"))
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}, "src-glue": _GLUE})
        sql = "SELECT 1 FROM claims JOIN policies ON TRUE JOIN mystery ON TRUE"
        with pytest.raises(QualificationError, match="mystery.*routing metadata"):
            await qualify_cross_source_sql(sql, routing, reg, namespace="ns")
        reg.get_source.assert_not_awaited()

    async def test_qualified_routing_key_matches_schema_dot_table(self):
        """Routing keyed 'schema.table' resolves for SQL that carries the schema."""
        routing = _routing(("public.claims", "src-pg", "public"), ("insurance.policies", "src-glue", "insurance"))
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}, "src-glue": _GLUE})
        sql = "SELECT 1 FROM public.claims JOIN insurance.policies ON TRUE"
        out = await qualify_cross_source_sql(sql, routing, reg, namespace="ns")
        assert "pg_cat.public.claims" in _unquoted(out)
        assert "awsdatacatalog.insurance.policies" in _unquoted(out)

    async def test_missing_per_table_schema_falls_back_to_source_schema(self):
        """NL->SQL routing has no sourceSchema — the per-source schema fills in."""
        routing = {"claims": {"datasourceId": "src-pg"}, "policies": {"datasourceId": "src-glue"}}
        reg = _registry(
            {
                "src-pg": {"athenaDataCatalogName": "pg_cat", "discoveredSchemas": ["public"]},
                "src-glue": {"glueDatabaseName": "insurance"},
            }
        )
        out = await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")
        assert "pg_cat.public.claims" in _unquoted(out)
        assert "awsdatacatalog.insurance.policies" in _unquoted(out)

    async def test_unresolvable_schema_fails_closed(self):
        """No schema anywhere means the cross-source statement must not execute."""
        routing = {"claims": {"datasourceId": "src-pg"}, "policies": {"datasourceId": "src-glue"}}
        reg = _registry({"src-pg": {"queryable": True}, "src-glue": {"queryable": True}})
        with pytest.raises(QualificationError, match="source schema"):
            await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")

    async def test_unrouted_table_rejects_whole_rewrite(self):
        """An unattributable table rejects the statement rather than running it bare."""
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-glue", "insurance"))
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}, "src-glue": _GLUE})
        sql = "SELECT 1 FROM claims JOIN policies ON TRUE JOIN mystery ON TRUE"
        with pytest.raises(QualificationError, match="mystery.*routing metadata"):
            await qualify_cross_source_sql(sql, routing, reg, namespace="ns")

    async def test_no_registry_fails_closed(self):
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-glue", "insurance"))
        with pytest.raises(QualificationError, match="sources registry"):
            await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, None, namespace="ns")

    async def test_missing_source_record_fails_closed(self):
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-gone", "insurance"))
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}})
        with pytest.raises(QualificationError, match="src-gone.*not found"):
            await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")

    async def test_non_queryable_source_leaves_sql_unchanged(self):
        """A cross-source query must not become a way around the queryable gate.

        The single-source path refuses to resolve a catalog for a source whose
        Lake Formation grant did not land (``athena.py`` logs ``source_not_queryable``
        and falls back to the default database). Qualifying such a source's tables
        would send the query straight at it instead.
        """
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-blocked", "insurance"))
        reg = _registry(
            {
                "src-pg": {"athenaDataCatalogName": "pg_cat"},
                "src-blocked": {"glueDatabaseName": "insurance", "queryable": False},
            }
        )
        assert await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns") == _CROSS_SOURCE_SQL

    async def test_malformed_catalog_name_fails_closed(self):
        """A malformed catalog name must not fall through to bare execution."""
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-glue", "insurance"))
        reg = _registry(
            {
                "src-pg": {"athenaDataCatalogName": 'pg"; DROP TABLE x --'},
                "src-glue": _GLUE,
            }
        )
        with pytest.raises(QualificationError, match="invalid catalog or schema"):
            await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")

    async def test_statement_with_only_ctes_is_not_reported_as_qualified(self):
        """No real table references → no rewrite, and no 'qualified' log line.

        Claiming a rewrite that did not happen would let the executor skip catalog
        resolution for SQL that still needs it.
        """
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-glue", "insurance"))
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}, "src-glue": _GLUE})
        sql = "WITH r AS (SELECT 1 AS id) SELECT id FROM r"
        with patch("coa_serve.tier2.table_qualifier.logger") as log:
            assert await qualify_cross_source_sql(sql, routing, reg, namespace="ns") == sql
        assert "cross_source_sql_qualified" not in [c.args[0] for c in log.info.call_args_list]

    async def test_same_table_name_in_two_sources_raises(self):
        """A colliding bare name cannot be attributed — fail loud, never guess.

        Upstream (R2RML ``rr:tableName``, the VKG routing map and the H2 schema)
        all key on the bare name, so the collision has already lost information
        by the time SQL arrives. Executing anything here risks reading the wrong
        physical table.
        """
        routing = _routing(("public.customers", "src-pg", "public"), ("crm.customers", "src-glue", "crm"))
        reg = _registry({"src-pg": _GLUE, "src-glue": _GLUE})
        with pytest.raises(QualificationError, match="customers"):
            await qualify_cross_source_sql("SELECT 1 FROM customers", routing, reg, namespace="ns")

    async def test_two_schemas_of_one_source_colliding_on_a_bare_name_raises(self):
        """Ambiguity is per (source, schema), not per source.

        ONE datasource exposing ``public.claims`` and ``restricted.claims`` gives a
        bare ``claims`` two different physical targets. Measuring ambiguity on the
        datasource id alone found only one id here, registered the bare name, and
        silently attributed the reference to whichever schema indexed first — a
        wrong-table read inside a single source, which is exactly the outcome the
        collision check exists to prevent.
        """
        routing = _routing(
            ("public.claims", "src-pg", "public"),
            ("restricted.claims", "src-pg", "restricted"),
            ("sales.orders", "src-glue", "sales"),
        )
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}, "src-glue": _GLUE})
        with pytest.raises(QualificationError, match="claims"):
            await qualify_cross_source_sql(_AMBIGUOUS_BARE_SQL, routing, reg, namespace="ns")

    async def test_duplicate_refs_to_the_same_target_are_not_ambiguous(self):
        """Two keys that would qualify identically must not trip the check.

        ``datasourceRouting`` legitimately carries both a bare and a qualified key
        for one table. Flagging that as a collision would fail queries that have no
        ambiguity at all.
        """
        routing = _routing(
            ("public.claims", "src-pg", "public"),
            ("claims", "src-pg", "public"),
            ("sales.orders", "src-glue", "sales"),
        )
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}, "src-glue": _GLUE})
        out = await qualify_cross_source_sql(_AMBIGUOUS_BARE_SQL, routing, reg, namespace="ns")
        assert "pg_cat.public.claims" in _unquoted(out)

    async def test_colliding_name_resolves_when_sql_carries_the_schema(self):
        """The Phase-2 payoff: a re-induced mapping emits "schema"."table", so the
        SAME collision that raises above is attributable and executes.

        This is why the collision check is per-REFERENCE and not a precondition on
        the routing map: after this fix a shared table name legitimately appears twice
        in ``datasourceRouting``, once per schema.
        """
        routing = _routing(("public.customers", "src-pg", "public"), ("crm.customers", "src-glue", "crm"))
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}, "src-glue": _GLUE})
        sql = "SELECT 1 FROM public.customers a JOIN crm.customers b ON a.id = b.id"
        out = _unquoted(await qualify_cross_source_sql(sql, routing, reg, namespace="ns"))
        assert "pg_cat.public.customers" in out
        assert "awsdatacatalog.crm.customers" in out

    async def test_cte_name_is_not_qualified(self):
        """A CTE is not a physical table; qualifying it would break the query."""
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-glue", "insurance"))
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}, "src-glue": _GLUE})
        sql = "WITH recent AS (SELECT 1 AS id FROM claims) SELECT 1 FROM recent JOIN policies ON TRUE"
        out = await qualify_cross_source_sql(sql, routing, reg, namespace="ns")
        assert "pg_cat.public.claims" in _unquoted(out)
        assert "awsdatacatalog.insurance.policies" in _unquoted(out)
        assert "FROM recent" in out

    async def test_cte_named_after_the_table_it_wraps_does_not_shadow_it(self):
        """CTE detection is scope-correct, not name-only.

        ``WITH customers AS (SELECT * FROM crm.customers)`` makes the bare outer
        ``customers`` the CTE, while the schema-qualified inner reference is still
        the real table. Excluding every reference whose NAME matched a CTE alias
        skipped the real ``crm.customers``, producing exactly the partial rewrite
        the all-or-nothing contract forbids — and one that
        ``is_fully_catalog_qualified`` then certified as complete, so the executor
        skipped catalog resolution for a statement that still needed it.
        """
        routing = _routing(("crm.customers", "src-pg", "crm"), ("sales.orders", "src-glue", "sales"))
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}, "src-glue": _GLUE})
        sql = (
            "WITH customers AS (SELECT * FROM crm.customers) "
            "SELECT o.id FROM sales.orders o JOIN customers c ON o.cid = c.id"
        )
        out = await qualify_cross_source_sql(sql, routing, reg, namespace="ns")
        assert "pg_cat.crm.customers" in _unquoted(out)
        assert "awsdatacatalog.sales.orders" in _unquoted(out)
        assert "JOIN customers AS c" in out  # the CTE reference stays bare
        assert is_fully_catalog_qualified(out)

    async def test_parse_failure_fails_closed(self):
        """sqlglot is lenient, so the failure is forced rather than hoped for."""
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-glue", "insurance"))
        reg = _registry({"src-pg": _GLUE, "src-glue": _GLUE})
        with (
            patch(
                "coa_serve.tier2.table_qualifier.sqlglot.parse_one",
                side_effect=sqlglot.errors.ParseError("boom"),
            ),
            pytest.raises(QualificationError, match="could not be parsed"),
        ):
            await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")

    async def test_parse_failure_does_not_log_the_statement(self):
        """sqlglot error text embeds a window of the SQL, literals included."""
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-glue", "insurance"))
        reg = _registry({"src-pg": _GLUE, "src-glue": _GLUE})
        secret = "SELECT * FROM claims WHERE ssn = '123-45-6789'"
        with (
            patch(
                "coa_serve.tier2.table_qualifier.sqlglot.parse_one",
                side_effect=sqlglot.errors.ParseError(secret),
            ),
            patch("coa_serve.tier2.table_qualifier.logger") as log,
            pytest.raises(QualificationError),
        ):
            await qualify_cross_source_sql(secret, routing, reg, namespace="ns")
        assert log.warning.call_args.kwargs["error"] == "ParseError"
        assert "123-45-6789" not in str(log.warning.call_args)


@pytest.mark.unit
class TestIdentifierQuoting:
    """Quoting is asserted literally — a helper that strips quotes cannot fail."""

    async def test_hyphenated_catalog_is_quoted(self):
        """Connector catalogs provisioned at onboarding routinely carry hyphens.

        Trino will not parse ``pg-conn-cat.public.claims`` unquoted, so an
        unquoted rewrite produces a statement Athena rejects outright.
        """
        routing = _routing(("claims", "src-pg", "public"), ("policies", "src-glue", "insurance"))
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg-conn-cat"}, "src-glue": _GLUE})
        out = await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")
        assert '"pg-conn-cat"."public"' in out
        assert "pg-conn-cat.public" not in out

    async def test_hyphenated_schema_is_quoted(self):
        routing = _routing(("claims", "src-pg", "my-schema"), ("policies", "src-glue", "insurance"))
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}, "src-glue": _GLUE})
        out = await qualify_cross_source_sql(_CROSS_SOURCE_SQL, routing, reg, namespace="ns")
        assert '"my-schema"' in out


@pytest.mark.unit
class TestRestoreSourceSchemas:
    """The synthetic-schema undo. Runs for EVERY statement, single-source included."""

    # Two datasources exposing the same public.customers force the inducer to mint
    # a synthetic schema token for the mapping and the H2 validation schema.
    _SYNTHETIC = {
        "public__2f77729f.customers": {"datasourceId": "src-pg", "sourceSchema": "public"},
    }

    def test_synthetic_schema_is_replaced_with_the_real_one(self):
        sql = 'SELECT id FROM "public__2f77729f"."customers"'
        out = restore_source_schemas(sql, self._SYNTHETIC)
        assert '"public"."customers"' in out
        assert "public__2f77729f" not in out

    def test_runs_for_a_single_source_statement(self):
        """The crux: a one-table query never reaches the cross-source qualifier.

        Gating restoration on "spans sources" fixed the join and broke every
        single-source query over the same table — the synthetic token names no
        schema in the real database either way.
        """
        assert len(distinct_datasources(self._SYNTHETIC)) == 1
        out = restore_source_schemas('SELECT id FROM "public__2f77729f"."customers"', self._SYNTHETIC)
        assert '"public"."customers"' in out

    def test_real_schema_left_alone(self):
        routing = {"public.customers": {"datasourceId": "src-pg", "sourceSchema": "public"}}
        sql = "SELECT id FROM public.customers"
        assert restore_source_schemas(sql, routing) == sql

    def test_idempotent(self):
        once = restore_source_schemas('SELECT id FROM "public__2f77729f"."customers"', self._SYNTHETIC)
        assert restore_source_schemas(once, self._SYNTHETIC) == once

    def test_unrouted_table_left_alone(self):
        sql = 'SELECT id FROM "other"."things"'
        assert restore_source_schemas(sql, self._SYNTHETIC) == sql

    def test_bare_reference_left_alone(self):
        """A bare reference carries no schema to correct."""
        sql = "SELECT id FROM customers"
        assert restore_source_schemas(sql, self._SYNTHETIC) == sql

    def test_empty_routing_is_a_noop(self):
        sql = 'SELECT id FROM "public__2f77729f"."customers"'
        assert restore_source_schemas(sql, {}) == sql

    def test_malformed_real_schema_is_not_written_into_sql(self):
        routing = {"logical.customers": {"datasourceId": "src-pg", "sourceSchema": 'x"; DROP TABLE y --'}}
        sql = "SELECT id FROM logical.customers"
        assert restore_source_schemas(sql, routing) == sql

    def test_parse_failure_leaves_sql_unchanged(self):
        with patch(
            "coa_serve.tier2.table_qualifier.sqlglot.parse_one",
            side_effect=sqlglot.errors.ParseError("boom"),
        ):
            sql = 'SELECT id FROM "public__2f77729f"."customers"'
            assert restore_source_schemas(sql, self._SYNTHETIC) == sql


@pytest.mark.unit
class TestPrepareExecutionSQL:
    """The single entry point both Tier-2 strategies use."""

    async def test_restores_then_qualifies(self):
        routing = {
            "public__2f77729f.customers": {"datasourceId": "src-pg", "sourceSchema": "public"},
            "sales.orders": {"datasourceId": "src-glue", "sourceSchema": "sales"},
        }
        reg = _registry({"src-pg": {"athenaDataCatalogName": "pg_cat"}, "src-glue": _GLUE})
        sql = 'SELECT 1 FROM "public__2f77729f"."customers" JOIN sales.orders ON TRUE'
        prepared = await prepare_execution_sql(sql, routing, reg, namespace="ns")
        assert prepared.error is None
        assert "pg_cat.public.customers" in _unquoted(prepared.sql)
        assert "awsdatacatalog.sales.orders" in _unquoted(prepared.sql)

    async def test_ambiguity_reports_an_error_instead_of_raising(self):
        """Fail closed, and identically for both strategies.

        The two paths previously disagreed — one aborted the query, the other
        executed the unqualified statement — so the same collision produced a
        wrong-table risk on one path and an error on the other.
        """
        routing = _routing(("public.customers", "src-pg", "public"), ("crm.customers", "src-glue", "crm"))
        reg = _registry({"src-pg": _GLUE, "src-glue": _GLUE})
        prepared = await prepare_execution_sql("SELECT 1 FROM customers", routing, reg, namespace="ns")
        assert prepared.error is not None
        assert "customers" in prepared.error

    async def test_missing_routing_for_cross_source_table_fails_closed(self):
        """A cross-source query must not execute bare when one table is unrouted.

        The namespace has a customers table in each source. The translated query
        selects source A's customers and joins source B's payments table, but the
        routing entry for payments is missing. Returning the input SQL here lets
        Athena's default context resolve payments to any same-named local table and
        can turn the metadata defect into a wrong successful answer.
        """
        routing = _routing(
            ("a.customers", "source-a", "a"),
            ("b.customers", "source-b", "b"),
        )
        sql = "SELECT c.id FROM a.customers c JOIN payments p ON c.id = p.customer_id"
        reg = _registry(
            {
                "source-a": {"athenaDataCatalogName": "catalog-a"},
                "source-b": {"athenaDataCatalogName": "catalog-b"},
            }
        )

        prepared = await prepare_execution_sql(sql, routing, reg, namespace="ns")

        assert prepared.sql == sql
        assert prepared.error is not None
        assert "payments" in prepared.error
        reg.get_source.assert_not_awaited()

    async def test_single_source_statement_passes_through(self):
        routing = _routing(("claims", "src-1", "public"))
        reg = _registry({"src-1": {"athenaDataCatalogName": "pg_cat"}})
        prepared = await prepare_execution_sql("SELECT 1 FROM claims", routing, reg, namespace="ns")
        assert prepared == type(prepared)(sql="SELECT 1 FROM claims")

    async def test_two_sources_sharing_identical_real_schema_are_qualified_not_refused(self):
        """Code review finding: two datasources that expose the SAME real
        ``schema.table`` (both ``public.claims``) are distinguished only by the
        inducer's synthetic schema tokens (``public__aaa`` / ``public__bbb``) in
        the SQL. restore_source_schemas must NOT collapse both to ``public.claims``
        before the qualifier runs — doing so erased the tokens and forced a
        QualificationError on a query that IS attributable. The qualifier matches
        the synthetic keys, restores the real schema, AND attributes the catalog in
        one pass, so this must produce a fully 3-part statement, not an error.
        """
        routing = {
            "public__aaa.claims": {"datasourceId": "src-a", "sourceSchema": "public"},
            "public__bbb.claims": {"datasourceId": "src-b", "sourceSchema": "public"},
        }
        reg = _registry(
            {
                "src-a": {"athenaDataCatalogName": "cat_a"},
                "src-b": {"athenaDataCatalogName": "cat_b"},
            }
        )
        sql = 'SELECT a.id FROM "public__aaa"."claims" a JOIN "public__bbb"."claims" b ON a.id = b.id'
        prepared = await prepare_execution_sql(sql, routing, reg, namespace="ns")
        assert prepared.error is None, f"identical-schema two-source query was wrongly refused: {prepared.error}"
        out = _unquoted(prepared.sql)
        # Both refs fully 3-part, each to its OWN catalog, with the REAL schema restored.
        assert "cat_a.public.claims" in out, f"src-a ref not qualified to its catalog+real schema: {out}"
        assert "cat_b.public.claims" in out, f"src-b ref not qualified to its catalog+real schema: {out}"
        # The synthetic tokens must not survive into the executed SQL.
        assert "public__aaa" not in out and "public__bbb" not in out, f"synthetic token leaked: {out}"


@pytest.mark.unit
class TestAmbiguousReference:
    """The VKG side of the ambiguity contract: an entry that names its candidates
    instead of a datasource.

    Such an entry contributes NO ``datasourceId``, so ``distinct_datasources``
    undercounts and the cross-source gate never opens — the whole reason the check
    runs before, and independently of, that gate.
    """

    @staticmethod
    def _flagged() -> dict[str, dict[str, str]]:
        """``claims`` answers to two tables; ``sales.orders`` is unambiguous."""
        return {
            "claims": {AMBIGUOUS_KEY: "CRM.CLAIMS,PUBLIC.CLAIMS"},
            "public.claims": {"datasourceId": "src-pg", "sourceSchema": "public"},
            "crm.claims": {"datasourceId": "src-glue", "sourceSchema": "crm"},
            "sales.orders": {"datasourceId": "src-glue", "sourceSchema": "sales"},
        }

    def test_flagged_reference_in_the_sql_is_reported_with_its_candidates(self):
        message = ambiguous_reference("SELECT 1 FROM claims", self._flagged())
        assert message is not None
        assert "claims" in message
        assert "CRM.CLAIMS,PUBLIC.CLAIMS" in message

    def test_qualified_reference_to_the_same_table_is_not_ambiguous(self):
        """The marker sits under the BARE key only. SQL that carries the schema
        names one physical table and must keep executing."""
        assert ambiguous_reference("SELECT 1 FROM public.claims", self._flagged()) is None

    def test_flagged_table_the_sql_never_mentions_is_ignored(self):
        """The routing map describes the whole mapping, not just this statement."""
        assert ambiguous_reference("SELECT 1 FROM sales.orders", self._flagged()) is None

    def test_no_flagged_entries_skips_the_parse_entirely(self):
        """The common path must not pay for a parse it cannot learn anything from."""
        routing = _routing(("claims", "src-1", "public"))
        with patch("coa_serve.tier2.table_qualifier.sqlglot.parse_one") as parse_one:
            assert ambiguous_reference("SELECT 1 FROM claims", routing) is None
        parse_one.assert_not_called()

    def test_unparseable_sql_with_a_flagged_entry_is_not_cleared(self):
        """Failing to parse means failing to rule the ambiguity out, and the entry
        exists precisely because a wrong-table read is possible.

        The failure is FORCED, like the sibling parse-failure tests: sqlglot is
        lenient enough that a hand-written "broken" statement can start parsing on a
        version bump, and this assertion would then pass for the wrong reason.
        """
        with patch(
            "coa_serve.tier2.table_qualifier.sqlglot.parse_one",
            side_effect=sqlglot.errors.ParseError("boom"),
        ):
            message = ambiguous_reference("SELECT 1 FROM claims", self._flagged())
        assert message is not None
        assert "could not parse" in message

    async def test_prepare_execution_sql_refuses_without_rewriting(self):
        """End to end: the statement is returned untouched with ``error`` set, so a
        caller that ignores ``error`` still cannot execute a rewritten-looking
        query against a guessed catalog."""
        sql = "SELECT 1 FROM claims JOIN sales.orders ON TRUE"
        reg = _registry({"src-pg": _GLUE, "src-glue": _GLUE})
        prepared = await prepare_execution_sql(sql, self._flagged(), reg, namespace="ns")
        assert prepared.sql == sql
        assert prepared.error is not None
        assert "CRM.CLAIMS,PUBLIC.CLAIMS" in prepared.error
        reg.get_source.assert_not_awaited()


@pytest.mark.unit
class TestIsFullyCatalogQualified:
    def test_three_part_names_are_qualified(self):
        assert is_fully_catalog_qualified('SELECT 1 FROM "pg_cat"."public"."claims"')

    def test_bare_names_are_not(self):
        assert not is_fully_catalog_qualified("SELECT 1 FROM claims")

    def test_two_part_names_are_not(self):
        assert not is_fully_catalog_qualified("SELECT 1 FROM public.claims")

    def test_mixed_qualification_is_not(self):
        sql = 'SELECT 1 FROM "pg_cat"."public"."claims" JOIN policies ON TRUE'
        assert not is_fully_catalog_qualified(sql)

    def test_cte_reference_does_not_defeat_qualification(self):
        sql = 'WITH r AS (SELECT 1 AS id FROM "c"."s"."t") SELECT 1 FROM r'
        assert is_fully_catalog_qualified(sql)

    def test_cte_named_after_an_unqualified_table_is_not_certified(self):
        """The name-only CTE test made this return True, hiding a bare reference."""
        sql = "WITH customers AS (SELECT * FROM crm.customers) SELECT 1 FROM customers"
        assert not is_fully_catalog_qualified(sql)

    def test_no_tables_is_not_qualified(self):
        assert not is_fully_catalog_qualified("SELECT 1")

    def test_unparseable_is_not_qualified(self):
        with patch(
            "coa_serve.tier2.table_qualifier.sqlglot.parse_one",
            side_effect=sqlglot.errors.ParseError("boom"),
        ):
            assert not is_fully_catalog_qualified("SELECT 1 FROM claims")

    def test_ast_form_agrees_with_the_string_form(self):
        """The executor reuses its LIMIT-injection AST rather than re-parsing."""
        for sql in (
            'SELECT 1 FROM "pg_cat"."public"."claims"',
            "SELECT 1 FROM claims",
            "SELECT 1",
            "WITH customers AS (SELECT * FROM crm.customers) SELECT 1 FROM customers",
        ):
            parsed = sqlglot.parse_one(sql, dialect="trino")
            assert is_fully_catalog_qualified_ast(parsed) == is_fully_catalog_qualified(sql), sql


@pytest.mark.unit
class TestRealTables:
    def test_excludes_bare_cte_references_only(self):
        sql = "WITH customers AS (SELECT * FROM crm.customers) SELECT 1 FROM sales.orders JOIN customers ON TRUE"
        parsed = sqlglot.parse_one(sql, dialect="trino")
        names = sorted(f"{t.db}.{t.name}" for t in real_tables(parsed))
        assert names == ["crm.customers", "sales.orders"]
