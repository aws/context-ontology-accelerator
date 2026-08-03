#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Full CDK deployment — preflight, build, synth, deploy.
# CDK handles stack ordering via addDependency() in bin/app.ts.
set -euo pipefail

ENV="${1:-dev}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Optional context overrides via environment variables:
#   SCL_PREFIX=myproj  make deploy-dev
#   SCL_VPC_ID=vpc-abc make deploy-dev
# Custom domains (all-or-nothing — set all five or none):
#   SCL_UI_DOMAIN, SCL_UI_CERT_ARN (us-east-1),
#   SCL_API_DOMAIN, SCL_API_CERT_ARN (API region), SCL_HOSTED_ZONE_ID
CONTEXT="--context env=$ENV"
[ -n "${SCL_PREFIX:-}" ]        && CONTEXT="$CONTEXT --context resource_prefix=$SCL_PREFIX"
[ -n "${SCL_PROJECT_TAG:-}" ]   && CONTEXT="$CONTEXT --context project_tag=$SCL_PROJECT_TAG"
[ -n "${SCL_VPC_ID:-}" ]        && CONTEXT="$CONTEXT --context vpc_id=$SCL_VPC_ID"
[ -n "${SCL_UI_DOMAIN:-}" ]     && CONTEXT="$CONTEXT --context ui_domain=$SCL_UI_DOMAIN"
[ -n "${SCL_UI_CERT_ARN:-}" ]   && CONTEXT="$CONTEXT --context ui_cert_arn=$SCL_UI_CERT_ARN"
[ -n "${SCL_API_DOMAIN:-}" ]    && CONTEXT="$CONTEXT --context api_domain=$SCL_API_DOMAIN"
[ -n "${SCL_API_CERT_ARN:-}" ]  && CONTEXT="$CONTEXT --context api_cert_arn=$SCL_API_CERT_ARN"
[ -n "${SCL_HOSTED_ZONE_ID:-}" ] && CONTEXT="$CONTEXT --context hosted_zone_id=$SCL_HOSTED_ZONE_ID"

# ── Resolve VPC peering context from test-databases stack (if deployed) ──
# Prefix default matches the CDK app (see DEFAULT_RESOURCE_PREFIX).
TEST_STACK_NAME="${SCL_PREFIX:-coa}-integ-test-databases"
if [ -z "${SCL_JDBC_PEER_VPC_ID:-}" ]; then
  SCL_JDBC_PEER_VPC_ID=$(aws cloudformation describe-stacks \
    --stack-name "$TEST_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='VpcId'].OutputValue" \
    --output text 2>/dev/null || echo "")
fi
if [ -z "${SCL_JDBC_PEER_CIDRS:-}" ]; then
  SCL_JDBC_PEER_CIDRS=$(aws cloudformation describe-stacks \
    --stack-name "$TEST_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='VpcCidr'].OutputValue" \
    --output text 2>/dev/null || echo "")
fi
if [ -n "${SCL_JDBC_PEER_VPC_ID:-}" ] && [ "$SCL_JDBC_PEER_VPC_ID" != "None" ] \
  && [ -n "${SCL_JDBC_PEER_CIDRS:-}" ] && [ "$SCL_JDBC_PEER_CIDRS" != "None" ]; then
  echo "Peering into test VPC ${SCL_JDBC_PEER_VPC_ID} (${SCL_JDBC_PEER_CIDRS})"
  CONTEXT="$CONTEXT --context jdbc_peer_vpc_id=$SCL_JDBC_PEER_VPC_ID --context jdbc_peer_cidrs=$SCL_JDBC_PEER_CIDRS"
else
  # The stack is optional, so skipping is normal — but say so. Silence here is
  # indistinguishable from a prefix mismatch resolving the wrong stack name, which
  # is how this went unnoticed while the default was 'scl'.
  echo "No JDBC peering context (stack ${TEST_STACK_NAME} not found or has no VPC outputs) — skipping"
fi

# ponytail: CDK_DOCKER selection — only pick Finch if its daemon is actually
# reachable, matching preflight's check. Otherwise CDK falls back to docker.
# (A bare `command -v finch` would pin a down daemon and fail preflight even
# when Docker is up.)
if [ -z "${CDK_DOCKER:-}" ]; then
  if command -v finch >/dev/null 2>&1 && finch info >/dev/null 2>&1; then
    export CDK_DOCKER=finch
  fi
fi

echo "=== Deploying to $ENV ==="
echo "CDK context: $CONTEXT"

cd "$REPO_ROOT"

# ── Preflight ────────────────────────────────────────────────────────────
echo ""
./scripts/preflight-deploy.sh

# ── Build ────────────────────────────────────────────────────────────────
echo ""
echo "Building all packages..."
pnpm nx run-many -t build

# ── CDK synth + deploy ───────────────────────────────────────────────────
echo ""
echo "Synthesizing CDK stacks..."
pnpm --filter coa-infra exec cdk synth $CONTEXT >/dev/null

echo ""
echo "Deploying all stacks..."
pnpm --filter coa-infra exec cdk deploy --all $CONTEXT --require-approval=never

echo ""
echo "=== Deployment to $ENV complete ==="
