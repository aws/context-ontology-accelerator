#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Usage: check-tool-version.sh node|uv
#
# Print a one-line diagnosis and return non-zero unless the tool on PATH meets
# the repo's minimum version. The minimums are read from the files that already
# enforce them, so a bump in one place is picked up here:
#   node  "engines.node" in package.json (pnpm refuses an older Node too)
#   uv    "required-version" under [tool.uv] in pyproject.toml (uv refuses an
#         older uv for `uv sync`/`uv run`, but not every path the deploy takes:
#         a failed local Lambda bundle silently falls back to Docker)
# Only ">=X.Y.Z" specifiers are understood; anything else is reported as a
# parse failure rather than guessed at.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOL="${1:-}"

# Succeeds when dotted version $1 >= $2 (numeric per component, missing = 0).
_version_ge() {
  local IFS=.
  # shellcheck disable=SC2206 # intentional split on dots
  local -a have=($1) want=($2)
  local i h w
  for ((i = 0; i < ${#have[@]} || i < ${#want[@]}; i++)); do
    h="${have[i]:-0}"
    w="${want[i]:-0}"
    if ((10#$h > 10#$w)); then return 0; fi
    if ((10#$h < 10#$w)); then return 1; fi
  done
  return 0
}

# Leading X[.Y[.Z...]] of a version string: drops a "v" prefix and any
# pre-release/build suffix ("22.23.4-nightly", "0.13.5+3").
_numeric_version() {
  printf '%s\n' "$1" | sed -nE 's/^v?([0-9]+(\.[0-9]+)*).*/\1/p'
}

_node_minimum() {
  sed -n '/"engines"[[:space:]]*:/,/}/p' "$REPO_ROOT/package.json" 2>/dev/null \
    | sed -nE 's/.*"node"[[:space:]]*:[[:space:]]*">=[[:space:]]*([0-9]+(\.[0-9]+)*)[[:space:]]*".*/\1/p' \
    | sed -n '1p'
}

_uv_minimum() {
  sed -nE 's/^[[:space:]]*required-version[[:space:]]*=[[:space:]]*">=[[:space:]]*([0-9]+(\.[0-9]+)*)[[:space:]]*".*/\1/p' \
    "$REPO_ROOT/pyproject.toml" 2>/dev/null | sed -n '1p'
}

case "$TOOL" in
  node)
    MIN="$(_node_minimum)"
    if [ -z "$MIN" ]; then
      echo "Could not read the minimum Node.js version (\"engines\": {\"node\": \">=X.Y.Z\"}) from package.json"
      exit 1
    fi
    FIX_HINT="Install Node.js $MIN or newer (e.g. 'nvm install $MIN', 'mise use -g node@$MIN', or https://nodejs.org/en/download)"
    if ! command -v node >/dev/null 2>&1; then
      echo "Node.js not found. $FIX_HINT"
      exit 1
    fi
    RAW="$(node --version 2>/dev/null)" || {
      echo "Could not run 'node --version'. $FIX_HINT"
      exit 1
    }
    ;;
  uv)
    MIN="$(_uv_minimum)"
    if [ -z "$MIN" ]; then
      echo "Could not read the minimum uv version (required-version = \">=X.Y.Z\") from pyproject.toml"
      exit 1
    fi
    FIX_HINT="Run: uv self update (standalone installer), or upgrade it the way you installed it (pip install -U uv, brew upgrade uv, mise use -g uv@latest); see https://docs.astral.sh/uv/getting-started/installation/"
    if ! command -v uv >/dev/null 2>&1; then
      echo "uv not found. Install uv $MIN or newer: https://docs.astral.sh/uv/getting-started/installation/"
      exit 1
    fi
    # Run outside the repo: `uv --version` needs no project, and must not trip
    # over the very required-version this check reports on.
    RAW="$(cd / && uv --version 2>/dev/null)" || {
      echo "Could not run 'uv --version'. $FIX_HINT"
      exit 1
    }
    RAW="${RAW#uv }"
    ;;
  *)
    echo "usage: $0 node|uv"
    exit 2
    ;;
esac

FOUND="$(_numeric_version "$RAW")"
if [ -z "$FOUND" ]; then
  echo "Could not parse the $TOOL version from '$RAW'. $FIX_HINT"
  exit 1
fi

case "$TOOL" in
  node) LABEL="Node.js" ;;
  *) LABEL="$TOOL" ;;
esac

if ! _version_ge "$FOUND" "$MIN"; then
  echo "$LABEL $MIN+ required (found $FOUND). $FIX_HINT"
  exit 1
fi

echo "$LABEL $FOUND found (minimum $MIN)"
