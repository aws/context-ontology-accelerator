// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template, Match } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { MetricServiceStack } from "../../lib/stacks/services/metric-service-stack";
import { DEFAULT_BEDROCK_MODEL_ID } from "../../lib/constants";

// Mock bundlePython to avoid fingerprinting the entire repo root during tests.
jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const TEST_ENV = { account: "123456789012", region: "us-east-1" };
const TEST_CONTEXT = { "aws:cdk:bundling-stacks": [] };

describe("MetricServiceStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const network = new NetworkStack(app, "TestNetwork", { env: TEST_ENV });

    template = Template.fromStack(
      new MetricServiceStack(app, "TestMetricService", {
        vpc: network.vpc,
        lambdaSecurityGroup: network.lambdaSecurityGroup,
        aossSecurityGroup: network.aossSecurityGroup,
        neptuneEndpoint:
          "test-neptune.cluster-abc.us-east-1.neptune.amazonaws.com",
        neptuneClusterArn:
          "arn:aws:neptune-db:us-east-1:123456789012:cluster-abc123/*",
        env: TEST_ENV,
      }),
    );
  });

  // ── Lambda Function ─────────────────────────────────────────────

  test("creates metric API Lambda function", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      Runtime: "python3.12",
      Timeout: 30,
      MemorySize: 512,
    });
  });

  test("Lambda is placed in VPC with private subnets", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      VpcConfig: Match.objectLike({
        SubnetIds: Match.anyValue(),
        SecurityGroupIds: Match.anyValue(),
      }),
    });
  });

  test("Lambda has required environment variables", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      Environment: Match.objectLike({
        Variables: Match.objectLike({
          NEPTUNE_ENDPOINT: Match.stringLikeRegexp("https://.*:8182"),
          OPENSEARCH_ENDPOINT: Match.objectLike({
            Ref: Match.stringLikeRegexp(
              "SsmParameterValue.*opensearchendpoint.*",
            ),
          }),
          EVENTBRIDGE_BUS_NAME: "default",
          BEDROCK_MODEL_ID: DEFAULT_BEDROCK_MODEL_ID,
          BEDROCK_REGION: "us-east-1",
        }),
      }),
    });
  });

  test("OSI staging bucket versions and retains recovery artifacts", () => {
    template.hasResourceProperties("AWS::S3::Bucket", {
      VersioningConfiguration: { Status: "Enabled" },
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({
            ExpirationInDays: 30,
            NoncurrentVersionExpiration: { NoncurrentDays: 30 },
            Status: "Enabled",
          }),
        ]),
      },
    });
  });

  test("import worker has durable-claim and SMUS catalog configuration", () => {
    // Without these the worker cannot fence duplicate writes or initialize
    // source lookup, so async imports would be unsafe or fail closed.
    template.hasResourceProperties("AWS::Lambda::Function", {
      Handler: "coa_metrics.api.import_worker.handler",
      Environment: Match.objectLike({
        Variables: Match.objectLike({
          IMPORT_OFFSET_LEASE_SECONDS: "960",
          DATA_SOURCES_TABLE: Match.anyValue(),
          NAMESPACES_TABLE: Match.anyValue(),
          SMUS_DOMAIN_ID: Match.anyValue(),
          PROJECT_ACCESS_ROLE_ARN: Match.anyValue(),
        }),
      }),
    });
  });

  test("DLQ recovery retries outside the VPC and can report terminal failure", () => {
    template.hasResourceProperties("AWS::SQS::Queue", {
      RedrivePolicy: Match.objectLike({ maxReceiveCount: 6 }),
    });
    template.hasResourceProperties("AWS::SQS::Queue", {
      MessageRetentionPeriod: 1209600,
      VisibilityTimeout: 180,
    });
    template.hasResourceProperties("AWS::Lambda::Function", {
      Handler: "coa_metrics.api.import_dlq_handler.handler",
      Timeout: 30,
      MemorySize: 256,
      Environment: Match.objectLike({
        Variables: Match.objectLike({
          IMPORT_QUEUE_URL: Match.anyValue(),
          IMPORT_JOBS_TABLE: Match.anyValue(),
          IMPORT_REDRIVE_DELAY_SECONDS: "60",
        }),
      }),
    });
    template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
      BatchSize: 1,
      EventSourceArn: {
        "Fn::GetAtt": [Match.stringLikeRegexp("^ImportDLQ"), "Arn"],
      },
      FunctionName: {
        Ref: Match.stringLikeRegexp("^ImportDlqRecoveryFn"),
      },
    });

    const recoveryFunctions = template.findResources("AWS::Lambda::Function");
    const recovery = Object.values(recoveryFunctions).find(
      (resource) =>
        resource.Properties?.Handler ===
        "coa_metrics.api.import_dlq_handler.handler",
    );
    expect(recovery).toBeDefined();
    expect(recovery?.Properties.VpcConfig).toBeUndefined();
    template.resourceCountIs("AWS::Lambda::EventSourceMapping", 2);
  });

  // ── IAM Permissions ─────────────────────────────────────────────

  interface SynthesizedResource {
    readonly logicalId: string;
    readonly properties: Record<string, unknown>;
  }

  interface CustomAllowPermission {
    readonly action: string;
    readonly resourceLogicalId: string;
  }

  function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
  }

  function requireRecord(
    value: unknown,
    description: string,
  ): Record<string, unknown> {
    if (!isRecord(value)) {
      throw new Error(`Expected ${description} to be an object`);
    }
    return value;
  }

  function requireString(value: unknown, description: string): string {
    if (typeof value !== "string") {
      throw new Error(`Expected ${description} to be a string`);
    }
    return value;
  }

  function normalizeScalarOrArray(value: unknown): readonly unknown[] {
    if (value === undefined) {
      return [];
    }
    return Array.isArray(value) ? value : [value];
  }

  function synthesizedResources(resourceType: string): SynthesizedResource[] {
    const rawResources: unknown = template.findResources(resourceType);
    const resources = requireRecord(rawResources, `${resourceType} resources`);

    return Object.entries(resources).map(([logicalId, rawResource]) => {
      const resource = requireRecord(rawResource, `${logicalId} resource`);
      return {
        logicalId,
        properties: requireRecord(
          resource.Properties,
          `${logicalId} properties`,
        ),
      };
    });
  }

  function requireSingleResource(
    resources: readonly SynthesizedResource[],
    predicate: (resource: SynthesizedResource) => boolean,
    description: string,
  ): SynthesizedResource {
    const matches = resources.filter(predicate);
    if (matches.length !== 1) {
      throw new Error(
        `Expected exactly one ${description}, found ${matches.length}`,
      );
    }
    const [match] = matches;
    if (match === undefined) {
      throw new Error(`Expected ${description} to exist`);
    }
    return match;
  }

  function requireRefLogicalId(value: unknown, description: string): string {
    const reference = requireRecord(value, description);
    if (
      Object.keys(reference).length !== 1 ||
      typeof reference.Ref !== "string"
    ) {
      throw new Error(`Expected ${description} to contain exactly one Ref`);
    }
    return reference.Ref;
  }

  function isRefTo(value: unknown, logicalId: string): boolean {
    return (
      isRecord(value) &&
      Object.keys(value).length === 1 &&
      value.Ref === logicalId
    );
  }

  function requireGetAttLogicalId(
    value: unknown,
    attribute: string,
    description: string,
  ): string {
    const reference = requireRecord(value, description);
    const getAtt = reference["Fn::GetAtt"];
    if (
      Object.keys(reference).length !== 1 ||
      !Array.isArray(getAtt) ||
      getAtt.length !== 2 ||
      typeof getAtt[0] !== "string" ||
      getAtt[1] !== attribute
    ) {
      throw new Error(
        `Expected ${description} to contain exactly one Fn::GetAtt for ${attribute}`,
      );
    }
    return getAtt[0];
  }

  function sortPermissions(
    permissions: readonly CustomAllowPermission[],
  ): CustomAllowPermission[] {
    return [...permissions].sort((left, right) => {
      const leftKey = `${left.action}\u0000${left.resourceLogicalId}`;
      const rightKey = `${right.action}\u0000${right.resourceLogicalId}`;
      return leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0;
    });
  }

  test("import worker can write only job checkpoint objects and read pinned versions", () => {
    const workerFunction = requireSingleResource(
      synthesizedResources("AWS::Lambda::Function"),
      ({ properties }) =>
        properties.Handler === "coa_metrics.api.import_worker.handler",
      "import worker function",
    );
    const workerRoleLogicalId = requireGetAttLogicalId(
      workerFunction.properties.Role,
      "Arn",
      "import worker function role",
    );
    const statements: Record<string, unknown>[] = [];
    for (const policy of synthesizedResources("AWS::IAM::Policy")) {
      const attached = normalizeScalarOrArray(policy.properties.Roles).some(
        (role) => isRefTo(role, workerRoleLogicalId),
      );
      if (!attached) {
        continue;
      }
      const document = requireRecord(
        policy.properties.PolicyDocument,
        `${policy.logicalId} policy document`,
      );
      for (const rawStatement of normalizeScalarOrArray(document.Statement)) {
        statements.push(
          requireRecord(rawStatement, `${policy.logicalId} statement`),
        );
      }
    }

    const checkpointWrites = statements.filter((statement) =>
      normalizeScalarOrArray(statement.Action).includes("s3:PutObject"),
    );
    expect(checkpointWrites).toHaveLength(1);
    const checkpointWrite = checkpointWrites[0];
    if (checkpointWrite === undefined) {
      throw new Error("Expected checkpoint write statement");
    }
    expect(checkpointWrite.Effect).toBe("Allow");
    const checkpointResources = normalizeScalarOrArray(
      checkpointWrite.Resource,
    );
    expect(checkpointResources).toHaveLength(1);
    expect(JSON.stringify(checkpointResources[0])).toContain(
      "/*/imports/checkpoints/*",
    );

    const objectReads = statements.filter((statement) => {
      const actions = normalizeScalarOrArray(statement.Action);
      return (
        actions.includes("s3:GetObject") ||
        actions.includes("s3:GetObjectVersion")
      );
    });
    expect(objectReads).toHaveLength(1);
    const objectRead = objectReads[0];
    if (objectRead === undefined) {
      throw new Error("Expected import object read statement");
    }
    expect(normalizeScalarOrArray(objectRead.Action)).toEqual([
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]);
    const readResources = normalizeScalarOrArray(objectRead.Resource);
    expect(readResources).toHaveLength(1);
    expect(JSON.stringify(readResources[0])).toContain("/*/imports/*");
    for (const statement of statements) {
      for (const action of normalizeScalarOrArray(statement.Action)) {
        if (typeof action === "string" && action.startsWith("s3:")) {
          expect(action).not.toContain("*");
        }
      }
    }
  });

  test("DLQ recovery role has only the required custom permissions", () => {
    const recoveryFunction = requireSingleResource(
      synthesizedResources("AWS::Lambda::Function"),
      ({ properties }) =>
        properties.Handler === "coa_metrics.api.import_dlq_handler.handler",
      "DLQ recovery function",
    );
    const recoveryFunctionLogicalId = recoveryFunction.logicalId;
    const recoveryRoleLogicalId = requireGetAttLogicalId(
      recoveryFunction.properties.Role,
      "Arn",
      "DLQ recovery function role",
    );
    const environment = requireRecord(
      recoveryFunction.properties.Environment,
      "DLQ recovery function environment",
    );
    const variables = requireRecord(
      environment.Variables,
      "DLQ recovery function environment variables",
    );
    const importJobsTableLogicalId = requireRefLogicalId(
      variables.IMPORT_JOBS_TABLE,
      "IMPORT_JOBS_TABLE",
    );
    const importQueueLogicalId = requireRefLogicalId(
      variables.IMPORT_QUEUE_URL,
      "IMPORT_QUEUE_URL",
    );

    const recoveryEventSourceMapping = requireSingleResource(
      synthesizedResources("AWS::Lambda::EventSourceMapping"),
      ({ properties }) =>
        isRefTo(properties.FunctionName, recoveryFunctionLogicalId),
      "DLQ recovery event source mapping",
    );
    const importDlqLogicalId = requireGetAttLogicalId(
      recoveryEventSourceMapping.properties.EventSourceArn,
      "Arn",
      "DLQ recovery event source ARN",
    );

    const recoveryRole = requireSingleResource(
      synthesizedResources("AWS::IAM::Role"),
      ({ logicalId }) => logicalId === recoveryRoleLogicalId,
      "DLQ recovery role",
    );
    expect(recoveryRole.properties.ManagedPolicyArns).toEqual([
      {
        "Fn::Join": [
          "",
          [
            "arn:",
            { Ref: "AWS::Partition" },
            ":iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
          ],
        ],
      },
    ]);

    const policyDocuments: unknown[] = [];
    for (const policy of synthesizedResources("AWS::IAM::Policy")) {
      const attachedToRecoveryRole = normalizeScalarOrArray(
        policy.properties.Roles,
      ).some((role) => isRefTo(role, recoveryRoleLogicalId));
      if (attachedToRecoveryRole) {
        policyDocuments.push(policy.properties.PolicyDocument);
      }
    }

    for (const [index, rawInlinePolicy] of normalizeScalarOrArray(
      recoveryRole.properties.Policies,
    ).entries()) {
      const inlinePolicy = requireRecord(
        rawInlinePolicy,
        `DLQ recovery role inline policy ${index}`,
      );
      policyDocuments.push(inlinePolicy.PolicyDocument);
    }

    for (const managedPolicy of synthesizedResources(
      "AWS::IAM::ManagedPolicy",
    )) {
      const attachedToRecoveryRole = normalizeScalarOrArray(
        managedPolicy.properties.Roles,
      ).some((role) => isRefTo(role, recoveryRoleLogicalId));
      if (attachedToRecoveryRole) {
        policyDocuments.push(managedPolicy.properties.PolicyDocument);
      }
    }
    expect(policyDocuments.length).toBeGreaterThan(0);

    const actualPermissions: CustomAllowPermission[] = [];
    for (const [policyIndex, rawPolicyDocument] of policyDocuments.entries()) {
      const policyDocument = requireRecord(
        rawPolicyDocument,
        `DLQ recovery policy document ${policyIndex}`,
      );
      const statements = normalizeScalarOrArray(policyDocument.Statement);
      if (statements.length === 0) {
        throw new Error(
          `DLQ recovery policy document ${policyIndex} has no statements`,
        );
      }

      for (const [statementIndex, rawStatement] of statements.entries()) {
        const description = `DLQ recovery policy ${policyIndex} statement ${statementIndex}`;
        const statement = requireRecord(rawStatement, description);
        const effect = requireString(statement.Effect, `${description} Effect`);
        if (effect === "Deny") {
          continue;
        }
        if (effect !== "Allow") {
          throw new Error(`${description} has unsupported Effect ${effect}`);
        }
        if (Object.prototype.hasOwnProperty.call(statement, "NotAction")) {
          throw new Error(`${description} must not use NotAction`);
        }
        if (Object.prototype.hasOwnProperty.call(statement, "NotResource")) {
          throw new Error(`${description} must not use NotResource`);
        }

        const rawActions = normalizeScalarOrArray(statement.Action);
        if (rawActions.length === 0) {
          throw new Error(`${description} has no actions`);
        }
        const actions = rawActions.map((rawAction, actionIndex) => {
          const action = requireString(
            rawAction,
            `${description} Action ${actionIndex}`,
          );
          if (action.includes("*") || action.includes("?")) {
            throw new Error(
              `${description} contains wildcard action ${action}`,
            );
          }
          return action;
        });

        const rawResources = normalizeScalarOrArray(statement.Resource);
        if (rawResources.length === 0) {
          throw new Error(`${description} has no resources`);
        }
        const resourceLogicalIds = rawResources.map(
          (rawResource, resourceIndex) =>
            requireGetAttLogicalId(
              rawResource,
              "Arn",
              `${description} Resource ${resourceIndex}`,
            ),
        );

        for (const action of actions) {
          for (const resourceLogicalId of resourceLogicalIds) {
            actualPermissions.push({ action, resourceLogicalId });
          }
        }
      }
    }

    const expectedPermissions: CustomAllowPermission[] = [
      {
        action: "dynamodb:GetItem",
        resourceLogicalId: importJobsTableLogicalId,
      },
      {
        action: "dynamodb:UpdateItem",
        resourceLogicalId: importJobsTableLogicalId,
      },
      {
        action: "sqs:ChangeMessageVisibility",
        resourceLogicalId: importDlqLogicalId,
      },
      {
        action: "sqs:DeleteMessage",
        resourceLogicalId: importDlqLogicalId,
      },
      {
        action: "sqs:GetQueueAttributes",
        resourceLogicalId: importDlqLogicalId,
      },
      {
        action: "sqs:GetQueueUrl",
        resourceLogicalId: importDlqLogicalId,
      },
      {
        action: "sqs:ReceiveMessage",
        resourceLogicalId: importDlqLogicalId,
      },
      {
        action: "sqs:GetQueueAttributes",
        resourceLogicalId: importQueueLogicalId,
      },
      {
        action: "sqs:GetQueueUrl",
        resourceLogicalId: importQueueLogicalId,
      },
      {
        action: "sqs:SendMessage",
        resourceLogicalId: importQueueLogicalId,
      },
    ];

    expect(sortPermissions(actualPermissions)).toEqual(
      sortPermissions(expectedPermissions),
    );
  });

  test("Lambda has Neptune read/write/delete access", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: [
              "neptune-db:ReadDataViaQuery",
              "neptune-db:WriteDataViaQuery",
              "neptune-db:DeleteDataViaQuery",
            ],
            Effect: "Allow",
          }),
        ]),
      }),
    });
  });

  test("Lambda has OpenSearch Serverless access", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "aoss:APIAccessAll",
            Effect: "Allow",
          }),
        ]),
      }),
    });
  });

  test("Lambda has Bedrock InvokeModel access", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "bedrock:InvokeModel",
            Effect: "Allow",
          }),
        ]),
      }),
    });
  });

  test("Lambda has EventBridge PutEvents access", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "events:PutEvents",
            Effect: "Allow",
          }),
        ]),
      }),
    });
  });

  // ── AOSS Data Access Policy ─────────────────────────────────────

  test("creates AOSS data access policy", () => {
    template.hasResourceProperties("AWS::OpenSearchServerless::AccessPolicy", {
      Type: "data",
    });
  });

  // ── S3 Bucket (OSI import/export) ───────────────────────────────

  test("creates OSI S3 bucket with encryption and block public access", () => {
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          { ServerSideEncryptionByDefault: { SSEAlgorithm: "AES256" } },
        ],
      },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  // ── SSM Parameter ───────────────────────────────────────────────

  test("publishes Lambda ARN to SSM", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Type: "String",
    });
  });

  // ── Resource Count ──────────────────────────────────────────────

  test("creates expected Lambda functions", () => {
    // MetricApiFn + ImportWorkerFn + ImportDlqRecoveryFn + CDK auto-delete
    // custom resource for the OSI S3 bucket
    template.resourceCountIs("AWS::Lambda::Function", 4);
  });

  // ── OE Monitoring ────────────────────────────────────────────

  it("emits a metric-service OE dashboard and alarms", () => {
    template.resourceCountIs("AWS::CloudWatch::Dashboard", 1);
    const alarms = template.findResources("AWS::CloudWatch::Alarm");
    expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(2);
  });
});
