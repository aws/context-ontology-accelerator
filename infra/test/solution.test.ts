// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { SCLStack } from "../lib/constructs/scl-stack";
import {
  SOLUTION_ID,
  SOLUTION_VERSION,
  solutionStackDescription,
} from "../lib/solution";
import { version } from "../package.json";

describe("solution identity", () => {
  it("uses the assigned solution ID and release version", () => {
    expect(SOLUTION_ID).toBe("SO0376");
    expect(SOLUTION_VERSION).toBe(`v${version}`);
  });

  it("starts the stack description with the solution ID", () => {
    expect(solutionStackDescription("coa-dev-network")).toBe(
      `(SO0376) - Context Ontology Accelerator - coa-dev-network. Version v${version}`,
    );
  });
});

describe("SCLStack description", () => {
  const synth = (props?: cdk.StackProps) => {
    const app = new cdk.App({
      context: { resource_prefix: "coa", env: "dev" },
    });
    const stack = new SCLStack(app, "coa-dev-test", props);
    new cdk.CfnOutput(stack, "Out", { value: "x" });
    return Template.fromStack(stack).toJSON();
  };

  it("sets the (SO0376) description on every stack by default", () => {
    expect(synth().Description).toBe(solutionStackDescription("coa-dev-test"));
  });

  it("keeps a description a stack passes explicitly", () => {
    expect(synth({ description: "custom" }).Description).toBe("custom");
  });
});
