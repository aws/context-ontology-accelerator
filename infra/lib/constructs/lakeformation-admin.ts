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
  /**
   * ARN of a role that is ALREADY a Lake Formation data-lake admin, to run the
   * onEvent Lambda as. Brownfield bootstrap escape hatch.
   *
   * `PutDataLakeSettings` may only be called by an existing data-lake admin once
   * an account has any admins, and the caller is this construct's Lambda role.
   * Left unset, that role is created by this deployment and auto-named, so the
   * documented "register it as an LF admin before the first deploy" is circular:
   * there is nothing to register until the deploy that needs it has already run,
   * and a rollback deletes it and re-creates it under a new name on retry.
   *
   * Supplying a role you already own and have already registered breaks the
   * cycle — registration happens once, out-of-band, against a stable ARN,
   * before any COA deployment. Greenfield accounts (zero admins) do not need
   * this: LF has no admin to enforce, so the auto-created role self-bootstraps.
   *
   * The role must be assumable by `lambda.amazonaws.com`. This construct
   * attaches the permissions it needs (LF settings read/write, one SSM
   * parameter read, CloudWatch Logs) to it, so it is imported as mutable.
   */
  readonly existingAdminRoleArn?: string;
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
 * to already be an LF data-lake admin once any admins exist. Greenfield accounts
 * (no admins) self-bootstrap. Accounts that already have admins must supply
 * {@link LakeFormationAdminProps.existingAdminRoleArn} — see that prop for why
 * registering the auto-created role "before first deploy" cannot work.
 */
export class LakeFormationAdmin extends Construct {
  /** Execution role of the onEvent Lambda — must be an LF admin to write settings. */
  public readonly adminLambdaRole: iam.IRole;

  constructor(scope: Construct, id: string, props: LakeFormationAdminProps) {
    super(scope, id);

    // Imported mutable (the default) so the addToRolePolicy calls below attach
    // to it: a caller-supplied role is expected to be an LF admin, not to have
    // guessed the exact IAM statements this handler needs.
    //
    // Rejected at synth if not same-account, because CDK silently imports a
    // cross-account role as IMMUTABLE — every policy attachment below would
    // become a no-op and the deploy would instead fail at runtime on an
    // unrelated-looking AccessDenied against SSM. Lambda cannot use a
    // cross-account execution role anyway, so nothing valid is being rejected.
    if (
      props.existingAdminRoleArn &&
      !cdk.Token.isUnresolved(props.existingAdminRoleArn)
    ) {
      const { account } = cdk.Stack.of(this);
      const arnAccount = cdk.Arn.split(
        props.existingAdminRoleArn,
        cdk.ArnFormat.SLASH_RESOURCE_NAME,
      ).account;
      if (!cdk.Token.isUnresolved(account) && arnAccount !== account) {
        throw new Error(
          `existingAdminRoleArn must be a role in this account (${account}), got ` +
            `${props.existingAdminRoleArn}. Lambda cannot assume a cross-account ` +
            "execution role, and CDK would import it as immutable.",
        );
      }
    }
    const suppliedRole = props.existingAdminRoleArn
      ? iam.Role.fromRoleArn(
          this,
          "SuppliedAdminRole",
          props.existingAdminRoleArn,
        )
      : undefined;

    const onEvent = new lambda.Function(this, "OnEvent", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.minutes(2),
      code: lambda.Code.fromAsset(
        path.join(__dirname, "../lambdas/lakeformation-admin"),
      ),
      ...(suppliedRole && { role: suppliedRole }),
    });
    this.adminLambdaRole = onEvent.role!;

    // Passing an explicit `role` suppresses CDK's default basic-execution
    // attachment, so the handler's warning logs (the only signal on a
    // best-effort Delete failure) would silently go nowhere.
    //
    // Granted as an inline statement, NOT via addManagedPolicy: on an imported
    // role the latter is a silent no-op — CDK emits no resource for it, because
    // it does not own the role and cannot rewrite its ManagedPolicyArns. This
    // path emits an AWS::IAM::Policy attached to the role by name, which does.
    if (suppliedRole) {
      suppliedRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
          ],
          resources: [
            cdk.Stack.of(this).formatArn({
              service: "logs",
              resource: "log-group",
              resourceName: "/aws/lambda/*",
              arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
            }),
          ],
        }),
      );
    }

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
