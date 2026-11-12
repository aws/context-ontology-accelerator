// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.metadata;

import org.apache.arrow.vector.types.Types;
import org.apache.arrow.vector.types.pojo.ArrowType;

import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Maps a Databricks {@code full_data_type} to the Arrow type the connector serves it as.
 *
 * <p>Every complex type becomes {@code VARCHAR}. {@code BlockUtils.setValue}, and the SDK's whole
 * extractor mechanism, covers scalars only: handed a struct, list or map vector it throws
 * {@code Unknown type Struct} at read time, after deploy. So {@code ARRAY<>}, {@code MAP<>},
 * {@code STRUCT<>}, {@code VARIANT}, {@code OBJECT}, {@code GEOMETRY} and {@code GEOGRAPHY} are
 * declared {@code VARCHAR} and read with {@code getString}, which is what the driver returns for them
 * while {@code EnableComplexDatatypeSupport=0}, the driver's default, pinned by
 * {@link dev.coa.databricks.jdbc.DatabricksConnectionFactory}. The value is the column's JSON
 * rendering: usable in a projection, not as a predicate target.
 *
 * <p>Decimals keep their precision and scale, which is why the caller reads {@code full_data_type} and
 * not {@code data_type}: {@code DECIMAL(38,9)} mapped through a default-scaled decimal changes every
 * value in the column without erroring.
 *
 * <p>Timestamps are {@code DATEMILLI}, since Arrow rejects a naive timestamp with
 * {@code Unsupported Arrow Type}. The inherited {@code JdbcRecordHandler.makeExtractor} writes
 * {@code ResultSet.getTimestamp(...).getTime()}, which is epoch millis and so zone-anchored, and
 * {@code TIMESTAMP_NTZ} carries no zone, so it is read as UTC. Reading it as local time would shift
 * values by the Lambda's offset.
 */
public final class DatabricksTypes
{
    /** {@code decimal(10,2)}, {@code DECIMAL(38, 9)}, {@code numeric(5)}. */
    private static final Pattern DECIMAL =
            Pattern.compile("^(?:decimal|dec|numeric)\\s*\\(\\s*(\\d+)\\s*(?:,\\s*(\\d+)\\s*)?\\)$");

    /** Databricks' default when a decimal is declared without arguments. */
    private static final int DEFAULT_DECIMAL_PRECISION = 10;

    private DatabricksTypes()
    {
    }

    /**
     * @param fullDataType a value from {@code information_schema.columns.full_data_type}, e.g.
     *                     {@code bigint}, {@code decimal(10,2)}, {@code array<string>}.
     * @return never null. An unrecognised type falls back to {@code VARCHAR} rather than failing the
     *         whole table, since one exotic column should not make a schema undiscoverable.
     * @throws IllegalArgumentException if null or blank, which would mean {@code information_schema}
     *                                 returned a column with no type.
     */
    public static ArrowType toArrowType(String fullDataType)
    {
        if (fullDataType == null || fullDataType.trim().isEmpty()) {
            throw new IllegalArgumentException("Column type must not be null or blank");
        }
        String type = fullDataType.trim().toLowerCase(Locale.ROOT);

        Matcher decimal = DECIMAL.matcher(type);
        if (decimal.matches()) {
            int precision = Integer.parseInt(decimal.group(1));
            int scale = (decimal.group(2) == null) ? 0 : Integer.parseInt(decimal.group(2));
            return new ArrowType.Decimal(precision, scale, 128);
        }
        // Bare "decimal"/"numeric": Databricks' own default is DECIMAL(10,0).
        if ("decimal".equals(type) || "dec".equals(type) || "numeric".equals(type)) {
            return new ArrowType.Decimal(DEFAULT_DECIMAL_PRECISION, 0, 128);
        }

        // Strip an argument list so char(10), varchar(255) and interval spellings fall through to
        // the switch on their base name.
        String base = type;
        int parenthesis = base.indexOf('(');
        if (parenthesis > 0) {
            base = base.substring(0, parenthesis).trim();
        }
        int angle = base.indexOf('<');
        if (angle > 0) {
            base = base.substring(0, angle).trim();
        }

        switch (base) {
            case "boolean":
                return Types.MinorType.BIT.getType();
            case "tinyint":
            case "byte":
                return Types.MinorType.TINYINT.getType();
            case "smallint":
            case "short":
                return Types.MinorType.SMALLINT.getType();
            case "int":
            case "integer":
                return Types.MinorType.INT.getType();
            case "bigint":
            case "long":
                return Types.MinorType.BIGINT.getType();
            case "float":
            case "real":
                return Types.MinorType.FLOAT4.getType();
            case "double":
                return Types.MinorType.FLOAT8.getType();
            case "date":
                return Types.MinorType.DATEDAY.getType();
            case "timestamp":
            case "timestamp_ntz":
            case "timestamp_ltz":
                return Types.MinorType.DATEMILLI.getType();
            case "binary":
                return Types.MinorType.VARBINARY.getType();
            default:
                // ARRAY, MAP, STRUCT, VARIANT, OBJECT, GEOMETRY, GEOGRAPHY, every INTERVAL spelling, and
                // anything Databricks adds later.
                return Types.MinorType.VARCHAR.getType();
        }
    }
}
