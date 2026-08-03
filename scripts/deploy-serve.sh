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

# Resolution order matches CDK's own precedence (infra/bin/app.ts uses
# CDK_DEFAULT_REGION as the source of truth), so this script pushes the
# ECR image to the same region CDK will deploy the stack into.
REGION="${CDK_DEFAULT_REGION:-${AWS_DEFAULT_REGION:-${AWS_REGION:-$(aws configure get region 2>/dev/null || echo us-east-1)}}}"
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
echo "--- Deploying CDK stack ---"

CONTEXT="--context env=${ENV} --context context_manager_image_uri=${IMAGE_FULL}"
[ -n "${SCL_PREFIX:-}" ] && CONTEXT="${CONTEXT} --context resource_prefix=${SCL_PREFIX}"

APPROVAL="--require-approval=never"
if [ "${ENV}" = "prod" ] || [ "${ENV}" = "production" ]; then
  APPROVAL="--require-approval=broadening"
fi

pnpm --filter coa-infra exec cdk deploy "${PREFIX}-${ENV}-serve" \
  ${CONTEXT} \
  ${APPROVAL}

# ── Smoke Test ───────────────────────────────────────────────────────────────

echo ""
echo "--- Smoke test ---"

RUNTIME_ARN=$(aws cloudformation describe-stacks \
  --stack-name "${PREFIX}-${ENV}-serve" \
  --query 'Stacks[0].Outputs[?OutputKey==`AgentRuntimeArn`].OutputValue' \
  --output text 2>/dev/null || echo "")

# The serve stack exports AgentRuntimeArn / AgentRuntimeId / AgentRuntimeName —
# there is no AgentRuntimeEndpointName output. Reading it always yielded an empty
# qualifier, and because every failure below was redirected to /dev/null the smoke
# test "passed" while verifying nothing. DEFAULT is the qualifier AgentCore
# creates alongside a runtime.
ENDPOINT_NAME="${AGENT_RUNTIME_QUALIFIER:-DEFAULT}"

if [ -z "${RUNTIME_ARN}" ] || [ "${RUNTIME_ARN}" = "None" ]; then
  echo "  FAILED: stack ${PREFIX}-${ENV}-serve has no AgentRuntimeArn output" >&2
  exit 1
fi

HEALTH_PAYLOAD=$(mktemp)
RESPONSE_FILE=$(mktemp)
INVOKE_LOG=$(mktemp)
echo '{"query":"ping","namespace":"smoke-test"}' > "${HEALTH_PAYLOAD}"

SMOKE_OK=0
# Allow warm-up time — retry up to 3 times
for attempt in 1 2 3; do
  if aws bedrock-agentcore invoke-agent-runtime \
    --agent-runtime-arn "${RUNTIME_ARN}" \
    --qualifier "${ENDPOINT_NAME}" \
    --content-type "application/json" \
    --payload "fileb://${HEALTH_PAYLOAD}" \
    "${RESPONSE_FILE}" > "${INVOKE_LOG}" 2>&1; then
    echo "  Health: $(cat "${RESPONSE_FILE}")"
    SMOKE_OK=1
    break
  fi
  echo "  attempt ${attempt}/3 failed: $(tail -n 3 "${INVOKE_LOG}")" >&2
  [ "${attempt}" -lt 3 ] && sleep 10
done

rm -f "${HEALTH_PAYLOAD}" "${RESPONSE_FILE}" "${INVOKE_LOG}"

# Fail loudly. A deploy script whose verification cannot fail is not a
# verification.
if [ "${SMOKE_OK}" -ne 1 ]; then
  echo "  FAILED: smoke test could not invoke the runtime after 3 attempts" >&2
  exit 1
fi

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "=== Serve Layer Deployment Complete ==="
echo "  Stack:    ${PREFIX}-${ENV}-serve"
echo "  Image:    ${IMAGE_FULL}"
if [ -n "${RUNTIME_ARN}" ] && [ "${RUNTIME_ARN}" != "None" ]; then
  echo "  Runtime:  ${RUNTIME_ARN}"
  echo "  Endpoint: ${ENDPOINT_NAME}"
  echo ""
  echo "Invoke with:"
  echo "  echo '{\"query\":\"...\",\"namespace\":\"...\"}' > payload.json"
  echo "  aws bedrock-agentcore invoke-agent-runtime \\"
  echo "    --agent-runtime-arn '${RUNTIME_ARN}' \\"
  echo "    --qualifier '${ENDPOINT_NAME}' \\"
  echo "    --content-type 'application/json' \\"
  echo "    --payload 'fileb://payload.json' \\"
  echo "    response.json"
fi
