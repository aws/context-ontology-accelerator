// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { IConstruct } from "constructs";

/**
 * Role-name segment reserved for CUSTOMER-owned datasource-access roles, i.e. the roles COA assumes
 * to reach a source's credential; the reserved name is `{resourcePrefix}datasource-access-`.
 * Duplicated from `infra/lib/aspects/reserved-role-prefix.ts` because `connectors/` is its own pnpm
 * workspace and must build when copied out of the repository. Nothing makes the two agree.
 */
export const RESERVED_DATASOURCE_ROLE_SEGMENT = "datasource-access-";

/**
 * Fails the synth if a COA-internal role falls under the reserved datasource-access prefix, by its
 * name, its IAM path, or the two together. `AssumeRoleCoaManaged` has no account restriction, so the
 * ARN suffix (`path + name`) is the whole boundary and a role of COA's own inside it would be
 * assumable by this connector. Throws rather than annotating, because an annotation is only fatal
 * when the caller synthesises with validation on and `Template.fromStack` does not.
 */
export class ReservedRoleNamePrefix implements cdk.IAspect {
  /** @param reservedPrefix the fully prefixed reserved name, e.g. `coa-dev-datasource-access-`. */
  constructor(private readonly reservedPrefix: string) {}

  public visit(node: IConstruct): void {
    if (!(node instanceof iam.CfnRole)) {
      return;
    }
    const stack = cdk.Stack.of(node);

    // `node.path` is the IAM path property, not the construct path. IAM paths start and end with
    // `/`, so a reserved prefix cannot straddle the path/name boundary.
    const path: unknown = stack.resolve(node.path);
    const pathPart = (typeof path === "string" ? path : "/").replace(/^\//, "");

    const roleName: unknown = stack.resolve(node.roleName);

    // A fully tokenised name is undecidable, so `*` stands in; a partially tokenised one can still
    // carry a literal head inside the grant, hence the blunt search of the whole resolved structure
    // (covers `Fn::Join`, `Fn::Sub` and anything else).
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
        `(${node.node.path}). This connector's sts:AssumeRole grant is scoped to ` +
        `arn:aws:iam::*:role/${this.reservedPrefix}* with no account restriction, so a role of ` +
        `COA's own here would be assumable by the connector. Rename it, or move it off that IAM path.`,
    );
  }
}
