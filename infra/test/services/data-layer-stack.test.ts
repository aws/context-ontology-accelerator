// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template, Match } from "aws-cdk-lib/assertions";
import { DataLayerStack } from "../../lib/stacks/services/data-layer-stack";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../../lib/constants";

// Spy on the Python bundler so we can assert the Lambda ships the pip deps its
// handler needs (see the cold-start regression test below), while still
// returning a trivial inline asset so no real Docker/pip bundling runs in tests.
const mockBundlePython = jest.fn((_opts: unknown) =>
  lambda.Code.fromInline("def handler(event, context): pass"),
);
jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: (opts: unknown) => mockBundlePython(opts),
}));

const TEST_CONTEXT = {
  resource_prefix: DEFAULT_RESOURCE_PREFIX,
  env: DEFAULT_ENV,
  "aws:cdk:bundling-stacks": [],
};

// The DescribeSchema and ListMetrics handlers invoke backend Lambdas directly
// (bypassing the Context Manager). That direct-invoke path only works if the
// Lambda is wired up two ways:
//   1. Both backend ARNs must reach it as environment variables so the Python
//      handler can dispatch to the correct FunctionName.
//   2. Its execution role must hold ``lambda:InvokeFunction`` on both ARNs;
//      otherwise the invoke returns 502.
// These assertions pin both. A regression that drops either would silently
// break discovery in production — and the code review on MR !939 caught it
// once already.
describe("DataLayerStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });

    const depStack = new cdk.Stack(app, "DepStack", {
      env: { account: "123456789012", region: "us-east-1" },
    });
    const vpc = new ec2.Vpc(depStack, "Vpc", { maxAzs: 2 });
    const lambdaSg = new ec2.SecurityGroup(depStack, "LambdaSg", { vpc });

    const stack = new DataLayerStack(app, "TestDataLayer", {
      env: { account: "123456789012", region: "us-east-1" },
      vpc,
      lambdaSecurityGroup: lambdaSg,
    });

    template = Template.fromStack(stack);
  });

  it("bundles the deps coa_common needs at cold start (502 regression guard)", () => {
    // handler.py imports from coa_common; importing any coa_common submodule
    // runs its package __init__, which eagerly imports pydantic (authnz_types),
    // pydantic-settings (config) and structlog. If the bundle omits them the
    // Lambda raises Runtime.ImportModuleError at cold start and API Gateway
    // returns 502 on EVERY route — the exact regression the MCP/data-layer
    // parity refactor introduced by adding coa_common imports to a Lambda whose
    // bundle previously shipped zero pip deps.
    expect(mockBundlePython).toHaveBeenCalled();
    const opts = mockBundlePython.mock.calls[0][0] as { pipDeps?: string[] };
    expect(opts.pipDeps).toEqual(
      expect.arrayContaining(["pydantic", "pydantic-settings", "structlog"]),
    );
  });

  it("wires the ontology-proxy Lambda ARN into the API Lambda env", () => {
    template.hasResourceProperties(
      "AWS::Lambda::Function",
      Match.objectLike({
        Environment: {
          Variables: Match.objectLike({
            ONTOLOGY_PROXY_LAMBDA_ARN: Match.anyValue(),
          }),
        },
      }),
    );
  });

  it("wires the metric-service Lambda ARN into the API Lambda env", () => {
    template.hasResourceProperties(
      "AWS::Lambda::Function",
      Match.objectLike({
        Environment: {
          Variables: Match.objectLike({
            METRIC_SERVICE_LAMBDA_ARN: Match.anyValue(),
          }),
        },
      }),
    );
  });

  it("grants lambda:InvokeFunction on both discovery-backend ARNs", () => {
    // A single policy statement grants invoke on both ARNs (ontology-proxy +
    // metric-service). Assert the statement exists AND that its Resource list
    // has exactly 2 entries — dropping either one → 502 for that surface.
    const policies = template.findResources("AWS::IAM::Policy");
    const invokeStatements = Object.values(policies)
      .flatMap((p: any) => p.Properties.PolicyDocument.Statement)
      .filter((s: any) => {
        const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
        return actions.includes("lambda:InvokeFunction");
      });
    expect(invokeStatements.length).toBeGreaterThanOrEqual(1);
    // The invoke statement lives on the api-fn's role and targets both
    // backend ARNs — collect every resource across all matching statements.
    const targets = invokeStatements.flatMap((s: any) =>
      Array.isArray(s.Resource) ? s.Resource : [s.Resource],
    );
    expect(targets.length).toBe(2);
  });

  it("publishes the API Lambda ARN via SSM for the ApiStack to import", () => {
    // Match on the suffix — the leading ``/{prefix}`` piece is deployment-
    // context-dependent (see ``resolveContext``) so keep the assertion on
    // the invariant part of the parameter path.
    template.hasResourceProperties(
      "AWS::SSM::Parameter",
      Match.objectLike({
        Name: Match.stringLikeRegexp(".+/data-layer/api-fn-arn$"),
      }),
    );
  });
});
