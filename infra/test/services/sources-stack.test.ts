// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template, Match } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { StorageStack } from "../../lib/stacks/foundation/storage-stack";
import { SourcesStack } from "../../lib/stacks/services/sources-stack";

// Mock bundlePython to avoid fingerprinting the entire repo root during tests.
jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const TEST_ENV = { account: "123456789012", region: "us-east-1" };

// Provide fake ECR context so CDK uses fromEcr() instead of fromImageAsset()
// (which would trigger a real Docker build and hang the test).
// Mirrors the approach used in unstructured-stack.test.ts.
const TEST_CONTEXT = {
  ecr_repository_arn: "arn:aws:ecr:us-east-1:123456789012:repository/coa-test",
  ecr_repository_name: "coa-test",
  sources_db_enrichment_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:db-enrichment-test",
  sources_preprocessing_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:preprocessing-test",
  sources_kg_build_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:kg-build-test",
  "aws:cdk:bundling-stacks": [],
};

describe("SourcesStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const network = new NetworkStack(app, "TestNetwork", { env: TEST_ENV });
    const storage = new StorageStack(app, "TestStorage", {
      network,
      env: TEST_ENV,
    });
    template = Template.fromStack(
      new SourcesStack(app, "TestSources", {
        network,
        storage,
        allowedOrigin: "https://test.example.com",
        env: TEST_ENV,
      }),
    );
  });

  describe("DynamoDB Tables", () => {
    it("creates sources-table with PK and SK string keys", () => {
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        KeySchema: Match.arrayWith([
          { AttributeName: "PK", KeyType: "HASH" },
          { AttributeName: "SK", KeyType: "RANGE" },
        ]),
        BillingMode: "PAY_PER_REQUEST",
      });
    });

    it("sources-table has ByNamespace GSI on namespaceId + createdAt", () => {
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({
            IndexName: "ByNamespace",
            KeySchema: Match.arrayWith([
              { AttributeName: "namespaceId", KeyType: "HASH" },
              { AttributeName: "createdAt", KeyType: "RANGE" },
            ]),
          }),
        ]),
      });
    });

    it("sources-table has BySourceType GSI on namespaceId + sourceTypeCreatedAt", () => {
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({
            IndexName: "BySourceType",
            KeySchema: Match.arrayWith([
              { AttributeName: "namespaceId", KeyType: "HASH" },
              { AttributeName: "sourceTypeCreatedAt", KeyType: "RANGE" },
            ]),
          }),
        ]),
      });
    });

    it("sources-table has ByName GSI on namespaceId + name for O(1) uniqueness checks", () => {
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({
            IndexName: "ByName",
            KeySchema: Match.arrayWith([
              { AttributeName: "namespaceId", KeyType: "HASH" },
              { AttributeName: "name", KeyType: "RANGE" },
            ]),
          }),
        ]),
      });
    });
  });

  describe("Sources API Lambda", () => {
    it("creates a Python 3.12 ARM64 Lambda function", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Runtime: "python3.12",
        Architectures: ["arm64"],
        Timeout: 30,
        MemorySize: 256,
      });
    });

    it("Lambda has SOURCES_TABLE environment variable", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Environment: {
          Variables: Match.objectLike({
            SOURCES_TABLE: Match.anyValue(),
          }),
        },
      });
    });

    it("Lambda has SOURCE_SCAN_JOBS_TABLE environment variable", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Environment: {
          Variables: Match.objectLike({
            SOURCE_SCAN_JOBS_TABLE: Match.anyValue(),
          }),
        },
      });
    });

    it("Lambda has ALLOWED_ORIGIN environment variable", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Environment: {
          Variables: Match.objectLike({
            ALLOWED_ORIGIN: "https://test.example.com",
          }),
        },
      });
    });

    it("Lambda has REVIEW_QUEUE_URL environment variable for bulk approve/reject dispatch", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Environment: {
          Variables: Match.objectLike({
            REVIEW_QUEUE_URL: Match.anyValue(),
          }),
        },
      });
    });
  });

  describe("Federation Provisioner (Option B isolation)", () => {
    it("creates a dedicated JDBC federation provisioner Lambda in VPC", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(
          ".*sources-federation-provisioner$",
        ),
        Handler: "coa_sources.database.pipeline.federation_handler.handler",
        VpcConfig: Match.objectLike({ SubnetIds: Match.anyValue() }),
        Environment: {
          Variables: Match.objectLike({
            FEDERATED_CATALOG_ROLE_ARN: Match.anyValue(),
            ATHENA_SPILL_BUCKET: Match.anyValue(),
          }),
        },
      });
    });

    it("publishes the federation provisioner role ARN for central LF-admin registration", () => {
      template.hasResourceProperties("AWS::SSM::Parameter", {
        Type: "String",
        Description: Match.stringLikeRegexp(
          ".*Lake Formation data-lake admin.*",
        ),
      });
    });

    it("passes the consumer query role SSM param to the federation provisioner", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(
          ".*sources-federation-provisioner$",
        ),
        Environment: {
          Variables: Match.objectLike({
            CONSUMER_QUERY_ROLE_SSM_PARAM: Match.stringLikeRegexp(
              ".*/serve/runtime-role-arn$",
            ),
          }),
        },
      });
    });

    it("grants the federation provisioner Glue read on federated databases/tables (for GrantPermissions)", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "GlueFederatedCatalogRead",
              Action: [
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetTable",
                "glue:GetTables",
              ],
              Resource: Match.arrayWith([
                Match.stringLikeRegexp(".*:database/.*"),
                Match.stringLikeRegexp(".*:table/.*"),
              ]),
            }),
          ]),
        },
      });
    });

    it("grants the federation provisioner Glue read on ALL native databases (for native LF grant)", () => {
      // Required so lakeformation:GrantPermissions can validate the grantor has
      // access to the target native Glue database (GLUE_DATABASE sources, strict-LF accounts).
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "GlueNativeDatabaseRead",
              Action: [
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetTable",
                "glue:GetTables",
              ],
              // Wildcard suffixes cover ALL native databases, not just scldevds_* federated ones.
              Resource: Match.arrayWith([
                Match.stringLikeRegexp(":catalog$"),
                Match.stringLikeRegexp(":database/\\*$"),
                Match.stringLikeRegexp(":table/\\*/\\*$"),
              ]),
            }),
          ]),
        },
      });
    });

    it("grants the federation provisioner lakeformation:GrantPermissions", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "LakeFormationFederation",
              Action: Match.arrayWith(["lakeformation:GrantPermissions"]),
            }),
          ]),
        },
      });
    });

    it("registers the provisioner role as an LF admin via a custom resource (non-destructive)", () => {
      // onEvent Lambda role can read/write LF settings + read the role-ARN SSM param.
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                "lakeformation:GetDataLakeSettings",
                "lakeformation:PutDataLakeSettings",
              ]),
            }),
          ]),
        },
      });
    });

    it("lets the federated-catalog role decrypt CMK-encrypted secrets via Secrets Manager only", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "DecryptCredentialSecret",
              Action: "kms:Decrypt",
              Condition: {
                StringLike: {
                  "kms:ViaService": "secretsmanager.*.amazonaws.com",
                },
              },
            }),
          ]),
        },
      });
    });

    it("grants the federated-catalog role ENI actions on * (Glue dry-runs them against a wildcard)", () => {
      // Regression cover for a failure that presents as a networking problem:
      // Glue's managed connector pre-flight authorizes DeleteNetworkInterface
      // against `arn:aws:ec2:<region>:<account>:*/*`, so any resource-scoped
      // statement is denied and the connection reports "Unable to access VPC
      // provided in the connection" — naming the subnet and SG, never the denied
      // action. Scoping this statement breaks every federated catalog in a fresh
      // environment. AWS's own AWSGlueServiceRole uses `*` for the same actions.
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "Ec2NetworkInterfaceManagement",
              Action: Match.arrayWith([
                "ec2:CreateNetworkInterface",
                "ec2:DeleteNetworkInterface",
              ]),
              Resource: "*",
            }),
          ]),
        },
      });
    });

    it("lets the provisioner assume the federated-catalog role for the secret precheck", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "sts:AssumeRole",
              Resource: Match.anyValue(),
            }),
          ]),
        },
      });
    });
  });

  describe("Bulk Review Pipeline", () => {
    it("creates a SQS queue with SQS-managed encryption and 6-minute visibility timeout", () => {
      template.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: Match.stringLikeRegexp(".*sources-bulk-review-queue$"),
        VisibilityTimeout: 360,
        SqsManagedSseEnabled: true,
      });
    });

    it("creates a DLQ with SQS-managed encryption and 14-day retention", () => {
      template.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: Match.stringLikeRegexp(".*sources-bulk-review-dlq$"),
        MessageRetentionPeriod: 14 * 24 * 60 * 60,
        SqsManagedSseEnabled: true,
      });
    });

    it("queue has a redrive policy pointing to the DLQ with maxReceiveCount=3", () => {
      template.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: Match.stringLikeRegexp(".*sources-bulk-review-queue$"),
        RedrivePolicy: Match.objectLike({
          maxReceiveCount: 3,
        }),
      });
    });

    it("creates a worker Lambda with 5-minute timeout, ARM64, in VPC", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-bulk-review-worker$"),
        Runtime: "python3.12",
        Architectures: ["arm64"],
        Timeout: 300,
        VpcConfig: Match.objectLike({
          SubnetIds: Match.anyValue(),
          SecurityGroupIds: Match.anyValue(),
        }),
      });
    });

    it("worker has a SQS event source mapping to the bulk review queue", () => {
      // batchSize=1 means SQS feeds one message per worker invocation,
      // matching the worker's per-message idempotency check.
      template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
        BatchSize: 1,
        EventSourceArn: Match.anyValue(),
        FunctionName: Match.anyValue(),
      });
    });

    it("worker can re-enqueue to its own queue: REVIEW_QUEUE_URL env + sqs:SendMessage grant", () => {
      // Self-continuation (#853): a source too large for one invocation pages
      // itself by re-enqueuing with a nextToken, so the worker needs both the
      // queue URL and SendMessage on it. The grant is checked against the
      // worker's own role — the API Lambda also holds SendMessage on this
      // queue, so an unscoped assertion would still pass with the worker's
      // grant removed.
      const queue = Object.entries(
        template.findResources("AWS::SQS::Queue"),
      ).find(([, q]) =>
        /sources-bulk-review-queue$/.test(q.Properties.QueueName),
      );
      const worker = Object.entries(
        template.findResources("AWS::Lambda::Function"),
      ).find(([, f]) =>
        /sources-bulk-review-worker$/.test(f.Properties.FunctionName),
      );
      expect(queue).toBeDefined();
      expect(worker).toBeDefined();
      const [queueId] = queue!;
      const [, workerFn] = worker!;

      // Ref on an AWS::SQS::Queue resolves to the queue URL.
      expect(
        workerFn.Properties.Environment.Variables.REVIEW_QUEUE_URL,
      ).toEqual({ Ref: queueId });

      const workerRoleId = workerFn.Properties.Role["Fn::GetAtt"][0];
      const workerCanSend = Object.values(
        template.findResources("AWS::IAM::Policy"),
      ).some(
        (p: any) =>
          p.Properties.Roles?.some((r: any) => r.Ref === workerRoleId) &&
          p.Properties.PolicyDocument.Statement.some(
            (s: any) =>
              [s.Action].flat().includes("sqs:SendMessage") &&
              JSON.stringify(s.Resource).includes(queueId),
          ),
      );
      expect(workerCanSend).toBe(true);
    });
  });

  describe("Federated Catalog Role — Glue VPC connection", () => {
    // Regression guard: Glue managed/VPC federated connections require
    // ec2:CreateNetworkInterfacePermission (in addition to CreateNetworkInterface)
    // to attach the ENI to the managed service account. Without it MySQL/SQLServer
    // (Athena-federation) sources FAILED at query time with Athena
    // HIVE_METASTORE_ERROR / Glue "Unable to access VPC ... check the policies on
    // the IAM role" — confirmed live 2026-07-29 with valid networking + SG.
    it("grants the federated catalog role ec2:CreateNetworkInterfacePermission scoped to Glue", () => {
      // The grant lives in its own statement, scoped to network-interface/* AND
      // conditioned on ec2:AuthorizedService=glue.amazonaws.com so the role cannot
      // hand ENI-attach permission to an arbitrary service/account (least-privilege).
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "Ec2CreateNetworkInterfacePermissionForGlue",
              Action: "ec2:CreateNetworkInterfacePermission",
              Condition: {
                StringEquals: { "ec2:AuthorizedService": "glue.amazonaws.com" },
              },
            }),
          ]),
        },
      });
    });
  });

  describe("SSM Parameters", () => {
    it("publishes Lambda ARN to SSM", () => {
      template.hasResourceProperties("AWS::SSM::Parameter", {
        Type: "String",
        Description: "Sources API Lambda ARN",
      });
    });

    it("publishes sources table name to SSM", () => {
      template.hasResourceProperties("AWS::SSM::Parameter", {
        Type: "String",
        Description: "Sources DynamoDB table name",
      });
    });

    it("publishes source scan jobs table name to SSM", () => {
      template.hasResourceProperties("AWS::SSM::Parameter", {
        Type: "String",
        Description: "Source scan jobs DynamoDB table name",
      });
    });
  });

  describe("CfnOutputs", () => {
    it("exports SourcesTableName", () => {
      expect(Object.keys(template.findOutputs("SourcesTableName")).length).toBe(
        1,
      );
    });

    it("exports SourceScanJobsTableName", () => {
      expect(
        Object.keys(template.findOutputs("SourceScanJobsTableName")).length,
      ).toBe(1);
    });

    it("exports SourcesApiFnArn", () => {
      expect(Object.keys(template.findOutputs("SourcesApiFnArn")).length).toBe(
        1,
      );
    });
  });

  describe("Telemetry — CloudWatch Dashboard and Alarms", () => {
    it("emits a facade OE dashboard and no legacy SourcesScanDashboard", () => {
      // The facade creates exactly one env-prefixed dashboard for this stack.
      template.resourceCountIs("AWS::CloudWatch::Dashboard", 1);
      // Legacy dashboard name must be gone.
      const dashboards = template.findResources("AWS::CloudWatch::Dashboard", {
        Properties: { DashboardName: Match.stringLikeRegexp("sources-scan$") },
      });
      expect(Object.keys(dashboards)).toHaveLength(0);
    });

    it("still emits alarms for the sources pipeline (migrated to facade)", () => {
      // At least the pre-migration alarm count (8) survives the migration.
      const alarms = template.findResources("AWS::CloudWatch::Alarm");
      expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(8);
    });
  });

  describe("Database Connector Lambda (DbConnectorFn)", () => {
    it("has CONSUMER_QUERY_ROLE_ARN environment variable (from SSM)", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-db-connector$"),
        Environment: {
          Variables: Match.objectLike({
            CONSUMER_QUERY_ROLE_ARN: Match.anyValue(),
          }),
        },
      });
    });

    it("has LF_GRANTOR_ROLE_ARN environment variable", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-db-connector$"),
        Environment: {
          Variables: Match.objectLike({
            LF_GRANTOR_ROLE_ARN: Match.anyValue(),
          }),
        },
      });
    });
  });

  describe("Database Scan State Machine — SCAN_FAILED source update", () => {
    // The scan-failed branch writes three fields (status, updatedAt,
    // lastScanJobId) to the sources table instead of two. These assertions
    // pin the UpdateItem expression and the scanJobSK → lastScanJobId mapping
    // so a regression in the state machine definition fails the build.
    const stateMachineDefinition = (): string => {
      const machines = template.findResources(
        "AWS::StepFunctions::StateMachine",
      );
      // Serialize every state machine definition (incl. Fn::Join fragments)
      // so we can assert on the rendered Amazon States Language.
      return JSON.stringify(Object.values(machines));
    };

    it("writes lastScanJobId in the SCAN_FAILED source UpdateItem expression", () => {
      const definition = stateMachineDefinition();
      // The update expression sets status (#s), updatedAt (#u) and the new
      // lastScanJobId (#l) attribute.
      expect(definition).toContain("SET #s = :s, #u = :u, #l = :l");
      expect(definition).toContain("lastScanJobId");
      expect(definition).toContain("SCAN_FAILED");
    });

    it("maps scanJobSK from the state input into lastScanJobId", () => {
      const definition = stateMachineDefinition();
      // The lastScanJobId value (:l) is sourced from $.scanJobSK in the
      // execution input, not a literal.
      expect(definition).toContain("$.scanJobSK");
    });
  });

  describe("Scan timeout terminal state", () => {
    // Fix A: the DbEnrichment EcsRunTask carries a CATCHABLE per-task timeout
    // (States.Timeout) so an over-long enrichment routes through the error
    // chain to SCAN_FAILED, instead of the un-catchable execution-level
    // ExecutionTimedOut that stranded the source in ENRICHING.
    it("DbEnrichment task has a catchable per-task TimeoutSeconds and the state machine caps 2 min higher", () => {
      const machines = template.findResources(
        "AWS::StepFunctions::StateMachine",
      );
      // The ASL is embedded as an escaped JSON string inside a Fn::Join;
      // strip the backslash escaping so the rendered States Language can be
      // matched directly. The enrichment timeout is configurable
      // (dbScanEnrichmentTimeoutMinutes); default 120 min → taskTimeout 7200 s
      // on DbEnrichment, and the db-scan state-machine `TimeoutSeconds` is that
      // + 2 min = 7320 s. 7320 is unique to this state machine's own timeout,
      // so assert on it to prove the taskTimeout < execution-timeout ordering
      // that keeps the catchable path firing first.
      const definition = JSON.stringify(Object.values(machines)).replace(
        /\\/g,
        "",
      );
      // Per-task deadline on DbEnrichment (default 120 min). Both values live
      // inside the backslash-stripped ASL: the task-level 7200 in the
      // DbEnrichment state, and the state-machine execution ceiling 7320
      // (task + 2 min), which keeps the catchable States.Timeout firing before
      // the un-catchable ExecutionTimedOut.
      expect(definition).toContain('"TimeoutSeconds":7200');
      expect(definition).toContain('"TimeoutSeconds":7320');
    });

    // Fix B: the reaper is the out-of-band backstop for execution-level aborts
    // that are not catchable in-machine.
    it("creates the db-scan reaper Lambda in VPC with SOURCES_TABLE", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-db-scan-reaper$"),
        Handler: "coa_sources.database.pipeline.reaper_handler.handler",
        Runtime: "python3.12",
        Architectures: ["arm64"],
        VpcConfig: Match.objectLike({
          SubnetIds: Match.anyValue(),
          SecurityGroupIds: Match.anyValue(),
        }),
        Environment: {
          Variables: Match.objectLike({
            SOURCES_TABLE: Match.anyValue(),
          }),
        },
      });
    });

    it("has an EventBridge rule on aws.states execution status change filtering TIMED_OUT/ABORTED/FAILED with a Lambda target", () => {
      template.hasResourceProperties("AWS::Events::Rule", {
        EventPattern: Match.objectLike({
          source: ["aws.states"],
          "detail-type": ["Step Functions Execution Status Change"],
          detail: Match.objectLike({
            status: ["TIMED_OUT", "ABORTED", "FAILED"],
          }),
        }),
        Targets: Match.arrayWith([Match.objectLike({ Arn: Match.anyValue() })]),
      });
    });

    it("scopes the reaper rule to the db-scan state machine ARN", () => {
      // The rule must fire only for the db-scan pipeline, not any state machine.
      const rules = template.findResources("AWS::Events::Rule");
      const reaperRule = Object.values(rules).find(
        (r: any) =>
          r.Properties?.EventPattern?.detail?.stateMachineArn !== undefined,
      ) as any;
      expect(reaperRule).toBeDefined();
      expect(
        JSON.stringify(
          reaperRule.Properties.EventPattern.detail.stateMachineArn,
        ),
      ).toContain("DbScanStateMachine");
    });

    // Build a fresh SourcesStack with an extra context override, so the
    // configurable enrichment timeout can be exercised without disturbing the
    // shared `template` from beforeAll.
    const synthWithContext = (extra: Record<string, unknown>): Template => {
      const app = new cdk.App({ context: { ...TEST_CONTEXT, ...extra } });
      const network = new NetworkStack(app, "CtxNetwork", { env: TEST_ENV });
      const storage = new StorageStack(app, "CtxStorage", {
        network,
        env: TEST_ENV,
      });
      return Template.fromStack(
        new SourcesStack(app, "CtxSources", {
          network,
          storage,
          env: TEST_ENV,
        }),
      );
    };

    it("honors a custom dbScanEnrichmentTimeoutMinutes (task value + 2 min ceiling)", () => {
      const t = synthWithContext({ dbScanEnrichmentTimeoutMinutes: 30 });
      const machines = t.findResources("AWS::StepFunctions::StateMachine");
      const definition = JSON.stringify(Object.values(machines)).replace(
        /\\/g,
        "",
      );
      // 30 min → task 1800 s, state-machine ceiling 32 min → 1920 s.
      expect(definition).toContain('"TimeoutSeconds":1800');
      expect(definition).toContain('"TimeoutSeconds":1920');
    });

    it("rejects a non-positive / non-numeric dbScanEnrichmentTimeoutMinutes at synth", () => {
      expect(() =>
        synthWithContext({ dbScanEnrichmentTimeoutMinutes: 0 }),
      ).toThrow(/dbScanEnrichmentTimeoutMinutes must be a positive number/);
      expect(() =>
        synthWithContext({ dbScanEnrichmentTimeoutMinutes: "abc" }),
      ).toThrow(/dbScanEnrichmentTimeoutMinutes must be a positive number/);
    });
  });

  describe("VPC Configuration — All Lambdas in VPC (security baseline)", () => {
    it("DbScanTriggerFn is deployed in VPC", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-db-scan-trigger$"),
        VpcConfig: Match.objectLike({
          SubnetIds: Match.anyValue(),
          SecurityGroupIds: Match.anyValue(),
        }),
      });
    });

    it("SourcesDocDeletionCleanupFn is deployed in VPC", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-doc-deletion-cleanup$"),
        VpcConfig: Match.objectLike({
          SubnetIds: Match.anyValue(),
          SecurityGroupIds: Match.anyValue(),
        }),
      });
    });

    it("SourcesDocIngestionTriggerFn is deployed in VPC", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(
          ".*sources-doc-ingestion-trigger$",
        ),
        VpcConfig: Match.objectLike({
          SubnetIds: Match.anyValue(),
          SecurityGroupIds: Match.anyValue(),
        }),
      });
    });
  });

  describe("KG Build Observability", () => {
    it("enables Container Insights on the kg-build cluster so task memory is measurable", () => {
      // Without this, a task killed with no exit code leaves no memory data and
      // an OOM cannot be confirmed or ruled out.
      const clusters = template.findResources("AWS::ECS::Cluster");
      const kgBuildCluster = Object.values(clusters).find((c: any) =>
        String(c.Properties?.ClusterName ?? "").includes(
          "sources-doc-kg-build-cluster",
        ),
      ) as any;

      expect(kgBuildCluster).toBeDefined();
      expect(kgBuildCluster.Properties.ClusterSettings).toEqual(
        expect.arrayContaining([
          { Name: "containerInsights", Value: "enabled" },
        ]),
      );
    });

    it("sets DEPENDENCY_LOG_LEVEL on the kg-build container so graphrag INFO logs are retained", () => {
      // graphrag-toolkit logs via stdlib logging; its per-batch pipeline line
      // (num_workers, job_sizes) is INFO and is the only report of effective
      // write parallelism.
      const taskDefs = template.findResources("AWS::ECS::TaskDefinition");
      const kgBuildTaskDef = Object.values(taskDefs).find((t: any) =>
        t.Properties?.ContainerDefinitions?.some((c: any) =>
          String(c.Name ?? "").includes("sources-doc-kg-build"),
        ),
      ) as any;

      expect(kgBuildTaskDef).toBeDefined();
      const container = kgBuildTaskDef.Properties.ContainerDefinitions.find(
        (c: any) => String(c.Name ?? "").includes("sources-doc-kg-build"),
      );
      expect(container.Environment).toEqual(
        expect.arrayContaining([
          { Name: "DEPENDENCY_LOG_LEVEL", Value: "INFO" },
        ]),
      );
    });
  });

  describe("KG Build Task Role IAM Permissions", () => {
    it("Bedrock batch job actions are scoped to specific model ARNs, not wildcard", () => {
      const policies = template.findResources("AWS::IAM::Policy");
      const kgBuildPolicy = Object.values(policies).find((p: any) =>
        p.Properties?.PolicyDocument?.Statement?.some(
          (stmt: any) =>
            Array.isArray(stmt.Action) &&
            stmt.Action.includes("bedrock:CreateModelInvocationJob"),
        ),
      ) as any;

      expect(kgBuildPolicy).toBeDefined();
      const batchJobStatement =
        kgBuildPolicy.Properties.PolicyDocument.Statement.find(
          (stmt: any) =>
            Array.isArray(stmt.Action) &&
            stmt.Action.includes("bedrock:CreateModelInvocationJob"),
        );

      expect(batchJobStatement).toBeDefined();
      expect(batchJobStatement.Resource).not.toEqual(["*"]);
      const resourceStr = JSON.stringify(batchJobStatement.Resource);
      expect(resourceStr).toContain("foundation-model/*");
      expect(resourceStr).toContain("inference-profile/*");
    });

    it("Bedrock InvokeModel is scoped to specific model ARNs", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "bedrock:InvokeModel",
              Resource: Match.arrayWith([
                Match.stringLikeRegexp("arn:aws:bedrock:.*:foundation-model/"),
              ]),
            }),
          ]),
        },
      });
    });

    it("Bedrock batch job actions include all required management operations", () => {
      const policies = template.findResources("AWS::IAM::Policy");
      const kgBuildPolicy = Object.values(policies).find((p: any) =>
        p.Properties?.PolicyDocument?.Statement?.some(
          (stmt: any) =>
            Array.isArray(stmt.Action) &&
            stmt.Action.includes("bedrock:CreateModelInvocationJob"),
        ),
      ) as any;

      expect(kgBuildPolicy).toBeDefined();
      const batchJobStatement =
        kgBuildPolicy.Properties.PolicyDocument.Statement.find(
          (stmt: any) =>
            Array.isArray(stmt.Action) &&
            stmt.Action.includes("bedrock:CreateModelInvocationJob"),
        );

      expect(batchJobStatement.Action).toEqual(
        expect.arrayContaining([
          "bedrock:CreateModelInvocationJob",
          "bedrock:GetModelInvocationJob",
          "bedrock:ListModelInvocationJobs",
          "bedrock:StopModelInvocationJob",
        ]),
      );
    });

    it("kg-build task role can publish guardrail metrics to COA/Guardrails", () => {
      // The screener emits decisions via PutMetricData (matching the task's
      // other custom metrics), so the task role needs this grant.
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "cloudwatch:PutMetricData",
              Effect: "Allow",
              Resource: "*",
              Condition: {
                StringEquals: { "cloudwatch:namespace": "COA/Guardrails" },
              },
            }),
          ]),
        }),
      });
    });
  });

  describe("DB Enrichment Guardrail Wiring (#111 AC5/AC6)", () => {
    // Regression guard: the enrichment task ran UNGUARDED because
    // GUARDRAIL_SSM_PARAM was never set, so _resolve_guardrail_id() always
    // returned None and every Converse call went out without a guardrail.
    const enrichmentContainer = (): any => {
      const taskDefs = template.findResources("AWS::ECS::TaskDefinition");
      const taskDef = Object.values(taskDefs).find((t: any) =>
        JSON.stringify(t.Properties?.Family ?? "").includes(
          "sources-db-enrichment-agent",
        ),
      ) as any;
      expect(taskDef).toBeDefined();
      return taskDef.Properties.ContainerDefinitions[0];
    };

    it("sets GUARDRAIL_SSM_PARAM on the enrichment container", () => {
      const env = enrichmentContainer().Environment as Array<{
        Name: string;
        Value: unknown;
      }>;
      const param = env.find((e) => e.Name === "GUARDRAIL_SSM_PARAM");
      expect(param).toBeDefined();
      expect(JSON.stringify(param!.Value)).toContain("/bedrock/guardrail-id");
    });

    it("grants the enrichment task role bedrock:ApplyGuardrail", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "bedrock:ApplyGuardrail",
              Resource: Match.stringLikeRegexp("arn:aws:bedrock:.*guardrail/"),
            }),
          ]),
        }),
      });
    });

    it("grants the enrichment task role ssm:GetParameter on that exact param", () => {
      // Scoped to the one param, not the whole prefix — ApplyGuardrail is
      // useless without the id, and a wildcard here would leak every param.
      const policies = template.findResources("AWS::IAM::Policy");
      const statements = Object.values(policies).flatMap(
        (p: any) => p.Properties?.PolicyDocument?.Statement ?? [],
      );
      const ssmReads = statements.filter(
        (s: any) => s.Action === "ssm:GetParameter",
      );
      const guardrailRead = ssmReads.find((s: any) =>
        JSON.stringify(s.Resource).includes("/bedrock/guardrail-id"),
      );
      expect(guardrailRead).toBeDefined();
      expect(JSON.stringify(guardrailRead.Resource)).not.toContain("*");
    });
  });

  describe("Guarded ECS task defs carry the deployment region", () => {
    // Regression guard: both task defs shipped with GUARDRAIL_SSM_PARAM /
    // RETRIEVAL_GUARDRAIL_ID but no region env var. ECS injects none, so
    // resolve_region() fell back to us-east-1 and ApplyGuardrail was DENIED
    // by the region-scoped IAM policy in every non-us-east-1 deployment —
    // graph_build screening then failed OPEN.
    const containerFor = (family: string): { Environment?: unknown[] } => {
      const taskDefs = template.findResources("AWS::ECS::TaskDefinition");
      const taskDef = Object.values(taskDefs).find((t) =>
        JSON.stringify(
          (t as { Properties?: { Family?: unknown } }).Properties?.Family ?? "",
        ).includes(family),
      ) as {
        Properties: { ContainerDefinitions: { Environment?: unknown[] }[] };
      };
      expect(taskDef).toBeDefined();
      return taskDef.Properties.ContainerDefinitions[0];
    };

    for (const family of [
      "sources-db-enrichment-agent",
      "sources-doc-kg-build",
    ]) {
      it(`${family} sets AWS_DEFAULT_REGION and BEDROCK_REGION to the stack region`, () => {
        const env = (containerFor(family).Environment ?? []) as {
          Name: string;
          Value: unknown;
        }[];
        for (const name of ["AWS_DEFAULT_REGION", "BEDROCK_REGION"]) {
          const entry = env.find((e) => e.Name === name);
          expect(entry).toBeDefined();
          expect(entry!.Value).toEqual({ Ref: "AWS::Region" });
        }
      });
    }
  });
});

/**
 * Brownfield Lake Formation bootstrap.
 *
 * `PutDataLakeSettings` may only be called by an existing data-lake admin once an
 * account has any admins, and the caller is the LakeFormationAdmin custom
 * resource's own Lambda role. Auto-created, that role does not exist until the
 * deploy that needs it has already run — and a rollback deletes it and re-creates
 * it under a new auto-generated name, invalidating any registration made against
 * the previous ARN. `lf_admin_role_arn` lets an operator supply a role that is
 * already an admin, so registration happens once against a stable ARN.
 */
describe("SourcesStack — Lake Formation admin caller role", () => {
  const SUPPLIED_ROLE_ARN = "arn:aws:iam::123456789012:role/existing-lf-admin";

  function templateWith(extraContext: Record<string, unknown>): Template {
    const app = new cdk.App({ context: { ...TEST_CONTEXT, ...extraContext } });
    const network = new NetworkStack(app, "TestNetwork", { env: TEST_ENV });
    const storage = new StorageStack(app, "TestStorage", {
      network,
      env: TEST_ENV,
    });
    return Template.fromStack(
      new SourcesStack(app, "TestSources", {
        network,
        storage,
        allowedOrigin: "https://test.example.com",
        env: TEST_ENV,
      }),
    );
  }

  /** The onEvent Lambda of the LakeFormationAdmin custom resource. */
  function lfAdminOnEventRole(template: Template): unknown {
    const fns = template.findResources("AWS::Lambda::Function");
    const match = Object.entries(fns).find(([logicalId]) =>
      logicalId.includes("FederationProvisionerLfAdminOnEvent"),
    );
    expect(match).toBeDefined();
    return match![1].Properties.Role;
  }

  it("runs the custom resource as the supplied role when lf_admin_role_arn is set", () => {
    const template = templateWith({ lf_admin_role_arn: SUPPLIED_ROLE_ARN });

    // The literal ARN, not a GetAtt at a role this stack created — that is the
    // whole point: the ARN is stable and registerable before the first deploy.
    expect(lfAdminOnEventRole(template)).toEqual(SUPPLIED_ROLE_ARN);
  });

  it("still grants the supplied role LF settings access and Lambda logging", () => {
    const template = templateWith({ lf_admin_role_arn: SUPPLIED_ROLE_ARN });

    // A caller-supplied role is expected to be an LF admin, not to have guessed
    // the IAM statements the handler needs, so the construct attaches them.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              "lakeformation:GetDataLakeSettings",
              "lakeformation:PutDataLakeSettings",
            ]),
          }),
        ]),
      },
      Roles: Match.arrayWith(["existing-lf-admin"]),
    });

    // Passing an explicit role suppresses CDK's default basic-execution
    // attachment, which would silently drop the handler's warning logs. Granted
    // inline rather than as a managed policy: addManagedPolicy on an IMPORTED
    // role emits no resource at all, so this assertion is what catches a
    // regression back to that silent no-op.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["logs:PutLogEvents"]),
          }),
        ]),
      },
      Roles: Match.arrayWith(["existing-lf-admin"]),
    });
  });

  it("rejects a cross-account lf_admin_role_arn at synth", () => {
    // CDK imports a cross-account role as immutable, so the permission
    // attachments would silently no-op and the failure would surface at deploy
    // time as an unrelated-looking AccessDenied on SSM. Fail loudly instead.
    expect(() =>
      templateWith({
        lf_admin_role_arn: "arn:aws:iam::999988887777:role/foreign-lf-admin",
      }),
    ).toThrow(/must be a role in this account/);
  });

  it("auto-creates the caller role when lf_admin_role_arn is absent (greenfield self-bootstrap)", () => {
    const template = templateWith({});

    // Greenfield accounts have no admin for LF to enforce, so the auto-created
    // role can register itself — no operator action required.
    expect(lfAdminOnEventRole(template)).toEqual({
      "Fn::GetAtt": [
        expect.stringContaining(
          "FederationProvisionerLfAdminOnEventServiceRole",
        ),
        "Arn",
      ],
    });
  });
});
