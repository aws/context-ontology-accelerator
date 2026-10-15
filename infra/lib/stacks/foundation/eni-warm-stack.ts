// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Construct } from "constructs";
import { SCLStack } from "../../constructs/scl-stack";

export interface EniWarmStackProps extends cdk.StackProps {
  readonly vpc: ec2.IVpc;
  readonly lambdaSecurityGroup: ec2.ISecurityGroup;
  readonly discoveryOcspSecurityGroup: ec2.ISecurityGroup;
  readonly ecsSecurityGroup: ec2.ISecurityGroup;
  readonly authorizerSecurityGroup: ec2.ISecurityGroup;
}

/**
 * Pre-creates the Lambda Hyperplane ENIs that the service stacks need.
 *
 * Lambda creates one shared ENI per (subnet, security-group set) combination,
 * and the first function to use a combination waits for it (measured ~228 s),
 * while every later function on the same combination creates in ~10 s. This
 * stack holds one never-invoked function per combination the platform uses, so the
 * wait happens here (in parallel with the storage stack) instead of inside
 * every service stack on the critical path. The functions are never invoked.
 *
 * Keep the combinations in step with the VPC Lambdas: a function whose SG set
 * matches none of these pays the full wait again.
 */
export class EniWarmStack extends SCLStack {
  constructor(scope: Construct, id: string, props: EniWarmStackProps) {
    super(scope, id, props);
    this.addComponentTag("foundation");

    const combos: Record<string, ec2.ISecurityGroup[]> = {
      Lambda: [props.lambdaSecurityGroup],
      LambdaOcsp: [props.lambdaSecurityGroup, props.discoveryOcspSecurityGroup],
      // vkg's reload Lambda runs on the ECS SG.
      Ecs: [props.ecsSecurityGroup],
      Authorizer: [props.authorizerSecurityGroup],
    };

    for (const [name, securityGroups] of Object.entries(combos)) {
      new lambda.Function(this, `EniWarm${name}`, {
        functionName: this.prefixed(`eni-warm-${name.toLowerCase()}`),
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: "index.handler",
        // Lambda creates the ENI when the function is created, not when it is
        // invoked, so the handler only has to refuse to run.
        code: lambda.Code.fromInline(
          "def handler(event, context):\n" +
            '    raise RuntimeError("eni-warm: holds a VPC network interface for this security-group set; not meant to be invoked")\n',
        ),
        vpc: props.vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups,
        description:
          "Never invoked: holds a Hyperplane ENI for this SG combination",
      });
    }
  }
}
