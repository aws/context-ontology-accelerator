// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.connector.metadata;

import dev.coa.connector.schema.TableSchema;
import org.apache.arrow.vector.types.pojo.ArrowType;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * One table as the source describes it: a name and its columns in order. What
 * {@link CoaMetadataHandler#describeTable} returns, and free of any Athena SDK type.
 *
 * <p><b>Declaration order matters.</b> It is the order Athena reports columns in, and the order COA
 * reads a composite primary key in — there is no ordinal anywhere else.
 *
 * <p>There is deliberately no table-level description: no Athena read path surfaces one for a
 * {@code LAMBDA} catalog, so a setter for it would silently discard what a caller wrote. See
 * {@link TableSchema}, which makes the same argument. Column descriptions do arrive.
 *
 * <p>Immutable, and genuinely so: it snapshots the columns it is given and hands back copies. The
 * {@link Builder} is not thread-safe; build one per table.
 */
public final class CoaTable
{
    private final String name;
    private final List<CoaColumn> columns;

    private CoaTable(Builder builder)
    {
        this.name = builder.name;
        List<CoaColumn> snapshot = new ArrayList<>(builder.columns.size());
        for (CoaColumn column : builder.columns.values()) {
            snapshot.add(column.copy());
        }
        this.columns = Collections.unmodifiableList(snapshot);
    }

    /**
     * @param tableName the table's name as Athena will see it.
     * @return a builder.
     * @throws IllegalArgumentException if the name is null or blank.
     */
    public static Builder named(String tableName)
    {
        return new Builder(tableName);
    }

    /** @return the table name. */
    public String name()
    {
        return name;
    }

    /**
     * @return copies of the columns, in declaration order. Copies because a {@link CoaColumn} is
     *         mutable while being declared, and a handler that caches its tables — as the reference
     *         connector does, in a static field — would otherwise let one request's edit change what
     *         every later request sees, losing declared keys with no error anywhere.
     */
    public List<CoaColumn> columns()
    {
        List<CoaColumn> copies = new ArrayList<>(columns.size());
        for (CoaColumn column : columns) {
            copies.add(column.copy());
        }
        return Collections.unmodifiableList(copies);
    }

    /** @return the column names, in declaration order. */
    public List<String> columnNames()
    {
        List<String> names = new ArrayList<>(columns.size());
        for (CoaColumn column : columns) {
            names.add(column.name());
        }
        return Collections.unmodifiableList(names);
    }

    /**
     * Turns this table into the Arrow schema Athena receives — <b>and this is where declared keys
     * become comments</b>, because the protocol has no field for a key anywhere.
     *
     * <p>Two steps, each owned by one class: {@link CoaColumn} renders prose plus key intent into a
     * comment via {@code ColumnComment}, which applies the tag syntax, quoting and deduplication;
     * {@link TableSchema} puts that comment where Athena reads it, which is the schema's metadata
     * keyed by column name and <b>not</b> the Arrow field.
     *
     * <p>Writing a connector without {@link CoaMetadataHandler}? This method is the one to copy.
     */
    public TableSchema toTableSchema()
    {
        TableSchema.Builder schema = TableSchema.named(name);
        for (CoaColumn column : columns) {
            // TableSchema renders the comment. Rendering here and passing the string back through
            // ColumnComment.of() would hit its guard against prose already carrying a live tag.
            schema.column(column.name(), column.type(), column.toColumnComment());
        }
        return schema.build();
    }

    /** Fluent builder for {@link CoaTable}. */
    public static final class Builder
    {
        private final String name;
        private final Map<String, CoaColumn> columns = new LinkedHashMap<>();

        private Builder(String name)
        {
            if (name == null || name.trim().isEmpty()) {
                throw new IllegalArgumentException("Table name must not be null or blank");
            }
            this.name = name;
        }

        /**
         * Adds a column; declaration order is the schema's and a composite key's column order.
         *
         * @throws IllegalArgumentException if the column is null or its name is already taken.
         */
        public Builder column(CoaColumn column)
        {
            if (column == null) {
                throw new IllegalArgumentException("Table " + name + " was given a null column");
            }
            if (columns.containsKey(column.name())) {
                throw new IllegalArgumentException(
                        "Table " + name + " already has a column named " + column.name());
            }
            columns.put(column.name(), column);
            return this;
        }

        /** A column with no description and no key. */
        public Builder column(String columnName, ArrowType type)
        {
            return column(CoaColumn.of(columnName, type));
        }

        /**
         * @return the table.
         * @throws IllegalStateException if no columns were added.
         */
        public CoaTable build()
        {
            if (columns.isEmpty()) {
                throw new IllegalStateException("Table " + name + " has no columns");
            }
            return new CoaTable(this);
        }
    }
}
