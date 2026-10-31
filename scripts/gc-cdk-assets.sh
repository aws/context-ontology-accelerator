#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Prune stale container images from the CDK bootstrap asset repository with
# `cdk gc`, for `make gc-assets`.
#
#   gc-cdk-assets.sh
#
# `make deploy-dev` pushes every service image to the bootstrap repository
# (cdk-<qualifier>-container-assets-<account>-<region>), tagged by content hash,
# and nothing removes them: the bootstrap lifecycle rule only expires untagged
# images. ECR scanning then reports CVEs for every image ever pushed.
#
# A count or age lifecycle rule is not safe here: the repository is shared by
# every service and every CDK app in the account and region, and such a rule can
# expire an image a running service still uses. `cdk gc` deletes only images no
# CloudFormation template in the region references, and only after they have
# been unreferenced for GC_ROLLBACK_BUFFER_DAYS. It cannot see images used outside
# CloudFormation, such as the per-namespace VKG services the reload Lambda
# creates, so before deleting anything this script refuses if any ECS service
# runs an asset image no template references.
#
# Environment:
#   GC_ACTION                 print (default, read-only), tag, delete-tagged or full
#   GC_ROLLBACK_BUFFER_DAYS   days an image must have been unreferenced before it
#                             is deleted (default 30); 0 deletes at once
#   GC_CREATED_BUFFER_DAYS    never touch images younger than this (default 1)
#   GC_TYPE                   ecr (default), s3 or all
#   GC_YES=1                  skip cdk's confirmation prompt (scheduled runs)
#
# The first `full` run only tags the unreferenced images; they are deleted by a
# run at least GC_ROLLBACK_BUFFER_DAYS later. Exit status: 0 done, 1 refused or
# failed, 2 could not check (no AWS credentials).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/resolve-region.sh source-path=SCRIPTDIR
source "${REPO_ROOT}/scripts/lib/resolve-region.sh"
REGION="$(coa_resolve_region)"

ACTION="${GC_ACTION:-print}"
ROLLBACK_BUFFER_DAYS="${GC_ROLLBACK_BUFFER_DAYS:-30}"
CREATED_BUFFER_DAYS="${GC_CREATED_BUFFER_DAYS:-1}"
GC_TYPE="${GC_TYPE:-ecr}"
CDK_BIN="${CDK_BIN:-${REPO_ROOT}/infra/node_modules/.bin/cdk}"

case "$ACTION" in
  print | tag) DELETES=false ;;
  delete-tagged | full) DELETES=true ;;
  *) echo "ERROR: GC_ACTION must be print, tag, delete-tagged or full, got: $ACTION" >&2; exit 1 ;;
esac
case "$GC_TYPE" in
  ecr | s3 | all) ;;
  *) echo "ERROR: GC_TYPE must be ecr, s3 or all, got: $GC_TYPE" >&2; exit 1 ;;
esac
for KNOB in GC_ROLLBACK_BUFFER_DAYS GC_CREATED_BUFFER_DAYS; do
  if [ -n "${!KNOB:-}" ] && [[ ! "${!KNOB}" =~ ^(0|[1-9][0-9]*)$ ]]; then
    echo "ERROR: $KNOB must be a whole number, got: ${!KNOB}" >&2; exit 1
  fi
done
if [ "$CREATED_BUFFER_DAYS" -lt 1 ]; then
  # An image pushed by a deploy still creating its change set is not in any template yet.
  echo "ERROR: GC_CREATED_BUFFER_DAYS must be at least 1" >&2; exit 1
fi
if [ ! -x "$CDK_BIN" ]; then
  echo "ERROR: CDK CLI not found at $CDK_BIN. Run 'pnpm install --frozen-lockfile' first." >&2; exit 1
fi

if ! command -v aws >/dev/null 2>&1 || ! ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null); then
  echo "AWS credentials not available" >&2
  exit 2
fi

if ! REPO=$(aws cloudformation describe-stacks --region "$REGION" --stack-name CDKToolkit \
    --query "Stacks[0].Outputs[?OutputKey=='ImageRepositoryName'].OutputValue | [0]" --output text 2>&1); then
  echo "ERROR: cannot read the CDKToolkit stack in $REGION: $REPO" >&2; exit 1
fi
echo "Bootstrap image repository: $REPO ($ACCOUNT, $REGION). It is shared by every CDK app in this account and region."

STACKS=$(aws cloudformation list-stacks --region "$REGION" \
  --query "StackSummaries[?StackStatus!='DELETE_COMPLETE'].[StackName,StackStatus]" --output text) || {
  echo "ERROR: cannot list stacks in $REGION" >&2; exit 1
}

# A stack mid-update has only its new template readable, so its rollback image
# would look unreferenced.
BUSY=$(awk '$2 ~ /_IN_PROGRESS$/ && $2 != "REVIEW_IN_PROGRESS" {print $1 " (" $2 ")"}' <<<"$STACKS")
if [ -n "$BUSY" ] && $DELETES; then
  echo "ERROR: refusing to delete while stacks are changing:" >&2
  echo "$BUSY" >&2
  exit 1
fi

# Asset images running ECS services use, as "<service> <image tag>" lines. Any
# call failing here stops the run: a guard that silently skipped a cluster would
# let cdk gc delete what that cluster runs.
cannot_check() { echo "ERROR: cannot list the images ECS services use in $REGION: $1" >&2; exit 1; }
IN_USE=""
CLUSTERS=$(aws ecs list-clusters --region "$REGION" --query 'clusterArns[]' --output text) || cannot_check list-clusters
for CLUSTER in $CLUSTERS; do
  SERVICES=$(aws ecs list-services --region "$REGION" --cluster "$CLUSTER" --query 'serviceArns[]' --output text) ||
    cannot_check "list-services $CLUSTER"
  # describe-services takes at most 10 services per call.
  # shellcheck disable=SC2086
  set -- $SERVICES
  while [ "$#" -gt 0 ]; do
    BATCH=("${@:1:10}")
    shift "$(( $# < 10 ? $# : 10 ))"
    DEPLOYED=$(aws ecs describe-services --region "$REGION" --cluster "$CLUSTER" --services "${BATCH[@]}" \
      --query "services[].[serviceName, join(' ', deployments[].taskDefinition)]" --output text) ||
      cannot_check "describe-services $CLUSTER"
    while read -r SERVICE TASK_DEFS; do
      for TASK_DEF in $TASK_DEFS; do
        IMAGES=$(aws ecs describe-task-definition --region "$REGION" --task-definition "$TASK_DEF" \
          --query 'taskDefinition.containerDefinitions[].image' --output text) ||
          cannot_check "describe-task-definition $TASK_DEF"
        for IMAGE in $IMAGES; do
          case "$IMAGE" in
            */"$REPO":*) IN_USE+="$SERVICE ${IMAGE##*:}"$'\n' ;;
          esac
        done
      done
    done <<<"$DEPLOYED"
  done
done

UNREFERENCED=""
if [ -n "$IN_USE" ]; then
  # Only the templates cdk gc itself counts: it ignores stacks in these states,
  # so an image only they reference is deleted even if a service still runs it.
  TEMPLATES=""
  while read -r STACK STATUS; do
    [ -z "$STACK" ] && continue
    case "$STATUS" in CREATE_FAILED | DELETE_IN_PROGRESS | DELETE_FAILED | REVIEW_IN_PROGRESS) continue ;; esac
    TEMPLATE=$(aws cloudformation get-template --region "$REGION" --stack-name "$STACK" \
      --query TemplateBody --output json) || { echo "ERROR: cannot read the template of $STACK" >&2; exit 1; }
    TEMPLATES+="$TEMPLATE"
  done <<<"$STACKS"
  while read -r SERVICE TAG; do
    [ -z "$TAG" ] && continue
    [[ "$TEMPLATES" == *"$TAG"* ]] || UNREFERENCED+="  $SERVICE uses $REPO:$TAG"$'\n'
  done <<<"$IN_USE"
fi
if [ -n "$UNREFERENCED" ]; then
  if $DELETES; then
    echo "ERROR: these ECS services run images no CloudFormation template references, so cdk gc would delete them:" >&2
    printf '%s' "$UNREFERENCED" >&2
    echo "Move them to the current image (for VKG, the reload sweep), or delete them, then run this again." >&2
    exit 1
  fi
  echo "WARNING: these ECS services run images no CloudFormation template references; a deleting run will refuse until they move:"
  printf '%s' "$UNREFERENCED"
fi

ARGS=(gc "aws://$ACCOUNT/$REGION" --unstable=gc --type="$GC_TYPE" --action="$ACTION"
  --rollback-buffer-days="$ROLLBACK_BUFFER_DAYS" --created-buffer-days="$CREATED_BUFFER_DAYS")
[ "${GC_YES:-}" = "1" ] && ARGS+=(--confirm=false)

# Run outside infra/: with a cdk.json in reach, cdk gc synthesizes the app first.
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
if [ "$ACTION" != print ]; then
  (cd "$WORKDIR" && "$CDK_BIN" "${ARGS[@]}")
  exit
fi

# cdk gc's print action shows only scan progress; its counts are debug output.
LOG="$WORKDIR/gc.log"
if ! (cd "$WORKDIR" && "$CDK_BIN" "${ARGS[@]}" --verbose) >"$LOG" 2>&1; then
  tail -n 40 "$LOG" >&2
  exit 1
fi
COUNTS=$(grep -E '^(\[[0-9:]+\] )?[0-9]+ ((not )?isolated (images|assets)|(images|objects) total|deletable assets|taggable assets|assets to untag)$' "$LOG" |
  sed -E 's/^(\[[0-9:]+\] )?/  /')
if [ -z "$COUNTS" ]; then
  echo "WARNING: cdk gc printed no counts; this CLI version may log them differently. Full output:"
  cat "$LOG"
  exit 0
fi
echo "cdk gc counts (untagged images are not counted; the bootstrap lifecycle rule expires them):"
printf '%s\n' "$COUNTS"
echo "With these buffers, GC_ACTION=full deletes the 'deletable' images and tags the 'taggable' ones."
