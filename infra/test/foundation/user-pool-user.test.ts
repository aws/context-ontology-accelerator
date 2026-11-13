// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import { Template, Match } from "aws-cdk-lib/assertions";
import { UserPoolUser } from "../../lib/constructs/user-pool-user";

const TEST_CONTEXT = { resource_prefix: "coa", env: "dev" };

function buildStack(
  userDetails = {
    username: "admin@example.com",
    email: "admin@example.com",
    group: "Admin",
  },
) {
  const app = new cdk.App({ context: TEST_CONTEXT });
  const stack = new cdk.Stack(app, "TestStack");
  const userPool = new cognito.UserPool(stack, "Pool", {
    removalPolicy: cdk.RemovalPolicy.DESTROY,
  });
  new UserPoolUser(stack, "AdminUser", { userPool, userDetails });
  return Template.fromStack(stack);
}

describe("UserPoolUser construct", () => {
  let template: Template;

  beforeAll(() => {
    template = buildStack();
  });

  test("does NOT create a Secrets Manager secret", () => {
    template.resourceCountIs("AWS::SecretsManager::Secret", 0);
  });

  test("creates a Lambda function with prefixed name and Node.js 22 runtime", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "coa-dev-admin-user-cr",
      Runtime: "nodejs22.x",
      Timeout: 60,
    });
  });

  test("grants Lambda Cognito admin permissions (no AdminSetUserPassword)", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              "cognito-idp:AdminCreateUser",
              "cognito-idp:AdminDeleteUser",
              "cognito-idp:AdminAddUserToGroup",
              "cognito-idp:CreateGroup",
              "cognito-idp:DeleteGroup",
            ]),
            Effect: "Allow",
          }),
        ]),
      },
    });
  });

  test("custom resource passes Username, Email, Group directly", () => {
    template.hasResourceProperties("AWS::CloudFormation::CustomResource", {
      UserPoolId: Match.anyValue(),
      Username: "admin@example.com",
      Email: "admin@example.com",
      Group: "Admin",
      ForceUpdate: Match.stringLikeRegexp("^[a-f0-9]{64}$"),
    });
  });
});

describe("UserPoolUser with custom details", () => {
  test("uses provided user details in custom resource properties", () => {
    const template = buildStack({
      username: "superadmin@corp.com",
      email: "superadmin@corp.com",
      group: "SuperAdmins",
    });

    template.hasResourceProperties("AWS::CloudFormation::CustomResource", {
      Username: "superadmin@corp.com",
      Email: "superadmin@corp.com",
      Group: "SuperAdmins",
    });
  });
});

describe("IdpAuthenticationStack provisions admin user", () => {
  test("Cognito mode creates UserPoolUser resources (no Secrets Manager)", () => {
    const { IdpAuthenticationStack } = require("../../lib/stacks/foundation");
    const app = new cdk.App({ context: TEST_CONTEXT });
    const template = Template.fromStack(
      new IdpAuthenticationStack(app, "AuthWithUser"),
    );

    template.resourceCountIs("AWS::SecretsManager::Secret", 0);

    template.hasResourceProperties("AWS::CloudFormation::CustomResource", {
      Username: Match.anyValue(),
      Email: Match.anyValue(),
      Group: Match.anyValue(),
    });

    template.resourceCountIs("AWS::CloudFormation::CustomResource", 1);
  });

  test("OIDC mode does NOT create UserPoolUser resources", () => {
    const { IdpAuthenticationStack } = require("../../lib/stacks/foundation");
    const { IdpType } = require("../../lib/types");
    const app = new cdk.App({ context: TEST_CONTEXT });
    const template = Template.fromStack(
      new IdpAuthenticationStack(app, "AuthOidc", {
        idpType: IdpType.OIDC,
        oidcSettings: {
          issuerUrl: "https://idp.example.com",
          clientId: "abc123",
        },
      }),
    );

    template.resourceCountIs("AWS::SecretsManager::Secret", 0);
    template.resourceCountIs("AWS::CloudFormation::CustomResource", 0);
  });
});
