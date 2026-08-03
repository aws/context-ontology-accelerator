// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import { resolveContext, prefixed } from "../context";

/**
 * Base stack for all SemanticContext CloudFormation stacks.
 *
 * ## CDK Context Variables
 *   - `resource_prefix` (default: `DEFAULT_RESOURCE_PREFIX`, i.e. `"coa"`)
 *   - `env`             (default: `"dev"`)
 *   - `project_tag`     (default: `"semantic-context"`)
 *   - `vpc_id`          (optional, on NetworkStack - import existing VPC)
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
 *   cdk deploy --all -c vpc_id=vpc-0abc123                    # import VPC
 *   ```
 */
export class SCLStack extends cdk.Stack {
  public readonly envName: string;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const { envName, projectTag } = resolveContext(this.node);
    cdk.Tags.of(this).add("Project", projectTag);
    cdk.Tags.of(this).add("Environment", envName);

    this.envName = envName;
  }

  /** Return a consistently prefixed resource name. */
  protected prefixed(name: string): string {
    return prefixed(this.node, name);
  }

  /** Add a `Component` tag to this stack and all its children. */
  protected addComponentTag(component: string): void {
    cdk.Tags.of(this).add("Component", component);
  }
}
