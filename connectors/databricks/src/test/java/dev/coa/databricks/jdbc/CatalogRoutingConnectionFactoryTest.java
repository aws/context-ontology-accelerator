// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.jdbc;

import dev.coa.databricks.config.ConnectionConfig;
import dev.coa.databricks.config.ConnectionConfigProvider;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;
import java.util.function.Supplier;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The inherited read loop takes its connection factory at construction and calls it per request with
 * nothing that identifies the request, so the request's catalog travels across that call in a thread-local.
 */
class CatalogRoutingConnectionFactoryTest
{
    private static final String CATALOG_A = "coadevds_144a95d84d98c87d";
    private static final String CATALOG_B = "coadevds_9f2b1c7ae4d05631";

    private final List<String> opened = new ArrayList<>();

    private static ConnectionConfigProvider twoEndpoints()
    {
        Map<String, ConnectionConfig> byCatalog = new HashMap<>();
        byCatalog.put(CATALOG_A, endpoint("main", "sales"));
        byCatalog.put(CATALOG_B, endpoint("other", "finance"));
        return catalog -> {
            ConnectionConfig config = byCatalog.get(catalog);
            if (config == null) {
                throw new IllegalArgumentException("no endpoint for catalog " + catalog);
            }
            return config;
        };
    }

    private static ConnectionConfig endpoint(String ucCatalog, String schema)
    {
        return ConnectionConfig.builder()
                .workspaceHostname("dbc-a1b2345c-d6e7.cloud.databricks.com")
                .httpPath("/sql/1.0/warehouses/a1b234c567d8e9fa")
                .catalog(ucCatalog)
                .schema(schema)
                .credentialSecretArn(
                        "arn:aws:secretsmanager:us-east-1:111122223333:secret:dbx-AbCdEf")
                .build();
    }

    private Function<ConnectionConfig, Supplier<Connection>> recording()
    {
        return config -> () -> {
            opened.add(config.catalog() + '.' + config.schema());
            return (Connection) java.lang.reflect.Proxy.newProxyInstance(
                    CatalogRoutingConnectionFactoryTest.class.getClassLoader(),
                    new Class<?>[] {Connection.class},
                    (proxy, method, args) -> "toString".equals(method.getName())
                            ? config.catalog() + '.' + config.schema()
                            : null);
        };
    }

    private CatalogRoutingConnectionFactory factory()
    {
        return new CatalogRoutingConnectionFactory(twoEndpoints(), recording());
    }

    @Test
    void aBoundCatalogGetsItsOwnEndpointsConnection()
    {
        CatalogRoutingConnectionFactory factory = factory();

        try (CatalogRoutingConnectionFactory.Binding bound = factory.bind(CATALOG_A)) {
            assertEquals("main.sales", factory.getConnection(null).toString());
        }
        try (CatalogRoutingConnectionFactory.Binding bound = factory.bind(CATALOG_B)) {
            assertEquals("other.finance", factory.getConnection(null).toString());
        }

        assertEquals(java.util.Arrays.asList("main.sales", "other.finance"), opened);
    }

    @Test
    void interleavedBindingsNeverCross()
    {
        // The failure this guards returns another tenant's rows rather than an error, so it is asserted on
        // what was opened rather than on what was returned.
        CatalogRoutingConnectionFactory factory = factory();

        for (String catalog : java.util.Arrays.asList(CATALOG_A, CATALOG_B, CATALOG_A)) {
            try (CatalogRoutingConnectionFactory.Binding bound = factory.bind(catalog)) {
                factory.getConnection(null);
            }
        }

        assertEquals(java.util.Arrays.asList("main.sales", "other.finance", "main.sales"), opened);
    }

    @Test
    void anUnboundCallFailsRatherThanChoosingAnEndpoint()
    {
        // With several sources behind one function, the choice would be another tenant's warehouse. An
        // unbound call is a defect: some path reached the read loop without the override that binds.
        CatalogRoutingConnectionFactory factory = factory();

        IllegalStateException failure =
                assertThrows(IllegalStateException.class, () -> factory.getConnection(null));

        assertTrue(failure.getMessage().contains("outside a bound request"), failure.getMessage());
        assertTrue(opened.isEmpty());
    }

    @Test
    void theBindingIsReleasedEvenWhenTheBodyThrows()
    {
        // A Lambda container reuses its threads, so a binding surviving its request would let a later
        // unbound call find a previous request's catalog and answer from it.
        CatalogRoutingConnectionFactory factory = factory();

        assertThrows(IllegalStateException.class, () -> {
            try (CatalogRoutingConnectionFactory.Binding bound = factory.bind(CATALOG_A)) {
                throw new IllegalStateException("the read failed");
            }
        });

        assertThrows(IllegalStateException.class, () -> factory.getConnection(null));
    }

    @Test
    void aBindingOnOneThreadIsInvisibleToAnother() throws Exception
    {
        // Why a ThreadLocal rather than a field: a field would be safe only by accident of the Lambda
        // runtime serialising invocations per container.
        CatalogRoutingConnectionFactory factory = factory();
        AtomicReference<Throwable> onOtherThread = new AtomicReference<>();

        try (CatalogRoutingConnectionFactory.Binding bound = factory.bind(CATALOG_A)) {
            Thread other = new Thread(() -> {
                try {
                    factory.getConnection(null);
                }
                catch (Throwable expected) {
                    onOtherThread.set(expected);
                }
            });
            other.start();
            other.join();
        }

        assertTrue(onOtherThread.get() instanceof IllegalStateException,
                "another thread must not see this one's binding: " + onOtherThread.get());
        assertTrue(opened.isEmpty());
    }

    @Test
    void bindingRefusesAMissingCatalogName()
    {
        CatalogRoutingConnectionFactory factory = factory();

        for (String catalog : new String[] {null, "", "  "}) {
            assertThrows(IllegalArgumentException.class, () -> factory.bind(catalog),
                    String.valueOf(catalog));
        }
    }

    @Test
    void openTakesACatalogDirectlyForACallerNotGoingThroughTheReadLoop()
    {
        assertEquals("other.finance", factory().open(CATALOG_B).toString());
        assertEquals(java.util.Arrays.asList("other.finance"), opened);
    }

    @Test
    void anUnknownCatalogFailsAsAConfigurationResolutionRatherThanAConnection()
    {
        CatalogRoutingConnectionFactory factory = factory();

        try (CatalogRoutingConnectionFactory.Binding bound = factory.bind("coadevds_unknown")) {
            assertThrows(IllegalArgumentException.class, () -> factory.getConnection(null));
        }
        assertTrue(opened.isEmpty(), "nothing should be opened for a catalog with no endpoint");
    }

    @Test
    void nullIsRefusedForTheProviderAndTheConnections()
    {
        Function<ConnectionConfig, Supplier<Connection>> noConnections = null;
        assertThrows(NullPointerException.class,
                () -> new CatalogRoutingConnectionFactory(null, recording()));
        assertThrows(NullPointerException.class,
                () -> new CatalogRoutingConnectionFactory(twoEndpoints(), noConnections));
    }
}
