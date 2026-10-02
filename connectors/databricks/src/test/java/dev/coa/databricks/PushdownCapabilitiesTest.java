// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** The three optimisations advertised, their sub-types, and that the map cannot be edited. */
class PushdownCapabilitiesTest
{
    @Test
    void theThreeMeasuredOptimisationsAreAdvertised()
    {
        assertEquals(3, PushdownCapabilities.ADVERTISED.size(),
                "unexpected key set: " + PushdownCapabilities.ADVERTISED.keySet());
        assertTrue(PushdownCapabilities.ADVERTISED.containsKey("supports_filter_pushdown"));
        assertTrue(PushdownCapabilities.ADVERTISED.containsKey("supports_limit_pushdown"));
        assertTrue(PushdownCapabilities.ADVERTISED.containsKey("supports_top_n_pushdown"));
    }

    @Test
    void eachCarriesTheSubTypesTheQueryBuilderActuallyApplies()
    {
        // An advertisement is a guarantee Athena may act on by not re-applying, so the sub-types are part
        // of it: JdbcSplitQueryBuilder writes sorted ranges and null checks, an integer LIMIT, and ORDER BY.
        assertEquals(2, PushdownCapabilities.ADVERTISED.get("supports_filter_pushdown").size());
        assertEquals("integer_constant",
                PushdownCapabilities.ADVERTISED.get("supports_limit_pushdown").get(0).getSubType());
        // Upper case: the SDK spells this enum's wire value differently from the other two.
        assertEquals("SUPPORTS_ORDER_BY",
                PushdownCapabilities.ADVERTISED.get("supports_top_n_pushdown").get(0).getSubType());
    }

    @Test
    void theMapIsImmutable()
    {
        // Held static and handed to every response, so a caller editing it would change what this
        // connector claims for the container's life.
        assertThrows(UnsupportedOperationException.class,
                () -> PushdownCapabilities.ADVERTISED.put("supports_aggregation_pushdown", null));
    }
}
