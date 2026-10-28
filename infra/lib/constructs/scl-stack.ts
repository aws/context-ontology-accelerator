// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import { aossName, resolveContext, prefixed } from "../context";
import {
  RESERVED_DATASOURCE_ROLE_SEGMENT,
  ReservedRoleNamePrefix,
} from "../aspects/reserved-role-prefix";
import { solutionStackDescription } from "../solution";

/**
 * The deploying account and region, when the app imports a VPC (`vpc_id`).
 *
 * `Vpc.fromLookup` needs a concrete environment on the network stack, and every
 * stack that uses the VPC must share it, so all stacks get the account and
 * region the CDK CLI resolved from the credentials. Without `vpc_id` stacks stay
 * environment-agnostic, as before.
 */
function importedVpcEnv(scope: Construct): cdk.Environment | undefined {
  if (!scope.node.tryGetContext("vpc_id")) {
    return undefined;
  }
  return {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  };
}

/**
 * Base stack for all SemanticContext CloudFormation stacks.
 *
 * ## CDK Context Variables
 *   - `resource_prefix` (default: `DEFAULT_RESOURCE_PREFIX`, i.e. `"coa"`)
 *   - `env`             (default: `"dev"`)
 *   - `project_tag`     (default: `"semantic-context"`)
 *   - `vpc_id`          (optional, on NetworkStack - import existing VPC)
 *   - `aoss_vpc_endpoint_id` (required with `vpc_id` - its aoss-data endpoint)
 *
 * ## Resource Naming
 *   All resources follow `{prefix}-{env}-{name}` pattern via `prefixed()`.
 *   Example: `coa-dev-neptune`, `coa-staging-ingestion-queue`
 *
 * ## Tagging
 *   Auto-applied to all resources in every stack:
 *   - `Project`     = `project_tag` context value
 *   - `Environment` = `env` context value
 *   - `Component`   = per-stack via `addComponentTag()`
 *
 * ## Deployment Examples
 *   ```
 *   cdk deploy --all                                          # defaults
 *   cdk deploy --all -c env=staging                           # staging
 *   cdk deploy --all -c resource_prefix=myproj -c env=prod   # custom prefix
 *   cdk deploy --all -c vpc_id=vpc-0abc123 -c aoss_vpc_endpoint_id=vpce-0123456789abcdef0  # import VPC
 *   ```
 */
export class SCLStack extends cdk.Stack {
  public readonly envName: string;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, {
      ...props,
      env: props?.env ?? importedVpcEnv(scope),
      // AWS Solutions counts stack launches by the `(SO0376)` description prefix.
      description: props?.description ?? solutionStackDescription(id),
    });

    const { envName, projectTag } = resolveContext(this.node);
    cdk.Tags.of(this).add("Project", projectTag);
    cdk.Tags.of(this).add("Environment", envName);

    this.envName = envName;

    // Attached in the base class, not at the grant site: the reserved
    // `{prefix}-{env}-datasource-access-*` name bounds which roles COA will
    // assume, and a role created in any stack lands inside that bound.
    // Inheritance means a stack added later cannot forget the check.
    cdk.Aspects.of(this).add(
      new ReservedRoleNamePrefix(
        prefixed(this.node, RESERVED_DATASOURCE_ROLE_SEGMENT),
      ),
    );
  }

  /** Return a consistently prefixed resource name. */
  protected prefixed(name: string): string {
    return prefixed(this.node, name);
  }

  /** Prefixed name for an OpenSearch Serverless collection or policy (at most 32 characters). */
  protected aossName(name: string): string {
    return aossName(this.node, name);
  }

  /** Add a `Component` tag to this stack and all its children. */
  protected addComponentTag(component: string): void {
    cdk.Tags.of(this).add("Component", component);
  }
}
