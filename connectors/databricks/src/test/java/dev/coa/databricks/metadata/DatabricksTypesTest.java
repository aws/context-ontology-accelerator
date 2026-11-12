// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.metadata;

import org.apache.arrow.vector.types.Types;
import org.apache.arrow.vector.types.pojo.ArrowType;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

/** Databricks {@code full_data_type} to Arrow. */
class DatabricksTypesTest
{
    private static void maps(String databricksType, Types.MinorType expected)
    {
        assertEquals(expected.getType(), DatabricksTypes.toArrowType(databricksType),
                databricksType + " should map to " + expected);
    }

    @Test
    void mapsTheIntegerFamily()
    {
        maps("boolean", Types.MinorType.BIT);
        maps("tinyint", Types.MinorType.TINYINT);
        maps("byte", Types.MinorType.TINYINT);
        maps("smallint", Types.MinorType.SMALLINT);
        maps("short", Types.MinorType.SMALLINT);
        maps("int", Types.MinorType.INT);
        maps("integer", Types.MinorType.INT);
        maps("bigint", Types.MinorType.BIGINT);
        maps("long", Types.MinorType.BIGINT);
    }

    @Test
    void mapsTheFloatingFamily()
    {
        maps("float", Types.MinorType.FLOAT4);
        maps("real", Types.MinorType.FLOAT4);
        maps("double", Types.MinorType.FLOAT8);
    }

    @Test
    void mapsStringsAndBinary()
    {
        maps("string", Types.MinorType.VARCHAR);
        maps("varchar(255)", Types.MinorType.VARCHAR);
        maps("char(10)", Types.MinorType.VARCHAR);
        maps("binary", Types.MinorType.VARBINARY);
    }

    @Test
    void mapsDatesAndTimestamps()
    {
        maps("date", Types.MinorType.DATEDAY);
        // Arrow rejects a naive timestamp with "Unsupported Arrow Type". The inherited extractor writes
        // ResultSet.getTimestamp(...).getTime(), which is epoch millis and so zone-anchored, and
        // TIMESTAMP_NTZ carries no zone, so it is read as UTC.
        maps("timestamp", Types.MinorType.DATEMILLI);
        maps("timestamp_ntz", Types.MinorType.DATEMILLI);
        maps("timestamp_ltz", Types.MinorType.DATEMILLI);
    }

    @Test
    void keepsADecimalsPrecisionAndScale()
    {
        // Why the reader selects full_data_type rather than data_type: mapping DECIMAL(38,9) through a
        // default-scaled decimal changes every value in the column without erroring.
        assertEquals(new ArrowType.Decimal(10, 2, 128), DatabricksTypes.toArrowType("decimal(10,2)"));
        assertEquals(new ArrowType.Decimal(38, 9, 128),
                DatabricksTypes.toArrowType("DECIMAL(38, 9)"));
        assertEquals(new ArrowType.Decimal(5, 0, 128), DatabricksTypes.toArrowType("numeric(5)"));
    }

    @Test
    void aBareDecimalGetsDatabricksOwnDefault()
    {
        assertEquals(new ArrowType.Decimal(10, 0, 128), DatabricksTypes.toArrowType("decimal"));
    }

    @Test
    void everyComplexTypeBecomesVarchar()
    {
        // BlockUtils.setValue covers scalars only: handed a struct, list or map vector it throws "Unknown
        // type Struct" at read time, after deploy. The driver returns these as their string rendering while
        // EnableComplexDatatypeSupport=0, which the connection factory pins.
        for (String complex : new String[] {
            "array<string>", "map<string,int>", "struct<a:int,b:string>", "variant", "object",
            "geometry", "geography", "interval day to second", "interval year to month"}) {
            maps(complex, Types.MinorType.VARCHAR);
        }
    }

    @Test
    void anUnknownTypeFallsBackRatherThanFailingTheWholeTable()
    {
        maps("some_type_databricks_adds_in_2027", Types.MinorType.VARCHAR);
    }

    @Test
    void isCaseAndWhitespaceInsensitive()
    {
        maps("  BigInt  ", Types.MinorType.BIGINT);
        maps("TIMESTAMP", Types.MinorType.DATEMILLI);
    }

    @Test
    void refusesAMissingType()
    {
        // information_schema returning a column with no type means something is wrong upstream, and calling
        // it VARCHAR would hide that.
        assertThrows(IllegalArgumentException.class, () -> DatabricksTypes.toArrowType(null));
        assertThrows(IllegalArgumentException.class, () -> DatabricksTypes.toArrowType("  "));
    }
}
