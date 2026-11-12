// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.metadata;

import org.junit.jupiter.api.Test;

import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** The allowlist, its three deliberate exclusions, and the ANSI value that does not exist here. */
class TableTypesTest
{
    @Test
    void theAllowlistIsTheFiveDatabricksTypesWeExpose()
    {
        assertEquals(new LinkedHashSet<>(Arrays.asList(
                        "MANAGED", "EXTERNAL", "VIEW", "MATERIALIZED_VIEW", "STREAMING_TABLE")),
                TableTypes.allowed());
    }

    @Test
    void baseTableIsNotADatabricksValue()
    {
        // The ANSI spelling does not appear in Unity Catalog, so a filter of
        // table_type IN ('BASE TABLE', 'VIEW') discards every table and returns only the views.
        assertFalse(TableTypes.allowed().contains("BASE TABLE"));
    }

    @Test
    void foreignTablesAreExcludedBecauseTheirOwnSourceShouldBeOnboarded()
    {
        assertTrue(TableTypes.excluded().contains("FOREIGN"));
        assertFalse(TableTypes.allowed().contains("FOREIGN"));
    }

    @Test
    void shallowClonesAreExcludedBecauseTheirRowsDuplicateAnotherTable()
    {
        assertTrue(TableTypes.excluded().contains("MANAGED_SHALLOW_CLONE"));
        assertTrue(TableTypes.excluded().contains("EXTERNAL_SHALLOW_CLONE"));
        assertFalse(TableTypes.allowed().contains("MANAGED_SHALLOW_CLONE"));
        assertFalse(TableTypes.allowed().contains("EXTERNAL_SHALLOW_CLONE"));
    }

    @Test
    void theAllowlistAndTheExclusionsTogetherCoverEveryTypeDatabricksReturns()
    {
        // If Databricks adds a type, this fails and someone decides which side it belongs on, rather than
        // the type appearing in or vanishing from an ontology unnoticed.
        Set<String> everyKnownType = new LinkedHashSet<>(Arrays.asList(
                "MANAGED", "EXTERNAL", "VIEW", "MATERIALIZED_VIEW", "STREAMING_TABLE",
                "FOREIGN", "MANAGED_SHALLOW_CLONE", "EXTERNAL_SHALLOW_CLONE"));
        Set<String> covered = new LinkedHashSet<>(TableTypes.allowed());
        covered.addAll(TableTypes.excluded());
        assertEquals(everyKnownType, covered);
    }

    @Test
    void theSetsAreImmutable()
    {
        Set<String> allowed = TableTypes.allowed();
        assertThrows(UnsupportedOperationException.class, () -> allowed.add("FOREIGN"));
    }
}
