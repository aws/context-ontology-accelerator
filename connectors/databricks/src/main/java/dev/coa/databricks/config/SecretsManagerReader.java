// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import software.amazon.awssdk.services.secretsmanager.SecretsManagerClient;
import software.amazon.awssdk.services.secretsmanager.model.GetSecretValueRequest;

/**
 * Reads a secret's value with the connector's <b>own</b> role: the {@code environment}-mode credential
 * path, one secret and an execution role holding {@code secretsmanager:GetSecretValue} on it. The
 * {@code coa-managed} path is {@link AssumedRoleCredentialSource}, which holds no such grant.
 *
 * <p>The client is built on first use, because this is constructed before {@code super(...)} in
 * {@link dev.coa.databricks.DatabricksRecordHandler}, where no region or credentials are resolvable.
 * Uncached: {@link CredentialSource} already caches the parsed credential on a jittered TTL, and the
 * SDK's caching client behind that would hold a rotated secret past the expiry meant to pick one up.
 *
 * <p>Thread-safe. A benign race builds a second client and discards it.
 */
public final class SecretsManagerReader implements CredentialSource.SecretReader
{
    private volatile SecretsManagerClient client;

    /** @param config the resolved configuration; only its {@code credentialSecretArn} is used. */
    @Override
    public String read(ConnectionConfig config)
    {
        return client().getSecretValue(
                        GetSecretValueRequest.builder()
                                .secretId(config.credentialSecretArn())
                                .build())
                .secretString();
    }

    private SecretsManagerClient client()
    {
        SecretsManagerClient snapshot = client;
        if (snapshot == null) {
            snapshot = SecretsManagerClient.create();
            client = snapshot;
        }
        return snapshot;
    }
}
