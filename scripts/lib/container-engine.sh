# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash

# Deploy-preflight check for the container engine CDK builds image assets with.
#
# CDK shells out to $CDK_DOCKER (default: docker). Five service stacks (mcp,
# ontology, serve, sources, vkg) build container images, and a full deploy
# deploys all of them, so without an engine the deploy builds and synthesizes
# for ~20 minutes and then fails at the first image asset. Unlike Lambda
# bundling there is no local fallback for an image, so a missing engine is an
# error, not a warning.
#
# One legitimate engine-less case exists: CDK checks ECR before building and
# skips the engine entirely for an image that is already published, so a
# redeploy in which no image changed succeeds without one. An operator who knows
# that is the case opts in with SCL_ALLOW_NO_CONTAINER_ENGINE=1, which turns the
# error back into a warning.
#
# Sourced, not run. Plain bash 3.2, since the scripts also run on macOS. The
# caller provides ok/warn/err (see preflight-deploy.sh):
#   source "${REPO_ROOT}/scripts/lib/container-engine.sh"
#   coa_check_container_engine
# Sets CONTAINER_ENGINE to the reachable engine ("" if none) and exports
# CDK_DOCKER=finch when Finch is the engine picked.

# CONTAINER_ENGINE is read by the caller (preflight's arm64 and ECR checks).
# shellcheck disable=SC2034
coa_check_container_engine() {
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
  elif [ "${SCL_ALLOW_NO_CONTAINER_ENGINE:-}" = "1" ]; then
    warn "No container engine running (docker/finch), continuing because SCL_ALLOW_NO_CONTAINER_ENGINE=1. Container images cannot be built: the deploy succeeds only if no image asset changed since the last deploy (CDK reuses images already in ECR)."
  else
    err "No container engine running (docker/finch). The mcp, ontology, serve, sources and vkg stacks build container images, so the deploy would fail at the first image asset after the build. Start Docker or Finch, or set CDK_DOCKER to a running engine. If no image changed since the last deploy, set SCL_ALLOW_NO_CONTAINER_ENGINE=1 to continue without one."
  fi
}
