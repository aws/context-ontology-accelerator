// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as fs from "fs";
import * as path from "path";

/**
 * AWS Solutions CloudFormation metrics identity, matching the main COA app (`infra/lib/solution.ts`):
 * every stack description starts with `(SO0376)`, which is how AWS Solutions counts stack launches.
 *
 * The version is the solution release from the repo-root `VERSION`, not this package's own version,
 * so the connector's stacks report the same release as the rest of COA.
 */
export const SOLUTION_ID = "SO0376";
export const SOLUTION_NAME = "Context Ontology Accelerator";

const VERSION_FILE = path.join(__dirname, "..", "..", "..", "..", "VERSION");

export const SOLUTION_VERSION = `v${fs.readFileSync(VERSION_FILE, "utf8").trim()}`;

/** CloudFormation template description for the stack `stackId`. */
export function solutionStackDescription(stackId: string): string {
  return `(${SOLUTION_ID}) - ${SOLUTION_NAME} - ${stackId}. Version ${SOLUTION_VERSION}`;
}
