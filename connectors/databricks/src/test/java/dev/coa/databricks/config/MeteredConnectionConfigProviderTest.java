// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import dev.coa.connector.metrics.ConnectorMetrics;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class MeteredConnectionConfigProviderTest
{
    private final List<String> emitted = new ArrayList<>();
    private final ConnectorMetrics metrics = new ConnectorMetrics("databricks", emitted::add);

    private static ConnectionConfig anyConfig()
    {
        return ConnectionConfig.builder()
                .workspaceHostname("dbc-a1b2345c-d6e7.cloud.databricks.com")
                .httpPath("/sql/1.0/warehouses/a1b234c567d8e9fa")
                .catalog("main")
                .credentialSecretArn(
                        "arn:aws:secretsmanager:us-east-1:111122223333:secret:dbx-AbCdEf")
                .build();
    }

    @Test
    void aResolvedConfigurationPassesThroughUncounted()
    {
        ConnectionConfig config = anyConfig();

        ConnectionConfig returned =
                new MeteredConnectionConfigProvider(catalog -> config, metrics).configFor("cat");

        assertSame(config, returned);
        assertTrue(emitted.isEmpty(), "a success must not count as a failure");
    }

    @Test
    void aFailureIsCountedAgainstTheCatalogItWasAskedFor()
    {
        ConnectionConfigProvider failing = catalog -> {
            throw new IllegalArgumentException("DATABRICKS_CATALOG is not set");
        };

        assertThrows(IllegalArgumentException.class,
                () -> new MeteredConnectionConfigProvider(failing, metrics).configFor("acme_dbx"));

        assertEquals(1, emitted.size());
        assertTrue(emitted.get(0).contains(ConnectorMetrics.CONFIG_RESOLUTION_FAILURES));
        assertTrue(emitted.get(0).contains("\"Catalog\":\"acme_dbx\""),
                "the runbook distinguishes one catalog from fleet-wide: " + emitted.get(0));
    }

    @Test
    void theExceptionIsRethrownUnchangedBecauseItsMessageReachesTheUsersQuery()
    {
        IllegalArgumentException original = new IllegalArgumentException("DATABRICKS_HTTP_PATH is blank");
        ConnectionConfigProvider failing = catalog -> {
            throw original;
        };

        IllegalArgumentException thrown = assertThrows(IllegalArgumentException.class,
                () -> new MeteredConnectionConfigProvider(failing, metrics).configFor("cat"));

        assertSame(original, thrown);
    }

    @Test
    void aNullCatalogIsCountedWithoutTheCatalogDimension()
    {
        // The cold-start path resolves configuration before any catalog name exists.
        ConnectionConfigProvider failing = catalog -> {
            throw new IllegalStateException("parameter store unreachable");
        };

        assertThrows(IllegalStateException.class,
                () -> new MeteredConnectionConfigProvider(failing, metrics).configFor(null));

        assertEquals(1, emitted.size());
        assertTrue(emitted.get(0).contains(ConnectorMetrics.CONFIG_RESOLUTION_FAILURES));
        assertTrue(emitted.get(0).contains("[[\"Connector\"]]"),
                "expected only the fleet dimension set: " + emitted.get(0));
    }

    @Test
    void aCatalogNameThatIsNotOneIsCountedWithoutBecomingADimension()
    {
        // The count happens before the delegate validates, so the name is whatever the caller sent, and a
        // principal holding lambda:InvokeFunction can post any string. Not an injection — the JSON is
        // escaped — but each distinct name would mint a custom CloudWatch metric at about $0.30 a month, so
        // the dimension is dropped and the count kept.
        ConnectionConfigProvider failing = catalog -> {
            throw new IllegalArgumentException("no such source");
        };

        for (String hostile : new String[] {
            "../deployment/function-arn", "a b", "a\nb", "a\"b", "'; DROP", "x".repeat(400), "-"}) {
            emitted.clear();
            assertThrows(IllegalArgumentException.class,
                    () -> new MeteredConnectionConfigProvider(failing, metrics).configFor(hostile));

            assertEquals(1, emitted.size(), "the failure has to stay counted: " + hostile);
            assertTrue(emitted.get(0).contains(ConnectorMetrics.CONFIG_RESOLUTION_FAILURES),
                    emitted.get(0));
            assertTrue(emitted.get(0).contains("[[\"Connector\"]]"),
                    "expected the fleet dimension set alone for " + hostile + ": " + emitted.get(0));
            assertFalse(emitted.get(0).contains("\"Catalog\""),
                    "an unvalidated name must not become a dimension: " + emitted.get(0));
        }
    }

    @Test
    void aCatalogNameCoaCouldHaveDerivedStillBecomesADimension()
    {
        // The other side of it: the check must not cost the dimension for a real name — `<prefix>ds_<digest>`
        // for COA, whatever the operator registered for stage-1, letters, digits and underscores either way.
        ConnectionConfigProvider failing = catalog -> {
            throw new IllegalArgumentException("no such source");
        };

        for (String legitimate : new String[] {
            "coadevds_144a95d84d98c87d", "databricks", "databricks_sales", "_x", "A1"}) {
            emitted.clear();
            assertThrows(IllegalArgumentException.class,
                    () -> new MeteredConnectionConfigProvider(failing, metrics).configFor(legitimate));

            assertTrue(emitted.get(0).contains("\"Catalog\":\"" + legitimate + "\""),
                    "the runbook distinguishes one catalog from fleet-wide: " + emitted.get(0));
        }
    }

    @Test
    void anyRuntimeExceptionCountsNotOnlyIllegalArgument()
    {
        // A provider reading a remote store fails in its own ways, and each is a resolution failure to
        // whoever holds the alarm.
        ConnectionConfigProvider failing = catalog -> {
            throw new RuntimeException("throttled");
        };

        assertThrows(RuntimeException.class,
                () -> new MeteredConnectionConfigProvider(failing, metrics).configFor("cat"));

        assertEquals(1, emitted.size());
    }
}
