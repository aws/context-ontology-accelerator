// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import java.util.Objects;

/**
 * Reads a credential secret and caches the parsed result per container on a jittered TTL, so a rotated
 * credential takes effect without waiting for containers to recycle.
 *
 * <p>Takes a {@link SecretReader} rather than a Secrets Manager client, which makes the two
 * credential-custody modes a constructor argument rather than a branch: {@link SecretsManagerReader} uses
 * the connector's own role, {@link AssumedRoleCredentialSource} assumes a customer-owned role first.
 *
 * <p>Keyed by namespace, role and secret rather than single-slot, because one container serves several
 * Athena catalogs. {@link #keyFor} says why all three.
 *
 * <p>Thread-safe. A benign race re-reads one secret and discards the loser.
 */
public final class CredentialSource
{
    /** Default cache lifetime, before jitter. */
    public static final long DEFAULT_TTL_MILLIS = 5L * 60L * 1000L;

    /**
     * Reads the credential secret's value for one resolved configuration.
     *
     * <p>Takes the whole {@link ConnectionConfig} rather than a secret ARN, because in
     * {@code coa-managed} mode the read needs the customer-owned role and the namespace as well, and
     * both live on the configuration. An implementation returns the secret string exactly as
     * {@code GetSecretValue} does; parsing is {@link DatabricksCredential}'s.
     */
    @FunctionalInterface
    public interface SecretReader
    {
        /** @throws RuntimeException if the secret cannot be reached; the caller classifies it. */
        String read(ConnectionConfig config);
    }

    private final SecretReader secretReader;

    /** Keyed by namespace, role and secret — see {@link #keyFor}. */
    private final ExpiringCache<String, DatabricksCredential> byKey;

    /** @param secretReader reads a secret's value for a configuration. */
    public CredentialSource(SecretReader secretReader)
    {
        this(secretReader, DEFAULT_TTL_MILLIS);
    }

    /**
     * @param ttlMillis cache lifetime before jitter. Zero disables caching, which is what a test
     *                  asserting the reader was called wants.
     */
    public CredentialSource(SecretReader secretReader, long ttlMillis)
    {
        this.secretReader = Objects.requireNonNull(secretReader, "secretReader");
        this.byKey = new ExpiringCache<>(ttlMillis);
    }

    /**
     * The parsed credential, from cache when it is still fresh.
     *
     * <p>No catch here on purpose. Unreadable and unusable leave as different types, each already
     * classified one layer out: {@link DatabricksCredential#fromSecretJson} names the secret and the
     * accepted shapes, {@link AssumedRoleCredentialSource} separates assume-denied from
     * readable-but-not-through-this-role, and {@code DatabricksConnectionFactory.open()} turns a raw
     * Secrets Manager exception into {@code CONNECTOR_CREDENTIAL_UNREADABLE}. A catch here could only
     * restate one of them.
     *
     * @throws IllegalArgumentException if the secret is not a supported shape.
     */
    public DatabricksCredential credentialFor(ConnectionConfig config)
    {
        return byKey.get(keyFor(config), () -> DatabricksCredential.fromSecretJson(
                secretReader.read(config), config.credentialSecretArn()));
    }

    /**
     * The cache key: namespace, the role the secret is reached through, then the secret, newline-separated
     * so no combination of values can concatenate into another combination's key.
     *
     * <p>The namespace is in the key even though nothing can currently exercise it. A hit short-circuits
     * {@link SecretReader#read}, where the assume presents the ExternalId binding a read to one tenant, so
     * a key omitting the namespace would let two namespaces sharing a role and secret serve each other's
     * cached credential with no assume made. What makes that unreachable today is upstream: the sources
     * API's role claim refuses a {@code crossAccountRoleArn} another namespace already claimed. Different
     * service, different language, so neither side may be weakened because the other covers it.
     */
    private static String keyFor(ConnectionConfig config)
    {
        if (!config.isCoaManaged()) {
            // One endpoint, one secret, one deployment-wide role: there is no tenant to discriminate on,
            // and no assume to short-circuit.
            return "\n\n" + config.credentialSecretArn();
        }
        ManagedSource source = config.managedSource();
        return source.namespaceId() + '\n' + source.crossAccountRoleArn() + '\n'
                + config.credentialSecretArn();
    }

}
