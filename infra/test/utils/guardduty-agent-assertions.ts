// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Template } from "aws-cdk-lib/assertions";
import { GUARDDUTY_FARGATE_AGENT_REPOSITORY } from "../../lib/utils/guardduty-agent";

const PULL_ACTIONS = [
  "ecr:BatchCheckLayerAvailability",
  "ecr:GetDownloadUrlForLayer",
  "ecr:BatchGetImage",
];

/** Renders a CFN string, Ref or Fn::Join as one string, Refs as `${Name}`. */
function renderArn(x: unknown): string {
  if (typeof x === "string") return x;
  const obj = x as { Ref?: string; "Fn::Join"?: [string, unknown[]] };
  if (obj?.Ref) return `\${${obj.Ref}}`;
  if (obj?.["Fn::Join"]) {
    const [sep, parts] = obj["Fn::Join"];
    return parts.map(renderArn).join(sep);
  }
  return JSON.stringify(x);
}

const toArray = (x: unknown): unknown[] =>
  Array.isArray(x) ? x : x === undefined || x === null ? [] : [x];

interface Statement {
  Effect?: string;
  Action?: unknown;
  Resource?: unknown;
}

/**
 * Every task definition's execution role may pull GuardDuty's Fargate agent
 * (Runtime Monitoring injects it as a sidecar), and the token is granted.
 * `region` is the stack's Region: `${AWS::Region}` for an env-agnostic
 * stack, the literal Region when the stack has an env.
 */
export function expectEveryTaskCanPullGuardDutyAgent(
  template: Template,
  expectedTaskDefs: number,
  region: string,
): void {
  const agentRepositoryArn = `arn:\${AWS::Partition}:ecr:${region}:*:repository/${GUARDDUTY_FARGATE_AGENT_REPOSITORY}`;
  const taskDefs = Object.values(
    template.findResources("AWS::ECS::TaskDefinition"),
  );
  expect(taskDefs).toHaveLength(expectedTaskDefs);
  const policies = Object.values(template.findResources("AWS::IAM::Policy"));

  for (const taskDef of taskDefs) {
    const role = taskDef.Properties.ExecutionRoleArn?.["Fn::GetAtt"]?.[0];
    expect(role).toBeDefined();
    const statements: Statement[] = policies
      .filter((p) =>
        toArray(p.Properties.Roles).some(
          (r) => JSON.stringify(r) === JSON.stringify({ Ref: role }),
        ),
      )
      .flatMap((p) => toArray(p.Properties.PolicyDocument.Statement))
      .filter((s): s is Statement => typeof s === "object" && s !== null);

    // Exact ARN: the stack's own Region, any account, this repo only.
    const agentPull = statements.find(
      (s) =>
        s.Effect === "Allow" &&
        JSON.stringify(toArray(s.Resource).map(renderArn)) ===
          JSON.stringify([agentRepositoryArn]),
    );
    expect(agentPull).toBeDefined();
    expect(toArray(agentPull!.Action)).toEqual(
      expect.arrayContaining(PULL_ACTIONS),
    );
    expect(
      statements.some(
        (s) =>
          s.Effect === "Allow" &&
          toArray(s.Action).includes("ecr:GetAuthorizationToken") &&
          s.Resource === "*",
      ),
    ).toBe(true);
  }
}
