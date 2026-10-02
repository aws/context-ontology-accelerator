// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import dev.coa.connector.metrics.ConnectorMetrics;
import dev.coa.databricks.DatabricksMetadataHandler;
import dev.coa.databricks.Settings;
import dev.coa.databricks.jdbc.DatabricksErrors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import software.amazon.awssdk.auth.credentials.AwsCredentialsProvider;
import software.amazon.awssdk.auth.credentials.AwsSessionCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.services.secretsmanager.SecretsManagerClient;
import software.amazon.awssdk.services.secretsmanager.model.GetSecretValueRequest;
import software.amazon.awssdk.services.sts.StsClient;
import software.amazon.awssdk.services.sts.model.AssumeRoleRequest;
import software.amazon.awssdk.services.sts.model.AssumeRoleResponse;
import software.amazon.awssdk.services.sts.model.Credentials;

import java.util.Objects;
import java.util.regex.Pattern;

/**
 * Reads a credential secret as a session assumed on a <b>customer-owned</b> role: the
 * {@code coa-managed} credential path, in which the connector holds no Secrets Manager or KMS permission
 * of its own, only {@code sts:AssumeRole}.
 *
 * <p>The {@code sts:ExternalId} is mandatory and derived by {@link #externalIdFor} from the namespace on
 * the resolved parameter, never taken from a request. Every customer role trusts the same connector role
 * and a role ARN is not a secret, so without that condition a steward in one namespace could register a
 * source naming another namespace's role and secret, and COA would read it (confused deputy).
 *
 * <p>Sessions are cached on the same jittered TTL as the parsed credential, with
 * {@link #SESSION_DURATION_SECONDS} long enough to outlive a cache entry.
 *
 * <p>Thread-safe. A benign race assumes twice and discards the loser.
 */
public final class AssumedRoleCredentialSource implements CredentialSource.SecretReader
{
    private static final Logger LOGGER = LoggerFactory.getLogger(AssumedRoleCredentialSource.class);

    /**
     * Environment variable carrying the deployment's resource prefix, e.g. {@code coa-dev-}. The only
     * input to {@link #externalIdFor} the connector holds itself; the namespace comes from the source's
     * own parameter.
     */
    public static final String RESOURCE_PREFIX_VAR = "COA_RESOURCE_PREFIX";

    /**
     * Requested session lifetime. Three times {@link CredentialSource#DEFAULT_TTL_MILLIS}, so it outlives
     * a cache entry even with the TTL's ±20% jitter, and inside a role's one-hour default maximum, so no
     * customer has to raise {@code MaxSessionDuration}.
     */
    public static final int SESSION_DURATION_SECONDS = 15 * 60;

    /** STS's limit on a {@code RoleSessionName}. */
    static final int MAX_SESSION_NAME_LENGTH = 64;

    /**
     * Characters STS permits in a {@code RoleSessionName} ({@code [\w+=,.@-]}). Anything else is replaced
     * rather than refused: a session name is attribution and must not be why a read fails.
     */
    private static final String SESSION_NAME_ALLOWED = "[^A-Za-z0-9_+=,.@-]";

    /**
     * Any whitespace character, anywhere in an ExternalId operand. Refused rather than stripped, because
     * Python concatenates its operands verbatim: trimming here would accept an input Python got wrong and
     * surface it as {@code AccessDenied} on every query.
     */
    private static final Pattern ANY_WHITESPACE = Pattern.compile("\\s");

    /** Assumes a role. The seam that keeps STS out of a unit test. */
    @FunctionalInterface
    public interface RoleAssumer
    {
        AssumeRoleResponse assume(AssumeRoleRequest request);
    }

    /** Reads a secret's value as some other principal. The seam that keeps Secrets Manager out of one. */
    @FunctionalInterface
    public interface SecretFetcher
    {
        String fetch(AwsCredentialsProvider session, String secretArn);
    }

    private final String resourcePrefix;
    private final RoleAssumer assumer;
    private final SecretFetcher fetcher;
    private final ConnectorMetrics metrics;

    /** Keyed by role and ExternalId, not by secret: one role can guard several of a namespace's secrets. */
    private final ExpiringCache<String, AwsCredentialsProvider> sessions;

    /**
     * @param resourcePrefix the deployment's resource prefix, e.g. {@code coa-dev-}. Passed in rather
     *                       than read here, so a deployment that has not set it fails at initialisation
     *                       rather than on a request.
     */
    public AssumedRoleCredentialSource(String resourcePrefix)
    {
        this(resourcePrefix, null, null,
                new ConnectorMetrics(DatabricksMetadataHandler.SOURCE_TYPE),
                CredentialSource.DEFAULT_TTL_MILLIS);
    }

    /**
     * @param assumer null for the real STS client, built on first use — this class is constructed
     *                before {@code super(...)} in the record handler, so it has to be constructible
     *                without credentials or a region.
     * @param fetcher null for a real Secrets Manager client per read.
     */
    AssumedRoleCredentialSource(String resourcePrefix, RoleAssumer assumer, SecretFetcher fetcher,
                                ConnectorMetrics metrics, long ttlMillis)
    {
        this.resourcePrefix = requirePrefix(resourcePrefix);
        this.assumer = (assumer != null) ? assumer : new LazyStsAssumer();
        this.fetcher = (fetcher != null) ? fetcher : AssumedRoleCredentialSource::getSecretValue;
        this.metrics = Objects.requireNonNull(metrics, "metrics");
        this.sessions = new ExpiringCache<>(ttlMillis);
    }

    /**
     * The {@code sts:ExternalId} the connector presents, which is what the customer wrote into their
     * role's trust policy: {@code resourcePrefix + namespaceId}, both operands concatenated verbatim.
     *
     * <p><b>Must stay byte-identical to Python's {@code coa_common.constants.datasource_external_id},</b>
     * which the control plane publishes as {@code NamespaceDetail$datasourceExternalId}. No case folding,
     * no inserted separator, no trimming: each of those makes every onboarding fail
     * {@code AccessDenied} with nothing to point at.
     *
     * <p>With no separator, {@code prefixA + namespaceX} can collide with {@code prefixB + namespaceY}
     * when one prefix is a strict prefix of the other. Replay across deployments is bounded by
     * {@code namespaceId} being server-minted and UUID-shaped, since the derivation cannot change on one
     * side alone.
     *
     * @param resourcePrefix the deployment prefix, e.g. {@code coa-dev-}, used verbatim.
     * @param namespaceId    the namespace that owns the source, from its own parameter.
     * @throws IllegalArgumentException if either operand is missing, empty, or carries whitespace.
     */
    public static String externalIdFor(String resourcePrefix, String namespaceId)
    {
        // Re-checked here as well as where each is read: an operand that cannot round-trip to Python
        // must not get past the derivation itself, whatever route it arrived by.
        requireExternalIdOperand(resourcePrefix, RESOURCE_PREFIX_VAR, "");
        requireExternalIdOperand(namespaceId, "namespaceId", "");
        return resourcePrefix + namespaceId;
    }

    /**
     * Refuses an ExternalId operand that could not be concatenated byte for byte with Python's. Returns
     * the value <b>untouched</b> on success.
     *
     * @param what   the value's name, for the message: an environment variable, or a parameter field.
     * @param origin where the value came from, or {@code ""} when there is nothing useful to add.
     * @throws IllegalArgumentException naming the value and the derivation it has to satisfy.
     */
    static String requireExternalIdOperand(String value, String what, String origin)
    {
        String where = (origin == null || origin.isEmpty()) ? "" : " in " + origin;
        if (value == null || value.isEmpty()) {
            throw new IllegalArgumentException(
                    what + " is not set" + where + ", so the connector cannot derive the sts:ExternalId"
                            + " the customer's trust policy requires. An assume presenting no ExternalId"
                            + " is denied by the connector's own role policy as well, so there is"
                            + " nothing to fall back to.");
        }
        if (ANY_WHITESPACE.matcher(value).find()) {
            throw new IllegalArgumentException(
                    what + "=\"" + value + "\"" + where + " contains whitespace. The ExternalId is this"
                            + " value concatenated with the deployment's other one, byte for byte and"
                            + " with nothing trimmed, because COA's control plane derives the same"
                            + " string in Python and publishes it for the customer to paste into their"
                            + " role's trust policy. Trimming here would make the two sides disagree and"
                            + " every assume fail AccessDenied with nothing to point at, so the"
                            + " whitespace is refused rather than removed. Correct the value at source.");
        }
        return value;
    }

    /**
     * The {@code RoleSessionName}, carrying the namespace and the source so every read is attributable in
     * the <b>customer's</b> CloudTrail as well as COA's.
     *
     * <p>A pair of UUIDs is 73 characters against STS's 64, so the namespace is kept whole and the source
     * id takes the truncation.
     */
    static String sessionNameFor(String namespaceId, String sourceId)
    {
        String namespace = sanitiseForSessionName(namespaceId);
        String source = sanitiseForSessionName(sourceId);
        if (namespace.length() >= MAX_SESSION_NAME_LENGTH) {
            return namespace.substring(0, MAX_SESSION_NAME_LENGTH);
        }
        int roomForSource = MAX_SESSION_NAME_LENGTH - namespace.length() - 1;
        if (roomForSource <= 0) {
            return namespace;
        }
        return namespace + '-'
                + source.substring(0, Math.min(source.length(), roomForSource));
    }

    /**
     * Assumes the configuration's role and reads its secret as that session.
     *
     * @throws IllegalStateException if {@code config} carries no managed source, i.e. the mode switch
     *                              wired this reader into an {@code environment}-mode deployment.
     * @throws com.amazonaws.athena.connector.lambda.exceptions.AthenaConnectorException if the assume or
     *         the read fails, with a message that says which of the two and whose policy to look at.
     */
    @Override
    public String read(ConnectionConfig config)
    {
        if (!config.isCoaManaged()) {
            throw new IllegalStateException(
                    "This connector is reading credentials by assuming a customer-owned role, but the"
                            + " resolved configuration carries no source record, so there is no role to"
                            + " assume and no namespace to derive an ExternalId from. That combination"
                            + " means the credential path and the configuration source disagree, which"
                            + " the mode switch is supposed to make impossible at initialisation.");
        }
        ManagedSource source = config.managedSource();
        String externalId = externalIdFor(resourcePrefix, source.namespaceId());
        String key = source.crossAccountRoleArn() + '\n' + externalId;

        return fetch(config, source, sessions.get(key, () -> assume(source, externalId)));
    }

    private AwsCredentialsProvider assume(ManagedSource source, String externalId)
    {
        AssumeRoleRequest request = AssumeRoleRequest.builder()
                .roleArn(source.crossAccountRoleArn())
                .roleSessionName(sessionNameFor(source.namespaceId(), source.sourceId()))
                .externalId(externalId)
                .durationSeconds(SESSION_DURATION_SECONDS)
                .build();
        AssumeRoleResponse response;
        try {
            response = assumer.assume(request);
        }
        catch (RuntimeException cause) {
            // Its own metric, dimensioned by catalog: one catalog means one customer changed a policy COA
            // does not own, fleet-wide means the connector's own role or deployment moved.
            metrics.count(ConnectorMetrics.CREDENTIAL_ASSUME_FAILURES, source.athenaCatalogName());
            throw DatabricksErrors.credentialAssumeDenied(
                    source.crossAccountRoleArn(), externalId, cause);
        }
        Credentials credentials = (response == null) ? null : response.credentials();
        if (credentials == null) {
            metrics.count(ConnectorMetrics.CREDENTIAL_ASSUME_FAILURES, source.athenaCatalogName());
            throw DatabricksErrors.credentialAssumeDenied(
                    source.crossAccountRoleArn(), externalId,
                    new IllegalStateException("AssumeRole returned no credentials"));
        }
        // No credential and no secret value in this line.
        LOGGER.info("Assumed {} for catalog {} (source {}, namespace {}) to read its Databricks"
                        + " credential", source.crossAccountRoleArn(), source.athenaCatalogName(),
                source.sourceId(), source.namespaceId());
        return StaticCredentialsProvider.create(AwsSessionCredentials.create(
                credentials.accessKeyId(), credentials.secretAccessKey(), credentials.sessionToken()));
    }

    private String fetch(ConnectionConfig config, ManagedSource source, AwsCredentialsProvider session)
    {
        try {
            return fetcher.fetch(session, config.credentialSecretArn());
        }
        catch (RuntimeException cause) {
            // Not an assume failure: the assume worked, so the trust policy is right and it is the role's
            // PERMISSION policy — or the secret's key policy — that is short.
            throw DatabricksErrors.credentialUnreadableThroughRole(
                    config.credentialSecretArn(), source.crossAccountRoleArn(), cause);
        }
    }

    private static String sanitiseForSessionName(String value)
    {
        return (value == null) ? "" : value.trim().replaceAll(SESSION_NAME_ALLOWED, "-");
    }

    /**
     * The prefix, verbatim, checked at construction so a deployment that cannot derive a matching
     * ExternalId fails to start rather than failing every query. {@link Settings#lookUp} already treats a
     * blank variable as unset, so what is refused here is whitespace Python would carry into the
     * published ExternalId.
     */
    private static String requirePrefix(String resourcePrefix)
    {
        if (resourcePrefix == null || resourcePrefix.isEmpty()) {
            throw new IllegalArgumentException(
                    RESOURCE_PREFIX_VAR + " is not set, so no ExternalId can be derived and every"
                            + " assume would be denied. Set it to the deployment's resource prefix,"
                            + " e.g. coa-dev- — including the trailing separator, which is part of the"
                            + " value the customer's trust policy carries.");
        }
        return requireExternalIdOperand(resourcePrefix, RESOURCE_PREFIX_VAR, "the connector's environment");
    }

    /**
     * Reads one secret with a client built for that session, and closes it. A client per read rather than
     * per cached session, which would outlive the session inside it and hold a connection pool per
     * customer role for the life of the container.
     */
    private static String getSecretValue(AwsCredentialsProvider session, String secretArn)
    {
        try (SecretsManagerClient client = SecretsManagerClient.builder()
                .credentialsProvider(session)
                .build()) {
            return client.getSecretValue(
                            GetSecretValueRequest.builder().secretId(secretArn).build())
                    .secretString();
        }
    }

    /**
     * The real STS client, built on first use because this class is constructed before {@code super(...)}
     * in the record handler, where no region or credentials are resolvable yet.
     */
    private static final class LazyStsAssumer implements RoleAssumer
    {
        private volatile StsClient client;

        @Override
        public AssumeRoleResponse assume(AssumeRoleRequest request)
        {
            StsClient snapshot = client;
            if (snapshot == null) {
                snapshot = StsClient.create();
                client = snapshot;
            }
            return snapshot.assumeRole(request);
        }
    }
}
