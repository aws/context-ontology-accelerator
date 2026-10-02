// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as cdk from "aws-cdk-lib";
import {
  AthenaFederationConnector,
  functionNamePrefix,
  optionalEnv,
  requiredEnv,
} from "coa-connector-cdk";
import { Construct } from "constructs";
import { MANAGED_FUNCTION_NAME_SEGMENT } from "./constants";
import {
  DatabricksConnectorStackProps,
  operationalEnv,
  publishFunctionArnOutput,
  publishSpillOutputs,
} from "./connector";

/**
 * `ConnectorDatabase`'s value when `DATABRICKS_SCHEMA` is unset.
 *
 * Deliberately not schema-shaped: a consumer who pastes it into an Athena catalog registration or
 * COA's onboarding form should get an obvious error rather than a lookup for a schema called `all`.
 */
export const UNPINNED_DATABASE_OUTPUT = "<unpinned: every schema in the catalog>";

/**
 * Environment variables whose absence has to fail synth, in the README's order.
 *
 * A separate list from the union below, so the "missing coordinate fails synth" test iterates
 * exactly the variables that claim to be required.
 */
export const REQUIRED_CONNECTOR_ENV_VARS = [
  "DATABRICKS_WORKSPACE_HOSTNAME",
  "DATABRICKS_HTTP_PATH",
  "DATABRICKS_CATALOG",
  "CREDENTIAL_SECRET_ARN",
] as const;

/**
 * Environment variables the connector reads but does not require. `DATABRICKS_SCHEMA` pins it to one
 * Unity Catalog schema; unset, it serves every schema in `DATABRICKS_CATALOG` and the credential's
 * own grants are the only boundary.
 */
export const OPTIONAL_CONNECTOR_ENV_VARS = ["DATABRICKS_SCHEMA"] as const;

/** Every environment variable this stack reads and passes to the connector. */
export const CONNECTOR_ENV_VARS = [
  ...REQUIRED_CONNECTOR_ENV_VARS,
  ...OPTIONAL_CONNECTOR_ENV_VARS,
] as const;

/**
 * Lower-cases a Unity Catalog identifier, as `ConnectionConfig.Builder` does on the Java side.
 *
 * Without the same fold here, `DATABRICKS_SCHEMA=Sales` deploys cleanly, publishes `Sales` as the
 * `ConnectorDatabase` output, and every consumer of that output gets
 * `Unknown schema: "Sales". This connector serves only "sales"`.
 */
function ucIdentifier(value: string): string {
  return value.toLowerCase();
}

/**
 * A bare SQL identifier, mirroring `ConnectionConfig.IDENTIFIER` on the Java side.
 *
 * Checked here as well as there because `DATABRICKS_SCHEMA` is optional: a value the Java pattern
 * rejects would deploy cleanly and fail at the first query, where it is indistinguishable from the
 * unpinned mode.
 */
const UC_IDENTIFIER = /^[a-z_][a-z0-9_]*$/;

/**
 * Reads an optional Unity Catalog identifier: lower-cased, shape-checked, `undefined` when absent.
 *
 * Blank counts as absent, matching `ConnectionConfig.Builder.schema`. CDK, the console and a shell
 * disagree about whether an unset variable arrives missing or empty, and an operator means the same
 * thing by both.
 */
function optionalUcIdentifier(name: string): string | undefined {
  const raw = optionalEnv(name);
  if (raw === undefined || raw.trim() === "") {
    return undefined;
  }
  const value = ucIdentifier(raw.trim());
  if (!UC_IDENTIFIER.test(value)) {
    throw new Error(
      `${name}="${raw}" is not a bare SQL identifier: a letter or underscore, then letters, ` +
        `digits or underscores. Unity Catalog allows a quoted name containing more, but such a ` +
        `name is not addressable through both of Athena's parsers, so the connector refuses it.`,
    );
  }
  return value;
}

/**
 * The connector's environment: three required coordinates, the secret's ARN, and two optional
 * settings (the schema pin and the row ceiling).
 *
 * The credential's *value* is absent. `CREDENTIAL_SECRET_ARN` is a pointer; a Lambda environment
 * variable is readable by anyone with `lambda:GetFunctionConfiguration` and appears in the
 * CloudFormation template in plain text.
 */
export function singleTargetEnvironment(credentialSecretArn: string): Record<string, string> {
  const environment: Record<string, string> = {
    DATABRICKS_WORKSPACE_HOSTNAME: requiredEnv(
      "DATABRICKS_WORKSPACE_HOSTNAME",
      "The workspace host, with no scheme and no port, on any cloud: " +
        "dbc-xxxxxxxx-xxxx.cloud.databricks.com (AWS), " +
        "adb-xxxxxxxxxxxxxxxx.x.azuredatabricks.net (Azure), " +
        "xxxxxxxxxxxxxxxx.x.gcp.databricks.com (GCP)",
    ),
    DATABRICKS_HTTP_PATH: requiredEnv(
      "DATABRICKS_HTTP_PATH",
      "The SQL Warehouse's HTTP path, from its Connection details tab:\n" +
        "/sql/1.0/warehouses/<warehouse id>",
    ),
    DATABRICKS_CATALOG: ucIdentifier(
      requiredEnv("DATABRICKS_CATALOG", "The Unity Catalog catalog to read."),
    ),
    CREDENTIAL_SECRET_ARN: credentialSecretArn,
  };

  // Set, the connector serves that schema and refuses every other name, a containment boundary it
  // enforces itself on top of the credential's Unity Catalog grants. Unset, it enumerates the
  // catalog's schemas and those grants are the only boundary. Omitted rather than set empty when
  // unpinned: an empty Lambda environment variable reads in the console as a value someone cleared
  // by mistake.
  const schema = optionalUcIdentifier("DATABRICKS_SCHEMA");
  if (schema !== undefined) {
    environment.DATABRICKS_SCHEMA = schema;
  }

  return { ...environment, ...operationalEnv() };
}

/**
 * Refuses a deployment that would take over the managed connector's name — the one route to a
 * cross-namespace read that carries no `COA_*` variable for anything else to see.
 *
 * Checked on the ending rather than anywhere in the prefix, because ending in the reserved segment is
 * exactly what makes the derived function name identical to a managed deployment's.
 */
export function assertNotManagedFunctionName(props: DatabricksConnectorStackProps): void {
  const prefix = props.functionNamePrefix ?? functionNamePrefix();
  if (prefix === undefined || !prefix.endsWith(MANAGED_FUNCTION_NAME_SEGMENT)) {
    return;
  }
  throw new Error(
    `FUNCTION_NAME_PREFIX="${prefix}" ends in the reserved "${MANAGED_FUNCTION_NAME_SEGMENT}" ` +
      `segment, which is COA's own managed deployment of this connector.\n` +
      `This would not collide: the stack name comes from the same prefix, so it would UPDATE COA's ` +
      `managed connector in place, keeping its function ARN. Every Athena catalog COA has already ` +
      `created for a DATABRICKS_SQL_WAREHOUSE source embeds that ARN and never re-resolves it, so ` +
      `all of them would keep invoking this function — now in single-endpoint mode, where the ` +
      `catalog name is ignored and every namespace resolves the one workspace and the one ` +
      `credential this deployment supplies. One namespace's query would return another's rows.\n` +
      `Choose a different FUNCTION_NAME_PREFIX — unset is the normal case — or set ` +
      `DATABRICKS_CONFIG_SOURCE=coa-managed if you meant to deploy COA's own connector.`,
  );
}

/**
 * Outputs the integration tests and the onboarding steps read. Three of them describe ONE endpoint
 * and so exist in this mode only.
 */
export function publishSingleTargetOutputs(
  scope: Construct,
  connector: AthenaFederationConnector,
  credentialSecretArn: string,
): void {
  publishFunctionArnOutput(scope, connector);
  // Lower-cased, like the environment variables above: this output is what the README tells an
  // operator to paste into the Athena catalog registration and COA's onboarding form, and the
  // connector only answers to the folded name. Always published, so a consumer never has to handle a
  // missing key, and when unpinned it names the mode rather than a schema the connector does not
  // serve.
  const schema = optionalUcIdentifier("DATABRICKS_SCHEMA");
  new cdk.CfnOutput(scope, "ConnectorDatabase", {
    value: schema ?? UNPINNED_DATABASE_OUTPUT,
    description: schema
      ? "The single Unity Catalog schema this connector exposes; Athena calls it a schema too"
      : "This connector is unpinned and exposes every schema in DatabricksCatalog; " +
        "run SHOW DATABASES against the registered Athena catalog to list them",
  });
  new cdk.CfnOutput(scope, "DatabricksCatalog", {
    value: ucIdentifier(requiredEnv("DATABRICKS_CATALOG")),
    description: "Unity Catalog catalog this connector reads",
  });
  new cdk.CfnOutput(scope, "CredentialSecretArn", {
    value: credentialSecretArn,
    description: "Secret the connector reads its Databricks credential from",
  });
  publishSpillOutputs(scope, connector);
}
