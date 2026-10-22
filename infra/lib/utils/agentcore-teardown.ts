// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { resolveContext } from "../context";

/** Value of the `<prefix>:teardown` tag on a retained AgentCore Runtime security group. */
const RETAINED_SG_TAG_VALUE = "delete-after-eni-release";

/**
 * Keep an AgentCore Runtime's security group out of CloudFormation's deletes, rollback and
 * replacement included: its ENIs hold it for up to 8 hours after the runtime is deleted, and
 * a serve stack failing on it keeps storage, authnz and network alive. scripts/destroy.sh
 * deletes it later. Safe in dev: the name is generated, so a redeploy never collides. The tag
 * makes existing stacks apply the DeletionPolicy, which alone CloudFormation ignores.
 */
export function retainAgentCoreSecurityGroup(sg: ec2.SecurityGroup): void {
  sg.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
  cdk.Tags.of(sg).add(
    `${resolveContext(sg.node).prefix}:teardown`,
    RETAINED_SG_TAG_VALUE,
  );
}
