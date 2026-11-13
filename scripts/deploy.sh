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
#   SCL_VPC_ID=vpc-abc SCL_AOSS_VPC_ENDPOINT_ID=vpce-0123456789abcdef0 make deploy-dev
#     (an imported VPC needs its OpenSearch Serverless aoss-data endpoint)
# CIDR for a NEW VPC (default 10.0.0.0/16; ignored-and-refused with SCL_VPC_ID;
# never change it on an existing deployment — that replaces the VPC):
#   SCL_VPC_CIDR=10.40.0.0/16 make deploy-dev
# Custom domains (all-or-nothing — set all five or none):
#   SCL_UI_DOMAIN, SCL_UI_CERT_ARN (us-east-1),
#   SCL_API_DOMAIN, SCL_API_CERT_ARN (API region), SCL_HOSTED_ZONE_ID
# Database scan enrichment timeout (minutes; raise for very large sources):
#   SCL_DB_SCAN_ENRICHMENT_TIMEOUT_MINUTES=180 make deploy-dev
# Tier-1 curated metric SQL timeout (seconds; default 35):
#   SCL_TIER1_METRIC_TIMEOUT_SECONDS=75 make deploy-dev
# Lambda reserved concurrency (default 5; set 0 to disable reserving on
# accounts whose Lambda concurrent-executions quota is the reduced default 10):
#   SCL_LAMBDA_RESERVED_CONCURRENCY=0 make deploy-dev
# Bedrock Guardrails safeguard tier (default classic). The Standard tier needs
# a system-defined guardrail profile and routes screened text cross-Region.
# On an existing deployment set the profile alone first (grants the callers'
# IAM), then add the tier (see deploying.md "Guardrail safeguard tier"):
#   SCL_GUARDRAIL_PROFILE=apac.guardrail.v1:0 make deploy-dev
#   SCL_GUARDRAIL_TIER=standard SCL_GUARDRAIL_PROFILE=apac.guardrail.v1:0 make deploy-dev
# Tier-2 flat NL→SQL ontology FK expansion (on by default; false turns it off)
# and how many walked tables it may append (default 8):
#   SCL_NL2SQL_GRAPH_EXPAND=false make deploy-dev
#   SCL_NL2SQL_GRAPH_EXPAND_MAX_TABLES=12 make deploy-dev
# Deep-reasoning stop signals (all off by default; see deploying.md
# "Deep-reasoning stop signals"). Confidence and similarity are 0-1 floats,
# stability steps an int 1-20 (default 2), telemetry true/false (default false):
#   SCL_DEEP_REASONING_SUFFICIENCY_MIN_CONFIDENCE=0.7 make deploy-dev
#   SCL_DEEP_REASONING_PROGRESS_SIMILARITY_THRESHOLD=0.95 SCL_DEEP_REASONING_STABILITY_STEPS=2 make deploy-dev
#   SCL_DEEP_REASONING_STOP_TELEMETRY=true make deploy-dev
# OpenSearch Serverless capacity floor and ceiling in OCU (default 2 / 96; a
# floor of 0 scales to zero when idle). Not persisted: set it on every deploy.
#   SCL_AOSS_MIN_OCU=0 make deploy-dev
#   SCL_AOSS_MAX_OCU=32 make deploy-dev
# Graph sources (RDF_SPARQL: Neptune graphs queried directly). Off by default;
# on/true/1/yes turns them on. Set it on EVERY deploy: an unset value turns them off.
#   SCL_GRAPH_SOURCES=on make deploy-dev
# Prefer IPv4 in the Python service images (on by default; false keeps
# glibc's IPv6-first order):
#   SCL_PREFER_IPV4=false make deploy-dev
# SMUS admin principal(s) — required, comma-separated IAM role/user ARN(s)
# that human admins federate into (e.g. your IAM Identity Center
# permission-set role): SCL_SMUS_ADMIN_ARNS=arn:aws:iam::123456789012:role/... make deploy-dev
CONTEXT=(--context "env=$ENV")
# Context for every SCL_* setting that is set (shared with deploy-serve.sh).
# shellcheck source=lib/cdk-context.sh source-path=SCRIPTDIR
source "$REPO_ROOT/scripts/lib/cdk-context.sh"

# Parallel image builds (SCL_ASSET_BUILD_CONCURRENCY, default 1). CDK starts no
# image build at all for 0 or a non-number, so reject those before any work.
ASSET_BUILD_CONCURRENCY="${SCL_ASSET_BUILD_CONCURRENCY:-1}"
[[ "$ASSET_BUILD_CONCURRENCY" =~ ^[1-9][0-9]*$ ]] || {
  echo "ERROR: SCL_ASSET_BUILD_CONCURRENCY must be a positive integer (got '$ASSET_BUILD_CONCURRENCY')" >&2
  exit 1
}

# ── Preflight: SMUS admin principal ──────────────────────────────────────
# NamespaceStack falls back to arn:aws:iam::<account>:role/Admin when
# SCL_SMUS_ADMIN_ARNS is unset. Validate the fallback before CDK runs while
# preserving the difference between missing credentials, access denial, service
# failure, and a confirmed NoSuchEntity result.
"$REPO_ROOT/scripts/check-smus-admin-principal.sh"

# ── VPC peering context from the test-databases stack (if deployed) ──
coa_add_jdbc_peering_context

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
echo "CDK context: ${CONTEXT[*]}"

cd "$REPO_ROOT"

# ── Preflight ────────────────────────────────────────────────────────────
echo ""
./scripts/preflight-deploy.sh "$ENV"

# ── Dependencies ─────────────────────────────────────────────────────────
# Install exactly what pnpm-lock.yaml pins. After `git pull`, node_modules still
# holds the previous release's packages; pnpm 10 would build against them, and
# pnpm 11+ runs a plain `pnpm install` before the first `pnpm nx`, which on the
# public repo rewrites pnpm-lock.yaml (it lists workspace packages the mirror
# leaves out) and makes the next `git pull` fail on local changes.
echo ""
echo "Installing locked Node dependencies..."
pnpm install --frozen-lockfile

# ── Build ────────────────────────────────────────────────────────────────
echo ""
echo "Building all packages..."
pnpm nx run-many -t build

# ── CDK synth + deploy ───────────────────────────────────────────────────
echo ""
echo "Synthesizing CDK stacks..."
pnpm --filter coa-infra exec cdk synth "${CONTEXT[@]}" >/dev/null

echo ""
echo "Deploying all stacks..."
# Stacks deploy in parallel where the dependency graph allows. Override with
# SCL_CDK_CONCURRENCY (1 deploys one stack at a time).
# SCL_ASSET_BUILD_CONCURRENCY builds that many images at once. Default 1: each
# parallel build needs its own memory, and on small hosts builds fail or swap.
pnpm --filter coa-infra exec cdk deploy --all "${CONTEXT[@]}" --require-approval=never \
  --concurrency "${SCL_CDK_CONCURRENCY:-3}" \
  --asset-build-concurrency "$ASSET_BUILD_CONCURRENCY"

echo ""
echo "=== Deployment to $ENV complete ==="
