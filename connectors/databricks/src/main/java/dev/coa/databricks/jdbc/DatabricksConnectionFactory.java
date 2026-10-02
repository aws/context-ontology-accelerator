// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.jdbc;

import com.amazonaws.athena.connector.credentials.CredentialsProvider;
import com.amazonaws.athena.connector.lambda.exceptions.AthenaConnectorException;
import com.amazonaws.athena.connectors.jdbc.connection.JdbcConnectionFactory;
import dev.coa.connector.metrics.ConnectorMetrics;
import dev.coa.databricks.DatabricksMetadataHandler;
import dev.coa.databricks.config.ConnectionConfig;
import dev.coa.databricks.config.CredentialSource;
import dev.coa.databricks.config.DatabricksCredential;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.Properties;

/**
 * Opens a JDBC connection to one SQL Warehouse.
 *
 * <p>Nothing but the host goes in the URL. The Databricks JDBC URL is
 * {@code jdbc:databricks://host[:port][/path][;prop=value...]}, a {@code ;}-delimited property list, so
 * concatenating configuration into it makes every value a potential property assignment: a {@code ;} in
 * the HTTP path could set {@code SSL=0}, {@code ProxyHost}, or {@code LogPath} with {@code LogLevel=6}
 * to write connection details to the Lambda's disk. The URL this class builds is exactly
 * {@code jdbc:databricks://<host>:443}, and the HTTP path, catalog, schema, credential and every
 * hardening property go on a {@link Properties} object, which the driver reads as a map and never
 * parses. {@link ConnectionConfig}'s patterns are the second layer; this is the first.
 *
 * <p>Four driver defaults are wrong for a Lambda:
 *
 * <table>
 *   <caption>Overridden driver defaults</caption>
 *   <tr><th>Property</th><th>Default</th><th>Here</th><th>Why</th></tr>
 *   <tr><td>{@code TemporarilyUnavailableRetry}</td><td>1</td><td>0</td>
 *       <td>A stopped warehouse makes the driver retry for up to
 *           {@code TemporarilyUnavailableRetryTimeout} = 900 s, outliving even the connector's 600 s
 *           timeout, so the query dies with a timeout instead of a diagnosis. Off gives one attempt and
 *           one clear error, and Athena's own retry then finds a warehouse further along
 *           resuming.</td></tr>
 *   <tr><td>{@code socketTimeout}</td><td>900 s</td><td>540 s</td>
 *       <td>Same reason: a socket timeout above the invocation timeout can never fire.</td></tr>
 *   <tr><td>{@code EnableTelemetry}</td><td>1</td><td>0</td>
 *       <td>The driver reports usage to Databricks. A connector deployed into a customer's account
 *           should not add an unasked-for egress path.</td></tr>
 *   <tr><td>{@code LogLevel}</td><td>OFF</td><td>0 (OFF), pinned</td>
 *       <td>Already the default, pinned because there is no other way to reach it: the driver bundles
 *           its own relocated SLF4J bound to a JUL provider, so {@code simplelogger.properties} cannot
 *           quiet it. Turning logging on is how a connection string containing {@code PWD=} reaches
 *           disk.</td></tr>
 * </table>
 *
 * <p>Thread-safe and stateless past construction. It opens a connection per call and does not pool: a
 * Lambda invocation is short, Athena gives one split per table, and a pool that outlives an invocation
 * holds a warehouse session open across a freeze.
 */
public final class DatabricksConnectionFactory implements JdbcConnectionFactory
{
    /** The only JDBC URL prefix the Databricks driver accepts. */
    private static final String URL_PREFIX = "jdbc:databricks://";

    /** SQL Warehouses listen on 443 and nothing else. */
    private static final int PORT = 443;

    /** Driver class, from the open-source driver's {@code META-INF/services/java.sql.Driver}. */
    static final String DRIVER_CLASS = "com.databricks.client.jdbc.Driver";

    /** Seconds a socket read may block. Below the connector's 600 s invocation timeout. */
    private static final String SOCKET_TIMEOUT_SECONDS = "540";

    /** Seconds the driver may spend retrying a throttled request. */
    private static final String RATE_LIMIT_RETRY_TIMEOUT_SECONDS = "15";

    private final ConnectionConfig config;
    private final CredentialSource credentials;
    private final String url;
    private final ConnectorMetrics metrics;

    public DatabricksConnectionFactory(ConnectionConfig config, CredentialSource credentials)
    {
        this(config, credentials, new ConnectorMetrics(DatabricksMetadataHandler.SOURCE_TYPE));
    }

    /** @param metrics injectable so a test can assert the failure was counted. */
    DatabricksConnectionFactory(ConnectionConfig config, CredentialSource credentials,
                                ConnectorMetrics metrics)
    {
        this.config = Objects.requireNonNull(config, "config");
        this.credentials = Objects.requireNonNull(credentials, "credentials");
        this.url = URL_PREFIX + config.workspaceHostname() + ":" + PORT;
        this.metrics = Objects.requireNonNull(metrics, "metrics");
    }

    /**
     * Opens a connection, already pointed at the configured catalog and schema. The caller closes it.
     *
     * @throws com.amazonaws.athena.connector.lambda.exceptions.AthenaConnectorException with a message
     *         telling a stopped warehouse from a rejected credential from anything else. Never carries
     *         the URL or the credential.
     */
    public Connection open()
    {
        // Resolved before the connection attempt and in its own try, because three non-Databricks faults
        // live in this one call:
        //
        //   - an unusable secret SHAPE, an IllegalArgumentException whose message already names the
        //     secret and the two accepted shapes. Rethrown untouched;
        //   - a credential failure the reader has already classified, which in coa-managed mode says
        //     whether the assume or the read failed and whose policy to look at. Rethrown untouched too:
        //     re-wrapping it would replace CONNECTOR_CREDENTIAL_ASSUME_DENIED with
        //     CONNECTOR_CREDENTIAL_UNREADABLE and send the operator to the connector's own role, which in
        //     that mode holds no Secrets Manager grant to fix;
        //   - a secret that cannot be READ with the connector's own role, for want of
        //     secretsmanager:GetSecretValue or of kms:Decrypt on the customer-managed key. That is what a
        //     first deployment usually hits, and going through classify() would blame the warehouse.
        Properties properties;
        try {
            properties = properties();
        }
        catch (IllegalArgumentException | AthenaConnectorException cause) {
            throw cause;
        }
        catch (RuntimeException cause) {
            throw DatabricksErrors.credentialUnreadable(config.credentialSecretArn(), cause);
        }
        try {
            // Registered by ServiceLoader through META-INF/services/java.sql.Driver, which the shade
            // plugin's ServicesResourceTransformer preserves. Loaded explicitly anyway: lose that file
            // in a repackaging and the failure is "No suitable driver found for jdbc:databricks://..."
            // with no hint at the cause.
            Class.forName(DRIVER_CLASS);
            return DriverManager.getConnection(url, properties);
        }
        catch (ClassNotFoundException cause) {
            throw DatabricksErrors.asConnectorFailure(
                    "loading the Databricks JDBC driver (" + DRIVER_CLASS + ")", cause);
        }
        // SQLException AND RuntimeException: the driver's authentication path throws
        // DatabricksDriverException, which extends RuntimeException, so a catch on SQLException alone
        // lets an OAuth failure escape both classification and redaction.
        catch (SQLException | RuntimeException cause) {
            // No catalog dimension: this runs below the request, so the Athena catalog name every other
            // metric is dimensioned by is not in scope here. The fleet-level dimension set still counts
            // it, and the runbook's first action for this alarm is to read the cold-start log line for
            // the workspace host rather than to look up a catalog.
            metrics.count(ConnectorMetrics.WAREHOUSE_CONNECT_FAILURES, null);
            throw DatabricksErrors.asConnectorFailure(
                    "connecting to the SQL Warehouse at " + config.workspaceHostname(), cause);
        }
    }

    /**
     * {@inheritDoc}
     *
     * <p>The argument is ignored, including when null. It is here because
     * {@code JdbcRecordHandler}'s constructor demands a {@link JdbcConnectionFactory}, and that
     * interface assumes a user-and-password credential resolved by the handler. Databricks has neither:
     * a personal access token goes in {@code PWD} under the fixed user {@code token}, and OAuth M2M has
     * no user. This class resolves its own credential through {@link CredentialSource}, so
     * {@link #open()} is the method to call.
     */
    @Override
    public Connection getConnection(CredentialsProvider credentialsProvider)
    {
        return open();
    }

    /** The JDBC URL: scheme, host, port, and nothing else. */
    public String url()
    {
        return url;
    }

    /**
     * The connection properties, credential included. Package-private and fresh each call, so a test
     * can assert the property spellings without a warehouse and nothing holds a Properties object
     * carrying a credential for longer than a connect.
     */
    Properties properties()
    {
        Properties properties = new Properties();

        // Which warehouse. The driver's own name for it; not on its published properties page.
        properties.setProperty("httpPath", config.httpPath());

        // Namespace pinned on the session, so a table cannot be read out of a catalog this connector
        // does not expose.
        properties.setProperty("ConnCatalog", config.catalog());

        // ConnSchema only when there IS one: an unpinned connector opens a catalog-scoped connection to
        // enumerate schemas and has none to pin yet. Properties.setProperty(key, null) throws NPE, which
        // open() catches as a RuntimeException and reports as CONNECTOR_CREDENTIAL_UNREADABLE, naming
        // Secrets Manager and IAM for a fault that is neither.
        //
        // Safe to omit: every statement InformationSchemaSql builds either qualifies the catalog or
        // binds catalog and schema, so the session default is never consulted.
        if (config.isSchemaPinned()) {
            properties.setProperty("ConnSchema", config.schema());
        }

        for (Map.Entry<String, String> hardening : HARDENING.entrySet()) {
            properties.setProperty(hardening.getKey(), hardening.getValue());
        }

        DatabricksCredential credential = credentials.credentialFor(config);
        credential.applyTo(properties);
        return properties;
    }

    /**
     * Driver settings that do not depend on the configuration. Ordered for readability in a test
     * failure; the driver reads a map, so order carries no meaning.
     */
    private static final Map<String, String> HARDENING;

    static {
        Map<String, String> hardening = new LinkedHashMap<>();
        hardening.put("SSL", "1");
        hardening.put("EnableTelemetry", "0");
        hardening.put("LogLevel", "0");
        hardening.put("TemporarilyUnavailableRetry", "0");
        hardening.put("RateLimitRetryTimeout", RATE_LIMIT_RETRY_TIMEOUT_SECONDS);
        hardening.put("socketTimeout", SOCKET_TIMEOUT_SECONDS);
        // Complex columns arrive as their string rendering, which is what this connector's type mapping
        // expects: BlockUtils has no case for a struct, list or map vector, so a complex column served
        // as anything else fails at read time with "Unknown type Struct".
        hardening.put("EnableComplexDatatypeSupport", "0");
        // Makes setAutoCommit, commit and rollback no-ops.
        //
        // The inherited read loop calls both, for Databricks. The guard in
        // JdbcRecordHandler.readWithConstraint (2026.33.1) is NEGATED:
        //
        //     // clickhouse does not support disabling auto-commit
        //     if (!CLICKHOUSE_DB.equalsIgnoreCase(databaseProductName)) {
        //         connection.setAutoCommit(false);
        //     }
        //
        // so the block is skipped only for ClickHouse and every other engine runs it, commit() included
        // under the same guard later in the method. Confirmed in the bytecode: "ifne" jumps PAST the
        // setAutoCommit call when equalsIgnoreCase("clickhouse") returns true.
        //
        // Neither call buys anything here. They exist for engines that need cursor-based streaming, and
        // the Databricks driver streams in Arrow chunks regardless. Without this flag each one executes
        // a real statement on the warehouse, two extra round trips per read, against a warehouse whose
        // transaction support depends on its runtime version.
        //
        // The driver marks the flag deprecated. If a release removes it, the driver's own setAutoCommit
        // path takes over and the reads still work, with those two round trips back.
        hardening.put("IgnoreTransactions", "1");
        hardening.put("UserAgentEntry", "COA-Databricks-Connector");
        HARDENING = Collections.unmodifiableMap(hardening);
    }
}
