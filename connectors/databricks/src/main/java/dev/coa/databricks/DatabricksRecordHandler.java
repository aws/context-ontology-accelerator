// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import com.amazonaws.athena.connector.lambda.QueryStatusChecker;
import com.amazonaws.athena.connector.lambda.data.BlockSpiller;
import com.amazonaws.athena.connector.lambda.domain.Split;
import com.amazonaws.athena.connector.lambda.domain.TableName;
import com.amazonaws.athena.connector.lambda.domain.predicate.Constraints;
import com.amazonaws.athena.connector.lambda.records.ReadRecordsRequest;
import com.amazonaws.athena.connectors.jdbc.connection.DatabaseConnectionConfig;
import com.amazonaws.athena.connectors.jdbc.manager.JdbcRecordHandler;
import dev.coa.connector.metrics.ConnectorMetrics;
import dev.coa.databricks.config.ConnectionConfig;
import dev.coa.databricks.config.ConnectionConfigProvider;
import dev.coa.databricks.config.ConnectionConfigProviders;
import dev.coa.databricks.config.CredentialSource;
import dev.coa.databricks.jdbc.CatalogRoutingConnectionFactory;
import dev.coa.databricks.jdbc.DatabricksErrors;
import org.apache.arrow.vector.types.pojo.Schema;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import software.amazon.awssdk.services.athena.AthenaClient;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.secretsmanager.SecretsManagerClient;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.function.Supplier;

/**
 * The record half: streams rows for one split, as Arrow.
 *
 * <p>{@link JdbcRecordHandler} supplies the read loop and {@code makeExtractor}, a typed extractor per
 * projected column for eleven Arrow types, with two corrections that are only obvious once they have
 * bitten: a date before 1970 is off by one if read as millis, and {@code FLOAT8} sometimes arrives as a
 * currency-formatted string. The loop honours schema projection (writing a column absent from the
 * request's schema throws inside {@code BlockUtils.setValue}, and unprojected queries pass, so the
 * mistake surfaces late) and checks for query cancellation between rows.
 * {@link DatabricksQueryBuilder} supplies the statement. This class supplies configuration, the
 * connection, the row ceiling, error classification, and the pinned-schema check that keeps the schema
 * pin a boundary on this path too ({@link #requireServableSchema}).
 *
 * <p><b>Everything endpoint-shaped is resolved per request, not at construction</b> — the
 * {@code DatabaseConnectionConfig} handed to {@code super}, the Unity Catalog catalog in the query
 * builder's {@code FROM} clause, and the JDBC URL — because one deployment serves every Databricks source
 * in a COA deployment and the only thing that says which source a request belongs to is the Athena catalog
 * name on the request itself. {@link CatalogRoutingConnectionFactory} resolves the connection per request
 * and a query builder is held per Unity Catalog catalog, so nothing endpoint-specific is shared across
 * catalogs.
 */
public class DatabricksRecordHandler extends JdbcRecordHandler
{
    private static final Logger LOGGER = LoggerFactory.getLogger(DatabricksRecordHandler.class);

    /**
     * Seconds a row-reading statement may run. Below the connector's 600 s invocation timeout, leaving
     * room for JVM init, the spill flush and the response.
     */
    static final int QUERY_TIMEOUT_SECONDS = 540;

    /**
     * The catalog name on the {@code DatabaseConnectionConfig} handed to {@code super}.
     *
     * <p>An inert placeholder, spelled to say so: audited in the 2026.33.1 bytecode, {@code catalog} and
     * {@code jdbcConnectionString} are read only by {@code JDBCUtil}, which serves the multiplexing
     * handler this connector does not use, so <b>nothing</b> on any path it takes reads them, and in
     * {@code coa-managed} mode no single catalog or URL would be true. Neither carries a host, a
     * credential or a predicate, so both are safe even in a log line.
     */
    private static final String UNUSED_CATALOG_NAME = "resolved-per-request";

    /** See {@link #UNUSED_CATALOG_NAME}. Read by nothing; deliberately not a valid JDBC URL. */
    private static final String UNUSED_JDBC_URL = "jdbc:databricks://resolved-per-request";

    private final ConnectionConfigProvider configs;
    private final CatalogRoutingConnectionFactory connections;
    private final long maxRowsPerTable;
    private final ConnectorMetrics metrics;

    /**
     * One query builder per <b>Unity Catalog</b> catalog, since that is all a builder depends on: it
     * spells {@code FROM `ucCatalog`.`schema`.`table`} and inherits everything else.
     *
     * <p>Keyed on the Unity Catalog name rather than the Athena one, so two Athena catalogs resolving the
     * same Unity Catalog catalog share a builder. A builder cannot itself cross a tenant boundary: schema
     * and table come from the request, the catalog from the configuration resolved for it.
     */
    private final ConcurrentMap<String, DatabricksQueryBuilder> queryBuilders =
            new ConcurrentHashMap<>();

    /**
     * A record-only Lambda's entry point, which resolves everything from the environment itself.
     *
     * @throws IllegalArgumentException if the environment is missing, invalid, or describes both
     *                                 configuration modes at once, naming what is wrong. Thrown during
     *                                 initialisation so a misconfigured deployment fails once rather
     *                                 than per request.
     */
    public DatabricksRecordHandler(Map<String, String> configOptions)
    {
        this(configOptions, ConnectionConfigProviders.fromEnvironment(configOptions));
    }

    /**
     * The constructor {@link DatabricksCompositeHandler} uses, so one container holds one of each cache.
     *
     * @param wiring the provider and the credential path the environment described, from one read of the
     *               mode. Taken as a pair so nothing here can combine a provider with a credential path
     *               the mode did not choose.
     */
    public DatabricksRecordHandler(Map<String, String> configOptions,
                                   ConnectionConfigProviders.Wiring wiring)
    {
        this(configOptions, wiring.provider(), wiring.credentials());
    }

    /**
     * @param configs     resolves the endpoint for the catalog a request arrived under.
     * @param credentials resolves that endpoint's credential, by whichever route it was built with.
     */
    DatabricksRecordHandler(Map<String, String> configOptions,
                            ConnectionConfigProvider configs,
                            CredentialSource credentials)
    {
        this(configOptions, configs,
                new CatalogRoutingConnectionFactory(configs, credentials));
    }

    /**
     * The constructor a test uses: it supplies the connections directly, so the record path is reachable
     * without a warehouse.
     *
     * @param connections opens a connection for a resolved configuration.
     */
    DatabricksRecordHandler(Map<String, String> configOptions,
                            ConnectionConfigProvider configs,
                            java.util.function.Function<ConnectionConfig, Supplier<Connection>>
                                    connections)
    {
        this(configOptions, configs, connections,
                new ConnectorMetrics(DatabricksMetadataHandler.SOURCE_TYPE));
    }

    /**
     * As above, with the metric emitter injected.
     *
     * @param metrics where {@link ConnectorMetrics#ROWS_RETURNED} and
     *                {@link ConnectorMetrics#TABLE_CEILING_EXCEEDED} go.
     */
    DatabricksRecordHandler(Map<String, String> configOptions,
                            ConnectionConfigProvider configs,
                            java.util.function.Function<ConnectionConfig, Supplier<Connection>>
                                    connections,
                            ConnectorMetrics metrics)
    {
        this(configOptions, configs, new CatalogRoutingConnectionFactory(configs, connections),
                metrics);
    }

    /**
     * The constructor that calls {@code super}. Split out because the routing factory has to exist before
     * the {@code super(...)} expression.
     */
    private DatabricksRecordHandler(Map<String, String> configOptions,
                                    ConnectionConfigProvider configs,
                                    CatalogRoutingConnectionFactory connections)
    {
        this(configOptions, configs, connections,
                new ConnectorMetrics(DatabricksMetadataHandler.SOURCE_TYPE));
    }

    private DatabricksRecordHandler(Map<String, String> configOptions,
                                    ConnectionConfigProvider configs,
                                    CatalogRoutingConnectionFactory connections,
                                    ConnectorMetrics metrics)
    {
        // The six-argument constructor is the only one that sets the connection factory the inherited
        // read loop uses; the shorter one is for the multiplexing handler and leaves it null.
        super(S3Client.create(),
                SecretsManagerClient.create(),
                AthenaClient.create(),
                // The three-argument DatabaseConnectionConfig, with no secret name. With one, the base
                // class resolves it into a user-and-password pair the Databricks driver cannot use.
                new DatabaseConnectionConfig(
                        UNUSED_CATALOG_NAME,
                        DatabricksMetadataHandler.SOURCE_TYPE,
                        UNUSED_JDBC_URL),
                connections,
                configOptions);
        this.configs = configs;
        this.connections = connections;
        this.maxRowsPerTable = Settings.maxRowsPerTable(configOptions);
        this.metrics = metrics;
        // Asked of the provider rather than resolved here: in coa-managed mode no catalog name exists yet,
        // so resolving one would mean picking a tenant's at random.
        LOGGER.info("Databricks record handler ready: {}", configs.describe());
    }

    /**
     * {@inheritDoc}
     *
     * <p>Wraps the spiller so the inherited loop is bounded, binds this request's catalog so the loop's
     * connection comes from the right warehouse, then hands off. A driver failure is classified on the
     * way out, so a stopped warehouse reads as one rather than as a generic query error.
     *
     * <p>The pin is checked here, before anything opens a connection, and again in
     * {@link #buildSplitSql}.
     */
    @Override
    public void readWithConstraint(BlockSpiller spiller, ReadRecordsRequest request,
                                   QueryStatusChecker queryStatusChecker)
            throws Exception
    {
        String catalog = request.getCatalogName();
        
        ConnectionConfig config = configs.configFor(catalog);
        requireServableSchema(config, request.getTableName());
        String table = request.getTableName().getTableName();
        RowCeilingSpiller bounded = new RowCeilingSpiller(spiller, table, maxRowsPerTable);
        // The binding's extent is exactly the inherited loop's, which is where it calls its connection
        // factory. See CatalogRoutingConnectionFactory for why the catalog travels this way.
        try (CatalogRoutingConnectionFactory.Binding bound = connections.bind(catalog)) {
            super.readWithConstraint(bounded, request, queryStatusChecker);
        }
        catch (SQLException | RuntimeException cause) {
            // The spiller counts the row that breached before refusing it, so the comparison identifies
            // a ceiling breach exactly. Matching on the exception's message would work today and break
            // the first time that message is reworded.
            if (bounded.rowsWritten() > maxRowsPerTable) {
                metrics.count(ConnectorMetrics.TABLE_CEILING_EXCEEDED, catalog);
            }
            throw DatabricksErrors.asConnectorFailure(
                    "reading rows from " + qualified(config, request.getTableName()), cause);
        }
        metrics.emit(ConnectorMetrics.ROWS_RETURNED, bounded.rowsWritten(),
                ConnectorMetrics.UNIT_COUNT, catalog);
        // Row count only. No predicate value, no SQL, no identifier beyond the table's name.
        LOGGER.info("Read {} rows from {}", bounded.rowsWritten(), table);
    }

    /**
     * {@inheritDoc}
     *
     * @param catalogName Athena's catalog name. The query builder substitutes the Unity Catalog catalog
     *                    for it, but it is what resolves the configuration this request is served from, so
     *                    it is load-bearing rather than ignored.
     */
    @Override
    public PreparedStatement buildSplitSql(Connection jdbcConnection, String catalogName,
                                           TableName tableName, Schema schema,
                                           Constraints constraints, Split split)
            throws SQLException
    {
        // Also checked in readWithConstraint, which is earlier and cheaper. Repeated here because this is
        // the method that turns a request into SQL, and it is public: an inherited read loop, a
        // multiplexing handler or a later override reaches it without going through the other one.
        ConnectionConfig config = configs.configFor(catalogName);
        requireServableSchema(config, tableName);
        PreparedStatement statement = queryBuilderFor(config).buildSql(
                jdbcConnection,
                catalogName,
                tableName.getSchemaName(),
                tableName.getTableName(),
                schema,
                constraints,
                split);
        statement.setQueryTimeout(QUERY_TIMEOUT_SECONDS);
        return statement;
    }

    /** The builder for {@code config}'s Unity Catalog catalog, made once per container per catalog. */
    private DatabricksQueryBuilder queryBuilderFor(ConnectionConfig config)
    {
        // computeIfAbsent is safe here, unlike for the caches that make a network call: the mapping
        // function is a constructor over a string.
        return queryBuilders.computeIfAbsent(config.catalog(), DatabricksQueryBuilder::new);
    }

    /**
     * Refuses a read of a schema a pinned connector does not serve.
     *
     * <p>The schema pin is a containment boundary independent of the credential's Unity Catalog grants, and
     * without this check it is one only on the metadata path: Athena reaches a read through
     * {@code GetTable}, which the metadata handler refuses first, but a principal holding
     * {@code lambda:InvokeFunction} can post a hand-built {@code ReadRecordsRequest} naming any schema, and
     * every statement this connector builds takes the schema from the request.
     *
     * <p>In {@code coa-managed} mode the configuration is <b>always</b> pinned — a managed source is
     * exactly one Unity Catalog schema — so this check is unconditional there, and it is the boundary
     * that keeps one source's catalog from reading another schema in the same Unity Catalog catalog even
     * where the credential's own grants would allow it.
     *
     * <p>Nothing to check when the connector is unpinned, which only a stage-1 deployment can be: there
     * the credential's grants are the boundary, and the metadata handler's own gate decides which schemas
     * are addressable.
     *
     * <p>Static, and taking the configuration rather than reading a field, so it is testable: this class
     * builds three AWS clients in its constructor.
     *
     * @throws IllegalArgumentException if {@code config} is pinned to another schema.
     */
    static void requireServableSchema(ConnectionConfig config, TableName tableName)
    {
        if (!config.isSchemaPinned()) {
            return;
        }
        String schema = tableName.getSchemaName();
        if (!config.schema().equals(schema)) {
            throw new IllegalArgumentException(
                    "Refusing to read \"" + schema + "." + tableName.getTableName() + "\". This"
                            + " connector serves schema \"" + config.schema() + "\" in catalog \""
                            + config.catalog() + "\" for this Athena catalog, and that pin bounds the"
                            + " record path as well as the metadata path. " + fixFor(config));
        }
    }

    /**
     * How to widen the pin, which differs by mode. In {@code environment} mode the pin is a variable an
     * operator set and can unset; in {@code coa-managed} mode it is the source's own schema, and that
     * variable does not exist in such a deployment at all.
     */
    private static String fixFor(ConnectionConfig config)
    {
        if (config.isCoaManaged()) {
            return "A COA-managed source is exactly one Unity Catalog schema, so this is not a"
                    + " deployment setting: register a second source for \"" + config.catalog()
                    + "\" if that schema should be readable too.";
        }
        return "Unset " + ConnectionConfig.SCHEMA_VAR + " to serve every schema in the catalog, or"
                + " deploy a second connector for the other schema.";
    }

    /**
     * The Unity Catalog three-part name, for an error message.
     *
     * <p>Takes the configuration rather than resolving one, so that it cannot fail. It is called from a
     * {@code catch} block, where a throw would discard the exception being reported — see
     * {@link #readWithConstraint}.
     */
    private static String qualified(ConnectionConfig config, TableName tableName)
    {
        return config.catalog() + "." + tableName.getSchemaName() + "." + tableName.getTableName();
    }
}
