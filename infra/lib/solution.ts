// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * AWS Solutions CloudFormation metrics identity.
 *
 * Every stack description starts with `(SO0376)`, which is how AWS Solutions
 * counts stack launches and deletions. (Service API usage is attributed
 * separately, by the User-Agent token in `coa_common.aws_user_agent`.)
 *
 * The version is this package's `version`, which `scripts/sync_version.py`
 * keeps equal to the repo-root `VERSION`.
 */
import { version } from "../package.json";

export const SOLUTION_ID = "SO0376";
export const SOLUTION_NAME = "Context Ontology Accelerator";
export const SOLUTION_VERSION = `v${version}`;

/** CloudFormation template description for the stack `stackId`. */
export function solutionStackDescription(stackId: string): string {
  return `(${SOLUTION_ID}) - ${SOLUTION_NAME} - ${stackId}. Version ${SOLUTION_VERSION}`;
}
