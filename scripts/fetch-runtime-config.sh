#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Fetches stack outputs from a deployed SCL environment and writes
# packages/web-app/public/runtime-config.json for local development.
#
# Usage: ./scripts/fetch-runtime-config.sh --region us-east-1 --profile my-profile
#        ./scripts/fetch-runtime-config.sh -r us-east-1 -p my-profile

set -euo pipefail

REGION=""
PROFILE=""
# Keep in sync with DEFAULT_RESOURCE_PREFIX in libs/ts-shared/src/constants.ts.
# Defaulting to "scl" while the CDK app defaults to "coa" made this script read
# outputs from a stack named scl-dev-* that a default deployment never creates,
# so runtime-config.json came back empty instead of failing loudly.
PREFIX="${SCL_PREFIX:-coa}"
ENV="dev"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -r|--region)  REGION="$2"; shift 2;;
    -p|--profile) PROFILE="$2"; shift 2;;
    --prefix)     PREFIX="$2"; shift 2;;
    --env)        ENV="$2"; shift 2;;
    *) echo "Unknown arg: $1"; exit 1;;
  esac
done

if [[ -z "$REGION" || -z "$PROFILE" ]]; then
  echo "Usage: $0 --region <region> --profile <profile> [--prefix scl] [--env dev]"
  exit 1
fi

AWS="aws --region $REGION --profile $PROFILE"
STACK_PREFIX="${PREFIX}-${ENV}"

get_output() {
  local stack="$1" key="$2"
  $AWS cloudformation describe-stacks --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue" \
    --output text
}

echo "Fetching outputs from ${STACK_PREFIX}-auth ..."
AUTHORITY=$(get_output "${STACK_PREFIX}-auth" "Issuer" 2>/dev/null || true)
CLIENT_ID=$(get_output "${STACK_PREFIX}-auth" "UserPoolClientId" 2>/dev/null || true)
USER_POOL_ID=$(get_output "${STACK_PREFIX}-auth" "UserPoolId" 2>/dev/null || true)

# If no OIDC Issuer output, build it from UserPoolId (Cognito mode)
if [[ -z "$AUTHORITY" && -n "$USER_POOL_ID" ]]; then
  AUTHORITY="https://cognito-idp.${REGION}.amazonaws.com/${USER_POOL_ID}"
fi

echo "Fetching outputs from ${STACK_PREFIX}-api ..."
API_ENDPOINT=$(get_output "${STACK_PREFIX}-api" "ApiEndpoint" 2>/dev/null || true)

echo "Fetching outputs from ${STACK_PREFIX}-serve ..."
SERVE_RUNTIME_ARN=$(get_output "${STACK_PREFIX}-serve" "AgentRuntimeArn" 2>/dev/null || true)

if [[ -z "$AUTHORITY" || -z "$CLIENT_ID" ]]; then
  echo "ERROR: Could not resolve authority or clientId from ${STACK_PREFIX}-auth stack."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_FILE="${SCRIPT_DIR}/../packages/web-app/public/runtime-config.json"

cat > "$OUT_FILE" <<EOF
{
  "region": "${REGION}",
  "stage": "${ENV}",
  "authority": "${AUTHORITY}",
  "clientId": "${CLIENT_ID}",
  "apiEndpoint": "${API_ENDPOINT}",
  "serveRuntimeArn": "${SERVE_RUNTIME_ARN}"
}
EOF

echo "✅ Written to $(realpath "$OUT_FILE")"
