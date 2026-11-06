// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";

/** Repository GuardDuty hosts its Fargate agent in. */
export const GUARDDUTY_FARGATE_AGENT_REPOSITORY = "aws-guardduty-agent-fargate";

/**
 * Let the task's execution role pull GuardDuty Runtime Monitoring's agent
 * sidecar. CDK only grants pull on the task's own image repositories, so
 * without this the sidecar fails with 403 and the task runs unmonitored.
 * The agent repo's account differs per Region, hence the account wildcard.
 */
export function grantGuardDutyAgentPull(taskDef: ecs.TaskDefinition): void {
  taskDef.obtainExecutionRole().addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions: [
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
      ],
      resources: [
        cdk.Stack.of(taskDef).formatArn({
          service: "ecr",
          account: "*",
          resource: "repository",
          resourceName: GUARDDUTY_FARGATE_AGENT_REPOSITORY,
        }),
      ],
    }),
  );
}
