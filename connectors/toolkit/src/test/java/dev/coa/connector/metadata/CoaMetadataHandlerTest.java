// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.connector.metadata;

import com.amazonaws.athena.connector.lambda.data.BlockAllocatorImpl;
import com.amazonaws.athena.connector.lambda.domain.TableName;
import com.amazonaws.athena.connector.lambda.metadata.GetTableRequest;
import com.amazonaws.athena.connector.lambda.metadata.ListSchemasRequest;
import com.amazonaws.athena.connector.lambda.metadata.ListTablesRequest;
import com.amazonaws.athena.connector.lambda.security.FederatedIdentity;
import org.apache.arrow.vector.types.Types;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * The SPI's one structural guarantee: the Athena catalog name reaches the implementation.
 *
 * <h2>Why this is worth a test of its own</h2>
 *
 * The first version of this class passed {@code request.getCatalogName()} to the <i>response</i> and
 * not to {@link CoaMetadataHandler#listDatabases(String)}. Every connector that needed the value —
 * which is every multiplexed one — reached it by overriding a {@code doXxx} method purely to stash it
 * in a field, then reading the field back from these methods. That is mutable handler state, correct
 * only because the Lambda runtime happens to serialise invocations per container, and invisible in
 * the signature that depends on it.
 *
 * <p>So these three assertions are what stop the parameter from being quietly dropped again: a
 * regression here does not break any single-source connector, which is exactly why nothing else would
 * catch it.
 */
class CoaMetadataHandlerTest
{
    private static final FederatedIdentity IDENTITY = new FederatedIdentity(
            "arn:aws:iam::123456789012:role/query", "123456789012",
            Collections.emptyMap(), Collections.emptyList(), Collections.emptyMap());

    /** Records the catalog name each SPI method was handed. */
    private static final class RecordingHandler extends CoaMetadataHandler
    {
        private final List<String> catalogsSeen = new ArrayList<>();

        RecordingHandler()
        {
            super("recording", Collections.emptyMap());
        }

        @Override
        protected List<String> listDatabases(String catalog)
        {
            catalogsSeen.add(catalog);
            return Collections.singletonList("sales");
        }

        @Override
        protected List<String> listTables(String catalog, String database)
        {
            catalogsSeen.add(catalog);
            return Collections.singletonList("orders");
        }

        @Override
        protected CoaTable describeTable(String catalog, String database, String tableName)
        {
            catalogsSeen.add(catalog);
            return CoaTable.named(tableName)
                    .column(CoaColumn.of("id", Types.MinorType.BIGINT.getType()))
                    .build();
        }
    }

    @Test
    void listSchemaNamesHandsTheRequestsCatalogToListDatabases()
    {
        RecordingHandler handler = new RecordingHandler();

        handler.doListSchemaNames(new BlockAllocatorImpl(),
                new ListSchemasRequest(IDENTITY, "query-1", "warehouse_a"));

        assertEquals(Collections.singletonList("warehouse_a"), handler.catalogsSeen);
    }

    @Test
    void listTablesHandsTheRequestsCatalogToListTables()
    {
        RecordingHandler handler = new RecordingHandler();

        handler.doListTables(new BlockAllocatorImpl(),
                new ListTablesRequest(IDENTITY, "query-2", "warehouse_b", "sales", null, 100));

        assertEquals(Collections.singletonList("warehouse_b"), handler.catalogsSeen);
    }

    @Test
    void getTableHandsTheRequestsCatalogToDescribeTable()
    {
        RecordingHandler handler = new RecordingHandler();

        handler.doGetTable(new BlockAllocatorImpl(),
                new GetTableRequest(IDENTITY, "query-3", "warehouse_c",
                        new TableName("sales", "orders"), Collections.emptyMap()));

        assertEquals(Collections.singletonList("warehouse_c"), handler.catalogsSeen);
    }

    @Test
    void twoRequestsWithDifferentCatalogsEachSeeTheirOwn()
    {
        // The property the removed field could not offer: no ordering, no container affinity, and no
        // reliance on the runtime serialising invocations. One handler, two catalogs, each carried on
        // its own request.
        RecordingHandler handler = new RecordingHandler();
        Map<String, String> noProperties = Collections.emptyMap();

        handler.doListSchemaNames(new BlockAllocatorImpl(),
                new ListSchemasRequest(IDENTITY, "query-4", "warehouse_a"));
        handler.doGetTable(new BlockAllocatorImpl(),
                new GetTableRequest(IDENTITY, "query-5", "warehouse_b",
                        new TableName("sales", "orders"), noProperties));
        handler.doListTables(new BlockAllocatorImpl(),
                new ListTablesRequest(IDENTITY, "query-6", "warehouse_c", "sales", null, 100));

        assertEquals(java.util.Arrays.asList("warehouse_a", "warehouse_b", "warehouse_c"),
                handler.catalogsSeen);
    }
}
