# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared SQL side-effect policy for onboarding and serve-time validation.

The metric and serve packages own their sqlglot parsers, while shared pure
helpers here keep statement shape and dangerous-operation decisions identical.
This avoids a metric being accepted during onboarding and rejected (or
executed) by the serve firewall later.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import cache
from typing import Any

# Dangerous in every dialect. Most are PostgreSQL names, but a data source can
# expose user-defined functions under the same names; treating them as safe in
# another dialect would make the security policy depend on catalog contents.
_ALWAYS_BLOCKED_FUNCTIONS = frozenset(
    {
        "copy",
        "dblink",
        "dblink_connect",
        "dblink_connect_u",
        "dblink_disconnect",
        "dblink_exec",
        "lo_close",
        "lo_creat",
        "lo_create",
        "lo_export",
        "lo_from_bytea",
        "lo_import",
        "lo_open",
        "lo_put",
        "lo_truncate",
        "lo_truncate64",
        "lo_unlink",
        "lowrite",
        "pg_ls_dir",
        "pg_read_binary_file",
        "pg_read_file",
        "pg_sleep",
        "pg_stat_file",
    }
)

_POSTGRES_BLOCKED_FUNCTIONS = frozenset(
    {
        "nextval",
        "pg_advisory_lock",
        "pg_advisory_lock_shared",
        "pg_advisory_unlock",
        "pg_advisory_unlock_all",
        "pg_advisory_unlock_shared",
        "pg_advisory_xact_lock",
        "pg_advisory_xact_lock_shared",
        "pg_backup_start",
        "pg_backup_stop",
        "pg_cancel_backend",
        "pg_create_restore_point",
        "pg_log_backend_memory_contexts",
        "pg_logical_emit_message",
        "pg_notify",
        "pg_reload_conf",
        "pg_replication_origin_advance",
        "pg_replication_origin_session_reset",
        "pg_replication_origin_session_setup",
        "pg_replication_origin_xact_setup",
        "pg_rotate_logfile",
        "pg_switch_wal",
        "pg_terminate_backend",
        "pg_try_advisory_lock",
        "pg_try_advisory_lock_shared",
        "pg_try_advisory_xact_lock",
        "pg_try_advisory_xact_lock_shared",
        "pg_wal_replay_pause",
        "pg_wal_replay_resume",
        "set_config",
        "setval",
    }
)

_MYSQL_BLOCKED_FUNCTIONS = frozenset(
    {
        "benchmark",
        "get_lock",
        "is_free_lock",
        "is_used_lock",
        "last_insert_id",
        "load_file",
        "master_pos_wait",
        "release_all_locks",
        "release_lock",
        "sleep",
        "source_pos_wait",
        "sys_eval",
        "sys_exec",
        "wait_for_executed_gtid_set",
    }
)

_SNOWFLAKE_BLOCKED_FUNCTIONS = frozenset(
    {
        "system$abort_session",
        "system$cancel_query",
        "system$send_email",
        "system$send_snowflake_notification",
        "system$set_return_value",
        "system$wait",
    }
)

_TSQL_BLOCKED_FUNCTIONS = frozenset(
    {
        "opendatasource",
        "openquery",
        "openrowset",
        "xp_cmdshell",
    }
)

_DIALECT_ALIASES = {
    "postgresql": "postgres",
    "ansi_sql": "postgres",
    "mariadb": "mysql",
    "mssql": "tsql",
    "sqlserver": "tsql",
}

_DIALECT_BLOCKED_FUNCTIONS = {
    "postgres": _POSTGRES_BLOCKED_FUNCTIONS,
    "redshift": _POSTGRES_BLOCKED_FUNCTIONS,
    "mysql": _MYSQL_BLOCKED_FUNCTIONS,
    "snowflake": _SNOWFLAKE_BLOCKED_FUNCTIONS,
    "tsql": _TSQL_BLOCKED_FUNCTIONS,
}

_ALL_KNOWN_BLOCKED_FUNCTIONS = frozenset().union(
    _ALWAYS_BLOCKED_FUNCTIONS,
    _POSTGRES_BLOCKED_FUNCTIONS,
    _MYSQL_BLOCKED_FUNCTIONS,
    _SNOWFLAKE_BLOCKED_FUNCTIONS,
    _TSQL_BLOCKED_FUNCTIONS,
)

# sqlglot node names that are read-shaped but still mutate database/session
# state. Names rather than classes keep coa-common independent of sqlglot.
DANGEROUS_SQL_AST_NODE_NAMES = frozenset({"Lock", "NextValueFor"})

_DANGEROUS_TSQL_TABLE_HINTS = frozenset(
    {
        "holdlock",
        "paglock",
        "readcommittedlock",
        "repeatableread",
        "rowlock",
        "serializable",
        "tablock",
        "tablockx",
        "updlock",
        "xlock",
    }
)


def _normalize_dialect(dialect: Any) -> str:
    value = getattr(dialect, "value", dialect)
    normalized = str(value or "").strip().lower()
    return _DIALECT_ALIASES.get(normalized, normalized)


def _last_sql_word_before(sql: str, end: int) -> str:
    """Return the last non-comment, non-quoted SQL word before ``end``."""
    last_word = ""
    i = 0
    while i < end:
        if sql.startswith("--", i):
            newline = sql.find("\n", i + 2, end)
            i = end if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", i):
            # T-SQL permits nested block comments. Stopping at the first
            # closing delimiter can expose words from the outer comment as
            # executable tokens (for example a fake ``AS`` before a legacy
            # table hint).
            depth = 1
            i += 2
            while i < end and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue

        char = sql[i]
        if char in {"'", '"', "`", "["}:
            closing = "]" if char == "[" else char
            i += 1
            while i < end:
                if sql[i] == closing:
                    # ``]]`` escapes a closing bracket in a T-SQL delimited
                    # identifier, just as doubled quotes escape quote-delimited
                    # identifiers and strings.
                    if i + 1 < end and sql[i + 1] == closing:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        if char.isalnum() or char in {"_", "$", "#", "@"}:
            start = i
            i += 1
            while i < end and (sql[i].isalnum() or sql[i] in {"_", "$", "#", "@"}):
                i += 1
            last_word = sql[start:i]
            continue

        i += 1

    return last_word.lower()


def is_trino_dialect(dialect: Any) -> bool:
    """Return whether a string or generated enum denotes the TRINO dialect."""
    return _normalize_dialect(dialect) == "trino"


def select_tier1_sql_expression(dialects: Sequence[Mapping[str, Any]]) -> str:
    """Select the expression Tier 1 will execute.

    Tier 1 consumes Trino SQL. It prefers an explicitly labelled TRINO entry
    and retains the historical fallback to the first entry when one is absent.
    Onboarding uses this same helper so it validates the exact expression and
    ordering the resolver will later execute.
    """
    if not dialects:
        return ""
    for entry in dialects:
        if is_trino_dialect(entry.get("dialect", "")):
            return str(entry.get("expression") or "")
    return str(dialects[0].get("expression") or "")


@cache
def dangerous_sql_functions(dialect: str | None) -> frozenset[str]:
    """Return side-effecting function names blocked for ``dialect``.

    Known dialects get their family-specific list plus the universal baseline.
    Unknown or externally preserved OSI dialects get the union of every known
    dangerous function. That conservative fallback is intentional: accepting a
    dialect without a security profile must not weaken the write/serve gate.
    """
    normalized = _normalize_dialect(dialect)
    specific = _DIALECT_BLOCKED_FUNCTIONS.get(normalized)
    if specific is None:
        return _ALL_KNOWN_BLOCKED_FUNCTIONS
    return _ALWAYS_BLOCKED_FUNCTIONS | specific


def dangerous_sql_ast_reason(node: Any, dialect: Any = None, sql: str = "") -> str | None:
    """Describe a dangerous sqlglot node without importing sqlglot here."""
    node_name = type(node).__name__
    if node_name in DANGEROUS_SQL_AST_NODE_NAMES:
        return node_name

    if node_name == "WithTableHint":
        hints = {
            str(getattr(expression, "name", "")).strip().lower()
            for expression in (getattr(node, "expressions", None) or [])
        }
        dangerous = sorted(hints & _DANGEROUS_TSQL_TABLE_HINTS)
        if dangerous:
            return f"WithTableHint({', '.join(dangerous)})"

    # SQL Server's legacy ``FROM table (TABLOCKX)`` syntax is parsed by
    # sqlglot as a table-valued function, not as WithTableHint. The even older
    # no-parentheses form is parsed as an alias. Inspect the Table wrapper so
    # both encodings share the same policy without regex-parsing SQL text.
    if node_name == "Table" and _normalize_dialect(dialect) == "tsql":
        table_args = getattr(node, "args", {})
        table_expression = table_args.get("this")
        legacy_hints: set[str] = set()
        if type(table_expression).__name__ == "Anonymous":
            for expression in getattr(table_expression, "expressions", None) or []:
                expression_name = type(expression).__name__
                expression_args = getattr(expression, "args", {})
                if expression_name == "Column":
                    identifier = expression_args.get("this")
                    if (
                        type(identifier).__name__ != "Identifier"
                        or bool(getattr(identifier, "args", {}).get("quoted"))
                        or any(expression_args.get(part) is not None for part in ("table", "db", "catalog"))
                    ):
                        continue
                elif expression_name != "Var":
                    # A real table-valued function can receive parameters,
                    # literals, calls, and arbitrary expressions. Only the
                    # simple unquoted identifier shape is ambiguous with
                    # SQL Server's legacy ``table (TABLOCKX)`` hint syntax.
                    continue
                legacy_hints.add(str(getattr(expression, "name", "")).strip().lower())
        alias = table_args.get("alias")
        if alias is not None:
            alias_identifier = getattr(alias, "args", {}).get("this")
            alias_meta = getattr(alias_identifier, "meta", {})
            alias_start = alias_meta.get("start")
            explicit_alias = bool(getattr(alias_identifier, "args", {}).get("quoted")) or (
                isinstance(alias_start, int) and _last_sql_word_before(sql, alias_start) == "as"
            )
            if not explicit_alias:
                legacy_hints.add(str(getattr(alias, "name", "")).strip().lower())
        dangerous = sorted(legacy_hints & _DANGEROUS_TSQL_TABLE_HINTS)
        if dangerous:
            return f"LegacyTableHint({', '.join(dangerous)})"

    return None


def executable_select_shape_error(parsed: Any) -> str | None:
    """Return an error for syntactically parsed but non-executable SELECTs.

    Uses sqlglot's public expression attributes through duck typing so the
    common package does not need to depend on sqlglot itself.
    """
    for node in parsed.walk():
        if type(node).__name__ != "Select":
            continue

        expressions = list(getattr(node, "expressions", None) or [])
        if not expressions:
            return "SELECT statement must include at least one projection."

        has_direct_star = False
        for expression in expressions:
            unalias = getattr(expression, "unalias", None)
            projection = unalias() if callable(unalias) else expression
            projection_name = type(projection).__name__
            projection_this = getattr(projection, "args", {}).get("this")
            if projection_name == "Star" or (projection_name == "Column" and type(projection_this).__name__ == "Star"):
                has_direct_star = True
                break

        if has_direct_star and not getattr(node, "args", {}).get("from_"):
            return (
                "SELECT * without a FROM source is not executable. "
                "Add 'FROM <source_table>' or select a scalar expression."
            )

    return None


def contains_mysql_executable_comment(sql: str, dialect: Any) -> bool:
    """Detect MySQL/MariaDB ``/*! ... */`` comments outside string literals."""
    if _normalize_dialect(dialect) != "mysql":
        return False

    quote = ""
    i = 0
    while i < len(sql):
        char = sql[i]
        if quote:
            # MySQL string literals accept backslash escapes, but backtick-
            # quoted identifiers escape an embedded backtick by doubling it.
            # Treating ``\``` as an escape would skip the identifier's real
            # closing delimiter and hide a following executable comment.
            if char == "\\" and quote != "`":
                i += 2
                continue
            if char == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    i += 2
                    continue
                quote = ""
            i += 1
            continue

        if char in {"'", '"', "`"}:
            quote = char
            i += 1
            continue
        if sql.startswith("/*!", i):
            return True
        i += 1

    return False
