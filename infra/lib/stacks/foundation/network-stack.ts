// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";

import { SCLStack } from "../../constructs";
import { resolveContext } from "../../context";
import { graphSourcesEnabled } from "../../utils/graph-sources";
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
 * key.  When omitted, a new VPC is created, on `vpc_cidr` when set (default
 * `10.0.0.0/16`). `vpc_cidr` only applies to a NEW deployment: changing it on
 * an existing one replaces the VPC and everything in it.
 *
 * Context keys (in addition to SCLStack defaults):
 *   - `vpc_id` - optional; if provided, looks up an existing VPC instead
 *     of creating one.
 *   - `aoss_vpc_endpoint_id` - required with `vpc_id`: the existing
 *     `com.amazonaws.<region>.aoss-data` interface endpoint in that VPC. The
 *     OpenSearch Serverless network policy allows only this endpoint, and COA
 *     creates no endpoints in an imported VPC.
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

  /**
   * API authorizer Lambda. Keeps unrestricted egress: it fetches the identity
   * provider's signing keys, and an external OIDC IdP may serve them on any port.
   */
  public readonly authorizerSecurityGroup: ec2.SecurityGroup;

  /** Port-80 egress used only by direct Snowflake discovery for OCSP. */
  public readonly discoveryOcspSecurityGroup: ec2.SecurityGroup;
  /**
   * SPARQL egress for the graph-verify Lambda: reaches a registered RDF_SPARQL
   * source's endpoint, which may be any cluster in a reachable network. Attached
   * only to that Lambda, alongside the shared Lambda SG. Undefined unless
   * `graph_sources` is on.
   */
  public readonly sparqlSourceEgressSecurityGroup?: ec2.SecurityGroup;

  /**
   * Security group meant for the AOSS VPC endpoint (port 443). It is NOT attached
   * to the `aoss-data` endpoint: in a created VPC that endpoint has the security
   * group CDK creates for it (HTTPS from the VPC CIDR), and in an imported VPC
   * the customer's endpoint has its own. The ingress rules other stacks add here
   * therefore have no effect; AOSS traffic relies on the callers' egress, so do
   * not narrow their 443 egress to this group.
   */
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

    const existingVpcId = this.node.tryGetContext("vpc_id") as
      | string
      | undefined;

    if (existingVpcId) {
      if (this.ctxString("vpc_cidr")) {
        throw new Error(
          "vpc_cidr and vpc_id are mutually exclusive: an imported VPC keeps its own CIDR",
        );
      }
      if (
        cdk.Token.isUnresolved(this.account) ||
        cdk.Token.isUnresolved(this.region)
      ) {
        throw new Error(
          "vpc_id needs an AWS account and region at synth to look up the VPC, " +
            "and none was resolved. Configure AWS credentials and a region for " +
            "the CDK CLI (AWS_PROFILE or credentials, and AWS_REGION), or set " +
            "CDK_DEFAULT_ACCOUNT and CDK_DEFAULT_REGION.",
        );
      }
      this.vpc = ec2.Vpc.fromLookup(this, "ImportedVpc", {
        vpcId: existingVpcId,
      });
      const aossVpcEndpointId = this.ctxString("aoss_vpc_endpoint_id");
      if (!aossVpcEndpointId) {
        throw new Error(
          "vpc_id requires aoss_vpc_endpoint_id (SCL_AOSS_VPC_ENDPOINT_ID): the ID " +
            `of the com.amazonaws.${cdk.Stack.of(this).region}.aoss-data interface ` +
            `endpoint in ${existingVpcId}, with private DNS enabled. COA creates no ` +
            "VPC endpoints in an imported VPC, and the OpenSearch Serverless network " +
            "policy allows only this endpoint.",
        );
      }
      if (!/^vpce-[0-9a-f]{8,17}$/.test(aossVpcEndpointId)) {
        throw new Error(
          `aoss_vpc_endpoint_id must be a VPC endpoint ID (vpce-...), got "${aossVpcEndpointId}"`,
        );
      }
      this.aossVpcEndpointId = aossVpcEndpointId;
    } else {
      // Fail rather than ignore it: an endpoint without vpc_id means the
      // operator meant an existing VPC, and would silently get a new one.
      if (this.ctxString("aoss_vpc_endpoint_id")) {
        throw new Error(
          "aoss_vpc_endpoint_id (SCL_AOSS_VPC_ENDPOINT_ID) only applies with " +
            "vpc_id (SCL_VPC_ID). Set vpc_id to deploy into that endpoint's " +
            "VPC, or unset aoss_vpc_endpoint_id to let COA create its own VPC.",
        );
      }
      // Pin VPC AZs to AgentCore-supported physical zones: AZ names map to different AZ IDs
      // per account, so the app entrypoint resolves the supported names via
      // ``resolveAgentCoreAzNames`` and passes them in. Without them (unit synth), fall back
      // to the default 2-AZ spread.
      const azNames = props?.agentCoreAzNames;
      const vpcCidr = this.ctxString("vpc_cidr");
      if (vpcCidr) {
        validateVpcCidr(vpcCidr, {
          azCount: azNames && azNames.length > 0 ? azNames.length : 2,
          peerCidrs: [
            ...this.ctxList("jdbc_peer_cidrs"),
            ...this.ctxList("jdbc_tgw_cidrs"),
          ],
        });
      }
      this.vpc = new ec2.Vpc(this, "Vpc", {
        vpcName: this.prefixed("vpc"),
        // Unset keeps CDK's default (10.0.0.0/16), so existing deployments see
        // no diff.
        ...(vpcCidr && { ipAddresses: ec2.IpAddresses.cidr(vpcCidr) }),
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

    // Neptune - allow inbound 8182 from the ECS + Lambda SGs only (not the
    // whole VPC CIDR). Neptune is a database; it never initiates connections,
    // so outbound stays disabled. Ingress rules are added below, after the
    // ECS and Lambda SGs are constructed.
    this.neptuneSecurityGroup = new ec2.SecurityGroup(this, "NeptuneSG", {
      vpc: this.vpc,
      securityGroupName: this.prefixed("neptune-sg"),
      // GroupDescription is immutable in CloudFormation; keep it rule-agnostic to avoid replacement.
      description: "Neptune cluster security group",
      allowAllOutbound: false, // Neptune is a database; it doesn't initiate connections
    });

    // ECS - egress restricted to least privilege. Fargate tasks pull private ECR images
    // (ECR api/dkr interface endpoints + S3 gateway endpoint) and call AWS service APIs on
    // 443. 443 egress is left open to 0.0.0.0/0 rather than the VPC CIDR because gateway
    // endpoints (S3/DynamoDB) route to AWS public IP ranges via prefix lists and some SDK
    // calls may fall back to public regional endpoints. All non-HTTPS, non-Neptune outbound
    // is denied.
    this.ecsSecurityGroup = new ec2.SecurityGroup(this, "EcsSG", {
      vpc: this.vpc,
      securityGroupName: this.prefixed("ecs-sg"),
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
    // The API authorizer fetches the IdP's JWKS, and an external OIDC IdP can
    // serve it on any port, so this SG allows all outbound traffic. It is defined
    // here, not in ApiStack, so that EniWarmStack can pre-warm its Lambda network
    // interface.
    this.authorizerSecurityGroup = new ec2.SecurityGroup(this, "AuthorizerSG", {
      vpc: this.vpc,
      securityGroupName: this.prefixed("authorizer-sg"),
      description: "API authorizer security group",
      allowAllOutbound: true,
    });

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

    // A graph source's SPARQL endpoint is customer-chosen, so the shared Lambda
    // SG's 8182 rule (scoped to the platform Neptune SG) does not reach it. Kept
    // off the shared SG for the same reason as the OCSP egress: only the
    // graph-verify Lambda probes source endpoints. 8182 is Neptune's port and the
    // configuration default; a source on another port needs a rule here.
    // The group and its export exist with the switch off too, with no rules: a
    // consumer deployed with the switch on still imports the export, and this stack
    // is updated before it, so dropping the export would fail the off deploy. It
    // gets its rules, and is handed to consumers, only with `-c graph_sources=on`.
    const sparqlSg = new ec2.SecurityGroup(this, "SparqlSourceEgressSG", {
      vpc: this.vpc,
      securityGroupName: this.prefixed("sparql-source-egress-sg"),
      description: "Graph source SPARQL endpoint egress security group",
      allowAllOutbound: false,
    });
    this.exportValue(sparqlSg.securityGroupId);
    if (graphSourcesEnabled(this.node)) {
      // Only this VPC and the networks it reaches by peering or transit gateway. A
      // registered endpoint must also be a Neptune cluster endpoint of this account on
      // 8182 (coa_common.sparql_config, graph_verify), so the probe cannot be pointed at
      // an outside host.
      sparqlSg.addEgressRule(
        ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
        ec2.Port.tcp(8182),
        "RDF_SPARQL source endpoint in the VPC (graph-verify reachability probe)",
      );
      for (const cidr of [
        ...this.ctxList("jdbc_peer_cidrs"),
        ...this.ctxList("jdbc_tgw_cidrs"),
      ]) {
        sparqlSg.addEgressRule(
          ec2.Peer.ipv4(cidr),
          ec2.Port.tcp(8182),
          `RDF_SPARQL source endpoint in peer network ${cidr} (graph-verify reachability probe)`,
        );
      }
      this.sparqlSourceEgressSecurityGroup = sparqlSg;
    }

    // Neptune ingress: scoped to the ECS + Lambda SGs (least privilege).
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

    // Not attached to the aoss-data endpoint in either mode, so its ingress
    // rules have no effect: see the aossSecurityGroup field doc.
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
    // Snowflake (443) and any AWS API the connector calls are covered by the 443 rule below.
    // A managed Glue connector reaching a customer database or SaaS warehouse has no
    // enumerable destination set, so the default is `0.0.0.0/0` for these ports. Where the
    // destinations are known, narrow all internet-bound egress of this connector with
    //   -c connector_egress_cidrs=10.20.0.0/16,192.0.2.10/32
    // It applies to the DB ports, OCSP 80 and 443 alike.
    for (const peer of connectorEgressPeers) {
      for (const port of [5432, 3306, 1433, 5439, 1521]) {
        this.connectorSecurityGroup.addEgressRule(
          peer,
          ec2.Port.tcp(port),
          `Source database ${port}`,
        );
      }
    }
    // OCSP certificate-revocation checks use plain HTTP on port 80 (OCSP responses are
    // signed, so plaintext is by design). snowflake-connector-python checks revocation on
    // every connect; without this rule each attempt burns ~5-30s per responder before
    // failing open. Only the Snowflake driver does internet OCSP; a deployment without
    // Snowflake sources can drop the rule with `-c connector_ocsp_egress=false`.
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

    new cdk.CfnOutput(this, "VpcId", {
      value: this.vpc.vpcId,
      description: "SemanticContext VPC ID",
    });

    // For CDK apps outside this one that attach compute to this VPC, today the COA-operated
    // Databricks connector. Its deploy script reads both and passes them in as variables:
    // SSM parameters are the cross-app contract here, and an SSM StringList read inside the
    // other stack would be a list token of unknown length, which Vpc.fromVpcAttributes cannot
    // map to subnets.
    const { ssmPrefix, envName } = resolveContext(this.node);
    new ssm.StringParameter(this, "SsmVpcId", {
      parameterName: `${ssmPrefix}/${envName}/network/vpc-id`,
      stringValue: this.vpc.vpcId,
      description: "SemanticContext VPC ID",
    });
    new ssm.StringParameter(this, "SsmPrivateSubnetIds", {
      parameterName: `${ssmPrefix}/${envName}/network/private-subnet-ids`,
      stringValue: cdk.Fn.join(
        ",",
        this.vpc.selectSubnets({
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        }).subnetIds,
      ),
      description:
        "SemanticContext private subnet IDs (with egress), comma-separated",
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

/** Each AZ gets one public and one private subnet of this size. */
const SUBNET_PREFIX = 24;

function parseIpv4Cidr(cidr: string): { base: number; prefix: number } {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/.exec(
    cidr,
  );
  const octets = match ? match.slice(1, 5).map(Number) : [];
  const prefix = match ? Number(match[5]) : NaN;
  if (!match || octets.some((o) => o > 255) || prefix > 32) {
    throw new Error(`'${cidr}' is not an IPv4 CIDR (e.g. 10.40.0.0/16)`);
  }
  const address = octets.reduce((acc, o) => acc * 256 + o, 0);
  const size = 2 ** (32 - prefix);
  if (address % size !== 0) {
    throw new Error(
      `'${cidr}' has host bits set; use the network address of the block`,
    );
  }
  return { base: address, prefix };
}

function overlaps(a: string, b: string): boolean {
  const x = parseIpv4Cidr(a);
  const y = parseIpv4Cidr(b);
  const xEnd = x.base + 2 ** (32 - x.prefix);
  const yEnd = y.base + 2 ** (32 - y.prefix);
  return x.base < yEnd && y.base < xEnd;
}

/**
 * Reject a `vpc_cidr` the platform VPC cannot be built on, at synth rather than
 * as a CloudFormation failure an hour into a deploy.
 *
 * - AWS allows a VPC between /16 and /28.
 * - It must hold one public and one private /24 per AZ.
 * - It must not overlap a peered or Transit Gateway network, or the routes to
 *   that network cannot exist.
 */
export function validateVpcCidr(
  cidr: string,
  opts: { azCount: number; peerCidrs: string[] },
): void {
  const { prefix } = parseIpv4Cidr(cidr);
  if (prefix < 16 || prefix > 28) {
    throw new Error(`vpc_cidr ${cidr}: AWS allows a VPC between /16 and /28`);
  }
  const subnetsNeeded = opts.azCount * 2;
  const subnetsAvailable = 2 ** (SUBNET_PREFIX - prefix);
  if (prefix > SUBNET_PREFIX || subnetsAvailable < subnetsNeeded) {
    throw new Error(
      `vpc_cidr ${cidr} is too small: ${opts.azCount} AZs need ${subnetsNeeded} /${SUBNET_PREFIX} subnets ` +
        `(use /${SUBNET_PREFIX - Math.ceil(Math.log2(subnetsNeeded))} or larger)`,
    );
  }
  for (const peer of opts.peerCidrs) {
    if (overlaps(cidr, peer)) {
      throw new Error(
        `vpc_cidr ${cidr} overlaps peer network ${peer}; peering and its routes need disjoint ranges`,
      );
    }
  }
}
