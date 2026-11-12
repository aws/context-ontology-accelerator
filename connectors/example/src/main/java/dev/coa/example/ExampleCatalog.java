// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.example;


import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/**
 * The rows the fabricated {@code example_source} database serves.
 *
 * <p>Rows only. Each table's <b>shape</b> — its columns, their prose and their declared keys — is
 * declared in {@link ExampleMetadataHandler}, which is the file to read when drafting a connector.
 * This one exists so that the example is a working fixture rather than a sketch, and so the metadata
 * handler stays about the contract.
 *
 * <p>Nothing here touches AWS, so it is unit-testable on its own — unlike the handlers, whose
 * superclass constructors build S3, Athena and Secrets Manager clients.
 *
 * <p>{@code bulk_rows} is the exception to "rows only": how many rows it serves and how wide they
 * are come from the environment, which is what makes the S3 spill path reachable at all. See
 * {@link #BULK_ROWS_OPTION}.
 */
final class ExampleCatalog
{

    /**
     * Environment variable setting {@code bulk_rows}' row count. Default
     * {@value #DEFAULT_BULK_ROWS}, i.e. deliberately too small to spill.
     *
     * <p>Also accepted upper-cased, since the SDK passes {@code System.getenv()} through
     * verbatim and shells and consoles disagree about the case of environment variables.
     */
    static final String BULK_ROWS_OPTION = "example_bulk_rows";

    /**
     * Environment variable setting the width in characters of {@code bulk_rows.payload}.
     * Default {@value #DEFAULT_BULK_ROW_BYTES}.
     */
    static final String BULK_ROW_BYTES_OPTION = "example_bulk_row_bytes";

    static final int DEFAULT_BULK_ROWS = 64;
    static final int DEFAULT_BULK_ROW_BYTES = 64;

    /** 2026-01-01 as an epoch day — DATEDAY's wire form. */
    private static final int SIGNUP_EPOCH_DAY_BASE = (int) LocalDate.of(2026, 1, 1).toEpochDay();

    private static final String[] TIERS = {"bronze", "silver", "gold"};
    private static final int CUSTOMER_ROWS = 50;
    private static final int ORDER_ROWS = 200;
    private static final int LINES_PER_ORDER = 3;
    private static final int ORDER_LINE_ROWS = ORDER_ROWS * LINES_PER_ORDER;

    private ExampleCatalog()
    {
    }

    /**
     * Builds the catalog: table name to definition, in the order {@code SHOW TABLES} should list them.
     * Only the {@code bulk_rows} sizing options are read from {@code configOptions}.
     */
    static Map<String, ExampleTable> tables(Map<String, String> configOptions)
    {
        Map<String, ExampleTable> tables = new LinkedHashMap<>();
        tables.put("customers", customers());
        tables.put("orders", orders());
        tables.put("order_lines", orderLines());
        tables.put("shipment_lines", shipmentLines());
        tables.put("bulk_rows", bulkRows(
                option(configOptions, BULK_ROWS_OPTION, DEFAULT_BULK_ROWS),
                option(configOptions, BULK_ROW_BYTES_OPTION, DEFAULT_BULK_ROW_BYTES)));
        return Collections.unmodifiableMap(tables);
    }

    private static ExampleTable customers()
    {
        return ExampleTable.named("customers")
                .rows(CUSTOMER_ROWS)
                .rowsFrom(n -> {
                    Map<String, Object> row = new HashMap<>();
                    row.put("customer_id", (long) n);
                    row.put("email", "customer" + n + "@example.com");
                    row.put("signup_date", SIGNUP_EPOCH_DAY_BASE + n);
                    row.put("tier", TIERS[n % TIERS.length]);
                    return row;
                })
                .build();
    }

    private static ExampleTable orders()
    {
        return ExampleTable.named("orders")
                .rows(ORDER_ROWS)
                .rowsFrom(n -> {
                    Map<String, Object> row = new HashMap<>();
                    row.put("order_id", orderId(n));
                    row.put("customer_id", (long) ((n % CUSTOMER_ROWS) + 1));
                    row.put("total_amount", 10.0d * n + 0.99d);
                    // Timezone-aware epoch millis; a naive LocalDateTime is rejected.
                    row.put("order_ts", LocalDateTime.of(2026, 3, 1, 12, 0).plusMinutes(n)
                            .toInstant(ZoneOffset.UTC).toEpochMilli());
                    return row;
                })
                .build();
    }

    /**
     * A composite-primary-key table: {@code (order_id, line_no)}. {@code order_id} is also a
     * foreign key onto {@code orders}, so it carries both tags.
     */
    private static ExampleTable orderLines()
    {
        return ExampleTable.named("order_lines")
                .rows(ORDER_LINE_ROWS)
                .rowsFrom(n -> {
                    Map<String, Object> row = new HashMap<>();
                    row.put("order_id", lineOrderId(n));
                    row.put("line_no", lineNo(n));
                    row.put("sku", String.format("SKU-%04d", ((n - 1) % 40) + 1));
                    row.put("quantity", (n % 5) + 1);
                    return row;
                })
                .build();
    }

    /**
     * The composite-foreign-key child: {@code (order_id, line_no)} references
     * {@code order_lines}' two-column key as two separate {@code @fk(...)} tags.
     */
    private static ExampleTable shipmentLines()
    {
        return ExampleTable.named("shipment_lines")
                .rows(ORDER_LINE_ROWS)
                .rowsFrom(n -> {
                    Map<String, Object> row = new HashMap<>();
                    row.put("shipment_line_id", 5000L + n);
                    row.put("order_id", lineOrderId(n));
                    row.put("line_no", lineNo(n));
                    row.put("shipped_ts", LocalDateTime.of(2026, 3, 2, 9, 0).plusMinutes(n)
                            .toInstant(ZoneOffset.UTC).toEpochMilli());
                    return row;
                })
                .build();
    }

    /**
     * The spill driver: {@code rowCount} rows of a {@code rowBytes}-wide string, so a single
     * split's response size is set from the environment rather than fixed by the fixture.
     */
    private static ExampleTable bulkRows(int rowCount, int rowBytes)
    {
        // One filler string, reused, with the row number stamped over its head so no two payloads
        // are byte-identical — insurance against anything in the path deduplicating them and
        // keeping the response under the limit this table exists to cross.
        final String filler = "x".repeat(rowBytes);
        return ExampleTable.named("bulk_rows")
                .rows(rowCount)
                .rowsFrom(n -> {
                    Map<String, Object> row = new HashMap<>();
                    row.put("row_id", (long) n);
                    row.put("payload", stamp(filler, n));
                    return row;
                })
                .build();
    }

    /** @return {@code orders.order_id} for the nth order row. */
    private static long orderId(int n)
    {
        return 1000L + n;
    }

    /** @return the parent {@code orders.order_id} for the nth order-line row. */
    private static long lineOrderId(int n)
    {
        return orderId(((n - 1) % ORDER_ROWS) + 1);
    }

    /** @return {@code line_no} for the nth order-line row: 1..{@value #LINES_PER_ORDER}. */
    private static long lineNo(int n)
    {
        return ((n - 1) / ORDER_ROWS) + 1L;
    }

    /**
     * A positive integer option, matched case-insensitively, falling back rather than throwing: a
     * typo in a test knob should not fail Lambda initialisation and read as "connector broken".
     */
    private static int option(Map<String, String> configOptions, String name, int fallback)
    {
        if (configOptions == null) {
            return fallback;
        }
        String raw = configOptions.get(name);
        if (raw == null) {
            raw = configOptions.get(name.toUpperCase(Locale.ROOT));
        }
        if (raw == null) {
            return fallback;
        }
        try {
            int value = Integer.parseInt(raw.trim());
            return (value > 0) ? value : fallback;
        }
        catch (NumberFormatException e) {
            return fallback;
        }
    }

    /** @return {@code filler} with the decimal form of {@code n} written over its head. */
    private static String stamp(String filler, int n)
    {
        String head = Integer.toString(n);
        if (head.length() >= filler.length()) {
            return head;
        }
        return head + filler.substring(head.length());
    }
}
