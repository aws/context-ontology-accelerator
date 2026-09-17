# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the column-cardinality probe timeout bound (GitHub #131).

Verifies:
- Postgres/Redshift dialects emit the SET statement_timeout statement; others don't.
- The timeout statements are actually *executed* on the connection before the
  probe query (call-order canary).
- A probe that raises a timeout marks the column non-enum; discovery continues.
- The PROBE_TIMEOUT_MS env var is configurable and malformed values fall back
  safely (never crash cold start, never silently disable the bound).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestProbeTimeoutStatements:
    """Dialect.probe_timeout_statements() returns the right SQL per engine."""

    def test_postgres_emits_timeout(self):
        from coa_sources.database.connectors.dialects import PostgresDialect

        dialect = PostgresDialect()
        stmts = dialect.probe_timeout_statements()
        assert len(stmts) == 1
        assert stmts[0].startswith("SET statement_timeout = ")
        # Value should be a positive integer (ms)
        ms = int(stmts[0].split("=")[1].strip())
        assert ms > 0

    def test_redshift_inherits_postgres_timeout(self):
        from coa_sources.database.connectors.dialects import RedshiftDialect

        dialect = RedshiftDialect()
        stmts = dialect.probe_timeout_statements()
        assert len(stmts) == 1
        assert "statement_timeout" in stmts[0]

    def test_mysql_emits_nothing(self):
        from coa_sources.database.connectors.dialects import MySqlDialect

        assert MySqlDialect().probe_timeout_statements() == ()

    def test_sqlserver_emits_nothing(self):
        from coa_sources.database.connectors.dialects import SqlServerDialect

        assert SqlServerDialect().probe_timeout_statements() == ()

    def test_snowflake_emits_nothing(self):
        from coa_sources.database.connectors.dialects import SnowflakeDialect

        assert SnowflakeDialect().probe_timeout_statements() == ()

    def test_oracle_emits_nothing(self):
        from coa_sources.database.connectors.dialects import OracleDialect

        assert OracleDialect().probe_timeout_statements() == ()

    def test_base_dialect_emits_nothing(self):
        from coa_sources.database.connectors.dialects import Dialect

        assert Dialect().probe_timeout_statements() == ()


@pytest.mark.unit
class TestProbeTimeoutWiring:
    """The timeout hook is actually *called* on the connection before the probe."""

    def test_timeout_executed_before_probe(self):
        """Canary: SET statement_timeout runs before COUNT/COUNT(DISTINCT)."""
        from coa_common.domain_models import Column
        from coa_sources.database.connectors.dialects import PostgresDialect
        from coa_sources.database.connectors.jdbc import JdbcConnector

        dialect = PostgresDialect()
        timeout_stmt = dialect.probe_timeout_statements()[0]

        # Track the order of ALL execute() calls on cursors from this connection.
        executed: list[str] = []

        mock_cursor = MagicMock()

        def track_execute(sql, params=()):
            executed.append(sql)

        mock_cursor.execute.side_effect = track_execute
        mock_cursor.fetchall.return_value = [(100, 5)]  # total=100, distinct=5

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        cols = [Column(name="status", data_type="varchar", nullable=True, is_partition_key=False)]

        JdbcConnector._safe_distinct_values(dialect, mock_conn, "public", "orders", cols)

        # The timeout SET must appear before any COUNT query.
        assert len(executed) >= 2, f"Expected at least 2 execute calls, got {len(executed)}"
        assert executed[0] == timeout_stmt, f"First execute should be the timeout statement, got: {executed[0]!r}"
        assert "COUNT" in executed[1].upper(), f"Second execute should be the probe COUNT, got: {executed[1]!r}"

    def test_no_timeout_executed_for_mysql(self):
        """MySQL dialect has no timeout hook — no extra execute before probe."""
        from coa_common.domain_models import Column
        from coa_sources.database.connectors.dialects import MySqlDialect
        from coa_sources.database.connectors.jdbc import JdbcConnector

        dialect = MySqlDialect()

        executed: list[str] = []
        mock_cursor = MagicMock()

        def track_execute(sql, params=()):
            executed.append(sql)

        mock_cursor.execute.side_effect = track_execute
        mock_cursor.fetchall.return_value = [(100, 5)]

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        cols = [Column(name="status", data_type="varchar", nullable=True, is_partition_key=False)]

        JdbcConnector._safe_distinct_values(dialect, mock_conn, "mydb", "orders", cols)

        # First execute should go straight to the COUNT probe — no timeout SET.
        assert len(executed) >= 1
        assert "COUNT" in executed[0].upper()


@pytest.mark.unit
class TestProbeTimeoutGracefulDegradation:
    """A probe timeout marks the column non-enum; discovery continues."""

    def test_timeout_error_skips_column_discovery_continues(self):
        """A timeout exception in fetch_distinct_values still yields remaining columns."""
        from coa_common.domain_models import Column
        from coa_sources.database.connectors.dialects import PostgresDialect
        from coa_sources.database.connectors.jdbc import JdbcConnector

        dialect = PostgresDialect()

        call_count = {"n": 0}

        mock_cursor = MagicMock()

        def side_effect_execute(sql, params=()):
            if "statement_timeout" in sql.lower():
                return  # timeout setup OK
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First COUNT probe — simulate a timeout
                raise Exception("canceling statement due to statement timeout")
            # Subsequent calls succeed
            return

        mock_cursor.execute.side_effect = side_effect_execute
        mock_cursor.fetchall.return_value = []

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        cols = [
            Column(name="big_col", data_type="varchar", nullable=True, is_partition_key=False),
            Column(name="small_col", data_type="varchar", nullable=True, is_partition_key=False),
        ]

        # Should NOT raise — timeout is swallowed inside the per-column try/except
        # in fetch_distinct_values.
        JdbcConnector._safe_distinct_values(dialect, mock_conn, "public", "orders", cols)

        # Neither column should have distinct_values set (since mock returns empty)
        for col in cols:
            assert not col.distinct_values

    def test_timeout_logs_distinct_probe_timeout_event(self):
        """The distinct_probe_timeout structured event fires on timeout errors."""
        from coa_sources.database.connectors import dialects

        # Create a connection whose probe COUNT raises a timeout.
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("canceling statement due to statement timeout")
        mock_cursor.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        dialect = dialects.PostgresDialect()
        with patch.object(dialects.logger, "warning") as mock_warn:
            dialect.fetch_distinct_values(mock_conn, "public", "orders", ["big_col"])

        # The distinct_probe_timeout event should have fired.
        timeout_calls = [c for c in mock_warn.call_args_list if c[0][0] == "distinct_probe_timeout"]
        assert len(timeout_calls) >= 1, (
            f"Expected distinct_probe_timeout log, got: {[c[0][0] for c in mock_warn.call_args_list]}"
        )


@pytest.mark.unit
class TestProbeTimeoutEnvVar:
    """PROBE_TIMEOUT_MS env var: configurable, malformed values handled safely."""

    def test_custom_value_applied(self):
        """A valid integer env var overrides the default."""
        with patch.dict(os.environ, {"PROBE_TIMEOUT_MS": "5000"}):
            # Re-import to pick up the new env var (module-level constant).
            import importlib

            import coa_sources.database.connectors.dialects as mod

            importlib.reload(mod)
            try:
                assert mod.PROBE_TIMEOUT_MS == 5000
                pg = mod.PostgresDialect()
                stmts = pg.probe_timeout_statements()
                assert "5000" in stmts[0]
            finally:
                # Restore the module to its default state.
                os.environ.pop("PROBE_TIMEOUT_MS", None)
                importlib.reload(mod)

    def test_malformed_value_falls_back_safely(self):
        """A non-integer PROBE_TIMEOUT_MS logs a warning and uses the default."""
        with patch.dict(os.environ, {"PROBE_TIMEOUT_MS": "not-a-number"}):
            import importlib

            import coa_sources.database.connectors.dialects as mod

            importlib.reload(mod)
            try:
                assert mod.PROBE_TIMEOUT_MS == mod._DEFAULT_PROBE_TIMEOUT_MS
                assert mod.PROBE_TIMEOUT_MS > 0  # bound is NOT disabled
            finally:
                os.environ.pop("PROBE_TIMEOUT_MS", None)
                importlib.reload(mod)

    def test_zero_value_falls_back_safely(self):
        """PROBE_TIMEOUT_MS=0 is non-positive and falls back to default."""
        with patch.dict(os.environ, {"PROBE_TIMEOUT_MS": "0"}):
            import importlib

            import coa_sources.database.connectors.dialects as mod

            importlib.reload(mod)
            try:
                assert mod.PROBE_TIMEOUT_MS == mod._DEFAULT_PROBE_TIMEOUT_MS
            finally:
                os.environ.pop("PROBE_TIMEOUT_MS", None)
                importlib.reload(mod)

    def test_negative_value_falls_back_safely(self):
        """PROBE_TIMEOUT_MS=-1 falls back to default (fail-closed)."""
        with patch.dict(os.environ, {"PROBE_TIMEOUT_MS": "-1"}):
            import importlib

            import coa_sources.database.connectors.dialects as mod

            importlib.reload(mod)
            try:
                assert mod.PROBE_TIMEOUT_MS == mod._DEFAULT_PROBE_TIMEOUT_MS
            finally:
                os.environ.pop("PROBE_TIMEOUT_MS", None)
                importlib.reload(mod)

    def test_unset_uses_default(self):
        """When PROBE_TIMEOUT_MS is not set, the default is used."""
        env = os.environ.copy()
        env.pop("PROBE_TIMEOUT_MS", None)
        with patch.dict(os.environ, env, clear=True):
            import importlib

            import coa_sources.database.connectors.dialects as mod

            importlib.reload(mod)
            try:
                assert mod.PROBE_TIMEOUT_MS == mod._DEFAULT_PROBE_TIMEOUT_MS
            finally:
                importlib.reload(mod)


@pytest.mark.unit
class TestProbeTimeoutFailsClosed:
    """When the timeout bound cannot be applied, sampling is skipped, not run unbounded."""

    def test_timeout_setup_failure_skips_sampling_entirely(self):
        """GH-131 fail-closed: if `SET statement_timeout` raises, no COUNT probe runs.

        A dialect that declares timeout statements is asserting its probe needs
        bounding. Proceeding without the bound would reintroduce the exact
        unbounded COUNT(DISTINCT ...) that burns the 15-minute discovery Lambda,
        so the correct behaviour is to give up enum detection for this table.
        """
        from coa_common.domain_models import Column
        from coa_sources.database.connectors.dialects import PostgresDialect
        from coa_sources.database.connectors.jdbc import JdbcConnector

        dialect = PostgresDialect()

        executed: list[str] = []
        mock_cursor = MagicMock()

        def track_execute(sql, params=()):
            executed.append(sql)
            if "statement_timeout" in sql.lower():
                raise Exception("permission denied to set parameter")

        mock_cursor.execute.side_effect = track_execute
        mock_cursor.fetchall.return_value = [(100, 5)]

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        cols = [Column(name="status", data_type="varchar", nullable=True, is_partition_key=False)]

        JdbcConnector._safe_distinct_values(dialect, mock_conn, "public", "orders", cols)

        # The SET was attempted and failed; nothing else may be issued.
        assert len(executed) == 1, f"Expected only the failed SET, got: {executed!r}"
        assert "statement_timeout" in executed[0].lower()
        assert not any("COUNT" in sql.upper() for sql in executed), (
            f"Unbounded probe ran despite the timeout bound failing: {executed!r}"
        )
        # And the column is left un-enumerated rather than half-populated.
        assert cols[0].distinct_values in (None, [])
