# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash

# The AWS region the deploy, preflight and destroy scripts act on, resolved in ONE
# place so they cannot drift apart again.
#
# The CDK CLI reads the region from the active AWS profile and passes it to the CDK
# app as CDK_DEFAULT_REGION, but only inside the app's own process: the calling shell
# never sees it. A script that stops at the environment variables therefore falls back
# to us-east-1 while the stacks land in the profile's region, and then checks, reads or
# deletes in the wrong one. So after the environment variables this asks the AWS CLI
# for the profile's region (which honors AWS_PROFILE), and only then defaults.
#
# Order: CDK_DEFAULT_REGION, AWS_DEFAULT_REGION, AWS_REGION, the profile's region
# (`aws configure get region`), then us-east-1.
#
# Sourced, not run. Plain bash 3.2, since the scripts also run on macOS:
#   source "${REPO_ROOT}/scripts/lib/resolve-region.sh"
#   REGION="$(coa_resolve_region)"

coa_resolve_region() {
  local region="${CDK_DEFAULT_REGION:-${AWS_DEFAULT_REGION:-${AWS_REGION:-}}}"
  if [ -z "$region" ]; then
    # No AWS CLI, no region in the profile (the CLI exits 1) or an unreadable config
    # all fall through to the default instead of failing a caller under `set -e`.
    region="$(aws configure get region 2>/dev/null)" || region=""
  fi
  printf '%s\n' "${region:-us-east-1}"
}
