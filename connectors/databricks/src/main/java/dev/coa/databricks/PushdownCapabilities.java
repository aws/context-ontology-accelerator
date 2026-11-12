// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import com.amazonaws.athena.connector.lambda.metadata.optimizations.DataSourceOptimizations;
import com.amazonaws.athena.connector.lambda.metadata.optimizations.OptimizationSubType;
import com.amazonaws.athena.connector.lambda.metadata.optimizations.pushdown.FilterPushdownSubType;
import com.amazonaws.athena.connector.lambda.metadata.optimizations.pushdown.LimitPushdownSubType;
import com.amazonaws.athena.connector.lambda.metadata.optimizations.pushdown.TopNPushdownSubType;

import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The capability map {@code doGetDataSourceCapabilities} returns: predicates, {@code LIMIT n} and
 * {@code ORDER BY ... LIMIT n}, always.
 *
 * <p>Advertising does not cause push-down: Athena sends the predicate, limit and top-N in the base
 * {@code ReadRecords} payload either way. The map grants Athena permission to stop re-applying them,
 * which is safe because {@code JdbcSplitQueryBuilder} applies all three. See DESIGN.md for the
 * measurement.
 *
 * <p>Complex expressions are absent: {@code DatabricksQueryBuilder} inherits
 * {@code DefaultJdbcFederationExpressionParser}, whose {@code mapFunctionToDataSourceSyntax} is an
 * unconditional throw as of 2026.33.1, so advertising them would guarantee a run-time failure the first
 * time Athena pushed a function expression.
 */
public final class PushdownCapabilities
{
    /** Built once per container and immutable: the same answer for every request. */
    public static final Map<String, List<OptimizationSubType>> ADVERTISED = advertised();

    private PushdownCapabilities()
    {
    }

    private static Map<String, List<OptimizationSubType>> advertised()
    {
        Map<String, List<OptimizationSubType>> capabilities = new LinkedHashMap<>();
        for (Map.Entry<String, List<OptimizationSubType>> entry : Arrays.asList(
                DataSourceOptimizations.SUPPORTS_FILTER_PUSHDOWN.withSupportedSubTypes(
                        FilterPushdownSubType.SORTED_RANGE_SET,
                        FilterPushdownSubType.NULLABLE_COMPARISON),
                DataSourceOptimizations.SUPPORTS_LIMIT_PUSHDOWN.withSupportedSubTypes(
                        LimitPushdownSubType.INTEGER_CONSTANT),
                DataSourceOptimizations.SUPPORTS_TOP_N_PUSHDOWN.withSupportedSubTypes(
                        TopNPushdownSubType.SUPPORTS_ORDER_BY))) {
            capabilities.put(entry.getKey(), entry.getValue());
        }
        return Collections.unmodifiableMap(capabilities);
    }
}
