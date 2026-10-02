// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.metadata;

import dev.coa.connector.metadata.CoaTable;
import dev.coa.databricks.config.ConnectionConfig;
import dev.coa.databricks.jdbc.DatabricksErrors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.function.Supplier;

/**
 * Reads Unity Catalog's {@code information_schema} for one schema.
 *
 * <p>One instance serves one schema, {@code config.schema()}, so an unpinned connector builds a fresh
 * instance per request through {@link ConnectionConfig#withSchema(String)}. {@link #listSchemas()} is
 * the exception: it is catalog-scoped, and it is what makes the unpinned mode possible.
 *
 * <p>This does not use {@code athena-jdbc}'s metadata handler because that one derives a table's
 * schema from JDBC result-set metadata, which carries neither column comments nor key constraints, and
 * never queries the catalog for them. Comments are the channel declared keys travel through.
 *
 * <p>A connection is opened per public call and closed before returning. Holding one across a Lambda
 * freeze leaves a warehouse session open that nothing will reuse.
 */
public final class InformationSchemaReader
{
    private static final Logger LOGGER = LoggerFactory.getLogger(InformationSchemaReader.class);

    private final ConnectionConfig config;
    private final Supplier<Connection> connections;
    private final int queryTimeoutSeconds;

    /**
     * Seconds a metadata statement may run. Far below the connector's 600 s invocation timeout on
     * purpose: these statements answer in milliseconds, and discovery is a per-table fan-out where a
     * stuck {@code DESCRIBE} should fail fast rather than hold an invocation for minutes.
     */
    public static final int DEFAULT_QUERY_TIMEOUT_SECONDS = 60;

    /**
     * @param connections normally {@code DatabricksConnectionFactory::open}. A supplier rather than the
     *                    factory itself, so a test can hand over a fake connection.
     */
    public InformationSchemaReader(ConnectionConfig config, Supplier<Connection> connections)
    {
        this(config, connections, DEFAULT_QUERY_TIMEOUT_SECONDS);
    }

    /** @param queryTimeoutSeconds per-statement timeout; zero means the driver's own. */
    public InformationSchemaReader(ConnectionConfig config, Supplier<Connection> connections,
                                  int queryTimeoutSeconds)
    {
        this.config = Objects.requireNonNull(config, "config");
        this.connections = Objects.requireNonNull(connections, "connections");
        if (queryTimeoutSeconds < 0) {
            throw new IllegalArgumentException("queryTimeoutSeconds must not be negative");
        }
        this.queryTimeoutSeconds = queryTimeoutSeconds;
    }

    /**
     * The catalog's schemas, alphabetically, excluding {@code information_schema}.
     *
     * <p><b>The only method here safe to call on an unpinned config.</b> Everything else reads
     * {@code config.schema()}, which is null when {@link ConnectionConfig#SCHEMA_VAR} is unset; this
     * reads only {@link ConnectionConfig#catalog()}.
     *
     * @throws com.amazonaws.athena.connector.lambda.exceptions.AthenaConnectorException if the read
     *         fails; the message distinguishes a stopped warehouse from a rejected credential.
     */
    public List<String> listSchemas()
    {
        return read("listing schemas in catalog " + config.catalog(), connection -> {
            List<Object> parameters = new ArrayList<>();
            parameters.add(config.catalog());
            List<String> schemas = new ArrayList<>();
            try (PreparedStatement statement = prepare(
                            connection, InformationSchemaSql.schemata(config.catalog()), parameters);
                    ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    String name = rows.getString("schema_name");
                    if (name != null && !name.trim().isEmpty()) {
                        schemas.add(name);
                    }
                }
            }
            if (schemas.isEmpty()) {
                // Not an error: a catalog can hold nothing but information_schema. But an unpinned
                // connector advertising nothing looks identical to a broken one, and a missing
                // USE SCHEMA grant is the likelier cause than an empty catalog.
                LOGGER.warn("Catalog {} exposes no schemas to this credential, so this connector is"
                                + " advertising none. Check the principal holds USE CATALOG on {} and"
                                + " USE SCHEMA on at least one schema within it.",
                        config.catalog(), config.catalog());
            }
            return Collections.unmodifiableList(schemas);
        });
    }

    /**
     * The tables in the pinned schema, in {@code SHOW TABLES} order, which is alphabetical.
     *
     * <p>Two statements intersected, and both are load-bearing. {@code SHOW TABLES} answers what
     * exists: creating a materialized view or a streaming table also creates internal side tables
     * ({@code __materialization_mat_<uuid>_<name>_1}, {@code event_log_<uuid>}), which
     * {@code information_schema.tables} lists as ordinary {@code MANAGED} tables with nothing in any
     * of its fifteen columns marking them internal, and {@code SHOW TABLES} omits.
     * {@code information_schema.tables} answers what kind each one is, which {@code SHOW TABLES} does
     * not say, and is what excludes a {@code FOREIGN} table and a shallow clone (see
     * {@link TableTypes}). The intersection also drops a session-scoped temporary view, which
     * {@code SHOW TABLES} lists and {@code information_schema} does not.
     *
     * @throws com.amazonaws.athena.connector.lambda.exceptions.AthenaConnectorException if the read
     *         fails; the message distinguishes a stopped warehouse from a rejected credential.
     */
    public List<String> listTables()
    {
        return read("listing tables in " + qualifiedSchema(), connection -> {
            List<String> present = showTables(connection);
            Map<String, String> typesByName = readTableTypes(connection);

            List<String> tables = new ArrayList<>(present.size());
            for (String name : present) {
                String tableType = typesByName.get(name);
                if (tableType == null) {
                    // In SHOW TABLES but not in information_schema: a temporary view, or a race with
                    // a concurrent DROP. Not exposed, and not worth a warning either way.
                    continue;
                }
                if (!TableTypes.allowed().contains(tableType)) {
                    LOGGER.info("Skipping {}.{}: table_type {} is not exposed by this connector",
                            qualifiedSchema(), name, tableType);
                    continue;
                }
                tables.add(name);
            }
            if (tables.isEmpty() && !present.isEmpty()) {
                // Skipping ONE unmatched row unlogged is right; skipping every one is not. A steward
                // would see a source that scanned successfully and contains nothing, which reads as an
                // empty schema rather than a fault. Two routes here: a principal with USE SCHEMA but
                // tighter information_schema.tables filtering, and a driver change to the SHOW TABLES
                // column label.
                LOGGER.warn("{} reported {} object(s) but none survived the table_type allowlist, so"
                                + " this connector is exposing an empty schema. Check the principal"
                                + " holds SELECT on {}, and that these are not all excluded types.",
                        "SHOW TABLES", present.size(), qualifiedSchema());
            }
            return Collections.unmodifiableList(tables);
        });
    }

    /**
     * Reads one table's columns, comments and declared keys, and assembles the schema.
     *
     * <p>Four statements on one connection: the {@code table_type} check, then columns, primary key and
     * foreign keys. Separate rather than one join, because a table with no constraints is the common
     * case and an outer-joined single statement would make every table pay for the constraint views
     * while producing a result set the caller has to de-duplicate row by row.
     *
     * @param tableName the table, as {@link #listTables()} returned it.
     * @throws com.amazonaws.athena.connector.lambda.exceptions.AthenaConnectorException if a read
     *         fails.
     * @throws IllegalArgumentException if the table has no columns, i.e. it does not exist here.
     */
    public CoaTable describeTable(String tableName)
    {
        String table = requireTableName(tableName);
        return read("reading information_schema for " + qualifiedSchema() + "." + table, connection -> {
            requireExposedTableType(connection, table);
            List<ColumnDefinition> columns = readColumns(connection, table);
            DeclaredKeys keys = readKeys(connection, table);
            return TableAssembler.assemble(table, columns, keys);
        });
    }

    /**
     * One table's declared keys. Exposed so a test can assert the constraint reads independently of
     * schema assembly.
     */
    public DeclaredKeys readDeclaredKeys(String tableName)
    {
        String table = requireTableName(tableName);
        return read("reading declared keys for " + qualifiedSchema() + "." + table,
                connection -> readKeys(connection, table));
    }

    /**
     * Refuses a table whose {@code table_type} this connector does not expose.
     *
     * <p>Filtering in {@code listTables} is not enough: Athena calls {@code GetTable} for a name the
     * user typed, not only for names {@code ListTables} returned, so without this check
     * {@code SELECT * FROM cat.sales.orders_clone} against a {@code MANAGED_SHALLOW_CLONE} describes and
     * reads perfectly. {@link #isReferenceable} also drops a foreign key on the premise that an
     * excluded type is absent from the ontology, and a describable, readable table is present.
     *
     * <p>A table with no row here falls through: the columns read produces the better message, which
     * names the table and points at the catalog, schema and grants.
     */
    private void requireExposedTableType(Connection connection, String table) throws SQLException
    {
        List<Object> parameters = new ArrayList<>();
        parameters.add(config.catalog());
        parameters.add(config.schema());
        parameters.add(table);
        String tableType = null;
        try (PreparedStatement statement =
                     prepare(connection, InformationSchemaSql.tableType(config.catalog()), parameters);
                ResultSet rows = statement.executeQuery()) {
            if (rows.next()) {
                tableType = rows.getString("table_type");
            }
        }
        if (tableType != null && !TableTypes.allowed().contains(tableType)) {
            throw new IllegalArgumentException(
                    "Table \"" + table + "\" has table_type " + tableType + ", which this connector"
                            + " does not expose. " + TableTypes.excluded()
                            + " are excluded deliberately: a FOREIGN table's own source should be"
                            + " onboarded directly, and a shallow clone's rows duplicate another"
                            + " table's.");
        }
    }

    /** The names {@code SHOW TABLES} reports, in the order it reports them. */
    private List<String> showTables(Connection connection) throws SQLException
    {
        List<String> names = new ArrayList<>();
        String sql = InformationSchemaSql.showTables(config.catalog(), config.schema());
        try (PreparedStatement statement = prepare(connection, sql, Collections.emptyList());
                ResultSet rows = statement.executeQuery()) {
            while (rows.next()) {
                String name = rows.getString("tableName");
                // A null here would match nothing in the type map, take the skip branch, and turn into
                // an empty schema with no error.
                if (name != null && !name.trim().isEmpty()) {
                    names.add(name);
                }
            }
        }
        return names;
    }

    /** {@code table_name} to {@code table_type}, for every row in the view. */
    private Map<String, String> readTableTypes(Connection connection) throws SQLException
    {
        List<Object> parameters = new ArrayList<>();
        parameters.add(config.catalog());
        parameters.add(config.schema());
        Map<String, String> types = new LinkedHashMap<>();
        try (PreparedStatement statement =
                     prepare(connection, InformationSchemaSql.tableTypes(config.catalog()), parameters);
                ResultSet rows = statement.executeQuery()) {
            while (rows.next()) {
                types.put(rows.getString("table_name"), rows.getString("table_type"));
            }
        }
        return types;
    }

    private List<ColumnDefinition> readColumns(Connection connection, String table)
            throws SQLException
    {
        List<Object> parameters = new ArrayList<>();
        parameters.add(config.catalog());
        parameters.add(config.schema());
        parameters.add(table);
        List<ColumnDefinition> columns = new ArrayList<>();
        try (PreparedStatement statement =
                     prepare(connection, InformationSchemaSql.columns(config.catalog()), parameters);
                ResultSet rows = statement.executeQuery()) {
            while (rows.next()) {
                columns.add(new ColumnDefinition(
                        rows.getString("column_name"),
                        rows.getString("full_data_type"),
                        rows.getString("comment"),
                        ColumnDefinition.Nullability.of(rows.getString("is_nullable"))));
            }
        }
        return columns;
    }

    private DeclaredKeys readKeys(Connection connection, String table) throws SQLException
    {
        List<Object> parameters = new ArrayList<>();
        parameters.add(config.catalog());
        parameters.add(config.schema());
        parameters.add(table);

        DeclaredKeys.Builder keys = DeclaredKeys.builder();
        try (PreparedStatement statement =
                     prepare(connection, InformationSchemaSql.primaryKey(config.catalog()), parameters);
                ResultSet rows = statement.executeQuery()) {
            while (rows.next()) {
                keys.primaryKeyColumn(rows.getString("column_name"));
            }
        }
        try (PreparedStatement statement =
                     prepare(connection, InformationSchemaSql.foreignKeys(config.catalog()), parameters);
                ResultSet rows = statement.executeQuery()) {
            while (rows.next()) {
                String childColumn = rows.getString("child_column");
                String parentCatalog = rows.getString("parent_catalog");
                String parentSchema = rows.getString("parent_schema");
                String parentTable = rows.getString("parent_table");
                String parentTableType = rows.getString("parent_table_type");

                if (!isReferenceable(table, childColumn, parentCatalog, parentSchema, parentTable,
                        parentTableType)) {
                    continue;
                }
                keys.foreignKey(childColumn, parentTable, rows.getString("parent_column"));
            }
        }
        return keys.build();
    }

    /**
     * Whether a declared foreign key's parent is something the {@code @fk} tag can name. The tag is
     * {@code @fk(table.column)} with no slot for a schema, and COA resolves it inside the one schema
     * this connector exposes, so a tag for a parent outside that schema is wrong whenever a table of
     * the same name exists here and dangling otherwise.
     *
     * @return true to emit the reference; false having logged why it was dropped.
     */
    private boolean isReferenceable(String table, String childColumn, String parentCatalog,
                                    String parentSchema, String parentTable, String parentTableType)
    {
        if (!config.catalog().equals(parentCatalog) || !config.schema().equals(parentSchema)) {
            // Dropped even when this connector is UNPINNED and does serve the parent's schema as a
            // separate Athena schema. The limit is the tag's, not the connector's.
            LOGGER.warn("Dropping the declared foreign key on {}.{}.{}: it references {}.{}.{}, which"
                            + " is in a different schema. The @fk tag cannot carry a schema qualifier"
                            + " and COA resolves it within the source's own schema, so emitting it"
                            + " would dangle or assert a relationship to a different table of the"
                            + " same name.",
                    qualifiedSchema(), table, childColumn,
                    parentCatalog, parentSchema, parentTable);
            return false;
        }
        if (parentTableType == null) {
            // The LEFT JOIN found no row in information_schema.tables. Either the parent was dropped
            // between the two reads, or the principal cannot see it, which is likelier: the view is
            // privilege-filtered.
            LOGGER.warn("Dropping the declared foreign key on {}.{}.{}: its parent {} is not visible"
                            + " in information_schema.tables. Check the principal holds SELECT on it.",
                    qualifiedSchema(), table, childColumn, parentTable);
            return false;
        }
        if (!TableTypes.allowed().contains(parentTableType)) {
            LOGGER.warn("Dropping the declared foreign key on {}.{}.{}: its parent {} has table_type"
                            + " {}, which this connector does not expose, so the tag would reference a"
                            + " table absent from the ontology.",
                    qualifiedSchema(), table, childColumn, parentTable, parentTableType);
            return false;
        }
        return true;
    }

    private PreparedStatement prepare(Connection connection, String sql, List<Object> parameters)
            throws SQLException
    {
        PreparedStatement statement = connection.prepareStatement(sql);
        // Closed by hand on a throw. The caller's try-with-resources variable is not assigned until
        // this method RETURNS, so a failure in setQueryTimeout or setString leaves the statement open,
        // bounded only by the connection close a few frames up.
        try {
            if (queryTimeoutSeconds > 0) {
                statement.setQueryTimeout(queryTimeoutSeconds);
            }
            for (int i = 0; i < parameters.size(); i++) {
                statement.setString(i + 1, String.valueOf(parameters.get(i)));
            }
            return statement;
        }
        catch (SQLException | RuntimeException cause) {
            try {
                statement.close();
            }
            catch (SQLException suppressed) {
                cause.addSuppressed(suppressed);
            }
            throw cause;
        }
    }

    /**
     * Opens a connection, runs {@code work}, and turns any driver failure into a classified Athena
     * error. Nothing here logs the SQL: the operation description is what an operator needs, and the
     * statements are pinned by tests.
     */
    private <T> T read(String operation, Work<T> work)
    {
        try (Connection connection = connections.get()) {
            return work.run(connection);
        }
        // SQLException AND RuntimeException: the driver's authentication path throws
        // DatabricksDriverException, which extends RuntimeException, so a catch on SQLException alone
        // lets an OAuth failure escape both classification and redaction. asConnectorFailure is also
        // what keeps this connector's own IllegalArgumentExceptions passing through intact.
        catch (SQLException | RuntimeException cause) {
            throw DatabricksErrors.asConnectorFailure(operation, cause);
        }
    }

    private String qualifiedSchema()
    {
        return config.catalog() + "." + config.schema();
    }

    private static String requireTableName(String tableName)
    {
        if (tableName == null || tableName.trim().isEmpty()) {
            throw new IllegalArgumentException("Table name must not be null or blank");
        }
        return tableName;
    }

    @FunctionalInterface
    private interface Work<T>
    {
        T run(Connection connection) throws SQLException;
    }
}
