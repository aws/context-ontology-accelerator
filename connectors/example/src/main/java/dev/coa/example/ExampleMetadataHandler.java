// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.example;

import dev.coa.connector.metadata.CoaColumn;
import dev.coa.connector.metadata.CoaMetadataHandler;
import dev.coa.connector.metadata.CoaTable;
import org.apache.arrow.vector.types.Types;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Metadata half of the example connector — and the file to read first when drafting your own.
 *
 * <p>Everything a connector's metadata half has to say is here: which databases, which tables, and
 * what each table looks like. Extending {@link CoaMetadataHandler} rather than the SDK's
 * {@code MetadataHandler} means no {@code GetTableResponse}, no {@code TableName}, no
 * {@code BlockAllocator}, no {@code Split}, and no need to know that declared keys travel inside
 * column comments or where in an Arrow schema a comment has to sit. Partitions and splits come from
 * the base class's unpartitioned defaults.
 *
 * <p>Keys are declared as <b>intent</b> — {@code .primaryKey()}, {@code .foreignKey(...)} — on the
 * column they belong to. The toolkit turns that into the {@code @pk} / {@code @fk} tags and puts
 * them where Athena reads them; see {@link CoaTable#toTableSchema()} if you want to know how.
 *
 * <h2>What the fixture demonstrates</h2>
 *
 * <ul>
 *   <li><b>Single-column PK</b> — {@code customers.customer_id}, {@code orders.order_id}.</li>
 *   <li><b>Single-column FK</b> — {@code orders.customer_id} onto {@code customers.customer_id}.</li>
 *   <li><b>Composite PK</b> — {@code order_lines} is keyed on {@code (order_id, line_no)}: one
 *       {@code .primaryKey()} on each, no ordinal, key order taken from column order.</li>
 *   <li><b>Composite FK</b> — {@code shipment_lines} references that two-column key with one
 *       {@code .foreignKey(...)} per participating child column. The format's subtlest case: there
 *       is deliberately no way to group the two, because COA stores a composite foreign key as N
 *       single-column records — exactly as its JDBC path already flattens them.</li>
 *   <li><b>A column that is both</b> — {@code order_lines.order_id} is a primary-key member and a
 *       foreign key at once.</li>
 *   <li><b>The same column name meaning different things</b> — {@code customers.customer_id} is a
 *       primary key where {@code orders.customer_id} is a foreign key, which is why keys are
 *       declared per column rather than in one flat map.</li>
 *   <li><b>Spill</b> — {@code bulk_rows}, sized from the environment; see {@link ExampleCatalog}.</li>
 * </ul>
 *
 * <p>The rows these tables serve live in {@link ExampleCatalog}, and {@link ExampleRecordHandler}
 * writes them. A real connector would fetch both shape and rows from its source; the split here is
 * so that this file stays about the contract and nothing else.
 */
public class ExampleMetadataHandler extends CoaMetadataHandler
{
    /** Short name for the source type; the SDK uses it in metrics and log lines. */
    private static final String SOURCE_TYPE = "example_source";

    /** The single database this connector serves. Athena calls it a schema. */
    static final String DATABASE = "example_source";

    /** Table name to shape. Fixed: nothing about a table's columns depends on configuration. */
    private static final Map<String, CoaTable> TABLES = declareTables();

    public ExampleMetadataHandler(Map<String, String> configOptions)
    {
        super(SOURCE_TYPE, configOptions);
    }

    /**
     * @param catalog the Athena catalog name of the request. Ignored here, and that is the normal
     *                case for a single-source connector — see {@link CoaMetadataHandler}'s note on
     *                why the parameter exists anyway. A MULTIPLEXED connector selects its endpoint,
     *                credential and configuration on this value.
     */
    @Override
    protected List<String> listDatabases(String catalog)
    {
        return Collections.singletonList(DATABASE);
    }

    /** @param catalog ignored; see {@link #listDatabases(String)}. */
    @Override
    protected List<String> listTables(String catalog, String database)
    {
        requireKnownDatabase(database);
        return new ArrayList<>(TABLES.keySet());
    }

    /** @param catalog ignored; see {@link #listDatabases(String)}. */
    @Override
    protected CoaTable describeTable(String catalog, String database, String tableName)
    {
        requireKnownDatabase(database);
        CoaTable table = TABLES.get(tableName);
        if (table == null) {
            throw new IllegalArgumentException("Unknown table: " + tableName);
        }
        return table;
    }

    /**
     * Rejects a database this connector never advertised. Without it, Athena answers
     * {@code SELECT * FROM example.made_up.customers} with this connector's real rows, which reads
     * as the made-up database existing.
     *
     * @throws IllegalArgumentException if the name is not the one {@link #listDatabases(String)} returns.
     */
    private static void requireKnownDatabase(String database)
    {
        if (!DATABASE.equals(database)) {
            throw new IllegalArgumentException(
                    "Unknown database: " + database + " (this connector serves only " + DATABASE + ")");
        }
    }

    /**
     * Every table this connector serves, in the order {@code SHOW TABLES} should list them.
     *
     * <p>Package-private so the fixture's tests can assert on the shapes without constructing this
     * handler — {@code MetadataHandler}'s constructor builds S3, Athena and Secrets Manager clients,
     * so instantiating one needs credentials and a region.
     *
     * @return table name to shape.
     */
    static Map<String, CoaTable> declareTables()
    {
        Map<String, CoaTable> tables = new LinkedHashMap<>();
        tables.put("customers", CoaTable.named("customers")
                .column(CoaColumn.of("customer_id", Types.MinorType.BIGINT.getType())
                        .describedAs("Surrogate key for the customer")
                        .primaryKey())
                .column(CoaColumn.of("email", Types.MinorType.VARCHAR.getType())
                        .describedAs("Primary contact email address"))
                .column(CoaColumn.of("signup_date", Types.MinorType.DATEDAY.getType())
                        .describedAs("Date the customer registered"))
                .column(CoaColumn.of("tier", Types.MinorType.VARCHAR.getType())
                        .describedAs("Loyalty tier: bronze, silver or gold"))
                .build());

        tables.put("orders", CoaTable.named("orders")
                .column(CoaColumn.of("order_id", Types.MinorType.BIGINT.getType())
                        .describedAs("Surrogate key for the order")
                        .primaryKey())
                .column(CoaColumn.of("customer_id", Types.MinorType.BIGINT.getType())
                        .describedAs("Customer that placed the order")
                        .foreignKey("customers", "customer_id"))
                .column(CoaColumn.of("total_amount", Types.MinorType.FLOAT8.getType())
                        .describedAs("Order total in account currency"))
                .column(CoaColumn.of("order_ts", Types.MinorType.DATEMILLI.getType())
                        .describedAs("When the order was placed"))
                .build());

        tables.put("order_lines", CoaTable.named("order_lines")
                .column(CoaColumn.of("order_id", Types.MinorType.BIGINT.getType())
                        .describedAs("Order this line belongs to")
                        .primaryKey()
                        .foreignKey("orders", "order_id"))
                .column(CoaColumn.of("line_no", Types.MinorType.BIGINT.getType())
                        .describedAs("Position of this line within the order")
                        .primaryKey())
                .column(CoaColumn.of("sku", Types.MinorType.VARCHAR.getType())
                        .describedAs("Stock keeping unit ordered"))
                .column(CoaColumn.of("quantity", Types.MinorType.INT.getType())
                        .describedAs("Units ordered on this line"))
                .build());

        tables.put("shipment_lines", CoaTable.named("shipment_lines")
                .column(CoaColumn.of("shipment_line_id", Types.MinorType.BIGINT.getType())
                        .describedAs("Surrogate key for the shipped line")
                        .primaryKey())
                .column(CoaColumn.of("order_id", Types.MinorType.BIGINT.getType())
                        .describedAs("Order of the line being shipped")
                        .foreignKey("order_lines", "order_id"))
                .column(CoaColumn.of("line_no", Types.MinorType.BIGINT.getType())
                        .describedAs("Line number of the line being shipped")
                        .foreignKey("order_lines", "line_no"))
                .column(CoaColumn.of("shipped_ts", Types.MinorType.DATEMILLI.getType())
                        .describedAs("When the line shipped"))
                .build());

        tables.put("bulk_rows", CoaTable.named("bulk_rows")
                .column(CoaColumn.of("row_id", Types.MinorType.BIGINT.getType())
                        .describedAs("Sequential row number")
                        .primaryKey())
                .column(CoaColumn.of("payload", Types.MinorType.VARCHAR.getType())
                        .describedAs("Filler text; its width comes from "
                                + ExampleCatalog.BULK_ROW_BYTES_OPTION))
                .build());

        return Collections.unmodifiableMap(tables);
    }
}
