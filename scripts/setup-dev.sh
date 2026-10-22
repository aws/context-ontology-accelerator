#!/usr/bin/env sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

set -eu

echo "=== SCL Dev Environment Setup ==="

# Check UV
if ! command -v uv &> /dev/null; then
    echo "Installing UV..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
else
    echo "✓ UV $(uv --version) found"
fi

# Check pnpm
if ! command -v pnpm &> /dev/null; then
    echo "Installing pnpm..."
    npm install -g pnpm
else
    echo "✓ pnpm $(pnpm --version) found"
fi

# Check Java (17-24; see check-java-version.sh for the upper bound)
if JAVA_CHECK="$("$(dirname "$0")/check-java-version.sh")"; then
    echo "✓ $JAVA_CHECK"
else
    echo "⚠ $JAVA_CHECK"
fi

# Check Gradle
if command -v gradle &> /dev/null; then
    echo "✓ Gradle found"
else
    echo "⚠ Gradle not found — required for Smithy code generation"
    echo "  Install via: brew install gradle  (macOS)"
fi

# Unset VIRTUAL_ENV to avoid interference from other activated environments
unset VIRTUAL_ENV

# Sync UV workspace (Python)
echo ""
echo "Syncing UV workspace..."
uv sync --all-packages

# Install Node dependencies via pnpm (TS packages: infra, web-app, ts-shared).
# --frozen-lockfile: install exactly what pnpm-lock.yaml pins and never rewrite it.
# A plain install rewrites it on the public repo (it lists workspace packages the
# mirror leaves out), and the next `git pull` then fails on local changes. After
# changing a package.json, run a plain `pnpm install` yourself.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo ""
echo "Installing Node dependencies..."
cd "$REPO_ROOT" && pnpm install --frozen-lockfile

# Install pre-commit hooks
echo ""
echo "Installing pre-commit hooks..."
if git config --get core.hooksPath &> /dev/null; then
    HOOKS_PATH=$(git config --get core.hooksPath)
    echo "⚠ Skipping pre-commit install: core.hooksPath is set to '$HOOKS_PATH' (likely git-defender)."
    echo "  Run 'make lint' manually before committing, or configure pre-commit to run via git-defender."
else
    if command -v pre-commit &> /dev/null; then
        pre-commit install
        echo "✓ Pre-commit hooks installed"
    else
        echo "Installing pre-commit..."
        uv tool install pre-commit
        pre-commit install
    fi
fi

echo ""
echo "=== Setup complete! ==="
echo "Run 'make docker-up' to start local services."
echo "Run 'make test' to run tests."
