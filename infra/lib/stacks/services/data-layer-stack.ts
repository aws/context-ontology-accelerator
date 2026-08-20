// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Data Layer Stack — Lambda handler for runtime query/retrieval APIs.
 *
 * Provisions:
 * - Data Layer API Lambda (thin adapter to Context Manager via AgentCore Runtime)
 * - IAM permissions for bedrock-agentcore:InvokeAgentRuntime
 * - SSM parameter publishing the Lambda ARN for the API stack
 */
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import { SCLStack } from "../../constructs/scl-stack";
import { resolveContext } from "../../context";
import { fromRoot, Paths } from "../../paths";
import { bundlePython } from "../../utils/python-bundling";

export interface DataLayerStackProps extends cdk.StackProps {
  /** VPC for Lambda placement (AgentCore Runtime requires VPC connectivity). */
  readonly vpc: ec2.IVpc;

  /** Shared Lambda security group (from network stack). */
  readonly lambdaSecurityGroup: ec2.ISecurityGroup;

  /** Allowed CORS origin (e.g. "https://app.example.com"). Defaults to "*". */
  readonly allowedOrigin?: string;
}

export class DataLayerStack extends SCLStack {
  public readonly apiFn: lambda.Function;

  constructor(scope: Construct, id: string, props: DataLayerStackProps) {
    super(scope, id, props);

    const { ssmPrefix } = resolveContext(this.node);

    const runtimeArn = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/serve/runtime-arn`,
    );

    // Ontology-engine api-proxy Lambda ARN — data-layer's DescribeSchema
    // proxies straight to it (the CM never sees the request). Same source
    // MCP's discovery.describe_schema uses, so both surfaces converge on
    // one backend endpoint (``/namespaces/{namespaceId}/schema``).
    const ontologyProxyLambdaArn = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/ontology-engine/api-fn-arn`,
    );

    // Metric-service Lambda ARN — data-layer's ServeListMetrics proxies
    // straight to it (the CM never sees the request). Same source MCP's
    // discovery.list_metrics uses, so both surfaces return the same metric
    // catalog for the same input (``/namespaces/{namespaceId}/metrics``).
    const metricServiceLambdaArn = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/metric/api-fn-arn`,
    );

    // ── Lambda code bundle ─────────────────────────────────────────
    // The handler imports from ``coa_common`` (constants + smithy_shapes).
    // Importing any ``coa_common`` submodule runs its package ``__init__``,
    // which eagerly imports pydantic (authnz_types), pydantic-settings (config)
    // and structlog — so those MUST be in the bundle or the Lambda
    // ImportModuleErrors at cold start and API Gateway returns 502 on every
    // route. boto3/botocore are provided by the Lambda runtime and are not
    // bundled. (Regression guard: handler.py had no coa_common import before the
    // MCP/data-layer parity refactor, so the bundle shipped zero pip deps.)
    const lambdaCode = bundlePython({
      srcDirs: [fromRoot("packages/data-layer/src"), Paths.commonLib],
      pipDeps: ["pydantic", "pydantic-settings", "structlog"],
    });

    // ── Data Layer API Lambda ──────────────────────────────────────
    this.apiFn = new lambda.Function(this, "DataLayerApiFn", {
      functionName: this.prefixed("data-layer-api"),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "coa_data_layer.handler.handler",
      code: lambdaCode,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.lambdaSecurityGroup],
      environment: {
        AGENTCORE_RUNTIME_ARN: runtimeArn,
        ONTOLOGY_PROXY_LAMBDA_ARN: ontologyProxyLambdaArn,
        METRIC_SERVICE_LAMBDA_ARN: metricServiceLambdaArn,
        ALLOWED_ORIGIN: props.allowedOrigin ?? "*",
      },
    });

    // ── IAM: invoke AgentCore Runtime ─────────────────────────────
    // The actual invocation targets runtime/{id}/runtime-endpoint/{qualifier},
    // so we need a wildcard suffix on the runtime ARN.
    this.apiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: [`${runtimeArn}`, `${runtimeArn}/*`],
      }),
    );

    // ── IAM: invoke the discovery Lambdas direct-through (bypass CM) ──
    // Schema queries hit the ontology-api-proxy; metric-catalog queries hit
    // the metric-service. Both are pure reads with no tier orchestration, so
    // they skip the Context Manager entirely.
    this.apiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["lambda:InvokeFunction"],
        resources: [ontologyProxyLambdaArn, metricServiceLambdaArn],
      }),
    );

    // ── Publish Lambda ARN to SSM ──────────────────────────────────
    new ssm.StringParameter(this, "SsmDataLayerApiFnArn", {
      parameterName: `${ssmPrefix}/data-layer/api-fn-arn`,
      stringValue: this.apiFn.functionArn,
    });

    // ── Stack output ──────────────────────────────────────────────
    new cdk.CfnOutput(this, "DataLayerApiFnArn", {
      value: this.apiFn.functionArn,
      description: "Data Layer API Lambda function ARN",
    });
  }
}
