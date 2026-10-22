#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Full CDK teardown, orchestrated step by step so nothing CFN doesn't own
# blocks `cdk destroy`:
#
#   1. Delete AgentCore Runtimes (serve + mcp) directly via the API —
#      CFN doesn't wait for their ENIs to detach, so deleting them here
#      first (rather than letting `cdk destroy` hit DependencyViolation
#      on the security group) starts the ENI-detach clock as early as
#      possible.
#   2. Check those Runtimes' ENIs, without waiting for them. AgentCore
#      releases them up to ~8 h after the Runtimes are deleted, and they hold
#      only the runtime security groups, the subnets and the VPC.
#   3. Delete VKG's per-namespace ECS services directly (they were
#      created outside CFN by the reload Lambda) and wait for them to
#      reach INACTIVE, so ECS DeleteCluster doesn't fail with
#      ClusterContainsServicesException.
#   4. Delete the Cloud Map services registered inside the private DNS
#      namespace. CFN owns the namespace (network-stack) but NOT the
#      services inside it — those are created at runtime by the VKG
#      reload Lambda (`servicediscovery:CreateService`), so CFN has no
#      record of them. DeleteNamespace fails while any service remains,
#      which puts the network stack in DELETE_FAILED. The application's
#      own namespace-delete flow cleans these up
#      (namespace-deletion-pipeline.ts), but this script never takes that
#      path, so anything left behind has to be cleared here.
#   5. Force-delete the DataZone domain via
#      `delete-domain --skip-deletion-check`, which cascades to every
#      project, asset, and asset type under it. CFN's own DeleteProject
#      calls (which it already issues with skipDeletionCheck=true
#      internally) can't clear a shared asset type still referenced by
#      OTHER projects' assets, which stalls the system project's delete,
#      which stalls the domain's — confirmed against a real stuck stack.
#      The domain and its child resources (project, form type, policy
#      grants, etc.) are RemovalPolicy.RETAIN in namespace-stack.ts so
#      CFN never attempts (and fails) to delete them itself; this step
#      is what actually tears the whole tree down.
#   6. Start deleting every connector stack. They belong to separate CDK apps in
#      connectors/, so `cdk destroy` below never sees them, and left behind
#      each keeps a spill bucket and a customer-managed KMS key alive. Step 8
#      waits for them.
#   7. `cdk destroy` every stack but network, re-running it past the failures
#      it is known to hit (see fix_failed_stack).
#   8. Finish the network stack. While AgentCore still holds ENIs, CloudFormation
#      deletes only its NAT gateway, Elastic IP and VPC endpoints, and the script
#      exits 3; a later run finishes it.
#
# Exit status: 0 everything deleted, 3 deferred (run again later), anything
# else a failure. `make destroy-dev` reports any non-zero status as 2.
#
# Mirrors deploy.sh's context resolution so `make destroy-dev` tears
# down exactly what `make deploy-dev` created.
set -uo pipefail

ENV="${1:-dev}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Resolved the way the CDK CLI resolves it, the profile's region included, so the
# pre-cleanup steps below act on the region `cdk destroy` tears down.
# shellcheck source=lib/resolve-region.sh source-path=SCRIPTDIR
source "${REPO_ROOT}/scripts/lib/resolve-region.sh"
REGION="$(coa_resolve_region)"
# The CDK CLI reads AWS_REGION first, this resolver CDK_DEFAULT_REGION: refuse a
# split instead of acting on two regions, then pin one for every child process.
for REGION_VAR in AWS_REGION AWS_DEFAULT_REGION CDK_DEFAULT_REGION; do
  if [ -n "${!REGION_VAR:-}" ] && [ "${!REGION_VAR}" != "$REGION" ]; then
    echo "ERROR: $REGION_VAR=${!REGION_VAR} but the region resolves to $REGION; set them to the same region." >&2
    exit 1
  fi
done
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION" CDK_DEFAULT_REGION="$REGION"
# Default MUST match the CDK app's DEFAULT_RESOURCE_PREFIX
# (libs/ts-shared/src/constants.ts, resolved in infra/lib/context.ts).
# A mismatch means this script resolves scl-* names / /scl SSM parameters
# while `cdk` operates on coa-* — every lookup silently misses.
PREFIX="${SCL_PREFIX:-coa}"
STACK_PREFIX="${PREFIX}-${ENV}"
NETWORK_STACK="${STACK_PREFIX}-network"
SSM_PREFIX="/${PREFIX}"

# Validate inputs that will be interpolated into shell commands.
# These are controlled env vars (set by CI or the developer), not user input,
# but validating them defends against accidental misconfiguration.
if [[ ! "$ENV" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo "ERROR: ENV must be alphanumeric/dash/underscore, got: $ENV" >&2; exit 1
fi
if [[ ! "$PREFIX" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo "ERROR: SCL_PREFIX must be alphanumeric/dash/underscore, got: $PREFIX" >&2; exit 1
fi
for KNOB in SCL_ENI_WAIT_MAX_SECONDS SCL_ECS_WAIT_MAX_SECONDS SCL_DOMAIN_WAIT_MAX_SECONDS \
    SCL_CLOUDMAP_WAIT_MAX_SECONDS SCL_CONNECTOR_DELETE_WAIT_MAX_SECONDS SCL_DESTROY_CONCURRENCY; do
  if [ -n "${!KNOB:-}" ] && [[ ! "${!KNOB}" =~ ^(0|[1-9][0-9]*)$ ]]; then
    echo "ERROR: $KNOB must be a whole number, got: ${!KNOB}" >&2; exit 1
  fi
done
if [ -n "${SCL_PROJECT_TAG:-}" ] && [[ ! "$SCL_PROJECT_TAG" =~ ^[a-zA-Z0-9_.:-]+$ ]]; then
  echo "ERROR: SCL_PROJECT_TAG contains invalid characters: $SCL_PROJECT_TAG" >&2; exit 1
fi

CONTEXT="--context env=$ENV"
[ -n "${SCL_PREFIX:-}" ]      && CONTEXT="$CONTEXT --context resource_prefix=$SCL_PREFIX"
[ -n "${SCL_PROJECT_TAG:-}" ] && CONTEXT="$CONTEXT --context project_tag=$SCL_PROJECT_TAG"
[ -n "${SCL_VPC_ID:-}" ]      && CONTEXT="$CONTEXT --context vpc_id=$SCL_VPC_ID"

# Statuses that mean "this stack still exists and owns resources", for step 6's connector sweep.
# It once omitted ROLLBACK_FAILED, UPDATE_ROLLBACK_FAILED and CREATE_FAILED, so a stack stuck in
# any of them was reported as gone.
# REVIEW_IN_PROGRESS is deliberately absent: cdk-dryrun-mr creates change-set-only stacks in that
# state on every MR, and they are not this deployment's to delete.
CFN_LIVE_STATUSES="CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE ROLLBACK_COMPLETE \
CREATE_FAILED UPDATE_FAILED DELETE_FAILED ROLLBACK_FAILED UPDATE_ROLLBACK_FAILED \
IMPORT_COMPLETE IMPORT_ROLLBACK_COMPLETE IMPORT_ROLLBACK_FAILED"
# A deploy is still changing the stack: never deleted here, but listed, so a sibling
# deployment mid-deploy is recognised and a stack of ours still settling is reported.
CFN_BUSY_STATUSES="CREATE_IN_PROGRESS ROLLBACK_IN_PROGRESS UPDATE_IN_PROGRESS \
UPDATE_COMPLETE_CLEANUP_IN_PROGRESS UPDATE_ROLLBACK_IN_PROGRESS UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS \
IMPORT_IN_PROGRESS IMPORT_ROLLBACK_IN_PROGRESS"
# 30 minutes, longer than the other budgets, because the COA-operated Databricks connector is
# VPC-attached: its security group cannot be deleted until Lambda releases the function's ENIs,
# which can take up to 20 minutes. Giving up sooner leaves the group behind, and step 8 then fails
# to delete the VPC it sits in.
CONNECTOR_DELETE_WAIT_MAX_SECONDS="${SCL_CONNECTOR_DELETE_WAIT_MAX_SECONDS:-1800}"

# Step 8's wait for AgentCore's ENIs, after what runs continuously is deleted.
ENI_WAIT_MAX_SECONDS="${SCL_ENI_WAIT_MAX_SECONDS:-0}"
ECS_WAIT_MAX_SECONDS="${SCL_ECS_WAIT_MAX_SECONDS:-300}"
DOMAIN_WAIT_MAX_SECONDS="${SCL_DOMAIN_WAIT_MAX_SECONDS:-300}"
CLOUDMAP_WAIT_MAX_SECONDS="${SCL_CLOUDMAP_WAIT_MAX_SECONDS:-180}"
# Bound on each stack delete this script issues itself; the network stack's took ~20 minutes.
STACK_DELETE_WAIT_MAX_SECONDS=3600
# Wait for the Runtimes step 1 deleted (36 s – 6 min observed); step 7 handles stragglers.
RUNTIME_DELETE_WAIT_MAX_SECONDS=180
# Stacks deleted in parallel; independent stacks overlap their waits (~30 vs 81 min serially).
DESTROY_CONCURRENCY="${SCL_DESTROY_CONCURRENCY:-4}"
# `cdk destroy` runs again after each fix, while that makes progress.
DESTROY_MAX_PASSES=8
EXIT_DEFERRED=3
LOOKUP_FAILED=false

info() { echo "  ->   $1"; }
ok() { echo "  OK:  $1"; }
warn() { echo "WARN:  $1" >&2; }
err() { echo "ERROR: $1" >&2; }

cd "$REPO_ROOT" || { err "Failed to change to repository root: $REPO_ROOT"; exit 1; }

echo "=== Destroying $ENV ($STACK_PREFIX-*, region $REGION) ==="
echo ""

if ! command -v aws >/dev/null 2>&1 || ! aws sts get-caller-identity >/dev/null 2>&1; then
  err "AWS credentials not available. Configure credentials before destroying."
  exit 1
fi
ok "AWS credentials valid ($(aws sts get-caller-identity --query Account --output text 2>/dev/null))"

if [ -t 0 ] && [ "${SCL_DESTROY_YES:-}" != "1" ]; then
  echo ""
  read -r -p "Destroy all '${STACK_PREFIX}-*' stacks in $REGION? Type 'yes' to continue: " CONFIRM
  if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted."
    exit 1
  fi
fi

# ── Helpers ──────────────────────────────────────────────────────────────

# Extract the trailing /<id> segment from an ARN.
arn_id() { echo "$1" | awk -F/ '{print $NF}'; }

# Add this deployment's runtime security groups to AGENTCORE_SG_IDS, by name: it outlives their stacks.
#
# The SGs carry no explicit name, so CloudFormation names them
# "<stack name>-<logical id>-<suffix>", with the stacks named
# "${STACK_PREFIX}-serve" / "${STACK_PREFIX}-mcp" (infra/bin/app.ts) and the
# logical ids starting "AgentCoreSG" / "McpSG". Spell out the full stack name
# before the logical id: ENV and SCL_PREFIX may contain dashes, so a looser
# "${STACK_PREFIX}-*AgentCoreSG*" also matches a sibling deployment such as
# env "dev-b" when tearing down "dev", and this script then waits on its ENIs.
find_runtime_sgs() {
  local sg out
  if ! out=$(aws ec2 describe-security-groups --region "$REGION" \
      --filters "Name=group-name,Values=${STACK_PREFIX}-serve-AgentCoreSG*,${STACK_PREFIX}-mcp-McpSG*" \
      --query 'SecurityGroups[].GroupId' --output text 2>&1); then
    warn "Could not look up the runtime security groups: $out"
    return 1
  fi
  for sg in $out; do
    [[ " ${AGENTCORE_SG_IDS[*]-} " == *" $sg "* ]] || AGENTCORE_SG_IDS+=("$sg")
  done
}

# The output of the command "${@:2}"; GONE if it fails with "$1" in its error,
# UNKNOWN if it fails otherwise.
status_of() {
  local gone="$1" out
  shift
  if out=$("$@" 2>&1); then echo "$out"
  elif [[ "$out" == *"$gone"* ]]; then echo "GONE"
  else echo "UNKNOWN"; fi
}
stack_status() {
  status_of "does not exist" aws cloudformation describe-stacks --stack-name "$1" --region "$2" \
    --query 'Stacks[0].StackStatus' --output text
}
runtime_state() {
  status_of "ResourceNotFound" aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$1" --region "$2" --query status --output text
}
stack_gone() { [[ "$(stack_status "$1" "$2")" =~ ^(GONE|DELETE_COMPLETE)$ ]]; }
# Sets LAST_STATUS, so a wait decides from the lookup that ended it.
stack_settled() {
  LAST_STATUS=$(stack_status "$1" "$2")
  [[ "$LAST_STATUS" =~ ^(GONE|DELETE_COMPLETE|DELETE_FAILED)$ ]]
}

# Run "${@:3}" every $2 seconds until it succeeds; fail once $1 seconds have
# passed. Every wait here is bounded (AWS CLI v2's `cloudformation wait` is not).
poll() {
  local deadline=$(( $(date +%s) + $1 )) interval="$2"
  shift 2
  until "$@"; do
    [ "$(date +%s)" -ge "$deadline" ] && return 1
    sleep "$interval"
  done
}
wait_stack_deleted() {
  poll "${3:-$STACK_DELETE_WAIT_MAX_SECONDS}" 15 stack_settled "$1" "$2" \
    && [[ "$LAST_STATUS" =~ ^(GONE|DELETE_COMPLETE)$ ]]
}

# What holds a security group: none, agentcore (only AgentCore's ENIs), other
# (another ENI), or unknown (the lookup failed).
sg_holder() {
  local types type
  if ! types=$(aws ec2 describe-network-interfaces --region "$REGION" --filters "Name=group-id,Values=$1" \
      --query 'NetworkInterfaces[].InterfaceType' --output text 2>&1); then
    warn "Could not look up the ENIs of $1: $types"
    echo "unknown"
    return
  fi
  [ -z "$types" ] || [ "$types" == "None" ] && { echo "none"; return; }
  for type in $types; do [ "$type" == "agentic_ai" ] || { echo "other"; return; }; done
  echo "agentcore"
}

# ── The app's stacks ────────────────────────────────────────────────────
# Synthesized before anything is deleted, so a checkout that cannot synth stops the run here.
echo ""
echo "CDK context: $CONTEXT"
# One synth per run, in a directory of its own (CDK commands sharing
# infra/cdk.out block each other). `cdk ls` synthesizes without bundling.
CDK_OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/coa-destroy.XXXXXX")"
trap 'rm -rf "$CDK_OUT_DIR"' EXIT
CLASSIFIER="$REPO_ROOT/scripts/lib/teardown_failures.py"
# shellcheck disable=SC2086 # CONTEXT is a list of --context flags
if ! pnpm --filter coa-infra exec cdk ls $CONTEXT --output "$CDK_OUT_DIR" >/dev/null; then
  err "cdk synth failed, so this script cannot tell which stacks the app owns."
  exit 1
fi

# This app's exact stacks and their regions: a name prefix could also match a
# sibling deployment.
APP_STACKS=()
APP_STACK_REGIONS=()
NON_NETWORK_IDS=()
NON_NETWORK_STACKS=()
while IFS=$'\t' read -r ARTIFACT_ID NAME STACK_REGION; do
  [ -z "$NAME" ] && continue
  APP_STACKS+=("$NAME")
  APP_STACK_REGIONS+=("$STACK_REGION")
  [ "$NAME" == "$NETWORK_STACK" ] && continue
  NON_NETWORK_IDS+=("$ARTIFACT_ID")
  NON_NETWORK_STACKS+=("$NAME")
done < <(python3 "$CLASSIFIER" stacks "$CDK_OUT_DIR/manifest.json" --default-region "$REGION")
if [ ${#APP_STACKS[@]} -eq 0 ]; then
  err "The synthesized app has no stacks — check the CDK context above."
  exit 1
fi

region_of() {
  local i
  for i in "${!APP_STACKS[@]}"; do
    if [ "${APP_STACKS[$i]}" == "$1" ]; then echo "${APP_STACK_REGIONS[$i]}"; return; fi
  done
  echo "$REGION"
}

# ── Step 1: Delete AgentCore Runtimes ──────────────────────────────────────
echo ""
echo "=== Step 1/8: Delete AgentCore Runtimes ==="

# The pointers are per prefix, not per env: once this env's stacks are gone they can name
# another env's Runtimes. Ours are prefixed("context-manager"|"mcp-server"), dashes as underscores.
RUNTIME_ID_PATTERN="^${STACK_PREFIX//-/_}_(context_manager|mcp_server)-[A-Za-z0-9]+$"
RUNTIME_ARNS=()
for PARAM in "${SSM_PREFIX}/serve/runtime-arn" "${SSM_PREFIX}/mcp/runtime-arn"; do
  ARN=$(aws ssm get-parameter --name "$PARAM" --region "$REGION" \
    --query 'Parameter.Value' --output text 2>/dev/null || echo "")
  if [ -z "$ARN" ] || [ "$ARN" == "None" ]; then continue; fi
  if [[ "$(arn_id "$ARN")" =~ $RUNTIME_ID_PATTERN ]]; then
    RUNTIME_ARNS+=("$ARN")
  else
    warn "$PARAM names $(arn_id "$ARN"), not a Runtime of this deployment; leaving it."
  fi
done

# The Runtimes' security groups, read while the Runtimes exist (once one is
# deleted, the API may stop returning its networkConfiguration).
AGENTCORE_SG_IDS=()
DELETED_RUNTIME_IDS=()

# Record on a deleted Runtime's groups (which outlive it) when it was deleted, for
# the summary and the deploy preflight; an earlier time is kept.
tag_deletion_time() {
  local sg existing now
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  for sg in "$@"; do
    existing=$(aws ec2 describe-security-groups --region "$REGION" --group-ids "$sg" \
      --query "SecurityGroups[0].Tags[?Key=='${PREFIX}:runtime-deleted-at'].Value | [0]" --output text 2>/dev/null)
    [ -z "$existing" ] || [ "$existing" == "None" ] || continue
    aws ec2 create-tags --region "$REGION" --resources "$sg" \
      --tags "Key=${PREFIX}:runtime-deleted-at,Value=${now}" >/dev/null 2>&1 || true
  done
}

if [ ${#RUNTIME_ARNS[@]} -eq 0 ]; then
  ok "No AgentCore Runtime ARNs found in SSM — already deleted or never deployed."
else
  for ARN in "${RUNTIME_ARNS[@]}"; do
    RUNTIME_ID=$(arn_id "$ARN")
    # networkMode is the string VPC or PUBLIC; the groups are in networkModeConfig.
    RT_SGS=()
    for SG_ID in $(aws bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
        --query 'networkConfiguration.networkModeConfig.securityGroups[]' --output text 2>/dev/null); do
      [ "$SG_ID" != "None" ] && RT_SGS+=("$SG_ID") && AGENTCORE_SG_IDS+=("$SG_ID")
    done

    info "Deleting AgentCore Runtime $RUNTIME_ID ..."
    if aws bedrock-agentcore-control delete-agent-runtime \
        --agent-runtime-id "$RUNTIME_ID" --region "$REGION" >/dev/null 2>&1; then
      ok "Deleted $RUNTIME_ID"
      DELETED_RUNTIME_IDS+=("$RUNTIME_ID")
      tag_deletion_time ${RT_SGS[@]+"${RT_SGS[@]}"}
    else
      # Only a runtime that is really going may be left out of its stack's delete.
      RT_STATE=$(runtime_state "$RUNTIME_ID" "$REGION")
      case "$RT_STATE" in
        GONE|DELETING) DELETED_RUNTIME_IDS+=("$RUNTIME_ID"); info "$RUNTIME_ID is already $RT_STATE" ;;
        *) warn "Could not delete AgentCore Runtime $RUNTIME_ID (it is $RT_STATE); its stack's delete will try." ;;
      esac
    fi
  done
fi

# By name too: that also finds groups whose Runtime an earlier run deleted. Step 8
# looks again before deciding anything, so a failure here costs only step 2's check.
find_runtime_sgs

# ── Step 2: Check AgentCore Runtime ENIs ──────────────────────────────────
# AgentCore releases them up to 8 hours after the Runtimes are deleted. They hold
# only the runtime security groups, the subnets and the VPC, which step 8 finishes.
echo ""
echo "=== Step 2/8: Check AgentCore Runtime ENIs ==="

if [ ${#AGENTCORE_SG_IDS[@]} -eq 0 ]; then
  ok "No AgentCore security groups found."
else
  info "Runtime security group(s): ${AGENTCORE_SG_IDS[*]}"
  HELD_SGS=0
  for SG_ID in "${AGENTCORE_SG_IDS[@]}"; do
    [ "$(sg_holder "$SG_ID")" == "none" ] || HELD_SGS=$((HELD_SGS + 1))
  done
  if [ "$HELD_SGS" -eq 0 ]; then
    ok "No AgentCore ENIs attached."
  else
    info "ENIs still hold $HELD_SGS of them; AWS releases AgentCore's up to 8 h after the"
    info "Runtimes are deleted. Continuing: they hold only the subnets and security groups."
  fi
fi

# ── Step 3: Delete VKG ECS services and wait ──────────────────────────────
echo ""
echo "=== Step 3/8: Delete VKG cluster services ==="

VKG_CLUSTER="${STACK_PREFIX}-vkg-cluster"
SERVICE_ARNS=()
while IFS= read -r ARN; do
  [ -n "$ARN" ] && SERVICE_ARNS+=("$ARN")
done < <(aws ecs list-services --cluster "$VKG_CLUSTER" --region "$REGION" \
  --query 'serviceArns[]' --output text 2>/dev/null | tr '\t' '\n')

if [ ${#SERVICE_ARNS[@]} -eq 0 ]; then
  ok "VKG cluster '$VKG_CLUSTER' has no active services (or cluster not found)."
else
  info "Found ${#SERVICE_ARNS[@]} active service(s) in $VKG_CLUSTER — scaling to zero and deleting."
  for SVC in "${SERVICE_ARNS[@]}"; do
    SVC_ERR=$(mktemp)
    if ! aws ecs update-service --cluster "$VKG_CLUSTER" --service "$SVC" \
        --desired-count 0 --region "$REGION" >/dev/null 2>"$SVC_ERR"; then
      SVC_STDERR=$(cat "$SVC_ERR")
      if echo "$SVC_STDERR" | grep -q "ServiceNotFoundException\|ServiceNotActiveException\|ClusterNotFoundException"; then
        info "Service $(arn_id "$SVC") already gone — skipping."
        rm -f "$SVC_ERR"
        continue
      fi
      warn "update-service failed for $(arn_id "$SVC"): $SVC_STDERR"
    fi
    rm -f "$SVC_ERR"

    SVC_ERR=$(mktemp)
    if ! aws ecs delete-service --cluster "$VKG_CLUSTER" --service "$SVC" \
        --force --region "$REGION" >/dev/null 2>"$SVC_ERR"; then
      SVC_STDERR=$(cat "$SVC_ERR")
      if echo "$SVC_STDERR" | grep -q "ServiceNotFoundException\|ClusterNotFoundException"; then
        info "Service $(arn_id "$SVC") already deleted."
      else
        err "delete-service failed for $(arn_id "$SVC"): $SVC_STDERR"
        err "Cannot proceed — VKG cluster still has active services that"
        err "we failed to delete. Fix the error above and re-run."
        rm -f "$SVC_ERR"
        exit 1
      fi
    else
      ok "Deleted service $(arn_id "$SVC")"
    fi
    rm -f "$SVC_ERR"
  done

  info "Waiting for services to reach INACTIVE (up to ${ECS_WAIT_MAX_SECONDS}s)..."
  DEADLINE=$(( $(date +%s) + ECS_WAIT_MAX_SECONDS ))
  while true; do
    if ! REMAINING=$(aws ecs describe-services --cluster "$VKG_CLUSTER" \
        --services "${SERVICE_ARNS[@]}" --region "$REGION" \
        --query "length(services[?status!='INACTIVE'])" --output text 2>/dev/null); then
      warn "describe-services API call failed (throttling, network, or"
      warn "AccessDenied) — treating as still-active and retrying, not as 0"
      warn "remaining."
      sleep 10
      if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        warn "describe-services kept failing through the ${ECS_WAIT_MAX_SECONDS}s"
        warn "wait budget — proceeding anyway; 'cdk destroy' will fail with"
        warn "ClusterContainsServicesException if services are still active."
        break
      fi
      continue
    fi

    if [ "$REMAINING" == "0" ]; then
      ok "All VKG services INACTIVE."
      break
    fi

    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
      warn "$REMAINING VKG service(s) still not INACTIVE after ${ECS_WAIT_MAX_SECONDS}s."
      warn "Proceeding anyway — 'cdk destroy' will fail on the VKG stack with"
      warn "ClusterContainsServicesException if they're still draining;"
      warn "re-run this script if that happens."
      break
    fi

    sleep 10
  done
fi

# ── Step 4: Delete Cloud Map services (CFN owns the namespace, not the
# services registered inside it) ──────────────────────────────────────────
echo ""
echo "=== Step 4/8: Delete Cloud Map services ==="

# Scoped by namespace NAME, which carries the prefix and env
# (`${this.prefixed("services")}.local` in network-stack.ts). Service names
# inside the namespace are per-SCL-namespace and carry no prefix, so filtering
# by name would be wrong — and with two deployments in one account it could
# match the other one's services. Always filter by namespace id.
CLOUDMAP_NS_NAME="${STACK_PREFIX}-services.local"
CLOUDMAP_NS_ID=$(aws servicediscovery list-namespaces --region "$REGION" \
  --query "Namespaces[?Name=='${CLOUDMAP_NS_NAME}'].Id | [0]" \
  --output text 2>/dev/null || echo "")

if [ -z "$CLOUDMAP_NS_ID" ] || [ "$CLOUDMAP_NS_ID" == "None" ]; then
  ok "No Cloud Map namespace '${CLOUDMAP_NS_NAME}' — nothing to clean up."
else
  info "Namespace $CLOUDMAP_NS_NAME ($CLOUDMAP_NS_ID)"
  CLOUDMAP_SERVICE_IDS=()
  while IFS= read -r SVC_ID; do
    [ -n "$SVC_ID" ] && CLOUDMAP_SERVICE_IDS+=("$SVC_ID")
  done < <(aws servicediscovery list-services --region "$REGION" \
    --filters "Name=NAMESPACE_ID,Values=${CLOUDMAP_NS_ID},Condition=EQ" \
    --query 'Services[].Id' --output text 2>/dev/null | tr '\t' '\n')

  if [ ${#CLOUDMAP_SERVICE_IDS[@]} -eq 0 ]; then
    ok "No Cloud Map services registered — CFN can delete the namespace."
  else
    info "${#CLOUDMAP_SERVICE_IDS[@]} service(s) to remove"
    CLOUDMAP_FAILED=0
    for SVC_ID in "${CLOUDMAP_SERVICE_IDS[@]}"; do
      # Deregister every instance first: DeleteService returns
      # ResourceInUse while any instance is still registered. ECS normally
      # deregisters its own instances when tasks stop (Step 3), but a service
      # whose tasks were already gone, or one registered by something other
      # than ECS, can still hold instances.
      while IFS= read -r INSTANCE_ID; do
        [ -z "$INSTANCE_ID" ] && continue
        if aws servicediscovery deregister-instance --region "$REGION" \
            --service-id "$SVC_ID" --instance-id "$INSTANCE_ID" >/dev/null 2>&1; then
          info "  deregistered $INSTANCE_ID from $SVC_ID"
        else
          warn "  could not deregister $INSTANCE_ID from $SVC_ID"
        fi
      done < <(aws servicediscovery list-instances --region "$REGION" \
        --service-id "$SVC_ID" --query 'Instances[].Id' --output text 2>/dev/null | tr '\t' '\n')

      # Deregistration is asynchronous, so retry the delete until the
      # instance count actually reaches zero rather than deleting once and
      # hoping. Bounded so a stuck service cannot hang the teardown.
      DEADLINE=$(( $(date +%s) + CLOUDMAP_WAIT_MAX_SECONDS ))
      while true; do
        DEL_ERR=$(mktemp)
        if aws servicediscovery delete-service --region "$REGION" \
            --id "$SVC_ID" >/dev/null 2>"$DEL_ERR"; then
          ok "Deleted Cloud Map service $SVC_ID"
          rm -f "$DEL_ERR"
          break
        fi
        DEL_STDERR=$(cat "$DEL_ERR"); rm -f "$DEL_ERR"

        # Already gone (concurrent teardown, or a previous run) is success.
        if echo "$DEL_STDERR" | grep -q "ServiceNotFound"; then
          ok "Cloud Map service $SVC_ID already gone"
          break
        fi
        if ! echo "$DEL_STDERR" | grep -q "ResourceInUse"; then
          warn "delete-service failed for $SVC_ID: $DEL_STDERR"
          CLOUDMAP_FAILED=$((CLOUDMAP_FAILED + 1))
          break
        fi
        if [ "$(date +%s)" -ge "$DEADLINE" ]; then
          COUNT=$(aws servicediscovery get-service --region "$REGION" --id "$SVC_ID" \
            --query 'Service.InstanceCount' --output text 2>/dev/null || echo "?")
          warn "Cloud Map service $SVC_ID still has $COUNT instance(s) after ${CLOUDMAP_WAIT_MAX_SECONDS}s."
          CLOUDMAP_FAILED=$((CLOUDMAP_FAILED + 1))
          break
        fi
        info "  $SVC_ID still in use, waiting 10s..."
        sleep 10
      done
    done

    if [ "$CLOUDMAP_FAILED" -gt 0 ]; then
      warn "$CLOUDMAP_FAILED Cloud Map service(s) could not be deleted."
      warn "'cdk destroy' will leave the network stack in DELETE_FAILED on"
      warn "DeleteNamespace while any service remains. Inspect with:"
      warn "  aws servicediscovery list-services --region $REGION \\"
      warn "    --filters Name=NAMESPACE_ID,Values=${CLOUDMAP_NS_ID},Condition=EQ"
    else
      ok "All Cloud Map services removed."
    fi
  fi
fi

# ── Step 5: Delete the DataZone domain (force-cascades to child projects,
# assets, and asset types that CFN's own DeleteDomain/DeleteProject calls
# cannot clear on their own) ──────────────────────────────────────────────
echo ""
echo "=== Step 5/8: Delete DataZone domain ==="

DOMAIN_ID=$(aws ssm get-parameter --name "${SSM_PREFIX}/smus/domain-id" \
  --region "$REGION" --query 'Parameter.Value' --output text 2>/dev/null || echo "")

# Per prefix too: delete only this deployment's domain, prefixed("smus-catalog"). One that is
# unreadable or named otherwise is left, but stops the run while this env's namespace stack, whose
# delete takes the only pointer to the domain, still exists.
domain_is_ours() {
  local name
  if name=$(aws datazone get-domain --identifier "$1" --region "$REGION" --query name --output text 2>&1); then
    [ "$name" == "${STACK_PREFIX}-smus-catalog" ] && return 0
  elif [[ "$name" == *ResourceNotFound* || "$name" == *AccessDenied* ]]; then
    ok "DataZone domain $1 is already deleted."; return 1  # AccessDenied too: see the wait below
  fi
  stack_gone "${STACK_PREFIX}-namespace" "$REGION" \
    || { err "Could not confirm DataZone domain $1 as ${STACK_PREFIX}-smus-catalog; run this again: $name"; exit 1; }
  warn "Leaving DataZone domain $1, not ${STACK_PREFIX}-smus-catalog: $name"; return 1
}

if [ -z "$DOMAIN_ID" ] || [ "$DOMAIN_ID" == "None" ]; then
  ok "No DataZone domain ID found in SSM — already deleted or never deployed."
elif domain_is_ours "$DOMAIN_ID"; then
  # CFN already calls DeleteProject with skipDeletionCheck=true internally
  # (confirmed via CloudTrail), but that cascade stops at cross-project
  # asset-type references: a shared asset type (e.g. CoaRelationalTable,
  # owned by the system project) can't be force-deleted by DeleteProject
  # while OTHER projects' assets still use that type — and every namespace
  # has its own project holding its scanned-table assets of that type, so
  # this isn't an edge case. That leaves the system project DELETE_FAILED
  # ("failed to stabilize"), which cascades to DefaultProjectProfile, which
  # cascades to SMUSDomain itself ("Domain cannot be deleted because there
  # are existing projects under this domain") — confirmed against a real
  # stuck stack. DataZone's own delete-domain --skip-deletion-check is a
  # true force-delete at the domain level with no such cross-project
  # carve-out, so it succeeds where DeleteProject's narrower cascade can't.
  info "Deleting DataZone domain $DOMAIN_ID (--skip-deletion-check force-"
  info "deletes every project, asset, and asset type under it — this is"
  info "asynchronous and can take a while for a domain with many namespaces)."
  if aws datazone delete-domain --identifier "$DOMAIN_ID" \
      --skip-deletion-check --region "$REGION" >/dev/null 2>&1; then
    ok "DeleteDomain accepted for $DOMAIN_ID."
  else
    warn "delete-domain failed for $DOMAIN_ID — it may already be deleting,"
    warn "or the caller lacks datazone:DeleteDomain. Continuing; if"
    warn "'cdk destroy' fails on namespace-stack, check domain status:"
    warn "  aws datazone get-domain --identifier $DOMAIN_ID --region $REGION"
  fi

  info "Waiting for domain deletion to complete (up to ${DOMAIN_WAIT_MAX_SECONDS}s)..."
  DEADLINE=$(( $(date +%s) + DOMAIN_WAIT_MAX_SECONDS ))
  while true; do
    GET_DOMAIN_ERR=$(mktemp)
    STATUS=$(aws datazone get-domain --identifier "$DOMAIN_ID" --region "$REGION" \
      --query 'status' --output text 2>"$GET_DOMAIN_ERR")
    GET_DOMAIN_EXIT=$?
    GET_DOMAIN_STDERR=$(cat "$GET_DOMAIN_ERR")
    rm -f "$GET_DOMAIN_ERR"

    if [ "$GET_DOMAIN_EXIT" -ne 0 ]; then
      if echo "$GET_DOMAIN_STDERR" | grep -q "ResourceNotFoundException"; then
        ok "Domain $DOMAIN_ID no longer exists."
        break
      fi
      if echo "$GET_DOMAIN_STDERR" | grep -q "AccessDeniedException"; then
        # DataZone returns AccessDenied (not ResourceNotFound) for domains
        # that have been deleted — treat as gone.
        ok "Domain $DOMAIN_ID no longer exists (AccessDenied → deleted)."
        break
      fi
      warn "get-domain API call failed (throttling or network failure) —"
      warn "not treating as deleted. Error: $GET_DOMAIN_STDERR"
      if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        warn "get-domain kept failing through the ${DOMAIN_WAIT_MAX_SECONDS}s"
        warn "wait budget — proceeding anyway; 'cdk destroy' will fail on"
        warn "namespace-stack if the domain hasn't actually finished deleting."
        break
      fi
      sleep 20
      continue
    fi

    if [ -z "$STATUS" ] || [ "$STATUS" == "None" ]; then
      ok "Domain $DOMAIN_ID no longer exists."
      break
    fi
    if [ "$STATUS" == "DELETION_FAILED" ]; then
      warn "Domain $DOMAIN_ID reported DELETION_FAILED. Proceeding anyway —"
      warn "'cdk destroy' will likely fail on namespace-stack; check"
      warn "  aws datazone get-domain --identifier $DOMAIN_ID --region $REGION"
      warn "for the failure reason and re-run this script."
      break
    fi

    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
      warn "Domain $DOMAIN_ID still $STATUS after ${DOMAIN_WAIT_MAX_SECONDS}s."
      warn "Proceeding anyway — 'cdk destroy' will fail on namespace-stack if"
      warn "the domain (or its retained children) haven't finished deleting;"
      warn "re-run this script if that happens."
      break
    fi

    info "Domain status: $STATUS, waiting 20s..."
    sleep 20
  done
fi

# ── Step 6: Delete the example connector stack ─────────────────────────────
# Not part of the CDK app `cdk destroy --all` covers — it is a separate app in a
# separate workspace (connectors/), deployed by scripts/deploy-example-connector.sh.
# Left behind it would keep a spill bucket and a customer-managed KMS key alive, and
# the verification below would report it.
#
# Deleted through CloudFormation rather than `cdk destroy`: that would synthesize the
# connector's app, which needs COA's serve and discovery role ARNs from SSM. At teardown
# those parameters may already be gone, so synth would fail and block the whole step.
# Everything the stack owns is RemovalPolicy.DESTROY, including the auto-delete of the
# spill bucket's objects, so CloudFormation clears it unaided.
echo ""
echo "=== Step 6/8: Delete connector stacks ==="

# Matched by SUFFIX, not named one by one: every connector's stack ends in -coa-connector
# (CONNECTOR_FUNCTION_SUFFIX in connectors/cdk-toolkit), so a second connector added later is
# torn down without editing this script. Naming only the example's stack meant any other
# connector survived, then failed the verification below on every subsequent run.
# DELETE_IN_PROGRESS too: an earlier run may have started a delete that step 8 still awaits.
CONNECTORS_DELETING=()
CONNECTOR_STACKS=""
# "<name>\t<status>" of every stack under this prefix that is not deleted.
list_prefix_stacks() {
  aws cloudformation list-stacks --region "$REGION" \
    --stack-status-filter $CFN_LIVE_STATUSES DELETE_IN_PROGRESS $CFN_BUSY_STATUSES \
    --query "StackSummaries[?starts_with(StackName, '${STACK_PREFIX}-')].[StackName,StackStatus]" --output text
}
# A sibling deployment whose name extends this one (env "dev-b" next to "dev") has stacks
# that match too: the name prefix of every other network stack, whatever its state.
siblings_in() {
  awk -v own="$NETWORK_STACK" '$1 ~ /-network$/ && $1 != own { sub(/network$/, "", $1); print $1 }' <<< "$1"
}
if ! PREFIX_LIST=$(list_prefix_stacks 2>&1); then
  err "Could not list the connector stacks: $PREFIX_LIST"
  LOOKUP_FAILED=true
  PREFIX_LIST=""
fi
SIBLINGS=$(siblings_in "$PREFIX_LIST")
while IFS=$'\t' read -r NAME STATUS; do
  [[ "$NAME" == *-coa-connector && " $CFN_BUSY_STATUSES " != *" $STATUS "* ]] || continue
  for SIBLING in $SIBLINGS; do [[ "$NAME" == "$SIBLING"* ]] && continue 2; done
  CONNECTOR_STACKS+=" $NAME"
done <<< "$PREFIX_LIST"

if [ -z "$CONNECTOR_STACKS" ]; then
  [ "$LOOKUP_FAILED" = true ] || ok "No connector stacks (${STACK_PREFIX}-*-coa-connector) — nothing to delete"
else
  # Awaited in step 8: the VPC-attached Databricks connector waits up to ~20 minutes for
  # Lambda to release its ENIs, and nothing step 7 deletes depends on it.
  for CONNECTOR_STACK in $CONNECTOR_STACKS; do
    echo "Deleting $CONNECTOR_STACK..."
    if aws cloudformation delete-stack --stack-name "$CONNECTOR_STACK" --region "$REGION"; then
      CONNECTORS_DELETING+=("$CONNECTOR_STACK")
    else
      warn "delete-stack failed for $CONNECTOR_STACK — continuing."
    fi
  done
fi
# A connector deployed with a custom FUNCTION_NAME_PREFIX carries a name this filter cannot
# predict, so it is not covered. Say so rather than implying the account is clean.
echo "Note: only ${STACK_PREFIX}-* connector stacks are covered. A connector deployed with a"
echo "custom FUNCTION_NAME_PREFIX must be destroyed by hand."

# ── Step 7: cdk destroy every stack but network ───────────────────────────
# Network is left to step 8: its VPC is what AgentCore's ENIs keep in use.
echo ""
echo "=== Step 7/8: cdk destroy (every stack but network) ==="

# CloudFormation cannot delete a Runtime AgentCore is still deleting (AccessDenied),
# so give step 1's deletes a moment; fix_failed_stack handles stragglers.
runtime_gone() { [ "$(runtime_state "$1" "$REGION")" == "GONE" ]; }
DEADLINE=$(( $(date +%s) + RUNTIME_DELETE_WAIT_MAX_SECONDS ))
for RUNTIME_ID in ${DELETED_RUNTIME_IDS[@]+"${DELETED_RUNTIME_IDS[@]}"}; do
  if poll $(( DEADLINE - $(date +%s) )) 10 runtime_gone "$RUNTIME_ID"; then
    ok "AgentCore Runtime $RUNTIME_ID deleted"
  else
    info "AgentCore Runtime $RUNTIME_ID is still being deleted; continuing."
  fi
done

# Fix what a DELETE_FAILED stack is known to fail on (see teardown_failures.py)
# and delete it again. Returns 0 on progress, 1 when it needs a person.
fix_failed_stack() {
  local name="$1" stack_region="$2" action logical physical rtype bucket rows
  local retain=() buckets=() unknown=()
  for _ in 1 2 3; do
    buckets=()
    unknown=()
    rows=0
    while IFS=$'\t' read -r action logical physical rtype; do
      rows=$((rows + 1))
      # A Runtime is left out only while it is going away: an earlier run's delete
      # counts as much as step 1's, and one whose delete failed counts for nothing.
      if [ "$rtype" == "AWS::BedrockAgentCore::Runtime" ]; then
        case "$(runtime_state "$physical" "$stack_region")" in
          GONE|DELETING) action="retain-deleted-runtime" ;;
          UNKNOWN) ;;  # unreadable: step 1's record decides
          *) action="unknown" ;;
        esac
      fi
      case "$action" in
        retain-*) [[ " ${retain[*]-} " == *" $logical "* ]] || retain+=("$logical") ;;
        empty-bucket) buckets+=("$physical") ;;
        *) unknown+=("$logical ($rtype)") ;;
      esac
    done < <(aws cloudformation list-stack-resources --stack-name "$name" --region "$stack_region" \
        --output json 2>/dev/null | python3 "$CLASSIFIER" classify \
        --runtime-sgs "${AGENTCORE_SG_IDS[*]-}" --deleted-runtimes "${DELETED_RUNTIME_IDS[*]-}")

    if [ "$rows" -eq 0 ]; then
      err "$name is DELETE_FAILED with no failed resource this script can fix."
      return 1
    fi
    if [ ${#unknown[@]} -gt 0 ]; then
      err "$name failed to delete for a reason this script does not handle: ${unknown[*]}"
      return 1
    fi
    for bucket in ${buckets[@]+"${buckets[@]}"}; do
      info "$name: access-log bucket $bucket refilled after its auto-delete — emptying it"
      # Server-access-log buckets are unversioned (constructs/access-logs-bucket.ts).
      aws s3 rm "s3://$bucket" --recursive --only-show-errors --region "$stack_region" || return 1
    done
    [ ${#retain[@]} -eq 0 ] && return 0  # emptied buckets: the next pass deletes it
    info "$name: deleting it again without ${retain[*]} (AgentCore still holds or deleted them)"
    if ! aws cloudformation delete-stack --stack-name "$name" --region "$stack_region" \
        --retain-resources "${retain[@]}"; then
      err "delete-stack failed for $name."
      return 1
    fi
    wait_stack_deleted "$name" "$stack_region" && { ok "Deleted $name (kept ${retain[*]})"; return 0; }
    [ "$LAST_STATUS" == "DELETE_FAILED" ] || { err "$name is still $LAST_STATUS; run this again."; return 1; }
    # A retained Runtime can let its security group fail next: look again.
  done
  err "$name still failed to delete after 3 attempts."
  return 1
}

# `cdk destroy` stops at the first stack that fails to delete: fix it and run
# again, while that makes progress.
DESTROY_EXIT=1
for PASS in $(seq "$DESTROY_MAX_PASSES"); do
  info "cdk destroy, pass $PASS (concurrency $DESTROY_CONCURRENCY)"
  pnpm --filter coa-infra exec cdk destroy "${NON_NETWORK_IDS[@]}" --exclusively \
    --app "$CDK_OUT_DIR" --force --concurrency "$DESTROY_CONCURRENCY"
  DESTROY_EXIT=$?
  [ "$DESTROY_EXIT" -eq 0 ] && break
  PROGRESS=false
  BLOCKED=false
  for NAME in "${NON_NETWORK_STACKS[@]}"; do
    STACK_REGION=$(region_of "$NAME")
    [ "$(stack_status "$NAME" "$STACK_REGION")" == "DELETE_FAILED" ] || continue
    if fix_failed_stack "$NAME" "$STACK_REGION"; then PROGRESS=true; else BLOCKED=true; fi
  done
  [ "$PROGRESS" = true ] && [ "$BLOCKED" = false ] || break
done

# ── Step 8: finish the network stack ──────────────────────────────────────
# Left until the ENIs in its subnets are gone: AgentCore's (up to ~8 h after the
# runtimes were deleted) and detached Lambda ones.
echo ""
echo "=== Step 8/8: Finish the network stack ==="

DEFERRED=false
NETWORK_FAILED=false
PENDING_SG_IDS=()  # held only by AgentCore's ENIs: the teardown can finish later
PROBLEM_SG_IDS=()  # kept by anything else: a failure
NETWORK_REGION=$(region_of "$NETWORK_STACK")
# The stack's own VPC, or the imported one (SCL_VPC_ID): where the Lambda ENIs are.
VPC_ID=$(aws cloudformation list-stack-resources --stack-name "$NETWORK_STACK" --region "$NETWORK_REGION" \
  --query "StackResourceSummaries[?ResourceType=='AWS::EC2::VPC'].PhysicalResourceId" \
  --output text 2>/dev/null | tr '\t' '\n' | grep -m1 '^vpc-')
[ -z "$VPC_ID" ] && VPC_ID=$(aws ssm get-parameter --name "${SSM_PREFIX}/${ENV}/network/vpc-id" \
  --region "$NETWORK_REGION" --query 'Parameter.Value' --output text 2>/dev/null)
{ [ -z "$VPC_ID" ] || [ "$VPC_ID" == "None" ]; } && VPC_ID="${SCL_VPC_ID:-}"

# EC2 can still report a dependency for a moment after the last ENI is gone.
delete_runtime_sg() {
  SG_DELETE_ERROR=$(aws ec2 delete-security-group --group-id "$1" --region "$REGION" 2>&1 >/dev/null) \
    || [[ "$SG_DELETE_ERROR" == *InvalidGroup.NotFound* ]]
}

# Delete the runtime security groups no live stack owns any more (retained, or
# left by an earlier run) once nothing holds them.
sweep_runtime_sgs() {
  local sg owner out
  PENDING_SG_IDS=()
  PROBLEM_SG_IDS=()
  find_runtime_sgs || LOOKUP_FAILED=true
  for sg in ${AGENTCORE_SG_IDS[@]+"${AGENTCORE_SG_IDS[@]}"}; do
    if ! owner=$(aws ec2 describe-security-groups --region "$REGION" --group-ids "$sg" \
        --query "SecurityGroups[0].Tags[?Key=='aws:cloudformation:stack-name'].Value | [0]" \
        --output text 2>&1); then
      [[ "$owner" == *InvalidGroup.NotFound* ]] && continue  # already deleted
      warn "Could not look up runtime security group $sg: $owner"
      PROBLEM_SG_IDS+=("$sg")
      continue
    fi
    if [ -n "$owner" ] && [ "$owner" != "None" ]; then
      case "$(stack_status "$owner" "$REGION")" in
        GONE|DELETE_COMPLETE) ;;
        UNKNOWN) warn "Could not look up $owner, the stack of $sg"; PROBLEM_SG_IDS+=("$sg"); continue ;;
        *) continue ;;  # its stack deletes it
      esac
    fi
    case "$(sg_holder "$sg")" in
      agentcore) PENDING_SG_IDS+=("$sg") ;;
      none)
        if poll 60 10 delete_runtime_sg "$sg"; then
          ok "Deleted retained runtime security group $sg"
        else
          warn "Could not delete runtime security group $sg: $SG_DELETE_ERROR"
          PROBLEM_SG_IDS+=("$sg")
        fi ;;
      other) warn "ENIs other than AgentCore's hold runtime security group $sg"; PROBLEM_SG_IDS+=("$sg") ;;
      *) PROBLEM_SG_IDS+=("$sg") ;;  # the lookup failed, as sg_holder said
    esac
  done
}

# Detached Lambda ENIs of this deployment's deleted functions; Lambda reclaims
# them on its own schedule, sometimes hours later.
sweep_detached_lambda_enis() {
  [ -z "$VPC_ID" ] && return 0
  local eni desc fn out
  while IFS=$'\t' read -r eni desc; do
    [ -z "$eni" ] && continue
    fn="${desc#AWS Lambda VPC ENI-}"
    # Only once its function is gone: a live one may still use the ENI.
    out=$(aws lambda get-function --function-name "$fn" --region "$REGION" 2>&1 >/dev/null) && continue
    [[ "$out" == *ResourceNotFoundException* ]] || continue
    if out=$(aws ec2 delete-network-interface --network-interface-id "$eni" --region "$REGION" 2>&1); then
      ok "Deleted detached Lambda ENI $eni ($fn)"
    else
      warn "Could not delete detached Lambda ENI $eni: $out"
    fi
  done < <(aws ec2 describe-network-interfaces --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=status,Values=available" \
      "Name=description,Values=AWS Lambda VPC ENI-${STACK_PREFIX}-*" \
    --query 'NetworkInterfaces[].[NetworkInterfaceId,Description]' --output text 2>/dev/null)
}

# True while the network stack still has a NAT gateway, Elastic IP or VPC
# endpoint, or that cannot be looked up.
billable_left() {
  local out
  if ! out=$(aws cloudformation list-stack-resources --stack-name "$NETWORK_STACK" \
      --region "$NETWORK_REGION" \
      --query "StackResourceSummaries[?contains(['AWS::EC2::NatGateway','AWS::EC2::EIP','AWS::EC2::VPCEndpoint'], ResourceType) && ResourceStatus!='DELETE_COMPLETE'].LogicalResourceId" \
      --output text 2>&1); then
    [[ "$out" != *"does not exist"* ]]
    return
  fi
  [ -n "$out" ] && [ "$out" != "None" ]
}

# True when the network stack failed to delete only what ENIs hold, and none of
# its NAT gateways, Elastic IPs and VPC endpoints is left.
network_failed_only_on_enis() {
  local action rows=0 others=0
  OTHER_ENIS=""
  [ "$(stack_status "$NETWORK_STACK" "$NETWORK_REGION")" == "DELETE_FAILED" ] || return 1
  while IFS=$'\t' read -r action _ _ rtype; do
    rows=$((rows + 1))
    # AgentCore's ENIs carry only the runtime groups, whose rules name CIDRs: they
    # can hold subnets and the VPC, never one of the network stack's own groups.
    [ "$action" == "eni-pinned" ] && [ "$rtype" != "AWS::EC2::SecurityGroup" ] || others=$((others + 1))
  done < <(aws cloudformation list-stack-resources --stack-name "$NETWORK_STACK" \
      --region "$NETWORK_REGION" --output json 2>/dev/null | python3 "$CLASSIFIER" classify)
  [ "$rows" -gt 0 ] && [ "$others" -eq 0 ] && ! billable_left && only_agentcore_enis
}

# True when the only ENIs left in the VPC are AgentCore's (and Lambda's for this
# deployment's deleted functions); OTHER_ENIS gets the rest, or the lookup error.
only_agentcore_enis() {
  OTHER_ENIS=$(aws ec2 describe-network-interfaces --region "$REGION" --filters "Name=vpc-id,Values=$VPC_ID" \
    --query "NetworkInterfaces[?InterfaceType!='agentic_ai' && !starts_with(Description || '', 'AWS Lambda VPC ENI-${STACK_PREFIX}-')].[NetworkInterfaceId,InterfaceType,Description]" \
    --output text 2>&1) && [ -z "$OTHER_ENIS" ]
}

# Only AgentCore's ENIs keep what is left, and AWS releases those on its own.
agentcore_hold() { [ ${#PENDING_SG_IDS[@]} -gt 0 ] && [ ${#PROBLEM_SG_IDS[@]} -eq 0 ]; }
runtime_enis_released() {
  local sg
  for sg in "${PENDING_SG_IDS[@]}"; do [ "$(sg_holder "$sg")" == "none" ] || return 1; done
}

# Delete the network stack and wait for CloudFormation's result, retrying once after
# a Lambda ENI sweep. Skipped while it fails only on what AgentCore's ENIs hold: a
# delete would end there again.
finish_network_stack() {
  local out _
  stack_gone "$NETWORK_STACK" "$NETWORK_REGION" && return 0
  agentcore_hold && network_failed_only_on_enis && return 0
  for _ in 1 2; do
    info "Deleting $NETWORK_STACK ..."
    if ! out=$(aws cloudformation delete-stack --stack-name "$NETWORK_STACK" \
        --region "$NETWORK_REGION" 2>&1); then
      err "delete-stack failed for $NETWORK_STACK: $out"
      NETWORK_FAILED=true
      return 0
    fi
    if ! poll "$STACK_DELETE_WAIT_MAX_SECONDS" 15 stack_settled "$NETWORK_STACK" "$NETWORK_REGION"; then
      err "$NETWORK_STACK is still $LAST_STATUS after ${STACK_DELETE_WAIT_MAX_SECONDS}s; run this again."
      NETWORK_FAILED=true
      return 0
    fi
    [ "$LAST_STATUS" == "DELETE_FAILED" ] || return 0
    agentcore_hold && network_failed_only_on_enis && return 0  # a retry would end the same way
    sweep_detached_lambda_enis  # Lambda can detach more while the stack deletes
  done
}

if [ "$DESTROY_EXIT" -ne 0 ]; then
  warn "Step 7 left stacks behind — not touching the network stack (see the verification below)."
else
  # Connector stacks from step 6 sit in the VPC (Lambda ENIs, security groups).
  FAILED_CONNECTORS=()
  for CONNECTOR_STACK in ${CONNECTORS_DELETING[@]+"${CONNECTORS_DELETING[@]}"}; do
    if wait_stack_deleted "$CONNECTOR_STACK" "$REGION" "$CONNECTOR_DELETE_WAIT_MAX_SECONDS"; then
      ok "Deleted $CONNECTOR_STACK"
    elif [ "$LAST_STATUS" == "DELETE_FAILED" ]; then
      FAILED_CONNECTORS+=("$CONNECTOR_STACK")
    else
      warn "$CONNECTOR_STACK did not finish deleting — see the verification below."
    fi
  done
  sweep_detached_lambda_enis
  # A connector's security group can wait on a Lambda ENI the sweep just deleted.
  for CONNECTOR_STACK in ${FAILED_CONNECTORS[@]+"${FAILED_CONNECTORS[@]}"}; do
    info "Deleting $CONNECTOR_STACK again ..."
    if aws cloudformation delete-stack --stack-name "$CONNECTOR_STACK" --region "$REGION" \
        && wait_stack_deleted "$CONNECTOR_STACK" "$REGION" "$CONNECTOR_DELETE_WAIT_MAX_SECONDS"; then
      ok "Deleted $CONNECTOR_STACK"
    else
      warn "$CONNECTOR_STACK did not finish deleting — see the verification below."
    fi
  done
  sweep_runtime_sgs
  finish_network_stack
  if [ "$ENI_WAIT_MAX_SECONDS" -gt 0 ] && agentcore_hold \
      && { stack_gone "$NETWORK_STACK" "$NETWORK_REGION" || network_failed_only_on_enis; }; then
    info "Waiting up to ${ENI_WAIT_MAX_SECONDS}s for AgentCore to release its ENIs..."
    poll "$ENI_WAIT_MAX_SECONDS" 15 runtime_enis_released
    sweep_detached_lambda_enis
    sweep_runtime_sgs
    finish_network_stack
  fi
  # Deferred only while AgentCore's own ENIs hold what is left; anything else fails.
  if [ "$NETWORK_FAILED" = true ] || [ ${#PROBLEM_SG_IDS[@]} -gt 0 ]; then
    NETWORK_FAILED=true
  elif ! stack_gone "$NETWORK_STACK" "$NETWORK_REGION"; then
    if agentcore_hold && network_failed_only_on_enis; then
      DEFERRED=true
    else
      NETWORK_FAILED=true
      err "$NETWORK_STACK is not deleted, and not only because of AgentCore's ENIs:"
      aws cloudformation list-stack-resources --stack-name "$NETWORK_STACK" --region "$NETWORK_REGION" \
        --query "StackResourceSummaries[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
        --output text 2>&1 | sed 's/^/  /' >&2
      [ -n "$OTHER_ENIS" ] && { err "ENIs in $VPC_ID other than AgentCore's:"; echo "$OTHER_ENIS" | sed 's/^/  /' >&2; }
    fi
  elif [ ${#PENDING_SG_IDS[@]} -gt 0 ]; then
    DEFERRED=true
  else
    ok "Deleted $NETWORK_STACK and the runtime security groups."
  fi
fi

# ── Verify ────────────────────────────────────────────────────────────────
echo ""
echo "=== Post-destroy verification ==="
REMAINING=""
note_remaining() {
  local status
  status=$(stack_status "$1" "$2")
  case "$status" in
    GONE|DELETE_COMPLETE|REVIEW_IN_PROGRESS) ;;  # the last holds change sets only
    UNKNOWN) LOOKUP_FAILED=true ;;
    *) REMAINING="${REMAINING}$1"$'\t'"$status"$'\n' ;;
  esac
}
for i in "${!APP_STACKS[@]}"; do
  note_remaining "${APP_STACKS[$i]}" "${APP_STACK_REGIONS[$i]}"
done
for CONNECTOR_STACK in $CONNECTOR_STACKS; do
  note_remaining "$CONNECTOR_STACK" "$REGION"
done
# Any other stack under this prefix (one the app no longer synthesizes, or one still
# being created at step 6) is a leftover too; a sibling deployment's are not ours.
while IFS=$'\t' read -r NAME _; do
  [ -z "$NAME" ] || [[ " ${APP_STACKS[*]} $CONNECTOR_STACKS " == *" $NAME "* ]] && continue
  for SIBLING in $SIBLINGS; do [[ "$NAME" == "$SIBLING"* ]] && continue 2; done
  note_remaining "$NAME" "$REGION"
done <<< "$PREFIX_LIST"
REMAINING=$(echo "$REMAINING" | sed '/^$/d')
if [ "$LOOKUP_FAILED" = true ]; then
  err "Could not look up everything the teardown needs — cannot verify it. Check manually:"
  err "  aws cloudformation list-stacks --region $REGION"
  exit 1
fi

OTHER_REMAINING=$(echo "$REMAINING" | awk -v network="$NETWORK_STACK" 'NF && $1 != network')
if [ "$DEFERRED" = true ] && [ "$NETWORK_FAILED" = false ] && [ -z "$OTHER_REMAINING" ]; then
  NETWORK_LEFT=$(aws cloudformation list-stack-resources --stack-name "$NETWORK_STACK" \
    --region "$NETWORK_REGION" \
    --query "StackResourceSummaries[?ResourceStatus!='DELETE_COMPLETE'].[ResourceType,PhysicalResourceId]" \
    --output text 2>/dev/null)
  DELETED_AT=None
  [ ${#PENDING_SG_IDS[@]} -gt 0 ] && DELETED_AT=$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=group-id,Values=$(IFS=,; echo "${PENDING_SG_IDS[*]}")" \
    --query "SecurityGroups[].Tags[?Key=='${PREFIX}:runtime-deleted-at'].Value | [] | [0]" --output text 2>/dev/null)
  echo ""
  echo "=== Teardown deferred: AWS still holds the AgentCore Runtimes' ENIs ==="
  echo "Deleted everything that runs continuously: Neptune, OpenSearch Serverless, the"
  echo "Fargate services, and the NAT gateway and VPC endpoints of a VPC COA created."
  echo "What remains until the next run is network configuration AWS does not charge"
  echo "for (the VPC, its subnets and the runtime security groups):"
  [ -n "$NETWORK_LEFT" ] && echo "  $NETWORK_STACK: ${NETWORK_LEFT//$'\n'/$'\n'  $NETWORK_STACK: }"
  for SG_ID in ${PENDING_SG_IDS[@]+"${PENDING_SG_IDS[@]}"}; do echo "  AgentCore Runtime security group $SG_ID"; done
  echo ""
  if [ -n "$DELETED_AT" ] && [ "$DELETED_AT" != "None" ]; then
    echo "The runtimes were deleted at $DELETED_AT; AWS releases their ENIs up to 8 hours later."
  fi
  echo "Then run this again to delete the rest:"
  echo "  SCL_PREFIX=$PREFIX AWS_REGION=$REGION ./scripts/destroy.sh $ENV"
  echo "Or by hand, once those ENIs are gone:"
  for SG_ID in ${PENDING_SG_IDS[@]+"${PENDING_SG_IDS[@]}"}; do
    echo "  aws ec2 delete-security-group --region $REGION --group-id $SG_ID"
  done
  [ -n "$NETWORK_LEFT" ] && echo "  aws cloudformation delete-stack --region $NETWORK_REGION --stack-name $NETWORK_STACK"
  exit "$EXIT_DEFERRED"
fi

if [ -n "$REMAINING" ]; then
  echo "The following stacks did not fully delete:" >&2
  echo "$REMAINING" >&2
  echo "" >&2
  # Known S3 autoDeleteObjects failure modes (GetBucketTagging AccessDenied,
  # NoSuchTagSet) — an upstream CDK Custom::S3AutoDeleteObjects race, not
  # something this script can prevent (the bucket policy granting the
  # Lambda access IS created with an explicit DependsOn ahead of it; the
  # failure is a timing/propagation issue on AWS's side, not a missing
  # grant). Scan each failed stack's events and print copy-pasteable
  # remediation so this doesn't require a wiki lookup every time.
  while IFS=$'\t' read -r STACK_NAME _; do
    [ -z "$STACK_NAME" ] && continue
    EVENTS=$(aws cloudformation describe-stack-events --stack-name "$STACK_NAME" \
      --region "$(region_of "$STACK_NAME")" \
      --query "StackEvents[?ResourceStatus=='DELETE_FAILED']" --output json 2>/dev/null || echo "[]")

    while IFS=$'\t' read -r LOGICAL_ID PHYSICAL_ID REASON; do
      [ -z "$LOGICAL_ID" ] && continue
      case "$REASON" in
        *GetBucketTagging*AccessDenied*|*NoSuchTagSet*)
          echo "KNOWN ISSUE: $STACK_NAME/$LOGICAL_ID — S3 autoDeleteObjects" >&2
          echo "  custom-resource race (upstream CDK; not fixable in this repo)." >&2
          echo "  Bucket: $PHYSICAL_ID" >&2
          echo "  Fix: re-tag the bucket so the custom resource recognizes it," >&2
          echo "  then re-run this script:" >&2
          echo "    aws s3api put-bucket-tagging --bucket $PHYSICAL_ID --region $REGION \\" >&2
          echo "      --tagging 'TagSet=[{Key=aws-cdk:auto-delete-objects,Value=true}]'" >&2
          echo "" >&2
          ;;
        *BucketNotEmpty*|*"bucket is not empty"*|*"not empty"*)
          echo "KNOWN ISSUE: $STACK_NAME/$LOGICAL_ID — bucket not empty" >&2
          echo "  (autoDeleteObjects custom resource didn't fully clear it —" >&2
          echo "  same upstream CDK race as above). Bucket: $PHYSICAL_ID" >&2
          echo "  Fix: empty it manually (handles versioned buckets too)," >&2
          echo "  then re-run this script:" >&2
          echo "    aws s3api delete-objects --bucket $PHYSICAL_ID --region $REGION --delete \"\$(" >&2
          echo "      aws s3api list-object-versions --bucket $PHYSICAL_ID --region $REGION \\" >&2
          echo "        --output json --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}')\"" >&2
          echo "" >&2
          ;;
      esac
    done < <(echo "$EVENTS" | python3 -c "
import json, sys
for e in json.load(sys.stdin):
    print(f\"{e.get('LogicalResourceId','')}\t{e.get('PhysicalResourceId','')}\t{e.get('ResourceStatusReason','')}\")
" 2>/dev/null)
  done < <(echo "$REMAINING")

  echo "Check aws cloudformation describe-stack-events --stack-name <name> for" >&2
  echo "the full failure detail, then re-run this script: every step picks up" >&2
  echo "where the last run left off." >&2
  exit 1
fi

if [ "$DESTROY_EXIT" -ne 0 ] || [ "$NETWORK_FAILED" = true ]; then
  err "The teardown did not finish; see the errors above."
  [ ${#PROBLEM_SG_IDS[@]} -gt 0 ] && err "  Runtime security groups left: ${PROBLEM_SG_IDS[*]}"
  exit 1
fi

echo "All $STACK_PREFIX stacks deleted."
exit 0
