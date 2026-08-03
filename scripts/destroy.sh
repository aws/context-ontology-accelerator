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
#   2. Poll for those ENIs to fully detach. If they haven't after the
#      wait budget, STOP and tell the operator to wait — don't proceed
#      into `cdk destroy` where the SG delete would just fail anyway.
#   3. Delete VKG's per-namespace ECS services directly (they were
#      created outside CFN by the reload Lambda) and wait for them to
#      reach INACTIVE, so ECS DeleteCluster doesn't fail with
#      ClusterContainsServicesException.
#   4. Force-delete the DataZone domain via
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
#   5. `cdk destroy --all`, then verify no stacks remain.
#
# Mirrors deploy.sh's context resolution so `make destroy-dev` tears
# down exactly what `make deploy-dev` created.
set -uo pipefail

ENV="${1:-dev}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${CDK_DEFAULT_REGION:-${AWS_DEFAULT_REGION:-${AWS_REGION:-us-east-1}}}"
# Default MUST match the CDK app's DEFAULT_RESOURCE_PREFIX
# (libs/ts-shared/src/constants.ts, resolved in infra/lib/context.ts).
# A mismatch means this script resolves scl-* names / /scl SSM parameters
# while `cdk` operates on coa-* — every lookup silently misses.
PREFIX="${SCL_PREFIX:-coa}"
STACK_PREFIX="${PREFIX}-${ENV}"
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
if [ -n "${SCL_PROJECT_TAG:-}" ] && [[ ! "$SCL_PROJECT_TAG" =~ ^[a-zA-Z0-9_.:-]+$ ]]; then
  echo "ERROR: SCL_PROJECT_TAG contains invalid characters: $SCL_PROJECT_TAG" >&2; exit 1
fi

CONTEXT="--context env=$ENV"
[ -n "${SCL_PREFIX:-}" ]      && CONTEXT="$CONTEXT --context resource_prefix=$SCL_PREFIX"
[ -n "${SCL_PROJECT_TAG:-}" ] && CONTEXT="$CONTEXT --context project_tag=$SCL_PROJECT_TAG"
[ -n "${SCL_VPC_ID:-}" ]      && CONTEXT="$CONTEXT --context vpc_id=$SCL_VPC_ID"

ENI_WAIT_MAX_SECONDS="${SCL_ENI_WAIT_MAX_SECONDS:-600}"
ENI_POLL_INTERVAL_SECONDS=15
ECS_WAIT_MAX_SECONDS="${SCL_ECS_WAIT_MAX_SECONDS:-300}"
DOMAIN_WAIT_MAX_SECONDS="${SCL_DOMAIN_WAIT_MAX_SECONDS:-300}"

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

# ── Step 1: Delete AgentCore Runtimes ──────────────────────────────────────
echo ""
echo "=== Step 1/5: Delete AgentCore Runtimes ==="

RUNTIME_ARNS=()
for PARAM in "${SSM_PREFIX}/serve/runtime-arn" "${SSM_PREFIX}/mcp/runtime-arn"; do
  ARN=$(aws ssm get-parameter --name "$PARAM" --region "$REGION" \
    --query 'Parameter.Value' --output text 2>/dev/null || echo "")
  if [ -n "$ARN" ] && [ "$ARN" != "None" ]; then
    RUNTIME_ARNS+=("$ARN")
  fi
done

# Security groups whose ENIs we'll wait on in Step 2 — collected while we
# still have the running Runtimes to look up (post-delete, the API may
# stop returning networkConfiguration for a deleted runtime).
AGENTCORE_SG_IDS=()

if [ ${#RUNTIME_ARNS[@]} -eq 0 ]; then
  ok "No AgentCore Runtime ARNs found in SSM — already deleted or never deployed."
else
  for ARN in "${RUNTIME_ARNS[@]}"; do
    RUNTIME_ID=$(arn_id "$ARN")
    SG_ID=$(aws bedrock-agentcore-control get-agent-runtime \
      --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
      --query 'networkConfiguration.networkMode.vpc.securityGroups[0]' \
      --output text 2>/dev/null || echo "")
    if [ -n "$SG_ID" ] && [ "$SG_ID" != "None" ]; then
      AGENTCORE_SG_IDS+=("$SG_ID")
    fi

    info "Deleting AgentCore Runtime $RUNTIME_ID ..."
    if aws bedrock-agentcore-control delete-agent-runtime \
        --agent-runtime-id "$RUNTIME_ID" --region "$REGION" >/dev/null 2>&1; then
      ok "Deleted $RUNTIME_ID"
    else
      warn "delete-agent-runtime failed for $RUNTIME_ID (may already be deleted) — continuing"
    fi
  done
fi

# Fall back to a name-based SG lookup if we couldn't resolve any via the
# runtime API (e.g. runtime already gone but SG/ENIs are still draining
# from a previous delete).
if [ ${#AGENTCORE_SG_IDS[@]} -eq 0 ]; then
  while IFS= read -r SG_ID; do
    [ -n "$SG_ID" ] && AGENTCORE_SG_IDS+=("$SG_ID")
  done < <(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=group-name,Values=*AgentCoreSG*,*McpSG*" \
    --query 'SecurityGroups[].GroupId' --output text 2>/dev/null | tr '\t' '\n')
fi

# ── Step 2: Wait for AgentCore ENIs to detach ─────────────────────────────
echo ""
echo "=== Step 2/5: Wait for AgentCore Runtime ENIs to detach ==="

if [ ${#AGENTCORE_SG_IDS[@]} -eq 0 ]; then
  ok "No AgentCore security groups found — nothing to wait on."
else
  info "Watching security group(s): ${AGENTCORE_SG_IDS[*]}"
  DEADLINE=$(( $(date +%s) + ENI_WAIT_MAX_SECONDS ))
  while true; do
    TOTAL_ENIS=0
    ENI_API_FAILED=false
    for SG_ID in "${AGENTCORE_SG_IDS[@]}"; do
      ENI_ERR=$(mktemp)
      if COUNT=$(aws ec2 describe-network-interfaces --region "$REGION" \
          --filters "Name=group-id,Values=$SG_ID" \
          --query 'length(NetworkInterfaces)' --output text 2>"$ENI_ERR"); then
        TOTAL_ENIS=$((TOTAL_ENIS + COUNT))
      else
        ENI_API_FAILED=true
        warn "describe-network-interfaces failed for SG $SG_ID: $(cat "$ENI_ERR")"
      fi
      rm -f "$ENI_ERR"
    done

    if [ "$ENI_API_FAILED" = true ]; then
      warn "API call(s) failed — not treating as 'ENIs detached', retrying..."
      if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        err "ENI API calls kept failing through the ${ENI_WAIT_MAX_SECONDS}s wait budget."
        err "Cannot confirm ENI state. Check manually before running 'cdk destroy':"
        err "  aws ec2 describe-network-interfaces --region $REGION --filters Name=group-id,Values=${AGENTCORE_SG_IDS[*]}"
        exit 1
      fi
      sleep "$ENI_POLL_INTERVAL_SECONDS"
      continue
    fi

    if [ "$TOTAL_ENIS" -eq 0 ]; then
      ok "All AgentCore ENIs detached."
      break
    fi

    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
      err "AgentCore Runtime ENIs are still attached after ${ENI_WAIT_MAX_SECONDS}s."
      err "This is a known AgentCore behavior (ENIs can take up to 8 hours to detach"
      err "after Runtime delete and"
      err "https://github.com/hashicorp/terraform-provider-aws/issues/47399)."
      err ""
      err "Re-run this script in a few hours — the runtimes are already"
      err "deleted, so re-running will skip straight to this wait. Do NOT run"
      err "'cdk destroy' directly yet; DeleteSecurityGroup will fail with"
      err "DependencyViolation while ${TOTAL_ENIS} ENI(s) remain attached."
      exit 1
    fi

    info "$TOTAL_ENIS ENI(s) still attached, waiting ${ENI_POLL_INTERVAL_SECONDS}s..."
    sleep "$ENI_POLL_INTERVAL_SECONDS"
  done
fi

# ── Step 3: Delete VKG ECS services and wait ──────────────────────────────
echo ""
echo "=== Step 3/5: Delete VKG cluster services ==="

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

# ── Step 4: Delete the DataZone domain (force-cascades to child projects,
# assets, and asset types that CFN's own DeleteDomain/DeleteProject calls
# cannot clear on their own) ──────────────────────────────────────────────
echo ""
echo "=== Step 4/5: Delete DataZone domain ==="

DOMAIN_ID=$(aws ssm get-parameter --name "${SSM_PREFIX}/smus/domain-id" \
  --region "$REGION" --query 'Parameter.Value' --output text 2>/dev/null || echo "")

if [ -z "$DOMAIN_ID" ] || [ "$DOMAIN_ID" == "None" ]; then
  ok "No DataZone domain ID found in SSM — already deleted or never deployed."
else
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

# ── Step 5: cdk destroy --all, then verify ────────────────────────────────
echo ""
echo "=== Step 5/5: cdk destroy --all ==="
echo "CDK context: $CONTEXT"
pnpm --filter coa-infra exec cdk destroy --all $CONTEXT --force
DESTROY_EXIT=$?

echo ""
echo "=== Post-destroy verification ==="
LIST_STACKS_ERR=$(mktemp)
REMAINING=$(aws cloudformation list-stacks --region "$REGION" \
  --stack-status-filter DELETE_FAILED CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE ROLLBACK_COMPLETE \
  --query "StackSummaries[?starts_with(StackName, '${STACK_PREFIX}-')].[StackName,StackStatus]" \
  --output text 2>"$LIST_STACKS_ERR")
LIST_STACKS_EXIT=$?
LIST_STACKS_STDERR=$(cat "$LIST_STACKS_ERR")
rm -f "$LIST_STACKS_ERR"

if [ "$LIST_STACKS_EXIT" -ne 0 ]; then
  err "list-stacks API call failed — cannot verify teardown completeness."
  err "Error: $LIST_STACKS_STDERR"
  err "Check manually: aws cloudformation list-stacks --region $REGION"
  exit 1
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
  while IFS=$'\t' read -r STACK_NAME STACK_STATUS; do
    [ -z "$STACK_NAME" ] && continue
    EVENTS=$(aws cloudformation describe-stack-events --stack-name "$STACK_NAME" \
      --region "$REGION" \
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
  echo "the full failure detail. If it's DependencyViolation on an AgentCore SG" >&2
  echo "or ClusterContainsServicesException on the VKG cluster, re-run this" >&2
  echo "script — steps 1-4 are idempotent and will pick up where they left off." >&2
  exit 1
fi

echo "All '${STACK_PREFIX}-*' stacks deleted cleanly."
exit $DESTROY_EXIT
