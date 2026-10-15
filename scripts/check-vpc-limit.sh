#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Print a one-line VPC quota diagnosis for the deploy preflight.
# Exit 0 = OK, 1 = the region has no room for the VPC this deploy creates,
# 2 = could not check (the preflight warns).
#
#   check-vpc-limit.sh REGION NETWORK_STACK
#
# The limit only matters when the deploy creates a VPC. It creates none when
# SCL_VPC_ID imports one, or on an update, which reuses the network stack's VPC.
set -uo pipefail

REGION="$1"
NETWORK_STACK="$2"

if [ -n "${SCL_VPC_ID:-}" ]; then
  echo "VPC limit check skipped: SCL_VPC_ID imports $SCL_VPC_ID"
  exit 0
fi

if ! command -v aws >/dev/null 2>&1 || ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "AWS credentials not available — skipping VPC limit check"
  exit 2
fi

# Empty when the stack doesn't exist or can't be read: checked as a fresh deploy.
# Only settled states count. A VPC that was deleted (the first deploy failed and
# rolled back) or never created (CREATE_FAILED) doesn't: the next deploy creates
# a new one. In-progress and failed states are checked too.
VPC_STATUS=$(aws cloudformation list-stack-resources --region "$REGION" \
  --stack-name "$NETWORK_STACK" \
  --query "StackResourceSummaries[?ResourceType=='AWS::EC2::VPC'].ResourceStatus | [0]" \
  --output text 2>/dev/null || true)
case "$VPC_STATUS" in
  CREATE_COMPLETE | UPDATE_COMPLETE | UPDATE_ROLLBACK_COMPLETE | IMPORT_COMPLETE | IMPORT_ROLLBACK_COMPLETE)
    echo "VPC limit check skipped: update reuses the VPC in $NETWORK_STACK"
    exit 0
    ;;
esac

VPC_COUNT=$(aws ec2 describe-vpcs --region "$REGION" --query 'length(Vpcs)' --output text 2>/dev/null || echo "?")
VPC_LIMIT_RAW=$(aws service-quotas get-service-quota --service-code vpc --quota-code L-F678F1CE --region "$REGION" --query 'Quota.Value' --output text 2>/dev/null || echo "5")
VPC_LIMIT="${VPC_LIMIT_RAW%%.*}"
[[ "$VPC_LIMIT" =~ ^[0-9]+$ ]] || VPC_LIMIT=5

if ! [[ "$VPC_COUNT" =~ ^[0-9]+$ ]]; then
  echo "Could not count VPCs in $REGION — skipping VPC limit check"
  exit 2
fi
if [ "$VPC_COUNT" -ge "$VPC_LIMIT" ]; then
  echo "VPC limit reached: $VPC_COUNT/$VPC_LIMIT VPCs in $REGION. Delete unused VPCs or request a quota increase before deploying."
  exit 1
fi
echo "VPC headroom: $VPC_COUNT/$VPC_LIMIT used in $REGION"
