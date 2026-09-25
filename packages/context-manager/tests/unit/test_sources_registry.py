# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SourcesRegistry."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_serve.clients.sources_registry import _ITEMS_CACHE_MAX, SourcesRegistry


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
