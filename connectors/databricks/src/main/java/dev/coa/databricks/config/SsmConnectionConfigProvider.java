// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.coa.connector.metrics.ConnectorMetrics;
import dev.coa.databricks.DatabricksMetadataHandler;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import software.amazon.awssdk.awscore.exception.AwsErrorDetails;
import software.amazon.awssdk.awscore.exception.AwsServiceException;
import software.amazon.awssdk.services.ssm.SsmClient;
import software.amazon.awssdk.services.ssm.model.GetParameterRequest;
import software.amazon.awssdk.services.ssm.model.ParameterNotFoundException;

import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ThreadLocalRandom;

/**
 * The multiplexed provider: resolves each source's endpoint from one SSM parameter per Athena catalog
 * name, so a single deployed connector serves every Databricks source in a COA deployment.
 *
 * <p>The discriminator is the Athena catalog name, the only per-source value that reaches the connector.
 * It is a one-way digest, so the connector cannot recover a source id from it and cannot read the sources
 * table directly. Hence the second store.
 *
 * <p>The parameter's threat model is integrity: it holds no credential. Every value read is re-validated
 * against the patterns registration applied, and a parameter carrying another deployment's
 * {@code deploymentId} is refused. Throttles are retried and counted separately from resolution failures.
 *
 * <p>Thread-safe. Design and threat model: LLD §6.2, "Integrity of the config parameter".
 */
public final class SsmConnectionConfigProvider implements ConnectionConfigProvider
{
    private static final Logger LOGGER =
            LoggerFactory.getLogger(SsmConnectionConfigProvider.class);

    /**
     * Environment variable holding the path the per-source parameters sit under, e.g.
     * {@code /coa/dev/connectors/databricks/sources}. <b>The environment segment is part of it</b>: a
     * path without one would let a dev query resolve prod's credential and return prod's rows.
     */
    public static final String SSM_PREFIX_VAR = "COA_CONFIG_SSM_PREFIX";

    /**
     * Environment variable holding this connector's own deployment id, e.g. {@code coa-dev}. Compared
     * against every parameter's {@code deploymentId}, behind the environment-scoped path.
     */
    public static final String DEPLOYMENT_ID_VAR = "COA_DEPLOYMENT_ID";

    /** Default cache lifetime, before jitter. */
    public static final long DEFAULT_TTL_MILLIS = CredentialSource.DEFAULT_TTL_MILLIS;

    /** Attempts per resolution, including the first. Only a throttle is retried. */
    static final int MAX_ATTEMPTS = 4;

    /** First backoff, doubling per attempt and jittered. 50/100/200 ms against a 120 s invocation. */
    static final long DEFAULT_BACKOFF_BASE_MILLIS = 50L;

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** Reads one parameter's value by name. The seam that keeps Parameter Store out of a unit test. */
    @FunctionalInterface
    public interface ParameterReader
    {
        /** @throws RuntimeException as the SDK does; throttling is detected and retried by the caller. */
        String read(String parameterName);
    }

    /** Waits between retries. A seam so a test does not spend the backoff. */
    @FunctionalInterface
    interface Sleeper
    {
        void sleep(long millis);
    }

    private final ParameterReader parameters;
    private final String ssmPrefix;
    private final String deploymentId;
    private final ConnectorMetrics metrics;
    private final long backoffBaseMillis;

    /** Package-private so a test can file an entry by hand and prove {@link ConfigCache} refuses it. */
    final ConfigCache cache;

    private final Sleeper sleeper;

    /**
     * @param ssmPrefix the path the per-source parameters sit under. A trailing {@code /} is stripped:
     *                  {@code //} addresses a different parameter in Parameter Store.
     */
    public SsmConnectionConfigProvider(String ssmPrefix, String deploymentId)
    {
        this(null, ssmPrefix, deploymentId,
                new ConnectorMetrics(DatabricksMetadataHandler.SOURCE_TYPE),
                DEFAULT_TTL_MILLIS, DEFAULT_BACKOFF_BASE_MILLIS, null);
    }

    /**
     * @param parameters null for the real SSM client, built on first use — this class is constructed
     *                   before {@code super(...)} in the record handler, so it has to be constructible
     *                   without credentials or a region.
     * @param ttlMillis  cache lifetime before jitter. Zero disables the cache.
     * @param sleeper    null for {@link Thread#sleep}.
     */
    SsmConnectionConfigProvider(ParameterReader parameters, String ssmPrefix, String deploymentId,
                                ConnectorMetrics metrics, long ttlMillis, long backoffBaseMillis,
                                Sleeper sleeper)
    {
        this.parameters = (parameters != null) ? parameters : new LazySsmReader();
        this.ssmPrefix = requirePrefix(ssmPrefix);
        this.deploymentId = requireDeploymentId(deploymentId);
        this.metrics = Objects.requireNonNull(metrics, "metrics");
        this.cache = new ConfigCache(ttlMillis);
        if (backoffBaseMillis < 0) {
            throw new IllegalArgumentException("backoffBaseMillis must not be negative");
        }
        this.backoffBaseMillis = backoffBaseMillis;
        this.sleeper = (sleeper != null) ? sleeper : SsmConnectionConfigProvider::sleepQuietly;
    }

    /**
     * {@inheritDoc}
     *
     * @throws IllegalStateException if {@code athenaCatalogName} is null or blank. A managed connector's
     *                              whole configuration is per catalog, so there is no deployment-wide
     *                              default to serve instead.
     */
    @Override
    public ConnectionConfig configFor(String athenaCatalogName)
    {
        String catalog = requireCatalogName(athenaCatalogName);
        ConnectionConfig cached = cache.get(catalog);
        if (cached != null) {
            return cached;
        }
        ConnectionConfig resolved = read(catalog);
        cache.put(catalog, resolved);
        return resolved;
    }

    /**
     * {@inheritDoc}
     *
     * <p>Nothing per-source: at cold start no catalog name exists yet, so there is no single endpoint to
     * describe.
     */
    @Override
    public String describe()
    {
        return "mode=" + ConfigSource.COA_MANAGED.wireName()
                + " ssmPrefix=" + ssmPrefix
                + " deploymentId=" + deploymentId;
    }

    /** Reads and validates one parameter, retrying only a throttle. */
    private ConnectionConfig read(String athenaCatalogName)
    {
        String parameterName = ssmPrefix + '/' + athenaCatalogName;
        String body = readWithBackoff(parameterName, athenaCatalogName);
        ConnectionConfig config = parse(parameterName, athenaCatalogName, body);
        // No credential, JDBC URL or predicate value, and no secret or role ARN either: both name
        // customer resources and neither is needed to read this line.
        LOGGER.info("Resolved catalog {} (source {}): host={} unityCatalog={} schema={}",
                athenaCatalogName, config.sourceId(), config.workspaceHostname(), config.catalog(),
                config.isSchemaPinned() ? config.schema() : "<every schema in the catalog>");
        return config;
    }

    private String readWithBackoff(String parameterName, String athenaCatalogName)
    {
        RuntimeException lastThrottle = null;
        for (int attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
            if (attempt > 0) {
                sleeper.sleep(backoffFor(attempt));
            }
            try {
                String body = parameters.read(parameterName);
                if (body == null || body.trim().isEmpty()) {
                    throw new IllegalArgumentException(
                            "SSM parameter " + parameterName + " is empty. It should hold this source's"
                                    + " connection facts as JSON. An empty value means the parameter was"
                                    + " written by something other than the sources API.");
                }
                return body;
            }
            catch (ParameterNotFoundException absent) {
                // Deliberately not retried: no amount of waiting creates a parameter.
                throw new IllegalArgumentException(
                        "No configuration exists for Athena catalog \"" + athenaCatalogName
                                + "\": SSM parameter " + parameterName + " does not exist. Either no"
                                + " Databricks source is registered under that catalog in this"
                                + " deployment, or its registration wrote the catalog and not the"
                                + " parameter. Re-register the source; there is nothing to repair"
                                + " here.", absent);
            }
            catch (RuntimeException cause) {
                if (!isThrottling(cause)) {
                    throw cause;
                }
                lastThrottle = cause;
                // Per attempt, not per resolution: the alarm on this metric is a rate question.
                metrics.count(ConnectorMetrics.CONFIG_THROTTLES, athenaCatalogName);
            }
        }
        throw new IllegalStateException(
                "Parameter Store throttled " + MAX_ATTEMPTS + " attempts to read " + parameterName
                        + ". Nothing is wrong with the configuration: GetParameter is limited to 40"
                        + " transactions per second per account and region by default, and a discovery"
                        + " scan is one read per table across cold containers. Raise that limit, or"
                        + " retry the scan.", lastThrottle);
    }

    /**
     * Turns the parameter body into a validated configuration. Order matters: the deployment-id check
     * runs before any field is read, so a parameter belonging to another deployment is refused before
     * its values are used to build anything.
     */
    private ConnectionConfig parse(String parameterName, String athenaCatalogName, String body)
    {
        JsonNode root = parseJson(parameterName, body);
        requireOwnDeployment(parameterName, text(root, "deploymentId"));

        String origin = "SSM parameter " + parameterName;
        ManagedSource source = ManagedSource.builder()
                .origin(origin)
                // The catalog the parameter was resolved FOR, not a value read out of it: the cache
                // re-checks against this, so taking it from the body would let a repointed parameter
                // satisfy its own check.
                .athenaCatalogName(athenaCatalogName)
                .sourceId(text(root, "sourceId"))
                // Verbatim, alone among these: this one is concatenated into the ExternalId rather than
                // only validated, and Python builds the same string from the same bytes.
                .namespaceId(textVerbatim(root, "namespaceId"))
                .crossAccountRoleArn(text(root, "crossAccountRoleArn"))
                .build();
        ConnectionConfig config = ConnectionConfig.builder()
                .origin(origin)
                .workspaceHostname(text(root, "workspaceHostname"))
                .httpPath(text(root, "httpPath"))
                .catalog(text(root, "databricksCatalog"))
                // REQUIRED in this mode, and requirePinnedSchema rather than text():
                // ConnectionConfig.Builder.schema stores an absent schema as null without validating,
                // which means "serve every schema in the catalog" — the one thing this parameter must
                // not be able to say.
                .schema(requirePinnedSchema(parameterName, text(root, "databaseName")))
                .credentialSecretArn(text(root, "credentialSecretArn"))
                .managedSource(source)
                .build();
        // A structural backstop: no configuration leaves this class unpinned, however it was built.
        if (!config.isSchemaPinned()) {
            throw new IllegalStateException(
                    "Refusing a configuration resolved from SSM parameter " + parameterName + " that is"
                            + " not pinned to one Unity Catalog schema. A managed source is exactly one"
                            + " schema, and an unpinned configuration serves every schema in the catalog"
                            + " its credential can see. This is a defect in this connector rather than a"
                            + " bad parameter: the schema was required a few lines above.");
        }
        return config;
    }

    private static JsonNode parseJson(String parameterName, String body)
    {
        JsonNode root;
        try {
            root = MAPPER.readTree(body);
        }
        // The parse failure alone, so a Jackson upgrade throwing something else is not relabelled "not
        // valid JSON". Chained rather than echoed: the cause carries the syntax error and its offset into
        // the log, while Jackson's own message would render the body into a user-visible error.
        catch (JsonProcessingException cause) {
            throw new IllegalArgumentException(
                    "SSM parameter " + parameterName + " is not valid JSON. It should hold this"
                            + " source's connection facts as a JSON object.", cause);
        }
        if (root == null || !root.isObject()) {
            throw new IllegalArgumentException(
                    "SSM parameter " + parameterName + "'s top level is not a JSON object.");
        }
        return root;
    }

    /**
     * Refuses a parameter that names no schema — the only field here whose <b>absence</b> widens what the
     * connector serves. Unpinned, {@code listDatabases} enumerates every schema in the Unity Catalog
     * catalog and {@code readerFor} accepts any of them, past the one-schema-per-source boundary.
     */
    private static String requirePinnedSchema(String parameterName, String databaseName)
    {
        if (databaseName == null) {
            throw new IllegalArgumentException(
                    "SSM parameter " + parameterName + " names no databaseName. A COA-managed Databricks"
                            + " source is exactly one Unity Catalog schema, and that pin is a containment"
                            + " boundary on top of the credential's own grants — so this is refused rather"
                            + " than treated as \"serve every schema in the catalog\", which is what an"
                            + " absent schema means for a customer-deployed connector. Served unpinned,"
                            + " this one catalog name would read every schema in the Unity Catalog catalog"
                            + " that its credential can see, including schemas no source was registered"
                            + " for. Re-register the source.");
        }
        return databaseName;
    }

    /**
     * Refuses a parameter written for another deployment. Behind the environment-scoped path: if that
     * path ever lost its environment segment, this is what still stops a dev write reaching a prod query.
     */
    private void requireOwnDeployment(String parameterName, String parameterDeploymentId)
    {
        if (parameterDeploymentId == null) {
            throw new IllegalArgumentException(
                    "SSM parameter " + parameterName + " carries no deploymentId. This connector"
                            + " refuses a parameter it cannot attribute to its own deployment ("
                            + deploymentId + "), because environments share an account and the check is"
                            + " what stops one environment's configuration being served to another's"
                            + " query.");
        }
        if (!deploymentId.equals(parameterDeploymentId)) {
            throw new IllegalArgumentException(
                    "SSM parameter " + parameterName + " belongs to deployment \""
                            + parameterDeploymentId + "\" and this connector serves \"" + deploymentId
                            + "\". Refusing it: environments share an account, so serving another"
                            + " deployment's parameter would resolve that deployment's warehouse and"
                            + " credential under a catalog name registered here.");
        }
    }

    /**
     * The trimmed text at {@code key}, or null when absent, null or blank. Right for every field this
     * reads <b>except</b> {@code namespaceId} — see {@link #textVerbatim}.
     */
    private static String text(JsonNode root, String key)
    {
        JsonNode node = root.get(key);
        if (node == null || !node.isTextual()) {
            return null;
        }
        String value = node.textValue().trim();
        return value.isEmpty() ? null : value;
    }

    /**
     * The text at {@code key} exactly as the parameter holds it, or null when absent or not a string.
     *
     * <p>For {@code namespaceId} alone, because that one is <b>concatenated</b> into
     * {@code sts:ExternalId} rather than merely validated, and COA's control plane derives the same string
     * in Python from the same bytes and publishes it for the customer's trust policy. Trimming here would
     * make Java's ExternalId differ from the published one, surfacing as {@code AccessDenied} on every
     * query. {@link AssumedRoleCredentialSource#requireExternalIdOperand} refuses the whitespace instead.
     */
    private static String textVerbatim(JsonNode root, String key)
    {
        JsonNode node = root.get(key);
        return (node == null || !node.isTextual()) ? null : node.textValue();
    }

    private String requireCatalogName(String athenaCatalogName)
    {
        if (athenaCatalogName == null || athenaCatalogName.trim().isEmpty()) {
            throw new IllegalStateException(
                    "This connector was asked to resolve its configuration before Athena named a"
                            + " catalog. In " + ConfigSource.COA_MANAGED.wireName() + " mode there is no"
                            + " deployment-wide endpoint to fall back to: every source's workspace,"
                            + " schema and credential come from the parameter for the catalog a request"
                            + " arrived under, so a resolution with no catalog name has nothing to"
                            + " resolve. This is a defect in the connector rather than a"
                            + " misconfiguration — a cold-start path is reading configuration it should"
                            + " be reading per request.");
        }
        String catalog = athenaCatalogName.trim();
        // ManagedSource's rule, not a second copy of it: the same string becomes that class's
        // athenaCatalogName.
        if (!ManagedSource.isAthenaCatalogName(catalog)) {
            throw new IllegalArgumentException(
                    "Athena catalog name \"" + catalog + "\" is not a name COA derives, so no"
                            + " configuration is looked up for it. Expected letters, digits and"
                            + " underscores: the name becomes the last segment of a Parameter Store"
                            + " path, and one containing a separator would address a different"
                            + " parameter.");
        }
        return catalog;
    }

    /**
     * Whether {@code cause} is Parameter Store saying "too fast" rather than "no" — the one condition
     * worth retrying. The explicit list covers the spellings
     * {@link AwsServiceException#isThrottlingException()} does not know.
     */
    static boolean isThrottling(RuntimeException cause)
    {
        if (!(cause instanceof AwsServiceException)) {
            return false;
        }
        AwsServiceException aws = (AwsServiceException) cause;
        if (aws.isThrottlingException()) {
            return true;
        }
        AwsErrorDetails details = aws.awsErrorDetails();
        String code = (details == null) ? null : details.errorCode();
        if (code == null) {
            return false;
        }
        switch (code) {
            case "ThrottlingException":
            case "ThrottledException":
            case "Throttling":
            case "TooManyRequestsException":
            case "TooManyUpdates":
            case "RequestThrottled":
            case "RequestThrottledException":
            case "RequestLimitExceeded":
                return true;
            default:
                return false;
        }
    }

    /** Exponential, jittered so a fan-out's retries do not re-align on the next attempt either. */
    private long backoffFor(int attempt)
    {
        long base = backoffBaseMillis * (1L << (attempt - 1));
        if (base <= 0) {
            return 0;
        }
        return base / 2 + ThreadLocalRandom.current().nextLong(base);
    }

    private static void sleepQuietly(long millis)
    {
        if (millis <= 0) {
            return;
        }
        try {
            Thread.sleep(millis);
        }
        catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }

    private static String requirePrefix(String ssmPrefix)
    {
        if (ssmPrefix == null || ssmPrefix.trim().isEmpty()) {
            throw new IllegalArgumentException(
                    SSM_PREFIX_VAR + " is not set, so this connector has no path to resolve a source's"
                            + " configuration under. Set it to the deployment's environment-scoped"
                            + " sources path, e.g. /coa/dev/connectors/databricks/sources.");
        }
        String prefix = ssmPrefix.trim();
        while (prefix.endsWith("/")) {
            prefix = prefix.substring(0, prefix.length() - 1);
        }
        if (prefix.isEmpty()) {
            throw new IllegalArgumentException(
                    SSM_PREFIX_VAR + " is only separators. Set it to the deployment's"
                            + " environment-scoped sources path, e.g."
                            + " /coa/dev/connectors/databricks/sources.");
        }
        return prefix;
    }

    private static String requireDeploymentId(String deploymentId)
    {
        if (deploymentId == null || deploymentId.trim().isEmpty()) {
            throw new IllegalArgumentException(
                    DEPLOYMENT_ID_VAR + " is not set, so this connector cannot tell its own"
                            + " deployment's parameters from another environment's. Set it to the"
                            + " deployment id, e.g. coa-dev.");
        }
        return deploymentId.trim();
    }

    /**
     * One resolved configuration per Athena catalog, held for a jittered TTL — see {@link ExpiringCache}.
     *
     * <p><b>{@link #get} re-checks the entry against the catalog it was built for, on every use.</b> On a
     * shared connector serving several tenants from one container this cache is the one place where a
     * cross-wiring defect would return another namespace's rows rather than an error, so the value
     * carries {@link ManagedSource#athenaCatalogName()} and a mismatch fails the request. {@link #put}
     * takes the key separately so a test can file an entry under the wrong catalog.
     */
    static final class ConfigCache
    {
        private final ExpiringCache<String, ConnectionConfig> byCatalog;

        ConfigCache(long ttlMillis)
        {
            this.byCatalog = new ExpiringCache<>(ttlMillis);
        }

        /**
         * The cached configuration for {@code athenaCatalogName}, or null when there is none or it has
         * expired.
         *
         * @throws IllegalStateException if a cached entry was built for a different catalog. The
         *                              alternative is answering with another namespace's warehouse and
         *                              credential, and no error.
         */
        ConnectionConfig get(String athenaCatalogName)
        {
            ConnectionConfig cached = byCatalog.get(athenaCatalogName);
            if (cached == null) {
                return null;
            }
            String builtFor = cached.isCoaManaged()
                    ? cached.managedSource().athenaCatalogName()
                    : null;
            if (!athenaCatalogName.equals(builtFor)) {
                byCatalog.remove(athenaCatalogName);
                throw new IllegalStateException(
                        "Refusing a cached configuration: it is filed under Athena catalog \""
                                + athenaCatalogName + "\" and was resolved for \"" + builtFor
                                + "\". These cannot differ unless this connector has a cross-wiring"
                                + " defect, and serving the entry would answer one namespace's query"
                                + " from another namespace's warehouse and credential with nothing"
                                + " erroring. The entry has been dropped; retrying re-reads it.");
            }
            return cached;
        }

        /** @param athenaCatalogName the key. Production passes the catalog {@code config} was built for. */
        void put(String athenaCatalogName, ConnectionConfig config)
        {
            byCatalog.put(athenaCatalogName, config);
        }
    }

    /**
     * The real SSM client, built on first use because this class is constructed before {@code super(...)}
     * in the record handler, where no region or credentials are resolvable yet.
     */
    private static final class LazySsmReader implements ParameterReader
    {
        private volatile SsmClient client;

        @Override
        public String read(String parameterName)
        {
            SsmClient snapshot = client;
            if (snapshot == null) {
                snapshot = SsmClient.create();
                client = snapshot;
            }
            // No WithDecryption: the parameter is a plaintext String by design, since nothing in it is
            // secret and the threat to it is integrity rather than confidentiality.
            return snapshot.getParameter(
                            GetParameterRequest.builder().name(parameterName).build())
                    .parameter()
                    .value();
        }
    }
}
