// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Provider } from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";

export interface LakeFormationAdminProps {
  /** SSM parameter name holding the IAM role ARN to register as an LF data-lake admin. */
  readonly roleArnSsmParameterName: string;
  /**
   * The role ARN the SSM parameter resolves to.
   *
   * Passed as a custom-resource property purely so a change to the *value*
   * produces a property diff. With only the parameter *name* as a property, a
   * deployment that repointed the parameter at a different role left the custom
   * resource untouched (no diff → no Update → the new role never registered);
   * the handler still reads the authoritative value from SSM at runtime.
   */
  readonly roleArn: string;
}

/**
 * Non-destructively registers an IAM role as a Lake Formation data-lake admin.
 *
 * Reads the role ARN from SSM, fetches the current settings
 * (`GetDataLakeSettings`), appends the role to `DataLakeAdmins` if absent, and
 * writes the full settings back (`PutDataLakeSettings`) — preserving existing
 * admins and other settings. Removes the role on stack deletion (best-effort).
 *
 * NOTE: `PutDataLakeSettings` requires the caller (this construct's Lambda role)
 * to already be an LF data-lake admin once any admins exist. In a greenfield
 * account (no admins) it self-bootstraps; in an account that already has admins,
 * add `adminLambdaRole` as an LF admin once, out-of-band, before first deploy.
 */
export class LakeFormationAdmin extends Construct {
  /** Execution role of the onEvent Lambda — must be an LF admin to write settings. */
  public readonly adminLambdaRole: iam.IRole;

  constructor(scope: Construct, id: string, props: LakeFormationAdminProps) {
    super(scope, id);

    const onEvent = new lambda.Function(this, "OnEvent", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.minutes(2),
      code: lambda.Code.fromAsset(
        path.join(__dirname, "../lambdas/lakeformation-admin"),
      ),
    });
    this.adminLambdaRole = onEvent.role!;

    // LF settings APIs do not support resource-level scoping.
    onEvent.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "lakeformation:GetDataLakeSettings",
          "lakeformation:PutDataLakeSettings",
        ],
        resources: ["*"],
      }),
    );
    onEvent.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [
          cdk.Stack.of(this).formatArn({
            service: "ssm",
            resource: "parameter",
            resourceName: props.roleArnSsmParameterName.replace(/^\//, ""),
          }),
        ],
      }),
    );

    const provider = new Provider(this, "Provider", {
      onEventHandler: onEvent,
    });
    new cdk.CustomResource(this, "Resource", {
      serviceToken: provider.serviceToken,
      properties: {
        RoleArnSsmParameterName: props.roleArnSsmParameterName,
        // Value-carrying property so repointing the parameter triggers an
        // Update — see LakeFormationAdminProps.roleArn. The handler reads the
        // authoritative ARN from SSM; this is only a change detector.
        RoleArn: props.roleArn,
      },
    });
  }
}
