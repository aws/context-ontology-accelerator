// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.metadata;

import java.util.Objects;

/**
 * One row of {@code information_schema.columns}: a column's name, its declared type, whether it is
 * declared {@code NOT NULL}, and the comment a data engineer wrote on it.
 *
 * <p>Raw. The comment is what Unity Catalog holds, tags and all; stripping happens in
 * {@link TableAssembler}, so a test can tell "the connector read the comment" from "the connector
 * cleaned it".
 *
 * <p><b>Nullability travels the comment channel, as the declared keys do.</b> Athena's {@code Column}
 * type has no field for it and {@code DESCRIBE} returns name, type and comment, so it reaches COA
 * through the {@code @notnull} tag or not at all.
 *
 * <p>Three states, not two ({@link Nullability}): "unknown" is not "nullable", and the distinction is
 * what keeps the tag additive — absence means nobody said.
 */
public final class ColumnDefinition
{
    /**
     * What {@code information_schema.columns.is_nullable} said about a column.
     *
     * <p>{@link #UNKNOWN} exists because a value this connector does not recognise must not be guessed
     * at in either direction: guessing {@code NOT_NULL} asserts a constraint the source never declared,
     * and guessing {@code NULLABLE} is indistinguishable from unknown to COA anyway, so the honest
     * answer is to emit no tag.
     */
    public enum Nullability
    {
        /** {@code is_nullable = 'NO'} — declared {@code NOT NULL}, so the tag is emitted. */
        NOT_NULL,

        /** {@code is_nullable = 'YES'} — no tag, which is also COA's default. */
        NULLABLE,

        /** Absent, null, or a spelling this connector does not recognise. No tag. */
        UNKNOWN;

        /**
         * Reads one {@code is_nullable} value. Databricks returns {@code 'YES'} or {@code 'NO'},
         * measured; anything else — including null, blank, and the {@code true}/{@code false} spelling
         * some other engines use — is {@link #UNKNOWN} rather than an error.
         */
        public static Nullability of(String isNullable)
        {
            if (isNullable == null) {
                return UNKNOWN;
            }
            String value = isNullable.trim();
            if ("NO".equalsIgnoreCase(value)) {
                return NOT_NULL;
            }
            if ("YES".equalsIgnoreCase(value)) {
                return NULLABLE;
            }
            return UNKNOWN;
        }
    }

    private final String name;
    private final String fullDataType;
    private final String comment;
    private final Nullability nullability;

    /**
     * A column whose nullability was not read. Kept because most of this connector's own tests are
     * about types, comments and keys, and because a caller that has no {@code is_nullable} to hand
     * should say so rather than pick a side.
     */
    public ColumnDefinition(String name, String fullDataType, String comment)
    {
        this(name, fullDataType, comment, Nullability.UNKNOWN);
    }

    /**
     * @param name         the column name, exactly as {@code information_schema.columns} spells it.
     *                     <b>Case-preserving:</b> Unity Catalog lower-cases a table name but keeps a
     *                     column's case, so {@code CustomerName STRING} comes back as
     *                     {@code column_name = "CustomerName"} (measured; see
     *                     {@link InformationSchemaSql}). Do not normalise it — this string has to match
     *                     the Arrow field name Athena projects, and folding it breaks every mixed-case
     *                     column with no error anywhere.
     * @param fullDataType the value of {@code full_data_type}, e.g. {@code decimal(10,2)}.
     * @param comment      the column comment, or null when it has none. Kept verbatim.
     * @param nullability  what {@code is_nullable} said. Null is treated as
     *                     {@link Nullability#UNKNOWN}.
     * @throws IllegalArgumentException if the name or the type is null or blank.
     */
    public ColumnDefinition(String name, String fullDataType, String comment, Nullability nullability)
    {
        if (name == null || name.trim().isEmpty()) {
            throw new IllegalArgumentException("Column name must not be null or blank");
        }
        if (fullDataType == null || fullDataType.trim().isEmpty()) {
            throw new IllegalArgumentException(
                    "Column " + name + " has no type in information_schema.columns.full_data_type");
        }
        this.name = name;
        this.fullDataType = fullDataType;
        this.comment = comment;
        this.nullability = (nullability == null) ? Nullability.UNKNOWN : nullability;
    }

    public String name()
    {
        return name;
    }

    /** The Databricks type, arguments included. */
    public String fullDataType()
    {
        return fullDataType;
    }

    /** The comment as Unity Catalog holds it, or null. */
    public String comment()
    {
        return comment;
    }

    /** What {@code is_nullable} said. Never null. */
    public Nullability nullability()
    {
        return nullability;
    }

    /**
     * Whether this column earns a {@code @notnull} tag. {@link Nullability#UNKNOWN} must emit nothing,
     * since absence of the tag is how COA reads "nobody said".
     */
    public boolean isNotNull()
    {
        return nullability == Nullability.NOT_NULL;
    }

    @Override
    public String toString()
    {
        return "ColumnDefinition{" + name + " " + fullDataType + " " + nullability + "}";
    }

    @Override
    public boolean equals(Object other)
    {
        if (this == other) {
            return true;
        }
        if (!(other instanceof ColumnDefinition)) {
            return false;
        }
        ColumnDefinition that = (ColumnDefinition) other;
        return name.equals(that.name)
                && fullDataType.equals(that.fullDataType)
                && Objects.equals(comment, that.comment)
                && nullability == that.nullability;
    }

    @Override
    public int hashCode()
    {
        return Objects.hash(name, fullDataType, comment, nullability);
    }
}
