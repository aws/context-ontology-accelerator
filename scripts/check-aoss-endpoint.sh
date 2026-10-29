#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Print a one-line diagnosis of the OpenSearch Serverless endpoint for a deploy
# into an existing VPC. Exit 0 = OK (or not applicable), 1 = the endpoint can't
# serve the collection, 2 = could not check (the preflight warns).
#
#   check-aoss-endpoint.sh REGION
#
# With SCL_VPC_ID set, SCL_AOSS_VPC_ENDPOINT_ID becomes the only network path
# the collection's network policy allows. Synth checks only its format, so a
# typo or the wrong endpoint would deploy cleanly and fail at runtime; an
# endpoint in another VPC would also open the collection to that VPC.
set -uo pipefail

REGION="$1"

# Trim surrounding whitespace, as synth does with the same values, so a stray
# space from a copy-paste or .env file is treated the same in both places.
trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  printf '%s' "${s%"${s##*[![:space:]]}"}"
}
VPC_ID="$(trim "${SCL_VPC_ID:-}")"
ENDPOINT_ID="$(trim "${SCL_AOSS_VPC_ENDPOINT_ID:-}")"

if [ -z "$VPC_ID" ]; then
  if [ -n "$ENDPOINT_ID" ]; then
    echo "SCL_AOSS_VPC_ENDPOINT_ID is set without SCL_VPC_ID: it only applies to a deploy into an existing VPC"
    exit 1
  fi
  echo "OpenSearch Serverless endpoint check skipped: COA creates its own VPC and endpoint"
  exit 0
fi

if [ -z "$ENDPOINT_ID" ]; then
  echo "SCL_VPC_ID requires SCL_AOSS_VPC_ENDPOINT_ID: the ID of the com.amazonaws.$REGION.aoss-data interface endpoint in $VPC_ID"
  exit 1
fi

if ! command -v aws >/dev/null 2>&1 || ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "AWS credentials not available — skipping OpenSearch Serverless endpoint check"
  exit 2
fi

if ! DETAILS=$(aws ec2 describe-vpc-endpoints --region "$REGION" --vpc-endpoint-ids "$ENDPOINT_ID" \
  --query 'VpcEndpoints[0].[VpcId,ServiceName,PrivateDnsEnabled,State]' --output text 2>&1); then
  # Only EC2 saying the ID is wrong fails the deploy. A missing
  # ec2:DescribeVpcEndpoints permission, throttling or a network error says
  # nothing about the endpoint, so the preflight only warns.
  case "$DETAILS" in
    *InvalidVpcEndpointId.NotFound*)
      echo "SCL_AOSS_VPC_ENDPOINT_ID $ENDPOINT_ID was not found in $REGION"
      exit 1
      ;;
    *InvalidVpcEndpointId.Malformed*)
      echo "SCL_AOSS_VPC_ENDPOINT_ID $ENDPOINT_ID is not a valid VPC endpoint ID (vpce- followed by lowercase hex)"
      exit 1
      ;;
  esac
  echo "Could not check SCL_AOSS_VPC_ENDPOINT_ID $ENDPOINT_ID in $REGION — skipping: ${DETAILS##*: }"
  exit 2
fi
read -r EP_VPC EP_SERVICE EP_DNS EP_STATE <<<"$DETAILS"

PROBLEMS=()
[ "$EP_VPC" = "$VPC_ID" ] || PROBLEMS+=("is in $EP_VPC, not SCL_VPC_ID $VPC_ID")
[ "$EP_SERVICE" = "com.amazonaws.$REGION.aoss-data" ] || PROBLEMS+=("is for $EP_SERVICE, not com.amazonaws.$REGION.aoss-data")
[ "$EP_DNS" = "True" ] || PROBLEMS+=("has private DNS disabled")
[ "$EP_STATE" = "available" ] || PROBLEMS+=("is $EP_STATE, not available")

if [ ${#PROBLEMS[@]} -gt 0 ]; then
  MSG="SCL_AOSS_VPC_ENDPOINT_ID $ENDPOINT_ID"
  for p in "${PROBLEMS[@]}"; do MSG="$MSG; $p"; done
  echo "${MSG/;/}"
  exit 1
fi
echo "OpenSearch Serverless endpoint $ENDPOINT_ID: aoss-data in $VPC_ID, private DNS on, available"
