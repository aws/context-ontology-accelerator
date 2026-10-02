// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as cdk from "aws-cdk-lib";
import { AthenaFederationConnector, connectorNetworkFromEnv } from "coa-connector-cdk";
import { Construct } from "constructs";
import { addConnectorAlarms, addManagedAlarms } from "./alarms";
import { ConnectorStackProps, createConnector } from "./connector";
import { PROD_ENV_NAME } from "./constants";
import {
  ManagedDeployment,
  assertManagedFunctionName,
  grantManagedConfigAccess,
  managedEnvironment,
  publishDeploymentParameters,
  publishManagedOutputs,
  resolveManagedDeployment,
} from "./managed-deployment";
import {
  RESERVED_DATASOURCE_ROLE_SEGMENT,
  ReservedRoleNamePrefix,
} from "./reserved-role-prefix";

/**
 * COA's own deployment of the Databricks connector: one Lambda serving every Databricks SQL
 * Warehouse source in one COA environment, resolving each source's endpoint and credential per
 * request from the Parameter Store parameter for the Athena catalog it was invoked under.
 *
 * Its role reads that parameter and assumes the role the source's owner named. Nothing on Secrets
 * Manager and nothing on KMS, unlike {@link DatabricksConnectorStack};
 * `connectors/databricks/README.md` compares the two modes row by row.
 *
 * It publishes its function and role ARNs under the sibling `deployment/` subtree, which it holds no
 * read on. COA creates one Athena catalog per source at registration, which is what the published
 * function ARN is for.
 */
export class ManagedDatabricksConnectorStack extends cdk.Stack {
  public readonly connector: AthenaFederationConnector;

  /** The facts derived from `COA_PREFIX` and `COA_ENV_NAME`. */
  public readonly managed: ManagedDeployment;

  constructor(scope: Construct, id: string, props: ConnectorStackProps = {}) {
    super(scope, id, props);

    // Resolved first, so a mistake fails before the fat jar is staged.
    const managed = resolveManagedDeployment();
    this.managed = managed;
    // COA's own VPC, which the deploy script reads from COA's parameters and exports. Required,
    // because this is COA-operated compute. CONNECTOR_SECURITY_GROUP_IDS is not read: the stack
    // creates the connector's own HTTPS-only group, so a value left in a shell or a `.env` cannot
    // widen what COA's connector may reach.
    const { vpc, subnets } = connectorNetworkFromEnv(this, {
      required: true,
      hint:
        "Deploy with scripts/deploy-managed-databricks-connector.sh, which reads both from " +
        "COA's /{prefix}/{env}/network/ parameters.",
    });

    // The synth-time half of the bound the assume grant relies on, attached before the function
    // exists so a role added anywhere later is covered. `infra` attaches the same aspect from
    // `SCLStack`, which never sees this app.
    cdk.Aspects.of(this).add(
      new ReservedRoleNamePrefix(`${managed.resourcePrefix}${RESERVED_DATASOURCE_ROLE_SEGMENT}`),
    );

    this.connector = createConnector(this, props, {
      functionNamePrefix: managed.functionNamePrefix,
      roleName: managed.roleName,
      spillRemovalPolicy:
        managed.envName === PROD_ENV_NAME ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      network: { vpc, subnets },
      environment: managedEnvironment(managed),
      description:
        "Athena Query Federation connector for every Databricks SQL Warehouse source in one COA\n" +
        "environment: endpoint and credential resolved per request from the parameter for the\n" +
        "Athena catalog it was invoked under",
    });

    assertManagedFunctionName(this.connector, managed);
    grantManagedConfigAccess(this, this.connector, managed);
    publishDeploymentParameters(this, this.connector, managed);
    addConnectorAlarms(
      this.connector,
      "At request time, the connector could not resolve a source's configuration: the parameter " +
        "under COA_CONFIG_SSM_PREFIX for the Athena catalog it was invoked under is missing, " +
        "unreadable, or carries another deployment's id. The connector's log names the catalog.",
    );
    addManagedAlarms(this.connector);
    publishManagedOutputs(this, this.connector, managed);
  }
}
