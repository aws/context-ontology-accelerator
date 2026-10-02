// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import dev.coa.databricks.Settings;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * A COA-deployed connector left in {@code environment} mode with the single-target variables present
 * ignores the Athena catalog name entirely, so every namespace resolves the one workspace and credential
 * that stack was given and namespace A's query returns namespace B's rows, with nothing erroring. The
 * environment is the only place that contradiction is visible.
 */
class ConnectionConfigProvidersTest
{
    private static final String HOST = "dbc-a1b2345c-d6e7.cloud.databricks.com";
    private static final String HTTP_PATH = "/sql/1.0/warehouses/a1b234c567d8e9fa";
    private static final String SECRET =
            "arn:aws:secretsmanager:us-east-1:111122223333:secret:dbx-AbCdEf";
    private static final String SSM_PREFIX = "/coa/dev/connectors/databricks/sources";

    private static Map<String, String> singleTarget()
    {
        Map<String, String> environment = new HashMap<>();
        environment.put(ConnectionConfig.WORKSPACE_HOSTNAME_VAR, HOST);
        environment.put(ConnectionConfig.HTTP_PATH_VAR, HTTP_PATH);
        environment.put(ConnectionConfig.CATALOG_VAR, "main");
        environment.put(ConnectionConfig.CREDENTIAL_SECRET_ARN_VAR, SECRET);
        return environment;
    }

    private static Map<String, String> managed()
    {
        Map<String, String> environment = new HashMap<>();
        environment.put(Settings.CONFIG_SOURCE_VAR, "coa-managed");
        environment.put(SsmConnectionConfigProvider.SSM_PREFIX_VAR, SSM_PREFIX);
        environment.put(SsmConnectionConfigProvider.DEPLOYMENT_ID_VAR, "coa-dev");
        environment.put(AssumedRoleCredentialSource.RESOURCE_PREFIX_VAR, "coa-dev-");
        return environment;
    }

    @Test
    void theDefaultIsTheSingleEndpointMode()
    {
        // A deployed stage-1 stack that pulls a newer jar has never heard of this variable and has to
        // behave no differently.
        assertEquals(ConfigSource.ENVIRONMENT, Settings.configSource(singleTarget()));

        Map<String, String> blank = singleTarget();
        blank.put(Settings.CONFIG_SOURCE_VAR, "  ");
        assertEquals(ConfigSource.ENVIRONMENT, Settings.configSource(blank),
                "blank has to mean unset: CDK, a console and a shell disagree about which arrives");
    }

    @Test
    void theModeIsReadCaseInsensitivelyInBothTheNameAndTheValue()
    {
        Map<String, String> environment = managed();
        environment.remove(Settings.CONFIG_SOURCE_VAR);
        environment.put("databricks_config_source", "COA-Managed");

        assertEquals(ConfigSource.COA_MANAGED, Settings.configSource(environment));
    }

    @Test
    void anUnrecognisedModeFailsAtInitialisationRatherThanFallingBack()
    {
        // Unlike the operational settings, which shrug at a typo: falling back here would choose the
        // single-endpoint mode for a deployment that asked for the multiplexed one.
        Map<String, String> environment = managed();
        environment.put(Settings.CONFIG_SOURCE_VAR, "ssm");

        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> ConnectionConfigProviders.fromEnvironment(environment));

        assertTrue(failure.getMessage().contains("ssm"), failure.getMessage());
        assertTrue(failure.getMessage().contains("environment"), failure.getMessage());
        assertTrue(failure.getMessage().contains("coa-managed"), failure.getMessage());
    }

    @Test
    void theSingleTargetEnvironmentSelectsThatOneEndpoint()
    {
        ConnectionConfigProvider provider =
                ConnectionConfigProviders.fromEnvironment(singleTarget()).provider();

        // Resolved for any catalog name, since one deployment is one endpoint in this mode.
        assertEquals("main", provider.configFor("coadevds_144a95d84d98c87d").catalog());
        assertTrue(provider.describe().contains("mode=environment"), provider.describe());
        assertTrue(provider.describe().contains(HOST), provider.describe());
    }

    @Test
    void theManagedEnvironmentSelectsTheParameterBackedProvider()
    {
        ConnectionConfigProvider provider =
                ConnectionConfigProviders.fromEnvironment(managed()).provider();

        assertTrue(provider.describe().contains("mode=coa-managed"), provider.describe());
        assertTrue(provider.describe().contains(SSM_PREFIX), provider.describe());
        assertThrows(IllegalStateException.class, () -> provider.configFor(null));
    }

    @Test
    void aResolutionFailureIsCountedWhicheverModeItCameFrom()
    {
        // So the alarm on ConnectorConfigResolutionFailures does not depend on a deployment's mode.
        assertTrue(ConnectionConfigProviders.fromEnvironment(managed()).provider()
                instanceof MeteredConnectionConfigProvider);
        assertTrue(ConnectionConfigProviders.fromEnvironment(singleTarget()).provider()
                instanceof MeteredConnectionConfigProvider);
    }

    @Test
    void managedModeCarryingAnySingleTargetVariableRefusesToStart()
    {
        // One case per variable, because each is enough on its own to make it unclear which of the two
        // configurations a request would be answered from.
        for (String name : ConnectionConfigProviders.SINGLE_TARGET_VARS) {
            Map<String, String> environment = managed();
            environment.put(name, "anything");

            IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                    () -> ConnectionConfigProviders.fromEnvironment(environment), name);

            assertTrue(failure.getMessage().contains(name), failure.getMessage());
            assertTrue(failure.getMessage().contains("one endpoint fixed at deploy time"),
                    "the message has to say which direction the operator is in: "
                            + failure.getMessage());
        }
    }

    @Test
    void environmentModeCarryingAnyManagedVariableRefusesToStart()
    {
        // The dangerous direction: the connector would answer, and answer from the wrong tenant's
        // warehouse, so the message has to say what the silent failure would have been.
        for (String name : ConnectionConfigProviders.MANAGED_VARS) {
            Map<String, String> environment = singleTarget();
            environment.put(name, "anything");

            IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                    () -> ConnectionConfigProviders.fromEnvironment(environment), name);

            assertTrue(failure.getMessage().contains(name), failure.getMessage());
            assertTrue(failure.getMessage().contains("returns another's rows"),
                    "the message has to say which direction the operator is in: "
                            + failure.getMessage());
        }
    }

    @Test
    void theTwoRefusalsAreToldApartByTheirMessages()
    {
        Map<String, String> managedWithSingleTarget = managed();
        managedWithSingleTarget.put(ConnectionConfig.CATALOG_VAR, "main");
        Map<String, String> singleTargetWithManaged = singleTarget();
        singleTargetWithManaged.put(SsmConnectionConfigProvider.SSM_PREFIX_VAR, SSM_PREFIX);

        String managedDirection = assertThrows(IllegalArgumentException.class,
                () -> ConnectionConfigProviders.fromEnvironment(managedWithSingleTarget)).getMessage();
        String environmentDirection = assertThrows(IllegalArgumentException.class,
                () -> ConnectionConfigProviders.fromEnvironment(singleTargetWithManaged)).getMessage();

        assertFalse(managedDirection.equals(environmentDirection));
        assertTrue(managedDirection.startsWith("Refusing to start."), managedDirection);
        assertTrue(environmentDirection.startsWith("Refusing to start."), environmentDirection);
    }

    @Test
    void aBlankSingleTargetVariableIsNotTakenAsPresent()
    {
        // CDK, a console and a shell disagree about whether an unset variable arrives absent or empty, so a
        // stack writing DATABRICKS_CATALOG="" must not be read as presenting the single-target shape.
        Map<String, String> environment = managed();
        environment.put(ConnectionConfig.CATALOG_VAR, "");
        environment.put(ConnectionConfig.WORKSPACE_HOSTNAME_VAR, "   ");

        assertTrue(ConnectionConfigProviders.fromEnvironment(environment).provider()
                .describe().contains("coa-managed"));
    }

    @Test
    void theOptionalSchemaVariableIsRefusedInManagedModeRatherThanIgnored()
    {
        // A managed deployment would not read it — the schema comes from each source's own parameter — and a
        // silently ignored variable is a trap: whoever set it believes it constrains something.
        Map<String, String> environment = managed();
        environment.put(ConnectionConfig.SCHEMA_VAR, "sales");

        assertThrows(IllegalArgumentException.class,
                () -> ConnectionConfigProviders.fromEnvironment(environment));
    }

    @Test
    void anEnvironmentModeDeploymentMayStillPinItsSchema()
    {
        Map<String, String> environment = singleTarget();
        environment.put(ConnectionConfig.SCHEMA_VAR, "sales");

        assertEquals("sales", ConnectionConfigProviders.fromEnvironment(environment).provider()
                .configFor("coadevds_144a95d84d98c87d").schema());
    }

    @Test
    void managedModeMissingAnyRequiredVariableRefusesToStart()
    {
        for (String name : ConnectionConfigProviders.MANAGED_VARS) {
            Map<String, String> environment = managed();
            environment.remove(name);

            IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                    () -> ConnectionConfigProviders.fromEnvironment(environment), name);

            assertTrue(failure.getMessage().contains(name), failure.getMessage());
            assertTrue(failure.getMessage().contains("None has a safe default"), failure.getMessage());
        }
    }

    /**
     * The CDK app that deploys this connector, relative to this module's directory. It lives inside
     * {@code connectors/databricks/} so it travels with the jar it deploys.
     *
     * <p>The whole directory rather than one file: the app is split by mode, so a variable may be named
     * from the stack, from the single-target environment builder or from the managed one.
     */
    private static final String CDK_LIB_PATH = "cdk/lib";

    @Test
    void everyVariableThisJarReadsIsAlsoNamedByTheCdkAppThatDeploysIt()
    {
        // Reads the TypeScript as text, deliberately: the CDK app writes the function's environment and the
        // jar reads it, with no compiler across the boundary and a list on each side. A one-sided rename
        // ships green on both suites and fails at the function's initialisation. The CDK app is a separate
        // pnpm workspace and this is a Maven module, so do not "fix" this into an import.
        String source = cdkStackSource();

        // CONTAINMENT, not equality, and in this direction only. The app legitimately names a key the jar
        // cannot see — CREDENTIAL_KMS_KEY_ARN decides whether a kms:Decrypt grant is made and never reaches
        // the function's environment — while the other direction would let the jar grow a variable the app
        // never writes.
        for (String name : ConnectionConfigProviders.MANAGED_VARS) {
            assertTrue(source.contains('"' + name + '"'),
                    "this jar requires " + name + " in coa-managed mode, but " + CDK_LIB_PATH
                            + " never names it — so nothing sets it and the connector refuses to start."
                            + " Add it there, or rename it back here.");
        }
        for (String name : ConnectionConfigProviders.SINGLE_TARGET_VARS) {
            assertTrue(source.contains('"' + name + '"'),
                    "this jar refuses " + name + " in coa-managed mode, but " + CDK_LIB_PATH
                            + " never names it — so a deployment carrying it would synthesise cleanly and"
                            + " then fail at the connector's initialisation, which is the wrong place to"
                            + " find out.");
        }
        assertTrue(source.contains('"' + Settings.CONFIG_SOURCE_VAR + '"'),
                Settings.CONFIG_SOURCE_VAR + " does not appear in " + CDK_LIB_PATH);
    }

    @Test
    void bothModesWireNamesTheCdkAppAlsoKnows()
    {
        // The mode VALUES, not just the variable: the app writes `coa-managed` and ConfigSource.of parses it
        // here, so changing either spelling alone produces a function that refuses to start.
        String source = cdkStackSource();

        for (ConfigSource mode : ConfigSource.values()) {
            assertTrue(source.contains('"' + mode.wireName() + '"'),
                    "mode \"" + mode.wireName() + "\" does not appear in " + CDK_LIB_PATH
                            + ", so the two sides disagree about what this variable may say");
        }
    }

    /** Every TypeScript file of the CDK app, joined, or a skipped test when the app is not in this tree. */
    private static String cdkStackSource()
    {
        java.io.File[] modules = new java.io.File(CDK_LIB_PATH).listFiles(
                (dir, name) -> name.endsWith(".ts"));
        // A visible skip rather than a silent pass: a connector copied out without its CDK app has no
        // second side to compare against.
        org.junit.jupiter.api.Assumptions.assumeTrue(modules != null && modules.length > 0,
                "no " + CDK_LIB_PATH + " — the CDK half of the environment contract is not in this"
                        + " tree, so there is nothing to compare these variable names against");
        StringBuilder source = new StringBuilder();
        for (java.io.File module : modules) {
            try {
                source.append(new String(java.nio.file.Files.readAllBytes(module.toPath()),
                        java.nio.charset.StandardCharsets.UTF_8));
            }
            catch (java.io.IOException cause) {
                throw new IllegalStateException("could not read " + module, cause);
            }
        }
        return source.toString();
    }

    @Test
    void theCredentialPathIsPairedWithTheProviderFromOneReadOfTheMode()
    {
        // Only two of the four combinations are valid: SSM facts with a direct read would mean COA holding
        // a durable read on every customer's secret, and environment facts with an assume has no caller.
        // Returned together, so no second read of the mode can pair them any other way.
        ConnectionConfigProviders.Wiring managed =
                ConnectionConfigProviders.fromEnvironment(managed());
        assertTrue(managed.secretReader() instanceof AssumedRoleCredentialSource);
        assertTrue(managed.provider().describe().contains("mode=coa-managed"));

        ConnectionConfigProviders.Wiring environment =
                ConnectionConfigProviders.fromEnvironment(singleTarget());
        assertTrue(environment.secretReader() instanceof SecretsManagerReader);
        assertTrue(environment.provider().describe().contains("mode=environment"));
    }
}
