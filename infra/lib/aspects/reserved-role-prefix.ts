// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { IConstruct } from "constructs";

/**
 * Role-name segment reserved for CUSTOMER-owned datasource-access roles (the
 * ones COA assumes to reach a source credential), i.e.
 * `{prefix}-{env}-datasource-access-*`. Every `sts:AssumeRole` grant in this app
 * interpolates it, so the grant scope and the aspect below cannot drift apart.
 */
export const RESERVED_DATASOURCE_ROLE_SEGMENT = "datasource-access-";

/**
 * Fail the synth if a COA-internal role falls under the reserved
 * datasource-access prefix — by its name, its IAM path, or the two together.
 *
 * The assume grants carry no account restriction (the customer's role may live
 * in any account, this deployment's own included), so the ARN suffix —
 * everything after `role/`, i.e. `path + name` — is the entire boundary: a
 * COA-internal role under the prefix would be assumable by the discovery role,
 * the enrichment task, the sources API and the Databricks connector alike.
 *
 * Throws rather than raising an error annotation: an annotation is only fatal
 * when the caller synthesises with validation on, and `Template.fromStack` is
 * not that caller.
 */
export class ReservedRoleNamePrefix implements cdk.IAspect {
  /** @param reservedPrefix fully prefixed, e.g. `coa-dev-datasource-access-` */
  constructor(private readonly reservedPrefix: string) {}

  public visit(node: IConstruct): void {
    if (!(node instanceof iam.CfnRole)) {
      return;
    }
    const stack = cdk.Stack.of(node);

    // `node.path` is the IAM path, not the construct path (`node.node.path`).
    // Read UNCONDITIONALLY: most roles here leave `roleName` to CloudFormation,
    // so a name-first check that returns early on a generated name would skip
    // the path for exactly the common case. Paths always end in `/`, so the
    // prefix cannot straddle the path/name boundary.
    const path: unknown = stack.resolve(node.path);
    const pathPart = (typeof path === "string" ? path : "/").replace(/^\//, "");

    const roleName: unknown = stack.resolve(node.roleName);

    // A non-string name is absent (CloudFormation generates one that cannot
    // start with the prefix) or token-valued. `*` stands in for a fully
    // tokenised name, but a partial token resolves to an `Fn::Join` whose
    // literal head is inside the grant, so the resolved JSON is searched for the
    // prefix anywhere — blunt on purpose: a head-only check would miss `Fn::Sub`.
    const namePart = typeof roleName === "string" ? roleName : "*";
    const arnSuffix = `${pathPart}${namePart}`;
    const tokenCarriesPrefix =
      typeof roleName !== "string" &&
      roleName !== undefined &&
      JSON.stringify(roleName).includes(this.reservedPrefix);

    if (!arnSuffix.startsWith(this.reservedPrefix) && !tokenCarriesPrefix) {
      return;
    }
    throw new Error(
      `Role ${
        tokenCarriesPrefix
          ? `whose unresolved name ${JSON.stringify(roleName)} contains`
          : `"${arnSuffix}" falls under`
      } the reserved datasource-access prefix "${this.reservedPrefix}" ` +
        `(${node.node.path}). Every sts:AssumeRole grant in this app is scoped to ` +
        `arn:aws:iam::*:role/${this.reservedPrefix}* with no account restriction, so ` +
        `a COA-internal role here would be assumable by the discovery role, the ` +
        `enrichment task, the sources API and the Databricks connector. Rename it, or ` +
        `move it off that IAM path.`,
    );
  }
}
