#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Full deploy: the platform stacks (scripts/deploy.sh) and the COA-operated
# Databricks connector (scripts/deploy-managed-databricks-connector.sh).
#
# The connector only needs the network, serve and sources stacks (it reads their
# SSM parameters), so on a first deploy it starts as soon as those exist instead
# of after the whole platform, and runs alongside the remaining platform stacks.
# On an update those stacks already exist before this deploy has touched them,
# so the connector runs after the platform deploy.
#
# Usage: ./scripts/deploy-all.sh [env]
set -euo pipefail

ENV="${1:-dev}"
PREFIX="${SCL_PREFIX:-coa}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Resolve the region the way the CDK/AWS CLI does (AWS_REGION wins) and pin it for
# both child scripts, so the stack checks, the platform deploy and the connector
# all target the same region.
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
REGION="${REGION:-us-east-1}"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION" CDK_DEFAULT_REGION="$REGION"

stack_exists() {
  local err
  err=$(aws cloudformation describe-stacks --region "$REGION" \
    --stack-name "${PREFIX}-${ENV}-$1" 2>&1 >/dev/null) && return 0
  [[ "$err" == *"does not exist"* ]] && return 1
  # Anything else (credentials, throttling, network) must not be mistaken for a
  # first deploy, which would run the connector alongside a platform update.
  echo "deploy-all: cannot check ${PREFIX}-${ENV}-$1, aborting: $err" >&2
  exit 1
}

if stack_exists sources || stack_exists serve; then
  "$SCRIPT_DIR/deploy.sh" "$ENV"
  "$SCRIPT_DIR/deploy-managed-databricks-connector.sh" "$ENV"
  exit
fi

# Background jobs in a non-interactive shell ignore SIGINT, so forward Ctrl-C:
# stop the whole process group (platform deploy and connector wait) together.
trap 'trap - INT TERM; kill 0' INT TERM
"$SCRIPT_DIR/deploy.sh" "$ENV" &
platform_pid=$!

stack_ready() {
  local status
  status=$(aws cloudformation describe-stacks --region "$REGION" \
    --stack-name "${PREFIX}-${ENV}-$1" --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || true)
  [[ "$status" == "CREATE_COMPLETE" || "$status" == "UPDATE_COMPLETE" ]]
}

(
  until stack_ready sources && stack_ready serve; do
    # Stop waiting if the platform deploy already ended without the stacks.
    if ! kill -0 "$platform_pid" 2>/dev/null; then
      stack_ready sources && stack_ready serve && break
      echo "[connector] platform deploy ended before sources/serve were ready; skipping" >&2
      exit 1
    fi
    sleep 20
  done
  echo "[connector] sources and serve are ready; deploying the Databricks connector"
  "$SCRIPT_DIR/deploy-managed-databricks-connector.sh" "$ENV" 2>&1 | sed 's/^/[connector] /'
) &
connector_pid=$!

platform_rc=0; wait "$platform_pid" || platform_rc=$?
connector_rc=0; wait "$connector_pid" || connector_rc=$?
echo "platform deploy exit=${platform_rc}, connector deploy exit=${connector_rc}"
exit $(( platform_rc != 0 ? platform_rc : connector_rc ))
