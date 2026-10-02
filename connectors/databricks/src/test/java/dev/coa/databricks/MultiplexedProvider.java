// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import dev.coa.databricks.config.ConnectionConfig;
import dev.coa.databricks.config.ConnectionConfigProvider;
import dev.coa.databricks.config.ManagedSource;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * A provider mapping Athena catalog names to distinct endpoints, as the parameter-backed one does.
 * {@link #asked} records every name it was asked for, in order.
 */
final class MultiplexedProvider implements ConnectionConfigProvider
{
    private static final String SECRET =
            "arn:aws:secretsmanager:us-east-1:111122223333:secret:dbx-AbCdEf";

    static final String HOST = "dbc-a1b2345c-d6e7.cloud.databricks.com";

    static final String OTHER_HOST = "dbc-9f8e7d6c-5b4a.cloud.databricks.com";

    private final Map<String, ConnectionConfig> byCatalog = new HashMap<>();

    final List<String> asked = new ArrayList<>();

    /** A stage-1-shaped endpoint: no source record, so no role to assume and no namespace. */
    MultiplexedProvider add(String athenaCatalog, String ucCatalog, String schema)
    {
        byCatalog.put(athenaCatalog, base(ucCatalog, schema).build());
        return this;
    }

    /** The same, with {@code DATABRICKS_SCHEMA} unset: the enumerate-then-scope mode. */
    MultiplexedProvider addUnpinned(String athenaCatalog, String ucCatalog)
    {
        return add(athenaCatalog, ucCatalog, null);
    }

    /**
     * A COA-managed source: schema-pinned, and carrying the namespace and the customer-owned role. The
     * ids are derived from the catalog name so a failure message says which source it was.
     */
    MultiplexedProvider addManaged(String athenaCatalog, String ucCatalog, String schema)
    {
        return addManaged(athenaCatalog, ucCatalog, schema, HOST);
    }

    /**
     * A COA-managed source on a named workspace, so a fixture can put two sources on <b>one</b>
     * workspace and a third on another.
     *
     * <p>That shape is exit requirement 5's, and it is the interesting one: two sources sharing a
     * workspace differ only in their schema, namespace, role and secret, so a cache keyed on the
     * warehouse — or on anything else they share — collapses two tenants onto one credential while every
     * two-workspace test still passes.
     */
    MultiplexedProvider addManaged(String athenaCatalog, String ucCatalog, String schema,
                                   String workspaceHostname)
    {
        byCatalog.put(athenaCatalog, base(ucCatalog, schema, workspaceHostname)
                .managedSource(ManagedSource.builder()
                        .athenaCatalogName(athenaCatalog)
                        .sourceId("src-" + athenaCatalog)
                        .namespaceId("ns-" + athenaCatalog)
                        .crossAccountRoleArn("arn:aws:iam::222233334444:role/"
                                + "coa-dev-datasource-access-" + schema)
                        .build())
                .build());
        return this;
    }

    private static ConnectionConfig.Builder base(String ucCatalog, String schema)
    {
        return base(ucCatalog, schema, HOST);
    }

    private static ConnectionConfig.Builder base(String ucCatalog, String schema,
                                                 String workspaceHostname)
    {
        return ConnectionConfig.builder()
                .workspaceHostname(workspaceHostname)
                .httpPath("/sql/1.0/warehouses/a1b234c567d8e9fa")
                .catalog(ucCatalog)
                .schema(schema)
                .credentialSecretArn(SECRET);
    }

    @Override
    public ConnectionConfig configFor(String athenaCatalogName)
    {
        asked.add(athenaCatalogName);
        if (athenaCatalogName == null) {
            // Nothing in the connector passes null any more; both cold-start sites that used to now ask
            // describe() instead. Refused rather than answered, because a multiplexed provider answering
            // it has to pick one tenant's endpoint, and the real parameter-backed provider refuses too.
            throw new IllegalStateException(
                    "asked to resolve configuration before Athena named a catalog");
        }
        ConnectionConfig config = byCatalog.get(athenaCatalogName);
        if (config == null) {
            throw new IllegalArgumentException("no endpoint for catalog " + athenaCatalogName);
        }
        return config;
    }
}
