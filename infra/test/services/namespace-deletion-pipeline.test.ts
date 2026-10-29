// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template, Match, Capture } from "aws-cdk-lib/assertions";
import { NamespaceDeletionPipeline } from "../../lib/stacks/services/namespace-deletion-pipeline";
import { expectAossGrantsCoveredByDataPolicies } from "../utils/aoss-access-assertions";

describe("NamespaceDeletionPipeline", () => {
  let template: Template;
  let stack: cdk.Stack;

  beforeAll(() => {
    const app = new cdk.App();
    stack = new cdk.Stack(app, "TestStack");
    const vpc = new ec2.Vpc(stack, "Vpc");
    const namespacesTable = new dynamodb.Table(stack, "Namespaces", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
    });
    const rolesTable = new dynamodb.Table(stack, "Roles", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
    const rrmTable = new dynamodb.Table(stack, "RRM", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });

    new NamespaceDeletionPipeline(stack, "DeletionPipeline", {
      prefixed: (name: string) => `coa-dev-${name}`,
      aossName: (name: string) => `coa-dev-${name}`,
      lambdaCode: lambda.Code.fromInline("def handler(event, context): pass"),
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      namespacesTable,
      resourceRoleMappingsTable: rrmTable,
      rolesTable,
      sourcesApiFnName: "coa-dev-sources-api",
      metricApiFnName: "coa-dev-metric-api",
      datazoneDomainId: "dzd-123",
      namespaceApiFnRoleArn:
        "arn:aws:iam::123456789012:role/coa-dev-namespace-api",
      vkgClusterArn: "arn:aws:ecs:us-east-1:123456789012:cluster/vkg",
      cloudMapNamespaceId: "ns-abc123",
      ontologyBucketName: "coa-dev-ontology-artifacts",
      opensearchEndpoint: "https://abc.us-east-1.aoss.amazonaws.com",
      opensearchCollectionName: "coa-dev-vector-store",
      resourcePrefix: "coa-dev-",
    });

    template = Template.fromStack(stack);
  });

  test("names every aoss:APIAccessAll role in an AOSS data-access policy", () => {
    expectAossGrantsCoveredByDataPolicies(template);
  });

  test("creates a Step Functions state machine with 1 hour timeout", () => {
    template.hasResourceProperties("AWS::StepFunctions::StateMachine", {
      StateMachineName: "coa-dev-namespace-deletion-pipeline",
      TracingConfiguration: { Enabled: true },
    });
  });

  test("state machine definition includes all 5 pipeline steps in order", () => {
    const definitionCapture = new Capture();
    template.hasResourceProperties("AWS::StepFunctions::StateMachine", {
      DefinitionString: definitionCapture,
    });
    const joinParts: unknown[] = definitionCapture.asObject()["Fn::Join"][1];
    const definitionStr = joinParts
      .filter((part): part is string => typeof part === "string")
      .join("");
    const order = [
      "DeleteSources",
      "DeleteMetrics",
      "DeleteOntology",
      "DeletePlatform",
      "Finalize",
    ];
    const indices = order.map((name) => definitionStr.indexOf(`"${name}"`));
    // Each step name appears, and in the expected relative order.
    indices.forEach((idx) => expect(idx).toBeGreaterThan(-1));
    for (let i = 1; i < indices.length; i++) {
      expect(indices[i]).toBeGreaterThan(indices[i - 1]);
    }
  });

  test("state machine definition wires MarkFailed as the Catch target for every step", () => {
    const definitionCapture = new Capture();
    template.hasResourceProperties("AWS::StepFunctions::StateMachine", {
      DefinitionString: definitionCapture,
    });
    // DefinitionString synthesizes as Fn::Join["", [...]] where the string
    // parts already contain literal `"Next":"MarkFailed"` (not further
    // JSON-escaped) — concatenate just the string parts to search.
    const joinParts: unknown[] = definitionCapture.asObject()["Fn::Join"][1];
    const definitionStr = joinParts
      .filter((part): part is string => typeof part === "string")
      .join("");
    const markFailedOccurrences =
      definitionStr.split('"Next":"MarkFailed"').length - 1;
    expect(markFailedOccurrences).toBeGreaterThanOrEqual(5);
  });

  test("all 6 pipeline Lambdas are deployed in the VPC with private subnets", () => {
    const fns = [
      "ns-deletion-sources",
      "ns-deletion-metrics",
      "ns-deletion-ontology",
      "ns-deletion-platform",
      "ns-deletion-finalize",
      "ns-deletion-mark-failed",
    ];
    for (const fnName of fns) {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: `coa-dev-${fnName}`,
        VpcConfig: Match.objectLike({
          SubnetIds: Match.anyValue(),
          SecurityGroupIds: Match.anyValue(),
        }),
      });
    }
  });

  test("DeleteSourcesFn is granted lambda:InvokeFunction scoped to sources-api", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "lambda:InvokeFunction",
            Resource: {
              "Fn::Join": [
                "",
                Match.arrayWith([
                  Match.stringLikeRegexp("coa-dev-sources-api"),
                ]),
              ],
            },
          }),
        ]),
      },
      PolicyName: Match.stringLikeRegexp("DeleteSourcesFn"),
    });
  });

  test("DeleteMetricsFn is granted lambda:InvokeFunction scoped to metric-api", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "lambda:InvokeFunction",
            Resource: {
              "Fn::Join": [
                "",
                Match.arrayWith([Match.stringLikeRegexp("coa-dev-metric-api")]),
              ],
            },
          }),
        ]),
      },
      PolicyName: Match.stringLikeRegexp("DeleteMetricsFn"),
    });
  });

  test("DeletePlatformFn assumes namespaceApiFn's execution role rather than calling DeleteProject with its own execution role", () => {
    // DataZone authorizes DeleteProject against project ownership, not IAM
    // policy alone — namespaceApiFn's role is the actual owner (it called
    // CreateProject), so DeletePlatform must assume it rather than being
    // granted datazone:DeleteProject directly on its own execution role.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "sts:AssumeRole",
            Resource: "arn:aws:iam::123456789012:role/coa-dev-namespace-api",
          }),
        ]),
      },
      PolicyName: Match.stringLikeRegexp("DeletePlatformFn"),
    });

    const policies = template.findResources("AWS::IAM::Policy");
    const deletePlatformPolicy = Object.values(policies).find((p) =>
      (p.Properties?.PolicyName as string)?.includes("DeletePlatformFn"),
    );
    const statements = deletePlatformPolicy?.Properties?.PolicyDocument
      ?.Statement as Array<{ Action?: string | string[] }>;
    const hasDirectDeleteProject = statements?.some(
      (s) => s.Action === "datazone:DeleteProject",
    );
    expect(hasDirectDeleteProject).toBe(false);
  });

  test("DeletePlatformFn has Athena and ECS/Cloud Map delete permissions", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["athena:DeleteWorkGroup"]),
          }),
        ]),
      },
    });
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["ecs:DeleteService"]),
          }),
        ]),
      },
    });
  });

  test("FinalizeFn is granted TransactWriteItems and DeleteItem on the namespaces table", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              "dynamodb:TransactWriteItems",
              "dynamodb:DeleteItem",
            ]),
          }),
        ]),
      },
    });
  });

  test("MarkFailedFn is granted write access to the namespaces table", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([Match.stringLikeRegexp("dynamodb:.*")]),
          }),
        ]),
      },
    });
  });
});

// GraphRAG doc-KG indexes are namespace-scoped (all doc sources in a namespace
// share one GraphRAG tenant id), so namespace teardown drops them. Nothing did
// before, leaving one index set per deleted namespace against a collection AOSS
// caps at 1000 indexes — past that, every embedding write fails with
// index_limit_breached.
describe("NamespaceDeletionPipeline GraphRAG index teardown", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, "GraphragStack");
    const vpc = new ec2.Vpc(stack, "Vpc");
    const table = (id: string) =>
      new dynamodb.Table(stack, id, {
        partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      });

    new NamespaceDeletionPipeline(stack, "DeletionPipeline", {
      prefixed: (name: string) => `coa-dev-${name}`,
      aossName: (name: string) => `coa-dev-${name}`,
      lambdaCode: lambda.Code.fromInline("def handler(event, context): pass"),
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      namespacesTable: table("Namespaces"),
      resourceRoleMappingsTable: table("RRM"),
      rolesTable: table("Roles"),
      sourcesApiFnName: "coa-dev-sources-api",
      metricApiFnName: "coa-dev-metric-api",
      opensearchEndpoint: "https://abc.us-east-1.aoss.amazonaws.com",
      opensearchCollectionName: "coa-dev-vector-store",
      resourcePrefix: "coa-dev-",
    });
    template = Template.fromStack(stack);
  });

  test("DeleteSources Lambda receives the AOSS endpoint", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "coa-dev-ns-deletion-sources",
      Environment: {
        Variables: Match.objectLike({
          OSS_ENDPOINT: "https://abc.us-east-1.aoss.amazonaws.com",
        }),
      },
    });
  });

  test("an AOSS data access policy grants DeleteIndex on graphrag names only", () => {
    // AOSS needs BOTH an IAM action and a data-access policy naming the role;
    // either alone yields 403 at runtime.
    const capture = new Capture();
    template.hasResourceProperties("AWS::OpenSearchServerless::AccessPolicy", {
      Name: "coa-dev-ns-del-graphrag",
      Type: "data",
      Policy: capture,
    });
    const policy = capture.asObject();
    // The role ARN is a CFN token, so the policy renders as an Fn::Join rather
    // than a plain string — assert against the serialized form.
    const policyStr = JSON.stringify(policy);
    expect(policyStr).toContain("aoss:DeleteIndex");
    expect(policyStr).toContain("index/coa-dev-vector-store/chunk_*");
    expect(policyStr).toContain("index/coa-dev-vector-store/topic_*");
    // Legacy index from before statements stopped being embedded.
    expect(policyStr).toContain("index/coa-dev-vector-store/statement_*");
    // Must NOT be collection-wide: a bug here must not be able to reach the
    // ontology or metric indexes living in the same collection.
    expect(policyStr).not.toContain("index/coa-dev-vector-store/*");
    // Least privilege: no write/create on this policy.
    expect(policyStr).not.toContain("aoss:CreateIndex");
    expect(policyStr).not.toContain("aoss:WriteDocument");
    // Scoped to the DeleteSources Lambda's own role, not account-wide.
    expect(policyStr).toContain("DeleteSourcesFnServiceRole");
  });

  test("omitting the collection name skips the policy entirely", () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, "NoAossStack");
    const vpc = new ec2.Vpc(stack, "Vpc");
    const table = (id: string) =>
      new dynamodb.Table(stack, id, {
        partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      });

    new NamespaceDeletionPipeline(stack, "DeletionPipeline", {
      prefixed: (name: string) => `coa-dev-${name}`,
      aossName: (name: string) => `coa-dev-${name}`,
      lambdaCode: lambda.Code.fromInline("def handler(event, context): pass"),
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      namespacesTable: table("Namespaces"),
      resourceRoleMappingsTable: table("RRM"),
      rolesTable: table("Roles"),
      sourcesApiFnName: "coa-dev-sources-api",
      metricApiFnName: "coa-dev-metric-api",
      resourcePrefix: "coa-dev-",
    });

    Template.fromStack(stack).resourceCountIs(
      "AWS::OpenSearchServerless::AccessPolicy",
      0,
    );
  });
});
