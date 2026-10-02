// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.metadata;

import dev.coa.databricks.jdbc.Identifiers;

/**
 * The statements this connector issues against one Unity Catalog schema, as text.
 *
 * <p>Kept apart from the code that runs them so the SQL is assertable without a warehouse. Every
 * method takes the Unity Catalog catalog, unquoted. That is the only identifier interpolated into
 * any of these (plus the schema, for {@code SHOW TABLES}), and it goes through {@link Identifiers};
 * everything else is a bind parameter. The table-scoped statements take {@code table_catalog},
 * {@code table_schema} and {@code table_name}, in that order.
 *
 * <p>Five things measured against a live SQL Warehouse, none of them visible in the SQL:
 *
 * <ol>
 *   <li>{@code SHOW TABLES} decides what exists, not {@code information_schema.tables}. Creating a
 *       materialized view or a streaming table also creates internal side tables
 *       ({@code __materialization_mat_<uuid>_<name>_1}, {@code event_log_<uuid>}), and
 *       {@code information_schema.tables} lists them as ordinary {@code MANAGED} tables with nothing
 *       in any of its fifteen columns marking them internal. Four such rows out of eleven on the
 *       fixture schema. {@code SHOW TABLES} omits them, so it answers "what exists" and
 *       {@code information_schema.tables} is read only for the {@code table_type} allowlist.</li>
 *   <li>Every statement predicates on catalog and schema. {@code LIMIT} is not pushed down inside
 *       {@code information_schema}, so an unfiltered read of a large metastore's {@code columns} view
 *       scans the whole thing and can time out.</li>
 *   <li>Table names come back lower-cased; column names keep their case. {@code CREATE TABLE
 *       MixedCaseTable} gives {@code table_name = "mixedcasetable"}, while {@code CustomerName
 *       STRING} gives {@code column_name = "CustomerName"}. So catalog, schema and table are compared
 *       against lower-case literals, and a column name is carried through exactly as returned,
 *       because that string has to match the field names Athena projects.</li>
 *   <li>{@code ordinal_position} counts from 0 in {@code information_schema.columns} and from 1 in
 *       {@code key_column_usage}. Neither base is documented. Never join the two on it. Both uses
 *       here are {@code ORDER BY}, which does not care.</li>
 *   <li>The foreign-key join walks {@code referential_constraints}. The obvious join,
 *       {@code key_column_usage} to {@code constraint_column_usage} on {@code constraint_name},
 *       gives an N x N cartesian product for a composite key and pairs child columns with the wrong
 *       parents. It survives review because for a single-column key N x N is 1 x 1. A two-column key
 *       returns four rows, two of them wrong.</li>
 * </ol>
 */
public final class InformationSchemaSql
{
    private InformationSchemaSql()
    {
    }

    /** The catalog's schemas. Only an unpinned connector issues this; a pinned one has the answer. */
    // information_schema is excluded in the SQL. Every Unity Catalog catalog has one and schemata
    // lists it, so without the predicate an unpinned connector advertises it to Athena and COA
    // discovers columns, tables, key_column_usage and the rest as ordinary tables.
    public static String schemata(String catalog)
    {
        return "SELECT schema_name"
                + " FROM " + informationSchema(catalog) + ".schemata"
                + " WHERE catalog_name = ?"
                + " AND schema_name <> 'information_schema'"
                + " ORDER BY schema_name";
    }

    /** Everything the schema contains, as Databricks reports it. Yields database/tableName/isTemporary. */
    // No bind parameters: SHOW has no WHERE clause, so the schema is an identifier here rather than a
    // value. Both segments are quoted, and both have already passed ConnectionConfig's pattern.
    public static String showTables(String catalog, String schema)
    {
        return "SHOW TABLES IN " + Identifiers.qualify(catalog, schema);
    }

    /** Each table's type, for applying the allowlist to what SHOW TABLES returned. Takes no table_name. */
    // Returns the type rather than filtering on it, so the caller can tell "excluded by the allowlist"
    // from "not a real table". The first is worth a log line naming the type; the second is noise.
    public static String tableTypes(String catalog)
    {
        return "SELECT table_name, table_type"
                + " FROM " + informationSchema(catalog) + ".tables"
                + " WHERE table_catalog = ?"
                + " AND table_schema = ?"
                + " ORDER BY table_name";
    }

    /** One table's type, so describeTable applies the same allowlist as listTables. */
    // Not a reuse of tableTypes(): that reads the whole schema, so a 200-table discovery would pay
    // for it per DESCRIBE.
    public static String tableType(String catalog)
    {
        return "SELECT table_type"
                + " FROM " + informationSchema(catalog) + ".tables"
                + " WHERE table_catalog = ?"
                + " AND table_schema = ?"
                + " AND table_name = ?";
    }

    /** One table's columns, types, nullability and comments. */
    // full_data_type, not data_type: the latter drops a decimal's precision and scale, turning
    // DECIMAL(38,9) into a default-scaled decimal and changing every value in the column.
    //
    // is_nullable rides along because it is the only route the fact has: Athena's Column type has no
    // nullability field and DESCRIBE returns name, type and comment, so it reaches COA as a @notnull tag
    // inside the comment or not at all. Databricks answers 'YES' or 'NO'.
    public static String columns(String catalog)
    {
        // ordinal_position is 0-based here. Only sorting on it, so that's fine.
        return "SELECT column_name, full_data_type, is_nullable, comment"
                + " FROM " + informationSchema(catalog) + ".columns"
                + " WHERE table_catalog = ?"
                + " AND table_schema = ?"
                + " AND table_name = ?"
                + " ORDER BY ordinal_position";
    }

    /** One table's declared primary-key columns, in key order. */
    public static String primaryKey(String catalog)
    {
        String schema = informationSchema(catalog);
        return "SELECT kcu.column_name"
                + " FROM " + schema + ".table_constraints tc"
                + " JOIN " + schema + ".key_column_usage kcu"
                + " ON kcu.constraint_catalog = tc.constraint_catalog"
                + " AND kcu.constraint_schema = tc.constraint_schema"
                + " AND kcu.constraint_name = tc.constraint_name"
                + " WHERE tc.constraint_type = 'PRIMARY KEY'"
                + " AND tc.table_catalog = ?"
                + " AND tc.table_schema = ?"
                + " AND tc.table_name = ?"
                + " ORDER BY kcu.ordinal_position";
    }

    /** One table's declared foreign keys, one row per participating child column. */
    // The four-way join is the point. tc selects the table's foreign-key constraints; rc names the
    // unique or primary-key constraint each one references; kcu lists the child columns with their
    // position_in_unique_constraint; ref is the referenced constraint's own column list, joined on
    // that position. Pair on constraint_name alone and a two-column key gives four rows, two of them
    // pairing the wrong columns.
    //
    // The parent's catalog, schema and table_type are selected rather than filtered on, so the reader
    // can drop a reference and log which one and why. The @fk(table.column) tag has no slot for a
    // schema and COA resolves it inside the one schema this connector exposes, so a cross-schema
    // parent would either dangle or bind to a same-named table in the wrong schema. The pt join is a
    // LEFT JOIN for the same reason: a parent missing from information_schema.tables still has to
    // produce a row someone can report.
    public static String foreignKeys(String catalog)
    {
        String schema = informationSchema(catalog);
        return "SELECT kcu.column_name AS child_column,"
                + " ref.table_catalog AS parent_catalog,"
                + " ref.table_schema AS parent_schema,"
                + " ref.table_name AS parent_table,"
                + " ref.column_name AS parent_column,"
                + " pt.table_type AS parent_table_type"
                + " FROM " + schema + ".table_constraints tc"
                + " JOIN " + schema + ".referential_constraints rc"
                + " ON rc.constraint_catalog = tc.constraint_catalog"
                + " AND rc.constraint_schema = tc.constraint_schema"
                + " AND rc.constraint_name = tc.constraint_name"
                + " JOIN " + schema + ".key_column_usage kcu"
                + " ON kcu.constraint_catalog = tc.constraint_catalog"
                + " AND kcu.constraint_schema = tc.constraint_schema"
                + " AND kcu.constraint_name = tc.constraint_name"
                + " JOIN " + schema + ".key_column_usage ref"
                + " ON ref.constraint_catalog = rc.unique_constraint_catalog"
                + " AND ref.constraint_schema = rc.unique_constraint_schema"
                + " AND ref.constraint_name = rc.unique_constraint_name"
                + " AND ref.ordinal_position = kcu.position_in_unique_constraint"
                + " LEFT JOIN " + schema + ".tables pt"
                + " ON pt.table_catalog = ref.table_catalog"
                + " AND pt.table_schema = ref.table_schema"
                + " AND pt.table_name = ref.table_name"
                + " WHERE tc.constraint_type = 'FOREIGN KEY'"
                + " AND tc.table_catalog = ?"
                + " AND tc.table_schema = ?"
                + " AND tc.table_name = ?"
                + " ORDER BY tc.constraint_name, kcu.ordinal_position";
    }

    /** {@code `catalog`.information_schema}. */
    private static String informationSchema(String catalog)
    {
        // information_schema is a fixed bare identifier, so it needs no quoting, and quoting it would
        // imply it could be something else.
        return Identifiers.quote(catalog) + ".information_schema";
    }
}
