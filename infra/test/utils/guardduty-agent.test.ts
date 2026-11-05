// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as fs from "fs";
import * as path from "path";

/**
 * Every Fargate task definition in the app grants the GuardDuty agent pull.
 *
 * The per-stack tests pin their stack's task-definition count, so they miss a
 * task definition added in a new stack. `infra/bin/app.ts` can't be synthesised
 * in a unit test, so this reads the source instead and covers every stack.
 */
const INFRA_LIB = path.join(__dirname, "..", "..", "lib");

function sourceFiles(directory: string): string[] {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      return sourceFiles(full);
    }
    return entry.isFile() && entry.name.endsWith(".ts") ? [full] : [];
  });
}

function count(source: string, pattern: RegExp): number {
  return (source.match(pattern) ?? []).length;
}

describe("grantGuardDutyAgentPull", () => {
  it("is called once for every Fargate task definition in infra/lib", () => {
    const perFile = sourceFiles(INFRA_LIB).map((file) => {
      const source = fs.readFileSync(file, "utf8");
      return {
        file: path.relative(INFRA_LIB, file),
        taskDefs: count(source, /new\s+(?:ecs\.)?FargateTaskDefinition\s*\(/g),
        grants: count(source, /^\s*grantGuardDutyAgentPull\(/gm),
      };
    });

    // Guards against the scan silently matching nothing.
    const total = perFile.reduce((sum, f) => sum + f.taskDefs, 0);
    expect(total).toBeGreaterThanOrEqual(4);
    expect(
      perFile
        .filter((f) => f.taskDefs > 0 || f.grants > 0)
        .filter((f) => f.grants !== f.taskDefs),
    ).toEqual([]);
  });
});
