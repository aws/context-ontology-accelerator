// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.connector.metadata;

import dev.coa.connector.constraints.ColumnComment;
import org.apache.arrow.vector.types.Types;
import org.apache.arrow.vector.types.pojo.ArrowType;
import org.apache.arrow.vector.types.pojo.Field;
import org.apache.arrow.vector.types.pojo.FieldType;
import org.apache.arrow.vector.types.pojo.Schema;
import org.junit.jupiter.api.Test;

import java.util.Collections;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Pins the translation a connector author never has to write: key intent declared on a column,
 * out the other end as a comment, in the place Athena reads it.
 *
 * <p>Asserts on the finished Arrow schema rather than on intermediate strings, because that is what
 * crosses the wire — and it is the only thing that can be wrong in a way nothing else notices.
 */
class CoaTableTest
{
    private static final ArrowType BIGINT = Types.MinorType.BIGINT.getType();
    private static final ArrowType VARCHAR = Types.MinorType.VARCHAR.getType();

    private static Map<String, String> commentsOf(CoaTable table)
    {
        return table.toTableSchema().toArrowSchema().getCustomMetadata();
    }

    @Test
    void aSingleColumnPrimaryKeyBecomesOnePkTag()
    {
        CoaTable orders = CoaTable.named("orders")
                .column(CoaColumn.of("order_id", BIGINT)
                        .describedAs("Surrogate key for the order")
                        .primaryKey())
                .build();
        assertEquals("Surrogate key for the order @pk", commentsOf(orders).get("order_id"));
    }

    @Test
    void aForeignKeyBecomesAnFkTagNamingOnlyTheParent()
    {
        // The child column is never named in the tag: the tag lives in that column's own comment.
        CoaTable orders = CoaTable.named("orders")
                .column(CoaColumn.of("customer_id", BIGINT)
                        .describedAs("Customer that placed the order")
                        .foreignKey("customers", "customer_id"))
                .build();
        assertEquals("Customer that placed the order @fk(customers.customer_id)",
                commentsOf(orders).get("customer_id"));
    }

    @Test
    void aColumnCanBeBothAPrimaryAndAForeignKey()
    {
        CoaTable lines = CoaTable.named("order_lines")
                .column(CoaColumn.of("order_id", BIGINT)
                        .describedAs("Order this line belongs to")
                        .primaryKey()
                        .foreignKey("orders", "order_id"))
                .build();
        assertEquals("Order this line belongs to @pk @fk(orders.order_id)",
                commentsOf(lines).get("order_id"));
    }

    @Test
    void aCompositePrimaryKeyIsOneTagPerColumn_inColumnOrder()
    {
        // There is no ordinal anywhere: COA reads the key's column order from the table's column
        // order, so declaration order is the key's order.
        CoaTable lines = CoaTable.named("order_lines")
                .column(CoaColumn.of("order_id", BIGINT).primaryKey())
                .column(CoaColumn.of("line_no", BIGINT).primaryKey())
                .column(CoaColumn.of("sku", VARCHAR).describedAs("Stock keeping unit"))
                .build();
        Map<String, String> comments = commentsOf(lines);
        assertEquals("@pk", comments.get("order_id"));
        assertEquals("@pk", comments.get("line_no"));
        assertEquals(List.of("order_id", "line_no", "sku"), lines.columnNames());
    }

    @Test
    void aCompositeForeignKeyIsOneTagPerChildColumn()
    {
        CoaTable shipments = CoaTable.named("shipment_lines")
                .column(CoaColumn.of("order_id", BIGINT).foreignKey("order_lines", "order_id"))
                .column(CoaColumn.of("line_no", BIGINT).foreignKey("order_lines", "line_no"))
                .build();
        Map<String, String> comments = commentsOf(shipments);
        assertEquals("@fk(order_lines.order_id)", comments.get("order_id"));
        assertEquals("@fk(order_lines.line_no)", comments.get("line_no"));
    }

    @Test
    void quotingIsAppliedToAParentThatNeedsIt()
    {
        // The author passes identifiers as the source spells them and never pre-quotes.
        CoaTable table = CoaTable.named("t")
                .column(CoaColumn.of("c", BIGINT).foreignKey("my orders", "customer id"))
                .build();
        assertEquals("@fk(\"my orders\".\"customer id\")", commentsOf(table).get("c"));
    }

    @Test
    void aColumnWithNothingToSayGetsNoComment()
    {
        CoaTable table = CoaTable.named("t").column("bare", BIGINT).build();
        Schema schema = table.toTableSchema().toArrowSchema();
        assertTrue(schema.getCustomMetadata().isEmpty());
        assertEquals("bare", schema.getFields().get(0).getName());
    }

    @Test
    void commentsLandInSchemaMetadataAndNotOnTheArrowFields()
    {
        // The placement rule TableSchema owns, asserted through this path too: a comment on a field
        // reaches Athena and is ignored, so getting it wrong here would be invisible.
        CoaTable table = CoaTable.named("t")
                .column(CoaColumn.of("id", BIGINT).describedAs("Key").primaryKey())
                .build();
        Schema schema = table.toTableSchema().toArrowSchema();
        assertEquals("Key @pk", schema.getCustomMetadata().get("id"));
        for (Field field : schema.getFields()) {
            assertTrue(field.getMetadata().isEmpty(), field.getName() + " carries field metadata");
        }
    }


    @Test
    void proseAlreadyContainingALiveTagIsRefused()
    {
        // Refused at the author's own call, not at render time. COA would strip such a tag and
        // declare a key nobody asked for.
        assertThrows(IllegalArgumentException.class,
                () -> CoaColumn.of("c", BIGINT).describedAs("Owner @pk"));
        // Prose that merely resembles a tag is fine, and both sides leave it alone.
        assertEquals("see @PK and bob@pk.example.com",
                commentsOf(CoaTable.named("t")
                        .column(CoaColumn.of("c", BIGINT).describedAs("see @PK and bob@pk.example.com"))
                        .build()).get("c"));
    }

    @Test
    void aDuplicateColumnNameIsRefused()
    {
        assertThrows(IllegalArgumentException.class,
                () -> CoaTable.named("t")
                        .column(CoaColumn.of("c", BIGINT))
                        .column(CoaColumn.of("c", VARCHAR)));
    }

    @Test
    void aTableWithNoColumnsIsRefused()
    {
        assertThrows(IllegalStateException.class, () -> CoaTable.named("empty").build());
    }

    @Test
    void aRepeatedForeignKeyTargetCollapses()
    {
        // COA dedups by resolved target, so two identical declarations must not emit two tags.
        CoaTable table = CoaTable.named("t")
                .column(CoaColumn.of("c", BIGINT)
                        .foreignKey("orders", "order_id")
                        .foreignKey(ColumnComment.Reference.of("analytics", "orders", "order_id")))
                .build();
        assertEquals("@fk(orders.order_id)", commentsOf(table).get("c"));
    }


    @Test
    void mutatingAColumnAfterTheTableIsBuiltCannotChangeTheTable()
    {
        // The reference connector caches its tables in a static field, so they outlive a request.
        // Without the snapshot, one invocation editing a column changed what every later
        // invocation saw — declared keys quietly lost, with no error anywhere.
        CoaColumn column = CoaColumn.of("order_id", BIGINT).describedAs("Order").primaryKey();
        CoaTable table = CoaTable.named("order_lines").column(column).build();

        column.describedAs("something else").foreignKey("orders", "order_id");
        for (CoaColumn handedBack : table.columns()) {
            handedBack.describedAs("and again");
        }

        assertEquals("Order @pk", commentsOf(table).get("order_id"));
    }

    @Test
    void aNotNullColumnBecomesANotNullTagAndOneWithNothingSaidGetsNone()
    {
        CoaTable orders = CoaTable.named("orders")
                .column(CoaColumn.of("order_id", BIGINT)
                        .describedAs("Surrogate key for the order")
                        .primaryKey()
                        .notNull())
                .column(CoaColumn.of("note", VARCHAR).describedAs("Free text"))
                .build();

        assertEquals("Surrogate key for the order @pk @notnull",
                commentsOf(orders).get("order_id"));
        // Absence is how COA reads "nobody said", which keeps this additive for connectors already deployed.
        assertEquals("Free text", commentsOf(orders).get("note"));
    }

    @Test
    void notNullSurvivesTheSnapshotTheTableTakes()
    {
        // A new field on CoaColumn that copy() forgets is invisible in every other test here and shows up as
        // a tag that goes missing under exactly the caching the snapshot exists for.
        CoaColumn column = CoaColumn.of("order_id", BIGINT).describedAs("Order").notNull();
        CoaTable table = CoaTable.named("order_lines").column(column).build();

        assertTrue(table.columns().get(0).isNotNull());
        assertEquals("Order @notnull", commentsOf(table).get("order_id"));
    }
}
