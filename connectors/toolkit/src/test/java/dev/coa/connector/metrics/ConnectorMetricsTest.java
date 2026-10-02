// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.connector.metrics;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The document has to be valid EMF or CloudWatch silently extracts nothing — there is no error, the
 * metric simply never appears. So these tests parse the line rather than matching substrings.
 */
class ConnectorMetricsTest
{
    private static final ObjectMapper MAPPER = new ObjectMapper();

    /**
     * The CDK toolkit's copy of the metric contract, relative to this module's directory. Restated there
     * rather than imported from here because the two modules are meant to be copied together.
     */
    private static final String CDK_CONTRACT_PATH = "../cdk-toolkit/src/coa-contract.ts";

    private static final List<String> ALL_METRIC_NAMES = Arrays.asList(
            ConnectorMetrics.CONFIG_RESOLUTION_FAILURES,
            ConnectorMetrics.CONFIG_THROTTLES,
            ConnectorMetrics.CREDENTIAL_ASSUME_FAILURES,
            ConnectorMetrics.WAREHOUSE_CONNECT_FAILURES,
            ConnectorMetrics.ROWS_RETURNED,
            ConnectorMetrics.TABLE_CEILING_EXCEEDED);

    private final List<String> emitted = new ArrayList<>();
    private final ConnectorMetrics metrics = new ConnectorMetrics("databricks", emitted::add);

    private JsonNode only() throws Exception
    {
        assertEquals(1, emitted.size(), "expected exactly one emitted line");
        return MAPPER.readTree(emitted.get(0));
    }

    private JsonNode metricDirective(JsonNode doc)
    {
        return doc.get("_aws").get("CloudWatchMetrics").get(0);
    }

    @Test
    void countEmitsValidEmfWithBothDimensionSets() throws Exception
    {
        metrics.count(ConnectorMetrics.ROWS_RETURNED, "acme_dbx");

        JsonNode doc = only();
        JsonNode directive = metricDirective(doc);

        assertEquals(ConnectorMetrics.NAMESPACE, directive.get("Namespace").asText());
        assertEquals(ConnectorMetrics.ROWS_RETURNED, directive.get("Metrics").get(0).get("Name").asText());
        assertEquals(ConnectorMetrics.UNIT_COUNT, directive.get("Metrics").get(0).get("Unit").asText());

        JsonNode dimensions = directive.get("Dimensions");
        assertEquals(2, dimensions.size(), "per-catalog and fleet-wide sets");
        // The parsed structure, not the array's toString, which would assert Jackson's serialisation too.
        assertEquals(Arrays.asList("Connector", "Catalog"), namesIn(dimensions.get(0)));
        assertEquals(Collections.singletonList("Connector"), namesIn(dimensions.get(1)));

        // Every dimension named in the directive must also exist as a top-level property, or the whole
        // document is discarded.
        assertEquals("databricks", doc.get("Connector").asText());
        assertEquals("acme_dbx", doc.get("Catalog").asText());
        assertEquals(1, doc.get(ConnectorMetrics.ROWS_RETURNED).asInt());
    }

    @Test
    void nullCatalogDropsThatDimensionRatherThanInventingAValue() throws Exception
    {
        metrics.count(ConnectorMetrics.WAREHOUSE_CONNECT_FAILURES, null);

        JsonNode doc = only();
        JsonNode dimensions = metricDirective(doc).get("Dimensions");

        assertEquals(1, dimensions.size());
        assertEquals(Collections.singletonList("Connector"), namesIn(dimensions.get(0)));
        assertFalse(doc.has("Catalog"), "an \"unknown\" bucket would merge unrelated deployments");
    }

    @Test
    void emptyCatalogIsTreatedAsAbsent() throws Exception
    {
        metrics.count(ConnectorMetrics.CONFIG_RESOLUTION_FAILURES, "");

        assertEquals(1, metricDirective(only()).get("Dimensions").size());
    }

    @Test
    void wholeNumbersAreWrittenWithoutADecimalPoint() throws Exception
    {
        metrics.emit(ConnectorMetrics.ROWS_RETURNED, 2_000_000, ConnectorMetrics.UNIT_COUNT, "c");

        assertTrue(emitted.get(0).contains("\"ConnectorRowsReturned\":2000000"),
                "a count should read as a count in the log: " + emitted.get(0));
    }

    @Test
    void fractionalValuesSurvive() throws Exception
    {
        metrics.emit("SomeLatency", 12.5, ConnectorMetrics.UNIT_MILLISECONDS, "c");

        assertEquals(12.5, only().get("SomeLatency").asDouble(), 0.0001);
    }

    @Test
    void quotesAndControlCharactersInADimensionCannotBreakTheDocument() throws Exception
    {
        // A catalog name cannot contain these today, but one malformed line breaks metric extraction for
        // the whole invocation, so the escaping is not left to the caller's good behaviour.
        new ConnectorMetrics("db\"ricks\\", emitted::add)
                .count(ConnectorMetrics.ROWS_RETURNED, "a\nb\tc");

        JsonNode doc = only();
        assertEquals("db\"ricks\\", doc.get("Connector").asText());
        assertEquals("a\nb\tc", doc.get("Catalog").asText());
    }

    @Test
    void nonFiniteValuesAreDroppedRatherThanEmittedAsInvalidJson()
    {
        metrics.emit("X", Double.NaN, ConnectorMetrics.UNIT_COUNT, "c");
        metrics.emit("X", Double.POSITIVE_INFINITY, ConnectorMetrics.UNIT_COUNT, "c");

        // NaN and Infinity are not JSON numbers; emitting them would poison the line.
        assertTrue(emitted.isEmpty(), "expected nothing emitted, got " + emitted);
    }

    @Test
    void missingMetricNameEmitsNothing()
    {
        metrics.count(null, "c");
        metrics.count("", "c");

        assertTrue(emitted.isEmpty());
    }

    @Test
    void aFailingSinkDoesNotSurfaceAsAConnectorFailure()
    {
        ConnectorMetrics broken = new ConnectorMetrics("databricks", line -> {
            throw new IllegalStateException("stdout is gone");
        });

        // The caller is usually inside an exception handler; a metric must never replace the exception
        // it was counting.
        assertDoesNotThrow(() -> broken.count(ConnectorMetrics.TABLE_CEILING_EXCEEDED, "c"));
    }

    @Test
    void aMissingConnectorIdStillProducesAUsableDocument() throws Exception
    {
        new ConnectorMetrics(null, emitted::add).count(ConnectorMetrics.ROWS_RETURNED, "c");

        assertEquals("unknown", only().get("Connector").asText());
    }

    @Test
    void everyMetricNameIsExactlyTheStringTheAlarmsMatchOn()
    {
        // LITERALS, deliberately: each name is half of a contract duplicated in `ConnectorMetricName` in
        // connectors/cdk-toolkit/src/coa-contract.ts, which the CDK app builds an alarm from. A disagreement
        // is silent — the alarm sits in INSUFFICIENT_DATA for ever rather than firing or erroring — and
        // asserting `document()` against the constant that wrote it cannot catch a rename of the value.
        assertEquals("ConnectorConfigResolutionFailures", ConnectorMetrics.CONFIG_RESOLUTION_FAILURES);
        assertEquals("ConnectorConfigThrottles", ConnectorMetrics.CONFIG_THROTTLES);
        assertEquals("ConnectorCredentialAssumeFailures", ConnectorMetrics.CREDENTIAL_ASSUME_FAILURES);
        assertEquals("ConnectorWarehouseConnectFailures", ConnectorMetrics.WAREHOUSE_CONNECT_FAILURES);
        assertEquals("ConnectorRowsReturned", ConnectorMetrics.ROWS_RETURNED);
        assertEquals("ConnectorTableCeilingExceeded", ConnectorMetrics.TABLE_CEILING_EXCEEDED);
        // `CONNECTOR_METRIC_NAMESPACE` in coa-contract.ts has to equal this or every alarm watches an empty
        // namespace.
        assertEquals("COA/Connectors", ConnectorMetrics.NAMESPACE);
    }

    @Test
    void everyMetricNameAlsoAppearsInTheCdkToolkitsCopyOfTheContract()
    {
        // Reads the OTHER language's source as text on purpose. A jar and a CDK app cannot share a constant,
        // so each suite pinning its own literals catches a rename only against ITSELF; this catches one
        // across the boundary. DO NOT "fix" this into an import — that is the coupling the boundary exists
        // to prevent, and it would make a copied-out connector unbuildable.
        java.io.File contract = new java.io.File(CDK_CONTRACT_PATH);
        // A visible skip rather than a silent pass: the toolkit is documented as copyable on its own.
        org.junit.jupiter.api.Assumptions.assumeTrue(contract.isFile(),
                "no " + CDK_CONTRACT_PATH + " — the CDK half of the metric contract is not in this tree,"
                        + " so there is nothing to compare the Java names against");

        String source;
        try {
            source = new String(java.nio.file.Files.readAllBytes(contract.toPath()),
                    java.nio.charset.StandardCharsets.UTF_8);
        }
        catch (java.io.IOException cause) {
            throw new IllegalStateException("could not read " + CDK_CONTRACT_PATH, cause);
        }

        for (String name : ALL_METRIC_NAMES) {
            assertTrue(source.contains('"' + name + '"'),
                    "metric \"" + name + "\" is emitted by this connector but does not appear in "
                            + CDK_CONTRACT_PATH + ", so no CDK app can alarm on it. Add it to"
                            + " ConnectorMetricName there, or rename it back here. A metric whose alarm"
                            + " watches a different name never fires and never errors — it sits in"
                            + " INSUFFICIENT_DATA for ever.");
        }
        // An alarm in the wrong namespace finds no data.
        assertTrue(source.contains('"' + ConnectorMetrics.NAMESPACE + '"'),
                "namespace \"" + ConnectorMetrics.NAMESPACE + "\" does not appear in "
                        + CDK_CONTRACT_PATH);
    }

    @Test
    void everyMetricNameIsDistinctAndCarriesTheConnectorPrefix()
    {
        // A name colliding with another silently merges two alarms' data, and a name shaped unlike its
        // neighbours is the one a copy-paste into the CDK app drops the prefix from.
        assertEquals(ALL_METRIC_NAMES.size(), new java.util.HashSet<>(ALL_METRIC_NAMES).size(),
                "two metrics share a name: " + ALL_METRIC_NAMES);
        for (String name : ALL_METRIC_NAMES) {
            assertTrue(name.startsWith("Connector"), name);
        }
    }

    @Test
    void anAssumeFailureCarriesBothDimensionSets() throws Exception
    {
        // A single catalog means one customer changed a policy; fleet-wide means the connector's own role or
        // deployment moved. The shipped alarm is on the undimensioned set, since naming a catalog would make
        // it stop matching the day a second source is registered.
        metrics.count(ConnectorMetrics.CREDENTIAL_ASSUME_FAILURES, "acme_dbx");

        JsonNode doc = only();
        JsonNode dimensions = metricDirective(doc).get("Dimensions");
        assertEquals(2, dimensions.size());
        assertEquals(Arrays.asList("Connector", "Catalog"), namesIn(dimensions.get(0)));
        assertEquals(Collections.singletonList("Connector"), namesIn(dimensions.get(1)));
        // Every dimension named in the directive must exist as a top-level property, or CloudWatch
        // discards the whole document.
        for (JsonNode set : dimensions) {
            for (String dimension : namesIn(set)) {
                assertTrue(doc.hasNonNull(dimension),
                        "dimension " + dimension + " is declared but has no value: " + doc);
            }
        }
    }

    private static List<String> namesIn(JsonNode dimensionSet)
    {
        List<String> names = new ArrayList<>();
        for (JsonNode name : dimensionSet) {
            names.add(name.asText());
        }
        return names;
    }

    @Test
    void unitDefaultsToCount() throws Exception
    {
        metrics.emit(ConnectorMetrics.ROWS_RETURNED, 1, null, "c");

        assertEquals(ConnectorMetrics.UNIT_COUNT,
                metricDirective(only()).get("Metrics").get(0).get("Unit").asText());
    }
}
