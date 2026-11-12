// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.connector.metadata;

import com.amazonaws.athena.connector.lambda.QueryStatusChecker;
import com.amazonaws.athena.connector.lambda.data.BlockAllocator;
import com.amazonaws.athena.connector.lambda.data.BlockWriter;
import com.amazonaws.athena.connector.lambda.domain.Split;
import com.amazonaws.athena.connector.lambda.domain.TableName;
import com.amazonaws.athena.connector.lambda.handlers.MetadataHandler;
import com.amazonaws.athena.connector.lambda.metadata.GetSplitsRequest;
import com.amazonaws.athena.connector.lambda.metadata.GetSplitsResponse;
import com.amazonaws.athena.connector.lambda.metadata.GetTableLayoutRequest;
import com.amazonaws.athena.connector.lambda.metadata.GetTableRequest;
import com.amazonaws.athena.connector.lambda.metadata.GetTableResponse;
import com.amazonaws.athena.connector.lambda.metadata.ListSchemasRequest;
import com.amazonaws.athena.connector.lambda.metadata.ListSchemasResponse;
import com.amazonaws.athena.connector.lambda.metadata.ListTablesRequest;
import com.amazonaws.athena.connector.lambda.metadata.ListTablesResponse;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * Base class for the metadata half of a COA connector. Subclass this, not the SDK's
 * {@link MetadataHandler}.
 *
 * <p>The SDK asks for five methods, four of them the same boilerplate in every simple connector and
 * all five speaking in Athena request and response objects. This implements all five and asks for
 * three that speak about a source instead:
 *
 * <pre>{@code
 * public class YourMetadataHandler extends CoaMetadataHandler {
 *     public YourMetadataHandler(Map<String, String> configOptions) {
 *         super("your_source", configOptions);
 *     }
 *
 *     protected List<String> listDatabases(String catalog) { ... }
 *     protected List<String> listTables(String catalog, String database) { ... }
 *     protected CoaTable describeTable(String catalog, String database, String tableName) { ... }
 * }
 * }</pre>
 *
 * No {@code GetTableResponse}, {@code TableName}, {@code BlockAllocator}, {@code Split} or
 * {@code SpillLocation} — and, the part that matters most, no need to know that declared keys travel
 * inside column comments or where in an Arrow schema a comment has to sit. Declare a column
 * {@code .primaryKey()} and it arrives.
 *
 * <p>Two things it does not hide. Arrow types, since {@link CoaColumn} takes an {@code ArrowType} —
 * Arrow is a type vocabulary, not part of the SDK. And the record half, which still speaks in
 * {@code Block} and {@code BlockSpiller}; see the example connector for the one rule that matters
 * there, writing only the columns present in the request's schema.
 *
 * <p>{@link #getPartitions} and {@link #doGetSplits} default to unpartitioned: one split covering
 * the whole table, carrying its name under {@link #SPLIT_TABLE_PROPERTY}. A partitioned source
 * overrides them. None of the five is {@code final} — overriding {@link #doGetTable} means taking on
 * the comment placement, so prefer calling {@code super} and adjusting the result.
 *
 * <h2>Why all three take the Athena catalog name</h2>
 *
 * A connector serving one source will ignore it, but a multiplexed one selects its endpoint, credential
 * and configuration by it, and would otherwise have to stash it in a handler field.
 */
public abstract class CoaMetadataHandler extends MetadataHandler
{
    /** Split property carrying the table name, so a record handler can read it back. */
    public static final String SPLIT_TABLE_PROPERTY = "table";

    /**
     * @param sourceType    short name for the source type, e.g. {@code sap_hana}; used in metrics.
     * @param configOptions the Lambda's environment, as handed to the handler.
     */
    protected CoaMetadataHandler(String sourceType, Map<String, String> configOptions)
    {
        super(sourceType, configOptions);
    }

    /**
     * @param catalog the Athena catalog name of the request being served — see the class javadoc on why
     *                all three of these receive it.
     * @return the databases, which Athena calls schemas, this connector serves.
     */
    protected abstract List<String> listDatabases(String catalog);

    /**
     * @param catalog  the Athena catalog name of the request being served.
     * @param database the database to list.
     * @return the tables in {@code database}, in the order {@code SHOW TABLES} should list them.
     */
    protected abstract List<String> listTables(String catalog, String database);

    /**
     * @param catalog   the Athena catalog name of the request being served.
     * @param database  the database the table is in.
     * @param tableName the table.
     * @return one table's columns, types, prose and declared keys.
     * @throws RuntimeException if the table is unknown; the message reaches the user's query.
     */
    protected abstract CoaTable describeTable(String catalog, String database, String tableName);

    @Override
    public ListSchemasResponse doListSchemaNames(BlockAllocator allocator, ListSchemasRequest request)
    {
        return new ListSchemasResponse(request.getCatalogName(), listDatabases(request.getCatalogName()));
    }

    @Override
    public ListTablesResponse doListTables(BlockAllocator allocator, ListTablesRequest request)
    {
        List<TableName> names = new ArrayList<>();
        for (String table : listTables(request.getCatalogName(), request.getSchemaName())) {
            names.add(new TableName(request.getSchemaName(), table));
        }
        // Null nextToken means "that is all". A source with more tables than fit should override
        // this and page.
        return new ListTablesResponse(request.getCatalogName(), names, null);
    }

    @Override
    public GetTableResponse doGetTable(BlockAllocator allocator, GetTableRequest request)
    {
        CoaTable table = describeTable(
                request.getCatalogName(),
                request.getTableName().getSchemaName(),
                request.getTableName().getTableName());
        if (table == null) {
            throw new IllegalArgumentException(
                    "Unknown table: " + request.getTableName().getQualifiedTableName());
        }
        return new GetTableResponse(
                request.getCatalogName(),
                request.getTableName(),
                table.toTableSchema().toArrowSchema(),
                Collections.emptySet());
    }

    /**
     * Unpartitioned: a single implicit partition, so nothing is written. Override for a partitioned
     * source, declaring its columns via {@code enhancePartitionSchema} first.
     */
    @Override
    public void getPartitions(BlockWriter blockWriter, GetTableLayoutRequest request,
                              QueryStatusChecker queryStatusChecker)
    {
        // Nothing to write.
    }

    /**
     * One split covering the whole table, carrying its name under {@link #SPLIT_TABLE_PROPERTY} — so
     * one response has to carry the whole table, which is what makes the spill path reachable.
     *
     * <p>{@code makeSpillLocation} reads {@code spill_bucket} and {@code spill_prefix} from the
     * environment. With no bucket it still returns a location, with a null bucket, and the SDK's
     * check short-circuits — so only a response large enough to spill fails.
     *
     * <p>Override to split a source that can be read in parallel.
     */
    @Override
    public GetSplitsResponse doGetSplits(BlockAllocator allocator, GetSplitsRequest request)
    {
        Split split = Split.newBuilder(makeSpillLocation(request), makeEncryptionKey())
                .add(SPLIT_TABLE_PROPERTY, request.getTableName().getTableName())
                .build();
        return new GetSplitsResponse(request.getCatalogName(), split);
    }
}
