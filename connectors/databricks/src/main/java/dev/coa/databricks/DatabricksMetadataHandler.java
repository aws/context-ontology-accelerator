// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import com.amazonaws.athena.connector.lambda.data.BlockAllocator;
import com.amazonaws.athena.connector.lambda.domain.Split;
import com.amazonaws.athena.connector.lambda.metadata.GetDataSourceCapabilitiesRequest;
import com.amazonaws.athena.connector.lambda.metadata.GetDataSourceCapabilitiesResponse;
import com.amazonaws.athena.connector.lambda.metadata.GetSplitsRequest;
import com.amazonaws.athena.connector.lambda.metadata.GetSplitsResponse;
import dev.coa.connector.metadata.CoaMetadataHandler;
import dev.coa.connector.metadata.CoaTable;
import dev.coa.connector.metrics.ConnectorMetrics;
import dev.coa.databricks.config.ConnectionConfig;
import dev.coa.databricks.config.ConnectionConfigProvider;
import dev.coa.databricks.config.ConnectionConfigProviders;
import dev.coa.databricks.config.CredentialSource;
import dev.coa.databricks.config.ExpiringCache;
import dev.coa.databricks.jdbc.DatabricksConnectionFactory;
import dev.coa.databricks.metadata.InformationSchemaReader;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.function.Supplier;

/**
 * The metadata half: one Athena schema, its tables, and each table's columns, comments and declared
 * keys.
 *
 * <p>This extends the COA toolkit's {@link CoaMetadataHandler} while {@link DatabricksRecordHandler}
 * extends {@code athena-jdbc}'s record handler, since a Java class cannot do both. The toolkit supplies the
 * comment builder, the {@code @pk}/{@code @notnull}/{@code @fk} encoder, and the Arrow-schema placement
 * that makes a comment reach Athena at all.
 *
 * <p>A {@link ConnectionConfigProvider} resolves the configuration for the Athena catalog name each request
 * arrives under, so every method here resolves per request. The cold-start log line asks the provider to
 * {@link ConnectionConfigProvider#describe()} itself, because a cold start happens before any catalog name
 * exists and naming an endpoint would mean picking a tenant's at random.
 *
 * <p>Unity Catalog addresses {@code catalog.schema.table}, and an Athena federated catalog has one level
 * left below the registered catalog name. This connector spends it on the Unity Catalog schema and takes
 * the catalog from the resolved configuration. A schema-pinned configuration (always, for a COA-managed
 * source; otherwise {@code DATABRICKS_SCHEMA}) exposes that one schema and refuses every other name, a
 * containment boundary independent of the credential's Unity Catalog grants. Unpinned, it enumerates the
 * catalog's addressable schemas from {@code information_schema} and those grants are the only boundary.
 * What it advertises and what it serves are the same set either way, see {@link #servable}. The record half
 * enumerates nothing but enforces the pin as well, so the boundary does not depend on Athena having called
 * {@code GetTable} first.
 *
 * <p>{@link #doGetSplits} emits a split with no properties. The toolkit's default puts the table name on it
 * under the key {@code "table"}, and {@code athena-jdbc}'s query builder reads every split property as a
 * partition value: it drops those names from the projection, skips their constraints, and feeds the split's
 * value to the extractor instead of the result set. A Databricks table with a column named {@code table}
 * would silently return the literal string {@code orders} in it for every row. The table name is already on
 * the request.
 */
public class DatabricksMetadataHandler extends CoaMetadataHandler
{
    private static final Logger LOGGER = LoggerFactory.getLogger(DatabricksMetadataHandler.class);

    /**
     * Short name for the source type; the SDK uses it in metrics and log lines, and it is the
     * {@code Connector} dimension every metric this connector emits carries.
     *
     * <p>Public because the connection factory needs it for that dimension and sits in a sub-package.
     */
    public static final String SOURCE_TYPE = "databricks";

    private final ConnectionConfigProvider configs;

    /**
     * Opens connections for a configuration. The module's containment boundary runs through
     * {@link #readerFor}, so this is a field rather than a {@code new} expression inside it: with the
     * factory constructed inline, {@code listTables} and {@code describeTable} could only be exercised
     * against a live warehouse, and the connector has no integration suite.
     */
    private final Function<ConnectionConfig, Supplier<Connection>> connections;

    /** The advertised schema list per configuration, for an unpinned connector. */
    private final ExpiringCache<ConnectionConfig, List<String>> servableSchemas;

    /**
     * The container's credential cache, shared with the record half: one container, one cache, one assumed
     * session per role, and one {@code GetSecretValue} per source per TTL window.
     */
    private final CredentialSource credentials;

    /**
     * A metadata-only Lambda's entry point, which resolves everything from the environment itself.
     *
     * @throws IllegalArgumentException if the environment is missing or invalid, naming the variable.
     *                                 Thrown during initialisation so a misconfigured connector fails
     *                                 once, loudly, rather than once per request.
     */
    public DatabricksMetadataHandler(Map<String, String> configOptions)
    {
        this(configOptions, ConnectionConfigProviders.fromEnvironment(configOptions));
    }

    /**
     * @param wiring the provider and the credential path the environment described, from one read of the
     *               mode. Taken as a pair so nothing here can combine a provider with a credential path
     *               the mode did not choose, and so both halves of a container share one of each cache.
     */
    public DatabricksMetadataHandler(Map<String, String> configOptions,
                                     ConnectionConfigProviders.Wiring wiring)
    {
        this(configOptions, wiring.provider(), wiring.credentials(), null,
                CredentialSource.DEFAULT_TTL_MILLIS);
    }

    /**
     * The constructor a test uses.
     *
     * @param connections            opens connections for a resolved configuration, or null for the real
     *                               one, which needs {@code this.credentials}.
     * @param schemaCacheTtlMillis   how long an unpinned connector may reuse an enumerated schema list.
     *                               Zero disables the cache, which is what a test asserting the
     *                               enumeration happened wants.
     */
    DatabricksMetadataHandler(Map<String, String> configOptions,
                              ConnectionConfigProvider configs,
                              CredentialSource credentials,
                              Function<ConnectionConfig, Supplier<Connection>> connections,
                              long schemaCacheTtlMillis)
    {
        super(SOURCE_TYPE, configOptions);
        this.configs = configs;
        this.credentials = credentials;
        this.connections = (connections != null)
                ? connections
                : config -> new DatabricksConnectionFactory(config, this.credentials)::open;
        this.servableSchemas = new ExpiringCache<>(schemaCacheTtlMillis);

        // Asked of the provider rather than resolved here: a cold start happens before any catalog name
        // exists, and in coa-managed mode naming an endpoint would mean picking a tenant's at random.
        LOGGER.info("Databricks connector ready: {} pushdown={}", configs.describe(),
                PushdownCapabilities.ADVERTISED.keySet());
    }

    /**
     * The pinned Unity Catalog schema alone, or, when unpinned, those of the catalog's schemas this
     * connector can also serve.
     */
    @Override
    protected List<String> listDatabases(String catalog)
    {
        ConnectionConfig config = configs.configFor(catalog);
        if (config.isSchemaPinned()) {
            // No connection needed: the pin is the answer, and a warehouse round-trip could only agree
            // with it or fail.
            return Collections.singletonList(config.schema());
        }
        return advertisedSchemas(config);
    }

    /**
     * The schema's tables, allowlisted {@code table_type} values only.
     *
     * @throws IllegalArgumentException if {@code database} is not a schema this connector exposes.
     */
    @Override
    protected List<String> listTables(String catalog, String database)
    {
        return readerFor(catalog, database).listTables();
    }

    /**
     * One table's columns, types, prose, nullability and declared keys. The toolkit turns those into
     * {@code @pk}/{@code @notnull}/{@code @fk} tags and puts them where Athena reads them.
     */
    @Override
    protected CoaTable describeTable(String catalog, String database, String tableName)
    {
        return readerFor(catalog, database).describeTable(tableName);
    }

    /**
     * {@inheritDoc}
     *
     * <p>One split with no properties, since {@code athena-jdbc}'s query builder reads every split
     * property as a partition column.
     */
    @Override
    public GetSplitsResponse doGetSplits(BlockAllocator allocator, GetSplitsRequest request)
    {
        Split split = Split.newBuilder(makeSpillLocation(request), makeEncryptionKey()).build();
        return new GetSplitsResponse(request.getCatalogName(), split);
    }

    /**
     * {@inheritDoc}
     *
     * <p>The map is what lets Athena stop re-applying a predicate, a limit or a top-N it has already sent.
     * {@link PushdownCapabilities} carries the measurement behind each of the three.
     */
    @Override
    public GetDataSourceCapabilitiesResponse doGetDataSourceCapabilities(
            BlockAllocator allocator, GetDataSourceCapabilitiesRequest request)
    {
        return new GetDataSourceCapabilitiesResponse(request.getCatalogName(),
                PushdownCapabilities.ADVERTISED);
    }

    /**
     * A reader scoped to {@code database}, after checking this connector will serve it. Athena calls
     * {@code GetTable} for a name the user typed, so without the check
     * {@code SELECT * FROM cat.made_up.t} is answered from whichever schema the config happens to name,
     * reading as though {@code made_up} existed.
     *
     * @throws IllegalArgumentException if it will not serve {@code database}. Three messages, because
     *                                 the fixes differ: a pinned connector asked for another schema is a
     *                                 deployment decision, an unpinned one asked for a name Athena
     *                                 cannot address is a schema that needs renaming or fronting with a
     *                                 view, and an unpinned one asked for a schema that does not exist
     *                                 is a typo or a missing grant.
     */
    private InformationSchemaReader readerFor(String athenaCatalog, String database)
    {
        ConnectionConfig config = configs.configFor(athenaCatalog);

        if (config.isSchemaPinned()) {
            if (!config.schema().equals(database)) {
                throw new IllegalArgumentException(
                        "Unknown schema: \"" + database + "\". This connector serves \""
                                + config.schema() + "\" for this Athena catalog and no other schema. "
                                + widenThePin(config, database));
            }
            return schemaScopedReader(config);
        }

        // Checked before the catalog is asked, so a name this connector could never address is refused
        // by shape rather than by absence. Unity Catalog allows a quoted name containing almost
        // anything, and Athena cannot address one on both its parsers, so such a schema is neither
        // advertised nor served; saying so is the only message that names the actual obstacle.
        if (!ConnectionConfig.isServableSchemaName(database)) {
            throw new IllegalArgumentException(
                    "Unknown schema: \"" + database + "\". Its name is not a bare SQL identifier, so"
                            + " this connector neither advertises nor serves it, whether or not catalog"
                            + " \"" + config.catalog() + "\" contains it: Athena parses SHOW/DESCRIBE"
                            + " and SELECT with different quoting rules, so a name needing quotes"
                            + " cannot be addressed on both paths. Expose the tables through a schema"
                            + " whose name is a bare identifier, or pin a connector per schema.");
        }

        // Unpinned: the catalog decides what exists. Enumerated rather than probed, so the failure can
        // list the alternatives. A schema name is a coordinate, not a secret, and the principal can
        // already see every name this returns.
        List<String> available = advertisedSchemas(config);
        if (!available.contains(database)) {
            throw new IllegalArgumentException(
                    "Unknown schema: \"" + database + "\". Catalog \"" + config.catalog()
                            + "\" exposes " + describe(available) + ". Check the spelling, or that"
                            + " the connector's principal holds USE SCHEMA on it — a schema the"
                            + " credential cannot see is indistinguishable from one that does not"
                            + " exist.");
        }
        return schemaScopedReader(config.withSchema(database));
    }

    /**
     * How to make {@code database} readable, which differs by mode.
     *
     * <p>Naming {@link ConnectionConfig#SCHEMA_VAR} is the actionable answer for a stage-1 deployment,
     * where it is set and unsetting it is one of the two fixes. It is the wrong answer for a COA-managed
     * source: that variable is absent from such a deployment by construction — the mode switch refuses to
     * start if it is present alongside the others — so pointing an operator at it sends them looking for
     * something that is not there, and the real fix is a second source record.
     */
    private static String widenThePin(ConnectionConfig config, String database)
    {
        if (config.isCoaManaged()) {
            return "A COA-managed source is exactly one Unity Catalog schema, so this is not a"
                    + " deployment setting to change: register a second source for \"" + database
                    + "\" in catalog \"" + config.catalog() + "\" if it should be readable too.";
        }
        return "Unset " + ConnectionConfig.SCHEMA_VAR + " to serve every schema in catalog \""
                + config.catalog() + "\", or deploy a second connector for \"" + database + "\".";
    }

    /**
     * The schemas an unpinned connector advertises for {@code config}, which are exactly the ones it
     * will serve.
     *
     * <p>Cached per container, keyed on the configuration rather than the Athena catalog name: a
     * multiplexed provider hands out a different configuration per catalog, and two of them must not
     * share a list. This is read on every {@code ListTables} and {@code GetTable} as well as
     * on {@code ListSchemas}, and discovery is a per-table {@code GetTable} fan-out, so without the
     * cache a 200-table schema pays 201 metastore-wide {@code information_schema.schemata} scans and
     * 201 extra connections for one enumeration that does not change between them.
     */
    private List<String> advertisedSchemas(ConnectionConfig config)
    {
        return servableSchemas.get(config,
                () -> servable(catalogScopedReader(config).listSchemas(), config.catalog()));
    }

    /**
     * The subset of {@code discovered} this connector can address, each rejection logged with its
     * reason.
     *
     * <p>Static and taking the list rather than fetching it, so the rule that decides what is advertised
     * is testable without a warehouse. The rule has to be the one {@link ConnectionConfig#withSchema}
     * applies, or the connector advertises a schema and then refuses every request against it, blaming
     * {@link ConnectionConfig#SCHEMA_VAR} — which an unpinned deployment never set.
     *
     * @param discovered  the names {@link InformationSchemaReader#listSchemas()} returned.
     * @param unityCatalog the catalog they came from, for the log lines.
     */
    static List<String> servable(List<String> discovered, String unityCatalog)
    {
        List<String> servable = new ArrayList<>(discovered.size());
        for (String name : discovered) {
            if (ConnectionConfig.isServableSchemaName(name)) {
                servable.add(name);
                continue;
            }
            // WARN, not INFO: from the operator's side this is a schema that has gone missing from a
            // source they expected to see, and nothing else says why.
            LOGGER.warn("Not advertising schema {}.{}: its name is not a bare SQL identifier, and"
                            + " Athena cannot address it on both its SHOW/DESCRIBE and SELECT parsers,"
                            + " so a source onboarded against it could be listed and never read. Rename"
                            + " it, or expose its tables through views in a schema whose name is one.",
                    unityCatalog, name);
        }
        if (servable.isEmpty() && !discovered.isEmpty()) {
            // Every name refused. One refusal is a data-modelling choice; all of them looks from the
            // outside like an empty catalog or a broken connector.
            LOGGER.warn("Catalog {} exposes {} schema(s) to this credential and none of their names is"
                            + " a bare SQL identifier, so this connector is advertising none.",
                    unityCatalog, discovered.size());
        }
        return Collections.unmodifiableList(servable);
    }

    /** A reader for {@code config}'s own schema. {@code config} has to be pinned. */
    private InformationSchemaReader schemaScopedReader(ConnectionConfig config)
    {
        return new InformationSchemaReader(config, connections.apply(config));
    }

    /**
     * A reader for {@link InformationSchemaReader#listSchemas()} only, which is the one method on that
     * class safe to call on an unpinned config.
     */
    private InformationSchemaReader catalogScopedReader(ConnectionConfig config)
    {
        return new InformationSchemaReader(config, connections.apply(config));
    }

    /** The names quoted and comma-separated, or a phrase saying there are none. */
    private static String describe(List<String> schemas)
    {
        if (schemas.isEmpty()) {
            return "no schemas this credential can see";
        }
        return "\"" + String.join("\", \"", schemas) + "\"";
    }

}
