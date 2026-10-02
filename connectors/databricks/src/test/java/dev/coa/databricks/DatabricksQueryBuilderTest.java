// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import com.amazonaws.athena.connector.lambda.data.BlockAllocator;
import com.amazonaws.athena.connector.lambda.data.BlockAllocatorImpl;
import com.amazonaws.athena.connector.lambda.data.SchemaBuilder;
import com.amazonaws.athena.connector.lambda.domain.Split;
import com.amazonaws.athena.connector.lambda.domain.predicate.Constraints;
import com.amazonaws.athena.connector.lambda.domain.predicate.OrderByField;
import com.amazonaws.athena.connector.lambda.domain.predicate.Range;
import com.amazonaws.athena.connector.lambda.domain.predicate.SortedRangeSet;
import com.amazonaws.athena.connector.lambda.domain.predicate.ValueSet;
import org.apache.arrow.vector.types.Types;
import org.apache.arrow.vector.types.pojo.Schema;
import org.junit.jupiter.api.Test;

import java.sql.SQLException;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The statement the record path sends, captured through a recorded JDBC layer. Most of it is inherited
 * from {@code athena-jdbc}; what is tested here is the {@code FROM} clause, the Unity Catalog
 * substitution, and that the inherited backtick quoting reaches the projection and predicate too.
 */
class DatabricksQueryBuilderTest
{
    private static Split split()
    {
        // No properties. The toolkit's default puts the table name on the split, and the inherited query
        // builder reads every split property as a PARTITION column: it drops that name from the projection,
        // skips its constraints, and feeds the split's value to the extractor. A Databricks table with a
        // column called "table" would return the literal table name in it for every row.
        return new Split(null, null, Collections.emptyMap());
    }

    private static Schema orders()
    {
        return SchemaBuilder.newBuilder()
                .addBigIntField("order_num")
                .addStringField("region_code")
                .addStringField("CustomerName")
                .build();
    }

    private static Constraints noConstraints()
    {
        return new Constraints(Collections.emptyMap(), Collections.emptyList(),
                Collections.emptyList(), Constraints.DEFAULT_NO_LIMIT,
                Collections.emptyMap(), null);
    }

    private static String sqlFor(Constraints constraints) throws SQLException
    {
        return sqlFor(constraints, orders());
    }

    private static String sqlFor(Constraints constraints, Schema schema) throws SQLException
    {
        FakeJdbc jdbc = new FakeJdbc(sql -> Collections.emptyList());
        new DatabricksQueryBuilder("workspace").buildSql(
                jdbc.connection(),
                // The Athena catalog name, which the builder ignores.
                "scldevds_144a95d84d98c87d",
                "coa_dbx_test",
                "orders",
                schema,
                constraints,
                split());
        return jdbc.statements().get(0).sql();
    }

    @Test
    void namesTheUnityCatalogCatalogRatherThanTheAthenaCatalog()
    throws SQLException
    {
        // The base class passes ReadRecordsRequest.getCatalogName() down, which is COA's derived Athena
        // catalog name and means nothing to Databricks. Using it produces SQL naming a catalog that does
        // not exist.
        String sql = sqlFor(noConstraints());
        assertTrue(sql.contains("FROM `workspace`.`coa_dbx_test`.`orders`"), sql);
        assertTrue(!sql.contains("scldevds"), sql);
    }

    @Test
    void quotesEveryProjectedColumnWithBackticks()
    throws SQLException
    {
        // Backticks, not double quotes. With ANSI mode off, SELECT "CustomerName" is a string literal and
        // every row comes back with the column's own name in it, no error anywhere.
        String sql = sqlFor(noConstraints());
        assertTrue(sql.startsWith("SELECT `order_num`, `region_code`, `CustomerName` FROM "), sql);
    }

    @Test
    void preservesAColumnNamesCase()
    throws SQLException
    {
        // information_schema lower-cases table names but preserves column names, and the preserved spelling
        // has to reach the warehouse.
        assertTrue(sqlFor(noConstraints()).contains("`CustomerName`"));
    }

    @Test
    void appendsALimitWhenAthenaPushedOne()
    throws SQLException
    {
        Constraints withLimit = new Constraints(Collections.emptyMap(), Collections.emptyList(),
                Collections.emptyList(), 25L, Collections.emptyMap(), null);
        assertTrue(sqlFor(withLimit).endsWith(" LIMIT 25"), sqlFor(withLimit));
    }

    @Test
    void appendsNoLimitWhenAthenaPushedNone()
    throws SQLException
    {
        assertTrue(!sqlFor(noConstraints()).contains("LIMIT"), sqlFor(noConstraints()));
    }

    @Test
    void appendsATopNAsOrderByThenLimit()
    throws SQLException
    {
        Constraints topN = new Constraints(Collections.emptyMap(), Collections.emptyList(),
                Collections.singletonList(
                        new OrderByField("order_num", OrderByField.Direction.DESC_NULLS_LAST)),
                10L, Collections.emptyMap(), null);
        String sql = sqlFor(topN);
        assertTrue(sql.contains("ORDER BY `order_num` DESC NULLS LAST"), sql);
        assertTrue(sql.endsWith(" LIMIT 10"), sql);
    }

    @Test
    void turnsAnEqualityConstraintIntoABoundParameter()
    throws SQLException
    {
        try (BlockAllocator allocator = new BlockAllocatorImpl()) {
            Map<String, ValueSet> summary = new HashMap<>();
            summary.put("region_code", SortedRangeSet.of(
                    Range.equal(allocator, Types.MinorType.VARCHAR.getType(), "emea")));
            Constraints constraints = new Constraints(summary, Collections.emptyList(),
                    Collections.emptyList(), Constraints.DEFAULT_NO_LIMIT,
                    Collections.emptyMap(), null);

            String sql = sqlFor(constraints);
            // A "?" and never a literal, which is what makes the predicate path immune to injection
            // through a constraint value.
            assertTrue(sql.contains("WHERE (`region_code` = ?)"), sql);
            assertTrue(!sql.contains("emea"), sql);
        }
    }

    @Test
    void turnsARangeConstraintIntoBoundParameters()
    throws SQLException
    {
        try (BlockAllocator allocator = new BlockAllocatorImpl()) {
            Map<String, ValueSet> summary = new HashMap<>();
            summary.put("order_num", SortedRangeSet.of(Range.range(
                    allocator, Types.MinorType.BIGINT.getType(), 10L, true, 20L, false)));
            Constraints constraints = new Constraints(summary, Collections.emptyList(),
                    Collections.emptyList(), Constraints.DEFAULT_NO_LIMIT,
                    Collections.emptyMap(), null);

            String sql = sqlFor(constraints);
            assertTrue(sql.contains("`order_num` >= ?"), sql);
            assertTrue(sql.contains("`order_num` < ?"), sql);
        }
    }

    @Test
    void turnsSeveralSingleValuesIntoAnInList()
    throws SQLException
    {
        try (BlockAllocator allocator = new BlockAllocatorImpl()) {
            Map<String, ValueSet> summary = new HashMap<>();
            summary.put("region_code", SortedRangeSet.of(
                    Range.equal(allocator, Types.MinorType.VARCHAR.getType(), "emea"),
                    Range.equal(allocator, Types.MinorType.VARCHAR.getType(), "amer")));
            Constraints constraints = new Constraints(summary, Collections.emptyList(),
                    Collections.emptyList(), Constraints.DEFAULT_NO_LIMIT,
                    Collections.emptyMap(), null);

            String sql = sqlFor(constraints);
            assertTrue(sql.contains("`region_code` IN (?,?)"), sql);
        }
    }

    @Test
    void emitsNoPartitionPredicates()
    throws SQLException
    {
        // One split per table, so there is no partition to restrict to. A stray predicate here filters rows
        // out of every read.
        assertEquals(Collections.emptyList(),
                new DatabricksQueryBuilder("workspace").getPartitionWhereClauses(split()));
    }

    @Test
    void refusesATableThatArrivedWithNoSchemaName()
    {
        // Without the schema the Unity Catalog namespace cannot be completed, and the alternative spellings
        // all resolve to some other table.
        DatabricksQueryBuilder builder = new DatabricksQueryBuilder("workspace");
        assertThrows(IllegalArgumentException.class,
                () -> builder.getFromClauseWithSplit("cat", null, "orders", split()));
        assertThrows(IllegalArgumentException.class,
                () -> builder.getFromClauseWithSplit("cat", "", "orders", split()));
    }

    @Test
    void aNameContainingABacktickCannotEscapeTheFromClause()
    {
        String from = new DatabricksQueryBuilder("work`space")
                .getFromClauseWithSplit("cat", "sch`ema", "or`ders", split());
        assertEquals(" FROM `work``space`.`sch``ema`.`or``ders`", from);
    }

    @Test
    void refusesANullUnityCatalog()
    {
        assertThrows(NullPointerException.class, () -> new DatabricksQueryBuilder(null));
    }

    @Test
    void projectsOnlyTheColumnsTheRequestAsksFor()
    throws SQLException
    {
        // The request's schema carries only the columns the query needs, and the statement has to match it
        // or the extractors and the result set disagree.
        Schema projected = SchemaBuilder.newBuilder().addStringField("region_code").build();
        String sql = sqlFor(noConstraints(), projected);
        assertTrue(sql.startsWith("SELECT `region_code` FROM "), sql);
    }

    @Test
    void theWholeStatementIsOneLineForALoggableShape()
    throws SQLException
    {
        List<String> lines = Arrays.asList(sqlFor(noConstraints()).split("\n"));
        assertEquals(1, lines.size(), "generated SQL should be a single line");
    }
}
