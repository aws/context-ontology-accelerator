// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import com.amazonaws.athena.connector.lambda.data.Block;
import com.amazonaws.athena.connector.lambda.data.BlockSpiller;
import com.amazonaws.athena.connector.lambda.data.SchemaBuilder;
import com.amazonaws.athena.connector.lambda.domain.Split;
import com.amazonaws.athena.connector.lambda.domain.TableName;
import com.amazonaws.athena.connector.lambda.domain.predicate.ConstraintEvaluator;
import com.amazonaws.athena.connector.lambda.domain.predicate.Constraints;
import com.amazonaws.athena.connector.lambda.domain.spill.SpillLocation;
import com.amazonaws.athena.connector.lambda.QueryStatusChecker;
import com.amazonaws.athena.connector.lambda.data.BlockWriter;
import com.amazonaws.athena.connector.lambda.exceptions.AthenaConnectorException;
import com.amazonaws.athena.connector.lambda.records.ReadRecordsRequest;
import com.amazonaws.athena.connector.lambda.security.FederatedIdentity;
import dev.coa.connector.metrics.ConnectorMetrics;
import dev.coa.databricks.config.ConnectionConfig;
import dev.coa.databricks.config.ConnectionConfigProviders;
import dev.coa.databricks.jdbc.DatabricksErrors;
import org.apache.arrow.vector.types.pojo.Schema;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.function.Supplier;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * One deployment serves every Databricks source, and the only thing saying which source a request belongs
 * to is the Athena catalog name on it. So these tests interleave two catalogs through one handler: a
 * handler resolving anything at construction, or caching a derived object across catalogs, answers the
 * second catalog's request from the first's warehouse and returns another namespace's rows with nothing
 * erroring.
 *
 * <p>The pin is the other half. It is a containment boundary independent of the credential's Unity Catalog
 * grants, and it was one only on the metadata path: a principal holding {@code lambda:InvokeFunction} can
 * post a hand-built {@code ReadRecordsRequest}, leaving the credential's grants — what the pin is meant to
 * sit on top of — as the only boundary.
 */
class DatabricksRecordHandlerTest
{
    private static final FederatedIdentity IDENTITY = new FederatedIdentity(
            "arn:aws:iam::123456789012:role/query", "123456789012",
            Collections.emptyMap(), Collections.emptyList(), Collections.emptyMap());

    private static final String CATALOG_A = "coadevds_144a95d84d98c87d";
    private static final String CATALOG_B = "coadevds_9f2b1c7ae4d05631";
    private static final String CATALOG_C = "coadevds_3c5d7e9fa1b20486";

    private static ConnectionConfig.Builder valid()
    {
        return ConnectionConfig.builder()
                .workspaceHostname("dbc-a1b2345c-d6e7.cloud.databricks.com")
                .httpPath("/sql/1.0/warehouses/a1b234c567d8e9fa")
                .catalog("main")
                .credentialSecretArn(
                        "arn:aws:secretsmanager:us-east-1:111122223333:secret:dbx-AbCdEf");
    }

    /** Two managed sources behind one connector: different Unity Catalog catalogs and schemas. */
    private static MultiplexedProvider twoSources()
    {
        return new MultiplexedProvider()
                .addManaged(CATALOG_A, "main", "sales")
                .addManaged(CATALOG_B, "other", "finance");
    }

    /**
     * Every endpoint a connection was opened against, in order. The {@code buildSplitSql} tests assert the
     * statements instead, since that method is handed its connection and opens nothing.
     */
    private final List<String> opened = new ArrayList<>();

    /** Every metric line the handler emitted, so the read path's two emissions are observable. */
    private final List<String> emitted = new ArrayList<>();

    private Function<ConnectionConfig, Supplier<Connection>> recordingConnections(FakeJdbc jdbc)
    {
        return config -> () -> {
            opened.add(config.catalog() + '.' + config.schema());
            return jdbc.connection();
        };
    }

    private DatabricksRecordHandler handlerOver(MultiplexedProvider provider, FakeJdbc jdbc)
    {
        return handlerOver(provider, jdbc, Collections.emptyMap());
    }

    /** @param configOptions the function's environment, for the row ceiling and the mode. */
    private DatabricksRecordHandler handlerOver(MultiplexedProvider provider, FakeJdbc jdbc,
                                                Map<String, String> configOptions)
    {
        return new DatabricksRecordHandler(configOptions, provider, recordingConnections(jdbc),
                new ConnectorMetrics("databricks", emitted::add));
    }

    private static Schema orders()
    {
        return SchemaBuilder.newBuilder().addBigIntField("order_num").build();
    }

    private static Constraints noConstraints()
    {
        return new Constraints(Collections.emptyMap(), Collections.emptyList(),
                Collections.emptyList(), Constraints.DEFAULT_NO_LIMIT,
                Collections.emptyMap(), null);
    }

    private static Split split()
    {
        // No properties: the inherited query builder reads every split property as a partition column.
        return new Split(null, null, Collections.emptyMap());
    }

    private static ReadRecordsRequest readRequest(String catalog, String schema, String table)
    {
        return new ReadRecordsRequest(IDENTITY, catalog, "query-" + catalog,
                new TableName(schema, table), orders(), split(), noConstraints(),
                1_000_000L, 1_000_000L);
    }

    /**
     * Counts the rows the inherited read loop hands it, and deliberately does not invoke the row writer.
     *
     * <p>The loop calls {@code spiller.writeRows(writer)} once per result-set row and
     * {@code RowCeilingSpiller} counts exactly those calls, so the row count, the ceiling discriminator
     * and both metric emissions are all driven by this. Invoking the writer as well would put Arrow
     * allocation and {@code athena-jdbc}'s typed extractors in a unit test for no assertion made here.
     */
    private static final class CountingSpiller implements BlockSpiller
    {
        private int rows;

        @Override
        public void writeRows(RowWriter rowWriter)
        {
            rows++;
        }

        @Override
        public ConstraintEvaluator getConstraintEvaluator()
        {
            return null;
        }

        @Override
        public boolean spilled()
        {
            return false;
        }

        @Override
        public Block getBlock()
        {
            return null;
        }

        @Override
        public List<SpillLocation> getSpillLocations()
        {
            return Collections.emptyList();
        }

        @Override
        public void close()
        {
        }
    }

    /**
     * A checker the read loop can consult without an Athena client.
     *
     * <p>{@code QueryStatusChecker.isQueryRunning()} starts a background poller against Athena the first
     * time it is called, so the real one cannot be used here — and passing null makes the loop NPE on its
     * first row, which is why no test in this file reached a second row before. Overriding is enough: the
     * constructor only assigns its arguments, so the nulls are never dereferenced.
     */
    private static final class AlwaysRunning extends QueryStatusChecker
    {
        private AlwaysRunning()
        {
            super(null, null, "query-id");
        }

        @Override
        public boolean isQueryRunning()
        {
            return true;
        }
    }

    /** A result set of {@code rows} single-column rows, for whatever statement the loop prepares. */
    private static FakeJdbc warehouseReturning(int rows)
    {
        List<Map<String, Object>> resultSet = new ArrayList<>(rows);
        for (int row = 0; row < rows; row++) {
            resultSet.add(FakeJdbc.row("order_num", row));
        }
        return new FakeJdbc(sql -> resultSet);
    }

    @Test
    void readingRowsCountsThemAndEmitsRowsReturnedForThatCatalog()
    {
        // Drives data through the REAL read loop rather than failing at the connection, which is what makes
        // the row count, the ConnectorRowsReturned emission and the ceiling discriminator reachable at all.
        // All three are on the success path, so nothing else in the suite would notice them going missing —
        // and that metric is the leading indicator that push-down has regressed.
        FakeJdbc jdbc = warehouseReturning(3);
        DatabricksRecordHandler handler = handlerOver(twoSources(), jdbc);
        CountingSpiller spiller = new CountingSpiller();

        assertDoesNotThrow(() -> handler.readWithConstraint(spiller,
                readRequest(CATALOG_A, "sales", "orders"), new AlwaysRunning()));

        assertEquals(3, spiller.rows, "every result-set row has to reach the spiller");
        assertEquals(1, emitted.size(), "expected one metric line: " + emitted);
        assertTrue(emitted.get(0).contains(ConnectorMetrics.ROWS_RETURNED), emitted.get(0));
        assertTrue(emitted.get(0).contains("\"" + ConnectorMetrics.ROWS_RETURNED + "\":3"),
                "the value is the row count: " + emitted.get(0));
        // Dimensioned by the catalog the request arrived under, which is what makes the metric usable for
        // triage on a connector serving several sources.
        assertTrue(emitted.get(0).contains("\"Catalog\":\"" + CATALOG_A + "\""), emitted.get(0));
        assertEquals(Collections.singletonList("main.sales"), opened);
    }

    @Test
    void aReadThatBreachesTheCeilingFailsNamingTheTableAndCountsIt()
    {
        // The ceiling exists because federation cannot express aggregation: a GROUP BY reads every
        // predicate-matching row out of the warehouse, so a large table exhausts the invocation. The failure
        // has to name the table — a timeout names none, suggests no action and gets retried — and be counted.
        FakeJdbc jdbc = warehouseReturning(5);
        Map<String, String> ceilingOfTwo = new HashMap<>();
        ceilingOfTwo.put(Settings.MAX_ROWS_PER_TABLE_VAR, "2");
        DatabricksRecordHandler handler = handlerOver(twoSources(), jdbc, ceilingOfTwo);

        AthenaConnectorException failure = assertThrows(AthenaConnectorException.class,
                () -> handler.readWithConstraint(new CountingSpiller(),
                        readRequest(CATALOG_A, "sales", "orders"), new AlwaysRunning()));

        assertTrue(failure.getMessage().startsWith(DatabricksErrors.TABLE_TOO_LARGE_PREFIX),
                failure.getMessage());
        assertTrue(failure.getMessage().contains("orders"),
                "the message has to name the table: " + failure.getMessage());
        // The discriminator is the row count against the ceiling, not the exception's text, so that a
        // reworded message cannot silently stop counting.
        List<String> ceilingMetrics = new ArrayList<>();
        for (String line : emitted) {
            if (line.contains(ConnectorMetrics.TABLE_CEILING_EXCEEDED)) {
                ceilingMetrics.add(line);
            }
        }
        assertEquals(1, ceilingMetrics.size(), "expected one ceiling breach counted: " + emitted);
        assertTrue(ceilingMetrics.get(0).contains("\"Catalog\":\"" + CATALOG_A + "\""),
                ceilingMetrics.get(0));
    }

    @Test
    void aReadThatFailsForAnotherReasonIsNotCountedAsACeilingBreach()
    {
        // Every read failure passes through the same catch, so discriminating on the exception's type or
        // message would file a stopped warehouse under "table too large".
        FakeJdbc jdbc = new FakeJdbc(sql -> {
            throw new IllegalStateException("the warehouse went away mid-read");
        });
        DatabricksRecordHandler handler = handlerOver(twoSources(), jdbc);

        assertThrows(RuntimeException.class, () -> handler.readWithConstraint(new CountingSpiller(),
                readRequest(CATALOG_A, "sales", "orders"), new AlwaysRunning()));

        for (String line : emitted) {
            assertFalse(line.contains(ConnectorMetrics.TABLE_CEILING_EXCEEDED),
                    "a read that failed below the ceiling is not a ceiling breach: " + line);
        }
    }

    @Test
    void aReadReturningNoRowsStillEmitsTheCountAndOpensOneConnection()
    {
        FakeJdbc jdbc = warehouseReturning(0);
        DatabricksRecordHandler handler = handlerOver(twoSources(), jdbc);

        assertDoesNotThrow(() -> handler.readWithConstraint(new CountingSpiller(),
                readRequest(CATALOG_A, "sales", "orders"), new AlwaysRunning()));

        assertTrue(emitted.get(0).contains("\"" + ConnectorMetrics.ROWS_RETURNED + "\":0"),
                emitted.get(0));
        assertEquals(1, jdbc.connectionsOpened());
        assertEquals(1, jdbc.connectionsClosed(),
                "the inherited loop closes the connection it was handed");
    }

    @Test
    void interleavedRequestsForTwoCatalogsProduceTwoDifferentFromClauses()
    throws Exception
    {
        // A query builder built at construction spells one Unity Catalog catalog into every statement for the
        // life of the container, so B's request reads main.finance: a table that either does not exist or is
        // somebody else's.
        FakeJdbc jdbc = new FakeJdbc(sql -> Collections.emptyList());
        DatabricksRecordHandler handler = handlerOver(twoSources(), jdbc);

        handler.buildSplitSql(jdbc.connection(), CATALOG_A,
                new TableName("sales", "orders"), orders(), noConstraints(), split());
        handler.buildSplitSql(jdbc.connection(), CATALOG_B,
                new TableName("finance", "orders"), orders(), noConstraints(), split());
        handler.buildSplitSql(jdbc.connection(), CATALOG_A,
                new TableName("sales", "orders"), orders(), noConstraints(), split());

        List<String> fromClauses = new ArrayList<>();
        for (FakeJdbc.Statement statement : jdbc.statements()) {
            fromClauses.add(statement.sql().substring(statement.sql().indexOf(" FROM ")));
        }
        assertEquals(Arrays.asList(
                " FROM `main`.`sales`.`orders`",
                " FROM `other`.`finance`.`orders`",
                " FROM `main`.`sales`.`orders`"), fromClauses);
    }

    @Test
    void theStatementNeverNamesTheAthenaCatalogItWasInvokedUnder()
    throws Exception
    {
        // The Athena catalog name is COA's derived digest and means nothing to Databricks. It is what
        // resolves the configuration, and it must not reach the SQL.
        FakeJdbc jdbc = new FakeJdbc(sql -> Collections.emptyList());
        handlerOver(twoSources(), jdbc).buildSplitSql(jdbc.connection(), CATALOG_A,
                new TableName("sales", "orders"), orders(), noConstraints(), split());

        assertFalse(jdbc.statements().get(0).sql().contains("coadevds"),
                jdbc.statements().get(0).sql());
    }

    @Test
    void aRequestForACatalogThisConnectorDoesNotServeIsRefused()
    throws Exception
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> Collections.emptyList());
        DatabricksRecordHandler handler = handlerOver(twoSources(), jdbc);

        assertThrows(IllegalArgumentException.class,
                () -> handler.buildSplitSql(jdbc.connection(), "coadevds_unknown",
                        new TableName("sales", "orders"), orders(), noConstraints(), split()));
        assertTrue(jdbc.statements().isEmpty(), "refused before any statement was built");
    }

    @Test
    void interleavedReadsOpenTwoDifferentConnectionsAndNeverCross()
    {
        // The loop asks its connection factory for a connection before it calls buildSplitSql, and the factory
        // receives nothing that identifies the request, so this asserts the request's catalog reaches it at
        // all. Failed deliberately at the connection, the first thing the loop does.
        MultiplexedProvider provider = twoSources();
        List<String> resolvedFor = new ArrayList<>();
        DatabricksRecordHandler handler = new DatabricksRecordHandler(
                Collections.emptyMap(), provider,
                config -> () -> {
                    resolvedFor.add(config.catalog() + '.' + config.schema());
                    // An IllegalArgumentException, because DatabricksErrors passes this connector's own
                    // errors through untouched, so the message survives to be asserted on.
                    throw new IllegalArgumentException(
                            "would open " + config.catalog() + '.' + config.schema());
                });

        assertEquals("would open main.sales", readFailureFor(handler, CATALOG_A, "sales"));
        assertEquals("would open other.finance", readFailureFor(handler, CATALOG_B, "finance"));
        assertEquals("would open main.sales", readFailureFor(handler, CATALOG_A, "sales"));

        assertEquals(Arrays.asList("main.sales", "other.finance", "main.sales"), resolvedFor);
    }

    /** Runs one read that is expected to fail at the connection, and returns the failure's message. */
    private String readFailureFor(DatabricksRecordHandler handler, String catalog, String schema)
    {
        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> handler.readWithConstraint(new CountingSpiller(),
                        readRequest(catalog, schema, "orders"), null));
        return failure.getMessage();
    }

    @Test
    void aReadOutsideItsOwnSchemaIsRefusedBeforeAnythingIsOpened()
    {
        MultiplexedProvider provider = twoSources();
        List<String> resolvedFor = new ArrayList<>();
        DatabricksRecordHandler handler = new DatabricksRecordHandler(
                Collections.emptyMap(), provider,
                config -> () -> {
                    resolvedFor.add(config.catalog());
                    throw new IllegalStateException("should not have been reached");
                });

        // Namespace A's catalog, naming namespace B's schema.
        IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
                () -> handler.readWithConstraint(new CountingSpiller(),
                        readRequest(CATALOG_A, "finance", "salaries"), null));

        assertTrue(refused.getMessage().contains("finance"), refused.getMessage());
        assertTrue(refused.getMessage().contains("sales"), refused.getMessage());
        assertTrue(resolvedFor.isEmpty(), "nothing may be opened for a refused read");
    }

    @Test
    void anEnvironmentModeDeploymentReadsItsOneEndpointThroughTheSamePath()
    {
        // The only place in the Java suite where the REAL EnvironmentConnectionConfigProvider is wired into a
        // handler — every other handler test goes through the multiplexed fake, so without this the mode
        // switch could break the default path with the whole suite green.
        Map<String, String> stageOne = new HashMap<>();
        stageOne.put(ConnectionConfig.WORKSPACE_HOSTNAME_VAR, MultiplexedProvider.HOST);
        stageOne.put(ConnectionConfig.HTTP_PATH_VAR, "/sql/1.0/warehouses/a1b234c567d8e9fa");
        stageOne.put(ConnectionConfig.CATALOG_VAR, "workspace");
        stageOne.put(ConnectionConfig.SCHEMA_VAR, "coa_dbx_test");
        stageOne.put(ConnectionConfig.CREDENTIAL_SECRET_ARN_VAR,
                "arn:aws:secretsmanager:us-east-1:111122223333:secret:dbx-AbCdEf");

        FakeJdbc jdbc = warehouseReturning(2);
        DatabricksRecordHandler handler = new DatabricksRecordHandler(stageOne,
                // fromEnvironment, not the provider directly: the factory is what a real cold start calls,
                // so this covers the mode default and the mutual-exclusion check as well.
                ConnectionConfigProviders.fromEnvironment(stageOne).provider(),
                recordingConnections(jdbc),
                new ConnectorMetrics("databricks", emitted::add));
        CountingSpiller spiller = new CountingSpiller();

        // Any Athena catalog name resolves the one endpoint in this mode, which is the whole difference.
        assertDoesNotThrow(() -> handler.readWithConstraint(spiller,
                readRequest("some_registered_catalog", "coa_dbx_test", "orders"),
                new AlwaysRunning()));

        assertEquals(2, spiller.rows);
        assertEquals(Collections.singletonList("workspace.coa_dbx_test"), opened);
        assertTrue(jdbc.statements().get(0).sql().contains(" FROM `workspace`.`coa_dbx_test`.`orders`"),
                jdbc.statements().get(0).sql());
    }

    @Test
    void anEnvironmentModeDeploymentStillRefusesASchemaOutsideItsPin()
    {
        // The pin is a containment boundary in this mode too, and it is the one a stage-1 deployer set
        // themselves — so the message names the variable, unlike the managed case below.
        Map<String, String> stageOne = new HashMap<>();
        stageOne.put(ConnectionConfig.WORKSPACE_HOSTNAME_VAR, MultiplexedProvider.HOST);
        stageOne.put(ConnectionConfig.HTTP_PATH_VAR, "/sql/1.0/warehouses/a1b234c567d8e9fa");
        stageOne.put(ConnectionConfig.CATALOG_VAR, "workspace");
        stageOne.put(ConnectionConfig.SCHEMA_VAR, "coa_dbx_test");
        stageOne.put(ConnectionConfig.CREDENTIAL_SECRET_ARN_VAR,
                "arn:aws:secretsmanager:us-east-1:111122223333:secret:dbx-AbCdEf");

        DatabricksRecordHandler handler = new DatabricksRecordHandler(stageOne,
                ConnectionConfigProviders.fromEnvironment(stageOne).provider(),
                recordingConnections(warehouseReturning(1)),
                new ConnectorMetrics("databricks", emitted::add));

        IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
                () -> handler.readWithConstraint(new CountingSpiller(),
                        readRequest("some_registered_catalog", "someone_elses_schema", "salaries"),
                        new AlwaysRunning()));

        assertTrue(refused.getMessage().contains(ConnectionConfig.SCHEMA_VAR), refused.getMessage());
        assertTrue(opened.isEmpty(), "refused before anything was opened");
    }

    @Test
    void threeSourcesAcrossTwoWorkspacesResolveIndependentlyIncludingTheSharedWorkspacePair()
    {
        // The third source is the point: two sources on ONE workspace differ only in schema, namespace, role
        // and secret, so a cache keyed on the warehouse host, the JDBC URL or the configuration's endpoint
        // half collapses them onto one credential while every two-workspace test here still passes.
        MultiplexedProvider provider = new MultiplexedProvider()
                .addManaged(CATALOG_A, "main", "sales", MultiplexedProvider.HOST)
                .addManaged(CATALOG_B, "other", "finance", MultiplexedProvider.OTHER_HOST)
                // The third: same workspace as the first, different everything else.
                .addManaged(CATALOG_C, "main", "marketing", MultiplexedProvider.HOST);
        FakeJdbc jdbc = warehouseReturning(1);
        DatabricksRecordHandler handler = handlerOver(provider, jdbc);

        for (String[] source : new String[][] {
            {CATALOG_A, "sales"}, {CATALOG_C, "marketing"}, {CATALOG_B, "finance"},
            {CATALOG_A, "sales"}, {CATALOG_C, "marketing"}}) {
            assertDoesNotThrow(() -> handler.readWithConstraint(new CountingSpiller(),
                    readRequest(source[0], source[1], "orders"), new AlwaysRunning()),
                    source[0] + '/' + source[1]);
        }

        assertEquals(Arrays.asList("main.sales", "main.marketing", "other.finance",
                "main.sales", "main.marketing"), opened);
        // And the statements: the two sources sharing a workspace must still name their own schema.
        List<String> fromClauses = new ArrayList<>();
        for (FakeJdbc.Statement statement : jdbc.statements()) {
            fromClauses.add(statement.sql().substring(statement.sql().indexOf(" FROM ")));
        }
        assertEquals(Arrays.asList(
                " FROM `main`.`sales`.`orders`",
                " FROM `main`.`marketing`.`orders`",
                " FROM `other`.`finance`.`orders`",
                " FROM `main`.`sales`.`orders`",
                " FROM `main`.`marketing`.`orders`"), fromClauses);
    }

    @Test
    void aSourceCannotReadTheSchemaOfAnotherSourceOnTheSameWorkspace()
    {
        // Both sources are in Unity Catalog catalog `main` on one warehouse, so one credential's grants could
        // well cover both schemas — and the pin refuses anyway, which is what makes it a boundary on top of
        // those grants.
        MultiplexedProvider provider = new MultiplexedProvider()
                .addManaged(CATALOG_A, "main", "sales", MultiplexedProvider.HOST)
                .addManaged(CATALOG_C, "main", "marketing", MultiplexedProvider.HOST);
        DatabricksRecordHandler handler = handlerOver(provider, warehouseReturning(1));

        IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
                () -> handler.readWithConstraint(new CountingSpiller(),
                        readRequest(CATALOG_A, "marketing", "campaigns"), new AlwaysRunning()));

        assertTrue(refused.getMessage().contains("marketing"), refused.getMessage());
        assertTrue(refused.getMessage().contains("sales"), refused.getMessage());
        assertTrue(opened.isEmpty(), "nothing may be opened for a refused read");
    }

    @Test
    void theHandlerHoldsNoMutableStateBeyondTheCachesThatAreNamedHere()
    {
        // An allowlist rather than "no mutable state", because behavioural tests cannot replace it: the
        // interleaving tests above pass even with a per-request field written on every path, since a
        // single-threaded test never interleaves the way a second concurrent invocation would. Both
        // mutable fields are named, so a NEW one fails this rather than joining them silently:
        //   queryBuilders — a ConcurrentMap keyed on the Unity Catalog catalog, so the key is the value's
        //     own identity rather than the request's.
        //   connections   — a ThreadLocal binding whose lifetime is one super.readWithConstraint call on
        //     one thread, per-thread by construction where a field is not.
        // Sorted, because getDeclaredFields() has no specified order.
        List<String> allowedMutable = new ArrayList<>(Arrays.asList("connections", "queryBuilders"));
        Collections.sort(allowedMutable);
        List<String> mutableFound = new ArrayList<>();

        for (Field field : DatabricksRecordHandler.class.getDeclaredFields()) {
            if (field.isSynthetic() || Modifier.isStatic(field.getModifiers())) {
                continue;
            }
            assertTrue(Modifier.isFinal(field.getModifiers()),
                    "field " + field.getName() + " is not final; per-request state on a federation"
                            + " handler is safe only by accident of the Lambda runtime serialising"
                            + " invocations per container");
            assertFalse(Modifier.isVolatile(field.getModifiers()),
                    "field " + field.getName() + " is volatile, which is how a smuggled per-request"
                            + " parameter looks");
            if (!isEffectivelyImmutable(field)) {
                mutableFound.add(field.getName());
            }
        }

        Collections.sort(mutableFound);
        assertEquals(allowedMutable, mutableFound,
                "the set of fields holding mutable state changed. Each one is a place a request can leave"
                        + " something behind for the next one, so add it to the list above only with a"
                        + " comment saying why it cannot: " + mutableFound);
    }

    /** Whether a field's declared type carries no mutable state of its own. */
    private static boolean isEffectivelyImmutable(Field field)
    {
        Class<?> type = field.getType();
        return type.isPrimitive()
                || type == String.class
                || dev.coa.databricks.config.ConnectionConfigProvider.class.isAssignableFrom(type)
                || ConnectorMetrics.class == type;
    }

    @Test
    void aPinnedConnectorRefusesToReadAnotherSchema()
    {
        ConnectionConfig pinned = valid().schema("sales").build();

        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> DatabricksRecordHandler.requireServableSchema(
                        pinned, new TableName("finance", "salaries")));

        assertTrue(failure.getMessage().contains("finance"), failure.getMessage());
        assertTrue(failure.getMessage().contains("sales"), failure.getMessage());
        // Set, so naming it is the actionable part: unsetting it is one of the two fixes.
        assertTrue(failure.getMessage().contains(ConnectionConfig.SCHEMA_VAR), failure.getMessage());
    }

    @Test
    void aManagedSourceIsRefusedWithoutBlamingAVariableThatDeploymentDoesNotHave()
    {
        // DATABRICKS_SCHEMA is absent from a managed deployment by construction, so naming it would send an
        // operator looking for something that is not there. The fix is a second source record.
        ConnectionConfig pinned = valid().schema("sales")
                .managedSource(dev.coa.databricks.config.ManagedSource.builder()
                        .athenaCatalogName(CATALOG_A)
                        .sourceId("src-abc123")
                        .namespaceId("ns-1")
                        .crossAccountRoleArn(
                                "arn:aws:iam::222233334444:role/coa-dev-datasource-access-sales")
                        .build())
                .build();

        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> DatabricksRecordHandler.requireServableSchema(
                        pinned, new TableName("finance", "salaries")));

        assertFalse(failure.getMessage().contains(ConnectionConfig.SCHEMA_VAR), failure.getMessage());
        assertTrue(failure.getMessage().contains("register a second source"), failure.getMessage());
    }

    @Test
    void aPinnedConnectorReadsItsOwnSchema()
    {
        ConnectionConfig pinned = valid().schema("sales").build();

        assertDoesNotThrow(() -> DatabricksRecordHandler.requireServableSchema(
                pinned, new TableName("sales", "orders")));
    }

    @Test
    void anUnpinnedConnectorLeavesTheCredentialsGrantsAsTheBoundary()
    {
        // Nothing to compare against: the unpinned mode's contract is that the credential's Unity Catalog
        // grants decide, and the metadata handler's own gate decides which names are addressable at all.
        // Only a stage-1 deployment can be unpinned; a managed source is always one schema.
        ConnectionConfig unpinned = valid().build();

        assertDoesNotThrow(() -> DatabricksRecordHandler.requireServableSchema(
                unpinned, new TableName("anything", "orders")));
    }

    @Test
    void theComparisonIsExactRatherThanCaseFolded()
    {
        // The same comparison the metadata path makes, so a name that got past one cannot fail only at the
        // other. config.schema() is already folded; a request naming "Sales" was never advertised.
        ConnectionConfig pinned = valid().schema("sales").build();

        assertThrows(IllegalArgumentException.class,
                () -> DatabricksRecordHandler.requireServableSchema(
                        pinned, new TableName("Sales", "orders")));
    }
}
