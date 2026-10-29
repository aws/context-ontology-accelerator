// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Namespace deletion Step Functions pipeline.
 *
 * Runs the cascading namespace cleanup asynchronously so the DELETE
 * /namespaces/{id} API call can return 202 immediately instead of blocking
 * on cleanup work that can exceed API Gateway's 29s integration timeout
 *.
 *
 * Pipeline:
 *   DeleteSources -> DeleteMetrics -> DeleteOntology -> DeletePlatform -> Finalize
 *   (any step's Catch -> MarkFailed -> Fail)
 *
 * DeleteSources/DeleteMetrics/DeletePlatform are steps whose underlying
 * cleanup is itself already best-effort (see cleanup.py — VKG, Athena,
 * DataZone, S3 all log-and-continue on failure) or safe to retry
 * (source/metric deletes are idempotent against a 404).  Each step gets 3
 * retries with backoff before the chain proceeds to MarkFailed, per the #213
 * acceptance criteria for non-critical steps.
 *
 * DeleteOntology is NOT in that best-effort group. It owns the AOSS vector
 * index, which is one index per namespace against a collection with a hard
 * 1000-index cap. Finalize deletes the namespace record, so an ontology
 * teardown that "logged and continued" would orphan that index forever —
 * nothing can target a namespace that no longer exists — until the collection
 * hits its cap and every embedding write in the deployment fails with
 * index_limit_breached. So delete_all_ontologies raises, the 3 retries apply,
 * and on exhaustion the Catch lands DELETE_FAILED, which is recoverable.
 *
 * Finalize (namespace record + name reservation delete) is the other
 * must-succeed step — it has no continue-on-failure path, since a
 * namespace whose finalize failed needs to remain retryable via
 * DELETE_FAILED rather than silently vanish from being "mostly" deleted.
 */
import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as opensearchserverless from "aws-cdk-lib/aws-opensearchserverless";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import { Construct } from "constructs";

export interface NamespaceDeletionPipelineProps {
  /** Prefixed name helper from SCLStack. */
  readonly prefixed: (name: string) => string;
  /** OpenSearch Serverless name helper from SCLStack (at most 32 characters). */
  readonly aossName: (name: string) => string;
  /** Bundled Python Lambda code (shared with the other namespace Lambdas). */
  readonly lambdaCode: lambda.Code;
  readonly vpc: ec2.IVpc;
  readonly vpcSubnets: ec2.SubnetSelection;
  /** Shared Lambda security group (allows egress + recognized by ECS ingress rules). */
  readonly securityGroups?: ec2.ISecurityGroup[];

  /** Namespaces table — read/write for finalize + mark-failed. */
  readonly namespacesTable: dynamodb.ITable;
  /** Resource-role-mappings table — delete-all for the namespace's grants. */
  readonly resourceRoleMappingsTable: dynamodb.ITable;
  /** Roles table — delete-all for the namespace's Cedar policies. */
  readonly rolesTable: dynamodb.ITable;

  /** sources-api Lambda function name (cross-Lambda invoke target). */
  readonly sourcesApiFnName: string;
  /** metric-api Lambda function name (cross-Lambda invoke target). */
  readonly metricApiFnName: string;

  /**
   * AOSS collection endpoint + name, for dropping the namespace's GraphRAG
   * doc-KG vector indexes (chunk, topic, and the legacy statement index).
   *
   * Those indexes are namespace-scoped — every doc source in a namespace shares
   * one GraphRAG tenant id — so they become garbage only when the namespace is
   * deleted, and DeleteSources is where "no sources remain" becomes true.
   * Nothing deleted them before, leaving one index set per deleted namespace
   * against a collection AOSS caps at 1000 indexes; past that every embedding
   * write in the deployment fails with index_limit_breached.
   *
   * Optional so a deployment without an AOSS collection still synthesizes; the
   * cleanup no-ops when OSS_ENDPOINT is unset.
   */
  readonly opensearchEndpoint?: string;
  readonly opensearchCollectionName?: string;

  readonly datazoneDomainId?: string;
  /**
   * ARN of namespaceApiFn's own execution role. DeletePlatform assumes
   * this role for datazone:DeleteProject — DataZone records the
   * CreateProject caller as the project's owner, and namespaceApiFn is
   * the role that actually calls CreateProject (see service.py's
   * NamespaceService.create), so it is the principal DataZone will
   * authorize for DeleteProject. Plain IAM permission on the action
   * alone (with a non-owner caller) is not sufficient.
   */
  readonly namespaceApiFnRoleArn?: string;
  readonly vkgClusterArn?: string;
  readonly cloudMapNamespaceId?: string;
  readonly ontologyBucketName?: string;
  readonly ontologyEngineEndpoint?: string;
  readonly resourcePrefix: string;
}

export class NamespaceDeletionPipeline extends Construct {
  public readonly stateMachine: sfn.StateMachine;
  /** DeletePlatform's Lambda — exposed so NamespaceStack can grant it
   * sts:AssumeRole on namespaceApiFn's role from the trust-policy side
   * (iam.Role.grantAssumeRole), avoiding a construction-order cycle
   * between the two Lambdas. */
  public readonly deletePlatformFn: lambda.Function;

  constructor(
    scope: Construct,
    id: string,
    props: NamespaceDeletionPipelineProps,
  ) {
    super(scope, id);

    const region = cdk.Stack.of(this).region;
    const account = cdk.Stack.of(this).account;

    const commonLambdaProps = {
      runtime: lambda.Runtime.PYTHON_3_12,
      code: props.lambdaCode,
      vpc: props.vpc,
      vpcSubnets: props.vpcSubnets,
      ...(props.securityGroups && { securityGroups: props.securityGroups }),
      memorySize: 256,
    };

    // ── DeleteSources ────────────────────────────────────────────────
    const deleteSourcesFn = new lambda.Function(this, "DeleteSourcesFn", {
      ...commonLambdaProps,
      functionName: props.prefixed("ns-deletion-sources"),
      handler:
        "coa_control_plane.namespace.deletion_pipeline.delete_sources.handler",
      timeout: cdk.Duration.minutes(10),
      environment: {
        SOURCES_API_FN_NAME: props.sourcesApiFnName,
        ...(props.opensearchEndpoint && {
          OSS_ENDPOINT: props.opensearchEndpoint,
        }),
      },
    });
    deleteSourcesFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["lambda:InvokeFunction"],
        resources: [
          `arn:aws:lambda:${region}:${account}:function:${props.sourcesApiFnName}`,
        ],
      }),
    );

    // Drop the namespace's GraphRAG doc-KG indexes after its sources are gone.
    // AOSS needs BOTH an IAM action and a data-access policy naming the role;
    // either alone yields 403. The data-access policy is declared here rather
    // than added to the sources stack's policy so this stays a single-stack
    // change with no cross-stack role-ARN dependency (AOSS permissions across
    // multiple data-access policies are additive).
    if (props.opensearchCollectionName) {
      deleteSourcesFn.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["aoss:APIAccessAll"],
          resources: [`arn:aws:aoss:${region}:${account}:collection/*`],
        }),
      );
      new opensearchserverless.CfnAccessPolicy(
        this,
        "NsDeletionGraphragIndexAccess",
        {
          name: props.aossName("ns-del-graphrag"),
          type: "data",
          description:
            "Allows namespace deletion to drop GraphRAG doc-KG indexes",
          policy: JSON.stringify([
            {
              Rules: [
                {
                  ResourceType: "index",
                  // Scoped to the GraphRAG index names this pipeline deletes —
                  // deliberately NOT index/<collection>/* so a bug here cannot
                  // reach the ontology or metric indexes in the same collection.
                  Resource: [
                    `index/${props.opensearchCollectionName}/chunk_*`,
                    `index/${props.opensearchCollectionName}/topic_*`,
                    `index/${props.opensearchCollectionName}/statement_*`,
                  ],
                  Permission: ["aoss:DescribeIndex", "aoss:DeleteIndex"],
                },
              ],
              Principal: [deleteSourcesFn.role!.roleArn],
            },
          ]),
        },
      );
    }

    // ── DeleteMetrics ────────────────────────────────────────────────
    const deleteMetricsFn = new lambda.Function(this, "DeleteMetricsFn", {
      ...commonLambdaProps,
      functionName: props.prefixed("ns-deletion-metrics"),
      handler:
        "coa_control_plane.namespace.deletion_pipeline.delete_metrics.handler",
      timeout: cdk.Duration.minutes(5),
      environment: { METRIC_API_FN_NAME: props.metricApiFnName },
    });
    deleteMetricsFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["lambda:InvokeFunction"],
        resources: [
          `arn:aws:lambda:${region}:${account}:function:${props.metricApiFnName}`,
        ],
      }),
    );

    // ── DeleteOntology ───────────────────────────────────────────────
    const deleteOntologyFn = new lambda.Function(this, "DeleteOntologyFn", {
      ...commonLambdaProps,
      functionName: props.prefixed("ns-deletion-ontology"),
      handler:
        "coa_control_plane.namespace.deletion_pipeline.delete_ontology.handler",
      // ontology-engine's own HTTP call has a 120s client timeout; give the
      // Lambda enough headroom above that plus the S3 sweep.
      timeout: cdk.Duration.minutes(4),
      environment: {
        ...(props.ontologyBucketName && {
          ONTOLOGY_BUCKET: props.ontologyBucketName,
        }),
        ...(props.ontologyEngineEndpoint && {
          ONTOLOGY_ENGINE_ENDPOINT: props.ontologyEngineEndpoint,
        }),
      },
    });
    if (props.ontologyBucketName) {
      deleteOntologyFn.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["s3:DeleteObject", "s3:ListBucket"],
          resources: [
            `arn:aws:s3:::${props.ontologyBucketName}`,
            `arn:aws:s3:::${props.ontologyBucketName}/ontologies/*`,
          ],
        }),
      );
    }

    // ── DeletePlatform (VKG, Athena, DataZone, role mappings, roles) ──
    const deletePlatformFn = new lambda.Function(this, "DeletePlatformFn", {
      ...commonLambdaProps,
      functionName: props.prefixed("ns-deletion-platform"),
      handler:
        "coa_control_plane.namespace.deletion_pipeline.delete_platform.handler",
      timeout: cdk.Duration.minutes(2),
      environment: {
        RESOURCE_ROLE_MAPPINGS_TABLE: props.resourceRoleMappingsTable.tableName,
        ROLES_TABLE: props.rolesTable.tableName,
        RESOURCE_PREFIX: props.resourcePrefix,
        ...(props.datazoneDomainId && {
          DATAZONE_DOMAIN_ID: props.datazoneDomainId,
        }),
        ...(props.namespaceApiFnRoleArn && {
          PROJECT_ACCESS_ROLE_ARN: props.namespaceApiFnRoleArn,
        }),
        ...(props.vkgClusterArn && { VKG_CLUSTER_ARN: props.vkgClusterArn }),
        ...(props.cloudMapNamespaceId && {
          CLOUD_MAP_NAMESPACE_ID: props.cloudMapNamespaceId,
        }),
      },
    });
    this.deletePlatformFn = deletePlatformFn;
    props.resourceRoleMappingsTable.grantReadWriteData(deletePlatformFn);
    props.rolesTable.grantReadWriteData(deletePlatformFn);
    deletePlatformFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["athena:DeleteWorkGroup", "athena:GetWorkGroup"],
        resources: [`arn:aws:athena:${region}:${account}:workgroup/*`],
      }),
    );
    if (props.namespaceApiFnRoleArn) {
      // DataZone requires the caller to be the project *owner* for
      // DeleteProject — plain IAM permission on the action is not
      // sufficient. namespaceApiFn's role is the actual owner (it called
      // CreateProject), so DeletePlatform assumes it rather than calling
      // DeleteProject with its own (non-owner) execution role. The
      // corresponding grantAssumeRole (trust-policy side) is added on
      // namespaceApiFn's role in namespace-stack.ts, since granting
      // sts:AssumeRole requires cooperation from both the assuming
      // principal's identity policy (here) and the target role's trust
      // policy (there).
      deletePlatformFn.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["sts:AssumeRole"],
          resources: [props.namespaceApiFnRoleArn],
        }),
      );
    }
    if (props.vkgClusterArn) {
      deletePlatformFn.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["ecs:DeleteService", "ecs:DescribeServices"],
          resources: ["*"],
          conditions: { ArnLike: { "ecs:cluster": props.vkgClusterArn } },
        }),
      );
    }
    if (props.cloudMapNamespaceId) {
      deletePlatformFn.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "servicediscovery:DeleteService",
            "servicediscovery:ListServices",
            "servicediscovery:ListInstances",
            "servicediscovery:DeregisterInstance",
          ],
          resources: ["*"],
        }),
      );
    }

    // ── Finalize (must-succeed: namespace record + name reservation) ─
    const finalizeFn = new lambda.Function(this, "FinalizeFn", {
      ...commonLambdaProps,
      functionName: props.prefixed("ns-deletion-finalize"),
      handler: "coa_control_plane.namespace.deletion_pipeline.finalize.handler",
      timeout: cdk.Duration.seconds(30),
      environment: { NAMESPACES_TABLE: props.namespacesTable.tableName },
    });
    finalizeFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:TransactWriteItems", "dynamodb:DeleteItem"],
        resources: [props.namespacesTable.tableArn],
      }),
    );

    // ── MarkFailed (error path: status -> DELETE_FAILED) ─────────────
    const markFailedFn = new lambda.Function(this, "MarkFailedFn", {
      ...commonLambdaProps,
      functionName: props.prefixed("ns-deletion-mark-failed"),
      handler:
        "coa_control_plane.namespace.deletion_pipeline.mark_failed.handler",
      timeout: cdk.Duration.seconds(10),
      memorySize: 128,
      environment: { NAMESPACES_TABLE: props.namespacesTable.tableName },
    });
    props.namespacesTable.grantWriteData(markFailedFn);

    // ── Step Functions definition ─────────────────────────────────────
    const deleteSources = new tasks.LambdaInvoke(this, "DeleteSources", {
      lambdaFunction: deleteSourcesFn,
      resultPath: "$.sourcesResult",
      payloadResponseOnly: true,
    });
    deleteSources.addRetry({
      errors: ["States.ALL"],
      maxAttempts: 3,
      backoffRate: 2,
      interval: cdk.Duration.seconds(5),
    });

    const deleteMetrics = new tasks.LambdaInvoke(this, "DeleteMetrics", {
      lambdaFunction: deleteMetricsFn,
      resultPath: "$.metricsResult",
      payloadResponseOnly: true,
    });
    deleteMetrics.addRetry({
      errors: ["States.ALL"],
      maxAttempts: 3,
      backoffRate: 2,
      interval: cdk.Duration.seconds(5),
    });

    const deleteOntology = new tasks.LambdaInvoke(this, "DeleteOntology", {
      lambdaFunction: deleteOntologyFn,
      resultPath: "$.ontologyResult",
      payloadResponseOnly: true,
    });
    deleteOntology.addRetry({
      errors: ["States.ALL"],
      maxAttempts: 3,
      backoffRate: 2,
      interval: cdk.Duration.seconds(5),
    });

    const deletePlatform = new tasks.LambdaInvoke(this, "DeletePlatform", {
      lambdaFunction: deletePlatformFn,
      resultPath: "$.platformResult",
      payloadResponseOnly: true,
    });
    deletePlatform.addRetry({
      errors: ["States.ALL"],
      maxAttempts: 3,
      backoffRate: 2,
      interval: cdk.Duration.seconds(5),
    });

    const finalize = new tasks.LambdaInvoke(this, "Finalize", {
      lambdaFunction: finalizeFn,
      resultPath: "$.finalizeResult",
      payloadResponseOnly: true,
    });
    finalize.addRetry({
      errors: ["States.ALL"],
      maxAttempts: 3,
      backoffRate: 2,
      interval: cdk.Duration.seconds(5),
    });

    const markFailed = new tasks.LambdaInvoke(this, "MarkFailed", {
      lambdaFunction: markFailedFn,
      resultPath: "$.markFailedResult",
      payloadResponseOnly: true,
    });

    const deletionFailed = new sfn.Fail(this, "DeletionFailed", {
      cause: "Namespace deletion failed",
      error: "NamespaceDeletionError",
    });
    markFailed.next(deletionFailed);

    // Each step catches into MarkFailed after its own retries are
    // exhausted — a single terminal failure path, no continue-past-failure
    // for any step. (The MVP simplification called for in #213 — "steps
    // 2-5 may be stubs" — doesn't apply here since delete_sources/
    // delete_metrics call the real, already-implemented sources-api and
    // metric-api delete paths rather than stubs.)
    deleteSources.addCatch(markFailed, { resultPath: "$.error" });
    deleteMetrics.addCatch(markFailed, { resultPath: "$.error" });
    deleteOntology.addCatch(markFailed, { resultPath: "$.error" });
    deletePlatform.addCatch(markFailed, { resultPath: "$.error" });
    finalize.addCatch(markFailed, { resultPath: "$.error" });

    const definition = deleteSources
      .next(deleteMetrics)
      .next(deleteOntology)
      .next(deletePlatform)
      .next(finalize);

    this.stateMachine = new sfn.StateMachine(this, "StateMachine", {
      stateMachineName: props.prefixed("namespace-deletion-pipeline"),
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      timeout: cdk.Duration.hours(1),
      tracingEnabled: true,
    });
  }
}
