// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import type { IAlarmActionStrategy } from "cdk-monitoring-constructs/lib/common/alarm/action";

import { SCLStack } from "../../constructs/scl-stack";
import { SclMonitoring } from "../../constructs";
import { BRAND } from "../../constants";
import {
  resolveContext,
  resolveLambdaReservedConcurrency,
} from "../../context";
import { NetworkStack } from "../foundation/network-stack";

export interface VkgStackProps extends cdk.StackProps {
  /** Network stack providing VPC, subnets, and security groups. */
  readonly network: NetworkStack;

  /**
   * S3 bucket containing published ontology artifacts (Turtle + R2RML).
   * Owned by StorageStack and shared with OntologyStack (writer) +
   * ServeStack (reader).
   */
  readonly ontologyBucket: s3.IBucket;

  /** Shared Cloud Map private DNS namespace for service discovery. */
  readonly serviceNamespace: servicediscovery.IPrivateDnsNamespace;

  /** Fargate CPU units (default: 1024 = 1 vCPU). */
  readonly cpu?: number;

  /** Fargate memory in MiB (default: 2048 = 2 GB). */
  readonly memoryLimitMiB?: number;

  /** Container port for the Ontop SPARQL endpoint (default: 8080). */
  readonly containerPort?: number;

  /**
   * JVM args for the Ontop launcher, passed via ONTOP_JAVA_ARGS (NOT JAVA_OPTS,
   * which the launcher ignores). Default "-Xmx1536m -Xms512m". Applied to both
   * the CDK-provisioned service and the per-namespace reload Lambda so they
   * stay in sync.
   */
  readonly ontopJavaArgs?: string;

  /** Alarm action strategy for monitoring alarms. */
  readonly alarmAction?: IAlarmActionStrategy;
}

/**
 * VKG infrastructure stack — cluster, task definition, and reload automation.
 *
 * Per-namespace VKG services are provisioned dynamically by the reload Lambda
 * when an ontology is published (EventBridge event). No static "default" service
 * is deployed — VKG instances only exist when a namespace has a published ontology.
 *
 * This stack provides:
 * - ECS cluster for per-namespace VKG Fargate tasks
 * - Task definition template (image, roles, logging)
 * - Reload Lambda (EventBridge → provision or restart per-namespace service)
 * - SSM parameters (cluster ARN, image URI, endpoint pattern)
 */
export class VkgStack extends SCLStack {
  /** The ECS cluster hosting per-namespace VKG services. */
  public readonly cluster: ecs.Cluster;

  /** The container port Ontop listens on. */
  public readonly containerPort: number;

  /** S3 bucket holding ontology artifacts. */
  public readonly ontologyBucket: s3.IBucket;

  constructor(scope: Construct, id: string, props: VkgStackProps) {
    super(scope, id, props);
    this.addComponentTag("vkg");

    const { prefix, envName, ssmPrefix } = resolveContext(this.node);
    const resourcePrefix = `${prefix}-${envName}`;
    const vpc = props.network.vpc;
    const ecsSecurityGroup = props.network.ecsSecurityGroup;

    const cpu = props.cpu ?? 1024;
    const memoryLimitMiB = props.memoryLimitMiB ?? 2048;
    // Ontop heap. The launcher reads ONTOP_JAVA_ARGS (NOT JAVA_OPTS) — see
    // packages/vkg/entrypoint.sh. When not overridden, derive from task memory
    // (max heap ~= 75%, initial ~= 25%) so raising memoryLimitMiB alone scales
    // the heap in step, leaving headroom for the JVM, H2, and the Python facade
    // (#149 causes B/C). This same value is handed to the reload Lambda so
    // per-namespace reloads and the CDK-provisioned service stay in lockstep
    // from one source of truth.
    const ontopJavaArgs =
      props.ontopJavaArgs ??
      `-Xmx${Math.max(512, Math.floor((memoryLimitMiB * 3) / 4))}m ` +
        `-Xms${Math.max(256, Math.floor(memoryLimitMiB / 4))}m`;
    this.containerPort = props.containerPort ?? 8080;
    const namespace = props.serviceNamespace;
    const cloudMapNamespaceName = namespace.namespaceName;

    this.ontologyBucket = props.ontologyBucket;

    // ================================================================
    // ECS Cluster (no default namespace — we create Cloud Map separately)
    // ================================================================
    this.cluster = new ecs.Cluster(this, "VkgCluster", {
      clusterName: this.prefixed("vkg-cluster"),
      vpc,
    });

    // ================================================================
    // Task Definition
    // ================================================================
    const taskDef = new ecs.FargateTaskDefinition(this, "VkgTaskDef", {
      family: this.prefixed("vkg-service"),
      cpu,
      memoryLimitMiB,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    // ── Container ────────────────────────────────────────────────────
    // Support pre-built image from CI (Kaniko) or local Docker build (fromAsset)
    const vkgImageUri = this.node.tryGetContext("vkg_image_uri") as
      | string
      | undefined;
    const ecrRepositoryArn = this.node.tryGetContext("ecr_repository_arn") as
      | string
      | undefined;
    const ecrRepositoryName = this.node.tryGetContext("ecr_repository_name") as
      | string
      | undefined;

    let containerImage: ecs.ContainerImage;
    if (vkgImageUri && ecrRepositoryArn && ecrRepositoryName) {
      const ecrRepo = cdk.aws_ecr.Repository.fromRepositoryAttributes(
        this,
        "VkgEcrRepo",
        { repositoryArn: ecrRepositoryArn, repositoryName: ecrRepositoryName },
      );
      containerImage = ecs.ContainerImage.fromEcrRepository(
        ecrRepo,
        vkgImageUri.split(":").pop() || "latest",
      );
    } else {
      containerImage = ecs.ContainerImage.fromAsset("../packages/vkg", {
        platform: cdk.aws_ecr_assets.Platform.LINUX_ARM64,
      });
    }

    taskDef.addContainer("OntopContainer", {
      containerName: this.prefixed("ontop"),
      image: containerImage,
      environment: {
        ONTOLOGY_BUCKET: this.ontologyBucket.bucketName,
        ONTOLOGY_PREFIX: "ontologies/",
        ENDPOINT_PORT: String(this.containerPort),
        ONTOP_JAVA_ARGS: ontopJavaArgs,
      },
      portMappings: [
        {
          containerPort: this.containerPort,
          protocol: ecs.Protocol.TCP,
        },
      ],
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "vkg",
      }),
      healthCheck: {
        command: [
          "CMD-SHELL",
          `curl -sf http://localhost:${this.containerPort}/health || exit 1`,
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(10),
        retries: 5,
        startPeriod: cdk.Duration.seconds(180),
      },
    });

    // ── Task role permissions ─────────────────────────────────────────
    // S3 read for ontology artifacts (all namespaces — per-namespace services
    // also use this role and scope to their prefix via ONTOLOGY_PREFIX env var).
    this.ontologyBucket.grantRead(taskDef.taskRole, "ontologies/*");

    // Allow execution role to create log streams for per-namespace VKG services
    taskDef.executionRole!.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ],
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:/ecs/vkg-*`,
          `arn:aws:logs:${this.region}:${this.account}:log-group:/ecs/vkg-*:*`,
        ],
      }),
    );

    // Allow inbound traffic on the container port from within the VPC
    // (per-namespace VKG services use this security group)
    ecsSecurityGroup.addIngressRule(
      ec2.Peer.ipv4(vpc.vpcCidrBlock),
      ec2.Port.tcp(this.containerPort),
      "Allow VKG SPARQL translation from VPC",
    );

    // ================================================================
    // SSM: VKG container image URI (source of truth for reload Lambda)
    // ================================================================
    const ontopContainer = taskDef.findContainer(this.prefixed("ontop"))!;
    const vkgImageParam = new ssm.StringParameter(
      this,
      "SsmVkgContainerImage",
      {
        parameterName: `${ssmPrefix}/vkg/container-image`,
        stringValue: ontopContainer.imageName,
        description: "Latest VKG container image URI (updated by CDK deploy)",
      },
    );

    // ================================================================
    // Ontology Reload (EventBridge → Lambda → ECS forceNewDeployment)
    // ================================================================
    // Reserved concurrency bounds this function's blast radius (it drives ECS
    // service updates and is effectively serial). Configurable via
    // `lambda_reserved_concurrency`; `0`/undefined omits the reservation so the
    // stack deploys on reduced-quota accounts (see resolveLambdaReservedConcurrency).
    const reservedConcurrency = resolveLambdaReservedConcurrency(this.node);
    const reloadFn = new lambda.Function(this, "VkgReloadFn", {
      functionName: this.prefixed("vkg-reload"),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset("lambda/vkg-reload"),
      environment: {
        CLUSTER_ARN: this.cluster.clusterArn,
        RESOURCE_PREFIX: resourcePrefix,
        CLOUD_MAP_NAMESPACE_ID: namespace.namespaceId,
        VKG_TASK_ROLE_ARN: taskDef.taskRole.roleArn,
        VKG_EXECUTION_ROLE_ARN: taskDef.executionRole!.roleArn,
        ONTOLOGY_BUCKET: this.ontologyBucket.bucketName,
        PRIVATE_SUBNET_IDS: vpc
          .selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS })
          .subnetIds.join(","),
        ECS_SECURITY_GROUP_ID: ecsSecurityGroup.securityGroupId,
        VKG_IMAGE_PARAM_NAME: vkgImageParam.parameterName,
        // Keep per-namespace reload task sizing in lockstep with the CDK
        // service above (#149 causes B/C). One source of truth for cpu/memory
        // and Ontop heap.
        VKG_TASK_CPU: String(cpu),
        VKG_TASK_MEMORY: String(memoryLimitMiB),
        VKG_ONTOP_JAVA_ARGS: ontopJavaArgs,
      },
      timeout: cdk.Duration.seconds(30),
      reservedConcurrentExecutions: reservedConcurrency,
      logRetention: logs.RetentionDays.ONE_MONTH,
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [ecsSecurityGroup],
    });

    reloadFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "ecs:UpdateService",
          "ecs:DescribeServices",
          "ecs:CreateService",
        ],
        resources: [
          `arn:aws:ecs:${this.region}:${this.account}:service/${this.cluster.clusterName}/*`,
        ],
      }),
    );
    reloadFn.addToRolePolicy(
      new iam.PolicyStatement({
        // The scheduled sweep enumerates per-namespace VKG services.
        // ecs:ListServices has no resource type in the IAM authorization model,
        // so it must use Resource:"*"; scope it to this cluster via the
        // ecs:cluster condition key instead (same shape as PutMetricData below).
        actions: ["ecs:ListServices"],
        resources: ["*"],
        conditions: {
          ArnEquals: { "ecs:cluster": this.cluster.clusterArn },
        },
      }),
    );
    reloadFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"],
        resources: ["*"],
      }),
    );
    reloadFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["iam:PassRole"],
        resources: [taskDef.taskRole.roleArn, taskDef.executionRole!.roleArn],
        conditions: {
          StringEquals: { "iam:PassedToService": "ecs-tasks.amazonaws.com" },
        },
      }),
    );
    reloadFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "servicediscovery:CreateService",
          "servicediscovery:ListServices",
        ],
        resources: ["*"],
      }),
    );
    reloadFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "application-autoscaling:RegisterScalableTarget",
          "application-autoscaling:PutScalingPolicy",
        ],
        resources: [
          `arn:aws:application-autoscaling:${this.region}:${this.account}:scalable-target/*`,
        ],
      }),
    );
    reloadFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ],
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:/ecs/vkg-*`,
          `arn:aws:logs:${this.region}:${this.account}:log-group:/ecs/vkg-*:*`,
        ],
      }),
    );
    vkgImageParam.grantRead(reloadFn);
    // PutMetricData has no resource-level scoping; restrict by metric namespace.
    reloadFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: { "cloudwatch:namespace": "COA/VKG" },
        },
      }),
    );

    const { eventSourcePrefix } = resolveContext(this.node);
    // Matches BOTH the deployment-scoped source and the legacy static `coa`
    // one. TRANSITIONAL: the rule (CloudFormation) and the emitter's
    // EVENT_SOURCE_PREFIX env var (ontology-stack, injected by BrandEnvAspect)
    // do not flip atomically — they are separate stacks, so whichever updates
    // second leaves a window where emitted and matched sources disagree. A miss
    // there is SILENT: ontology accept still succeeds, VKG just never reloads
    // and serves stale mappings (and #851 means a skipped notify_vkg has no
    // in-product retry). Accepting both closes that window.
    //
    // REMOVE the legacy entry once every environment has deployed past this
    // change. Note it re-opens cross-deployment triggering for any deployment
    // still emitting `coa.ontology`, so do not stand up a second co-located
    // deployment until the legacy entry is gone.
    const legacySource = `${BRAND}.ontology`;
    const scopedSource = `${eventSourcePrefix}.ontology`;
    new events.Rule(this, "OntologyPublishedRule", {
      ruleName: this.prefixed("ontology-reload"),
      eventPattern: {
        source:
          scopedSource === legacySource
            ? [scopedSource]
            : [scopedSource, legacySource],
        detailType: ["ontology.published"],
      },
      targets: [
        new targets.LambdaFunction(reloadFn, {
          maxEventAge: cdk.Duration.minutes(5),
          retryAttempts: 2,
        }),
      ],
    });

    // Scheduled sweep: weekly, reconcile EVERY per-namespace VKG service to the
    // latest SSM image. OntologyPublishedRule only refreshes a namespace when
    // its ontology is re-accepted, so a long-lived namespace that is never
    // republished keeps its provision-time image forever — and Renovate
    // base-image digest bumps never reach a running task, leaving stale
    // (Inspector-flagged) VKG images. The sweep closes that gap. It is a no-op
    // per namespace when the image already matches, and each reload deploys with
    // the circuit-breaker + rollback in the handler, so a bad image self-heals.
    new events.Rule(this, "VkgReloadSweepRule", {
      ruleName: this.prefixed("vkg-reload-sweep"),
      schedule: events.Schedule.rate(cdk.Duration.days(7)),
      targets: [
        new targets.LambdaFunction(reloadFn, {
          event: events.RuleTargetInput.fromObject({ sweep: true }),
        }),
      ],
    });

    // ================================================================
    // OE monitoring (cdk-monitoring-constructs)
    // ================================================================
    new SclMonitoring(this, "Monitoring", {
      alarmNamePrefix: this.prefixed("vkg"),
      alarmAction: props.alarmAction,
    }).monitorClusterCpuMem(this.cluster.clusterName, "vkg");

    // Alarm on VKG reload failures. The reload Lambda emits
    // ReloadFailed both per-Namespace (for drill-down) and as an undimensioned
    // cluster-wide roll-up; the alarm evaluates the roll-up series. A metric
    // alarm CANNOT be backed by a SEARCH() expression (CloudFormation rejects it
    // with "SEARCH is not supported on Metric Alarms"), so we alarm on the
    // concrete undimensioned Sum, which receives data from every namespace.
    new cloudwatch.Alarm(this, "ReloadFailedAlarm", {
      alarmName: this.prefixed("vkg-reload-failed"),
      alarmDescription:
        "VKG ontology reload failed — the namespace may be serving stale or unavailable results",
      metric: new cloudwatch.Metric({
        namespace: "COA/VKG",
        metricName: "ReloadFailed",
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ================================================================
    // SSM Parameters
    // ================================================================
    new ssm.StringParameter(this, "SsmVkgClusterArn", {
      parameterName: `${ssmPrefix}/vkg/cluster-arn`,
      stringValue: this.cluster.clusterArn,
      description: "VKG ECS cluster ARN",
    });

    new ssm.StringParameter(this, "SsmVkgEndpoint", {
      parameterName: `${ssmPrefix}/vkg/endpoint`,
      stringValue: `http://vkg.${cloudMapNamespaceName}:${this.containerPort}`,
      description:
        "VKG endpoint pattern (per-namespace services resolve as vkg-{ns}.domain:port)",
    });

    // ================================================================
    // CfnOutputs
    // ================================================================
    new cdk.CfnOutput(this, "VkgClusterArn", {
      value: this.cluster.clusterArn,
      description: "VKG ECS cluster ARN",
    });

    new cdk.CfnOutput(this, "VkgEndpointPattern", {
      value: `http://vkg.${cloudMapNamespaceName}:${this.containerPort}`,
      description: "VKG endpoint pattern (per-namespace: vkg-{ns}.domain:port)",
    });
  }
}
