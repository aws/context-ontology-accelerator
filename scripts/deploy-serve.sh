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

# ── Post-deploy check ────────────────────────────────────────────────────────
#
# This deliberately does NOT invoke the runtime. The runtime is created with
# `RuntimeAuthorizerConfiguration.usingJWT(...)` (serve-stack.ts), so it accepts
# only a bearer ID token from the configured IdP — a SigV4
# `aws bedrock-agentcore invoke-agent-runtime` is rejected no matter how long the
# runtime has been warm. The previous version of this block appeared to invoke the
# runtime but read a CFN output that does not exist (`AgentRuntimeEndpointName`,
# while the stack exports AgentRuntimeArn / Id / Name), so the qualifier was always
# empty and every failure was redirected to /dev/null — it "passed" while verifying
# nothing.
#
# Rather than fake an invocation, assert what can actually be checked without a
# user token: the runtime exists and reached a READY state. An end-to-end query
# needs an ID token and belongs in the integ suite (`make test-integ`), not here.

echo ""
echo "--- Post-deploy check ---"

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
if [ -n "${RUNTIME_ARN}" ] && [ "${RUNTIME_ARN}" != "None" ]; then
  echo "  Runtime:  ${RUNTIME_ARN}"
  echo ""
  # The runtime authorizes with JWT (serve-stack.ts), so it needs a bearer ID
  # token from the configured IdP — a SigV4 `aws bedrock-agentcore
  # invoke-agent-runtime` is rejected. The previous version of this block
  # printed exactly that command, which cannot work.
  echo "Invoke with a bearer ID token from the app's IdP:"
  echo "  curl -X POST \\"
  echo "    'https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${RUNTIME_ID}/invocations?qualifier=DEFAULT' \\"
  echo "    -H \"Authorization: Bearer \${ID_TOKEN}\" \\"
  echo "    -H 'Content-Type: application/json' \\"
  echo "    -d '{\"query\":\"...\",\"namespace\":\"...\"}'"
  echo ""
  echo "Or run the integ suite, which provisions a token for you:"
  echo "  make test-integ"
fi
