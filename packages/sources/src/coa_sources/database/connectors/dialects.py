# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-engine SQL dialects for JDBC schema discovery.

A ``Dialect`` encapsulates the two things that vary across relational engines:
the driver/connection, and the catalog SQL used to enumerate schemas, tables,
columns, and key constraints. ``JdbcConnector`` owns the discovery flow
(regex filtering, ``Table`` assembly, resilience); dialects return only plain
primitives, so they stay small and individually testable.

Engines that expose the ANSI ``information_schema`` (PostgreSQL, Redshift,
MySQL, SQL Server, Snowflake) share :class:`InformationSchemaDialect` and only
override the driver ``connect`` (and, where the standard FK view is missing,
like MySQL, ``fetch_constraints``). Oracle has no ``information_schema`` and uses
its ``ALL_*`` catalog views, so it overrides all the SQL. Snowflake additionally
needs an active warehouse (passed via ``options``) for its INFORMATION_SCHEMA.

Drivers are imported lazily inside ``connect`` so a missing optional driver
only fails the engine that needs it (and keeps the Lambda bundle lean). All
identifiers from configuration are bound as query parameters — never
string-interpolated — except the IN-clause placeholder list, which is built
solely from a count of validated names.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Keep a column as "enum-like" only when rows repeat: total >= this * distinct
# (avg >=3 occurrences/value). Rejects unique ids and near-unique free text that
# a distinct cap alone would miss in small tables.
MIN_REPETITION_FACTOR = 3

# Max distinct values for an enum. Both the high-cardinality cutoff and a guard
# against near-unique columns; above this the column is dropped (never truncated
# to a partial list, which would look exhaustive to the NL→SQL LLM).
MAX_ENUM_DISTINCT = 25

# Enum detection is DATA-driven, never name-driven — a column-name prior is
# brittle (drops real categoricals like `category_name`/`payment_type`). Beyond
# cardinality, the value SHAPE separates a low-cardinality free-text column
# (long, multi-word) from a genuine enum (short code/label like "RETURNED").
_MAX_ENUM_AVG_VALUE_LEN = 40  # avg chars across sampled values
_MAX_ENUM_AVG_WORDS = 3  # avg whitespace-separated tokens per value


def values_look_categorical(values: list[str]) -> bool:
    """True when sampled values look like short category codes, not free text.

    Applied AFTER fetching the distinct values (which already passed the
    cardinality gate). Rejects value sets that are long on average or
    sentence-like (many words) — the count-independent signal that a
    low-cardinality column is free text rather than a category.
    """
    if not values:
        return False
    avg_len = sum(len(v) for v in values) / len(values)
    avg_words = sum(len(v.split()) for v in values) / len(values)
    return avg_len <= _MAX_ENUM_AVG_VALUE_LEN and avg_words <= _MAX_ENUM_AVG_WORDS


# (table, column, data_type, nullable)
ColumnRow = tuple[str, str, str, bool]
# pk: {table: [cols]}; fk: {table: [(col, ref_table, ref_col)]}
Constraints = tuple[dict[str, list[str]], dict[str, list[tuple[str, str, str]]]]
# Source-existing descriptions discovered from the catalog.
#   table_descriptions:  {table_name: description}
#   column_descriptions: {table_name: {column_name: description}}
Descriptions = tuple[dict[str, str], dict[str, dict[str, str]]]


def _run(conn: Any, sql: str, params: tuple[Any, ...] | Mapping[str, Any] = ()) -> list[tuple]:
    """Execute a parameterized query and return all rows.

    ``params`` accepts either a positional tuple or a mapping of NAMED binds.
    Named binds are required by dialects whose driver counts each occurrence of
    a positional placeholder (python-oracledb — see ``OracleDialect.list_tables``).
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        return cursor.fetchall() or []
    except Exception:
        # Postgres-family engines abort the whole transaction on any statement
        # error (SQLSTATE 25P02) and reject every later statement on the same
        # connection until a rollback. discover_metadata() reuses ONE
        # (autocommit=False) connection across the whole pass, and the _safe_*
        # helpers swallow-and-log by design — so without this rollback a single
        # incidental catalog-query error poisons the connection and every
        # subsequent query fails with a misleading 25P02 (issue #129). Roll back
        # at this single choke point so it covers every dialect. Guard the
        # rollback itself so it can never mask the original error.
        try:
            conn.rollback()
        except Exception:
            logger.warning("jdbc_run_rollback_failed", exc_info=True)
        raise
    finally:
        cursor.close()


def _nullable(value: Any) -> bool:
    """Normalize a catalog nullability flag (YES/NO, Y/N, true/false) to bool."""
    return str(value).strip().upper() in ("YES", "Y", "TRUE", "1")


class Dialect:
    """Engine-specific connection + catalog SQL. Subclass per engine family."""

    engines: frozenset[str] = frozenset()
    default_port: int = 0
    # Default schema_exclude_filter regex (system schemas) when none configured.
    system_schema_pattern: str = r"^$"
    # Identifier quote character. ANSI double-quote by default; MySQL/MariaDB do
    # not treat "x" as an identifier under their default sql_mode (it's a string
    # literal) so that family overrides this with a backtick.
    identifier_quote: str = '"'

    def connect(self, *, host: str, port: int, database: str, user: str, password: str, options: dict | None = None):
        """Open a DB-API connection to the engine.

        Args:
            host: Database host name.
            port: TCP port to connect on.
            database: Database (or service) name.
            user: Login user name.
            password: Login password.
            options: Engine-specific extras (e.g. Snowflake ``warehouse``/``role``).

        Returns:
            An open DB-API connection.

        Raises:
            NotImplementedError: Always; subclasses must implement this.
        """
        raise NotImplementedError

    def list_schemas(self, conn: Any) -> list[str]:
        """Return every schema name visible on the connection.

        Args:
            conn: An open DB-API connection.

        Returns:
            Schema names (unfiltered; system-schema filtering happens upstream).

        Raises:
            NotImplementedError: Always; subclasses must implement this.
        """
        raise NotImplementedError

    def list_tables(self, conn: Any, schema: str) -> list[str]:
        """Return the table and view names in a schema.

        Args:
            conn: An open DB-API connection.
            schema: Schema to enumerate.

        Returns:
            Table/view names within ``schema``.

        Raises:
            NotImplementedError: Always; subclasses must implement this.
        """
        raise NotImplementedError

    def fetch_columns(self, conn: Any, schema: str, tables: list[str]) -> list[ColumnRow]:
        """Return column metadata for the given tables.

        Args:
            conn: An open DB-API connection.
            schema: Schema the tables live in.
            tables: Table names to fetch columns for.

        Returns:
            ``(table, column, data_type, nullable)`` rows.

        Raises:
            NotImplementedError: Always; subclasses must implement this.
        """
        raise NotImplementedError

    def fetch_constraints(self, conn: Any, schema: str, tables: list[str]) -> Constraints:
        """Return primary- and foreign-key constraints for the given tables.

        Default returns empty mappings; engines that expose key metadata override
        this.

        Args:
            conn: An open DB-API connection.
            schema: Schema the tables live in.
            tables: Table names to inspect.

        Returns:
            A ``(primary_keys, foreign_keys)`` pair keyed by table name.
        """
        return {}, {}

    def fetch_descriptions(self, conn: Any, schema: str, tables: list[str]) -> Descriptions:
        """Fetch source-catalog descriptions/comments for tables and columns.

        Default returns empty dicts; engines that expose comments override this.
        Best-effort: callers must tolerate missing descriptions for engines that
        don't expose them or for individual tables/columns without comments.
        """
        return {}, {}

    def fetch_distinct_values(
        self, conn: Any, schema: str, table: str, columns: list[str], *, max_distinct: int = MAX_ENUM_DISTINCT
    ) -> dict[str, list[str]]:
        """Sample distinct values for low-cardinality string columns.

        Returns ``{col_name: [val1, val2, ...]}`` for columns whose distinct
        count ≤ ``max_distinct``. High-cardinality columns (> threshold) and
        columns that error are omitted (returned as ``{}``). Best-effort: a
        sampling failure never aborts discovery.

        Default returns empty; engines that support sampling override this.
        """
        return {}

    def fetch_database_description(self, conn: Any, schema: str) -> str:
        """Fetch the schema/database-level COMMENT, if the engine supports it.

        Default returns an empty string. PostgreSQL/Redshift and Snowflake
        override this; MySQL, SQL Server, and Oracle do not expose a
        first-class schema-level comment in their standard catalog and
        therefore stay with the empty default.
        """
        return ""


class InformationSchemaDialect(Dialect):
    """ANSI ``information_schema`` implementation shared by most engines."""

    def list_schemas(self, conn: Any) -> list[str]:
        """List schemas from ``information_schema.schemata``."""
        return [r[0] for r in _run(conn, "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name")]

    def list_tables(self, conn: Any, schema: str) -> list[str]:
        """List base tables and views in a schema from ``information_schema.tables``."""
        rows = _run(
            conn,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW') "
            "ORDER BY table_name",
            (schema,),
        )
        return [r[0] for r in rows]

    def fetch_columns(self, conn: Any, schema: str, tables: list[str]) -> list[ColumnRow]:
        """Fetch column metadata from ``information_schema.columns``, ordered by position."""
        if not tables:
            return []
        placeholders = ", ".join(["%s"] * len(tables))
        rows = _run(
            conn,
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            f"WHERE table_schema = %s AND table_name IN ({placeholders}) "
            "ORDER BY table_name, ordinal_position",
            (schema, *tables),
        )
        return [(t, c, dt or "unknown", _nullable(n)) for (t, c, dt, n) in rows]

    def fetch_constraints(self, conn: Any, schema: str, tables: list[str]) -> Constraints:
        """Discover PK/FK constraints via the ANSI ``information_schema`` key views."""
        if not tables:
            return {}, {}
        placeholders = ", ".join(["%s"] * len(tables))
        params = (schema, *tables)
        pk: dict[str, list[str]] = {}
        for table_name, column_name in _run(
            conn,
            "SELECT kcu.table_name, kcu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
            "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s "
            f"AND tc.table_name IN ({placeholders}) ORDER BY kcu.table_name, kcu.ordinal_position",
            params,
        ):
            pk.setdefault(table_name, []).append(column_name)
        fk: dict[str, list[tuple[str, str, str]]] = {}
        # Pair each FK column with its referenced column BY POSITION. Joining
        # key_column_usage (FK side) to constraint_column_usage (referenced side)
        # on constraint_name alone yields an N×N cartesian product for composite
        # (multi-column) FKs, mis-pairing columns. The ANSI-correct join walks
        # referential_constraints to the referenced unique/PK constraint and
        # matches kcu.position_in_unique_constraint = ref.ordinal_position, so an
        # (a,b)->(x,y) FK produces exactly a->x and b->y. For single-column FKs
        # position=1 on both sides, so this is equivalent to the old query.
        for fk_table, fk_col, ref_table, ref_col in _run(
            conn,
            "SELECT kcu.table_name, kcu.column_name, ref.table_name, ref.column_name "
            "FROM information_schema.referential_constraints rc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON kcu.constraint_name = rc.constraint_name "
            "  AND kcu.constraint_schema = rc.constraint_schema "
            "JOIN information_schema.key_column_usage ref "
            "  ON ref.constraint_name = rc.unique_constraint_name "
            "  AND ref.constraint_schema = rc.unique_constraint_schema "
            "  AND ref.ordinal_position = kcu.position_in_unique_constraint "
            "WHERE kcu.table_schema = %s "
            f"AND kcu.table_name IN ({placeholders}) "
            "ORDER BY kcu.table_name, kcu.ordinal_position",
            params,
        ):
            fk.setdefault(fk_table, []).append((fk_col, ref_table, ref_col))
        return pk, fk

    def fetch_distinct_values(
        self, conn: Any, schema: str, table: str, columns: list[str], *, max_distinct: int = MAX_ENUM_DISTINCT
    ) -> dict[str, list[str]]:
        """Sample distinct values for low-cardinality *categorical* string columns.

        Two-stage, per candidate column:

        1. A cheap aggregate (``COUNT(*)`` / ``COUNT(DISTINCT col)`` over non-null,
           non-empty values) decides whether the column looks like an enum. A
           column is kept only when it has at least one distinct value, no more
           than ``max_distinct`` distinct values, *and* meaningful repetition
           (``total >= MIN_REPETITION_FACTOR * distinct`` — i.e. each value recurs
           on average). The repetition test is what excludes unique identifiers:
           a count-only cap misses them in *small* tables, where a fully-unique
           id column (``distinct == total``) still has ≤ ``max_distinct`` values.
        2. For columns that pass, one ``SELECT DISTINCT ... LIMIT max_distinct``
           pulls the actual values.

        Identifiers are quoted to prevent SQL injection (they originate from the
        information_schema, not user input, but we quote defensively anyway).
        """
        result: dict[str, list[str]] = {}
        q = self.identifier_quote
        for col in columns:
            try:
                # Quote identifiers with the dialect's quote char (doubled to escape).
                q_schema = f"{q}{schema.replace(q, q + q)}{q}"
                q_table = f"{q}{table.replace(q, q + q)}{q}"
                q_col = f"{q}{col.replace(q, q + q)}{q}"
                where = f"WHERE {q_col} IS NOT NULL AND {q_col} != ''"
                count_rows = _run(
                    conn,
                    f"SELECT COUNT(*), COUNT(DISTINCT {q_col}) FROM {q_schema}.{q_table} {where}",
                )
                if not count_rows:
                    continue
                total, distinct = int(count_rows[0][0] or 0), int(count_rows[0][1] or 0)
                # Enum-like: bounded distinct set with average per-value repetition.
                # Excludes unique-identifier columns (distinct ≈ total) regardless
                # of table size.
                if not (1 <= distinct <= max_distinct and total >= MIN_REPETITION_FACTOR * distinct):
                    continue
                rows = _run(
                    conn,
                    self._limited_distinct_sql(q_col, q_schema, q_table, where, max_distinct),
                )
                values = [str(r[0])[:100] for r in rows if r[0] is not None]
                # VALUE-SHAPE backstop: drop free-text columns that pass the
                # cardinality gate (long / sentence-like values are not enum
                # labels, e.g. a low-cardinality "comment" column).
                if values and values_look_categorical(values):
                    result[col] = values
            except Exception:
                # Best-effort: never abort discovery for a per-column sampling
                # error (missing perms, odd type, etc.). Log at debug so a
                # column that silently yields no values is diagnosable.
                logger.debug("distinct_values failed for column %r in %s.%s", col, schema, table, exc_info=True)
                continue
        return result

    def _limited_distinct_sql(self, q_col: str, q_schema: str, q_table: str, where: str, limit: int) -> str:
        """Build a row-limited ``SELECT DISTINCT`` query.

        Overridden per dialect for engines whose row-limit syntax is not the
        ANSI ``LIMIT`` form.
        """
        return f"SELECT DISTINCT {q_col} FROM {q_schema}.{q_table} {where} LIMIT {limit}"


class PostgresDialect(InformationSchemaDialect):
    """PostgreSQL dialect (via pg8000).

    Redshift is handled by ``RedshiftDialect``, which subclasses this — it speaks
    the same wire protocol but needs a different schema-listing query.
    """

    engines = frozenset({"POSTGRESQL"})
    default_port = 5432
    system_schema_pattern = r"^(pg_catalog|pg_toast.*|information_schema)$"

    def connect(self, *, host, port, database, user, password, options=None):
        """Open a TLS-enabled PostgreSQL connection via pg8000."""
        import pg8000  # noqa: PLC0415

        return pg8000.connect(host=host, port=port, user=user, password=password, database=database, ssl_context=True)

    def fetch_descriptions(self, conn: Any, schema: str, tables: list[str]) -> Descriptions:
        """Pull table / column comments from pg_catalog.pg_description.

        ``obj_description(c.oid)`` returns the table-level COMMENT; per-column
        comments live in ``pg_description.objsubid = pg_attribute.attnum``.
        Works on Redshift as well — it exposes the same catalog views.
        """
        if not tables:
            return {}, {}
        placeholders = ", ".join(["%s"] * len(tables))
        params = (schema, *tables)
        table_desc: dict[str, str] = {}
        for table_name, comment in _run(
            conn,
            "SELECT c.relname, COALESCE(obj_description(c.oid, 'pg_class'), '') "
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = %s AND c.relname IN ({placeholders}) "
            "AND c.relkind IN ('r', 'v', 'm', 'f', 'p')",
            params,
        ):
            if comment:
                table_desc[table_name] = comment
        col_desc: dict[str, dict[str, str]] = {}
        for table_name, col_name, comment in _run(
            conn,
            "SELECT c.relname, a.attname, COALESCE(pg_catalog.col_description(c.oid, a.attnum), '') "
            "FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON a.attrelid = c.oid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = %s AND c.relname IN ({placeholders}) "
            "AND a.attnum > 0 AND NOT a.attisdropped",
            params,
        ):
            if comment:
                col_desc.setdefault(table_name, {})[col_name] = comment
        return table_desc, col_desc

    def fetch_database_description(self, conn: Any, schema: str) -> str:
        """Schema-level COMMENT via ``obj_description((schema)::regnamespace)``.

        PostgreSQL exposes the schema's own COMMENT through pg_description on
        the pg_namespace OID. Redshift supports the same view.
        """
        rows = _run(
            conn,
            "SELECT COALESCE(obj_description(n.oid, 'pg_namespace'), '') "
            "FROM pg_catalog.pg_namespace n WHERE n.nspname = %s",
            (schema,),
        )
        return rows[0][0] if rows and rows[0] and rows[0][0] else ""


class RedshiftDialect(PostgresDialect):
    """Amazon Redshift / Redshift Serverless.

    Speaks the PostgreSQL wire protocol (pg8000) and reuses PostgreSQL's
    table/column/constraint/description SQL, which all work on Redshift. The one
    exception is schema listing: Redshift's ``information_schema.schemata`` is
    unreliable and commonly returns **no rows**, so schemas are listed from
    ``pg_catalog.pg_namespace`` instead (which Redshift populates correctly).

    Redshift's ``information_schema`` also lacks ``position_in_unique_constraint``
    which breaks the ANSI FK discovery query. FK constraints are therefore
    discovered via ``pg_constraint`` + ``pg_attribute`` (Redshift's pg_catalog).

    The system-schema exclude also covers Redshift's extra internal schemas
    (``pg_internal``, ``pg_auto_copy``, ``pg_automv``, ``pg_mv``, ``pg_s3``,
    ``catalog_history``, …) so only real user schemas (e.g. ``public``) remain.
    """

    engines = frozenset({"REDSHIFT"})
    default_port = 5439
    system_schema_pattern = r"^(pg_.*|information_schema|catalog_history)$"

    def list_schemas(self, conn: Any) -> list[str]:
        """List schemas from ``pg_catalog.pg_namespace`` (Redshift's schemata view is unreliable)."""
        return [r[0] for r in _run(conn, "SELECT nspname FROM pg_catalog.pg_namespace ORDER BY nspname")]

    def fetch_constraints(self, conn: Any, schema: str, tables: list[str]) -> Constraints:
        """Redshift-specific constraint discovery via pg_constraint.

        Redshift's information_schema lacks position_in_unique_constraint,
        so the ANSI FK query fails. This uses pg_catalog directly which
        Redshift supports fully.
        """
        if not tables:
            return {}, {}
        placeholders = ", ".join(["%s"] * len(tables))
        params = (schema, *tables)

        # Primary keys
        pk: dict[str, list[str]] = {}
        for table_name, column_name in _run(
            conn,
            "SELECT c.relname, a.attname "
            "FROM pg_constraint con "
            "JOIN pg_class c ON con.conrelid = c.oid "
            "JOIN pg_namespace ns ON c.relnamespace = ns.oid "
            "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = ANY(con.conkey) "
            "WHERE con.contype = 'p' AND ns.nspname = %s "
            f"AND c.relname IN ({placeholders}) "
            "ORDER BY c.relname, a.attnum",
            params,
        ):
            pk.setdefault(table_name, []).append(column_name)

        # Foreign keys
        fk: dict[str, list[tuple[str, str, str]]] = {}
        for fk_table, fk_col, ref_table, ref_col in _run(
            conn,
            "SELECT fk_tbl.relname, fk_col.attname, ref_tbl.relname, ref_col.attname "
            "FROM pg_constraint con "
            "JOIN pg_class fk_tbl ON con.conrelid = fk_tbl.oid "
            "JOIN pg_namespace ns ON fk_tbl.relnamespace = ns.oid "
            "JOIN pg_class ref_tbl ON con.confrelid = ref_tbl.oid "
            "JOIN pg_attribute fk_col ON fk_col.attrelid = con.conrelid "
            "  AND fk_col.attnum = ANY(con.conkey) "
            "JOIN pg_attribute ref_col ON ref_col.attrelid = con.confrelid "
            "  AND ref_col.attnum = ANY(con.confkey) "
            "WHERE con.contype = 'f' AND ns.nspname = %s "
            f"AND fk_tbl.relname IN ({placeholders}) "
            "ORDER BY fk_tbl.relname, fk_col.attnum",
            params,
        ):
            fk.setdefault(fk_table, []).append((fk_col, ref_table, ref_col))

        return pk, fk

    def fetch_columns(self, conn: Any, schema: str, tables: list[str]) -> list[ColumnRow]:
        """Fetch columns, recovering Redshift late-binding views that information_schema misses.

        Redshift late-binding views (``CREATE VIEW ... WITH NO SCHEMA BINDING`` —
        the default output for many dbt models) never populate
        ``information_schema.columns``: the column list is resolved at query time,
        so the inherited query returns zero rows for them with no error, and the
        view shows up with 0 columns (issue #134). Any requested table that comes
        back with no columns is retried through ``pg_get_late_binding_view_cols()``,
        Redshift's own introspection for these views.

        The fallback is scoped to the zero-column tables only: that function is a
        full-catalog scan, so it is never run when information_schema already
        answered for every table. It carries no nullability, so recovered columns
        default to nullable — the safe assumption when nullability is unknown.
        """
        rows = super().fetch_columns(conn, schema, tables)
        covered = {table_name for (table_name, _c, _dt, _n) in rows}
        missing = [t for t in tables if t not in covered]
        if not missing:
            return rows

        placeholders = ", ".join(["%s"] * len(missing))
        late_rows = _run(
            conn,
            "SELECT viewname, columnname, columntype "
            "FROM pg_get_late_binding_view_cols() "
            "AS cols(schemaname name, viewname name, columnname name, columntype varchar, columnnum int) "
            f"WHERE schemaname = %s AND viewname IN ({placeholders}) "
            "ORDER BY viewname, columnnum",
            (schema, *missing),
        )
        # pg_get_late_binding_view_cols has no is_nullable -> default nullable=True.
        rows.extend((v, c, dt or "unknown", True) for (v, c, dt) in late_rows)
        return rows


class MySqlDialect(InformationSchemaDialect):
    """MySQL / MariaDB (via pymysql). FK metadata lives in KEY_COLUMN_USAGE."""

    engines = frozenset({"MYSQL"})
    default_port = 3306
    system_schema_pattern = r"^(information_schema|mysql|performance_schema|sys)$"
    # MySQL/MariaDB quote identifiers with backticks; "x" is a string literal
    # under the default sql_mode. Enum-value sampling would otherwise fail.
    identifier_quote = "`"

    def connect(self, *, host, port, database, user, password, options=None):
        """Open a TLS-enabled MySQL/MariaDB connection via pymysql."""
        import ssl  # noqa: PLC0415

        import pymysql  # type: ignore[import-untyped]  # noqa: PLC0415

        return pymysql.connect(
            host=host, port=port, user=user, password=password, database=database, ssl=ssl.create_default_context()
        )

    def fetch_constraints(self, conn: Any, schema: str, tables: list[str]) -> Constraints:
        """Discover PK/FK constraints from ``KEY_COLUMN_USAGE`` (MySQL has no constraint_column_usage)."""
        # MySQL has no information_schema.constraint_column_usage; FK targets are
        # on KEY_COLUMN_USAGE itself (referenced_table_name/_column_name).
        if not tables:
            return {}, {}
        placeholders = ", ".join(["%s"] * len(tables))
        params = (schema, *tables)
        pk: dict[str, list[str]] = {}
        for table_name, column_name in _run(
            conn,
            "SELECT table_name, column_name FROM information_schema.key_column_usage "
            "WHERE constraint_name = 'PRIMARY' AND table_schema = %s "
            f"AND table_name IN ({placeholders}) ORDER BY table_name, ordinal_position",
            params,
        ):
            pk.setdefault(table_name, []).append(column_name)
        fk: dict[str, list[tuple[str, str, str]]] = {}
        for fk_table, fk_col, ref_table, ref_col in _run(
            conn,
            "SELECT table_name, column_name, referenced_table_name, referenced_column_name "
            "FROM information_schema.key_column_usage "
            "WHERE referenced_table_name IS NOT NULL AND table_schema = %s "
            f"AND table_name IN ({placeholders}) ORDER BY table_name",
            params,
        ):
            fk.setdefault(fk_table, []).append((fk_col, ref_table, ref_col))
        return pk, fk

    def fetch_descriptions(self, conn: Any, schema: str, tables: list[str]) -> Descriptions:
        """MySQL exposes comments directly on information_schema.

        ``information_schema.tables.table_comment`` and
        ``information_schema.columns.column_comment``.
        """
        if not tables:
            return {}, {}
        placeholders = ", ".join(["%s"] * len(tables))
        params = (schema, *tables)
        table_desc: dict[str, str] = {}
        for table_name, comment in _run(
            conn,
            "SELECT table_name, COALESCE(table_comment, '') FROM information_schema.tables "
            f"WHERE table_schema = %s AND table_name IN ({placeholders})",
            params,
        ):
            if comment:
                table_desc[table_name] = comment
        col_desc: dict[str, dict[str, str]] = {}
        for table_name, col_name, comment in _run(
            conn,
            "SELECT table_name, column_name, COALESCE(column_comment, '') "
            "FROM information_schema.columns "
            f"WHERE table_schema = %s AND table_name IN ({placeholders})",
            params,
        ):
            if comment:
                col_desc.setdefault(table_name, {})[col_name] = comment
        return table_desc, col_desc


class SqlServerDialect(InformationSchemaDialect):
    """Microsoft SQL Server (via python-tds, pure-Python — no ODBC needed)."""

    engines = frozenset({"SQLSERVER"})
    default_port = 1433
    system_schema_pattern = r"^(sys|INFORMATION_SCHEMA|guest|db_.*)$"

    def connect(self, *, host, port, database, user, password, options=None):
        """Open a SQL Server connection via the pure-Python python-tds driver."""
        import pytds  # type: ignore[import-untyped]  # noqa: PLC0415

        return pytds.connect(server=host, port=port, database=database, user=user, password=password)

    def _limited_distinct_sql(self, q_col: str, q_schema: str, q_table: str, where: str, limit: int) -> str:
        # SQL Server has no LIMIT clause — row-limiting uses SELECT TOP N.
        return f"SELECT DISTINCT TOP {limit} {q_col} FROM {q_schema}.{q_table} {where}"

    def fetch_descriptions(self, conn: Any, schema: str, tables: list[str]) -> Descriptions:
        """SQL Server descriptions live in sys.extended_properties (MS_Description).

        Joins ``sys.tables`` / ``sys.columns`` to the extended properties view
        filtered by the standard ``MS_Description`` property name.
        ``minor_id = 0`` identifies a table-level property; column-level
        properties have ``minor_id = sys.columns.column_id``.
        """
        if not tables:
            return {}, {}
        placeholders = ", ".join(["%s"] * len(tables))
        params = (schema, *tables)
        table_desc: dict[str, str] = {}
        for table_name, value in _run(
            conn,
            "SELECT t.name, CAST(ep.value AS NVARCHAR(MAX)) "
            "FROM sys.tables t "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "JOIN sys.extended_properties ep "
            "  ON ep.major_id = t.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description' "
            f"WHERE s.name = %s AND t.name IN ({placeholders})",
            params,
        ):
            if value:
                table_desc[table_name] = str(value)
        col_desc: dict[str, dict[str, str]] = {}
        for table_name, col_name, value in _run(
            conn,
            "SELECT t.name, c.name, CAST(ep.value AS NVARCHAR(MAX)) "
            "FROM sys.columns c "
            "JOIN sys.tables t ON c.object_id = t.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "JOIN sys.extended_properties ep "
            "  ON ep.major_id = c.object_id AND ep.minor_id = c.column_id AND ep.name = 'MS_Description' "
            f"WHERE s.name = %s AND t.name IN ({placeholders})",
            params,
        ):
            if value:
                col_desc.setdefault(table_name, {})[col_name] = str(value)
        return table_desc, col_desc


class SnowflakeDialect(InformationSchemaDialect):
    """Snowflake (via snowflake-connector-python).

    INFORMATION_SCHEMA queries require an active warehouse, so ``warehouse`` (and
    optionally ``role``) must be supplied via ``options`` (sourced from the JDBC
    config). The account is derived from the host (``<account>.snowflakecomputing.com``).
    Snowflake does not enforce/expose FK metadata, so constraints are best-effort.
    """

    engines = frozenset({"SNOWFLAKE"})
    default_port = 443
    system_schema_pattern = r"^(INFORMATION_SCHEMA)$"

    def connect(self, *, host, port, database, user, password, options=None):
        """Open a Snowflake connection, deriving the account from the host.

        The warehouse and (optional) role are read from ``options`` since
        INFORMATION_SCHEMA queries require an active warehouse.

        Raises:
            ValueError: If ``host`` is not a ``<account>.snowflakecomputing.com``
                name.
        """
        options = options or {}
        if not host.endswith(".snowflakecomputing.com"):
            raise ValueError(f"Invalid Snowflake host (expected <account>.snowflakecomputing.com): {host!r}")
        account = host.split(".snowflakecomputing.com")[0]

        import snowflake.connector  # type: ignore[import-untyped]  # noqa: PLC0415

        return snowflake.connector.connect(
            account=account,
            host=host,
            user=user,
            password=password,
            database=database,
            warehouse=options.get("warehouse"),
            role=options.get("role"),
        )

    def fetch_descriptions(self, conn: Any, schema: str, tables: list[str]) -> Descriptions:
        """Snowflake exposes COMMENT directly on INFORMATION_SCHEMA views."""
        if not tables:
            return {}, {}
        placeholders = ", ".join(["%s"] * len(tables))
        params = (schema, *tables)
        table_desc: dict[str, str] = {}
        for table_name, comment in _run(
            conn,
            "SELECT table_name, COALESCE(comment, '') FROM information_schema.tables "
            f"WHERE table_schema = %s AND table_name IN ({placeholders})",
            params,
        ):
            if comment:
                table_desc[table_name] = comment
        col_desc: dict[str, dict[str, str]] = {}
        for table_name, col_name, comment in _run(
            conn,
            "SELECT table_name, column_name, COALESCE(comment, '') "
            "FROM information_schema.columns "
            f"WHERE table_schema = %s AND table_name IN ({placeholders})",
            params,
        ):
            if comment:
                col_desc.setdefault(table_name, {})[col_name] = comment
        return table_desc, col_desc

    def fetch_database_description(self, conn: Any, schema: str) -> str:
        """Schema-level COMMENT via ``information_schema.schemata.comment``."""
        rows = _run(
            conn,
            "SELECT COALESCE(comment, '') FROM information_schema.schemata WHERE schema_name = %s",
            (schema,),
        )
        return rows[0][0] if rows and rows[0] and rows[0][0] else ""


class OracleDialect(Dialect):
    """Oracle (via oracledb thin mode — pure Python, no Oracle client).

    Oracle has no information_schema; uses ALL_* catalog views and ``:n`` binds.
    A "schema" is an owner/user. ``database`` is the service name.
    """

    engines = frozenset({"ORACLE"})
    default_port = 1521
    system_schema_pattern = (
        r"^(SYS|SYSTEM|OUTLN|XDB|CTXSYS|MDSYS|DBSNMP|APPQOSSYS|ORDSYS|ORDDATA|WMSYS|"
        r"LBACSYS|DVSYS|AUDSYS|GSMADMIN_INTERNAL|OJVMSYS|DBSFWUSER)$"
    )

    def connect(self, *, host, port, database, user, password, options=None):
        """Open an Oracle connection via oracledb thin mode (no Oracle client)."""
        import oracledb  # type: ignore[import-untyped]  # noqa: PLC0415

        return oracledb.connect(user=user, password=password, dsn=f"{host}:{port}/{database}")

    def list_schemas(self, conn: Any) -> list[str]:
        """List schema owners from the ``all_users`` catalog view."""
        return [r[0] for r in _run(conn, "SELECT username FROM all_users ORDER BY username")]

    def list_tables(self, conn: Any, schema: str) -> list[str]:
        """List tables and views for an owner from ``all_tables`` / ``all_views``."""
        # `:owner` is a NAMED bind, deliberately: with positional `:1` binds
        # python-oracledb counts each OCCURRENCE, so reusing `:1` across both
        # UNION arms demands two values and discovery died on every Oracle
        # source with
        #   DPY-4009: 2 positional bind values are required but 1 were provided
        # A named bind is supplied once and referenced as often as needed.
        rows = _run(
            conn,
            "SELECT table_name FROM all_tables WHERE owner = :owner "
            "UNION SELECT view_name FROM all_views WHERE owner = :owner ORDER BY 1",
            {"owner": schema},
        )
        return [r[0] for r in rows]

    def fetch_columns(self, conn: Any, schema: str, tables: list[str]) -> list[ColumnRow]:
        """Fetch column metadata from ``all_tab_columns``, ordered by column id."""
        if not tables:
            return []
        binds = ", ".join(f":{i + 2}" for i in range(len(tables)))
        rows = _run(
            conn,
            "SELECT table_name, column_name, data_type, nullable FROM all_tab_columns "
            f"WHERE owner = :1 AND table_name IN ({binds}) ORDER BY table_name, column_id",
            (schema, *tables),
        )
        return [(t, c, dt or "unknown", _nullable(n)) for (t, c, dt, n) in rows]

    def fetch_constraints(self, conn: Any, schema: str, tables: list[str]) -> Constraints:
        """Discover PK/FK constraints from Oracle's ``all_constraints`` catalog views."""
        if not tables:
            return {}, {}
        binds = ", ".join(f":{i + 2}" for i in range(len(tables)))
        params = (schema, *tables)
        pk: dict[str, list[str]] = {}
        for table_name, column_name in _run(
            conn,
            "SELECT acc.table_name, acc.column_name FROM all_constraints ac "
            "JOIN all_cons_columns acc ON ac.constraint_name = acc.constraint_name AND ac.owner = acc.owner "
            f"WHERE ac.constraint_type = 'P' AND ac.owner = :1 AND acc.table_name IN ({binds}) "
            "ORDER BY acc.table_name, acc.position",
            params,
        ):
            pk.setdefault(table_name, []).append(column_name)
        fk: dict[str, list[tuple[str, str, str]]] = {}
        for fk_table, fk_col, ref_table, ref_col in _run(
            conn,
            "SELECT acc.table_name, acc.column_name, rcc.table_name, rcc.column_name FROM all_constraints ac "
            "JOIN all_cons_columns acc ON ac.constraint_name = acc.constraint_name AND ac.owner = acc.owner "
            "JOIN all_cons_columns rcc ON ac.r_constraint_name = rcc.constraint_name "
            "  AND ac.r_owner = rcc.owner AND acc.position = rcc.position "
            f"WHERE ac.constraint_type = 'R' AND ac.owner = :1 AND acc.table_name IN ({binds}) "
            "ORDER BY acc.table_name",
            params,
        ):
            fk.setdefault(fk_table, []).append((fk_col, ref_table, ref_col))
        return pk, fk

    def fetch_descriptions(self, conn: Any, schema: str, tables: list[str]) -> Descriptions:
        """Oracle stores comments in ALL_TAB_COMMENTS and ALL_COL_COMMENTS."""
        if not tables:
            return {}, {}
        binds = ", ".join(f":{i + 2}" for i in range(len(tables)))
        params = (schema, *tables)
        table_desc: dict[str, str] = {}
        for table_name, comment in _run(
            conn,
            f"SELECT table_name, NVL(comments, '') FROM all_tab_comments WHERE owner = :1 AND table_name IN ({binds})",
            params,
        ):
            if comment:
                table_desc[table_name] = comment
        col_desc: dict[str, dict[str, str]] = {}
        for table_name, col_name, comment in _run(
            conn,
            "SELECT table_name, column_name, NVL(comments, '') FROM all_col_comments "
            f"WHERE owner = :1 AND table_name IN ({binds})",
            params,
        ):
            if comment:
                col_desc.setdefault(table_name, {})[col_name] = comment
        return table_desc, col_desc


_IMPLEMENTED_DIALECTS: tuple[Dialect, ...] = (
    PostgresDialect(),
    RedshiftDialect(),
    MySqlDialect(),
    SqlServerDialect(),
    SnowflakeDialect(),
    OracleDialect(),
)

# All implemented dialects are enabled.
_DIALECTS: tuple[Dialect, ...] = _IMPLEMENTED_DIALECTS

# Engines for which schema discovery is implemented and enabled (one dialect each).
DISCOVERY_ENGINES: frozenset[str] = frozenset().union(*(d.engines for d in _DIALECTS))


def get_dialect(engine: str | None) -> Dialect | None:
    """Return the dialect serving ``engine`` (case-insensitive), or None.

    None means "no discovery path for this engine" — an unknown token. Callers
    surface it as an unsupported-engine error.
    """
    if not engine:
        return None
    engine = engine.upper()
    for dialect in _DIALECTS:
        if engine in dialect.engines:
            return dialect
    return None
