// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import {
  EC2Client,
  DescribeAvailabilityZonesCommand,
  DescribeVpcEndpointServicesCommand,
} from "@aws-sdk/client-ec2";

/**
 * Regions where AgentCore does NOT support all AZs, requiring explicit
 * filtering. Currently only us-east-1 (3 of 6 AZs supported). All other
 * AgentCore-supported regions have full AZ coverage — the default
 * ``maxAzs: 2`` is safe there and no resolution is needed.
 *
 * Source: cross-referenced against AgentCore VPC docs, accurate as of
 * mid-2026. If AgentCore restricts AZs in another region in the future,
 * add it here.
 */
export const AGENTCORE_RESTRICTED_AZ_IDS: Record<string, readonly string[]> = {
  "us-east-1": ["use1-az1", "use1-az2", "use1-az4"],
};

/**
 * Resolve AgentCore-supported AZ **names** for the deploying account in
 * a restricted region, by querying ``ec2:DescribeAvailabilityZones`` and
 * filtering to the supported AZ-IDs.
 *
 * Returns ``undefined`` for regions not in ``AGENTCORE_RESTRICTED_AZ_IDS``
 * — those regions support all AZs and need no filtering.
 *
 * **Fails loud** if called for a restricted region and resolution fails
 * (missing creds, API error, fewer than ``count`` supported AZs). This
 * is intentional: silently falling back to default AZs in a restricted
 * region reintroduces the exact bug this module prevents.
 */
export async function resolveAgentCoreAzNames(
  region: string,
  count = 2,
  client?: Pick<EC2Client, "send">,
  requiredAlsoIn?: readonly string[],
): Promise<string[] | undefined> {
  const restrictedIds = AGENTCORE_RESTRICTED_AZ_IDS[region];
  if (!restrictedIds) {
    return undefined;
  }

  const ec2Client = client ?? new EC2Client({ region });
  const resp = await ec2Client.send(
    new DescribeAvailabilityZonesCommand({
      Filters: [{ Name: "region-name", Values: [region] }],
    }),
  );

  const supportedIdSet = new Set(restrictedIds);
  let names = (resp.AvailabilityZones ?? [])
    .filter(
      (az) =>
        az.ZoneName &&
        az.ZoneId &&
        az.ZoneType === "availability-zone" &&
        supportedIdSet.has(az.ZoneId),
    )
    .map((az) => az.ZoneName as string)
    .sort();

  // Optionally narrow to AZs ALSO supported by another service (e.g. the
  // OpenSearch Serverless control-plane VPC endpoint, offered in only a
  // subset of AZs). Intersect BEFORE the count check + slice so the
  // ">= count" guarantee holds against the actually-usable set rather than
  // the AgentCore-only set — otherwise the VPC can land on an AZ with no
  // AOSS endpoint (e.g. us-east-1a) and the endpoint create fails.
  if (requiredAlsoIn && requiredAlsoIn.length > 0) {
    const alsoIn = new Set(requiredAlsoIn);
    names = names.filter((n) => alsoIn.has(n));
  }

  if (names.length < count) {
    throw new Error(
      `Region ${region} resolved only ${names.length} AgentCore-supported ` +
        `AZ(s) (${names.join(", ") || "none"}) for this account; ` +
        `requires at least ${count}. Verify with ` +
        `"aws ec2 describe-availability-zones --region ${region}".`,
    );
  }

  return names.slice(0, count);
}

/**
 * Resolve the AZ **names** in which an interface VPC endpoint service is
 * offered for the deploying account (via ``ec2:DescribeVpcEndpointServices``).
 *
 * Endpoint services are offered in only a subset of a region's AZs (e.g. the
 * OpenSearch Serverless control-plane service ``com.amazonaws.<region>.aoss``
 * is not offered in every AZ). Pair with ``resolveAgentCoreAzNames``'s
 * ``requiredAlsoIn`` argument to pin the VPC to AZs valid for both.
 *
 * Returns ``undefined`` when the service is not found or lists no AZs, so
 * callers can treat it as "no additional constraint" (passthrough).
 */
export async function resolveEndpointServiceAzNames(
  region: string,
  serviceName: string,
  client?: Pick<EC2Client, "send">,
): Promise<string[] | undefined> {
  const ec2Client = client ?? new EC2Client({ region });
  const resp = await ec2Client.send(
    new DescribeVpcEndpointServicesCommand({
      Filters: [{ Name: "service-name", Values: [serviceName] }],
    }),
  );
  const azs = resp.ServiceDetails?.[0]?.AvailabilityZones;
  return azs && azs.length > 0 ? [...azs].sort() : undefined;
}

/**
 * Filter VPC subnets to those in AgentCore-supported AZs.
 *
 * Pass the result to ``RuntimeNetworkConfiguration.usingVpc({
 * vpcSubnets: { subnets } })``. Defense-in-depth at the AgentCore call
 * site even when NetworkStack already pins the VPC — catches imported
 * VPCs and future regressions.
 *
 * Falls back to passthrough when ``supportedAzNames`` is
 * undefined/empty (unrestricted region or env-agnostic synth).
 * Throws if a non-empty set filters every subnet away.
 */
export function agentCoreSupportedSubnets(
  vpc: ec2.IVpc,
  supportedAzNames: readonly string[] | undefined,
  subnetType: ec2.SubnetType = ec2.SubnetType.PRIVATE_WITH_EGRESS,
): ec2.ISubnet[] {
  const all = vpc.selectSubnets({ subnetType }).subnets;

  if (!supportedAzNames || supportedAzNames.length === 0) {
    return all;
  }

  const supported = new Set(supportedAzNames);
  const filtered = all.filter((s) => {
    if (cdk.Token.isUnresolved(s.availabilityZone)) {
      return true;
    }
    return supported.has(s.availabilityZone);
  });

  if (filtered.length === 0) {
    throw new Error(
      `No AgentCore-supported subnets found in VPC ${vpc.vpcId}. ` +
        `Supported AZ names for this account: ` +
        `[${[...supported].sort().join(", ")}]. ` +
        `Check NetworkStack's availabilityZones configuration.`,
    );
  }
  return filtered;
}
