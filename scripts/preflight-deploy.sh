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

# ── 8. CDK bootstrap in every region the deploy touches ───────────────────
# The *-edge-waf stack ALWAYS deploys to us-east-1 (CloudFront-scope WAF WebACLs
# and CloudFront ACM certs exist only there), so a non-us-east-1 deploy needs
# BOTH regions bootstrapped. Without this the deploy runs for a long while and
# then fails on that one stack with a bootstrap error — the docs showed a single
# `cdk bootstrap <REGION>` and never mentioned the second one.
if command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1; then
  _bootstrap_regions="$REGION"
  [ "$REGION" != "us-east-1" ] && _bootstrap_regions="$REGION us-east-1"
  for _bsr in $_bootstrap_regions; do
    if aws cloudformation describe-stacks --stack-name CDKToolkit --region "$_bsr" \
        >/dev/null 2>&1; then
      ok "CDK bootstrapped in $_bsr"
    else
      err "CDK is not bootstrapped in $_bsr."
      if [ "$_bsr" = "us-east-1" ] && [ "$REGION" != "us-east-1" ]; then
        err "us-east-1 is always required — the *-edge-waf stack deploys there regardless of your deploy region."
      fi
      err "Run: npx cdk bootstrap aws://\$(aws sts get-caller-identity --query Account --output text)/$_bsr"
    fi
  done
else
  warn "AWS credentials not available — skipping CDK bootstrap check"
fi

# ── 9. Effective Bedrock model IDs are invocable from the deploy region ───
# Two checks, with different strengths, on the model IDs the stacks will
# actually deploy — the value in /{prefix}/config when set, else the built-in
# default (#1020: with no config every default is a `us.` profile, so a
# non-US deploy reaches CREATE_COMPLETE and fails at the first Bedrock call).
#
#   ERROR  a geographic inference profile the deploy region's geography does
#          not publish (`us.` in ap-northeast-1). Deterministic; bin/app.ts
#          fails synth on the same condition, this is the earlier, friendlier
#          report.
#   WARN   the model is not listed in the region. Availability is
#          account-scoped and a bare in-region ID lives in a different API
#          than a geographic profile, so a hard failure here could block a
#          valid deploy.
#
# The defaults MUST match libs/ts-shared/src/constants.ts — pinned by
# infra/test/model-id-defaults.test.ts. Node is already a required deploy
# prerequisite, so use it here instead of making model validation depend on jq.
_DEFAULT_LLM_MODEL_ID="us.anthropic.claude-sonnet-5"
_DEFAULT_EMBED_MODEL_ID="us.cohere.embed-v4:0"
_DEFAULT_INDUCTION_MODEL_ID="us.anthropic.claude-sonnet-5"
_DEFAULT_CHAT_MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0"
_CONFIG_PARAM="/${SCL_PREFIX:-coa}/config"

# Geographies (as inference-profile prefixes) whose profiles are published in
# a region. Mirrors regionGeographies() in infra/lib/utils/model-region.ts —
# infra/test/model-id-defaults.test.ts runs this function and compares.
_coa_region_geographies() {
  case "$1" in
    us-gov-*) echo "us-gov" ;;
    cn-*) echo "" ;;
    us-*) echo "us global" ;;
    eu-*) echo "eu global" ;;
    ca-*) echo "ca global" ;;
    ap-northeast-1|ap-northeast-3) echo "jp apac global" ;;
    ap-southeast-2|ap-southeast-4) echo "au apac global" ;;
    me-central-1) echo "apac global" ;;
    ap-*) echo "apac global" ;;
    *) echo "global" ;;
  esac
}

# The leading geography of an inference-profile ID (3+ dot segments, known
# prefix), else nothing — bare IDs, ARNs and unknown geographies are not judged.
_coa_model_geo_prefix() {
  case "$1" in
    *.*.*) ;;
    *) return ;;
  esac
  _p="$(printf '%s' "$1" | cut -d. -f1 | tr '[:upper:]' '[:lower:]')"
  case "$_p" in
    us|eu|apac|jp|au|ca|us-gov|global) echo "$_p" ;;
  esac
}

_coa_resolve_model_ids() {
  _COA_DEFAULT_LLM_MODEL_ID="$_DEFAULT_LLM_MODEL_ID" \
  _COA_DEFAULT_EMBED_MODEL_ID="$_DEFAULT_EMBED_MODEL_ID" \
  _COA_DEFAULT_INDUCTION_MODEL_ID="$_DEFAULT_INDUCTION_MODEL_ID" \
  _COA_DEFAULT_CHAT_MODEL_ID="$_DEFAULT_CHAT_MODEL_ID" \
  node -e '
    const fail = (message) => {
      console.error(message);
      process.exit(1);
    };

    let config;
    try {
      config = JSON.parse(process.argv[1]);
    } catch (error) {
      fail(error instanceof Error ? error.message : String(error));
    }
    if (config === null || Array.isArray(config) || typeof config !== "object") {
      fail("config root must be an object");
    }

    const defaults = [
      ["bedrockLlmModelId", process.env._COA_DEFAULT_LLM_MODEL_ID],
      ["bedrockEmbedModelId", process.env._COA_DEFAULT_EMBED_MODEL_ID],
      ["bedrockInductionLlmModelId", process.env._COA_DEFAULT_INDUCTION_MODEL_ID],
      ["bedrockChatModelId", process.env._COA_DEFAULT_CHAT_MODEL_ID],
    ];
    const rows = defaults.map(([key, fallback]) => {
      const value = Object.prototype.hasOwnProperty.call(config, key)
        ? config[key]
        : null;
      if (value === null) {
        return [key, fallback, "default"];
      }
      if (typeof value !== "string") {
        fail(`${key} must be a string`);
      }
      if (/^\s*$/.test(value)) {
        return [key, fallback, "default"];
      }
      if (/\s/.test(value)) {
        fail(`${key} must not contain whitespace`);
      }
      return [key, value, "config"];
    });
    process.stdout.write(`${rows.map((row) => row.join("\t")).join("\n")}\n`);
  ' "$1"
}

if command -v aws >/dev/null 2>&1 \
    && aws sts get-caller-identity >/dev/null 2>&1; then
  if _cfg_json="$(aws ssm get-parameter --name "$_CONFIG_PARAM" --region "$REGION" \
      --query 'Parameter.Value' --output text 2>&1)"; then
    :
  elif printf '%s' "$_cfg_json" | grep -q 'ParameterNotFound'; then
    warn "$_CONFIG_PARAM not found in $REGION — every Bedrock model ID falls back to its built-in default."
    _cfg_json="{}"
  else
    err "Could not read $_CONFIG_PARAM in $REGION — model-ID validation cannot proceed."
    echo "       detail: $_cfg_json" >&2
    _cfg_json=""
  fi
  if [ -n "$_cfg_json" ]; then
    # One line per key: key<TAB>effective id<TAB>origin
    if _effective="$(_coa_resolve_model_ids "$_cfg_json" 2>&1)"; then
      :
    else
      err "$_CONFIG_PARAM is not valid deployment JSON — model-ID validation cannot proceed."
      echo "       detail: $_effective" >&2
      _effective=""
    fi
  fi
  if [ -n "${_effective:-}" ]; then
    _geos="$(_coa_region_geographies "$REGION")"
    _profiles="$(aws bedrock list-inference-profiles --region "$REGION" \
      --query 'inferenceProfileSummaries[].inferenceProfileId' --output text 2>/dev/null || echo "")"
    _models="$(aws bedrock list-foundation-models --region "$REGION" \
      --query 'modelSummaries[].modelId' --output text 2>/dev/null || echo "")"
    if [ -z "$_profiles" ] && [ -z "$_models" ]; then
      warn "Could not list Bedrock models in $REGION — skipping the availability check"
    fi
    while IFS="$(printf '\t')" read -r _key _mid _origin; do
      [ -n "$_key" ] || continue
      case "$_origin" in
        config) _from="from $_CONFIG_PARAM" ;;
        *) _from="built-in default; $_CONFIG_PARAM does not set $_key" ;;
      esac
      _geo="$(_coa_model_geo_prefix "$_mid")"
      if [ -n "$_geo" ] && ! printf ' %s ' "$_geos" | grep -qF " $_geo "; then
        err "$_key '$_mid' is a '$_geo.' inference profile, which Bedrock does not publish in $REGION ($_from)."
        echo "       fix: set $_key in $_CONFIG_PARAM to a profile published in $REGION (${_geos:-no geographic profiles here}) or the bare in-region model ID." >&2
        continue
      fi
      if [ -n "$_profiles" ] || [ -n "$_models" ]; then
        if printf '%s %s' "$_profiles" "$_models" | grep -qF "$_mid"; then
          ok "Bedrock model available in $REGION: $_key=$_mid ($_origin)"
        else
          warn "Bedrock model '$_mid' ($_key, $_from) not found in $REGION."
          warn "  It may be account-scoped or not yet enabled; if it is a cross-geography profile it fails at"
          warn "  first invocation with ValidationException. See 'Where the model IDs live' in external-docs/content/deploying.md."
        fi
      fi
    done <<EOF_EFFECTIVE
$_effective
EOF_EFFECTIVE
  fi
fi

# ── 10. Stale cdk.context.json ───────────────────────────────────────────
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
