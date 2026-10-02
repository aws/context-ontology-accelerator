// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as cdk from "aws-cdk-lib";
import {
  AthenaFederationConnector,
  ConnectorNetwork,
  optionalEnv,
  optionalIntEnv,
  queryRoleArns,
} from "coa-connector-cdk";
import { Construct } from "constructs";
import {
  CONNECTOR_ID,
  DEFAULT_JAR_PATH,
  HANDLER,
  MEMORY_SIZE_MB,
  TIMEOUT_SECONDS,
} from "./constants";

/** What both of this connector's stacks accept. */
export interface ConnectorStackProps extends cdk.StackProps {
  /** Overrides the jar location, so tests need no Maven build. */
  readonly jarPath?: string;

  /**
   * Overrides the principals granted invoke and spill read. Defaults to the COA roles named by
   * `SERVE_ROLE_ARN` and `DISCOVERY_ROLE_ARN`.
   */
  readonly queryRoleArns?: readonly string[];
}

/** Properties for {@link DatabricksConnectorStack}. */
export interface DatabricksConnectorStackProps extends ConnectorStackProps {
  /**
   * Prefix for the Lambda name, defaulting to `FUNCTION_NAME_PREFIX`. Tells two customer-deployed
   * connectors apart when they share an account.
   */
  readonly functionNamePrefix?: string;
}

/** What each stack supplies for itself: everything the two modes do not share. */
export interface ConnectorOverrides {
  readonly environment: Record<string, string>;
  readonly description: string;

  /** Prefix for the Lambda name. */
  readonly functionNamePrefix?: string;

  /** Pins the execution role name. Managed mode only: a customer's trust policy names it. */
  readonly roleName?: string;

  /** Defaults to the construct's `DESTROY`. */
  readonly spillRemovalPolicy?: cdk.RemovalPolicy;

  /** The VPC attachment. Required in managed mode, optional for a customer deployment. */
  readonly network?: ConnectorNetwork;
}

/**
 * The function itself, which both stacks share verbatim — same jar, handler, sizing, tag and
 * resource policies. Only the environment differs, and with it everything the role may reach.
 */
export function createConnector(
  scope: Construct,
  props: ConnectorStackProps,
  overrides: ConnectorOverrides,
): AthenaFederationConnector {
  return new AthenaFederationConnector(scope, "Connector", {
    connectorId: CONNECTOR_ID,
    handler: HANDLER,
    jarPath: props.jarPath ?? DEFAULT_JAR_PATH,
    queryRoleArns: props.queryRoleArns ?? queryRoleArns(),
    memorySize: MEMORY_SIZE_MB,
    timeout: cdk.Duration.seconds(TIMEOUT_SECONDS),
    alarmTopicArn: optionalEnv("ALARM_TOPIC_ARN"),
    ...overrides,
  });
}

/**
 * The one setting that is about the *deployment* rather than about one endpoint, so both modes
 * carry it.
 *
 * Validated here rather than in Java: a non-integer deploys untouched, the connector falls back to
 * its default, and the operator believes they raised the ceiling.
 */
export function operationalEnv(): Record<string, string> {
  const maxRows = optionalIntEnv("DATABRICKS_MAX_ROWS_PER_TABLE");
  return maxRows === undefined ? {} : { DATABRICKS_MAX_ROWS_PER_TABLE: String(maxRows) };
}

/**
 * Outputs declared at stack level rather than on the construct: CDK prefixes a construct's output
 * with its path and a hash, and a test looking up a mangled key that has since changed skips while
 * looking healthy.
 */
export function publishFunctionArnOutput(
  scope: Construct,
  connector: AthenaFederationConnector,
): void {
  new cdk.CfnOutput(scope, "ConnectorFunctionArn", {
    value: connector.connectorFunction.functionArn,
    description: "Register this ARN as an Athena LAMBDA data catalog",
  });
}

export function publishSpillOutputs(
  scope: Construct,
  connector: AthenaFederationConnector,
): void {
  new cdk.CfnOutput(scope, "SpillBucket", {
    value: connector.spillBucket?.bucketName ?? "<none>",
    description: "Bucket the connector spills responses over 6 MB to",
  });
  new cdk.CfnOutput(scope, "SpillKeyArn", {
    value: connector.spillKey?.keyArn ?? "<none>",
    description: "Customer-managed key encrypting the spill bucket",
  });
}
