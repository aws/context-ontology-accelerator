# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metric validation module.

Validates metric definitions against data source metadata and SQL syntax.

Checks (per Metric Onboarding Service LLD §5.1):
  1. SQL parses without syntax errors in declared dialect (sqlglot) → ERROR (blocks)
  2. All table references exist in OMS for the metric's dataSourceId → WARNING
  3. All column references exist and match expected types → WARNING
  4. Dimension columns exist in referenced tables → WARNING
  5. Filter columns exist and type matches operator → WARNING
  6. :governedMetricFor references a valid ontology class → WARNING

Philosophy: "Author early, validate continuously"
- SQL syntax, read-only semantics, and executable statement shape block writes
- Checks 2-6 produce soft warnings — metric is still published
- Re-scan impact detection triggers re-validation when schema changes
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from functools import cache
from typing import Any

import structlog
from coa_common.sql_safety import (
    contains_mysql_executable_comment,
    dangerous_sql_ast_reason,
    dangerous_sql_functions,
    executable_select_shape_error,
    is_trino_dialect,
    select_tier1_sql_expression,
)

logger = structlog.get_logger(__name__)


# ── Result types ────────────────────────────────────────────────────────


class Severity(Enum):
    """Validation check severity."""

    ERROR = "error"
    WARNING = "warning"


# Onboarding-blocking checks (CreateMetric/UpdateMetric return 400 on these).
# Scoped by check NAME rather than by ERROR severity: the SQL syntax + shape
# checks (1/1b) mirror the serve-time SQL firewall's SELECT-only rule, so a
# metric that fails them would persist only to fail at query time (#617/#1050).
# Other checks can also be ERROR-severity — e.g. `table_reference` on provable
# sourceTable absence (see below) — but those have their own dedicated gate
# (check_source_table_exists) and must NOT block here, or create/update would
# 400 in a case the contract says should publish (Kun's review, !1133).
BLOCKING_CHECKS = frozenset({"sql_syntax", "sql_shape"})


@dataclass
class ValidationCheck:
    """A single validation check result."""

    check: str
    severity: Severity
    passed: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    """Aggregate result of metric validation."""

    valid: bool
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]

    @classmethod
    def from_checks(cls, checks: list[ValidationCheck]) -> ValidationResult:
        """Build a ValidationResult from a list of individual check results."""
        errors = []
        warnings = []
        for c in checks:
            if c.passed:
                continue
            entry = {
                "check": c.check,
                "severity": c.severity.value,
                "message": c.message,
                "details": c.details,
            }
            if c.severity == Severity.ERROR:
                errors.append(entry)
            else:
                warnings.append(entry)
        return cls(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )


# ── Data source lookup interface ────────────────────────────────────────
# Base classes and implementations live in lookups.py (per review — keeps
# validator.py focused on check orchestration).

from coa_metrics.lookups import (  # noqa: E402, F401
    ColumnMetadata,
    DataSourceLookup,
    NeptuneOntologyLookup,
    OntologyLookup,
    SmusCatalogDataSourceLookup,
)

# Backward compat alias — new code should use SmusCatalogDataSourceLookup.
DynamoDBDataSourceLookup = None  # type: ignore[assignment]


# ── Main validation function ────────────────────────────────────────────

_DIALECT_MAP: dict[str, str] = {
    "trino": "trino",
    "postgresql": "postgres",
    "redshift": "redshift",
    "mysql": "mysql",
    "snowflake": "snowflake",
    "databricks": "databricks",
}


def _resolve_dialect(dialect: str) -> str | None:
    """Resolve a dialect string to a sqlglot dialect name (case-insensitive).

    Internal dialects use COA names (for example ``POSTGRESQL`` → ``postgres``),
    while lenient OSI import can preserve a dialect outside the internal enum.
    If sqlglot knows that preserved dialect, validate with its real parser
    rather than silently falling back to the generic grammar.
    """
    normalized = dialect.strip().lower()
    mapped = _DIALECT_MAP.get(normalized)
    if mapped:
        return mapped

    import sqlglot

    try:
        sqlglot.Dialect.get_or_raise(normalized)
    except ValueError:
        return None
    return normalized


_COMMENT_PATTERN = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)

# Pre-parse denylist — mirrors the serve firewall's ``_BLOCKED_STATEMENTS``
# (``packages/context-manager/src/coa_serve/tier2/sql_firewall.py``)
# so DML hidden in an otherwise-unparseable string (e.g.
# ``DELETE FROM orders WHERE (((``) is still caught, plus the admin/DML verbs
# the firewall covers via its allowlist stage. Keep the two in sync.
#
# Anchored at ``^\s*`` — it only ever sees the FIRST statement. Stacked forms
# (``SELECT 1; TRUNCATE TABLE x``) are the AST walk's job when the SQL parses;
# when it does not, ``_blocked_statement_segment`` re-applies this pattern to
# each ``;``-segment so the stacked payload is still caught.
_BLOCKED_STATEMENTS = re.compile(
    r"^\s*("
    r"MSCK\s+REPAIR|CREATE|DROP|ALTER|INSERT|DELETE|UPDATE|MERGE|UNLOAD|PREPARE|EXECUTE|EXPLAIN"
    r"|TRUNCATE|GRANT|REVOKE|COPY|CALL|VACUUM|SET|ANALYZE|COMMENT|LOAD\s+DATA|RENAME|OPTIMIZE|PUT|REFRESH"
    r")\b",
    re.IGNORECASE,
)

_DATA_MODIFYING_MESSAGE = (
    "Expression contains a data-modifying operation ({detail}) — the serve-time "
    "SQL firewall rejects these. Remove the data-modifying clause; metric "
    "expressions must be read-only."
)

# AST denylist, by sqlglot node name. This is the load-bearing half of the DML
# block: unlike the ``^\s*``-anchored regex it walks EVERY parsed statement, so
# it is what catches stacked forms (``SELECT 1; TRUNCATE TABLE x``) and DML
# nested in a CTE (``WITH d AS (DELETE ...) SELECT * FROM d``).
#
# ``Command`` is the high-leverage entry: sqlglot funnels every verb it has no
# dedicated node for (CALL, VACUUM, OPTIMIZE, RENAME TABLE, ALTER SESSION, …)
# into it, so one name covers an open-ended set of admin statements. It cannot
# over-reach onto metric SQL — a read-only SELECT never parses to a Command.
#
# Aggregate fragments are deliberately absent: ``COUNT(*)``/``SUM(x)`` parse to
# ``exp.Count``/``exp.Sum``, which are expressions, not data-modifying
# statements. ``check_select_shape`` rejects them separately.
_DATA_MODIFYING_NODE_NAMES: tuple[str, ...] = (
    "Command",
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Into",
    "Create",
    "Drop",
    "Alter",
    "TruncateTable",
    "Grant",
    "Revoke",
    "Copy",
    "Set",
    "Analyze",
    "Comment",
    "LoadData",
    "AlterSession",
)


@cache
def _data_modifying_nodes() -> tuple[type, ...]:
    """Resolve ``_DATA_MODIFYING_NODE_NAMES`` to sqlglot node classes.

    Resolved lazily (and memoized) so importing this module does not pull in
    sqlglot — every caller here imports it inside the function for Lambda
    cold-start reasons. Names absent from the installed sqlglot are skipped
    rather than raising, so a version bump that renames a node degrades to a
    narrower denylist instead of breaking every metric write.

    Returns:
        The tuple of node classes to use with ``isinstance``.
    """
    import sqlglot

    return tuple(node for name in _DATA_MODIFYING_NODE_NAMES if (node := getattr(sqlglot.exp, name, None)))


def _blocked_statement_segment(sql: str) -> bool:
    r"""Check every ``;``-separated segment against ``_BLOCKED_STATEMENTS``.

    Used only when ``sqlglot.parse`` FAILS. The AST walk is the authority for
    parseable SQL, but an unparseable string gets no walk at all — and a
    stacked payload is unparseable precisely because of its second statement
    (``SELECT 1; MSCK REPAIR TABLE t`` → ParseError, ``'; DROP TABLE users; --``
    → TokenError). Scanning each segment restores the block those cases would
    otherwise slip past, since the ``^\s*`` anchor only ever sees the leading
    SELECT.

    Deliberately not applied on the parse-SUCCESS path: a segment scan cannot
    tell a statement separator from a ``;`` inside a string literal, so running
    it there would hard-block read-only SQL the AST walk correctly accepts.

    Args:
        sql: The metric SQL expression.

    Returns:
        True if any segment begins with a data-modifying verb.
    """
    return any(_BLOCKED_STATEMENTS.match(segment) for segment in _COMMENT_PATTERN.sub(" ", sql).split(";"))


def check_data_modifying(sql: str, dialect: str = "") -> str | None:
    """Reject a metric expression that modifies data (issue #161).

    DML/DDL is a hard block because it is a security boundary. Statement
    syntax and executable SELECT shape are validated separately by
    ``check_select_shape``. Data-modifying operations are caught two ways so
    neither path alone has to be complete:

    1. A pre-parse regex (``_BLOCKED_STATEMENTS``, comments stripped first) —
       catches DML that sqlglot cannot parse. Anchored at statement start, so
       it only sees the first statement; when the parse FAILS it is re-run per
       ``;``-segment (``_blocked_statement_segment``), which is what catches a
       stacked payload whose second statement is what broke the parser.
    2. A full-AST walk over every parsed statement against
       ``_DATA_MODIFYING_NODE_NAMES`` — catches DML nested in a CTE
       (``WITH d AS (DELETE ...) SELECT * FROM d``), trailing a safe one
       (``SELECT 1; DROP TABLE x``), or using a verb the regex omits, none of
       which the regex can see. This is the authoritative half.

    Args:
        sql: The metric SQL expression.
        dialect: The metric dialect (e.g. POSTGRESQL); used for parsing.

    Returns:
        None if the expression contains no recognized data-modifying
        operation, otherwise a human-actionable error message. Callers must
        also run ``check_select_shape`` before persistence.
    """
    import sqlglot

    if _BLOCKED_STATEMENTS.match(_COMMENT_PATTERN.sub(" ", sql)):
        return _DATA_MODIFYING_MESSAGE.format(detail="statement type")

    try:
        statements = sqlglot.parse(sql, read=_resolve_dialect(dialect))
    except (sqlglot.errors.SqlglotError, RecursionError):
        # No AST to walk, so the regex is the only line of defence left — and
        # the leading-statement anchor is blind to exactly the payloads that
        # broke the parse. Re-run it per ``;``-segment before conceding.
        if _blocked_statement_segment(sql):
            return _DATA_MODIFYING_MESSAGE.format(detail="statement type")
        # Unparseable AND no segment looks data-modifying → not provably
        # data-modifying. ``check_select_shape`` is responsible for rejecting
        # the parser failure before persistence.
        #
        # SqlglotError (not just ParseError) because TokenError is a SIBLING of
        # ParseError, not a subclass — ``'; DROP TABLE users; --`` raises it.
        # RecursionError comes from the recursive-descent parser on deeply
        # nested parens.
        return None

    data_modifying_nodes = _data_modifying_nodes()
    for statement in statements:
        if statement is None:
            continue
        for node in statement.walk():
            if isinstance(node, data_modifying_nodes):
                return _DATA_MODIFYING_MESSAGE.format(detail=type(node).__name__)
    return None


def check_select_shape(sql: str, dialect: str = "") -> str | None:
    """Check that a metric expression is a full SELECT statement.

    Mirrors the serve-time SQL firewall's statement-shape rule
    (``packages/context-manager/src/coa_serve/tier2/sql_firewall.py``
    — ``_SAFE_STATEMENT_TYPES``): only a top-level SELECT or set operation
    (UNION/INTERSECT/EXCEPT) is executable at Tier 1. Aggregate fragments
    like ``COUNT(*)`` or ``SUM(orders.total)`` parse fine but are rejected
    by the firewall at query time, so they must be rejected at onboarding —
    keep this in sync with the firewall so ``validate`` predicts serve-time
    acceptance.

    Also mirrors the firewall's deep scan and shared
    :mod:`coa_common.sql_safety` policy: a data-modifying node, locking clause,
    or dangerous function nested anywhere in the AST passes the top-level shape
    check but is rejected by the firewall, so it must be rejected here too for
    early feedback.

    Finally, require every SELECT in the tree to be executable on its own:
    it must have a projection, and a direct ``*`` projection must have a FROM
    source. Constants and aggregates such as ``SELECT 1`` and
    ``SELECT COUNT(*)`` remain valid without FROM.

    Args:
        sql: The metric SQL expression.
        dialect: The metric dialect (e.g. POSTGRESQL); used for parsing.

    Returns:
        None if the expression is a full SELECT statement, otherwise a
        human-actionable error message.
    """
    import sqlglot

    if contains_mysql_executable_comment(sql, dialect):
        return "MySQL executable comments are not allowed in metric SQL."

    safe_statement_types: tuple[type[sqlglot.exp.Expression], ...] = (
        sqlglot.exp.Select,
        sqlglot.exp.Union,
        sqlglot.exp.Intersect,
        sqlglot.exp.Except,
    )

    resolved_dialect = _resolve_dialect(dialect)
    try:
        parsed = sqlglot.parse_one(sql, read=resolved_dialect)
    except (sqlglot.errors.SqlglotError, RecursionError) as exc:
        # SqlglotError, not ParseError: TokenError is a sibling of ParseError,
        # and RecursionError comes from deeply nested parens. Normalize every
        # parser failure into a human-actionable message for both persistence
        # handlers and the explicit validate endpoint.
        return f"SQL expression could not be parsed ({dialect}): {exc}"

    if not isinstance(parsed, safe_statement_types):
        return (
            f"Expression must be a full SELECT statement — got a "
            f"{type(parsed).__name__} fragment. The serve-time SQL firewall only "
            f"executes SELECT statements; wrap the fragment, e.g. "
            f"'SELECT {sql.strip()} FROM <source_table>'."
        )

    shape_error = executable_select_shape_error(parsed)
    if shape_error:
        return shape_error

    # Deep scan: data-modifying nodes must not appear anywhere in the AST,
    # even nested in CTEs/subqueries of an otherwise-safe SELECT. Shares
    # check_data_modifying's denylist so the two cannot drift apart.
    for node in parsed.walk():
        if isinstance(node, _data_modifying_nodes()):
            return (
                f"Expression contains a data-modifying operation "
                f"({type(node).__name__}) nested inside the statement — the "
                f"serve-time SQL firewall rejects these. Remove the "
                f"data-modifying clause; metric expressions must be read-only."
            )
        dangerous_reason = dangerous_sql_ast_reason(node, resolved_dialect or dialect, sql)
        if dangerous_reason:
            return (
                f"Expression contains an unsafe operation ({dangerous_reason}) that can lock or mutate database state."
            )

    blocked_functions = dangerous_sql_functions(resolved_dialect or dialect)
    for func in parsed.find_all(sqlglot.exp.Anonymous, sqlglot.exp.Func):
        func_name = getattr(func, "name", "").lower()
        if func_name in blocked_functions:
            return (
                f"Expression uses forbidden function '{func_name}' — the serve-time SQL firewall rejects this function."
            )

    return None


def check_tier1_execution_shape(dialects: list[dict[str, Any]]) -> str | None:
    """Validate the exact expression Tier 1 will execute as Trino SQL.

    Each expression is also validated in its declared dialect, but the resolver
    prefers TRINO and otherwise falls back to the first entry before sending it
    to the Trino firewall/executor. This extra gate prevents dialect-valid SQL
    (for example TSQL ``TOP``) from being persisted when Tier 1 cannot parse it.
    """
    selected = select_tier1_sql_expression(dialects)
    if not selected:
        return "Tier 1 selected an empty SQL expression."

    error = check_select_shape(selected, "TRINO")
    if error:
        return f"Tier 1 executes the selected expression as TRINO SQL, but it is not executable: {error}"
    return None


def validate_metric(
    metric_body: dict[str, Any],
    data_sources_lookup: DataSourceLookup | None = None,
    ontology_lookup: OntologyLookup | None = None,
    namespace: str = "",
) -> ValidationResult:
    """Validate a metric definition against OMS metadata and ontology.

    Args:
        metric_body: The metric definition (API format or internal dict).
        data_sources_lookup: Lookup for data source/table/column validation (Checks 2-5).
        ontology_lookup: Lookup for ontology class validation (Check 6).
        namespace: The namespace context for ontology lookups.

    Returns:
        ValidationResult with errors (block creation) and warnings (non-blocking).
    """
    import sqlglot
    from sqlglot.errors import ParseError

    checks: list[ValidationCheck] = []

    data_source_id = metric_body.get("dataSourceId", "")
    source_table = metric_body.get("sourceTable", "")
    expression = metric_body.get("expression", {})
    dialects = expression.get("dialects", [])
    ontology_concepts = metric_body.get("ontologyConcepts", [])

    # ── Parse SQL once — reuse ASTs across all checks ───────────────────
    # Each entry: (dialect_name, list of parsed statements)
    parsed_dialects: list[tuple[str, list[Any]]] = []

    for i, dialect_entry in enumerate(dialects):
        dialect = dialect_entry.get("dialect", "")
        sql_expr = dialect_entry.get("expression", "")
        if not sql_expr:
            continue

        sqlglot_dialect = _resolve_dialect(dialect)
        try:
            stmts = sqlglot.parse(sql_expr, dialect=sqlglot_dialect)
            if not stmts or all(s is None for s in stmts):
                checks.append(
                    ValidationCheck(
                        check="sql_syntax",
                        severity=Severity.ERROR,
                        passed=False,
                        message=f"SQL expression is empty or unparseable ({dialect})",
                        details={"dialect": dialect, "index": i},
                    )
                )
            else:
                checks.append(
                    ValidationCheck(
                        check="sql_syntax",
                        severity=Severity.ERROR,
                        passed=True,
                        message=f"SQL syntax valid ({dialect})",
                        details={"dialect": dialect, "index": i},
                    )
                )
                parsed_dialects.append((dialect, [s for s in stmts if s is not None]))

                # ── Check 1b: statement shape (serve-firewall parity, #617) ─
                # A syntactically-valid fragment (e.g. COUNT(*)) is rejected by
                # the serve-time SQL firewall — flag it here so validation
                # predicts serve-time acceptance.
                shape_error = check_select_shape(sql_expr, dialect)
                checks.append(
                    ValidationCheck(
                        check="sql_shape",
                        severity=Severity.ERROR,
                        passed=shape_error is None,
                        message=shape_error or f"Expression is a full SELECT statement ({dialect})",
                        details={"dialect": dialect, "index": i},
                    )
                )
        except ParseError as exc:
            checks.append(
                ValidationCheck(
                    check="sql_syntax",
                    severity=Severity.ERROR,
                    passed=False,
                    message=f"SQL syntax error ({dialect}): {exc}",
                    details={"dialect": dialect, "index": i, "error": str(exc)},
                )
            )
        except Exception as exc:
            logger.warning("sqlglot_unexpected_error", error=str(exc), dialect=dialect)
            checks.append(
                ValidationCheck(
                    check="sql_syntax",
                    severity=Severity.ERROR,
                    passed=False,
                    message=f"Could not parse SQL expression ({dialect}): {exc}",
                    details={"dialect": dialect, "index": i, "error": str(exc)},
                )
            )

    # Tier 1 selects TRINO when present, otherwise the first list entry, and
    # always sends that selected expression through the Trino execution path.
    # Declared-dialect validity alone therefore cannot predict runtime success.
    trino_expression_already_checked = any(
        is_trino_dialect(entry.get("dialect", "")) and bool(entry.get("expression")) for entry in dialects
    )
    if not trino_expression_already_checked:
        tier1_error = check_tier1_execution_shape(dialects)
        checks.append(
            ValidationCheck(
                check="sql_shape",
                severity=Severity.ERROR,
                passed=tier1_error is None,
                message=tier1_error or "Tier 1 selected expression is executable as TRINO SQL",
                details={"dialect": "TRINO", "executionPath": "tier1"},
            )
        )

    # ── Checks 2-5: OMS metadata validation (WARNING) ───────────────────
    # Only run if we have a lookup AND at least one successfully parsed SQL.
    if data_sources_lookup and data_source_id and parsed_dialects:
        checks.extend(_check_table_references(parsed_dialects, data_source_id, source_table, data_sources_lookup))
        checks.extend(_check_column_references(parsed_dialects, data_source_id, source_table, data_sources_lookup))
        checks.extend(_check_dimension_columns(parsed_dialects, data_source_id, source_table, data_sources_lookup))
        checks.extend(_check_filter_compatibility(parsed_dialects, data_source_id, source_table, data_sources_lookup))
    elif data_source_id and source_table and parsed_dialects:
        # An unconfigured namespace is not an outage and therefore cannot be a
        # hard failure, but an empty report must not imply that sourceTable was
        # verified. Surface the degraded check explicitly; validate maps this
        # to INFO, while create/update return it as an advisory warning.
        checks.append(
            ValidationCheck(
                check="table_reference",
                severity=Severity.WARNING,
                passed=False,
                message=(
                    f"Source table '{source_table}' was not verified because "
                    "data source catalog metadata is not configured"
                ),
                details={"table": source_table, "dataSourceId": data_source_id, "verification": "unavailable"},
            )
        )

    # ── Check 6: Ontology class linkage (WARNING) ───────────────────────
    if ontology_lookup and ontology_concepts:
        checks.extend(_check_ontology_linkage(ontology_concepts, ontology_lookup, namespace))

    return ValidationResult.from_checks(checks)


# ── Check 2: Table References ───────────────────────────────────────────


def _check_table_references(
    parsed_dialects: list[tuple[str, list[Any]]],
    data_source_id: str,
    source_table: str,
    lookup: DataSourceLookup,
) -> list[ValidationCheck]:
    """Check 2: All table references in SQL exist in OMS for the metric's dataSourceId."""
    from sqlglot import exp

    checks: list[ValidationCheck] = []
    tables_checked: set[str] = set()
    source_table_lower = source_table.lower()
    catalog_available = lookup.catalog_available(data_source_id)
    known_tables = {table.lower() for table in lookup.known_tables(data_source_id)}

    # Match the dedicated create/update sourceTable gate: a declaration can be
    # qualified while the catalog exposes a bare name (or vice versa). A known
    # name whose approved form cannot be resolved may still deserve a WARNING,
    # but it is not a provably absent table and therefore must not become ERROR.
    declared_source_known = source_table_lower in known_tables or source_table_lower.rsplit(".", 1)[-1] in known_tables

    for _dialect, stmts in parsed_dialects:
        for stmt in stmts:
            for table in stmt.find_all(exp.Table):
                # DataZone indexes database-qualified names. ``Table.name``
                # drops the qualifier, which turns an exact ``sales.orders``
                # reference into the ambiguous bare ``orders`` when multiple
                # databases expose that table name.
                table_name = f"{table.db}.{table.name}" if table.db else table.name
                if not table_name or table_name.lower() in tables_checked:
                    continue
                tables_checked.add(table_name.lower())

                exists = lookup.table_exists(data_source_id, table_name)
                is_declared_source = bool(source_table) and table_name.lower() == source_table_lower
                provable_source_absence = (
                    is_declared_source
                    and not exists
                    and catalog_available
                    and bool(known_tables)
                    and not declared_source_known
                )
                checks.append(
                    ValidationCheck(
                        check="table_reference",
                        severity=Severity.ERROR if provable_source_absence else Severity.WARNING,
                        passed=exists,
                        message=(
                            f"{'Source table' if is_declared_source else 'Table'} '{table_name}' exists in data source"
                            if exists
                            else (
                                f"{'Source table' if is_declared_source else 'Table'} '{table_name}' "
                                f"not found in data source '{data_source_id}'"
                            )
                        ),
                        details={"table": table_name, "dataSourceId": data_source_id},
                    )
                )

    # Also verify the declared sourceTable exists. Its absence is an ERROR only
    # when provable (#161): the catalog was read AND enumerates tables for this
    # source. Otherwise "missing" is indistinguishable from an unreadable or
    # not-yet-approved catalog, so it stays advisory. The loop above always stays
    # WARNING — find_all(exp.Table) also matches CTE/subquery aliases, which are
    # not catalog tables. The exact table that also equals the declared
    # sourceTable is promoted above, because it is not an alias ambiguity.
    if source_table and source_table.lower() not in tables_checked:
        exists = lookup.table_exists(data_source_id, source_table)
        provable_absence = not exists and catalog_available and bool(known_tables) and not declared_source_known
        checks.append(
            ValidationCheck(
                check="table_reference",
                severity=Severity.ERROR if provable_absence else Severity.WARNING,
                passed=exists,
                message=(
                    f"Source table '{source_table}' exists in data source"
                    if exists
                    else f"Source table '{source_table}' not found in data source '{data_source_id}'"
                ),
                details={"table": source_table, "dataSourceId": data_source_id},
            )
        )

    return checks


# ── Check 3: Column References ──────────────────────────────────────────


def _check_column_references(
    parsed_dialects: list[tuple[str, list[Any]]],
    data_source_id: str,
    source_table: str,
    lookup: DataSourceLookup,
) -> list[ValidationCheck]:
    """Check 3: All column references in SQL exist in the source table metadata."""
    from sqlglot import exp

    checks: list[ValidationCheck] = []
    columns_checked: set[str] = set()

    table_columns = lookup.get_table_columns(data_source_id, source_table)
    if table_columns is None:
        return checks

    known_columns = {col.name.lower() for col in table_columns}

    for _dialect, stmts in parsed_dialects:
        for stmt in stmts:
            for col in stmt.find_all(exp.Column):
                col_name = col.name
                if not col_name or col_name.lower() in columns_checked:
                    continue
                columns_checked.add(col_name.lower())

                exists = col_name.lower() in known_columns
                if not exists:
                    checks.append(
                        ValidationCheck(
                            check="column_reference",
                            severity=Severity.WARNING,
                            passed=False,
                            message=f"Column '{col_name}' not found in table '{source_table}' metadata",
                            details={
                                "column": col_name,
                                "table": source_table,
                                "dataSourceId": data_source_id,
                            },
                        )
                    )

    return checks


# ── Check 4: Dimension Columns ──────────────────────────────────────────


def _check_dimension_columns(
    parsed_dialects: list[tuple[str, list[Any]]],
    data_source_id: str,
    source_table: str,
    lookup: DataSourceLookup,
) -> list[ValidationCheck]:
    """Check 4: Columns used in GROUP BY (dimensions) exist in the source table."""
    from sqlglot import exp

    checks: list[ValidationCheck] = []
    dimensions_checked: set[str] = set()

    table_columns = lookup.get_table_columns(data_source_id, source_table)
    if table_columns is None:
        return checks

    known_columns = {col.name.lower() for col in table_columns}

    for _dialect, stmts in parsed_dialects:
        for stmt in stmts:
            group_by = stmt.find(exp.Group)
            if group_by is None:
                continue
            for col in group_by.find_all(exp.Column):
                col_name = col.name
                if not col_name or col_name.lower() in dimensions_checked:
                    continue
                dimensions_checked.add(col_name.lower())

                exists = col_name.lower() in known_columns
                if not exists:
                    checks.append(
                        ValidationCheck(
                            check="dimension_column",
                            severity=Severity.WARNING,
                            passed=False,
                            message=f"Dimension column '{col_name}' not found in table '{source_table}'",
                            details={
                                "column": col_name,
                                "table": source_table,
                                "usage": "GROUP BY (dimension)",
                            },
                        )
                    )

    return checks


# ── Check 5: Filter Type Compatibility ──────────────────────────────────

# Operators that require numeric/date types
_NUMERIC_OPS = {"GT", "LT", "GTE", "LTE", "BETWEEN"}
# Operators that require string types
_STRING_OPS = {"LIKE", "ILIKE"}
# Numeric-compatible types
_NUMERIC_TYPES = {"integer", "int", "bigint", "smallint", "decimal", "numeric", "float", "double", "real", "number"}
# Date-compatible types
_DATE_TYPES = {"date", "timestamp", "timestamptz", "datetime", "timestamp_ntz", "timestamp_ltz"}
# String types
_STRING_TYPES = {"varchar", "text", "char", "string", "character varying", "nvarchar"}


def _check_filter_compatibility(
    parsed_dialects: list[tuple[str, list[Any]]],
    data_source_id: str,
    source_table: str,
    lookup: DataSourceLookup,
) -> list[ValidationCheck]:
    """Check 5: Columns in WHERE clauses have type-compatible operators."""
    from sqlglot import exp

    checks: list[ValidationCheck] = []
    filters_checked: set[str] = set()

    table_columns = lookup.get_table_columns(data_source_id, source_table)
    if table_columns is None:
        return checks

    for _dialect, stmts in parsed_dialects:
        for stmt in stmts:
            where = stmt.find(exp.Where)
            if where is None:
                continue

            for comparison in where.find_all(exp.GT, exp.LT, exp.GTE, exp.LTE, exp.Like, exp.ILike, exp.Between):
                col_node = comparison.find(exp.Column)
                if col_node is None:
                    continue

                col_name = col_node.name
                if not col_name:
                    continue

                check_key = f"{col_name}:{type(comparison).__name__}"
                if check_key in filters_checked:
                    continue
                filters_checked.add(check_key)

                col_type = lookup.get_column_type(data_source_id, source_table, col_name)
                if col_type is None:
                    continue

                op_name = type(comparison).__name__.upper()
                compatible = _is_type_compatible(col_type, op_name)

                if not compatible:
                    checks.append(
                        ValidationCheck(
                            check="filter_type_compatibility",
                            severity=Severity.WARNING,
                            passed=False,
                            message=(
                                f"Column '{col_name}' (type: {col_type}) may be incompatible with operator '{op_name}'"
                            ),
                            details={
                                "column": col_name,
                                "columnType": col_type,
                                "operator": op_name,
                                "table": source_table,
                            },
                        )
                    )

    return checks


def _is_type_compatible(col_type: str, operator: str) -> bool:
    """Check if a column type is compatible with a comparison operator."""
    col_type_lower = col_type.lower()

    # LIKE/ILIKE only makes sense on string types
    if operator in ("LIKE", "ILIKE"):
        return col_type_lower in _STRING_TYPES or col_type_lower in _DATE_TYPES

    # Numeric comparisons (GT, LT, GTE, LTE, BETWEEN) work on numeric and date types
    if operator in ("GT", "LT", "GTE", "LTE", "BETWEEN"):
        return col_type_lower in _NUMERIC_TYPES or col_type_lower in _DATE_TYPES

    return True  # Unknown operator — don't flag


# ── Check 6: Ontology Class Linkage ────────────────────────────────────


def _check_ontology_linkage(
    ontology_concepts: list[str],
    ontology_lookup: OntologyLookup,
    namespace: str,
) -> list[ValidationCheck]:
    """Check 6: :governedMetricFor references valid ontology classes in published graph."""
    checks: list[ValidationCheck] = []

    for class_ref in ontology_concepts:
        if not class_ref:
            continue
        exists = ontology_lookup.class_exists(class_ref, namespace)
        checks.append(
            ValidationCheck(
                check="ontology_linkage",
                severity=Severity.WARNING,
                passed=exists,
                message=(
                    f"Ontology class '{class_ref}' exists in published ontology"
                    if exists
                    else f"Ontology class '{class_ref}' not found in published ontology for namespace '{namespace}'"
                ),
                details={"classUri": class_ref, "namespace": namespace},
            )
        )

    return checks
