// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.connector.metrics;

import java.util.function.Consumer;

/**
 * Emits CloudWatch metrics as Embedded Metric Format (EMF) JSON on stdout.
 *
 * <p>No {@code PutMetricData} call, so no CloudWatch client, no IAM grant, and nothing added to the
 * request's latency — the Lambda log pipeline extracts the metric from the log line after the fact. It
 * matches how the rest of COA emits custom metrics, so a connector's numbers land in the same place as
 * the scan pipeline's.
 *
 * <p><b>Every method swallows its own failures.</b> A connector that cannot emit a metric is still a
 * working connector, and a metric emitted from inside an exception handler must not replace the
 * exception the caller is about to throw. Failures are silent rather than logged, because the one thing
 * that can plausibly fail here — writing to stdout — is also how a log would get out.
 *
 * <p>Dimensioned twice on purpose: {@code [Connector, Catalog]} for one deployment and
 * {@code [Connector]} for the fleet. The runbook asks both questions ("this catalog" versus
 * "fleet-wide") and CloudWatch cannot aggregate away a dimension after the fact, so both sets have to
 * be declared at emit time. That is two custom metrics per name, which is the cost of the fleet view.
 */
public final class ConnectorMetrics
{
    /** The CloudWatch namespace every COA connector emits into. */
    public static final String NAMESPACE = "COA/Connectors";

    /**
     * Configuration could not be resolved for the catalog a request arrived under. Alarm above a small
     * share of invocations: on one catalog it is that source's configuration, fleet-wide it is the
     * connector's own environment.
     */
    public static final String CONFIG_RESOLUTION_FAILURES = "ConnectorConfigResolutionFailures";

    /**
     * The configuration store throttled the connector. Counted separately from
     * {@link #CONFIG_RESOLUTION_FAILURES} because nothing is wrong with the configuration: Parameter
     * Store's default {@code GetParameter} throughput is 40 transactions per second per account and
     * region, and discovery is a per-table fan-out. The first action is a rate limit.
     */
    public static final String CONFIG_THROTTLES = "ConnectorConfigThrottles";

    /**
     * {@code sts:AssumeRole} on the customer-owned role that guards a credential failed. Its own metric
     * rather than a configuration failure, because the cause is a trust policy or a permission policy
     * <b>COA does not own and cannot repair</b> — one catalog means one customer changed a policy,
     * fleet-wide means the connector's own role or deployment moved.
     */
    public static final String CREDENTIAL_ASSUME_FAILURES = "ConnectorCredentialAssumeFailures";

    /**
     * A connection to the upstream data source could not be opened. Distinguishes a source that is
     * unreachable or refusing credentials from a query that failed for its own reasons — the two are
     * indistinguishable in Athena's error, which reports only that the connector failed.
     */
    public static final String WAREHOUSE_CONNECT_FAILURES = "ConnectorWarehouseConnectFailures";

    /**
     * Rows returned by one table read. The leading indicator that push-down has regressed or that an
     * aggregate-heavy workload has arrived, both of which show up here long before they show up as a
     * timeout.
     */
    public static final String ROWS_RETURNED = "ConnectorRowsReturned";

    /** A read was refused because the table exceeded the connector's row ceiling. */
    public static final String TABLE_CEILING_EXCEEDED = "ConnectorTableCeilingExceeded";

    /** CloudWatch unit for a plain tally. */
    public static final String UNIT_COUNT = "Count";

    /** CloudWatch unit for a duration in milliseconds. */
    public static final String UNIT_MILLISECONDS = "Milliseconds";

    private static final String CONNECTOR_DIMENSION = "Connector";
    private static final String CATALOG_DIMENSION = "Catalog";

    private final String connectorId;
    private final Consumer<String> sink;

    /**
     * @param connectorId the connector's id, e.g. {@code databricks}. Becomes the {@code Connector}
     *                    dimension, so it must be low-cardinality — an id, never a table or a query.
     */
    public ConnectorMetrics(String connectorId)
    {
        this(connectorId, System.out::println);
    }

    /**
     * @param sink where the EMF line goes. Only non-default in tests: asserting on a captured line is
     *             worth more than asserting that stdout was written to.
     */
    public ConnectorMetrics(String connectorId, Consumer<String> sink)
    {
        this.connectorId = (connectorId == null || connectorId.isEmpty()) ? "unknown" : connectorId;
        this.sink = sink;
    }

    /**
     * Emits one occurrence of {@code metricName}.
     *
     * @param catalog the Athena catalog the request arrived under, or null when none exists yet — at
     *                cold start, or when resolving the catalog is itself what failed. Null drops the
     *                {@code Catalog} dimension rather than inventing a value for it, because an
     *                "unknown" bucket would silently merge unrelated deployments.
     */
    public void count(String metricName, String catalog)
    {
        emit(metricName, 1, UNIT_COUNT, catalog);
    }

    /** Emits {@code value} for {@code metricName}. See {@link #count} for {@code catalog}. */
    public void emit(String metricName, double value, String unit, String catalog)
    {
        try {
            if (metricName == null || metricName.isEmpty() || !isFinite(value)) {
                return;
            }
            sink.accept(document(metricName, value, unit, catalog));
        }
        catch (RuntimeException ignored) {
            // Deliberate. See the class comment: emission must not surface as a connector failure, and
            // logging the failure would use the channel that just failed.
        }
    }

    private String document(String metricName, double value, String unit, String catalog)
    {
        boolean hasCatalog = catalog != null && !catalog.isEmpty();
        StringBuilder json = new StringBuilder(256);
        json.append("{\"_aws\":{\"Timestamp\":").append(System.currentTimeMillis())
                .append(",\"CloudWatchMetrics\":[{\"Namespace\":\"").append(NAMESPACE)
                .append("\",\"Dimensions\":[");
        if (hasCatalog) {
            json.append("[\"").append(CONNECTOR_DIMENSION).append("\",\"")
                    .append(CATALOG_DIMENSION).append("\"],");
        }
        json.append("[\"").append(CONNECTOR_DIMENSION).append("\"]]")
                .append(",\"Metrics\":[{\"Name\":\"").append(escape(metricName))
                .append("\",\"Unit\":\"").append(escape(unit == null ? UNIT_COUNT : unit))
                .append("\"}]}]},\"").append(CONNECTOR_DIMENSION).append("\":\"")
                .append(escape(connectorId)).append('"');
        if (hasCatalog) {
            json.append(",\"").append(CATALOG_DIMENSION).append("\":\"")
                    .append(escape(catalog)).append('"');
        }
        // A whole number is written without a decimal point so a count reads as a count in the log.
        json.append(",\"").append(escape(metricName)).append("\":");
        if (value == Math.rint(value) && Math.abs(value) < 1e15) {
            json.append((long) value);
        }
        else {
            json.append(value);
        }
        return json.append('}').toString();
    }

    private static boolean isFinite(double value)
    {
        return !Double.isNaN(value) && !Double.isInfinite(value);
    }

    /**
     * Escapes a JSON string body. Hand-rolled rather than pulled from Jackson: the SDK's copy is
     * transitive and could be shaded away, and one malformed line here would break metric extraction
     * for the whole invocation.
     */
    private static String escape(String raw)
    {
        StringBuilder out = new StringBuilder(raw.length() + 8);
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            switch (c) {
                case '"':  out.append("\\\""); break;
                case '\\': out.append("\\\\"); break;
                case '\n': out.append("\\n");  break;
                case '\r': out.append("\\r");  break;
                case '\t': out.append("\\t");  break;
                case '\b': out.append("\\b");  break;
                case '\f': out.append("\\f");  break;
                default:
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    }
                    else {
                        out.append(c);
                    }
            }
        }
        return out.toString();
    }
}
