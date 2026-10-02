// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import dev.coa.databricks.config.ConfigSource;

import java.util.Map;

/**
 * The connector's operational settings, and how they are read.
 *
 * <p>Separate from {@link dev.coa.databricks.config.ConnectionConfig}, which says where the warehouse is.
 * These have working defaults, and an unusable value falls back rather than failing initialisation: a typo
 * in an operational knob should not present as "the connector is broken".
 *
 * <p><b>{@link #CONFIG_SOURCE_VAR} is the exception.</b> It decides whether the connector resolves one
 * endpoint or one per tenant, so a typo falling back to the default would serve one tenant's warehouse and
 * credential under every tenant's catalog name. {@link dev.coa.databricks.config.ConfigSource#of}
 * therefore throws where the row ceiling shrugs.
 */
public final class Settings
{
    /**
     * Where the connector reads a source's connection facts, and therefore how it reaches the
     * credential. Unset or blank means {@code environment}. See
     * {@link dev.coa.databricks.config.ConfigSource}.
     */
    public static final String CONFIG_SOURCE_VAR = "DATABRICKS_CONFIG_SOURCE";

    /**
     * Rows one table may return before the connector fails with an error naming it. A ceiling exists
     * because federation cannot express aggregation: a {@code GROUP BY} reads every predicate-matching
     * row out of the warehouse for Athena to aggregate. At 3008 MB, a table well past this ceiling
     * exhausts the invocation, and neither an out-of-memory nor a timeout says which table was too big.
     */
    public static final String MAX_ROWS_PER_TABLE_VAR = "DATABRICKS_MAX_ROWS_PER_TABLE";

    /** Default for {@link #MAX_ROWS_PER_TABLE_VAR}. */
    public static final long DEFAULT_MAX_ROWS_PER_TABLE = 2_000_000L;

    private Settings()
    {
    }

    /**
     * The configuration mode, read case-insensitively like the others.
     *
     * @throws IllegalArgumentException on a value that is neither mode, naming both.
     */
    public static ConfigSource configSource(Map<String, String> environment)
    {
        return ConfigSource.of(lookUp(environment, CONFIG_SOURCE_VAR));
    }

    /**
     * Whether {@code name} is set to a non-blank value, for the mode switch's mutual-exclusion check.
     *
     * <p>Blank counts as unset, because CDK, a console and a shell disagree about whether an unset
     * variable arrives absent or empty — so a stack writing {@code DATABRICKS_CATALOG=""} must not be read
     * as presenting the single-target shape.
     */
    public static boolean isSet(Map<String, String> environment, String name)
    {
        return lookUp(environment, name) != null;
    }

    /** The row ceiling: the configured value when it is a positive long, the default otherwise. */
    public static long maxRowsPerTable(Map<String, String> environment)
    {
        String raw = lookUp(environment, MAX_ROWS_PER_TABLE_VAR);
        if (raw == null) {
            return DEFAULT_MAX_ROWS_PER_TABLE;
        }
        try {
            long value = Long.parseLong(raw.trim());
            return (value > 0) ? value : DEFAULT_MAX_ROWS_PER_TABLE;
        }
        catch (NumberFormatException ignored) {
            return DEFAULT_MAX_ROWS_PER_TABLE;
        }
    }

    /**
     * The value of {@code name}, matched case-insensitively, or null when unset or blank. A scan rather
     * than a few exact lookups, because an exact/lower/upper triple misses a mixed-case name.
     *
     * <p>Public because the mode switch reads two variables that are neither operational settings nor
     * connection coordinates, and reading them by any other route would apply a different
     * case-sensitivity rule to them than to everything else this connector reads.
     *
     * <p><b>The CDK app's own {@code optionalEnv} is case-SENSITIVE, and the asymmetry is deliberate
     * rather than an oversight.</b> The two read different things: that one reads a deployer's shell, where
     * a variable is typed by hand and a case-insensitive match would silently accept
     * {@code databricks_catalog} as a synonym it never documented; this one reads the Lambda's own
     * environment, which CDK writes in full and in one case.
     */
    public static String lookUp(Map<String, String> environment, String name)
    {
        if (environment == null) {
            return null;
        }
        String value = environment.get(name);
        if (value == null) {
            for (Map.Entry<String, String> entry : environment.entrySet()) {
                if (entry.getKey() != null && entry.getKey().equalsIgnoreCase(name)) {
                    value = entry.getValue();
                    break;
                }
            }
        }
        return (value == null || value.trim().isEmpty()) ? null : value;
    }
}
