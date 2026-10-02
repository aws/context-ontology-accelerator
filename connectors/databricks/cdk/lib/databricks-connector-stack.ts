// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as cdk from "aws-cdk-lib";
import * as kms from "aws-cdk-lib/aws-kms";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import {
  AthenaFederationConnector,
  connectorNetworkFromEnv,
  functionNamePrefix,
  optionalEnv,
  requiredEnv,
} from "coa-connector-cdk";
import { Construct } from "constructs";
import { addConnectorAlarms } from "./alarms";
import { DatabricksConnectorStackProps, createConnector } from "./connector";
import {
  assertNotManagedFunctionName,
  publishSingleTargetOutputs,
  singleTargetEnvironment,
} from "./single-target";

/**
 * A customer-deployed Databricks connector: the Lambda, its own spill bucket and CMK, and read
 * access to one credential secret. One workspace, one warehouse, one Unity Catalog catalog and one
 * credential, all in the function's environment. COA's own deployment is
 * {@link ManagedDatabricksConnectorStack}; `connectors/databricks/README.md` compares the two.
 */
export class DatabricksConnectorStack extends cdk.Stack {
  public readonly connector: AthenaFederationConnector;

  constructor(scope: Construct, id: string, props: DatabricksConnectorStackProps = {}) {
    super(scope, id, props);

    assertNotManagedFunctionName(props);

    const credentialSecretArn = requiredEnv(
      "CREDENTIAL_SECRET_ARN",
      "A Secrets Manager secret holding {\"token\": ...} for a personal access token, or\n" +
        "{\"client_id\": ..., \"client_secret\": ...} for OAuth machine-to-machine. The shape\n" +
        "selects the auth mode; the connector refuses a secret carrying both.",
    );

    this.connector = createConnector(this, props, {
      functionNamePrefix: props.functionNamePrefix ?? functionNamePrefix(),
      environment: singleTargetEnvironment(credentialSecretArn),
      // Opt-in: unset leaves the function outside any VPC. Set, it is also what makes a
      // PrivateLink-only workspace reachable.
      network: connectorNetworkFromEnv(this),
      description:
        "Athena Query Federation connector for one Databricks SQL Warehouse: one Unity Catalog\n" +
        "catalog, and either one pinned schema or every schema within it",
    });

    // fromSecretCompleteArn, not fromSecretNameV2: the six-character suffix has to be part of the
    // grant, or the policy covers every secret whose name is a prefix of this one.
    const credential = secretsmanager.Secret.fromSecretCompleteArn(
      this,
      "Credential",
      credentialSecretArn,
    );
    credential.grantRead(this.connector.connectorFunction);

    // A secret encrypted with a customer-managed key needs kms:Decrypt on BOTH sides: this grant, and
    // a statement in the key's own policy. The second half is the key owner's and cannot be written
    // here.
    const credentialKeyArn = optionalEnv("CREDENTIAL_KMS_KEY_ARN");
    if (credentialKeyArn !== undefined) {
      kms.Key.fromKeyArn(this, "CredentialKey", credentialKeyArn).grantDecrypt(
        this.connector.connectorFunction,
      );
    }

    addConnectorAlarms(
      this.connector,
      "The connector could not resolve its configuration. Check the four required DATABRICKS_* " +
        "variables against the cold-start log line, which names the host, catalog and schema it read.",
    );
    publishSingleTargetOutputs(this, this.connector, credentialSecretArn);
  }
}
