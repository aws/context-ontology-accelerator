// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.connector.constraints;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Objects;

/**
 * Builds the column-comment string that carries declared key constraints to COA.
 *
 * <p>Most connectors never call this. {@code CoaColumn} declares keys as intent and
 * {@code CoaTable.toTableSchema()} calls this for you; reach for it directly only when writing a
 * connector without {@code CoaMetadataHandler}.
 *
 * <h2>Why comments</h2>
 *
 * The federation protocol has <b>no field anywhere</b> for primary or foreign keys —
 * {@code GetTableResponse} carries an Arrow schema, which describes names and types only. So
 * declared keys travel inside the column comments a connector already emits, which COA recovers via
 * {@code DESCRIBE}, parses, and strips before storing the remainder as the column description:
 *
 * <pre>
 *   &#64;pk                             this column is a member of the table's primary key
 *   &#64;notnull                        this column is declared NOT NULL
 *   &#64;fk(parent_table.parent_column) this column references that parent column
 * </pre>
 *
 * <pre>{@code
 * ColumnComment.of("Order this line belongs to")
 *              .primaryKey()
 *              .foreignKey("orders", "order_id")
 *              .build();                       // -> "Order this line belongs to @pk @fk(orders.order_id)"
 * }</pre>
 *
 * <h2>Rules it applies for you</h2>
 *
 * <ul>
 *   <li><b>Composite primary key</b> — {@code .primaryKey()} on <i>every</i> member column. COA
 *       collects them in {@code DESCRIBE} order; there is no ordinal.</li>
 *   <li><b>Composite foreign key</b> — one {@code .foreignKey(...)} per participating child column,
 *       each naming its own parent. Two columns means two tags, never one listing both, because COA
 *       stores a composite key as N single-column records.</li>
 *   <li><b>The child column is never named</b>: the tag lives in that column's own comment.</li>
 *   <li><b>Quoting is per segment and automatic</b> — bare when it matches
 *       {@code [A-Za-z0-9_$-]+}, double-quoted otherwise with embedded quotes doubled. Never
 *       pre-quote an argument; pass the identifier as the source spells it.</li>
 *   <li><b>Extra qualification is allowed</b> via {@link Reference#of(String...)}. COA reads the last
 *       two segments as {@code TABLE.COLUMN} and discards the rest, so it is documentation.</li>
 *   <li><b>Duplicate targets collapse</b>, matching COA, which dedups by resolved target.</li>
 * </ul>
 *
 * <h2>Nullability is the exception, not the rule</h2>
 *
 * {@code @notnull} tags a column the source declares {@code NOT NULL}, and there is deliberately no
 * {@code @nullable} counterpart: absence has to keep meaning <i>unknown</i>, since every connector
 * deployed before this tag existed emits none and COA defaults a column to nullable. Emit it only from
 * something the source actually declared.
 *
 * <p>A malformed tag is <b>left in the stored description verbatim</b> — COA's warning goes to its
 * own logs, which the connector author cannot see, so surviving text is the only feedback that
 * reaches them. This class validates and throws instead. For the same reason {@link #of(String)}
 * rejects prose already carrying a live tag, which COA would strip and act on. Prose that merely
 * resembles one is safe: recognition is case-sensitive and needs a non-identifier character in
 * front, so {@code @PK}, {@code @pkey} and {@code bob@pk.example.com} are left alone.
 *
 * <p>Deliberately dependency-free — no Athena SDK, no Arrow, no logging — so it can be copied out with
 * {@link ConstraintTags}. Not thread-safe; build one per column.
 */
public final class ColumnComment
{
    /** Marks the column as a member of its table's primary key. Takes no operand. */
    private static final String PK_TAG = "@pk";

    /**
     * Marks the column as declared {@code NOT NULL}. Takes no operand, like {@link #PK_TAG}.
     *
     * <p>Public because it is the spelling a connector author checks against when their source's own
     * nullability column is not a simple yes/no, and because these same eight characters have to be
     * matched by COA's parser on the other side of the wire.
     */
    public static final String NOT_NULL_TAG = "@notnull";

    private final String description;
    private final List<Reference> foreignKeys = new ArrayList<>();
    private boolean primaryKeyMember;
    private boolean notNull;

    private ColumnComment(String description)
    {
        this.description = description;
    }

    /**
     * Starts a comment from its human-readable prose.
     *
     * @param description the column's prose description, or {@code null} / blank for a
     *                    comment that carries nothing but tags. Kept verbatim (internal
     *                    whitespace included); only the finished comment is trimmed.
     * @return a new builder.
     * @throws IllegalArgumentException if the prose contains a tag COA would act on
     *                                  ({@code @pk}, {@code @notnull}, or {@code @fk(}) —
     *                                  declare it with {@link #primaryKey()} /
     *                                  {@link #notNull()} / {@link #foreignKey(String,
     *                                  String)} instead of writing it by hand, or reword
     *                                  the prose.
     */
    public static ColumnComment of(String description)
    {
        String prose = (description == null) ? "" : description;
        if (ConstraintTags.PRIMARY_KEY.matcher(prose).find()) {
            throw new IllegalArgumentException(
                    "Column prose contains a live @pk tag, which COA would strip and act on: \"" + prose
                            + "\". Call primaryKey() instead of writing the tag into the prose.");
        }
        if (ConstraintTags.NOT_NULL.matcher(prose).find()) {
            throw new IllegalArgumentException(
                    "Column prose contains a live " + NOT_NULL_TAG + " tag, which COA would strip and"
                            + " act on: \"" + prose + "\". Call notNull() instead of writing the tag"
                            + " into the prose.");
        }
        if (ConstraintTags.FOREIGN_KEY_OPEN.matcher(prose).find()) {
            throw new IllegalArgumentException(
                    "Column prose contains a live @fk( tag, which COA would strip and act on: \"" + prose
                            + "\". Call foreignKey(...) instead of writing the tag into the prose.");
        }
        return new ColumnComment(prose);
    }

    /**
     * Declares this column a member of its table's primary key.
     *
     * <p>Call it on every member of a composite key; the key's column order is
     * {@code DESCRIBE} order, so there is nothing else to say. Calling it more than once on
     * one column is harmless and emits one tag.
     *
     * @return this builder.
     */
    public ColumnComment primaryKey()
    {
        this.primaryKeyMember = true;
        return this;
    }

    /**
     * Declares this column {@code NOT NULL} in the source.
     *
     * <p>Call it only for a column the source says so about. Not calling it means "nobody said", not
     * "nullable", and there is no tag that means the latter. Calling it more than once emits one tag.
     *
     * @return this builder.
     */
    public ColumnComment notNull()
    {
        this.notNull = true;
        return this;
    }

    /**
     * Declares this column a foreign key onto {@code parentTable.parentColumn}.
     *
     * @param parentTable  the parent table, spelled as {@code DESCRIBE} reports it (lower
     *                     case, for Athena). Pass it unquoted; quoting is applied for you.
     * @param parentColumn the parent column, same.
     * @return this builder.
     * @throws IllegalArgumentException if either name is null or empty.
     */
    public ColumnComment foreignKey(String parentTable, String parentColumn)
    {
        return foreignKey(Reference.of(parentTable, parentColumn));
    }

    /**
     * Declares this column a foreign key onto a possibly qualified parent reference.
     *
     * @param parent the parent reference; see {@link Reference#of(String...)}.
     * @return this builder.
     * @throws IllegalArgumentException if {@code parent} is null.
     */
    public ColumnComment foreignKey(Reference parent)
    {
        if (parent == null) {
            throw new IllegalArgumentException("Foreign key reference must not be null");
        }
        // Dedup by resolved target, mirroring COA's own rule: it stores the decoded
        // (table, column) pair, so a repeated target cannot mean two keys.
        if (!foreignKeys.contains(parent)) {
            foreignKeys.add(parent);
        }
        return this;
    }

    /**
     * Renders the comment: the prose, then {@code @pk}, then {@code @notnull}, then one
     * {@code @fk(...)} per declared reference in declaration order, single-space separated and
     * trimmed.
     *
     * <p>The order is fixed and pinned by a test: the operand-free tags come first so the {@code @fk}
     * list stays last and contiguous, and {@code @notnull} goes <b>after</b> {@code @pk} so a comment for
     * a primary-key-only column is byte-identical to what this class emitted before the tag existed.
     *
     * @return the comment string. Put it in the Arrow <b>schema's</b> metadata keyed by this
     *         column's name — {@code SchemaBuilder.addMetadata(columnName, comment)}. <b>Never on
     *         the field's own metadata</b>, which Athena delivers and then ignores, so the tags
     *         reach nothing and no error is raised. {@link dev.coa.connector.schema.TableSchema}
     *         does this for you.
     */
    public String build()
    {
        StringBuilder out = new StringBuilder(description);
        if (primaryKeyMember) {
            appendTag(out, PK_TAG);
        }
        if (notNull) {
            appendTag(out, NOT_NULL_TAG);
        }
        for (Reference reference : foreignKeys) {
            appendTag(out, "@fk(" + reference.render() + ")");
        }
        return out.toString().trim();
    }

    /** @return {@link #build()}, so a comment can be used directly in string contexts. */
    @Override
    public String toString()
    {
        return build();
    }

    private static void appendTag(StringBuilder out, String tag)
    {
        if (out.length() > 0) {
            out.append(' ');
        }
        out.append(tag);
    }

    /**
     * Renders one identifier as an operand segment: bare when it can be, double-quoted
     * (with embedded quotes doubled) when it must be.
     *
     * <p>Exposed because it is the one rule an author is likely to want to check against a
     * real source's naming.
     *
     * @param identifier the identifier, exactly as the source spells it.
     * @return the segment as it appears inside {@code @fk( )}.
     * @throws IllegalArgumentException if {@code identifier} is null or empty. An empty
     *                                  segment is not an identifier — COA reports
     *                                  {@code @fk("".x)} as malformed — so it is refused
     *                                  here rather than emitted.
     */
    public static String renderSegment(String identifier)
    {
        if (identifier == null || identifier.isEmpty()) {
            throw new IllegalArgumentException("Reference segment must not be null or empty");
        }
        if (ConstraintTags.OPERAND_SEGMENT.matcher(identifier).matches()) {
            return identifier;
        }
        return '"' + identifier.replace("\"", "\"\"") + '"';
    }

    /**
     * A parent {@code TABLE.COLUMN} reference, optionally qualified.
     *
     * <p>Two references are equal when they resolve to the same target — the last two
     * segments — regardless of how much qualification each carries or which segments needed
     * quoting. That mirrors COA, which stores only the decoded pair, and is what makes
     * {@link ColumnComment#foreignKey(Reference)} dedup correctly.
     */
    public static final class Reference
    {
        private final List<String> segments;

        private Reference(List<String> segments)
        {
            this.segments = Collections.unmodifiableList(new ArrayList<>(segments));
        }

        /**
         * Builds a reference from its dotted segments.
         *
         * <p>The last two are the parent {@code TABLE} and {@code COLUMN}; anything before
         * them is catalog/database/schema qualification, which COA validates and then
         * discards. Pass every segment unquoted — quoting is applied per segment as needed.
         *
         * @param segments at least two segments, none null or empty.
         * @return the reference.
         * @throws IllegalArgumentException if fewer than two segments are given, or any is
         *                                  null or empty.
         */
        public static Reference of(String... segments)
        {
            if (segments == null || segments.length < 2) {
                throw new IllegalArgumentException(
                        "A reference needs at least two segments (parent table and parent column)");
            }
            for (String segment : segments) {
                // renderSegment rejects null/empty; call it now so the failure lands on the
                // author's own call rather than later inside build().
                renderSegment(segment);
            }
            return new Reference(Arrays.asList(segments));
        }

        /** @return the parent table — the second-to-last segment. */
        public String targetTable()
        {
            return segments.get(segments.size() - 2);
        }

        /** @return the parent column — the last segment. */
        public String targetColumn()
        {
            return segments.get(segments.size() - 1);
        }

        /**
         * @return the operand text as it appears inside {@code @fk( )}: the segments joined
         *         by {@code .}, each quoted only if it needs to be.
         */
        public String render()
        {
            StringBuilder out = new StringBuilder();
            for (String segment : segments) {
                if (out.length() > 0) {
                    out.append('.');
                }
                out.append(renderSegment(segment));
            }
            return out.toString();
        }

        /** @return {@link #render()}. */
        @Override
        public String toString()
        {
            return render();
        }

        @Override
        public boolean equals(Object other)
        {
            if (this == other) {
                return true;
            }
            if (!(other instanceof Reference)) {
                return false;
            }
            Reference that = (Reference) other;
            return targetTable().equals(that.targetTable()) && targetColumn().equals(that.targetColumn());
        }

        @Override
        public int hashCode()
        {
            return Objects.hash(targetTable(), targetColumn());
        }
    }
}
