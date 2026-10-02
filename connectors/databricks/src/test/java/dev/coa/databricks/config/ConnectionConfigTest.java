// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** Every value the connector reads, and every shape it refuses. */
class ConnectionConfigTest
{
    private static final String HOST = "dbc-a1b2345c-d6e7.cloud.databricks.com";
    private static final String HTTP_PATH = "/sql/1.0/warehouses/a1b234c567d8e9fa";
    private static final String SECRET =
            "arn:aws:secretsmanager:eu-central-1:111122223333:secret:dbx-sp-AbCdEf";

    private static ConnectionConfig.Builder valid()
    {
        return ConnectionConfig.builder()
                .workspaceHostname(HOST)
                .httpPath(HTTP_PATH)
                .catalog("main")
                .schema("sales")
                .credentialSecretArn(SECRET);
    }

    @Test
    void acceptsAValidConfiguration()
    {
        ConnectionConfig config = valid().build();
        assertEquals(HOST, config.workspaceHostname());
        assertEquals(HTTP_PATH, config.httpPath());
        assertEquals("main", config.catalog());
        assertEquals("sales", config.schema());
        assertEquals(SECRET, config.credentialSecretArn());
    }

    @Test
    void lowerCasesTheHostnameBecauseDnsIsCaseInsensitive()
    {
        assertEquals(HOST, valid().workspaceHostname("DBC-A1B2345C-D6E7.Cloud.Databricks.COM")
                .build().workspaceHostname());
    }

    @Test
    void lowerCasesTheCatalogAndSchemaBecauseInformationSchemaStoresThemThatWay()
    {
        // CREATE TABLE MixedCaseTable yields table_name "mixedcasetable", and the same folding applies to
        // catalog and schema, so comparing against a lower-case literal is what works. The folded schema is
        // also the Athena schema name this connector advertises.
        ConnectionConfig config = valid().catalog("MainCatalog").schema("Sales").build();
        assertEquals("maincatalog", config.catalog());
        assertEquals("sales", config.schema());
    }

    @Test
    void trimsSurroundingWhitespace()
    {
        assertEquals("sales", valid().schema("  sales \n").build().schema());
    }

    @Test
    void refusesANonDatabricksHostname()
    {
        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> valid().workspaceHostname("evil.example.com"));
        assertTrue(failure.getMessage().contains(ConnectionConfig.WORKSPACE_HOSTNAME_VAR),
                "message must name the variable: " + failure.getMessage());
    }

    @Test
    void acceptsWorkspaceHostnamesOnAllThreeClouds()
    {
        assertEquals("dbc-a1b2345c-d6e7.cloud.databricks.com",
                valid().workspaceHostname("dbc-a1b2345c-d6e7.cloud.databricks.com")
                        .build().workspaceHostname());
        assertEquals("adb-1234567890.7.azuredatabricks.net",
                valid().workspaceHostname("adb-1234567890.7.azuredatabricks.net")
                        .build().workspaceHostname());
        assertEquals("1234567890.7.gcp.databricks.com",
                valid().workspaceHostname("1234567890.7.gcp.databricks.com")
                        .build().workspaceHostname());
    }

    @Test
    void refusesAHostnameOutsideTheThreeKnownSuffixes()
    {
        // The suffixes are enumerated so this value cannot redirect a JDBC connection, so a
        // look-alike host has to be refused however plausible it reads.
        assertThrows(IllegalArgumentException.class,
                () -> valid().workspaceHostname("dbc-a1b2345c-d6e7.databricks.com"));
        assertThrows(IllegalArgumentException.class,
                () -> valid().workspaceHostname("dbc-a1b2345c-d6e7.cloud.databricks.com.evil.test"));
        assertThrows(IllegalArgumentException.class,
                () -> valid().workspaceHostname("adb-1234567890.7.azuredatabricks.net.evil.test"));
    }

    @Test
    void refusesASchemeOrPortInTheHostname()
    {
        assertThrows(IllegalArgumentException.class,
                () -> valid().workspaceHostname("https://" + HOST));
        assertThrows(IllegalArgumentException.class,
                () -> valid().workspaceHostname(HOST + ":443"));
    }

    @Test
    void refusesAnHttpPathThatCouldInjectADriverProperty()
    {
        // A ";" in this value becomes a driver property assignment if it reaches a JDBC URL: SSL=0,
        // ProxyHost, LogPath.
        assertThrows(IllegalArgumentException.class,
                () -> valid().httpPath(HTTP_PATH + ";SSL=0"));
        assertThrows(IllegalArgumentException.class,
                () -> valid().httpPath(HTTP_PATH + ";ProxyHost=attacker.example.com;ProxyPort=8080"));
    }

    @Test
    void refusesAnHttpPathThatIsNotAWarehousePath()
    {
        assertThrows(IllegalArgumentException.class, () -> valid().httpPath("/sql/protocolv1/o/0/1"));
        assertThrows(IllegalArgumentException.class, () -> valid().httpPath("sql/1.0/warehouses/abc"));
        assertThrows(IllegalArgumentException.class, () -> valid().httpPath("/sql/1.0/warehouses/"));
    }

    @Test
    void acceptsTheOlderEndpointsSpelling()
    {
        assertEquals("/sql/1.0/endpoints/a1b234c567d8e9fa",
                valid().httpPath("/sql/1.0/endpoints/a1b234c567d8e9fa").build().httpPath());
    }

    @Test
    void preservesTheHttpPathsCaseBecauseAWarehouseIdIsCaseSensitive()
    {
        assertEquals("/sql/1.0/warehouses/A1B2c3",
                valid().httpPath("/sql/1.0/warehouses/A1B2c3").build().httpPath());
    }

    @Test
    void refusesACatalogOrSchemaThatIsNotABareIdentifier()
    {
        for (String bad : new String[] {"1main", "main-catalog", "main.sub", "main;x", "main=x",
                "main schema", "\"main\"", "main`x"}) {
            assertThrows(IllegalArgumentException.class, () -> valid().catalog(bad),
                    "catalog \"" + bad + "\" should be refused");
            assertThrows(IllegalArgumentException.class, () -> valid().schema(bad),
                    "schema \"" + bad + "\" should be refused");
        }
    }

    @Test
    void refusesASecretArnThatIsNotASecretsManagerSecret()
    {
        assertThrows(IllegalArgumentException.class,
                () -> valid().credentialSecretArn("dbx-sp-creds"));
        assertThrows(IllegalArgumentException.class,
                () -> valid().credentialSecretArn("arn:aws:ssm:eu-central-1:111122223333:parameter/x"));
        assertThrows(IllegalArgumentException.class,
                () -> valid().credentialSecretArn("arn:aws:secretsmanager:eu-central-1:123:secret:x"));
    }

    @Test
    void refusesABlankValueAndNamesTheVariable()
    {
        IllegalArgumentException failure =
                assertThrows(IllegalArgumentException.class, () -> valid().catalog("   "));
        assertTrue(failure.getMessage().contains(ConnectionConfig.CATALOG_VAR),
                failure.getMessage());
    }

    @Test
    void refusesAMissingValueAndNamesTheVariable()
    {
        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> ConnectionConfig.builder()
                        .workspaceHostname(HOST)
                        .httpPath(HTTP_PATH)
                        .schema("sales")
                        .credentialSecretArn(SECRET)
                        .build());
        assertTrue(failure.getMessage().contains(ConnectionConfig.CATALOG_VAR),
                failure.getMessage());
    }

    // ── The schema is the one optional coordinate ────────────────────────────

    @Test
    void anOmittedSchemaLeavesTheConnectorUnpinned()
    {
        ConnectionConfig config = ConnectionConfig.builder()
                .workspaceHostname(HOST)
                .httpPath(HTTP_PATH)
                .catalog("main")
                .credentialSecretArn(SECRET)
                .build();

        assertFalse(config.isSchemaPinned());
        assertNull(config.schema());
    }

    @Test
    void aBlankSchemaMeansUnsetRatherThanInvalid()
    {
        // CDK, the console and a shell disagree about whether an unset variable arrives absent or empty, and
        // an operator means the same thing by both. Every other field still refuses a blank.
        for (String blank : new String[] {"", "   ", "\n"}) {
            ConnectionConfig config = valid().schema(blank).build();
            assertFalse(config.isSchemaPinned(), "schema \"" + blank + "\" should leave it unpinned");
            assertNull(config.schema());
        }
    }

    @Test
    void aPresentSchemaIsStillShapeChecked()
    {
        // Optional must not mean unvalidated: a malformed value has to fail at deploy, because at query time
        // it is indistinguishable from the unpinned mode.
        assertThrows(IllegalArgumentException.class, () -> valid().schema("main-schema"));
        assertThrows(IllegalArgumentException.class, () -> valid().schema("main;x"));
    }

    @Test
    void withSchemaPinsACopyAndLeavesEverythingElseAlone()
    {
        ConnectionConfig unpinned = ConnectionConfig.builder()
                .workspaceHostname(HOST)
                .httpPath(HTTP_PATH)
                .catalog("main")
                .credentialSecretArn(SECRET)
                .build();

        ConnectionConfig pinned = unpinned.withSchema("Other_Schema");

        assertTrue(pinned.isSchemaPinned());
        assertEquals("other_schema", pinned.schema(), "folded like every other identifier");
        assertEquals(unpinned.workspaceHostname(), pinned.workspaceHostname());
        assertEquals(unpinned.httpPath(), pinned.httpPath());
        assertEquals(unpinned.catalog(), pinned.catalog());
        assertEquals(unpinned.credentialSecretArn(), pinned.credentialSecretArn());
        assertFalse(unpinned.isSchemaPinned(), "the original must be untouched");
    }

    @Test
    void withSchemaRefusesABlankRatherThanSilentlyUnpinning()
    {
        // The builder treats a blank as "unset", which is right when reading the environment and wrong here:
        // a caller asking to pin must never receive an unpinned config back.
        assertThrows(IllegalArgumentException.class, () -> valid().build().withSchema("  "));
        assertThrows(IllegalArgumentException.class, () -> valid().build().withSchema(null));
    }

    @Test
    void withSchemaAppliesTheIdentifierPatternToAValueFromARequest()
    {
        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> valid().build().withSchema("made;up"));
        assertTrue(failure.getMessage().contains("made;up"), failure.getMessage());
        assertTrue(failure.getMessage().contains("bare SQL identifier"), failure.getMessage());
    }

    @Test
    void withSchemaBlamesTheRequestRatherThanAnEnvironmentVariableNobodySet()
    {
        // This method is reached only on the request path of an UNPINNED connector, where DATABRICKS_SCHEMA
        // is by definition unset. Naming it sends an operator to change a variable they never touched: the
        // schema arrived in the request, from a name the catalog reported.
        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> valid().build().withSchema("sales-eu"));
        assertFalse(failure.getMessage().contains(ConnectionConfig.SCHEMA_VAR),
                failure.getMessage());
        assertTrue(failure.getMessage().contains("in the request"), failure.getMessage());
    }

    @Test
    void isServableSchemaNameAgreesWithWhatWithSchemaAccepts()
    {
        // The gate exists so an unpinned connector can advertise exactly the schemas it will serve. If the
        // two ever disagree, the connector lists a schema and then refuses every request against it, which
        // is the bug this pair of assertions is here to stop.
        ConnectionConfig unpinned = valid().build();
        for (String name : new String[] {
            "sales", "SALES", "  sales  ", "_private", "s1", "sales_eu",
            "sales-eu", "sales eu", "sales;drop", "sales.eu", "1sales", "", "   ", null,
            "\"quoted\"", "sales`eu", repeat("s", 255), repeat("s", 256)}) {
            boolean servable = ConnectionConfig.isServableSchemaName(name);
            boolean pinnable;
            try {
                unpinned.withSchema(name);
                pinnable = true;
            }
            catch (IllegalArgumentException refused) {
                pinnable = false;
            }
            assertEquals(pinnable, servable,
                    "isServableSchemaName and withSchema disagree about " + name);
        }
    }

    private static String repeat(String unit, int times)
    {
        StringBuilder out = new StringBuilder(unit.length() * times);
        for (int i = 0; i < times; i++) {
            out.append(unit);
        }
        return out.toString();
    }

    @Test
    void toStringSaysUnpinnedRatherThanNull()
    {
        ConnectionConfig unpinned = ConnectionConfig.builder()
                .workspaceHostname(HOST)
                .httpPath(HTTP_PATH)
                .catalog("main")
                .credentialSecretArn(SECRET)
                .build();

        assertTrue(unpinned.toString().contains("every schema"), unpinned.toString());
        assertFalse(unpinned.toString().contains("null"), unpinned.toString());
    }

    @Test
    void twoUnpinnedConfigsAreEqualAndHashAlike()
    {
        // Objects.equals/hash rather than schema.equals: a null schema must not NPE here.
        ConnectionConfig one = ConnectionConfig.builder().workspaceHostname(HOST)
                .httpPath(HTTP_PATH).catalog("main").credentialSecretArn(SECRET).build();
        ConnectionConfig two = ConnectionConfig.builder().workspaceHostname(HOST)
                .httpPath(HTTP_PATH).catalog("main").credentialSecretArn(SECRET).build();

        assertEquals(one, two);
        assertEquals(one.hashCode(), two.hashCode());
        assertNotEquals(one, one.withSchema("sales"));
    }

    @Test
    void namesTheOriginSoAnErrorIsActionableWhereverTheValueCameFrom()
    {
        IllegalArgumentException failure = assertThrows(IllegalArgumentException.class,
                () -> ConnectionConfig.builder()
                        .origin("SSM parameter /coa/dev/connectors/databricks/x")
                        .schema("not a schema"));
        assertTrue(failure.getMessage().contains("SSM parameter /coa/dev/connectors/databricks/x"),
                failure.getMessage());
    }

    @Test
    void toStringOmitsTheSecretArnAndTheHttpPath()
    {
        String rendered = valid().build().toString();
        assertTrue(rendered.contains(HOST), rendered);
        assertTrue(!rendered.contains(SECRET), "toString must not carry the secret ARN: " + rendered);
        assertTrue(!rendered.contains(HTTP_PATH), "toString must not carry the HTTP path: " + rendered);
    }

    private static ManagedSource source(String athenaCatalog, String namespaceId)
    {
        return ManagedSource.builder()
                .athenaCatalogName(athenaCatalog)
                .sourceId("src-abc123")
                .namespaceId(namespaceId)
                .crossAccountRoleArn(
                        "arn:aws:iam::222233334444:role/coa-dev-datasource-access-sales")
                .build();
    }

    @Test
    void anEnvironmentModeConfigurationCarriesNoManagedBlock()
    {
        // Absent rather than empty, which is what lets the credential path be selected on this rather than
        // on a mode flag threaded separately.
        ConnectionConfig config = valid().build();

        assertFalse(config.isCoaManaged());
        assertNull(config.managedSource());
        assertNull(config.sourceId());
    }

    @Test
    void aManagedConfigurationExposesItsSourceAndNamespace()
    {
        ConnectionConfig config = valid().managedSource(source("coadevds_a", "ns-1")).build();

        assertTrue(config.isCoaManaged());
        assertEquals("src-abc123", config.sourceId());
        assertEquals("ns-1", config.managedSource().namespaceId());
    }

    @Test
    void withSchemaCarriesTheManagedBlockThrough()
    {
        // A pinned copy losing it would lose the role the credential is behind and the catalog name the
        // configuration cache re-checks against, the second of which fails open rather than closed.
        ConnectionConfig pinned = valid().managedSource(source("coadevds_a", "ns-1"))
                .build()
                .withSchema("finance");

        assertTrue(pinned.isCoaManaged());
        assertEquals("coadevds_a", pinned.managedSource().athenaCatalogName());
        assertEquals("finance", pinned.schema());
    }

    @Test
    void twoConfigurationsDifferingOnlyInTheirSourceAreNotEqual()
    {
        // The schema-list cache keys on this class, and two catalogs that happen to name the same warehouse
        // and secret still belong to different namespaces.
        ConnectionConfig a = valid().managedSource(source("coadevds_a", "ns-1")).build();
        ConnectionConfig b = valid().managedSource(source("coadevds_b", "ns-2")).build();

        assertNotEquals(a, b);
        assertNotEquals(a, valid().build());
        assertEquals(a, valid().managedSource(source("coadevds_a", "ns-1")).build());
        assertEquals(a.hashCode(),
                valid().managedSource(source("coadevds_a", "ns-1")).build().hashCode());
    }
}
