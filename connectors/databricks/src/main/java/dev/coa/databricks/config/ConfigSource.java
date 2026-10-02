// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import java.util.Locale;

/**
 * Where this connector reads a source's connection facts, and therefore how it reaches the credential.
 * Selected at run time from {@code DATABRICKS_CONFIG_SOURCE}
 * ({@link dev.coa.databricks.Settings#CONFIG_SOURCE_VAR}); {@link #ENVIRONMENT} is the default.
 *
 * <p>Why two modes and not four, and why the default matters:
 * {@code connectors/databricks/DESIGN.md}, "One jar, two modes, and where the seam had to go".
 */
public enum ConfigSource
{
    /** Stage-1 behaviour: one deployment, one endpoint, fixed at deploy time. The default. */
    ENVIRONMENT("environment"),

    /** One deployment serving every Databricks source in a COA deployment, resolved per catalog. */
    COA_MANAGED("coa-managed");

    private final String wireName;

    ConfigSource(String wireName)
    {
        this.wireName = wireName;
    }

    /** The spelling an operator writes in the environment variable. */
    public String wireName()
    {
        return wireName;
    }

    /**
     * Parses one {@code DATABRICKS_CONFIG_SOURCE} value.
     *
     * @param raw the variable's value. Null or blank selects {@link #ENVIRONMENT}, so a deployment that
     *            has never set this variable behaves as it did before the variable existed.
     * @throws IllegalArgumentException on any other value. Unlike the operational settings, this one does
     *                                 <b>not</b> fall back on a typo: that would serve one tenant's
     *                                 warehouse under every tenant's catalog.
     */
    public static ConfigSource of(String raw)
    {
        if (raw == null || raw.trim().isEmpty()) {
            return ENVIRONMENT;
        }
        String value = raw.trim().toLowerCase(Locale.ROOT);
        for (ConfigSource candidate : values()) {
            if (candidate.wireName.equals(value)) {
                return candidate;
            }
        }
        throw new IllegalArgumentException(
                dev.coa.databricks.Settings.CONFIG_SOURCE_VAR + "=\"" + raw.trim() + "\" is not a mode"
                        + " this connector has. Expected \"" + ENVIRONMENT.wireName + "\" — one"
                        + " endpoint from this function's own environment variables, which is the"
                        + " default when the variable is unset — or \"" + COA_MANAGED.wireName + "\","
                        + " one endpoint per Athena catalog resolved from Parameter Store. Refusing to"
                        + " start rather than defaulting: a typo silently choosing the single-endpoint"
                        + " mode would serve one tenant's warehouse under every tenant's catalog.");
    }
}
