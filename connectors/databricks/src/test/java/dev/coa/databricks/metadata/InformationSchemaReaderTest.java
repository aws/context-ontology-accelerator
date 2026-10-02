// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.metadata;

import dev.coa.connector.metadata.CoaTable;
import dev.coa.databricks.FakeJdbc;
import dev.coa.databricks.config.ConnectionConfig;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.coa.databricks.FakeJdbc.row;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The reader against a recorded JDBC layer: what it asks, what it binds, and what it builds. The row
 * fixtures mirror what a live SQL Warehouse returned, including the internal side tables
 * {@code information_schema.tables} reports and the composite foreign key whose child columns are named
 * differently from their parents.
 */
class InformationSchemaReaderTest
{
    private static final String CATALOG = "workspace";
    private static final String SCHEMA = "coa_dbx_test";

    private static ConnectionConfig config()
    {
        return ConnectionConfig.builder()
                .workspaceHostname("dbc-a1b2345c-d6e7.cloud.databricks.com")
                .httpPath("/sql/1.0/warehouses/a1b234c567d8e9fa")
                .catalog(CATALOG)
                .schema(SCHEMA)
                .credentialSecretArn(
                        "arn:aws:secretsmanager:us-east-1:111122223333:secret:dbx-AbCdEf")
                .build();
    }

    /** The live fixture schema's {@code SHOW TABLES} output. */
    private static List<Map<String, Object>> showTablesRows()
    {
        List<Map<String, Object>> rows = new ArrayList<>();
        for (String name : Arrays.asList("coa_dbx_test_audit", "mixedcasetable", "order_counts_mv",
                "order_lines", "order_stream_st", "order_totals_vw", "orders")) {
            rows.add(row("database", SCHEMA, "tableName", name, "isTemporary", "false"));
        }
        return rows;
    }

    /**
     * The same schema's {@code information_schema.tables} output, which carries the internal side tables
     * {@code SHOW TABLES} does not. The excluded {@code table_type} values are added by the test that
     * covers them.
     */
    private static List<Map<String, Object>> tableTypeRows()
    {
        List<Map<String, Object>> rows = new ArrayList<>();
        rows.add(row("table_name",
                "__materialization_mat_96ea77da_9ea6_45e9_8309_13bdb824a013_order_counts_mv_1",
                "table_type", "MANAGED"));
        rows.add(row("table_name", "event_log_96ea77da_9ea6_45e9_8309_13bdb824a013",
                "table_type", "MANAGED"));
        rows.add(row("table_name", "coa_dbx_test_audit", "table_type", "MANAGED"));
        rows.add(row("table_name", "mixedcasetable", "table_type", "MANAGED"));
        rows.add(row("table_name", "order_counts_mv", "table_type", "MATERIALIZED_VIEW"));
        rows.add(row("table_name", "order_lines", "table_type", "MANAGED"));
        rows.add(row("table_name", "order_stream_st", "table_type", "STREAMING_TABLE"));
        rows.add(row("table_name", "order_totals_vw", "table_type", "VIEW"));
        rows.add(row("table_name", "orders", "table_type", "MANAGED"));
        return rows;
    }

    private static InformationSchemaReader readerOver(FakeJdbc jdbc)
    {
        return new InformationSchemaReader(config(), jdbc::connection);
    }

    /** An unpinned config, {@code schema} unset, as an unpinned connector builds. */
    private static ConnectionConfig unpinnedConfig()
    {
        return ConnectionConfig.builder()
                .workspaceHostname("dbc-a1b2345c-d6e7.cloud.databricks.com")
                .httpPath("/sql/1.0/warehouses/a1b234c567d8e9fa")
                .catalog(CATALOG)
                .credentialSecretArn(
                        "arn:aws:secretsmanager:us-east-1:111122223333:secret:dbx-AbCdEf")
                .build();
    }

    // ── listSchemas: the one catalog-scoped read ─────────────────────────────

    @Test
    void listSchemasReturnsTheCatalogsSchemasAndBindsTheCatalog()
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> sql.contains(".schemata")
                ? Arrays.asList(row("schema_name", "coa_dbx_test"),
                        row("schema_name", "coa_dbx_test_other"),
                        row("schema_name", "default"))
                : Collections.emptyList());

        List<String> schemas = readerOver(jdbc).listSchemas();

        assertEquals(Arrays.asList("coa_dbx_test", "coa_dbx_test_other", "default"), schemas);
        List<FakeJdbc.Statement> reads = jdbc.statementsContaining(".schemata");
        assertEquals(1, reads.size(), "one statement, not one per schema");
        assertEquals(Collections.singletonList(CATALOG), reads.get(0).parameters(),
                "the catalog is bound, not interpolated");
    }

    @Test
    void listSchemasWorksOnAnUnpinnedConfig()
    {
        // Every other read here dereferences config.schema(), which is null when DATABRICKS_SCHEMA is
        // unset. If this starts touching the schema, an unpinned connector NPEs on ListSchemaNames, its
        // first call.
        FakeJdbc jdbc = new FakeJdbc(sql -> sql.contains(".schemata")
                ? Collections.singletonList(row("schema_name", "sales"))
                : Collections.emptyList());

        List<String> schemas =
                new InformationSchemaReader(unpinnedConfig(), jdbc::connection).listSchemas();

        assertEquals(Collections.singletonList("sales"), schemas);
    }

    @Test
    void listSchemasExcludesInformationSchemaInTheStatement()
    {
        // Without it an unpinned connector advertises information_schema as a queryable schema and COA
        // discovers the metastore views as tables.
        FakeJdbc jdbc = new FakeJdbc(sql -> Collections.emptyList());

        readerOver(jdbc).listSchemas();

        assertTrue(jdbc.statementsContaining(".schemata").get(0).sql()
                        .contains("schema_name <> 'information_schema'"),
                jdbc.statementsContaining(".schemata").get(0).sql());
    }

    @Test
    void listSchemasSkipsNullAndBlankNamesRatherThanAdvertisingThem()
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> sql.contains(".schemata")
                ? Arrays.asList(row("schema_name", "sales"), row("schema_name", "  "))
                : Collections.emptyList());

        assertEquals(Collections.singletonList("sales"), readerOver(jdbc).listSchemas());
    }

    @Test
    void listSchemasReturnsEmptyRatherThanFailingWhenTheCredentialSeesNothing()
    {
        // A warning rather than an exception: ListSchemaNames returning nothing is a legitimate answer for
        // a catalog whose schemas are all ungranted.
        FakeJdbc jdbc = new FakeJdbc(sql -> Collections.emptyList());

        assertEquals(Collections.emptyList(), readerOver(jdbc).listSchemas());
    }

    @Test
    void listTablesTakesItsNameSetFromShowTablesAndItsTypesFromInformationSchema()
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> {
            if (sql.startsWith("SHOW TABLES")) {
                return showTablesRows();
            }
            if (sql.contains(".tables")) {
                return tableTypeRows();
            }
            return Collections.emptyList();
        });

        List<String> tables = readerOver(jdbc).listTables();

        // The four internal side tables are gone. They appear in information_schema.tables as ordinary
        // MANAGED tables with no column distinguishing them, so SHOW TABLES is the only thing that
        // excludes them.
        assertEquals(Arrays.asList("coa_dbx_test_audit", "mixedcasetable", "order_counts_mv",
                "order_lines", "order_stream_st", "order_totals_vw", "orders"), tables);
        for (String name : tables) {
            assertTrue(!name.startsWith("__materialization"), name);
            assertTrue(!name.startsWith("event_log_"), name);
        }
    }

    @Test
    void listTablesIncludesViewsMaterializedViewsAndStreamingTables()
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> sql.startsWith("SHOW TABLES")
                ? showTablesRows()
                : sql.contains(".tables") ? tableTypeRows() : Collections.emptyList());
        List<String> tables = readerOver(jdbc).listTables();
        assertTrue(tables.contains("order_totals_vw"), "a VIEW must be exposed");
        assertTrue(tables.contains("order_counts_mv"), "a MATERIALIZED_VIEW must be exposed");
        assertTrue(tables.contains("order_stream_st"), "a STREAMING_TABLE must be exposed");
    }

    @Test
    void listTablesExcludesForeignTablesAndShallowClones()
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> {
            if (sql.startsWith("SHOW TABLES")) {
                List<Map<String, Object>> rows = showTablesRows();
                rows.add(row("database", SCHEMA, "tableName", "federated_pg", "isTemporary", "false"));
                rows.add(row("database", SCHEMA, "tableName", "orders_clone", "isTemporary", "false"));
                return rows;
            }
            if (sql.contains(".tables")) {
                List<Map<String, Object>> rows = tableTypeRows();
                rows.add(row("table_name", "federated_pg", "table_type", "FOREIGN"));
                rows.add(row("table_name", "orders_clone", "table_type", "MANAGED_SHALLOW_CLONE"));
                return rows;
            }
            return Collections.emptyList();
        });

        List<String> tables = readerOver(jdbc).listTables();
        assertTrue(!tables.contains("federated_pg"), "FOREIGN must be excluded: " + tables);
        assertTrue(!tables.contains("orders_clone"),
                "MANAGED_SHALLOW_CLONE must be excluded: " + tables);
    }

    @Test
    void listTablesExcludesATemporaryViewThatInformationSchemaDoesNotKnow()
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> {
            if (sql.startsWith("SHOW TABLES")) {
                List<Map<String, Object>> rows = showTablesRows();
                rows.add(row("database", "", "tableName", "tmp_scratch", "isTemporary", "true"));
                return rows;
            }
            return sql.contains(".tables") ? tableTypeRows() : Collections.emptyList();
        });
        assertTrue(!readerOver(jdbc).listTables().contains("tmp_scratch"));
    }

    @Test
    void listTablesBindsTheCatalogAndSchema()
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> sql.startsWith("SHOW TABLES")
                ? showTablesRows()
                : sql.contains(".tables") ? tableTypeRows() : Collections.emptyList());
        readerOver(jdbc).listTables();

        FakeJdbc.Statement typeQuery = jdbc.statementsContaining(".tables").get(0);
        assertEquals(Arrays.asList(CATALOG, SCHEMA), typeQuery.parameters());
    }

    @Test
    void describeTableReadsColumnsThenBothConstraintViews()
    {
        FakeJdbc jdbc = new FakeJdbc(InformationSchemaReaderTest::ordersRows);
        CoaTable table = readerOver(jdbc).describeTable("orders");

        assertEquals(Arrays.asList("region_code", "order_num", "order_total", "CustomerName"),
                table.columnNames());
        assertEquals(1, jdbc.statementsContaining("SELECT table_type").size());
        assertEquals(1, jdbc.statementsContaining(".columns").size());
        assertEquals(1, jdbc.statementsContaining("'PRIMARY KEY'").size());
        assertEquals(1, jdbc.statementsContaining("'FOREIGN KEY'").size());
        // One connection, opened and closed. Holding one across a Lambda freeze leaves a warehouse session
        // nothing will reuse.
        assertEquals(1, jdbc.connectionsOpened());
        assertEquals(1, jdbc.connectionsClosed());
    }

    @Test
    void describeTableBindsCatalogSchemaAndTableOnEveryStatement()
    {
        FakeJdbc jdbc = new FakeJdbc(InformationSchemaReaderTest::ordersRows);
        readerOver(jdbc).describeTable("orders");
        for (FakeJdbc.Statement statement : jdbc.statements()) {
            assertEquals(Arrays.asList(CATALOG, SCHEMA, "orders"), statement.parameters(),
                    statement.sql());
        }
    }

    @Test
    void aCompositePrimaryKeyTagsEveryMemberInKeyOrder()
    {
        FakeJdbc jdbc = new FakeJdbc(InformationSchemaReaderTest::ordersRows);
        Map<String, String> comments = readerOver(jdbc).describeTable("orders")
                .toTableSchema().toArrowSchema().getCustomMetadata();

        assertEquals("Region code. Parent key part 1. @pk @notnull", comments.get("region_code"));
        assertEquals("Order number within region. Parent key part 2. @pk @notnull",
                comments.get("order_num"));
        assertEquals("Order total in account currency", comments.get("order_total"));
    }

    @Test
    void nullabilityTravelsAsANotNullTagAndOnlyWhenTheCatalogSaidSo()
    {
        // All three states are in the fixture: NO earns a tag, YES earns none — absence is what COA reads as
        // "nobody said" — and an unrecognised spelling earns none either, since guessing would assert a
        // constraint Unity Catalog never declared.
        FakeJdbc jdbc = new FakeJdbc(InformationSchemaReaderTest::ordersRows);
        Map<String, String> comments = readerOver(jdbc).describeTable("orders")
                .toTableSchema().toArrowSchema().getCustomMetadata();

        assertTrue(comments.get("region_code").contains("@notnull"), comments.get("region_code"));
        assertFalse(comments.get("order_total").contains("@notnull"), comments.get("order_total"));
        assertNull(comments.get("CustomerName"),
                "an unrecognised is_nullable must produce no comment at all for a column with no prose,"
                        + " rather than a bare tag: " + comments.get("CustomerName"));
    }

    @Test
    void aCompositeForeignKeyPairsEachChildColumnWithItsOwnParent()
    {
        // The test the naive constraint join fails. The child columns are named differently from their
        // parents, in an order where alphabetical sorting gives the wrong pairing, so a cartesian product
        // or an ordinal mis-sort shows up rather than coming out coincidentally right.
        FakeJdbc jdbc = new FakeJdbc(InformationSchemaReaderTest::orderLinesRows);
        Map<String, String> comments = readerOver(jdbc).describeTable("order_lines")
                .toTableSchema().toArrowSchema().getCustomMetadata();

        assertEquals("Parent region. @fk(orders.region_code)", comments.get("order_region"));
        assertEquals("Parent order number. @fk(orders.order_num)", comments.get("order_id"));
        assertEquals("Line surrogate key. @pk", comments.get("line_id"));
    }

    @Test
    void aForeignKeyPointingOutOfTheExposedSchemaIsDroppedRatherThanEmitted()
    {
        // The @fk(table.column) tag has no slot for a schema and COA resolves it inside the one schema
        // this connector exposes, so a tag for a parent in another schema is wrong whenever a table of the
        // same name exists here and dangling otherwise.
        FakeJdbc jdbc = new FakeJdbc(InformationSchemaReaderTest::crossSchemaChildRows);
        DeclaredKeys keys = readerOver(jdbc).readDeclaredKeys("cross_schema_child");

        assertEquals(Collections.emptyList(), keys.foreignKeysFor("parent_id"),
                "a parent in coa_dbx_test_other must not produce a tag");
        // The good half of the same table still does, so this is a drop rather than a bail-out.
        assertEquals("orders.order_num", keys.foreignKeysFor("order_num").get(0).toString());
        assertEquals(1, keys.foreignKeyColumnCount());
    }

    @Test
    void aForeignKeyOntoAnExcludedTableTypeIsDropped()
    {
        // A parent this connector does not expose is absent from the ontology, so a tag naming it could
        // only dangle.
        FakeJdbc jdbc = new FakeJdbc(sql -> {
            if (sql.contains(".columns")) {
                return Collections.singletonList(row(
                        "column_name", "federated_id", "full_data_type", "bigint",
                        "is_nullable", "YES", "comment", null));
            }
            if (sql.contains("'FOREIGN KEY'")) {
                return Collections.singletonList(row(
                        "child_column", "federated_id",
                        "parent_catalog", CATALOG,
                        "parent_schema", SCHEMA,
                        "parent_table", "federated_pg",
                        "parent_column", "id",
                        "parent_table_type", "FOREIGN"));
            }
            return Collections.emptyList();
        });
        assertEquals(0, readerOver(jdbc).readDeclaredKeys("child").foreignKeyColumnCount());
    }

    @Test
    void aForeignKeyOntoAParentThatIsNotVisibleIsDropped()
    {
        // parent_table_type null means the LEFT JOIN found no row: the parent was dropped between the two
        // reads, or the principal cannot see it, which is likelier since information_schema is
        // privilege-filtered.
        FakeJdbc jdbc = new FakeJdbc(sql -> {
            if (sql.contains(".columns")) {
                return Collections.singletonList(row(
                        "column_name", "parent_id", "full_data_type", "bigint", "is_nullable", "YES", "comment", null));
            }
            if (sql.contains("'FOREIGN KEY'")) {
                return Collections.singletonList(row(
                        "child_column", "parent_id",
                        "parent_catalog", CATALOG,
                        "parent_schema", SCHEMA,
                        "parent_table", "invisible_parent",
                        "parent_column", "id",
                        "parent_table_type", null));
            }
            return Collections.emptyList();
        });
        assertEquals(0, readerOver(jdbc).readDeclaredKeys("child").foreignKeyColumnCount());
    }

    @Test
    void aMalformedCustomerCommentDoesNotFailTheDescribe()
    {
        // End to end through the reader. strip() keeps an unterminated @fk( and the toolkit's encoder
        // refuses it by throwing, and IllegalArgumentException is not an SQLException, so without
        // neutralise() this one comment fails the table's DESCRIBE permanently and unclassified.
        FakeJdbc jdbc = new FakeJdbc(sql -> sql.contains(".columns")
                ? Collections.singletonList(row("column_name", "note", "full_data_type", "string", "is_nullable", "YES",
                        "comment", "Line total @fk(orders.order_id"))
                : Collections.emptyList());

        CoaTable table = readerOver(jdbc).describeTable("malformed_comments");
        assertEquals("Line total _@fk(orders.order_id",
                table.toTableSchema().toArrowSchema().getCustomMetadata().get("note"));
    }

    @Test
    void aCustomerAuthoredTagIsStrippedFromTheForwardedComment()
    {
        FakeJdbc jdbc = new FakeJdbc(InformationSchemaReaderTest::orderLinesRows);
        Map<String, String> comments = readerOver(jdbc).describeTable("order_lines")
                .toTableSchema().toArrowSchema().getCustomMetadata();

        // The live fixture's own comment: the email must not mint a primary key, and the hand-written @pk
        // must not survive into the channel.
        assertEquals("Contact bob@pk.example.com about this column", comments.get("sku"));
    }

    @Test
    void declaredKeysCanBeReadOnTheirOwn()
    {
        FakeJdbc jdbc = new FakeJdbc(InformationSchemaReaderTest::orderLinesRows);
        DeclaredKeys keys = readerOver(jdbc).readDeclaredKeys("order_lines");

        assertEquals(Collections.singletonList("line_id"), new ArrayList<>(keys.primaryKeyColumns()));
        assertEquals(2, keys.foreignKeyColumnCount());
        assertEquals("orders.region_code", keys.foreignKeysFor("order_region").get(0).toString());
        assertEquals("orders.order_num", keys.foreignKeysFor("order_id").get(0).toString());
    }

    @Test
    void aTableWithNoConstraintsGetsNoTags()
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> sql.contains(".columns")
                ? Collections.singletonList(
                        row("column_name", "value", "full_data_type", "string",
                                "is_nullable", "YES", "comment", "Just prose"))
                : Collections.emptyList());
        Map<String, String> comments = readerOver(jdbc).describeTable("mixedcasetable")
                .toTableSchema().toArrowSchema().getCustomMetadata();
        assertEquals("Just prose", comments.get("value"));
    }

    @Test
    void anUnknownTableIsAnErrorNamingIt()
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> Collections.emptyList());
        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> readerOver(jdbc).describeTable("ghost"));
        assertTrue(failure.getMessage().contains("ghost"), failure.getMessage());
    }

    @Test
    void describeTableRefusesATableTypeThisConnectorDoesNotExpose()
    {
        // Athena calls GetTable for a name the USER typed, not only for names ListTables returned, so
        // without this check SELECT * FROM cat.sales.orders_clone against a MANAGED_SHALLOW_CLONE
        // describes and reads perfectly. isReferenceable also drops a foreign key on the premise that an
        // excluded type is absent from the ontology, and a describable, readable table is present.
        FakeJdbc jdbc = new FakeJdbc(sql -> {
            if (sql.contains("SELECT table_type")) {
                return Collections.singletonList(row("table_type", "MANAGED_SHALLOW_CLONE"));
            }
            if (sql.contains(".columns")) {
                return Collections.singletonList(row(
                        "column_name", "id", "full_data_type", "bigint", "is_nullable", "YES", "comment", null));
            }
            return Collections.emptyList();
        });

        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> readerOver(jdbc).describeTable("orders_clone"));
        assertTrue(failure.getMessage().contains("orders_clone"), failure.getMessage());
        assertTrue(failure.getMessage().contains("MANAGED_SHALLOW_CLONE"), failure.getMessage());
        // Stops before reading columns, so an excluded table costs one small statement.
        assertEquals(0, jdbc.statementsContaining(".columns").size());
    }

    @Test
    void describeTableAllowsEveryTypeListTablesExposes()
    {
        for (String tableType : TableTypes.allowed()) {
            FakeJdbc jdbc = new FakeJdbc(sql -> {
                if (sql.contains("SELECT table_type")) {
                    return Collections.singletonList(row("table_type", tableType));
                }
                if (sql.contains(".columns")) {
                    return Collections.singletonList(row(
                            "column_name", "id", "full_data_type", "bigint", "is_nullable", "YES", "comment", null));
                }
                return Collections.emptyList();
            });
            assertEquals(Collections.singletonList("id"),
                    readerOver(jdbc).describeTable("t").columnNames(), tableType);
        }
    }

    @Test
    void describeTableLetsAnUnknownTableFallThroughToTheBetterMessage()
    {
        // No row in information_schema.tables means the table does not exist here, or the principal cannot
        // see it. The columns read produces the message that says so and names the grants, so the type
        // check does not pre-empt it.
        FakeJdbc jdbc = new FakeJdbc(sql -> Collections.emptyList());
        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> readerOver(jdbc).describeTable("ghost"));
        assertTrue(failure.getMessage().contains("information_schema.columns"), failure.getMessage());
    }

    @Test
    void listTablesReportsAnEmptyResultRatherThanReturningOneSilently()
    {
        // Skipping ONE unmatched row unlogged is right; skipping every one is not. A steward would see a
        // source that scanned successfully and contains nothing.
        FakeJdbc jdbc = new FakeJdbc(sql -> {
            if (sql.startsWith("SHOW TABLES")) {
                return showTablesRows();
            }
            if (sql.contains(".tables")) {
                // Every object excluded: a schema of nothing but shallow clones.
                List<Map<String, Object>> rows = new ArrayList<>();
                for (Map<String, Object> shown : showTablesRows()) {
                    rows.add(row("table_name", shown.get("tableName"),
                            "table_type", "MANAGED_SHALLOW_CLONE"));
                }
                return rows;
            }
            return Collections.emptyList();
        });
        assertEquals(Collections.emptyList(), readerOver(jdbc).listTables());
    }

    @Test
    void showTablesNeverContributesANullName()
    {
        // A null name matches nothing in the type map, takes the skip branch, and turns into an empty
        // schema with no error.
        FakeJdbc jdbc = new FakeJdbc(sql -> {
            if (sql.startsWith("SHOW TABLES")) {
                return Arrays.asList(
                        row("database", SCHEMA, "tableName", null, "isTemporary", "false"),
                        row("database", SCHEMA, "tableName", "   ", "isTemporary", "false"),
                        row("database", SCHEMA, "tableName", "orders", "isTemporary", "false"));
            }
            if (sql.contains(".tables")) {
                return Collections.singletonList(row("table_name", "orders", "table_type", "MANAGED"));
            }
            return Collections.emptyList();
        });
        assertEquals(Collections.singletonList("orders"), readerOver(jdbc).listTables());
    }

    @Test
    void refusesABlankTableName()
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> Collections.emptyList());
        assertThrows(IllegalArgumentException.class, () -> readerOver(jdbc).describeTable("  "));
        assertThrows(IllegalArgumentException.class, () -> readerOver(jdbc).describeTable(null));
    }

    @Test
    void refusesANegativeQueryTimeout()
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> Collections.emptyList());
        assertThrows(IllegalArgumentException.class,
                () -> new InformationSchemaReader(config(), jdbc::connection, -1));
    }

    @Test
    void aConnectionIsClosedEvenWhenAStatementFails()
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> {
            throw new IllegalStateException("boom");
        });
        Connection ignored;
        assertThrows(RuntimeException.class, () -> readerOver(jdbc).listTables());
        assertEquals(jdbc.connectionsOpened(), jdbc.connectionsClosed(),
                "every opened connection must be closed");
    }

    /**
     * One row of the foreign-key statement, with the parent inside the exposed schema and a
     * {@code table_type} the connector exposes, so a reference that should be emitted.
     */
    private static Map<String, Object> foreignKeyRow(String childColumn, String parentTable,
                                                     String parentColumn)
    {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("child_column", childColumn);
        row.put("parent_catalog", CATALOG);
        row.put("parent_schema", SCHEMA);
        row.put("parent_table", parentTable);
        row.put("parent_column", parentColumn);
        row.put("parent_table_type", "MANAGED");
        return row;
    }

    /**
     * {@code cross_schema_child}: one foreign key out of the exposed schema, which must be dropped,
     * and one inside it, which must survive.
     */
    private static List<Map<String, Object>> crossSchemaChildRows(String sql)
    {
        if (sql.contains("SELECT table_type")) {
            return Collections.singletonList(row("table_type", "MANAGED"));
        }
        if (sql.contains(".columns")) {
            return Arrays.asList(
                    row("column_name", "child_id", "full_data_type", "bigint", "is_nullable", "YES",
                            "comment", "Child surrogate key."),
                    row("column_name", "parent_id", "full_data_type", "bigint", "is_nullable", "YES",
                            "comment", "References a parent in another schema."),
                    row("column_name", "order_num", "full_data_type", "bigint", "is_nullable", "YES",
                            "comment", "Also references orders, in this schema."));
        }
        if (sql.contains("'PRIMARY KEY'")) {
            return Collections.singletonList(row("column_name", "child_id"));
        }
        if (sql.contains("'FOREIGN KEY'")) {
            Map<String, Object> outward = new LinkedHashMap<>();
            outward.put("child_column", "parent_id");
            outward.put("parent_catalog", CATALOG);
            outward.put("parent_schema", "coa_dbx_test_other");
            outward.put("parent_table", "external_parent");
            outward.put("parent_column", "parent_id");
            outward.put("parent_table_type", null);
            return Arrays.asList(outward, foreignKeyRow("order_num", "orders", "order_num"));
        }
        return Collections.emptyList();
    }

    /** {@code orders}: mixed-case column, decimal, composite primary key, no foreign key. */
    private static List<Map<String, Object>> ordersRows(String sql)
    {
        if (sql.contains("SELECT table_type")) {
            return Collections.singletonList(row("table_type", "MANAGED"));
        }
        if (sql.contains(".columns")) {
            return Arrays.asList(
                    // The key columns are NOT NULL as a declared primary key's are, the total is nullable,
                    // and CustomerName's is_nullable is a spelling Databricks does not use.
                    row("column_name", "region_code", "full_data_type", "string", "is_nullable", "NO",
                            "comment", "Region code. Parent key part 1."),
                    row("column_name", "order_num", "full_data_type", "bigint", "is_nullable", "NO",
                            "comment", "Order number within region. Parent key part 2."),
                    row("column_name", "order_total", "full_data_type", "decimal(10,2)", "is_nullable", "YES",
                            "comment", "Order total in account currency"),
                    row("column_name", "CustomerName", "full_data_type", "string", "is_nullable", "true",
                            "comment", null));
        }
        if (sql.contains("'PRIMARY KEY'")) {
            return Arrays.asList(
                    row("column_name", "region_code"),
                    row("column_name", "order_num"));
        }
        return Collections.emptyList();
    }

    /** {@code order_lines}: single-column primary key, two-column foreign key, tagged comment. */
    private static List<Map<String, Object>> orderLinesRows(String sql)
    {
        if (sql.contains("SELECT table_type")) {
            return Collections.singletonList(row("table_type", "MANAGED"));
        }
        if (sql.contains(".columns")) {
            return Arrays.asList(
                    row("column_name", "line_id", "full_data_type", "bigint", "is_nullable", "YES",
                            "comment", "Line surrogate key."),
                    row("column_name", "order_region", "full_data_type", "string", "is_nullable", "YES",
                            "comment", "Parent region."),
                    row("column_name", "order_id", "full_data_type", "bigint", "is_nullable", "YES",
                            "comment", "Parent order number."),
                    row("column_name", "sku", "full_data_type", "string", "is_nullable", "YES",
                            "comment", "Contact bob@pk.example.com about this @pk column"));
        }
        if (sql.contains("'PRIMARY KEY'")) {
            return Collections.singletonList(row("column_name", "line_id"));
        }
        if (sql.contains("'FOREIGN KEY'")) {
            // What the ANSI join returns, in child-ordinal order. The naive constraint_column_usage join
            // returns four rows here, two of them pairing order_id with region_code and order_region with
            // order_num.
            List<Map<String, Object>> rows = new ArrayList<>();
            rows.add(foreignKeyRow("order_region", "orders", "region_code"));
            rows.add(foreignKeyRow("order_id", "orders", "order_num"));
            return rows;
        }
        return Collections.emptyList();
    }
}
