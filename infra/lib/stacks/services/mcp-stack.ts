// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as bedrockagentcore from "aws-cdk-lib/aws-bedrockagentcore";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import { Construct } from "constructs";
import { prefixedLambdaArn, resolveContext } from "../../context";
import { agentCoreSupportedSubnets } from "../../utils/agentcore-az";
import { brandEnv } from "../../utils/brand-env";
import { parseEcrImageUri } from "../../utils/ecr-utils";
import { Paths } from "../../paths";
import { METRIC_API_FUNCTION_NAME } from "./metric-service-stack";
import { ONTOLOGY_API_PROXY_FUNCTION_NAME } from "./ontology-stack";
import { SCLStack } from "../../constructs";

export interface McpStackProps extends cdk.StackProps {
  /** VPC for AgentCore Runtime network interfaces. */
  readonly vpc: ec2.IVpc;
  /** Roles DDB table for Cedar auth in MCP grant resolver. */
  readonly rolesTable: dynamodb.ITable;
  /** Resource-role-mappings DDB table for role resolution. */
  readonly resourceRoleMappingsTable: dynamodb.ITable;
  /** AgentCore-supported AZ names for subnet filtering. */
  readonly agentCoreAzNames?: string[];
}

/**
 * MCP Server: Thin protocol adapter on AgentCore Runtime.
 *
 * Architecture (post-refactor):
 * - Discovery tools (list_metrics, describe_schema) invoke backend Lambdas directly
 * - Execution tools (query, translate, rag, graph) forward to Context Manager
 *   via AgentCore invocations API (HTTP POST with JWT forwarding)
 * - MCP server does NOT run the orchestrator in-process
 * - Minimal IAM: Lambda invoke (discovery), InvokeAgentRuntime (execution),
 *   DynamoDB read (roles/RRM), SSM
 *
 */
export class McpStack extends SCLStack {
  public readonly agentRuntimeArn: string;

  constructor(scope: Construct, id: string, props: McpStackProps) {
    super(scope, id, props);
    this.addComponentTag("mcp");

    const region = cdk.Stack.of(this).region;
    const account = cdk.Stack.of(this).account;
    const { ssmPrefix } = resolveContext(this.node);

    const runtimeName = this.prefixed("mcp-server").replace(/-/g, "_");

    // ── Resolve Context Manager Runtime ARN from SSM ──────────────────
    const cmRuntimeArn = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/serve/runtime-arn`,
    );

    // IdP issuer/client IDs via SSM (avoids a cross-stack export on the auth
    // stack's UserPool/UserPoolClient/McpClient — see idp-authentication-stack.ts
    // for the writer).
    const issuerUrl = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/issuer`,
    );
    const clientId = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/userpool-client-id`,
    );
    const mcpClientId = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/mcp-client-id`,
    );
    // The JWT claim name carrying group membership — "cognito:groups" for
    // Cognito, or the customer's configured claim (oidcSettings.groupClaim,
    // default "groups") for direct OIDC. Read from SSM instead of hardcoding
    // DEFAULT_GROUP_CLAIM ("cognito:groups"): that constant is wrong on the
    // OIDC path and silently breaks all group-based role resolution.
    const groupClaimName = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/authentication-group-token-name`,
    );

    // ── Backend Lambda ARNs (discovery tools) ─────────────────────────
    // Built from the functions' fixed names so this stack needs no dependency on
    // the metric-service and ontology stacks. Keep it that way: serve and mcp
    // share one image asset, and CDK makes that publish wait for all of mcp's
    // dependencies, so an edge here would delay ServeStack too.
    const metricServiceLambdaArn = prefixedLambdaArn(
      this,
      METRIC_API_FUNCTION_NAME,
    );
    const ontologyProxyLambdaArn = prefixedLambdaArn(
      this,
      ONTOLOGY_API_PROXY_FUNCTION_NAME,
    );

    // ── Security Group (minimal: HTTPS only) ──────────────────────────
    const mcpSg = new ec2.SecurityGroup(this, "McpSG", {
      vpc: props.vpc,
      description: "AgentCore Runtime - MCP Server (thin proxy)",
      allowAllOutbound: false,
    });

    // HTTPS to VPC endpoints (AgentCore invocations)
    mcpSg.addEgressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(443),
      "HTTPS to VPC endpoints (AgentCore)",
    );
    // HTTPS to AgentCore public endpoint (invocations API via NAT)
    mcpSg.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "HTTPS to AgentCore invocations API",
    );

    // ── Container Image ───────────────────────────────────────────────
    const explicitImageUri = this.node.tryGetContext(
      "context_manager_image_uri",
    ) as string | undefined;

    let imageUri: string;
    let dockerAsset: DockerImageAsset | undefined;
    if (explicitImageUri) {
      imageUri = explicitImageUri;
    } else {
      dockerAsset = new DockerImageAsset(this, "McpImage", {
        directory: Paths.root,
        file: "packages/context-manager/Dockerfile",
        platform: Platform.LINUX_ARM64,
      });
      imageUri = dockerAsset.imageUri;
    }

    // ── AgentCore Runtime (MCP protocol) ──────────────────────────────
    const runtime = new bedrockagentcore.Runtime(this, "McpRuntime", {
      runtimeName,
      agentRuntimeArtifact:
        bedrockagentcore.AgentRuntimeArtifact.fromImageUri(imageUri),
      protocolConfiguration: bedrockagentcore.ProtocolType.MCP,
      authorizerConfiguration:
        bedrockagentcore.RuntimeAuthorizerConfiguration.usingJWT(
          `${issuerUrl}/.well-known/openid-configuration`,
          // allowedClients omitted: AgentCore uses AND semantics, and ID tokens
          // don't carry client_id. We use allowedAudience (aud claim) instead,
          // which is present on both ID tokens and works with the CM runtime.
          undefined,
          [clientId, mcpClientId],
        ),
      networkConfiguration:
        bedrockagentcore.RuntimeNetworkConfiguration.usingVpc(this, {
          vpc: props.vpc,
          vpcSubnets: {
            subnets: agentCoreSupportedSubnets(
              props.vpc,
              props.agentCoreAzNames,
            ),
          },
          securityGroups: [mcpSg],
        }),
      requestHeaderConfiguration: {
        allowlistedHeaders: ["Authorization"],
      },
      tracingEnabled: false,
      environmentVariables: {
        ...brandEnv(this.node),
        ENVIRONMENT: (this.node.tryGetContext("env") as string) ?? "dev",
        SSM_PREFIX: ssmPrefix,
        LOG_LEVEL: "INFO",
        AWS_REGION: region,
        // Grant resolver needs roles/RRM tables
        ROLES_TABLE_NAME: props.rolesTable.tableName,
        RRM_TABLE_NAME: props.resourceRoleMappingsTable.tableName,
        GROUP_CLAIM_NAME: groupClaimName,
        // Execution tools forward to Context Manager via HTTP
        CM_RUNTIME_ARN: cmRuntimeArn,
        // Discovery tools invoke backend Lambdas directly
        METRIC_SERVICE_LAMBDA_ARN: metricServiceLambdaArn,
        ONTOLOGY_PROXY_LAMBDA_ARN: ontologyProxyLambdaArn,
        // Override entrypoint to run MCP server instead of Context Manager
        SCL_MCP_MODE: "true",
        // Independent JWT signature verification in claims.py, as
        // defense-in-depth behind the AgentCore RuntimeAuthorizerConfiguration
        // below. Same issuer/audience trust anchor, IdP-agnostic (Cognito,
        // Okta, EntraID, ...) via coa_common.auth.build_token_authorizer.
        // MCP callers authenticate as the MCP/CLI client, so JWT_CLIENT_ID
        // uses mcpClientId (not the web app's clientId).
        JWT_ISSUER_URL: issuerUrl,
        JWT_CLIENT_ID: mcpClientId,
      },
      description:
        "MCP Server - protocol adapter with PKCE auth, forwards execution to Context Manager",
    });

    // ── IAM Policies (minimal) ────────────────────────────────────────

    // ECR image pull
    runtime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ecr:GetAuthorizationToken"],
        resources: ["*"],
      }),
    );
    if (explicitImageUri) {
      const ecr = parseEcrImageUri(explicitImageUri);
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchGetImage",
            "ecr:BatchCheckLayerAvailability",
          ],
          resources: [ecr.repositoryArn],
        }),
      );
    } else if (dockerAsset) {
      dockerAsset.repository.grantPull(runtime.role!);
    }

    // SSM read (resolve CM runtime ARN, other config)
    runtime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [`arn:aws:ssm:${region}:${account}:parameter${ssmPrefix}/*`],
      }),
    );

    // Invoke Context Manager via AgentCore invocations API
    runtime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:InvokeAgentRuntime",
          "bedrock-agentcore:InvokeAgentRuntimeForUser",
        ],
        resources: [`arn:aws:bedrock-agentcore:${region}:${account}:runtime/*`],
      }),
    );

    // Invoke backend Lambdas directly (discovery tools)
    runtime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["lambda:InvokeFunction"],
        resources: [metricServiceLambdaArn, ontologyProxyLambdaArn],
      }),
    );

    // DynamoDB read (grant resolver needs roles + resource-role-mappings)
    runtime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:BatchGetItem",
        ],
        resources: [
          props.rolesTable.tableArn,
          props.resourceRoleMappingsTable.tableArn,
          props.resourceRoleMappingsTable.tableArn + "/index/*",
        ],
      }),
    );

    // ── SSM Parameters ────────────────────────────────────────────────
    new ssm.StringParameter(this, "McpRuntimeArnParam", {
      parameterName: `${ssmPrefix}/mcp/runtime-arn`,
      stringValue: runtime.agentRuntimeArn,
      description: "AgentCore Runtime ARN for the MCP Server",
    });

    new ssm.StringParameter(this, "McpRuntimeRoleArnParam", {
      parameterName: `${ssmPrefix}/mcp/runtime-role-arn`,
      stringValue: runtime.role!.roleArn,
      description: "MCP Runtime IAM role ARN",
    });

    this.agentRuntimeArn = runtime.agentRuntimeArn;

    // ── Outputs ───────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "McpRuntimeArn", {
      value: runtime.agentRuntimeArn,
      description: "MCP AgentCore Runtime ARN",
    });

    new cdk.CfnOutput(this, "McpRuntimeId", {
      value: runtime.agentRuntimeId,
      description: "MCP AgentCore Runtime ID",
    });

    new cdk.CfnOutput(this, "McpRuntimeName", {
      value: runtimeName,
      description: "MCP AgentCore Runtime name",
    });
  }
}
