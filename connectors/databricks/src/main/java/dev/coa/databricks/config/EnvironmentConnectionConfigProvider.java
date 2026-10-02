// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import java.util.Map;

/**
 * The one-endpoint provider: reads the five coordinates from the Lambda's environment once, at
 * construction, and serves the same configuration to every catalog.
 *
 * <p>Eager, so a misconfigured connector fails during Lambda initialisation with a message naming the
 * variable, on the first invocation of a cold container, rather than as a per-request error whose cause
 * is several stack frames away.
 *
 * <p>Names are matched case-insensitively by comparing each key. The federation SDK hands the handler
 * {@code System.getenv()} verbatim, and shells, consoles and CDK disagree about the case of an
 * environment variable often enough that a mismatch is a real support cost.
 */
public final class EnvironmentConnectionConfigProvider implements ConnectionConfigProvider
{
    private final ConnectionConfig config;

    /**
     * @param environment the Lambda's environment, as the SDK hands it to a handler.
     * @throws IllegalArgumentException if any required variable is missing or invalid, naming it.
     */
    public EnvironmentConnectionConfigProvider(Map<String, String> environment)
    {
        this.config = ConnectionConfig.builder()
                .origin("the connector's environment")
                .workspaceHostname(lookUp(environment, ConnectionConfig.WORKSPACE_HOSTNAME_VAR))
                .httpPath(lookUp(environment, ConnectionConfig.HTTP_PATH_VAR))
                .catalog(lookUp(environment, ConnectionConfig.CATALOG_VAR))
                .schema(lookUp(environment, ConnectionConfig.SCHEMA_VAR))
                .credentialSecretArn(lookUp(environment, ConnectionConfig.CREDENTIAL_SECRET_ARN_VAR))
                .build();
    }

    /** @param athenaCatalogName ignored: this connector serves one endpoint, fixed at deploy time. */
    @Override
    public ConnectionConfig configFor(String athenaCatalogName)
    {
        return config;
    }

    /**
     * {@inheritDoc}
     *
     * <p>No secret ARN and no HTTP path: the latter identifies one specific warehouse and adds nothing
     * here.
     */
    @Override
    public String describe()
    {
        return "mode=" + ConfigSource.ENVIRONMENT.wireName()
                + " host=" + config.workspaceHostname()
                + " catalog=" + config.catalog()
                + " schema=" + (config.isSchemaPinned()
                        ? config.schema() : "<every schema in the catalog>");
    }

    /**
     * The value of {@code name}, matched case-insensitively. A scan rather than an exact/lower/upper
     * triple, which would resolve {@code databricks_catalog} and {@code DATABRICKS_CATALOG} but not
     * {@code Databricks_Catalog}. An exact hit short-circuits, so the normal case is one map lookup.
     */
    private static String lookUp(Map<String, String> environment, String name)
    {
        if (environment == null) {
            return null;
        }
        String exact = environment.get(name);
        if (exact != null) {
            return exact;
        }
        for (Map.Entry<String, String> entry : environment.entrySet()) {
            if (entry.getKey() != null && entry.getKey().equalsIgnoreCase(name)) {
                return entry.getValue();
            }
        }
        return null;
    }
}
