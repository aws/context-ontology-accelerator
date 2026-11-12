// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.connector.schema;

import dev.coa.connector.constraints.ColumnComment;
import org.apache.arrow.vector.types.Types;
import org.apache.arrow.vector.types.pojo.ArrowType;
import org.apache.arrow.vector.types.pojo.Field;
import org.apache.arrow.vector.types.pojo.Schema;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** Pins where a column comment lands in the Arrow schema, and every way the builder refuses. */
class TableSchemaTest
{
    private static final ArrowType BIGINT = Types.MinorType.BIGINT.getType();
    private static final ArrowType VARCHAR = Types.MinorType.VARCHAR.getType();

    private static TableSchema orders()
    {
        return TableSchema.named("orders")
                .column("order_id", BIGINT, ColumnComment.of("Surrogate key for the order").primaryKey())
                .column("customer_id", BIGINT, ColumnComment.of("Customer that placed the order")
                        .foreignKey("customers", "customer_id"))
                .column("total_amount", Types.MinorType.FLOAT8.getType(),
                        ColumnComment.of("Order total in account currency"))
                .build();
    }

    @Test
    void commentsGoInSchemaMetadataKeyedByColumnName()
    {
        // The whole reason this class exists. Athena reads the schema's metadata map; the
        // identical data on an Arrow field is serialised, delivered, and ignored.
        Map<String, String> metadata = orders().toArrowSchema().getCustomMetadata();
        assertEquals("Surrogate key for the order @pk", metadata.get("order_id"));
        assertEquals("Customer that placed the order @fk(customers.customer_id)",
                metadata.get("customer_id"));
        assertEquals("Order total in account currency", metadata.get("total_amount"));
    }

    @Test
    void noArrowFieldCarriesMetadata()
    {
        for (Field field : orders().toArrowSchema().getFields()) {
            assertTrue(field.getMetadata().isEmpty(),
                    field.getName() + " carries field metadata, which Athena ignores");
        }
    }

    @Test
    void declarationOrderIsPreserved()
    {
        // Column order is DESCRIBE order, which is the order a composite primary key is read
        // in — so this is load-bearing, not cosmetic.
        assertEquals(List.of("order_id", "customer_id", "total_amount"), orders().columnNames());
        Schema schema = orders().toArrowSchema();
        assertEquals("order_id", schema.getFields().get(0).getName());
        assertEquals("total_amount", schema.getFields().get(2).getName());
    }

    @Test
    void columnNamesAreTheOnlyKeysInTheSchemaMetadata()
    {
        // Nothing but a column name may claim a key here — in particular not the table-level
        // "comment" slot, which is deliberately never written because no Athena read path surfaces it.
        // Asserted as a subset: not every column carries a comment.
        assertTrue(
                orders().columnNames().containsAll(orders().toArrowSchema().getCustomMetadata().keySet()),
                "schema metadata carried a key that is not a column name: "
                        + orders().toArrowSchema().getCustomMetadata().keySet());
    }

    @Test
    void aColumnNamedCommentIsAnOrdinaryColumn()
    {
        // The table-level comment slot is unused, so nothing collides with this column's comment.
        TableSchema tickets = TableSchema.named("tickets")
                .column("id", BIGINT, ColumnComment.of("Surrogate key").primaryKey())
                .column("comment", VARCHAR, ColumnComment.of("Free text left by the user"))
                .build();
        Map<String, String> metadata = tickets.toArrowSchema().getCustomMetadata();
        assertEquals("Free text left by the user", metadata.get("comment"));
        assertEquals("Surrogate key @pk", metadata.get("id"));
        assertEquals(2, metadata.size());
    }

    @Test
    void aColumnMayHaveNoComment()
    {
        TableSchema schema = TableSchema.named("t").column("bare", BIGINT).build();
        assertNull(schema.comment("bare"));
        assertTrue(schema.toArrowSchema().getCustomMetadata().isEmpty());
    }

    @Test
    void anEmptyCommentIsNotWritten()
    {
        TableSchema schema = TableSchema.named("t")
                .column("bare", BIGINT, ColumnComment.of(""))
                .build();
        assertNull(schema.comment("bare"));
        assertTrue(schema.toArrowSchema().getCustomMetadata().isEmpty());
    }

    @Test
    void commentOfAnUnknownColumnIsNull()
    {
        assertNull(orders().comment("nope"));
    }

    @Test
    void aDuplicateColumnNameIsRefused()
    {
        IllegalArgumentException thrown = assertThrows(IllegalArgumentException.class,
                () -> TableSchema.named("orders")
                        .column("order_id", BIGINT, ColumnComment.of("First"))
                        .column("order_id", VARCHAR, ColumnComment.of("Second")));
        assertTrue(thrown.getMessage().contains("already has a column named order_id"), thrown.getMessage());
    }

    @Test
    void badArgumentsAreRefused()
    {
        assertThrows(IllegalArgumentException.class, () -> TableSchema.named(null));
        assertThrows(IllegalArgumentException.class, () -> TableSchema.named("  "));
        assertThrows(IllegalArgumentException.class, () -> TableSchema.named("t").column(null, BIGINT));
        assertThrows(IllegalArgumentException.class, () -> TableSchema.named("t").column("c", null));
    }

    @Test
    void aTableWithNoColumnsIsRefused()
    {
        IllegalStateException thrown = assertThrows(IllegalStateException.class,
                () -> TableSchema.named("empty").build());
        assertTrue(thrown.getMessage().contains("empty"), thrown.getMessage());
    }
}
