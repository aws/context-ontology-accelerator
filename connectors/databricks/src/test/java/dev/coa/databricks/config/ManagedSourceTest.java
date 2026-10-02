// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The patterns are a security control, not input hygiene: the ids reach an {@code sts:ExternalId} and a
 * {@code RoleSessionName} and the role ARN an {@code sts:AssumeRole}, each checked against a subset of what
 * STS accepts, so a value that passes here cannot be why an assume is rejected as malformed.
 */
class ManagedSourceTest
{
    private static final String ROLE =
            "arn:aws:iam::222233334444:role/coa-dev-datasource-access-sales";

    private static ManagedSource.Builder valid()
    {
        return ManagedSource.builder()
                .origin("SSM parameter /coa/dev/connectors/databricks/sources/coadevds_144a95d")
                .athenaCatalogName("coadevds_144a95d84d98c87d")
                .sourceId("src-abc123")
                .namespaceId("ns-1")
                .crossAccountRoleArn(ROLE);
    }

    @Test
    void carriesTheFourFactsItWasBuiltWith()
    {
        ManagedSource source = valid().build();

        assertEquals("coadevds_144a95d84d98c87d", source.athenaCatalogName());
        assertEquals("src-abc123", source.sourceId());
        assertEquals("ns-1", source.namespaceId());
        assertEquals(ROLE, source.crossAccountRoleArn());
    }

    @Test
    void aUuidShapedIdIsAccepted()
    {
        // Ids are UUIDs in some deployments and prefixed forms in others; the pattern admits both without
        // admitting a separator.
        assertEquals("11111111-2222-3333-4444-555555555555",
                valid().namespaceId("11111111-2222-3333-4444-555555555555").build().namespaceId());
    }

    @Test
    void everyFieldIsRequired()
    {
        assertThrows(IllegalArgumentException.class, () -> ManagedSource.builder()
                .sourceId("src-abc123").namespaceId("ns-1").crossAccountRoleArn(ROLE).build());
        assertThrows(IllegalArgumentException.class, () -> ManagedSource.builder()
                .athenaCatalogName("c").namespaceId("ns-1").crossAccountRoleArn(ROLE).build());
        assertThrows(IllegalArgumentException.class, () -> ManagedSource.builder()
                .athenaCatalogName("c").sourceId("src-abc123").crossAccountRoleArn(ROLE).build());
        assertThrows(IllegalArgumentException.class, () -> ManagedSource.builder()
                .athenaCatalogName("c").sourceId("src-abc123").namespaceId("ns-1").build());
    }

    @Test
    void aRoleArnThatIsNotOneIsRefused()
    {
        for (String arn : new String[] {
            "arn:aws:iam::222233334444:user/someone",
            "arn:aws:sts::222233334444:assumed-role/x/y",
            "arn:aws:iam::22223333:role/short-account",
            "coa-dev-datasource-access-sales",
            "arn:aws:iam::222233334444:role/x;y",
            "arn:aws:iam::222233334444:role/\"quoted\""}) {
            assertThrows(IllegalArgumentException.class,
                    () -> valid().crossAccountRoleArn(arn).build(), arn);
        }
    }

    @Test
    void aRoleUnderAPathIsAcceptedBecauseIamAllowsOne()
    {
        assertEquals("arn:aws:iam::222233334444:role/coa/coa-dev-datasource-access-sales",
                valid().crossAccountRoleArn(
                                "arn:aws:iam::222233334444:role/coa/coa-dev-datasource-access-sales")
                        .build()
                        .crossAccountRoleArn());
    }

    @Test
    void noAccountIsRequiredOfTheRole()
    {
        // Deliberately unconstrained: COA is deployed INTO the customer's account, so excluding the
        // deployment account would refuse the single-account topology. What bounds COA is the reserved
        // role-name prefix its assume grant is scoped to, plus the target role's trust policy.
        assertEquals("arn:aws-cn:iam::999988887777:role/coa-dev-datasource-access-sales",
                valid().crossAccountRoleArn(
                                "arn:aws-cn:iam::999988887777:role/coa-dev-datasource-access-sales")
                        .build()
                        .crossAccountRoleArn());
    }

    @Test
    void anIdCarryingASeparatorOrAQuoteIsRefused()
    {
        for (String id : new String[] {"ns 1", "ns/1", "ns;1", "ns\"1", "ns'1", "ns\n1", ""}) {
            assertThrows(IllegalArgumentException.class, () -> valid().namespaceId(id).build(), id);
            assertThrows(IllegalArgumentException.class, () -> valid().sourceId(id).build(), id);
        }
    }

    @Test
    void aCatalogNameThatCouldEscapeAParameterPathIsRefused()
    {
        for (String name : new String[] {"a/b", "..", "a b", "a-b", "a*"}) {
            assertThrows(IllegalArgumentException.class,
                    () -> valid().athenaCatalogName(name).build(), name);
        }
    }

    @Test
    void aRejectionNamesTheFieldAndWhereTheValueCameFrom()
    {
        // The origin is the parameter's name, the only thing that says which of a deployment's sources is
        // misconfigured.
        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> valid().crossAccountRoleArn("nonsense").build());

        assertTrue(failure.getMessage().contains("crossAccountRoleArn"), failure.getMessage());
        assertTrue(failure.getMessage().contains("SSM parameter"), failure.getMessage());
        assertTrue(failure.getMessage().contains("nonsense"),
                "the value is an ARN rather than a credential, and a rejection the operator cannot see is"
                        + " a support ticket: " + failure.getMessage());
    }

    @Test
    void twoSourcesDifferingOnlyInNamespaceAreNotEqual()
    {
        // Caches key on the configuration this hangs off, and two catalogs naming the same warehouse and
        // secret still belong to different sources.
        assertEquals(valid().build(), valid().build());
        assertNotEquals(valid().build(), valid().namespaceId("ns-2").build());
        assertNotEquals(valid().build(), valid().athenaCatalogName("coadevds_other").build());
    }

    @Test
    void toStringOmitsTheRoleArn()
    {
        // The role names a customer resource and nothing in a log line needs it.
        String rendered = valid().build().toString();

        assertTrue(rendered.contains("ns-1"), rendered);
        assertFalse(rendered.contains(ROLE), rendered);
    }
}
