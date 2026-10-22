# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SourcesRegistry."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from coa_serve.clients.sources_registry import _ITEMS_CACHE_MAX, SourcesRegistry
from coa_serve.tier2.nl_to_sql.sql_generator import sql_table_routing
from coa_serve.tier2.sql_firewall import SQLFirewall
from coa_serve.tier2.table_qualifier import AMBIGUOUS_SOURCE, prepare_execution_sql


@pytest.mark.unit
class TestSourcesRegistry:
    """Test source record lookup and database resolution."""

    def _make_registry(self, items=None):
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            registry = SourcesRegistry(table_name="test-sources", region="us-west-2")
            registry._mock_table = mock_table
            if items is not None:
                mock_table.get_item.return_value = {"Item": items}
        return registry

    @pytest.mark.asyncio
    async def test_resolve_database_from_configuration(self):
        registry = self._make_registry({"configuration": json.dumps({"databaseName": "bird_test_db_catalog"})})
        result = await registry.resolve_database_name("ns-123", "src-456")
        assert result == "bird_test_db_catalog"

    @pytest.mark.asyncio
    async def test_resolve_database_prefers_top_level_field(self):
        registry = self._make_registry(
            {
                "glueDatabaseName": "top_level_db",
                "configuration": json.dumps({"databaseName": "config_db"}),
            }
        )
        result = await registry.resolve_database_name("ns-123", "src-456")
        assert result == "top_level_db"

    @pytest.mark.asyncio
    async def test_resolve_database_uses_athena_catalog_name(self):
        registry = self._make_registry({"athenaDataCatalogName": "fed_catalog", "configuration": "{}"})
        result = await registry.resolve_database_name("ns-123", "src-456")
        assert result == "fed_catalog"

    @pytest.mark.asyncio
    async def test_resolve_database_returns_empty_when_not_found(self):
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.get_item.return_value = {"Item": None}
            mock_resource.return_value.Table.return_value = mock_table
            registry = SourcesRegistry(table_name="test-sources", region="us-west-2")
        result = await registry.resolve_database_name("ns-123", "src-missing")
        assert result == ""

    @pytest.mark.asyncio
    async def test_resolve_database_default_source_id_queries_namespace(self):
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.query.return_value = {
                "Items": [
                    {
                        "sourceType": "DATABASE",
                        "configuration": json.dumps({"databaseName": "found_db"}),
                    }
                ]
            }
            mock_resource.return_value.Table.return_value = mock_table
            registry = SourcesRegistry(table_name="test-sources", region="us-west-2")

        result = await registry.resolve_database_name("ns-123", "default")
        assert result == "found_db"
        mock_table.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolve_database_returns_empty_when_no_table_configured(self):
        registry = SourcesRegistry(table_name="", region="us-west-2")
        assert not registry.available
        result = await registry.resolve_database_name("ns-123", "src-456")
        assert result == ""

    @pytest.mark.asyncio
    async def test_get_source(self):
        registry = self._make_registry({"PK": "NS#ns-1", "SK": "SRC#s-1", "name": "test"})
        item = await registry.get_source("ns-1", "s-1")
        assert item["name"] == "test"

    def test_parse_configuration_string(self):
        item = {"configuration": '{"databaseName": "mydb", "host": "localhost"}'}
        config = SourcesRegistry.parse_configuration(item)
        assert config["databaseName"] == "mydb"
        assert config["host"] == "localhost"

    def test_parse_configuration_dict(self):
        item = {"configuration": {"databaseName": "mydb"}}
        config = SourcesRegistry.parse_configuration(item)
        assert config["databaseName"] == "mydb"

    def test_parse_configuration_missing(self):
        config = SourcesRegistry.parse_configuration({})
        assert config == {}

    def _make_registry_with_query(self, items):
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.query.return_value = {"Items": items}
            mock_resource.return_value.Table.return_value = mock_table
            registry = SourcesRegistry(table_name="test-sources", region="us-west-2")
            registry._mock_table = mock_table
        return registry

    @pytest.mark.asyncio
    async def test_find_sole_database_source_single(self):
        registry = self._make_registry_with_query(
            [
                {"sourceType": "DATABASE", "sourceId": "src-1", "queryEngine": "JDBC", "queryable": True},
            ]
        )
        result = await registry.find_sole_database_source("ns-1")
        assert result is not None
        assert result["sourceId"] == "src-1"

    @pytest.mark.asyncio
    async def test_find_sole_database_source_multiple_returns_none(self):
        registry = self._make_registry_with_query(
            [
                {"sourceType": "DATABASE", "sourceId": "src-1"},
                {"sourceType": "DATABASE", "sourceId": "src-2"},
            ]
        )
        result = await registry.find_sole_database_source("ns-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_find_sole_database_source_zero_returns_none(self):
        registry = self._make_registry_with_query(
            [
                {"sourceType": "S3", "sourceId": "src-1"},
            ]
        )
        result = await registry.find_sole_database_source("ns-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_database_source_count_zero(self):
        """No DATABASE source → 0 (caller must treat as 'cannot confirm single-source')."""
        registry = self._make_registry_with_query([{"sourceType": "S3", "sourceId": "s3-1"}])
        assert await registry.database_source_count("ns-1") == 0

    @pytest.mark.asyncio
    async def test_database_source_count_single(self):
        """Exactly one DATABASE source → 1 (the safe-to-pin single-source case)."""
        registry = self._make_registry_with_query(
            [
                {"sourceType": "DATABASE", "sourceId": "db-1"},
                {"sourceType": "S3", "sourceId": "s3-1"},
            ]
        )
        assert await registry.database_source_count("ns-1") == 1

    @pytest.mark.asyncio
    async def test_database_source_count_multiple(self):
        """Two or more DATABASE sources → the count (the ambiguous multi-source case)."""
        registry = self._make_registry_with_query(
            [
                {"sourceType": "DATABASE", "sourceId": "db-1"},
                {"sourceType": "DATABASE", "sourceId": "db-2"},
                {"sourceType": "DATABASE", "sourceId": "db-3"},
            ]
        )
        assert await registry.database_source_count("ns-1") == 3

    @pytest.mark.asyncio
    async def test_find_sole_database_source_filters_non_database(self):
        registry = self._make_registry_with_query(
            [
                {"sourceType": "S3", "sourceId": "src-s3"},
                {"sourceType": "DATABASE", "sourceId": "src-db"},
                {"sourceType": "GLUE", "sourceId": "src-glue"},
            ]
        )
        result = await registry.find_sole_database_source("ns-1")
        assert result is not None
        assert result["sourceId"] == "src-db"

    @pytest.mark.asyncio
    async def test_find_sole_database_source_cached(self):
        registry = self._make_registry_with_query(
            [
                {"sourceType": "DATABASE", "sourceId": "src-1"},
            ]
        )
        result1 = await registry.find_sole_database_source("ns-1")
        registry._mock_table.query.return_value = {"Items": []}
        result2 = await registry.find_sole_database_source("ns-1")
        assert result1 == result2
        registry._mock_table.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_sql_namespace_scope_includes_only_queryable_database_sources(self):
        registry = self._make_registry_with_query(
            [
                {
                    "sourceType": "DATABASE",
                    "athenaDatabase": "tenant_a_glue",
                    "queryable": True,
                },
                {
                    "sourceType": "DATABASE",
                    "athenaDataCatalogName": "sclds_a",
                    "discoveredSchemas": ["sales", "analytics"],
                    "queryable": True,
                },
                {
                    "sourceType": "DATABASE",
                    "athenaDatabase": "not_queryable",
                    "queryable": False,
                },
                {
                    "sourceType": "DOCUMENTS",
                    "configuration": {"databaseName": "not_a_database_source"},
                    "queryable": True,
                },
            ]
        )

        scope = await registry.sql_namespace_scope("ns-a")

        assert scope is not None
        assert scope.native_databases == frozenset({"tenant_a_glue"})
        assert scope.federated_catalog_schemas == frozenset({("sclds_a", "sales"), ("sclds_a", "analytics")})

    @pytest.mark.asyncio
    async def test_sql_namespace_scope_native_glue_source_in_distinct_catalog(self):
        """A native Glue source declaring a distinct (non-root) ``athenaCatalog`` is
        authorized under THAT catalog keyed by its Glue database — matching how the
        executor addresses ``<athenaCatalog>.<database>.<table>`` — so a correctly
        qualified cross-catalog reference is not denied. A root ``athenaCatalog`` stays
        on the native-database path.
        """
        registry = self._make_registry_with_query(
            [
                {
                    "sourceType": "DATABASE",
                    "athenaDatabase": "orders_db",
                    "athenaCatalog": "AwsDataCatalog",
                    "queryable": True,
                },
                {
                    "sourceType": "DATABASE",
                    "athenaDatabase": "customers_db",
                    "athenaCatalog": "coa_integ_xcat_abc123",
                    "athenaCatalogOwnershipVerified": True,
                    "queryable": True,
                },
            ]
        )

        scope = await registry.sql_namespace_scope("ns-x")

        assert scope is not None
        # The root-catalog source stays native; the distinct-catalog source is keyed
        # by (catalog, database) under federated_catalog_schemas.
        assert scope.native_databases == frozenset({"orders_db"})
        assert scope.federated_catalog_schemas == frozenset({("coa_integ_xcat_abc123", "customers_db")})

    @pytest.mark.asyncio
    async def test_sql_namespace_scope_unverified_catalog_not_authorized(self):
        """A distinct ``athenaCatalog`` WITHOUT the ownership-verified marker
        (a legacy or seeded row) must NOT be promoted into the authorization
        oracle — ``athenaCatalog`` is derived from a caller-supplied ``catalogId``
        and only ``assert_namespace_may_catalog`` at create makes it trustworthy.
        The source falls through to its native database instead of being dropped."""
        registry = self._make_registry_with_query(
            [
                {
                    "sourceType": "DATABASE",
                    "athenaDatabase": "customers_db",
                    "athenaCatalog": "someone_elses_catalog",
                    # no athenaCatalogOwnershipVerified marker
                    "queryable": True,
                },
            ]
        )

        scope = await registry.sql_namespace_scope("ns-x")

        assert scope is not None
        # NOT added under the unowned catalog...
        assert scope.federated_catalog_schemas == frozenset()
        # ...but still addressable via its native database (additive fall-through).
        assert scope.native_databases == frozenset({"customers_db"})

    @pytest.mark.asyncio
    async def test_sql_namespace_scope_verified_marker_required_exact_true(self):
        """The marker gate is strict: a truthy-but-not-True value (e.g. a stray
        string) does not authorize the nested catalog."""
        registry = self._make_registry_with_query(
            [
                {
                    "sourceType": "DATABASE",
                    "athenaDatabase": "customers_db",
                    "athenaCatalog": "coa_integ_xcat_abc123",
                    "athenaCatalogOwnershipVerified": "yes",  # not the boolean True
                    "queryable": True,
                },
            ]
        )

        scope = await registry.sql_namespace_scope("ns-x")

        assert scope is not None
        assert scope.federated_catalog_schemas == frozenset()
        assert scope.native_databases == frozenset({"customers_db"})

    @pytest.mark.asyncio
    async def test_marker_contract_serve_reads_exact_field_written_by_create_path(self):
        """CROSS-PACKAGE CONTRACT (F5): serve authorizes a nested catalog ONLY via the
        field ``athenaCatalogOwnershipVerified``, which is the exact attribute the
        sources create-path writes (packages/sources/.../database_routes.py:
        ``item["athenaCatalogOwnershipVerified"] = True``; asserted on the write side by
        packages/sources/.../test_database_routes.py::
        ``test_create_glue_source_records_the_declared_catalog_without_system_authority``).

        This test pins the READ side of that contract: a row carrying exactly that
        field+value is authorized, and the SAME row with the field renamed is NOT — so
        a rename on either side (write or read) turns one of the paired tests red. It
        deliberately does not import coa_sources (its package init has a circular import
        under the serve test env); the paired sources test guards the write side.
        """
        _MARKER = "athenaCatalogOwnershipVerified"  # the one contract string
        base_row = {
            "sourceType": "DATABASE",
            "athenaDatabase": "customers_db",
            "athenaCatalog": "coa_ds_owned_cat",
            "queryable": True,
        }
        # With the exact marker field=True → authorized.
        reg_ok = self._make_registry_with_query([{**base_row, _MARKER: True}])
        scope_ok = await reg_ok.sql_namespace_scope("ns-x")
        assert scope_ok is not None
        assert ("coa_ds_owned_cat", "customers_db") in scope_ok.federated_catalog_schemas, (
            f"serve must authorize a nested catalog when the row carries {_MARKER}=True"
        )
        # Same row, marker under any OTHER field name → NOT authorized (proves serve
        # keys on this exact field, so a write-side rename would break authorization).
        reg_renamed = self._make_registry_with_query([{**base_row, "athenaCatalogVerified_RENAMED": True}])
        scope_renamed = await reg_renamed.sql_namespace_scope("ns-x")
        assert scope_renamed is not None
        assert scope_renamed.federated_catalog_schemas == frozenset(), (
            f"serve authorized a nested catalog WITHOUT {_MARKER} — it is not keying on the contract field, "
            "so a create-path that wrote the marker under a different name would be silently trusted"
        )
        assert scope_renamed.native_databases == frozenset({"customers_db"})

    @pytest.mark.asyncio
    async def test_find_sole_database_source_not_available(self):
        registry = SourcesRegistry(table_name="", region="us-west-2")
        result = await registry.find_sole_database_source("ns-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_find_sole_database_source_timeout(self):
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            registry = SourcesRegistry(table_name="test-sources", region="us-west-2")

        with patch("asyncio.wait_for", side_effect=TimeoutError("timed out")):
            result = await registry.find_sole_database_source("ns-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_sql_namespace_scope_includes_a_scanned_databricks_source(self):
        """A scanned Databricks source must be IN the scope, or every qualified query
        against it is denied.

        The negative test passes trivially when a sub-type is absent from the scope map,
        so this positive assertion is the one that proves the integration.
        """
        registry = self._make_registry_with_query(
            [
                {
                    "sourceType": "DATABASE",
                    "sourceSubType": "DATABRICKS_SQL_WAREHOUSE",
                    "athenaDataCatalogName": "coadevds_dbx1",
                    "discoveredSchemas": ["coa_dbx_test"],
                    "queryable": True,
                },
            ]
        )

        scope = await registry.sql_namespace_scope("ns-dbx")

        assert scope is not None
        assert ("coadevds_dbx1", "coa_dbx_test") in scope.federated_catalog_schemas
        # A leak into the native-Glue set would authorize a bare `AwsDataCatalog.<db>`
        # reference the source does not own.
        assert scope.native_databases == frozenset()

    @pytest.mark.asyncio
    async def test_sql_namespace_scope_lowercases_a_databricks_catalog_and_schema(self):
        """Both halves are lower-cased before comparison, so a mixed-case Unity
        Catalog schema still matches the qualifier the executors check."""
        registry = self._make_registry_with_query(
            [
                {
                    "sourceType": "DATABASE",
                    "sourceSubType": "DATABRICKS_SQL_WAREHOUSE",
                    "athenaDataCatalogName": "CoaDevDS_DBX1",
                    "discoveredSchemas": ["Coa_DBX_Test"],
                    "queryable": True,
                },
            ]
        )

        scope = await registry.sql_namespace_scope("ns-dbx")

        assert scope is not None
        assert scope.federated_catalog_schemas == frozenset({("coadevds_dbx1", "coa_dbx_test")})

    @pytest.mark.asyncio
    async def test_sql_namespace_scope_excludes_a_databricks_source_with_no_discovered_schemas(self):
        """The scope is built from catalog x schema, so an empty ``discoveredSchemas``
        contributes no pair and every qualified reference is refused, with the source
        otherwise looking healthy. Reachable when the connector's ``listTables`` returns a
        table with an empty schema.
        """
        registry = self._make_registry_with_query(
            [
                {
                    "sourceType": "DATABASE",
                    "sourceSubType": "DATABRICKS_SQL_WAREHOUSE",
                    "athenaDataCatalogName": "coadevds_dbx1",
                    "discoveredSchemas": [],
                    "queryable": True,
                },
            ]
        )

        scope = await registry.sql_namespace_scope("ns-dbx")

        assert scope is not None
        assert scope.federated_catalog_schemas == frozenset()
        # The catalog is present, so the source is federated-addressed or not at all.
        assert scope.native_databases == frozenset()

    @pytest.mark.asyncio
    async def test_sql_namespace_scope_excludes_a_not_yet_queryable_databricks_source(self):
        """`queryable` is False from create until discovery succeeds, and a source in
        that window owns no catalog reference yet."""
        registry = self._make_registry_with_query(
            [
                {
                    "sourceType": "DATABASE",
                    "sourceSubType": "DATABRICKS_SQL_WAREHOUSE",
                    "athenaDataCatalogName": "coadevds_dbx1",
                    "discoveredSchemas": ["coa_dbx_test"],
                    "queryable": False,
                },
            ]
        )

        scope = await registry.sql_namespace_scope("ns-dbx")

        assert scope is not None
        assert scope.federated_catalog_schemas == frozenset()

    @pytest.mark.asyncio
    async def test_sql_namespace_scope_stays_none_on_an_incomplete_inventory(self):
        """The executors treat None as a DENY, because allowing on uncertainty reopens the
        cross-namespace catalog escape."""
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.query.return_value = {
                "Items": [{"sourceType": "DATABASE", "athenaDataCatalogName": "coadevds_dbx1"}],
                "LastEvaluatedKey": {"PK": "NS#ns-dbx", "SK": "SRC#truncated"},
            }
            mock_resource.return_value.Table.return_value = mock_table
            registry = SourcesRegistry(table_name="test-sources", region="us-west-2")

        assert await registry.sql_namespace_scope("ns-dbx") is None


@pytest.mark.unit
class TestSourceComposition:
    """get_source_composition — the signal that drives serve-tier gating."""

    def _make_registry_with_query(self, items):
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.query.return_value = {"Items": items}
            mock_resource.return_value.Table.return_value = mock_table
            registry = SourcesRegistry(table_name="test-sources", region="us-west-2")
            registry._mock_table = mock_table
        return registry

    @pytest.mark.asyncio
    async def test_documents_only(self):
        registry = self._make_registry_with_query([{"sourceType": "DOCUMENTS", "sourceId": "d1"}])
        comp = await registry.get_source_composition("ns-doc")
        assert comp.has_unstructured_source is True
        assert comp.has_structured_source is False
        assert comp.unknown is False

    @pytest.mark.asyncio
    async def test_database_only(self):
        registry = self._make_registry_with_query([{"sourceType": "DATABASE", "sourceId": "s1"}])
        comp = await registry.get_source_composition("ns-db")
        assert comp.has_structured_source is True
        assert comp.has_unstructured_source is False

    @pytest.mark.asyncio
    async def test_mixed(self):
        registry = self._make_registry_with_query(
            [
                {"sourceType": "DATABASE", "sourceId": "s1"},
                {"sourceType": "DOCUMENTS", "sourceId": "d1"},
            ]
        )
        comp = await registry.get_source_composition("ns-mixed")
        assert comp.has_structured_source is True
        assert comp.has_unstructured_source is True

    @pytest.mark.asyncio
    async def test_empty_namespace_fails_open(self):
        """No SRC# records (empty / not-yet-provisioned / unresolvable namespace) is
        ambiguous, not a reliable zero-count — it must fail open (unknown), never skip
        every tier."""
        registry = self._make_registry_with_query([])
        comp = await registry.get_source_composition("ns-empty")
        assert comp.unknown is True
        # Fail-open: unknown must present as has-everything so no tier is dropped.
        assert comp.has_structured_source is True
        assert comp.has_unstructured_source is True

    @pytest.mark.asyncio
    async def test_not_available_is_unknown(self):
        registry = SourcesRegistry(table_name="", region="us-west-2")
        comp = await registry.get_source_composition("ns")
        assert comp.unknown is True
        # Fail-open: unknown must present as has-everything.
        assert comp.has_structured_source is True
        assert comp.has_unstructured_source is True

    @pytest.mark.asyncio
    async def test_timeout_is_unknown_and_not_cached(self):
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            registry = SourcesRegistry(table_name="test-sources", region="us-west-2")

        with patch("asyncio.wait_for", side_effect=TimeoutError("timed out")):
            comp = await registry.get_source_composition("ns")
        assert comp.unknown is True
        # Unknown must not be cached, so a later successful query is used.
        assert "ns" not in registry._items_cache

    @pytest.mark.asyncio
    async def test_result_is_cached(self):
        registry = self._make_registry_with_query([{"sourceType": "DOCUMENTS", "sourceId": "d1"}])
        first = await registry.get_source_composition("ns-doc")
        # Change the underlying data; cached value should be returned instead.
        registry._mock_table.query.return_value = {"Items": [{"sourceType": "DATABASE", "sourceId": "s1"}]}
        second = await registry.get_source_composition("ns-doc")
        assert first == second
        registry._mock_table.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_truncated_page_is_unknown_and_not_cached(self):
        # DynamoDB signals more records beyond this page via LastEvaluatedKey. A
        # DATABASE source could exist beyond the page, so deriving has_structured=
        # False would wrongly skip Tier 2 — truncation must fail open to unknown.
        registry = self._make_registry_with_query([{"sourceType": "DOCUMENTS", "sourceId": "d1"}])
        registry._mock_table.query.return_value = {
            "Items": [{"sourceType": "DOCUMENTS", "sourceId": "d1"}],
            "LastEvaluatedKey": {"PK": "NS#ns-big", "SK": "SRC#d1"},
        }
        comp = await registry.get_source_composition("ns-big")
        assert comp.unknown is True
        assert comp.has_structured_source is True
        assert comp.has_unstructured_source is True
        # Uncertain (truncated) reads must not be cached, so a later (shrunk) query
        # re-derives cleanly.
        assert "ns-big" not in registry._items_cache

    @pytest.mark.asyncio
    async def test_non_timeout_error_is_unknown(self):
        # A non-timeout DDB failure (e.g. boto3 ClientError) must also fail open to
        # unknown — the documented contract — not propagate out of the method.
        registry = self._make_registry_with_query([{"sourceType": "DOCUMENTS", "sourceId": "d1"}])
        registry._mock_table.query.side_effect = RuntimeError("ddb boom")
        comp = await registry.get_source_composition("ns-err")
        assert comp.unknown is True
        assert comp.has_structured_source is True
        assert comp.has_unstructured_source is True
        assert "ns-err" not in registry._items_cache

    @pytest.mark.asyncio
    async def test_shared_query_cache_across_helpers(self):
        # The #1 fix: get_source_composition and find_sole_database_source both
        # read the same cached SRC# query, so a request that touches gating AND
        # Tier-2 routing issues the underlying DDB query only once.
        registry = self._make_registry_with_query([{"sourceType": "DATABASE", "sourceId": "s1"}])
        comp = await registry.get_source_composition("ns-shared")
        sole = await registry.find_sole_database_source("ns-shared")
        assert comp.has_structured_source is True
        assert sole is not None and sole["sourceId"] == "s1"
        # Only one DDB round-trip despite two derived lookups.
        registry._mock_table.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_items_cache_lru_eviction_is_bounded(self):
        # The #9 fix: the shared items cache is a bounded LRU — filling it past the
        # cap evicts the least-recently-used namespace instead of growing forever.
        registry = self._make_registry_with_query([{"sourceType": "DOCUMENTS", "sourceId": "d1"}])
        for i in range(_ITEMS_CACHE_MAX + 5):
            await registry.get_source_composition(f"ns-{i}")
        assert len(registry._items_cache) == _ITEMS_CACHE_MAX
        # The earliest-inserted namespaces were evicted; the most recent remain.
        assert "ns-0" not in registry._items_cache
        assert f"ns-{_ITEMS_CACHE_MAX + 4}" in registry._items_cache


@pytest.mark.unit
class TestNamespaceExists:
    """Namespace existence check used by the serve entrypoint's 404 gate."""

    def _make_ns_registry(self):
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            registry = SourcesRegistry(
                table_name="test-sources", namespaces_table="test-namespaces", region="us-west-2"
            )
            registry._mock_table = mock_table
        return registry

    @pytest.mark.asyncio
    async def test_true_for_existing_uuid(self):
        reg = self._make_ns_registry()
        ns_uuid = "11111111-2222-3333-4444-555555555555"
        reg._mock_table.get_item.return_value = {"Item": {"PK": f"NS#{ns_uuid}", "SK": "METADATA"}}
        assert await reg.namespace_exists(ns_uuid) is True

    @pytest.mark.asyncio
    async def test_false_when_unresolvable(self):
        reg = self._make_ns_registry()
        # A non-UUID name with no NS_NAME reservation cannot be resolved → not found.
        reg._mock_table.get_item.return_value = {"Item": None}
        assert await reg.namespace_exists("nonexistent-ns-id-12345") is False

    @pytest.mark.asyncio
    async def test_none_when_table_unconfigured(self):
        # No namespaces table configured → existence is undeterminable (fail open).
        with patch("boto3.resource"):
            reg = SourcesRegistry(table_name="test-sources", region="us-west-2")
        assert await reg.namespace_exists("anything") is None

    def test_namespaces_configured_reflects_table_wiring(self):
        """namespaces_configured distinguishes "cannot check" from "lookup errored"
        so the entrypoint fails closed only on the latter (F-2).
        """
        configured = self._make_ns_registry()
        assert configured.namespaces_configured is True

        with patch("boto3.resource"):
            unconfigured = SourcesRegistry(table_name="test-sources", region="us-west-2")
        assert unconfigured.namespaces_configured is False


@pytest.mark.unit
class TestNamespaceLanguage:
    """The namespace language behind the request's label preference (LLD §2.2.5)."""

    NS = "11111111-2222-3333-4444-555555555555"

    def _registry(self, **item):
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            registry = SourcesRegistry(
                table_name="test-sources", namespaces_table="test-namespaces", region="us-west-2"
            )
        mock_table.get_item.return_value = {"Item": {"PK": f"NS#{self.NS}", "SK": "METADATA", **item}}
        registry._mock_table = mock_table
        return registry

    @pytest.mark.asyncio
    async def test_returns_the_stored_language(self):
        assert await self._registry(language="ja").namespace_language(self.NS) == "ja"

    @pytest.mark.asyncio
    async def test_legacy_item_without_language_reads_as_english(self):
        assert await self._registry().namespace_language(self.NS) == "en"

    @pytest.mark.asyncio
    async def test_missing_item_fails_closed(self):
        from coa_serve.clients.sources_registry import NamespaceLanguageUnavailableError

        registry = self._registry()
        registry._mock_table.get_item.return_value = {}
        with pytest.raises(NamespaceLanguageUnavailableError):
            await registry.namespace_language(self.NS)

    @pytest.mark.asyncio
    async def test_read_error_fails_closed(self):
        from coa_serve.clients.sources_registry import NamespaceLanguageUnavailableError

        registry = self._registry(language="ja")
        registry._mock_table.get_item.side_effect = RuntimeError("throttled")
        with pytest.raises(NamespaceLanguageUnavailableError):
            await registry.namespace_language(self.NS)

    @pytest.mark.asyncio
    async def test_invalid_stored_language_fails_closed(self):
        from coa_serve.clients.sources_registry import NamespaceLanguageUnavailableError

        with pytest.raises(NamespaceLanguageUnavailableError):
            await self._registry(language="ja_JP").namespace_language(self.NS)

    @pytest.mark.asyncio
    async def test_unconfigured_table_has_no_language(self):
        with patch("boto3.resource"):
            registry = SourcesRegistry(table_name="test-sources", region="us-west-2")
        assert await registry.namespace_language("anything") is None

    @pytest.mark.asyncio
    async def test_existence_gate_read_is_reused(self):
        """The gate's METADATA read supplies the language; no second read."""
        registry = self._registry(language="ja")
        assert await registry.namespace_exists(self.NS) is True
        reads = registry._mock_table.get_item.call_count
        assert await registry.namespace_language(self.NS) == "ja"
        assert registry._mock_table.get_item.call_count == reads


def _glue_source(source_id, database, **extra):
    """A root-catalog Glue source shaped like the ones the sources API wrote before it stopped setting
    ``athenaDataCatalogName`` for Glue."""
    return {
        "sourceType": "DATABASE",
        "sourceSubType": "GLUE_DATABASE",
        "sourceId": source_id,
        "athenaDatabase": database,
        "athenaDataCatalogName": "AwsDataCatalog",
        "athenaCatalog": "AwsDataCatalog",
        "discoveredSchemas": [database],
        "configuration": json.dumps({"catalogId": "123456789012", "databaseName": database}),
        "queryable": True,
        **extra,
    }


@pytest.mark.unit
class TestPooledGlueDatabases:
    """A namespace pooling several Glue databases in the root catalog."""

    def _registry(self, items):
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.query.return_value = {"Items": items}
            by_sk = {f"SRC#{i['sourceId']}": i for i in items}
            mock_table.get_item.side_effect = lambda Key: {"Item": by_sk[Key["SK"]]} if Key["SK"] in by_sk else {}
            mock_resource.return_value.Table.return_value = mock_table
            registry = SourcesRegistry(table_name="test-sources", region="us-west-2")
        return registry

    def _glue(self, registry, tables_by_db):
        glue = MagicMock()
        glue.get_paginator.return_value.paginate.side_effect = lambda DatabaseName, **_: [
            {"TableList": [{"Name": t} for t in tables_by_db[DatabaseName]]}
        ]
        registry._glue = glue
        return glue

    @pytest.mark.asyncio
    async def test_root_catalog_glue_source_is_a_native_database(self):
        # `AwsDataCatalog` is the root catalog, not a nested one: filing it as a
        # federated pair left native_databases empty and denied the qualifier's
        # own three-part rewrite of a cross-database join.
        registry = self._registry([_glue_source("s-ops", "bank_ops"), _glue_source("s-risk", "bank_risk")])

        scope = await registry.sql_namespace_scope("ns-a")

        assert scope.native_databases == frozenset({"bank_ops", "bank_risk"})
        assert scope.federated_catalog_schemas == frozenset()
        for sql in (
            'SELECT * FROM "awsdatacatalog"."bank_risk"."risk_ecl"',
            "SELECT * FROM bank_ops.acct_mstr a JOIN bank_risk.risk_ecl r ON a.id = r.id",
        ):
            assert SQLFirewall.validate_namespace_sql_scope(
                sql,
                native_databases=scope.native_databases,
                federated_catalog_schemas=scope.federated_catalog_schemas,
                default_catalog="",
            )

    @pytest.mark.asyncio
    async def test_single_database_namespace_places_nothing(self):
        registry = self._registry([_glue_source("s-ops", "bank_ops")])
        glue = self._glue(registry, {"bank_ops": ["acct_mstr"]})

        assert await registry.table_placements("ns-a", {"acct_mstr": "s-ops"}) == {}
        glue.get_paginator.assert_not_called()

    @pytest.mark.asyncio
    async def test_places_by_source_id_then_by_catalog(self):
        registry = self._registry([_glue_source("s-ops", "bank_ops"), _glue_source("s-party", "bank_party")])
        self._glue(registry, {"bank_ops": ["acct_mstr", "dim_dt"], "bank_party": ["br_dir", "dim_dt"]})

        out = await registry.table_placements(
            "ns-a",
            # br_dir carries no source (an uploaded ontology has no R2RML);
            # dim_dt exists in BOTH databases, so it is marked ambiguous.
            {"acct_mstr": "s-ops", "br_dir": "", "dim_dt": ""},
        )

        assert out == {
            "acct_mstr": ("s-ops", "bank_ops"),
            "br_dir": ("s-party", "bank_party"),
            "dim_dt": (AMBIGUOUS_SOURCE, ""),
        }

    @pytest.mark.asyncio
    async def test_table_of_another_source_is_never_looked_up_in_glue(self):
        # `customers` belongs to a JDBC source; a same-named Glue table must not
        # label it with the Glue database.
        registry = self._registry([_glue_source("s-ops", "bank_ops"), _glue_source("s-party", "bank_party")])
        glue = self._glue(registry, {"bank_ops": ["customers"], "bank_party": ["br_dir"]})

        out = await registry.table_placements("ns-a", {"customers": "pg-src"})

        assert out == {}
        glue.get_paginator.assert_not_called()

    @pytest.mark.asyncio
    async def test_database_shared_by_two_sources_is_not_placed_by_catalog(self):
        registry = self._registry(
            [
                _glue_source("s-ops", "bank_ops"),
                _glue_source("s-ops-2", "bank_ops"),
                _glue_source("s-party", "bank_party"),
            ]
        )
        self._glue(registry, {"bank_ops": ["acct_mstr"], "bank_party": ["br_dir"]})

        out = await registry.table_placements("ns-a", {"acct_mstr": "", "br_dir": ""})

        assert out == {"acct_mstr": (AMBIGUOUS_SOURCE, ""), "br_dir": ("s-party", "bank_party")}

    @pytest.mark.asyncio
    async def test_catalog_failure_leaves_unplaced_tables_bare(self):
        registry = self._registry([_glue_source("s-ops", "bank_ops"), _glue_source("s-party", "bank_party")])
        glue = MagicMock()
        glue.get_paginator.side_effect = RuntimeError("AccessDenied")
        registry._glue = glue

        out = await registry.table_placements("ns-a", {"acct_mstr": "s-ops", "br_dir": ""})

        assert out == {"acct_mstr": ("s-ops", "bank_ops")}

    @pytest.mark.asyncio
    async def test_nested_catalog_source_is_not_a_bare_database(self):
        registry = self._registry(
            [
                _glue_source("s-ops", "bank_ops"),
                _glue_source("s-fed", "sales", athenaDataCatalogName="sclds_a"),
            ]
        )

        assert await registry.native_glue_databases("ns-a") == {"s-ops": ("123456789012", "bank_ops")}

    @pytest.mark.asyncio
    async def test_mixed_namespace_does_not_guess_by_catalog(self):
        # A JDBC source cannot be listed in Glue, so a same-named table there
        # would be invisible: an unplaced `customers` must stay unplaced (and be
        # refused downstream), not be guessed onto bank_ops.
        pg = {"sourceType": "DATABASE", "sourceSubType": "JDBC", "sourceId": "s-pg", "queryable": True}
        registry = self._registry([_glue_source("s-ops", "bank_ops"), _glue_source("s-party", "bank_party"), pg])
        glue = self._glue(registry, {"bank_ops": ["customers"], "bank_party": ["br_dir"]})

        out = await registry.table_placements("ns-a", {"customers": "", "br_dir": "s-party"})

        # A table carrying its own Glue source id is still placed.
        assert out == {"br_dir": ("s-party", "bank_party")}
        glue.get_paginator.assert_not_called()

    @pytest.mark.asyncio
    async def test_listing_failure_is_cached(self):
        registry = self._registry([_glue_source("s-ops", "bank_ops"), _glue_source("s-party", "bank_party")])
        glue = MagicMock()
        glue.get_paginator.side_effect = RuntimeError("AccessDenied")
        registry._glue = glue

        for _ in range(3):
            assert await registry.table_placements("ns-a", {"br_dir": ""}) == {}

        # Paid once per TTL, not once per request.
        assert glue.get_paginator.call_count == 1

    @pytest.mark.asyncio
    async def test_listing_asks_glue_for_names_only(self):
        registry = self._registry([_glue_source("s-ops", "bank_ops"), _glue_source("s-party", "bank_party")])
        glue = self._glue(registry, {"bank_ops": ["acct_mstr"], "bank_party": ["br_dir"]})

        await registry.table_placements("ns-a", {"br_dir": ""})

        for call in glue.get_paginator.return_value.paginate.call_args_list:
            assert call.kwargs["AttributesToGet"] == ["NAME"]

    @pytest.mark.asyncio
    async def test_added_source_is_a_cache_miss(self):
        # The listing is cached by the set of databases it covered: once a third
        # source holding the same name is added, a cached two-database listing
        # must not still call the name unique.
        ops, party = _glue_source("s-ops", "bank_ops"), _glue_source("s-party", "bank_party")
        registry = self._registry([ops, party])
        tables = {"bank_ops": ["dim_dt"], "bank_party": ["br_dir"], "bank_risk": ["dim_dt"]}
        self._glue(registry, tables)
        assert await registry.table_placements("ns-a", {"dim_dt": ""}) == {"dim_dt": ("s-ops", "bank_ops")}

        registry._dynamodb.Table.return_value.query.return_value = {
            "Items": [ops, party, _glue_source("s-risk", "bank_risk")]
        }
        registry._items_cache.clear()  # the source list refreshed; the listing did not

        assert await registry.table_placements("ns-a", {"dim_dt": ""}) == {"dim_dt": (AMBIGUOUS_SOURCE, "")}

    @pytest.mark.asyncio
    async def test_concurrent_misses_share_one_listing(self):
        registry = self._registry([_glue_source("s-ops", "bank_ops"), _glue_source("s-party", "bank_party")])
        glue = self._glue(registry, {"bank_ops": ["acct_mstr"], "bank_party": ["br_dir"]})

        results = await asyncio.gather(*(registry.table_placements("ns-a", {"br_dir": ""}) for _ in range(5)))

        assert all(r == {"br_dir": ("s-party", "bank_party")} for r in results)
        # One listing: one paginator per database, not one per request.
        assert glue.get_paginator.call_count == 2

    @pytest.mark.asyncio
    async def test_legacy_record_with_mismatched_schema_grants_nothing(self):
        # The executor runs a legacy root-catalog Glue record against
        # discoveredSchemas[0]; granting athenaDatabase when the two differ would
        # authorize a database the source is not queried as.
        odd = _glue_source("s-odd", "bank_risk", discoveredSchemas=["bank_other"])
        registry = self._registry([_glue_source("s-ops", "bank_ops"), odd])

        scope = await registry.sql_namespace_scope("ns-a")

        assert scope.native_databases == frozenset({"bank_ops"})
        assert scope.federated_catalog_schemas == frozenset()

    @pytest.mark.asyncio
    async def test_record_with_mismatched_schema_places_nothing(self):
        # Placing `t` under athenaDatabase would pin a bare query to a source the
        # executor runs against discoveredSchemas[0], a different database. The
        # record is left out, and since it then cannot be listed, the lookup is
        # off for the namespace (it could hold `u` too), as for a JDBC source.
        odd = _glue_source("s-odd", "db_x", discoveredSchemas=["db_y"])
        registry = self._registry([odd, _glue_source("s-z", "db_z"), _glue_source("s-w", "db_w")])
        glue = self._glue(registry, {"db_x": ["t"], "db_z": ["u"], "db_w": []})

        assert "s-odd" not in await registry.native_glue_databases("ns-a")
        assert await registry.table_placements("ns-a", {"t": "", "u": ""}) == {}
        glue.get_paginator.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_listing_is_retried_after_the_short_window(self, monkeypatch):
        import coa_serve.clients.sources_registry as sr

        registry = self._registry([_glue_source("s-ops", "bank_ops"), _glue_source("s-party", "bank_party")])
        glue = MagicMock()
        glue.get_paginator.side_effect = RuntimeError("Throttling")
        registry._glue = glue
        now = [1000.0]
        monkeypatch.setattr(sr.time, "monotonic", lambda: now[0])

        assert await registry.table_placements("ns-a", {"br_dir": ""}) == {}
        now[0] += sr._FAILED_LISTING_TTL_S + 1
        self._glue(registry, {"bank_ops": ["acct_mstr"], "bank_party": ["br_dir"]})

        assert await registry.table_placements("ns-a", {"br_dir": ""}) == {"br_dir": ("s-party", "bank_party")}

    @pytest.mark.asyncio
    async def test_top_down_join_with_a_shared_name_is_refused(self):
        # acct_mstr is only in bank_ops; dim_dt is in both. Routing the join to
        # bank_ops and running dim_dt bare would silently pick bank_ops.dim_dt.
        registry = self._registry([_glue_source("s-ops", "bank_ops"), _glue_source("s-party", "bank_party")])
        self._glue(registry, {"bank_ops": ["acct_mstr", "dim_dt"], "bank_party": ["br_dir", "dim_dt"]})
        sql = "SELECT * FROM acct_mstr a JOIN dim_dt d ON a.dt = d.dt"

        placements = await registry.table_placements("ns-a", {"acct_mstr": "", "dim_dt": ""})
        routing = sql_table_routing(sql, {t: source for t, (source, _) in placements.items()})
        prepared = await prepare_execution_sql(sql, routing, registry, namespace="ns-a")

        assert prepared.error is not None
        assert "dim_dt" in prepared.error

    @pytest.mark.asyncio
    async def test_root_catalog_value_on_a_non_glue_record_is_not_native(self):
        # Only a Glue record is read as a legacy root-catalog source; any other
        # record keeps its (awsdatacatalog, schema) pair and gains no native grant.
        odd = _glue_source("s-odd", "other_db", sourceSubType="CUSTOM_CONNECTOR")
        registry = self._registry([_glue_source("s-ops", "bank_ops"), odd])

        scope = await registry.sql_namespace_scope("ns-a")

        assert scope.native_databases == frozenset({"bank_ops"})
        assert scope.federated_catalog_schemas == frozenset({("awsdatacatalog", "other_db")})

    @pytest.mark.asyncio
    async def test_top_down_cross_database_join_is_placed_qualified_and_authorized(self):
        # The reported failure, end to end: top-down classes carry no source id;
        # placement routes them, the qualifier rewrites to three-part names, and
        # the executor's namespace check accepts the rewrite.
        registry = self._registry([_glue_source("s-ops", "bank_ops"), _glue_source("s-risk", "bank_risk")])
        self._glue(registry, {"bank_ops": ["acct_mstr"], "bank_risk": ["risk_ecl"]})
        sql = "SELECT a.id FROM bank_ops.acct_mstr a JOIN risk_ecl r ON a.id = r.acct_id"

        placements = await registry.table_placements("ns-a", {"acct_mstr": "", "risk_ecl": ""})
        routing = sql_table_routing(sql, {t: source for t, (source, _) in placements.items()})
        prepared = await prepare_execution_sql(sql, routing, registry, namespace="ns-a")
        scope = await registry.sql_namespace_scope("ns-a")

        assert prepared.error is None
        assert prepared.sql == (
            'SELECT a.id FROM "AwsDataCatalog"."bank_ops".acct_mstr AS a '
            'JOIN "AwsDataCatalog"."bank_risk".risk_ecl AS r ON a.id = r.acct_id'
        )
        assert SQLFirewall.validate_namespace_sql_scope(
            prepared.sql,
            native_databases=scope.native_databases,
            federated_catalog_schemas=scope.federated_catalog_schemas,
            default_catalog="",
        )
