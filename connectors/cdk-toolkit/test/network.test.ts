// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { AthenaFederationConnector, ConnectorNetwork } from "../src/athena-federation-connector";
import { NETWORK_VARS, connectorNetworkFromEnv } from "../src/network";

const VPC = "vpc-0a1b2c3d4e5f60718";
const SUBNET_A = "subnet-0a1b2c3d4e5f60711";
const SUBNET_B = "subnet-0a1b2c3d4e5f60722";
const SG = "sg-0a1b2c3d4e5f60733";

const TOUCHED = Object.values(NETWORK_VARS);
let saved: Record<string, string | undefined>;

beforeEach(() => {
  saved = {};
  for (const name of TOUCHED) {
    saved[name] = process.env[name];
    delete process.env[name];
  }
});

afterEach(() => {
  for (const name of TOUCHED) {
    if (saved[name] === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = saved[name];
    }
  }
});

const FAKE_JAR = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "coa-net-")), "connector.jar");
fs.writeFileSync(FAKE_JAR, "not really a jar");

function newStack(): cdk.Stack {
  return new cdk.Stack(new cdk.App(), "TestStack", {
    env: { account: "123456789012", region: "eu-central-1" },
  });
}

function synthWith(network: (stack: cdk.Stack) => ConnectorNetwork | undefined): Template {
  const stack = newStack();
  new AthenaFederationConnector(stack, "Connector", {
    connectorId: "example",
    handler: "dev.coa.example.ExampleCompositeHandler",
    jarPath: FAKE_JAR,
    queryRoleArns: ["arn:aws:iam::123456789012:role/serve"],
    network: network(stack),
  });
  return Template.fromStack(stack);
}

describe("connectorNetworkFromEnv", () => {
  it("returns nothing when no variable is set, leaving the function outside any VPC", () => {
    expect(connectorNetworkFromEnv(newStack())).toBeUndefined();
  });

  it("refuses an unset VPC when one is required, with the caller's hint", () => {
    expect(() =>
      connectorNetworkFromEnv(newStack(), { required: true, hint: "Run the deploy script." }),
    ).toThrow(/CONNECTOR_VPC_ID and CONNECTOR_SUBNET_IDS are not set[\s\S]*Run the deploy script/);
  });

  it("refuses a VPC without subnets rather than deploying half-attached", () => {
    process.env.CONNECTOR_VPC_ID = VPC;
    expect(() => connectorNetworkFromEnv(newStack())).toThrow(
      /CONNECTOR_VPC_ID is set but CONNECTOR_SUBNET_IDS is not/,
    );
  });

  it("refuses subnets or security groups without a VPC", () => {
    process.env.CONNECTOR_SUBNET_IDS = SUBNET_A;
    process.env.CONNECTOR_SECURITY_GROUP_IDS = SG;
    expect(() => connectorNetworkFromEnv(newStack())).toThrow(
      "CONNECTOR_SUBNET_IDS and CONNECTOR_SECURITY_GROUP_IDS set without CONNECTOR_VPC_ID.",
    );
  });

  it("refuses a malformed id, naming the variable it came from", () => {
    process.env.CONNECTOR_VPC_ID = VPC;
    process.env.CONNECTOR_SUBNET_IDS = `${SUBNET_A},sg-0a1b2c3d`;
    expect(() => connectorNetworkFromEnv(newStack())).toThrow(
      'CONNECTOR_SUBNET_IDS entry "sg-0a1b2c3d" is not a subnet id.',
    );
    process.env.CONNECTOR_SUBNET_IDS = SUBNET_A;
    process.env.CONNECTOR_VPC_ID = "my-vpc";
    expect(() => connectorNetworkFromEnv(newStack())).toThrow('"my-vpc" is not a VPC id');
  });

  it("attaches the function to every subnet given, deduplicated", () => {
    process.env.CONNECTOR_VPC_ID = VPC;
    process.env.CONNECTOR_SUBNET_IDS = ` ${SUBNET_A}, ${SUBNET_B},${SUBNET_A},`;
    synthWith((stack) => connectorNetworkFromEnv(stack)).hasResourceProperties(
      "AWS::Lambda::Function",
      { VpcConfig: { SubnetIds: [SUBNET_A, SUBNET_B] } },
    );
  });

  it("uses the security groups given instead of creating one", () => {
    process.env.CONNECTOR_VPC_ID = VPC;
    process.env.CONNECTOR_SUBNET_IDS = SUBNET_A;
    process.env.CONNECTOR_SECURITY_GROUP_IDS = SG;
    const template = synthWith((stack) => connectorNetworkFromEnv(stack));
    template.hasResourceProperties("AWS::Lambda::Function", {
      VpcConfig: { SecurityGroupIds: [SG] },
    });
    template.resourceCountIs("AWS::EC2::SecurityGroup", 0);
  });
});

describe("a VPC-attached connector", () => {
  beforeEach(() => {
    process.env.CONNECTOR_VPC_ID = VPC;
    process.env.CONNECTOR_SUBNET_IDS = SUBNET_A;
  });

  it("gets its own security group with HTTPS egress and nothing else", () => {
    const template = synthWith((stack) => connectorNetworkFromEnv(stack));
    template.resourceCountIs("AWS::EC2::SecurityGroup", 1);
    template.hasResourceProperties("AWS::EC2::SecurityGroup", {
      VpcId: VPC,
      SecurityGroupEgress: [
        Match.objectLike({ CidrIp: "0.0.0.0/0", IpProtocol: "tcp", FromPort: 443, ToPort: 443 }),
      ],
    });
  });

  it("gets the ENI permissions a VPC-attached Lambda needs", () => {
    synthWith((stack) => connectorNetworkFromEnv(stack)).hasResourceProperties("AWS::IAM::Role", {
      ManagedPolicyArns: Match.arrayWith([
        Match.objectLike({
          "Fn::Join": Match.arrayWith([
            Match.arrayWith([":iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"]),
          ]),
        }),
      ]),
    });
  });

  it("is not attached at all when no network is given", () => {
    const template = synthWith(() => undefined);
    const [fn] = Object.values(template.findResources("AWS::Lambda::Function")) as {
      Properties: Record<string, unknown>;
    }[];
    expect(fn.Properties.VpcConfig).toBeUndefined();
    template.resourceCountIs("AWS::EC2::SecurityGroup", 0);
  });
});
