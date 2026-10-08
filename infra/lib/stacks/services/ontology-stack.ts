// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as opensearchserverless from "aws-cdk-lib/aws-opensearchserverless";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as ssm from "aws-cdk-lib/aws-ssm";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import type { IAlarmActionStrategy } from "cdk-monitoring-constructs/lib/common/alarm/action";
import { Construct } from "constructs";

import { SCLStack } from "../../constructs/scl-stack";
import { SclMonitoring } from "../../constructs";
import { Paths } from "../../paths";
import { resolveContext } from "../../context";
import { bundlePython } from "../../utils/python-bundling";
import { NetworkStack } from "../foundation/network-stack";
import {
  DEFAULT_BEDROCK_MODEL_ID,
  DEFAULT_BEDROCK_CHAT_MODEL_ID,
  DEFAULT_BEDROCK_INDUCTION_MODEL_ID,
} from "../../constants";

export interface OntologyStackProps extends cdk.StackProps {
  readonly network: NetworkStack;
  readonly neptuneClusterArn: string;
  readonly neptuneClusterEndpoint: string;
  readonly ontologyArtifactsBucket: s3.IBucket;
  /**
   * Required: scopes the task role's DataZone IAM grants to a single
   * domain. We never deploy without one — running with no domain would
   * either need a wildcard policy (forbidden) or break the catalog reader
   * at runtime, so fail at synth time instead.
   */
  readonly smusDomainId: string;
  /**
   * CORS allowed origin for the API proxy Lambda.
   * Production deployments require an explicit origin (e.g. https://app.example.com).
   */
  readonly allowedOrigin: string;

  readonly cpu?: number;
  readonly memoryLimitMiB?: number;
  readonly containerPort?: number;
  readonly desiredCount?: number;
  /** Shared Cloud Map private DNS namespace for service discovery. */
  readonly serviceNamespace: servicediscovery.IPrivateDnsNamespace;
  /**
   * Bedrock model IDs, resolved from the SSM deploy config (#94). Omitted →
   * the shared defaults apply, so a deployment that configures nothing behaves
   * exactly as before. Set these to deploy outside the US, where the default
   * `us.` inference profiles are not invocable.
   */
  readonly bedrockEmbedModelId?: string;
  readonly bedrockEmbedDimensions?: number;
  readonly bedrockInductionLlmModelId?: string;
  readonly bedrockChatModelId?: string;
  readonly alarmAction?: IAlarmActionStrategy;
}

/**
 * Service stack: Ontology Engine — induction, catalog, validation, and
 * embedding management for the SemanticContext platform.
 *
 * Deploys the ontology-engine FastAPI service on ECS Fargate with access to:
 *   - Neptune DB (shared) — read/write for catalog projection + Turtle load
 *   - OpenSearch Serverless (shared) — read/write for lexical embeddings
 *   - DynamoDB (own table) — jobs, proposals, namespace registry
 *   - Bedrock — LLM (RIGOR induction) + embedding (Cohere Embed v4)
 *   - DataZone/SMUS — read approved catalog metadata
 *
 * Reachable via Cloud Map private DNS (resolvable VPC-wide):
 *   - http://ontology-engine:8001
 */
export class OntologyStack extends SCLStack {
  public readonly cluster: ecs.Cluster;
  public readonly service: ecs.FargateService;
  public readonly table: dynamodb.Table;
  public readonly containerPort: number;

  constructor(scope: Construct, id: string, props: OntologyStackProps) {
    super(scope, id, props);
    this.addComponentTag("ontology-engine");

    if (!props.smusDomainId) {
      throw new Error(
        "OntologyStack requires smusDomainId — DataZone permissions cannot be granted without a domain scope.",
      );
    }
    const { ssmPrefix } = resolveContext(this.node);
    const vpc = props.network.vpc;
    const ecsSecurityGroup = props.network.ecsSecurityGroup;

    // ── Bedrock model IDs (#94) ────────────────────────────────────────
    // Resolved ONCE here so the container environment, the IAM grants, and the
    // cost dashboard's ModelId dimensions all derive from the same value. They
    // used to be independent literals in this file, so changing the model for a
    // non-US deploy left the dashboard querying a model nothing invokes (empty
    // widgets). Config wins when set; otherwise the shared defaults apply, so a
    // deployment configuring nothing is unchanged.
    const embedModelId = props.bedrockEmbedModelId ?? DEFAULT_BEDROCK_MODEL_ID;
    const embedDimensions = String(props.bedrockEmbedDimensions ?? 1024);
    const inductionModelId =
      props.bedrockInductionLlmModelId ?? DEFAULT_BEDROCK_INDUCTION_MODEL_ID;
    const chatModelId =
      props.bedrockChatModelId ?? DEFAULT_BEDROCK_CHAT_MODEL_ID;

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

    // 8 vCPU / 32 GB. Induction + accept (Bedrock embeds) and async ontology
    // teardown (Neptune DROP + AOSS drain) share this single task; 2 vCPU
    // bottlenecked BIRD e2e runs. The 20k-table induction fusion peak (~24-26 GB
    // from 360k column vectors held alongside one transient channel) OOM-killed
    // the task at 16 GB, so bump to 32 GB — 8 vCPU Fargate supports 16-60 GB in
    // 4 GB steps.
    const cpu = props.cpu ?? 8192;
    const memoryLimitMiB = props.memoryLimitMiB ?? 32768;
    this.containerPort = props.containerPort ?? 8001;
    const desiredCount = props.desiredCount ?? 1;

    const namespace = props.serviceNamespace;

    // ── Cross-stack table names via SSM ──────────────────────────────
    // We avoid direct construct prop refs (which create CloudFormation
    // cross-stack exports) and instead read the table names from SSM
    // parameters published by NamespaceStack and SourcesStack.
    const namespacesTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/namespace/namespaces-table-name`,
    );
    // Unified sources table — source of truth for both DATABASE and DOCUMENTS
    // sources. Published by SourcesStack.
    const sourcesTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/sources/sources-table-name`,
    );

    // DataZone project-access role — IAM role registered as a member of
    // the namespace's DataZone project. The ECS task role assumes this role
    // before calling DataZone Search/GetAsset (project membership, not just
    // IAM permission, gates DataZone reads). Same pattern SourcesStack uses.
    const projectAccessRoleArn = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/smus/dz-project-access-role-arn`,
    );
    const projectAccessRole = iam.Role.fromRoleArn(
      this,
      "ProjectAccessRole",
      projectAccessRoleArn,
    );

    // ================================================================
    // DynamoDB Table (single-table PK/SK design)
    // ================================================================
    this.table = new dynamodb.Table(this, "OntologyEngineTable", {
      tableName: this.prefixed("ontology-engine"),
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy:
        this.envName === "prod"
          ? cdk.RemovalPolicy.RETAIN
          : cdk.RemovalPolicy.DESTROY,
    });

    // ================================================================
    // ECS Cluster
    // ================================================================
    this.cluster = new ecs.Cluster(this, "OntologyCluster", {
      clusterName: this.prefixed("ontology-cluster"),
      vpc,
    });

    // ================================================================
    // Task Definition
    // ================================================================
    const taskDef = new ecs.FargateTaskDefinition(this, "OntologyTaskDef", {
      family: this.prefixed("ontology-engine"),
      cpu,
      memoryLimitMiB,
    });

    const logGroup = new logs.LogGroup(this, "OntologyLogGroup", {
      logGroupName: `/ecs/${this.prefixed("ontology-engine")}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── Container image ──────────────────────────────────────────────
    const imageUri = this.node.tryGetContext("ontology_engine_image_uri") as
      | string
      | undefined;
    const ecrRepositoryArn = this.node.tryGetContext("ecr_repository_arn") as
      | string
      | undefined;
    const ecrRepositoryName = this.node.tryGetContext("ecr_repository_name") as
      | string
      | undefined;

    let containerImage: ecs.ContainerImage;
    if (imageUri && ecrRepositoryArn && ecrRepositoryName) {
      const ecrRepo = cdk.aws_ecr.Repository.fromRepositoryAttributes(
        this,
        "OntologyEcrRepo",
        { repositoryArn: ecrRepositoryArn, repositoryName: ecrRepositoryName },
      );
      containerImage = ecs.ContainerImage.fromEcrRepository(
        ecrRepo,
        imageUri.split(":").pop() || "latest",
      );
    } else {
      containerImage = ecs.ContainerImage.fromAsset(Paths.root, {
        file: "packages/ontology-engine/Dockerfile",
        platform: cdk.aws_ecr_assets.Platform.LINUX_AMD64,
      });
    }

    taskDef.addContainer("OntologyEngineContainer", {
      containerName: this.prefixed("ontology-engine"),
      image: containerImage,
      environment: {
        WORKBENCH_BACKEND: "opensearch_neptune",
        NDB_ENDPOINT: `https://${props.neptuneClusterEndpoint}:8182`,
        NDB_IAM_AUTH: "true",
        NDB_REGION: cdk.Aws.REGION,
        OSS_ENDPOINT: opensearchEndpoint,
        OSS_REGION: cdk.Aws.REGION,
        OSS_INDEX: opensearchCollectionName,
        OSS_DIMENSIONS: embedDimensions,
        ONTOLOGY_ARTIFACTS_BUCKET: props.ontologyArtifactsBucket.bucketName,
        DYNAMODB_TABLE: this.table.tableName,
        DYNAMODB_REGION: cdk.Aws.REGION,
        BEDROCK_REGION: cdk.Aws.REGION,
        BEDROCK_EMBED_MODEL_ID: embedModelId,
        BEDROCK_EMBED_DIMENSIONS: embedDimensions,
        LLM_MODEL_ID: inductionModelId,
        // Description generation reads its own variable (main.py) — set it from
        // the same resolved value so it cannot silently stay on a `us.` profile.
        DESCRIPTION_LLM_MODEL_ID: inductionModelId,
        // Shared BedrockClient default (constraint inference / nl_generator).
        BEDROCK_CHAT_MODEL_ID: chatModelId,
        LLM_REGION: cdk.Aws.REGION,
        // SSM path to the Bedrock guardrail id — the app resolves it at runtime
        // and applies content filtering to LLM calls (constraint inference).
        GUARDRAIL_SSM_PARAM: `${ssmPrefix}/bedrock/guardrail-id`,
        CATALOG_SOURCE: "smus",
        SMUS_DOMAIN_ID: props.smusDomainId,
        NAMESPACES_TABLE: namespacesTableName,
        DATASOURCES_TABLE: sourcesTableName,
        SOURCES_TABLE: sourcesTableName,
        PROJECT_ACCESS_ROLE_ARN: projectAccessRoleArn,
        SOURCES_API_FN_NAME: `${this.prefixed("sources-api")}`,
        INDUCE_OUTPUT_DIR: "/tmp/induce",
        PORT: String(this.containerPort),
      },
      portMappings: [
        {
          containerPort: this.containerPort,
          protocol: ecs.Protocol.TCP,
        },
      ],
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "ontology-engine",
        logGroup,
      }),
      healthCheck: {
        command: [
          "CMD-SHELL",
          `curl -sf http://localhost:${this.containerPort}/health || exit 1`,
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(10),
        retries: 5,
        // 90s gives FastAPI module-load (Neptune/OSS/Bedrock client construction
        // and AOSS data-access-policy propagation) time to settle before the
        // first probe is counted against the failure budget.
        startPeriod: cdk.Duration.seconds(90),
      },
    });

    // ================================================================
    // IAM Permissions
    // ================================================================

    // Neptune DB — read/write/delete for SPARQL queries + Graph Store Protocol
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "neptune-db:ReadDataViaQuery",
          "neptune-db:WriteDataViaQuery",
          "neptune-db:DeleteDataViaQuery",
          "neptune-db:GetQueryStatus",
          "neptune-db:CancelQuery",
        ],
        // neptuneClusterArn already includes the ``/*`` suffix from
        // storage-stack.ts:formatArn (cluster-id/*). Do NOT append another.
        resources: [props.neptuneClusterArn],
      }),
    );

    // OpenSearch Serverless — API access
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["aoss:APIAccessAll"],
        resources: [opensearchCollectionArn],
      }),
    );

    // AOSS Data Access Policy — full CRUD for embeddings
    const ossDataAccessPolicy = new opensearchserverless.CfnAccessPolicy(
      this,
      "OSSDataAccessPolicy",
      {
        name: this.prefixed("ontology-oss"),
        type: "data",
        policy: JSON.stringify([
          {
            Rules: [
              {
                ResourceType: "index",
                Resource: [`index/${opensearchCollectionName}/*`],
                Permission: [
                  "aoss:CreateIndex",
                  "aoss:DescribeIndex",
                  "aoss:UpdateIndex",
                  "aoss:DeleteIndex",
                  "aoss:ReadDocument",
                  "aoss:WriteDocument",
                ],
              },
              {
                ResourceType: "collection",
                Resource: [`collection/${opensearchCollectionName}`],
                Permission: ["aoss:DescribeCollectionItems"],
              },
            ],
            Principal: [taskDef.taskRole.roleArn],
          },
        ]),
      },
    );

    // DynamoDB — full access to own table
    this.table.grantReadWriteData(taskDef.taskRole);

    // S3 — read/write ontology artifacts (Turtle, R2RML) for accepted proposals
    props.ontologyArtifactsBucket.grantReadWrite(taskDef.taskRole);

    // DynamoDB — read access to the namespaces table so the catalog reader
    // can look up ``dataZoneProjectId`` per namespace.
    const nsTable = dynamodb.Table.fromTableName(
      this,
      "NamespacesTable",
      namespacesTableName,
    );
    nsTable.grantReadData(taskDef.taskRole);

    // DynamoDB — read access to the unified sources table so the catalog
    // reader can verify a sourceId is registered before fetching DataZone
    // assets. Replaces the legacy datasourcesTable lookup that only knew
    // about JDBC sources.
    const srcTable = dynamodb.Table.fromTableName(
      this,
      "SourcesTable",
      sourcesTableName,
    );
    srcTable.grantReadData(taskDef.taskRole);

    // Bedrock — LLM (Converse) + Embeddings (InvokeModel)
    const region = cdk.Aws.REGION;
    const account = cdk.Aws.ACCOUNT_ID;
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:Converse",
          "bedrock:ApplyGuardrail",
        ],
        resources: [
          `arn:aws:bedrock:*::foundation-model/*`,
          `arn:aws:bedrock:*:${account}:inference-profile/*`,
          `arn:aws:bedrock:${region}:${account}:guardrail/*`,
        ],
      }),
    );

    // AWS Marketplace — required for first-time Bedrock model subscription handshake.
    // When a task invokes a Marketplace-sourced Bedrock model for the first time in an
    // account, Bedrock completes an account-wide subscription automatically. Without
    // these permissions, the first InvokeModel call fails with AccessDeniedException
    // and induction proposals error at the Fetch metadata stage (issue #814).
    // These actions have no resource-level ARN (IAM limitation, not a design choice).
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "aws-marketplace:ViewSubscriptions",
          "aws-marketplace:Subscribe",
        ],
        resources: ["*"],
      }),
    );

    // SSM — read the Bedrock guardrail id at runtime (content filtering on LLM
    // calls). Resolved lazily by the app; grant read on just that parameter.
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [
          `arn:aws:ssm:${region}:${account}:parameter${ssmPrefix}/bedrock/guardrail-id`,
        ],
      }),
    );

    // DataZone/SMUS — read catalog metadata. Two layers required:
    // 1. IAM permissions on the domain (below).
    // 2. Project membership — granted by assuming ``projectAccessRole``,
    //    which is registered as a DataZone project member in NamespaceStack.
    //    DataZone gates Search/GetAsset on project membership, not IAM alone.
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "datazone:GetAsset",
          "datazone:SearchAssets",
          "datazone:Search",
          "datazone:GetProject",
        ],
        resources: [
          `arn:aws:datazone:${region}:${account}:domain/${props.smusDomainId}`,
          `arn:aws:datazone:${region}:${account}:domain/${props.smusDomainId}/*`,
        ],
      }),
    );
    projectAccessRole.grantAssumeRole(taskDef.taskRole);

    // Sources Lambda — invoke for catalog column fetching during induction
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["lambda:InvokeFunction"],
        resources: [
          `arn:aws:lambda:${region}:${account}:function:${this.prefixed("sources-api")}`,
        ],
      }),
    );

    // EventBridge — emit ontology.published events for VKG reload
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["events:PutEvents"],
        resources: [`arn:aws:events:${region}:${account}:event-bus/default`],
      }),
    );

    // CloudWatch — publish custom induction metrics (Bedrock rerank latency,
    // token usage, per-job cost) under the COA/Ontology namespace, plus
    // guardrail allow/block decisions under COA/Guardrails (#111 AC10).
    // PutMetricData exposes no resource-level ARN, so least-privilege is
    // enforced with the ``cloudwatch:namespace`` condition key instead.
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: {
            "cloudwatch:namespace": ["COA/Ontology", "COA/Guardrails"],
          },
        },
      }),
    );

    // ================================================================
    // Fargate Service with Cloud Map service discovery
    // ================================================================
    // Uses ``cloudMapOptions`` (not ``serviceConnectConfiguration``) so the
    // task IPs are published as real Route 53 A records under
    // ontology-engine.<namespace>.local. Service Connect's namespace DNS is
    // only resolvable from inside another ECS task (via the Envoy sidecar),
    // which would block the api-proxy Lambda from reaching this service.
    this.service = new ecs.FargateService(this, "OntologyService", {
      serviceName: this.prefixed("ontology-engine"),
      cluster: this.cluster,
      taskDefinition: taskDef,
      desiredCount,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [ecsSecurityGroup],
      assignPublicIp: false,
      cloudMapOptions: {
        cloudMapNamespace: namespace,
        name: "ontology-engine",
        dnsRecordType: servicediscovery.DnsRecordType.A,
        dnsTtl: cdk.Duration.seconds(10),
      },
      circuitBreaker: { rollback: true },
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
    });

    // Ensure the AOSS data access policy exists before tasks start probing
    // OpenSearch — without this, the first /readiness or _ensure_index call
    // races against policy creation and can return 403 long enough to trip
    // the deployment circuit breaker.
    this.service.node.addDependency(ossDataAccessPolicy);

    ecsSecurityGroup.addIngressRule(
      props.network.lambdaSecurityGroup,
      ec2.Port.tcp(this.containerPort),
      "Allow ontology-engine API from api-proxy Lambda",
    );

    // ================================================================
    // OE monitoring (cdk-monitoring-constructs)
    // ================================================================
    new SclMonitoring(this, "Monitoring", {
      alarmNamePrefix: this.prefixed("ontology"),
      alarmAction: props.alarmAction,
    })
      .monitorFargateService(this.service)
      .monitorTable(this.table);

    // ================================================================
    // Out-of-process OOM observability (#784)
    // ================================================================
    // A 50k induction OOM-killed the 32GB task (exit 137). The observability
    // was blind: the SIGKILL runs neither `except` nor `finally`, so the
    // single terminal PutMetricData never fired (blank dashboard); the facade
    // 90% memory alarm missed the 81%-then->100% between-sample ramp; and the
    // OOM label lived only in the ECS `stoppedReason`, queried by nothing.
    // Two out-of-process signals close the gap (the third, per-tick heartbeat
    // metrics, lives in the induction container's watchdog).

    // (b) Memory alarm at 85% Maximum, 1 period. 85 < the facade's 90% (which
    // sampled 81% and never breached); Maximum (not the facade's Average)
    // catches the ramp. Wired to props.alarmAction like the facade alarms.
    const memoryAlarm = new cloudwatch.Alarm(
      this,
      "MemoryUtilizationHighAlarm",
      {
        alarmName: this.prefixed("ontology-memory-high"),
        alarmDescription:
          "Ontology induction task memory >= 85% — pre-OOM ramp; the 32GB task exit-137'd once at 100%",
        metric: this.service.metricMemoryUtilization({
          period: cdk.Duration.minutes(1),
          statistic: "Maximum",
        }),
        threshold: 85,
        evaluationPeriods: 1,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      },
    );
    props.alarmAction?.addAlarmActions({
      alarm: memoryAlarm,
      action: props.alarmAction,
    });

    // (a) EventBridge rule on ECS OOM. The exit-137/OutOfMemory label lives only
    // in the ECS Task State Change event, so route it to a durable metric.
    // Target choice (laziest that works): rule → Logs log group + a metric
    // filter emitting COA/Ontology InductionTaskOOMKilled, then an alarm on it.
    // A native two-hop (no Lambda) — cheaper and simpler than a custom emitter,
    // and the log group also keeps the raw stop events for post-mortems.
    const oomLogGroup = new logs.LogGroup(this, "OomEventLogGroup", {
      logGroupName: `/aws/events/${this.prefixed("ontology-oom")}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    new events.Rule(this, "OntologyTaskOomRule", {
      ruleName: this.prefixed("ontology-oom"),
      description:
        "Ontology induction ECS task stopped by OOM (exit 137 / OutOfMemory stoppedReason)",
      eventPattern: {
        source: ["aws.ecs"],
        detailType: ["ECS Task State Change"],
        detail: {
          clusterArn: [this.cluster.clusterArn],
          lastStatus: ["STOPPED"],
          // Either the OOM stoppedReason OR any container that exited 137.
          $or: [
            { stoppedReason: [{ wildcard: "*OutOfMemory*" }] },
            { containers: { exitCode: [137] } },
          ],
        },
      },
      targets: [new targets.CloudWatchLogGroup(oomLogGroup)],
    });

    // Metric filter — every event delivered to the log group is an OOM stop
    // (the rule already filtered), so match all events and emit a count of 1.
    new logs.MetricFilter(this, "OomEventMetricFilter", {
      logGroup: oomLogGroup,
      filterPattern: logs.FilterPattern.allEvents(),
      metricNamespace: "COA/Ontology",
      metricName: "InductionTaskOOMKilled",
      metricValue: "1",
      defaultValue: 0,
    });

    const oomAlarm = new cloudwatch.Alarm(this, "InductionTaskOomKilledAlarm", {
      alarmName: this.prefixed("ontology-oom-killed"),
      alarmDescription:
        "An ontology induction ECS task was OOM-killed (exit 137) — right-size memory or shard the workload",
      metric: new cloudwatch.Metric({
        namespace: "COA/Ontology",
        metricName: "InductionTaskOOMKilled",
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    props.alarmAction?.addAlarmActions({
      alarm: oomAlarm,
      action: props.alarmAction,
    });

    // ================================================================
    // CloudWatch Dashboard — Induction Performance, Load & Cost
    // ================================================================
    // The single consolidated induction dashboard. The induction container
    // emits ALL custom metrics under the COA/Ontology namespace via
    // PutMetricData (NOT EMF-stdout — that is never extracted on this Fargate
    // task with a plain awsLogs driver), dimensioned by NamespaceId (and Stage
    // for the per-stage token counts). NamespaceId is per-induction-namespace
    // and unbounded (load-test namespaces are ephemeral), so every custom-metric
    // widget uses a CloudWatch SEARCH() MathExpression that auto-discovers all
    // NamespaceId dimensions live instead of pinning one. `usingMetrics` is
    // intentionally empty for SEARCH exprs.
    const inductionNamespace = "COA/Ontology";
    const defaultPeriod = cdk.Duration.minutes(5);
    const metricRegion = cdk.Aws.REGION;

    /** A SEARCH-based series across all NamespaceId dimensions for one metric. */
    const inductionSearch = (
      metricName: string,
      stat: string,
      label: string,
    ): cloudwatch.MathExpression =>
      new cloudwatch.MathExpression({
        expression: `SEARCH('{${inductionNamespace},NamespaceId} MetricName="${metricName}"', '${stat}', ${defaultPeriod.toSeconds()})`,
        label,
        usingMetrics: {},
        period: defaultPeriod,
        searchRegion: metricRegion,
      });

    /**
     * Same as `inductionSearch` but converts the `*DurationMs` value to MINUTES
     * for readability (a 60-min induce would otherwise read as 3,600,000 ms).
     * CloudWatch metric math broadcasts scalar division over the SEARCH array,
     * so appending `/60000` to the expression is valid. Display-only — emitted
     * metrics stay in milliseconds.
     */
    const inductionSearchMinutes = (
      metricName: string,
      stat: string,
      label: string,
    ): cloudwatch.MathExpression =>
      new cloudwatch.MathExpression({
        expression: `SEARCH('{${inductionNamespace},NamespaceId} MetricName="${metricName}"', '${stat}', ${defaultPeriod.toSeconds()})/60000`,
        label,
        usingMetrics: {},
        period: defaultPeriod,
        searchRegion: metricRegion,
      });

    /**
     * A SEARCH collapsed to a SINGLE series via SUM(), for use as a scalar
     * operand inside another MathExpression. Raw SEARCH() returns an ARRAY of
     * time series (one per NamespaceId), which cannot be combined with
     * arithmetic operators across two arrays — SUM() collapses each to one
     * series first.
     */
    const inductionSearchSum = (
      metricName: string,
      label: string,
    ): cloudwatch.MathExpression =>
      new cloudwatch.MathExpression({
        expression: `SUM(SEARCH('{${inductionNamespace},NamespaceId} MetricName="${metricName}"', 'Sum', ${defaultPeriod.toSeconds()}))`,
        label,
        usingMetrics: {},
        period: defaultPeriod,
        searchRegion: metricRegion,
      });

    /** AWS/Bedrock per-model metric (Invocations / InvocationThrottles / tokens). */
    const bedrockModelMetric = (
      metricName: string,
      modelId: string,
    ): cloudwatch.Metric =>
      new cloudwatch.Metric({
        namespace: "AWS/Bedrock",
        metricName,
        dimensionsMap: { ModelId: modelId },
        statistic: "Sum",
        period: defaultPeriod,
        region: metricRegion,
        label: `${modelId} ${metricName}`,
      });

    // Dashboard ModelId dimensions come from the SAME resolved values as the
    // container environment above (#94) — they used to be duplicate literals, so
    // a model change for a non-US deploy left these widgets querying a
    // dimension with no data.
    const rerankModelId = inductionModelId; // configured LLM_MODEL_ID
    const haikuModelId = chatModelId; // actual rerank/enrichment invoker

    // ponytail: Hardcoded us-west-2 on-demand list prices (USD per 1K tokens) as of
    // 2026-07-21 — CloudWatch math cannot look prices up, so these MUST be updated by
    // hand if AWS Bedrock pricing changes. The Sonnet >200K-context tier ($6/$22.50
    // per 1M) is intentionally NOT modeled: induction prompts are per-table and small,
    // so only the base tier applies.
    const sonnetInputPer1K = 0.003;
    const sonnetOutputPer1K = 0.015;
    const haikuInputPer1K = 0.001;
    const haikuOutputPer1K = 0.005;
    const embedInputPer1K = 0.00012;

    const dashboard = new cloudwatch.Dashboard(this, "InductionDashboard", {
      dashboardName: this.prefixed("ontology-induction"),
      defaultInterval: cdk.Duration.days(1),
    });

    dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: [
          "# Ontology Induction — Performance, Load & Cost",
          "",
          `Metrics are emitted per **NamespaceId** under the \`${inductionNamespace}\` namespace via PutMetricData.`,
          "Induction widgets use CloudWatch SEARCH() so ephemeral load-test namespaces appear automatically.",
          "Duration widgets show p50/p90 across jobs (single-value-per-job datums).",
        ].join("\n"),
        width: 24,
        height: 3,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Stage durations (Average, min)",
        left: [
          inductionSearchMinutes(
            "FetchMetadataDurationMs",
            "Average",
            "FetchMetadata",
          ),
          inductionSearchMinutes(
            "GroundingLoadDurationMs",
            "Average",
            "GroundingLoad",
          ),
          inductionSearchMinutes("InduceDurationMs", "Average", "Induce"),
          inductionSearchMinutes("StoreDurationMs", "Average", "Store"),
          inductionSearchMinutes(
            "InductionJobDurationMs",
            "Average",
            "JobWallclock",
          ),
        ],
        leftYAxis: { label: "min", showUnits: false },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Stage durations (p90, min)",
        left: [
          inductionSearchMinutes(
            "FetchMetadataDurationMs",
            "p90",
            "FetchMetadata p90",
          ),
          inductionSearchMinutes(
            "GroundingLoadDurationMs",
            "p90",
            "GroundingLoad p90",
          ),
          inductionSearchMinutes("InduceDurationMs", "p90", "Induce p90"),
          inductionSearchMinutes("StoreDurationMs", "p90", "Store p90"),
        ],
        leftYAxis: { label: "min", showUnits: false },
        width: 12,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Job wallclock (p50 / p90, min)",
        left: [
          inductionSearchMinutes(
            "InductionJobDurationMs",
            "p50",
            "JobWallclock p50",
          ),
          inductionSearchMinutes(
            "InductionJobDurationMs",
            "p90",
            "JobWallclock p90",
          ),
          inductionSearchMinutes("InduceDurationMs", "p50", "Induce p50"),
          inductionSearchMinutes("InduceDurationMs", "p90", "Induce p90"),
        ],
        leftYAxis: { label: "min", showUnits: false },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Throughput / volume (Sum)",
        left: [
          inductionSearch("TablesProcessed", "Sum", "Tables"),
          inductionSearch("ColumnsProcessed", "Sum", "Columns"),
          inductionSearch("GroundedClassesMatched", "Sum", "Grounded"),
          inductionSearch("NovelClassesCreated", "Sum", "Novel"),
        ],
        leftYAxis: { label: "count", showUnits: false },
        width: 12,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Grounding quality",
        left: [
          inductionSearch("GroundedClassesMatched", "Sum", "Grounded"),
          inductionSearch("NovelClassesCreated", "Sum", "Novel"),
        ],
        right: [
          new cloudwatch.MathExpression({
            // novel rate = novel / (grounded + novel)
            expression: "novel / (grounded + novel)",
            label: "Novel rate",
            usingMetrics: {
              grounded: inductionSearchSum(
                "GroundedClassesMatched",
                "grounded",
              ),
              novel: inductionSearchSum("NovelClassesCreated", "novel"),
            },
            period: defaultPeriod,
          }),
        ],
        leftYAxis: { label: "count", showUnits: false },
        rightYAxis: { label: "rate", min: 0, max: 1, showUnits: false },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Bedrock rerank",
        left: [
          // This branch's emitter publishes RerankInvocations (Sum) and
          // RerankLatencyMs as a StatisticSet (Average + Maximum). It does NOT
          // emit a rerank-errors metric, so no errors series here.
          inductionSearch("RerankInvocations", "Sum", "Invocations"),
        ],
        right: [
          inductionSearch("RerankLatencyMs", "Average", "Latency avg (ms)"),
          inductionSearch("RerankLatencyMs", "Maximum", "Latency max (ms)"),
        ],
        leftYAxis: { label: "count", showUnits: false },
        rightYAxis: { label: "ms", showUnits: false },
        width: 12,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Bedrock cost (USD)",
        left: [inductionSearch("InductionJobCostUsd", "Sum", "Job cost (Sum)")],
        right: [
          new cloudwatch.MathExpression({
            expression: `SEARCH('{${inductionNamespace},NamespaceId,Stage} MetricName="BedrockInputTokens"', 'Sum', ${defaultPeriod.toSeconds()})`,
            label: "BedrockInputTokens by Stage",
            usingMetrics: {},
            period: defaultPeriod,
            searchRegion: metricRegion,
          }),
          new cloudwatch.MathExpression({
            expression: `SEARCH('{${inductionNamespace},NamespaceId,Stage} MetricName="BedrockOutputTokens"', 'Sum', ${defaultPeriod.toSeconds()})`,
            label: "BedrockOutputTokens by Stage",
            usingMetrics: {},
            period: defaultPeriod,
            searchRegion: metricRegion,
          }),
        ],
        leftYAxis: { label: "USD", showUnits: false },
        rightYAxis: { label: "tokens", showUnits: false },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "AWS/Bedrock per-model",
        left: [
          bedrockModelMetric("Invocations", rerankModelId),
          bedrockModelMetric("Invocations", haikuModelId),
          bedrockModelMetric("Invocations", embedModelId),
        ],
        right: [
          bedrockModelMetric("InvocationThrottles", rerankModelId),
          bedrockModelMetric("InvocationThrottles", haikuModelId),
          bedrockModelMetric("InvocationThrottles", embedModelId),
        ],
        leftYAxis: { label: "invocations", showUnits: false },
        rightYAxis: { label: "throttles", showUnits: false },
        width: 12,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.SingleValueWidget({
        title: "Estimated run cost from AWS/Bedrock tokens (USD)",
        metrics: [
          new cloudwatch.MathExpression({
            // Total Bedrock spend: tokens are counts, so divide each term by 1000.
            // Cohere embed is input-only (no output term).
            expression: `si*${sonnetInputPer1K}/1000 + so*${sonnetOutputPer1K}/1000 + hi*${haikuInputPer1K}/1000 + ho*${haikuOutputPer1K}/1000 + ei*${embedInputPer1K}/1000`,
            label: "Est. cost (USD)",
            usingMetrics: {
              si: bedrockModelMetric("InputTokenCount", rerankModelId),
              so: bedrockModelMetric("OutputTokenCount", rerankModelId),
              hi: bedrockModelMetric("InputTokenCount", haikuModelId),
              ho: bedrockModelMetric("OutputTokenCount", haikuModelId),
              ei: bedrockModelMetric("InputTokenCount", embedModelId),
            },
            period: defaultPeriod,
          }),
        ],
        // Sum spans the whole selected time range, so this reads as the cost of the run in view.
        setPeriodToTimeRange: true,
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "ECS service utilization (%)",
        left: [
          this.service.metricCpuUtilization({
            period: defaultPeriod,
            label: "CPU",
          }),
          this.service.metricMemoryUtilization({
            period: defaultPeriod,
            label: "Memory",
          }),
        ],
        leftYAxis: { label: "%", min: 0, max: 100, showUnits: false },
        width: 12,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.SingleValueWidget({
        title: "Errors & throttles (Sum)",
        metrics: [
          // Rerank errors are intentionally absent — the emitter publishes no
          // rerank-errors metric (out of scope for this consolidation).
          inductionSearch("InductionJobErrors", "Sum", "Job errors"),
          bedrockModelMetric("InvocationThrottles", rerankModelId),
          bedrockModelMetric("InvocationThrottles", haikuModelId),
          bedrockModelMetric("InvocationThrottles", embedModelId),
        ],
        width: 24,
        height: 6,
      }),
    );

    // ================================================================
    // SSM Parameters
    // ================================================================
    new ssm.StringParameter(this, "SsmOntologyClusterArn", {
      parameterName: `${ssmPrefix}/ontology-engine/cluster-arn`,
      stringValue: this.cluster.clusterArn,
    });

    new ssm.StringParameter(this, "SsmOntologyServiceName", {
      parameterName: `${ssmPrefix}/ontology-engine/service-name`,
      stringValue: this.service.serviceName,
    });

    // Republishing the Cloud Map namespace ARN keeps the network-stack
    // export consumed across the SC → cloudMapOptions migration. Without
    // this, switching off ``serviceConnectConfiguration`` would orphan the
    // exported ARN and CFN would refuse to drop it (deployed ontology stack
    // still imports it). Real consumers can also discover the ARN here
    // instead of creating new cross-stack exports.
    new ssm.StringParameter(this, "SsmServiceNamespaceArn", {
      parameterName: `${ssmPrefix}/ontology-engine/service-namespace-arn`,
      stringValue: namespace.namespaceArn,
      description:
        "Cloud Map private DNS namespace ARN (consumed by ontology services)",
    });

    new ssm.StringParameter(this, "SsmOntologyEndpoint", {
      parameterName: `${ssmPrefix}/ontology-engine/endpoint`,
      stringValue: `http://ontology-engine.${namespace.namespaceName}:${this.containerPort}`,
    });

    new ssm.StringParameter(this, "SsmOntologyTableName", {
      parameterName: `${ssmPrefix}/ontology-engine/dynamodb-table`,
      stringValue: this.table.tableName,
    });

    // ================================================================
    // API Proxy Lambda (forwards API Gateway → ECS via Cloud Map DNS)
    // ================================================================
    const apiProxyFn = new lambda.Function(this, "ApiProxyFn", {
      functionName: this.prefixed("ontology-api-proxy"),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "coa_ontology.api_proxy_handler.handler",
      // Merges libs/common/src into the asset root so
      // `coa_common` (used for resolve_region) is importable.
      // Its transitive deps (structlog, pydantic-settings, etc. — see
      // libs/common/pyproject.toml) must be installed explicitly since this
      // Lambda has no requirements.txt of its own; mirrors the pipDeps used
      // wherever else commonLib is bundled (e.g. api-stack.ts).
      code: bundlePython({
        srcDirs: [Paths.ontologyEngineSrc, Paths.commonLib],
        pipDeps: [
          "cedarpy>=4.8.0",
          "pydantic>=2.0",
          "pydantic-settings>=2.0",
          "structlog>=24.0",
          "cryptography>=43.0",
          "PyJWT>=2.9.0",
          "requests>=2.32.0",
        ],
      }),
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.network.lambdaSecurityGroup],
      environment: {
        // Use the FQDN under the Cloud Map private DNS namespace. The short
        // name ``ontology-engine`` only resolves from inside ECS tasks
        // (which inject the namespace as a search domain); Lambda's
        // resolver requires the fully-qualified name.
        ONTOLOGY_ENGINE_ENDPOINT: `http://ontology-engine.${namespace.namespaceName}:${this.containerPort}`,
        SOURCES_TABLE: sourcesTableName,
        NEPTUNE_ENDPOINT: props.neptuneClusterEndpoint,
        OPENSEARCH_ENDPOINT: opensearchEndpoint,
        ALLOWED_ORIGIN: props.allowedOrigin,
      },
    });
    // System-health check describes only the sources table (SOURCES_TABLE).
    apiProxyFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "HealthCheckDescribeSourcesTable",
        actions: ["dynamodb:DescribeTable"],
        resources: [srcTable.tableArn],
      }),
    );
    apiProxyFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["neptune-db:ReadDataViaQuery", "neptune-db:GetQueryStatus"],
        resources: [props.neptuneClusterArn],
      }),
    );
    // aoss:BatchGetCollection does not support resource-level permissions
    // (AWS limitation — no resource type in the Service Authorization
    // Reference), so it must use "*". It is a read-only control-plane call
    // returning collection metadata (status, endpoints), never index data.
    apiProxyFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "HealthCheckBatchGetCollection",
        actions: ["aoss:BatchGetCollection"],
        resources: ["*"],
      }),
    );

    new ssm.StringParameter(this, "SsmOntologyApiFnArn", {
      parameterName: `${ssmPrefix}/ontology-engine/api-fn-arn`,
      stringValue: apiProxyFn.functionArn,
    });

    // ================================================================
    // Outputs
    // ================================================================
    new cdk.CfnOutput(this, "OntologyServiceEndpoint", {
      value: `http://ontology-engine.${namespace.namespaceName}:${this.containerPort}`,
      description: "Ontology Engine internal endpoint via Cloud Map DNS",
    });

    new cdk.CfnOutput(this, "OntologyTableName", {
      value: this.table.tableName,
      description: "Ontology Engine DynamoDB table name",
    });
  }
}
