// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import { Construct } from "constructs";

import { SCLStack } from "../../constructs";
import {
  JdbcConnectivity,
  type PeeringConfig,
  type TransitGatewayConfig,
  type PrivateLinkConfig,
} from "../../constructs/jdbc-connectivity";

/**
 * Foundation stack: VPC, subnets, security groups, VPC endpoints, and
 * network plumbing for the SemanticContext platform.
 *
 * Supports importing an existing VPC by setting the `vpc_id` CDK context
 * key.  When omitted, a new VPC is created.
 *
 * Context keys (in addition to SCLStack defaults):
 *   - `vpc_id` - optional; if provided, looks up an existing VPC instead
 *     of creating one.
 *
 * Cross-network JDBC connectivity (created-VPC only; any combination):
 *   - VPC peering: `jdbc_peer_vpc_id`, `jdbc_peer_cidrs` (comma-separated),
 *     `jdbc_peer_owner_id` (cross-account), `jdbc_peer_region` (cross-region).
 *   - Transit Gateway: `jdbc_tgw_id`, `jdbc_tgw_cidrs` (comma-separated).
 *   - PrivateLink: `jdbc_privatelink_service`, `jdbc_privatelink_port`,
 *     `jdbc_privatelink_private_dns` ("true"/"false").
 */

export interface NetworkStackProps extends cdk.StackProps {
  /**
   * AgentCore-supported AZ **names** for the deploying account, resolved
   * at synth time by ``resolveAgentCoreAzNames`` in the CDK app
   * entrypoint. When provided, the VPC is pinned to exactly these AZs so
   * AgentCore Runtime lands only on supported physical zones. When
   * omitted (e.g. unit-test synth, or an imported VPC), the VPC falls
   * back to CDK's default ``maxAzs: 2``.
   */
  readonly agentCoreAzNames?: string[];
}

export class NetworkStack extends SCLStack {
  /** The platform VPC shared by all SemanticContext workloads. */
  public readonly vpc: ec2.IVpc;

  /** Security group for the Neptune graph-database cluster (port 8182). */
  public readonly neptuneSecurityGroup: ec2.SecurityGroup;

  /** Security group for ECS Fargate tasks. */
  public readonly ecsSecurityGroup: ec2.SecurityGroup;

  /** Security group for Lambda functions. */
  public readonly lambdaSecurityGroup: ec2.SecurityGroup;

  /** Port-80 egress used only by direct Snowflake discovery for OCSP. */
  public readonly discoveryOcspSecurityGroup: ec2.SecurityGroup;

  /** Security group for the AOSS VPC endpoint (port 443). */
  public readonly aossSecurityGroup: ec2.SecurityGroup;
  /** EC2 Interface VPC Endpoint ID for AOSS (NextGen *.on.aws private DNS). */
  public readonly aossVpcEndpointId: string;

  /** Shared Cloud Map private DNS namespace for service discovery. */
  public readonly serviceNamespace: servicediscovery.PrivateDnsNamespace;

  /** Security group for Glue Connections to reach source databases. */
  public readonly connectorSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props?: NetworkStackProps) {
    super(scope, id, props);
    this.addComponentTag("foundation");

    // ── VPC (create or import) ───────────────────────────────────────
    const existingVpcId = this.node.tryGetContext("vpc_id") as
      | string
      | undefined;

    if (existingVpcId) {
      this.vpc = ec2.Vpc.fromLookup(this, "ImportedVpc", {
        vpcId: existingVpcId,
      });
    } else {
      // Pin VPC AZs to AgentCore-supported physical zones. AZ *names*
      // (us-east-1a/b/…) map to different physical AZ *IDs* per account,
      // so a fixed ``maxAzs: 2`` lands on supported zones in one account
      // and unsupported ones in another (our 9923 testing account failed
      // with "subnets in unsupported availability zones"). The app
      // entrypoint resolves the supported names for the deploying account
      // via ``resolveAgentCoreAzNames`` and passes them in here. When not
      // provided (unit synth), fall back to the default 2-AZ spread.
      const azNames = props?.agentCoreAzNames;
      this.vpc = new ec2.Vpc(this, "Vpc", {
        vpcName: this.prefixed("vpc"),
        ...(azNames && azNames.length > 0
          ? { availabilityZones: azNames }
          : { maxAzs: 2 }),
        natGateways: 1,
        subnetConfiguration: [
          {
            name: "Public",
            subnetType: ec2.SubnetType.PUBLIC,
            cidrMask: 24,
          },
          {
            name: "Private",
            subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
            cidrMask: 24,
          },
        ],
      });
    }

    // ── VPC Endpoints ────────────────────────────────────────────────
    // Only created for new VPCs. Imported VPCs are expected to have
    // their own endpoints managed externally.
    if (!existingVpcId) {
      const createdVpc = this.vpc as ec2.Vpc;

      // Gateway endpoints (free - avoid NAT charges for S3/DDB traffic)
      createdVpc.addGatewayEndpoint("S3Endpoint", {
        service: ec2.GatewayVpcEndpointAwsService.S3,
      });
      createdVpc.addGatewayEndpoint("DynamoDbEndpoint", {
        service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
      });

      // Interface endpoints
      createdVpc.addInterfaceEndpoint("BedrockRuntimeEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("BedrockAgentCoreEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("CloudWatchLogsEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("EcrApiEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.ECR,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("EcrDkrEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("SsmEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.SSM,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("SecretsManagerEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
        privateDnsEnabled: true,
      });
      const aossEndpoint = createdVpc.addInterfaceEndpoint("AossEndpoint", {
        service: new ec2.InterfaceVpcEndpointService(
          `com.amazonaws.${cdk.Stack.of(this).region}.aoss-data`,
        ),
        privateDnsEnabled: true,
      });
      this.aossVpcEndpointId = aossEndpoint.vpcEndpointId;

      // Additional interface endpoints: keep AWS API traffic private so
      // egress can be scoped away from the public internet. These cover the
      // services the ECS tasks and Lambdas call that previously required NAT.
      const region = cdk.Stack.of(this).region;
      createdVpc.addInterfaceEndpoint("StsEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.STS,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("EventBridgeEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.EVENTBRIDGE,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("SqsEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.SQS,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("StepFunctionsEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.STEP_FUNCTIONS,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("AthenaEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.ATHENA,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("GlueEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.GLUE,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("LambdaEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.LAMBDA,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("EcsEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.ECS,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("DataZoneEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.DATAZONE,
        privateDnsEnabled: true,
      });
      // Neptune Analytics (neptune-graph) — control + data plane.
      createdVpc.addInterfaceEndpoint("NeptuneGraphEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.NEPTUNE_ANALYTICS,
        privateDnsEnabled: true,
      });
      createdVpc.addInterfaceEndpoint("NeptuneGraphDataEndpoint", {
        service: ec2.InterfaceVpcEndpointAwsService.NEPTUNE_ANALYTICS_DATA,
        privateDnsEnabled: true,
      });
      // OpenSearch Serverless control plane (create/delete collection, policies).
      // No CDK constant exists; the data-plane endpoint above is separate.
      createdVpc.addInterfaceEndpoint("AossControlPlaneEndpoint", {
        service: new ec2.InterfaceVpcEndpointService(
          `com.amazonaws.${region}.aoss`,
        ),
        privateDnsEnabled: true,
      });
    }

    // ── Security Groups ──────────────────────────────────────────────

    // Neptune - allow inbound 8182 from the ECS + Lambda SGs only (not the
    // whole VPC CIDR). Neptune is a database; it never initiates connections,
    // so outbound stays disabled. Ingress rules are added below, after the
    // ECS and Lambda SGs are constructed.
    this.neptuneSecurityGroup = new ec2.SecurityGroup(this, "NeptuneSG", {
      vpc: this.vpc,
      securityGroupName: this.prefixed("neptune-sg"),
      // Generic description: GroupDescription is immutable in CloudFormation,
      // so keep it rule-agnostic to avoid future SG replacements. Actual rules
      // are defined below and in the surrounding comments.
      description: "Neptune cluster security group",
      allowAllOutbound: false, // Neptune is a database; it doesn't initiate connections
    });

    // ECS - egress restricted to least privilege. Fargate tasks pull private
    // ECR images (via the ECR api/dkr interface endpoints + S3 gateway
    // endpoint) and call AWS service APIs on 443. 443 egress is intentionally
    // left open to 0.0.0.0/0 rather than scoped to the VPC CIDR: the
    // interface endpoints above cover the AWS APIs privately, but gateway
    // endpoints (S3/DynamoDB) route to AWS public IP ranges via prefix lists,
    // and some SDK calls may still fall back to public regional endpoints.
    // Fully scoping egress (443 -> VPC CIDR + S3/DDB prefix lists) is a planned
    // follow-up pending connectivity validation. All non-HTTPS, non-Neptune
    // outbound is denied.
    this.ecsSecurityGroup = new ec2.SecurityGroup(this, "EcsSG", {
      vpc: this.vpc,
      securityGroupName: this.prefixed("ecs-sg"),
      // Generic description (immutable in CFN — keep rule-agnostic).
      description: "ECS Fargate tasks security group",
      allowAllOutbound: false,
    });
    this.ecsSecurityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "HTTPS to AWS APIs (interface endpoints + S3/DDB gateway prefix lists)",
    );
    this.ecsSecurityGroup.addEgressRule(
      this.neptuneSecurityGroup,
      ec2.Port.tcp(8182),
      "Neptune Gremlin/SPARQL",
    );

    // Internet-bound connector traffic can be narrowed to fixed NAT/proxy or
    // source ranges. The same peers must govern Snowflake OCSP from both the
    // managed connector and the direct-discovery Lambda so the two execution
    // paths cannot drift.
    const connectorEgressCidrs = this.ctxList("connector_egress_cidrs");
    const connectorEgressPeers: ec2.IPeer[] = connectorEgressCidrs.length
      ? connectorEgressCidrs.map((cidr) => ec2.Peer.ipv4(cidr))
      : [ec2.Peer.anyIpv4()];
    const egressScope = connectorEgressCidrs.length
      ? connectorEgressCidrs.join(",")
      : "anywhere";
    const addConfiguredOcspEgress = (
      securityGroup: ec2.SecurityGroup,
      workload: string,
    ): void => {
      if (this.ctxString("connector_ocsp_egress") === "false") return;
      for (const peer of connectorEgressPeers) {
        securityGroup.addEgressRule(
          peer,
          ec2.Port.tcp(80),
          `Snowflake OCSP for ${workload} to ${egressScope}`,
        );
      }
    };

    // Lambda - egress restricted to least privilege. Same rationale as
    // the ECS SG: 443 for AWS service APIs, 8182 for Neptune clients
    // (metric-service, etc.). No broad outbound.
    this.lambdaSecurityGroup = new ec2.SecurityGroup(this, "LambdaSG", {
      vpc: this.vpc,
      securityGroupName: this.prefixed("lambda-sg"),
      // Generic description (immutable in CFN — keep rule-agnostic).
      description: "Lambda functions security group",
      allowAllOutbound: false,
    });
    this.lambdaSecurityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "HTTPS to AWS APIs (interface endpoints + S3/DDB gateway prefix lists)",
    );
    this.lambdaSecurityGroup.addEgressRule(
      this.neptuneSecurityGroup,
      ec2.Port.tcp(8182),
      "Neptune Gremlin/SPARQL",
    );
    this.lambdaSecurityGroup.addEgressRule(
      this.ecsSecurityGroup,
      ec2.Port.tcp(8001),
      "Ontology-engine API: api-proxy Lambda to ECS Cloud Map service",
    );
    // The discovery Lambda (sources-db-connector) connects DIRECTLY to source
    // databases via its driver dialects (see database/connectors/dialects.py),
    // not through Glue/Athena — so it needs egress to the supported DB ports.
    // Snowflake (443) is already covered above. Non-standard ports configured
    // per-source would need to be added here.
    for (const port of [5432, 3306, 1433, 5439, 1521]) {
      this.lambdaSecurityGroup.addEgressRule(
        ec2.Peer.anyIpv4(),
        ec2.Port.tcp(port),
        `Source database ${port} (direct JDBC discovery)`,
      );
    }
    // Keep HTTP egress off the shared Lambda SG: most platform functions never
    // call an OCSP responder. SourcesStack attaches this dedicated SG only to
    // sources-db-connector, which is the Lambda that loads the Snowflake driver.
    this.discoveryOcspSecurityGroup = new ec2.SecurityGroup(
      this,
      "DiscoveryOcspSG",
      {
        vpc: this.vpc,
        securityGroupName: this.prefixed("discovery-ocsp-sg"),
        description: "Snowflake discovery OCSP egress security group",
        allowAllOutbound: false,
      },
    );
    addConfiguredOcspEgress(
      this.discoveryOcspSecurityGroup,
      "direct discovery",
    );

    // Neptune ingress — scoped to the ECS + Lambda SGs (least privilege),
    // replacing the previous VPC-CIDR-wide rule.
    this.neptuneSecurityGroup.addIngressRule(
      this.ecsSecurityGroup,
      ec2.Port.tcp(8182),
      "Allow Neptune Gremlin/SPARQL from ECS tasks",
    );
    this.neptuneSecurityGroup.addIngressRule(
      this.lambdaSecurityGroup,
      ec2.Port.tcp(8182),
      "Allow Neptune Gremlin/SPARQL from Lambda functions",
    );

    // AOSS VPC endpoint — dedicated SG so any component that needs AOSS
    // can be granted access via an ingress rule here.
    this.aossSecurityGroup = new ec2.SecurityGroup(this, "AossSG", {
      vpc: this.vpc,
      securityGroupName: this.prefixed("aoss-vpce-sg"),
      description:
        "AOSS VPC endpoint - accept HTTPS from authorized components",
      allowAllOutbound: false,
    });
    this.aossSecurityGroup.addIngressRule(
      this.ecsSecurityGroup,
      ec2.Port.tcp(443),
      "Allow HTTPS from ECS tasks",
    );

    // ── Cloud Map Namespace ────────────────────────────────────────────
    this.serviceNamespace = new servicediscovery.PrivateDnsNamespace(
      this,
      "ServiceNamespace",
      {
        name: `${this.prefixed("services")}.local`,
        vpc: this.vpc,
      },
    );

    // Connector SG — used by Glue Connections to reach source databases.
    // Egress restricted to least privilege: the supported source-DB
    // ports plus 443 (Snowflake wire protocol + AWS APIs the connector calls,
    // e.g. Glue/S3/STS). Source DB security groups must allow inbound from this
    // SG. Cross-network JDBC (peering/TGW/PrivateLink) adds its own scoped
    // egress rules in the JdbcConnectivity construct.
    this.connectorSecurityGroup = new ec2.SecurityGroup(this, "ConnectorSG", {
      vpc: this.vpc,
      securityGroupName: this.prefixed("connector-sg"),
      description:
        "Glue Connection / Athena connector - outbound to source databases",
      allowAllOutbound: false,
    });
    // Glue managed connectors require self-referencing rules for inter-ENI
    // communication during connection validation and query execution. Under
    // allowAllOutbound the egress side was implicit; with egress locked down it
    // must be declared explicitly on both directions.
    this.connectorSecurityGroup.addIngressRule(
      this.connectorSecurityGroup,
      ec2.Port.allTcp(),
      "Glue connector self-reference (ingress)",
    );
    this.connectorSecurityGroup.addEgressRule(
      this.connectorSecurityGroup,
      ec2.Port.allTcp(),
      "Glue connector self-reference (egress)",
    );
    // Supported source-DB engines/ports (see database/connectors/dialects.py):
    // PostgreSQL 5432, MySQL 3306, SQL Server 1433, Redshift 5439, Oracle 1521.
    // Snowflake (443) and any AWS API the connector calls are covered by the
    // 443 rule below.
    //
    // DESTINATION SCOPE: a managed Glue connector reaching a customer database
    // or a SaaS warehouse has no enumerable destination set, so the default is
    // `0.0.0.0/0` for these ports. Where the destinations ARE known — egress via
    // a fixed NAT/proxy range, or source databases on known ranges — narrow all
    // of this connector's internet-bound egress with one knob:
    //   -c connector_egress_cidrs=10.20.0.0/16,192.0.2.10/32
    // Same shape as `jdbc_peer_cidrs` (see addJdbcConnectivity), and it applies
    // to the DB ports, OCSP 80 and 443 alike rather than special-casing one
    // port. Cross-network JDBC (peering/TGW/PrivateLink) is already scoped to
    // its configured CIDRs by the JdbcConnectivity construct.
    for (const peer of connectorEgressPeers) {
      for (const port of [5432, 3306, 1433, 5439, 1521]) {
        this.connectorSecurityGroup.addEgressRule(
          peer,
          ec2.Port.tcp(port),
          `Source database ${port}`,
        );
      }
    }
    // OCSP certificate-revocation checks. OCSP is an HTTP protocol and always
    // uses port 80 — there is no HTTPS variant to fold into the 443 rule below.
    // snowflake-connector-python checks revocation on every connect; without
    // this rule each attempt burns ~5-30s per responder before failing open
    // (measured ValidationLatency of 184s on a Snowflake scan, against a 900s
    // Lambda timeout). Egress-only, and OCSP responses are signed, so plaintext
    // transport is the intended design rather than a weakening.
    //
    // Only the Snowflake driver does internet OCSP — every other engine talks
    // to a private endpoint. A deployment with no Snowflake source can drop the
    // rule entirely with `-c connector_ocsp_egress=false`; the cost of doing so
    // while Snowflake IS in use is soft-fail revocation checking plus the
    // latency above, so it is on by default.
    addConfiguredOcspEgress(this.connectorSecurityGroup, "managed connector");
    for (const peer of connectorEgressPeers) {
      this.connectorSecurityGroup.addEgressRule(
        peer,
        ec2.Port.tcp(443),
        "HTTPS: Snowflake wire protocol + AWS APIs (Glue/S3/STS)",
      );
    }

    // ── Cross-network JDBC connectivity (optional, created-VPC only) ──
    // Imported VPCs are expected to bring their own peering/TGW/PrivateLink +
    // routes (managed externally), mirroring the VPC-endpoint policy above.
    if (!existingVpcId) {
      this.addJdbcConnectivity(this.vpc as ec2.Vpc);
    }

    // ── CfnOutputs ──────────────────────────────────────────────────
    new cdk.CfnOutput(this, "VpcId", {
      value: this.vpc.vpcId,
      description: "SemanticContext VPC ID",
    });

    new cdk.CfnOutput(this, "ServiceNamespaceArn", {
      value: this.serviceNamespace.namespaceArn,
      description: "Shared Cloud Map namespace ARN for service discovery",
    });
  }

  /**
   * Parse the `jdbc_*` connectivity context keys and, if any are set, provision
   * the corresponding cross-network plumbing (peering / TGW / PrivateLink) so
   * the discovery Lambda and Glue connector can reach source databases in
   * another VPC, account, or on-prem network.
   */
  private addJdbcConnectivity(vpc: ec2.Vpc): void {
    const peering = this.parsePeeringContext();
    const transitGateway = this.parseTransitGatewayContext();
    const privateLink = this.parsePrivateLinkContext();

    if (!peering && !transitGateway && !privateLink) {
      return;
    }

    new JdbcConnectivity(this, "JdbcConnectivity", {
      vpc,
      clientSecurityGroups: [
        this.lambdaSecurityGroup,
        this.connectorSecurityGroup,
      ],
      peering,
      transitGateway,
      privateLink,
    });
  }

  private parsePeeringContext(): PeeringConfig | undefined {
    const peerVpcId = this.ctxString("jdbc_peer_vpc_id");
    if (!peerVpcId) {
      return undefined;
    }
    const destinationCidrs = this.ctxList("jdbc_peer_cidrs");
    if (destinationCidrs.length === 0) {
      throw new Error(
        "jdbc_peer_vpc_id requires jdbc_peer_cidrs (comma-separated CIDRs)",
      );
    }
    return {
      peerVpcId,
      destinationCidrs,
      peerOwnerId: this.ctxString("jdbc_peer_owner_id"),
      peerRegion: this.ctxString("jdbc_peer_region"),
    };
  }

  private parseTransitGatewayContext(): TransitGatewayConfig | undefined {
    const transitGatewayId = this.ctxString("jdbc_tgw_id");
    if (!transitGatewayId) {
      return undefined;
    }
    const destinationCidrs = this.ctxList("jdbc_tgw_cidrs");
    if (destinationCidrs.length === 0) {
      throw new Error(
        "jdbc_tgw_id requires jdbc_tgw_cidrs (comma-separated CIDRs)",
      );
    }
    return { transitGatewayId, destinationCidrs };
  }

  private parsePrivateLinkContext(): PrivateLinkConfig | undefined {
    const serviceName = this.ctxString("jdbc_privatelink_service");
    if (!serviceName) {
      return undefined;
    }
    const portRaw = this.ctxString("jdbc_privatelink_port");
    const port = portRaw ? Number(portRaw) : NaN;
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error(
        "jdbc_privatelink_service requires jdbc_privatelink_port (integer 1-65535)",
      );
    }
    return {
      serviceName,
      port,
      privateDnsEnabled:
        this.ctxString("jdbc_privatelink_private_dns") === "true",
    };
  }

  /** Read a string context value, returning undefined when unset/empty. */
  private ctxString(key: string): string | undefined {
    const raw = this.node.tryGetContext(key) as string | undefined;
    return raw && String(raw).trim() ? String(raw).trim() : undefined;
  }

  /** Read a comma-separated context value into a trimmed, non-empty list. */
  private ctxList(key: string): string[] {
    const raw = this.ctxString(key);
    if (!raw) {
      return [];
    }
    return raw
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
  }
}
