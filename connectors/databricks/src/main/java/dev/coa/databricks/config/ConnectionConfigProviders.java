// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import dev.coa.connector.metrics.ConnectorMetrics;
import dev.coa.databricks.DatabricksMetadataHandler;
import dev.coa.databricks.Settings;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

/**
 * Chooses the configuration provider and credential path from the connector's environment at cold start,
 * and refuses a deployment whose environment describes both modes at once.
 *
 * <p>The mode is read here and nowhere else: {@link #fromEnvironment} returns the pair as a
 * {@link Wiring}, so two reads cannot disagree. The refusal is the substance of this class, and what it
 * prevents is one namespace's query returning another's rows.
 *
 * <p>Why the environment is the only place that contradiction is visible, and why the check runs at
 * initialisation: {@code connectors/databricks/DESIGN.md}, "The dangerous environment is the inverse,
 * and the jar is what refuses it".
 */
public final class ConnectionConfigProviders
{
    /**
     * Variables that describe one endpoint fixed at deploy time. Present alongside {@code coa-managed},
     * they mean nobody can tell which configuration a request would be answered from.
     *
     * <p>{@code DATABRICKS_SCHEMA} is listed even though a managed deployment would not read it, because
     * an operator who set a silently ignored variable believes it constrains something.
     * {@code CREDENTIAL_KMS_KEY_ARN} is not listed: it is a deploy-time input to the CDK app and never
     * reaches the function's environment.
     */
    static final List<String> SINGLE_TARGET_VARS = Arrays.asList(
            ConnectionConfig.WORKSPACE_HOSTNAME_VAR,
            ConnectionConfig.HTTP_PATH_VAR,
            ConnectionConfig.CATALOG_VAR,
            ConnectionConfig.SCHEMA_VAR,
            ConnectionConfig.CREDENTIAL_SECRET_ARN_VAR);

    /** Variables only a COA-managed deployment has any use for. All three are required in that mode. */
    static final List<String> MANAGED_VARS = Arrays.asList(
            SsmConnectionConfigProvider.SSM_PREFIX_VAR,
            SsmConnectionConfigProvider.DEPLOYMENT_ID_VAR,
            AssumedRoleCredentialSource.RESOURCE_PREFIX_VAR);

    private ConnectionConfigProviders()
    {
    }

    /**
     * The provider and credential path this environment describes, from one read of the mode.
     *
     * @param environment the Lambda's environment, as the SDK hands it to a handler.
     * @throws IllegalArgumentException if the mode is unrecognised, if both modes are described, or if
     *                                 {@code coa-managed} is missing a variable it cannot work without.
     *                                 Thrown during initialisation on purpose, so a misconfigured
     *                                 deployment fails once rather than intermittently.
     */
    public static Wiring fromEnvironment(Map<String, String> environment)
    {
        ConfigSource mode = Settings.configSource(environment);
        requireOneShape(environment, mode);
        if (mode == ConfigSource.COA_MANAGED) {
            return new Wiring(
                    metered(new SsmConnectionConfigProvider(
                            Settings.lookUp(environment, SsmConnectionConfigProvider.SSM_PREFIX_VAR),
                            Settings.lookUp(environment,
                                    SsmConnectionConfigProvider.DEPLOYMENT_ID_VAR))),
                    new AssumedRoleCredentialSource(Settings.lookUp(
                            environment, AssumedRoleCredentialSource.RESOURCE_PREFIX_VAR)));
        }
        return new Wiring(metered(new EnvironmentConnectionConfigProvider(environment)),
                new SecretsManagerReader());
    }

    /** Wrapped so every resolution failure is counted, whichever mode it came from. */
    private static ConnectionConfigProvider metered(ConnectionConfigProvider provider)
    {
        return new MeteredConnectionConfigProvider(
                provider, new ConnectorMetrics(DatabricksMetadataHandler.SOURCE_TYPE));
    }

    /**
     * One mode's provider and the credential custody it implies, plus the one credential cache a container
     * resolves secrets through.
     *
     * <p>Built only by {@link #fromEnvironment}, so a handler taking this cannot be handed a provider and a
     * credential path the mode did not choose together.
     */
    public static final class Wiring
    {
        private final ConnectionConfigProvider provider;
        private final CredentialSource.SecretReader secretReader;
        private final CredentialSource credentials;

        private Wiring(ConnectionConfigProvider provider, CredentialSource.SecretReader secretReader)
        {
            this.provider = provider;
            this.secretReader = secretReader;
            this.credentials = new CredentialSource(secretReader);
        }

        /** Resolves the endpoint for the catalog a request arrived under. */
        public ConnectionConfigProvider provider()
        {
            return provider;
        }

        /** The credential cache both halves of a container share: one read per source per TTL window. */
        public CredentialSource credentials()
        {
            return credentials;
        }

        /** The custody model the mode chose. Package-private: only the pairing test needs the type. */
        CredentialSource.SecretReader secretReader()
        {
            return secretReader;
        }
    }

    private static void requireOneShape(Map<String, String> environment, ConfigSource mode)
    {
        List<String> present = setAmong(environment, SINGLE_TARGET_VARS);
        List<String> managed = setAmong(environment, MANAGED_VARS);

        if (mode == ConfigSource.COA_MANAGED) {
            if (!present.isEmpty()) {
                throw new IllegalArgumentException(
                        "Refusing to start. " + Settings.CONFIG_SOURCE_VAR + " is \""
                                + ConfigSource.COA_MANAGED.wireName() + "\", which resolves every"
                                + " source's workspace, schema and credential from Parameter Store per"
                                + " Athena catalog — but this function also sets " + join(present)
                                + ", which describe one endpoint fixed at deploy time. Nothing could say"
                                + " which of the two a request would be answered from. Unset "
                                + join(present) + " to keep the managed mode, or set "
                                + Settings.CONFIG_SOURCE_VAR + "=\""
                                + ConfigSource.ENVIRONMENT.wireName() + "\" to keep the single"
                                + " endpoint.");
            }
            List<String> missing = new ArrayList<>(MANAGED_VARS);
            missing.removeAll(managed);
            if (!missing.isEmpty()) {
                throw new IllegalArgumentException(
                        "Refusing to start. " + Settings.CONFIG_SOURCE_VAR + " is \""
                                + ConfigSource.COA_MANAGED.wireName() + "\" and " + join(missing)
                                + (missing.size() == 1 ? " is" : " are") + " not set. "
                                + SsmConnectionConfigProvider.SSM_PREFIX_VAR + " is the"
                                + " environment-scoped path each source's parameter sits under, e.g."
                                + " /coa/dev/connectors/databricks/sources; "
                                + SsmConnectionConfigProvider.DEPLOYMENT_ID_VAR + " is this"
                                + " deployment's id, which every parameter is checked against because"
                                + " environments share an account; and "
                                + AssumedRoleCredentialSource.RESOURCE_PREFIX_VAR + " is the prefix the"
                                + " ExternalId is derived from, without which every assume is denied."
                                + " None has a safe default: guessing one would either resolve another"
                                + " environment's configuration or present an ExternalId no trust"
                                + " policy names.");
            }
            return;
        }

        if (!managed.isEmpty()) {
            throw new IllegalArgumentException(
                    "Refusing to start. This function sets " + join(managed) + ", which only a"
                            + " COA-managed connector has any use for, but "
                            + Settings.CONFIG_SOURCE_VAR + " is \""
                            + ConfigSource.ENVIRONMENT.wireName() + "\" (its default when unset). In"
                            + " that mode the connector ignores the Athena catalog name entirely, so if"
                            + " this deployment is in fact serving several COA sources then every one of"
                            + " their catalogs resolves the single workspace and single credential this"
                            + " function was given, and one namespace's query returns another's rows"
                            + " with nothing erroring. Set " + Settings.CONFIG_SOURCE_VAR + "=\""
                            + ConfigSource.COA_MANAGED.wireName() + "\", or unset " + join(managed)
                            + " if this really is a single-endpoint deployment.");
        }
    }

    private static List<String> setAmong(Map<String, String> environment, List<String> names)
    {
        List<String> found = new ArrayList<>(names.size());
        for (String name : names) {
            if (Settings.isSet(environment, name)) {
                found.add(name);
            }
        }
        return found;
    }

    private static String join(List<String> names)
    {
        return String.join(", ", names);
    }
}
