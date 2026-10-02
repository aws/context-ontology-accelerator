// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import {
  AthenaFederationConnector,
  Provisioning,
  functionNamePrefix,
  optionalIntEnv,
  queryRoleArns,
} from "coa-connector-cdk";
import { Construct } from "constructs";

/**
 * The connector's id. Names the stack, the Lambda, and anything else that has to be unique
 * so that two connectors deployed into one account cannot collide.
 */
export const CONNECTOR_ID = "example";

/** Handler class in the connector's fat jar. */
export const HANDLER = "dev.coa.example.ExampleCompositeHandler";

/**
 * Restated from the Java that owns them — `ExampleMetadataHandler.DATABASE` and `declareTables()`.
 * Nothing checks the two agree. Published as stack outputs for integration tests only.
 */
export const CONNECTOR_DATABASE = "example_source";

export const CONNECTOR_TABLES = [
  "customers",
  "orders",
  "order_lines",
  "shipment_lines",
  "bulk_rows",
] as const;

/** The fat jar, relative to this app. Built by `pnpm run package` before deploy. */
export const DEFAULT_JAR_PATH = path.join(
  __dirname,
  "..",
  "..",
  "target",
  "example-connector-1.0.0.jar",
);

/** Properties for {@link ExampleConnectorStack}. */
export interface ExampleConnectorStackProps extends cdk.StackProps {
  /** Overrides the jar location, so tests need no Maven build. */
  readonly jarPath?: string;

  /**
   * Overrides the principals granted access. Defaults to the COA roles named by
   * `SERVE_ROLE_ARN` and `DISCOVERY_ROLE_ARN`.
   */
  readonly queryRoleArns?: readonly string[];

  /**
   * Prefix for the Lambda name, defaulting to `FUNCTION_NAME_PREFIX`. Pass the same value the
   * app used for the stack name — see `bin/app.ts`.
   */
  readonly functionNamePrefix?: string;

  /**
   * Defaults to {@link Provisioning.CREATE}. {@link Provisioning.NONE} suits only a source that
   * cannot exceed 6 MB — which this fixture deliberately can.
   */
  readonly spill?: Provisioning;
}

/**
 * The example connector's stack — the file to copy for your own.
 *
 * <p>Note what it is: an ordinary CDK stack that happens to contain an
 * {@link AthenaFederationConnector}. The construct owns the Lambda, its spill bucket and the
 * resource policies COA needs. Everything else is yours — a VPC, an RDS proxy, a secret, a KMS key,
 * a cache table — added here and granted access to the connector:
 *
 * <pre>
 * const secret = secretsmanager.Secret.fromSecretNameV2(this, "Creds", "prod/sap/creds");
 * secret.grantRead(this.connector.connectorFunction);
 * </pre>
 *
 * Nothing in `coa-connector-cdk` needs to know you did that, which is why each connector owns its
 * app rather than being described to a shared one by a config file — a config file can only express
 * the resources somebody anticipated.
 *
 * <p>The handler class and jar path are this connector's code, committed here. The COA role ARNs and
 * the `bulk_rows` sizing are properties of a *deployment*, read from the environment so a pipeline
 * can set them and no secret is committed.
 */
export class ExampleConnectorStack extends cdk.Stack {
  public readonly connector: AthenaFederationConnector;

  constructor(scope: Construct, id: string, props: ExampleConnectorStackProps = {}) {
    super(scope, id, props);

    this.connector = new AthenaFederationConnector(this, "Connector", {
      connectorId: CONNECTOR_ID,
      handler: HANDLER,
      jarPath: props.jarPath ?? DEFAULT_JAR_PATH,
      queryRoleArns: props.queryRoleArns ?? queryRoleArns(),
      functionNamePrefix: props.functionNamePrefix ?? functionNamePrefix(),
      spill: props.spill,
      environment: bulkRowsSizing(),
      description:
        "Example Athena Query Federation connector serving the fabricated example_source database",
    });

    this.publishIntegTestOutputs();
  }

  /**
   * Stack outputs the integration tests read via `describe_stacks`. Test scaffolding, not part of
   * the connector contract — a customer writing their own needs none of it.
   *
   * <p>At stack level, not on the construct: CDK prefixes a construct's output with its path and a
   * hash (`ConnectorConnectorFunctionArn7CB8EEBF`), and a test looking up a mangled key that has
   * since changed skips while appearing healthy. Here the logical id is the key verbatim.
   *
   * <p>No `exportName`: an export creates a deletion dependency, wrong for a stand-in for a
   * separate customer account.
   */
  private publishIntegTestOutputs(): void {
    new cdk.CfnOutput(this, "ConnectorFunctionArn", {
      value: this.connector.connectorFunction.functionArn,
      description: "Register this ARN as an Athena LAMBDA data catalog",
    });
    new cdk.CfnOutput(this, "ConnectorDatabase", {
      value: CONNECTOR_DATABASE,
      description: "Database the connector serves; Athena calls it a schema",
    });
    new cdk.CfnOutput(this, "ConnectorTables", {
      value: CONNECTOR_TABLES.join(","),
      description: "Tables in that database, comma-separated",
    });
    // Marked absent rather than omitted under Provisioning.NONE, so a test never has to tell "key
    // missing" from "no bucket".
    new cdk.CfnOutput(this, "SpillBucket", {
      value: this.connector.spillBucket?.bucketName ?? "<none>",
      description: "Bucket the connector spills responses over 6 MB to",
    });
    new cdk.CfnOutput(this, "SpillKeyArn", {
      value: this.connector.spillKey?.keyArn ?? "<none>",
      description: "Customer-managed key encrypting the spill bucket",
    });
  }
}

/**
 * The `bulk_rows` fixture's size, from the environment so that exercising spill needs no code
 * change: `EXAMPLE_BULK_ROWS=4096 EXAMPLE_BULK_ROW_BYTES=2048 pnpm run deploy`. Unset, the connector
 * defaults both small so nothing spills.
 */
function bulkRowsSizing(): Record<string, string> {
  const environment: Record<string, string> = {};
  // optionalIntEnv, not optionalEnv: a typo like 4O96 would otherwise deploy untouched, the Java
  // side would fall back to its small default, and nothing would spill — while the operator
  // believed they had just tested the spill path.
  const rows = optionalIntEnv("EXAMPLE_BULK_ROWS");
  if (rows !== undefined) {
    environment.example_bulk_rows = String(rows);
  }
  const rowBytes = optionalIntEnv("EXAMPLE_BULK_ROW_BYTES");
  if (rowBytes !== undefined) {
    environment.example_bulk_row_bytes = String(rowBytes);
  }
  return environment;
}
