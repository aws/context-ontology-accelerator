// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import java.util.Locale;
import java.util.Objects;
import java.util.regex.Pattern;

/**
 * Where one Databricks SQL Warehouse is, which Unity Catalog schema (if any) this connector is pinned to,
 * and which secret holds the credential. Immutable, built through {@link #builder()}; each setter
 * validates immediately, so a failure names the offending field.
 *
 * <p>The catalog is required, the schema is not. An Athena federated catalog has one namespace level below
 * the registered catalog name and this connector spends it on the Unity Catalog schema, so the UC catalog
 * cannot travel in a request while the schema always does. Unset, {@link #SCHEMA_VAR} serves every schema
 * in the catalog; set, it is a containment boundary ({@link #isSchemaPinned()}).
 *
 * <p>Every field is patterned, and that is a security control. The Databricks JDBC URL is a
 * {@code ;}-delimited property list, so a value containing {@code ;} sets driver properties
 * ({@code SSL=0} downgrades TLS, {@code ProxyHost} redirects an authenticated session, {@code LogPath}
 * writes connection details to disk), and the catalog name is interpolated into
 * {@code information_schema} SQL. Connection material travels only in a {@link java.util.Properties}
 * object ({@link dev.coa.databricks.jdbc.DatabricksConnectionFactory}); these patterns are the second
 * layer. All bar {@code ;} and every quote character, and the four identifier-shaped fields also bar
 * {@code =}. The secret ARN cannot, since {@code =} is legal in a secret name and that value reaches only
 * {@code GetSecretValue}.
 *
 * <p>{@link #managedSource()} is present only when the configuration came from a COA-managed source
 * parameter rather than the connector's own environment. It carries the source and namespace ids and the
 * customer-owned role the credential is behind, and is what {@link #isCoaManaged()} answers on.
 *
 * <p>The hostname is lower-cased because DNS is case-insensitive and the pattern is not. The catalog and
 * schema are lower-cased because {@code information_schema} stores identifiers that way; the folded schema
 * is also the Athena schema name advertised, so the two agree.
 */
public final class ConnectionConfig
{
    /** Environment variable naming the workspace host. Also the label in every error message. */
    public static final String WORKSPACE_HOSTNAME_VAR = "DATABRICKS_WORKSPACE_HOSTNAME";

    /** Environment variable naming the SQL Warehouse HTTP path. */
    public static final String HTTP_PATH_VAR = "DATABRICKS_HTTP_PATH";

    /** Environment variable naming the Unity Catalog catalog. */
    public static final String CATALOG_VAR = "DATABRICKS_CATALOG";

    /**
     * Environment variable pinning this connector to a single Unity Catalog schema. <b>Optional.</b>
     * Set, the connector exposes that schema and refuses every other name. Unset, it enumerates the
     * catalog's schemas and serves those it can address ({@link #isServableSchemaName}).
     */
    public static final String SCHEMA_VAR = "DATABRICKS_SCHEMA";

    /** Environment variable naming the Secrets Manager secret holding the credential. */
    public static final String CREDENTIAL_SECRET_ARN_VAR = "CREDENTIAL_SECRET_ARN";

    /**
     * A Databricks workspace host on any of the three clouds: {@code <name>.cloud.databricks.com} on
     * AWS, {@code <name>.azuredatabricks.net} on Azure, {@code <name>.gcp.databricks.com} on GCP.
     *
     * <p>The suffixes are enumerated rather than left open because this value reaches a JDBC property
     * list, so an arbitrary host would let a connection be redirected. Anything outside the three is
     * refused.
     */
    private static final Pattern WORKSPACE_HOSTNAME = Pattern.compile(
            "^[a-z0-9][a-z0-9.-]*\\.(cloud\\.databricks\\.com|azuredatabricks\\.net|gcp\\.databricks\\.com)$");

    /** {@code /sql/1.0/warehouses/<id>}, or the older {@code endpoints} spelling. */
    private static final Pattern HTTP_PATH =
            Pattern.compile("^/sql/1\\.0/(warehouses|endpoints)/[a-zA-Z0-9]+$");

    /**
     * A bare SQL identifier. Stricter than Unity Catalog, which allows a quoted name containing almost
     * anything. Athena parses {@code SHOW}/{@code DESCRIBE} and {@code SELECT} with different parsers:
     * the first wants backticks and rejects double quotes, the second the reverse. A name needing
     * quotes is therefore not addressable unquoted on both paths, and refusing it here is the only
     * option that cannot mis-address a table at query time.
     */
    private static final Pattern IDENTIFIER = Pattern.compile("^[a-zA-Z_][a-zA-Z0-9_]*$");

    /**
     * Longest catalog or schema name accepted. Named rather than repeated at each use so the advertising
     * gate and the two setters cannot disagree about it.
     */
    private static final int MAX_IDENTIFIER_LENGTH = 255;

    /**
     * A Secrets Manager secret ARN. Partition and region left open; account is 12 digits. The tail is
     * the character set Secrets Manager permits in a secret name, alphanumerics plus {@code /_+=.@-},
     * rather than {@code .+}, so it bars {@code ;} and every quote character.
     */
    private static final Pattern SECRET_ARN = Pattern.compile(
            "^arn:[a-z0-9-]+:secretsmanager:[a-z0-9-]+:\\d{12}:secret:[A-Za-z0-9/_+=.@-]+$");

    private final String workspaceHostname;
    private final String httpPath;
    private final String catalog;
    private final String schema;
    private final String credentialSecretArn;
    private final ManagedSource managedSource;

    private ConnectionConfig(Builder builder)
    {
        this.workspaceHostname = require(builder.workspaceHostname, WORKSPACE_HOSTNAME_VAR, builder.origin);
        this.httpPath = require(builder.httpPath, HTTP_PATH_VAR, builder.origin);
        this.catalog = require(builder.catalog, CATALOG_VAR, builder.origin);
        // Not require()d: an absent schema means "serve every schema in the catalog". Builder.schema
        // still checks the shape whenever a value is present.
        this.schema = builder.schema;
        this.credentialSecretArn =
                require(builder.credentialSecretArn, CREDENTIAL_SECRET_ARN_VAR, builder.origin);
        // Not require()d either: absent is what a deployment reading its endpoint from the environment
        // looks like, and it has no source, namespace or role to name.
        this.managedSource = builder.managedSource;
    }

    public static Builder builder()
    {
        return new Builder();
    }

    /**
     * Whether {@code name} is a schema name this connector can serve, i.e. whether
     * {@link #withSchema(String)} would accept it.
     *
     * <p>An unpinned connector advertises the schemas {@code information_schema.schemata} reports, and
     * Unity Catalog allows names this connector cannot address: {@code sales-eu} is a legal schema and
     * not a bare identifier. Advertising one and then refusing every request against it is worse than
     * not advertising it, so the enumerating side filters on this and the pinning side validates on the
     * same pattern. Public so the two cannot drift, and so a caller can decide what to advertise without
     * building a configuration and catching.
     *
     * @param name a candidate schema name, as the catalog reports it. Null and blank are not servable.
     */
    public static boolean isServableSchemaName(String name)
    {
        String normalised = Builder.lowerCase(Builder.trimToNull(name));
        return normalised != null
                && normalised.length() <= MAX_IDENTIFIER_LENGTH
                && IDENTIFIER.matcher(normalised).matches();
    }

    /** The workspace host, lower-cased, e.g. {@code dbc-a1b2345c-d6e7.cloud.databricks.com}. */
    public String workspaceHostname()
    {
        return workspaceHostname;
    }

    /** The SQL Warehouse HTTP path, e.g. {@code /sql/1.0/warehouses/a1b234c567d8e9fa}. */
    public String httpPath()
    {
        return httpPath;
    }

    /** The Unity Catalog catalog, lower-cased. */
    public String catalog()
    {
        return catalog;
    }

    /**
     * The schema this connector is pinned to, lower-cased, or {@code null} when it is unpinned and
     * serves every schema in {@link #catalog()}. Check {@link #isSchemaPinned()} before dereferencing.
     */
    public String schema()
    {
        return schema;
    }

    /**
     * Whether this connector is pinned to one Unity Catalog schema. A pinned connector refuses a
     * request naming any other schema even if the credential could read it, so the pin is a
     * containment boundary on top of whatever Unity Catalog grants the credential carries. Unpinned,
     * those grants are the only boundary.
     */
    public boolean isSchemaPinned()
    {
        return schema != null;
    }

    /**
     * A pinned copy of this configuration, for serving one request against one schema. The metadata
     * handler calls this once it has resolved which schema a request names. Going through the builder
     * rather than assigning the field is what applies the identifier pattern to a value that arrived
     * from a request rather than from the environment.
     *
     * @throws IllegalArgumentException if {@code value} is blank or not a bare identifier. The message
     *                                 describes the request, and deliberately does not name
     *                                 {@link #SCHEMA_VAR}: this method is reached on the request path of
     *                                 an <b>unpinned</b> connector, where that variable is unset and
     *                                 pointing an operator at it sends them to change something they
     *                                 never set.
     */
    public ConnectionConfig withSchema(String value)
    {
        // Guarded here rather than in the builder. Builder.schema treats a blank as "unset", which is
        // right for an optional environment variable and wrong here: it would hand an UNPINNED config
        // back to a caller that asked to pin one.
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException("Cannot pin to a null or blank schema");
        }
        if (!isServableSchemaName(value)) {
            // Reached before the builder so the message is about the request rather than about the
            // environment. The builder below still applies the same pattern, as a backstop.
            throw new IllegalArgumentException(
                    "Schema \"" + value + "\" in the request is not a schema this connector can serve. "
                            + hintFor(SCHEMA_VAR) + " Athena parses SHOW/DESCRIBE and SELECT with"
                            + " different quoting rules, so a name that needs quotes cannot be addressed"
                            + " on both paths; a schema whose name is not a bare identifier is therefore"
                            + " not advertised either.");
        }
        return builder()
                .origin("the request")
                .workspaceHostname(workspaceHostname)
                .httpPath(httpPath)
                .catalog(catalog)
                .schema(value)
                .credentialSecretArn(credentialSecretArn)
                // Carried through, or the pinned copy would lose the role the credential is behind and
                // the catalog name the cache re-checks against.
                .managedSource(managedSource)
                .build();
    }

    /** The ARN of the secret holding the credential. Never the credential itself. */
    public String credentialSecretArn()
    {
        return credentialSecretArn;
    }

    /**
     * The COA source this configuration belongs to and the role that guards its credential, or
     * {@code null} in {@code environment} mode where none of that exists. Check
     * {@link #isCoaManaged()} before dereferencing.
     */
    public ManagedSource managedSource()
    {
        return managedSource;
    }

    /**
     * Whether this configuration came from a COA-managed source parameter rather than from the
     * connector's own environment. In that mode the credential is reached by assuming a customer-owned
     * role ({@link AssumedRoleCredentialSource}) and the connector holds no Secrets Manager permission
     * of its own.
     */
    public boolean isCoaManaged()
    {
        return managedSource != null;
    }

    /**
     * The COA source id, or {@code null} in {@code environment} mode. A convenience for a log line,
     * which should not have to know whether a mode has a managed block.
     */
    public String sourceId()
    {
        return (managedSource == null) ? null : managedSource.sourceId();
    }

    /**
     * The host, catalog and schema, and nothing else. Safe to log: it omits the secret ARN and the
     * HTTP path, which identifies a specific warehouse.
     */
    @Override
    public String toString()
    {
        return "ConnectionConfig{workspaceHostname=" + workspaceHostname
                + ", catalog=" + catalog
                + ", schema=" + (isSchemaPinned() ? schema : "<every schema in the catalog>")
                + (isCoaManaged() ? ", " + managedSource : "") + "}";
    }

    @Override
    public boolean equals(Object other)
    {
        if (this == other) {
            return true;
        }
        if (!(other instanceof ConnectionConfig)) {
            return false;
        }
        ConnectionConfig that = (ConnectionConfig) other;
        return workspaceHostname.equals(that.workspaceHostname)
                && httpPath.equals(that.httpPath)
                && catalog.equals(that.catalog)
                // Objects.equals, not schema.equals: schema is null on an unpinned config.
                && Objects.equals(schema, that.schema)
                && credentialSecretArn.equals(that.credentialSecretArn)
                // Part of identity: two catalogs that happen to name the same warehouse and secret still
                // belong to different sources.
                && Objects.equals(managedSource, that.managedSource);
    }

    @Override
    public int hashCode()
    {
        return Objects.hash(workspaceHostname, httpPath, catalog, schema, credentialSecretArn,
                managedSource);
    }

    private static String require(String value, String label, String origin)
    {
        if (value == null) {
            throw new IllegalArgumentException(
                    label + " is not set" + in(origin) + ". " + hintFor(label));
        }
        return value;
    }

    private static String in(String origin)
    {
        return (origin == null || origin.isEmpty()) ? "" : " in " + origin;
    }

    private static String hintFor(String label)
    {
        if (HTTP_PATH_VAR.equals(label)) {
            return "Expected a SQL Warehouse HTTP path, e.g. /sql/1.0/warehouses/a1b234c567d8e9fa"
                    + " — copy it from the warehouse's Connection details tab.";
        }
        if (WORKSPACE_HOSTNAME_VAR.equals(label)) {
            return "Expected a workspace host ending .cloud.databricks.com (AWS),"
                    + " .azuredatabricks.net (Azure) or .gcp.databricks.com (GCP),"
                    + " e.g. dbc-a1b2345c-d6e7.cloud.databricks.com — no scheme and no port.";
        }
        if (CREDENTIAL_SECRET_ARN_VAR.equals(label)) {
            return "Expected a Secrets Manager secret ARN holding {\"token\": ...} for a personal"
                    + " access token, or {\"client_id\": ..., \"client_secret\": ...} for OAuth M2M.";
        }
        return "Expected a bare SQL identifier: a letter or underscore, then letters, digits or"
                + " underscores.";
    }

    /**
     * Fluent, validating builder. Not thread-safe; build one per configuration. Every setter throws
     * {@link IllegalArgumentException} naming the environment variable and the origin, so the message
     * is actionable whether the value came from the environment or from a parameter store.
     */
    public static final class Builder
    {
        private String origin = "the environment";
        private String workspaceHostname;
        private String httpPath;
        private String catalog;
        private String schema;
        private String credentialSecretArn;
        private ManagedSource managedSource;

        private Builder()
        {
        }

        /**
         * Where these values were read from, for error messages: {@code "the environment"},
         * {@code "SSM parameter /coa/dev/connectors/databricks/x"}, and so on.
         *
         * @param where a short human phrase. Null or blank omits it.
         */
        public Builder origin(String where)
        {
            this.origin = (where == null) ? "" : where.trim();
            return this;
        }

        /**
         * @param value the workspace host. Lower-cased before validation.
         * @throws IllegalArgumentException if blank or not a Databricks workspace host.
         */
        public Builder workspaceHostname(String value)
        {
            String normalised = lowerCase(trimToNull(value));
            this.workspaceHostname =
                    check(normalised, WORKSPACE_HOSTNAME, WORKSPACE_HOSTNAME_VAR, 512);
            return this;
        }

        /**
         * @param value the SQL Warehouse HTTP path, case preserved: a warehouse id is case-sensitive.
         * @throws IllegalArgumentException if blank or not a SQL Warehouse HTTP path.
         */
        public Builder httpPath(String value)
        {
            this.httpPath = check(trimToNull(value), HTTP_PATH, HTTP_PATH_VAR, 256);
            return this;
        }

        /**
         * @param value the Unity Catalog catalog. Lower-cased before validation.
         * @throws IllegalArgumentException if blank or not a bare identifier.
         */
        public Builder catalog(String value)
        {
            this.catalog =
                    check(lowerCase(trimToNull(value)), IDENTIFIER, CATALOG_VAR, MAX_IDENTIFIER_LENGTH);
            return this;
        }

        /**
         * The Unity Catalog schema to pin to. Optional: null or blank leaves the connector unpinned,
         * serving every schema in the catalog. Unlike every other setter this one accepts a blank,
         * because CDK, the console and a shell disagree about whether an unset variable arrives absent
         * or empty, and an operator means the same thing by both. A value that is present is held to
         * the identifier pattern, since it is still the Athena schema name.
         *
         * @throws IllegalArgumentException if present but not a bare identifier.
         */
        public Builder schema(String value)
        {
            String normalised = lowerCase(trimToNull(value));
            this.schema = (normalised == null)
                    ? null
                    : check(normalised, IDENTIFIER, SCHEMA_VAR, MAX_IDENTIFIER_LENGTH);
            return this;
        }

        /** @throws IllegalArgumentException if blank or not a Secrets Manager secret ARN. */
        public Builder credentialSecretArn(String value)
        {
            this.credentialSecretArn =
                    check(trimToNull(value), SECRET_ARN, CREDENTIAL_SECRET_ARN_VAR, 2048);
            return this;
        }

        /**
         * The COA source this configuration belongs to. <b>Optional:</b> null is what
         * {@code environment} mode looks like. Already validated by {@link ManagedSource}'s own builder.
         */
        public Builder managedSource(ManagedSource value)
        {
            this.managedSource = value;
            return this;
        }

        /** @throws IllegalArgumentException naming the first field that was never set. */
        public ConnectionConfig build()
        {
            return new ConnectionConfig(this);
        }

        private String check(String value, Pattern pattern, String label, int maxLength)
        {
            if (value == null) {
                throw new IllegalArgumentException(
                        label + " is empty" + in(origin) + ". " + hintFor(label));
            }
            if (value.length() > maxLength) {
                throw new IllegalArgumentException(
                        label + " is " + value.length() + " characters" + in(origin)
                                + "; the maximum is " + maxLength + ".");
            }
            if (!pattern.matcher(value).matches()) {
                // Safe to echo the value: every field here is a coordinate, not a credential, and a
                // rejected value the operator cannot see is a support ticket.
                throw new IllegalArgumentException(
                        label + "=\"" + value + "\"" + in(origin) + " is not valid. " + hintFor(label));
            }
            return value;
        }

        private static String trimToNull(String value)
        {
            if (value == null) {
                return null;
            }
            String trimmed = value.trim();
            return trimmed.isEmpty() ? null : trimmed;
        }

        private static String lowerCase(String value)
        {
            return (value == null) ? null : value.toLowerCase(Locale.ROOT);
        }
    }
}
