// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.jdbc;

import com.amazonaws.athena.connector.lambda.exceptions.AthenaConnectorException;
import software.amazon.awssdk.services.glue.model.ErrorDetails;
import software.amazon.awssdk.services.glue.model.FederationSourceErrorCode;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Turns a driver failure into an Athena-facing error that says which of three things went wrong.
 *
 * <p>A SQL Warehouse that is not running answers with a temporarily-unavailable error and resumes in
 * the background, taking seconds for serverless and minutes for classic and pro. The driver would
 * retry that for up to 900 seconds, outliving the connector's 120-second timeout, so
 * {@link DatabricksConnectionFactory} turns the retry off and this class labels the result. Athena
 * re-invokes a failed connector, so blocking turns one query into several billed Lambda-minutes and
 * several warehouse resumes.
 *
 * <p>Every message goes through {@link #redact(String)}. A driver message can quote the connection
 * string, and that string carries {@code PWD=} and {@code OAuth2Secret=} when a caller builds it that
 * way. This connector never does, but the cost of the regex is lower than a credential in CloudWatch.
 */
public final class DatabricksErrors
{
    /**
     * Phrases the driver and the warehouse use for "not running yet". Matched case-insensitively
     * against the whole exception chain: the useful text is often on a cause rather than on the
     * {@link java.sql.SQLException} the JDBC API hands back.
     */
    private static final String[] WAREHOUSE_STARTING_MARKERS = {
        "temporarily_unavailable",
        "temporarily unavailable",
        "warehouse is starting",
        "endpoint is starting",
        "starting_up",
        "cluster is starting",
    };

    /**
     * Phrases for a credential the workspace rejected. Specific because a loose marker such as
     * {@code "credential"} also catches "unable to load AWS credentials", a different fault with a
     * different first action.
     */
    private static final String[] AUTHENTICATION_MARKERS = {
        "invalid access token",
        "unauthorized",
        "forbidden",
        "authentication failed",
        "invalid_client",
        "pe_error_invalid_credentials",
    };

    /**
     * An HTTP status code, in a status-code context.
     *
     * <p>A plain substring test for {@code "503"} matches any hex id containing those digits, and
     * Databricks embeds them routinely ({@code [queryId: 01f0a503-...]}). That reads a missing table
     * as a warehouse still starting, telling the user to retry a query that can never succeed. So the
     * digits have to be preceded by a status-code word within a short distance and must not run into
     * further alphanumerics or a hyphen on either side, which is what excludes a UUID segment.
     *
     * <p>The optional version group is what admits a raw status line, {@code HTTP/1.1 503 Service
     * Unavailable}. Without it the gap between the keyword and the digits is
     * {@code [^0-9a-z]{0,12}}, which cannot cross the {@code 1.1}, so the commonest form of all did not
     * match. It failed safe — {@code DATABRICKS_REQUEST_FAILED} rather than a wrong retry hint — but it
     * failed.
     */
    private static final Pattern HTTP_STATUS = Pattern.compile(
            "(?i)\\b(?:http|https|status|statuscode|status code|response code|code|error code)\\b"
                    + "(?:/\\d(?:\\.\\d+)?)?"
                    + "[^0-9a-z]{0,12}(?<![0-9a-z-])(\\d{3})(?![0-9a-z-])");

    /** Status codes that mean the warehouse is not running yet. */
    private static final Set<String> WAREHOUSE_STARTING_STATUSES =
            new HashSet<>(Arrays.asList("503"));

    /** Status codes that mean the workspace rejected the credential or the grant. */
    private static final Set<String> AUTHENTICATION_STATUSES =
            new HashSet<>(Arrays.asList("401", "403"));

    /**
     * {@code key=value} for keys that can carry secret material, in a {@code ;}-delimited property
     * list or a URL query string. The value runs to the next delimiter.
     */
    private static final Pattern SENSITIVE_PROPERTY = Pattern.compile(
            "(?i)\\b(pwd|password|oauth2secret|auth_accesstoken|auth_refreshtoken|oauthrefreshtoken"
                    + "|sslkeystorepwd|ssltruststorepwd|proxypwd|cfproxypwd|tokencachepassphrase"
                    + "|auth_jwt_key_passphrase)\\s*=\\s*[^;&\\s]*");

    /** Prefix on the message for a warehouse that is not running. Stable: alarms match on it. */
    public static final String WAREHOUSE_STARTING_PREFIX = "DATABRICKS_WAREHOUSE_NOT_RUNNING";

    /** Prefix on the message for a credential the workspace rejected. */
    public static final String AUTHENTICATION_PREFIX = "DATABRICKS_AUTHENTICATION_FAILED";

    /** Prefix on the message for anything else. */
    public static final String QUERY_FAILED_PREFIX = "DATABRICKS_REQUEST_FAILED";

    /**
     * Prefix on the message for a table that exceeded the connector's row ceiling. Kept here beside
     * the other three rather than inline at its throw site: it is the same kind of thing, a stable
     * token the README documents and an alarm can match on.
     */
    public static final String TABLE_TOO_LARGE_PREFIX = "DATABRICKS_TABLE_TOO_LARGE";

    /**
     * Prefix for a failure that did not come from Databricks: a bug in this connector, a missing IAM
     * grant, a packaging fault. Not prefixed {@code DATABRICKS_}, because every other prefix here
     * names the warehouse as the thing at fault and an operator reading one goes and looks there.
     */
    public static final String CONNECTOR_INTERNAL_PREFIX = "CONNECTOR_INTERNAL_ERROR";

    /**
     * Prefix for a credential that could not be <b>read</b>, as against
     * {@link #AUTHENTICATION_PREFIX}, which is Databricks rejecting one that was read fine. One is an
     * IAM problem in this account, the other a credential problem in the workspace, and they have no
     * steps in common.
     */
    public static final String CREDENTIAL_UNREADABLE_PREFIX = "CONNECTOR_CREDENTIAL_UNREADABLE";

    /**
     * Prefix for a customer-owned role the connector could not <b>assume</b>, as against
     * {@link #CREDENTIAL_UNREADABLE_PREFIX}, which is a role that was assumed and then could not read
     * the secret. Its own prefix and its own metric because the fix is the role's <b>trust</b> policy or
     * the ExternalId condition on it, and COA neither owns nor can repair either.
     */
    public static final String CREDENTIAL_ASSUME_DENIED_PREFIX = "CONNECTOR_CREDENTIAL_ASSUME_DENIED";

    /** Package prefix of every class the Databricks JDBC driver throws from. */
    private static final String DRIVER_PACKAGE = "com.databricks.";

    private DatabricksErrors()
    {
    }

    /**
     * Classifies anything thrown while talking to Databricks, leaving this connector's own errors
     * alone.
     *
     * <p>The driver has four exception families and only one of them is a {@link java.sql.SQLException}:
     *
     * <pre>
     *   DatabricksSQLException             extends java.sql.SQLException
     *   DatabricksDriverException          extends java.lang.RuntimeException
     *   DatabricksTelemetryException       extends java.lang.RuntimeException
     *   DatabricksRetryHandlerException    extends java.io.IOException
     * </pre>
     *
     * <p>The authentication path throws the {@code RuntimeException} one:
     * {@code OAuthRefreshCredentialsProvider}, {@code DatabricksTokenFederationProvider},
     * {@code DatabricksClientConfiguratorManager} and {@code AuthMech} all do, verified in the 3.4.2
     * bytecode. So with OAuth machine-to-machine a rotated-out {@code client_secret} escapes a catch on
     * {@code SQLException} alone, and escapes {@link #redact(String)} with it. The catch sites widen to
     * {@code RuntimeException} and route through here, and this method owns the judgement that
     * widening creates: which exceptions are the driver's and which are ours.
     *
     * @param operation what the connector was doing, in a few words.
     * @return {@code cause} unchanged when it is already this connector's own error; otherwise a
     *         classified, redacted {@link AthenaConnectorException}.
     */
    public static RuntimeException asConnectorFailure(String operation, Throwable cause)
    {
        if (cause instanceof AthenaConnectorException) {
            // Already classified and redacted. Re-wrapping would bury the specific message under a
            // generic one - RowCeilingSpiller's ceiling error arrives this way.
            return (AthenaConnectorException) cause;
        }
        if (!isFromDriver(cause)) {
            if (cause instanceof IllegalArgumentException) {
                // This connector's own validation: an unusable secret shape, an unknown table, a
                // schema it does not serve. Those messages are written for the operator and say more
                // than any classification would, and they are ours, so there is nothing to redact.
                return (IllegalArgumentException) cause;
            }
            // A bug here, a missing IAM grant, a packaging fault. It must NOT go through classify(),
            // whose markers and status codes only mean something about Databricks.
            return internalFailure(operation, cause);
        }
        return classify(operation, cause);
    }

    /**
     * Reports a failure that did not come from Databricks, without pretending otherwise.
     *
     * <p>{@link #classify} answers "which kind of Databricks failure is this" by matching phrases and
     * HTTP status codes. Run against text that never came from the driver, every match is a
     * coincidence with a confident label attached, and those labels are documented as stable alarm
     * tokens, so a misclassification fires the wrong alarm as well as misleading the reader. A missing
     * {@code kms:Decrypt} grant is the likeliest first-deploy failure and reaches here.
     *
     * <p>The message is still redacted: provenance says nothing about whether the text is safe.
     *
     * @param cause the failure, known not to be the driver's.
     */
    private static AthenaConnectorException internalFailure(String operation, Throwable cause)
    {
        String detail = redact(rootMessage(cause));
        return new AthenaConnectorException(
                CONNECTOR_INTERNAL_PREFIX + ": the connector failed for a reason that did not come"
                        + " from Databricks — look at the connector's own logs and IAM before the"
                        + " warehouse. While " + operation + "."
                        + (detail == null ? "" : " Cause: " + cause.getClass().getSimpleName()
                                + ": " + detail),
                // Not OPERATION_TIMEOUT_EXCEPTION. A bug here and a missing grant are both
                // deterministic, and Athena re-invokes a failed connector, so labelling either
                // transient buys N identical failures and, on the read path, N billed warehouse reads.
                ErrorDetails.builder()
                        .errorCode(FederationSourceErrorCode.INTERNAL_SERVICE_EXCEPTION.toString())
                        .build());
    }

    /**
     * Reports a credential that could not be read out of Secrets Manager. Its own prefix because it has
     * a known cause and a specific first action, and it is what a first deployment usually hits.
     *
     * @param secretArn the secret the connector tried to read. Named because an operator with several
     *                  needs to know which one, and because it is an ARN rather than a credential.
     */
    public static AthenaConnectorException credentialUnreadable(String secretArn, Throwable cause)
    {
        String detail = redact(rootMessage(cause));
        return new AthenaConnectorException(
                CREDENTIAL_UNREADABLE_PREFIX + ": could not read the credential from " + secretArn
                        + ". This is an IAM problem in the connector's own account, not a Databricks"
                        + " one. Check the function's role holds secretsmanager:GetSecretValue on that"
                        + " secret, and — if the secret uses a customer-managed key — kms:Decrypt on"
                        + " that key, granted on BOTH the role and the key's own policy. The CDK app"
                        + " grants the role half from CREDENTIAL_KMS_KEY_ARN; the key policy is the key"
                        + " owner's."
                        + (detail == null ? "" : " Cause: " + cause.getClass().getSimpleName()
                                + ": " + detail),
                ErrorDetails.builder()
                        .errorCode(FederationSourceErrorCode.ACCESS_DENIED_EXCEPTION.toString())
                        .build());
    }

    /**
     * Reports a secret that could not be read <b>through an assumed role</b>: the assume worked, so the
     * trust policy is right, and it is the role's permission policy or the secret's key policy that is
     * short.
     *
     * <p>Shares {@link #CREDENTIAL_UNREADABLE_PREFIX} with the direct-read case — the same event to an
     * alarm — while the advice differs: in this mode the connector holds no Secrets Manager grant of its
     * own, so pointing an operator at the function's role sends them to a policy that grants nothing.
     *
     * @param roleArn the customer-owned role the secret was read as.
     */
    public static AthenaConnectorException credentialUnreadableThroughRole(
            String secretArn, String roleArn, Throwable cause)
    {
        String detail = redact(rootMessage(cause));
        return new AthenaConnectorException(
                CREDENTIAL_UNREADABLE_PREFIX + ": assumed " + roleArn + " successfully, but could not"
                        + " read the credential from " + secretArn + " as that session. The trust"
                        + " policy is therefore fine and the role's PERMISSION policy is not: it needs"
                        + " secretsmanager:GetSecretValue on that one secret, plus kms:Decrypt on the"
                        + " secret's key if that key is customer-managed — which AWS requires whenever"
                        + " the secret and the role are in different accounts, because the AWS-managed"
                        + " aws/secretsmanager key cannot be read from outside its own account by any"
                        + " policy. This connector holds no Secrets Manager or KMS permission of its"
                        + " own, so there is nothing to grant on COA's side."
                        + (detail == null ? "" : " Cause: " + cause.getClass().getSimpleName()
                                + ": " + detail),
                ErrorDetails.builder()
                        .errorCode(FederationSourceErrorCode.ACCESS_DENIED_EXCEPTION.toString())
                        .build());
    }

    /**
     * Reports a customer-owned role the connector could not assume.
     *
     * @param externalId the ExternalId that was presented. Safe to echo: an anti-confusion token derived
     *                   from the deployment prefix and namespace id, not a secret — and the commonest
     *                   cause is a trust policy conditioned on a different value, so the two strings side
     *                   by side are the whole diagnosis.
     */
    public static AthenaConnectorException credentialAssumeDenied(
            String roleArn, String externalId, Throwable cause)
    {
        String detail = redact(rootMessage(cause));
        return new AthenaConnectorException(
                CREDENTIAL_ASSUME_DENIED_PREFIX + ": could not assume " + roleArn + ", so the"
                        + " credential behind it was never reached. This is a policy in the role's own"
                        + " account, not a Databricks problem and not a grant COA can make. Check that"
                        + " the role's trust policy names this connector's execution role and"
                        + " conditions sts:ExternalId on exactly \"" + externalId + "\" — that value is"
                        + " what the COA UI publishes for the namespace, and a mismatch of one"
                        + " character reads as a plain AccessDenied. Check too that the role's name"
                        + " begins with the deployment's reserved datasource-access prefix, since the"
                        + " connector's own assume grant is scoped to it."
                        + (detail == null ? "" : " Cause: " + cause.getClass().getSimpleName()
                                + ": " + detail),
                ErrorDetails.builder()
                        .errorCode(FederationSourceErrorCode.ACCESS_DENIED_EXCEPTION.toString())
                        .build());
    }

    /**
     * Whether any exception in {@code cause}'s chain comes from the Databricks driver.
     *
     * <p>Matched on the package rather than on a type, so all four of the driver's exception
     * hierarchies are covered and a fifth would be too. The call sites reach three of them directly;
     * {@code DatabricksRetryHandlerException} extends {@link java.io.IOException} and arrives converted,
     * as {@code DatabricksHttpClient.throwHttpException} turns it into a {@code DatabricksHttpException},
     * which is a {@code SQLException}. Matching on the package makes that conversion irrelevant here.
     */
    public static boolean isFromDriver(Throwable cause)
    {
        Throwable current = cause;
        for (int depth = 0; current != null && depth < 20; depth++) {
            if (current.getClass().getName().startsWith(DRIVER_PACKAGE)) {
                return true;
            }
            Throwable next = current.getCause();
            current = (next == current) ? null : next;
        }
        return false;
    }

    /**
     * Classifies a driver failure. <b>{@code cause} has to have come from the driver</b>: every
     * heuristic below is about Databricks, and applied to anything else a match is a coincidence
     * wearing a stable alarm token. Package-private for that reason, with
     * {@link #asConnectorFailure} the only way in from outside; visible here so the classification can
     * be tested without a real driver exception for every case.
     *
     * @param operation what the connector was doing, in a few words, e.g.
     *                  {@code "reading information_schema.columns for orders"}. It appears in the
     *                  message, so it must not carry a predicate value.
     * @param cause     a failure from the Databricks driver. Its whole chain is inspected.
     */
    static AthenaConnectorException classify(String operation, Throwable cause)
    {
        String chain = chainText(cause);
        Set<String> statuses = httpStatuses(chain);
        if (containsAny(chain, WAREHOUSE_STARTING_MARKERS)
                || anyIn(statuses, WAREHOUSE_STARTING_STATUSES)) {
            return build(WAREHOUSE_STARTING_PREFIX,
                    operation,
                    "the Databricks SQL Warehouse is not running. It resumes on its own — retry"
                            + " shortly. Serverless warehouses resume in seconds; classic and pro"
                            + " warehouses take minutes.",
                    cause,
                    FederationSourceErrorCode.OPERATION_TIMEOUT_EXCEPTION);
        }
        if (containsAny(chain, AUTHENTICATION_MARKERS)
                || anyIn(statuses, AUTHENTICATION_STATUSES)) {
            return build(AUTHENTICATION_PREFIX,
                    operation,
                    "Databricks rejected the credential. Check the secret's value is current and"
                            + " that its principal holds USE CATALOG, USE SCHEMA and SELECT on the"
                            + " schema this connector exposes.",
                    cause,
                    FederationSourceErrorCode.INVALID_CREDENTIALS_EXCEPTION);
        }
        return build(QUERY_FAILED_PREFIX,
                operation,
                "the request to Databricks failed.",
                cause,
                FederationSourceErrorCode.INTERNAL_SERVICE_EXCEPTION);
    }

    /**
     * Replaces the value of every credential-bearing property in {@code message} with
     * {@code <redacted>}. Null in, null out.
     */
    public static String redact(String message)
    {
        if (message == null) {
            return null;
        }
        Matcher matcher = SENSITIVE_PROPERTY.matcher(message);
        StringBuilder out = new StringBuilder();
        int copiedTo = 0;
        while (matcher.find()) {
            out.append(message, copiedTo, matcher.start());
            out.append(matcher.group(1)).append("=<redacted>");
            copiedTo = matcher.end();
        }
        out.append(message.substring(copiedTo));
        return out.toString();
    }

    /** Whether {@code cause}'s chain reads as a warehouse that is not running yet. */
    public static boolean isWarehouseStarting(Throwable cause)
    {
        String chain = chainText(cause);
        return containsAny(chain, WAREHOUSE_STARTING_MARKERS)
                || anyIn(httpStatuses(chain), WAREHOUSE_STARTING_STATUSES);
    }

    private static AthenaConnectorException build(String prefix, String operation, String advice,
                                                 Throwable cause, FederationSourceErrorCode code)
    {
        String detail = redact(rootMessage(cause));
        String message = prefix + ": " + advice
                + " While " + operation + "."
                + (detail == null ? "" : " Driver reported: " + detail);
        return new AthenaConnectorException(message,
                ErrorDetails.builder().errorCode(code.toString()).build());
    }

    /** The whole cause chain's messages, lower-cased and joined, for marker matching. */
    private static String chainText(Throwable cause)
    {
        StringBuilder out = new StringBuilder();
        Throwable current = cause;
        // Bounded: a driver that builds a cyclic chain would otherwise spin here.
        for (int depth = 0; current != null && depth < 20; depth++) {
            out.append(current.getClass().getSimpleName()).append(' ');
            if (current.getMessage() != null) {
                out.append(current.getMessage()).append(' ');
            }
            if (current instanceof java.sql.SQLException) {
                java.sql.SQLException sql = (java.sql.SQLException) current;
                out.append(sql.getSQLState()).append(' ').append(sql.getErrorCode()).append(' ');
            }
            Throwable next = current.getCause();
            current = (next == current) ? null : next;
        }
        return out.toString().toLowerCase(Locale.ROOT);
    }

    /** The deepest non-null message in the chain, which is normally the useful one. */
    private static String rootMessage(Throwable cause)
    {
        String deepest = null;
        Throwable current = cause;
        for (int depth = 0; current != null && depth < 20; depth++) {
            if (current.getMessage() != null && !current.getMessage().trim().isEmpty()) {
                deepest = current.getMessage().trim();
            }
            Throwable next = current.getCause();
            current = (next == current) ? null : next;
        }
        return deepest;
    }

    /** Every HTTP status code appearing in a status-code context in {@code chain}. */
    private static Set<String> httpStatuses(String chain)
    {
        Set<String> statuses = new HashSet<>();
        Matcher matcher = HTTP_STATUS.matcher(chain);
        while (matcher.find()) {
            statuses.add(matcher.group(1));
        }
        return statuses;
    }

    private static boolean anyIn(Set<String> found, Set<String> interesting)
    {
        for (String status : found) {
            if (interesting.contains(status)) {
                return true;
            }
        }
        return false;
    }

    private static boolean containsAny(String haystack, String[] needles)
    {
        for (String needle : needles) {
            if (haystack.contains(needle)) {
                return true;
            }
        }
        return false;
    }
}
