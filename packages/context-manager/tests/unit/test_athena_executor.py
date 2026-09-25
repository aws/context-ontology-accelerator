# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for AthenaQueryExecutor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlglot
from coa_common.constants import RESOURCE_PREFIX
from coa_serve.clients.athena import AthenaQueryError, AthenaQueryExecutor
from coa_serve.clients.sources_registry import SQLNamespaceScope
from coa_serve.tier2.sql_firewall import UnsafeSQLError
from coa_serve.tier2.table_qualifier import real_tables


@pytest.mark.unit
class TestAthenaValidation:
    """Test namespace validation and SQL safety (via centralized firewall)."""

    def _make_executor(self):
        with patch("boto3.client"), patch("boto3.resource"):
            return AthenaQueryExecutor(region="us-east-1", sources_table="test-sources")

    def test_validates_namespace(self):
        from coa_serve.query_utils import validate_namespace

        validate_namespace("my-namespace")
        validate_namespace("test_ns")
        validate_namespace("demo123")

        with pytest.raises(ValueError):
            validate_namespace("invalid namespace!")
        with pytest.raises(ValueError):
            validate_namespace("ns with spaces")

    def test_workgroup_prefix(self):
        with patch("boto3.client"), patch("boto3.resource"):
            executor = AthenaQueryExecutor(workgroup_prefix="coa-", sources_table="t")
        assert executor._workgroup_prefix == "coa-"


@pytest.mark.unit
class TestAthenaExecution:
    """Test query execution (mocked boto3)."""

    async def test_execute_success(self):
        with patch("boto3.client") as mock_boto:
            mock_athena = MagicMock()
            mock_boto.return_value = mock_athena

            mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-123"}
            mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
            mock_athena.get_query_results.return_value = {
                "ResultSet": {
                    "ResultSetMetadata": {
                        "ColumnInfo": [
                            {"Name": "id"},
                            {"Name": "name"},
                        ]
                    },
                    "Rows": [
                        {"Data": [{"VarCharValue": "id"}, {"VarCharValue": "name"}]},  # header
                        {"Data": [{"VarCharValue": "1"}, {"VarCharValue": "Alice"}]},
                        {"Data": [{"VarCharValue": "2"}, {"VarCharValue": "Bob"}]},
                    ],
                }
            }

            with patch("boto3.resource"):
                executor = AthenaQueryExecutor(region="us-east-1", sources_table="t")
            result = await executor.execute(
                "SELECT id, name FROM users",
                namespace="demo",
                database="mydb",
            )

            assert result.row_count == 2
            assert result.columns == ["id", "name"]
            assert result.rows[0]["id"] == "1"
            assert result.rows[0]["name"] == "Alice"
            assert result.truncated is False

    async def test_invalid_timeout_raises(self):
        with patch("boto3.client"), patch("boto3.resource"):
            executor = AthenaQueryExecutor(region="us-east-1", sources_table="t")

        with pytest.raises(ValueError, match="timeout_seconds must be 1-300"):
            await executor.execute(
                "SELECT 1",
                namespace="demo",
                timeout_seconds=0,
            )

        with pytest.raises(ValueError, match="timeout_seconds must be 1-300"):
            await executor.execute(
                "SELECT 1",
                namespace="demo",
                timeout_seconds=400,
            )

    async def test_invalid_sql_raises(self):
        with patch("boto3.client"), patch("boto3.resource"):
            executor = AthenaQueryExecutor(region="us-east-1", sources_table="t")

        with pytest.raises(UnsafeSQLError):
            await executor.execute(
                "DROP TABLE users",
                namespace="demo",
            )

    async def test_foreign_qualified_database_is_denied_before_submission(self):
        with patch("boto3.client") as mock_boto, patch("boto3.resource"):
            athena_client = MagicMock()
            mock_boto.return_value = athena_client
            executor = AthenaQueryExecutor(region="us-east-1", sources_table="t")

        executor._sources.sql_namespace_scope = AsyncMock(
            return_value=SQLNamespaceScope(
                native_databases=frozenset({"tenant_a_db"}),
                federated_catalog_schemas=frozenset(),
            )
        )
        with pytest.raises(AthenaQueryError, match="outside the requested namespace"):
            await executor.execute(
                "SELECT * FROM AwsDataCatalog.tenant_b_db.customers",
                namespace="tenant-a",
                database="tenant_a_db",
            )

        athena_client.start_query_execution.assert_not_called()

    async def test_qualified_database_is_denied_when_namespace_scope_is_unavailable(self):
        with patch("boto3.client") as mock_boto, patch("boto3.resource"):
            athena_client = MagicMock()
            mock_boto.return_value = athena_client
            executor = AthenaQueryExecutor(region="us-east-1", sources_table="t")

        executor._sources.sql_namespace_scope = AsyncMock(return_value=None)
        with pytest.raises(AthenaQueryError, match="Unable to verify SQL references"):
            await executor.execute(
                "SELECT * FROM AwsDataCatalog.tenant_a_db.customers",
                namespace="tenant-a",
                database="tenant_a_db",
            )

        athena_client.start_query_execution.assert_not_called()


@pytest.mark.unit
class TestTableNameRewrite:
    """Test _rewrite_table_names_for_federation."""

    def test_strips_schema_prefix_and_quotes(self):
        result = AthenaQueryExecutor._rewrite_table_names_for_federation(
            "SELECT COUNT(*) AS v0 FROM BIRD_PUBLIC_INCOME AS V1", "public"
        )
        assert '"income"' in result
        assert "BIRD_PUBLIC_INCOME" not in result

    def test_handles_multiple_tables(self):
        sql = "SELECT a.id, b.name FROM BIRD_PUBLIC_INCOME AS a JOIN BIRD_PUBLIC_MAJOR AS b ON a.id = b.id"
        result = AthenaQueryExecutor._rewrite_table_names_for_federation(sql, "public")
        assert '"income"' in result
        assert '"major"' in result
        assert "BIRD_PUBLIC" not in result

    def test_no_match_leaves_sql_unchanged(self):
        sql = "SELECT 1 FROM some_table"
        result = AthenaQueryExecutor._rewrite_table_names_for_federation(sql, "public")
        assert "some_table" in result

    def test_preserves_column_references(self):
        sql = "SELECT V1.AMOUNT FROM BIRD_PUBLIC_INCOME AS V1 WHERE V1.AMOUNT > 100"
        result = AthenaQueryExecutor._rewrite_table_names_for_federation(sql, "public")
        assert '"income"' in result
        assert "AMOUNT" in result or "amount" in result.lower()

    def test_invalid_sql_returns_original(self):
        sql = "NOT VALID SQL {{{"
        result = AthenaQueryExecutor._rewrite_table_names_for_federation(sql, "public")
        assert result == sql


@pytest.mark.unit
class TestTableAliasDisambiguation:
    """Test _disambiguate_table_aliases.

    ``ONTOP_SQL`` is the real statement Ontop generated for a question about the
    largest orders against a custom-connector source. It failed against a live
    LAMBDA catalog with ``TYPE_MISMATCH: line 1:285: Expression V1 is not of
    type ROW``.
    """

    ONTOP_SQL = (
        'SELECT V1."order_id" AS "order_id1m11", V1."order_ts" AS "order_ts1m16", '
        'CAST(V1."total_amount" AS DOUBLE) AS "v1" FROM "orders" AS V1 '
        'WHERE V1."total_amount" IS NOT NULL '
        'ORDER BY CAST(V1."total_amount" AS DOUBLE) DESC LIMIT 10'
    )

    def test_renames_the_table_alias_that_collides_with_a_projection_alias(self):
        result = AthenaQueryExecutor._disambiguate_table_aliases(self.ONTOP_SQL)
        # The projection alias must survive: callers bind result columns by name,
        # so renaming that side instead would break them.
        assert '"v1"' in result
        # No bare V1 qualifier may remain in any case, or ORDER BY still resolves
        # it to the projection alias.
        assert "V1." not in result

    def test_keeps_every_column_bound_to_its_renamed_table(self):
        result = AthenaQueryExecutor._disambiguate_table_aliases(self.ONTOP_SQL)
        # Every reference must move together; a partial rename yields "column
        # cannot be resolved" rather than a clean failure. Asserted as a count
        # conservation rather than a literal, so the test states the invariant
        # instead of restating the fixture.
        qualified_before = self.ONTOP_SQL.count("V1.")
        assert result.count("V1_t.") == qualified_before
        assert result.lower().count("total_amount") == self.ONTOP_SQL.lower().count("total_amount")
        assert "order_id" in result and "order_ts" in result

    def test_no_collision_leaves_sql_untouched(self):
        # V1 vs v0 — the numbers do not meet, so nothing needs repairing.
        sql = 'SELECT V1."amount" AS "v0" FROM "orders" AS V1 ORDER BY V1."amount" DESC'
        assert AthenaQueryExecutor._disambiguate_table_aliases(sql) == sql

    def test_collision_is_detected_across_case(self):
        # Trino folds identifiers regardless of quoting, so "V1" and "v1" are one
        # name to it even though they differ as Python strings.
        sql = 'SELECT CAST(v1."amount" AS DOUBLE) AS "V1" FROM "orders" AS v1 ORDER BY v1."amount"'
        assert AthenaQueryExecutor._disambiguate_table_aliases(sql) != sql

    def test_generated_alias_avoids_a_second_collision(self):
        # A table already aliased V1_t must not be collided with by the rename.
        sql = (
            'SELECT CAST(V1."amount" AS DOUBLE) AS "v1", V1_t."x" AS "c" '
            'FROM "orders" AS V1 JOIN "other" AS V1_t ON V1."id" = V1_t."id"'
        )
        result = AthenaQueryExecutor._disambiguate_table_aliases(sql)
        assert "v1_t1" in result.lower()

    def test_multiple_colliding_aliases_are_each_renamed(self):
        sql = (
            'SELECT CAST(V1."a" AS DOUBLE) AS "v1", CAST(V2."b" AS DOUBLE) AS "v2" '
            'FROM "x" AS V1 JOIN "y" AS V2 ON V1."id" = V2."id"'
        )
        result = AthenaQueryExecutor._disambiguate_table_aliases(sql)
        assert "V1." not in result
        assert "V2." not in result

    def test_invalid_sql_returns_original(self):
        sql = "NOT VALID SQL {{{"
        assert AthenaQueryExecutor._disambiguate_table_aliases(sql) == sql

    def test_unaliased_tables_are_left_alone(self):
        sql = 'SELECT "amount" AS "v1" FROM "orders"'
        assert AthenaQueryExecutor._disambiguate_table_aliases(sql) == sql


@pytest.mark.unit
class TestFederationFailureExplanation:
    """Test _explain_federation_failure."""

    def test_invoke_denial_names_the_grant_and_the_role(self):
        exc = AthenaQueryError("Athena query FAILED: Insufficient permissions to execute the query.")
        result = AthenaQueryExecutor._explain_federation_failure(exc, "scldevds_abc123")
        assert "lambda:InvokeFunction" in str(result)
        assert "serve runtime role" in str(result)
        assert "coa:connector" in str(result)
        assert "scldevds_abc123" in str(result)

    def test_spill_denial_names_the_bucket_policy_and_prefix(self):
        exc = AthenaQueryError(
            "Athena query FAILED: Access Denied (Service: Amazon S3; Status Code: 403; "
            "Error Code: AccessDenied; Request ID: ABC)"
        )
        result = AthenaQueryExecutor._explain_federation_failure(exc, "scldevds_abc123")
        assert "s3:GetObject" in str(result)
        assert "connectors/<connectorId>/spills/" in str(result)
        # SSE-KMS with a tagged key is mandatory, so the hint must name both.
        assert "coa:connector-spill" in str(result)
        assert "kms:Decrypt" in str(result)

    def test_the_original_message_is_preserved(self):
        # Operators grep for Athena's own wording, and the request id is the only
        # handle AWS support can act on — neither may be dropped.
        exc = AthenaQueryError("Athena query FAILED: Access Denied (Service: Amazon S3; Request ID: XYZ789)")
        assert "Request ID: XYZ789" in str(AthenaQueryExecutor._explain_federation_failure(exc, "cat"))

    def test_glue_native_path_is_untouched(self):
        # No catalog means no federation, so the connector hints would be wrong.
        exc = AthenaQueryError("Athena query FAILED: Insufficient permissions to execute the query.")
        assert AthenaQueryExecutor._explain_federation_failure(exc, "") is exc

    def test_unrecognised_failures_pass_through_unchanged(self):
        # Guessing a cause for a syntax error would send the reader to IAM for a
        # problem that is in their query.
        exc = AthenaQueryError("Athena query FAILED: SYNTAX_ERROR: line 1:8: mismatched input")
        assert AthenaQueryExecutor._explain_federation_failure(exc, "scldevds_abc123") is exc


@pytest.mark.unit
class TestFullyQualifiedSQLSkipsCatalogResolution:
    """Cross-catalog SQL must reach Athena verbatim.

    A statement whose every table carries its own catalog is context-independent
    (verified live: it succeeds with a nonexistent context Database and with no
    context at all). Resolving ONE catalog for it is meaningless, and
    ``_rewrite_table_names_for_federation`` — which strips one source's schema
    prefix — would corrupt the names it was never meant to see.
    """

    def _mock_athena(self, mock_boto):
        mock_athena = MagicMock()
        mock_boto.return_value = mock_athena
        mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-xcat"}
        mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
        mock_athena.get_query_results.return_value = {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": "cnt"}]},
                "Rows": [{"Data": [{"VarCharValue": "cnt"}]}, {"Data": [{"VarCharValue": "7"}]}],
            }
        }
        return mock_athena

    @staticmethod
    async def _qualified_sql() -> str:
        """The cross-catalog statement as the QUALIFIER actually writes it.

        Composed rather than hand-authored on purpose: this class asserts that the
        executor leaves such SQL alone, and a hand-written literal only proves it
        leaves *that literal* alone. If ``qualify_cross_source_sql`` ever changed the
        form it emits (quoting, part order, alias placement), a hand-written fixture
        would keep passing while production SQL took the rewrite path again — the
        exact seam this fix lives in.
        """
        from unittest.mock import AsyncMock

        from coa_serve.tier2.table_qualifier import qualify_cross_source_sql

        sources = {
            "src-pg": {"athenaDataCatalogName": "pg_cat", "discoveredSchemas": ["public"]},
            "src-glue": {"glueDatabaseName": "insurance"},
        }
        registry = AsyncMock()
        registry.get_source.side_effect = lambda namespace, data_source_id: sources.get(data_source_id, {})
        routing = {
            "claims": {"datasourceId": "src-pg", "sourceSchema": "public"},
            "policies": {"datasourceId": "src-glue", "sourceSchema": "insurance"},
        }
        return await qualify_cross_source_sql(
            "SELECT COUNT(*) AS cnt FROM claims a JOIN policies b ON a.id = b.claim_id",
            routing,
            registry,
            namespace="ns-123",
        )

    async def test_cross_catalog_sql_is_not_rewritten(self):
        sql = await self._qualified_sql()
        # Guard the fixture itself: if the qualifier stopped producing 3-part names
        # the assertions below would be vacuous rather than failing.
        assert '"pg_cat"."public".claims' in sql and '"awsdatacatalog"."insurance".policies' in sql, sql
        with patch("boto3.client") as mock_boto, patch("boto3.resource") as mock_res:
            mock_athena = self._mock_athena(mock_boto)
            mock_table = MagicMock()
            mock_res.return_value.Table.return_value = mock_table
            mock_table.get_item.return_value = {"Item": None}
            executor = AthenaQueryExecutor(region="us-east-1", sources_table="coa-sources")

        # Namespace-scope authorization (a security control added after the cross-source fix)
        # runs on ANY qualified reference, including this fully-qualified cross-source
        # statement — it MUST NOT be skipped just because the SQL is self-routing, or
        # a qualified reference could reach a catalog outside the namespace. Grant a
        # scope covering exactly the two refs the qualifier wrote so authorization
        # passes and the invariant below (no rewrite) is what is under test.
        executor._sources.sql_namespace_scope = AsyncMock(
            return_value=SQLNamespaceScope(
                native_databases=frozenset({"insurance"}),
                federated_catalog_schemas=frozenset({("pg_cat", "public")}),
            )
        )

        await executor.execute(sql, namespace="ns-123")

        sent = mock_athena.start_query_execution.call_args[1]["QueryString"]
        # Verbatim: every table reference the qualifier wrote survives untouched.
        for table in real_tables(sqlglot.parse_one(sql, dialect="trino")):
            assert table.sql(dialect="trino") in sent, f"{table.sql(dialect='trino')} was rewritten. sent={sent!r}"
        # No catalog RESOLUTION was needed to route it: the source-metadata table is
        # never queried to pick a single (catalog, database) for the statement.
        # (sql_namespace_scope above is authorization, not routing, and is mocked.)
        mock_table.query.assert_not_called()

    async def test_bare_sql_still_resolves_a_catalog(self):
        """The unqualified path is untouched — it still consults the registry."""
        import json

        with patch("boto3.client") as mock_boto, patch("boto3.resource") as mock_res:
            mock_athena = self._mock_athena(mock_boto)
            mock_table = MagicMock()
            mock_res.return_value.Table.return_value = mock_table
            mock_table.query.return_value = {
                "Items": [
                    {
                        "sourceType": "DATABASE",
                        "athenaDataCatalogName": "scldevds_abc123",
                        "discoveredSchemas": ["public"],
                        "queryable": True,
                        "configuration": json.dumps({}),
                    }
                ]
            }
            mock_table.get_item.return_value = {"Item": None}
            executor = AthenaQueryExecutor(region="us-east-1", sources_table="coa-sources")

        await executor.execute("SELECT COUNT(*) AS cnt FROM claims", namespace="ns-123")

        ctx = mock_athena.start_query_execution.call_args[1]["QueryExecutionContext"]
        assert ctx["Catalog"] == "scldevds_abc123"
        assert ctx["Database"] == "public"


@pytest.mark.unit
class TestFederatedCatalogResolution:
    """Test that federated catalog sources route correctly."""

    async def test_federated_source_uses_discovered_schemas(self):
        """When athenaDataCatalogName + discoveredSchemas set, uses first discovered schema."""
        import json

        with patch("boto3.client") as mock_boto, patch("boto3.resource") as mock_res:
            mock_athena = MagicMock()
            mock_boto.return_value = mock_athena
            mock_table = MagicMock()
            mock_res.return_value.Table.return_value = mock_table

            mock_table.query.return_value = {
                "Items": [
                    {
                        "sourceType": "DATABASE",
                        "athenaDataCatalogName": "scldevds_abc123",
                        "discoveredSchemas": ["public", "analytics"],
                        "queryable": True,
                        "configuration": json.dumps({"schemaName": "ignored"}),
                    }
                ]
            }
            mock_table.get_item.return_value = {"Item": {"athenaWorkgroupName": f"{RESOURCE_PREFIX}-dev-ns-123"}}
            mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-fed"}
            mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
            mock_athena.get_query_results.return_value = {
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "cnt"}]},
                    "Rows": [
                        {"Data": [{"VarCharValue": "cnt"}]},
                        {"Data": [{"VarCharValue": "42"}]},
                    ],
                }
            }

            executor = AthenaQueryExecutor(region="us-west-2", sources_table="coa-sources", sources_registry=None)
            executor._sources._table_name = "coa-sources"

        result = await executor.execute(
            "SELECT COUNT(*) FROM BIRD_PUBLIC_INCOME",
            namespace="ns-123",
        )

        call_kwargs = mock_athena.start_query_execution.call_args[1]
        assert call_kwargs["QueryExecutionContext"]["Catalog"] == "scldevds_abc123"
        assert call_kwargs["QueryExecutionContext"]["Database"] == "public"
        assert '"income"' in call_kwargs["QueryString"]
        assert result.row_count == 1

    async def test_not_queryable_source_falls_back(self):
        """When queryable is explicitly False, falls back to default database."""
        import json

        with patch("boto3.client") as mock_boto, patch("boto3.resource") as mock_res:
            mock_athena = MagicMock()
            mock_boto.return_value = mock_athena
            mock_table = MagicMock()
            mock_res.return_value.Table.return_value = mock_table

            mock_table.query.return_value = {
                "Items": [
                    {
                        "sourceType": "DATABASE",
                        "athenaDataCatalogName": "scldevds_abc123",
                        "queryable": False,
                        "configuration": json.dumps({}),
                    }
                ]
            }
            mock_table.get_item.return_value = {"Item": None}
            mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-nq"}
            mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
            mock_athena.get_query_results.return_value = {
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "x"}]},
                    "Rows": [{"Data": [{"VarCharValue": "x"}]}],
                }
            }

            with patch.dict("os.environ", {"ATHENA_DATABASE": "fallback_db"}):
                executor = AthenaQueryExecutor(region="us-west-2", sources_table="coa-sources")

        await executor.execute("SELECT 1 as x", namespace="ns-123")

        call_kwargs = mock_athena.start_query_execution.call_args[1]
        assert call_kwargs["QueryExecutionContext"]["Database"] == "fallback_db"
        assert call_kwargs["QueryExecutionContext"]["Catalog"] == "AwsDataCatalog"

    async def test_glue_source_uses_athena_database(self):
        """Glue native source uses athenaDatabase field."""
        import json

        with patch("boto3.client") as mock_boto, patch("boto3.resource") as mock_res:
            mock_athena = MagicMock()
            mock_boto.return_value = mock_athena
            mock_table = MagicMock()
            mock_res.return_value.Table.return_value = mock_table

            mock_table.get_item.return_value = {
                "Item": {
                    "sourceType": "DATABASE",
                    "sourceSubType": "GLUE_DATABASE",
                    "athenaDatabase": "my_glue_db",
                    "queryable": True,
                    "configuration": json.dumps({"databaseName": "ignored"}),
                }
            }
            mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-g"}
            mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
            mock_athena.get_query_results.return_value = {
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "x"}]},
                    "Rows": [{"Data": [{"VarCharValue": "x"}]}],
                }
            }

            executor = AthenaQueryExecutor(region="us-west-2", sources_table="coa-sources")

        await executor.execute("SELECT 1", namespace="ns-123", data_source_id="src-glue")

        call_kwargs = mock_athena.start_query_execution.call_args[1]
        assert call_kwargs["QueryExecutionContext"]["Database"] == "my_glue_db"
        assert call_kwargs["QueryExecutionContext"]["Catalog"] == "AwsDataCatalog"


@pytest.mark.unit
class TestAthenaDatabaseResolution:
    """Test per-namespace database resolution via SourcesRegistry."""

    async def test_uses_resolved_database_from_ddb(self):
        """When data_source_id resolves to a database, that database is used in the query."""
        import json

        with patch("boto3.client") as mock_boto, patch("boto3.resource") as mock_res:
            mock_athena = MagicMock()
            mock_boto.return_value = mock_athena
            mock_table = MagicMock()
            mock_res.return_value.Table.return_value = mock_table

            mock_table.get_item.return_value = {
                "Item": {"configuration": json.dumps({"databaseName": "bird_test_db_catalog"})}
            }
            mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-1"}
            mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
            mock_athena.get_query_results.return_value = {
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "count"}]},
                    "Rows": [
                        {"Data": [{"VarCharValue": "count"}]},
                        {"Data": [{"VarCharValue": "42"}]},
                    ],
                }
            }

            executor = AthenaQueryExecutor(region="us-west-2", sources_table="coa-sources")

        await executor.execute(
            "SELECT count(*) as count FROM bird_public_claim",
            namespace="ns-123",
            data_source_id="src-456",
        )

        call_kwargs = mock_athena.start_query_execution.call_args[1]
        assert call_kwargs["QueryExecutionContext"]["Database"] == "bird_test_db_catalog"

    async def test_empty_id_multi_database_source_bare_sql_refuses(self):
        """With an empty data_source_id and >=2 DATABASE sources, a bare
        (unqualified) statement must be REFUSED, not silently pinned to the first
        DATABASE source (which ran same-named tables against the wrong database)."""
        with patch("boto3.client") as mock_boto, patch("boto3.resource") as mock_res:
            mock_athena = MagicMock()
            mock_boto.return_value = mock_athena
            mock_table = MagicMock()
            mock_res.return_value.Table.return_value = mock_table
            # Three DATABASE sources sharing a `customers` table — the same-name shape.
            mock_table.query.return_value = {
                "Items": [
                    {"sourceType": "DATABASE", "glueDatabaseName": "analytics", "queryable": True},
                    {"sourceType": "DATABASE", "glueDatabaseName": "sales", "queryable": True},
                    {"sourceType": "DATABASE", "glueDatabaseName": "billing", "queryable": True},
                ]
            }
            mock_table.get_item.return_value = {"Item": None}
            executor = AthenaQueryExecutor(region="us-east-1", sources_table="coa-sources")

        with pytest.raises(AthenaQueryError, match="ambiguous source"):
            await executor.execute("SELECT COUNT(*) FROM customers", namespace="ns-tri", data_source_id="")
        # Never dispatched to Athena — refused before start_query_execution.
        mock_athena.start_query_execution.assert_not_called()

    async def test_empty_id_single_database_source_bare_sql_still_pins(self):
        """No regression: with exactly ONE DATABASE source, an empty id + bare SQL
        still resolves to that sole source (the legitimate single-source case)."""
        import json

        with patch("boto3.client") as mock_boto, patch("boto3.resource") as mock_res:
            mock_athena = MagicMock()
            mock_boto.return_value = mock_athena
            mock_table = MagicMock()
            mock_res.return_value.Table.return_value = mock_table
            mock_table.query.return_value = {
                "Items": [
                    {
                        "sourceType": "DATABASE",
                        "glueDatabaseName": "only_db",
                        "queryable": True,
                        "configuration": json.dumps({}),
                    }
                ]
            }
            mock_table.get_item.return_value = {"Item": None}
            mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-solo"}
            mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
            mock_athena.get_query_results.return_value = {
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "c"}]},
                    "Rows": [{"Data": [{"VarCharValue": "c"}]}],
                }
            }
            executor = AthenaQueryExecutor(region="us-east-1", sources_table="coa-sources")

        await executor.execute("SELECT COUNT(*) AS c FROM customers", namespace="ns-solo", data_source_id="")
        ctx = mock_athena.start_query_execution.call_args[1]["QueryExecutionContext"]
        assert ctx["Database"] == "only_db"

    async def test_empty_id_multi_source_but_fully_qualified_sql_does_not_raise(self):
        """No regression: with an empty data_source_id and >=2 DATABASE
        sources, a FULLY catalog-qualified statement must still run (via the
        neutral-context path) — the same-name refusal applies ONLY to bare SQL, since
        qualified names are context-independent and route themselves."""
        with patch("boto3.client") as mock_boto, patch("boto3.resource") as mock_res:
            mock_athena = MagicMock()
            mock_boto.return_value = mock_athena
            mock_table = MagicMock()
            mock_res.return_value.Table.return_value = mock_table
            # >=2 DATABASE sources — the same ambiguous namespace as the refuse test.
            mock_table.query.return_value = {
                "Items": [
                    {"sourceType": "DATABASE", "glueDatabaseName": "analytics", "queryable": True},
                    {"sourceType": "DATABASE", "glueDatabaseName": "billing", "queryable": True},
                ]
            }
            mock_table.get_item.return_value = {"Item": None}
            mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-q"}
            mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
            mock_athena.get_query_results.return_value = {
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "c"}]},
                    "Rows": [{"Data": [{"VarCharValue": "c"}]}],
                }
            }
            executor = AthenaQueryExecutor(region="us-east-1", sources_table="coa-sources")
            # sql_namespace_scope covers the two qualified refs so authorization passes.
            executor._sources.sql_namespace_scope = AsyncMock(
                return_value=SQLNamespaceScope(
                    native_databases=frozenset({"analytics", "billing"}),
                    federated_catalog_schemas=frozenset(),
                )
            )

        # Fully qualified across two databases of AwsDataCatalog, empty id → must NOT raise.
        await executor.execute(
            'SELECT COUNT(*) AS c FROM "awsdatacatalog"."analytics".customers a '
            'JOIN "awsdatacatalog"."billing".invoices b ON a.id = b.cid',
            namespace="ns-tri",
            data_source_id="",
        )
        # Neutral context: no single database pinned.
        ctx = mock_athena.start_query_execution.call_args[1]["QueryExecutionContext"]
        assert ctx["Database"] == executor._default_database

    async def test_falls_back_to_env_var_when_ddb_empty(self):
        """When DDB lookup returns nothing, falls back to ATHENA_DATABASE env var."""
        with patch("boto3.client") as mock_boto, patch("boto3.resource") as mock_res:
            mock_athena = MagicMock()
            mock_boto.return_value = mock_athena
            mock_table = MagicMock()
            mock_res.return_value.Table.return_value = mock_table

            mock_table.get_item.return_value = {"Item": None}
            mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-2"}
            mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
            mock_athena.get_query_results.return_value = {
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "x"}]},
                    "Rows": [{"Data": [{"VarCharValue": "x"}]}],
                }
            }

            with patch.dict("os.environ", {"ATHENA_DATABASE": "fallback_db"}):
                executor = AthenaQueryExecutor(region="us-west-2", sources_table="coa-sources")

            await executor.execute(
                "SELECT 1 as x",
                namespace="ns-123",
                data_source_id="missing-source",
            )

        call_kwargs = mock_athena.start_query_execution.call_args[1]
        assert call_kwargs["QueryExecutionContext"]["Database"] == "fallback_db"

    async def test_explicit_database_param_skips_ddb(self):
        """When database param is passed explicitly, DDB is not queried."""
        with patch("boto3.client") as mock_boto, patch("boto3.resource") as mock_res:
            mock_athena = MagicMock()
            mock_boto.return_value = mock_athena
            mock_table = MagicMock()
            mock_res.return_value.Table.return_value = mock_table

            mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-3"}
            mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
            mock_athena.get_query_results.return_value = {
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "x"}]},
                    "Rows": [{"Data": [{"VarCharValue": "x"}]}],
                }
            }

            executor = AthenaQueryExecutor(region="us-west-2", sources_table="coa-sources")

        await executor.execute(
            "SELECT 1 as x",
            namespace="ns-123",
            database="explicit_db",
        )

        call_kwargs = mock_athena.start_query_execution.call_args[1]
        assert call_kwargs["QueryExecutionContext"]["Database"] == "explicit_db"
        mock_table.get_item.assert_not_called()


@pytest.mark.unit
class TestResultPagination:
    """Athena returns at most 1000 rows per call; results must page via NextToken.

    Regression coverage for the silent-truncation bug: `_get_results` issued a
    single GetQueryResults call and discarded `NextToken`, so every result set
    was capped at 999 rows (1000 minus the header) regardless of max_rows. It
    surfaced in benchmarking as four TPC-H queries returning exactly 999 rows.
    """

    @staticmethod
    def _page(start: int, count: int, *, header: bool, token: str | None):
        """Build one GetQueryResults page. The header row appears ONLY on page 1."""
        rows = []
        if header:
            rows.append({"Data": [{"VarCharValue": "id"}]})
        rows += [{"Data": [{"VarCharValue": str(i)}]} for i in range(start, start + count)]
        page = {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Name": "id"}]}, "Rows": rows}}
        if token:
            page["NextToken"] = token
        return page

    async def _run(self, pages, max_rows):
        with patch("boto3.client") as mock_boto:
            mock_athena = MagicMock()
            mock_boto.return_value = mock_athena
            mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
            mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
            mock_athena.get_query_results.side_effect = pages
            with patch("boto3.resource"):
                ex = AthenaQueryExecutor(region="us-east-1", sources_table="t")
            res = await ex.execute("SELECT id FROM t", namespace="demo", database="db", max_rows=max_rows)
            return res, mock_athena

    async def test_follows_next_token_across_pages(self):
        """2500 rows across 3 pages must all be returned, not truncated at 999."""
        pages = [
            self._page(0, 1000, header=True, token="t1"),
            self._page(1000, 1000, header=False, token="t2"),
            self._page(2000, 500, header=False, token=None),
        ]
        res, mock_athena = await self._run(pages, 5000)

        assert res.row_count == 2500, f"expected all 2500 rows, got {res.row_count}"
        assert mock_athena.get_query_results.call_count == 3
        assert res.truncated is False
        # No row lost or duplicated at page boundaries.
        assert res.rows[0]["id"] == "0"
        assert res.rows[999]["id"] == "999"
        assert res.rows[1000]["id"] == "1000"
        assert res.rows[-1]["id"] == "2499"

    async def test_header_stripped_only_on_first_page(self):
        """Stripping rows[1:] on every page would drop one real row per page."""
        pages = [
            self._page(0, 3, header=True, token="t1"),
            self._page(3, 3, header=False, token=None),
        ]
        res, _ = await self._run(pages, 100)
        assert [r["id"] for r in res.rows] == ["0", "1", "2", "3", "4", "5"]

    async def test_stops_at_max_rows_and_flags_truncated(self):
        pages = [
            self._page(0, 1000, header=True, token="t1"),
            self._page(1000, 1000, header=False, token="t2"),
        ]
        res, mock_athena = await self._run(pages, 1500)
        assert res.row_count == 1500
        assert res.truncated is True
        assert mock_athena.get_query_results.call_count == 2

    async def test_single_page_needs_one_call(self):
        pages = [self._page(0, 5, header=True, token=None)]
        res, mock_athena = await self._run(pages, 1000)
        assert res.row_count == 5
        assert res.truncated is False
        assert mock_athena.get_query_results.call_count == 1

    async def test_empty_result_set(self):
        pages = [self._page(0, 0, header=True, token=None)]
        res, _ = await self._run(pages, 1000)
        assert res.row_count == 0
        assert res.truncated is False


@pytest.mark.unit
class TestInjectLimitTrailingComment:
    """Regression: a trailing ``-- comment`` line must not swallow the appended LIMIT.

    Every textual-append fallback in ``_inject_limit`` used to append
    `` LIMIT n`` to the last line. LLM-generated SQL frequently ends with a
    ``-- comment`` line, so the cap landed inside the comment: the query
    parsed fine server-side and executed WITHOUT the scan cap.
    """

    def test_no_limit_with_trailing_comment_appends_on_new_line(self):
        sql = "SELECT a FROM t\n-- assumption: default scope"
        out = AthenaQueryExecutor._inject_limit(sql, 1000)
        assert out.splitlines()[-1].strip() == "LIMIT 1000", out

    def test_parse_failure_with_trailing_comment_appends_on_new_line(self):
        sql = "NOT VALID SQL AT ALL\n-- trailing"
        out = AthenaQueryExecutor._inject_limit(sql, 500)
        assert out.splitlines()[-1].strip() == "LIMIT 500", out

    def test_normal_append_still_caps(self):
        out = AthenaQueryExecutor._inject_limit("SELECT a FROM t", 100)
        assert "LIMIT 100" in out

    def test_existing_outer_limit_within_cap_unchanged(self):
        sql = "SELECT a FROM t LIMIT 5"
        assert AthenaQueryExecutor._inject_limit(sql, 100) == sql


@pytest.mark.unit
class TestCustomConnectorCatalogResolution:
    """A custom-connector source resolves its own Lambda-backed catalog, and must
    NOT be put through the Glue-crawler name rewrite."""

    @staticmethod
    def _wire(mock_boto, mock_res, item):
        import json

        mock_athena = MagicMock()
        mock_boto.return_value = mock_athena
        mock_table = MagicMock()
        mock_res.return_value.Table.return_value = mock_table
        mock_table.query.return_value = {"Items": [{"configuration": json.dumps({}), **item}]}
        mock_table.get_item.return_value = {"Item": {"athenaWorkgroupName": f"{RESOURCE_PREFIX}-dev-ns-123"}}
        mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-conn"}
        mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
        mock_athena.get_query_results.return_value = {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": "cnt"}]},
                "Rows": [{"Data": [{"VarCharValue": "cnt"}]}, {"Data": [{"VarCharValue": "7"}]}],
            }
        }
        return mock_athena

    async def _execute(self, item, sql="SELECT COUNT(*) FROM orders"):
        with patch("boto3.client") as mock_boto, patch("boto3.resource") as mock_res:
            mock_athena = self._wire(mock_boto, mock_res, item)
            executor = AthenaQueryExecutor(region="us-east-1", sources_table="coa-sources")
            executor._sources._table_name = "coa-sources"
        await executor.execute(sql, namespace="ns-123")
        return mock_athena.start_query_execution.call_args[1]

    async def test_uses_the_lambda_catalog_and_discovered_database(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "CUSTOM_CONNECTOR",
                "athenaDataCatalogName": "coadevds_abc123",
                "athenaDatabase": "widgets",
                "discoveredSchemas": ["widgets"],
                "queryable": True,
            }
        )
        assert kwargs["QueryExecutionContext"]["Catalog"] == "coadevds_abc123"
        assert kwargs["QueryExecutionContext"]["Database"] == "widgets"

    # The rewrite strips a `{database}_` substring from every table name. For a
    # custom connector that is not a no-op but a corruption: `widgets_orders` is a
    # real table its connector exposes, and `orders` is one it has never heard of.
    async def test_does_not_rewrite_a_table_name_that_starts_with_the_database(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "CUSTOM_CONNECTOR",
                "athenaDataCatalogName": "coadevds_abc123",
                "athenaDatabase": "widgets",
                "discoveredSchemas": ["widgets"],
                "queryable": True,
            },
            sql="SELECT COUNT(*) FROM widgets_orders",
        )
        assert "widgets_orders" in kwargs["QueryString"]

    # A federated JDBC source still gets the rewrite it exists for.
    async def test_a_federated_jdbc_source_is_still_rewritten(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "JDBC_DATABASE",
                "athenaDataCatalogName": "coadevds_abc123",
                "glueConnectionName": "coadevds_abc123",
                "discoveredSchemas": ["public"],
                "queryable": True,
            },
            sql="SELECT COUNT(*) FROM BIRD_PUBLIC_INCOME",
        )
        assert '"income"' in kwargs["QueryString"]

    # Reachable when a scan discovers zero tables (an over-narrow filter, or a
    # connector exposing none). Falling back to a hardcoded "public" would target
    # a database that has nothing to do with this connector.
    async def test_falls_back_to_the_configured_database_when_none_were_discovered(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "CUSTOM_CONNECTOR",
                "athenaDataCatalogName": "coadevds_abc123",
                "athenaDatabase": "widgets",
                "queryable": True,
            }
        )
        assert kwargs["QueryExecutionContext"]["Database"] == "widgets"

    # The `athenaCatalog` fallback added for nested Glue sources also reaches a
    # custom-connector row that somehow lacks `athenaDataCatalogName` — create writes
    # both attributes to the same derived name, so such a row resolves to the right
    # Lambda catalog instead of falling through to the Glue-native path. What must
    # NOT follow from taking that fallback is the crawled-name rewrite: this is still
    # a custom connector, where the strip corrupts real table names.
    async def test_a_custom_connector_without_the_system_attribute_resolves_and_is_not_rewritten(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "CUSTOM_CONNECTOR",
                "athenaCatalog": "coadevds_abc123",
                "athenaDatabase": "widgets",
                "discoveredSchemas": ["widgets"],
                "queryable": True,
            },
            sql="SELECT COUNT(*) FROM widgets_orders",
        )
        assert kwargs["QueryExecutionContext"]["Catalog"] == "coadevds_abc123"
        assert kwargs["QueryExecutionContext"]["Database"] == "widgets"
        assert "widgets_orders" in kwargs["QueryString"]

    # ...while a federated JDBC source keeps its long-standing "public" default,
    # where the Athena database is a PostgreSQL schema rather than the source's own
    # database name.
    async def test_a_federated_jdbc_source_keeps_the_public_default(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "JDBC_DATABASE",
                "athenaDataCatalogName": "coadevds_abc123",
                "queryable": True,
            }
        )
        assert kwargs["QueryExecutionContext"]["Database"] == "public"

    # A JDBC source's configured `databaseName` is its DATABASE, while the federated
    # catalog is keyed by SCHEMA — so the configured-database fallback that serves the
    # other two kinds must not divert this one to `postgres`.
    async def test_a_federated_jdbc_source_ignores_its_configured_database_name(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "JDBC_DATABASE",
                "athenaDataCatalogName": "coadevds_abc123",
                "athenaDatabase": "postgres",
                "queryable": True,
            }
        )
        assert kwargs["QueryExecutionContext"]["Database"] == "public"


@pytest.mark.unit
class TestNestedGlueCatalogResolution:
    """A native Glue source whose database lives in a non-root catalog is addressed
    through that nested catalog, read from `athenaCatalog`.

    It is NOT read from `athenaDataCatalogName`: that attribute is system-managed and
    DELETE runs a Lake-Formation-admin teardown against the name it finds there, so a
    caller-declared catalog is deliberately kept out of it (see
    `database_routes._create_database_source`).
    """

    # Same DDB/Athena wiring as the custom-connector class; re-wrapped as a
    # staticmethod because reading it off the other class yields the plain function.
    _execute = TestCustomConnectorCatalogResolution._execute
    _wire = staticmethod(TestCustomConnectorCatalogResolution._wire)

    async def test_uses_the_declared_nested_catalog(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "GLUE_DATABASE",
                "athenaCatalog": "customer_fed_cat",
                "athenaDatabase": "sales",
                "discoveredSchemas": ["sales"],
                "queryable": True,
            }
        )
        assert kwargs["QueryExecutionContext"]["Catalog"] == "customer_fed_cat"
        assert kwargs["QueryExecutionContext"]["Database"] == "sales"

    async def test_falls_back_to_the_configured_database_when_none_were_discovered(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "GLUE_DATABASE",
                "athenaCatalog": "customer_fed_cat",
                "athenaDatabase": "sales",
                "queryable": True,
            }
        )
        assert kwargs["QueryExecutionContext"]["Catalog"] == "customer_fed_cat"
        # Not "public" — that is the federated-JDBC default and names nothing here.
        assert kwargs["QueryExecutionContext"]["Database"] == "sales"

    # Every DATABASE source records `athenaCatalog`, and for most it is the root
    # catalog. That means "no nested catalog", so it must not be sent as one — and
    # the source must stay on the Glue-native path.
    async def test_the_root_catalog_is_not_treated_as_a_nested_one(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "GLUE_DATABASE",
                "athenaCatalog": "AwsDataCatalog",
                "athenaDatabase": "sales",
                "queryable": True,
            }
        )
        assert kwargs["QueryExecutionContext"]["Catalog"] == "AwsDataCatalog"
        assert kwargs["QueryExecutionContext"]["Database"] == "sales"

    # A source with no catalog information at all still resolves its database from
    # the configuration blob, as it did before the nested-catalog branch existed.
    async def test_a_source_with_no_catalog_falls_back_to_the_configured_database(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "GLUE_DATABASE",
                "queryable": True,
            }
        )
        assert kwargs["QueryExecutionContext"]["Catalog"] == "AwsDataCatalog"

    # The rewrite strips a `{database}_` substring from every table name. A native
    # Glue source's R2RML names are generated from Glue table metadata, so they
    # already ARE the catalog's own names — there is no crawler-added prefix to
    # strip, and stripping one corrupts any table whose name begins with its
    # database name. `sales_orders` in database `sales` is a real table here, not a
    # prefixed alias for `orders`.
    async def test_a_nested_glue_source_is_not_rewritten(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "GLUE_DATABASE",
                "athenaCatalog": "customer_fed_cat",
                "athenaDatabase": "sales",
                "discoveredSchemas": ["sales"],
                "queryable": True,
            },
            sql="SELECT COUNT(*) FROM sales_orders",
        )
        assert "sales_orders" in kwargs["QueryString"]

    # ...while a genuinely federated JDBC source keeps the rewrite, which is the path
    # it was written for. Guards against fixing the above by disabling it everywhere.
    async def test_a_federated_jdbc_source_still_gets_the_rewrite(self):
        kwargs = await self._execute(
            {
                "sourceType": "DATABASE",
                "sourceSubType": "JDBC_DATABASE",
                "athenaDataCatalogName": "coadevds_abc123",
                "discoveredSchemas": ["sales"],
                "queryable": True,
            },
            sql="SELECT COUNT(*) FROM BIRD_SALES_ORDERS",
        )
        assert '"orders"' in kwargs["QueryString"]
