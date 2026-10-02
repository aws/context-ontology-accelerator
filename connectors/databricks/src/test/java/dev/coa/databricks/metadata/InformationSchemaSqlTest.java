// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.metadata;

import org.junit.jupiter.api.Test;

import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.util.Arrays;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Pins the shape of every statement, because each carries a rule that is invisible in the SQL and
 * whose violation fails silently.
 */
class InformationSchemaSqlTest
{
    @Test
    void showTablesIsTheEnumerationAuthority()
    {
        // Not information_schema.tables: creating a materialized view or streaming table also creates
        // __materialization_* and event_log_* side tables, which that view lists as ordinary MANAGED
        // tables with nothing marking them internal. Four junk rows out of eleven on the fixture schema.
        assertTrue(InformationSchemaSql.showTables("main", "sales")
                .equals("SHOW TABLES IN `main`.`sales`"));
    }

    /**
     * Every {@code information_schema} statement this class publishes, found by reflection, as name to
     * SQL so a failure names the method rather than quoting anonymous SQL.
     *
     * <p>Reflection rather than a hand-written list: the two tests below are named "every statement", and
     * a list only holds while someone remembers to extend it. The selector is "public static, returns
     * String, takes exactly one String", the catalog, which is every statement here except
     * {@link InformationSchemaSql#showTables(String, String)}, a {@code SHOW} rather than a query against
     * the view.
     */
    private static Map<String, String> everyInformationSchemaStatement()
    {
        return statementsBuiltWithCatalog("main");
    }

    /** @see #everyInformationSchemaStatement() */
    private static Map<String, String> statementsBuiltWithCatalog(String catalog)
    {
        Map<String, String> statements = new TreeMap<>();
        for (Method method : InformationSchemaSql.class.getDeclaredMethods()) {
            if (!Modifier.isPublic(method.getModifiers()) || !Modifier.isStatic(method.getModifiers())) {
                continue;
            }
            if (method.getReturnType() != String.class
                    || method.getParameterCount() != 1
                    || method.getParameterTypes()[0] != String.class) {
                continue;
            }
            try {
                statements.put(method.getName(), (String) method.invoke(null, catalog));
            }
            catch (ReflectiveOperationException cause) {
                throw new AssertionError("could not invoke " + method.getName(), cause);
            }
        }
        return statements;
    }

    @Test
    void theReflectionFindsEveryStatementThisClassPublishes()
    {
        // Guards the guard: if the selector stops matching, the two tests below pass vacuously over an
        // empty set. Adding a statement means updating one obvious list, and forgetting means a failure
        // here rather than silent under-coverage.
        assertEquals(new TreeSet<>(Arrays.asList(
                        "columns", "foreignKeys", "primaryKey", "schemata", "tableType",
                        "tableTypes")),
                new TreeSet<>(everyInformationSchemaStatement().keySet()));
    }

    /**
     * The one catalog-scoped statement, which cannot carry the table predicates every other one is held
     * to. Named here rather than accommodated by a looser predicate: the invariant below is what keeps a
     * new statement from scanning a large metastore.
     */
    private static final String CATALOG_SCOPED_STATEMENT = "schemata";

    @Test
    void everyInformationSchemaStatementFiltersOnCatalogAndSchema()
    {
        // LIMIT is not pushed down inside information_schema, so an unfiltered read of a large
        // metastore's columns view scans it and can time out.
        for (Map.Entry<String, String> statement : everyInformationSchemaStatement().entrySet()) {
            if (CATALOG_SCOPED_STATEMENT.equals(statement.getKey())) {
                continue;
            }
            assertTrue(statement.getValue().contains("table_catalog = ?"), statement.getKey());
            assertTrue(statement.getValue().contains("table_schema = ?"), statement.getKey());
        }
    }

    @Test
    void theCatalogScopedStatementStillBindsItsOwnPredicate()
    {
        // schemata has no table, so it is exempt from the pair above but not from being filtered.
        // information_schema.schemata spans the metastore, and an unpredicated read of a large one is the
        // scan the rule above exists to prevent.
        String sql = InformationSchemaSql.schemata("main");
        assertTrue(sql.contains("catalog_name = ?"), sql);
    }

    @Test
    void schemataExcludesInformationSchemaItself()
    {
        // Every Unity Catalog catalog contains an information_schema, and schemata lists it. Left in, an
        // unpinned connector advertises it to Athena and COA discovers the metastore views as ordinary
        // tables for a steward to review.
        String sql = InformationSchemaSql.schemata("main");
        assertTrue(sql.contains("schema_name <> 'information_schema'"), sql);
    }

    @Test
    void schemataIsOrderedSoListSchemaNamesIsStable()
    {
        // Athena caches nothing here, and an unstable order makes a diff of two discoveries unreadable.
        assertTrue(InformationSchemaSql.schemata("main").endsWith("ORDER BY schema_name"),
                InformationSchemaSql.schemata("main"));
    }

    @Test
    void everyStatementReadsTheCatalogsOwnInformationSchema()
    {
        for (Map.Entry<String, String> statement : everyInformationSchemaStatement().entrySet()) {
            assertTrue(statement.getValue().contains("`main`.information_schema"), statement.getKey());
        }
    }

    @Test
    void everyStatementQuotesTheCatalogSoItCannotEscape()
    {
        // Unreachable through this connector, since ConnectionConfig refuses such a catalog, but the
        // quoting is the last line of defence and every statement needs it rather than the ones somebody
        // remembered.
        for (Map.Entry<String, String> statement
                : statementsBuiltWithCatalog("ma`in").entrySet()) {
            assertTrue(statement.getValue().contains("`ma``in`.information_schema"),
                    statement.getKey() + ": " + statement.getValue());
        }
    }

    @Test
    void columnsReadsFullDataTypeSoADecimalKeepsItsPrecision()
    {
        // data_type gives "DECIMAL" and loses (38,9), changing every value in the column without
        // erroring.
        String sql = InformationSchemaSql.columns("main");
        assertTrue(sql.contains("full_data_type"), sql);
        assertFalse(sql.matches(".*\\bdata_type\\b(?!.*full_data_type).*"), sql);
    }

    @Test
    void columnsAreOrderedByOrdinalPositionBecauseThatIsThePrimaryKeysOrder()
    {
        // The tag grammar has no ordinal: COA reads a composite primary key as the set of @pk columns
        // in DESCRIBE order, which is this order.
        assertTrue(InformationSchemaSql.columns("main").endsWith("ORDER BY ordinal_position"),
                InformationSchemaSql.columns("main"));
    }

    @Test
    void columnsSelectsIsNullableBecauseTheTagChannelCanNowCarryIt()
    {
        // Free: is_nullable is a column of the row this statement already reads, so carrying the fact costs
        // no extra statement.
        assertTrue(InformationSchemaSql.columns("main").contains("is_nullable"),
                InformationSchemaSql.columns("main"));
    }

    @Test
    void theForeignKeyJoinWalksReferentialConstraints()
    {
        String sql = InformationSchemaSql.foreignKeys("main");
        assertTrue(sql.contains("referential_constraints"), sql);
        assertTrue(sql.contains("ref.ordinal_position = kcu.position_in_unique_constraint"), sql);
        assertTrue(sql.contains("ref.constraint_name = rc.unique_constraint_name"), sql);
    }

    @Test
    void theForeignKeyJoinNeverTouchesConstraintColumnUsage()
    {
        // Joining key_column_usage to constraint_column_usage on constraint_name gives an N x N cartesian
        // product for a composite key and mis-pairs the columns: four rows for a two-column key, two of
        // them wrong. It is right for a single-column key, which is why it survives review.
        assertFalse(InformationSchemaSql.foreignKeys("main").contains("constraint_column_usage"),
                InformationSchemaSql.foreignKeys("main"));
    }

    @Test
    void theForeignKeyStatementReportsWhereTheParentLivesRatherThanFilteringOnIt()
    {
        // The @fk(table.column) tag has no slot for a schema and COA resolves it inside the one schema
        // this connector exposes, so a parent in another schema has to be dropped and logged rather than
        // emitted, which needs the parent's coordinates in the result set.
        String sql = InformationSchemaSql.foreignKeys("main");
        assertTrue(sql.contains("ref.table_catalog AS parent_catalog"), sql);
        assertTrue(sql.contains("ref.table_schema AS parent_schema"), sql);
        assertTrue(sql.contains("pt.table_type AS parent_table_type"), sql);
    }

    @Test
    void theParentsTableTypeIsLeftJoinedSoAnInvisibleParentStillProducesARow()
    {
        // An inner join would make a parent absent from information_schema.tables vanish, which is the
        // case that most needs reporting: a privilege-filtered view looks identical to a dropped table.
        String sql = InformationSchemaSql.foreignKeys("main");
        assertTrue(sql.contains("LEFT JOIN `main`.information_schema.tables pt"), sql);
    }

    @Test
    void theForeignKeyJoinIsOrderedByConstraintThenChildOrdinal()
    {
        // Stable output for a table with two foreign keys, and the child ordinal is what pairs a
        // composite key's columns with their parents.
        assertTrue(InformationSchemaSql.foreignKeys("main")
                .endsWith("ORDER BY tc.constraint_name, kcu.ordinal_position"),
                InformationSchemaSql.foreignKeys("main"));
    }

    @Test
    void noStatementJoinsColumnsToKeyColumnUsageOnOrdinalPosition()
    {
        // information_schema.columns.ordinal_position is 0-based, key_column_usage.ordinal_position is
        // 1-based. Neither base is documented, and joining them is off by one, pairing each key column
        // with its neighbour.
        String sql = InformationSchemaSql.primaryKey("main") + InformationSchemaSql.foreignKeys("main");
        assertFalse(sql.contains(".columns"), sql);
    }

    @Test
    void theConstraintStatementsSelectByConstraintType()
    {
        assertTrue(InformationSchemaSql.primaryKey("main")
                .contains("tc.constraint_type = 'PRIMARY KEY'"));
        assertTrue(InformationSchemaSql.foreignKeys("main")
                .contains("tc.constraint_type = 'FOREIGN KEY'"));
    }
}
