#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Deterministic Apache-2.0 SPDX header check for changed source files.
#
# Usage: scripts/ci/check-license-headers.sh [BASE_REF]   (default: origin/main)
#
# Lists added/modified source files vs BASE_REF and fails (exit 1) if any is
# missing the required header. Mirrors AGENTS.md -> "License Headers (Apache-2.0)".
set -uo pipefail

BASE="${1:-origin/main}"
NEEDLE="SPDX-License-Identifier: Apache-2.0"

# Added/Modified files on this branch relative to the merge base with BASE.
if ! changed=$(git diff --name-only --diff-filter=AM "${BASE}...HEAD" 2>/dev/null); then
  changed=$(git diff --name-only --diff-filter=AM "${BASE}" HEAD)
fi

missing=""
count=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  [ -f "$f" ] || continue
  case "$f" in
    *.py|*.ts|*.tsx|*.js|*.mjs|*.cjs|*.sh|*.smithy) ;;
    *) continue ;;
  esac
  # Skip generated, vendored, and build output, plus MIT-0 workshop content.
  # workshop/ is MIT No Attribution per workshop/LICENSE and workshop/NOTICE, which
  # names these files explicitly; stamping Apache-2.0 on them would contradict the
  # license shipped alongside them. They are also re-synced from Workshop Studio on
  # every content refresh, which would silently revert any header added here.
  case "$f" in
    smithy-generated/*|*/smithy-generated/*|infra/cdk.out/*|models/build/*|\
    */node_modules/*|*/dist/*|*/build/*|*/.venv/*|*/__pycache__/*|\
    workshop/*) continue ;;
  esac
  if ! head -n 5 "$f" | grep -q "$NEEDLE"; then
    missing="${missing}${f}"$'\n'
    count=$((count + 1))
  fi
done <<< "$changed"

if [ "$count" -gt 0 ]; then
  echo "Missing Apache-2.0 SPDX header in ${count} changed source file(s):"
  printf '%s' "$missing" | sed 's/^/  - /'
  echo ""
  echo "Add the header per AGENTS.md -> 'License Headers (Apache-2.0)'."
  exit 1
fi

echo "License header check passed: all changed source files carry the SPDX header."
exit 0
