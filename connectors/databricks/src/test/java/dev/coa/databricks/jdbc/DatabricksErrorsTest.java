// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.jdbc;

import com.amazonaws.athena.connector.lambda.exceptions.AthenaConnectorException;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.services.glue.model.FederationSourceErrorCode;

import java.io.IOException;
import java.sql.SQLException;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** Telling the three failure classes apart, and never letting a credential through. */
class DatabricksErrorsTest
{
    @Test
    void aStoppedWarehouseIsDistinguishable()
    {
        AthenaConnectorException failure = DatabricksErrors.classify("listing tables",
                new SQLException("[Databricks][JDBCDriver](500593) TEMPORARILY_UNAVAILABLE:"
                        + " The warehouse is starting."));
        assertTrue(failure.getMessage().startsWith(DatabricksErrors.WAREHOUSE_STARTING_PREFIX),
                failure.getMessage());
        assertTrue(failure.getMessage().contains("retry"), failure.getMessage());
        assertEquals(FederationSourceErrorCode.OPERATION_TIMEOUT_EXCEPTION.toString(),
                failure.getErrorDetails().errorCode());
    }

    @Test
    void aStoppedWarehouseIsRecognisedThroughACauseChain()
    {
        // The useful text is usually on a cause rather than on the SQLException the JDBC API hands back,
        // which is why the whole chain is inspected.
        SQLException wrapped = new SQLException("Error connecting to the warehouse",
                new IOException("HTTP 503 Service Unavailable"));
        assertTrue(DatabricksErrors.isWarehouseStarting(wrapped));
        assertTrue(DatabricksErrors.classify("connecting", wrapped).getMessage()
                .startsWith(DatabricksErrors.WAREHOUSE_STARTING_PREFIX));
    }

    @Test
    void aRejectedCredentialIsDistinguishable()
    {
        AthenaConnectorException failure = DatabricksErrors.classify("connecting",
                new SQLException("HTTP Response code: 401, Error message: Invalid access token"));
        assertTrue(failure.getMessage().startsWith(DatabricksErrors.AUTHENTICATION_PREFIX),
                failure.getMessage());
        assertEquals(FederationSourceErrorCode.INVALID_CREDENTIALS_EXCEPTION.toString(),
                failure.getErrorDetails().errorCode());
        // The advice has to name the grants: "rejected the credential" alone sends an operator to rotate a
        // secret that was never the problem.
        assertTrue(failure.getMessage().contains("SELECT"), failure.getMessage());
    }

    @Test
    void aQueryIdContainingTheDigits503IsNotAStoppedWarehouse()
    {
        // Databricks embeds hex ids routinely, and a plain substring test for "503" matches them, telling
        // the user to retry a query that can never succeed and inviting an Athena retry that pays for
        // another warehouse resume.
        AthenaConnectorException failure = DatabricksErrors.classify("reading rows from main.s.t",
                new SQLException("[TABLE_OR_VIEW_NOT_FOUND] The table or view `t` cannot be found."
                        + " [queryId: 01f0a503-8b1e-4c2f-9a77-2f4d503e1a40]"));
        assertTrue(failure.getMessage().startsWith(DatabricksErrors.QUERY_FAILED_PREFIX),
                failure.getMessage());
        assertFalse(DatabricksErrors.isWarehouseStarting(
                new SQLException("queryId 01f0a503-0000-0000-0000-000000000401")));
    }

    @Test
    void aStatusCodeInAStatusContextStillClassifies()
    {
        // Anchoring must not stop the real forms working, including the status line, whose "1.1" sits
        // between the keyword and the digits.
        for (String message : new String[] {
            "HTTP Response code: 503, Error message: Service Unavailable",
            "status: 503",
            "HTTP 503 Service Unavailable",
            "HTTP/1.1 503 Service Unavailable",
            "HTTP/2 503",
            "error code 503"}) {
            assertTrue(DatabricksErrors.classify("connecting", new SQLException(message))
                            .getMessage().startsWith(DatabricksErrors.WAREHOUSE_STARTING_PREFIX),
                    message);
        }
        for (String message : new String[] {
            "HTTP Response code: 401, Error message: Invalid credentials",
            "HTTP/1.1 401",
            "status code 403"}) {
            assertTrue(DatabricksErrors.classify("connecting", new SQLException(message))
                            .getMessage().startsWith(DatabricksErrors.AUTHENTICATION_PREFIX),
                    message);
        }
    }

    @Test
    void aRawStatusLineIsRecognisedAsAStoppedWarehouse()
    {
        // The status form alone, through the predicate the retry decision reads.
        assertTrue(DatabricksErrors.isWarehouseStarting(
                new SQLException("HTTP/1.1 503 Service Unavailable")));
        assertFalse(DatabricksErrors.isWarehouseStarting(new SQLException("HTTP/1.1 401")));
    }

    @Test
    void aVersionedStatusLinePrefixDoesNotLetAHexIdThrough()
    {
        // A query id's digits must not read as a status code, whatever precedes them.
        assertFalse(DatabricksErrors.isWarehouseStarting(new SQLException(
                "HTTP/1.1 200 OK [queryId: 01f0a503-8b1e-4c2f-9a77-2f4d503e1a40]")));
        assertTrue(DatabricksErrors.classify("reading rows", new SQLException(
                        "HTTP/1.1 200 OK [queryId: 01f0a503-0000-0000-0000-000000000401]"))
                .getMessage().startsWith(DatabricksErrors.QUERY_FAILED_PREFIX));
    }

    @Test
    void aTableNameContainingTheDigits403IsNotAnAuthFailure()
    {
        AthenaConnectorException failure = DatabricksErrors.classify("reading rows",
                new SQLException("[TABLE_OR_VIEW_NOT_FOUND] `events_403_archive` cannot be found"));
        assertTrue(failure.getMessage().startsWith(DatabricksErrors.QUERY_FAILED_PREFIX),
                failure.getMessage());
    }

    @Test
    void anAwsCredentialLoadFailureIsNotMistakenForADatabricksAuthFailure()
    {
        AthenaConnectorException failure = DatabricksErrors.classify("connecting",
                new SQLException("Unable to load AWS credentials from any provider in the chain"));
        assertTrue(failure.getMessage().startsWith(DatabricksErrors.QUERY_FAILED_PREFIX),
                failure.getMessage());
    }

    @Test
    void anythingElseFallsThroughToARequestFailure()
    {
        AthenaConnectorException failure = DatabricksErrors.classify("reading rows from main.s.t",
                new SQLException("[TABLE_OR_VIEW_NOT_FOUND] The table or view `t` cannot be found"));
        assertTrue(failure.getMessage().startsWith(DatabricksErrors.QUERY_FAILED_PREFIX),
                failure.getMessage());
        assertTrue(failure.getMessage().contains("reading rows from main.s.t"), failure.getMessage());
        assertTrue(failure.getMessage().contains("TABLE_OR_VIEW_NOT_FOUND"), failure.getMessage());
    }

    @Test
    void redactsEveryCredentialBearingProperty()
    {
        String message = DatabricksErrors.redact(
                "jdbc:databricks://host:443;httpPath=/sql/1.0/warehouses/x;UID=token;"
                        + "PWD=dapi-SUPERSECRET;OAuth2Secret=dose-SUPERSECRET;SSL=1");
        assertFalse(message.contains("SUPERSECRET"), message);
        assertTrue(message.contains("PWD=<redacted>"), message);
        assertTrue(message.contains("OAuth2Secret=<redacted>"), message);
        // Everything else survives, or the redaction destroys the diagnosis it was protecting.
        assertTrue(message.contains("httpPath=/sql/1.0/warehouses/x"), message);
        assertTrue(message.contains("SSL=1"), message);
    }

    @Test
    void redactionIsCaseInsensitiveAndToleratesSpacing()
    {
        assertTrue(DatabricksErrors.redact("pwd = dapi-x").contains("<redacted>"));
        assertTrue(DatabricksErrors.redact("Password=hunter2").contains("<redacted>"));
        assertFalse(DatabricksErrors.redact("pwd=hunter2&next=1").contains("hunter2"));
    }

    @Test
    void aClassifiedMessageIsAlreadyRedacted()
    {
        AthenaConnectorException failure = DatabricksErrors.classify("connecting",
                new SQLException("connect failed for url jdbc:databricks://h:443;PWD=dapi-LEAKED"));
        assertFalse(failure.getMessage().contains("LEAKED"), failure.getMessage());
    }

    /**
     * The real {@code DatabricksDriverException}, not a stand-in. {@code isFromDriver} matches on
     * {@code getClass().getName()}, so a local subclass of {@link RuntimeException} would prove nothing
     * about the driver's actual types. The driver is a compile dependency, so these tests also fail if a
     * release moves the class out of {@code com.databricks.} or changes its supertype.
     */
    private static RuntimeException driverRuntimeException(String message)
    {
        return new com.databricks.jdbc.exception.DatabricksDriverException(
                message,
                com.databricks.jdbc.model.telemetry.enums.DatabricksDriverErrorCode.CONNECTION_ERROR);
    }

    @Test
    void theDriversAuthExceptionIsARuntimeExceptionAndNotASqlException()
    {
        // Asked reflectively because javac rejects `instanceof SQLException` on this type outright. If a
        // driver release makes it a SQLException, the catch sites can narrow again.
        Class<?> driverExceptionType = driverRuntimeException("x").getClass();
        assertFalse(SQLException.class.isAssignableFrom(driverExceptionType),
                "a catch on SQLException alone would miss " + driverExceptionType.getName());
        assertTrue(RuntimeException.class.isAssignableFrom(driverExceptionType),
                driverExceptionType.getName() + " should be a RuntimeException");
        assertTrue(DatabricksErrors.isFromDriver(driverRuntimeException("x")));
    }

    @Test
    void aDriverRuntimeExceptionIsClassifiedAndRedacted()
    {
        // The OAuth failure path. Verified in the 3.4.2 bytecode: OAuthRefreshCredentialsProvider,
        // DatabricksTokenFederationProvider, DatabricksClientConfiguratorManager and AuthMech all throw
        // DatabricksDriverException, which extends RuntimeException, so a catch on SQLException alone lets
        // it escape both classification and redaction.
        RuntimeException raw = driverRuntimeException(
                "invalid_client: HTTP Response code: 401 for OAuth2Secret=dose-SUPERSECRET");

        RuntimeException classified = DatabricksErrors.asConnectorFailure("connecting", raw);

        assertTrue(classified instanceof AthenaConnectorException, classified.getClass().getName());
        assertTrue(classified.getMessage().startsWith(DatabricksErrors.AUTHENTICATION_PREFIX),
                classified.getMessage());
        assertFalse(classified.getMessage().contains("SUPERSECRET"),
                "the redactor must run on this path too: " + classified.getMessage());
        assertTrue(classified.getMessage().contains("OAuth2Secret=<redacted>"),
                classified.getMessage());
    }

    @Test
    void anAlreadyClassifiedErrorPassesThroughUnchanged()
    {
        // RowCeilingSpiller's ceiling error arrives this way. Re-wrapping buries a message that names the
        // table and the setting under a generic one.
        AthenaConnectorException already = DatabricksErrors.classify("reading rows",
                new SQLException("original"));
        assertSame(already, DatabricksErrors.asConnectorFailure("reading rows", already));
    }

    @Test
    void thisConnectorsOwnValidationErrorPassesThroughUnchanged()
    {
        // "Unknown table", "unusable secret shape", "schema this connector does not serve": those messages
        // are written for the operator and say more than any classification would.
        IllegalArgumentException ours = new IllegalArgumentException("Unknown table: \"ghost\"");
        assertSame(ours, DatabricksErrors.asConnectorFailure("describing", ours));
    }

    @Test
    void anIllegalArgumentExceptionFromTheDriverIsStillClassifiedAndRedacted()
    {
        // The pass-through above must not become a hole: an IllegalArgumentException whose chain reaches
        // the driver is the driver's, so it gets redacted like any other driver failure.
        RuntimeException fromDriver = new IllegalArgumentException("bad argument",
                driverRuntimeException("PWD=dapi-SUPERSECRET rejected"));

        RuntimeException classified = DatabricksErrors.asConnectorFailure("connecting", fromDriver);
        assertTrue(classified instanceof AthenaConnectorException, classified.getClass().getName());
        assertFalse(classified.getMessage().contains("SUPERSECRET"), classified.getMessage());
    }

    @Test
    void isFromDriverWalksTheWholeChain()
    {
        assertTrue(DatabricksErrors.isFromDriver(driverRuntimeException("x")));
        assertTrue(DatabricksErrors.isFromDriver(
                new SQLException("wrapper", driverRuntimeException("x"))));
        assertFalse(DatabricksErrors.isFromDriver(new SQLException("nothing to do with the driver")));
        assertFalse(DatabricksErrors.isFromDriver(null));
    }

    @Test
    void aDriverRuntimeExceptionReportingAStoppedWarehouseIsStillDistinguishable()
    {
        RuntimeException classified = DatabricksErrors.asConnectorFailure("listing tables",
                driverRuntimeException("TEMPORARILY_UNAVAILABLE: the warehouse is starting"));
        assertTrue(classified.getMessage().startsWith(DatabricksErrors.WAREHOUSE_STARTING_PREFIX),
                classified.getMessage());
    }

    @Test
    void aKmsDecryptFailureNamesSecretsManagerRatherThanDatabricks()
    {
        // What a first deployment usually hits: the missing kms:Decrypt grant that CREDENTIAL_KMS_KEY_ARN
        // exists for. Classified as a Databricks failure it reads "Driver reported: Secrets Manager can't
        // decrypt...", which is false on both counts.
        AthenaConnectorException failure = DatabricksErrors.credentialUnreadable(
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:dbx-AbCdEf",
                new RuntimeException("Secrets Manager can't decrypt the protected secret text using"
                        + " the provided KMS key. (Service: SecretsManager, Status Code: 400)"));

        assertTrue(failure.getMessage().startsWith(DatabricksErrors.CREDENTIAL_UNREADABLE_PREFIX),
                failure.getMessage());
        assertFalse(failure.getMessage().contains("Driver reported"),
                "nothing here came from the driver: " + failure.getMessage());
        // Has to name the grant and the secret, or the operator has nowhere to start.
        assertTrue(failure.getMessage().contains("kms:Decrypt"), failure.getMessage());
        assertTrue(failure.getMessage().contains("secretsmanager:GetSecretValue"),
                failure.getMessage());
        assertTrue(failure.getMessage().contains("dbx-AbCdEf"), failure.getMessage());
        assertEquals(FederationSourceErrorCode.ACCESS_DENIED_EXCEPTION.toString(),
                failure.getErrorDetails().errorCode());
    }

    @Test
    void aNonDriver403DoesNotTellTheOperatorToRotateACredential()
    {
        // Without the provenance gate an S3 or KMS denial in a status-code context takes the authentication
        // branch, sending the operator to rotate a Databricks credential that was never the problem.
        RuntimeException s3Denial = new RuntimeException(
                "Access Denied (Service: S3, Status Code: 403, Request ID: ABC123)");

        RuntimeException failure = DatabricksErrors.asConnectorFailure("spilling a block", s3Denial);

        assertTrue(failure.getMessage().startsWith(DatabricksErrors.CONNECTOR_INTERNAL_PREFIX),
                failure.getMessage());
        assertFalse(failure.getMessage().contains(DatabricksErrors.AUTHENTICATION_PREFIX),
                failure.getMessage());
        assertTrue(failure.getMessage().contains("did not come from Databricks"),
                failure.getMessage());
    }

    @Test
    void aNonDriver503IsNotReportedAsATransientWarehouseResume()
    {
        // The self-defeating one: the warehouse-starting branch carries the transient error code, which
        // reintroduces the retry behaviour the row ceiling's code avoids. Athena re-invokes a failed
        // connector, so on the read path that is N billed warehouse reads.
        RuntimeException notDatabricks = new RuntimeException(
                "Service Unavailable (Service: Sts, Status Code: 503)");

        RuntimeException failure = DatabricksErrors.asConnectorFailure("assuming a role", notDatabricks);

        assertTrue(failure.getMessage().startsWith(DatabricksErrors.CONNECTOR_INTERNAL_PREFIX),
                failure.getMessage());
        assertEquals(FederationSourceErrorCode.INTERNAL_SERVICE_EXCEPTION.toString(),
                ((AthenaConnectorException) failure).getErrorDetails().errorCode());
        assertNotEquals(FederationSourceErrorCode.OPERATION_TIMEOUT_EXCEPTION.toString(),
                ((AthenaConnectorException) failure).getErrorDetails().errorCode(),
                "a non-driver failure must never carry the retry class");
    }

    @Test
    void aBugInThisConnectorIsNotAttributedToDatabricks()
    {
        RuntimeException ourBug = new NullPointerException(
                "Cannot invoke \"String.length()\" because \"comment\" is null");

        RuntimeException failure = DatabricksErrors.asConnectorFailure("describing orders", ourBug);

        assertTrue(failure.getMessage().startsWith(DatabricksErrors.CONNECTOR_INTERNAL_PREFIX),
                failure.getMessage());
        assertFalse(failure.getMessage().contains("Driver reported"), failure.getMessage());
        // The class name is carried: for a bug it is the most useful single token.
        assertTrue(failure.getMessage().contains("NullPointerException"), failure.getMessage());
        assertTrue(failure.getMessage().contains("describing orders"), failure.getMessage());
    }

    @Test
    void aNonDriverFailureIsStillRedacted()
    {
        // Provenance says nothing about whether the text is safe.
        RuntimeException leaky = new RuntimeException("config was PWD=dapi-SUPERSECRET");
        assertFalse(DatabricksErrors.asConnectorFailure("connecting", leaky).getMessage()
                .contains("SUPERSECRET"));
        assertFalse(DatabricksErrors.credentialUnreadable("arn:x", leaky).getMessage()
                .contains("SUPERSECRET"));
    }

    @Test
    void aDriverFailureStillGetsTheDatabricksPrefixes()
    {
        // The other side of the gate: the provenance check must not break the cases that were right.
        assertTrue(DatabricksErrors.asConnectorFailure("connecting",
                        driverRuntimeException("HTTP Response code: 503, Service Unavailable"))
                .getMessage().startsWith(DatabricksErrors.WAREHOUSE_STARTING_PREFIX));
        assertTrue(DatabricksErrors.asConnectorFailure("connecting",
                        driverRuntimeException("HTTP Response code: 401, Invalid access token"))
                .getMessage().startsWith(DatabricksErrors.AUTHENTICATION_PREFIX));
    }

    @Test
    void redactPassesNullAndPlainTextThrough()
    {
        assertEquals(null, DatabricksErrors.redact(null));
        assertEquals("nothing to redact", DatabricksErrors.redact("nothing to redact"));
    }

    @Test
    void aCyclicCauseChainDoesNotHang()
    {
        // Throwable.initCause refuses self-causation, but a driver that overrides getCause() can still
        // produce a cycle, so the walk is bounded rather than trusting it not to.
        SQLException cyclic = new SQLException("outer")
        {
            @Override
            public synchronized Throwable getCause()
            {
                return this;
            }
        };
        assertTrue(DatabricksErrors.classify("connecting", cyclic).getMessage()
                .startsWith(DatabricksErrors.QUERY_FAILED_PREFIX));
    }
}
