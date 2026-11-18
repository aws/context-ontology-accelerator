# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the SQL Firewall — safety and authorization gate for SQL execution."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import sqlglot
import structlog.testing
from coa_serve.tier2.cedar_authorizer import CedarDecision
from coa_serve.tier2.sql_firewall import (
    FirewallResult,
    NamespaceSQLScopeError,
    SQLFirewall,
    UnsafeSQLError,
    _references_a_table,
)

_DENY_SSN = {"columnDenylist": {"customers": ["ssn"]}}
_SSN = "Access denied to column(s): customers.ssn"
_WHOLE_ROW = "Access denied: whole-row reference may expose restricted columns of 'customers'"


@pytest.mark.unit
class TestFirewallResult:
    def test_frozen_dataclass(self):
        result = FirewallResult(denied=False, authorized_sql="SELECT 1")
        with pytest.raises(AttributeError):
            result.denied = True  # type: ignore[misc]


@pytest.mark.unit
class TestSQLFirewallValidate:
    def test_accepts_simple_select(self):
        fw = SQLFirewall()
        fw.validate("SELECT id, name FROM orders WHERE id > 5")

    def test_accepts_subquery(self):
        fw = SQLFirewall()
        fw.validate("SELECT * FROM (SELECT id FROM orders) sub WHERE sub.id > 1")

    def test_validate_redshift_dialect_accepts_engine_specific_sql(self):
        """SQL already transpiled to Redshift must validate with the redshift reader;
        ``APPROXIMATE COUNT(DISTINCT …)`` does not parse as trino."""
        fw = SQLFirewall()
        redshift_sql = "SELECT APPROXIMATE COUNT(DISTINCT customer) AS c FROM wi766.orders"
        with pytest.raises(UnsafeSQLError, match="Failed to parse SQL statement"):
            fw.validate(redshift_sql, dialect="trino")
        fw.validate(redshift_sql, dialect="redshift")

    def test_validate_dialect_still_blocks_non_select_before_parse(self):
        """The cheap statement-type gate runs regardless of dialect (pre-parse)."""
        fw = SQLFirewall()
        for d in ("trino", "postgres", "redshift"):
            with pytest.raises(UnsafeSQLError, match="Statement type not allowed"):
                fw.validate("DROP TABLE orders", dialect=d)

    def test_check_statement_type_rejects_dml_without_parsing(self):
        """check_statement_type is the dialect-independent fast gate used by the
        direct-JDBC path before the source (and thus dialect) is resolved."""
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Statement type not allowed"):
            fw.check_statement_type("DELETE FROM orders WHERE id = 1")
        # A SELECT passes the cheap gate (deeper parse checks happen in validate()).
        fw.check_statement_type("SELECT 1")

    def test_rejects_insert(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Statement type not allowed"):
            fw.validate("INSERT INTO orders VALUES (1, 'test')")

    def test_rejects_drop(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Statement type not allowed"):
            fw.validate("DROP TABLE orders")

    def test_rejects_delete(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Statement type not allowed"):
            fw.validate("DELETE FROM orders WHERE id = 1")

    def test_rejects_update(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Statement type not allowed"):
            fw.validate("UPDATE orders SET status = 'done'")

    def test_rejects_create(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Statement type not allowed"):
            fw.validate("CREATE TABLE foo (id INT)")

    def test_rejects_select_into(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Into"):
            fw.validate("SELECT * INTO new_table FROM orders")

    def test_rejects_comment_bypass(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Statement type not allowed"):
            fw.validate("/* SELECT */ DROP TABLE orders")

    def test_rejects_unparseable(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Failed to parse"):
            fw.validate("THIS IS NOT SQL AT ALL ;;; {{{}}")

    def test_rejects_dangerous_function_pg_read_file(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Function not allowed"):
            fw.validate("SELECT pg_read_file('/etc/passwd')")

    def test_rejects_dangerous_function_dblink(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Function not allowed: dblink"):
            fw.validate("SELECT dblink('host=evil.com', 'SELECT 1')")
        # In FROM it is a table function, refused before the function-name check.
        with pytest.raises(UnsafeSQLError, match=r"TableFunction\(dblink\)"):
            fw.validate("SELECT * FROM dblink('host=evil.com', 'SELECT 1')")

    def test_rejects_dangerous_function_pg_sleep(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Function not allowed"):
            fw.validate("SELECT pg_sleep(10)")

    @pytest.mark.parametrize(
        ("dialect", "sql"),
        [
            ("postgres", "SELECT setval('seq', 1)"),
            ("postgres", "SELECT nextval('seq')"),
            ("postgres", "SELECT pg_advisory_lock(1)"),
            ("postgres", "SELECT pg_logical_emit_message(true, 'coa', 'x') FROM orders LIMIT 1"),
            ("postgres", "SELECT pg_notify('channel', 'payload')"),
            ("mysql", "SELECT SLEEP(10)"),
            ("mysql", "SELECT BENCHMARK(1000000, MD5('x'))"),
            ("mysql", "SELECT LOAD_FILE('/etc/passwd')"),
            ("mysql", "SELECT GET_LOCK('resource', 10)"),
            ("postgres", "SELECT lo_create(12345) FROM orders LIMIT 1"),
            ("postgres", "SELECT lo_from_bytea(12345, 'payload') FROM orders LIMIT 1"),
            ("postgres", "SELECT lo_put(12345, 0, 'payload') FROM orders LIMIT 1"),
            ("tsql", "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Server=x').db.dbo.orders"),
        ],
    )
    def test_rejects_dialect_specific_side_effect_function(self, dialect: str, sql: str):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Function not allowed"):
            fw.validate(sql, dialect=dialect)

    def test_rejects_locking_select(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Unsafe operation 'Lock'"):
            fw.validate("SELECT * FROM orders FOR UPDATE", dialect="postgres")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders WITH (TABLOCKX)",
            "SELECT * FROM orders (TABLOCKX)",
            "SELECT * FROM orders TABLOCKX",
            "SELECT * FROM orders WITH (SERIALIZABLE)",
            "SELECT * FROM orders WITH (REPEATABLEREAD)",
            "SELECT * FROM orders WITH (READCOMMITTEDLOCK)",
            "SELECT * FROM orders /* outer /* inner */ AS */ TABLOCKX",
            "SELECT * FROM [orders]] AS] TABLOCKX",
            'SELECT * FROM dbo.fn(CAST(x AS "type\\") + safe) TABLOCKX',
            "SELECT NEXT VALUE FOR dbo.seq FROM orders",
        ],
    )
    def test_rejects_tsql_lock_or_sequence_side_effect(self, sql: str):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Unsafe operation"):
            fw.validate(sql, dialect="tsql")

    def test_accepts_tsql_read_only_nolock_hint(self):
        SQLFirewall().validate("SELECT * FROM orders WITH (NOLOCK)", dialect="tsql")
        SQLFirewall().validate("SELECT * FROM orders (NOLOCK)", dialect="tsql")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders AS TABLOCKX",
            "SELECT * FROM orders AS [TABLOCKX]",
            "SELECT * FROM orders [SERIALIZABLE]",
            "SELECT * FROM orders AS /* alias */ TABLOCKX",
        ],
    )
    def test_accepts_explicit_tsql_alias_named_like_hint(self, sql: str):
        SQLFirewall().validate(sql, dialect="tsql")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM dbo.order_rows(@TABLOCKX)",
            "SELECT * FROM dbo.order_rows('TABLOCKX')",
            "SELECT * FROM dbo.order_rows([TABLOCKX])",
        ],
    )
    def test_tsql_table_valued_function_arguments_named_like_hint_are_not_hints(self, sql: str):
        # Not read as a lock hint; refused because a table-valued function hides
        # the tables it reads (see TestTableFunctionsRefused).
        with pytest.raises(UnsafeSQLError, match=r"TableFunction\(dbo\.order_rows\)"):
            SQLFirewall().validate(sql, dialect="tsql")

    def test_comment_as_does_not_hide_bare_tsql_hint(self):
        with pytest.raises(UnsafeSQLError, match="LegacyTableHint"):
            SQLFirewall().validate("SELECT * FROM orders -- AS\nTABLOCKX", dialect="tsql")

    def test_rejects_mysql_executable_comment_but_accepts_string_literal(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="executable comments"):
            fw.validate("SELECT /*!50000 SLEEP(10), */ 1", dialect="mysql")
        with pytest.raises(UnsafeSQLError, match="executable comments"):
            fw.validate(
                "SELECT `metric\\` /*!50000 , SLEEP(10) */ FROM `orders`",
                dialect="mysql",
            )
        fw.validate("SELECT '/*!50000 SLEEP(10) */'", dialect="mysql")

    @pytest.mark.parametrize("sql", ["SELECT", "SELECT *", "WITH x AS (SELECT 1) SELECT"])
    def test_rejects_incomplete_select_shape(self, sql: str):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="SELECT"):
            fw.validate(sql)

    def test_accepts_union(self):
        fw = SQLFirewall()
        fw.validate("SELECT id FROM orders UNION ALL SELECT id FROM returns")

    def test_accepts_union_distinct(self):
        fw = SQLFirewall()
        fw.validate("SELECT id FROM orders UNION SELECT id FROM returns")

    def test_accepts_intersect(self):
        fw = SQLFirewall()
        fw.validate("SELECT id FROM orders INTERSECT SELECT id FROM returns")

    def test_accepts_except(self):
        fw = SQLFirewall()
        fw.validate("SELECT id FROM orders EXCEPT SELECT id FROM returns")

    def test_accepts_nested_union(self):
        fw = SQLFirewall()
        fw.validate("SELECT a FROM t1 UNION ALL SELECT b FROM t2 UNION ALL SELECT c FROM t3")

    def test_rejects_data_modifying_cte(self):
        """Deep scan catches DML hidden inside a CTE (bypass vector)."""
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Delete"):
            fw.validate("WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d")

    def test_rejects_insert_inside_union(self):
        """Regex pre-check catches INSERT even when combined with UNION."""
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Statement type not allowed"):
            fw.validate("INSERT INTO evil SELECT id FROM orders UNION ALL SELECT id FROM returns")


@pytest.mark.unit
class TestSQLFirewallEvaluate:
    def test_allows_simple_select(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM orders")
        assert not result.denied
        assert result.authorized_sql == "SELECT * FROM orders"
        assert result.reason is None

    def test_allows_complex_query(self):
        fw = SQLFirewall()
        sql = "SELECT o.id, c.name FROM orders o JOIN customers c ON o.cust_id = c.id WHERE o.total > 100"
        result = fw.evaluate(sql)
        assert not result.denied
        assert result.authorized_sql == sql

    def test_allows_with_empty_profile(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT id FROM orders", profile={})
        assert not result.denied

    def test_allows_with_none_profile(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT id FROM orders", profile=None)
        assert not result.denied

    def test_empty_profile_fail_open_characterization(self):
        # An empty (or None) profile is intentionally fail-open: no data-level restriction.
        fw = SQLFirewall()
        assert not fw.evaluate("SELECT * FROM payroll", profile={}).denied
        assert not fw.evaluate("SELECT * FROM payroll", profile=None).denied

    def test_allows_with_populated_profile(self):
        fw = SQLFirewall()
        profile = {"user_id": "u1", "roles": ["analyst"], "department": "finance"}
        result = fw.evaluate("SELECT * FROM revenue", profile=profile)
        assert not result.denied
        assert result.authorized_sql == "SELECT * FROM revenue"

    def test_allows_aggregate_query(self):
        fw = SQLFirewall()
        sql = "SELECT region, SUM(amount) FROM orders GROUP BY region"
        result = fw.evaluate(sql)
        assert not result.denied
        assert result.authorized_sql == sql

    def test_sql_passthrough_unchanged(self):
        """Steel thread: authorized_sql must be identical to input."""
        fw = SQLFirewall()
        sql = "SELECT sum(amount) AS total_revenue FROM orders WHERE year = 2024"
        result = fw.evaluate(sql)
        assert result.authorized_sql == sql

    def test_evaluate_raises_on_unsafe_sql(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError):
            fw.evaluate("DROP TABLE orders")

    def test_evaluate_threads_dialect_to_validate(self):
        """evaluate() must pass ``dialect`` through to validate() so an already-
        transpiled engine-specific statement isn't rejected as unparseable trino.
        ``APPROXIMATE COUNT(DISTINCT …)`` is Redshift-only syntax."""
        from coa_serve.tier2.cedar_authorizer import NullCedarAuthorizer

        fw = SQLFirewall(cedar_authorizer=NullCedarAuthorizer())
        redshift_sql = "SELECT APPROXIMATE COUNT(DISTINCT customer) AS c FROM orders"
        # Default (trino) reader can't parse it → spurious safety rejection.
        with pytest.raises(UnsafeSQLError, match="Failed to parse SQL statement"):
            fw.evaluate(redshift_sql, profile={})
        # Redshift reader parses it cleanly → allowed.
        assert not fw.evaluate(redshift_sql, profile={}, dialect="redshift").denied


@pytest.mark.unit
class TestSQLFirewallExtractTables:
    def test_single_table(self):
        tables = SQLFirewall.extract_tables("SELECT * FROM orders")
        assert tables == ["orders"]

    def test_multiple_tables_via_join(self):
        tables = SQLFirewall.extract_tables("SELECT o.id FROM orders o JOIN customers c ON o.cust_id = c.id")
        assert "orders" in tables
        assert "customers" in tables

    def test_schema_qualified_table(self):
        tables = SQLFirewall.extract_tables("SELECT * FROM public.orders")
        assert "public.orders" in tables

    def test_subquery_table(self):
        tables = SQLFirewall.extract_tables(
            "SELECT * FROM (SELECT id FROM orders) sub JOIN products p ON sub.id = p.order_id"
        )
        assert "orders" in tables
        assert "products" in tables

    def test_no_duplicate_tables(self):
        tables = SQLFirewall.extract_tables("SELECT * FROM orders o1 JOIN orders o2 ON o1.id = o2.parent_id")
        assert tables.count("orders") == 1

    def test_empty_on_parse_error(self):
        """Parse failures should return empty list, never raise."""
        tables = SQLFirewall.extract_tables("THIS IS NOT SQL AT ALL {{{}}")
        assert tables == []

    def test_cte_table(self):
        sql = (
            "WITH active AS (SELECT * FROM users WHERE status = 'active') "
            "SELECT * FROM active JOIN orders ON active.id = orders.user_id"
        )
        tables = SQLFirewall.extract_tables(sql)
        assert "users" in tables
        assert "orders" in tables

    def test_union_tables(self):
        sql = "SELECT id FROM orders UNION ALL SELECT id FROM returns"
        tables = SQLFirewall.extract_tables(sql)
        assert "orders" in tables
        assert "returns" in tables


@pytest.mark.unit
class TestSQLFirewallTableAllowlist:
    """Test table-level enforcement via the grant profile's tableAllowlist."""

    def test_allows_table_in_allowlist(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT id FROM orders", profile={"tableAllowlist": ["orders", "customers"]})
        assert not result.denied

    def test_denies_table_not_in_allowlist(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM payroll", profile={"tableAllowlist": ["orders"]})
        assert result.denied
        assert "payroll" in result.reason

    def test_denies_when_any_joined_table_not_allowed(self):
        fw = SQLFirewall()
        sql = "SELECT o.id FROM orders o JOIN payroll p ON o.id = p.order_id"
        result = fw.evaluate(sql, profile={"tableAllowlist": ["orders"]})
        assert result.denied
        assert "payroll" in result.reason

    def test_allowlist_matches_bare_name_of_schema_qualified_table(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT id FROM public.orders", profile={"tableAllowlist": ["orders"]})
        assert not result.denied

    def test_cte_name_not_treated_as_table(self):
        fw = SQLFirewall()
        sql = "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent"
        result = fw.evaluate(sql, profile={"tableAllowlist": ["orders"]})
        assert not result.denied

    def test_no_allowlist_allows_any_table(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM anything", profile={"role": "owner"})
        assert not result.denied


@pytest.mark.unit
class TestSQLFirewallColumnDenylist:
    """Test column-level enforcement via the grant profile's columnDenylist."""

    def test_denies_qualified_denied_column(self):
        fw = SQLFirewall()
        sql = "SELECT c.name, c.ssn FROM customers c"
        result = fw.evaluate(sql, profile={"columnDenylist": {"customers": ["ssn"]}})
        assert result.denied
        assert "customers.ssn" in result.reason

    def test_denies_unqualified_denied_column(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT ssn FROM customers", profile={"columnDenylist": {"customers": ["ssn"]}})
        assert result.denied

    def test_denies_select_star_over_restricted_table(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM customers", profile={"columnDenylist": {"customers": ["ssn"]}})
        assert result.denied
        assert "SELECT *" in result.reason

    def test_denies_qualified_star_over_restricted_table(self):
        fw = SQLFirewall()
        sql = "SELECT c.* FROM customers c"
        result = fw.evaluate(sql, profile={"columnDenylist": {"customers": ["ssn"]}})
        assert result.denied

    def test_allows_when_denied_columns_not_referenced(self):
        fw = SQLFirewall()
        sql = "SELECT name, email FROM customers"
        result = fw.evaluate(sql, profile={"columnDenylist": {"customers": ["ssn"]}})
        assert not result.denied

    def test_allows_count_star_over_restricted_table(self):
        """COUNT(*) does not project columns, so it must not trip the star rule."""
        fw = SQLFirewall()
        result = fw.evaluate("SELECT COUNT(*) FROM customers", profile={"columnDenylist": {"customers": ["ssn"]}})
        assert not result.denied

    def test_ignores_denylist_for_table_not_in_query(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT id FROM orders", profile={"columnDenylist": {"customers": ["ssn"]}})
        assert not result.denied

    def test_combined_allowlist_and_denylist(self):
        fw = SQLFirewall()
        profile = {"tableAllowlist": ["orders", "customers"], "columnDenylist": {"customers": ["ssn"]}}
        sql = "SELECT o.id, c.name FROM orders o JOIN customers c ON o.cust_id = c.id"
        result = fw.evaluate(sql, profile=profile)
        assert not result.denied

    def test_unsafe_sql_still_raises_with_restricted_profile(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError):
            fw.evaluate("DROP TABLE customers", profile={"tableAllowlist": ["orders"]})


@pytest.mark.unit
class TestSQLFirewallCaseInsensitivity:
    """Authorization must be case-insensitive — SQL identifiers are case-insensitive
    for unquoted names and case manipulation must not bypass the allowlist/denylist."""

    def test_allowlist_uppercase_query_lowercase_profile(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM ORDERS", profile={"tableAllowlist": ["orders"]})
        assert not result.denied

    def test_allowlist_mixed_case_query_lowercase_profile(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT id FROM Orders", profile={"tableAllowlist": ["orders"]})
        assert not result.denied

    def test_allowlist_lowercase_query_uppercase_profile(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT id FROM orders", profile={"tableAllowlist": ["ORDERS"]})
        assert not result.denied

    def test_allowlist_denies_unauthorized_regardless_of_case(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM PAYROLL", profile={"tableAllowlist": ["orders"]})
        assert result.denied
        assert "payroll" in result.reason.lower()

    def test_allowlist_schema_qualified_uppercase(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT id FROM PUBLIC.ORDERS", profile={"tableAllowlist": ["orders"]})
        assert not result.denied

    def test_denylist_uppercase_column_lowercase_profile(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT SSN FROM customers", profile={"columnDenylist": {"customers": ["ssn"]}})
        assert result.denied

    def test_denylist_mixed_case_column_lowercase_profile(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT Ssn FROM customers", profile={"columnDenylist": {"customers": ["ssn"]}})
        assert result.denied

    def test_denylist_lowercase_column_uppercase_profile(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT ssn FROM customers", profile={"columnDenylist": {"customers": ["SSN"]}})
        assert result.denied

    def test_denylist_uppercase_table_in_profile(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT ssn FROM customers", profile={"columnDenylist": {"CUSTOMERS": ["ssn"]}})
        assert result.denied

    def test_denylist_qualified_column_case_insensitive(self):
        fw = SQLFirewall()
        sql = "SELECT C.SSN FROM customers C"
        result = fw.evaluate(sql, profile={"columnDenylist": {"customers": ["ssn"]}})
        assert result.denied

    def test_denylist_select_star_uppercase_table(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM CUSTOMERS", profile={"columnDenylist": {"customers": ["ssn"]}})
        assert result.denied

    def test_denylist_allows_when_uppercase_safe_column(self):
        fw = SQLFirewall()
        sql = "SELECT NAME, EMAIL FROM customers"
        result = fw.evaluate(sql, profile={"columnDenylist": {"customers": ["ssn"]}})
        assert not result.denied


@pytest.mark.unit
class TestSQLFirewallProfileTypeValidation:
    """Malformed grant profiles (wrong types) must fail closed deterministically
    rather than iterating over string characters or raising AttributeError."""

    def test_string_table_allowlist_is_denied(self):
        fw = SQLFirewall()
        # If allowlist were treated as iterable string, set() would yield {'o','r','d','e','r','s'}
        # — this must not be allowed to pass. Fail closed.
        result = fw.evaluate("SELECT * FROM orders", profile={"tableAllowlist": "orders"})
        assert result.denied
        assert "tableAllowlist" in result.reason

    def test_int_table_allowlist_is_denied(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM orders", profile={"tableAllowlist": 123})
        assert result.denied
        assert "tableAllowlist" in result.reason

    def test_dict_table_allowlist_is_denied(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM orders", profile={"tableAllowlist": {"orders": True}})
        assert result.denied
        assert "tableAllowlist" in result.reason

    def test_tuple_table_allowlist_is_accepted(self):
        """Tuples are sequence-like and should be accepted alongside lists."""
        fw = SQLFirewall()
        result = fw.evaluate("SELECT id FROM orders", profile={"tableAllowlist": ("orders",)})
        assert not result.denied

    def test_list_column_denylist_is_denied(self):
        fw = SQLFirewall()
        # If denylist were a list, .items() would AttributeError. Fail closed instead.
        result = fw.evaluate("SELECT * FROM customers", profile={"columnDenylist": ["ssn"]})
        assert result.denied
        assert "columnDenylist" in result.reason

    def test_string_column_denylist_is_denied(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM customers", profile={"columnDenylist": "ssn"})
        assert result.denied
        assert "columnDenylist" in result.reason

    def test_column_denylist_with_string_value_is_denied(self):
        """Each table's denied-columns must be a list, not a string."""
        fw = SQLFirewall()
        result = fw.evaluate(
            "SELECT id FROM customers",
            profile={"columnDenylist": {"customers": "ssn"}},
        )
        assert result.denied
        assert "columnDenylist" in result.reason

    def test_empty_list_allowlist_denies_every_table(self):
        """An empty allowlist permits no tables; absence of the key means unrestricted."""
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM orders", profile={"tableAllowlist": []})
        assert result.denied
        assert "orders" in (result.reason or "")

    def test_absent_allowlist_imposes_no_restriction(self):
        """The counterpart to the above: absence really does mean unrestricted."""
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM orders", profile={})
        assert not result.denied

    def test_empty_dict_denylist_treated_as_unset(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM customers", profile={"columnDenylist": {}})
        assert not result.denied


@pytest.mark.unit
class TestCedarAuthorizationSeam:
    """Cedar policy gate — additional deny layer, default allow-all."""

    def test_default_authorizer_allows_empty_profile(self):
        """Default functional Cedar evaluator allows when the profile has no scopes."""
        fw = SQLFirewall()
        result = fw.evaluate("SELECT * FROM orders", profile={}, namespace="ns")
        assert not result.denied

    def test_cedar_deny_is_terminal(self):
        """A Cedar DENY blocks even SQL that passes structural checks."""
        from coa_serve.tier2.cedar_authorizer import CedarDecision

        class _DenyAll:
            def authorize(self, *, namespace, profile, tables):
                return CedarDecision(allowed=False, reason="policy: no read on namespace")

        fw = SQLFirewall(cedar_authorizer=_DenyAll())
        result = fw.evaluate("SELECT * FROM orders", profile={}, namespace="ns")
        assert result.denied
        assert result.authorized_sql == ""

    def test_cedar_evaluator_error_fails_closed(self):
        """An authorizer that raises must DENY (fail-closed), never allow."""

        class _Boom:
            def authorize(self, *, namespace, profile, tables):
                raise RuntimeError("policy store unreachable")

        fw = SQLFirewall(cedar_authorizer=_Boom())
        result = fw.evaluate("SELECT * FROM orders", profile={}, namespace="ns")
        assert result.denied

    def test_cedar_runs_after_structural_allow_with_restrictive_profile(self):
        """Cedar still consulted on the restricted-profile allow path."""
        from coa_serve.tier2.cedar_authorizer import CedarDecision

        seen = {}

        class _Recorder:
            def authorize(self, *, namespace, profile, tables):
                seen["tables"] = tables
                seen["namespace"] = namespace
                return CedarDecision(allowed=True)

        fw = SQLFirewall(cedar_authorizer=_Recorder())
        result = fw.evaluate("SELECT id FROM orders", profile={"tableAllowlist": ["orders"]}, namespace="ns-1")
        assert not result.denied
        assert "orders" in seen["tables"]
        assert seen["namespace"] == "ns-1"

    def test_cedar_namespace_falls_back_to_profile(self):
        """When evaluate() gets no namespace arg, Cedar reads it from the profile."""
        from coa_serve.tier2.cedar_authorizer import CedarDecision

        seen = {}

        class _Recorder:
            def authorize(self, *, namespace, profile, tables):
                seen["namespace"] = namespace
                return CedarDecision(allowed=True)

        fw = SQLFirewall(cedar_authorizer=_Recorder())
        fw.evaluate("SELECT id FROM orders", profile={"namespace": "from-profile"})
        assert seen["namespace"] == "from-profile"


@pytest.mark.unit
class TestRealCedarThroughFirewall:
    """REAL cedarpy evaluation (namespace 'query' admission) via the firewall."""

    def _fw(self, fail_open_no_roles=False):
        from coa_serve.tier2.cedar_authorizer import RealCedarAuthorizer

        return SQLFirewall(cedar_authorizer=RealCedarAuthorizer(fail_open_no_roles=fail_open_no_roles))

    def test_data_analyst_on_own_namespace_allows(self):
        fw = self._fw(fail_open_no_roles=False)
        result = fw.evaluate(
            "SELECT id FROM orders",
            profile={"userId": "u1", "resourceRoles": [{"role": "data-analyst", "resourceUID": "ns"}]},
            namespace="ns",
        )
        assert not result.denied

    def test_data_analyst_on_other_namespace_denies(self):
        fw = self._fw(fail_open_no_roles=False)
        result = fw.evaluate(
            "SELECT id FROM orders",
            profile={"userId": "u1", "resourceRoles": [{"role": "data-analyst", "resourceUID": "other"}]},
            namespace="ns",
        )
        assert result.denied

    def test_platform_admin_allows(self):
        fw = self._fw(fail_open_no_roles=False)
        result = fw.evaluate(
            "SELECT id FROM orders", profile={"userId": "a", "globalRoles": ["platform-admin"]}, namespace="ns"
        )
        assert not result.denied

    def test_no_roles_prod_fails_closed(self):
        fw = self._fw(fail_open_no_roles=False)
        result = fw.evaluate("SELECT id FROM orders", profile={"userId": "u1"}, namespace="ns")
        assert result.denied

    def test_no_roles_dev_fails_open(self):
        fw = self._fw(fail_open_no_roles=True)
        result = fw.evaluate("SELECT id FROM orders", profile={"userId": "u1"}, namespace="ns")
        assert not result.denied

    def test_comma_joined_roles_accepted(self):
        """Roles forwarded as a comma-joined string (authorizer style) still evaluate."""
        fw = self._fw(fail_open_no_roles=False)
        result = fw.evaluate(
            "SELECT id FROM orders", profile={"userId": "a", "globalRoles": "platform-admin"}, namespace="ns"
        )
        assert not result.denied

    def test_null_authorizer_optout_allows(self):
        from coa_serve.tier2.cedar_authorizer import NullCedarAuthorizer

        fw = SQLFirewall(cedar_authorizer=NullCedarAuthorizer())
        # Cedar layer opted out → no namespace admission check (structural checks
        # still apply, but none set here).
        result = fw.evaluate("SELECT id FROM orders", profile={"userId": "u1"}, namespace="ns")
        assert not result.denied

    @pytest.mark.parametrize("probe", ["can_view_source", "can_view_ontology"])
    def test_schema_view_steward_own_namespace_true(self, probe):
        fw = self._fw(fail_open_no_roles=False)
        profile = {"userId": "u1", "resourceRoles": [{"role": "data-steward", "resourceUID": "ns"}]}
        assert getattr(fw, probe)("ns", profile) is True

    @pytest.mark.parametrize("probe", ["can_view_source", "can_view_ontology"])
    def test_schema_view_data_analyst_false(self, probe):
        fw = self._fw(fail_open_no_roles=False)
        profile = {"userId": "u1", "resourceRoles": [{"role": "data-analyst", "resourceUID": "ns"}]}
        assert getattr(fw, probe)("ns", profile) is False

    @pytest.mark.parametrize("probe", ["can_view_source", "can_view_ontology"])
    def test_schema_view_no_roles_prod_false(self, probe):
        fw = self._fw(fail_open_no_roles=False)
        assert getattr(fw, probe)("ns", {"userId": "u1"}) is False

    @pytest.mark.parametrize("probe", ["can_view_source", "can_view_ontology"])
    def test_schema_view_fails_closed_on_authorizer_error(self, probe):
        """A raising authorizer withholds artifacts (returns False), never leaks."""

        class _BoomAuthorizer:
            def authorize(self, **kwargs):
                raise RuntimeError("boom")

            def can_view_source(self, **kwargs):
                raise RuntimeError("boom")

            def can_view_ontology(self, **kwargs):
                raise RuntimeError("boom")

        fw = SQLFirewall(cedar_authorizer=_BoomAuthorizer())
        assert getattr(fw, probe)("ns", {"userId": "a", "globalRoles": ["platform-admin"]}) is False

    @pytest.mark.parametrize("probe", ["can_view_source", "can_view_ontology"])
    def test_schema_view_namespace_falls_back_to_profile(self, probe):
        """With no namespace argument the probe evaluates the profile's namespace."""
        seen: dict = {}

        class _RecordingAuthorizer:
            def authorize(self, **kwargs):
                raise AssertionError("not used")

            def can_view_source(self, *, namespace, profile):
                seen["ns"] = namespace
                return True

            can_view_ontology = can_view_source

        fw = SQLFirewall(cedar_authorizer=_RecordingAuthorizer())
        assert getattr(fw, probe)("", {"namespace": "from-profile"}) is True
        assert seen["ns"] == "from-profile"


@pytest.mark.unit
class TestNamespaceSQLScope:
    """Qualified SQL may not escape the requested namespace's sources."""

    _NATIVE = frozenset({"tenant_a_db"})
    _FEDERATED = frozenset({("sclds_a", "sales")})

    def _validate(self, sql: str, default_catalog: str = "AwsDataCatalog") -> bool:
        return SQLFirewall.validate_namespace_sql_scope(
            sql,
            native_databases=self._NATIVE,
            federated_catalog_schemas=self._FEDERATED,
            default_catalog=default_catalog,
        )

    def test_owned_native_database_is_allowed(self):
        assert self._validate("SELECT * FROM AwsDataCatalog.tenant_a_db.customers")

    def test_owned_federated_catalog_schema_is_allowed(self):
        assert self._validate("SELECT * FROM sclds_a.sales.customers")

    def test_bare_table_uses_namespace_context_without_inventory_lookup(self):
        assert not self._validate("SELECT * FROM customers")

    def test_foreign_native_database_is_denied_even_when_table_name_matches(self):
        with pytest.raises(NamespaceSQLScopeError, match="not available in the requested namespace"):
            self._validate("SELECT * FROM AwsDataCatalog.tenant_b_db.customers")

    def test_foreign_federated_catalog_is_denied(self):
        with pytest.raises(NamespaceSQLScopeError, match="not available in the requested namespace"):
            self._validate("SELECT * FROM sclds_b.sales.customers")

    def test_two_part_foreign_database_is_denied_under_native_context(self):
        with pytest.raises(NamespaceSQLScopeError, match="not available in the requested namespace"):
            self._validate("SELECT * FROM tenant_b_db.customers")

    def test_information_schema_enumeration_is_denied(self):
        with pytest.raises(NamespaceSQLScopeError, match="not available in the requested namespace"):
            self._validate("SELECT * FROM information_schema.tables")

    def _validate_jdbc(self, sql: str, selected_source_schemas: frozenset[str] | None = None) -> bool:
        """Direct-JDBC route: catalog supplied by the connection, so schema_only."""
        return SQLFirewall.validate_namespace_sql_scope(
            sql,
            native_databases=self._NATIVE,
            federated_catalog_schemas=self._FEDERATED,
            default_catalog="awsdatacatalog",
            schema_only=True,
            selected_source_schemas=(
                selected_source_schemas if selected_source_schemas is not None else frozenset({"tenant_a_db", "sales"})
            ),
        )

    def test_jdbc_two_part_federated_schema_is_allowed(self):
        """A bare ``schema.table`` on direct JDBC takes its catalog from the connection.

        schema_only authorizes the schema from the selected source without an Athena catalog pin."""
        assert self._validate_jdbc("SELECT COUNT(*) FROM sales.orders")

    def test_jdbc_two_part_schema_is_bound_to_executing_source(self):
        """A namespace sibling's schema is not authorized on source A's connection."""
        namespace_native = frozenset()
        namespace_federated = frozenset({("source_a", "public"), ("source_b", "secret")})

        def validate(sql: str, selected_source_schemas: frozenset[str]) -> bool:
            return SQLFirewall.validate_namespace_sql_scope(
                sql,
                native_databases=namespace_native,
                federated_catalog_schemas=namespace_federated,
                default_catalog="awsdatacatalog",
                schema_only=True,
                selected_source_schemas=selected_source_schemas,
            )

        assert validate("SELECT * FROM public.customers", frozenset({"public"}))
        assert validate("SELECT * FROM secret.salaries", frozenset({"secret"}))
        with pytest.raises(NamespaceSQLScopeError):
            validate("SELECT * FROM secret.salaries", frozenset({"public"}))

    def test_jdbc_two_part_native_schema_is_allowed(self):
        assert self._validate_jdbc("SELECT COUNT(*) FROM tenant_a_db.customers")

    def test_jdbc_two_part_foreign_schema_is_still_denied(self):
        """schema_only relaxes the CATALOG pin, not the schema boundary: a schema
        the namespace does not own is still denied."""
        with pytest.raises(NamespaceSQLScopeError, match="not available in the requested namespace"):
            self._validate_jdbc("SELECT COUNT(*) FROM secret_hr.salaries")

    def test_jdbc_explicit_three_part_still_catalog_strict(self):
        """An explicit catalog on the JDBC route is still checked catalog-strict —
        schema_only only affects the unqualified-catalog case."""
        with pytest.raises(NamespaceSQLScopeError, match="not available in the requested namespace"):
            self._validate_jdbc("SELECT * FROM sclds_b.sales.customers")

    def _validate_redshift(self, sql: str) -> bool:
        """The Redshift executor's re-check of its rewritten statement."""
        return SQLFirewall.validate_namespace_sql_scope(
            sql,
            native_databases=self._NATIVE,
            federated_catalog_schemas=frozenset(),
            default_catalog="awsdatacatalog",
            dialect="redshift",
        )

    def test_redshift_dialect_allows_quoted_lowercase_owned_database(self):
        assert self._validate_redshift('SELECT * FROM "awsdatacatalog"."tenant_a_db"."customers"')

    def test_redshift_dialect_denies_quoted_lowercase_foreign_database(self):
        with pytest.raises(NamespaceSQLScopeError, match="tenant_b_db"):
            self._validate_redshift('SELECT * FROM "awsdatacatalog"."tenant_b_db"."customers"')

    def test_dialect_is_passed_through_to_the_parser(self):
        # TOP is Redshift syntax, not Trino: it only parses (and so is only checked)
        # when the redshift dialect actually reaches sqlglot.
        sql = "SELECT TOP 5 * FROM awsdatacatalog.tenant_b_db.customers"
        with pytest.raises(NamespaceSQLScopeError, match="Unable to verify SQL references"):
            self._validate(sql)
        with pytest.raises(NamespaceSQLScopeError, match="tenant_b_db"):
            self._validate_redshift(sql)
        assert self._validate_redshift("SELECT TOP 5 * FROM awsdatacatalog.tenant_a_db.customers")


@pytest.mark.unit
class TestParseErrorLogsOmitSql:
    """CWE-532: a firewall parse failure must not log the SQL text, which can
    carry caller-supplied literal values (potential PII)."""

    _SQL = "SELECT * FROM orders WHERE email = 'alice@example.com' AND (("

    def test_analyze_refs_parse_error_logs_length_not_sql(self):
        with structlog.testing.capture_logs() as logs:
            assert SQLFirewall._analyze_refs(self._SQL) is None
        event = next(log for log in logs if log["event"] == "sql_firewall_parse_error")
        assert event["sql_len"] == len(self._SQL)
        assert "alice@example.com" not in repr(logs)

    def test_extract_tables_parse_error_logs_length_not_sql(self):
        with structlog.testing.capture_logs() as logs:
            assert SQLFirewall.extract_tables(self._SQL) == []
        event = next(log for log in logs if log["event"] == "sql_firewall_parse_error")
        assert event["sql_len"] == len(self._SQL)
        assert "alice@example.com" not in repr(logs)


@pytest.mark.unit
class TestSameNamedTableGrants:
    """Two databases holding a table of one name: a database-qualified grant tells them apart."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM crm_db.customers",
            'SELECT id FROM "CRM_DB"."customers"',
            'SELECT id FROM "AwsDataCatalog"."crm_db"."customers"',
        ],
    )
    def test_qualified_entry_allows_that_database_in_every_spelling(self, sql):
        result = SQLFirewall().evaluate(sql, profile={"tableAllowlist": ["crm_db.customers"]})
        assert not result.denied

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM sales_db.customers",
            "SELECT id FROM customers",
            "SELECT id FROM othercat.crm_db.customers",
            "SELECT c.id FROM crm_db.customers c JOIN sales_db.customers s ON c.id = s.id",
        ],
        ids=["other-database", "bare", "other-catalog", "join-with-the-other"],
    )
    def test_qualified_entry_denies_every_other_reference(self, sql):
        result = SQLFirewall().evaluate(sql, profile={"tableAllowlist": ["crm_db.customers"]})
        assert result.denied
        assert "customers" in result.reason

    def test_root_catalog_entry_reads_as_its_database_entry(self):
        profile = {"tableAllowlist": ["AwsDataCatalog.crm_db.customers"]}
        assert not SQLFirewall().evaluate("SELECT id FROM crm_db.customers", profile=profile).denied
        assert SQLFirewall().evaluate("SELECT id FROM sales_db.customers", profile=profile).denied

    @pytest.mark.parametrize(
        "sql",
        ["SELECT id FROM customers", "SELECT id FROM crm_db.customers", "SELECT id FROM sales_db.customers"],
    )
    def test_bare_entry_still_covers_every_same_named_table(self, sql):
        # Compatibility: an existing bare grant behaves exactly as before.
        assert not SQLFirewall().evaluate(sql, profile={"tableAllowlist": ["customers"]}).denied

    def test_qualified_denylist_key_applies_only_to_that_database(self):
        profile = {"columnDenylist": {"crm_db.customers": ["points"]}}
        denied = SQLFirewall().evaluate("SELECT points FROM crm_db.customers", profile=profile)
        assert denied.denied
        assert "points" in denied.reason
        assert not SQLFirewall().evaluate("SELECT points FROM sales_db.customers", profile=profile).denied

    def test_bare_denylist_key_still_applies_to_every_same_named_table(self):
        profile = {"columnDenylist": {"customers": ["points"]}}
        for sql in ("SELECT points FROM crm_db.customers", "SELECT points FROM sales_db.customers"):
            assert SQLFirewall().evaluate(sql, profile=profile).denied

    def test_cedar_receives_one_spelling_per_table(self):
        from coa_serve.tier2.cedar_authorizer import CedarDecision

        seen: dict[str, list[str]] = {}

        class _Recorder:
            def authorize(self, *, namespace, profile, tables):
                seen["tables"] = tables
                return CedarDecision(allowed=True)

        fw = SQLFirewall(cedar_authorizer=_Recorder())
        for sql in (
            "SELECT id FROM crm_db.customers",
            'SELECT id FROM "AwsDataCatalog"."CRM_DB"."customers"',
        ):
            assert not fw.evaluate(sql, profile={}, namespace="ns").denied
            assert seen["tables"] == ["crm_db.customers"]

    def test_trace_filter_matches_the_gate(self):
        from coa_serve.tier2.sql_firewall import tables_visible_to

        profile = {"tableAllowlist": ["crm_db.customers", "orders"]}
        names = ["crm_db.customers", "sales_db.customers", "customers", "orders", "x.orders"]
        assert tables_visible_to(names, profile) == ["crm_db.customers", "orders", "x.orders"]


@pytest.mark.unit
class TestSchemaEchoRefused:
    """A statement that can only echo text from its own SQL is refused without viewSource.

    Real seed policies: the analyst holds neither viewSource nor viewOntology; the
    steward and the owner hold both and are the positive controls.
    """

    _NS = "demo"
    _NO_TABLE = "Query must read from a data source"
    _CONSTANT = "Query must return data from a data source"

    def _fw(self):
        from coa_serve.tier2.cedar_authorizer import RealCedarAuthorizer

        return SQLFirewall(cedar_authorizer=RealCedarAuthorizer(fail_open_no_roles=False))

    def _profile(self, role):
        return {"userId": "u", "resourceRoles": [{"role": role, "resourceUID": self._NS}]}

    def _eval(self, sql, role, dialect="trino"):
        return self._fw().evaluate(sql, profile=self._profile(role), namespace=self._NS, dialect=dialect)

    @pytest.mark.parametrize(
        ("sql", "dialect", "reason"),
        [
            ("SELECT 'sales_db.customers' AS t UNION ALL SELECT 'x'", "trino", _NO_TABLE),
            ("WITH t AS (SELECT 'sales.orders' AS n) SELECT n FROM t", "trino", _NO_TABLE),
            (
                "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r",
                "postgres",
                _NO_TABLE,
            ),
            ("SELECT x FROM (VALUES ('sales.orders'), ('b')) AS v(x)", "trino", _NO_TABLE),
            ("SELECT x FROM UNNEST(ARRAY['sales.orders']) AS u(x)", "trino", _NO_TABLE),
            ("SELECT * FROM generate_series(1, 3)", "postgres", _NO_TABLE),
            # Trino/Athena bind these references to the CTE, so nothing stored is read.
            ("WITH X AS (SELECT 'leak' AS c) SELECT c FROM x", "trino", _NO_TABLE),
            ("WITH \"X\" AS (SELECT 'leak' AS c) SELECT c FROM x", "trino", _NO_TABLE),
            ("WITH x AS (SELECT 'leak' AS c) SELECT c FROM \"X\"", "trino", _NO_TABLE),
            ("WITH RECURSIVE a AS (SELECT c FROM b), b AS (SELECT 'leak' AS c) SELECT c FROM a", "postgres", _NO_TABLE),
            ("SELECT 'customer_id' AS c FROM orders", "trino", _CONSTANT),
            ("SELECT 'customer_id' AS c FROM orders UNION ALL SELECT name FROM orders", "trino", _CONSTANT),
            ("SELECT CHR(99) || CHR(117) AS c FROM orders", "trino", _CONSTANT),
        ],
    )
    def test_refused_for_analyst_with_reason(self, sql, dialect, reason):
        res = self._eval(sql, "data-analyst", dialect)
        assert res.denied
        assert res.reason == reason
        for role in ("data-steward", "namespace-owner"):
            assert not self._eval(sql, role, dialect).denied, role

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT COUNT(*) FROM t",
            "SELECT id, name FROM orders WHERE total > 10",
            "SELECT 1 FROM orders",
            "SELECT 'Total' AS label, SUM(amount) AS s FROM orders",
            "WITH x AS (SELECT a FROM orders) SELECT a FROM x",
        ],
    )
    def test_data_queries_still_allowed_for_analyst(self, sql):
        assert not self._eval(sql, "data-analyst").denied

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 'NOT_ANSWERABLE_FIELD: no cost field for service job' AS answer",
            "SELECT 'NOT_ANSWERABLE_RANGE: 2031 is outside the data' AS answer;",
        ],
    )
    def test_not_answerable_refusal_row_allowed_for_analyst(self, sql):
        assert not self._eval(sql, "data-analyst").denied

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 'NOT_ANSWERABLE_FIELD: x' AS answer UNION ALL SELECT 'sales.orders'",
            "SELECT 'NOT_ANSWERABLE_FIELD: x' AS answer, 'sales.orders' AS t",
            "SELECT 'NOT_ANSWERABLE_FIELD: x' AS other",
            "SELECT 'sales.orders' AS answer",
        ],
    )
    def test_not_answerable_look_alikes_still_refused_for_analyst(self, sql):
        assert self._eval(sql, "data-analyst").denied

    def test_redshift_dialect_table_query_allowed(self):
        sql = "SELECT APPROXIMATE COUNT(DISTINCT c) AS n FROM orders"
        assert not self._eval(sql, "data-analyst", dialect="redshift").denied

    def test_from_less_select_allowed_for_source_viewer(self):
        assert not self._eval("SELECT 1 AS one", "data-steward").denied

    def test_parse_error_is_refused_not_allowed(self):
        with pytest.raises(UnsafeSQLError):
            self._eval("SELECT FROM WHERE (", "data-analyst")

    def test_nothing_parsed_counts_as_reading_no_table(self):
        from coa_serve.tier2.sql_firewall import _references_a_table, _schema_echo_reason

        assert _references_a_table(None) is False
        assert _schema_echo_reason(None) == self._NO_TABLE

    def test_source_viewer_skips_the_ast_checks(self, monkeypatch):
        """The cheap rights check runs first: a steward never reaches the echo analysis."""
        from coa_serve.tier2 import sql_firewall

        calls: list = []
        monkeypatch.setattr(sql_firewall, "_schema_echo_reason", lambda parsed: calls.append(parsed))
        assert not self._eval("SELECT id FROM orders", "data-steward").denied
        assert calls == []
        self._eval("SELECT id FROM orders", "data-analyst")
        assert len(calls) == 1

    def test_request_visibility_reused_without_cedar_probe(self):
        """Inside the entrypoint's scope the firewall uses that decision, not a second probe."""
        from coa_serve.schema_visibility import FULL_VISIBILITY, schema_visibility_scope
        from coa_serve.tier2.cedar_authorizer import CedarDecision

        class _NoViewProbe:
            def authorize(self, **kwargs):
                return CedarDecision(allowed=True)

            def can_view_source(self, **kwargs):
                raise AssertionError("viewSource re-evaluated")

            can_view_ontology = can_view_source

        fw = SQLFirewall(cedar_authorizer=_NoViewProbe())
        with schema_visibility_scope(self._NS, FULL_VISIBILITY):
            assert not fw.evaluate("SELECT 'x' AS c", profile={}, namespace=self._NS).denied
        # Outside the scope the probe raises → fails closed → refused.
        assert fw.evaluate("SELECT 'x' AS c", profile={}, namespace=self._NS).denied


@pytest.mark.unit
class TestCteNameShadowing:
    """A CTE name hides only the BARE references bound to it, never a real table.

    A ``WITH`` alias binds bare references in its own scope. A schema- or
    catalog-qualified reference always names a physical table, and a bare
    reference outside the CTE's scope (or inside a non-recursive CTE's own body)
    reads the real table too, so it is checked against the allowlist and denylist.
    """

    _ALLOW_ORDERS = {"tableAllowlist": ["orders"]}
    _DENY_SSN = {"columnDenylist": {"customers": ["ssn"]}}

    @pytest.mark.parametrize(
        ("sql", "denied_table"),
        [
            ("WITH customers AS (SELECT 1 AS id) SELECT * FROM crm.customers", "crm.customers"),
            ("WITH customers AS (SELECT 1 AS id) SELECT id FROM crm.customers", "crm.customers"),
            ("WITH customers AS (SELECT 1 AS id) SELECT id FROM AwsDataCatalog.crm.customers", "crm.customers"),
            ("WITH customers AS (SELECT 1 AS id) SELECT id FROM othercat.crm.customers", "customers"),
            ('WITH "Customers" AS (SELECT 1 AS id) SELECT id FROM "CRM"."CUSTOMERS"', "crm.customers"),
            (
                "WITH customers AS (SELECT id FROM orders) "
                "SELECT o.id FROM orders o JOIN crm.customers c ON o.id = c.id",
                "crm.customers",
            ),
            # Bare reference outside the CTE's scope reads the real table.
            (
                "SELECT id FROM customers WHERE id IN (WITH customers AS (SELECT 1 AS id) SELECT id FROM customers)",
                "customers",
            ),
            # Inside a non-recursive CTE's own body its name is not yet bound.
            ("WITH customers AS (SELECT id FROM customers) SELECT id FROM customers", "customers"),
            # A later CTE's name is not bound inside an earlier CTE's body.
            ("WITH a AS (SELECT id FROM customers), customers AS (SELECT 1 AS id) SELECT id FROM a", "customers"),
        ],
        ids=[
            "two-part-star",
            "two-part",
            "three-part-root-catalog",
            "three-part-other-catalog",
            "quoted-mixed-case",
            "join",
            "outer-scope",
            "self-reference-non-recursive",
            "forward-reference",
        ],
    )
    def test_shadowed_table_is_checked_against_allowlist(self, sql, denied_table):
        result = SQLFirewall().evaluate(sql, profile=self._ALLOW_ORDERS)
        assert result.denied
        assert result.reason == f"Access denied to table(s): {denied_table}"

    @pytest.mark.parametrize(
        ("sql", "reason"),
        [
            (
                "WITH customers AS (SELECT 1 AS id) SELECT customers.ssn FROM crm.customers",
                "Access denied to column(s): customers.ssn",
            ),
            (
                "WITH customers AS (SELECT 1 AS id) SELECT * FROM crm.customers",
                "Access denied: SELECT * may expose restricted columns of 'customers'",
            ),
            (
                "WITH customers AS (SELECT 1 AS id) SELECT customers.* FROM crm.customers",
                "Access denied: SELECT * may expose restricted columns of 'customers'",
            ),
            (
                "WITH customers AS (SELECT 1 AS id) SELECT c.ssn FROM cat.crm.customers c",
                "Access denied to column(s): customers.ssn",
            ),
            # Alias equal to a CTE name: the qualifier names the real table.
            (
                "WITH x AS (SELECT 1 AS id) SELECT x.ssn FROM crm.customers AS x",
                "Access denied to column(s): customers.ssn",
            ),
            (
                "WITH x AS (SELECT 1 AS ssn) SELECT x.ssn FROM customers AS x",
                "Access denied to column(s): customers.ssn",
            ),
        ],
        ids=["qualified-column", "star", "qualified-star", "three-part-alias", "alias-equals-cte", "bare-alias-cte"],
    )
    def test_shadowed_table_is_checked_against_column_denylist(self, sql, reason):
        result = SQLFirewall().evaluate(sql, profile=self._DENY_SSN)
        assert result.denied
        assert result.reason == reason

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent",
            "WITH orders AS (SELECT id FROM crm.orders) SELECT id FROM orders",
            "WITH a AS (SELECT id FROM orders), b AS (SELECT id FROM a) SELECT id FROM b",
            "SELECT id FROM (WITH r AS (SELECT id FROM orders) SELECT id FROM r) s",
            "WITH RECURSIVE r (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
            "SELECT o.id FROM orders o JOIN r ON o.id = r.n",
            # sqlglot hangs the WITH on the set operation, not on either SELECT.
            "WITH c AS (SELECT id FROM orders) SELECT id FROM c UNION ALL SELECT id FROM c",
            "WITH c AS (SELECT id FROM orders) SELECT id FROM c INTERSECT SELECT id FROM c",
            # The reference sits in a subquery under the WITH-bearing SELECT.
            "WITH c AS (SELECT id FROM orders) SELECT id FROM orders WHERE id IN (SELECT id FROM c)",
        ],
        ids=[
            "plain",
            "cte-wraps-allowed-table",
            "chained",
            "nested",
            "recursive",
            "union",
            "intersect",
            "subquery-in-body",
        ],
    )
    def test_genuine_cte_references_still_pass(self, sql):
        assert not SQLFirewall().evaluate(sql, profile=self._ALLOW_ORDERS).denied

    @pytest.mark.parametrize("dialect", ["trino", "postgres"])
    @pytest.mark.parametrize(
        ("sql", "denied_table"),
        [
            ('WITH "Customers" AS (SELECT 1 AS id) SELECT * FROM customers JOIN orders ON 1=1', "customers"),
            ('WITH "Customers" AS (SELECT 1 AS id) SELECT id FROM "customers"', "customers"),
            ('WITH "CUSTOMERS" AS (SELECT id FROM orders) SELECT id FROM CUSTOMERS', "customers"),
            ('WITH customers AS (SELECT id FROM orders) SELECT id FROM "CUSTOMERS"', "customers"),
            ('WITH "customers" AS (SELECT id FROM orders) SELECT id FROM CUSTOMERS', "customers"),
            ("WITH Customers AS (SELECT id FROM orders) SELECT id FROM customers", "customers"),
            ("WITH Customers AS (SELECT id FROM orders) SELECT id FROM CUSTOMERS", "customers"),
            ('WITH "Customers" AS (SELECT 1 AS id) SELECT id FROM customers', "customers"),
        ],
        ids=[
            "quoted-mixed-cte-unquoted-ref",
            "quoted-mixed-cte-quoted-lower-ref",
            "quoted-upper-cte-unquoted-upper-ref",
            "unquoted-cte-quoted-upper-ref",
            "quoted-lower-cte-unquoted-upper-ref",
            "unquoted-case-variant",
            "unquoted-mixed-cte-unquoted-upper-ref",
            "quoted-mixed-cte-unquoted-lower-ref",
        ],
    )
    def test_case_or_quoting_mismatch_is_the_stored_table(self, sql, denied_table, dialect, monkeypatch):
        # A viewSource holder reaches the table checks; a schema-blind caller is
        # refused earlier because the reference binds to the CTE on Trino.
        monkeypatch.setattr(SQLFirewall, "_source_visible", lambda *_a: True)
        result = SQLFirewall().evaluate(sql, profile=self._ALLOW_ORDERS, dialect=dialect)
        assert result.denied
        assert result.reason == f"Access denied to table(s): {denied_table}"
        monkeypatch.undo()
        assert SQLFirewall().evaluate(sql, profile=self._ALLOW_ORDERS, dialect=dialect).denied

    @pytest.mark.parametrize("dialect", ["trino", "postgres"])
    def test_quoted_mixed_case_cte_does_not_hide_denied_column(self, dialect, monkeypatch):
        monkeypatch.setattr(SQLFirewall, "_source_visible", lambda *_a: True)
        sql = 'WITH "Customers" AS (SELECT 1 AS ssn) SELECT customers.ssn FROM customers'
        result = SQLFirewall().evaluate(sql, profile=self._DENY_SSN, dialect=dialect)
        assert result.denied
        assert result.reason == "Access denied to column(s): customers.ssn"

    @pytest.mark.parametrize("dialect", ["trino", "postgres"])
    @pytest.mark.parametrize(
        "sql",
        [
            'WITH "Recent" AS (SELECT id FROM orders) SELECT id FROM "Recent"',
            'WITH "recent" AS (SELECT id FROM orders) SELECT id FROM recent',
            'WITH recent AS (SELECT id FROM orders) SELECT id FROM "recent"',
            "WITH Recent AS (SELECT id FROM orders) SELECT id FROM Recent",
        ],
        ids=["both-quoted-mixed", "quoted-lower-cte", "quoted-lower-ref", "same-unquoted-mixed"],
    )
    def test_same_name_on_every_engine_still_binds_to_the_cte(self, sql, dialect):
        assert not SQLFirewall().evaluate(sql, profile=self._ALLOW_ORDERS, dialect=dialect).denied

    def test_recursive_forward_reference_is_the_stored_table(self):
        sql = "WITH RECURSIVE b AS (SELECT * FROM a), a AS (SELECT id FROM orders) SELECT * FROM b"
        result = SQLFirewall().evaluate(sql, profile=self._ALLOW_ORDERS, dialect="postgres")
        assert result.denied
        assert result.reason == "Access denied to table(s): a"

    def test_cte_column_not_attributed_to_restricted_table(self):
        sql = "WITH safe AS (SELECT name FROM customers) SELECT safe.name FROM safe"
        assert not SQLFirewall().evaluate(sql, profile=self._DENY_SSN).denied

    def test_referenced_tables_reports_the_shadowed_table(self):
        sql = "WITH customers AS (SELECT 1 AS id) SELECT id FROM crm.customers"
        assert SQLFirewall.referenced_tables(sql) == ["customers"]

    def test_constant_projection_over_shadowed_table_reads_a_table(self):
        parsed = sqlglot.parse_one("WITH customers AS (SELECT 1 AS id) SELECT 'x' FROM crm.customers", read="trino")
        assert _references_a_table(parsed)
        assert not _references_a_table(
            sqlglot.parse_one("WITH customers AS (SELECT 1 AS id) SELECT 'x' FROM customers", read="trino")
        )

    def test_namespace_scope_checks_the_shadowed_table(self):
        with pytest.raises(NamespaceSQLScopeError):
            SQLFirewall.validate_namespace_sql_scope(
                "WITH customers AS (SELECT 1 AS id) SELECT id FROM other_db.customers",
                native_databases=frozenset({"tenant_a_db"}),
                federated_catalog_schemas=frozenset(),
                default_catalog="AwsDataCatalog",
            )


@pytest.mark.unit
class TestWholeRowReferences:
    """A table name or alias used as a value exposes the whole row.

    PostgreSQL reads ``SELECT c FROM customers c`` as the composite row of
    ``customers``, so ``to_json(c)`` or ``CAST(c AS JSON)`` returns every column,
    the denied ones included. sqlglot parses ``c`` as an ordinary column, so the
    denylist must recognise the shape itself and treat it like ``c.*``.
    """

    @pytest.mark.parametrize("dialect", ["trino", "postgres"])
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT c FROM customers c",
            "SELECT customers FROM customers",
            "SELECT CAST(c AS JSON) FROM customers c",
            "SELECT to_json(c) FROM customers c",
            "SELECT ARRAY[c] FROM customers c",
            "SELECT row_to_json(c) FROM customers c",
            "SELECT json_agg(customers) FROM customers",
            "SELECT CAST(c AS VARCHAR) FROM customers c",
            # PostgreSQL functional notation: ssn(c) is c.ssn.
            "SELECT ssn(c) FROM customers c",
            "SELECT (c).ssn FROM customers c",
            "SELECT (c).* FROM customers c",
            # Schema-qualified table name as a value.
            "SELECT crm.customers FROM crm.customers",
            "SELECT db.crm.customers FROM db.crm.customers",
            "SELECT C FROM CUSTOMERS c",
            'SELECT "c" FROM customers AS "c"',
            # Outside the projection it is still a read of every column.
            "SELECT id FROM customers c WHERE CAST(c AS VARCHAR) LIKE '%123%'",
            "SELECT id FROM customers c ORDER BY c",
            "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id GROUP BY o.id, c",
            # Through a derived table: the inner projection is the whole row.
            "SELECT t.x FROM (SELECT c AS x FROM customers c) t",
            "SELECT id FROM orders WHERE customer_id IN (SELECT to_json(c) FROM customers c)",
        ],
    )
    def test_whole_row_reference_is_denied(self, sql, dialect):
        result = SQLFirewall().evaluate(sql, profile=_DENY_SSN, dialect=dialect)
        assert result.denied
        assert result.reason == _WHOLE_ROW

    def test_qualified_denylist_key_applies(self):
        result = SQLFirewall().evaluate(
            "SELECT to_json(c) FROM crm.customers c", profile={"columnDenylist": {"crm.customers": ["ssn"]}}
        )
        assert result.denied
        assert result.reason == _WHOLE_ROW

    def test_ambiguous_column_named_like_an_alias_is_denied(self):
        # ``status`` may be a column of orders or the row of the table aliased
        # ``status``; the firewall cannot tell, so it fails closed.
        sql = "SELECT status FROM orders JOIN customers status ON orders.customer_id = status.id"
        result = SQLFirewall().evaluate(sql, profile=_DENY_SSN)
        assert result.denied
        assert result.reason == _WHOLE_ROW

    def test_row_of_star_is_still_denied_as_star(self):
        result = SQLFirewall().evaluate("SELECT ROW(c.*) FROM customers c", profile=_DENY_SSN)
        assert result.denied
        assert result.reason == "Access denied: SELECT * may expose restricted columns of 'customers'"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT name FROM customers",
            "SELECT c.name, c.id FROM customers c",
            "SELECT customers.name FROM customers",
            "SELECT COUNT(*) FROM customers c",
            "SELECT to_json(c.name) FROM customers c",
            # A qualifier naming a stored table makes the name an ordinary column.
            "SELECT o.customers FROM orders o JOIN customers c ON o.customer_id = c.id",
            # A whole row of an unrestricted table is fine.
            "SELECT to_json(o) FROM orders o",
            "SELECT o FROM orders o JOIN customers c ON o.customer_id = c.id",
            # A CTE's row only holds what the CTE projected.
            "WITH safe AS (SELECT name FROM customers) SELECT to_json(safe) FROM safe",
            # Lambda parameters are not column references.
            "SELECT transform(ARRAY[c.id], x -> x + 1) FROM customers c",
        ],
    )
    def test_allowed_shapes_still_pass(self, sql):
        assert not SQLFirewall().evaluate(sql, profile=_DENY_SSN).denied

    def test_whole_row_ignored_without_denylist_for_that_table(self):
        result = SQLFirewall().evaluate("SELECT to_json(c) FROM customers c", profile={"tableAllowlist": ["customers"]})
        assert not result.denied

    def test_whole_row_of_a_cte_shadowing_the_restricted_table(self):
        # The outer `customers` binds to the CTE, whose row holds only `name`.
        allowed = "WITH customers AS (SELECT name FROM customers) SELECT to_json(customers) FROM customers"
        assert not SQLFirewall().evaluate(allowed, profile=_DENY_SSN).denied
        # The same shape over an inner star still exposes every column.
        denied = SQLFirewall().evaluate(
            "WITH customers AS (SELECT * FROM customers) SELECT to_json(customers) FROM customers",
            profile=_DENY_SSN,
        )
        assert denied.denied
        assert denied.reason == "Access denied: SELECT * may expose restricted columns of 'customers'"

    def test_name_declared_only_in_another_scope_still_fails_closed(self):
        # The outer `status` cannot bind to the subquery's alias, but no enclosing
        # SELECT declares it either, so it is matched against every table and
        # alias in the statement and treated as a whole row of customers.
        sql = "SELECT status FROM orders WHERE id IN (SELECT id FROM customers status)"
        result = SQLFirewall().evaluate(sql, profile=_DENY_SSN)
        assert result.denied
        assert result.reason == _WHOLE_ROW


@pytest.mark.unit
class TestDerivedTableScope:
    """A derived table cannot see the other FROM items of the SELECT it sits in.

    In ``SELECT (SELECT j FROM (SELECT c.ssn AS j FROM orders) c) FROM customers c``
    the inner ``c.ssn`` binds to the outermost ``customers c``, not to the derived
    table that is itself aliased ``c``.
    """

    @pytest.mark.parametrize(
        ("sql", "reason"),
        [
            ("SELECT (SELECT j FROM (SELECT c.ssn AS j FROM orders x LIMIT 1) c) FROM customers c", _SSN),
            (
                "SELECT 1 FROM customers c WHERE EXISTS "
                "(SELECT 1 FROM (SELECT c.ssn AS s FROM orders) c WHERE c.s LIKE '1%')",
                _SSN,
            ),
            ("SELECT (SELECT j FROM (SELECT to_json(c) AS j FROM orders x LIMIT 1) c) FROM customers c", _WHOLE_ROW),
            (
                "SELECT (SELECT j FROM (SELECT c.ssn AS j FROM orders x LIMIT 1) d JOIN orders c ON true) "
                "FROM customers c",
                _SSN,
            ),
            (
                "WITH c AS (SELECT 1 AS k) SELECT (SELECT j FROM (SELECT c.ssn AS j FROM orders x) d, c) "
                "FROM customers c",
                _SSN,
            ),
            ("SELECT l.x FROM customers c, LATERAL (SELECT c.ssn AS x) l", _SSN),
        ],
        ids=["scalar-subquery", "exists", "whole-row", "sibling-join", "sibling-cte", "lateral-sees-sibling"],
    )
    def test_reference_inside_a_derived_table_binds_further_out(self, sql, reason):
        result = SQLFirewall().evaluate(sql, profile=_DENY_SSN)
        assert result.denied
        assert result.reason == reason

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT (SELECT j FROM (SELECT c.name AS j FROM orders x LIMIT 1) c) FROM customers c",
            "SELECT d.j FROM (SELECT c.name AS j FROM customers c) d",
            "SELECT l.x FROM customers c, LATERAL (SELECT c.name AS x) l",
        ],
        ids=["scalar-subquery", "plain-derived-table", "lateral"],
    )
    def test_allowed_columns_still_pass(self, sql):
        assert not SQLFirewall().evaluate(sql, profile=_DENY_SSN).denied


@pytest.mark.unit
class TestColumnAliasList:
    """``customers AS x(id, name, s)`` renames columns by position, so ``x.s`` is ``ssn``."""

    @pytest.mark.parametrize(
        "sql",
        ["SELECT x.s FROM customers AS x(id, name, s)", "SELECT s FROM customers AS x(id, name, s)"],
        ids=["qualified", "unqualified"],
    )
    def test_column_alias_list_over_a_restricted_table_is_denied(self, sql):
        result = SQLFirewall().evaluate(sql, profile=_DENY_SSN, dialect="postgres")
        assert result.denied
        assert result.reason == "Access denied: column alias list may rename restricted columns of 'customers'"

    def test_column_alias_list_over_an_unrestricted_table_passes(self):
        sql = "SELECT x.a FROM orders AS x(a, b)"
        assert not SQLFirewall().evaluate(sql, profile=_DENY_SSN, dialect="postgres").denied


@pytest.mark.unit
class TestAliasReuseAcrossScopes:
    """A qualifier resolves to the alias declared in its own SELECT scope.

    The same alias can name different tables in different scopes:
    ``x.ssn`` over ``customers x`` is charged to ``customers`` even when another
    scope declares ``orders x``.
    """

    @pytest.mark.parametrize("dialect", ["trino", "postgres"])
    @pytest.mark.parametrize(
        "sql",
        [
            "WITH o AS (SELECT x.id FROM orders x) SELECT x.ssn FROM customers x",
            "SELECT x.ssn FROM customers x WHERE EXISTS (SELECT 1 FROM orders x)",
            "SELECT x.ssn FROM customers x UNION ALL SELECT x.id FROM orders x",
            "SELECT x.id FROM orders x UNION ALL SELECT x.ssn FROM customers x",
            "SELECT x.ssn FROM customers x WHERE x.id IN (SELECT x.id FROM orders x)",
            "SELECT x.ssn FROM customers x JOIN (SELECT x.id FROM orders x) o ON o.id = x.id",
            # Correlated: the inner x is not declared inside, so it is the outer customers.
            "SELECT 1 FROM customers x WHERE EXISTS (SELECT 1 FROM orders o WHERE o.id = x.ssn)",
            # Ambiguous in one scope: fail closed.
            "SELECT x.ssn FROM orders x CROSS JOIN customers x",
        ],
    )
    def test_column_is_charged_to_the_table_in_its_scope(self, sql, dialect):
        result = SQLFirewall().evaluate(sql, profile=_DENY_SSN, dialect=dialect)
        assert result.denied
        assert result.reason == "Access denied to column(s): customers.ssn"

    def test_star_is_charged_to_the_table_in_its_scope(self):
        result = SQLFirewall().evaluate(
            "SELECT x.* FROM customers x WHERE EXISTS (SELECT 1 FROM orders x)", profile=_DENY_SSN
        )
        assert result.denied
        assert result.reason == "Access denied: SELECT * may expose restricted columns of 'customers'"

    @pytest.mark.parametrize(
        "sql",
        [
            # ssn on orders is not the restricted customers.ssn.
            "SELECT x.ssn FROM orders x UNION ALL SELECT x.name FROM customers x",
            "SELECT x.name FROM customers x WHERE EXISTS (SELECT 1 FROM orders x WHERE x.ssn = 'a')",
            "WITH o AS (SELECT x.ssn FROM orders x) SELECT x.name FROM customers x",
            "SELECT x.* FROM orders x WHERE EXISTS (SELECT 1 FROM customers x WHERE x.id = 1)",
            # A derived table's row holds only what it projected.
            "SELECT x.name FROM (SELECT name FROM customers) x",
            "SELECT customers FROM (SELECT id FROM customers) customers",
        ],
    )
    def test_legitimate_alias_reuse_is_allowed(self, sql):
        assert not SQLFirewall().evaluate(sql, profile=_DENY_SSN).denied


@pytest.mark.unit
class TestRelationNameIdentity:
    """A relation name binds in a scope only when it names that relation on every engine.

    Trino/Athena compare names case-insensitively; Postgres folds unquoted names to
    lower case and keeps quoted ones exact. A reference that matches a nearer alias
    only after case-folding may bind further out on some engine, so it is charged to
    every candidate, the same rule CTE references follow (``resolves_to_cte``).
    """

    @pytest.mark.parametrize("dialect", ["trino", "postgres"])
    @pytest.mark.parametrize(
        "sql",
        [
            'SELECT (SELECT "c".ssn FROM (SELECT 1 AS ssn) "C") FROM customers c',
            'SELECT (SELECT C.ssn FROM (SELECT 1 AS ssn) "C") FROM customers c',
            'SELECT (SELECT "C".ssn FROM (SELECT 1 AS ssn) c) FROM customers "C"',
            "SELECT (SELECT C.ssn FROM (SELECT 1 AS ssn) c) FROM customers c",
            'SELECT x.ssn FROM customers "X" WHERE EXISTS (SELECT 1 FROM orders x)',
        ],
    )
    def test_case_folded_match_does_not_hide_an_outer_table(self, sql, dialect):
        result = SQLFirewall().evaluate(sql, profile=_DENY_SSN, dialect=dialect)
        assert result.denied
        assert result.reason == _SSN

    def test_case_folded_whole_row_does_not_hide_an_outer_table(self):
        sql = 'SELECT (SELECT to_json("c") FROM (SELECT 1 AS ssn) "C") FROM customers c'
        result = SQLFirewall().evaluate(sql, profile=_DENY_SSN)
        assert result.denied
        assert result.reason == "Access denied: whole-row reference may expose restricted columns of 'customers'"

    @pytest.mark.parametrize(
        "sql",
        [
            # The reference names the inner derived table on every engine.
            'SELECT (SELECT "C".ssn FROM (SELECT 1 AS ssn) "C") FROM customers c',
            'SELECT (SELECT "c".ssn FROM (SELECT 1 AS ssn) c) FROM customers c',
            "SELECT (SELECT c.ssn FROM (SELECT 1 AS ssn) c) FROM customers c",
            'WITH "C" AS (SELECT name FROM customers) SELECT "C".name FROM "C"',
            "SELECT d.name FROM (SELECT name FROM customers) d",
            "SELECT t.x FROM customers c CROSS JOIN UNNEST(c.tags) AS t(x)",
        ],
    )
    def test_exact_inner_binding_is_allowed(self, sql):
        assert not SQLFirewall().evaluate(sql, profile=_DENY_SSN).denied


@pytest.mark.unit
class TestAliasHidesOwnTableName:
    """An alias hides the stored table's own name: ``orders`` in ``FROM orders o`` binds further out.

    In ``(SELECT orders.ssn FROM orders o) FROM customers orders`` the engine reads
    ``customers.ssn``, so the reference is charged to both tables (fail closed).
    """

    @pytest.mark.parametrize("dialect", ["trino", "postgres"])
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT (SELECT orders.ssn FROM orders o LIMIT 1) FROM customers orders",
            "SELECT (SELECT to_json(orders) FROM orders o LIMIT 1) FROM customers orders",
            "SELECT (SELECT orders.* FROM orders o LIMIT 1) FROM customers orders",
            "SELECT (SELECT ssn(orders) FROM orders o LIMIT 1) FROM customers orders",
            "SELECT (SELECT orders FROM orders o LIMIT 1) FROM customers orders",
            'SELECT (SELECT "orders".ssn FROM orders o LIMIT 1) FROM customers orders',
            "SELECT (SELECT orders.ssn FROM crm.orders o LIMIT 1) FROM customers orders",
            "SELECT x.s FROM customers orders, LATERAL (SELECT orders.ssn AS s FROM orders o) x",
        ],
    )
    def test_aliased_own_name_binds_to_outer_relation(self, sql, dialect):
        result = SQLFirewall().evaluate(sql, profile=_DENY_SSN, dialect=dialect)
        assert result.denied
        assert "customers" in (result.reason or "")

    def test_aliased_own_name_still_charges_inner_table(self):
        sql = "SELECT (SELECT orders.ssn FROM orders o LIMIT 1) FROM customers c"
        result = SQLFirewall().evaluate(sql, profile={"columnDenylist": {"orders": ["ssn"]}})
        assert result.denied
        assert result.reason == "Access denied to column(s): orders.ssn"

    def test_alias_reference_still_binds_inner(self):
        sql = "SELECT (SELECT o.ssn FROM orders o LIMIT 1) FROM customers orders"
        assert not SQLFirewall().evaluate(sql, profile=_DENY_SSN).denied


@pytest.mark.unit
class TestParenthesizedTableReference:
    """``FROM (customers) c`` aliases the stored table, not a derived query."""

    @pytest.mark.parametrize("dialect", ["trino", "postgres"])
    @pytest.mark.parametrize(
        ("sql", "reason"),
        [
            ("SELECT c.ssn FROM (customers) c", _SSN),
            ("SELECT c.ssn FROM orders o JOIN (customers) AS c ON o.id = c.id", _SSN),
            ("SELECT c.ssn FROM ((customers)) c", _SSN),
            ("SELECT c.* FROM (customers) c", "Access denied: SELECT * may expose restricted columns of 'customers'"),
            (
                "SELECT c FROM (customers) c",
                "Access denied: whole-row reference may expose restricted columns of 'customers'",
            ),
            (
                "SELECT c.s2 FROM (customers) AS c(id, s2)",
                "Access denied: column alias list may rename restricted columns of 'customers'",
            ),
            ("SELECT c.ssn FROM (TABLE customers) c", "Query could not be parsed for authorization"),
            ("SELECT c.ssn FROM (customers JOIN orders ON true) c", _SSN),
        ],
    )
    def test_parenthesized_restricted_table_is_checked(self, sql, reason, dialect):
        result = SQLFirewall().evaluate(sql, profile=_DENY_SSN, dialect=dialect)
        assert result.denied
        assert result.reason == reason

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT c.name FROM (customers) c",
            "SELECT c.ssn FROM (orders) c",
            'SELECT c.ssn FROM "TABLE" c',
            "WITH customers AS (SELECT name FROM customers) SELECT c.name FROM (customers) c",
        ],
    )
    def test_parenthesized_allowed_shapes_pass(self, sql):
        assert not SQLFirewall().evaluate(sql, profile=_DENY_SSN).denied


@pytest.mark.unit
class TestTableFunctionsRefused:
    """A table function hides the tables it reads, so it is refused in every dialect.

    A Trino/Athena pass-through function carries a second statement as a string
    literal: ``TABLE(pg.system.query(query => 'SELECT ssn FROM hr.salaries'))``.
    The source database runs it, but sqlglot sees only a nameless Table node,
    which the table, column and namespace-scope checks skipped. Joined to an
    allowlisted table it passed ``evaluate``.
    """

    _ALLOW_ORDERS = {"tableAllowlist": ["orders"]}
    _DENY_SSN = {"columnDenylist": {"salaries": ["ssn"]}}
    _DIALECTS = ["trino", "athena", "postgres", "redshift", "mysql", "tsql", "snowflake"]

    _ATTACKS = [
        # Standalone, catalog-qualified and unqualified.
        "SELECT * FROM TABLE(pg.system.query(query => 'SELECT ssn FROM hr.salaries'))",
        "SELECT * FROM TABLE(system.query(query => 'SELECT ssn FROM hr.salaries'))",
        # Combined with an allowlisted table, which also beats the schema-echo gate.
        "SELECT o.id FROM orders o CROSS JOIN TABLE(system.query(query => 'SELECT ssn FROM hr.salaries')) t",
        "SELECT o.id FROM orders o JOIN TABLE(system.query(query => 'SELECT 1 AS id')) t ON o.id = t.id",
        "SELECT c.id FROM orders c UNION ALL SELECT t.v FROM TABLE(system.query(query => 'SELECT 1 AS v')) t",
        "SELECT id FROM orders WHERE id IN (SELECT v FROM TABLE(system.query(query => 'SELECT 1 AS v')) t)",
        "WITH p AS (SELECT * FROM TABLE(system.query(query => 'SELECT 1'))) SELECT o.id FROM orders o, p",
        "SELECT (SELECT max(v) FROM TABLE(system.query(query => 'SELECT 1 AS v')) t) AS m FROM orders",
        # Blocked functions inside the literal.
        "SELECT * FROM TABLE(system.query(query => 'SELECT pg_read_file(''/etc/passwd'') AS v'))",
        # A table argument and the call without TABLE().
        "SELECT * FROM TABLE(system.fn(input => TABLE(crm_db.secrets)))",
        "SELECT * FROM system.query(TABLE(crm_db.secrets))",
        "SELECT * FROM system.query(query => 'SELECT ssn FROM hr.salaries')",
        "SELECT * FROM pg.system.query(query => 'SELECT ssn FROM hr.salaries')",
        "SELECT o.id FROM orders o, system.query(query => 'SELECT 1') t",
        "SELECT * FROM query('SELECT ssn FROM hr.salaries')",
        # Other engines' pass-through and table functions.
        "SELECT * FROM OPENQUERY(srv, 'SELECT ssn FROM hr.salaries')",
        "SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT ssn FROM hr.salaries')",
        "SELECT * FROM my_udf(1) t",
        # A dotted call is a function, not a four-part table name.
        "SELECT * FROM a.b.c.query(query => 'SELECT ssn FROM hr.salaries')",
    ]

    @pytest.mark.parametrize("dialect", _DIALECTS)
    @pytest.mark.parametrize("sql", _ATTACKS)
    def test_validate_refuses_table_functions(self, sql, dialect):
        with pytest.raises(UnsafeSQLError):
            SQLFirewall().validate(sql, dialect=dialect)

    @pytest.mark.parametrize("dialect", ["trino", "athena", "postgres", "redshift"])
    @pytest.mark.parametrize("sql", _ATTACKS)
    def test_refusal_names_the_table_function(self, sql, dialect):
        with pytest.raises(UnsafeSQLError, match=r"Unsafe operation 'TableFunction\("):
            SQLFirewall().validate(sql, dialect=dialect)

    @pytest.mark.parametrize("profile", [_ALLOW_ORDERS, _DENY_SSN, {}])
    @pytest.mark.parametrize("sql", _ATTACKS[:8])
    def test_evaluate_refuses_for_restricted_and_unrestricted_callers(self, sql, profile):
        with pytest.raises(UnsafeSQLError, match="TableFunction"):
            SQLFirewall().evaluate(sql, profile=profile)

    def test_exact_reason_for_the_reported_shape(self):
        with pytest.raises(UnsafeSQLError) as exc:
            SQLFirewall().evaluate(self._ATTACKS[2], profile=self._ALLOW_ORDERS)
        assert str(exc.value) == "Unsafe operation 'TableFunction(TABLE(system.query))' found inside statement"

    def test_transpiled_jdbc_statement_is_still_refused(self):
        # The direct-JDBC route validates the statement after re-transpiling it.
        for target in ("postgres", "redshift", "mysql", "tsql"):
            transpiled = sqlglot.transpile(self._ATTACKS[2], read="trino", write=target)[0]
            with pytest.raises(UnsafeSQLError, match="TableFunction"):
                SQLFirewall().validate(transpiled, dialect=target)

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            ("SELECT g FROM generate_series(1, 3) g", "postgres"),
            ("SELECT g FROM generate_series(1, 3) g", "redshift"),
            ("SELECT o.id, g FROM orders o, LATERAL generate_series(1, o.qty) g", "postgres"),
            ("SELECT o.id, i FROM orders o CROSS JOIN UNNEST(o.items) AS t(i)", "trino"),
            ("SELECT d FROM orders CROSS JOIN UNNEST(sequence(1, 3)) AS t(d)", "trino"),
            ("SELECT o.id, x FROM orders o, LATERAL (SELECT 1 AS x) s", "postgres"),
            ("SELECT x FROM orders, (VALUES (1), (2)) v(x)", "trino"),
            ("SELECT o.id, f.value FROM orders o, LATERAL FLATTEN(input => o.items) f", "snowflake"),
            ("SELECT id FROM orders (NOLOCK)", "tsql"),
            ("SELECT id FROM orders WITH (NOLOCK)", "tsql"),
            ("SELECT a.id FROM db.crm.orders a", "trino"),
            ("SELECT x.id FROM a.b.c.d x", "trino"),
        ],
    )
    def test_row_generators_and_named_tables_still_pass(self, sql, dialect):
        SQLFirewall().validate(sql, dialect=dialect)

    @pytest.mark.parametrize("dialect", ["trino", "athena", "postgres", "redshift"])
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM x.generate_series(1, 2)",
            "SELECT * FROM a.b.unnest(array[1])",
            "SELECT o.id, g FROM orders o, x.generate_series(1, 2) g",
        ],
    )
    def test_qualified_row_generator_is_refused(self, sql, dialect):
        # A schema-qualified name may resolve to a user-defined table function, not the built-in.
        with pytest.raises(UnsafeSQLError, match=r"Unsafe operation 'TableFunction\("):
            SQLFirewall().validate(sql, dialect=dialect)

    @pytest.mark.parametrize(
        ("sql", "dialect", "label"),
        [
            # A typed sqlglot function (JSON_TABLE) and a LATERAL call that is not a generator.
            ("SELECT * FROM JSON_TABLE(data, '$[*]' COLUMNS (id INT PATH '$.id')) AS jt", "mysql", "JSONTable"),
            ("SELECT o.id, t.v FROM orders o, LATERAL some_udf(o.id) AS t(v)", "postgres", "LATERAL some_udf"),
        ],
    )
    def test_typed_and_lateral_table_functions_are_refused(self, sql, dialect, label):
        with pytest.raises(UnsafeSQLError, match=rf"Unsafe operation 'TableFunction\({label}\)'"):
            SQLFirewall().validate(sql, dialect=dialect)

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            (
                "SELECT * FROM orders o, JSON_TABLE('{\"ssn\":\"123-45-6789\"}', '$' COLUMNS (id INT PATH '$.id')) jt",
                "mysql",
            ),
            ("SELECT * FROM orders o, read_csv('s3://private-bucket/x.csv') t", "duckdb"),
        ],
    )
    def test_refusal_label_never_echoes_a_literal(self, sql, dialect):
        # A typed function's ``.name`` is its first argument; the label must name the function instead.
        with pytest.raises(UnsafeSQLError) as exc:
            SQLFirewall().validate(sql, dialect=dialect)
        assert "123-45-6789" not in str(exc.value)
        assert "private-bucket" not in str(exc.value)

    @pytest.mark.parametrize("suffix", ["", "(1)"])
    def test_too_deeply_dotted_relation_is_refused_not_raised(self, suffix):
        sql = "SELECT * FROM " + ".".join(["a"] * 3000) + suffix
        with pytest.raises(UnsafeSQLError):
            SQLFirewall().validate(sql)
        assert SQLFirewall.requires_namespace_scope_check(sql)
        refs = SQLFirewall._analyze_refs(sql)
        assert refs is None or refs.unattributed_relation is not None

    def test_generator_argument_tables_are_still_authorized(self):
        sql = "SELECT g FROM generate_series(1, (SELECT max(ssn) FROM salaries)) g"
        result = SQLFirewall().evaluate(sql, profile=self._ALLOW_ORDERS, dialect="postgres")
        assert result.denied
        assert result.reason == "Access denied to table(s): salaries"

    def test_reference_analysis_fails_closed_on_a_table_function(self):
        # Defense in depth behind the safety check: no nameless relation is skipped.
        refs = SQLFirewall._analyze_refs(self._ATTACKS[2])
        assert refs is not None
        assert refs.unattributed_relation == "TableFunction(TABLE(system.query))"
        assert refs.tables == frozenset()
        other = SQLFirewall._analyze_refs("SELECT * FROM system.query(query => 'x')")
        assert other is not None
        assert other.unattributed_relation == "TableFunction(system.query)"
        refs = SQLFirewall._analyze_refs("SELECT g FROM generate_series(1, 3) g")
        assert refs is not None
        assert refs.unattributed_relation is None
        assert refs.tables == frozenset()

    def test_authorization_denial_names_the_refused_relation_and_logs_it(self):
        # T-SQL's legacy hint form parses as a call that names no stored table, so
        # the safety check allows it but the authorization check cannot see
        # `customers`. It is refused with its own reason, not a parse error.
        sql = "SELECT o.id FROM orders o JOIN customers (NOLOCK) ON 1 = 1"
        with structlog.testing.capture_logs() as logs:
            result = SQLFirewall().evaluate(sql, profile=self._ALLOW_ORDERS, dialect="tsql")
        assert result.denied
        assert result.reason == (
            "Query reads a relation the firewall cannot attribute to stored tables: TableFunction(customers)"
        )
        events = [e for e in logs if e["event"] == "sql_firewall_unattributed_relation"]
        assert events == [
            {
                "event": "sql_firewall_unattributed_relation",
                "reason": "TableFunction(customers)",
                "log_level": "warning",
            }
        ]

    @pytest.mark.parametrize(
        ("sql", "label"),
        [
            ("SELECT * FROM ?", "UnresolvedTableSource(Placeholder)"),
            ("SELECT * FROM :t", "UnresolvedTableSource(Placeholder)"),
            ("SELECT * FROM @t", "UnresolvedTableSource(Parameter)"),
        ],
    )
    def test_placeholder_relation_is_refused(self, sql, label):
        with pytest.raises(UnsafeSQLError) as exc:
            SQLFirewall().validate(sql)
        assert str(exc.value) == f"Unsafe operation '{label}' found inside statement"

    @pytest.mark.parametrize("sql", _ATTACKS)
    def test_namespace_scope_check_refuses_table_functions(self, sql):
        assert SQLFirewall.requires_namespace_scope_check(sql)
        with pytest.raises(NamespaceSQLScopeError, match=r"pass-through queries are not permitted: TableFunction"):
            SQLFirewall.validate_namespace_sql_scope(
                sql,
                native_databases=frozenset({"tenant_a_db"}),
                federated_catalog_schemas=frozenset(),
                default_catalog="AwsDataCatalog",
            )

    @pytest.mark.parametrize(
        "error",
        [sqlglot.errors.TokenError("unterminated"), RecursionError("too deep")],
        ids=["token-error", "recursion-error"],
    )
    def test_namespace_scope_check_turns_every_parse_failure_into_a_scope_denial(self, error):
        with patch("coa_serve.tier2.sql_firewall.sqlglot.parse_one", side_effect=error):
            assert SQLFirewall.requires_namespace_scope_check("SELECT 1")
            with pytest.raises(NamespaceSQLScopeError, match="Unable to verify SQL references"):
                SQLFirewall.validate_namespace_sql_scope(
                    "SELECT 1",
                    native_databases=frozenset({"tenant_a_db"}),
                    federated_catalog_schemas=frozenset(),
                    default_catalog="AwsDataCatalog",
                )

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM orders",
            "SELECT id FROM tenant_a_db.orders",
            "SELECT id FROM cat.tenant_a_db.orders",
            'SELECT id FROM "tenant_a_db"."orders" JOIN customers ON 1 = 1',
            "WITH c AS (SELECT id FROM orders) SELECT id FROM c",
            "SELECT g FROM generate_series(1, 3) g",
        ],
    )
    def test_namespace_scope_gate_parses_once_and_matches_extract_tables(self, sql):
        expected = any("." in ref for ref in SQLFirewall.extract_tables(sql))
        with patch("coa_serve.tier2.sql_firewall.sqlglot.parse_one", wraps=sqlglot.parse_one) as parse_one:
            assert SQLFirewall.requires_namespace_scope_check(sql) == expected
        assert parse_one.call_count == 1

    def test_namespace_scope_check_still_skips_bare_tables(self):
        assert not SQLFirewall.requires_namespace_scope_check("SELECT id FROM orders")
        assert not SQLFirewall.requires_namespace_scope_check("SELECT g FROM generate_series(1, 3) g")
        assert SQLFirewall.requires_namespace_scope_check("SELECT id FROM tenant_a_db.orders")
        assert SQLFirewall.requires_namespace_scope_check("SELECT FROM WHERE (((")


class _TablesSeenByCedar:
    """Cedar stand-in that allows everything and records the table list it was given."""

    tables: list[str] | None = None

    def authorize(self, *, namespace, profile, tables):
        self.tables = tables
        return CedarDecision(allowed=True)


@pytest.mark.unit
class TestSQLFirewallAuthorizationParseDialect:
    """The allowlist, denylist and Cedar checks read the statement in the dialect the safety check parsed.

    Trino ends a block comment at the first ``*/``; sqlglot's default dialect and
    PostgreSQL nest them. Every executor reads NL/VKG SQL as Trino, so a check that
    parsed in the default dialect would see the text after the first ``*/`` as a
    comment while the engine reads it as SQL.
    """

    _HIDDEN_TABLE = "SELECT id FROM orders /* a /* b */ CROSS JOIN secrets -- */"
    _HIDDEN_COLUMN = "SELECT c.id /* a /* b */ , c.ssn -- */\nFROM customers c"

    def test_evaluate_nested_comment_hidden_table_denied_by_allowlist(self):
        fw = SQLFirewall()
        result = fw.evaluate(self._HIDDEN_TABLE, profile={"tableAllowlist": ["orders"]})
        assert result.denied
        assert "secrets" in (result.reason or "")

    def test_evaluate_nested_comment_hidden_column_denied_by_denylist(self):
        fw = SQLFirewall()
        result = fw.evaluate(self._HIDDEN_COLUMN, profile={"columnDenylist": {"customers": ["ssn"]}})
        assert result.denied
        assert "ssn" in (result.reason or "")

    def test_evaluate_nested_comment_hidden_table_reaches_cedar(self):
        recorder = _TablesSeenByCedar()
        fw = SQLFirewall(cedar_authorizer=recorder)
        result = fw.evaluate(self._HIDDEN_TABLE, profile={}, namespace="ns")
        assert not result.denied
        assert "secrets" in recorder.tables

    def test_evaluate_engine_dialect_authorizes_engine_parse(self):
        """Already-transpiled engine SQL is authorized as the engine reads it (PostgreSQL nests comments)."""
        fw = SQLFirewall()
        result = fw.evaluate(self._HIDDEN_TABLE, profile={"tableAllowlist": ["orders"]}, dialect="postgres")
        assert not result.denied

    def test_evaluate_engine_dialect_cedar_tables_come_from_the_engine_parse(self):
        """With ``dialect="postgres"`` Cedar sees the PostgreSQL parse, where the nested comment hides nothing."""
        recorder = _TablesSeenByCedar()
        fw = SQLFirewall(cedar_authorizer=recorder)
        assert not fw.evaluate(self._HIDDEN_TABLE, profile={}, namespace="ns", dialect="postgres").denied
        assert recorder.tables == ["orders"]


def _engine_tables(sql: str, engine: str) -> list[str]:
    """Tables the engine reads after the executor's Trino -> engine transpile."""
    transpiled = sqlglot.transpile(sql, read="trino", write=engine)[0]
    return sorted(t.name for t in sqlglot.parse_one(transpiled, read=engine).find_all(sqlglot.exp.Table))


@pytest.mark.unit
class TestSQLFirewallAuthorizesWhatEachEngineRuns:
    """NL->SQL output is authorized as Trino, and every executor reads it as Trino.

    Athena runs it directly; the Redshift and direct-JDBC executors transpile it
    with ``read="trino"`` first. These cases pin that the tables the firewall
    authorizes are the tables each engine ends up reading, and that SQL written in
    another engine's own syntax fails closed rather than being half-understood.
    """

    _ENGINES = ["athena", "postgres", "mysql", "redshift"]

    _PARSEABLE = [
        pytest.param("SELECT id FROM orders /* a /* b */ CROSS JOIN secrets -- */", id="nested-block-comment"),
        pytest.param(
            "SELECT o.id FROM orders o CROSS JOIN secrets s WHERE o.note <> '-- /*'", id="comment-marker-in-string"
        ),
        pytest.param('SELECT "id" FROM "orders" CROSS JOIN "secrets"', id="double-quoted-identifiers"),
        pytest.param("SELECT o.id FROM orders o /* x */ CROSS JOIN secrets s", id="plain-block-comment"),
    ]

    @pytest.mark.parametrize("engine", _ENGINES)
    @pytest.mark.parametrize("sql", _PARSEABLE)
    def test_cedar_tables_match_the_engine_parse(self, sql: str, engine: str):
        recorder = _TablesSeenByCedar()
        SQLFirewall(cedar_authorizer=recorder).evaluate(sql, profile={}, namespace="ns")
        assert sorted({t.rsplit(".", 1)[-1] for t in recorder.tables}) == _engine_tables(sql, engine)

    @pytest.mark.parametrize("engine", _ENGINES)
    @pytest.mark.parametrize("sql", _PARSEABLE)
    def test_allowlist_denies_a_table_every_engine_reads(self, sql: str, engine: str):
        assert "secrets" in _engine_tables(sql, engine)
        result = SQLFirewall().evaluate(sql, profile={"tableAllowlist": ["orders"]})
        assert result.denied
        assert "secrets" in (result.reason or "")

    @pytest.mark.parametrize(
        "sql",
        [
            pytest.param("SELECT id FROM orders # x\nCROSS JOIN secrets", id="mysql-hash-comment"),
            pytest.param("SELECT `id` FROM `orders`", id="mysql-backticks"),
            pytest.param("SELECT $$ */ $$ AS x FROM orders", id="postgres-dollar-quote"),
        ],
    )
    def test_engine_only_syntax_fails_closed(self, sql: str):
        with pytest.raises(UnsafeSQLError):
            SQLFirewall().evaluate(sql, profile={"tableAllowlist": ["orders"]})

    def test_postgres_style_casts_and_concat_still_pass(self):
        """Syntax Trino also accepts (``::`` casts, ``||``) is not denied by mistake."""
        sql = "SELECT o.created::date, o.a || o.b FROM orders o"
        assert not SQLFirewall().evaluate(sql, profile={"tableAllowlist": ["orders"]}).denied

    @pytest.mark.parametrize("engine", _ENGINES)
    def test_hidden_column_denied_when_the_engine_reads_it(self, engine: str):
        sql = "SELECT c.id /* a /* b */ , c.ssn -- */\nFROM customers c"
        transpiled = sqlglot.transpile(sql, read="trino", write=engine)[0]
        engine_columns = {c.name for c in sqlglot.parse_one(transpiled, read=engine).find_all(sqlglot.exp.Column)}
        assert "ssn" in engine_columns
        result = SQLFirewall().evaluate(sql, profile={"columnDenylist": {"customers": ["ssn"]}})
        assert result.denied
