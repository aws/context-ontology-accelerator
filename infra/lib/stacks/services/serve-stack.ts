// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as bedrockagentcore from "aws-cdk-lib/aws-bedrockagentcore";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as opensearchserverless from "aws-cdk-lib/aws-opensearchserverless";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import { Construct } from "constructs";
import type { IAlarmActionStrategy } from "cdk-monitoring-constructs/lib/common/alarm/action";
import { resolveContext } from "../../context";
import { agentCoreSupportedSubnets } from "../../utils/agentcore-az";
import { brandEnv } from "../../utils/brand-env";
import { parseEcrImageUri } from "../../utils/ecr-utils";
import { bundlePython } from "../../utils/python-bundling";
import { fromRoot, Paths } from "../../paths";
import { DynamoDBTable, SCLStack, SclMonitoring } from "../../constructs";
import { DEFAULT_BEDROCK_MODEL_ID } from "../../constants";
import { TABLE_NAMES } from "@coa/shared";

export interface ServeStackProps extends cdk.StackProps {
  /** VPC for AgentCore Runtime network interfaces. */
  readonly vpc: ec2.IVpc;
  /** Security group attached to the AOSS VPC endpoint — AgentCore SG needs ingress here. */
  readonly aossSecurityGroup: ec2.ISecurityGroup;
  /** Neptune cluster security group — AgentCore SG needs ingress here (Gremlin/SPARQL). */
  readonly neptuneSecurityGroup: ec2.ISecurityGroup;
  /** Lambda security group (allowAllOutbound) for AOSS proxy. TODO(aoss-proxy): remove */
  readonly lambdaSecurityGroup: ec2.ISecurityGroup;
  /** Cognito issuer URL for JWT auth (e.g., https://cognito-idp.<region>.amazonaws.com/<poolId>). */
  readonly issuerUrl: string;
  /** Cognito App Client ID — used as allowed audience for JWT validation. */
  readonly clientId: string;
  /** Additional client IDs to accept as audience (e.g. MCP CLI client). */
  readonly additionalAudience?: string[];
  /** Neptune cluster ARN — graph store for ontology traversal. */
  readonly neptuneClusterArn: string;
  /** Neptune cluster writer endpoint (host for SigV4 Gremlin/SPARQL). */
  readonly neptuneEndpoint: string;

  /** S3 bucket ARN — compiled ontology artifacts (OWL, R2RML). */
  readonly ontologyBucketArn: string;
  /** VKG translation service endpoint (e.g. http://vkg.coa-dev-services.local:8080). */
  readonly vkgEndpoint: string;
  /** S3 bucket name for Athena query results. */
  readonly athenaResultsBucketName?: string;
  /**
   * Roles DDB table (authnz stack) — the serve-path Cedar authorizer loads
   * role policies from DDB (control-plane parity, enables namespace-custom
   * policies on the data path). Required, matching {@link ApiStack}; the authnz
   * stack always provides it. Seed-only operation is selected at the runtime
   * layer (the `ROLES_TABLE_NAME` env var being unset), not by omitting it here.
   */
  readonly rolesTable: dynamodb.ITable;
  /** Resource-role-mappings DDB table (authnz stack) — resolves principal
   *  (user/group) grants to globalRoles/resourceRoles for Cedar evaluation. */
  readonly resourceRoleMappingsTable: dynamodb.ITable;
  /** IdP group claim name forwarded to the runtime (same prop as api-stack). */
  readonly groupClaimName?: string;
  /**
   * AgentCore-supported AZ **names** for the deploying account (resolved
   * in the app entrypoint). Used to defensively filter the subnets handed
   * to AgentCore Runtime. Omit to pass all subnets through.
   */
  readonly agentCoreAzNames?: string[];

  /** Bedrock LLM model ID for query resolution (NL-to-SPARQL, synthesis).
   *  Defaults to us.anthropic.claude-sonnet-5 at runtime when omitted. */
  readonly bedrockLlmModelId?: string;

  /** Comma-separated list of model IDs allowed for per-request override.
   *  When omitted, any valid model ID is accepted (open by default). */
  readonly allowedOverrideModels?: string[];

  /** OE alarm action strategy (SNS/chatbot routing). */
  readonly alarmAction?: IAlarmActionStrategy;
}

/**
 * Serve layer: Context Manager on Bedrock AgentCore Runtime.
 *
 * Uses the L2 alpha construct (@aws-cdk/aws-bedrock-agentcore-alpha) which
 * handles role trust policy, workload identity, and runtime lifecycle
 * automatically — matching the proven Fusion pattern.
 *
 * The runtime is created only when `context_manager_image_uri` CDK context
 * is provided. When omitted (local dev), only the AOSS data access policy
 * and SG ingress rule are created.
 */
export class ServeStack extends SCLStack {
  public readonly agentRuntimeName: string;
  /** AOSS search proxy Lambda ARN — shared with MCP stack. */
  public readonly aossProxyLambdaArn: string;
  /** AgentCore Runtime public invocations endpoint for SSE streaming. */
  public readonly queryEndpoint?: string;

  constructor(scope: Construct, id: string, props: ServeStackProps) {
    super(scope, id, props);
    this.addComponentTag("serve");

    this.agentRuntimeName = this.prefixed("context-manager");

    const region = cdk.Stack.of(this).region;
    const account = cdk.Stack.of(this).account;
    const { prefix, ssmPrefix } = resolveContext(this.node);

    // ── Resolve OpenSearch config via SSM (decoupled from storage stack exports) ──
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

    // ── Construct sources table name deterministically ─────────────────
    // Avoids an SSM cross-stack read from SourcesStack (which would create a
    // circular dependency when SourcesStack needs this stack's runtime role ARN).
    const dataSourcesTableName = this.prefixed(TABLE_NAMES.SOURCES);
    const dataSourcesTableArn = `arn:aws:dynamodb:${region}:${account}:table/${dataSourcesTableName}`;

    const namespacesTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/namespace/namespaces-table-name`,
    );
    const namespacesTableArn = `arn:aws:dynamodb:${region}:${account}:table/${namespacesTableName}`;

    const runtimeName = this.agentRuntimeName.replace(/-/g, "_");

    // ── Security Group ──────────────────────────────────────────────────

    const agentCoreSg = new ec2.SecurityGroup(this, "AgentCoreSG", {
      vpc: props.vpc,
      description: "AgentCore Runtime - Context Manager",
      allowAllOutbound: false,
    });

    agentCoreSg.addEgressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(443),
      "HTTPS to VPC endpoints",
    );
    agentCoreSg.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "HTTPS to external services via NAT",
    );
    agentCoreSg.addEgressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(8182),
      "Neptune within VPC",
    );
    agentCoreSg.addEgressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(8080),
      "VKG service within VPC",
    );
    agentCoreSg.addEgressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(5432),
      "PostgreSQL to JDBC sources within VPC",
    );
    agentCoreSg.addEgressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(3306),
      "MySQL to JDBC sources within VPC",
    );
    agentCoreSg.addEgressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(1433),
      "MSSQL to JDBC sources within VPC",
    );
    agentCoreSg.addEgressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(5439),
      "Redshift to JDBC sources within VPC",
    );

    // ── Cross-VPC JDBC egress (peering / TGW destination CIDRs) ──────
    // When jdbc_peer_cidrs or jdbc_tgw_cidrs are configured, the serve
    // runtime must reach databases in those remote networks. Without these
    // rules, connections hang at 60s (TCP SYN silently dropped by SG).
    // Standard DB ports: PostgreSQL(5432), MySQL(3306), MSSQL(1433), Redshift(5439).
    const jdbcDbPorts = [5432, 3306, 1433, 5439];
    const parseCidrs = (key: string): string[] => {
      const raw = this.node.tryGetContext(key);
      if (typeof raw !== "string" || !raw.trim()) {
        return [];
      }
      return raw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    };
    const remoteCidrs = [
      ...parseCidrs("jdbc_peer_cidrs"),
      ...parseCidrs("jdbc_tgw_cidrs"),
    ];
    for (const cidr of remoteCidrs) {
      for (const port of jdbcDbPorts) {
        agentCoreSg.addEgressRule(
          ec2.Peer.ipv4(cidr),
          ec2.Port.tcp(port),
          `JDBC port ${port} to peer network ${cidr}`,
        );
      }
    }

    // Allow AgentCore → AOSS VPC endpoint (L1 to avoid cross-stack cycle)
    new ec2.CfnSecurityGroupIngress(this, "AossVpceIngress", {
      groupId: props.aossSecurityGroup.securityGroupId,
      ipProtocol: "tcp",
      fromPort: 443,
      toPort: 443,
      sourceSecurityGroupId: agentCoreSg.securityGroupId,
      description: "Allow HTTPS from AgentCore to AOSS VPC endpoint",
    });

    // Allow AgentCore → Neptune (L1 to avoid cross-stack cycle). Without this,
    // agentCoreSg's egress rule on 8182 is necessary but not sufficient —
    // Neptune's own SG only allowed ingress from the ECS and Lambda SGs, so
    // every Gremlin/SPARQL call from the AgentCore runtime hung until
    // ConnectTimeout (~30s) and Tier-1/Tier-2 resolution never completed.
    new ec2.CfnSecurityGroupIngress(this, "NeptuneIngress", {
      groupId: props.neptuneSecurityGroup.securityGroupId,
      ipProtocol: "tcp",
      fromPort: 8182,
      toPort: 8182,
      sourceSecurityGroupId: agentCoreSg.securityGroupId,
      description: "Allow Neptune Gremlin/SPARQL from AgentCore runtime",
    });

    // ── AOSS Search Proxy Lambda ──────────────────────────────────────
    // AgentCore containers cannot access AOSS data plane directly (service
    // principal not recognized). Route vector search through a Lambda proxy
    // whose role IS recognized by AOSS. Remove when AWS fixes this.

    const proxyCode = bundlePython({
      srcDirs: [
        fromRoot("packages/context-manager/lambda/aoss-search-proxy"),
        // index.py imports coa_common (resolve_region); include
        // libs/common/src + its runtime deps or the Lambda fails at import
        // with Runtime.ImportModuleError: No module named
        // 'coa_common'. Bare names (no `>=`) avoid the shell
        // treating the version spec as an output redirect during local bundling.
        fromRoot("libs/common/src"),
      ],
      pipDeps: [
        "opensearch-py",
        "requests-aws4auth",
        "cedarpy",
        "cryptography",
        "pydantic-settings",
        "PyJWT",
        "requests",
        "structlog",
      ],
      architecture: "arm64",
    });

    const aossProxyFn = new lambda.Function(this, "AossSearchProxyFn", {
      functionName: this.prefixed("aoss-search-proxy"),
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "index.handler",
      code: proxyCode,
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      environment: {
        OPENSEARCH_ENDPOINT: opensearchEndpoint,
      },
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.lambdaSecurityGroup],
    });

    // Expose for MCP stack to share the same proxy
    this.aossProxyLambdaArn = aossProxyFn.functionArn;

    // Write to SSM so other stacks can read without cross-stack exports
    new ssm.StringParameter(this, "AossProxyArnParam", {
      parameterName: `${ssmPrefix}/serve/aoss-proxy-lambda-arn`,
      stringValue: aossProxyFn.functionArn,
      description: "AOSS search proxy Lambda ARN (shared with MCP stack)",
    });

    aossProxyFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["aoss:APIAccessAll"],
        resources: [opensearchCollectionArn],
      }),
    );

    new opensearchserverless.CfnAccessPolicy(this, "OSSProxyDataAccess", {
      name: this.prefixed("proxy-read-only"),
      type: "data",
      // NOTE: keep this description in sync with the deployed policy. AOSS's
      // UpdateAccessPolicy API rejects an update that only removes the
      // description (InvalidRequest), which would fail the whole stack update,
      // so it must be preserved rather than dropped.
      description: "Read-only data access for AOSS search proxy Lambda",
      policy: JSON.stringify([
        {
          Rules: [
            {
              ResourceType: "index",
              Resource: [`index/${opensearchCollectionName}/*`],
              Permission: ["aoss:DescribeIndex", "aoss:ReadDocument"],
            },
            {
              ResourceType: "collection",
              Resource: [`collection/${opensearchCollectionName}`],
              Permission: ["aoss:DescribeCollectionItems"],
            },
          ],
          Principal: [aossProxyFn.role!.roleArn],
        },
      ]),
    });

    // ================================================================
    // OE monitoring (cdk-monitoring-constructs). Covers the AOSS search
    // proxy Lambda. AgentCore Runtime internal health is out of round one
    // (no native facade support); the WebSocket ALB was removed from this
    // stack in the WebSocket-cleanup migration, so there is no ALB to cover.
    // ================================================================
    const monitoring = new SclMonitoring(this, "Monitoring", {
      alarmNamePrefix: this.prefixed("serve"),
      alarmAction: props.alarmAction,
    });
    monitoring.monitorLambda(aossProxyFn);

    // ── AgentCore Runtime (L2 alpha construct) ──────────────────────────

    // ── Session Metadata Table (DynamoDB) ────────────────────────────────
    // Lightweight index for session discovery (most-recent lookup).
    // Message content stays in AgentCore Memory; this stores only metadata.
    const sessionMetadata = new DynamoDBTable(this, "SessionMetadata", {
      tableName: "session-metadata",
      partitionKey: { name: "userId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sessionId", type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: "ttl",
    });
    sessionMetadata.addGlobalSecondaryIndex({
      indexName: "userId-lastActiveAt-index",
      partitionKey: { name: "userId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "lastActiveAt", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    // GSI for namespace-scoped session queries (most recent per user+namespace)
    sessionMetadata.addGlobalSecondaryIndex({
      indexName: "userId-nsLastActiveAt-index",
      partitionKey: { name: "userId", type: dynamodb.AttributeType.STRING },
      sortKey: {
        name: "nsLastActiveAt",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    const sessionMetadataTableArn = sessionMetadata.table.tableArn;

    const explicitImageUri = this.node.tryGetContext(
      "context_manager_image_uri",
    ) as string | undefined;

    let imageUri: string;
    let dockerAsset: DockerImageAsset | undefined;
    if (explicitImageUri) {
      imageUri = explicitImageUri;
    } else {
      dockerAsset = new DockerImageAsset(this, "ContextManagerImage", {
        directory: Paths.root,
        file: "packages/context-manager/Dockerfile",
        platform: Platform.LINUX_ARM64,
      });
      imageUri = dockerAsset.imageUri;
    }

    {
      // ── AgentCore Memory — conversation session state ───────────────
      const memory = new bedrockagentcore.Memory(this, "SessionMemory", {
        memoryName: runtimeName + "_memory",
        description: "Conversation history for Playground multi-turn sessions",
        expirationDuration: cdk.Duration.days(30),
      });

      const runtime = new bedrockagentcore.Runtime(this, "AgentCoreRuntime", {
        runtimeName,
        agentRuntimeArtifact:
          bedrockagentcore.AgentRuntimeArtifact.fromImageUri(imageUri),
        authorizerConfiguration:
          bedrockagentcore.RuntimeAuthorizerConfiguration.usingJWT(
            `${props.issuerUrl}/.well-known/openid-configuration`,
            // allowedClients is intentionally omitted. AgentCore verifies ALL
            // configured fields (AND semantics), and `client_id` is an
            // access-token-only claim — setting it would reject ID tokens.
            // We validate the standard OIDC `aud` claim instead, which is
            // IdP-agnostic and present on ID tokens from any compliant IdP.
            // The web-app sends the ID token (carries email + groups) on both
            // the WebSocket and REST (via data-layer) paths.
            undefined,
            // allowedAudience: accept ID tokens from web app AND MCP proxy
            [props.clientId, ...(props.additionalAudience ?? [])], // allowedAudience — ID token `aud` == app client id
          ),
        // Defense-in-depth: even though NetworkStack pins the VPC's
        // AZs to AgentCore-supported ones, filter again here so an
        // imported VPC (or a future regression) can't sneak an
        // unsupported subnet through. ``agentCoreSupportedSubnets``
        // throws at synth time if the result would be empty.
        networkConfiguration:
          bedrockagentcore.RuntimeNetworkConfiguration.usingVpc(this, {
            vpc: props.vpc,
            vpcSubnets: {
              subnets: agentCoreSupportedSubnets(
                props.vpc,
                props.agentCoreAzNames,
              ),
            },
            securityGroups: [agentCoreSg],
          }),
        requestHeaderConfiguration: {
          allowlistedHeaders: ["Authorization"],
        },
        environmentVariables: {
          ...brandEnv(this.node),
          ENVIRONMENT: (this.node.tryGetContext("env") as string) ?? "dev",
          SSM_PREFIX: ssmPrefix,
          LOG_LEVEL: "INFO",
          AWS_REGION: region,
          NEPTUNE_ENDPOINT: props.neptuneEndpoint,
          VKG_ENDPOINT: props.vkgEndpoint,
          OPENSEARCH_ENDPOINT: opensearchEndpoint,
          // TODO(aoss-proxy): Remove when AgentCore supports AOSS directly
          OPENSEARCH_PROXY_LAMBDA_ARN: aossProxyFn.functionArn,
          ATHENA_OUTPUT_S3: `s3://${props.athenaResultsBucketName ?? this.prefixed("athena-results")}/`,
          ATHENA_WORKGROUP_PREFIX: `${this.prefixed("")}`,
          // ATHENA_WORKGROUP_PREFIX is a fallback; primary resolution reads
          // athenaWorkgroupName from the namespaces DDB table at runtime.
          OSS_ONTOLOGY_INDEX: opensearchCollectionName,
          // Default engine for a request that does not set `options.mode`. Standard
          // (not agentic), because agentic runs ~90s at p50 and the REST/MCP callers
          // time out at 15-30s. This does NOT gate construction — the agentic
          // retriever is always built, so an explicit `options.mode: "agentic"` still
          // engages the loop.
          //
          // Within standard, the engine is "lexical-baseline" running topic_beam
          // (LEXICAL_RETRIEVER_STRATEGY below) rather than the older "hand-rolled"
          // VectorRetriever + GraphTraverser path. topic_beam is the strongest
          // single-shot strategy on the SEC-10-Q benchmark (45.13% strict vs 34.36%
          // hand-rolled, 195 questions), so hand-rolled was leaving ~11pp on the
          // table for every caller that did not know to pass
          // options.retrieverStrategy. Override with TIER3_STRATEGY context to get
          // back to "hand-rolled" (or "agentic") deployment-wide.
          TIER3_STRATEGY:
            (this.node.tryGetContext("tier3_strategy") as string) ??
            "lexical-baseline",
          // The graphrag strategy standard mode runs under lexical-baseline. Only
          // consulted when TIER3_STRATEGY == "lexical-baseline"; a per-request
          // options.retrieverStrategy still overrides it.
          LEXICAL_RETRIEVER_STRATEGY:
            (this.node.tryGetContext("lexical_retriever_strategy") as string) ??
            "topic_beam",
          // Agentic Tier-3 budgets. The 30s code default squeezes later tools below
          // their runtime (graphrag strategy calls take 10-40s); much above this the
          // AgentCore endpoint returns an empty envelope. MUST stay under
          // RESOLVE_TIMEOUT_S below, which is itself under the ~180s AgentCore
          // ceiling. Note the synthesis floor (retriever.py _MIN_SYNTHESIS_TIMEOUT_S)
          // is 60s, so worst case is budget + 60, not budget + reserve.
          AGENTIC_TIME_BUDGET_S:
            (this.node.tryGetContext("agentic_time_budget_s") as string) ?? "110",
          AGENTIC_PER_TOOL_TIMEOUT_S:
            (this.node.tryGetContext("agentic_per_tool_timeout_s") as string) ?? "45",
          AGENTIC_SYNTHESIS_RESERVE_S:
            (this.node.tryGetContext("agentic_synthesis_reserve_s") as string) ?? "25",
          // Hard per-request cap inside serve (main.py RESOLVE_TIMEOUT_S, clamped
          // 10..300). Must exceed AGENTIC_TIME_BUDGET_S + SYNTHESIS_RESERVE_S or serve
          // aborts a session the agentic budget still considers live.
          RESOLVE_TIMEOUT_S:
            (this.node.tryGetContext("resolve_timeout_s") as string) ?? "170",
          ALLOW_NO_GUARDRAIL: this.envName !== "prod" ? "true" : "false",
          GRAPH_URI_TEMPLATE:
            (this.node.tryGetContext("graph_uri_template") as string) ??
            `https://ontology-workbench.local/{namespace}`,
          DATA_SOURCES_TABLE: dataSourcesTableName,
          NAMESPACES_TABLE: namespacesTableName,
          // Serve-path Cedar: DDB role-policy loading (control-plane parity).
          ROLES_TABLE_NAME: props.rolesTable.tableName,
          // Role resolution: maps principal (user/group) to globalRoles/resourceRoles.
          RRM_TABLE_NAME: props.resourceRoleMappingsTable.tableName,
          // IdP group claim name — optional (same pattern as api-stack); the
          // runtime defaults to "groups" when unset.
          ...(props.groupClaimName && {
            GROUP_CLAIM_NAME: props.groupClaimName,
          }),
          MEMORY_ID: memory.memoryId,
          SESSION_METADATA_TABLE: sessionMetadata.table.tableName,
          SCL_CEDAR_FAIL_OPEN_NO_ROLES: (() => {
            const optIn =
              this.node.tryGetContext("cedar_fail_open_no_roles") === "true";
            if (optIn && this.envName === "prod") {
              throw new Error(
                "SCL_CEDAR_FAIL_OPEN_NO_ROLES cannot be enabled in prod",
              );
            }
            return optIn ? "true" : "false";
          })(),
          ...(props.bedrockLlmModelId && {
            BEDROCK_MODEL_ID: props.bedrockLlmModelId,
          }),
          // Query embedding + graphrag lexical retriever MUST use the same model
          // doc-kg-build ingested with. Single source of truth in ts-shared.
          BEDROCK_EMBED_MODEL_ID: DEFAULT_BEDROCK_MODEL_ID,
          ...(props.allowedOverrideModels?.length && {
            ALLOWED_OVERRIDE_MODELS: props.allowedOverrideModels.join(","),
          }),
        },
        description: "Context Manager orchestration service",
      });

      // Grant runtime access to Memory (short-term read/write for session history)
      memory.grantWrite(runtime);
      memory.grantRead(runtime);

      // ── AgentCore public endpoint for SSE streaming ──────────────────
      // Write the runtime ARN to the config. The frontend constructs the
      // full InvokeAgentRuntime URL at runtime using the ARN + region.
      this.queryEndpoint = runtime.agentRuntimeArn;

      // ── Add service-specific IAM policies to the L2's auto-created role ──

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

      // SSM read access
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["ssm:GetParameter"],
          resources: [
            `arn:aws:ssm:${region}:${account}:parameter${ssmPrefix}/*`,
          ],
        }),
      );

      // Bedrock — invoke/converse for LLM + embeddings + guardrails
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
            "bedrock:Converse",
            "bedrock:ConverseStream",
            "bedrock:ApplyGuardrail",
          ],
          resources: [
            `arn:aws:bedrock:*:${account}:inference-profile/*`,
            `arn:aws:bedrock:*::foundation-model/*`,
            `arn:aws:bedrock:${region}:${account}:guardrail/*`,
          ],
        }),
      );

      // Neptune — graph store (read-only)
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["neptune-db:ReadDataViaQuery"],
          resources: [props.neptuneClusterArn],
        }),
      );

      // DynamoDB — data sources registry (read-only)
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "dynamodb:GetItem",
            "dynamodb:Query",
            "dynamodb:BatchGetItem",
          ],
          resources: [
            dataSourcesTableArn,
            dataSourcesTableArn + "/index/*",
            namespacesTableArn,
            // Roles table (Cedar role policies) — read-only.
            props.rolesTable.tableArn,
            // RRM table (role resolution via PrincipalIndex GSI) — read-only.
            props.resourceRoleMappingsTable.tableArn,
            props.resourceRoleMappingsTable.tableArn + "/index/*",
          ],
        }),
      );

      // DynamoDB — session metadata (read-write for session lifecycle)
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "dynamodb:PutItem",
            "dynamodb:GetItem",
            "dynamodb:Query",
            "dynamodb:DeleteItem",
            "dynamodb:UpdateItem",
          ],
          resources: [
            sessionMetadataTableArn,
            sessionMetadataTableArn + "/index/*",
          ],
        }),
      );

      // S3 — ontology bucket + Athena results + data buckets
      const athenaResultsBucket =
        props.athenaResultsBucketName ?? this.prefixed("athena-results");
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "s3:GetObject",
            "s3:ListBucket",
            "s3:PutObject",
            "s3:GetBucketLocation",
            // Required for Athena to write query results reliably
            "s3:AbortMultipartUpload",
            "s3:ListMultipartUploadParts",
            "s3:DeleteObject",
          ],
          resources: [
            props.ontologyBucketArn,
            props.ontologyBucketArn + "/*",
            `arn:aws:s3:::${athenaResultsBucket}`,
            `arn:aws:s3:::${athenaResultsBucket}/*`,
            `arn:aws:s3:::${this.prefixed("demo-data")}`,
            `arn:aws:s3:::${this.prefixed("demo-data")}/*`,
          ],
        }),
      );

      // S3 — data source buckets that Athena reads from (Glue tables backed by S3).
      // Lake Formation GetDataAccess handles LF-governed tables, but non-LF tables
      // require direct IAM S3 access. Scoped to prefix-matching buckets.
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"],
          resources: [`arn:aws:s3:::${prefix}-*`, `arn:aws:s3:::${prefix}-*/*`],
        }),
      );

      // Athena — query execution + federated catalog resolution
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "athena:StartQueryExecution",
            "athena:GetQueryExecution",
            "athena:GetQueryResults",
            "athena:GetWorkGroup",
            "athena:GetDataCatalog",
          ],
          resources: [
            `arn:aws:athena:${region}:${account}:workgroup/*`,
            `arn:aws:athena:${region}:${account}:datacatalog/*`,
          ],
        }),
      );

      // Federated (nested) Glue catalogs are named `{sanitizedPrefix}ds_*` by the
      // federation provisioner; their database/table ARNs carry the catalog segment.
      const fedCatalogPrefix =
        this.prefixed("")
          .toLowerCase()
          .replace(/[^a-z0-9]/g, "") + "ds_";

      // Glue — read table/database/catalog metadata for Athena queries.
      // Nested Glue federated catalogs require actions on arn:...:catalog/*
      // in addition to standard database/table resources.
      // GetUnfiltered* required when Lake Formation enforces fine-grained access.
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "glue:GetCatalog",
            "glue:GetDatabase",
            "glue:GetTable",
            "glue:GetTables",
            "glue:GetPartitions",
            "glue:GetConnection",
            "glue:GetUnfilteredTableMetadata",
            "glue:GetUnfilteredPartitionMetadata",
          ],
          resources: [
            `arn:aws:glue:${region}:${account}:catalog`,
            `arn:aws:glue:${region}:${account}:catalog/*`,
            `arn:aws:glue:${region}:${account}:database/*`,
            `arn:aws:glue:${region}:${account}:table/*/*`,
            `arn:aws:glue:${region}:${account}:connection/${fedCatalogPrefix}*`,
            `arn:aws:glue:${region}:${account}:catalog/${fedCatalogPrefix}*`,
            `arn:aws:glue:${region}:${account}:database/${fedCatalogPrefix}*/*`,
            `arn:aws:glue:${region}:${account}:table/${fedCatalogPrefix}*/*/*`,
          ],
        }),
      );

      // Lake Formation — credential vending for LF-governed (federated) tables.
      // Data permissions (SELECT/DESCRIBE) are granted to this role at federation
      // provision time; this allows the query engine to obtain access.
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["lakeformation:GetDataAccess"],
          resources: ["*"],
        }),
      );

      // S3 — Athena federated connector spill bucket. The managed connector
      // writes spill data here; the query engine reads it to assemble results.
      const athenaSpillBucket = this.prefixed(`athena-spill-${this.account}`);
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "s3:GetObject",
            "s3:PutObject",
            "s3:ListBucket",
            "s3:GetBucketLocation",
            "s3:AbortMultipartUpload",
            "s3:ListMultipartUploadParts",
            "s3:DeleteObject",
          ],
          resources: [
            `arn:aws:s3:::${athenaSpillBucket}`,
            `arn:aws:s3:::${athenaSpillBucket}/*`,
          ],
        }),
      );

      // Lake Formation — data access vending for federated catalog queries.
      // lakeformation:GetDataAccess does not support resource-level restrictions.
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["lakeformation:GetDataAccess"],
          resources: ["*"],
        }),
      );

      // Secrets Manager — access secrets for this deployment prefix (e.g. memory API key).
      // JDBC source credentials are granted dynamically via secret resource policies
      // at source registration time (federation handler).
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["secretsmanager:GetSecretValue"],
          resources: [
            `arn:aws:secretsmanager:${region}:${account}:secret:${prefix}-*`,
          ],
        }),
      );

      // Grant AgentCore → Lambda proxy invoke
      // TODO(aoss-proxy): Remove when AgentCore supports AOSS directly
      aossProxyFn.grantInvoke(runtime.grantPrincipal);

      // ── Direct AOSS access for the graphrag lexical-baseline retriever ──
      // The hand-rolled vector_search path reaches AOSS through the proxy Lambda
      // above, but the graphrag retrievers (the `traversal` and
      // `topic-beam-chunk_only` strategies selected via options.retrieverStrategy)
      // open their own SigV4 connection straight to the AOSS collection. That
      // requires BOTH an IAM grant (aoss:APIAccessAll) and an AOSS data-access
      // policy naming the runtime role as a principal — without the data-access
      // policy the collection returns AuthorizationException(403). The network
      // path already exists (AgentCore SG → AOSS VPC endpoint, see AossVpceIngress).
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["aoss:APIAccessAll"],
          resources: [opensearchCollectionArn],
        }),
      );

      new opensearchserverless.CfnAccessPolicy(this, "OSSServeDataAccess", {
        name: this.prefixed("serve-read-only"),
        type: "data",
        // Keep in sync with the deployed policy — AOSS rejects a description-only
        // removal on update (see OSSProxyDataAccess note above).
        description: "Read-only data access for AgentCore runtime task",
        policy: JSON.stringify([
          {
            Rules: [
              {
                ResourceType: "index",
                Resource: [`index/${opensearchCollectionName}/*`],
                Permission: ["aoss:DescribeIndex", "aoss:ReadDocument"],
              },
              {
                ResourceType: "collection",
                Resource: [`collection/${opensearchCollectionName}`],
                Permission: ["aoss:DescribeCollectionItems"],
              },
            ],
            Principal: [runtime.role!.roleArn],
          },
        ]),
      });

      // ── SSM Parameter for auto-discovery ─────────────────────────────
      new ssm.StringParameter(this, "AgentRuntimeArnParam", {
        parameterName: `${ssmPrefix}/serve/runtime-arn`,
        stringValue: runtime.agentRuntimeArn,
        description: "AgentCore Runtime ARN for the Context Manager",
      });
      // The runtime's IAM role ARN — the consumer query principal that the
      // federation provisioner grants Lake Formation SELECT to.
      new ssm.StringParameter(this, "AgentRuntimeRoleArnParam", {
        parameterName: `${ssmPrefix}/serve/runtime-role-arn`,
        stringValue: runtime.role!.roleArn,
        description:
          "AgentCore Runtime IAM role ARN (consumer query principal)",
      });

      // ── Outputs ──────────────────────────────────────────────────────
      new cdk.CfnOutput(this, "AgentRuntimeArn", {
        value: runtime.agentRuntimeArn,
        description: "AgentCore Runtime ARN",
      });

      new cdk.CfnOutput(this, "AgentRuntimeId", {
        value: runtime.agentRuntimeId,
        description: "AgentCore Runtime ID",
      });
    }

    new cdk.CfnOutput(this, "AgentRuntimeName", {
      value: runtimeName,
      description: "AgentCore Runtime name for the Context Manager",
    });
  }
}
