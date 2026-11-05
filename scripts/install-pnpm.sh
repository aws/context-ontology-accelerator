#!/usr/bin/env sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Installs the pnpm version .mise.toml pins, unless pnpm is already on PATH (whatever its version).
# `make setup` runs this before smithy-generate.sh, which needs pnpm; setup-dev.sh runs it too.

set -eu

if command -v pnpm >/dev/null 2>&1; then
    echo "✓ pnpm $(pnpm --version) found"
    exit 0
fi

MISE_TOML="$(cd "$(dirname "$0")/.." && pwd)/.mise.toml"
VERSION="$(sed -n 's/^pnpm = "\(.*\)"$/\1/p' "$MISE_TOML" 2>/dev/null | head -n 1)"
if [ -z "$VERSION" ]; then
    echo "✗ pnpm is not installed, and .mise.toml pins no pnpm version to install." >&2
    exit 1
fi

echo "Installing pnpm $VERSION..."
npm install -g "pnpm@$VERSION"
if ! command -v pnpm >/dev/null 2>&1; then
    echo "✗ npm installed pnpm $VERSION, but its global bin directory ($(npm prefix -g)/bin) is not on PATH." >&2
    exit 1
fi
