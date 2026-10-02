// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import com.amazonaws.athena.connector.lambda.exceptions.AthenaConnectorException;
import dev.coa.connector.metrics.ConnectorMetrics;
import dev.coa.databricks.jdbc.DatabricksErrors;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.auth.credentials.AwsCredentials;
import software.amazon.awssdk.auth.credentials.AwsSessionCredentials;
import software.amazon.awssdk.awscore.exception.AwsErrorDetails;
import software.amazon.awssdk.services.sts.model.AssumeRoleRequest;
import software.amazon.awssdk.services.sts.model.AssumeRoleResponse;
import software.amazon.awssdk.services.sts.model.Credentials;
import software.amazon.awssdk.services.sts.model.StsException;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The ExternalId assertions are the load-bearing ones. That value is written by a human into a trust policy
 * from what COA's UI shows them, and COA's UI computes it in Python, so the two derivations are one contract
 * expressed twice and a drift of one character fails every onboarding with a bare {@code AccessDenied}.
 */
class AssumedRoleCredentialSourceTest
{
    /**
     * The deployment prefix as CDK injects it, <b>trailing separator included</b>. Python's
     * {@code coa_common.constants.datasource_external_id} concatenates straight from the environment, so
     * stripping the hyphen produces {@code coa-devns-1} and breaks every trust policy.
     */
    private static final String RESOURCE_PREFIX = "coa-dev-";

    private static final String NAMESPACE = "ns-1";
    private static final String SOURCE_ID = "src-abc123";
    private static final String CATALOG = "coadevds_144a95d84d98c87d";
    private static final String ROLE =
            "arn:aws:iam::222233334444:role/coa-dev-datasource-access-sales";
    private static final String OTHER_ROLE =
            "arn:aws:iam::222233334444:role/coa-dev-datasource-access-finance";
    private static final String SECRET =
            "arn:aws:secretsmanager:us-east-1:222233334444:secret:dbx-sp-AbCdEf";

    /**
     * What Python computes, spelled out rather than derived, so a change to the Java derivation cannot
     * quietly change what these tests expect too.
     *
     * <pre>
     * &gt;&gt;&gt; os.environ["RESOURCE_PREFIX"] = "coa-dev-"
     * &gt;&gt;&gt; datasource_external_id("ns-1")
     * 'coa-dev-ns-1'
     * </pre>
     */
    private static final String EXPECTED_EXTERNAL_ID = "coa-dev-ns-1";

    private final List<String> emitted = new ArrayList<>();
    private final ConnectorMetrics metrics = new ConnectorMetrics("databricks", emitted::add);
    private final List<AssumeRoleRequest> assumeRequests = new ArrayList<>();
    private final List<String> fetchedWith = new ArrayList<>();

    private static ConnectionConfig managed(String athenaCatalog, String roleArn, String secretArn)
    {
        return ConnectionConfig.builder()
                .workspaceHostname("dbc-a1b2345c-d6e7.cloud.databricks.com")
                .httpPath("/sql/1.0/warehouses/a1b234c567d8e9fa")
                .catalog("main")
                .schema("sales")
                .credentialSecretArn(secretArn)
                .managedSource(ManagedSource.builder()
                        .athenaCatalogName(athenaCatalog)
                        .sourceId(SOURCE_ID)
                        .namespaceId(NAMESPACE)
                        .crossAccountRoleArn(roleArn)
                        .build())
                .build();
    }

    private static ConnectionConfig managed()
    {
        return managed(CATALOG, ROLE, SECRET);
    }

    /** The same, with the namespace varied — which is what the ExternalId is derived from. */
    private static ConnectionConfig managedFor(String namespaceId, String athenaCatalog,
                                               String roleArn, String secretArn)
    {
        return ConnectionConfig.builder()
                .workspaceHostname("dbc-a1b2345c-d6e7.cloud.databricks.com")
                .httpPath("/sql/1.0/warehouses/a1b234c567d8e9fa")
                .catalog("main")
                .schema("sales")
                .credentialSecretArn(secretArn)
                .managedSource(ManagedSource.builder()
                        .athenaCatalogName(athenaCatalog)
                        .sourceId("src-" + namespaceId)
                        .namespaceId(namespaceId)
                        .crossAccountRoleArn(roleArn)
                        .build())
                .build();
    }

    /** An {@code AssumeRole} response whose access key names the role, so a read can be traced to it. */
    private static AssumeRoleResponse sessionFor(String roleArn)
    {
        return AssumeRoleResponse.builder()
                .credentials(Credentials.builder()
                        .accessKeyId("ASIA-" + roleArn.substring(roleArn.lastIndexOf('/') + 1))
                        .secretAccessKey("session-secret")
                        .sessionToken("session-token")
                        .build())
                .build();
    }

    private AssumedRoleCredentialSource source()
    {
        return source(CredentialSource.DEFAULT_TTL_MILLIS);
    }

    private AssumedRoleCredentialSource source(long ttlMillis)
    {
        return new AssumedRoleCredentialSource(RESOURCE_PREFIX,
                request -> {
                    assumeRequests.add(request);
                    return sessionFor(request.roleArn());
                },
                (session, secretArn) -> {
                    AwsCredentials credentials = session.resolveCredentials();
                    fetchedWith.add(credentials.accessKeyId() + " -> " + secretArn);
                    return "{\"token\": \"dapi-example\"}";
                },
                metrics, ttlMillis);
    }

    @Test
    void theExternalIdIsThePrefixConcatenatedWithTheNamespace()
    {
        assertEquals(EXPECTED_EXTERNAL_ID,
                AssumedRoleCredentialSource.externalIdFor(RESOURCE_PREFIX, NAMESPACE));
    }

    @Test
    void theExternalIdIsNeitherFoldedNorSeparatedNorStrippedOfItsPrefixesHyphen()
    {
        // Three normalisations a reader might take for tidying up, each producing a value no trust policy
        // names.
        String externalId = AssumedRoleCredentialSource.externalIdFor(RESOURCE_PREFIX, NAMESPACE);
        assertFalse(externalId.contains("--"), externalId);
        assertTrue(externalId.startsWith(RESOURCE_PREFIX),
                "the prefix's trailing hyphen is part of the value: " + externalId);
        assertEquals("COA-Dev-NS-1",
                AssumedRoleCredentialSource.externalIdFor("COA-Dev-", "NS-1"),
                "case must survive: the value is compared byte for byte by StringEquals");
    }

    @Test
    void aWhitespaceBearingOperandIsRefusedRatherThanTrimmed()
    {
        // Python trims neither operand, so trimming here would make Java SUCCEED on an input Python got
        // wrong: the two produce different strings and the mismatch surfaces as AccessDenied on every query
        // rather than as a bad deployment. Refusing is stricter than the authority, which is safe.
        for (String prefix : new String[] {"coa dev-", " coa-dev-", "coa-dev- ", "coa-dev-\t",
            "coa-\ndev-"}) {
            assertThrows(IllegalArgumentException.class,
                    () -> AssumedRoleCredentialSource.externalIdFor(prefix, NAMESPACE), prefix);
        }
        for (String namespace : new String[] {"ns 1", " ns-1", "ns-1 ", "ns-1\n"}) {
            assertThrows(IllegalArgumentException.class,
                    () -> AssumedRoleCredentialSource.externalIdFor(RESOURCE_PREFIX, namespace),
                    namespace);
        }
    }

    @Test
    void theRefusalNamesTheDerivationRatherThanJustTheValue()
    {
        // The obvious local repair — trim it — is the bug, so the message has to say that the ExternalId is
        // a byte-for-byte concatenation shared with the control plane.
        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> AssumedRoleCredentialSource.externalIdFor("coa dev-", NAMESPACE));

        assertTrue(failure.getMessage().contains(AssumedRoleCredentialSource.RESOURCE_PREFIX_VAR),
                failure.getMessage());
        assertTrue(failure.getMessage().contains("byte for byte"), failure.getMessage());
        assertTrue(failure.getMessage().contains("nothing trimmed"), failure.getMessage());
        assertTrue(failure.getMessage().contains("trust policy"), failure.getMessage());
    }

    @Test
    void aWhitespaceBearingPrefixFailsAtInitialisationRatherThanPerRequest()
    {
        // The prefix is a deployment fact, so failing here is one loud error at cold start instead of an
        // AccessDenied per query with nothing in common between them.
        for (String prefix : new String[] {"coa dev-", " coa-dev-", "coa-dev-\t"}) {
            assertThrows(IllegalArgumentException.class,
                    () -> new AssumedRoleCredentialSource(prefix), prefix);
        }
    }

    @Test
    void theConcatenationIsAmbiguousAcrossPrefixesAndThatIsRecordedRatherThanFixed()
    {
        // Pinned so nobody "fixes" it by inserting a separator: the derivation is shared with Python and
        // cannot change on one side. With no separator, one prefix being a strict prefix of another collides,
        // so cross-deployment replay is improbable rather than impossible. What bounds it is that
        // namespaceId is server-minted and UUID-shaped, so the pair below is unreachable.
        assertEquals(AssumedRoleCredentialSource.externalIdFor("coa-dev-", "x"),
                AssumedRoleCredentialSource.externalIdFor("coa-", "dev-x"));
        // And with a real, server-minted namespace id, no plausible prefix pair collides.
        assertNotEquals(
                AssumedRoleCredentialSource.externalIdFor(
                        "coa-dev-", "11111111-2222-3333-4444-555555555555"),
                AssumedRoleCredentialSource.externalIdFor(
                        "coa-prod-", "11111111-2222-3333-4444-555555555555"));
    }

    @Test
    void noPairOfRealPrefixesAndRealNamespaceIdsCollides()
    {
        // What bounds the ambiguity above, asserted rather than argued. A collision needs the shorter
        // prefix's namespace id to open with the longer prefix's tail, "dev-", and every namespace id COA
        // mints is a UUID, which cannot. Every prefix a deploy script produces, including the
        // strict-prefix pairs, against two namespaces in each: all distinct.
        List<String> prefixes = Arrays.asList("coa-", "coa-dev-", "coa-dev-2-", "coa-prod-");
        List<String> namespaces = Arrays.asList(
                "144a95d8-4d98-c87d-9f3e-0b2c1d4e5f60", "9f3e0b2c-1d4e-5f60-7a8b-9c0d1e2f3a4b");

        Set<String> externalIds = new HashSet<>();
        for (String prefix : prefixes) {
            for (String namespace : namespaces) {
                assertTrue(externalIds.add(
                                AssumedRoleCredentialSource.externalIdFor(prefix, namespace)),
                        "two deployments would present the same sts:ExternalId: " + prefix + namespace);
            }
        }
        assertEquals(prefixes.size() * namespaces.size(), externalIds.size());
    }

    @Test
    void theAssumePresentsThatExternalIdAndNoOther()
    {
        source().read(managed());

        assertEquals(1, assumeRequests.size());
        assertEquals(EXPECTED_EXTERNAL_ID, assumeRequests.get(0).externalId());
        assertEquals(ROLE, assumeRequests.get(0).roleArn());
    }

    @Test
    void aMissingPrefixOrNamespaceFailsRatherThanAssumingWithoutAnExternalId()
    {
        // Never fall back to an unconditioned assume: the connector's own grant carries
        // Null: {"sts:ExternalId": "false"} and denies one anyway, with an AccessDenied that says nothing
        // about the cause. IllegalArgumentException specifically, because a test written against
        // RuntimeException also passes on the NPE a deleted guard would raise.
        for (String prefix : new String[] {null, "", "  "}) {
            IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                    () -> AssumedRoleCredentialSource.externalIdFor(prefix, NAMESPACE),
                    String.valueOf(prefix));
            assertTrue(failure.getMessage().contains(AssumedRoleCredentialSource.RESOURCE_PREFIX_VAR),
                    "the message has to name the variable to set: " + failure.getMessage());

            IllegalArgumentException atInit = assertThrows(IllegalArgumentException.class,
                    () -> new AssumedRoleCredentialSource(prefix), String.valueOf(prefix));
            assertTrue(atInit.getMessage().contains(AssumedRoleCredentialSource.RESOURCE_PREFIX_VAR),
                    atInit.getMessage());
        }
        for (String namespace : new String[] {null, "", "  "}) {
            IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                    () -> AssumedRoleCredentialSource.externalIdFor(RESOURCE_PREFIX, namespace),
                    String.valueOf(namespace));
            assertTrue(failure.getMessage().contains("namespaceId"), failure.getMessage());
        }
    }

    @Test
    void theSessionNameCarriesTheNamespaceAndTheSource()
    {
        // So every read is attributable in the CUSTOMER's CloudTrail as well as COA's. Attribution, not
        // authorisation: a session name is caller-supplied, so neither side may decide anything on it.
        source().read(managed());

        String sessionName = assumeRequests.get(0).roleSessionName();
        assertTrue(sessionName.contains(NAMESPACE), sessionName);
        assertTrue(sessionName.contains(SOURCE_ID), sessionName);
    }

    @Test
    void theSessionNameIsTruncatedToStsLimitKeepingTheNamespaceWhole()
    {
        // A pair of UUIDs is 73 characters against STS's 64. The namespace is kept whole because it is the
        // tenant and the field a CloudTrail reader correlates on first; the source id gets what is left.
        String namespace = "11111111-2222-3333-4444-555555555555";
        String sourceId = "99999999-8888-7777-6666-555555555555";

        String sessionName = AssumedRoleCredentialSource.sessionNameFor(namespace, sourceId);

        assertTrue(sessionName.length() <= AssumedRoleCredentialSource.MAX_SESSION_NAME_LENGTH,
                sessionName.length() + ": " + sessionName);
        assertTrue(sessionName.startsWith(namespace), sessionName);
        assertTrue(sessionName.contains("9999"), "some of the source id has to survive: " + sessionName);
    }

    @Test
    void theSessionNameIsSanitisedToTheCharactersStsAccepts()
    {
        // ManagedSource's patterns already guarantee this. Asserted anyway because a session name is
        // attribution rather than a control, so it must never be the reason a read fails.
        String sessionName = AssumedRoleCredentialSource.sessionNameFor("ns/1 x", "src:2*");

        assertTrue(sessionName.matches("[A-Za-z0-9_+=,.@-]+"), sessionName);
    }

    @Test
    void aNamespaceIdAloneLongerThanTheLimitStillProducesAValidSessionName()
    {
        String namespace = new String(new char[80]).replace('\0', 'n');

        String sessionName = AssumedRoleCredentialSource.sessionNameFor(namespace, SOURCE_ID);

        assertEquals(AssumedRoleCredentialSource.MAX_SESSION_NAME_LENGTH, sessionName.length());
    }

    @Test
    void theSecretIsReadAsTheAssumedSessionAndNeverAsTheConnectorsOwnPrincipal()
    {
        // The connector's execution role holds nothing on Secrets Manager or KMS, so a read under its own
        // identity would fail — and if it ever succeeded, COA would hold the durable grant this avoids.
        String secretJson = source().read(managed());

        assertEquals("{\"token\": \"dapi-example\"}", secretJson);
        assertEquals(1, fetchedWith.size());
        assertEquals("ASIA-coa-dev-datasource-access-sales -> " + SECRET, fetchedWith.get(0));
    }

    @Test
    void theRequestedSessionOutlivesItsCacheEntry()
    {
        // The RELATION, not the constant: asserting SESSION_DURATION_SECONDS against itself stays green if
        // the duration is shortened or the TTL lengthened, either of which breaks the property. A session
        // expiring inside its own cache window presents as an intermittent authorisation failure with no
        // pattern, so the duration has to exceed the longest a cache entry can live — the TTL plus its
        // maximum +20% jitter.
        Integer requested = assumeRequestFor(managed()).durationSeconds();
        assertNotNull(requested,
                "no DurationSeconds was requested, so the session gets STS's default and nothing here"
                        + " governs how long it lives");

        long sessionLifetimeMillis = requested * 1000L;
        long longestCacheEntryMillis = CredentialSource.DEFAULT_TTL_MILLIS * 6 / 5;

        assertTrue(sessionLifetimeMillis > longestCacheEntryMillis,
                "a " + sessionLifetimeMillis + "ms session against a cache entry that can live "
                        + longestCacheEntryMillis + "ms: shorten one or lengthen the other and a cached"
                        + " session outlives its own credentials");
    }

    private AssumeRoleRequest assumeRequestFor(ConnectionConfig config)
    {
        source().read(config);
        return assumeRequests.get(0);
    }

    @Test
    void theSessionCacheIsKeyedSoTwoCatalogsDoNotThrashIt()
    {
        // Two catalogs alternating through one container is the normal case for a shared connector, and a
        // single-slot session cache would send STS one call per read for the life of the container.
        AssumedRoleCredentialSource source = source();
        ConnectionConfig a = managed(CATALOG, ROLE, SECRET);
        ConnectionConfig b = managed("coadevds_9f2b1c7ae4d05631", OTHER_ROLE, SECRET);

        source.read(a);
        source.read(b);
        source.read(a);
        source.read(b);

        assertEquals(2, assumeRequests.size(), "one assume per role, not one per read");
        assertEquals(4, fetchedWith.size(), "the secret read itself is CredentialSource's to cache");
    }

    @Test
    void twoNamespacesBehindOneRoleGetTwoSessionsWithTheirOwnExternalIds()
    {
        // The confused deputy: every customer's role trusts the same connector role and a role ARN is not a
        // secret, so a steward in namespace A could register a source naming namespace B's role and secret.
        // What stops the read is that the ExternalId is derived from the namespace on the resolved parameter
        // and B's trust policy conditions on B's value. Both halves are asserted: the presented ExternalId
        // tracks the namespace being served, and a cached session for one namespace is never handed to
        // another — these two configurations agree on the role AND the secret, so a cache keyed on either
        // would serve A's session to B and leave one ExternalId never presented.
        AssumedRoleCredentialSource source = source();
        ConnectionConfig namespaceA = managedFor("ns-a", CATALOG, ROLE, SECRET);
        ConnectionConfig namespaceB = managedFor("ns-b", CATALOG, ROLE, SECRET);

        source.read(namespaceA);
        source.read(namespaceB);
        source.read(namespaceA);
        source.read(namespaceB);

        assertEquals(2, assumeRequests.size(), "one assume per namespace, not one per read");
        List<String> presented = new ArrayList<>();
        for (AssumeRoleRequest request : assumeRequests) {
            presented.add(request.externalId());
        }
        // No third value: an invented ExternalId, or one carried over from the previous request, shows here.
        assertEquals(Arrays.asList(RESOURCE_PREFIX + "ns-a", RESOURCE_PREFIX + "ns-b"), presented);
        // The session name distinguishes them too, so a customer's CloudTrail can tell which tenant read.
        assertTrue(assumeRequests.get(0).roleSessionName().contains("ns-a"),
                assumeRequests.get(0).roleSessionName());
        assertTrue(assumeRequests.get(1).roleSessionName().contains("ns-b"),
                assumeRequests.get(1).roleSessionName());
    }

    @Test
    void twoNamespacesBehindOneRoleNeverShareAParsedCredentialEither()
    {
        // The same property one layer up: a hit in CredentialSource's cache short-circuits the reader, where
        // the assume happens, so a key omitting the namespace would answer B from A's cached credential with
        // no ExternalId presented at all. Unreachable today only because the sources API refuses a role ARN
        // already claimed by another namespace — a control in another service.
        AssumedRoleCredentialSource reader = source();
        CredentialSource credentials = new CredentialSource(reader);

        credentials.credentialFor(managedFor("ns-a", CATALOG, ROLE, SECRET));
        credentials.credentialFor(managedFor("ns-b", CATALOG, ROLE, SECRET));

        assertEquals(2, assumeRequests.size(),
                "the second namespace must not be answered from the first's cached credential");
    }

    @Test
    void threeSourcesTwoOfThemOnOneWorkspaceAssumeAndReadIndependently()
    {
        // Two of the three sources sit on the SAME workspace, so everything about their endpoint agrees and
        // only the namespace, the role and the secret differ. A cache keyed on anything they share collapses
        // those two onto one credential while every two-workspace test still passes.
        AssumedRoleCredentialSource source = source();
        String secondSecret = "arn:aws:secretsmanager:us-east-1:222233334444:secret:dbx-sp-Second0";
        String thirdSecret = "arn:aws:secretsmanager:us-east-1:222233334444:secret:dbx-sp-Third00";
        ConnectionConfig first = managedFor("ns-sales", "coadevds_first0", ROLE, SECRET);
        ConnectionConfig second = managedFor("ns-finance", "coadevds_second", OTHER_ROLE, secondSecret);
        // Third: the same workspace as the first, in a different namespace.
        ConnectionConfig third = managedFor("ns-marketing", "coadevds_third0",
                "arn:aws:iam::222233334444:role/coa-dev-datasource-access-marketing", thirdSecret);

        for (ConnectionConfig config : Arrays.asList(first, second, third, first, third)) {
            source.read(config);
        }

        assertEquals(3, assumeRequests.size(), "one assume per source, then cached: " + assumeRequests);
        List<String> presented = new ArrayList<>();
        for (AssumeRoleRequest request : assumeRequests) {
            presented.add(request.externalId() + " -> " + request.roleArn());
        }
        assertEquals(Arrays.asList(
                RESOURCE_PREFIX + "ns-sales -> " + ROLE,
                RESOURCE_PREFIX + "ns-finance -> " + OTHER_ROLE,
                RESOURCE_PREFIX + "ns-marketing -> arn:aws:iam::222233334444:role/"
                        + "coa-dev-datasource-access-marketing"), presented);
        assertEquals(Arrays.asList(
                "ASIA-coa-dev-datasource-access-sales -> " + SECRET,
                "ASIA-coa-dev-datasource-access-finance -> " + secondSecret,
                "ASIA-coa-dev-datasource-access-marketing -> " + thirdSecret,
                "ASIA-coa-dev-datasource-access-sales -> " + SECRET,
                "ASIA-coa-dev-datasource-access-marketing -> " + thirdSecret), fetchedWith);
    }

    @Test
    void oneRoleGuardingTwoSecretsSharesOneSession()
    {
        // Why the session cache is keyed on the role and the ExternalId rather than on the secret: a
        // namespace with several Databricks sources behind one role gets one assume for all of them.
        AssumedRoleCredentialSource source = source();

        source.read(managed(CATALOG, ROLE, SECRET));
        source.read(managed(CATALOG, ROLE,
                "arn:aws:secretsmanager:us-east-1:222233334444:secret:dbx-sp-Second0"));

        assertEquals(1, assumeRequests.size());
        assertEquals(2, fetchedWith.size());
    }

    @Test
    void aZeroTtlReAssumesEveryTime()
    {
        AssumedRoleCredentialSource source = source(0L);

        source.read(managed());
        source.read(managed());

        assertEquals(2, assumeRequests.size());
    }

    @Test
    void aDeniedAssumeCountsItsOwnMetricDimensionedByCatalog()
    {
        // Its own metric because the cause is a policy COA does not own, so the first action is to ask a
        // customer. Dimensioned by catalog: one catalog means one customer changed something, fleet-wide
        // means the connector's own role or deployment moved.
        AssumedRoleCredentialSource source = new AssumedRoleCredentialSource(RESOURCE_PREFIX,
                request -> {
                    throw accessDenied();
                },
                (session, secretArn) -> "{}", metrics, CredentialSource.DEFAULT_TTL_MILLIS);

        assertThrows(AthenaConnectorException.class, () -> source.read(managed()));

        assertEquals(1, emitted.size());
        assertTrue(emitted.get(0).contains(ConnectorMetrics.CREDENTIAL_ASSUME_FAILURES),
                emitted.get(0));
        assertTrue(emitted.get(0).contains("\"Catalog\":\"" + CATALOG + "\""), emitted.get(0));
    }

    @Test
    void aDeniedAssumeRaisesADistinguishableErrorNamingTheExternalIdItPresented()
    {
        // The commonest cause is a trust policy conditioned on a different value, and the two strings side by
        // side are the whole diagnosis. Safe to echo: the ExternalId is an anti-confusion token COA computes
        // server-side rather than accepting from a request.
        AssumedRoleCredentialSource source = new AssumedRoleCredentialSource(RESOURCE_PREFIX,
                request -> {
                    throw accessDenied();
                },
                (session, secretArn) -> "{}", metrics, CredentialSource.DEFAULT_TTL_MILLIS);

        AthenaConnectorException failure =
                assertThrows(AthenaConnectorException.class, () -> source.read(managed()));

        assertTrue(failure.getMessage().startsWith(DatabricksErrors.CREDENTIAL_ASSUME_DENIED_PREFIX),
                failure.getMessage());
        assertTrue(failure.getMessage().contains(EXPECTED_EXTERNAL_ID), failure.getMessage());
        assertTrue(failure.getMessage().contains(ROLE), failure.getMessage());
    }

    @Test
    void anAssumeThatSucceedsAndThenCannotReadTheSecretBlamesTheRolesPermissionPolicy()
    {
        // A different first action from a denied assume, so a different message: the trust policy is
        // demonstrably fine and it is the role's permission policy — or the secret's key policy — that is
        // short. It must not blame the connector's own role, which grants nothing here by design.
        AssumedRoleCredentialSource source = new AssumedRoleCredentialSource(RESOURCE_PREFIX,
                request -> sessionFor(request.roleArn()),
                (session, secretArn) -> {
                    throw accessDenied();
                },
                metrics, CredentialSource.DEFAULT_TTL_MILLIS);

        AthenaConnectorException failure =
                assertThrows(AthenaConnectorException.class, () -> source.read(managed()));

        assertTrue(failure.getMessage().startsWith(DatabricksErrors.CREDENTIAL_UNREADABLE_PREFIX),
                failure.getMessage());
        assertTrue(failure.getMessage().contains(ROLE), failure.getMessage());
        assertTrue(failure.getMessage().contains("PERMISSION policy"), failure.getMessage());
        assertTrue(emitted.isEmpty(), "this is not an assume failure: " + emitted);
    }

    @Test
    void anAssumeReturningNoCredentialsIsTreatedAsAFailureRatherThanDereferenced()
    {
        AssumedRoleCredentialSource source = new AssumedRoleCredentialSource(RESOURCE_PREFIX,
                request -> AssumeRoleResponse.builder().build(),
                (session, secretArn) -> "{}", metrics, CredentialSource.DEFAULT_TTL_MILLIS);

        assertThrows(AthenaConnectorException.class, () -> source.read(managed()));
        assertEquals(1, emitted.size());
    }

    @Test
    void anEnvironmentModeConfigurationReachingThisReaderIsARefusalNotAnAssume()
    {
        // The credential path and the configuration source are selected together, so a configuration with no
        // source record arriving here means the mode switch let through what it refuses at initialisation.
        ConnectionConfig unmanaged = ConnectionConfig.builder()
                .workspaceHostname("dbc-a1b2345c-d6e7.cloud.databricks.com")
                .httpPath("/sql/1.0/warehouses/a1b234c567d8e9fa")
                .catalog("main")
                .credentialSecretArn(SECRET)
                .build();

        assertThrows(IllegalStateException.class, () -> source().read(unmanaged));
        assertTrue(assumeRequests.isEmpty());
    }

    @Test
    void allThreeOfTheSessionsCredentialFieldsReachTheRead()
    {
        // Guards the adapter between STS's Credentials and the SDK's provider: a dropped session token, or
        // the secret key and token transposed, surfaces against a real Secrets Manager as a signature
        // mismatch, which reads as a clock or region problem rather than as a missing field.
        List<AwsCredentials> resolved = new ArrayList<>();
        AssumedRoleCredentialSource source = new AssumedRoleCredentialSource(RESOURCE_PREFIX,
                request -> sessionFor(request.roleArn()),
                (session, secretArn) -> {
                    resolved.add(session.resolveCredentials());
                    return "{\"token\": \"dapi-example\"}";
                },
                metrics, CredentialSource.DEFAULT_TTL_MILLIS);

        source.read(managed());

        assertEquals(1, resolved.size());
        AwsSessionCredentials session = (AwsSessionCredentials) resolved.get(0);
        assertEquals("ASIA-coa-dev-datasource-access-sales", session.accessKeyId());
        assertEquals("session-secret", session.secretAccessKey());
        assertEquals("session-token", session.sessionToken());
    }

    private static StsException accessDenied()
    {
        return (StsException) StsException.builder()
                .awsErrorDetails(AwsErrorDetails.builder()
                        .errorCode("AccessDenied").build())
                .message("User is not authorized to perform: sts:AssumeRole")
                .build();
    }
}
