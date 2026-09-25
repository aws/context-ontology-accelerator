# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SQL Firewall — safety and authorization gate for SQL execution.

Validates and authorizes SQL statements before execution across all engines.
Used by both Tier 1 (pre-compiled metric SQL) and Tier 2 (VKG-generated SQL).

Safety checks (always enforced):
- Only SELECT statements allowed (DDL/DML rejected via sqlglot AST).
- Dangerous functions blocked (filesystem access, network, sleep).

Authorization (table/column enforcement from the principal's grant profile):
- Table allowlist: every referenced table must appear in `tableAllowlist`.
- Column denylist: referenced columns are evaluated per table against
  `columnDenylist`; `SELECT *` over a restricted table is rejected since the
  denied columns cannot be proven excluded.
- Fail-closed: queries that cannot be parsed/analyzed are denied. Namespace
  and action-level authorization is enforced upstream (API Gateway authorizer).
- Row-level filtering is post-MVP (Row Filter Injector).

Uses sqlglot for AST-based validation and table/column extraction.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import sqlglot
import structlog

from .cedar_authorizer import CedarAuthorizer, NullCedarAuthorizer, RealCedarAuthorizer

logger = structlog.get_logger(__name__)

_COMMENT_PATTERN = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)

_BLOCKED_STATEMENTS = re.compile(
    r"^\s*(MSCK\s+REPAIR|CREATE|DROP|ALTER|INSERT|DELETE|UPDATE|MERGE|UNLOAD|PREPARE|EXECUTE|EXPLAIN)\b",
    re.IGNORECASE,
)

_DANGEROUS_FUNCTIONS = frozenset(
    {
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "lo_import",
        "lo_export",
        "copy",
        "pg_sleep",
    }
)

# Allowlist: only these top-level AST node types pass validation.
# - Select: plain queries and CTE-wrapped queries
# - Union/Intersect/Except: set operations combining SELECT results
# Pattern from Apache Airflow common-ai sql_validation (allowlist > denylist).
_SAFE_STATEMENT_TYPES: tuple[type[sqlglot.exp.Expression], ...] = (
    sqlglot.exp.Select,
    sqlglot.exp.Union,
    sqlglot.exp.Intersect,
    sqlglot.exp.Except,
)

# Deep-scan denylist: data-modifying nodes that must not appear anywhere in
# the AST, even inside otherwise-safe set operations or CTEs. Catches bypass
# vectors like `WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d`.
_DATA_MODIFYING_NODES: tuple[type[sqlglot.exp.Expression], ...] = (
    sqlglot.exp.Insert,
    sqlglot.exp.Update,
    sqlglot.exp.Delete,
    sqlglot.exp.Merge,
    sqlglot.exp.Into,
    sqlglot.exp.Create,
    sqlglot.exp.Drop,
    sqlglot.exp.Alter,
)


# Grant profile keys carrying the principal's resolved data-access overrides.
# These mirror the ResourceRoleMapping fields written by the control-plane
# grant API (tableAllowlist: list[str], columnDenylist: dict[str, list[str]]).
_TABLE_ALLOWLIST_KEY = "tableAllowlist"
_COLUMN_DENYLIST_KEY = "columnDenylist"


class UnsafeSQLError(ValueError):
    """Raised when SQL fails safety validation (non-SELECT, dangerous functions, etc.)."""


class NamespaceSQLScopeError(ValueError):
    """Raised when SQL names an object outside the requested namespace's sources."""


@dataclass(frozen=True)
class _QueryRefs:
    """Table and column references extracted from a SQL statement.

    All table names are bare (last identifier component) to match the
    grant profile's allowlist/denylist keys. Columns are grouped by their
    resolved table; unqualified columns are tracked separately because they
    may belong to any in-scope table (evaluated fail-closed).
    """

    tables: frozenset[str]
    columns_by_table: dict[str, set[str]]
    unqualified_columns: frozenset[str]
    star_tables: frozenset[str]
    bare_star: bool


@dataclass(frozen=True)
class FirewallResult:
    """Outcome of a SQL firewall evaluation.

    Attributes:
        denied: True if the query was blocked by the firewall.
        authorized_sql: The (possibly rewritten) SQL authorized for execution.
            Same as input when no rewriting is applied.
        reason: Human-readable denial reason, or None if allowed.
    """

    denied: bool
    authorized_sql: str
    reason: str | None = None


class SQLFirewall:
    """Safety and authorization gate for SQL execution.

    Safety checks are always enforced (statement type, dangerous functions).
    Authorization enforces table/column access from the caller's grant profile
    (``tableAllowlist`` / ``columnDenylist``) and fails closed on parse errors.

    a ``CedarAuthorizer`` adds a namespace-level policy gate that runs
    AFTER the structural checks pass (an additional deny, never a relaxation).
    The default is the REAL ``cedarpy``-backed ``RealCedarAuthorizer`` (evaluates
    ``query`` on the namespace vs the shipped seed policies using the caller's
    resolved roles); set ``SCL_DISABLE_CEDAR=true`` to swap in
    ``NullCedarAuthorizer`` (opt-out of the policy layer only — structural checks
    still apply).
    """

    def __init__(self, cedar_authorizer: CedarAuthorizer | None = None):
        """Select the Cedar authorizer used for the namespace policy gate.

        Args:
            cedar_authorizer: Explicit authorizer to use. When None, a
                ``NullCedarAuthorizer`` is used if ``SCL_DISABLE_CEDAR`` is set,
                otherwise the real ``cedarpy``-backed authorizer.
        """
        if cedar_authorizer is not None:
            self._cedar: CedarAuthorizer = cedar_authorizer
        elif os.environ.get("SCL_DISABLE_CEDAR", "").lower() == "true":
            self._cedar = NullCedarAuthorizer()
        else:
            self._cedar = RealCedarAuthorizer()

    def check_statement_type(self, sql: str) -> None:
        """Cheap, dialect-independent safety gate: reject blocked statement types.

        Matches leading DDL/DML keywords (DROP/INSERT/UPDATE/DELETE/…) via regex
        WITHOUT parsing, so it can run before any dialect is known (e.g. before a
        source lookup on the direct-JDBC path). ``validate`` calls this first; it is
        also exposed so callers that must fail-fast on a dangerous statement before
        resolving the engine/dialect can gate cheaply.

        Raises:
            UnsafeSQLError: If the statement type is not allowed.
        """
        stripped = _COMMENT_PATTERN.sub(" ", sql).strip()
        if _BLOCKED_STATEMENTS.match(stripped):
            raise UnsafeSQLError("Statement type not allowed")

    def validate(self, sql: str, dialect: str = "trino") -> None:
        """Validate SQL safety: only SELECT/set-operations allowed, no dangerous functions.

        Uses an allowlist approach (safer than denylist): only explicitly permitted
        statement types pass. Additionally walks the full AST to reject data-modifying
        nodes hidden inside CTEs or subqueries (defense-in-depth).

        ``dialect`` is the sqlglot read-dialect for parsing. It defaults to ``"trino"``
        (the VKG/NL-generated SQL the firewall usually sees). Callers that validate SQL
        already transpiled to an engine dialect (e.g. the direct-JDBC path, which
        re-transpiles to postgres/redshift before execution) must pass the
        matching dialect so a valid engine statement isn't rejected as unparseable.

        Raises:
            UnsafeSQLError: If SQL is not a safe read-only statement.
        """
        self.check_statement_type(sql)

        try:
            parsed = sqlglot.parse_one(sql, read=dialect)
        except sqlglot.errors.ParseError as e:
            raise UnsafeSQLError("Failed to parse SQL statement") from e

        if not isinstance(parsed, _SAFE_STATEMENT_TYPES):
            raise UnsafeSQLError(f"Only SELECT statements are allowed, got: {type(parsed).__name__}")

        # Deep scan: reject data-modifying nodes hidden anywhere in the AST
        # (e.g. data-modifying CTEs, SELECT INTO, DML inside subqueries).
        for node in parsed.walk():
            if isinstance(node, _DATA_MODIFYING_NODES):
                raise UnsafeSQLError(f"Data-modifying operation '{type(node).__name__}' found inside statement")

        for func in parsed.find_all(sqlglot.exp.Anonymous, sqlglot.exp.Func):
            func_name = getattr(func, "name", "").lower()
            if func_name in _DANGEROUS_FUNCTIONS:
                raise UnsafeSQLError(f"Function not allowed: {func_name}")

    def evaluate(
        self, sql: str, profile: dict[str, Any] | None = None, namespace: str = "", dialect: str = "trino"
    ) -> FirewallResult:
        """Validate safety, then enforce table/column authorization.

        Evaluates each referenced table against the caller's grant profile:
        every table must be in ``tableAllowlist`` (when set), and no referenced
        column may appear in ``columnDenylist`` for its table. When the profile
        carries no data-access restrictions the query is allowed (namespace and
        action authorization is enforced upstream at the API Gateway authorizer).

        Comparisons are case-insensitive (SQL identifiers are case-insensitive
        for unquoted names across Athena/Postgres/MySQL/etc.); both the profile
        and the extracted query refs are lower-cased before set operations.

        Args:
            sql: The SQL statement to evaluate.
            profile: Caller's authorization profile. Recognized keys:
                ``tableAllowlist`` (list[str]) and ``columnDenylist``
                (dict[str, list[str]]).
            namespace: Namespace evaluated by the Cedar policy gate.
            dialect: sqlglot read-dialect used to parse ``sql`` for the safety
                checks. Defaults to ``"trino"`` (the canonical generation dialect);
                the direct-JDBC path passes the engine's own dialect so an
                already-transpiled statement isn't rejected as unparseable.

        Returns:
            FirewallResult indicating whether the query is allowed.

        Raises:
            UnsafeSQLError: If SQL fails safety validation (incl. parse errors).
        """
        self.validate(sql, dialect=dialect)

        profile = profile or {}
        allowlist_raw = profile.get(_TABLE_ALLOWLIST_KEY)
        denylist_raw = profile.get(_COLUMN_DENYLIST_KEY)

        # Fail-closed type validation — malformed grant profiles must not silently
        # iterate over characters of a string or raise AttributeError downstream.
        if allowlist_raw is not None and not isinstance(allowlist_raw, (list, tuple)):
            return self._deny("Invalid tableAllowlist format: expected list")
        if denylist_raw is not None and not isinstance(denylist_raw, dict):
            return self._deny("Invalid columnDenylist format: expected mapping")

        # Normalize to lower-case for case-insensitive comparison. SQL identifiers
        # are case-insensitive for unquoted names; the allowlist/denylist must
        # not be bypassed by varying case (e.g. `SELECT * FROM ORDERS`).
        #
        # ``is not None``, not truthiness: an ABSENT allowlist means "no table
        # constraint", but an explicitly EMPTY one means "no tables permitted".
        # Collapsing the two made a deny-all grant fail OPEN — the empty list was
        # falsy, so the restriction was dropped and the unrestricted path below
        # allowed every table.
        allowlist: set[str] | None = None
        if allowlist_raw is not None:
            try:
                allowlist = {str(t).lower() for t in allowlist_raw}
            except TypeError:
                return self._deny("Invalid tableAllowlist format: non-iterable members")

        denylist: dict[str, set[str]] = {}
        if denylist_raw:
            for table, cols in denylist_raw.items():
                if not isinstance(cols, (list, tuple, set, frozenset)):
                    return self._deny(f"Invalid columnDenylist format for table '{table}': expected list")
                denylist[str(table).lower()] = {str(c).lower() for c in cols}

        # No table/column restrictions on this grant — the structural allowlist/
        # denylist is "sparse opt-in" (an authorization miss returns a
        # FirewallResult, never raises; the terminal-deny at the
        # orchestration boundary only bites on a RESTRICTIVE profile). The Cedar
        # policy gate below still runs regardless, since it is namespace/role
        # based rather than keyed off the sparse table profile.
        #
        # ``allowlist is None`` rather than ``not allowlist``: an empty allowlist is
        # a restriction (deny every table), not the absence of one.
        if allowlist is None and not denylist:
            logger.info("sql_firewall_evaluate", decision="allow", restricted=False)
            return self._apply_cedar(sql, namespace, profile)

        # Fail-closed: deny anything we cannot fully analyze for table/column refs.
        refs = self._analyze_refs(sql)
        if refs is None:
            return self._deny("Query could not be parsed for authorization")

        # Table allowlist — every referenced table must be permitted.
        # ``refs.tables`` and ``allowlist`` are both lower-cased.
        if allowlist is not None:
            unauthorized = refs.tables - allowlist
            if unauthorized:
                return self._deny(f"Access denied to table(s): {', '.join(sorted(unauthorized))}")

        # Column denylist — per-table evaluation of referenced columns
        # (keys/values lower-cased to match refs).
        for table_lc, denied_columns in denylist.items():
            if table_lc not in refs.tables:
                continue
            reason = self._evaluate_columns(table_lc, denied_columns, refs)
            if reason:
                return self._deny(reason)

        logger.info("sql_firewall_evaluate", tables=sorted(refs.tables), decision="allow", restricted=True)
        return self._apply_cedar(sql, namespace, profile)

    def authorize_namespace(self, namespace: str, profile: dict[str, Any] | None = None) -> FirewallResult:
        """Coarse namespace admission gate (Cedar only), independent of any SQL.

        Answers "may this principal query in this namespace at all?" — the same
        ``SCL::Action::"query"`` decision the SQL path applies, but for retrieval
        surfaces that never build SQL and so never reach :meth:`evaluate`: the
        isolated ``kbSearch`` / ``graphTraverse`` / ``translate`` actions and the
        Tier-3 retrieval/synthesis path. This is their ONLY namespace
        authorization point; without it any authenticated IdP user could read any
        namespace's chunks, graph and synthesized answers (F-2, CWE-862).

        Fails CLOSED: an evaluator error denies, and (in prod) a caller with no
        resolved roles denies — the posture is owned by the ``CedarAuthorizer``.
        ``authorized_sql`` is empty because there is no SQL on this path.
        """
        ns = namespace or str((profile or {}).get("namespace", ""))
        reason = self._cedar_gate(ns, profile or {}, tables=[])
        if reason is not None:
            # Logged here rather than via ``_deny`` so the event name reflects the
            # namespace-admission path — ``_deny`` emits ``sql_firewall_evaluate``,
            # which would be misleading when no SQL was evaluated at all.
            logger.warning("namespace_authorize_denied", namespace=ns, reason=reason)
            return FirewallResult(denied=True, authorized_sql="", reason=reason)
        return FirewallResult(denied=False, authorized_sql="")

    def _apply_cedar(self, sql: str, namespace: str, profile: dict[str, Any]) -> FirewallResult:
        """Apply the Cedar policy gate as a final authorization step.

        Runs AFTER structural checks pass. With the default NullCedarAuthorizer
        this is a no-op allow. Fails CLOSED: any evaluator exception denies
        (never silently allows). Namespace is taken from the profile when not
        passed (evaluate() does not currently receive it as a parameter).
        """
        ns = namespace or str(profile.get("namespace", ""))
        reason = self._cedar_gate(ns, profile, tables=self.extract_tables(sql))
        if reason is not None:
            return self._deny(reason)
        return FirewallResult(denied=False, authorized_sql=sql)

    def _cedar_gate(self, namespace: str, profile: dict[str, Any], tables: list[str]) -> str | None:
        """Run the Cedar policy decision. Returns a deny REASON, or None if allowed.

        The single call site for the Cedar authorizer, shared by the SQL path
        (:meth:`_apply_cedar`) and the SQL-free namespace gate
        (:meth:`authorize_namespace`) so the fail-closed error handling exists
        exactly once. Returns the reason string rather than a ``FirewallResult``
        so each caller keeps its own ``authorized_sql`` and emits its own log
        event (the SQL path's ``sql_firewall_evaluate`` would misdescribe the
        namespace-admission path).
        """
        try:
            decision = self._cedar.authorize(namespace=namespace, profile=profile, tables=tables)
        except Exception as exc:
            # Security-critical path: log full diagnostics server-side (the deny
            # reason returned to the caller stays generic).
            logger.warning(
                "cedar_authorize_error",
                namespace=namespace,
                table_count=len(tables),
                error=type(exc).__name__,
                error_msg=str(exc),
                exc_info=True,
            )
            return "Authorization policy evaluation failed"
        if not decision.allowed:
            return decision.reason or "Access denied by policy"
        return None

    @staticmethod
    def _deny(reason: str) -> FirewallResult:
        """Build a denial result and log it at WARN level."""
        logger.warning("sql_firewall_evaluate", decision="deny", reason=reason)
        return FirewallResult(denied=True, authorized_sql="", reason=reason)

    @staticmethod
    def _evaluate_columns(table: str, denied: set[str], refs: _QueryRefs) -> str | None:
        """Return a denial reason if the query exposes denied columns of ``table``.

        Fail-closed on ``SELECT *`` / ``table.*`` (cannot prove denied columns
        are excluded) and treats unqualified columns as potentially belonging to
        any in-scope table.
        """
        if not denied:
            return None
        if refs.bare_star or table in refs.star_tables:
            return f"Access denied: SELECT * may expose restricted columns of '{table}'"
        blocked = (refs.columns_by_table.get(table, set()) | set(refs.unqualified_columns)) & denied
        if blocked:
            cols = ", ".join(f"{table}.{c}" for c in sorted(blocked))
            return f"Access denied to column(s): {cols}"
        return None

    @staticmethod
    def validate_namespace_sql_scope(
        sql: str,
        *,
        native_databases: frozenset[str],
        federated_catalog_schemas: frozenset[tuple[str, str]],
        default_catalog: str,
        schema_only: bool = False,
    ) -> bool:
        """Authorize qualified table references against one namespace's sources.

        Returns ``True`` when a query contains at least one catalog/database
        qualifier (and therefore needed this check), ``False`` for bare-table SQL.
        Bare names resolve through the executor's namespace-scoped query context;
        this method protects the attacker-controlled qualifiers that override it.

        ``information_schema`` is denied rather than treated as a normal schema:
        querying it under ``AwsDataCatalog`` can enumerate every Glue database the
        shared runtime role can see, including other namespaces' sources.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="trino")
        except sqlglot.errors.ParseError as exc:
            raise NamespaceSQLScopeError("Unable to verify SQL references") from exc

        cte_names = {cte.alias_or_name.lower() for cte in parsed.find_all(sqlglot.exp.CTE)}
        checked = False
        default_catalog_lc = (default_catalog or "AwsDataCatalog").lower()
        # On the direct-JDBC route the catalog is supplied by the JDBC CONNECTION,
        # not by an Athena DataCatalog, so a bare "schema.table" (the form a
        # single-source metric emits) legitimately carries no Athena catalog and
        # must be authorized on its SCHEMA alone — against the union of every schema
        # the namespace owns under any authorized catalog. Pinning it to
        # awsdatacatalog/native_databases (the Athena rule) would wrongly deny a
        # federated JDBC source whose schema lives in federated_catalog_schemas.
        schema_scope = set(native_databases) | {schema for _cat, schema in federated_catalog_schemas}

        for table in parsed.find_all(sqlglot.exp.Table):
            if not table.name or (not table.db and table.name.lower() in cte_names):
                continue
            catalog = (table.catalog or "").lower()
            database = (table.db or "").lower()
            if not catalog and not database:
                continue
            checked = True

            if database == "information_schema":
                raise NamespaceSQLScopeError("SQL reference is not available in the requested namespace")

            if schema_only and not catalog:
                # JDBC route, unqualified catalog: authorize on schema membership in
                # ANY authorized catalog. A 3-part name still carries an explicit
                # catalog and falls through to the strict catalog-pinned check below.
                allowed = bool(database) and database in schema_scope
            else:
                effective_catalog = catalog or default_catalog_lc
                if effective_catalog == "awsdatacatalog":
                    allowed = bool(database) and database in native_databases
                else:
                    allowed = bool(database) and (effective_catalog, database) in federated_catalog_schemas
            if not allowed:
                # Name the specific rejected reference so a denial is diagnosable
                # from the caller's log (the caller records the authorized scope
                # alongside it). This is a policy statement about the query, not
                # sensitive data, so it is safe to carry in the internal message.
                rejected = (
                    f'"{database}"."{table.name.lower()}"'
                    if schema_only and not catalog
                    else f'"{catalog or default_catalog_lc}"."{database}"."{table.name.lower()}"'
                )
                raise NamespaceSQLScopeError(f"SQL reference is not available in the requested namespace: {rejected}")

        return checked

    @staticmethod
    def _analyze_refs(sql: str) -> _QueryRefs | None:
        """Parse ``sql`` and extract table/column references for authorization.

        Returns None when the statement cannot be parsed (caller fails closed).
        CTE names are excluded so they are not mistaken for source tables.

        All identifiers are normalized to lower-case so downstream comparisons
        with the (also lower-cased) grant profile are case-insensitive — SQL
        identifiers are case-insensitive for unquoted names and the firewall
        must not be bypassed by varying case in either the query or profile.
        """
        try:
            parsed = sqlglot.parse_one(sql)
        except sqlglot.errors.ParseError:
            logger.warning("sql_firewall_parse_error", sql_preview=sql)
            return None
        if parsed is None:
            return None

        cte_names = {cte.alias_or_name.lower() for cte in parsed.find_all(sqlglot.exp.CTE)}

        # alias/table-name (lowercased) -> bare table name (lowercased)
        alias_map: dict[str, str] = {}
        tables: set[str] = set()
        for tbl in parsed.find_all(sqlglot.exp.Table):
            name = tbl.name
            if not name or name.lower() in cte_names:
                continue
            name_lc = name.lower()
            tables.add(name_lc)
            alias_map[name_lc] = name_lc
            if tbl.alias:
                alias_map[tbl.alias.lower()] = name_lc

        def resolve(qualifier: str) -> str:
            return alias_map.get(qualifier.lower(), qualifier.lower())

        star_tables: set[str] = set()
        bare_star = False
        for star in parsed.find_all(sqlglot.exp.Star):
            parent = star.parent
            if isinstance(parent, sqlglot.exp.Column) and parent.table:
                star_tables.add(resolve(parent.table))
            elif isinstance(parent, sqlglot.exp.Select):
                bare_star = True  # SELECT * — exposes every column of in-scope tables

        columns_by_table: dict[str, set[str]] = {}
        unqualified: set[str] = set()
        for col in parsed.find_all(sqlglot.exp.Column):
            if isinstance(col.this, sqlglot.exp.Star):
                continue  # handled above
            name = col.name
            if not name:
                continue
            name_lc = name.lower()
            qualifier = col.table
            if qualifier and qualifier.lower() not in cte_names:
                columns_by_table.setdefault(resolve(qualifier), set()).add(name_lc)
            elif not qualifier:
                unqualified.add(name_lc)

        return _QueryRefs(
            tables=frozenset(tables),
            columns_by_table=columns_by_table,
            unqualified_columns=frozenset(unqualified),
            star_tables=frozenset(star_tables),
            bare_star=bare_star,
        )

    @staticmethod
    def extract_tables(sql: str) -> list[str]:
        """Extract table names referenced in a SQL statement via AST parsing.

        Uses sqlglot to parse the SQL and walk the AST for Table nodes.
        Returns a deduplicated list of fully-qualified table names (schema.table
        or just table depending on the SQL).

        Falls back to an empty list if parsing fails — never raises.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="trino")
        except sqlglot.errors.ParseError:
            logger.warning("sql_firewall_parse_error", sql_preview=sql)
            return []

        tables: list[str] = []
        seen: set[str] = set()
        for table_node in parsed.find_all(sqlglot.exp.Table):
            # Build qualified name: schema.table or just table
            parts = []
            if table_node.catalog:
                parts.append(table_node.catalog)
            if table_node.db:
                parts.append(table_node.db)
            if table_node.name:
                parts.append(table_node.name)
            if not parts:
                continue
            qualified = ".".join(parts)
            if qualified not in seen:
                seen.add(qualified)
                tables.append(qualified)

        return tables
