// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Fixtures shared by the three suites in this directory. Not a test file itself — jest matches
// `*.test.ts` only.
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { NETWORK_VARS } from "coa-connector-cdk";
import { CONFIG_SOURCE_ENV_VAR } from "../lib/constants";
import { DatabricksConnectorStack } from "../lib/databricks-connector-stack";
import { ConnectorStackProps, DatabricksConnectorStackProps } from "../lib/connector";
import { ManagedDatabricksConnectorStack } from "../lib/managed-databricks-connector-stack";
import { REQUIRED_MANAGED_ENV_VARS } from "../lib/managed-deployment";
import { CONNECTOR_ENV_VARS } from "../lib/single-target";

/**
 * A stand-in for the fat JAR, so the tests do not require `mvn package` to have run. CDK stages a
 * `.jar` as an archive asset without inspecting its contents.
 */
export const FAKE_JAR = path.join(__dirname, "..", "cdk.out", "test-fixture.jar");

export const SERVE_ROLE = "arn:aws:iam::999988887777:role/scl-dev-serve-role";
export const DISCOVERY_ROLE = "arn:aws:iam::999988887777:role/scl-dev-sources-db-connector";
export const SECRET_ARN =
  "arn:aws:secretsmanager:us-east-1:123456789012:secret:databricks-connector-pat-AbCdEf";

/** The COA environment the managed suites deploy. */
const PREFIX = "coa";
const ENV_NAME = "dev";
export const RESOURCE_PREFIX = `${PREFIX}-${ENV_NAME}-`;
/** The connector appends `/<catalogName>`. */
export const CONFIG_SSM_PREFIX = "/coa/dev/connectors/databricks/sources";
/** The two parameters the managed stack writes, both under `deployment/` and neither under `sources/`. */
export const FUNCTION_ARN_PARAMETER = "/coa/dev/connectors/databricks/deployment/function-arn";
export const ROLE_ARN_PARAMETER = "/coa/dev/connectors/databricks/deployment/role-arn";
export const MANAGED_FUNCTION_NAME = "coa-dev-managed-databricks-coa-connector";

const TOUCHED = [
  ...CONNECTOR_ENV_VARS,
  "DATABRICKS_MAX_ROWS_PER_TABLE",
  "CREDENTIAL_KMS_KEY_ARN",
  "FUNCTION_NAME_PREFIX",
  "ALARM_TOPIC_ARN",
  // The mode selector. Cleared like the rest, so a developer's exported value cannot decide which
  // stack a suite builds.
  CONFIG_SOURCE_ENV_VAR,
  ...REQUIRED_MANAGED_ENV_VARS,
  ...Object.values(NETWORK_VARS),
];

/**
 * Writes the fixture jar and resets every variable either stack reads around each test.
 *
 * The single-target variables are set for every suite, including the managed ones: a stage-1 `.env`
 * sourced in the deployer's shell is the usual mistake, and the managed stack reading none of them is
 * what makes it harmless.
 */
export function useConnectorEnv(): void {
  let saved: Record<string, string | undefined> = {};

  beforeAll(() => {
    fs.mkdirSync(path.dirname(FAKE_JAR), { recursive: true });
    fs.writeFileSync(FAKE_JAR, "not really a jar");
  });

  beforeEach(() => {
    saved = {};
    for (const name of TOUCHED) {
      saved[name] = process.env[name];
      delete process.env[name];
    }
    process.env.DATABRICKS_WORKSPACE_HOSTNAME = "dbc-a1b2345c-d6e7.cloud.databricks.com";
    process.env.DATABRICKS_HTTP_PATH = "/sql/1.0/warehouses/a1b234c567d8e9fa";
    process.env.DATABRICKS_CATALOG = "workspace";
    process.env.DATABRICKS_SCHEMA = "coa_dbx_test";
    process.env.CREDENTIAL_SECRET_ARN = SECRET_ARN;
  });

  afterEach(() => {
    for (const name of TOUCHED) {
      if (saved[name] === undefined) {
        delete process.env[name];
      } else {
        process.env[name] = saved[name];
      }
    }
  });
}

const STACK_ENV = { account: "123456789012", region: "us-east-1" };

/** A customer-deployed synth. */
export function synth(props: Partial<DatabricksConnectorStackProps> = {}): Template {
  return Template.fromStack(
    new DatabricksConnectorStack(new cdk.App(), "databricks-coa-connector", {
      env: STACK_ENV,
      jarPath: FAKE_JAR,
      queryRoleArns: [SERVE_ROLE, DISCOVERY_ROLE],
      ...props,
    }),
  );
}

/** COA's VPC as the managed deploy script reads it from Parameter Store. */
export const COA_VPC_ID = "vpc-0c0a0d0e0f0a0b0c1";
export const COA_SUBNET_IDS = ["subnet-0c0a0d0e0f0a0b0c2", "subnet-0c0a0d0e0f0a0b0c3"];

/** Exports what the managed deploy script passes, and nothing else. */
export function managedEnvFor(prefix: string, envName: string): void {
  process.env.COA_PREFIX = prefix;
  process.env.COA_ENV_NAME = envName;
  process.env.CONNECTOR_VPC_ID = COA_VPC_ID;
  process.env.CONNECTOR_SUBNET_IDS = COA_SUBNET_IDS.join(",");
}

/** A managed stack, unsynthesised, so a test can add a role of its own before the aspect runs. */
export function managedStack(
  props: Partial<ConnectorStackProps> = {},
): ManagedDatabricksConnectorStack {
  return new ManagedDatabricksConnectorStack(new cdk.App(), "databricks-coa-connector", {
    env: STACK_ENV,
    jarPath: FAKE_JAR,
    queryRoleArns: [SERVE_ROLE, DISCOVERY_ROLE],
    ...props,
  });
}

/** A managed synth of the `coa`/`dev` deployment. */
export function synthManaged(props: Partial<ConnectorStackProps> = {}): Template {
  managedEnvFor(PREFIX, ENV_NAME);
  return Template.fromStack(managedStack(props));
}

/** A managed synth of some other deployment, which derives every name from these two scalars. */
export function synthManagedFor(prefix: string, envName: string): Template {
  managedEnvFor(prefix, envName);
  return Template.fromStack(managedStack());
}

/** The connector function's environment variables. */
export function connectorEnvironment(template: Template): Record<string, unknown> {
  const functions = template.findResources("AWS::Lambda::Function");
  const variables = Object.values(functions)
    .map((resource) => resource.Properties?.Environment?.Variables)
    .find((vars) => vars?.spill_prefix !== undefined);
  expect(variables).toBeDefined();
  return variables as Record<string, unknown>;
}

/**
 * Every literal KMS ARN the function's identity policy names. The spill key is not one: its Resource
 * is an `Fn::GetAtt` rather than a string, because this stack creates it.
 */
export function externalKmsResources(template: Template): string[] {
  const found: string[] = [];
  for (const statement of policyStatements(template)) {
    const resource = statement.Resource;
    if (typeof resource === "string" && resource.startsWith("arn:aws:kms:")) {
      found.push(resource);
    }
  }
  return found;
}

export interface PolicyStatement {
  readonly Sid?: string;
  readonly Effect?: string;
  readonly Action?: string | string[];
  readonly Resource?: unknown;
  readonly Condition?: Record<string, Record<string, unknown>>;
}

export function policyStatements(template: Template): PolicyStatement[] {
  const statements: PolicyStatement[] = [];
  for (const policy of Object.values(template.findResources("AWS::IAM::Policy"))) {
    for (const statement of policy.Properties?.PolicyDocument?.Statement ?? []) {
      statements.push(statement);
    }
  }
  return statements;
}

/** A statement's actions, as a list whether the template rendered one action or several. */
export function actionsOf(statement: PolicyStatement): string[] {
  const action = statement.Action;
  if (action === undefined) {
    return [];
  }
  return typeof action === "string" ? [action] : action;
}

export function statementWithSid(template: Template, sid: string): PolicyStatement {
  const found = policyStatements(template).filter((statement) => statement.Sid === sid);
  expect(found).toHaveLength(1);
  return found[0];
}

/**
 * Alarms in a template, and the subset in the connector's own metric namespace.
 *
 * The `COA/Connectors` ones are the connector's own EMF metrics; the rest are `AWS/Lambda` on the
 * function. Split because the two sets have different rules — only the first can be dimensioned by
 * catalog, and only the second exists for every connector.
 */
export function alarmsIn(template: Template): { Properties?: Record<string, unknown> }[] {
  return Object.values(template.findResources("AWS::CloudWatch::Alarm"));
}

export function connectorAlarmsIn(template: Template): { Properties?: Record<string, unknown> }[] {
  return alarmsIn(template).filter((alarm) => alarm.Properties?.Namespace === "COA/Connectors");
}

/**
 * Environment mode's alarm counts, pinned here and nowhere else. Seven, not three: each of the four
 * the connector emits itself is a *caught* failure, so the invocation succeeds and appears in neither
 * the Lambda error rate nor its duration.
 */
export const ENVIRONMENT_ALARM_COUNT = 7;
export const ENVIRONMENT_CONNECTOR_ALARM_COUNT = 4;

/**
 * What the managed stack adds: `ConnectorConfigThrottles` and `ConnectorCredentialAssumeFailures`,
 * the only two failures the other mode cannot have. Asserted as a delta, so an alarm added to both
 * stacks does not touch this number.
 */
export const MANAGED_ONLY_ALARM_COUNT = 2;
