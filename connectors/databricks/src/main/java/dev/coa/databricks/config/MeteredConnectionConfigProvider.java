// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import dev.coa.connector.metrics.ConnectorMetrics;

import java.util.Objects;

/**
 * Counts configuration-resolution failures around any {@link ConnectionConfigProvider}.
 *
 * <p>A decorator rather than a counter inside each provider, and rather than a try/catch at each call
 * site. {@code configFor} is called from six places across the two handlers — including twice from a
 * constructor, before {@code this} exists — so instrumenting the call sites would mean six chances to
 * add the seventh and miss it. Wrapping once at construction cannot be bypassed.
 *
 * <p>The exception is rethrown unchanged. Its message reaches the user's query and is the only thing
 * that says which variable is wrong.
 */
public final class MeteredConnectionConfigProvider implements ConnectionConfigProvider
{
    private final ConnectionConfigProvider delegate;
    private final ConnectorMetrics metrics;

    public MeteredConnectionConfigProvider(ConnectionConfigProvider delegate, ConnectorMetrics metrics)
    {
        this.delegate = Objects.requireNonNull(delegate, "delegate");
        this.metrics = Objects.requireNonNull(metrics, "metrics");
    }

    /** {@inheritDoc} */
    @Override
    public ConnectionConfig configFor(String athenaCatalogName)
    {
        try {
            return delegate.configFor(athenaCatalogName);
        }
        catch (RuntimeException failure) {
            // RuntimeException, not IllegalArgumentException: the interface documents the latter, but a
            // provider that reads a remote store can fail in its own ways and every one of them is a
            // resolution failure to whoever is holding the alarm.
            metrics.count(ConnectorMetrics.CONFIG_RESOLUTION_FAILURES, dimension(athenaCatalogName));
            throw failure;
        }
    }

    /**
     * The catalog name as a metric dimension, or {@code null} for a name that is not one.
     *
     * <p>This runs <b>before</b> the delegate has validated anything, which is why the name cannot be
     * trusted here. A principal holding {@code lambda:InvokeFunction} directly can post a request naming
     * anything, and each distinct value would mint a custom CloudWatch metric at about $0.30 a month. Not
     * an injection — {@code ConnectorMetrics} escapes the JSON — just unbounded cardinality, billed.
     *
     * <p>So an unrecognised name loses the {@code Catalog} dimension and keeps the count, that way round
     * because the dimension is for triage and the count is the alarm. {@code ConnectorMetrics} already
     * emits the fleet-only dimension set for a null name, as it does at cold start.
     *
     * <p>The rule is {@link ManagedSource#isAthenaCatalogName}, shared with the provider that builds a
     * parameter path from the same string, rather than a looser pattern invented here.
     */
    private static String dimension(String athenaCatalogName)
    {
        return ManagedSource.isAthenaCatalogName(athenaCatalogName) ? athenaCatalogName : null;
    }

    /** {@inheritDoc} Delegated: metering says nothing about where configuration comes from. */
    @Override
    public String describe()
    {
        return delegate.describe();
    }
}
