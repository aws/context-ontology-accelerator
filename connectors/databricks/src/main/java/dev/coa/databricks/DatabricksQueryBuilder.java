// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import com.amazonaws.athena.connector.lambda.domain.Split;
import com.amazonaws.athena.connectors.jdbc.manager.JdbcSplitQueryBuilder;
import dev.coa.databricks.jdbc.Identifiers;

import java.util.Collections;
import java.util.List;
import java.util.Objects;

/**
 * Spells the {@code FROM} clause for Databricks. Everything else about the statement is inherited.
 *
 * <p>{@link JdbcSplitQueryBuilder} turns Athena's constraint model into a prepared statement: the
 * projection, the {@code WHERE} clause, typed parameter binding, {@code ORDER BY} and {@code LIMIT}.
 *
 * <p>The catalog Athena names is not the catalog this reads. The base class passes
 * {@code ReadRecordsRequest.getCatalogName()} into {@link #getFromClauseWithSplit}, and that is the
 * Athena data catalog name, a COA-derived string such as {@code scldevds_144a95d84d98c87d} which means
 * nothing to Databricks. The Unity Catalog catalog comes from this connector's configuration, so the
 * argument is ignored; using it would produce SQL naming a catalog that does not exist.
 *
 * <p>Quoting is backticks, passed to {@code super} so the inherited projection, predicate and
 * {@code ORDER BY} clauses use it too. With ANSI mode off, {@code "customer_id"} is a string literal and
 * every value in the column comes back wrong with no error anywhere. See {@link Identifiers}.
 */
public class DatabricksQueryBuilder extends JdbcSplitQueryBuilder
{
    private final String unityCatalog;

    /** @param unityCatalog the Unity Catalog catalog this connector exposes, unquoted. */
    public DatabricksQueryBuilder(String unityCatalog)
    {
        // The base class doubles this character inside an identifier, which is Databricks' own escaping
        // rule for a delimited identifier.
        super(String.valueOf(Identifiers.QUOTE));
        this.unityCatalog = Objects.requireNonNull(unityCatalog, "unityCatalog");
    }

    /**
     * {@code  FROM `catalog`.`schema`.`table`}, with the leading space the base class appends onto the
     * projection list.
     *
     * @param catalog Athena's catalog name, ignored in favour of this connector's own configuration.
     * @param schema  the Unity Catalog schema, from the request's table name.
     */
    @Override
    protected String getFromClauseWithSplit(String catalog, String schema, String table, Split split)
    {
        if (schema == null || schema.isEmpty()) {
            throw new IllegalArgumentException(
                    "Table \"" + table + "\" arrived with no schema name, so the Unity Catalog"
                            + " namespace cannot be completed.");
        }
        return " FROM " + Identifiers.qualify(unityCatalog, schema, table);
    }

    /**
     * No predicates. One split covers the whole table, so there is no partition to restrict to; the row
     * limit is Athena's {@code LIMIT} and the ceiling in {@link RowCeilingSpiller}.
     */
    @Override
    protected List<String> getPartitionWhereClauses(Split split)
    {
        return Collections.emptyList();
    }
}
