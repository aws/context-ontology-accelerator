#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Fingerprint of everything Smithy codegen reads, so a deploy can tell whether
# smithy-generated/ (gitignored, so `git pull` never updates it) still matches
# the checked-out models.
#
#   smithy-fingerprint.sh hash  [REPO_ROOT]   print the inputs' fingerprint
#   smithy-fingerprint.sh state [REPO_ROOT]   print one of:
#       current    generated output matches the inputs
#       missing    no OpenAPI specs generated
#       unstamped  output predates fingerprinting (treat as stale)
#       stale      an input changed since the output was generated
#
# The fingerprint covers file CONTENTS and repo-relative paths only, so it is
# the same in any clone and unaffected by timestamps. smithy-generate.sh writes
# it to STAMP_FILE as its last step, so a failed generation leaves no stamp and
# is re-run by the next deploy.
set -euo pipefail

MODE="${1:-}"
REPO_ROOT="${2:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP_FILE="smithy-generated/.inputs.sha256"

if command -v sha256sum >/dev/null 2>&1; then
  sha() { sha256sum; }
else
  sha() { shasum -a 256; }
fi

inputs_hash() {
  # Model sources, the models build config (incl. the Gradle wrapper version),
  # and the codegen script itself (it pins the openapi-generator version).
  cd "$REPO_ROOT"
  # A missing input is simply absent from the fingerprint (find's non-zero exit
  # must not abort the pipeline under pipefail).
  { find models/src models/build.gradle.kts models/smithy-build.json \
      models/gradle/wrapper/gradle-wrapper.properties scripts/smithy-generate.sh \
      -type f -print0 2>/dev/null || true; } \
    | LC_ALL=C sort -z \
    | while IFS= read -r -d '' f; do
        printf '%s %s\n' "$(sha < "$f" | cut -d' ' -f1)" "$f"
      done \
    | sha | cut -d' ' -f1
}

case "$MODE" in
  hash)
    inputs_hash
    ;;
  state)
    specs=$({ find "$REPO_ROOT/smithy-generated/openapi" -name "*.json" -size +100c 2>/dev/null || true; } | wc -l | tr -d ' ')
    if [ "${specs:-0}" -eq 0 ]; then
      echo missing
    elif [ ! -s "$REPO_ROOT/$STAMP_FILE" ]; then
      echo unstamped
    elif [ "$(cat "$REPO_ROOT/$STAMP_FILE")" = "$(inputs_hash)" ]; then
      echo current
    else
      echo stale
    fi
    ;;
  *)
    echo "usage: $0 {hash|state} [REPO_ROOT]" >&2
    exit 2
    ;;
esac
