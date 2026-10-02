// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.connector.constraints;

import java.util.regex.Pattern;

/**
 * The tag grammar's recognition rules, as patterns, shared by everything on this side of the wire.
 *
 * <p>One copy, because a connector both <b>writes</b> tags ({@link ColumnComment}) and <b>strips</b> them
 * (a connector reading comments a customer can edit, such as the Databricks one's {@code CommentTags}).
 * A spelling one treats as live and the other as prose is a hole, so the rule cannot be restated per
 * module.
 *
 * <p>These mirror COA's parser — {@code coa_sources}' {@code constraint_tags.py} — and must stay in step
 * with it by hand. A tag is live only when no identifier character precedes it, and the operand-free pair
 * only when no {@code =} or {@code (} follows: those are the near misses COA reports and leaves alone.
 */
public final class ConstraintTags
{
    /** No identifier character in front, which is what separates a tag from an address or a handle. */
    private static final String LEFT_BOUNDARY = "(?<![A-Za-z0-9_$])";

    /** Nothing that would make it another word, or a spelling that carries an operand. */
    private static final String NO_OPERAND = "(?![A-Za-z0-9_=(])";

    /** A live {@code @pk}: exact case, no operand. */
    public static final Pattern PRIMARY_KEY = Pattern.compile(LEFT_BOUNDARY + "@pk" + NO_OPERAND);

    /** A live {@code @notnull}. Its own pattern: {@link #PRIMARY_KEY}'s does not match it. */
    public static final Pattern NOT_NULL = Pattern.compile(LEFT_BOUNDARY + "@notnull" + NO_OPERAND);

    /** A live {@code @fk(}. The bracket is what makes COA read an operand. */
    public static final Pattern FOREIGN_KEY_OPEN = Pattern.compile(LEFT_BOUNDARY + "@fk\\(");

    /**
     * Both operand-free tags in one alternation, so a caller removing them makes <b>one</b> left-to-right
     * pass. Two passes are not the same thing: the second reads the first's output, where a near miss the
     * customer wrote has lost the identifier character that made it one.
     */
    public static final Pattern OPERAND_FREE =
            Pattern.compile(LEFT_BOUNDARY + "@(?:pk|notnull)" + NO_OPERAND);

    /** Any live tag. */
    public static final Pattern ANY =
            Pattern.compile(OPERAND_FREE.pattern() + "|" + FOREIGN_KEY_OPEN.pattern());

    /**
     * Characters an operand segment may contain without quotes. A superset of Athena's unquoted-identifier
     * set, matching {@code constraint_tags.py}'s {@code _UNQUOTED_SEGMENT}: {@code -} is legal in Glue
     * names and {@code $} appears in Hive-style ones.
     */
    public static final Pattern OPERAND_SEGMENT = Pattern.compile("[A-Za-z0-9_$-]+");

    private ConstraintTags()
    {
    }
}
