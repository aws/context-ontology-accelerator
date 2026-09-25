# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the SourceDB direct-JDBC query executor (engine-agnostic).

Engine-specific behavior (drivers, SSL, search_path, dialect, TOP/LIMIT) is
tested in test_db_adapters.py. These tests cover the executor's orchestration:
namespace/timeout/firewall gating, credential resolution, the connection cache,
and delegation to the resolved adapter.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
@patch("coa_serve.clients.sources_registry.boto3")
class TestSourceDBQueryExecutor:
    @patch("coa_serve.clients.source_db.boto3")
    def test_validate_namespace_rejects_invalid(self, mock_boto3, _reg):
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        executor = SourceDBQueryExecutor(data_sources_table="test-table")
        with pytest.raises(ValueError, match="Invalid namespace"):
            executor._validate_namespace("../../../etc/passwd")
        with pytest.raises(ValueError, match="Invalid namespace"):
            executor._validate_namespace("ns with spaces")
        # Valid namespaces
        executor._validate_namespace("demo")
        executor._validate_namespace("my-namespace_v2")

    @patch("coa_serve.clients.source_db.boto3")
    async def test_execute_rejects_invalid_timeout(self, mock_boto3, _reg):
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        executor = SourceDBQueryExecutor(data_sources_table="test-table")
        with pytest.raises(ValueError, match="timeout_seconds must be"):
            await executor.execute(
                "SELECT 1",
                namespace="ns",
                data_source_id="ds1",
                timeout_seconds=0,
            )

        with pytest.raises(ValueError, match="timeout_seconds must be"):
            await executor.execute(
                "SELECT 1",
                namespace="ns",
                data_source_id="ds1",
                timeout_seconds=999,
            )

    @patch("coa_serve.clients.source_db.boto3")
    async def test_execute_rejects_non_select(self, mock_boto3, _reg):
        from coa_serve.clients.source_db import SourceDBQueryExecutor
        from coa_serve.tier2.sql_firewall import UnsafeSQLError

        executor = SourceDBQueryExecutor(data_sources_table="test-table")
        with pytest.raises(UnsafeSQLError):
            await executor.execute(
                "DROP TABLE claims",
                namespace="ns",
                data_source_id="ds1",
            )

    @patch("coa_serve.clients.source_db.boto3")
    async def test_execute_delegates_to_adapter(self, mock_boto3, _reg):
        """execute() resolves ONE adapter and delegates inject_limit + run to it.

        This is the core of the maintainability refactor: the executor holds no
        engine ``if`` — it calls the adapter returned by _get_connection. A spy
        adapter proves inject_limit and run are both invoked and the rows flow back.
        """
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        executor = SourceDBQueryExecutor(data_sources_table="test-table")

        spy_adapter = MagicMock()
        spy_adapter.engine_type = "MYSQL"
        spy_adapter.sqlglot_dialect = "mysql"
        spy_adapter.inject_limit = MagicMock(return_value="SELECT a FROM t LIMIT 1000")
        spy_adapter.run = AsyncMock(return_value=([{"a": 1}], ["a"]))
        sentinel_conn = object()

        executor._get_connection = AsyncMock(  # type: ignore[method-assign]
            return_value=(spy_adapter, sentinel_conn, "", "mysql")
        )

        result = await executor.execute("SELECT a FROM t", namespace="ns", data_source_id="ds1", max_rows=1000)

        # Executor over-fetches by 1 (max_rows + 1) to distinguish full from truncated.
        spy_adapter.inject_limit.assert_called_once_with("SELECT a FROM t", 1001)
        spy_adapter.run.assert_awaited_once()
        # run() receives the connection handle + the limit-injected SQL.
        run_args = spy_adapter.run.await_args.args
        assert run_args[0] is sentinel_conn
        assert run_args[1] == "SELECT a FROM t LIMIT 1000"
        assert result.rows == [{"a": 1}]
        assert result.columns == ["a"]

    @patch("coa_serve.clients.source_db.boto3")
    def test_resolve_credentials_rejects_invalid_arn(self, mock_boto3, _reg):
        from coa_serve.clients.source_db import _SECRET_ARN_PATTERN

        assert not _SECRET_ARN_PATTERN.match("not-an-arn")
        assert not _SECRET_ARN_PATTERN.match("arn:aws:s3:::my-bucket")
        assert _SECRET_ARN_PATTERN.match("arn:aws:secretsmanager:us-east-1:123456789012:secret:coa/db-creds-abc123")

    @patch("coa_serve.clients.source_db.boto3")
    async def test_resolve_credentials_uses_new_key_schema(self, mock_source_boto3, _reg):
        """_resolve_credentials reads connection info from config blob, auth from secret."""
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "NS#my-ns",
                "SK": "SRC#ds-uuid",
                "configuration": (
                    '{"host": "db.example.com", "port": 5432, "databaseName": "mydb",'
                    ' "credentialSecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:coa/creds-abc"}'
                ),
            }
        }
        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        _reg.resource.return_value = mock_dynamodb

        mock_secrets = MagicMock()
        mock_secrets.get_secret_value.return_value = {"SecretString": '{"username":"user","password":"pass"}'}
        mock_source_boto3.client.return_value = mock_secrets

        executor = SourceDBQueryExecutor(data_sources_table="coa-dev-sources")
        creds = await executor._resolve_credentials("my-ns", "ds-uuid")

        mock_table.get_item.assert_called_once_with(Key={"PK": "NS#my-ns", "SK": "SRC#ds-uuid"})
        assert creds.username == "user"
        assert creds.host == "db.example.com"
        assert creds.port == 5432
        assert creds.database == "mydb"

    @patch("coa_serve.clients.source_db.boto3")
    async def test_resolve_credentials_handles_dict_configuration(self, mock_source_boto3, _reg):
        """_resolve_credentials handles configuration already parsed as a dict (not a JSON string)."""
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "NS#my-ns",
                "SK": "SRC#ds-uuid",
                "configuration": {
                    "host": "db.example.com",
                    "port": 5432,
                    "databaseName": "mydb",
                    "credentialSecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:coa/creds-abc",
                },
            }
        }
        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        _reg.resource.return_value = mock_dynamodb

        mock_secrets = MagicMock()
        mock_secrets.get_secret_value.return_value = {"SecretString": '{"username":"user","password":"pass"}'}
        mock_source_boto3.client.return_value = mock_secrets

        executor = SourceDBQueryExecutor(data_sources_table="coa-dev-sources")
        creds = await executor._resolve_credentials("my-ns", "ds-uuid")
        assert creds.username == "user"
        assert creds.host == "db.example.com"
        assert creds.database == "mydb"

    @patch("coa_serve.clients.source_db.boto3")
    async def test_resolve_credentials_validates_search_path_before_secret_fetch(self, mock_source_boto3, _reg):
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        executor = SourceDBQueryExecutor(data_sources_table="coa-dev-sources")
        executor._sources.get_source = AsyncMock(
            return_value={
                "discoveredSchemas": "public",
                "configuration": {
                    "engine": "POSTGRESQL",
                    "host": "db.example.com",
                    "port": 5432,
                    "databaseName": "mydb",
                    "credentialSecretArn": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:coa/creds-abc"),
                },
            }
        )

        with pytest.raises(ValueError, match="list of schema identifiers"):
            await executor._resolve_credentials("my-ns", "ds-uuid")

        executor._secrets.get_secret_value.assert_not_called()

    @patch("coa_serve.clients.source_db.boto3")
    async def test_resolve_credentials_defaults_port_per_engine(self, mock_source_boto3, _reg):
        """When the config omits ``port``, the engine adapter's default_port is used.

        MySQL -> 3306, SQL Server -> 1433 (vs the historical PostgreSQL 5432).
        """
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        def _item(engine: str) -> dict:
            return {
                "Item": {
                    "PK": "NS#ns",
                    "SK": "SRC#ds",
                    "configuration": {
                        "engine": engine,
                        "host": "db.example.com",
                        "databaseName": "mydb",
                        "credentialSecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:coa/creds-abc",
                    },
                }
            }

        mock_table = MagicMock()
        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        _reg.resource.return_value = mock_dynamodb
        mock_secrets = MagicMock()
        mock_secrets.get_secret_value.return_value = {"SecretString": '{"username":"u","password":"p"}'}
        mock_source_boto3.client.return_value = mock_secrets

        executor = SourceDBQueryExecutor(data_sources_table="coa-dev-sources")

        mock_table.get_item.return_value = _item("MYSQL")
        creds = await executor._resolve_credentials("ns", "ds")
        assert creds.port == 3306
        assert creds.engine_type == "MYSQL"

        mock_table.get_item.return_value = _item("SQLSERVER")
        creds = await executor._resolve_credentials("ns", "ds")
        assert creds.port == 1433
        assert creds.engine_type == "SQLSERVER"

    @patch("coa_serve.clients.source_db.boto3")
    async def test_resolve_credentials_raises_on_missing_credential_secret_arn(self, mock_source_boto3, _reg):
        """Raises ValueError when credentialSecretArn is missing from the item."""
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "NS#ns",
                "SK": "SRC#ds",
                "configuration": '{"host": "db.example.com", "port": 5432, "databaseName": "mydb"}',
            }
        }
        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        _reg.resource.return_value = mock_dynamodb
        mock_source_boto3.client.return_value = MagicMock()

        executor = SourceDBQueryExecutor(data_sources_table="coa-dev-sources")
        with pytest.raises(ValueError, match="Missing.*credentialSecretArn"):
            await executor._resolve_credentials("ns", "ds")

    @patch("coa_serve.clients.source_db.boto3")
    async def test_resolve_credentials_raises_on_incomplete_config(self, mock_source_boto3, _reg):
        """Missing host or databaseName raises 'Incomplete connection config'.

        port can't be falsy (it defaults to the engine's default_port), so this
        guards the host/database checks specifically.
        """
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        def _item(config: dict) -> dict:
            return {"Item": {"PK": "NS#ns", "SK": "SRC#ds", "configuration": config}}

        mock_table = MagicMock()
        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        _reg.resource.return_value = mock_dynamodb
        mock_source_boto3.client.return_value = MagicMock()
        executor = SourceDBQueryExecutor(data_sources_table="coa-dev-sources")

        base = {
            "engine": "POSTGRESQL",
            "host": "db.example.com",
            "databaseName": "mydb",
            "credentialSecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:coa/creds-abc",
        }
        # Missing host
        mock_table.get_item.return_value = _item({**base, "host": ""})
        with pytest.raises(ValueError, match="Incomplete connection config"):
            await executor._resolve_credentials("ns", "ds")
        # Missing databaseName
        mock_table.get_item.return_value = _item({**base, "databaseName": ""})
        with pytest.raises(ValueError, match="Incomplete connection config"):
            await executor._resolve_credentials("ns", "ds")

    @patch("coa_serve.clients.source_db.boto3")
    async def test_resolve_credentials_raises_on_malformed_arn(self, mock_source_boto3, _reg):
        """A well-formed-but-non-secretsmanager ARN reaching _resolve_credentials is
        rejected (exercises the code path, not just the regex constant)."""
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "NS#ns",
                "SK": "SRC#ds",
                "configuration": {
                    "engine": "POSTGRESQL",
                    "host": "db.example.com",
                    "databaseName": "mydb",
                    "credentialSecretArn": "arn:aws:s3:::my-bucket",
                },
            }
        }
        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        _reg.resource.return_value = mock_dynamodb
        mock_source_boto3.client.return_value = MagicMock()
        executor = SourceDBQueryExecutor(data_sources_table="coa-dev-sources")
        with pytest.raises(ValueError, match="Invalid secret ARN"):
            await executor._resolve_credentials("ns", "ds")

    @patch("coa_serve.clients.source_db.boto3")
    async def test_execute_reports_truncated_only_when_over_max_rows(self, mock_boto3, _reg):
        """A result of EXACTLY max_rows is NOT truncated; over max_rows IS.

        The executor caps the injected LIMIT at max_rows + 1, so the adapter can
        return max_rows + 1 rows, which the executor slices back to max_rows and
        flags truncated. Exactly max_rows returned means nothing was dropped.
        """
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        executor = SourceDBQueryExecutor(data_sources_table="test-table")
        spy_adapter = MagicMock()
        spy_adapter.engine_type = "POSTGRESQL"
        spy_adapter.inject_limit = MagicMock(side_effect=lambda sql, n: sql)

        # Case 1: adapter returns exactly max_rows -> not truncated.
        spy_adapter.run = AsyncMock(return_value=([{"a": i} for i in range(3)], ["a"]))
        executor._get_connection = AsyncMock(return_value=(spy_adapter, object(), "", "postgres"))  # type: ignore[method-assign]
        result = await executor.execute("SELECT a FROM t", namespace="ns", data_source_id="ds", max_rows=3)
        assert result.truncated is False
        assert result.row_count == 3
        # inject_limit was asked for max_rows + 1 (so over-fetch is detectable).
        spy_adapter.inject_limit.assert_called_once_with("SELECT a FROM t", 4)

        # Case 2: adapter returns max_rows + 1 -> truncated, sliced back to max_rows.
        spy_adapter.run = AsyncMock(return_value=([{"a": i} for i in range(4)], ["a"]))
        result = await executor.execute("SELECT a FROM t", namespace="ns", data_source_id="ds", max_rows=3)
        assert result.truncated is True
        assert result.row_count == 3
        assert len(result.rows) == 3

    @patch("coa_serve.clients.source_db.boto3")
    async def test_health_check_returns_error_on_failure(self, mock_source_boto3, _reg):
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        mock_table = MagicMock()
        mock_table.get_item.side_effect = RuntimeError("access denied")
        mock_dynamodb = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        _reg.resource.return_value = mock_dynamodb

        executor = SourceDBQueryExecutor(data_sources_table="test-table")
        result = await executor.health_check()
        assert result["status"] == "error"
        assert result["detail"] == "DDB connectivity check failed"

    def test_missing_table_raises(self, _reg):
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        with pytest.raises(ValueError, match="data_sources_table must be provided"):
            SourceDBQueryExecutor(data_sources_table="")

    # ------------------------------------------------------------------
    # Connection cache — now delegates disposal to the per-key adapter.
    # ------------------------------------------------------------------

    @patch("coa_serve.clients.source_db.boto3")
    async def test_close_disposes_via_adapter(self, mock_boto3, _reg):
        """close() calls the cached adapter's close() for every cached connection."""
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        executor = SourceDBQueryExecutor(data_sources_table="test-table")
        adapter = MagicMock()
        adapter.close = AsyncMock()
        conn = object()
        executor._conn_cache["ns:ds"] = conn
        executor._adapter_cache["ns:ds"] = adapter
        executor._schema_cache["ns:ds"] = "analytics"
        executor._dialect_cache["ns:ds"] = "postgres"

        await executor.close()

        adapter.close.assert_awaited_once_with(conn)
        assert len(executor._conn_cache) == 0
        assert len(executor._adapter_cache) == 0
        assert len(executor._schema_cache) == 0
        assert len(executor._dialect_cache) == 0

    @patch("coa_serve.clients.source_db.boto3")
    async def test_get_connection_evicts_lru_and_closes_via_adapter(self, mock_boto3, _reg):
        """Over MAX_CONN_CACHE_SIZE, the oldest handle is popped AND disposed via its
        adapter, and its schema/dialect/adapter entries are dropped in lockstep."""
        from coa_serve.clients import source_db as source_db_mod
        from coa_serve.clients.db_adapters import DBCredentials
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        executor = SourceDBQueryExecutor(data_sources_table="coa-dev-sources")

        # Track a distinct spy adapter + sentinel handle per key so we can assert the
        # evicted one is closed with its OWN handle.
        made: dict[str, tuple] = {}

        async def _fake_resolve(namespace, data_source_id):
            return DBCredentials(
                username="u", password="p", host="h", port=5432, database="d", schema="s", engine_type="POSTGRESQL"
            )

        executor._resolve_credentials = _fake_resolve  # type: ignore[method-assign]

        def _fake_get_adapter(engine_type):
            # A fresh spy adapter per call, tagged so eviction assertions can find it.
            adapter = MagicMock()
            adapter.engine_type = "POSTGRESQL"
            adapter.sqlglot_dialect = "postgres"
            handle = object()
            adapter.open_connection = AsyncMock(return_value=handle)
            adapter.close = AsyncMock()
            adapter._handle = handle
            return adapter

        with patch.object(source_db_mod, "get_adapter", side_effect=_fake_get_adapter):
            # Fill the cache to capacity.
            for i in range(source_db_mod.MAX_CONN_CACHE_SIZE):
                key = f"ns:ds{i}"
                adapter, conn, _, _ = await executor._get_connection("ns", f"ds{i}")
                made[key] = (adapter, conn)
            # One more insertion evicts the oldest (ns:ds0).
            await executor._get_connection("ns", "dsX")

        evicted_adapter, evicted_conn = made["ns:ds0"]
        evicted_adapter.close.assert_awaited_once_with(evicted_conn)
        assert "ns:ds0" not in executor._conn_cache
        assert "ns:ds0" not in executor._adapter_cache
        assert "ns:ds0" not in executor._schema_cache
        assert "ns:ds0" not in executor._dialect_cache
        assert len(executor._conn_cache) == source_db_mod.MAX_CONN_CACHE_SIZE

    @patch("coa_serve.clients.source_db.boto3")
    async def test_get_connection_caches_adapter_schema_dialect(self, mock_boto3, _reg):
        """_get_connection resolves the adapter by engine and caches all four fields.

        A REDSHIFT source resolves the RedshiftAdapter (dialect ``redshift``); the
        schema and dialect are cached in lockstep with the connection handle.
        """
        from coa_serve.clients.db_adapters import DBCredentials
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        executor = SourceDBQueryExecutor(data_sources_table="coa-dev-sources")
        executor._resolve_credentials = AsyncMock(  # type: ignore[method-assign]
            return_value=DBCredentials(
                username="u",
                password="p",
                host="rs.example.redshift.amazonaws.com",
                port=5439,
                database="demo",
                schema="analytics",
                engine_type="REDSHIFT",
            )
        )
        sentinel_conn = object()
        with patch(
            "coa_serve.clients.db_adapters.postgres.PostgresAdapter.open_connection",
            new=AsyncMock(return_value=sentinel_conn),
        ):
            adapter, conn, schema, dialect = await executor._get_connection("ns", "ds")

        assert adapter.engine_type == "REDSHIFT"
        assert conn is sentinel_conn
        assert schema == "analytics"
        assert dialect == "redshift"
        assert executor._adapter_cache["ns:ds"].engine_type == "REDSHIFT"
        assert executor._dialect_cache["ns:ds"] == "redshift"
        assert executor._schema_cache["ns:ds"] == "analytics"

    @patch("coa_serve.clients.source_db.boto3")
    async def test_get_connection_concurrent_misses_open_only_once(self, mock_boto3, _reg):
        """Concurrent cache MISSES on the same key must open exactly ONE connection —
        the cache lock serializes them so a burst can't exhaust the source DB's pool."""
        import asyncio

        from coa_serve.clients import source_db as source_db_mod
        from coa_serve.clients.db_adapters import DBCredentials
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        executor = SourceDBQueryExecutor(data_sources_table="coa-dev-sources")

        async def _fake_resolve(namespace, data_source_id):
            # Yield control so, without the lock, all coroutines would race past the
            # membership check and each open a connection.
            await asyncio.sleep(0)
            return DBCredentials(
                username="u", password="p", host="h", port=5432, database="d", schema="s", engine_type="POSTGRESQL"
            )

        executor._resolve_credentials = _fake_resolve  # type: ignore[method-assign]

        open_calls = 0
        shared_handle = object()

        def _fake_get_adapter(engine_type):
            adapter = MagicMock()
            adapter.engine_type = "POSTGRESQL"
            adapter.sqlglot_dialect = "postgres"

            async def _open(_creds):
                nonlocal open_calls
                open_calls += 1
                await asyncio.sleep(0)
                return shared_handle

            adapter.open_connection = AsyncMock(side_effect=_open)
            adapter.close = AsyncMock()
            return adapter

        with patch.object(source_db_mod, "get_adapter", side_effect=_fake_get_adapter):
            results = await asyncio.gather(*[executor._get_connection("ns", "ds") for _ in range(10)])

        # Exactly one open despite 10 concurrent misses; every caller got the same handle.
        assert open_calls == 1
        assert all(conn is shared_handle for _, conn, _, _ in results)
        assert len(executor._conn_cache) == 1

    @patch("coa_serve.clients.source_db.boto3")
    async def test_eviction_close_failure_does_not_break_new_connection(self, mock_boto3, _reg):
        """If disposing the evicted connection raises, the caller opening a NEW
        connection still succeeds — the close error is logged, not propagated."""
        from coa_serve.clients import source_db as source_db_mod
        from coa_serve.clients.db_adapters import DBCredentials
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        executor = SourceDBQueryExecutor(data_sources_table="coa-dev-sources")

        async def _fake_resolve(namespace, data_source_id):
            return DBCredentials(
                username="u", password="p", host="h", port=5432, database="d", schema="s", engine_type="POSTGRESQL"
            )

        executor._resolve_credentials = _fake_resolve  # type: ignore[method-assign]

        first = True

        def _fake_get_adapter(engine_type):
            nonlocal first
            adapter = MagicMock()
            adapter.engine_type = "POSTGRESQL"
            adapter.sqlglot_dialect = "postgres"
            adapter.open_connection = AsyncMock(return_value=object())
            # Only the FIRST adapter (the one that will be evicted) fails to close.
            adapter.close = AsyncMock(side_effect=RuntimeError("dispose boom") if first else None)
            first = False
            return adapter

        with patch.object(source_db_mod, "get_adapter", side_effect=_fake_get_adapter):
            for i in range(source_db_mod.MAX_CONN_CACHE_SIZE):
                await executor._get_connection("ns", f"ds{i}")
            # This insertion evicts ns:ds0, whose close() raises — must NOT propagate.
            adapter, conn, _, _ = await executor._get_connection("ns", "dsX")

        assert conn is not None
        assert "ns:dsX" in executor._conn_cache
        assert "ns:ds0" not in executor._conn_cache

    # ------------------------------------------------------------------
    # search_path resolution (still lives in source_db; PG-only consumers).
    # ------------------------------------------------------------------

    def test_resolve_search_path_prefers_discovered_schemas(self, _reg):
        """_resolve_search_path uses discoveredSchemas (authoritative) over the glob."""
        from coa_serve.clients.source_db import _resolve_search_path

        assert _resolve_search_path({"discoveredSchemas": ["dw"]}, {"schemaFilter": "d?"}, "POSTGRESQL") == "dw"
        assert _resolve_search_path({"discoveredSchemas": ["public", "sales"]}, {}, "REDSHIFT") == "public, sales"

    def test_resolve_search_path_falls_back_to_filter_then_public(self, _reg):
        from coa_serve.clients.source_db import _resolve_search_path

        assert _resolve_search_path({}, {"schemaFilter": "dw"}, "POSTGRESQL") == "dw"
        assert _resolve_search_path({}, {}, "REDSHIFT") == "public"

    @pytest.mark.parametrize("engine_type", ["MYSQL", "SQLSERVER"])
    def test_resolve_search_path_is_empty_for_other_engines(self, _reg, engine_type):
        from coa_serve.clients.source_db import _resolve_search_path

        assert _resolve_search_path({"discoveredSchemas": "sales-db"}, {"schemaName": "sales-db"}, engine_type) == ""

    @pytest.mark.parametrize(
        "discovered",
        [
            "public",
            ["bad;name"],
            ["public", "bad-name"],
            ["public", 42],
        ],
    )
    def test_resolve_search_path_rejects_invalid_discovered(self, _reg, discovered):
        """Corrupt metadata cannot silently change the authoritative search path."""
        from coa_serve.clients.source_db import _resolve_search_path

        with pytest.raises(ValueError, match="list of schema identifiers|invalid schema identifier"):
            _resolve_search_path({"discoveredSchemas": discovered}, {"schemaFilter": "dw"}, "POSTGRESQL")
