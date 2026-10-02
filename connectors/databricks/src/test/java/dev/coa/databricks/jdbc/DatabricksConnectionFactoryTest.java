// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.jdbc;

import com.amazonaws.athena.connector.lambda.exceptions.AthenaConnectorException;
import dev.coa.databricks.config.ConnectionConfig;
import dev.coa.databricks.config.CredentialSource;
import org.junit.jupiter.api.Test;

import java.util.Properties;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The JDBC URL and the connection properties: the injection boundary, and four driver defaults that are
 * wrong for a Lambda. The factory's constructor touches nothing and {@code properties()} is
 * package-private, so this can be asserted without a warehouse. The two {@code open()} cases fail while
 * resolving the credential, which is before the driver is loaded.
 */
class DatabricksConnectionFactoryTest
{
    private static final String HOST = "dbc-a1b2345c-d6e7.cloud.databricks.com";
    private static final String HTTP_PATH = "/sql/1.0/warehouses/a1b234c567d8e9fa";

    /**
     * The invocation timeout the CDK app deploys, restated because the jar cannot read it. If
     * {@code TIMEOUT_SECONDS} in the app moves down, this test is what catches the driver timeouts left
     * above it, where they can never fire.
     */
    private static final int INVOCATION_TIMEOUT_SECONDS = 600;

    private static ConnectionConfig config()
    {
        return ConnectionConfig.builder()
                .workspaceHostname(HOST)
                .httpPath(HTTP_PATH)
                .catalog("main")
                .schema("sales")
                .credentialSecretArn(
                        "arn:aws:secretsmanager:eu-central-1:111122223333:secret:dbx-AbCdEf")
                .build();
    }

    private static DatabricksConnectionFactory factory(String secretJson)
    {
        return new DatabricksConnectionFactory(config(), new CredentialSource(config -> secretJson));
    }

    @Test
    void theUrlCarriesTheHostAndNothingElse()
    {
        // The injection defence. HTTP path, catalog, schema and credential all go in Properties, because
        // the URL is a ";"-delimited property list and anything concatenated into it can be split by a ";".
        assertEquals("jdbc:databricks://" + HOST + ":443", factory("{\"token\":\"t\"}").url());
    }

    @Test
    void theUrlNeverCarriesTheCredentialOrTheHttpPath()
    {
        String url = factory("{\"token\":\"dapi-SUPERSECRET\"}").url();
        assertFalse(url.contains("SUPERSECRET"), url);
        assertFalse(url.contains("PWD"), url);
        assertFalse(url.contains(HTTP_PATH), url);
        assertFalse(url.contains(";"), url);
    }

    @Test
    void theHttpPathAndNamespaceGoInProperties()
    {
        Properties properties = factory("{\"token\":\"t\"}").properties();
        assertEquals(HTTP_PATH, properties.getProperty("httpPath"));
        assertEquals("main", properties.getProperty("ConnCatalog"));
        assertEquals("sales", properties.getProperty("ConnSchema"));
    }

    @Test
    void anUnpinnedConfigOmitsConnSchemaRatherThanThrowing()
    {
        // Properties.setProperty(key, null) throws NullPointerException, which
        // DatabricksConnectionFactory.open catches as a RuntimeException and reports as
        // CONNECTOR_CREDENTIAL_UNREADABLE, naming Secrets Manager, IAM and KMS for a fault involving none
        // of them. That is every metadata call on an unpinned connector.
        ConnectionConfig unpinned = ConnectionConfig.builder()
                .workspaceHostname(HOST)
                .httpPath(HTTP_PATH)
                .catalog("main")
                .credentialSecretArn(
                        "arn:aws:secretsmanager:eu-central-1:111122223333:secret:dbx-AbCdEf")
                .build();

        Properties properties = new DatabricksConnectionFactory(
                unpinned, new CredentialSource(config -> "{\"token\":\"t\"}")).properties();

        assertEquals("main", properties.getProperty("ConnCatalog"),
                "the catalog is still pinned — it cannot travel in a request");
        assertNull(properties.getProperty("ConnSchema"),
                "an unpinned connector has no schema to pin on the session");
    }

    @Test
    void theCredentialGoesInProperties()
    {
        Properties properties = factory("{\"token\":\"dapi-example\"}").properties();
        assertEquals("3", properties.getProperty("AuthMech"));
        assertEquals("token", properties.getProperty("UID"));
        assertEquals("dapi-example", properties.getProperty("PWD"));
    }

    @Test
    void theDriverIsToldNotToRetryAStoppedWarehouse()
    {
        // The default retries a temporarily-unavailable warehouse for up to 900 seconds, outliving the
        // connector's 120-second timeout, so the query dies with a timeout instead of a diagnosis after two
        // billed minutes.
        assertEquals("0", factory("{\"token\":\"t\"}").properties()
                .getProperty("TemporarilyUnavailableRetry"));
    }

    @Test
    void timeoutsSitBelowTheInvocationTimeout()
    {
        Properties properties = factory("{\"token\":\"t\"}").properties();
        assertTrue(Integer.parseInt(properties.getProperty("socketTimeout")) < INVOCATION_TIMEOUT_SECONDS,
                "socketTimeout must be below the invocation timeout, or it can never fire");
        assertTrue(Integer.parseInt(properties.getProperty("RateLimitRetryTimeout"))
                        < INVOCATION_TIMEOUT_SECONDS,
                "RateLimitRetryTimeout must be below the invocation timeout");
    }

    @Test
    void driverLoggingAndTelemetryAreOff()
    {
        Properties properties = factory("{\"token\":\"t\"}").properties();
        // LogLevel is the only control that reaches the driver: it bundles its own relocated SLF4J bound to
        // a java.util.logging provider, so simplelogger.properties cannot quiet it. Driver logging is also
        // how a connection string containing PWD= reaches disk.
        assertEquals("0", properties.getProperty("LogLevel"));
        assertEquals("0", properties.getProperty("EnableTelemetry"));
        assertEquals("1", properties.getProperty("SSL"));
    }

    @Test
    void transactionCallsAreNoOpsSoTheInheritedReadLoopCostsNoRoundTrips()
    {
        // The inherited loop calls setAutoCommit(false) and commit(). Without this flag the driver executes
        // a statement on the warehouse for each: two extra round trips per read, against a warehouse whose
        // transaction support depends on its runtime version.
        assertEquals("1", factory("{\"token\":\"t\"}").properties()
                .getProperty("IgnoreTransactions"));
    }

    @Test
    void complexTypesArriveAsStringsToMatchTheTypeMapping()
    {
        // BlockUtils has no case for a struct, list or map vector, so a complex column served as anything
        // but VARCHAR fails at read time with "Unknown type Struct".
        assertEquals("0", factory("{\"token\":\"t\"}").properties()
                .getProperty("EnableComplexDatatypeSupport"));
    }

    @Test
    void aFreshPropertiesObjectIsReturnedEachTime()
    {
        // Nothing should hold a Properties object carrying a credential for longer than a connect.
        DatabricksConnectionFactory factory = factory("{\"token\":\"t\"}");
        assertFalse(factory.properties() == factory.properties());
    }

    @Test
    void aCredentialFailureTheReaderAlreadyClassifiedTravelsOutUnchanged()
    {
        // In coa-managed mode AssumedRoleCredentialSource has already said which of the assume and the read
        // failed and whose policy to look at. Re-wrapping it here would relabel an assume denial as
        // CONNECTOR_CREDENTIAL_UNREADABLE and send the operator to the connector's own role, which in that
        // mode holds no Secrets Manager grant to widen.
        AthenaConnectorException classified = DatabricksErrors.credentialAssumeDenied(
                "arn:aws:iam::222233334444:role/coa-dev-datasource-access-sales", "coa-dev-ns-1",
                new IllegalStateException("AccessDenied"));
        DatabricksConnectionFactory factory = new DatabricksConnectionFactory(
                config(), new CredentialSource(config -> {
                    throw classified;
                }));

        assertSame(classified, assertThrows(AthenaConnectorException.class, factory::open));
    }

    @Test
    void anUnclassifiedSecretsManagerFailureStillNamesTheSecretAndTheIamToCheck()
    {
        // The environment-mode arm: the raw SDK exception reaches here, and a missing
        // secretsmanager:GetSecretValue or kms:Decrypt is what a first deployment hits.
        DatabricksConnectionFactory factory = new DatabricksConnectionFactory(
                config(), new CredentialSource(config -> {
                    throw new IllegalStateException("AccessDeniedException from Secrets Manager");
                }));

        AthenaConnectorException failure =
                assertThrows(AthenaConnectorException.class, factory::open);

        assertTrue(failure.getMessage().startsWith(DatabricksErrors.CREDENTIAL_UNREADABLE_PREFIX),
                failure.getMessage());
        assertTrue(failure.getMessage().contains("dbx-AbCdEf"), failure.getMessage());
        assertTrue(failure.getMessage().contains("kms:Decrypt"), failure.getMessage());
    }
}
