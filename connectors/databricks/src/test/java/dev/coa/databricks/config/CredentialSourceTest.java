// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;

class CredentialSourceTest
{
    private static final String ARN =
            "arn:aws:secretsmanager:eu-central-1:111122223333:secret:dbx-AbCdEf";
    private static final String OTHER_ARN =
            "arn:aws:secretsmanager:eu-central-1:111122223333:secret:dbx-Other0";
    private static final String ROLE_A =
            "arn:aws:iam::222233334444:role/coa-dev-datasource-access-a";
    private static final String ROLE_B =
            "arn:aws:iam::222233334444:role/coa-dev-datasource-access-b";

    private static ConnectionConfig configFor(String secretArn)
    {
        return builderFor(secretArn).build();
    }

    private static ConnectionConfig.Builder builderFor(String secretArn)
    {
        return ConnectionConfig.builder()
                .workspaceHostname("dbc-a1b2345c-d6e7.cloud.databricks.com")
                .httpPath("/sql/1.0/warehouses/a1b234c567d8e9fa")
                .catalog("main")
                .schema("sales")
                .credentialSecretArn(secretArn);
    }

    private static ConnectionConfig managed(String athenaCatalog, String roleArn, String secretArn)
    {
        return builderFor(secretArn)
                .managedSource(ManagedSource.builder()
                        .athenaCatalogName(athenaCatalog)
                        .sourceId("src-" + athenaCatalog)
                        .namespaceId("ns-1")
                        .crossAccountRoleArn(roleArn)
                        .build())
                .build();
    }

    @Test
    void readsTheSecretOnceAndThenServesTheCachedCredential()
    {
        AtomicInteger reads = new AtomicInteger();
        CredentialSource source = new CredentialSource(config -> {
            reads.incrementAndGet();
            return "{\"token\": \"dapi-example\"}";
        });
        ConnectionConfig config = configFor(ARN);

        DatabricksCredential first = source.credentialFor(config);
        assertSame(first, source.credentialFor(config));
        assertEquals(1, reads.get());
    }

    @Test
    void readsAgainWhenTheSecretArnChanges()
    {
        AtomicInteger reads = new AtomicInteger();
        CredentialSource source = new CredentialSource(config -> {
            reads.incrementAndGet();
            return "{\"token\": \"dapi-" + config.credentialSecretArn() + "\"}";
        });

        source.credentialFor(configFor(ARN));
        source.credentialFor(configFor(OTHER_ARN));
        assertEquals(2, reads.get());
    }

    @Test
    void twoCatalogsAlternatingDoNotEvictEachOther()
    {
        // A, B, A, B through a single-slot cache is four reads, so it stops being a cache exactly when the
        // discovery fan-out that justifies it is largest.
        AtomicInteger reads = new AtomicInteger();
        CredentialSource source = new CredentialSource(config -> {
            reads.incrementAndGet();
            return "{\"token\": \"dapi-example\"}";
        });
        ConnectionConfig a = managed("warehouse_a", ROLE_A, ARN);
        ConnectionConfig b = managed("warehouse_b", ROLE_B, OTHER_ARN);

        source.credentialFor(a);
        source.credentialFor(b);
        source.credentialFor(a);
        source.credentialFor(b);

        assertEquals(2, reads.get(), "one read per source, not one per request");
    }

    @Test
    void theSameSecretThroughTwoRolesIsTwoReads()
    {
        // Keyed on the role-and-secret pair: keyed on the secret alone, a hit would serve a credential
        // obtained through one customer's role to a request that should have used another's.
        List<String> rolesRead = new ArrayList<>();
        CredentialSource source = new CredentialSource(config -> {
            rolesRead.add(config.managedSource().crossAccountRoleArn());
            return "{\"token\": \"dapi-example\"}";
        });

        source.credentialFor(managed("warehouse_a", ROLE_A, ARN));
        source.credentialFor(managed("warehouse_b", ROLE_B, ARN));

        assertEquals(2, rolesRead.size(), "expected one read per role, got " + rolesRead);
    }

    @Test
    void readsEveryTimeWhenTheTtlIsZero()
    {
        AtomicInteger reads = new AtomicInteger();
        CredentialSource source = new CredentialSource(config -> {
            reads.incrementAndGet();
            return "{\"token\": \"dapi-example\"}";
        }, 0L);
        ConnectionConfig config = configFor(ARN);

        source.credentialFor(config);
        source.credentialFor(config);
        assertEquals(2, reads.get());
    }

    @Test
    void propagatesAnUnusableSecretShape()
    {
        CredentialSource source =
                new CredentialSource(config -> "{\"username\": \"u\", \"password\": \"p\"}");
        assertThrows(IllegalArgumentException.class, () -> source.credentialFor(configFor(ARN)));
    }

    @Test
    void refusesANullReaderOrNegativeTtl()
    {
        assertThrows(NullPointerException.class, () -> new CredentialSource(null));
        assertThrows(IllegalArgumentException.class,
                () -> new CredentialSource(config -> "{}", -1L));
    }
}
