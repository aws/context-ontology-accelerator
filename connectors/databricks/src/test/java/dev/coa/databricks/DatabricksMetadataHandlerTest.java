// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import com.amazonaws.athena.connector.lambda.data.BlockAllocatorImpl;
import com.amazonaws.athena.connector.lambda.domain.TableName;
import com.amazonaws.athena.connector.lambda.metadata.GetTableRequest;
import com.amazonaws.athena.connector.lambda.metadata.GetTableResponse;
import com.amazonaws.athena.connector.lambda.metadata.ListSchemasRequest;
import com.amazonaws.athena.connector.lambda.metadata.ListSchemasResponse;
import com.amazonaws.athena.connector.lambda.metadata.ListTablesRequest;
import com.amazonaws.athena.connector.lambda.metadata.ListTablesResponse;
import com.amazonaws.athena.connector.lambda.security.FederatedIdentity;
import dev.coa.databricks.config.ConnectionConfig;
import dev.coa.databricks.config.ConnectionConfigProviders;
import dev.coa.databricks.config.CredentialSource;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static dev.coa.databricks.FakeJdbc.row;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The metadata handler's catalog handling, its schema gate, and that it holds no request state.
 *
 * <p>Three halves. The name a request carries is the name its configuration is resolved for, and no
 * mutable state remains for a second request to observe — per-request state on a federation handler is
 * safe only because the Lambda runtime serialises invocations per container, which is a property of the
 * runtime rather than of the code and invisible in any signature that depends on it. Then the gate in
 * {@code readerFor}: which schema names this connector advertises, which it serves, and that those are
 * the same set.
 *
 * <p>Everything past {@code listDatabases} on a pinned config needs a warehouse, and there is none, so
 * the handler takes its connection supplier as a constructor parameter and these tests hand it a
 * {@link FakeJdbc}. Before that seam existed the connection factory was constructed inside
 * {@code readerFor} and neither {@code listTables} nor {@code describeTable} could be reached from a
 * unit test at all — which is how an unpinned connector came to advertise schema names it then refused.
 */
class DatabricksMetadataHandlerTest
{
    private static final FederatedIdentity IDENTITY = new FederatedIdentity(
            "arn:aws:iam::123456789012:role/query", "123456789012",
            Collections.emptyMap(), Collections.emptyList(), Collections.emptyMap());

    private static final String SECRET =
            "arn:aws:secretsmanager:us-east-1:111122223333:secret:dbx-AbCdEf";

    /**
     * A credential reader that returns a usable token. Every test here either stubs the connections or
     * fails before one opens, so nothing reads it; the handler takes it because production pairs it with
     * the provider in one place.
     */
    private static CredentialSource credentials()
    {
        return new CredentialSource(config -> "{\"token\": \"dapi-example\"}");
    }

    private static DatabricksMetadataHandler handlerOver(MultiplexedProvider provider)
    {
        return new DatabricksMetadataHandler(Collections.emptyMap(), provider, credentials(), null,
                CredentialSource.DEFAULT_TTL_MILLIS);
    }

    private static DatabricksMetadataHandler handlerOver(MultiplexedProvider provider, FakeJdbc jdbc)
    {
        return handlerOver(provider, jdbc, CredentialSource.DEFAULT_TTL_MILLIS);
    }

    /** @param schemaCacheTtlMillis zero to make every request re-enumerate. */
    private static DatabricksMetadataHandler handlerOver(MultiplexedProvider provider, FakeJdbc jdbc,
                                                         long schemaCacheTtlMillis)
    {
        return new DatabricksMetadataHandler(Collections.emptyMap(), provider, credentials(),
                config -> jdbc::connection, schemaCacheTtlMillis);
    }

    /**
     * Rows for every statement a discovery issues against the fixture catalogs. Catalog {@code main} holds
     * {@code default}, {@code sales} and {@code sales-eu} — the last legal in Unity Catalog and not a bare
     * identifier, which is the case this file exists for; catalog {@code other} holds {@code finance}.
     */
    private static List<Map<String, Object>> rowsFor(String sql)
    {
        if (sql.contains(".schemata")) {
            return sql.contains("`other`")
                    ? Collections.singletonList(row("schema_name", "finance"))
                    : Arrays.asList(row("schema_name", "default"), row("schema_name", "sales"),
                            row("schema_name", "sales-eu"));
        }
        if (sql.startsWith("SHOW TABLES")) {
            return Collections.singletonList(
                    row("database", "sales", "tableName", "orders", "isTemporary", "false"));
        }
        if (sql.startsWith("SELECT table_name, table_type")) {
            return Collections.singletonList(row("table_name", "orders", "table_type", "MANAGED"));
        }
        if (sql.startsWith("SELECT table_type")) {
            return Collections.singletonList(row("table_type", "MANAGED"));
        }
        if (sql.startsWith("SELECT column_name, full_data_type")) {
            return Collections.singletonList(
                    row("column_name", "id", "full_data_type", "bigint", "is_nullable", "YES", "comment", null));
        }
        // The two constraint reads. A table with no declared keys is the common case.
        return Collections.emptyList();
    }

    private static FakeJdbc fakeWarehouse()
    {
        return new FakeJdbc(DatabricksMetadataHandlerTest::rowsFor);
    }

    private static ListSchemasResponse listSchemas(DatabricksMetadataHandler handler, String catalog)
    {
        return handler.doListSchemaNames(new BlockAllocatorImpl(),
                new ListSchemasRequest(IDENTITY, "query-" + catalog, catalog));
    }

    private static List<String> schemasOf(DatabricksMetadataHandler handler, String catalog)
    {
        return new ArrayList<>(listSchemas(handler, catalog).getSchemas());
    }

    private static ListTablesResponse listTables(DatabricksMetadataHandler handler, String catalog,
                                                 String schema)
    {
        return handler.doListTables(new BlockAllocatorImpl(),
                new ListTablesRequest(IDENTITY, "query-" + catalog, catalog, schema, null, 100));
    }

    private static GetTableResponse getTable(DatabricksMetadataHandler handler, String catalog,
                                             String schema, String table)
    {
        return handler.doGetTable(new BlockAllocatorImpl(),
                new GetTableRequest(IDENTITY, "query-" + catalog, catalog,
                        new TableName(schema, table), Collections.emptyMap()));
    }

    /** Whether the handler will serve {@code schema}, i.e. whether {@code ListTables} against it works. */
    private static boolean serves(DatabricksMetadataHandler handler, String catalog, String schema)
    {
        try {
            listTables(handler, catalog, schema);
            return true;
        }
        catch (IllegalArgumentException refused) {
            return false;
        }
    }

    @Test
    void eachRequestResolvesTheConfigurationForItsOwnCatalog()
    {
        // Two catalogs, one handler, each answering from its own endpoint. A handler stashing the catalog
        // name in a field could only pass this by relying on invocation ordering.
        MultiplexedProvider provider = new MultiplexedProvider()
                .add("warehouse_a", "main", "sales")
                .add("warehouse_b", "other", "finance");
        DatabricksMetadataHandler handler = handlerOver(provider);

        assertEquals(Collections.singletonList("sales"),
                new ArrayList<>(listSchemas(handler, "warehouse_a").getSchemas()));
        assertEquals(Collections.singletonList("finance"),
                new ArrayList<>(listSchemas(handler, "warehouse_b").getSchemas()));
    }

    @Test
    void interleavedRequestsDoNotContaminateEachOther()
    {
        // A -> B -> A. A handler reading the catalog name back out of a field passes this only while every
        // doXxx rewrites it before listDatabases reads it; move the write, or interleave a request, and
        // the third call answers from B's endpoint.
        MultiplexedProvider provider = new MultiplexedProvider()
                .add("warehouse_a", "main", "sales")
                .add("warehouse_b", "other", "finance");
        DatabricksMetadataHandler handler = handlerOver(provider);

        assertEquals(Collections.singletonList("sales"),
                new ArrayList<>(listSchemas(handler, "warehouse_a").getSchemas()));
        assertEquals(Collections.singletonList("finance"),
                new ArrayList<>(listSchemas(handler, "warehouse_b").getSchemas()));
        assertEquals(Collections.singletonList("sales"),
                new ArrayList<>(listSchemas(handler, "warehouse_a").getSchemas()),
                "the third request must answer from A, not from whatever ran second");
    }

    @Test
    void theResponseCarriesTheRequestsCatalogName()
    {
        MultiplexedProvider provider = new MultiplexedProvider().add("warehouse_a", "main", "sales");

        assertEquals("warehouse_a",
                listSchemas(handlerOver(provider), "warehouse_a").getCatalogName());
    }

    @Test
    void theConfigurationIsResolvedForTheRequestsCatalogRatherThanNull()
    {
        // Guards against a regression to configFor(null), which a single-endpoint provider answers happily,
        // so the bug stays invisible until a multiplexed provider is wired in.
        MultiplexedProvider provider = new MultiplexedProvider().add("warehouse_a", "main", "sales");
        DatabricksMetadataHandler handler = handlerOver(provider);
        provider.asked.clear();

        listSchemas(handler, "warehouse_a");

        assertEquals(Collections.singletonList("warehouse_a"), provider.asked);
    }

    // ── the schema gate: what it advertises is what it serves ────────────────

    @Test
    void whatAnUnpinnedConnectorAdvertisesIsExactlyWhatItServes()
    {
        // The property the whole gate exists for. Before it, SHOW DATABASES listed sales-eu verbatim, a
        // source was onboarded against it, and every ListTables and GetTable then failed naming
        // DATABRICKS_SCHEMA — a variable an unpinned deployment never sets.
        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler =
                handlerOver(new MultiplexedProvider().addUnpinned("warehouse_a", "main"), jdbc);

        List<String> advertised = schemasOf(handler, "warehouse_a");

        for (String name : Arrays.asList("default", "sales", "sales-eu")) {
            assertEquals(advertised.contains(name), serves(handler, "warehouse_a", name),
                    "advertised and servable disagree about " + name);
        }
    }

    @Test
    void unpinnedListSchemasDropsANameAthenaCannotAddress()
    {
        // sales-eu is a legal Unity Catalog schema. It is not a bare identifier, and Athena parses
        // SHOW/DESCRIBE and SELECT with different quoting rules, so it cannot be addressed on both paths.
        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler =
                handlerOver(new MultiplexedProvider().addUnpinned("warehouse_a", "main"), jdbc);

        assertEquals(Arrays.asList("default", "sales"), schemasOf(handler, "warehouse_a"));
    }

    @Test
    void aSchemaItRefusesIsRefusedForItsOwnReasonRatherThanTheEnvironments()
    {
        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler =
                handlerOver(new MultiplexedProvider().addUnpinned("warehouse_a", "main"), jdbc);

        for (Runnable call : Arrays.<Runnable>asList(
                () -> listTables(handler, "warehouse_a", "sales-eu"),
                () -> getTable(handler, "warehouse_a", "sales-eu", "orders"))) {
            IllegalArgumentException failure =
                    assertThrows(IllegalArgumentException.class, call::run);
            assertTrue(failure.getMessage().contains("sales-eu"), failure.getMessage());
            assertTrue(failure.getMessage().contains("bare SQL identifier"), failure.getMessage());
            assertFalse(failure.getMessage().contains(ConnectionConfig.SCHEMA_VAR),
                    "an unpinned connector must not blame DATABRICKS_SCHEMA: " + failure.getMessage());
        }
    }

    @Test
    void unpinnedListTablesEnumeratesThenScopesTheReadToTheRequestedSchema()
    {
        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler =
                handlerOver(new MultiplexedProvider().addUnpinned("warehouse_a", "main"), jdbc);

        List<String> tables = new ArrayList<>();
        for (TableName name : listTables(handler, "warehouse_a", "sales").getTables()) {
            tables.add(name.getTableName());
        }

        assertEquals(Collections.singletonList("orders"), tables);
        assertEquals(1, jdbc.statementsContaining(".schemata").size(), "enumerated once");
        assertEquals("SHOW TABLES IN `main`.`sales`",
                jdbc.statementsContaining("SHOW TABLES").get(0).sql(),
                "the read has to be scoped to the schema the request named");
        assertEquals(Arrays.asList("main", "sales"),
                jdbc.statementsContaining("SELECT table_name, table_type").get(0).parameters());
    }

    @Test
    void unpinnedDescribeTableScopesTheReadToTheRequestedSchema()
    {
        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler =
                handlerOver(new MultiplexedProvider().addUnpinned("warehouse_a", "main"), jdbc);

        GetTableResponse response = getTable(handler, "warehouse_a", "sales", "orders");

        assertEquals("orders", response.getTableName().getTableName());
        assertEquals("id", response.getSchema().findField("id").getName());
        assertEquals(Arrays.asList("main", "sales", "orders"),
                jdbc.statementsContaining("SELECT column_name, full_data_type").get(0).parameters());
    }

    @Test
    void unpinnedRefusesAnUnknownSchemaAndListsTheAlternatives()
    {
        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler =
                handlerOver(new MultiplexedProvider().addUnpinned("warehouse_a", "main"), jdbc);

        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> getTable(handler, "warehouse_a", "made_up", "orders"));

        // A typo and a missing USE SCHEMA grant look identical from here, so the message has to cover both.
        assertTrue(failure.getMessage().contains("\"default\", \"sales\""), failure.getMessage());
        assertTrue(failure.getMessage().contains("USE SCHEMA"), failure.getMessage());
        assertFalse(failure.getMessage().contains("sales-eu"),
                "a name it does not advertise must not be offered as an alternative: "
                        + failure.getMessage());
    }

    @Test
    void aPinnedConnectorRefusesAnotherSchemaWithoutAskingTheWarehouse()
    {
        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler =
                handlerOver(new MultiplexedProvider().add("warehouse_a", "main", "sales"), jdbc);

        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> listTables(handler, "warehouse_a", "finance"));

        // Here naming the variable IS the right answer: it is set, and unsetting it is the fix.
        assertTrue(failure.getMessage().contains(ConnectionConfig.SCHEMA_VAR), failure.getMessage());
        assertTrue(failure.getMessage().contains("sales"), failure.getMessage());
        assertEquals(0, jdbc.connectionsOpened(),
                "a pinned refusal is a comparison, not a warehouse round trip");
    }

    @Test
    void aPinnedConnectorServesItsOwnSchemaWithoutEnumerating()
    {
        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler =
                handlerOver(new MultiplexedProvider().add("warehouse_a", "main", "sales"), jdbc);

        assertEquals("orders", getTable(handler, "warehouse_a", "sales", "orders")
                .getTableName().getTableName());
        assertTrue(jdbc.statementsContaining(".schemata").isEmpty(),
                "the pin is the answer; enumerating could only agree with it or fail");
    }

    // ── the enumeration is cached, because discovery is a per-table fan-out ───

    @Test
    void theSchemaListIsEnumeratedOncePerContainerRatherThanPerRequest()
    {
        // Discovery is a GetTable per table, and every one of them passes through the gate. Enumerating
        // per request costs a metastore-wide information_schema.schemata scan and a connection each time.
        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler =
                handlerOver(new MultiplexedProvider().addUnpinned("warehouse_a", "main"), jdbc);

        schemasOf(handler, "warehouse_a");
        listTables(handler, "warehouse_a", "sales");
        getTable(handler, "warehouse_a", "sales", "orders");
        getTable(handler, "warehouse_a", "sales", "orders");

        assertEquals(1, jdbc.statementsContaining(".schemata").size());
        assertEquals(4, jdbc.connectionsOpened(), "one to enumerate, one per read");
    }

    @Test
    void theSchemaListIsEnumeratedAgainOnceItsTtlHasPassed()
    {
        // A zero TTL is the boundary case, and it is what proves the cache is a cache rather than a
        // one-shot: a schema created after a container warmed up has to become visible.
        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler =
                handlerOver(new MultiplexedProvider().addUnpinned("warehouse_a", "main"), jdbc, 0);

        listTables(handler, "warehouse_a", "sales");
        listTables(handler, "warehouse_a", "sales");

        assertEquals(2, jdbc.statementsContaining(".schemata").size());
    }

    @Test
    void twoCatalogsDoNotShareOneCachedSchemaList()
    {
        // The cache is keyed on the configuration, not on nothing. A multiplexed provider hands out a
        // different endpoint per Athena catalog, and one catalog's schema list must not answer another's.
        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler = handlerOver(new MultiplexedProvider()
                .addUnpinned("warehouse_a", "main")
                .addUnpinned("warehouse_b", "other"), jdbc);

        assertEquals(Arrays.asList("default", "sales"), schemasOf(handler, "warehouse_a"));
        assertEquals(Collections.singletonList("finance"), schemasOf(handler, "warehouse_b"));
        assertEquals(Arrays.asList("default", "sales"), schemasOf(handler, "warehouse_a"));
        assertEquals(Collections.singletonList("finance"), schemasOf(handler, "warehouse_b"));
    }

    @Test
    void everyConnectionOpenedOnTheDiscoveryPathIsClosed()
    {
        // A connection held across a Lambda freeze leaves a warehouse session nothing will reuse.
        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler =
                handlerOver(new MultiplexedProvider().addUnpinned("warehouse_a", "main"), jdbc);

        schemasOf(handler, "warehouse_a");
        listTables(handler, "warehouse_a", "sales");
        getTable(handler, "warehouse_a", "sales", "orders");

        assertEquals(jdbc.connectionsOpened(), jdbc.connectionsClosed());
    }

    @Test
    void anEnvironmentModeDeploymentServesItsOnePinnedSchemaThroughTheSamePath()
    {
        // Every other test in this file goes through the multiplexed fake, so without this the real
        // EnvironmentConnectionConfigProvider is never wired into a handler anywhere in the Java suite —
        // and a deployed single-endpoint stack pulling a newer jar has to behave no differently.
        Map<String, String> stageOne = new HashMap<>();
        stageOne.put(ConnectionConfig.WORKSPACE_HOSTNAME_VAR, "dbc-a1b2345c-d6e7.cloud.databricks.com");
        stageOne.put(ConnectionConfig.HTTP_PATH_VAR, "/sql/1.0/warehouses/a1b234c567d8e9fa");
        stageOne.put(ConnectionConfig.CATALOG_VAR, "main");
        stageOne.put(ConnectionConfig.SCHEMA_VAR, "sales");
        stageOne.put(ConnectionConfig.CREDENTIAL_SECRET_ARN_VAR, SECRET);

        FakeJdbc jdbc = fakeWarehouse();
        // fromEnvironment rather than the provider directly: that factory is what a real cold start calls,
        // so this covers the mode defaulting to `environment` and the mutual-exclusion check too.
        DatabricksMetadataHandler handler = new DatabricksMetadataHandler(stageOne,
                ConnectionConfigProviders.fromEnvironment(stageOne).provider(), credentials(),
                config -> jdbc::connection, CredentialSource.DEFAULT_TTL_MILLIS);

        // Any Athena catalog name resolves the one endpoint, which is this mode's whole difference — and
        // the pinned schema is the answer without asking the warehouse.
        assertEquals(Collections.singletonList("sales"),
                new ArrayList<>(listSchemas(handler, "some_registered_catalog").getSchemas()));
        assertTrue(jdbc.statementsContaining(".schemata").isEmpty(),
                "a pinned connector does not enumerate");
        assertEquals("orders",
                getTable(handler, "some_registered_catalog", "sales", "orders")
                        .getTableName().getTableName());
        assertEquals(Arrays.asList("main", "sales", "orders"),
                jdbc.statementsContaining("SELECT column_name, full_data_type").get(0).parameters());
    }

    @Test
    void anEnvironmentModeDeploymentStillRefusesASchemaOutsideItsPin()
    {
        Map<String, String> stageOne = new HashMap<>();
        stageOne.put(ConnectionConfig.WORKSPACE_HOSTNAME_VAR, "dbc-a1b2345c-d6e7.cloud.databricks.com");
        stageOne.put(ConnectionConfig.HTTP_PATH_VAR, "/sql/1.0/warehouses/a1b234c567d8e9fa");
        stageOne.put(ConnectionConfig.CATALOG_VAR, "main");
        stageOne.put(ConnectionConfig.SCHEMA_VAR, "sales");
        stageOne.put(ConnectionConfig.CREDENTIAL_SECRET_ARN_VAR, SECRET);

        FakeJdbc jdbc = fakeWarehouse();
        DatabricksMetadataHandler handler = new DatabricksMetadataHandler(stageOne,
                ConnectionConfigProviders.fromEnvironment(stageOne).provider(), credentials(),
                config -> jdbc::connection, CredentialSource.DEFAULT_TTL_MILLIS);

        IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
                () -> listTables(handler, "some_registered_catalog", "finance"));

        // Set by the deployer, so naming it is the actionable part — unlike the managed case, where the
        // variable is absent from the deployment by construction.
        assertTrue(refused.getMessage().contains(ConnectionConfig.SCHEMA_VAR), refused.getMessage());
        assertEquals(0, jdbc.connectionsOpened());
    }

    // ── the gate itself, without a handler around it ─────────────────────────

    @Test
    void servableKeepsAddressableNamesAndDropsTheRest()
    {
        assertEquals(Arrays.asList("default", "sales", "_private"),
                DatabricksMetadataHandler.servable(
                        Arrays.asList("default", "sales", "sales-eu", "sales eu", "_private",
                                "\"quoted\"", "1sales"),
                        "main"));
    }

    @Test
    void servableReturnsNothingRatherThanFailingWhenNoNameIsAddressable()
    {
        // Advertising nothing is a legitimate answer; failing here would take ListSchemaNames down for a
        // catalog whose schemas simply cannot be reached through Athena.
        assertEquals(Collections.emptyList(),
                DatabricksMetadataHandler.servable(Arrays.asList("sales-eu", "sales.eu"), "main"));
        assertEquals(Collections.emptyList(),
                DatabricksMetadataHandler.servable(Collections.emptyList(), "main"));
    }

    @Test
    void theHandlerHoldsNoMutableState()
    {
        // Structural rather than behavioural: the three tests above pass even with a per-request field
        // written on every path. Every field this class declares has to be final.
        for (Field field : DatabricksMetadataHandler.class.getDeclaredFields()) {
            if (field.isSynthetic() || java.lang.reflect.Modifier.isStatic(field.getModifiers())) {
                continue;
            }
            assertTrue(java.lang.reflect.Modifier.isFinal(field.getModifiers()),
                    "field " + field.getName() + " is not final; per-request state on a federation"
                            + " handler is safe only by accident of the Lambda runtime");
            assertFalse(java.lang.reflect.Modifier.isVolatile(field.getModifiers()),
                    "field " + field.getName() + " is volatile, which is how the old"
                            + " requestCatalogName smuggled a parameter past the SPI");
        }
    }
}
