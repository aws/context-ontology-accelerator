// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.metadata;

import dev.coa.connector.constraints.ConstraintTags;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.regex.Matcher;

/**
 * Removes any {@code @pk}, {@code @notnull} or {@code @fk(...)} tag a customer wrote into a Unity
 * Catalog column comment, so the tag channel carries only what this connector put in it.
 *
 * <p>A column comment is set with {@code COMMENT ON COLUMN}, which anyone holding {@code MODIFY} can run,
 * so without this a comment reading {@code "customer surrogate key @pk"} mints a primary key Unity Catalog
 * never declared. This connector is the last place a hand-written tag can be told from a generated one,
 * because here the keys come from {@code table_constraints} and the comment from {@code columns}.
 *
 * <p>Liveness rules are restated from COA's parser so the two agree; the shared ones live in
 * {@link ConstraintTags}. Near misses are left alone, because COA leaves them alone and the surviving text
 * is a malformed tag's only feedback to its author.
 *
 * <p><b>{@link #strip(String)} alone is not enough to forward a comment</b>: {@link #neutralise(String)}
 * has to run after it, as {@code TableAssembler} does. Why, and the disarm-rather-than-delete choice:
 * {@code connectors/databricks/DESIGN.md}, "Comment tags are the connector's channel only" and "A tag can
 * become live after an earlier one is removed".
 */
public final class CommentTags
{
    /**
     * The character {@link #neutralise} puts in front of a tag to make it dead.
     *
     * <p>An underscore, and it has to be something in {@code [A-Za-z0-9_$]}: that is the set both
     * liveness rules refuse to see in front of a tag. A space, a backslash or a zero-width character
     * would leave the tag live.
     */
    private static final String DISARM_PREFIX = "_";

    /** What {@link #readOperand} returns for an operand that never closes. */
    private static final Operand NO_OPERAND = new Operand(-1, Collections.emptyList());

    private CommentTags()
    {
    }

    /**
     * A column comment with every live tag removed and whitespace collapsed. Null in, {@code ""} out.
     */
    public static String strip(String comment)
    {
        if (comment == null || comment.isEmpty()) {
            return "";
        }
        // The operand-free pass reads the foreign-key pass's output, which is sound in this direction
        // only: a removed @fk(...) becomes a space, so no @pk or @notnull changes liveness between the two
        // passes. Both tags go in ONE alternation for the same reason — see ConstraintTags.OPERAND_FREE.
        String withoutForeignKeys = stripForeignKeys(comment);
        return collapseWhitespace(
                ConstraintTags.OPERAND_FREE.matcher(withoutForeignKeys).replaceAll(" "));
    }

    /**
     * Makes any remaining tag-shaped sequence <b>dead in place</b>: every character the customer typed
     * survives, and the toolkit's encoder and COA's parser both read it as prose. Null in, {@code ""} out.
     *
     * <p>Separate from {@link #strip(String)} because the two answer to different owners. {@code strip}
     * mirrors COA's parser, which leaves a malformed {@code @fk(} in place as feedback to its author. The
     * toolkit's encoder refuses any {@code @fk(} in prose, closed or not, by throwing
     * {@link IllegalArgumentException}, which is not a {@link java.sql.SQLException} and so is not
     * classified on the way out: one comment reading {@code 'Line total @fk(orders.order_id'} would fail
     * that table's {@code DESCRIBE} permanently and take the schema's whole scan with it. Done by pattern
     * rather than by catching that exception, which would discard the whole comment and would stop working
     * silently if the toolkit ever refused for a second reason.
     *
     * <p>It disarms rather than deletes because COA parses what this connector forwards. Excising the token
     * loses text a direct read would have kept ({@code "@notnull@pk"} became {@code ""}), and forwarding it
     * live would mint a key from a field anyone holding {@code MODIFY} can write. Prefixing an identifier
     * character ({@link #DISARM_PREFIX}) makes the tag prose to both sides, so {@code "@notnull@pk"}
     * forwards as {@code "_@pk"}.
     *
     * <p>One pass suffices because the insertion goes in front of a match, so it changes the context of no
     * <i>later</i> match.
     *
     * @param text prose, normally the output of {@link #strip(String)}.
     */
    public static String neutralise(String text)
    {
        if (text == null || text.isEmpty()) {
            return "";
        }
        return collapseWhitespace(
                ConstraintTags.ANY.matcher(text).replaceAll(DISARM_PREFIX + "$0"));
    }

    /**
     * Whether {@link #strip(String)} would remove anything. For logging that it happened; the strip
     * itself is unconditional.
     */
    public static boolean carriesTag(String comment)
    {
        if (comment == null || comment.isEmpty()) {
            return false;
        }
        // Asked of strip() rather than restated, so the two cannot disagree about a near miss.
        return !strip(comment).equals(collapseWhitespace(comment));
    }

    /**
     * Removes every {@code @fk(...)} COA would act on, and keeps every other one verbatim. Left to right,
     * one pass.
     *
     * <p>COA acts on a tag only when its operand names a table and a column, so {@code @fk(orders)},
     * {@code @fk(orders.)}, {@code @fk()} and {@code @fk(orders.order id)} are reported, kept in the
     * stored description, and store no key. Deleting one here would erase a customer's typo <b>and</b> the
     * only signal that would ever get it fixed.
     */
    private static String stripForeignKeys(String comment)
    {
        StringBuilder out = new StringBuilder(comment.length());
        Matcher open = ConstraintTags.FOREIGN_KEY_OPEN.matcher(comment);
        int cursor = 0;
        // find(int) searches from an index without narrowing the region, so the left-boundary lookbehind
        // still reads the character before it. A region would hide it and make every tag after the first
        // look live.
        while (open.find(cursor)) {
            out.append(comment, cursor, open.start());
            Operand operand = readOperand(comment, open.end());
            if (operand.end < 0) {
                // Unterminated, so the tag has no known extent. COA keeps it and resumes just past the
                // head, which lets a later well-formed tag in the same comment still be read.
                out.append(comment, open.start(), open.end());
                cursor = open.end();
                continue;
            }
            if (operand.namesATableAndColumn()) {
                out.append(' ');
            }
            else {
                out.append(comment, open.start(), operand.end);
            }
            cursor = operand.end;
        }
        out.append(comment, cursor, comment.length());
        return out.toString();
    }

    /**
     * Reads the operand of one {@code @fk(} tag, mirroring {@code constraint_tags.py}'s
     * {@code _scan_operand}: segments are separated by {@code .} outside a quoted segment, the first
     * {@code )} outside one ends the operand, and {@code ""} inside one is an escaped quote.
     *
     * @param from index of the operand's first character, just past {@code @fk(}.
     */
    private static Operand readOperand(String comment, int from)
    {
        List<String> segments = new ArrayList<>(3);
        int start = from;
        int position = from;
        while (position < comment.length()) {
            char character = comment.charAt(position);
            if (character == ')') {
                segments.add(comment.substring(start, position));
                return new Operand(position + 1, segments);
            }
            if (character == '.') {
                segments.add(comment.substring(start, position));
                position++;
                start = position;
                continue;
            }
            if (character == '"') {
                int closing = closingQuote(comment, position);
                if (closing < 0) {
                    return NO_OPERAND;
                }
                position = closing;
                continue;
            }
            position++;
        }
        return NO_OPERAND;
    }

    /**
     * The index just past the closing quote of the delimited identifier opening at {@code start}, or -1
     * when it never closes. A doubled {@code ""} is content, as in SQL and in COA's own scanner.
     */
    private static int closingQuote(String text, int start)
    {
        for (int i = start + 1; i < text.length(); i++) {
            if (text.charAt(i) != '"') {
                continue;
            }
            if (i + 1 < text.length() && text.charAt(i + 1) == '"') {
                i++;
                continue;
            }
            return i + 1;
        }
        return -1;
    }

    /** One {@code @fk(} tag's operand, as read off the comment. */
    private static final class Operand
    {
        /** Index just past the terminating {@code )}, or -1 when the operand never closes. */
        private final int end;

        /** The raw segments — quotes in place, padding not yet dropped. */
        private final List<String> segments;

        private Operand(int end, List<String> segments)
        {
            this.end = end;
            this.segments = segments;
        }

        /**
         * Whether COA would resolve this operand to a parent {@code TABLE.COLUMN}, the only shape it acts
         * on. Mirrors {@code _split_reference}: the last two segments are the pair, anything before them is
         * qualification, and every one of them has to be a usable identifier.
         */
        private boolean namesATableAndColumn()
        {
            if (segments.size() < 2) {
                return false;
            }
            for (String segment : segments) {
                if (!isIdentifier(segment)) {
                    return false;
                }
            }
            return true;
        }

        /**
         * Whether one raw segment decodes to a non-empty identifier, mirroring {@code _decode_segment}.
         * Padding around it is dropped; whitespace inside a bare one is not an identifier character, and
         * a segment is quoted all through or not at all — {@code "a"b} names nothing rather than
         * {@code ab}.
         */
        private static boolean isIdentifier(String segment)
        {
            String trimmed = segment.strip();
            if (trimmed.startsWith("\"")) {
                // Longer than the two delimiters, since "" spells the empty name and no identifier is.
                return trimmed.length() > 2 && closingQuote(trimmed, 0) == trimmed.length();
            }
            return ConstraintTags.OPERAND_SEGMENT.matcher(trimmed).matches();
        }
    }

    private static String collapseWhitespace(String text)
    {
        StringBuilder out = new StringBuilder(text.length());
        boolean pendingSpace = false;
        for (int i = 0; i < text.length(); i++) {
            char character = text.charAt(i);
            if (Character.isWhitespace(character)) {
                pendingSpace = out.length() > 0;
                continue;
            }
            if (pendingSpace) {
                out.append(' ');
                pendingSpace = false;
            }
            out.append(character);
        }
        return out.toString();
    }
}
