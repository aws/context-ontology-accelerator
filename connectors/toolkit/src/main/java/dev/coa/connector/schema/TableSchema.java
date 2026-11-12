// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.connector.schema;

import com.amazonaws.athena.connector.lambda.data.SchemaBuilder;
import dev.coa.connector.constraints.ColumnComment;
import org.apache.arrow.vector.types.pojo.ArrowType;
import org.apache.arrow.vector.types.pojo.Field;
import org.apache.arrow.vector.types.pojo.FieldType;
import org.apache.arrow.vector.types.pojo.Schema;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * One table's columns, types and column comments, and the Arrow {@link Schema} Athena reads them
 * from.
 *
 * <p>Most connectors never call this either — {@code CoaTable.toTableSchema()} does. It exists as a
 * separate class because it owns exactly one fact, below.
 *
 * <h2>Where a column comment has to live</h2>
 *
 * Arrow offers two places to hang a {@code comment}: a {@link Field}'s metadata, via
 * {@link FieldType}, and the {@link Schema}'s own metadata map. <b>Athena reads the schema's map,
 * keyed by column name.</b> Metadata put on a {@code Field} is serialised, crosses the wire intact,
 * and is then ignored.
 *
 * <p>Choosing wrong fails <i>silently</i>: nothing throws, the connector's tests pass,
 * {@code SELECT} works, and {@code DESCRIBE} returns name and type with no comment column at all —
 * so no {@code @pk} / {@code @fk} tag ever reaches COA. The API points the wrong way, too:
 * {@code FieldType} takes a metadata map that looks purpose-built, while {@link SchemaBuilder}
 * offers no field-metadata method whatsoever. The SDK's own {@code GlueMetadataHandler} shows the
 * intended form — {@code schemaBuilder.addMetadata(columnName, comment)}, nothing on the field.
 *
 * <p>Which is why a column is named and typed, never handed over as a pre-built {@code Field}:
 * building the field here is what guarantees the metadata map is empty.
 *
 * <h2>There is no table-level comment</h2>
 *
 * Athena's convention has a slot for one and no read path surfaces it for a {@code LAMBDA} catalog.
 * Six were tried: {@code DESCRIBE}; {@code GetTableMetadata} and {@code ListTableMetadata}, whose
 * {@code Parameters} comes back absent; {@code SHOW TBLPROPERTIES} and {@code SHOW CREATE TABLE},
 * both rejected as unsupported DDL; and {@code information_schema.tables}. A setter for a value
 * nothing reads is a question every reader would have to answer, so it is not offered — and with the
 * {@code comment} key unused, a column actually named {@code comment} is an ordinary column.
 *
 * <h2>Notes</h2>
 *
 * Unlike {@link ColumnComment} this depends on Arrow and the Athena SDK; it exists to encapsulate an
 * SDK call. {@link #toArrowSchema()} delegates to {@link SchemaBuilder} rather than constructing a
 * {@link Schema} directly for the same reason: the convention is the SDK's, so if it moves, one
 * method changes.
 *
 * <p>Immutable. The {@link Builder} is not thread-safe; build one per table.
 */
public final class TableSchema
{
    private final String name;
    private final List<Field> fields;
    private final Map<String, String> comments;

    private TableSchema(Builder builder)
    {
        this.name = builder.name;
        this.fields = Collections.unmodifiableList(new ArrayList<>(builder.fields));
        this.comments = Collections.unmodifiableMap(new LinkedHashMap<>(builder.comments));
    }

    /**
     * Starts a table schema.
     *
     * @param tableName the table's name as Athena will see it. Not written into the Arrow
     *                  schema, which has no slot for it — it names the table in this class's
     *                  error messages, and is available via {@link #name()}.
     * @return a builder.
     * @throws IllegalArgumentException if {@code tableName} is null or blank.
     */
    public static Builder named(String tableName)
    {
        return new Builder(requireText(tableName, "Table name"));
    }

    /** @return the table name given to {@link #named(String)}. */
    public String name()
    {
        return name;
    }

    /** @return the column names in declaration order. */
    public List<String> columnNames()
    {
        List<String> names = new ArrayList<>(fields.size());
        for (Field field : fields) {
            names.add(field.getName());
        }
        return Collections.unmodifiableList(names);
    }

    /**
     * @param columnName the column name.
     * @return that column's comment — prose plus any constraint tags — or {@code null} when it
     *         has none, including when the column itself is unknown.
     */
    public String comment(String columnName)
    {
        return comments.get(columnName);
    }

    /**
     * Renders the Arrow schema for a {@code GetTableResponse}.
     *
     * <p>Every column becomes a {@link Field} carrying <b>no</b> metadata, and every non-empty
     * comment becomes one schema-level metadata entry keyed by the column's name. See the class
     * javadoc for why that placement is the only one that works.
     *
     * @return the schema, columns in declaration order.
     */
    public Schema toArrowSchema()
    {
        SchemaBuilder schemaBuilder = SchemaBuilder.newBuilder();
        for (Field field : fields) {
            schemaBuilder.addField(field);
        }
        for (Map.Entry<String, String> comment : comments.entrySet()) {
            schemaBuilder.addMetadata(comment.getKey(), comment.getValue());
        }
        // Nothing else goes in this map. Column names are the only keys, so no key can be
        // written twice — which matters, because SchemaBuilder accumulates metadata in a Guava
        // ImmutableMap.Builder that throws "Multiple entries with same key" at build().
        return schemaBuilder.build();
    }

    private static String requireText(String value, String what)
    {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(what + " must not be null or blank");
        }
        return value;
    }

    /** Fluent builder for {@link TableSchema}. */
    public static final class Builder
    {
        private final String name;
        private final List<Field> fields = new ArrayList<>();
        private final Map<String, String> comments = new LinkedHashMap<>();

        private Builder(String name)
        {
            this.name = name;
        }

        /**
         * Adds a column with no comment.
         *
         * @param columnName the column name.
         * @param type       its Arrow type.
         * @return this builder.
         * @throws IllegalArgumentException if the name is null or blank, the type is null, or
         *                                  the name is already taken.
         */
        public Builder column(String columnName, ArrowType type)
        {
            return column(columnName, type, null);
        }

        /**
         * Adds a column with a comment.
         *
         * <p>Declaration order is the schema's column order, which is also {@code DESCRIBE}
         * order — and therefore the order COA reads a composite primary key in.
         *
         * @param columnName the column name.
         * @param type       its Arrow type.
         * @param comment    its comment, built with {@link ColumnComment} so any {@code @pk} /
         *                   {@code @fk} tags are spelled correctly. Null for no comment.
         * @return this builder.
         * @throws IllegalArgumentException if the name is null or blank, the type is null, or
         *                                  the name is already taken.
         */
        public Builder column(String columnName, ArrowType type, ColumnComment comment)
        {
            requireText(columnName, "Column name");
            if (type == null) {
                throw new IllegalArgumentException(
                        "Column " + name + "." + columnName + " has no Arrow type");
            }
            if (comments.containsKey(columnName) || hasField(columnName)) {
                throw new IllegalArgumentException(
                        "Table " + name + " already has a column named " + columnName);
            }
            // Built here, with a null metadata map, so the one mistake that matters cannot be made:
            // Athena reads comments from the schema's metadata, never the field's.
            fields.add(new Field(columnName, new FieldType(true, type, null), null));
            if (comment != null) {
                String text = comment.build();
                if (!text.isEmpty()) {
                    comments.put(columnName, text);
                }
            }
            return this;
        }

        /**
         * Builds the table schema.
         *
         * @return the immutable result.
         * @throws IllegalStateException if no columns were added.
         */
        public TableSchema build()
        {
            if (fields.isEmpty()) {
                throw new IllegalStateException("Table " + name + " has no columns");
            }
            return new TableSchema(this);
        }

        private boolean hasField(String columnName)
        {
            for (Field field : fields) {
                if (field.getName().equals(columnName)) {
                    return true;
                }
            }
            return false;
        }
    }
}
