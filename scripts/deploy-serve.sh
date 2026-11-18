#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

# Deploy the Serve layer (Context Manager on AgentCore Runtime).
#
# Steps:
#   1. Build arm64 Docker image and push to ECR
#   2. CDK deploy serve stack with the image URI as context
#      (CDK creates/updates CfnRuntime + CfnRuntimeEndpoint)
#
# Prerequisites:
#   - AWS credentials configured
#   - Docker with buildx (arm64 emulation via QEMU or native)
#   - pnpm installed (for CDK)
#   - The rest of the environment already deployed (`make deploy-dev`); only the
#     serve stack is updated (--exclusively)
#   - The same SCL_* variables used for `make deploy-dev`: CDK context is not
#     persisted, so an unset one goes back to its default
#
# Usage:
#   ./scripts/deploy-serve.sh [env]          # default: dev
#   SKIP_BUILD=1 ./scripts/deploy-serve.sh   # skip Docker build, redeploy with last image
#   IMAGE_TAG=v1.2.3 ./scripts/deploy-serve.sh

ENV="${1:-dev}"
# Default MUST match the CDK app's DEFAULT_RESOURCE_PREFIX
# (libs/ts-shared/src/constants.ts, resolved in infra/lib/context.ts).
# A mismatch means this script resolves scl-* names / /scl SSM parameters
# while `cdk` operates on coa-* — every lookup silently misses.
PREFIX="${SCL_PREFIX:-coa}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

ECR_REPO="${PREFIX}-${ENV}-context-manager"
# Default to short git SHA for immutable image references
IMAGE_TAG="${IMAGE_TAG:-$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo latest)}"

# Resolved the way the CDK CLI resolves it, the profile's region included, so
# this script pushes the ECR image to the same region CDK will deploy the
# stack into.
# shellcheck source=lib/resolve-region.sh source-path=SCRIPTDIR
source "${REPO_ROOT}/scripts/lib/resolve-region.sh"
REGION="$(coa_resolve_region)"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}"
IMAGE_FULL="${ECR_URI}:${IMAGE_TAG}"

echo "=== Deploying Serve Layer (Context Manager) ==="
echo "  Environment: ${ENV}"
echo "  Image:       ${IMAGE_FULL}"
echo "  Region:      ${REGION}"
echo ""

cd "${REPO_ROOT}"

# ── Docker Build & Push ──────────────────────────────────────────────────────

if [ "${SKIP_BUILD:-}" != "1" ]; then
  echo "--- Building arm64 Docker image ---"

  # Ensure ECR repo exists
  aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${REGION}" > /dev/null 2>&1 || \
    aws ecr create-repository --repository-name "${ECR_REPO}" --image-scanning-configuration scanOnPush=true --region "${REGION}" > /dev/null

  # Login to ECR
  aws ecr get-login-password --region "${REGION}" | \
    docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

  # Ensure buildx builder with arm64 support
  BINFMT_TAG="qemu-v8.1.5"
  if ! docker buildx inspect arm64builder > /dev/null 2>&1; then
    docker run --privileged --rm "tonistiigi/binfmt:${BINFMT_TAG}" --install arm64 > /dev/null 2>&1 || true
    docker buildx create --name arm64builder --platform linux/arm64 > /dev/null 2>&1 || true
  fi
  docker buildx use arm64builder

  # Build and push
  docker buildx build \
    --platform linux/arm64 \
    -f packages/context-manager/Dockerfile \
    -t "${IMAGE_FULL}" \
    --push .

  echo "  Pushed: ${IMAGE_FULL}"
else
  echo "SKIP_BUILD=1 — using existing image ${IMAGE_FULL}"
fi

# ── CDK Deploy ───────────────────────────────────────────────────────────────

echo ""
echo "--- Installing locked Node dependencies ---"
# Same reason as scripts/deploy.sh: install what pnpm-lock.yaml pins, without
# rewriting it, before pnpm 11+ would run a plain install on its own.
pnpm install --frozen-lockfile

echo ""
echo "--- Deploying CDK stack ---"

CONTEXT=(--context "env=${ENV}" --context "context_manager_image_uri=${IMAGE_FULL}")
# Same SCL_* settings as deploy.sh, so the serve stack synthesizes the way it was created.
# shellcheck source=lib/cdk-context.sh source-path=SCRIPTDIR
source "${REPO_ROOT}/scripts/lib/cdk-context.sh"
# serve-stack reads the peering CIDRs too (cross-VPC JDBC egress on its security
# group), so leaving them out would delete those rules on every serve deploy.
coa_add_jdbc_peering_context

APPROVAL="--require-approval=never"
if [ "${ENV}" = "prod" ] || [ "${ENV}" = "production" ]; then
  APPROVAL="--require-approval=broadening"
fi

# --exclusively: only the serve stack. Without it CDK also deploys the stacks serve
# depends on (network, storage, auth, authnz, namespace), which would apply defaults
# for VPC, OpenSearch capacity and SMUS admin settings. Needs those stacks already
# deployed; use `make deploy-dev` for a fresh environment.
pnpm --filter coa-infra exec cdk deploy "${PREFIX}-${ENV}-serve" \
  --exclusively \
  "${CONTEXT[@]}" \
  ${APPROVAL}

# ── Post-deploy check ────────────────────────────────────────────────────────
# The runtime uses a JWT authorizer (serve-stack.ts), so a SigV4 invoke is rejected.
# Assert only that the runtime exists and is READY; end-to-end queries live in `make test-integ`.

echo ""
echo "--- Post-deploy check ---"

# shellcheck disable=SC2016  # JMESPath backtick literal, not a shell expansion
RUNTIME_ARN=$(aws cloudformation describe-stacks \
  --stack-name "${PREFIX}-${ENV}-serve" \
  --query 'Stacks[0].Outputs[?OutputKey==`AgentRuntimeArn`].OutputValue' \
  --output text 2>/dev/null || echo "")

if [ -z "${RUNTIME_ARN}" ] || [ "${RUNTIME_ARN}" = "None" ]; then
  echo "  FAILED: stack ${PREFIX}-${ENV}-serve has no AgentRuntimeArn output" >&2
  exit 1
fi
echo "  Runtime ARN: ${RUNTIME_ARN}"

RUNTIME_ID="${RUNTIME_ARN##*/}"
STATUS=""
for attempt in 1 2 3; do
  STATUS=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "${RUNTIME_ID}" \
    --query 'status' --output text 2>/dev/null || echo "")
  case "${STATUS}" in
    READY) break ;;
    CREATING|UPDATING) echo "  status=${STATUS}, waiting..." ;;
    "") echo "  could not read runtime status (attempt ${attempt}/3)" >&2 ;;
    *) echo "  status=${STATUS}" >&2 ;;
  esac
  [ "${attempt}" -lt 3 ] && sleep 10
done

if [ "${STATUS}" != "READY" ]; then
  echo "  FAILED: runtime ${RUNTIME_ID} is '${STATUS:-unknown}', expected READY" >&2
  echo "  (an end-to-end query needs an IdP token — see 'make test-integ')" >&2
  exit 1
fi
echo "  Runtime status: READY"

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "=== Serve Layer Deployment Complete ==="
echo "  Stack:    ${PREFIX}-${ENV}-serve"
echo "  Image:    ${IMAGE_FULL}"
echo "  Runtime:  ${RUNTIME_ARN}"
echo ""
# JWT authorizer: a bearer ID token is required.
echo "Invoke with a bearer ID token from the app's IdP:"
echo "  curl -X POST \\"
echo "    'https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${RUNTIME_ID}/invocations?qualifier=DEFAULT' \\"
echo "    -H \"Authorization: Bearer \${ID_TOKEN}\" \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"query\":\"...\",\"namespace\":\"...\"}'"
echo ""
echo "Or run the integ suite, which provisions a token for you:"
echo "  make test-integ"
