// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// The reserved datasource-access role prefix. The managed stack hands out `sts:AssumeRole` on
// `arn:aws:iam::*:role/{resourcePrefix}datasource-access-*` with no account restriction, so the prefix
// must stay reserved for customer-owned roles: a COA-internal role named under it would be assumable
// by the connector. `infra`'s aspect never sees this app, which keeps its own CDK app, so the managed
// stack attaches its own copy.
import * as iam from "aws-cdk-lib/aws-iam";
import { Template } from "aws-cdk-lib/assertions";
import { RESERVED_DATASOURCE_ROLE_SEGMENT } from "../lib/reserved-role-prefix";
import {
  RESOURCE_PREFIX,
  managedEnvFor,
  managedStack,
  statementWithSid,
  synth,
  synthManaged,
  useConnectorEnv,
} from "./helpers";

useConnectorEnv();

const RESERVED_DATASOURCE_ROLE_PREFIX = `${RESOURCE_PREFIX}${RESERVED_DATASOURCE_ROLE_SEGMENT}`;

/**
 * Logical ids of roles whose NAME or IAM PATH carries the reserved prefix. Blunt on purpose, as in
 * `infra/lib/aspects/reserved-role-prefix.ts`: the prefix anywhere in the resolved `RoleName`/`Path`
 * counts, since a structural check on an `Fn::Join` head quietly misses `Fn::Sub`. Scoped to those two
 * properties because the `AssumeRoleCoaManaged` statement legitimately names the prefix.
 */
function rolesUnderReservedPrefix(template: Template): string[] {
  return Object.entries(template.findResources("AWS::IAM::Role"))
    .filter(([, role]) => {
      const { RoleName, Path } = role.Properties ?? {};
      // An absent name is stringified rather than skipped, so a partially tokenised name whose
      // literal head sits inside the grant is still caught.
      return JSON.stringify({ RoleName: RoleName ?? null, Path: Path ?? null }).includes(
        RESERVED_DATASOURCE_ROLE_PREFIX,
      );
    })
    .map(([logicalId]) => logicalId);
}

/** A managed stack for `coa`/`dev`, unsynthesised, so a test can add a role before the aspect runs. */
function probeStack(): ReturnType<typeof managedStack> {
  managedEnvFor("coa", "dev");
  return managedStack();
}

describe("the reserved datasource-access role prefix", () => {
  it.each([
    ["the managed stack", () => synthManaged()],
    ["the customer-deployed stack", () => synth()],
  ])("has no COA-internal role under it in %s", (_stack, synthesise) => {
    const template = synthesise();

    // Both stacks create the Lambda's execution role, so the check is not vacuous.
    expect(Object.keys(template.findResources("AWS::IAM::Role")).length).toBeGreaterThan(0);
    expect(rolesUnderReservedPrefix(template)).toEqual([]);
  });

  it("is the same string the assume grant is scoped to", () => {
    // The guard above is only meaningful if it names the prefix the grant actually uses, and both
    // sides read one constant — so the literal is pinned here too.
    expect(RESERVED_DATASOURCE_ROLE_SEGMENT).toBe("datasource-access-");
    const statement = statementWithSid(synthManaged(), "AssumeRoleCoaManaged");
    expect(statement.Resource).toBe(`arn:aws:iam::*:role/${RESERVED_DATASOURCE_ROLE_PREFIX}*`);
  });

  it("fails the synth outright for a role named under the prefix", () => {
    // Refused rather than merely reported, and refused in this app's own synth, because infra's aspect
    // never sees this stack.
    const stack = probeStack();
    new iam.Role(stack, "Probe", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      roleName: `${RESERVED_DATASOURCE_ROLE_PREFIX}probe`,
    });

    expect(() => Template.fromStack(stack)).toThrow(
      /falls under the reserved datasource-access prefix "coa-dev-datasource-access-"/,
    );
    expect(() => Template.fromStack(stack)).toThrow(/assumable by the connector/);
  });

  it("fails the synth for a reserved IAM PATH on a role with no name of its own", () => {
    // An unnamed role at path `/coa-dev-datasource-access-x/` deploys as
    // `role/coa-dev-datasource-access-x/{generated}`, which the grant's wildcard matches.
    const stack = probeStack();
    new iam.Role(stack, "Probe", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      path: `/${RESERVED_DATASOURCE_ROLE_PREFIX}audit/`,
    });

    expect(() => Template.fromStack(stack)).toThrow(/reserved datasource-access prefix/);
  });

  it("allows an ordinary role beside the connector's, so the aspect is not a blanket refusal", () => {
    const stack = probeStack();
    new iam.Role(stack, "Probe", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      roleName: `${RESOURCE_PREFIX}databricks-probe`,
    });

    expect(() => Template.fromStack(stack)).not.toThrow();
  });
});
