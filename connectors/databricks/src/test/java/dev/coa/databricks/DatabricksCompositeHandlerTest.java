// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import com.amazonaws.athena.connector.lambda.data.BlockAllocatorImpl;
import com.amazonaws.athena.connector.lambda.data.SchemaBuilder;
import com.amazonaws.athena.connector.lambda.domain.Split;
import com.amazonaws.athena.connector.lambda.domain.TableName;
import com.amazonaws.athena.connector.lambda.domain.predicate.Constraints;
import com.amazonaws.athena.connector.lambda.handlers.CompositeHandler;
import com.amazonaws.athena.connector.lambda.metadata.ListSchemasRequest;
import com.amazonaws.athena.connector.lambda.records.ReadRecordsRequest;
import com.amazonaws.athena.connector.lambda.security.FederatedIdentity;
import dev.coa.databricks.config.ConnectionConfig;
import dev.coa.databricks.config.ConnectionConfigProvider;
import dev.coa.databricks.config.CredentialSource;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * Both halves must share one provider. {@link CompositeHandler} holds both for the container's life, so a
 * provider per half means two of every cache and every Parameter Store read paid twice per TTL window.
 */
class DatabricksCompositeHandlerTest
{
    private static final FederatedIdentity IDENTITY = new FederatedIdentity(
            "arn:aws:iam::123456789012:role/query", "123456789012",
            Collections.emptyMap(), Collections.emptyList(), Collections.emptyMap());

    private static final String CATALOG = "coadevds_144a95d84d98c87d";

    private static Map<String, String> singleEndpointEnvironment()
    {
        Map<String, String> environment = new HashMap<>();
        environment.put(ConnectionConfig.WORKSPACE_HOSTNAME_VAR, MultiplexedProvider.HOST);
        environment.put(ConnectionConfig.HTTP_PATH_VAR, "/sql/1.0/warehouses/a1b234c567d8e9fa");
        environment.put(ConnectionConfig.CATALOG_VAR, "main");
        environment.put(ConnectionConfig.CREDENTIAL_SECRET_ARN_VAR,
                "arn:aws:secretsmanager:us-east-1:111122223333:secret:dbx-AbCdEf");
        return environment;
    }

    @Test
    void bothHalvesResolveThroughOneProvider()
    {
        DatabricksCompositeHandler container =
                new DatabricksCompositeHandler(singleEndpointEnvironment());

        assertSame(fieldValue(metadataHalf(container), "configs"),
                fieldValue(recordHalf(container), "configs"),
                "one container, one configuration cache");
    }

    @Test
    void aCatalogIsResolvedOnceAcrossAMetadataCallAndThenARecordCall()
    {
        OneReadPerCatalog parameters = new OneReadPerCatalog();
        DatabricksCompositeHandler container = new DatabricksCompositeHandler(
                Collections.emptyMap(), parameters,
                new CredentialSource(config -> "{\"token\": \"dapi-example\"}"));

        assertEquals(Collections.singletonList("sales"),
                new ArrayList<>(metadataHalf(container)
                        .doListSchemaNames(new BlockAllocatorImpl(),
                                new ListSchemasRequest(IDENTITY, "query-metadata", CATALOG))
                        .getSchemas()));

        // The schema pin is checked after the configuration resolves and before any connection opens, so
        // the resolution still happens with null spiller and status checker.
        assertThrows(IllegalArgumentException.class,
                () -> recordHalf(container).readWithConstraint(
                        null, readRequest("finance", "orders"), null));

        assertEquals(Collections.singletonList(CATALOG), parameters.store.asked,
                "one read for the container, not one per half");
    }

    private static final class OneReadPerCatalog implements ConnectionConfigProvider
    {
        private final MultiplexedProvider store =
                new MultiplexedProvider().addManaged(CATALOG, "main", "sales");
        private final Map<String, ConnectionConfig> cached = new HashMap<>();

        @Override
        public ConnectionConfig configFor(String athenaCatalogName)
        {
            ConnectionConfig hit = cached.get(athenaCatalogName);
            if (hit != null) {
                return hit;
            }
            ConnectionConfig config = store.configFor(athenaCatalogName);
            cached.put(athenaCatalogName, config);
            return config;
        }
    }

    private static ReadRecordsRequest readRequest(String schema, String table)
    {
        return new ReadRecordsRequest(IDENTITY, CATALOG, "query-record",
                new TableName(schema, table),
                SchemaBuilder.newBuilder().addBigIntField("order_num").build(),
                new Split(null, null, Collections.emptyMap()),
                new Constraints(Collections.emptyMap(), Collections.emptyList(),
                        Collections.emptyList(), Constraints.DEFAULT_NO_LIMIT,
                        Collections.emptyMap(), null),
                1_000_000L, 1_000_000L);
    }

    private static DatabricksMetadataHandler metadataHalf(DatabricksCompositeHandler container)
    {
        return (DatabricksMetadataHandler) compositeField(container, "metadataHandler");
    }

    private static DatabricksRecordHandler recordHalf(DatabricksCompositeHandler container)
    {
        return (DatabricksRecordHandler) compositeField(container, "recordHandler");
    }

    private static Object compositeField(DatabricksCompositeHandler container, String name)
    {
        try {
            Field field = CompositeHandler.class.getDeclaredField(name);
            field.setAccessible(true);
            return field.get(container);
        }
        catch (ReflectiveOperationException cause) {
            throw new AssertionError("the SDK renamed " + name, cause);
        }
    }

    private static Object fieldValue(Object handler, String name)
    {
        try {
            Field field = handler.getClass().getDeclaredField(name);
            field.setAccessible(true);
            return field.get(handler);
        }
        catch (ReflectiveOperationException cause) {
            throw new AssertionError(name + " is no longer a field of " + handler.getClass(), cause);
        }
    }
}
