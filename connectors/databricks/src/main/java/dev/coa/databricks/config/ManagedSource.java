// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import java.util.Objects;
import java.util.regex.Pattern;

/**
 * The identity and credential custody of one COA-managed source: which Athena catalog it was resolved
 * for, which COA source and namespace it belongs to, and the customer-owned role that guards its
 * credential. Separate from {@link ConnectionConfig} because these facts exist only in
 * {@code coa-managed} mode.
 *
 * <p>Two fields are enforcement rather than diagnostics: {@link #athenaCatalogName()}, which the
 * configuration cache re-checks on every use, and {@link #namespaceId()}, the sole input to the
 * {@code sts:ExternalId} ({@link AssumedRoleCredentialSource#externalIdFor}). Why each matters:
 * {@code connectors/databricks/DESIGN.md}, "The cache is the one place a defect returns the wrong
 * tenant's rows" and "The credential path holds no durable grant".
 *
 * <p>Immutable, validated on construction. Every field is patterned because these values arrive from a
 * store that can be written to; the character sets are subsets of what {@code sts:ExternalId} and
 * {@code RoleSessionName} accept, so a value that passes here cannot make the assume malformed either.
 */
public final class ManagedSource
{
    /**
     * An Athena data catalog name. COA derives it as {@code <prefix>ds_<sha256 digest>}. Conservative on
     * purpose: the same string becomes the last segment of an SSM parameter path, so a name containing
     * {@code /} or {@code .} is a path-traversal question. Athena's own catalog names are letters, digits
     * and underscores, so nothing legal is refused.
     */
    private static final Pattern ATHENA_CATALOG_NAME = Pattern.compile("^[A-Za-z0-9_]{1,127}$");

    /**
     * A COA source or namespace id. Wide enough for a UUID and the {@code ns-} and {@code src-} forms;
     * narrow enough to be a subset of the {@code [\w+=,.@-]} character set STS allows in a
     * {@code RoleSessionName}, so an id can never be the reason an assume is rejected as malformed.
     */
    private static final Pattern COA_ID = Pattern.compile("^[A-Za-z0-9_.@=+-]{1,128}$");

    /**
     * An IAM role ARN. The name-and-path tail is left open because a role may sit under a path.
     * <b>No account check</b>, deliberately: COA is deployed into the customer's own account, so the role
     * may legitimately live there, and what bounds it is the reserved-name-prefix scope on the connector's
     * own {@code sts:AssumeRole} grant plus the target role's trust policy.
     */
    private static final Pattern IAM_ROLE_ARN =
            Pattern.compile("^arn:[a-z0-9-]+:iam::\\d{12}:role/[A-Za-z0-9+=,.@_/-]+$");

    private final String athenaCatalogName;
    private final String sourceId;
    private final String namespaceId;
    private final String crossAccountRoleArn;

    private ManagedSource(Builder builder)
    {
        this.athenaCatalogName = require(builder.athenaCatalogName, "athenaCatalogName", builder.origin);
        this.sourceId = require(builder.sourceId, "sourceId", builder.origin);
        this.namespaceId = require(builder.namespaceId, "namespaceId", builder.origin);
        this.crossAccountRoleArn =
                require(builder.crossAccountRoleArn, "crossAccountRoleArn", builder.origin);
    }

    public static Builder builder()
    {
        return new Builder();
    }

    /**
     * Whether {@code name} is shaped like an Athena data catalog name, i.e. whether
     * {@link Builder#athenaCatalogName(String)} would accept it.
     *
     * <p>Public because the configuration provider refuses a name that fails it before building a
     * parameter path from it, and {@link MeteredConnectionConfigProvider} declines to make it a CloudWatch
     * dimension: a name from a hand-built request is otherwise unbounded, and an unbounded dimension is
     * unbounded custom metrics.
     *
     * @param name a candidate catalog name. Null and blank are not catalog names.
     */
    public static boolean isAthenaCatalogName(String name)
    {
        return name != null && ATHENA_CATALOG_NAME.matcher(name).matches();
    }

    /** The Athena catalog name this configuration was resolved for. Never null. */
    public String athenaCatalogName()
    {
        return athenaCatalogName;
    }

    /** The COA source id, for attribution: it appears in the role session name and the cold-start log. */
    public String sourceId()
    {
        return sourceId;
    }

    /** The namespace that owns this source. The only input to the ExternalId beyond the deployment's prefix. */
    public String namespaceId()
    {
        return namespaceId;
    }

    /** The customer-owned role the connector assumes to read the credential secret. */
    public String crossAccountRoleArn()
    {
        return crossAccountRoleArn;
    }

    /** Coordinates and ids only. No credential, and no role ARN — that one names a customer resource. */
    @Override
    public String toString()
    {
        return "ManagedSource{athenaCatalog=" + athenaCatalogName
                + ", sourceId=" + sourceId
                + ", namespaceId=" + namespaceId + "}";
    }

    @Override
    public boolean equals(Object other)
    {
        if (this == other) {
            return true;
        }
        if (!(other instanceof ManagedSource)) {
            return false;
        }
        ManagedSource that = (ManagedSource) other;
        return athenaCatalogName.equals(that.athenaCatalogName)
                && sourceId.equals(that.sourceId)
                && namespaceId.equals(that.namespaceId)
                && crossAccountRoleArn.equals(that.crossAccountRoleArn);
    }

    @Override
    public int hashCode()
    {
        return Objects.hash(athenaCatalogName, sourceId, namespaceId, crossAccountRoleArn);
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
        if ("crossAccountRoleArn".equals(label)) {
            return "Expected an IAM role ARN, e.g."
                    + " arn:aws:iam::222233334444:role/coa-dev-datasource-access-sales. The connector"
                    + " holds no Secrets Manager or KMS permission of its own, so there is no"
                    + " direct-read fallback when this is wrong.";
        }
        if ("athenaCatalogName".equals(label)) {
            return "Expected an Athena data catalog name: letters, digits and underscores.";
        }
        return "Expected a COA id: letters, digits and any of _.@=+-";
    }

    /**
     * Fluent, validating builder. Each setter throws {@link IllegalArgumentException} naming the field
     * and the origin, so a bad parameter says which parameter it was.
     */
    public static final class Builder
    {
        private String origin = "";
        private String athenaCatalogName;
        private String sourceId;
        private String namespaceId;
        private String crossAccountRoleArn;

        private Builder()
        {
        }

        /**
         * Where these values came from, for error messages: normally
         * {@code "SSM parameter /coa/dev/connectors/databricks/sources/<catalog>"}.
         */
        public Builder origin(String where)
        {
            this.origin = (where == null) ? "" : where.trim();
            return this;
        }

        /** @throws IllegalArgumentException if blank or not an Athena catalog name. */
        public Builder athenaCatalogName(String value)
        {
            this.athenaCatalogName =
                    check(value, ATHENA_CATALOG_NAME, "athenaCatalogName", 127);
            return this;
        }

        /** @throws IllegalArgumentException if blank or not a COA id. */
        public Builder sourceId(String value)
        {
            this.sourceId = check(value, COA_ID, "sourceId", 128);
            return this;
        }

        /**
         * The namespace that owns the source.
         *
         * <p><b>Whitespace-checked before the shared {@link #check}, unlike every other field here</b>,
         * because this one is <i>concatenated</i> into {@code sts:ExternalId} and Python derives the same
         * string from the same bytes. {@code check} trims, and a trimmed value would satisfy the pattern
         * while no longer matching the published trust-policy value.
         *
         * @throws IllegalArgumentException if blank, whitespace-bearing, or not a COA id.
         */
        public Builder namespaceId(String value)
        {
            AssumedRoleCredentialSource.requireExternalIdOperand(value, "namespaceId", origin);
            this.namespaceId = check(value, COA_ID, "namespaceId", 128);
            return this;
        }

        /** @throws IllegalArgumentException if blank or not an IAM role ARN. */
        public Builder crossAccountRoleArn(String value)
        {
            this.crossAccountRoleArn =
                    check(value, IAM_ROLE_ARN, "crossAccountRoleArn", 2048);
            return this;
        }

        /** @throws IllegalArgumentException naming the first field that was never set. */
        public ManagedSource build()
        {
            return new ManagedSource(this);
        }

        private String check(String raw, Pattern pattern, String label, int maxLength)
        {
            String value = (raw == null) ? null : raw.trim();
            if (value == null || value.isEmpty()) {
                throw new IllegalArgumentException(
                        label + " is empty" + in(origin) + ". " + hintFor(label));
            }
            if (value.length() > maxLength) {
                throw new IllegalArgumentException(
                        label + " is " + value.length() + " characters" + in(origin)
                                + "; the maximum is " + maxLength + ".");
            }
            if (!pattern.matcher(value).matches()) {
                // Safe to echo: every field here is an identifier or an ARN, not a credential.
                throw new IllegalArgumentException(
                        label + "=\"" + value + "\"" + in(origin) + " is not valid. " + hintFor(label));
            }
            return value;
        }
    }
}
