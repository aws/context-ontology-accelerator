# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the SQL Firewall — safety and authorization gate for SQL execution."""

from __future__ import annotations

import pytest
from coa_serve.tier2.sql_firewall import FirewallResult, NamespaceSQLScopeError, SQLFirewall, UnsafeSQLError


@pytest.mark.unit
class TestFirewallResult:
    def test_allowed_result(self):
        result = FirewallResult(denied=False, authorized_sql="SELECT 1")
        assert not result.denied
        assert result.authorized_sql == "SELECT 1"
        assert result.reason is None

    def test_denied_result(self):
        result = FirewallResult(denied=True, authorized_sql="", reason="Table access denied")
        assert result.denied
        assert result.reason == "Table access denied"

    def test_frozen_dataclass(self):
        result = FirewallResult(denied=False, authorized_sql="SELECT 1")
        with pytest.raises(AttributeError):
            result.denied = True  # type: ignore[misc]


@pytest.mark.unit
class TestSQLFirewallValidate:
    """Test the validate() method — safety checks."""

    def test_accepts_simple_select(self):
        fw = SQLFirewall()
        fw.validate("SELECT id, name FROM orders WHERE id > 5")

    def test_accepts_subquery(self):
        fw = SQLFirewall()
        fw.validate("SELECT * FROM (SELECT id FROM orders) sub WHERE sub.id > 1")

    def test_validate_redshift_dialect_accepts_engine_specific_sql(self):
        """follow-up: SQL already transpiled to Redshift must validate with
        the redshift reader. ``APPROXIMATE COUNT(DISTINCT …)`` is Redshift syntax the
        default trino reader fails to parse — validating it as trino would spuriously
        raise ``Failed to parse SQL statement`` (the exact downstream blocker)."""
        fw = SQLFirewall()
        redshift_sql = "SELECT APPROXIMATE COUNT(DISTINCT customer) AS c FROM wi766.orders"
        # Trino reader can't parse it → the old behavior would reject a legit query.
        with pytest.raises(UnsafeSQLError, match="Failed to parse SQL statement"):
            fw.validate(redshift_sql, dialect="trino")
        # Redshift reader parses it cleanly → no error (the fix).
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
        with pytest.raises(UnsafeSQLError, match="Function not allowed"):
            fw.validate("SELECT * FROM dblink('host=evil.com', 'SELECT 1')")

    def test_rejects_dangerous_function_pg_sleep(self):
        fw = SQLFirewall()
        with pytest.raises(UnsafeSQLError, match="Function not allowed"):
            fw.validate("SELECT pg_sleep(10)")

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
    """Test the evaluate() method — safety + authorization (steel thread allows)."""

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
        result = fw.evaluate("SELECT 1", profile={})
        assert not result.denied

    def test_allows_with_none_profile(self):
        fw = SQLFirewall()
        result = fw.evaluate("SELECT 1", profile=None)
        assert not result.denied

    def test_empty_profile_fail_open_characterization(self):
        # AUDIT: an empty (or None) profile imposes NO data-level restriction —
        # even a sensitive table like `payroll` is allowed. This pins the intentional
        # "sparse opt-in" default so a silent flip to fail-closed fails this test loudly.
        # OPEN QUESTION (Eric/Kun/Noah): fail-open-on-empty-profile vs fail-closed is a
        # product decision; the terminal-deny work only bites when a RESTRICTIVE profile
        # is supplied, and the profile is currently caller-controlled. Server-side
        # authorizer-resolved profiles are a SEPARATE task.
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

    def test_evaluate_accepts_dialect_kwarg(self):
        """Regression: evaluate() must accept a ``dialect`` kwarg. The shared
        execution primitive calls ``evaluate(sql, profile, namespace=..., dialect=...)``;
        a signature without ``dialect`` raises TypeError there (uncaught — only
        UnsafeSQLError is handled), so every execution fails and the agentic arm
        returns no executed SQL. Cedar is stubbed out to isolate the structural check."""
        from coa_serve.tier2.cedar_authorizer import NullCedarAuthorizer

        fw = SQLFirewall(cedar_authorizer=NullCedarAuthorizer())
        result = fw.evaluate("SELECT * FROM orders", profile={}, namespace="ns", dialect="trino")
        assert not result.denied

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
    """Test table extraction via sqlglot AST parsing."""

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

    # --- tableAllowlist ---

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

    # --- columnDenylist ---

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
        """SEMANTICS CHANGE — an empty allowlist is a restriction, not its absence.

        This previously asserted the opposite ("should not impose any restriction"),
        which documented a fail-open: the empty list was falsy, so the restriction
        was dropped and every table was allowed. A grant declaring an empty
        allowlist means "no tables permitted", and collapsing that into
        "unrestricted" turned a deny-all into an allow-all. Absence of the key still
        means unrestricted — see the test below.
        """
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


@pytest.mark.unit
class TestNamespaceSQLScope:
    """F-3: qualified SQL may not escape the requested namespace's sources."""

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

    def _validate_jdbc(self, sql: str) -> bool:
        """Direct-JDBC route: catalog supplied by the connection, so schema_only."""
        return SQLFirewall.validate_namespace_sql_scope(
            sql,
            native_databases=self._NATIVE,
            federated_catalog_schemas=self._FEDERATED,
            default_catalog="awsdatacatalog",
            schema_only=True,
        )

    def test_jdbc_two_part_federated_schema_is_allowed(self):
        """Regression (direct-JDBC engines integ): a single-source metric emits a
        bare ``schema.table`` and dispatches to direct JDBC, where the catalog comes
        from the connection, not Athena. The federated JDBC source's schema lives in
        federated_catalog_schemas under its OWN nested catalog (``sclds_a``), not in
        native_databases — so the Athena rule (pin to awsdatacatalog/native) wrongly
        denied it (502 NamespaceSQLScopeError). schema_only authorizes on the schema
        against ANY authorized catalog."""
        assert self._validate_jdbc("SELECT COUNT(*) FROM sales.orders")

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
