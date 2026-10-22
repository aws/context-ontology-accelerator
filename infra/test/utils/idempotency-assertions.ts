// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Shared assertions for the idempotency-table wiring (utils/idempotency.ts).
 * Not a test file itself — imported by the per-stack tests.
 */

import { Template } from "aws-cdk-lib/assertions";
import {
  IDEMPOTENCY_TABLE_ACTIONS,
  IDEMPOTENCY_TABLE_SSM_SUFFIX,
} from "../../lib/utils/idempotency";

type CfnResource = {
  Type: string;
  Properties?: Record<string, unknown>;
};

/** Logical id of the SSM-backed CFN parameter carrying the table name. */
function idempotencyParamId(template: Template): string {
  const params = template.toJSON().Parameters ?? {};
  const ids = Object.entries(params)
    .filter(([, p]) =>
      String((p as { Default?: unknown }).Default ?? "").endsWith(
        IDEMPOTENCY_TABLE_SSM_SUFFIX,
      ),
    )
    .map(([id]) => id);
  expect(ids).toHaveLength(1);
  return ids[0];
}

function lambdaRoleIds(
  resources: Record<string, CfnResource>,
  functionNameSuffixes: readonly string[],
): Map<string, string> {
  const out = new Map<string, string>();
  for (const r of Object.values(resources)) {
    if (r.Type !== "AWS::Lambda::Function") continue;
    const name = String(r.Properties?.FunctionName ?? "");
    const suffix = functionNameSuffixes.find((s) => name.endsWith(s));
    if (!suffix) continue;
    const role = r.Properties?.Role as
      | { "Fn::GetAtt"?: [string, string] }
      | undefined;
    const getAtt = role?.["Fn::GetAtt"];
    // A matched Lambda must take its role from a same-stack IAM role, or the
    // policy check below has nothing to compare against. The function name is
    // in the asserted object so a failure says which Lambda broke the rule.
    expect({ functionName: name, roleGetAtt: getAtt }).toEqual({
      functionName: name,
      roleGetAtt: [expect.any(String), "Arn"],
    });
    if (getAtt) out.set(suffix, getAtt[0]);
  }
  return out;
}

/**
 * Assert that exactly the Lambdas whose FunctionName ends with one of
 * `functionNameSuffixes` get IDEMPOTENCY_TABLE and Get/Put/Update/DeleteItem
 * on the idempotency table — and no other Lambda in the stack touches it.
 */
export function expectIdempotencyWiring(
  template: Template,
  functionNameSuffixes: readonly string[],
): void {
  const paramId = idempotencyParamId(template);
  const resources = template.toJSON().Resources as Record<string, CfnResource>;
  const refJson = JSON.stringify({ Ref: paramId });

  // 1. Env var on exactly the expected Lambdas.
  const withEnv: string[] = [];
  for (const r of Object.values(resources)) {
    if (r.Type !== "AWS::Lambda::Function") continue;
    const env = (r.Properties?.Environment as {
      Variables?: Record<string, unknown>;
    })?.Variables;
    if (env?.IDEMPOTENCY_TABLE !== undefined) {
      expect(env.IDEMPOTENCY_TABLE).toEqual({ Ref: paramId });
      withEnv.push(String(r.Properties?.FunctionName ?? ""));
    }
  }
  expect(withEnv).toHaveLength(functionNameSuffixes.length);
  for (const suffix of functionNameSuffixes) {
    expect(withEnv.some((n) => n.endsWith(suffix))).toBe(true);
  }

  // 2. IAM: only the expected roles reference the table, with only the 4 actions.
  const roles = lambdaRoleIds(resources, functionNameSuffixes);
  expect(roles.size).toBe(functionNameSuffixes.length);
  const expectedRoleIds = new Set(roles.values());
  const grantedRoleIds = new Set<string>();
  for (const r of Object.values(resources)) {
    if (r.Type !== "AWS::IAM::Policy") continue;
    const statements = (
      r.Properties?.PolicyDocument as { Statement: Record<string, unknown>[] }
    ).Statement;
    const touching = statements.filter((s) =>
      JSON.stringify(s.Resource).includes(refJson),
    );
    if (touching.length === 0) continue;
    const policyRoles = (r.Properties?.Roles as { Ref: string }[]).map(
      (x) => x.Ref,
    );
    for (const roleId of policyRoles) {
      expect(expectedRoleIds.has(roleId)).toBe(true);
      grantedRoleIds.add(roleId);
    }
    for (const s of touching) {
      expect(s.Effect).toBe("Allow");
      expect([...(s.Action as string[])].sort()).toEqual(
        [...IDEMPOTENCY_TABLE_ACTIONS].sort(),
      );
      // Table ARN only — no index or wildcard resources.
      const resourceList = Array.isArray(s.Resource)
        ? s.Resource
        : [s.Resource];
      expect(resourceList).toHaveLength(1);
      expect(JSON.stringify(resourceList[0])).not.toContain("*");
      expect(JSON.stringify(resourceList[0])).toContain(":table/");
    }
  }
  expect(grantedRoleIds).toEqual(expectedRoleIds);
}
