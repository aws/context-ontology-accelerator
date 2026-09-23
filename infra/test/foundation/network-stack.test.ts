// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";

// Env required for fromLookup tests
const TEST_ENV = { account: "123456789012", region: "us-east-1" };

function buildStack(context: Record<string, string> = {}): Template {
  const app = new cdk.App({ context });
  const stack = new NetworkStack(app, "TestNetwork");
  return Template.fromStack(stack);
}

/** Egress rules of the Glue-connector security group. */
function connectorEgress(template: Template): any[] {
  const sgs = template.findResources("AWS::EC2::SecurityGroup");
  const connector = (Object.values(sgs) as any[]).find((sg) =>
    (sg.Properties.GroupDescription as string).startsWith(
      "Glue Connection / Athena connector",
    ),
  );
  expect(connector).toBeDefined();
  return connector.Properties.SecurityGroupEgress ?? [];
}

/** Egress rules of the shared Lambda security group. */
function lambdaEgress(template: Template): any[] {
  const sgs = template.findResources("AWS::EC2::SecurityGroup");
  const lambda = (Object.values(sgs) as any[]).find((sg) =>
    (sg.Properties.GroupDescription as string).startsWith("Lambda functions"),
  );
  expect(lambda).toBeDefined();
  return lambda.Properties.SecurityGroupEgress ?? [];
}

/** Egress rules dedicated to direct-discovery Snowflake OCSP. */
function discoveryOcspEgress(template: Template): any[] {
  const sgs = template.findResources("AWS::EC2::SecurityGroup");
  const discovery = (Object.values(sgs) as any[]).find((sg) =>
    (sg.Properties.GroupDescription as string).startsWith(
      "Snowflake discovery OCSP",
    ),
  );
  expect(discovery).toBeDefined();
  return discovery.Properties.SecurityGroupEgress ?? [];
}

function buildStackWithEnv(context: Record<string, string> = {}): Template {
  const app = new cdk.App({ context });
  const stack = new NetworkStack(app, "TestNetwork", { env: TEST_ENV });
  return Template.fromStack(stack);
}

describe("NetworkStack", () => {
  describe("VPC", () => {
    it("creates a VPC with public and private subnets", () => {
      const template = buildStack();
      template.hasResourceProperties("AWS::EC2::VPC", {});
      // 2 AZs x 2 subnet types = 4 subnets
      template.resourceCountIs("AWS::EC2::Subnet", 4);
    });

    it("creates exactly one NAT gateway", () => {
      const template = buildStack();
      template.resourceCountIs("AWS::EC2::NatGateway", 1);
    });

    it("uses fromLookup when vpc_id context is set (no VPC resource created)", () => {
      // fromLookup with a known account/region produces a dummy VPC at synth time
      const template = buildStackWithEnv({ vpc_id: "vpc-0abc123" });
      template.resourceCountIs("AWS::EC2::VPC", 0);
    });
  });

  describe("VPC Endpoints", () => {
    it("creates S3 and DynamoDB gateway endpoints", () => {
      const template = buildStack();
      const endpoints = template.findResources("AWS::EC2::VPCEndpoint");
      // Gateway endpoints have ServiceName as Fn::Sub object
      const serviceNames = Object.values(endpoints).map((e: any) =>
        JSON.stringify(e.Properties.ServiceName),
      );
      expect(serviceNames.some((s) => s.includes("s3"))).toBe(true);
      expect(serviceNames.some((s) => s.includes("dynamodb"))).toBe(true);
    });

    it("creates Bedrock Runtime, CloudWatch Logs, ECR API, and ECR Docker interface endpoints", () => {
      const template = buildStack();
      const endpoints = template.findResources("AWS::EC2::VPCEndpoint");
      const serviceNames = Object.values(endpoints).map((e: any) =>
        JSON.stringify(e.Properties.ServiceName),
      );
      expect(serviceNames.some((s) => s.includes("bedrock-runtime"))).toBe(
        true,
      );
      expect(serviceNames.some((s) => s.includes("logs"))).toBe(true);
      expect(serviceNames.some((s) => s.includes("ecr.api"))).toBe(true);
      expect(serviceNames.some((s) => s.includes("ecr.dkr"))).toBe(true);
    });

    it("creates the additional AWS API interface endpoints for scoped egress", () => {
      const template = buildStack();
      const endpoints = template.findResources("AWS::EC2::VPCEndpoint");
      const serviceNames = Object.values(endpoints).map((e: any) =>
        JSON.stringify(e.Properties.ServiceName),
      );
      for (const fragment of [
        ".sts",
        ".events",
        ".sqs",
        ".states",
        ".athena",
        ".glue",
        ".lambda",
        ".ecs",
        ".datazone",
        "neptune-graph",
        ".aoss",
      ]) {
        expect(serviceNames.some((s) => s.includes(fragment))).toBe(true);
      }
    });

    it("does not create VPC endpoints when importing an existing VPC", () => {
      const template = buildStackWithEnv({ vpc_id: "vpc-0abc123" });
      template.resourceCountIs("AWS::EC2::VPCEndpoint", 0);
    });
  });

  describe("Security Groups", () => {
    it("creates Neptune SG with correct description", () => {
      const template = buildStack();
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        GroupDescription: "Neptune cluster security group",
      });
    });

    it("Neptune SG ingress on 8182 is scoped to the ECS + Lambda SGs (not VPC CIDR)", () => {
      const template = buildStack();
      // Ingress is added after both client SGs exist, so it materializes as
      // standalone SecurityGroupIngress resources referencing a source SG.
      const ingress = template.findResources("AWS::EC2::SecurityGroupIngress", {
        Properties: {
          FromPort: 8182,
          ToPort: 8182,
          IpProtocol: "tcp",
          SourceSecurityGroupId: Match.anyValue(),
        },
      });
      // One rule from ECS SG + one from Lambda SG.
      expect(Object.keys(ingress).length).toBe(2);
      // And none of them open 8182 to a CIDR block.
      const cidrIngress = template.findResources(
        "AWS::EC2::SecurityGroupIngress",
        {
          Properties: { FromPort: 8182, CidrIp: Match.anyValue() },
        },
      );
      expect(Object.keys(cidrIngress).length).toBe(0);
    });

    it("creates ECS and Lambda security groups with egress locked down (no allow-all)", () => {
      const template = buildStack();
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        GroupDescription: "ECS Fargate tasks security group",
      });
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        GroupDescription: "Lambda functions security group",
      });
      // Neither SG should carry the CDK allow-all egress rule
      // (0.0.0.0/0 on IpProtocol "-1").
      const sgs = template.findResources("AWS::EC2::SecurityGroup");
      for (const sg of Object.values(sgs) as any[]) {
        const desc = sg.Properties.GroupDescription as string;
        if (desc.startsWith("ECS Fargate") || desc.startsWith("Lambda")) {
          const egress = sg.Properties.SecurityGroupEgress ?? [];
          expect(
            egress.some(
              (r: any) => r.IpProtocol === "-1" && r.CidrIp === "0.0.0.0/0",
            ),
          ).toBe(false);
          // 443 egress should be present.
          expect(
            egress.some((r: any) => r.FromPort === 443 && r.ToPort === 443),
          ).toBe(true);
        }
      }
    });

    it("Lambda SG has egress to source-DB ports (direct JDBC discovery)", () => {
      const template = buildStack();
      const sgs = template.findResources("AWS::EC2::SecurityGroup");
      const lambdaSg = (Object.values(sgs) as any[]).find((sg) =>
        (sg.Properties.GroupDescription as string).startsWith(
          "Lambda functions",
        ),
      );
      expect(lambdaSg).toBeDefined();
      const egress = lambdaSg.Properties.SecurityGroupEgress ?? [];
      for (const port of [5432, 3306, 1433, 5439, 1521]) {
        expect(
          egress.some((r: any) => r.FromPort === port && r.ToPort === port),
        ).toBe(true);
      }
    });

    it("Connector SG restricts egress to source-DB ports + 443 (no allow-all)", () => {
      const template = buildStack();
      const sgs = template.findResources("AWS::EC2::SecurityGroup");
      const connector = (Object.values(sgs) as any[]).find((sg) =>
        (sg.Properties.GroupDescription as string).startsWith(
          "Glue Connection / Athena connector",
        ),
      );
      expect(connector).toBeDefined();
      const egress = connector.Properties.SecurityGroupEgress ?? [];
      // No allow-all egress.
      expect(
        egress.some(
          (r: any) => r.IpProtocol === "-1" && r.CidrIp === "0.0.0.0/0",
        ),
      ).toBe(false);
      // Each supported DB port + 443 present.
      for (const port of [5432, 3306, 1433, 5439, 1521, 443]) {
        expect(
          egress.some((r: any) => r.FromPort === port && r.ToPort === port),
        ).toBe(true);
      }
    });

    it("Connector and direct-discovery SGs allow port 80 for Snowflake OCSP by default", () => {
      const template = buildStack();
      for (const egress of [
        connectorEgress(template),
        discoveryOcspEgress(template),
      ]) {
        expect(
          egress.some((r: any) => r.FromPort === 80 && r.ToPort === 80),
        ).toBe(true);
        expect(
          egress.some(
            (r: any) => r.IpProtocol === "-1" && r.CidrIp === "0.0.0.0/0",
          ),
        ).toBe(false);
      }
      expect(
        lambdaEgress(template).some(
          (r: any) => r.FromPort === 80 && r.ToPort === 80,
        ),
      ).toBe(false);
    });

    it("Connector and direct-discovery SGs drop OCSP when connector_ocsp_egress=false", () => {
      // A deployment with no Snowflake source has no use for port 80 egress.
      const template = buildStack({ connector_ocsp_egress: "false" });
      for (const egress of [
        connectorEgress(template),
        discoveryOcspEgress(template),
      ]) {
        expect(
          egress.some((r: any) => r.FromPort === 80 && r.ToPort === 80),
        ).toBe(false);
      }
      const egress = connectorEgress(template);
      expect(
        egress.some((r: any) => r.FromPort === 443 && r.ToPort === 443),
      ).toBe(true);
    });

    it("connector_egress_cidrs scopes every connector egress rule, not just one port", () => {
      // The default is 0.0.0.0/0 because a managed connector reaching a SaaS
      // warehouse has no enumerable destination set; where the egress path IS
      // known (fixed NAT/proxy), this narrows OCSP, 443 and the DB ports alike.
      const egress = connectorEgress(
        buildStack({ connector_egress_cidrs: "10.20.0.0/16,192.0.2.10/32" }),
      );
      const internetBound = egress.filter((r: any) =>
        [80, 443, 5432, 3306, 1433, 5439, 1521].includes(r.FromPort),
      );
      expect(internetBound.length).toBeGreaterThan(0);
      for (const rule of internetBound) {
        expect(["10.20.0.0/16", "192.0.2.10/32"]).toContain(rule.CidrIp);
      }
      expect(egress.some((r: any) => r.CidrIp === "0.0.0.0/0")).toBe(false);
      // Both CIDRs get the full port set, so narrowing never silently drops one.
      for (const cidr of ["10.20.0.0/16", "192.0.2.10/32"]) {
        for (const port of [80, 443, 1521]) {
          expect(
            egress.some((r: any) => r.CidrIp === cidr && r.FromPort === port),
          ).toBe(true);
        }
      }
    });

    it("connector_egress_cidrs also scopes direct-discovery OCSP egress", () => {
      const egress = discoveryOcspEgress(
        buildStack({
          connector_egress_cidrs: "10.20.0.0/16,192.0.2.10/32",
        }),
      ).filter((r: any) => r.FromPort === 80 && r.ToPort === 80);

      expect(egress).toHaveLength(2);
      expect(egress.map((r: any) => r.CidrIp).sort()).toEqual(
        ["10.20.0.0/16", "192.0.2.10/32"].sort(),
      );
      expect(egress.some((r: any) => r.CidrIp === "0.0.0.0/0")).toBe(false);
    });
  });

  describe("Cloud Map Namespace", () => {
    it("creates a shared PrivateDnsNamespace associated with the VPC", () => {
      const template = buildStack();
      template.hasResourceProperties(
        "AWS::ServiceDiscovery::PrivateDnsNamespace",
        {
          Name: Match.stringLikeRegexp(".*-services\\.local$"),
          Vpc: Match.anyValue(),
        },
      );
    });
  });

  describe("Outputs", () => {
    it("exports VPC ID", () => {
      const template = buildStack();
      const outputs = template.findOutputs("VpcId");
      expect(Object.keys(outputs).length).toBe(1);
    });
  });

  describe("Cross-network JDBC connectivity", () => {
    it("creates no connectivity resources by default", () => {
      const template = buildStack();
      template.resourceCountIs("AWS::EC2::VPCPeeringConnection", 0);
      template.resourceCountIs("AWS::EC2::TransitGatewayVpcAttachment", 0);
    });

    it("provisions VPC peering + routes from context", () => {
      const template = buildStack({
        jdbc_peer_vpc_id: "vpc-0peer123",
        jdbc_peer_cidrs: "10.20.0.0/16,10.21.0.0/16",
      });
      template.hasResourceProperties("AWS::EC2::VPCPeeringConnection", {
        PeerVpcId: "vpc-0peer123",
      });
      const routes = template.findResources("AWS::EC2::Route", {
        Properties: { VpcPeeringConnectionId: Match.anyValue() },
      });
      // 2 private subnets x 2 CIDRs.
      expect(Object.keys(routes).length).toBe(4);
    });

    it("throws when peer VPC is set without CIDRs", () => {
      expect(() => buildStack({ jdbc_peer_vpc_id: "vpc-0peer123" })).toThrow(
        /jdbc_peer_cidrs/,
      );
    });

    it("provisions a Transit Gateway attachment + routes from context", () => {
      const template = buildStack({
        jdbc_tgw_id: "tgw-0abc",
        jdbc_tgw_cidrs: "10.30.0.0/16",
      });
      template.hasResourceProperties("AWS::EC2::TransitGatewayVpcAttachment", {
        TransitGatewayId: "tgw-0abc",
      });
    });

    it("provisions a PrivateLink interface endpoint from context", () => {
      const template = buildStack({
        jdbc_privatelink_service: "com.amazonaws.vpce.us-east-1.vpce-svc-0abc",
        jdbc_privatelink_port: "5432",
      });
      template.hasResourceProperties("AWS::EC2::VPCEndpoint", {
        ServiceName: "com.amazonaws.vpce.us-east-1.vpce-svc-0abc",
        VpcEndpointType: "Interface",
      });
    });

    it("throws when PrivateLink service has an invalid port", () => {
      expect(() =>
        buildStack({
          jdbc_privatelink_service:
            "com.amazonaws.vpce.us-east-1.vpce-svc-0abc",
          jdbc_privatelink_port: "not-a-port",
        }),
      ).toThrow(/jdbc_privatelink_port/);
    });

    it("does not provision connectivity when importing an existing VPC", () => {
      const template = buildStackWithEnv({
        vpc_id: "vpc-0abc123",
        jdbc_peer_vpc_id: "vpc-0peer123",
        jdbc_peer_cidrs: "10.20.0.0/16",
      });
      template.resourceCountIs("AWS::EC2::VPCPeeringConnection", 0);
    });
  });
});
