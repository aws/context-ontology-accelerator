// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template, Match } from "aws-cdk-lib/assertions";
import { McpStack } from "../../lib/stacks/services/mcp-stack";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../../lib/constants";

jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const TEST_CONTEXT = {
  resource_prefix: DEFAULT_RESOURCE_PREFIX,
  env: DEFAULT_ENV,
  context_manager_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-dev:latest",
  "aws:cdk:bundling-stacks": [],
};

describe("McpStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });

    const depStack = new cdk.Stack(app, "DepStack", {
      env: { account: "123456789012", region: "us-east-1" },
    });
    const vpc = new ec2.Vpc(depStack, "Vpc", { maxAzs: 2 });
    const mkTable = (id: string) =>
      new dynamodb.Table(depStack, id, {
        partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      });

    const stack = new McpStack(app, "TestMcp", {
      env: { account: "123456789012", region: "us-east-1" },
      vpc,
      rolesTable: mkTable("Roles"),
      resourceRoleMappingsTable: mkTable("RRM"),
    });

    template = Template.fromStack(stack);
  });

  it("creates an AgentCore Runtime with MCP protocol", () => {
    template.hasResourceProperties(
      "AWS::BedrockAgentCore::Runtime",
      Match.objectLike({
        ProtocolConfiguration: "MCP",
      }),
    );
  });

  it("sets the runtime name with correct pattern", () => {
    template.hasResourceProperties(
      "AWS::BedrockAgentCore::Runtime",
      Match.objectLike({
        AgentRuntimeName: "coa_dev_mcp_server",
      }),
    );
  });

  it("configures JWT authorizer", () => {
    template.hasResourceProperties(
      "AWS::BedrockAgentCore::Runtime",
      Match.objectLike({
        AuthorizerConfiguration: Match.objectLike({
          CustomJWTAuthorizer: Match.objectLike({}),
        }),
      }),
    );
  });

  it("passes Authorization in allowlisted headers", () => {
    template.hasResourceProperties(
      "AWS::BedrockAgentCore::Runtime",
      Match.objectLike({
        RequestHeaderConfiguration: {
          RequestHeaderAllowlist: ["Authorization"],
        },
      }),
    );
  });

  it("has tracing disabled (pending XRay destination setup)", () => {
    // tracingEnabled is currently false (see TODO in mcp-stack.ts).
    // This test documents the current state and will need updating
    // when tracing is enabled.
    template.hasResourceProperties(
      "AWS::BedrockAgentCore::Runtime",
      Match.objectLike({
        AgentRuntimeName: "coa_dev_mcp_server",
      }),
    );
  });

  it("sets required environment variables (thin proxy)", () => {
    template.hasResourceProperties(
      "AWS::BedrockAgentCore::Runtime",
      Match.objectLike({
        EnvironmentVariables: Match.objectLike({
          ROLES_TABLE_NAME: Match.anyValue(),
          RRM_TABLE_NAME: Match.anyValue(),
          CM_RUNTIME_ARN: Match.anyValue(),
          SCL_MCP_MODE: "true",
        }),
      }),
    );
  });

  it("sets JWT issuer/client env vars for independent signature verification", () => {
    // Defense-in-depth: claims.py verifies signatures itself using the same
    // issuer/audience the AuthorizerConfiguration below trusts, rather than
    // assuming AgentCore's platform-level check is the only line of defense.
    // Values are SSM dynamic references (resolved from the auth stack's
    // /issuer and /mcp-client-id parameters at deploy time), not literals.
    template.hasResourceProperties(
      "AWS::BedrockAgentCore::Runtime",
      Match.objectLike({
        EnvironmentVariables: Match.objectLike({
          JWT_ISSUER_URL: Match.anyValue(),
          JWT_CLIENT_ID: Match.anyValue(),
        }),
      }),
    );
  });

  it("resolves GROUP_CLAIM_NAME from SSM (not a hardcoded Cognito default)", () => {
    // Regression: this used to be a hardcoded DEFAULT_GROUP_CLAIM
    // ("cognito:groups") passed from app.ts regardless of idpType, which
    // silently broke group-based role resolution on the direct-OIDC path
    // (external IdPs configure their own claim name, e.g. plain "groups").
    // Must be an SSM dynamic reference resolved from
    // /authentication-group-token-name (written by idp-authentication-stack.ts
    // on both the Cognito/SAML and OIDC paths), not a literal string.
    template.hasResourceProperties(
      "AWS::BedrockAgentCore::Runtime",
      Match.objectLike({
        EnvironmentVariables: Match.objectLike({
          GROUP_CLAIM_NAME: Match.anyValue(),
        }),
      }),
    );
  });

  it("does NOT set data-plane env vars (delegated to CM)", () => {
    template.hasResourceProperties(
      "AWS::BedrockAgentCore::Runtime",
      Match.objectLike({
        EnvironmentVariables: Match.not(
          Match.objectLike({
            NEPTUNE_ENDPOINT: Match.anyValue(),
          }),
        ),
      }),
    );
  });

  it("creates a minimal security group (HTTPS only)", () => {
    template.hasResourceProperties("AWS::EC2::SecurityGroup", {
      GroupDescription: "AgentCore Runtime - MCP Server (thin proxy)",
      SecurityGroupEgress: Match.arrayWith([
        Match.objectLike({
          IpProtocol: "tcp",
          FromPort: 443,
          ToPort: 443,
        }),
      ]),
    });
  });

  it("does NOT open Neptune port (delegated to CM)", () => {
    template.hasResourceProperties("AWS::EC2::SecurityGroup", {
      SecurityGroupEgress: Match.not(
        Match.arrayWith([
          Match.objectLike({
            FromPort: 8182,
          }),
        ]),
      ),
    });
  });

  it("does NOT open DB ports (no direct DB access)", () => {
    template.hasResourceProperties("AWS::EC2::SecurityGroup", {
      SecurityGroupEgress: Match.not(
        Match.arrayWith([
          Match.objectLike({
            FromPort: 5432,
          }),
        ]),
      ),
    });
  });

  it("does NOT grant Neptune access (delegated to CM)", () => {
    const policies = template.findResources("AWS::IAM::Policy");
    const allStatements = Object.values(policies).flatMap(
      (p: any) => p.Properties.PolicyDocument.Statement,
    );
    const neptuneActions = allStatements
      .flatMap((s: any) => (Array.isArray(s.Action) ? s.Action : [s.Action]))
      .filter((a: string) => a?.startsWith("neptune-db:"));
    expect(neptuneActions).toEqual([]);
  });

  it("grants InvokeAgentRuntime for CM delegation", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Allow",
            Action: Match.arrayWith(["bedrock-agentcore:InvokeAgentRuntime"]),
          }),
        ]),
      },
    });
  });

  it("grants DynamoDB read for roles/RRM only", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Allow",
            Action: Match.arrayWith([
              "dynamodb:GetItem",
              "dynamodb:Query",
              "dynamodb:BatchGetItem",
            ]),
          }),
        ]),
      },
    });
  });

  it("does NOT grant Bedrock invoke (delegated to CM)", () => {
    const policies = template.findResources("AWS::IAM::Policy");
    const allStatements = Object.values(policies).flatMap(
      (p: any) => p.Properties.PolicyDocument.Statement,
    );
    const bedrockActions = allStatements
      .flatMap((s: any) => (Array.isArray(s.Action) ? s.Action : [s.Action]))
      .filter((a: string) => a?.startsWith("bedrock:"));
    expect(bedrockActions).toEqual([]);
  });

  it("does NOT grant Secrets Manager access (delegated to CM)", () => {
    const policies = template.findResources("AWS::IAM::Policy");
    const allStatements = Object.values(policies).flatMap(
      (p: any) => p.Properties.PolicyDocument.Statement,
    );
    const smActions = allStatements
      .flatMap((s: any) => (Array.isArray(s.Action) ? s.Action : [s.Action]))
      .filter((a: string) => a?.startsWith("secretsmanager:"));
    expect(smActions).toEqual([]);
  });
  it("publishes MCP runtime ARN to SSM", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: "/coa/mcp/runtime-arn",
    });
  });

  it("publishes MCP runtime role ARN to SSM", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: "/coa/mcp/runtime-role-arn",
    });
  });

  // Deliberately NOT narrowed: the role reads across the whole `${ssmPrefix}` tree
  // (`/issuer`, `/userpool-client-id`, the Context Manager runtime ARN), so scoping
  // it to a subtree presents as a runtime AccessDenied that reads as a config error.
  // Accepted residual: the Databricks connector parameters are readable here too,
  // a metadata side channel over non-secret values.
  it("keeps its broad ssm:GetParameter on the whole prefix tree", () => {
    const reads = Object.values(template.findResources("AWS::IAM::Policy"))
      .flatMap(
        (p: any) =>
          p.Properties.PolicyDocument.Statement as Record<string, unknown>[],
      )
      .filter((st) => {
        const actions = Array.isArray(st.Action) ? st.Action : [st.Action];
        return st.Effect === "Allow" && actions.includes("ssm:GetParameter");
      });
    expect(
      reads.some((st) => {
        const resources = Array.isArray(st.Resource)
          ? st.Resource
          : [st.Resource];
        return resources.includes(
          "arn:aws:ssm:us-east-1:123456789012:parameter/coa/*",
        );
      }),
    ).toBe(true);
  });
});
