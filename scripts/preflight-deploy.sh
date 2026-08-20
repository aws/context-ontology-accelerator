#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Pre-deploy validation — catches common silent failures before CDK runs.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ERRORS=0

err() { echo "ERROR: $1" >&2; ERRORS=$((ERRORS + 1)); }
warn() { echo "WARN:  $1" >&2; }
ok() { echo "  OK:  $1"; }

echo "=== Pre-deploy preflight checks ==="
echo ""

# ── 1. Required toolchain versions (Node 22+, Java 17+, pnpm) ────────────
# Mirrors the versions pinned in .mise.toml. We only check presence/version
# here — installing mise itself is a one-time local setup step (see
# scripts/setup-dev.sh) and is intentionally not automated in this script,
# since CI provisions its own toolchain and never calls deploy.sh.
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR=$(node --version | sed 's/^v//' | cut -d. -f1)
  if [ "$NODE_MAJOR" -ge 22 ]; then
    ok "Node $(node --version) found"
  else
    err "Node 22+ required (found $(node --version)). Run: mise install"
  fi
else
  err "Node not found. Install via 'mise install' (see .mise.toml) or https://mise.run"
fi

if command -v java >/dev/null 2>&1; then
  JAVA_VER=$(java -version 2>&1 | head -1 | awk -F '"' '{print $2}' | cut -d. -f1)
  if [ "$JAVA_VER" -ge 17 ]; then
    ok "Java $JAVA_VER found"
  else
    err "Java 17+ required (found $JAVA_VER). Run: mise install"
  fi
else
  err "Java not found — required for Smithy code generation. Run: mise install"
fi

if command -v pnpm >/dev/null 2>&1; then
  ok "pnpm $(pnpm --version) found"
else
  err "pnpm not found. Run: mise install (or npm install -g pnpm)"
fi

# ── 2. Python pip availability ────────────────────────────────────────────
if command -v pip >/dev/null 2>&1 && pip --version >/dev/null 2>&1; then
  ok "pip functional: $(pip --version 2>&1)"
elif command -v pip3 >/dev/null 2>&1 && pip3 --version >/dev/null 2>&1; then
  ok "pip3 functional (bundler resolves this automatically)"
else
  warn "Neither pip nor pip3 is functional. Docker bundling required."
fi

# ── 3. Container engine (Docker or Finch) ─────────────────────────────────
# CDK shells out to $CDK_DOCKER (default: docker) for asset bundling.
CONTAINER_ENGINE=""
if [ -n "${CDK_DOCKER:-}" ]; then
  if command -v "$CDK_DOCKER" >/dev/null 2>&1 && "$CDK_DOCKER" info >/dev/null 2>&1; then
    CONTAINER_ENGINE="$CDK_DOCKER"
    ok "CDK_DOCKER=$CDK_DOCKER (explicit, daemon running)"
  else
    err "CDK_DOCKER=$CDK_DOCKER set but daemon is not reachable"
  fi
elif command -v finch >/dev/null 2>&1 && finch info >/dev/null 2>&1; then
  export CDK_DOCKER=finch
  CONTAINER_ENGINE=finch
  ok "Finch daemon running (exported CDK_DOCKER=finch)"
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  CONTAINER_ENGINE=docker
  ok "Docker daemon running"
else
  warn "No container engine running (docker/finch) — local pip bundling must succeed"
fi

# ── 4. Smithy-generated OpenAPI specs ────────────────────────────────────
OPENAPI_DIR="$REPO_ROOT/smithy-generated/openapi"
SPEC_COUNT=0
if [ -d "$OPENAPI_DIR" ]; then
  SPEC_COUNT=$(find "$OPENAPI_DIR" -name "*.json" -size +100c 2>/dev/null | wc -l | tr -d ' ' || echo "0")
fi

if [ "$SPEC_COUNT" -gt 0 ]; then
  ok "Smithy OpenAPI specs present ($SPEC_COUNT files)"
else
  warn "smithy-generated/openapi/ missing or empty — running 'make generate'..."
  if (cd "$REPO_ROOT" && make generate); then
    SPEC_COUNT=$(find "$OPENAPI_DIR" -name "*.json" -size +100c 2>/dev/null | wc -l | tr -d ' ' || echo "0")
    if [ "$SPEC_COUNT" -gt 0 ]; then
      ok "Smithy code generation complete ($SPEC_COUNT OpenAPI specs)"
    else
      err "'make generate' ran but smithy-generated/openapi/ is still empty."
    fi
  else
    err "'make generate' failed. Fix Smithy/Gradle errors above before deploying."
  fi
fi

# ── 5. ECR Public authentication ─────────────────────────────────────────
# The build pulls base images from ECR Public (hosted only in us-east-1,
# regardless of deploy region). We do NOT force `docker login`: a user who
# configures a credential helper for ECR Public (credHelpers "public.ecr.aws"
# -> ecr-login, or a credsStore) authenticates automatically on pull and CANNOT
# `docker login` at all — the helper has no writable credential store, so login
# exits non-zero. Forcing it turned a valid (AWS-recommended) setup into a hard
# preflight failure (issue #89). Instead: skip the explicit login when a helper
# is configured, and treat an inability to log in as a WARNING — ECR Public also
# serves anonymous (rate-limited) pulls, so a failed login must not block deploy.

# Path to the active engine's config.json (Docker honors $DOCKER_CONFIG).
_engine_config_file() {
  case "${CONTAINER_ENGINE:-}" in
    *finch*)
      for _ecr_f in "$HOME/.finch/config.json" "$HOME/.finch/.docker/config.json"; do
        [ -f "$_ecr_f" ] && { printf '%s\n' "$_ecr_f"; return 0; }
      done
      return 1
      ;;
    *)
      printf '%s\n' "${DOCKER_CONFIG:-$HOME/.docker}/config.json"
      ;;
  esac
}

# Echo the credential helper bound to public.ecr.aws, or return 1. Scope is
# deliberately the per-registry credHelper only: a plain credsStore (osxkeychain,
# desktop, …) supports `docker login` normally, so it must NOT be skipped. The
# exotic "credsStore": "ecr-login" case is still covered — its login fails and is
# handled by the non-fatal warning below, not a hard error.
_ecr_cred_helper() {
  _ecr_cfg="$(_engine_config_file)" || return 1
  [ -f "$_ecr_cfg" ] || return 1
  if command -v jq >/dev/null 2>&1; then
    _ecr_h="$(jq -r '.credHelpers["public.ecr.aws"] // empty' "$_ecr_cfg" 2>/dev/null)"
    [ -n "$_ecr_h" ] && { printf '%s\n' "$_ecr_h"; return 0; }
    return 1
  fi
  # jq-less fallback: is public.ecr.aws present as a credHelpers key?
  grep -Eq '"public\.ecr\.aws"[[:space:]]*:' "$_ecr_cfg" 2>/dev/null && { printf 'credHelper\n'; return 0; }
  return 1
}

if [ -n "${CONTAINER_ENGINE:-}" ]; then
  if command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1; then
    if ECR_HELPER="$(_ecr_cred_helper)"; then
      ok "ECR Public auth handled by credential helper ($ECR_HELPER) — skipping explicit login"
    else
      # Split the token fetch from the login so the RIGHT error surfaces (a pipe
      # would either discard the AWS error via 2>/dev/null, or feed it to docker
      # as the password via 2>&1). Failure is a warning, not a fatal error.
      _ecr_awserr="$(mktemp)"
      if _ecr_pw="$(aws ecr-public get-login-password --region us-east-1 2>"$_ecr_awserr")"; then
        ECR_LOGIN_ERR="$(printf '%s' "$_ecr_pw" \
          | "$CONTAINER_ENGINE" login --username AWS --password-stdin public.ecr.aws 2>&1)" \
          && ok "Authenticated to ECR Public (us-east-1)" \
          || warn "Could not authenticate to ECR Public; base image pulls fall back to anonymous (rate-limited). Detail: ${ECR_LOGIN_ERR:-unknown error}"
      else
        warn "Could not obtain an ECR Public token from AWS; base image pulls fall back to anonymous (rate-limited). Detail: $(cat "$_ecr_awserr" 2>/dev/null || echo 'unknown error')"
      fi
      rm -f "$_ecr_awserr"
    fi
  else
    warn "AWS credentials not available — skipping ECR Public authentication"
  fi
else
  warn "No container engine — skipping ECR Public authentication"
fi

# ── 6. VPC limit check (requires AWS credentials) ────────────────────────
# Resolution order matches CDK's own precedence (infra/bin/app.ts uses
# CDK_DEFAULT_REGION as the source of truth) so preflight validates the
# same region CDK will actually deploy to.
REGION="${CDK_DEFAULT_REGION:-${AWS_DEFAULT_REGION:-${AWS_REGION:-us-east-1}}}"
if command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1; then
  VPC_COUNT=$(aws ec2 describe-vpcs --region "$REGION" --query 'length(Vpcs)' --output text 2>/dev/null || echo "?")
  VPC_LIMIT_RAW=$(aws service-quotas get-service-quota --service-code vpc --quota-code L-F678F1CE --region "$REGION" --query 'Quota.Value' --output text 2>/dev/null || echo "5")
  VPC_LIMIT="${VPC_LIMIT_RAW%%.*}"
  [[ "$VPC_LIMIT" =~ ^[0-9]+$ ]] || VPC_LIMIT=5
  if [ "$VPC_COUNT" != "?" ]; then
    if [ "$VPC_COUNT" -ge "$VPC_LIMIT" ]; then
      err "VPC limit reached: $VPC_COUNT/$VPC_LIMIT VPCs in $REGION."
      err "Delete unused VPCs or request a quota increase before deploying."
    else
      ok "VPC headroom: $VPC_COUNT/$VPC_LIMIT used in $REGION"
    fi
  fi
else
  warn "AWS credentials not available — skipping VPC limit check"
fi

# ── 7. Lambda reserved-concurrency headroom (requires AWS credentials) ────
# The VKG-reload and doc-preprocessing Lambdas each reserve
# `lambda_reserved_concurrency` (default 5) executions. Lambda refuses ANY
# reservation that would drop account-wide unreserved concurrency below its
# floor of 10 — the reduced default AWS applies to some new accounts. We check
# ACTUAL unreserved headroom (not the raw L-B99A9384 quota) because other
# functions' existing reservations already reduce it. Without this, deploy runs
# ~30 min then fails and rolls back on coa-dev-vkg / coa-dev-sources.
RESERVED_PER_FN="${SCL_LAMBDA_RESERVED_CONCURRENCY:-5}"
if [[ "$RESERVED_PER_FN" =~ ^[0-9]+$ ]] && [ "$RESERVED_PER_FN" -gt 0 ]; then
  if command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1; then
    NUM_RESERVED_FNS=2 # VkgReloadFn + SourcesPreProcessingFn
    NEEDED=$((RESERVED_PER_FN * NUM_RESERVED_FNS))
    MIN_UNRESERVED=10 # Lambda's hard floor; binding on reduced-quota accounts
    UNRESERVED=$(aws lambda get-account-settings --region "$REGION" \
      --query 'AccountLimit.UnreservedConcurrentExecutions' --output text 2>/dev/null || echo "?")
    if [[ "$UNRESERVED" =~ ^[0-9]+$ ]]; then
      if [ "$((UNRESERVED - NEEDED))" -lt "$MIN_UNRESERVED" ]; then
        err "Lambda concurrency headroom too low in $REGION: $UNRESERVED unreserved, deployment reserves $NEEDED."
        err "Lambda rejects reservations that leave fewer than $MIN_UNRESERVED unreserved executions account-wide."
        err "Fix: request an increase for quota L-B99A9384, or deploy without reservations:"
        err "  SCL_LAMBDA_RESERVED_CONCURRENCY=0 make deploy-dev"
      else
        ok "Lambda concurrency headroom: $UNRESERVED unreserved, reserving $NEEDED in $REGION"
      fi
    else
      warn "Could not read Lambda account settings — skipping concurrency headroom check"
    fi
  else
    warn "AWS credentials not available — skipping Lambda concurrency check"
  fi
else
  ok "Lambda reserved concurrency disabled (lambda_reserved_concurrency=0) — skipping headroom check"
fi

# ── 8. Stale cdk.context.json ────────────────────────────────────────────
if [ -f "$REPO_ROOT/infra/cdk.context.json" ]; then
  warn "infra/cdk.context.json exists — cached lookups may be stale."
  warn "If deploy fails with 'resource not found', delete it: rm infra/cdk.context.json"
fi

# ── Summary ──────────────────────────────────────────────────────────────
echo ""
if [ $ERRORS -gt 0 ]; then
  echo "PREFLIGHT FAILED: $ERRORS error(s) found. Fix before deploying."
  exit 1
else
  echo "Preflight passed."
fi
