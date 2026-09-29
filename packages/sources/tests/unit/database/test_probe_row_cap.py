# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The cardinality probe's ROW bound (GH-131 follow-up).

``PROBE_TIMEOUT_MS`` bounds how long one probe statement may run. It does not
bound how much it reads, so a table with N string columns could still spend
N × the timeout — work proportional to schema width inside a fixed Lambda.
``PROBE_MAX_ROWS`` bounds the read itself, and both the JDBC dialects and the
Glue/Athena sampler build their probe SQL from the same two helpers so the bound
cannot apply to one path and not the other (it previously applied to neither).
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import MagicMock, patch

from coa_sources.database.connectors import dialects as d


class TestProbeGateSql:
    """The gate query counts over a capped subquery, not the whole table."""

    def test_counts_come_from_a_row_capped_subquery(self):
        sql = d.probe_gate_sql('"c"', '"s"."t"', "WHERE 1=1", row_cap=1000)
        # The cap is on the INNER read; the aggregate runs over that prefix.
        assert "LIMIT 1000" in sql
        assert sql.startswith("SELECT COUNT(*), COUNT(DISTINCT probe_col) FROM (")
        assert sql.index("LIMIT 1000") > sql.index("FROM (")

    def test_top_n_form_for_engines_without_limit(self):
        sql = d.probe_gate_sql('"c"', '"s"."t"', "WHERE 1=1", row_cap=1000, top_n=True)
        assert "TOP 1000" in sql
        assert "LIMIT" not in sql

    def test_no_order_by_is_emitted(self):
        # An ORDER BY would reintroduce the full sort the cap exists to avoid.
        assert "ORDER BY" not in d.probe_gate_sql('"c"', '"s"."t"', "", row_cap=10)


class TestProbeValuesSql:
    """The values query reads the SAME capped prefix the gate measured."""

    def test_applies_both_the_row_cap_and_the_distinct_limit(self):
        sql = d.probe_values_sql('"c"', '"s"."t"', "WHERE 1=1", row_cap=5000, limit=25)
        # Without the inner cap this query would full-scan even though the gate
        # only measured a prefix — the expensive scan, one statement later.
        assert "LIMIT 5000" in sql
        assert sql.rstrip().endswith("LIMIT 25")
        assert "SELECT DISTINCT" in sql

    def test_top_n_form_carries_both_bounds(self):
        sql = d.probe_values_sql('"c"', '"s"."t"', "", row_cap=5000, limit=25, top_n=True)
        assert "TOP 5000" in sql  # inner read cap
        assert "TOP 25" in sql  # distinct-value cap
        assert "LIMIT" not in sql


class TestRowLimitDialectFlag:
    """Only SQL Server needs TOP; everything else uses LIMIT."""

    def test_sqlserver_uses_top(self):
        assert d.SqlServerDialect().row_limit_uses_top is True

    def test_every_other_dialect_uses_limit(self):
        for dialect in (
            d.PostgresDialect(),
            d.RedshiftDialect(),
            d.MySqlDialect(),
            d.SnowflakeDialect(),
            d.OracleDialect(),
            d.Dialect(),
        ):
            assert dialect.row_limit_uses_top is False, type(dialect).__name__


class TestJdbcProbeIsBounded:
    """The JDBC probe emits the cap on both of its statements."""

    def _run_probe(self, dialect):
        conn = MagicMock()
        executed: list[str] = []

        def fake_run(_conn, sql):
            executed.append(sql)
            # Gate: 100 rows, 2 distinct → passes. Values: two short labels.
            if "COUNT(" in sql:
                return [(100, 2)]
            return [("RETURNED",), ("SHIPPED",)]

        with patch.object(d, "_run", fake_run):
            dialect.fetch_distinct_values(conn, "public", "orders", ["state"])
        return executed

    def test_both_statements_carry_the_row_cap(self):
        executed = self._run_probe(d.PostgresDialect())
        assert len(executed) == 2
        assert all(f"LIMIT {d.PROBE_MAX_ROWS}" in sql for sql in executed), executed

    def test_sqlserver_probe_uses_top_not_limit(self):
        executed = self._run_probe(d.SqlServerDialect())
        assert executed and all("LIMIT" not in sql for sql in executed), executed
        assert all(f"TOP {d.PROBE_MAX_ROWS}" in sql for sql in executed), executed


class TestAthenaProbeIsBounded:
    """The Glue path is bounded by the same helpers, not a second copy of the SQL."""

    def test_gate_and_values_queries_carry_the_row_cap(self):
        from coa_sources.database.connectors.athena_sampler import AthenaSampler

        sampler = AthenaSampler(workgroup="wg", output_location="s3://b/p/")
        executed: list[str] = []

        def fake_run(sql):
            executed.append(sql)
            if "COUNT(" in sql:
                return [["100", "2"]]
            return [["RETURNED"], ["SHIPPED"]]

        with patch.object(sampler, "_run", fake_run):
            values = sampler._sample_one("db", "orders", "state", d.MAX_ENUM_DISTINCT)

        assert values == ["RETURNED", "SHIPPED"]
        assert len(executed) == 2
        assert all(f"LIMIT {d.PROBE_MAX_ROWS}" in sql for sql in executed), executed


class TestProbeMaxRowsEnv:
    """PROBE_MAX_ROWS is configurable and can never disable the bound."""

    def _reload_with(self, value: str | None):
        env = os.environ.copy()
        env.pop("PROBE_MAX_ROWS", None)
        if value is not None:
            env["PROBE_MAX_ROWS"] = value
        with patch.dict(os.environ, env, clear=True):
            importlib.reload(d)
            try:
                return d.PROBE_MAX_ROWS
            finally:
                pass

    def teardown_method(self):
        os.environ.pop("PROBE_MAX_ROWS", None)
        importlib.reload(d)

    def test_custom_value_applied(self):
        assert self._reload_with("5000") == 5000

    def test_unset_uses_default(self):
        assert self._reload_with(None) == d._DEFAULT_PROBE_MAX_ROWS

    def test_malformed_zero_and_negative_all_fall_back(self):
        # Zero would mean "read nothing" and negative is nonsense; honouring
        # either would break sampling silently rather than bound it.
        for bad in ("not-a-number", "", "0", "-1"):
            assert self._reload_with(bad) == d._DEFAULT_PROBE_MAX_ROWS, bad
