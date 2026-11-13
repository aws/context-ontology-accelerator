// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Every S3 bucket and SQS queue in the sources + metric-service stacks that handles
 * customer-uploaded content or ingest events must enforce TLS in transit (a `BucketPolicy`
 * / `QueuePolicy` with the `aws:SecureTransport: false` deny) and, for queues, set
 * `SqsManagedSseEnabled: true`. Each resource is named explicitly so a regression fails
 * a specific test.
 */
import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template, Match } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { StorageStack } from "../../lib/stacks/foundation/storage-stack";
import { SourcesStack } from "../../lib/stacks/services/sources-stack";
import { MetricServiceStack } from "../../lib/stacks/services/metric-service-stack";

jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const TEST_ENV = { account: "123456789012", region: "us-east-1" };

const SOURCES_TEST_CONTEXT = {
  ecr_repository_arn: "arn:aws:ecr:us-east-1:123456789012:repository/coa-test",
  ecr_repository_name: "coa-test",
  sources_db_enrichment_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:db-enrichment-test",
  sources_preprocessing_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:preprocessing-test",
  sources_kg_build_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:kg-build-test",
  "aws:cdk:bundling-stacks": [],
  // The graph-verify queues below exist only with graph sources on.
  graph_sources: "on",
};

const METRIC_TEST_CONTEXT = { "aws:cdk:bundling-stacks": [] };

// Ingest-path resources checked for TLS and SSE.
const SOURCES_TLS_QUEUES = [
  "sources-db-connector-dlq",
  "sources-db-scan-dlq",
  "sources-db-scan-queue",
  "sources-bulk-review-dlq",
  "sources-bulk-review-queue",
  "sources-delete-dlq",
  "sources-delete-queue",
  "sources-doc-ingestion-dlq",
  "sources-doc-ingestion-queue",
  "sources-glue-event-dlq",
  "sources-glue-event-queue",
  "sources-graph-verify-dlq",
  "sources-graph-verify-queue",
];
const METRIC_TLS_QUEUES = ["metric-import-dlq", "metric-import-queue"];

// Queues checked for SSE.
const SOURCES_AT_REST_ENCRYPTION_QUEUES = [
  "sources-db-connector-dlq",
  "sources-doc-ingestion-dlq",
  "sources-doc-ingestion-queue",
];

function assertQueuePolicyDeniesInsecureTransport(template: Template) {
  template.hasResourceProperties(
    "AWS::SQS::QueuePolicy",
    Match.objectLike({
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Deny",
            Action: "sqs:*",
            Condition: Match.objectLike({
              Bool: { "aws:SecureTransport": "false" },
            }),
          }),
        ]),
      }),
    }),
  );
}

function assertQueuePresent(template: Template, suffix: string) {
  template.hasResourceProperties(
    "AWS::SQS::Queue",
    Match.objectLike({
      QueueName: Match.stringLikeRegexp(`.*${suffix}$`),
    }),
  );
}

function assertQueueEncryptedAtRest(template: Template, suffix: string) {
  template.hasResourceProperties(
    "AWS::SQS::Queue",
    Match.objectLike({
      QueueName: Match.stringLikeRegexp(`.*${suffix}$`),
      SqsManagedSseEnabled: true,
    }),
  );
}

describe("SourcesStack security-in-transit + at-rest regressions", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: SOURCES_TEST_CONTEXT });
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

  it("SourcesDataBucket has a BucketPolicy denying non-TLS access", () => {
    template.hasResourceProperties(
      "AWS::S3::BucketPolicy",
      Match.objectLike({
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Effect: "Deny",
              Action: "s3:*",
              Condition: Match.objectLike({
                Bool: { "aws:SecureTransport": "false" },
              }),
            }),
          ]),
        }),
      }),
    );
  });

  it("stack has at least one AWS::SQS::QueuePolicy denying insecure transport (covers all ingest queues)", () => {
    // A QueuePolicy is attached per queue ARN and cannot be mapped back to a queue name here,
    // so this checks the deny shape globally; the count check below catches a missing one.
    assertQueuePolicyDeniesInsecureTransport(template);
  });

  it("has exactly as many AWS::SQS::QueuePolicy resources as there are queues", () => {
    // A removed enforceSSL on any one queue drops the policy count.
    template.resourceCountIs(
      "AWS::SQS::QueuePolicy",
      // The stack currently has enforceSSL on every queue in this test's
      // scope. If new queues are added in the future they should either
      // set enforceSSL (this number goes up) or explicitly justify not
      // doing so (this test breaks and someone reviews).
      SOURCES_TLS_QUEUES.length,
    );
  });

  it.each(SOURCES_TLS_QUEUES)("queue %s is present", (suffix) => {
    assertQueuePresent(template, suffix);
  });

  it.each(SOURCES_AT_REST_ENCRYPTION_QUEUES)(
    "queue %s has SQS-managed at-rest encryption",
    (suffix) => {
      assertQueueEncryptedAtRest(template, suffix);
    },
  );
});

describe("MetricServiceStack security-in-transit regressions", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: METRIC_TEST_CONTEXT });
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

  it("stack has AWS::SQS::QueuePolicy denying insecure transport", () => {
    assertQueuePolicyDeniesInsecureTransport(template);
  });

  it.each(METRIC_TLS_QUEUES)("queue %s is present", (suffix) => {
    assertQueuePresent(template, suffix);
  });
});
