#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Print a one-line diagnosis of the Region quotas a new VPC consumes, for the deploy
# preflight. Exit 0 = OK, 1 = the region has no room for one of them, 2 = could not check
# (the preflight warns).
#
#   check-vpc-limit.sh REGION NETWORK_STACK
#
# The limits only matter when the deploy creates a VPC. It creates none when
# SCL_VPC_ID imports one, or on an update, which reuses the network stack's VPC.
set -uo pipefail

REGION="$1"
NETWORK_STACK="$2"

if [ -n "${SCL_VPC_ID:-}" ]; then
  echo "Network quota check skipped: SCL_VPC_ID imports $SCL_VPC_ID"
  exit 0
fi

if ! command -v aws >/dev/null 2>&1 || ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "AWS credentials not available — skipping network quota check"
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
    echo "Network quota check skipped: update reuses the VPC in $NETWORK_STACK"
    exit 0
    ;;
esac

# A new VPC takes one VPC, one Internet gateway and, for its NAT gateway, one
# Elastic IP: each a Region quota, default 5. A quota that can't be read (no
# servicequotas permission, throttling) is skipped with a warning, not guessed:
# accounts often have it raised.
is_number() { [[ "$1" =~ ^[0-9]+$ ]]; }

# Each check appends to these lists; the leading separator is stripped when printed.
USAGE="" FULL="" UNCHECKED=""

check_quota() {
  local name="$1" service="$2" quota_code="$3" describe_command="$4" list_field="$5"
  local count limit

  count=$(aws ec2 "$describe_command" --region "$REGION" \
    --query "length($list_field)" --output text 2>/dev/null)
  if ! is_number "$count"; then
    UNCHECKED+="; could not count $name"
    return
  fi

  limit=$(aws service-quotas get-service-quota --region "$REGION" \
    --service-code "$service" --quota-code "$quota_code" \
    --query 'Quota.Value' --output text 2>/dev/null)
  limit="${limit%%.*}" # the API returns e.g. 5.0
  if ! is_number "$limit"; then
    UNCHECKED+="; could not read the $name quota ($quota_code)"
    USAGE+=", $name $count/?"
    return
  fi

  USAGE+=", $name $count/$limit"
  if [ "$count" -ge "$limit" ]; then
    FULL+=", $name $count/$limit ($quota_code)"
  fi
}

#           name                service quota code  EC2 describe command        list field
check_quota "VPCs"              vpc     L-F678F1CE  describe-vpcs               Vpcs
check_quota "Internet gateways" vpc     L-A4707A72  describe-internet-gateways  InternetGateways
check_quota "Elastic IPs"       ec2     L-0263D0A3  describe-addresses          Addresses

if [ -n "$FULL" ]; then
  not_checked=""
  [ -n "$UNCHECKED" ] && not_checked=" Not checked: ${UNCHECKED#; }."
  echo "Network quota reached in $REGION: ${FULL#, }. The deploy creates one VPC, one Internet gateway and one Elastic IP. Release unused ones or request a quota increase before deploying.$not_checked"
  exit 1
fi
if [ -n "$UNCHECKED" ]; then
  counted="${USAGE#, }"
  echo "Network quota check incomplete in $REGION: ${UNCHECKED#; } — skipping those checks (${counted:-nothing else counted})"
  exit 2
fi
echo "Network quota headroom in $REGION: ${USAGE#, }"
