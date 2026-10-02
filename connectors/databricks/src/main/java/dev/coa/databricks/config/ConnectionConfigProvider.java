// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

/**
 * Resolves the {@link ConnectionConfig} for the Athena catalog a request arrived under.
 *
 * <p>Athena passes the registered catalog name verbatim on every call path, which makes it the only
 * per-source discriminator a connector receives. Two implementations, selected at cold start by
 * {@link ConnectionConfigProviders#fromEnvironment}: {@link EnvironmentConnectionConfigProvider} ignores
 * the argument, {@link SsmConnectionConfigProvider} resolves one parameter per catalog name.
 *
 * <p>An implementation is called on every request, so it should cache, and it should re-validate
 * whatever it read: a store that can be written to is not a trusted input.
 */
public interface ConnectionConfigProvider
{
    /**
     * @param athenaCatalogName the catalog name Athena invoked the connector under. An implementation that
     *                          discriminates on the name must fail on null rather than serve a
     *                          deployment-wide default: in a multiplexed deployment there is no such
     *                          thing, and answering would serve one tenant's request from whichever
     *                          endpoint happened to be first.
     * @throws IllegalArgumentException if no valid configuration exists for it. The message reaches
     *                                 the user's query, so it has to name what is wrong.
     */
    ConnectionConfig configFor(String athenaCatalogName);

    /**
     * A one-line description of where this provider reads configuration from, for the cold-start log.
     *
     * <p>It exists so the handlers do not have to resolve a configuration in order to log one: a
     * multiplexed deployment learns each source's coordinates only when a request names one.
     *
     * <p>Must carry no credential and no JDBC URL. Defaulted so this interface keeps a single abstract
     * method and a fake provider can stay a lambda.
     */
    default String describe()
    {
        return getClass().getSimpleName();
    }
}
