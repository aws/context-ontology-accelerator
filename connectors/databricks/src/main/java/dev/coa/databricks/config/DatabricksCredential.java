// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.util.Properties;

/**
 * The credential from the secret, and the JDBC properties that present it. Immutable, and
 * {@link #toString()} names the mode and nothing else.
 *
 * <p>The secret's shape selects the auth mode:
 *
 * <pre>
 *   {"token": "dapi..."}                              -> personal access token
 *   {"client_id": "...", "client_secret": "..."}      -> OAuth machine-to-machine
 * </pre>
 *
 * <p>Nothing declares the mode separately, so a customer cannot say one thing and store another. A
 * secret carrying both shapes is refused rather than resolved by precedence, since which one won would
 * be invisible and the wrong answer leaves a live credential unused while a stale one authenticates.
 * Prefer OAuth M2M: a personal access token carries a human's identity and expires on a schedule the
 * workspace admin may not control.
 *
 * <p>{@link #applyTo(Properties)} is the only way out of this class, so a credential never touches the
 * JDBC URL. That URL is a {@code ;}-delimited property list, and the driver logs connection strings
 * including {@code PWD=} when logging is on. A {@link Properties} object is neither parsed nor logged.
 */
public final class DatabricksCredential
{
    /** Which authentication mechanism the secret selected. */
    public enum Mode
    {
        /** {@code AuthMech=3} — a personal access token, presented as user {@code token}. */
        PERSONAL_ACCESS_TOKEN,

        /** {@code AuthMech=11}, {@code Auth_Flow=1} — OAuth 2.0 client credentials. */
        OAUTH_M2M
    }

    /** Secret key selecting {@link Mode#PERSONAL_ACCESS_TOKEN}. */
    public static final String TOKEN_KEY = "token";

    /** Secret key selecting {@link Mode#OAUTH_M2M}, with {@link #CLIENT_SECRET_KEY}. */
    public static final String CLIENT_ID_KEY = "client_id";

    /** Secret key selecting {@link Mode#OAUTH_M2M}, with {@link #CLIENT_ID_KEY}. */
    public static final String CLIENT_SECRET_KEY = "client_secret";

    private static final ObjectMapper MAPPER = new ObjectMapper();

    // Property spellings verified against the driver's own DatabricksJdbcUrlParams and its
    // AuthMech switch, which accepts exactly 3 (PAT) and 11 (OAuth).
    private static final String AUTH_MECH = "AuthMech";
    private static final String AUTH_MECH_PAT = "3";
    private static final String AUTH_MECH_OAUTH = "11";
    private static final String AUTH_FLOW = "Auth_Flow";
    private static final String AUTH_FLOW_CLIENT_CREDENTIALS = "1";
    private static final String UID = "UID";
    private static final String UID_TOKEN = "token";
    private static final String PWD = "PWD";
    private static final String OAUTH2_CLIENT_ID = "OAuth2ClientId";
    private static final String OAUTH2_SECRET = "OAuth2Secret";

    private final Mode mode;
    private final String primary;
    private final String secondary;

    private DatabricksCredential(Mode mode, String primary, String secondary)
    {
        this.mode = mode;
        this.primary = primary;
        this.secondary = secondary;
    }

    /**
     * Parses a secret's value.
     *
     * @param secretJson the secret string, as {@code GetSecretValue} returns it.
     * @param secretArn  the secret's ARN, for the error message. An operator with several secrets needs
     *                   to know which one is wrong. Nothing from the value is ever echoed.
     * @throws IllegalArgumentException if the value is not JSON, is neither supported shape, or carries
     *                                 both.
     */
    public static DatabricksCredential fromSecretJson(String secretJson, String secretArn)
    {
        JsonNode root = parse(secretJson, secretArn);

        String token = text(root, TOKEN_KEY);
        String clientId = text(root, CLIENT_ID_KEY);
        String clientSecret = text(root, CLIENT_SECRET_KEY);

        boolean looksLikePat = token != null;
        boolean looksLikeOauth = clientId != null || clientSecret != null;

        if (looksLikePat && looksLikeOauth) {
            throw reject(secretArn, "it carries both \"" + TOKEN_KEY + "\" and OAuth keys."
                    + " Exactly one auth mode per secret: which one took effect would otherwise be"
                    + " invisible.");
        }
        if (looksLikePat) {
            return new DatabricksCredential(Mode.PERSONAL_ACCESS_TOKEN, token, null);
        }
        if (looksLikeOauth) {
            if (clientId == null || clientSecret == null) {
                throw reject(secretArn, "OAuth machine-to-machine needs both \"" + CLIENT_ID_KEY
                        + "\" and \"" + CLIENT_SECRET_KEY + "\"; only one is present or non-empty.");
            }
            return new DatabricksCredential(Mode.OAUTH_M2M, clientId, clientSecret);
        }
        throw reject(secretArn, "it is neither supported shape.");
    }

    /** Which auth mechanism the secret selected. */
    public Mode mode()
    {
        return mode;
    }

    /**
     * Writes this credential into the driver's connection properties.
     *
     * @param properties the properties handed to {@code DriverManager.getConnection}. Mutated.
     */
    public void applyTo(Properties properties)
    {
        if (properties == null) {
            throw new IllegalArgumentException("Connection properties must not be null");
        }
        if (mode == Mode.PERSONAL_ACCESS_TOKEN) {
            properties.setProperty(AUTH_MECH, AUTH_MECH_PAT);
            properties.setProperty(UID, UID_TOKEN);
            properties.setProperty(PWD, primary);
            return;
        }
        properties.setProperty(AUTH_MECH, AUTH_MECH_OAUTH);
        properties.setProperty(AUTH_FLOW, AUTH_FLOW_CLIENT_CREDENTIALS);
        properties.setProperty(OAUTH2_CLIENT_ID, primary);
        properties.setProperty(OAUTH2_SECRET, secondary);
    }

    /** The mode only. No part of the credential appears here. */
    @Override
    public String toString()
    {
        return "DatabricksCredential{mode=" + mode + "}";
    }

    private static JsonNode parse(String secretJson, String secretArn)
    {
        if (secretJson == null || secretJson.trim().isEmpty()) {
            throw reject(secretArn, "it is empty.");
        }
        JsonNode root;
        try {
            root = MAPPER.readTree(secretJson);
        }
        catch (Exception cause) {
            // The parse failure's own message can quote the input, so it is neither chained nor echoed.
            throw reject(secretArn, "it is not valid JSON.");
        }
        if (root == null || !root.isObject()) {
            throw reject(secretArn, "its top level is not a JSON object.");
        }
        return root;
    }

    /** The trimmed text value at {@code key}, or null when absent, null or blank. */
    private static String text(JsonNode root, String key)
    {
        JsonNode node = root.get(key);
        if (node == null || !node.isTextual()) {
            return null;
        }
        String value = node.textValue().trim();
        return value.isEmpty() ? null : value;
    }

    private static IllegalArgumentException reject(String secretArn, String because)
    {
        return new IllegalArgumentException(
                "Secret " + secretArn + " cannot be used as a Databricks credential: " + because
                        + " Expected exactly one of {\"" + TOKEN_KEY + "\": \"...\"} for a personal"
                        + " access token, or {\"" + CLIENT_ID_KEY + "\": \"...\", \""
                        + CLIENT_SECRET_KEY + "\": \"...\"} for OAuth machine-to-machine.");
    }
}
