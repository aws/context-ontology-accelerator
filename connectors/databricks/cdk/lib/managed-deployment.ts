// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as ssm from "aws-cdk-lib/aws-ssm";
import {
  AthenaFederationConnector,
  ENV_FILE,
  connectorFunctionName,
  optionalEnv,
} from "coa-connector-cdk";
import { Construct } from "constructs";
import {
  CONFIG_SOURCE_ENV_VAR,
  CONFIG_SSM_PREFIX_SUFFIX,
  CONNECTOR_ID,
  CONNECTOR_ROLE_NAME_SUFFIX,
  FUNCTION_ARN_PARAMETER_SUFFIX,
  MANAGED_FUNCTION_NAME_SEGMENT,
  MANAGED_MODE,
  ROLE_ARN_PARAMETER_SUFFIX,
} from "./constants";
import { operationalEnv, publishFunctionArnOutput, publishSpillOutputs } from "./connector";
import { RESERVED_DATASOURCE_ROLE_SEGMENT } from "./reserved-role-prefix";

/**
 * The two scalars a managed deployment is given. Everything else is derived from them, so no two
 * values can name different environments.
 *
 * Two rather than one composed token: the deploy script permits hyphens in the environment name, so
 * a single `coa-dev-2-` is ambiguous between `(coa, dev-2)` and `(coa-dev, 2)`.
 */
export const REQUIRED_MANAGED_ENV_VARS = ["COA_PREFIX", "COA_ENV_NAME"] as const;

/**
 * The variables a `coa-managed` function carries.
 *
 * Mirrors `ConnectionConfigProviders.MANAGED_VARS` in the jar, which requires all three: a list that
 * disagreed would synthesise a stack whose function cannot initialise.
 */
export const MANAGED_ENV_VARS = [
  "COA_CONFIG_SSM_PREFIX",
  "COA_DEPLOYMENT_ID",
  "COA_RESOURCE_PREFIX",
] as const;

/** Everything a `coa-managed` deployment needs, all of it derived from the two inputs. */
export interface ManagedDeployment {
  /** `/{prefix}/{envName}/connectors/databricks/sources`. The connector appends `/{catalogName}`. */
  readonly configSsmPrefix: string;

  /** `/{prefix}/{envName}/connectors/databricks/deployment/function-arn`. */
  readonly functionArnParameterName: string;

  /**
   * `/{prefix}/{envName}/connectors/databricks/deployment/role-arn`. The principal a customer's
   * credential-access role has to trust.
   */
  readonly roleArnParameterName: string;

  /** `{prefix}-{envName}-`. Bounds the roles the connector may assume, and derives its ExternalId. */
  readonly resourcePrefix: string;

  /** The COA environment this connector serves. */
  readonly envName: string;

  /** `{prefix}-{envName}`. Checked against every parameter's `deploymentId`. */
  readonly deploymentId: string;

  /** `{prefix}-{envName}-managed-`. The reserved prefix this connector's Lambda name carries. */
  readonly functionNamePrefix: string;

  /** `{prefix}-{envName}-managed-databricks-coa-connector`. Fixed for the life of the deployment. */
  readonly functionName: string;

  /**
   * `{prefix}-{envName}-databricks-connector-role`. PINNED: every customer's credential-access role
   * names this ARN in its trust policy, and a CloudFormation-generated name changes on any
   * replacement, breaking every trust policy in the fleet at once with no repair from COA's side.
   *
   * Not derived from {@link functionName}: that is already up to Lambda's 64 characters, so a
   * suffixed form of it can exceed IAM's.
   */
  readonly roleName: string;
}

/**
 * Derives the whole `coa-managed` contract from `COA_PREFIX` and `COA_ENV_NAME`.
 *
 * Derived rather than configured, because the failure is silent in the worst direction: a dev
 * registration resolving prod's connector ARN creates a catalog pointing at prod's connector.
 *
 * @throws Error when either input is missing.
 */
export function resolveManagedDeployment(): ManagedDeployment {
  const { prefix, envName } = requireManagedEnvVars();

  // The sources API derives each parameter's `deploymentId` from its own RESOURCE_PREFIX the same
  // way, and the connector refuses a parameter whose id does not match. Change all three together.
  const deploymentId = `${prefix}-${envName}`;
  const resourcePrefix = `${deploymentId}-`;
  // Composed once rather than per path, so no two published paths can name different environments.
  const pathRoot = `/${prefix}/${envName}`;
  const functionNamePrefix = `${resourcePrefix}${MANAGED_FUNCTION_NAME_SEGMENT}`;

  return {
    configSsmPrefix: pathRoot + CONFIG_SSM_PREFIX_SUFFIX,
    functionArnParameterName: pathRoot + FUNCTION_ARN_PARAMETER_SUFFIX,
    roleArnParameterName: pathRoot + ROLE_ARN_PARAMETER_SUFFIX,
    resourcePrefix,
    envName,
    deploymentId,
    functionNamePrefix,
    functionName: connectorFunctionName(CONNECTOR_ID, functionNamePrefix),
    roleName: `${resourcePrefix}${CONNECTOR_ROLE_NAME_SUFFIX}`,
  };
}

/**
 * The two inputs, or a message naming **all** the missing ones.
 *
 * Not two `requiredEnv` calls: those exit on the first, so an operator who has exported neither is
 * told about one, exports it, and runs the same build again to be told about the other.
 */
function requireManagedEnvVars(): { prefix: string; envName: string } {
  const prefix = optionalEnv("COA_PREFIX");
  const envName = optionalEnv("COA_ENV_NAME");
  if (prefix !== undefined && envName !== undefined) {
    return { prefix, envName };
  }
  const missing = REQUIRED_MANAGED_ENV_VARS.filter((name) => optionalEnv(name) === undefined);
  throw new Error(
    `${missing.join(", ")} ${missing.length === 1 ? "is" : "are"} not set. Put ` +
      `${missing.length === 1 ? "it" : "them"} in ${ENV_FILE} or export ` +
      `${missing.length === 1 ? "it" : "them"} — ` +
      `scripts/deploy-managed-databricks-connector.sh does.\n` +
      `COA_PREFIX is COA's resource prefix token, e.g. coa, and COA_ENV_NAME is the COA ` +
      `environment this connector serves, e.g. dev. Together they give the parameter path ` +
      `/{prefix}/{envName}${CONFIG_SSM_PREFIX_SUFFIX} the connector reads per request, the ` +
      `{prefix}-{envName}- resource prefix each source's sts:ExternalId is derived from, and the ` +
      `deployment id every parameter is checked against.`,
  );
}

/**
 * A `coa-managed` connector's environment: the mode, and the two values it resolves everything else
 * from. Nothing that names one workspace, one warehouse, one catalog, one schema or one secret.
 *
 * `COA_CONFIG_SSM_PREFIX` is a path rather than a value: the connector appends the Athena catalog
 * name it was invoked under and reads that parameter per request, so no source's endpoint is in this
 * template.
 */
export function managedEnvironment(managed: ManagedDeployment): Record<string, string> {
  return {
    [CONFIG_SOURCE_ENV_VAR]: MANAGED_MODE,
    COA_CONFIG_SSM_PREFIX: managed.configSsmPrefix,
    COA_DEPLOYMENT_ID: managed.deploymentId,
    COA_RESOURCE_PREFIX: managed.resourcePrefix,
    ...operationalEnv(),
  };
}

/**
 * The same name rule again, against the name the toolkit actually derived. The name is
 * `{prefix}{connectorId}{suffix}`, whose last two parts are toolkit constants, and moving one is the
 * only way it changes without anyone touching this app.
 */
export function assertManagedFunctionName(
  connector: AthenaFederationConnector,
  managed: ManagedDeployment,
): void {
  if (connector.functionName === managed.functionName) {
    return;
  }
  throw new Error(
    `The managed connector's function name is "${connector.functionName}", not the ` +
      `"${managed.functionName}" this deployment is committed to. connectorFunctionName's ` +
      `connector id or suffix has changed. Every Athena catalog COA has created embeds the old ` +
      `ARN and cannot be repointed, so this rename cannot ship.`,
  );
}

/**
 * The connector role's whole reach in `coa-managed` mode: read the parameter for the catalog it was
 * invoked under, and assume the role that source's owner named.
 *
 * Nothing on Secrets Manager and nothing on KMS. The credential sits behind a role COA does not own,
 * so the connector's reach at any instant is one session, scoped by a policy COA did not write and
 * cannot widen — which is what makes one shared connector acceptable at all.
 */
export function grantManagedConfigAccess(
  scope: Construct,
  connector: AthenaFederationConnector,
  managed: ManagedDeployment,
): void {
  const stack = cdk.Stack.of(scope);
  connector.connectorFunction.addToRolePolicy(
    new iam.PolicyStatement({
      sid: "ReadDatabricksSourceParameters",
      actions: ["ssm:GetParameter"],
      // No separator before the prefix: an SSM parameter ARN is `parameter` immediately followed by
      // a name that already begins with "/", and `parameter//coa/dev/...` matches nothing.
      //
      // The `sources/` subtree only: a prefix covering the sibling `deployment/` subtree would let a
      // compromised connector repoint the parameter every future catalog is created from.
      resources: [
        `arn:aws:ssm:${stack.region}:${stack.account}:parameter${managed.configSsmPrefix}/*`,
      ],
    }),
  );

  connector.connectorFunction.addToRolePolicy(
    new iam.PolicyStatement({
      // Same sid and shape as the statement COA's own sources and discovery roles carry:
      // registration validates a role ARN against this scope, and the two agreeing is what stops
      // COA accepting a source its connector cannot then read.
      sid: "AssumeRoleCoaManaged",
      actions: ["sts:AssumeRole"],
      // NO account restriction, deliberately: COA is deployed INTO the customer's account, so
      // excluding the deployment account would refuse the most common topology outright. The bound
      // is the reserved role-name prefix plus the target role's own trust policy, and it shares one
      // constant with the aspect the stack attaches.
      resources: [
        `arn:aws:iam::*:role/${managed.resourcePrefix}${RESERVED_DATASOURCE_ROLE_SEGMENT}*`,
      ],
      // The ExternalId — derived from the namespace, never accepted from a request — binds the
      // assume to the namespace that asked for it, so a regression that stopped sending one fails
      // closed at IAM rather than silently widening access.
      conditions: { Null: { "sts:ExternalId": "false" } },
    }),
  );
}

/**
 * Publishes the function ARN COA's source registration resolves and the execution role ARN a
 * customer's credential-access role has to trust.
 *
 * Written by CloudFormation, so `ssm:PutParameter` belongs to the *deploy* role and the connector's
 * runtime role gains nothing. That also gives a second managed connector in one environment an
 * "already exists" failure rather than silently repointing whoever reads these next.
 */
export function publishDeploymentParameters(
  scope: Construct,
  connector: AthenaFederationConnector,
  managed: ManagedDeployment,
): void {
  new ssm.StringParameter(scope, "FunctionArnParameter", {
    parameterName: managed.functionArnParameterName,
    stringValue: connector.connectorFunction.functionArn,
    description:
      "Lambda serving DATABRICKS_SQL_WAREHOUSE sources in this COA environment. Read by the " +
      "sources API at source create, which fails the create if it is absent.",
  });

  new ssm.StringParameter(scope, "RoleArnParameter", {
    parameterName: managed.roleArnParameterName,
    stringValue: connectorRoleArn(connector),
    description:
      "Execution role of the Lambda above. A Databricks source's credential-access role must " +
      "name this principal in its trust policy, conditioned on the namespace's " +
      "sts:ExternalId. The role name is pinned, so this value is stable; published so nobody " +
      "has to derive it.",
  });
}

/**
 * The connector function's execution role ARN. It throws rather than publishing a placeholder: a
 * parameter holding one would be copied into a trust policy.
 */
export function connectorRoleArn(connector: AthenaFederationConnector): string {
  const role = connector.connectorFunction.role;
  if (role === undefined) {
    throw new Error(
      "The connector function has no execution role to publish, so a credential owner would have " +
        "no principal to trust. This means the function was given an imported role; publish that " +
        "role's ARN explicitly instead.",
    );
  }
  return role.roleArn;
}

/**
 * What a `coa-managed` deployment publishes: the handoff, and the prefix each source's own parameter
 * hangs off.
 *
 * No `DatabricksCatalog`, `ConnectorDatabase` or `CredentialSecretArn`: every source has its own, in
 * its own parameter, so publishing "the" catalog could only name whatever the deployer's shell held.
 */
export function publishManagedOutputs(
  scope: Construct,
  connector: AthenaFederationConnector,
  managed: ManagedDeployment,
): void {
  publishFunctionArnOutput(scope, connector);
  new cdk.CfnOutput(scope, "FunctionArnParameterName", {
    value: managed.functionArnParameterName,
    description: "SSM parameter the sources API resolves this connector's ARN from",
  });
  // The value rather than the path: whoever just ran the deploy is the person who pastes it into a
  // trust policy, so the handoff needs no lookup.
  new cdk.CfnOutput(scope, "ConnectorRoleArn", {
    value: connectorRoleArn(connector),
    description:
      "Execution role a Databricks source's credential-access role must trust, conditioned on " +
      `the namespace's sts:ExternalId. Also published to ${managed.roleArnParameterName}`,
  });
  new cdk.CfnOutput(scope, "ConfigSsmPrefix", {
    value: managed.configSsmPrefix,
    description:
      "Per-source parameter prefix; the connector appends /<athenaCatalogName> per request",
  });
  publishSpillOutputs(scope, connector);
}
