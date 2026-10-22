// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import { resolveContext } from "../context";

/** SSM path (under the deployment's ssmPrefix) holding the idempotency table name. Written by AuthnzStack. */
export const IDEMPOTENCY_TABLE_SSM_SUFFIX = "/authnz/idempotency-table-name";

/** Env var the Python helper (coa_common.idempotency) reads the table name from. */
export const IDEMPOTENCY_TABLE_ENV = "IDEMPOTENCY_TABLE";

/** The only actions the idempotency helper performs. */
export const IDEMPOTENCY_TABLE_ACTIONS: readonly string[] = [
  "dynamodb:GetItem",
  "dynamodb:PutItem",
  "dynamodb:UpdateItem",
  "dynamodb:DeleteItem",
];

/**
 * Wire a create-operation Lambda to the shared idempotency table.
 *
 * Reads the table name from SSM (written by AuthnzStack — the consuming stack
 * must `addDependency(authnz)` in bin/app.ts), sets `IDEMPOTENCY_TABLE`, and
 * grants exactly Get/Put/Update/DeleteItem on that table and nothing else.
 */
export function grantIdempotencyTable(
  scope: Construct,
  fn: lambda.Function,
): void {
  const { ssmPrefix } = resolveContext(scope.node);
  const tableName = ssm.StringParameter.valueForStringParameter(
    scope,
    `${ssmPrefix}${IDEMPOTENCY_TABLE_SSM_SUFFIX}`,
  );
  fn.addEnvironment(IDEMPOTENCY_TABLE_ENV, tableName);
  fn.addToRolePolicy(
    new iam.PolicyStatement({
      sid: "IdempotencyRecords",
      actions: [...IDEMPOTENCY_TABLE_ACTIONS],
      resources: [
        cdk.Stack.of(scope).formatArn({
          service: "dynamodb",
          resource: "table",
          resourceName: tableName,
        }),
      ],
    }),
  );
}
