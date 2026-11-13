// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";

import { SCLConstruct } from "./scl-construct";

/**
 * VPC-peering connectivity to a source-database network.
 *
 * Creates a peering connection from the platform VPC to `peerVpcId` and adds
 * routes to `destinationCidrs` on every private subnet's route table. For
 * **cross-account** peering, set `peerOwnerId` (and `peerRegion` for
 * cross-region); the peer account must accept the peering request and add the
 * return routes on their side.
 */
export interface PeeringConfig {
  /** The source-network VPC to peer with. */
  readonly peerVpcId: string;
  /** CIDR blocks in the peer network that host the database(s). */
  readonly destinationCidrs: string[];
  /** Peer account id — required only for cross-account peering. */
  readonly peerOwnerId?: string;
  /** Peer region — required only for cross-region peering. */
  readonly peerRegion?: string;
}

/**
 * Transit Gateway connectivity. Attaches the platform VPC's private subnets to
 * an existing Transit Gateway and routes `destinationCidrs` through it. The TGW
 * route table must have the return route to the platform VPC.
 */
export interface TransitGatewayConfig {
  /** Existing Transit Gateway id to attach to. */
  readonly transitGatewayId: string;
  /** CIDR blocks reachable via the Transit Gateway (the DB networks). */
  readonly destinationCidrs: string[];
}

/**
 * AWS PrivateLink connectivity. Creates an interface endpoint to a producer's
 * VPC endpoint service (an NLB fronting the database). Tolerates overlapping
 * CIDRs and needs no routes. Use the endpoint's DNS name (or a private hosted
 * zone alias) as the source `host`.
 */
export interface PrivateLinkConfig {
  /** Producer endpoint-service name, e.g. `com.amazonaws.vpce.us-east-1.vpce-svc-0abc...`. */
  readonly serviceName: string;
  /** Database port the endpoint service listens on (e.g. 5432, 3306, 1433, 1521). */
  readonly port: number;
  /** Enable private DNS for the endpoint (only if the service supports it). Default false. */
  readonly privateDnsEnabled?: boolean;
}

export interface JdbcConnectivityProps {
  /** The platform VPC (must be a created VPC so route tables are mutable). */
  readonly vpc: ec2.IVpc;
  /**
   * Security groups whose members must reach the database. Used for:
   * - **PrivateLink path:** scopes the endpoint's inbound rules to these SGs.
   * - **Peering/TGW paths:** adds JDBC-port egress rules to these SGs for the
   *   peer/TGW destination CIDRs.
   */
  readonly clientSecurityGroups: ec2.ISecurityGroup[];
  readonly peering?: PeeringConfig;
  readonly transitGateway?: TransitGatewayConfig;
  readonly privateLink?: PrivateLinkConfig;
}

/**
 * Provisions cross-network connectivity from the platform VPC to source
 * databases that live in another VPC, account, or on-premises network.
 *
 * Supports VPC peering, Transit Gateway, and PrivateLink — any combination.
 * This is the **Context Ontology Accelerator-side** network plumbing (attachment + routes + endpoint)
 * that complements the customer-side prerequisites (DB inbound security group,
 * DNS, and — for peering/TGW — the return route on the peer).
 *
 * Connectors are routing-agnostic (`socket.create_connection` to `host:port`),
 * so no application change is needed — only this infrastructure.
 */
export class JdbcConnectivity extends SCLConstruct {
  public readonly peeringConnection?: ec2.CfnVPCPeeringConnection;
  public readonly transitGatewayAttachment?: ec2.CfnTransitGatewayVpcAttachment;
  public readonly privateLinkEndpoint?: ec2.InterfaceVpcEndpoint;
  public readonly privateLinkSecurityGroup?: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: JdbcConnectivityProps) {
    super(scope, id);

    if (!props.peering && !props.transitGateway && !props.privateLink) {
      throw new Error(
        "JdbcConnectivity requires at least one of: peering, transitGateway, privateLink",
      );
    }

    const privateSubnets = props.vpc.selectSubnets({
      subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    }).subnets;

    if (props.peering) {
      this.peeringConnection = this.addPeering(
        props.vpc,
        props.peering,
        privateSubnets,
      );
    }
    if (props.transitGateway) {
      this.transitGatewayAttachment = this.addTransitGateway(
        props.vpc,
        props.transitGateway,
        privateSubnets,
      );
    }

    // ── Egress rules on client SGs for peering/TGW destination CIDRs ──
    // Clients with allowAllOutbound: true already reach everything, but
    // locked-down SGs need explicit rules. Adding egress on an open SG is
    // a harmless no-op (CDK deduplicates), so we always add them.
    const jdbcPorts = [5432, 3306, 1433, 5439];
    const egressCidrs = [
      ...(props.peering?.destinationCidrs ?? []),
      ...(props.transitGateway?.destinationCidrs ?? []),
    ];
    for (const sg of props.clientSecurityGroups) {
      for (const cidr of egressCidrs) {
        for (const port of jdbcPorts) {
          sg.addEgressRule(
            ec2.Peer.ipv4(cidr),
            ec2.Port.tcp(port),
            `JDBC ${port} to peer/TGW ${cidr}`,
          );
        }
      }
    }
    if (props.privateLink) {
      const { endpoint, securityGroup } = this.addPrivateLink(
        props.vpc,
        props.privateLink,
        props.clientSecurityGroups,
      );
      this.privateLinkEndpoint = endpoint;
      this.privateLinkSecurityGroup = securityGroup;
    }
  }

  private addPeering(
    vpc: ec2.IVpc,
    cfg: PeeringConfig,
    privateSubnets: ec2.ISubnet[],
  ): ec2.CfnVPCPeeringConnection {
    if (cfg.destinationCidrs.length === 0) {
      throw new Error("peering.destinationCidrs must not be empty");
    }
    const peering = new ec2.CfnVPCPeeringConnection(this, "Peering", {
      vpcId: vpc.vpcId,
      peerVpcId: cfg.peerVpcId,
      peerOwnerId: cfg.peerOwnerId,
      peerRegion: cfg.peerRegion,
    });
    privateSubnets.forEach((subnet, i) => {
      cfg.destinationCidrs.forEach((cidr, j) => {
        new ec2.CfnRoute(this, `PeerRoute${i}_${j}`, {
          routeTableId: subnet.routeTable.routeTableId,
          destinationCidrBlock: cidr,
          vpcPeeringConnectionId: peering.ref,
        });
      });
    });
    return peering;
  }

  private addTransitGateway(
    vpc: ec2.IVpc,
    cfg: TransitGatewayConfig,
    privateSubnets: ec2.ISubnet[],
  ): ec2.CfnTransitGatewayVpcAttachment {
    if (cfg.destinationCidrs.length === 0) {
      throw new Error("transitGateway.destinationCidrs must not be empty");
    }
    const attachment = new ec2.CfnTransitGatewayVpcAttachment(
      this,
      "TgwAttachment",
      {
        transitGatewayId: cfg.transitGatewayId,
        vpcId: vpc.vpcId,
        subnetIds: privateSubnets.map((s) => s.subnetId),
      },
    );
    privateSubnets.forEach((subnet, i) => {
      cfg.destinationCidrs.forEach((cidr, j) => {
        const route = new ec2.CfnRoute(this, `TgwRoute${i}_${j}`, {
          routeTableId: subnet.routeTable.routeTableId,
          destinationCidrBlock: cidr,
          transitGatewayId: cfg.transitGatewayId,
        });
        // The route cannot resolve until the VPC is attached to the TGW.
        route.addDependency(attachment);
      });
    });
    return attachment;
  }

  private addPrivateLink(
    vpc: ec2.IVpc,
    cfg: PrivateLinkConfig,
    clientSecurityGroups: ec2.ISecurityGroup[],
  ): { endpoint: ec2.InterfaceVpcEndpoint; securityGroup: ec2.SecurityGroup } {
    const securityGroup = new ec2.SecurityGroup(this, "PrivateLinkSG", {
      vpc,
      securityGroupName: this.prefixed("jdbc-privatelink-sg"),
      description: "JDBC PrivateLink endpoint - inbound from connector clients",
      allowAllOutbound: false,
    });
    // Only the discovery/connector SGs may reach the endpoint, on the DB port.
    for (const [i, client] of clientSecurityGroups.entries()) {
      securityGroup.addIngressRule(
        client,
        ec2.Port.tcp(cfg.port),
        `Allow client SG ${i} to JDBC PrivateLink`,
      );
    }
    const endpoint = new ec2.InterfaceVpcEndpoint(this, "PrivateLinkEndpoint", {
      vpc,
      service: new ec2.InterfaceVpcEndpointService(cfg.serviceName, cfg.port),
      privateDnsEnabled: cfg.privateDnsEnabled ?? false,
      securityGroups: [securityGroup],
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });
    return { endpoint, securityGroup };
  }
}
