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
import { resolveContext, namespaceTagKey } from "../../context";
import { agentCoreSupportedSubnets } from "../../utils/agentcore-az";
import { brandEnv } from "../../utils/brand-env";
import { parseEcrImageUri } from "../../utils/ecr-utils";
import { bundlePython } from "../../utils/python-bundling";
import { fromRoot, Paths } from "../../paths";
import { DynamoDBTable, SCLStack, SclMonitoring } from "../../constructs";
import {
  CONNECTOR_SPILL_KEY_GLOB,
  CONNECTOR_SPILL_KMS_TAG_KEY,
  CONNECTOR_SPILL_KMS_TAG_VALUE,
  CONNECTOR_TAG_KEY,
  CONNECTOR_TAG_VALUE,
  DEFAULT_BEDROCK_LLM_MODEL_ID,
  DEFAULT_BEDROCK_MODEL_ID,
  DEFAULT_GRAPH_URI_BASE,
} from "../../constants";
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
  /**
   * AgentCore-supported AZ **names** for the deploying account (resolved
   * in the app entrypoint). Used to defensively filter the subnets handed
   * to AgentCore Runtime. Omit to pass all subnets through.
   */
  readonly agentCoreAzNames?: string[];

  /** Bedrock LLM model ID for query resolution (NL-to-SPARQL, synthesis).
   *  Defaults to DEFAULT_BEDROCK_LLM_MODEL_ID when omitted. */
  readonly bedrockLlmModelId?: string;

  /** Bedrock embedding model ID for query embedding + the graphrag lexical
   *  retriever. MUST match what doc-kg-build ingested with; resolved from the
   *  SSM deploy config so a non-US deploy can set a region-appropriate model
   *  (#94). Falls back to the shared default when omitted. */
  readonly bedrockEmbedModelId?: string;

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

    // Removed knob: throwing beats ignoring, which would be the same silent
    // misconfiguration moved to synth time.
    if (this.node.tryGetContext("graph_uri_template") !== undefined) {
      throw new Error(
        "The `graph_uri_template` CDK context parameter was removed: it moved " +
          "serve (the graph reader) without the writers, so the prefix matched " +
          "no named graphs. Change DEFAULT_GRAPH_URI_BASE in " +
          "infra/lib/constants.ts instead — it feeds serve and metric-service " +
          "both, and ontology-engine's fallback must match it.",
      );
    }

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

    // IdP issuer/client IDs via SSM (avoids a cross-stack export on the auth
    // stack's UserPool/UserPoolClient/McpClient — see idp-authentication-stack.ts
    // for the writer). The MCP client ID is included in the allowed audience so
    // ID tokens minted for the MCP/CLI client are also accepted.
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
            `${issuerUrl}/.well-known/openid-configuration`,
            // allowedClients is intentionally omitted. AgentCore verifies ALL
            // configured fields (AND semantics), and `client_id` is an
            // access-token-only claim — setting it would reject ID tokens.
            // We validate the standard OIDC `aud` claim instead, which is
            // IdP-agnostic and present on ID tokens from any compliant IdP.
            // The web-app sends the ID token (carries email + groups) on both
            // the WebSocket and REST (via data-layer) paths.
            undefined,
            // allowedAudience: accept ID tokens from web app AND MCP proxy
            [clientId, mcpClientId], // allowedAudience — ID token `aud` == app client id
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
          // Redshift Data API `Database` used for awsdatacatalog queries.
          // Redshift Serverless namespaces default their DB to "dev"; override
          // via the redshift_serve_database context if a namespace differs.
          REDSHIFT_SERVE_DATABASE:
            (this.node.tryGetContext("redshift_serve_database") as string) ??
            "dev",
          OSS_ONTOLOGY_INDEX: opensearchCollectionName,
          // Default engine for a request that does not set `options.mode`. Standard
          // (not deep reasoning), because deep reasoning runs ~90s at p50 and the
          // REST/MCP callers time out at 15-30s. This does NOT gate construction —
          // the deep-reasoning retriever is always built, so an explicit
          // `options.mode: "deep-reasoning"` still engages the loop.
          //
          // Within standard, the engine is "lexical-baseline" running topic_beam
          // (LEXICAL_RETRIEVER_STRATEGY below) rather than the older "hand-rolled"
          // VectorRetriever + GraphTraverser path. topic_beam is the strongest
          // single-shot strategy on the SEC-10-Q benchmark (45.13% strict vs 34.36%
          // hand-rolled, 195 questions), so hand-rolled was leaving ~11pp on the
          // table for every caller that did not know to pass
          // options.retrieverStrategy. Override with TIER3_STRATEGY context to get
          // back to "hand-rolled" (or "deep-reasoning") deployment-wide.
          TIER3_STRATEGY:
            (this.node.tryGetContext("tier3_strategy") as string) ??
            "lexical-baseline",
          // The graphrag strategy standard mode runs under lexical-baseline. Only
          // consulted when TIER3_STRATEGY == "lexical-baseline"; a per-request
          // options.retrieverStrategy still overrides it.
          LEXICAL_RETRIEVER_STRATEGY:
            (this.node.tryGetContext("lexical_retriever_strategy") as string) ??
            "topic_beam",
          // Benchmark-only: `-c serve_guardrails_disabled=true` makes serve ignore
          // BOTH Bedrock guardrail ids from SSM. Needed because the primary
          // guardrail anonymizes PII on input, and a text-to-SQL question's
          // literals are the query — a question naming a person or place reaches
          // the model as "{NAME}" and the generated WHERE clause matches nothing,
          // worth ~1pp of execution accuracy on every Tier-2 BIRD run (1.6-3.2% of
          // questions hit, 98% of those scored wrong). Absent unless passed, so a
          // normal deployment sets no variable and is guarded; when it IS passed,
          // serve logs `guardrails_disabled_by_configuration` at ERROR on every
          // cold start. Refused outright in prod, like
          // SCL_CEDAR_FAIL_OPEN_NO_ROLES below: the primary guardrail is the
          // prompt-attack boundary, so this is a lab switch and synth is the last
          // place it can be stopped by review rather than by an alarm.
          //
          // Note the interaction with ALLOW_NO_GUARDRAIL below: with no guardrail
          // id, the Tier-3 Synthesizer refuses to construct in a non-local
          // environment unless that bypass is also true (it is, for every env but
          // prod). A Tier-2 benchmark never reaches Tier-3, so this matters only if
          // you point document questions at the same stack.
          ...(this.node.tryGetContext("serve_guardrails_disabled") !==
            undefined && {
            SERVE_GUARDRAILS_DISABLED: (() => {
              const raw = String(
                this.node.tryGetContext("serve_guardrails_disabled"),
              );
              // Same truthy set serve's config._guardrails_disabled() accepts, so
              // the guard cannot be sidestepped with a spelling it honours.
              const optIn = ["1", "true", "on", "yes"].includes(
                raw.trim().toLowerCase(),
              );
              if (optIn && this.envName === "prod") {
                throw new Error(
                  "SERVE_GUARDRAILS_DISABLED cannot be enabled in prod",
                );
              }
              return raw;
            })(),
          }),
          // Tier-2 flat NL→SQL: append the tables one induced FK hop out from the
          // retrieved ones. On in the code default, so this key exists to turn it
          // OFF (`-c serve_nl2sql_graph_expand=false`) or to pin it explicitly —
          // an operator should not need an image rebuild to disable a retrieval
          // lever that misbehaves on their schema. Left absent, the runtime's own
          // default applies and the variable is not set at all, so the code stays
          // the single source of truth for what "default" means.
          ...(this.node.tryGetContext("serve_nl2sql_graph_expand") !==
            undefined && {
            SERVE_NL2SQL_GRAPH_EXPAND: String(
              this.node.tryGetContext("serve_nl2sql_graph_expand"),
            ),
          }),
          // Companion budget: how many walked tables may be appended (default 8 in
          // code — the value the walk was benchmarked at). Raise when the missing
          // join table is plausibly further down the FK ordering.
          ...(this.node.tryGetContext(
            "serve_nl2sql_graph_expand_max_tables",
          ) !== undefined && {
            SERVE_NL2SQL_GRAPH_EXPAND_MAX_TABLES: String(
              this.node.tryGetContext("serve_nl2sql_graph_expand_max_tables"),
            ),
          }),
          // Deep-reasoning Tier-3 budgets. The 30s code default squeezes later tools
          // below their runtime (graphrag strategy calls take 10-40s); much above this
          // the AgentCore endpoint returns an empty envelope. MUST stay under
          // RESOLVE_TIMEOUT_S below, which is itself under the ~180s AgentCore
          // ceiling. Note the synthesis floor (retriever.py _MIN_SYNTHESIS_TIMEOUT_S)
          // is 60s, so worst case is budget + 60, not budget + reserve.
          DEEP_REASONING_TIME_BUDGET_S:
            (this.node.tryGetContext(
              "deep_reasoning_time_budget_s",
            ) as string) ?? "110",
          DEEP_REASONING_PER_TOOL_TIMEOUT_S:
            (this.node.tryGetContext(
              "deep_reasoning_per_tool_timeout_s",
            ) as string) ?? "45",
          DEEP_REASONING_SYNTHESIS_RESERVE_S:
            (this.node.tryGetContext(
              "deep_reasoning_synthesis_reserve_s",
            ) as string) ?? "25",
          // Hard per-request cap inside serve (main.py RESOLVE_TIMEOUT_S, clamped
          // 10..300). Must exceed DEEP_REASONING_TIME_BUDGET_S + SYNTHESIS_RESERVE_S
          // or serve aborts a session the deep-reasoning budget still considers live.
          RESOLVE_TIMEOUT_S:
            (this.node.tryGetContext("resolve_timeout_s") as string) ?? "170",
          // Tier-1 curated metrics execute synchronously. Keep their SQL budget
          // explicit and operator-configurable instead of inheriting the
          // CompositeQueryExecutor's 10-second method default.
          TIER1_METRIC_TIMEOUT_S:
            (this.node.tryGetContext("tier1_metric_timeout_s") as string) ??
            "35",
          ALLOW_NO_GUARDRAIL: this.envName !== "prod" ? "true" : "false",
          // Same base as the writers (metric-service NDB_GRAPH_URI_BASE,
          // ontology-engine neptune_db_graph). Serve reads these graphs, so a
          // reader-only override matches nothing — hence the guard above.
          GRAPH_URI_TEMPLATE: `${DEFAULT_GRAPH_URI_BASE}/{namespace}`,
          DATA_SOURCES_TABLE: dataSourcesTableName,
          NAMESPACES_TABLE: namespacesTableName,
          // Serve-path Cedar: DDB role-policy loading (control-plane parity).
          ROLES_TABLE_NAME: props.rolesTable.tableName,
          // Role resolution: maps principal (user/group) to globalRoles/resourceRoles.
          RRM_TABLE_NAME: props.resourceRoleMappingsTable.tableName,
          // IdP group claim name, resolved via SSM above.
          GROUP_CLAIM_NAME: groupClaimName,
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
          // Always emitted so the effective query model is visible in the template.
          BEDROCK_MODEL_ID:
            props.bedrockLlmModelId ?? DEFAULT_BEDROCK_LLM_MODEL_ID,
          // Query embedding + graphrag lexical retriever MUST use the same model
          // doc-kg-build ingested with. Config-resolved (#94) so a non-US deploy
          // can set a region-appropriate model; shared ts-shared constant is the
          // fallback, keeping producers and consumers on one value.
          BEDROCK_EMBED_MODEL_ID:
            props.bedrockEmbedModelId ?? DEFAULT_BEDROCK_MODEL_ID,
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

      // Redshift Data API — execute Glue/Iceberg queries via Redshift
      // Serverless (`awsdatacatalog` auto-mount) as an alternative to Athena for
      // Glue sources that opt in (GlueConfiguration.executionEngine=REDSHIFT).
      // The workgroup itself is provisioned/owned by the customer and named at
      // onboarding; this grants the serve runtime the Data API + Serverless
      // credential-vend actions to run statements against those workgroups. The
      // Glue/Lake Formation/S3 grants below are shared with the Athena path and
      // already cover the auto-mount's catalog + underlying-data reads.
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "redshift-data:ExecuteStatement",
            "redshift-data:DescribeStatement",
            "redshift-data:GetStatementResult",
            "redshift-data:ListStatements",
          ],
          // redshift-data actions do not support resource-level scoping to a
          // specific workgroup/statement (statement ids are created at call time
          // and owned by this principal); AWS models these as account-wide.
          resources: ["*"],
        }),
      );
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["redshift-serverless:GetCredentials"],
          resources: [
            `arn:aws:redshift-serverless:${region}:${account}:workgroup/*`,
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

      // ── Custom Athena federation connectors (CUSTOM_CONNECTOR sources) ──
      //
      // Invoke a customer-authored connector Lambda, and read what it spills.
      // Both resources live in the CUSTOMER's account and are unknown at deploy
      // time, so neither can be resource-enumerated. Containment is by condition
      // key plus the customer's own resource policies, which must independently
      // name this role — see the custom-connectors LLD §6.
      //
      // `aws:CalledVia` is populated on forward access sessions, so these Allows
      // match only while Athena is executing a statement for this role, never a
      // direct call from serve code. The key is multi-valued and its order
      // cannot be constrained, hence ForAnyValue: AWS documents "somewhere in
      // the chain" as the intended semantics. Evaluation fails CLOSED — an
      // absent key does not match — so an error here denies rather than widens.
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "AthenaFederationConnectorInvoke",
          actions: ["lambda:InvokeFunction"],
          // The account MUST stay a wildcard — the connector lives in the
          // customer's — so the ARN cannot scope this. A resource TAG does:
          // Lambda evaluates aws:ResourceTag natively for InvokeFunction, with no
          // per-resource opt-in, so an untagged function is simply unreachable.
          // Preferred over a name convention because a tag cannot be matched by
          // accident: a function that merely happens to be named a certain way
          // does not inherit the grant. Athena exposes no condition key naming the
          // catalog a forward-access-session invoke serves, so the tag is the
          // tightest mechanism available.
          //
          // Region-pinned by choice, not by necessity: Athena CAN invoke a
          // connector in another region when given its full ARN, but we do not
          // support that topology, and the control-plane rejects a connector ARN
          // outside this region at source-create.
          resources: [`arn:aws:lambda:${region}:*:function:*`],
          conditions: {
            "ForAnyValue:StringEquals": {
              "aws:CalledVia": "athena.amazonaws.com",
            },
            StringEquals: {
              [`aws:ResourceTag/${CONNECTOR_TAG_KEY}`]: CONNECTOR_TAG_VALUE,
            },
          },
        }),
      );
      // The escalation this Allow would otherwise open, closed explicitly.
      //
      // `aws:CalledVia` is satisfied by an Athena UDF
      // (`USING EXTERNAL FUNCTION ... LAMBDA '<arn>'`), which needs nothing but
      // StartQueryExecution — already held above — plus lambda:InvokeFunction. A
      // same-account invoke also needs no resource policy, so without this Deny
      // the Allow reaches every in-region Lambda in THIS account, including the
      // federation provisioner that holds Lake Formation admin.
      //
      // A same-account Deny rather than an `aws:ResourceAccount` exclusion on the
      // Allow, because excluding the account would also rule out a connector
      // deployed alongside this stack — which is how the reference connector and
      // its integration test are deployed. Spill works for such a connector too:
      // the spill read below spans every account including this one, bounded by
      // the key prefix rather than by an account exclusion.
      //
      // Conditioned on `aws:CalledVia` so it cannot touch the direct invokes this
      // role makes legitimately (the AOSS proxy Lambda, granted an unconditioned
      // grantInvoke); Athena has no reason to invoke one of our own functions.
      //
      // NOT scoped to our name prefix. It was, and that made a naming convention
      // load-bearing for security while silently refusing any connector deployed
      // into this account under the prefix — which is what the reference
      // connector's own deploy script does. The tag exemption below expresses the
      // real intent directly, so the prefix is gone and the statement is now
      // account-wide and region-wide. Breadth is the safe direction for a Deny,
      // and region breadth guards a future region-widened Allow.
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "DenyAthenaInvokeOfUntaggedFunctions",
          effect: iam.Effect.DENY,
          actions: ["lambda:InvokeFunction"],
          resources: [`arn:aws:lambda:*:${account}:function:*`],
          conditions: {
            "ForAnyValue:StringEquals": {
              "aws:CalledVia": "athena.amazonaws.com",
            },
            // StringNotEquals matches an ABSENT key, so an untagged function stays
            // denied — fail-closed, the direction a Deny needs. Same constants as
            // the Allow above, so the two agree by construction.
            //
            // This does not make the Deny a no-op against the Allow. The Allow is
            // what the escalation borrows: an Athena UDF
            // (`USING EXTERNAL FUNCTION ... LAMBDA '<arn>'`) needs only
            // StartQueryExecution — held for every user question — plus
            // lambda:InvokeFunction, and serve's SQL is LLM-generated, so the
            // vector is plausibly reachable by prompt injection. What this Deny
            // still catches is that UDF pointed at any same-account function
            // lacking the tag, the AOSS proxy included.
            //
            // The residual is one of our own functions ACQUIRING the tag — CDK's
            // `Tags.of(scope)` propagates to every taggable child, so this is the
            // realistic path. It is closed at build time instead of with a second
            // runtime tag: infra/test/app-connector-tag.test.ts asserts that no
            // synthesised resource in this app carries it. A second exemption tag
            // would only move the same propagation risk onto the second tag.
            StringNotEquals: {
              [`aws:ResourceTag/${CONNECTOR_TAG_KEY}`]: CONNECTOR_TAG_VALUE,
            },
          },
        }),
      );
      // Spill reads. Above 6 MB a connector's response is written to ITS OWN
      // spill bucket and Athena — acting for this role — fetches it to assemble
      // results. Spill is automatic and connector-side; the only choice here is
      // whether Orion can read the result, and withholding it fails ordinary
      // queries rather than exotic ones (a connector that advertises no limit
      // pushdown makes Athena request the whole table and apply the LIMIT
      // itself, so even a trivial SELECT ... LIMIT 1000 can spill).
      //
      // The bucket cannot be pinned — the customer owns it — so the KEY PREFIX is
      // what bounds this grant. Every connector is required to spill under
      // CONNECTOR_SPILL_KEY_GLOB (`connectors/{connectorId}/spills/...`), which is
      // what lets the statement span every account INCLUDING this one without
      // becoming a general S3 read: a bucket in our account is reachable only at
      // that path, which nothing else of ours writes to.
      //
      // Spanning our own account is deliberate. It is the one topology the earlier
      // `aws:ResourceAccount` exclusion could not serve — a connector deployed
      // alongside Orion could never spill — and excluding an account bought nothing
      // that a key prefix does not, because cross-account S3 already requires the
      // bucket's own policy to name this role.
      //
      // `aws:RequestedRegion` is kept, since S3 ARNs carry no region.
      //
      // Only the serve role gets this: spill is a record-path mechanism, and
      // discovery's entire Athena surface (SHOW/DESCRIBE) is metadata traffic that
      // never spills.
      //
      // Getting the prefix wrong fails in the worst way available: every small
      // result set works, and the first query to cross 6 MB returns AccessDenied.
      // Hence the onboarding guide states the required value rather than leaving it
      // to the connector's `spill_prefix` default.
      //
      // Note `*` spans `/` inside an S3 relative id, so this is not anchored at the
      // start of the key — it matches the segments appearing anywhere in it. It
      // still turns an account-wide read primitive into a spill-shaped one, which is
      // the point, but it is not a containment boundary on its own.
      //
      // The bucket-level half of this grant lives in the two statements below.
      // Cross-account S3 authorizes on both sides, and a spill read fails without
      // them even when the customer's bucket policy grants all three actions:
      // simulating the role showed GetObject `allowed` while ListBucket and
      // GetBucketLocation were `implicitDeny`.
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "AthenaFederationSpillRead",
          actions: ["s3:GetObject"],
          resources: [`arn:aws:s3:::*/${CONNECTOR_SPILL_KEY_GLOB}`],
          conditions: {
            "ForAnyValue:StringEquals": {
              "aws:CalledVia": "athena.amazonaws.com",
            },
            StringEquals: { "aws:RequestedRegion": region },
          },
        }),
      );
      // Bucket-level companions to the read above. Both are separate statements
      // rather than extra actions on it, for two independent reasons:
      //
      //  * The resource differs. GetObject takes an object ARN, these take the
      //    bucket ARN, so the spill-prefix path pattern cannot scope them.
      //  * `s3:prefix` applies to ListBucket only. Folded into one statement it
      //    would also gate GetBucketLocation, where the key is absent — and
      //    `StringLike` against an absent key is false, denying the very call
      //    this exists to allow.
      //
      // ListBucket carries the prefix condition because on a wildcard bucket it is
      // otherwise an enumeration primitive. Scoped this way it can only list under a
      // connector's spill prefix, via Athena — and for a bucket in another account
      // the bucket's own policy must still name this role.
      //
      // GetBucketLocation cannot be scoped at all: it takes a bucket ARN, has no
      // `s3:prefix`, and now spans this account too. It reveals only a bucket's
      // region, which is the least sensitive thing S3 will answer, and Athena calls
      // it to resolve the endpoint before reading a spilled object.
      //
      // Residual risk, stated because it is the likely next failure: if Athena ever
      // lists without supplying a prefix, `s3:prefix` is absent and this denies it.
      // The symptom would be an identical 403 with GetObject and GetBucketLocation
      // both simulating `allowed`.
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "AthenaFederationSpillBucketLocation",
          actions: ["s3:GetBucketLocation"],
          resources: ["arn:aws:s3:::*"],
          conditions: {
            "ForAnyValue:StringEquals": {
              "aws:CalledVia": "athena.amazonaws.com",
            },
            StringEquals: { "aws:RequestedRegion": region },
          },
        }),
      );
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "AthenaFederationSpillList",
          actions: ["s3:ListBucket"],
          resources: ["arn:aws:s3:::*"],
          conditions: {
            "ForAnyValue:StringEquals": {
              "aws:CalledVia": "athena.amazonaws.com",
            },
            StringEquals: { "aws:RequestedRegion": region },
            StringLike: { "s3:prefix": CONNECTOR_SPILL_KEY_GLOB },
          },
        }),
      );
      // Decrypt for a spill bucket under SSE-KMS. Note this is NOT the
      // connector's own `kms_key_id` spill encryption: there the SDK's
      // KmsKeyFactory calls GenerateDataKey and ships the PLAINTEXT key to
      // Athena on the Split, so the reader never calls KMS at all. Bucket-level
      // SSE-KMS is the case that needs a grant, and there S3 — not Athena — is
      // the immediate KMS caller, so the condition is `kms:ViaService`.
      // `aws:CalledVia` is deliberately absent: conditions within a statement
      // are ANDed, and adding it would make the grant depend on whether S3
      // appends itself to the chain, which AWS does not document.
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "AthenaFederationSpillDecryptViaS3",
          actions: ["kms:Decrypt"],
          resources: [`arn:aws:kms:${region}:*:key/*`],
          conditions: {
            // Both keys live in ONE StringEquals object: a second `StringEquals`
            // property would overwrite the first in the object literal, silently
            // dropping kms:ViaService and widening this grant to any KMS caller.
            //
            // Scoped by a resource TAG on the key, not by excluding this account.
            // The spill key prefix is not expressible here — S3 Bucket Keys put the
            // BUCKET arn in kms:EncryptionContext, not the object's — so a tag is
            // the only per-resource handle KMS offers, and it is a better one than
            // an account exclusion in both directions: it stops this reaching
            // Orion's own keys, AND it stops it reaching arbitrary FOREIGN keys,
            // which an exclusion left wide open.
            //
            // This is why SSE-KMS on a connector's spill bucket is REQUIRED rather
            // than one shape among several. The SDK already encrypts spilled content
            // with its own ephemeral AES-GCM key, so bucket encryption is not what
            // protects the data — mandating it is what puts a kms:Decrypt check on
            // EVERY spilled read. Without the mandate the check is skipped for the
            // common case (SSE-S3, or no bucket encryption) and spill authorization
            // rests on the key prefix alone; with it, a bucket Athena was induced to
            // read from fails closed unless its key was deliberately tagged.
            //
            // Spans every account including this one, which is what lets a connector
            // deployed alongside Orion spill at all — the previous exclusion made
            // that impossible.
            //
            // Cannot regress the managed-JDBC path: Orion's own spill bucket is
            // S3_MANAGED, so it never calls KMS.
            StringEquals: {
              "kms:ViaService": `s3.${region}.amazonaws.com`,
              [`aws:ResourceTag/${CONNECTOR_SPILL_KMS_TAG_KEY}`]:
                CONNECTOR_SPILL_KMS_TAG_VALUE,
            },
          },
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

      // Secrets Manager — platform secrets for this deployment prefix (e.g. the
      // memory API key).
      //
      // JDBC source credentials are NOT granted here. They are granted per secret
      // by the federation handler, which writes a resource policy whose StringLike
      // condition matches this namespace as a whole entry in the secret's
      // `<prefix>:namespace` tag. Same-account access is satisfied by EITHER
      // policy, so an identity grant covering those secrets would make that
      // condition unenforceable: `{prefix}-*` matches the platform's own
      // `{prefix}-{env}-datasource-*` credential-secret naming, so the runtime
      // could read any namespace's credential secret directly and the tag
      // condition would never be consulted. Verified against a live account: with
      // this statement unconditioned, a secret tagged for a DIFFERENT namespace was
      // readable.
      //
      // The two mechanisms are kept disjoint by requiring the tag to be ABSENT
      // here: platform secrets are untagged and stay readable by identity, while
      // every namespace-bound credential secret is reachable only through its
      // tag-conditioned resource policy.
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "ReadPlatformSecrets",
          actions: ["secretsmanager:GetSecretValue"],
          resources: [
            `arn:aws:secretsmanager:${region}:${account}:secret:${prefix}-*`,
          ],
          conditions: {
            Null: {
              [`secretsmanager:ResourceTag/${namespaceTagKey(this.node)}`]:
                "true",
            },
          },
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
