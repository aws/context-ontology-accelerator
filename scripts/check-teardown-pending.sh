#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Print a one-line diagnosis for the deploy preflight: is a teardown of this
# deployment still finishing? Exit 0 = OK, 1 = the deploy would fail (the
# preflight errors), 2 = could not check (the preflight warns).
#
#   check-teardown-pending.sh REGION PREFIX ENV
#
# AgentCore keeps the runtimes' ENIs for up to 8 hours, so `make destroy-dev`
# leaves the network stack mid-delete or DELETE_FAILED until a later run
# finishes it, and CloudFormation cannot deploy over either state.
set -uo pipefail

REGION="$1"
PREFIX="$2"
DEPLOY_ENV="$3"
STACK_PREFIX="${PREFIX}-${DEPLOY_ENV}"
NETWORK_STACK="${STACK_PREFIX}-network"

if ! command -v aws >/dev/null 2>&1 || ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "AWS credentials not available — skipping pending-teardown check"
  exit 2
fi

if ! STATUS=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$NETWORK_STACK" \
    --query 'Stacks[0].StackStatus' --output text 2>&1); then
  if [[ "$STATUS" != *"does not exist"* ]]; then
    echo "Could not check for a pending teardown of $STACK_PREFIX: $STATUS"
    exit 2
  fi
  STATUS=NONE
fi
case "$STATUS" in
  DELETE_IN_PROGRESS | DELETE_FAILED) ;;
  *)
    echo "No teardown of $STACK_PREFIX pending"
    exit 0
    ;;
esac

# destroy.sh tags the runtime security groups with the time it deleted the
# runtimes; AWS releases the ENIs up to 8 hours after that.
DELETED_AT=$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=group-name,Values=${STACK_PREFIX}-serve-AgentCoreSG*,${STACK_PREFIX}-mcp-McpSG*" \
  --query "SecurityGroups[].Tags[] | [?Key=='${PREFIX}:runtime-deleted-at'].Value | [0]" \
  --output text 2>/dev/null || echo "")
WHEN=""
if [[ "$DELETED_AT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z$ ]]; then
  ETA=$(python3 -c "
import datetime, sys
deleted = datetime.datetime.strptime(sys.argv[1], '%Y-%m-%dT%H:%M:%SZ')
print((deleted + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M UTC'))
" "$DELETED_AT" 2>/dev/null || echo "")
  [ -n "$ETA" ] && WHEN=" AWS releases the AgentCore ENIs that hold it around $ETA."
fi

echo "A teardown of $STACK_PREFIX has not finished: $NETWORK_STACK is $STATUS, and CloudFormation cannot deploy over it.${WHEN} Deploy under another SCL_PREFIX, or once the teardown can finish run 'SCL_PREFIX=$PREFIX AWS_REGION=$REGION ./scripts/destroy.sh $DEPLOY_ENV' again and then deploy."
exit 1
