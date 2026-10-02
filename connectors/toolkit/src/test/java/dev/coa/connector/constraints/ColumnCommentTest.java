// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.connector.constraints;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Pins the exact strings {@link ColumnComment} emits.
 *
 * <p>These are assertions about a wire format, so they are written as literal expected
 * strings rather than round-trips through the encoder's own helpers. COA's parser is the other
 * side of that format, and nothing here can check the two agree: a change to these strings has to
 * be matched there by hand.
 */
class ColumnCommentTest
{
    @Test
    void proseOnlyIsUnchanged()
    {
        assertEquals("Primary contact email address",
                ColumnComment.of("Primary contact email address").build());
    }

    @Test
    void primaryKeyTagFollowsProse()
    {
        assertEquals("Surrogate key for the customer @pk",
                ColumnComment.of("Surrogate key for the customer").primaryKey().build());
    }

    @Test
    void foreignKeyOperandIsBracketed()
    {
        assertEquals("Customer that placed the order @fk(customers.customer_id)",
                ColumnComment.of("Customer that placed the order")
                        .foreignKey("customers", "customer_id")
                        .build());
    }

    @Test
    void primaryKeyPrecedesForeignKeyOnOneColumn()
    {
        assertEquals("Order this line belongs to @pk @fk(orders.order_id)",
                ColumnComment.of("Order this line belongs to")
                        .primaryKey()
                        .foreignKey("orders", "order_id")
                        .build());
    }

    @Test
    void notNullTagFollowsProse()
    {
        assertEquals("Customer's legal name @notnull",
                ColumnComment.of("Customer's legal name").notNull().build());
    }

    @Test
    void theTagOrderIsProseThenPrimaryKeyThenNotNullThenForeignKeys()
    {
        // The operand-free tags come first so the @fk list stays last and contiguous, and @notnull goes
        // AFTER @pk so a primary-key-only column's comment is byte-identical to what was emitted before
        // @notnull existed.
        assertEquals("Order this line belongs to @pk @notnull @fk(orders.order_id)",
                ColumnComment.of("Order this line belongs to")
                        .primaryKey()
                        .notNull()
                        .foreignKey("orders", "order_id")
                        .build());
        assertEquals("Order this line belongs to @pk @notnull @fk(orders.order_id)",
                ColumnComment.of("Order this line belongs to")
                        .foreignKey("orders", "order_id")
                        .notNull()
                        .primaryKey()
                        .build());
    }

    @Test
    void repeatedNotNullEmitsOneTag()
    {
        assertEquals("Key @notnull", ColumnComment.of("Key").notNull().notNull().build());
    }

    @Test
    void thereIsNoNullableTagBecauseAbsenceAlreadyMeansUnknown()
    {
        // Connectors deployed before the tag existed emit no tag and COA defaults a column to nullable, so
        // a spelling that made absence mean "nullable" would reinterpret their columns as asserted rather
        // than unstated.
        assertEquals("Customer's legal name",
                ColumnComment.of("Customer's legal name").build());
    }

    @Test
    void tagOnlyCommentHasNoLeadingSpace()
    {
        assertEquals("@pk", ColumnComment.of(null).primaryKey().build());
        assertEquals("@pk", ColumnComment.of("").primaryKey().build());
        assertEquals("@fk(orders.order_id)", ColumnComment.of("").foreignKey("orders", "order_id").build());
    }

    @Test
    void repeatedPrimaryKeyEmitsOneTag()
    {
        assertEquals("Key @pk", ColumnComment.of("Key").primaryKey().primaryKey().build());
    }

    @Test
    void twoForeignKeysOnOneColumnBothAppear()
    {
        // Legal: one column can reference two different parents. A COMPOSITE foreign key is
        // the other shape — one tag on each of several columns — and is covered in
        // ExampleCatalogTest against the fixture.
        assertEquals("Shared @fk(orders.order_id) @fk(invoices.order_id)",
                ColumnComment.of("Shared")
                        .foreignKey("orders", "order_id")
                        .foreignKey("invoices", "order_id")
                        .build());
    }

    @Test
    void duplicateTargetCollapsesEvenWhenSpelledDifferently()
    {
        // The parser dedups on the DECODED (table, column) pair, so extra qualification does
        // not make a second key. The encoder must match, or it emits a tag the parser drops.
        assertEquals("Owner @fk(customers.customer_id)",
                ColumnComment.of("Owner")
                        .foreignKey("customers", "customer_id")
                        .foreignKey(ColumnComment.Reference.of("analytics", "public", "customers", "customer_id"))
                        .build());
    }

    @Test
    void qualifiedReferenceKeepsItsQualification()
    {
        assertEquals("Owner @fk(analytics.public.customers.customer_id)",
                ColumnComment.of("Owner")
                        .foreignKey(ColumnComment.Reference.of("analytics", "public", "customers", "customer_id"))
                        .build());
    }

    @Test
    void segmentsAreQuotedOnlyWhenTheyNeedIt()
    {
        assertEquals("orders", ColumnComment.renderSegment("orders"));
        assertEquals("order_id", ColumnComment.renderSegment("order_id"));
        assertEquals("cost-centre$1", ColumnComment.renderSegment("cost-centre$1"));
        assertEquals("\"my orders\"", ColumnComment.renderSegment("my orders"));
        assertEquals("\"total (usd)\"", ColumnComment.renderSegment("total (usd)"));
        assertEquals("\"a.b\"", ColumnComment.renderSegment("a.b"));
    }

    @Test
    void embeddedQuotesAreDoubled()
    {
        assertEquals("\"say \"\"hi\"\" now\"", ColumnComment.renderSegment("say \"hi\" now"));
        // A name ending in a quote produces a three-quote run. It is legal and the parser
        // handles it; only prose documentation struggles to show it.
        assertEquals("\"he said \"\"hi\"\"\"", ColumnComment.renderSegment("he said \"hi\""));
    }

    @Test
    void mixedQuotingIsNeverEmitted()
    {
        // Each segment is quoted or bare in its entirety — never half of each, which the
        // parser rejects as fk_operand_segment_partially_quoted.
        assertEquals("Line @fk(\"my orders\".order_id)",
                ColumnComment.of("Line").foreignKey("my orders", "order_id").build());
    }

    @Test
    void emptyOrNullSegmentsAreRefused()
    {
        assertThrows(IllegalArgumentException.class, () -> ColumnComment.renderSegment(""));
        assertThrows(IllegalArgumentException.class, () -> ColumnComment.renderSegment(null));
        assertThrows(IllegalArgumentException.class, () -> ColumnComment.Reference.of("orders", ""));
        assertThrows(IllegalArgumentException.class, () -> ColumnComment.Reference.of("orders", null));
    }

    @Test
    void aReferenceNeedsTableAndColumn()
    {
        assertThrows(IllegalArgumentException.class, () -> ColumnComment.Reference.of("orders"));
        assertThrows(IllegalArgumentException.class, ColumnComment.Reference::of);
    }

    @Test
    void referenceExposesItsResolvedTarget()
    {
        ColumnComment.Reference reference =
                ColumnComment.Reference.of("analytics", "public", "orders", "order_id");
        assertEquals("orders", reference.targetTable());
        assertEquals("order_id", reference.targetColumn());
        assertEquals(ColumnComment.Reference.of("orders", "order_id"), reference);
        assertNotEquals(ColumnComment.Reference.of("orders", "customer_id"), reference);
    }

    @Test
    void proseCarryingALiveTagIsRefused()
    {
        // COA would strip these and declare a key nobody asked for.
        assertThrows(IllegalArgumentException.class, () -> ColumnComment.of("see @pk for details"));
        assertThrows(IllegalArgumentException.class, () -> ColumnComment.of("ends with @pk"));
        assertThrows(IllegalArgumentException.class, () -> ColumnComment.of("see @fk(orders.id)"));
        // @pk's pattern does not match @notnull, so the newer tag needs a guard of its own.
        assertThrows(IllegalArgumentException.class, () -> ColumnComment.of("see @notnull for details"));
        assertThrows(IllegalArgumentException.class, () -> ColumnComment.of("ends with @notnull"));
    }

    @Test
    void proseThatMerelyResemblesTheNotNullTagIsAccepted()
    {
        // COA reports and keeps these, so refusing them here would refuse text COA would have stored.
        assertEquals("see @NOTNULL", ColumnComment.of("see @NOTNULL").build());
        assertEquals("see @notnullable", ColumnComment.of("see @notnullable").build());
        assertEquals("see @notnull=x", ColumnComment.of("see @notnull=x").build());
        assertEquals("owner bob@notnull.example.com",
                ColumnComment.of("owner bob@notnull.example.com").build());
    }

    @Test
    void proseThatMerelyResemblesATagIsAccepted()
    {
        // Every one of these is a near miss the parser leaves alone: wrong case, a longer
        // name, an identifier character in front of the "@", or an operand spelling @pk does
        // not take.
        assertEquals("owner bob@pk.example.com",
                ColumnComment.of("owner bob@pk.example.com").build());
        assertEquals("see @PK", ColumnComment.of("see @PK").build());
        assertEquals("see @pkey", ColumnComment.of("see @pkey").build());
        assertEquals("see @pk(customer_id)", ColumnComment.of("see @pk(customer_id)").build());
        assertEquals("see @pk=customer_id", ColumnComment.of("see @pk=customer_id").build());
        assertEquals("see @fk=orders.id", ColumnComment.of("see @fk=orders.id").build());
    }

    @Test
    void toStringRendersTheComment()
    {
        assertTrue(ColumnComment.of("Key").primaryKey().toString().endsWith("@pk"));
        assertEquals("orders.order_id", ColumnComment.Reference.of("orders", "order_id").toString());
    }
}
