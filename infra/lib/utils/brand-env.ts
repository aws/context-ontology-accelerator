// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { IAspect } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { IConstruct } from "constructs";

import { resolveContext } from "../context";

/**
 * Data-plane brand tokens for the runtime. They are static by design (see the
 * BRAND block in libs/ts-shared/src/constants.ts). Injecting them lets the
 * `graph_base_uri` and `event_source_prefix` context overrides reach the
 * runtime, and stops the runtime inferring graph IRIs from RESOURCE_PREFIX
 * (`{prefix}-{env}-`), which would not match the IRIs already in the graph.
 */
export function brandEnv(node: IConstruct["node"]): Record<string, string> {
  const { graphBaseUri, eventSourcePrefix } = resolveContext(node);
  return {
    GRAPH_BASE_URI: graphBaseUri,
    EVENT_SOURCE_PREFIX: eventSourcePrefix,
  };
}

/**
 * Applies {@link brandEnv} to every Lambda function and ECS container.
 *
 * Done as an aspect rather than by editing each of the ~32 `environment:` blocks
 * so a newly added function cannot miss it. Adding env vars changes only
 * resource properties, never construct paths, so no CloudFormation logical ID
 * moves.
 */
export class BrandEnvAspect implements IAspect {
  public visit(construct: IConstruct): void {
    const target =
      construct instanceof lambda.Function ||
      construct instanceof ecs.ContainerDefinition
        ? construct
        : undefined;
    if (!target) return;
    for (const [k, v] of Object.entries(brandEnv(target.node))) {
      target.addEnvironment(k, v);
    }
  }
}
