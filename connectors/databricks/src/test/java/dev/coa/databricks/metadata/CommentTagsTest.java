// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.metadata;

import org.junit.jupiter.api.Test;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Stripping a customer-authored tag out of a Unity Catalog comment, and leaving alone everything COA
 * leaves alone.
 */
class CommentTagsTest
{
    @Test
    void stripsALiveForeignKeyTag()
    {
        // Anyone with MODIFY can run COMMENT ON COLUMN. Left in, this asserts a relationship Unity Catalog
        // never declared, into a table that may not exist.
        assertEquals("Line total",
                CommentTags.strip("Line total @fk(payroll.ssn)"));
    }

    @Test
    void stripsALivePrimaryKeyTag()
    {
        assertEquals("Customer surrogate key",
                CommentTags.strip("Customer surrogate key @pk"));
    }

    @Test
    void stripsACustomerWrittenNotNullTag()
    {
        // Left in, a customer-written tag marks a nullable column non-nullable, and by the time COA sees it
        // it is byte-identical to one emitted from is_nullable. The channel is cleared here and refilled
        // only from information_schema.
        assertEquals("Optional note", CommentTags.strip("Optional note @notnull"));
        assertEquals("", CommentTags.strip("@notnull"));
        assertEquals("before after", CommentTags.strip("before @notnull after"));
    }

    @Test
    void leavesNotNullNearMissesAloneBecauseCoaLeavesThemAlone()
    {
        for (String prose : new String[] {
            "value @NOTNULL here", "value @notnullable here", "value @notnull=x here",
            "value @notnull(x) here", "owner bob@notnull.example.com"}) {
            assertEquals(prose, CommentTags.strip(prose), prose);
            assertEquals(prose, CommentTags.neutralise(prose), prose);
        }
    }

    @Test
    void stripsSeveralTagsAndKeepsTheProseAroundThem()
    {
        assertEquals("before middle after",
                CommentTags.strip("before @pk middle @fk(orders.order_id) after"));
    }

    @Test
    void stripsATagAtTheStart()
    {
        assertEquals("the rest", CommentTags.strip("@pk the rest"));
        assertEquals("the rest", CommentTags.strip("@fk(a.b) the rest"));
    }

    @Test
    void leavesNothingWhenTheCommentWasOnlyTags()
    {
        assertEquals("", CommentTags.strip("@pk @fk(orders.order_id)"));
    }

    @Test
    void anEmailAddressDoesNotLookLikeATag()
    {
        // The left-boundary rule: without it an address in a comment mints a primary key. This is the live
        // fixture's own order_lines.sku comment.
        assertEquals("Contact bob@pk.example.com about this column",
                CommentTags.strip("Contact bob@pk.example.com about this @pk column"));
    }

    @Test
    void leavesNearMissesAloneBecauseCoaLeavesThemAlone()
    {
        // Case is significant, and @pk takes no operand: @pk=x and @pk(x) are spellings COA reports and
        // keeps. Removing them here would delete text a customer wrote and COA would have stored.
        for (String prose : new String[] {
            "value @PK here", "value @pkey here", "value @pk=x here", "value @pk(x) here",
            "value @FK(a.b) here", "value @fkey(a.b) here"}) {
            assertEquals(prose, CommentTags.strip(prose), prose);
        }
    }

    @Test
    void aForeignKeyOperandMayContainABracketInsideQuotes()
    {
        // The grammar quotes per segment, and a ")" inside a quoted segment is data. Getting the extent
        // wrong leaves half a tag in the prose.
        assertEquals("after", CommentTags.strip("@fk(\"a)b\".c) after"));
        assertEquals("after", CommentTags.strip("@fk(\"say \"\"hi\"\")\".c) after"));
    }

    @Test
    void anUnterminatedForeignKeyTagIsLeftAlone()
    {
        // COA reports a malformed tag and keeps it in the stored description, and that surviving text is
        // the only feedback its author gets.
        assertEquals("Line total @fk(orders.order_id",
                CommentTags.strip("Line total @fk(orders.order_id"));
    }

    @Test
    void aForeignKeyTagWhoseOperandNamesNoColumnIsLeftAloneToo()
    {
        // Closing the bracket is not what makes a tag: COA acts on an @fk only when its operand resolves to
        // a parent TABLE.COLUMN, and keeps the rest verbatim as the only signal that would get a typo fixed.
        // None of these resolves.
        for (String comment : new String[] {
            "Parent key @fk(orders)", "@fk(orders.)", "@fk()", "@fk(orders.order id)",
            "@fk(orders.order_id, nullable)", "@fk(\"orders\".)", "@fk(\"\".\"c\")",
            "@fk(\"a\"b.c)"}) {
            assertEquals(comment, CommentTags.strip(comment), comment);
        }
        // And the encoder still accepts what is forwarded, because neutralise disarms what survived.
        for (String comment : new String[] {
            "Parent key @fk(orders)", "@fk(orders.)", "@fk()", "@fk(orders.order id)",
            "@fk(orders.order_id, nullable)"}) {
            dev.coa.connector.constraints.ColumnComment.of(
                    CommentTags.neutralise(CommentTags.strip(comment)));
        }
    }

    @Test
    void aForeignKeyTagWhoseOperandDoesResolveIsStripped()
    {
        // All three resolve, so COA acts on them: padding is dropped, extra leading qualification is
        // qualification, and a quoted segment names what it spells.
        assertEquals("", CommentTags.strip("@fk( orders . order_id )"));
        assertEquals("", CommentTags.strip("@fk(db.public.orders.order_id)"));
        assertEquals("", CommentTags.strip("@fk(\"my orders\".\"order id\")"));
    }

    @Test
    void collapsesTheWhitespaceATagLeavesBehind()
    {
        assertEquals("a b", CommentTags.strip("a   @pk    b"));
        assertEquals("a b", CommentTags.strip("a\n@pk\tb"));
    }

    @Test
    void handlesNullAndEmpty()
    {
        assertEquals("", CommentTags.strip(null));
        assertEquals("", CommentTags.strip(""));
        assertEquals("", CommentTags.strip("   "));
    }

    @Test
    void carriesTagReportsWhetherAnythingWouldBeStripped()
    {
        assertTrue(CommentTags.carriesTag("Line total @pk"));
        assertTrue(CommentTags.carriesTag("Line total @notnull"));
        assertTrue(CommentTags.carriesTag("Line total @fk(a.b)"));
        assertFalse(CommentTags.carriesTag("Line total"));
        // A closed bracket whose operand names no column is not a tag, and strip removes nothing from it.
        assertFalse(CommentTags.carriesTag("Line total @fk(orders)"));
        assertFalse(CommentTags.carriesTag("bob@pk.example.com"));
        assertFalse(CommentTags.carriesTag(null));
    }

    @Test
    void aStrippedCommentIsAcceptableToTheToolkitsEncoder()
    {
        // ColumnComment.of() throws on prose carrying a live tag, so any comment this class returns has to
        // pass it. Otherwise a customer's tag fails the whole table's describe rather than being ignored.
        dev.coa.connector.constraints.ColumnComment.of(
                CommentTags.strip("prose @pk more @fk(orders.order_id) end"));
        dev.coa.connector.constraints.ColumnComment.of(
                CommentTags.strip("Contact bob@pk.example.com about this @pk column"));
    }

    // ---------------------------------------------------------------------------------------------
    // neutralise: what strip() deliberately leaves behind, and the encoder refuses
    // ---------------------------------------------------------------------------------------------

    @Test
    void stripAloneLeavesAnUnterminatedTagTheEncoderRefuses()
    {
        // Pins the gap the two methods bridge, in both directions. strip() mirrors COA's parser and keeps
        // a malformed tag as feedback; the toolkit's encoder refuses ANY @fk(, by throwing, which for a
        // comment nobody can correct makes the table permanently undiscoverable.
        String stripped = CommentTags.strip("Line total @fk(orders.order_id");
        assertEquals("Line total @fk(orders.order_id", stripped);
        assertThrows(IllegalArgumentException.class,
                () -> dev.coa.connector.constraints.ColumnComment.of(stripped));

        // And neutralise closes it, keeping every character of the prose AND of the tag.
        assertEquals("Line total _@fk(orders.order_id", CommentTags.neutralise(stripped));
        dev.coa.connector.constraints.ColumnComment.of(CommentTags.neutralise(stripped));
    }

    @Test
    void neutraliseDisarmsOnlyTheTagShapedTokenAndKeepsEverythingElse()
    {
        assertEquals("Line total _@fk(orders.order_id",
                CommentTags.neutralise("Line total @fk(orders.order_id"));
        assertEquals("before _@fk( after", CommentTags.neutralise("before @fk( after"));
    }

    @Test
    void aTagThatBecomesLiveOnlyAfterAnEarlierOneIsRemovedIsDisarmedInPlace()
    {
        // To COA's parser "@notnull@pk" is one live tag followed by prose: the "@pk" is preceded by an
        // identifier character, so it is a near miss and COA keeps it. strip() agrees because both scan the
        // ORIGINAL string.
        assertEquals("@pk", CommentTags.strip("@notnull@pk"));
        // The other order, which is the one that was wrong: two sequential passes read the second tag
        // against the FIRST pass's output, where the "k" that made it a near miss is gone.
        assertEquals("@notnull", CommentTags.strip("@pk@notnull"));
        assertEquals("id @notnull rest", CommentTags.strip("id @pk@notnull rest"));

        // But COA parses the string this connector FORWARDS, not the customer's, and in "@pk" the tag is
        // live. The disarm prefixes an identifier character, which is precisely what BOTH liveness rules
        // refuse to see in front of a tag, so the token is prose to both sides with every character the
        // customer typed still present.
        assertEquals("_@pk", CommentTags.neutralise(CommentTags.strip("@notnull@pk")));
        assertEquals("_@pk", CommentTags.neutralise(CommentTags.strip("@pk@pk")));
        assertEquals("_@notnull", CommentTags.neutralise(CommentTags.strip("@pk@notnull")));
        assertEquals("before _@pk after",
                CommentTags.neutralise(CommentTags.strip("before @notnull@pk after")));
        assertEquals("note@pk", CommentTags.neutralise(CommentTags.strip("note@pk")));
        // What comes out is prose to the encoder, so it reaches COA as a description.
        dev.coa.connector.constraints.ColumnComment.of(
                CommentTags.neutralise(CommentTags.strip("@notnull@pk")));
    }

    @Test
    void disarmingKeepsEveryCharacterTheCustomerTyped()
    {
        // Asserted by removing the added characters and comparing against the input, so it holds for shapes
        // this file does not enumerate.
        for (String comment : new String[] {
            "@notnull@pk", "@pk@pk", "Line total @fk(orders.order_id", "@fk(@fk(",
            "before @notnull@pk after", "@pk@pk@pk", "a_b @pk c_d"}) {
            String forwarded = CommentTags.neutralise(comment);
            // Undo only the ADDED characters: the prefix is always inserted immediately before an "@", so
            // "_@" is the added pair and an underscore anywhere else is the customer's — which is why this
            // cannot be written as replace("_", ""). "order_id" is exactly the case that catches that.
            assertEquals(comment, forwarded.replace("_@", "@"),
                    "characters went missing from \"" + comment + "\": " + forwarded);
        }
    }

    @Test
    void neutraliseDisarmsEveryLiveTokenInTheSamePass()
    {
        // One left-to-right pass covers every token: the prefix goes in FRONT of a match, so it changes the
        // context of no later one.
        assertEquals("_@fk( _@pk", CommentTags.neutralise("@fk( @pk"));
        assertEquals("_@fk(_@fk(", CommentTags.neutralise("@fk(@fk("));
        assertEquals("_@fk(_@fk( _@pk tail", CommentTags.neutralise("@fk(@fk( @pk tail"));
        assertEquals("_@pk@pk@pk", CommentTags.neutralise("@pk@pk@pk"));
        assertEquals("_@fk(_@fk(_@fk(", CommentTags.neutralise("@fk(@fk(@fk("));
        assertEquals("_@notnull@notnull", CommentTags.neutralise("@notnull@notnull"));
    }

    @Test
    void nothingIsLostExceptATagThatWasLiveInTheCustomersOwnText()
    {
        // A character reaches the description unless it belonged to a tag that was live in the ORIGINAL
        // comment. The oracle below is a single left-to-right scan on purpose: a second pass over a first
        // pass's output sees boundaries the customer never wrote. The alphabet deliberately excludes an
        // operand-free tag written INSIDE an @fk operand ("@fk(a.@pk)"), the one shape where this class
        // still diverges from COA's parser — see CommentTags' class comment.
        String[] pieces = {"id", " ", "@pk", "@notnull", "@fk(a.b)", "@fk(bad)", "rest"};
        for (String first : pieces) {
            for (String second : pieces) {
                for (String third : pieces) {
                    String comment = first + second + third;
                    String forwarded = CommentTags.neutralise(CommentTags.strip(comment));
                    assertEquals(withoutWhitespace(liveTagsRemoved(comment)),
                            // Undo only the ADDED characters: the prefix is always inserted immediately
                            // before an "@", and no piece above contains an underscore.
                            withoutWhitespace(forwarded).replace("_@", "@"),
                            "characters went missing from \"" + comment + "\": \"" + forwarded + "\"");
                }
            }
        }
    }

    /**
     * The oracle: {@code comment} with every tag COA would act on removed, in ONE pass over the original.
     * A near miss and an {@code @fk(...)} whose operand names no {@code TABLE.COLUMN} are kept verbatim,
     * and scanning resumes past a kept tag's own extent, as {@code constraint_tags.py} does.
     */
    private static String liveTagsRemoved(String comment)
    {
        Pattern liveTag = Pattern.compile(
                "(?<![A-Za-z0-9_$])@(?:pk|notnull)(?![A-Za-z0-9_=(])"
                        + "|(?<![A-Za-z0-9_$])@fk\\([^)]*\\)");
        Matcher tag = liveTag.matcher(comment);
        StringBuilder kept = new StringBuilder();
        int cursor = 0;
        while (tag.find()) {
            kept.append(comment, cursor, tag.start());
            if (!namesATableAndColumn(tag.group())) {
                kept.append(tag.group());
            }
            cursor = tag.end();
        }
        return kept.append(comment.substring(cursor)).toString();
    }

    /**
     * Whether a matched tag is one COA acts on. The operand-free pair always is; an {@code @fk} has to
     * name a parent {@code TABLE.COLUMN}.
     */
    private static boolean namesATableAndColumn(String tag)
    {
        if (!tag.startsWith("@fk(")) {
            return true;
        }
        String[] segments = tag.substring(4, tag.length() - 1).split("\\.", -1);
        if (segments.length < 2) {
            return false;
        }
        for (String segment : segments) {
            if (!segment.trim().matches("[A-Za-z0-9_$-]+")) {
                return false;
            }
        }
        return true;
    }

    private static String withoutWhitespace(String text)
    {
        return text.replaceAll("\\s", "");
    }

    @Test
    void neutraliseLeavesNearMissesAlone()
    {
        // Same rule as strip: the toolkit accepts these and COA stores them, so removing them would delete
        // a customer's text on both sides' behalf.
        for (String prose : new String[] {
            "value @PK here", "value @pkey here", "value @pk=x here", "value @pk(x) here",
            "value @fk here", "owner bob@pk.example.com", "x@fk(y"}) {
            assertEquals(prose, CommentTags.neutralise(prose), prose);
        }
    }

    @Test
    void neutraliseHandlesNullAndEmpty()
    {
        assertEquals("", CommentTags.neutralise(null));
        assertEquals("", CommentTags.neutralise(""));
        assertEquals("", CommentTags.neutralise("   "));
    }

    @Test
    void stripThenNeutraliseIsAlwaysAcceptableToTheEncoder()
    {
        // The guarantee, over a corpus rather than a case. If the two restated regexes ever drift from the
        // toolkit's guard, this fails.
        String[] corpus = {
            null, "", "   ", "plain prose",
            "@pk", "@fk(a.b)", "@pk @fk(a.b)", "prose @pk more @fk(a.b) end",
            "@fk(orders.order_id", "@fk(", "@fk((", "@fk(@fk(", "@fk( @pk", "@pk @fk(",
            "@fk(\"a)b\".c) then @fk(unterminated",
            "@fk(\"unclosed quote", "@fk(\"\"\")",
            "bob@pk.example.com", "@PK", "@pkey", "@pk=x", "@pk(x)", "@fk", "x@fk(y",
            "tabs\tand\nnewlines @fk( here",
            "@fk(a.b)@pk", "x@pk@fk(", "@@fk(", " @fk(a.b) @fk(c.d) @pk ",
            "@notnull", "@notnull @pk", "@fk( @notnull", "@notnull@fk(", "x@notnull",
            "@NOTNULL", "@notnullable", "@notnull=x", "@notnull(x)",
            "@pk@notnull", "@fk(orders)", "@fk(orders.)", "@fk()", "@fk(orders.order id)",
            "@fk(orders.order_id, nullable)", "@fk(a.@pk)", "@fk(orders) @fk(a.b) @pk",
        };
        for (String comment : corpus) {
            String forwarded = CommentTags.neutralise(CommentTags.strip(comment));
            // Throws if the encoder would refuse it, which is the whole point.
            dev.coa.connector.constraints.ColumnComment.of(forwarded);
        }
    }
}
