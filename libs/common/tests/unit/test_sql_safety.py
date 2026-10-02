# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared SQL side-effect policy."""

from __future__ import annotations

from enum import Enum

import pytest
from coa_common.sql_safety import (
    DANGEROUS_SQL_AST_NODE_NAMES,
    contains_mysql_executable_comment,
    dangerous_sql_functions,
    is_trino_dialect,
    select_tier1_sql_expression,
)


@pytest.mark.unit
def test_postgres_policy_blocks_state_changes_and_locks() -> None:
    blocked = dangerous_sql_functions("POSTGRESQL")

    assert {
        "setval",
        "nextval",
        "pg_advisory_lock",
        "pg_logical_emit_message",
        "pg_notify",
        "pg_read_file",
        "lo_create",
        "lo_from_bytea",
        "lo_put",
        "lowrite",
    } <= blocked
    assert {"Lock", "NextValueFor"} <= DANGEROUS_SQL_AST_NODE_NAMES


@pytest.mark.unit
def test_mysql_policy_blocks_delay_file_and_lock_functions() -> None:
    blocked = dangerous_sql_functions("mysql")

    assert {"sleep", "benchmark", "load_file", "get_lock"} <= blocked


@pytest.mark.unit
def test_unknown_dialect_fails_closed_over_known_function_sets() -> None:
    blocked = dangerous_sql_functions("external_dialect")

    assert {"setval", "sleep", "system$wait", "pg_read_file", "opendatasource"} <= blocked


@pytest.mark.unit
def test_mysql_executable_comment_detection_ignores_string_literals() -> None:
    assert contains_mysql_executable_comment("SELECT /*!50000 SLEEP(10), */ 1", "MYSQL")
    assert contains_mysql_executable_comment("SELECT /*! STRAIGHT_JOIN */ * FROM orders", "mariadb")
    assert contains_mysql_executable_comment(
        "SELECT `metric\\` /*!50000 , SLEEP(10) */ FROM `orders`",
        "MYSQL",
    )
    assert not contains_mysql_executable_comment("SELECT '/*!50000 SLEEP(10) */'", "MYSQL")
    assert not contains_mysql_executable_comment(
        "SELECT `metric``/*!50000 SLEEP(10) */` FROM `orders`",
        "MYSQL",
    )
    assert not contains_mysql_executable_comment("SELECT /*!50000 SLEEP(10) */ 1", "POSTGRESQL")


@pytest.mark.unit
def test_tier1_expression_prefers_trino_then_falls_back_to_first() -> None:
    class _Dialect(Enum):
        TRINO = "TRINO"

    dialects = [
        {"dialect": "SNOWFLAKE", "expression": "SNOWFLAKE_EXPR"},
        {"dialect": _Dialect.TRINO, "expression": "TRINO_EXPR"},
    ]

    assert is_trino_dialect(_Dialect.TRINO)
    assert select_tier1_sql_expression(dialects) == "TRINO_EXPR"
    assert select_tier1_sql_expression(dialects[:1]) == "SNOWFLAKE_EXPR"
    assert select_tier1_sql_expression([]) == ""
