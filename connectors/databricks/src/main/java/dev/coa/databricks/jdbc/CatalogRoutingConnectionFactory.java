// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.jdbc;

import com.amazonaws.athena.connector.credentials.CredentialsProvider;
import com.amazonaws.athena.connectors.jdbc.connection.JdbcConnectionFactory;
import dev.coa.databricks.config.ConnectionConfig;
import dev.coa.databricks.config.ConnectionConfigProvider;
import dev.coa.databricks.config.CredentialSource;

import java.sql.Connection;
import java.util.Objects;
import java.util.function.Function;
import java.util.function.Supplier;

/**
 * Opens a connection to whichever warehouse the request being served belongs to, rather than to one
 * warehouse chosen at construction.
 *
 * <p>{@code JdbcRecordHandler} fixes its {@link JdbcConnectionFactory} at construction and calls
 * {@code getConnection} per request without passing anything that identifies the request, so the catalog
 * name travels across {@code super.readWithConstraint} in a {@link ThreadLocal}, bound by {@link #bind}
 * and released in the same statement's {@code finally}. {@link #getConnection} refuses an unbound call
 * rather than picking an endpoint, because the alternative is serving some tenant's warehouse.
 *
 * <p>Thread-safe. Why a ThreadLocal and not a field: {@code connectors/databricks/DESIGN.md}, "The seam
 * the inherited read loop forced".
 */
public final class CatalogRoutingConnectionFactory implements JdbcConnectionFactory
{
    private final ConnectionConfigProvider configs;
    private final Function<ConnectionConfig, Supplier<Connection>> connections;

    /**
     * The catalog the invocation currently on this thread is serving. Never read outside the extent of a
     * {@link #bind}, and never written by anything but it.
     */
    private final ThreadLocal<String> boundCatalog = new ThreadLocal<>();

    public CatalogRoutingConnectionFactory(ConnectionConfigProvider configs,
                                          CredentialSource credentials)
    {
        this(configs, factoryFor(Objects.requireNonNull(credentials, "credentials")));
    }

    /**
     * @param connections opens connections for a resolved configuration. A function rather than the
     *                   factory itself, so a test can hand over a fake connection.
     */
    public CatalogRoutingConnectionFactory(ConnectionConfigProvider configs,
                                           Function<ConnectionConfig, Supplier<Connection>> connections)
    {
        this.configs = Objects.requireNonNull(configs, "configs");
        this.connections = Objects.requireNonNull(connections, "connections");
    }

    /**
     * Binds this thread to {@code athenaCatalogName} for the duration of one request.
     *
     * <p>Use it as a resource, so the release cannot be forgotten and cannot be skipped by an exception:
     *
     * <pre>{@code
     * try (CatalogRoutingConnectionFactory.Binding bound = factory.bind(request.getCatalogName())) {
     *     super.readWithConstraint(spiller, request, checker);
     * }
     * }</pre>
     *
     * @param athenaCatalogName the catalog the request arrived under.
     * @throws IllegalArgumentException if it is null or blank.
     */
    public Binding bind(String athenaCatalogName)
    {
        if (athenaCatalogName == null || athenaCatalogName.trim().isEmpty()) {
            throw new IllegalArgumentException(
                    "A read arrived with no Athena catalog name, so there is no source to resolve its"
                            + " warehouse and credential from.");
        }
        boundCatalog.set(athenaCatalogName);
        return new Binding(boundCatalog);
    }

    /**
     * A connection to the warehouse of the currently bound catalog.
     *
     * @param credentialsProvider ignored, including when null — see
     *                            {@link DatabricksConnectionFactory#getConnection}.
     * @throws IllegalStateException if no catalog is bound on this thread.
     */
    @Override
    public Connection getConnection(CredentialsProvider credentialsProvider)
    {
        String catalog = boundCatalog.get();
        if (catalog == null) {
            throw new IllegalStateException(
                    "A connection was asked for outside a bound request, so there is no catalog to"
                            + " resolve an endpoint from. This is a defect in the connector rather than"
                            + " a misconfiguration: some path reached the inherited read loop without"
                            + " going through the override that binds the request's catalog name. It"
                            + " fails rather than choosing an endpoint, because with several sources"
                            + " behind one function the choice would be another tenant's warehouse.");
        }
        return open(catalog);
    }

    /**
     * A connection for one catalog, without binding. For a caller that already has the catalog name in
     * hand and is not going through the inherited read loop.
     */
    public Connection open(String athenaCatalogName)
    {
        ConnectionConfig config = configs.configFor(athenaCatalogName);
        return connections.apply(config).get();
    }

    private static Function<ConnectionConfig, Supplier<Connection>> factoryFor(
            CredentialSource credentials)
    {
        // A fresh factory per request: constructing one costs a string concatenation, and caching them per
        // catalog would keep a configuration alive past the TTL that exists so a corrected parameter takes
        // effect without recycling the container.
        return config -> new DatabricksConnectionFactory(config, credentials)::open;
    }

    /** One thread's binding, released on {@link #close()}. */
    public static final class Binding implements AutoCloseable
    {
        private final ThreadLocal<String> holder;

        private Binding(ThreadLocal<String> holder)
        {
            this.holder = holder;
        }

        /**
         * Removes rather than nulls the entry: a Lambda container reuses its threads, so a lingering value
         * would let a later unbound call find a previous request's catalog.
         */
        @Override
        public void close()
        {
            holder.remove();
        }
    }
}
