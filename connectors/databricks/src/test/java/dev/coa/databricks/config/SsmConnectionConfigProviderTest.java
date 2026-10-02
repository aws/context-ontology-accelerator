// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import dev.coa.connector.metrics.ConnectorMetrics;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.awscore.exception.AwsErrorDetails;
import software.amazon.awssdk.services.ssm.model.ParameterNotFoundException;
import software.amazon.awssdk.services.ssm.model.SsmException;
import software.amazon.awssdk.services.ssm.model.TooManyUpdatesException;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The parameter is a store COA writes and the connector reads, so its threat model is integrity: repointing
 * it aims a source at another workspace or another source's credential with the catalog name unchanged.
 * Every value therefore goes back through the patterns registration applied.
 */
class SsmConnectionConfigProviderTest
{
    private static final String PREFIX = "/coa/dev/connectors/databricks/sources";
    private static final String DEPLOYMENT = "coa-dev";
    private static final String CATALOG_A = "coadevds_144a95d84d98c87d";
    private static final String CATALOG_B = "coadevds_9f2b1c7ae4d05631";

    private final List<String> emitted = new ArrayList<>();
    private final ConnectorMetrics metrics = new ConnectorMetrics("databricks", emitted::add);

    /**
     * The golden parameter body, as the sources API writes one — <b>read from a file, not built here</b>.
     * It is one half of a nine-field contract with no compiler between its sides: the Python sources API
     * writes these key names and this connector reads them. A hand-built fixture cannot catch a rename,
     * because the same edit renames the test's copy of it. Python's own test reads the same JSON from
     * {@link #PYTHON_GOLDEN_PATH}, kept equal by
     * {@link #theGoldenFixtureIsTheSameFileThePythonSuiteReads}.
     */
    private static final String GOLDEN_RESOURCE = "/databricks_connector_parameter.golden.json";

    /**
     * Where the same fixture lives on the Python side. A copy rather than a shared file because
     * {@code connectors/} is a standalone workspace a customer can copy out and build: a Maven
     * {@code testResource} reaching up into {@code packages/} would make this suite unrunnable the moment it
     * is copied out. The cost of the copy is paid by the byte-equality test below.
     */
    private static final String PYTHON_GOLDEN_PATH =
            "../../packages/sources/tests/fixtures/databricks_connector_parameter.golden.json";

    /** Field names the golden body must carry, i.e. the whole contract, in the order it declares them. */
    private static final List<String> CONTRACT_FIELDS = Arrays.asList(
            "sourceId", "namespaceId", "deploymentId", "workspaceHostname", "httpPath",
            "databricksCatalog", "databaseName", "credentialSecretArn", "crossAccountRoleArn");

    private static String golden()
    {
        try (java.io.InputStream stream =
                     SsmConnectionConfigProviderTest.class.getResourceAsStream(GOLDEN_RESOURCE)) {
            assertNotNull(stream, "missing test resource " + GOLDEN_RESOURCE);
            java.io.ByteArrayOutputStream buffer = new java.io.ByteArrayOutputStream();
            byte[] chunk = new byte[4096];
            for (int read = stream.read(chunk); read >= 0; read = stream.read(chunk)) {
                buffer.write(chunk, 0, read);
            }
            return new String(buffer.toByteArray(), java.nio.charset.StandardCharsets.UTF_8);
        }
        catch (java.io.IOException cause) {
            throw new IllegalStateException("could not read " + GOLDEN_RESOURCE, cause);
        }
    }

    /** A parameter body for one source: the golden fixture, with {@code overrides} applied on top. */
    private static String body(Map<String, String> overrides)
    {
        Map<String, String> fields = new java.util.LinkedHashMap<>();
        try {
            com.fasterxml.jackson.databind.JsonNode root =
                    new com.fasterxml.jackson.databind.ObjectMapper().readTree(golden());
            java.util.Iterator<String> names = root.fieldNames();
            while (names.hasNext()) {
                String name = names.next();
                fields.put(name, root.get(name).textValue());
            }
        }
        catch (java.io.IOException cause) {
            throw new IllegalStateException("golden fixture is not JSON", cause);
        }
        // The one value this class controls rather than inherits: the deployment id is compared against the
        // provider's own, and these tests construct the provider with DEPLOYMENT.
        fields.put("deploymentId", DEPLOYMENT);
        fields.putAll(overrides);

        StringBuilder json = new StringBuilder("{");
        for (Map.Entry<String, String> field : fields.entrySet()) {
            if (json.length() > 1) {
                json.append(',');
            }
            json.append('"').append(field.getKey()).append("\":");
            if (field.getValue() == null) {
                json.append("null");
            }
            else {
                json.append('"').append(field.getValue()).append('"');
            }
        }
        return json.append('}').toString();
    }

    private static String body()
    {
        return body(java.util.Collections.emptyMap());
    }

    private static Map<String, String> with(String key, String value)
    {
        Map<String, String> overrides = new HashMap<>();
        overrides.put(key, value);
        return overrides;
    }

    private static final class FakeParameters
            implements SsmConnectionConfigProvider.ParameterReader
    {
        private final Map<String, String> bodies = new HashMap<>();
        private final List<String> reads = new ArrayList<>();

        FakeParameters put(String parameterName, String body)
        {
            bodies.put(parameterName, body);
            return this;
        }

        @Override
        public String read(String parameterName)
        {
            reads.add(parameterName);
            String body = bodies.get(parameterName);
            if (body == null) {
                throw ParameterNotFoundException.builder().message("not found").build();
            }
            return body;
        }
    }

    private SsmConnectionConfigProvider providerOver(SsmConnectionConfigProvider.ParameterReader reader)
    {
        return providerOver(reader, SsmConnectionConfigProvider.DEFAULT_TTL_MILLIS);
    }

    /** @param ttlMillis zero to make every resolution re-read, which is the TTL-expiry boundary case. */
    private SsmConnectionConfigProvider providerOver(SsmConnectionConfigProvider.ParameterReader reader,
                                                     long ttlMillis)
    {
        // A no-op sleeper: spending the real backoff would put three quarters of a second into the suite.
        return new SsmConnectionConfigProvider(reader, PREFIX, DEPLOYMENT, metrics, ttlMillis,
                SsmConnectionConfigProvider.DEFAULT_BACKOFF_BASE_MILLIS, millis -> { });
    }

    private static FakeParameters oneSource()
    {
        return new FakeParameters().put(PREFIX + '/' + CATALOG_A, body());
    }

    private static String parameterNameFor(String catalog)
    {
        return PREFIX + '/' + catalog;
    }

    @Test
    void everyFieldOfTheGoldenParameterIsReadAndRequired()
    {
        // Every one of the nine names is required by the code under test, so dropping one from the fixture is
        // refused — which means renaming the key the code looks for is refused too. Asserted by removal
        // rather than by listing the names, which would be a third copy of the contract to rename.
        for (String field : CONTRACT_FIELDS) {
            Map<String, String> withoutOne = new HashMap<>();
            withoutOne.put(field, null);
            FakeParameters parameters = new FakeParameters()
                    .put(parameterNameFor(CATALOG_A), body(withoutOne));

            assertThrows(RuntimeException.class,
                    () -> providerOver(parameters).configFor(CATALOG_A),
                    "a parameter with no \"" + field + "\" was accepted, so this connector does not"
                            + " require that field — and a rename of it on either side of the contract"
                            + " would ship green");
        }
    }

    @Test
    void theGoldenParameterResolvesEveryFieldToWhereItBelongs()
    {
        // The fixture's own values, wired through to where the connector puts them. The deployment id is the
        // exception — see body() — so it is asserted through acceptance rather than by value.
        FakeParameters parameters = new FakeParameters()
                .put(parameterNameFor(CATALOG_A), golden().replace("\"coa-dev\"", '"' + DEPLOYMENT + '"'));

        ConnectionConfig config = providerOver(parameters).configFor(CATALOG_A);

        assertEquals("dbc-a1b2345c-d6e7.cloud.databricks.com", config.workspaceHostname());
        assertEquals("/sql/1.0/warehouses/a1b234c567d8e9fa", config.httpPath());
        assertEquals("main", config.catalog());
        assertEquals("sales", config.schema());
        assertEquals("arn:aws:secretsmanager:us-east-1:222233334444:secret:dbx-sp-AbCdEf",
                config.credentialSecretArn());
        assertEquals("src-abc123", config.managedSource().sourceId());
        // A UUID, which is what a real namespace id is, and the shape the ExternalId's uniqueness argument
        // rests on.
        assertEquals("550e8400-e29b-41d4-a716-446655440000", config.managedSource().namespaceId());
        assertEquals("arn:aws:iam::222233334444:role/coa-dev-datasource-access-dbx",
                config.managedSource().crossAccountRoleArn());
    }

    @Test
    void theGoldenFixtureIsTheSameFileThePythonSuiteReads()
    {
        // Byte equality, not field-by-field: whitespace or key order is not a contract violation, but it is
        // evidence the two files have stopped being one thing, which has to fail before the values drift.
        java.io.File pythonGolden = new java.io.File(PYTHON_GOLDEN_PATH);
        // A visible skip rather than a silent pass when the file is absent, which is the copied-out case
        // this workspace is deliberately buildable in.
        org.junit.jupiter.api.Assumptions.assumeTrue(pythonGolden.isFile(),
                "no " + PYTHON_GOLDEN_PATH + " — this connector has been copied out of the repository,"
                        + " so the Python half of the contract is not here to compare against");

        String python;
        try {
            python = new String(java.nio.file.Files.readAllBytes(pythonGolden.toPath()),
                    java.nio.charset.StandardCharsets.UTF_8);
        }
        catch (java.io.IOException cause) {
            throw new IllegalStateException("could not read " + PYTHON_GOLDEN_PATH, cause);
        }

        assertEquals(python, golden(),
                "this connector's copy of the golden parameter body has diverged from "
                        + PYTHON_GOLDEN_PATH + ". They are two files because connectors/ is a standalone"
                        + " workspace that has to build when copied out; keeping them equal is what makes"
                        + " that copy honest. Update both.");
    }

    @Test
    void resolvesTheParameterNamedForTheCatalog()
    {
        FakeParameters parameters = oneSource();

        ConnectionConfig config = providerOver(parameters).configFor(CATALOG_A);

        assertEquals(java.util.Collections.singletonList(parameterNameFor(CATALOG_A)),
                parameters.reads);
        assertEquals("dbc-a1b2345c-d6e7.cloud.databricks.com", config.workspaceHostname());
        assertEquals("/sql/1.0/warehouses/a1b234c567d8e9fa", config.httpPath());
        assertEquals("main", config.catalog());
        assertEquals("sales", config.schema());
        assertEquals("arn:aws:secretsmanager:us-east-1:222233334444:secret:dbx-sp-AbCdEf",
                config.credentialSecretArn());
    }

    @Test
    void aManagedSourceIsAlwaysSchemaPinned()
    {
        // A managed source is exactly one Unity Catalog schema, and the pin is what refuses a request for any
        // other schema in the same catalog even where the credential's own grants would allow it.
        ConnectionConfig config = providerOver(oneSource()).configFor(CATALOG_A);

        assertTrue(config.isSchemaPinned());
        assertEquals("sales", config.schema());
    }

    @Test
    void theResolvedConfigurationCarriesTheSourceNamespaceAndRole()
    {
        // The values are the golden fixture's, not this file's: see body(). The catalog name is asserted
        // against the name the parameter was resolved FOR, since that one is not read out of the body.
        ConnectionConfig config = providerOver(oneSource()).configFor(CATALOG_A);

        assertTrue(config.isCoaManaged());
        assertEquals(CATALOG_A, config.managedSource().athenaCatalogName());
        assertEquals("src-abc123", config.managedSource().sourceId());
        assertEquals("550e8400-e29b-41d4-a716-446655440000", config.managedSource().namespaceId());
        assertEquals("arn:aws:iam::222233334444:role/coa-dev-datasource-access-dbx",
                config.managedSource().crossAccountRoleArn());
    }

    @Test
    void twoCatalogsResolveTheirOwnParameters()
    {
        FakeParameters parameters = oneSource()
                .put(parameterNameFor(CATALOG_B), body(with("databaseName", "finance")));
        SsmConnectionConfigProvider provider = providerOver(parameters);

        assertEquals("sales", provider.configFor(CATALOG_A).schema());
        assertEquals("finance", provider.configFor(CATALOG_B).schema());
        assertEquals("sales", provider.configFor(CATALOG_A).schema(),
                "the third resolution must answer from A, not from whatever ran second");
    }

    @Test
    void aTrailingSeparatorOnThePrefixDoesNotDoubleUp()
    {
        // // is a different parameter to Parameter Store, and a CDK app concatenating a separator and an
        // operator typing one both mean the same thing.
        FakeParameters parameters = oneSource();
        new SsmConnectionConfigProvider(parameters, PREFIX + "/", DEPLOYMENT, metrics,
                SsmConnectionConfigProvider.DEFAULT_TTL_MILLIS,
                SsmConnectionConfigProvider.DEFAULT_BACKOFF_BASE_MILLIS, millis -> { })
                .configFor(CATALOG_A);

        assertEquals(java.util.Collections.singletonList(parameterNameFor(CATALOG_A)),
                parameters.reads);
    }

    @Test
    void aCacheHitAvoidsASecondGetParameter()
    {
        // Discovery is one DESCRIBE per table and every one of them resolves configuration, against
        // Parameter Store's 40 TPS per account and region.
        FakeParameters parameters = oneSource();
        SsmConnectionConfigProvider provider = providerOver(parameters);

        ConnectionConfig first = provider.configFor(CATALOG_A);
        assertSame(first, provider.configFor(CATALOG_A));
        assertEquals(1, parameters.reads.size());
    }

    @Test
    void theParameterIsReadAgainOnceItsTtlHasPassed()
    {
        // A corrected HTTP path or a rotated secret ARN has to take effect without waiting for containers to
        // recycle.
        FakeParameters parameters = oneSource();
        SsmConnectionConfigProvider provider = providerOver(parameters, 0L);

        provider.configFor(CATALOG_A);
        provider.configFor(CATALOG_A);

        assertEquals(2, parameters.reads.size());
    }

    @Test
    void aValueBuiltForOneCatalogIsNeverServedForAnother()
    {
        // The one place a cross-wiring defect returns another namespace's rows rather than an error. Keying
        // the map is not treated as sufficient: the value carries the catalog it was built for and is
        // re-checked on every use. Filed by hand because production cannot produce a mismatched entry.
        SsmConnectionConfigProvider provider = providerOver(oneSource());
        ConnectionConfig builtForA = provider.configFor(CATALOG_A);
        provider.cache.put(CATALOG_B, builtForA);

        IllegalStateException refused =
                assertThrows(IllegalStateException.class, () -> provider.configFor(CATALOG_B));

        assertTrue(refused.getMessage().contains(CATALOG_A), refused.getMessage());
        assertTrue(refused.getMessage().contains(CATALOG_B), refused.getMessage());
    }

    @Test
    void aRefusedCacheEntryIsDroppedSoARetryCanRecover()
    {
        // Leaving the poisoned entry in place would fail every later request for that catalog for the life of
        // the container, including after whatever produced it was fixed.
        FakeParameters parameters = oneSource()
                .put(parameterNameFor(CATALOG_B), body(with("databaseName", "finance")));
        SsmConnectionConfigProvider provider = providerOver(parameters);
        provider.cache.put(CATALOG_B, provider.configFor(CATALOG_A));

        assertThrows(IllegalStateException.class, () -> provider.configFor(CATALOG_B));
        assertEquals("finance", provider.configFor(CATALOG_B).schema());
    }

    @Test
    void configForNullSaysItWasAskedBeforeAthenaNamedACatalog()
    {
        SsmConnectionConfigProvider provider = providerOver(oneSource());

        for (String catalog : new String[] {null, "", "   "}) {
            IllegalStateException failure =
                    assertThrows(IllegalStateException.class, () -> provider.configFor(catalog));
            assertTrue(failure.getMessage().contains("before Athena named a"), failure.getMessage());
        }
    }

    @Test
    void aCatalogNameThatWouldEscapeTheParameterPathIsRefused()
    {
        // The name reaches the connector from Athena rather than from a customer, but it becomes the last
        // segment of a Parameter Store path, and one carrying a separator addresses a different parameter.
        FakeParameters parameters = oneSource();
        SsmConnectionConfigProvider provider = providerOver(parameters);

        for (String catalog : new String[] {
            "../deployment/function-arn", "coadevds_x/y", "coadevds_x*", "coadevds_x y"}) {
            assertThrows(IllegalArgumentException.class, () -> provider.configFor(catalog), catalog);
        }
        assertTrue(parameters.reads.isEmpty(), "refused by shape, before any read: " + parameters.reads);
    }

    @Test
    void aParameterForAnotherDeploymentIsRefused()
    {
        // Environments share an AWS account, so this is what still stops a dev principal's write being served
        // to a prod query if the path ever lost its environment segment.
        FakeParameters parameters = new FakeParameters()
                .put(parameterNameFor(CATALOG_A), body(with("deploymentId", "coa-prod")));

        IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
                () -> providerOver(parameters).configFor(CATALOG_A));

        assertTrue(refused.getMessage().contains("coa-prod"), refused.getMessage());
        assertTrue(refused.getMessage().contains(DEPLOYMENT), refused.getMessage());
    }

    @Test
    void aParameterWithNoDatabaseNameIsRefusedBecauseUnpinnedWouldServeTheWholeCatalog()
    {
        // ConnectionConfig.Builder.schema stores null without validating, which is right for a
        // customer-deployed connector — an absent DATABRICKS_SCHEMA means "serve every schema" — and is the
        // one thing this parameter must not be able to say: unpinned, listDatabases takes its enumerating
        // branch and one source's catalog name reads every schema its credential can see. Absence widening
        // reach is the case re-validating a value cannot catch.
        for (String databaseName : new String[] {null, "", "   "}) {
            FakeParameters parameters = new FakeParameters()
                    .put(parameterNameFor(CATALOG_A), body(with("databaseName", databaseName)));

            IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
                    () -> providerOver(parameters).configFor(CATALOG_A),
                    "databaseName=" + databaseName + " was accepted");

            assertTrue(refused.getMessage().contains(parameterNameFor(CATALOG_A)),
                    refused.getMessage());
            assertTrue(refused.getMessage().contains("every schema"),
                    "the message has to say what serving it unpinned would mean: " + refused.getMessage());
        }
    }

    @Test
    void aParameterWithADatabaseNameIsAlwaysPinnedNeverUnpinned()
    {
        // Nothing this provider returns can be unpinned, so no caller has to handle that shape.
        assertTrue(providerOver(oneSource()).configFor(CATALOG_A).isSchemaPinned());
    }

    @Test
    void aParameterWithNoDeploymentIdIsRefusedRatherThanTrusted()
    {
        FakeParameters parameters = new FakeParameters()
                .put(parameterNameFor(CATALOG_A), body(with("deploymentId", null)));

        IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
                () -> providerOver(parameters).configFor(CATALOG_A));

        assertTrue(refused.getMessage().contains("no deploymentId"), refused.getMessage());
    }

    @Test
    void everyPatternViolationIsRefusedWithAMessageNamingTheParameter()
    {
        // One case per patterned field: the host and HTTP path reach a ";"-delimited JDBC property list, where
        // a ";" sets driver properties (SSL=0 downgrades TLS, ProxyHost redirects an authenticated session),
        // and the catalog and schema are interpolated into information_schema SQL. Registration applies these
        // too; re-applying them here keeps the validation on the connector's side of the store.
        Map<String, String> bad = new java.util.LinkedHashMap<>();
        bad.put("workspaceHostname", "evil.example.com");
        bad.put("httpPath", "/sql/1.0/warehouses/abc;SSL=0");
        bad.put("databricksCatalog", "main;drop");
        bad.put("databaseName", "sales-eu");
        bad.put("credentialSecretArn", "arn:aws:s3:::not-a-secret");
        bad.put("crossAccountRoleArn", "arn:aws:iam::222233334444:user/not-a-role");
        bad.put("sourceId", "src abc");
        bad.put("namespaceId", "");

        for (Map.Entry<String, String> field : bad.entrySet()) {
            FakeParameters parameters = new FakeParameters().put(parameterNameFor(CATALOG_A),
                    body(with(field.getKey(), field.getValue())));

            IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
                    () -> providerOver(parameters).configFor(CATALOG_A),
                    field.getKey() + "=\"" + field.getValue() + "\" was accepted");

            assertTrue(refused.getMessage().contains(parameterNameFor(CATALOG_A)),
                    "the message has to name where the bad value came from: " + refused.getMessage());
        }
    }

    @Test
    void aNamespaceIdCarryingWhitespaceIsRefusedRatherThanTrimmed()
    {
        // The one field CONCATENATED rather than only validated: it is the per-source input to
        // sts:ExternalId, which Python derives verbatim from the same field and publishes for a customer to
        // paste into a trust policy. Trimming here — which the shared text() reader does for every other
        // field — makes Java's ExternalId differ from the published one and surfaces as AccessDenied on every
        // query. Refused, therefore, not normalised.
        //
        // "ns\\t1" is a JSON escape in the fixture, not a raw tab: a raw control character inside a JSON
        // string is invalid JSON and would be refused one step earlier, proving nothing about this check.
        for (String namespaceId : new String[] {" ns-1", "ns-1 ", " ns-1 ", "ns 1", "ns\\t1"}) {
            FakeParameters parameters = new FakeParameters()
                    .put(parameterNameFor(CATALOG_A), body(with("namespaceId", namespaceId)));

            IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
                    () -> providerOver(parameters).configFor(CATALOG_A),
                    "namespaceId=\"" + namespaceId + "\" was accepted");

            assertTrue(refused.getMessage().contains(parameterNameFor(CATALOG_A)),
                    refused.getMessage());
            assertTrue(refused.getMessage().contains("byte for byte"),
                    "the message has to say why trimming is not the fix: " + refused.getMessage());
        }
    }

    @Test
    void anUnknownKeyIsIgnoredRatherThanRefused()
    {
        // The parameter's only writer is the sources API, which refuses a credential-shaped key before
        // it writes (test_a_credential_shaped_key_is_refused_before_the_write in packages/sources). So
        // an extra key here is forward compatibility rather than a leak, and a reader that refused one
        // would make adding a field a breaking change.
        FakeParameters parameters = new FakeParameters()
                .put(parameterNameFor(CATALOG_A), body(with("someFutureField", "whatever")));

        assertEquals("dbc-a1b2345c-d6e7.cloud.databricks.com",
                providerOver(parameters).configFor(CATALOG_A).workspaceHostname());
    }

    @Test
    void aBodyThatIsNotAJsonObjectIsRefusedWithoutEchoingIt()
    {
        // The body is not a credential, but it can be long and it reaches a user's query through the error
        // message. Jackson's own parse message renders the input, so that exception is chained for the log
        // rather than echoed into this message.
        String unmistakable = "NOT-JSON-MARKER-9d2f";
        for (String body : new String[] {
            unmistakable, "[" + unmistakable + "]", "\"" + unmistakable + "\"",
            "{\"unclosed\": \"" + unmistakable + "\""}) {
            FakeParameters parameters =
                    new FakeParameters().put(parameterNameFor(CATALOG_A), body);

            IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
                    () -> providerOver(parameters).configFor(CATALOG_A), body);

            assertTrue(refused.getMessage().contains(parameterNameFor(CATALOG_A)),
                    "the message has to name which parameter is malformed: " + refused.getMessage());
            assertFalse(refused.getMessage().contains(unmistakable),
                    "the body must not be echoed into the message: " + refused.getMessage());
        }
    }

    @Test
    void theParseFailureIsCarriedAsTheCauseRatherThanDropped()
    {
        // Without it the only thing recoverable about a malformed parameter is that it was malformed: the
        // offset and the syntax error live on Jackson's exception, and nothing else logs them.
        FakeParameters parameters = new FakeParameters()
                .put(parameterNameFor(CATALOG_A), "{\"sourceId\": ");

        IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
                () -> providerOver(parameters).configFor(CATALOG_A));

        assertTrue(refused.getCause() instanceof com.fasterxml.jackson.core.JsonProcessingException,
                "expected the parse failure as the cause, got " + refused.getCause());
        assertTrue(refused.getCause().getMessage().contains("line: 1"),
                refused.getCause().getMessage());
    }

    @Test
    void anEmptyParameterIsRefusedAsEmptyRatherThanAsBadJson()
    {
        // A different branch and a different message: the sources API always writes a body, so an empty value
        // was never written rather than written badly, and reporting a JSON parse failure would send an
        // operator looking for a syntax error.
        for (String body : new String[] {"", "   ", "\n"}) {
            FakeParameters parameters =
                    new FakeParameters().put(parameterNameFor(CATALOG_A), body);

            IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
                    () -> providerOver(parameters).configFor(CATALOG_A), "\"" + body + "\"");

            assertTrue(refused.getMessage().contains("is empty"), refused.getMessage());
            assertTrue(refused.getMessage().contains(parameterNameFor(CATALOG_A)),
                    refused.getMessage());
        }
    }

    @Test
    void aMissingParameterFailsWithItsOwnMessageAndIsNotRetried()
    {
        FakeParameters parameters = oneSource();

        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> providerOver(parameters).configFor(CATALOG_B));

        assertTrue(failure.getMessage().contains(parameterNameFor(CATALOG_B)), failure.getMessage());
        assertTrue(failure.getMessage().contains("Re-register"), failure.getMessage());
        assertEquals(1, parameters.reads.size(), "no amount of waiting creates a parameter");
    }

    @Test
    void aMissingParameterCountsAsAConfigResolutionFailureThroughTheMeteredProvider()
    {
        // The counting is the decorator's, not this class's, and this is the pairing an alarm depends on:
        // ConnectorConfigResolutionFailures is what fires for a parameter that is absent or invalid.
        ConnectionConfigProvider provider =
                new MeteredConnectionConfigProvider(providerOver(oneSource()), metrics);

        assertThrows(IllegalArgumentException.class, () -> provider.configFor(CATALOG_B));

        assertEquals(1, emitted.size());
        assertTrue(emitted.get(0).contains(ConnectorMetrics.CONFIG_RESOLUTION_FAILURES),
                emitted.get(0));
        assertTrue(emitted.get(0).contains("\"Catalog\":\"" + CATALOG_B + "\""), emitted.get(0));
    }

    @Test
    void aThrottleIsCountedAndRetriedRatherThanFailing()
    {
        AtomicInteger attempts = new AtomicInteger();
        SsmConnectionConfigProvider.ParameterReader throttlingTwice = name -> {
            if (attempts.incrementAndGet() <= 2) {
                throw throttling();
            }
            return body();
        };

        ConnectionConfig config = providerOver(throttlingTwice).configFor(CATALOG_A);

        assertEquals("main", config.catalog());
        assertEquals(3, attempts.get());
        assertEquals(2, emitted.size(), "one count per throttled attempt: " + emitted);
        for (String line : emitted) {
            assertTrue(line.contains(ConnectorMetrics.CONFIG_THROTTLES), line);
        }
    }

    @Test
    void aThrottleThatNeverClearsFailsAfterABoundedNumberOfAttempts()
    {
        AtomicInteger attempts = new AtomicInteger();
        SsmConnectionConfigProvider.ParameterReader alwaysThrottling = name -> {
            attempts.incrementAndGet();
            throw throttling();
        };

        IllegalStateException failure = assertThrows(IllegalStateException.class,
                () -> providerOver(alwaysThrottling).configFor(CATALOG_A));

        assertEquals(SsmConnectionConfigProvider.MAX_ATTEMPTS, attempts.get());
        // The message has to say the configuration is fine, or an operator spends the outage reading a
        // parameter that is correct.
        assertTrue(failure.getMessage().contains("Nothing is wrong with the configuration"),
                failure.getMessage());
    }

    @Test
    void throttlingIsRecognisedByCodeAndByTheSdksOwnFlag()
    {
        assertTrue(SsmConnectionConfigProvider.isThrottling(throttling()));
        assertTrue(SsmConnectionConfigProvider.isThrottling(
                TooManyUpdatesException.builder()
                        .awsErrorDetails(AwsErrorDetails.builder()
                                .errorCode("TooManyUpdates").build())
                        .build()));
        // Not a throttle: refusing to retry these is the point of the distinction.
        assertFalse(SsmConnectionConfigProvider.isThrottling(
                ParameterNotFoundException.builder().message("nope").build()));
        assertFalse(SsmConnectionConfigProvider.isThrottling(
                SsmException.builder()
                        .awsErrorDetails(AwsErrorDetails.builder()
                                .errorCode("AccessDeniedException").build())
                        .build()));
        assertFalse(SsmConnectionConfigProvider.isThrottling(new IllegalStateException("no")));
    }

    @Test
    void aNonThrottleIsNotRetried()
    {
        AtomicInteger attempts = new AtomicInteger();
        SsmConnectionConfigProvider.ParameterReader denied = name -> {
            attempts.incrementAndGet();
            throw SsmException.builder()
                    .awsErrorDetails(AwsErrorDetails.builder()
                            .errorCode("AccessDeniedException").build())
                    .build();
        };

        assertThrows(SsmException.class, () -> providerOver(denied).configFor(CATALOG_A));
        assertEquals(1, attempts.get());
    }

    @Test
    void describeNamesTheModeThePathAndTheDeploymentAndNothingPerSource()
    {
        // At cold start no catalog name exists, so there is no endpoint to name. Resolving one to log it
        // would mean picking some tenant's configuration at random.
        String description = providerOver(oneSource()).describe();

        assertTrue(description.contains("coa-managed"), description);
        assertTrue(description.contains(PREFIX), description);
        assertTrue(description.contains(DEPLOYMENT), description);
        assertFalse(description.contains("databricks.com"), description);
    }

    @Test
    void aMissingPrefixOrDeploymentIdFailsAtConstruction()
    {
        for (String prefix : new String[] {null, "", "  ", "/", "///"}) {
            assertThrows(IllegalArgumentException.class,
                    () -> new SsmConnectionConfigProvider(prefix, DEPLOYMENT), String.valueOf(prefix));
        }
        for (String deployment : new String[] {null, "", "  "}) {
            assertThrows(IllegalArgumentException.class,
                    () -> new SsmConnectionConfigProvider(PREFIX, deployment),
                    String.valueOf(deployment));
        }
    }

    private static SsmException throttling()
    {
        return (SsmException) SsmException.builder()
                .awsErrorDetails(AwsErrorDetails.builder()
                        .errorCode("ThrottlingException").build())
                .message("Rate exceeded")
                .build();
    }
}
