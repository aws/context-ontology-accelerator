// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Metric Service Stack — Lambda handlers for metric CRUD operations.
 *
 * Provisions:
 * - Metric API Lambda (combined handler for create/get/list/update/delete)
 * - IAM permissions for Neptune, OpenSearch, EventBridge, Bedrock
 * - SSM parameter publishing the Lambda ARN for the API stack
 */
import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as opensearchserverless from "aws-cdk-lib/aws-opensearchserverless";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import { SCLStack } from "../../constructs/scl-stack";
import { AccessLogsBucket, SclMonitoring } from "../../constructs";
import { Paths, fromRoot } from "../../paths";
import type { IAlarmActionStrategy } from "cdk-monitoring-constructs/lib/common/alarm/action";
import { resolveContext } from "../../context";
import {
  DEFAULT_EVENT_BUS_NAME,
  DEFAULT_BEDROCK_MODEL_ID,
  DEFAULT_GRAPH_URI_BASE,
} from "../../constants";
import { bundlePython } from "../../utils/python-bundling";
import { bedrockModelArn } from "../../utils/bedrock-utils";

export interface MetricServiceStackProps extends cdk.StackProps {
  /** VPC for Lambda placement. */
  readonly vpc: ec2.IVpc;

  /** Shared Lambda security group (from network stack). */
  readonly lambdaSecurityGroup: ec2.ISecurityGroup;

  /** AOSS VPC endpoint security group — needs ingress rule for Lambda access. */
  readonly aossSecurityGroup: ec2.ISecurityGroup;

  /** Neptune cluster writer endpoint (host only, no protocol/port). */
  readonly neptuneEndpoint: string;

  /** Neptune cluster ARN for IAM scoping. */
  readonly neptuneClusterArn: string;

  /** EventBridge bus name for metric lifecycle events. */
  readonly eventBusName?: string;

  /** Bedrock model ID for embeddings. */
  readonly bedrockModelId?: string;

  /** Allowed CORS origin (e.g. "https://app.example.com"). Defaults to "*". */
  readonly allowedOrigin?: string;

  /** OE alarm action strategy (optional, for monitoring alarms). */
  readonly alarmAction?: IAlarmActionStrategy;
}

export class MetricServiceStack extends SCLStack {
  public readonly metricApiFn: lambda.Function;

  constructor(scope: Construct, id: string, props: MetricServiceStackProps) {
    super(scope, id, props);

    const { ssmPrefix } = resolveContext(this.node);
    const eventBusName = props.eventBusName ?? DEFAULT_EVENT_BUS_NAME;
    const bedrockModelId = props.bedrockModelId ?? DEFAULT_BEDROCK_MODEL_ID;

    // ── OpenSearch config via SSM ──────────────────────────────────────
    const opensearchEndpoint = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/opensearch/endpoint`,
    );
    const opensearchCollectionName =
      ssm.StringParameter.valueForStringParameter(
        this,
        `${ssmPrefix}/opensearch/collection-name`,
      );
    const opensearchCollectionArn = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/opensearch/collection-arn`,
    );

    // ── SMUS catalog access (metric validation Checks 2-5) ─────────
    // Resolved via SSM dynamic refs to avoid direct stack dependencies.
    // Same pattern as OntologyStack (ontology-stack.ts).
    const sourcesTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/sources/sources-table-name`,
    );
    const namespacesTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/namespace/namespaces-table-name`,
    );
    const smusDomainId = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/smus/domain-id`,
    );
    const projectAccessRoleArn = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/smus/dz-project-access-role-arn`,
    );

    // ── Lambda code bundle ─────────────────────────────────────────
    const lambdaCode = bundlePython({
      srcDirs: [
        fromRoot("packages/metric-service/src"),
        Paths.commonLib,
        Paths.smithyGeneratedControlPlanePythonServer,
      ],
      requirementsFile: fromRoot("packages/metric-service/requirements.txt"),
    });

    // ── S3 Bucket — OSI import/export staging ────────────────────────
    const osiAccessLogs = new AccessLogsBucket(this, "OsiAccessLogs", {
      nameSuffix: "metric-logs",
    });
    const osiBucket = new s3.Bucket(this, "OsiBucket", {
      bucketName: cdk.Lazy.string({
        produce: () => `${this.prefixed("metric-osi")}-${this.account}`,
      }),
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      serverAccessLogsBucket: osiAccessLogs.bucket,
      serverAccessLogsPrefix: "metric-osi/",
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      cors: [
        {
          allowedOrigins: [props.allowedOrigin ?? "*"],
          allowedMethods: [s3.HttpMethods.PUT, s3.HttpMethods.GET],
          allowedHeaders: ["Content-Type"],
          maxAge: 3600,
        },
      ],
      lifecycleRules: [
        {
          // Keep pinned sources and checkpoints beyond the 4-day source queue
          // and 14-day DLQ recovery windows.
          expiration: cdk.Duration.days(30),
          noncurrentVersionExpiration: cdk.Duration.days(30),
        },
      ],
    });

    // ── Metric API Lambda ──────────────────────────────────────────
    this.metricApiFn = new lambda.Function(this, "MetricApiFn", {
      functionName: this.prefixed("metric-api"),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "coa_metrics.api.metric_api_handler.handler",
      code: lambdaCode,
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.lambdaSecurityGroup],
      environment: {
        NEPTUNE_ENDPOINT: `https://${props.neptuneEndpoint}:8182`,
        NDB_GRAPH_URI_BASE: DEFAULT_GRAPH_URI_BASE,
        OPENSEARCH_ENDPOINT: opensearchEndpoint,
        OSS_INDEX_PREFIX: opensearchCollectionName,
        EVENTBRIDGE_BUS_NAME: eventBusName,
        BEDROCK_MODEL_ID: bedrockModelId,
        BEDROCK_REGION: this.region,
        ALLOWED_ORIGIN: props.allowedOrigin ?? "*",
        OSI_BUCKET_NAME: osiBucket.bucketName,
        // SMUS catalog access — metric validation Checks 2-5
        DATA_SOURCES_TABLE: sourcesTableName,
        NAMESPACES_TABLE: namespacesTableName,
        SMUS_DOMAIN_ID: smusDomainId,
        PROJECT_ACCESS_ROLE_ARN: projectAccessRoleArn,
      },
    });

    // ── IAM: S3 (OSI import/export) ──────────────────────────────
    this.metricApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
        resources: [`${osiBucket.bucketArn}/*`],
      }),
    );

    // ── IAM: Neptune (SPARQL read/write/delete) ──────────────────
    this.metricApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "neptune-db:ReadDataViaQuery",
          "neptune-db:WriteDataViaQuery",
          "neptune-db:DeleteDataViaQuery",
        ],
        resources: [props.neptuneClusterArn],
      }),
    );

    // ── IAM: OpenSearch Serverless ─────────────────────────────────
    this.metricApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["aoss:APIAccessAll"],
        resources: [opensearchCollectionArn],
      }),
    );

    // ── IAM: Bedrock (embedding generation) ────────────────────────
    // The embed model is an inference profile (e.g. us.cohere.embed-v4:0) — grant
    // the profile ARN AND the underlying cross-region foundation-model ARNs it
    // fans out to (on-demand is unsupported, so a bare foundation-model ARN is
    // insufficient).
    this.metricApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel"],
        resources: [
          bedrockModelArn(bedrockModelId, "*", this.account),
          // Invoking an inference profile also requires permission on the
          // underlying foundation model it routes to, hence the wildcard.
          `arn:aws:bedrock:*::foundation-model/*`,
        ],
      }),
    );

    // ── IAM: EventBridge (lifecycle events) ────────────────────────
    this.metricApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["events:PutEvents"],
        resources: [
          `arn:aws:events:${this.region}:${this.account}:event-bus/${eventBusName}`,
        ],
      }),
    );

    // ── IAM: SMUS catalog access (metric validation Checks 2-5) ───
    const sourcesTable = dynamodb.Table.fromTableName(
      this,
      "SourcesTable",
      sourcesTableName,
    );
    sourcesTable.grantReadData(this.metricApiFn);

    const namespacesTable = dynamodb.Table.fromTableName(
      this,
      "NamespacesTable",
      namespacesTableName,
    );
    namespacesTable.grantReadData(this.metricApiFn);

    this.metricApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["sts:AssumeRole"],
        resources: [projectAccessRoleArn],
      }),
    );

    // ── AOSS VPC endpoint access ──────────────────────────────────
    // Allow the Lambda security group to reach the AOSS VPC endpoint
    props.aossSecurityGroup.addIngressRule(
      props.lambdaSecurityGroup as ec2.SecurityGroup,
      ec2.Port.tcp(443),
      "Allow HTTPS from metric Lambda to AOSS VPC endpoint",
    );

    // ── AOSS Data Access Policy ───────────────────────────────────
    // OpenSearch Serverless requires a data access policy in addition
    // to IAM permissions. Without this, the role cannot read/write documents.
    new opensearchserverless.CfnAccessPolicy(this, "OSSDataAccessPolicy", {
      name: this.prefixed("metric-data-access"),
      type: "data",
      policy: JSON.stringify([
        {
          Rules: [
            {
              ResourceType: "index",
              Resource: [`index/${opensearchCollectionName}/*`],
              Permission: [
                "aoss:CreateIndex",
                "aoss:UpdateIndex",
                "aoss:DescribeIndex",
                "aoss:ReadDocument",
                "aoss:WriteDocument",
              ],
            },
            {
              ResourceType: "collection",
              Resource: [`collection/${opensearchCollectionName}`],
              Permission: [
                "aoss:CreateCollectionItems",
                "aoss:DescribeCollectionItems",
                "aoss:UpdateCollectionItems",
              ],
            },
          ],
          Principal: [this.metricApiFn.role!.roleArn],
        },
      ]),
    });

    // ── Publish Lambda ARN to SSM ──────────────────────────────────
    new ssm.StringParameter(this, "SsmMetricApiFnArn", {
      parameterName: `${ssmPrefix}/metric/api-fn-arn`,
      stringValue: this.metricApiFn.functionArn,
    });

    // ── DynamoDB: Import Jobs table ────────────────────────────────
    const importJobsTable = new dynamodb.Table(this, "ImportJobsTable", {
      tableName: this.prefixed("metric-import-jobs"),
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: "ttl",
    });

    // ── SQS: Import queue + DLQ ────────────────────────────────────
    const importDlq = new sqs.Queue(this, "ImportDLQ", {
      queueName: this.prefixed("metric-import-dlq"),
      retentionPeriod: cdk.Duration.days(14),
      // Lambda's SQS guidance requires at least 6x the 30-second
      // recovery timeout to avoid concurrent delivery while throttled.
      visibilityTimeout: cdk.Duration.minutes(3),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });

    const importQueue = new sqs.Queue(this, "ImportQueue", {
      queueName: this.prefixed("metric-import-queue"),
      visibilityTimeout: cdk.Duration.minutes(15),
      retentionPeriod: cdk.Duration.days(4),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      deadLetterQueue: {
        queue: importDlq,
        maxReceiveCount: 6,
      },
    });

    // ── Import Worker Lambda ───────────────────────────────────────
    const importWorkerFn = new lambda.Function(this, "ImportWorkerFn", {
      functionName: this.prefixed("metric-import-worker"),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "coa_metrics.api.import_worker.handler",
      code: lambdaCode,
      timeout: cdk.Duration.minutes(15),
      memorySize: 512,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.lambdaSecurityGroup],
      environment: {
        NEPTUNE_ENDPOINT: `https://${props.neptuneEndpoint}:8182`,
        NDB_GRAPH_URI_BASE: DEFAULT_GRAPH_URI_BASE,
        OPENSEARCH_ENDPOINT: opensearchEndpoint,
        OSS_INDEX_PREFIX: opensearchCollectionName,
        BEDROCK_MODEL_ID: bedrockModelId,
        BEDROCK_REGION: this.region,
        OSI_BUCKET_NAME: osiBucket.bucketName,
        IMPORT_QUEUE_URL: importQueue.queueUrl,
        IMPORT_JOBS_TABLE: importJobsTable.tableName,
        // Longer than the 15-minute worker timeout, so a timed-out owner
        // cannot overlap Neptune writes with a retrying delivery.
        IMPORT_OFFSET_LEASE_SECONDS: "960",
        // SMUS catalog access — dataset resolution during async import.
        // Without these the worker's data source lookup can never initialize,
        // which previously silently degraded to the permissive (accept-all)
        // fallback and now fails the job (fail-closed).
        DATA_SOURCES_TABLE: sourcesTableName,
        NAMESPACES_TABLE: namespacesTableName,
        SMUS_DOMAIN_ID: smusDomainId,
        PROJECT_ACCESS_ROLE_ARN: projectAccessRoleArn,
      },
    });

    // Worker triggered by SQS (batch size 1 — each message is one chunk)
    importWorkerFn.addEventSource(
      new lambdaEventSources.SqsEventSource(importQueue, {
        batchSize: 1,
      }),
    );

    // DLQ recovery deliberately stays outside the VPC: it only accesses SQS
    // and DynamoDB, and must not share the worker's ENI cold-start failure mode.
    const importDlqRecoveryFn = new lambda.Function(
      this,
      "ImportDlqRecoveryFn",
      {
        functionName: this.prefixed("metric-import-dlq-recovery"),
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: "coa_metrics.api.import_dlq_handler.handler",
        code: lambdaCode,
        timeout: cdk.Duration.seconds(30),
        memorySize: 256,
        environment: {
          IMPORT_QUEUE_URL: importQueue.queueUrl,
          IMPORT_JOBS_TABLE: importJobsTable.tableName,
          IMPORT_REDRIVE_DELAY_SECONDS: "60",
        },
      },
    );
    importDlqRecoveryFn.addEventSource(
      new lambdaEventSources.SqsEventSource(importDlq, {
        batchSize: 1,
      }),
    );

    // ── IAM: Worker permissions ────────────────────────────────────
    importJobsTable.grantReadWriteData(importWorkerFn);
    importQueue.grantSendMessages(importWorkerFn);
    // Read only namespace-scoped import sources and checkpoints. The broader
    // imports prefix is retained for in-flight legacy jobs whose source keys
    // predate the dedicated imports/jobs sub-prefix.
    importWorkerFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetObject", "s3:GetObjectVersion"],
        resources: [`${osiBucket.bucketArn}/*/imports/*`],
      }),
    );
    importWorkerFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["s3:PutObject"],
        resources: [`${osiBucket.bucketArn}/*/imports/checkpoints/*`],
      }),
    );
    importDlqRecoveryFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:GetItem", "dynamodb:UpdateItem"],
        resources: [importJobsTable.tableArn],
      }),
    );
    importQueue.grantSendMessages(importDlqRecoveryFn);

    // SMUS catalog access — same grants as the API Lambda
    sourcesTable.grantReadData(importWorkerFn);
    namespacesTable.grantReadData(importWorkerFn);
    importWorkerFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["sts:AssumeRole"],
        resources: [projectAccessRoleArn],
      }),
    );

    importWorkerFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "neptune-db:ReadDataViaQuery",
          "neptune-db:WriteDataViaQuery",
          "neptune-db:DeleteDataViaQuery",
          "neptune-db:connect",
        ],
        resources: [props.neptuneClusterArn],
      }),
    );

    importWorkerFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["aoss:APIAccessAll"],
        resources: [opensearchCollectionArn],
      }),
    );

    importWorkerFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel"],
        resources: [
          bedrockModelArn(bedrockModelId, "*", this.account),
          // See the metric-api grant above: a profile also needs invoke on the
          // underlying foundation model.
          `arn:aws:bedrock:*::foundation-model/*`,
        ],
      }),
    );

    // ── IAM: API Lambda needs DynamoDB + SQS for async imports ─────
    importJobsTable.grantReadWriteData(this.metricApiFn);
    importQueue.grantSendMessages(this.metricApiFn);

    // Add env vars to API Lambda for async import
    this.metricApiFn.addEnvironment("IMPORT_QUEUE_URL", importQueue.queueUrl);
    this.metricApiFn.addEnvironment(
      "IMPORT_JOBS_TABLE",
      importJobsTable.tableName,
    );

    // ================================================================
    // OE monitoring (cdk-monitoring-constructs)
    // ================================================================
    new SclMonitoring(this, "Monitoring", {
      alarmNamePrefix: this.prefixed("metric-service"),
      alarmAction: props.alarmAction,
    })
      .monitorLambda(this.metricApiFn)
      .monitorLambda(importWorkerFn)
      .monitorLambda(importDlqRecoveryFn)
      .monitorQueueWithDlq(importQueue, importDlq)
      .monitorTable(importJobsTable);

    // ── Outputs ────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "MetricApiFnArn", {
      value: this.metricApiFn.functionArn,
      description: "Metric API Lambda function ARN",
    });
  }
}
