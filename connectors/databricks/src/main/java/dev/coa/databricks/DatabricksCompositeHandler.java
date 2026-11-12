// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import com.amazonaws.athena.connector.lambda.handlers.CompositeHandler;
import dev.coa.databricks.config.ConnectionConfigProvider;
import dev.coa.databricks.config.ConnectionConfigProviders;
import dev.coa.databricks.config.CredentialSource;

import java.util.Map;

/**
 * Lambda entry point: routes metadata and record requests to the two handlers. The Lambda handler string
 * is {@code dev.coa.databricks.DatabricksCompositeHandler}.
 *
 * <p><b>The environment is read once here, and both halves share what it resolved.</b> A half building its
 * own provider and credential source puts two of every cache in one container, costing two
 * {@code GetParameter}, two {@code AssumeRole} and two {@code GetSecretValue} per source per TTL window —
 * and the SDK's {@link CompositeHandler} holds both halves for the container's life.
 *
 * <p>{@link ConnectionConfigProviders#fromEnvironment} is the single entry point: it validates the
 * environment map, including the refusal of one describing both configuration modes at once. It reads the
 * mode once and returns the provider and the credential path together, so the two halves cannot be built
 * from different modes.
 */
public class DatabricksCompositeHandler extends CompositeHandler
{
    public DatabricksCompositeHandler()
    {
        this(System.getenv());
    }

    /** Builds a container from an explicit environment, so a test can build a whole one. */
    DatabricksCompositeHandler(Map<String, String> environment)
    {
        this(environment, ConnectionConfigProviders.fromEnvironment(environment));
    }

    /** Builds a container over one provider and credential cache shared by both halves. */
    DatabricksCompositeHandler(Map<String, String> environment,
                               ConnectionConfigProviders.Wiring wiring)
    {
        this(environment, wiring.provider(), wiring.credentials());
    }

    /** Takes the provider and credential cache loose so a test can hand over a fake provider. */
    DatabricksCompositeHandler(Map<String, String> environment, ConnectionConfigProvider configs,
                               CredentialSource credentials)
    {
        super(new DatabricksMetadataHandler(environment, configs, credentials, null,
                        CredentialSource.DEFAULT_TTL_MILLIS),
                new DatabricksRecordHandler(environment, configs, credentials));
    }
}
