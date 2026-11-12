// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.example;

import dev.coa.connector.metadata.CoaTable;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** Pins the fixture: its constraint tags, where its comments live in the schema, and its rows. */
class ExampleCatalogTest
{
    /** The row half, from the catalog. */
    private static final Map<String, ExampleTable> TABLES = ExampleCatalog.tables(Collections.emptyMap());

    /** The shape half, declared in the handler — where a connector author reads it. */
    private static final Map<String, CoaTable> SHAPES = ExampleMetadataHandler.declareTables();

    @Test
    void everyTableIsListed()
    {
        List<String> expected =
                List.of("customers", "orders", "order_lines", "shipment_lines", "bulk_rows");
        assertEquals(expected, new ArrayList<>(SHAPES.keySet()));
        // Shape and rows are declared in different files, so a table present in one and missing
        // from the other is a real possibility. Pin them together.
        assertEquals(expected, new ArrayList<>(TABLES.keySet()));
    }

    @Test
    void singleColumnPrimaryKeysAreTagged()
    {
        assertEquals("Surrogate key for the customer @pk", comment("customers", "customer_id"));
        assertEquals("Surrogate key for the order @pk", comment("orders", "order_id"));
        assertEquals("Surrogate key for the shipped line @pk", comment("shipment_lines", "shipment_line_id"));
    }

    @Test
    void theCollidingColumnNameCarriesDifferentTagsPerTable()
    {
        // The whole reason comments are keyed per table: `customer_id` is a primary key in
        // one table and a foreign key in the other.
        assertEquals("Surrogate key for the customer @pk", comment("customers", "customer_id"));
        assertEquals("Customer that placed the order @fk(customers.customer_id)",
                comment("orders", "customer_id"));
    }

    @Test
    void compositePrimaryKeyIsOneTagPerMemberColumn()
    {
        assertEquals("Order this line belongs to @pk @fk(orders.order_id)", comment("order_lines", "order_id"));
        assertEquals("Position of this line within the order @pk", comment("order_lines", "line_no"));
        // The key's column order is DESCRIBE order, i.e. declaration order here.
        List<String> pkColumns = new ArrayList<>();
        for (String column : SHAPES.get("order_lines").columnNames()) {
            if (comment("order_lines", column).contains("@pk")) {
                pkColumns.add(column);
            }
        }
        assertEquals(List.of("order_id", "line_no"), pkColumns);
    }

    @Test
    void compositeForeignKeyIsOneTagPerChildColumn()
    {
        assertEquals("Order of the line being shipped @fk(order_lines.order_id)",
                comment("shipment_lines", "order_id"));
        assertEquals("Line number of the line being shipped @fk(order_lines.line_no)",
                comment("shipment_lines", "line_no"));
    }

    @Test
    void everyTaggedColumnReachesTheSchemaMetadataAthenaReads()
    {
        // TableSchemaTest pins the placement rule itself. This asserts the fixture actually
        // goes through it, for every column of every table — so a future refactor that
        // hand-rolls a Schema again is caught here rather than in Athena.
        for (ExampleTable table : TABLES.values()) {
            Map<String, String> metadata =
                    SHAPES.get(table.name()).toTableSchema().toArrowSchema().getCustomMetadata();
            for (String column : SHAPES.get(table.name()).columnNames()) {
                assertEquals(comment(table.name(), column), metadata.get(column),
                        table.name() + "." + column + " lost its comment");
            }
        }
    }

    @Test
    void everyRowSuppliesEveryColumn()
    {
        for (ExampleTable table : TABLES.values()) {
            assertTrue(table.rowCount() > 0, table.name() + " serves no rows");
            for (int rowNumber : new int[] {1, table.rowCount()}) {
                Map<String, Object> row = table.row(rowNumber);
                for (String column : SHAPES.get(table.name()).columnNames()) {
                    assertNotNull(row.get(column), table.name() + " row " + rowNumber + " is missing " + column);
                }
            }
        }
    }

    @Test
    void foreignKeyValuesResolveToRealParentRows()
    {
        // The fixture is a join fixture; a composite FK that points at rows which do not
        // exist would make every join return nothing and look like a tag bug.
        ExampleTable orderLines = TABLES.get("order_lines");
        ExampleTable shipmentLines = TABLES.get("shipment_lines");
        assertEquals(orderLines.rowCount(), shipmentLines.rowCount());
        for (int rowNumber = 1; rowNumber <= orderLines.rowCount(); rowNumber++) {
            Map<String, Object> parent = orderLines.row(rowNumber);
            Map<String, Object> child = shipmentLines.row(rowNumber);
            assertEquals(parent.get("order_id"), child.get("order_id"));
            assertEquals(parent.get("line_no"), child.get("line_no"));
            long orderId = (Long) parent.get("order_id");
            assertTrue(orderId >= 1001L && orderId <= 1000L + TABLES.get("orders").rowCount(),
                    "order_lines.order_id " + orderId + " is outside orders");
        }
    }

    @Test
    void bulkRowsDefaultsSmall()
    {
        ExampleTable bulk = TABLES.get("bulk_rows");
        assertEquals(ExampleCatalog.DEFAULT_BULK_ROWS, bulk.rowCount());
        assertEquals(ExampleCatalog.DEFAULT_BULK_ROW_BYTES, ((String) bulk.row(1).get("payload")).length());
    }

    @Test
    void bulkRowsIsSizedFromTheEnvironment()
    {
        Map<String, String> config = new HashMap<>();
        config.put(ExampleCatalog.BULK_ROWS_OPTION, "4096");
        config.put(ExampleCatalog.BULK_ROW_BYTES_OPTION, "2048");
        ExampleTable bulk = ExampleCatalog.tables(config).get("bulk_rows");
        assertEquals(4096, bulk.rowCount());
        assertEquals(2048, ((String) bulk.row(1).get("payload")).length());
    }

    @Test
    void bulkRowsAlsoAcceptsUpperCasedOptionNames()
    {
        Map<String, String> config = new HashMap<>();
        config.put(ExampleCatalog.BULK_ROWS_OPTION.toUpperCase(java.util.Locale.ROOT), "7");
        assertEquals(7, ExampleCatalog.tables(config).get("bulk_rows").rowCount());
    }

    @Test
    void anUnusableSizeFallsBackToTheDefault()
    {
        // A typo in a test knob must not fail Lambda initialisation and take the whole
        // connector down with it.
        for (String bad : new String[] {"", "  ", "lots", "0", "-5"}) {
            Map<String, String> config = new HashMap<>();
            config.put(ExampleCatalog.BULK_ROWS_OPTION, bad);
            assertEquals(ExampleCatalog.DEFAULT_BULK_ROWS,
                    ExampleCatalog.tables(config).get("bulk_rows").rowCount(),
                    "\"" + bad + "\" should have fallen back to the default");
        }
    }

    @Test
    void payloadValuesAreNotAllIdentical()
    {
        ExampleTable bulk = TABLES.get("bulk_rows");
        assertTrue(!bulk.row(1).get("payload").equals(bulk.row(2).get("payload")));
    }

    /**
     * The comment a column ends up with, through the whole chain the toolkit owns: intent declared
     * on a {@code CoaColumn}, rendered into {@code @pk} / {@code @fk} tags, and placed where Athena
     * reads it. Asserting on the finished string rather than on the intent is the point — it is what
     * COA parses.
     */
    private static String comment(String table, String column)
    {
        return SHAPES.get(table).toTableSchema().comment(column);
    }
}
