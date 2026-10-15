// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Template } from "aws-cdk-lib/assertions";
import { EniWarmStack } from "../../lib/stacks/foundation";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../../lib/constants";

const TEST_CONTEXT = {
  resource_prefix: DEFAULT_RESOURCE_PREFIX,
  env: DEFAULT_ENV,
};
const PREFIX = `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}`;

describe("EniWarmStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const depStack = new cdk.Stack(app, "DepStack");
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const mkSg = (id: string) => new ec2.SecurityGroup(depStack, id, { vpc });
    const lambdaSecurityGroup = mkSg("LambdaSG");
    const discoveryOcspSecurityGroup = mkSg("OcspSG");
    const ecsSecurityGroup = mkSg("EcsSG");
    const authorizerSecurityGroup = mkSg("AuthorizerSG");
    const stack = new EniWarmStack(app, "TestEniWarm", {
      vpc,
      lambdaSecurityGroup,
      discoveryOcspSecurityGroup,
      ecsSecurityGroup,
      authorizerSecurityGroup,
    });
    template = Template.fromStack(stack);
  });

  // Cross-stack references resolve to Fn::ImportValue of a DepStack export whose
  // name embeds the SG's construct ID.
  const SG_EXPORT_IDS = {
    lambda: "LambdaSG",
    ocsp: "OcspSG",
    ecs: "EcsSG",
    authorizer: "AuthorizerSG",
  };

  /** Security-group logical IDs (from the import names) of one warm function. */
  function sgsOf(functionName: string): string[] {
    const fns = template.findResources("AWS::Lambda::Function", {
      Properties: { FunctionName: functionName },
    });
    const [fn] = Object.values(fns);
    expect(fn).toBeDefined();
    const ids: unknown[] = fn.Properties.VpcConfig.SecurityGroupIds;
    return ids.map((id) => {
      const name = JSON.stringify(id);
      const match = Object.entries(SG_EXPORT_IDS).find(([, constructId]) =>
        name.includes(constructId),
      );
      return match ? match[0] : name;
    });
  }

  test("holds one never-invoked VPC function per security-group combination", () => {
    template.resourceCountIs("AWS::Lambda::Function", 4);
    expect(sgsOf(`${PREFIX}-eni-warm-lambda`)).toEqual(["lambda"]);
    expect(sgsOf(`${PREFIX}-eni-warm-lambdaocsp`).sort()).toEqual(
      ["lambda", "ocsp"].sort(),
    );
    expect(sgsOf(`${PREFIX}-eni-warm-ecs`)).toEqual(["ecs"]);
    expect(sgsOf(`${PREFIX}-eni-warm-authorizer`)).toEqual(["authorizer"]);
  });

  test("creates no security groups of its own", () => {
    template.resourceCountIs("AWS::EC2::SecurityGroup", 0);
  });
});
