// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";
import { ConnectorNetwork } from "./athena-federation-connector";
import { optionalEnv } from "./env";

/** Environment variables that attach a connector to a VPC. */
export const NETWORK_VARS = {
  vpcId: "CONNECTOR_VPC_ID",
  subnetIds: "CONNECTOR_SUBNET_IDS",
  securityGroupIds: "CONNECTOR_SECURITY_GROUP_IDS",
} as const;

const VPC_ID = /^vpc-[0-9a-f]{8,17}$/;
const SUBNET_ID = /^subnet-[0-9a-f]{8,17}$/;
const SECURITY_GROUP_ID = /^sg-[0-9a-f]{8,17}$/;

/**
 * The connector's VPC attachment, from `CONNECTOR_VPC_ID`, `CONNECTOR_SUBNET_IDS` and, optionally,
 * `CONNECTOR_SECURITY_GROUP_IDS`. The two lists are comma-separated.
 *
 * All or nothing: a VPC without subnets, or subnets or security groups without a VPC, is refused
 * rather than deployed half-attached.
 *
 * The VPC is built from its id alone, with no lookup, so synth needs no credentials and writes nothing
 * to `cdk.context.json`. Lambda reads only the subnet and security-group ids.
 *
 * @param options `required` refuses an unset VPC; `hint` is appended to that error.
 * @returns the attachment, or undefined when none is configured and none is required.
 */
export function connectorNetworkFromEnv(
  scope: Construct,
  options: { required: true; hint?: string },
): ConnectorNetwork;
export function connectorNetworkFromEnv(
  scope: Construct,
  options?: { required?: boolean; hint?: string },
): ConnectorNetwork | undefined;
export function connectorNetworkFromEnv(
  scope: Construct,
  options: { required?: boolean; hint?: string } = {},
): ConnectorNetwork | undefined {
  const vpcId = optionalEnv(NETWORK_VARS.vpcId);
  const subnetIds = idList(NETWORK_VARS.subnetIds, SUBNET_ID, "subnet");
  const securityGroupIds = idList(NETWORK_VARS.securityGroupIds, SECURITY_GROUP_ID, "security group");

  if (vpcId === undefined) {
    const stray = [
      subnetIds.length > 0 ? NETWORK_VARS.subnetIds : undefined,
      securityGroupIds.length > 0 ? NETWORK_VARS.securityGroupIds : undefined,
    ].filter((name) => name !== undefined);
    if (stray.length > 0) {
      throw new Error(`${stray.join(" and ")} set without ${NETWORK_VARS.vpcId}.`);
    }
    if (options.required === true) {
      throw new Error(
        `${NETWORK_VARS.vpcId} and ${NETWORK_VARS.subnetIds} are not set, and this connector ` +
          "must run in a VPC." +
          (options.hint === undefined ? "" : `\n${options.hint}`),
      );
    }
    return undefined;
  }
  if (!VPC_ID.test(vpcId)) {
    throw new Error(`${NETWORK_VARS.vpcId} "${vpcId}" is not a VPC id.`);
  }
  if (subnetIds.length === 0) {
    throw new Error(
      `${NETWORK_VARS.vpcId} is set but ${NETWORK_VARS.subnetIds} is not. Name the private ` +
        "subnets with egress the connector should run in.",
    );
  }

  const vpc = ec2.Vpc.fromVpcAttributes(scope, "ConnectorVpc", {
    vpcId,
    // Required by the call and never read for a Lambda.
    availabilityZones: cdk.Fn.getAzs(),
  });
  return {
    vpc,
    subnets: subnetIds.map((id) => ec2.Subnet.fromSubnetId(scope, `ConnectorSubnet-${id}`, id)),
    securityGroups:
      securityGroupIds.length === 0
        ? undefined
        : securityGroupIds.map((id) =>
            ec2.SecurityGroup.fromSecurityGroupId(scope, `ConnectorSecurityGroup-${id}`, id),
          ),
  };
}

/** @returns the variable's comma-separated ids, deduplicated. @throws Error on a malformed id. */
function idList(name: string, pattern: RegExp, noun: string): string[] {
  const ids: string[] = [];
  for (const entry of (optionalEnv(name) ?? "").split(",")) {
    const id = entry.trim();
    if (id.length === 0) {
      continue;
    }
    if (!pattern.test(id)) {
      throw new Error(`${name} entry "${id}" is not a ${noun} id.`);
    }
    if (!ids.includes(id)) {
      ids.push(id);
    }
  }
  return ids;
}
