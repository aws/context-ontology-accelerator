// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { SCLStack } from "../../lib/constructs/scl-stack";

describe("SCLStack environment", () => {
  const saved = { ...process.env };
  afterEach(() => {
    process.env = { ...saved };
  });

  it("stays environment-agnostic without vpc_id", () => {
    process.env.CDK_DEFAULT_ACCOUNT = "0".repeat(12);
    process.env.CDK_DEFAULT_REGION = "eu-central-1";
    const stack = new SCLStack(new cdk.App(), "S");
    expect(cdk.Token.isUnresolved(stack.account)).toBe(true);
    expect(cdk.Token.isUnresolved(stack.region)).toBe(true);
  });

  it("takes the deploying account and region when vpc_id is set", () => {
    // Vpc.fromLookup cannot run in an environment-agnostic stack.
    process.env.CDK_DEFAULT_ACCOUNT = "0".repeat(12);
    process.env.CDK_DEFAULT_REGION = "eu-central-1";
    const app = new cdk.App({ context: { vpc_id: "vpc-0abc" } });
    const stack = new SCLStack(app, "S");
    expect(stack.account).toBe("0".repeat(12));
    expect(stack.region).toBe("eu-central-1");
  });

  it("keeps an explicit env", () => {
    process.env.CDK_DEFAULT_ACCOUNT = "0".repeat(12);
    process.env.CDK_DEFAULT_REGION = "eu-central-1";
    const app = new cdk.App({ context: { vpc_id: "vpc-0abc" } });
    const stack = new SCLStack(app, "S", {
      env: { account: "0".repeat(12), region: "us-east-1" },
    });
    expect(stack.region).toBe("us-east-1");
  });

  it.each([
    ["an account", "CDK_DEFAULT_ACCOUNT"],
    ["a region (credentials resolved, region unset)", "CDK_DEFAULT_REGION"],
  ])("fails with a reason when vpc_id is set without %s", (_label, missing) => {
    process.env.CDK_DEFAULT_ACCOUNT = "0".repeat(12);
    process.env.CDK_DEFAULT_REGION = "eu-central-1";
    delete process.env[missing];
    const app = new cdk.App({ context: { vpc_id: "vpc-0abc" } });
    expect(() => new SCLStack(app, "S")).toThrow(/credentials/);
  });
});
