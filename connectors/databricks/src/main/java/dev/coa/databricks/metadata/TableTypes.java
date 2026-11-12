// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.metadata;

import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.Set;

/**
 * Which {@code information_schema.tables.table_type} values this connector exposes. A constant rather
 * than configuration, so there is no second implementation for it to disagree with.
 *
 * <p>Databricks never says {@code 'BASE TABLE'}. It returns {@code MANAGED}, {@code EXTERNAL},
 * {@code VIEW}, {@code MATERIALIZED_VIEW}, {@code STREAMING_TABLE}, {@code FOREIGN},
 * {@code MANAGED_SHALLOW_CLONE} and {@code EXTERNAL_SHALLOW_CLONE}, so the filter every other
 * {@code information_schema} dialect writes, {@code table_type IN ('BASE TABLE', 'VIEW')}, discards
 * every table. It does not return nothing, because {@code 'VIEW'} is a Databricks value: a source scans
 * successfully and contains views but no tables.
 *
 * <p>Three types are excluded. {@code FOREIGN} is a table federated into Unity Catalog from somewhere
 * else, and reading it through two federation layers is slower and less faithful than onboarding its own
 * source, besides hiding which system owns the data. {@code MANAGED_SHALLOW_CLONE} and
 * {@code EXTERNAL_SHALLOW_CLONE} are copy-on-write clones whose rows duplicate another table's, and
 * including them would put the same facts in the ontology twice under two names.
 */
public final class TableTypes
{
    /**
     * The allowlist. Upper case because Databricks returns {@code table_type} upper-cased, unlike the
     * table-name column beside it.
     */
    private static final Set<String> ALLOWED = Collections.unmodifiableSet(
            new LinkedHashSet<>(Arrays.asList(
                    "MANAGED",
                    "EXTERNAL",
                    "VIEW",
                    "MATERIALIZED_VIEW",
                    "STREAMING_TABLE")));

    /** The excluded types, for tests and for the README, so the omission is checkable. */
    private static final Set<String> EXCLUDED = Collections.unmodifiableSet(
            new LinkedHashSet<>(Arrays.asList(
                    "FOREIGN",
                    "MANAGED_SHALLOW_CLONE",
                    "EXTERNAL_SHALLOW_CLONE")));

    private TableTypes()
    {
    }

    /** The {@code table_type} values this connector exposes. */
    public static Set<String> allowed()
    {
        return ALLOWED;
    }

    /** The {@code table_type} values not exposed. */
    public static Set<String> excluded()
    {
        return EXCLUDED;
    }
}
