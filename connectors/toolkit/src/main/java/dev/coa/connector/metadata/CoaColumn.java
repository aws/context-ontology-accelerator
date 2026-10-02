// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.connector.metadata;

import dev.coa.connector.constraints.ColumnComment;
import org.apache.arrow.vector.types.pojo.ArrowType;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * One column as the source describes it: a name, a type, prose, and whether it takes part in a key.
 *
 * <pre>{@code
 * CoaColumn.of("customer_id", Types.MinorType.BIGINT.getType())
 *          .describedAs("Customer that placed the order")
 *          .foreignKey("customers", "customer_id")
 * }</pre>
 *
 * <p>Keys are declared as <b>intent</b>. How that reaches COA — inside a column comment, as
 * {@code @pk} / {@code @fk(...)} tags — is {@link CoaTable#toTableSchema()}'s problem, not an
 * author's. Types are Arrow's rather than a parallel enum, since Arrow is a type vocabulary and not
 * part of the Athena SDK, and a closed enum would lose decimals and every future type for nothing.
 *
 * <p>Mutable while being declared, and not thread-safe: build one per column. Do not keep a
 * reference after handing it to a {@link CoaTable} — that snapshots what it is given.
 */
public final class CoaColumn
{
    private final String name;
    private final ArrowType type;
    private final List<ColumnComment.Reference> foreignKeys = new ArrayList<>();
    private String description = "";
    private boolean primaryKeyMember;
    private boolean notNull;

    private CoaColumn(String name, ArrowType type)
    {
        this.name = name;
        this.type = type;
    }

    /**
     * A scalar column, named as the source spells it.
     *
     * @throws IllegalArgumentException if the name is blank or the type is null.
     */
    public static CoaColumn of(String name, ArrowType type)
    {
        requireText(name, "Column name");
        if (type == null) {
            throw new IllegalArgumentException("Column " + name + " has no Arrow type");
        }
        return new CoaColumn(name, type);
    }

    /**
     * A copy of the declaration state. {@link ColumnComment.Reference} is immutable and an Arrow
     * {@link Field} is treated as such, so only what a caller can still change is duplicated. Lets
     * {@link CoaTable} snapshot what it is handed, which is what makes its immutability real.
     */
    CoaColumn copy()
    {
        CoaColumn copy = new CoaColumn(name, type);
        copy.description = description;
        copy.primaryKeyMember = primaryKeyMember;
        copy.notNull = notNull;
        copy.foreignKeys.addAll(foreignKeys);
        return copy;
    }

    /**
     * The column's prose description. Null is treated as none.
     *
     * @throws IllegalArgumentException if the text carries a live {@code @pk}, {@code @notnull} or
     *                                 {@code @fk(} tag, which COA would strip and act on — declare it
     *                                 instead.
     */
    public CoaColumn describedAs(String text)
    {
        // Validated here, not at render time, so the failure lands on the author's own call.
        ColumnComment.of(text);
        this.description = (text == null) ? "" : text;
        return this;
    }

    /**
     * Declares this column part of its table's primary key. For a composite key, call it on
     * <i>every</i> member; COA takes the key's order from column order, so there is no ordinal.
     */
    public CoaColumn primaryKey()
    {
        this.primaryKeyMember = true;
        return this;
    }

    /**
     * Declares this column {@code NOT NULL} in the source.
     *
     * <p>Only for a column the source declares so. Leaving it unset means "nobody said"; there is no way
     * to assert "nullable".
     */
    public CoaColumn notNull()
    {
        this.notNull = true;
        return this;
    }

    /**
     * Declares this column a foreign key onto {@code parentTable.parentColumn}, both unquoted as the
     * source spells them. For a composite key, call it once per participating child column naming
     * its own parent — COA stores a composite foreign key as one record per column.
     */
    public CoaColumn foreignKey(String parentTable, String parentColumn)
    {
        return foreignKey(ColumnComment.Reference.of(parentTable, parentColumn));
    }

    /** As above, for a possibly qualified parent; see {@link ColumnComment.Reference#of}. */
    public CoaColumn foreignKey(ColumnComment.Reference parent)
    {
        if (parent == null) {
            throw new IllegalArgumentException("Foreign key reference must not be null");
        }
        if (!foreignKeys.contains(parent)) {
            foreignKeys.add(parent);
        }
        return this;
    }

    /** @return the column name. */
    public String name()
    {
        return name;
    }

    /** @return the column's Arrow type. */
    public ArrowType type()
    {
        return type;
    }

    /** @return the prose description, or {@code ""} when none was set. */
    public String description()
    {
        return description;
    }

    /** @return whether this column is part of its table's primary key. */
    public boolean isPrimaryKey()
    {
        return primaryKeyMember;
    }

    /**
     * @return whether this column was declared {@code NOT NULL}. False also covers "the source did not
     *         say", which is why there is no {@code isNullable()} to pair with it.
     */
    public boolean isNotNull()
    {
        return notNull;
    }

    /** @return the parents this column references, in declaration order. */
    public List<ColumnComment.Reference> foreignKeys()
    {
        return Collections.unmodifiableList(new ArrayList<>(foreignKeys));
    }

    /**
     * @return this column's prose and key tags, <b>unrendered</b>. The builder rather than its
     *         string: handing a finished comment back to {@link ColumnComment#of(String)} trips its
     *         guard against prose already carrying a live tag.
     */
    ColumnComment toColumnComment()
    {
        ColumnComment comment = ColumnComment.of(description);
        if (primaryKeyMember) {
            comment.primaryKey();
        }
        if (notNull) {
            comment.notNull();
        }
        for (ColumnComment.Reference parent : foreignKeys) {
            comment.foreignKey(parent);
        }
        return comment;
    }

    private static void requireText(String value, String what)
    {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(what + " must not be null or blank");
        }
    }
}
