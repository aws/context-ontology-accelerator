#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

echo "=== Smithy Code Generation ==="

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODELS_DIR="$REPO_ROOT/models"
GENERATED_DIR="$REPO_ROOT/smithy-generated"

# openapi-generator version for Python server models
OPENAPI_GENERATOR_VERSION="7.12.0"

# Fingerprint the inputs BEFORE generating: the stamp written at the end then
# describes exactly what this run read, even if a model is edited mid-run.
INPUTS_HASH="$("$REPO_ROOT/scripts/smithy-fingerprint.sh" hash "$REPO_ROOT")"

# ── Clean previous generated output ──────────────────────────────────────────
echo ""
echo "Cleaning previous generated output..."
rm -rf "$GENERATED_DIR"
mkdir -p "$GENERATED_DIR"

# ── Build all Smithy projections (single gradlew build) ──────────────────────
#   - control-plane-ts      → TypeScript client (native Smithy)
#   - data-layer-ts         → TypeScript client (native Smithy)
#   - openapi-control-plane → OpenAPI spec (for API Gateway / docs)
#   - openapi-data-layer    → OpenAPI spec (for API Gateway / docs)
echo ""
echo "Building Smithy models..."
cd "$MODELS_DIR"
./gradlew clean build

PROJ_DIR="build/smithyprojections/models"
cd "$REPO_ROOT"

# ── Copy TypeScript clients ──────────────────────────────────────────────────
echo ""
echo "Copying TypeScript clients..."

for svc in "control-plane-ts:control-plane-typescript-client" "data-layer-ts:data-layer-typescript-client"; do
    proj="${svc%%:*}"
    dest="${svc##*:}"
    src="$MODELS_DIR/$PROJ_DIR/$proj/typescript-client-codegen"
    if [ ! -d "$src" ]; then
        echo "✗ TypeScript codegen output not found: $src"
        exit 1
    fi
    cp -r "$src" "$GENERATED_DIR/$dest"
done

echo "✓ TypeScript clients copied"

# ── Build TypeScript clients ─────────────────────────────────────────────────
# The Smithy codegen produces raw .ts source files. Consumers expect compiled
# dist-cjs/, dist-es/, and dist-types/ directories (per package.json "main",
# "module", "types" fields). Build them now so pnpm can resolve the packages.
# The generated package.json uses yarn: prefix syntax in the build script —
# replace it with npm-compatible sequential builds since yarn may not be available.
echo ""
echo "Building TypeScript clients..."

for client_dir in "$GENERATED_DIR/control-plane-typescript-client" "$GENERATED_DIR/data-layer-typescript-client"; do
    # Replace yarn: parallel syntax with sequential npm run calls
    sed -i.bak 's|"build": "concurrently.*"|"build": "npm run build:cjs \&\& npm run build:es \&\& npm run build:types"|' "$client_dir/package.json"
    rm -f "$client_dir/package.json.bak"
    (cd "$client_dir" && npm install --loglevel=error && npm run build --loglevel=error)
    if [ $? -ne 0 ]; then
        echo "✗ Failed to build TypeScript client: $client_dir"
        exit 1
    fi
done

echo "✓ TypeScript clients built"

# ── Copy OpenAPI specs ───────────────────────────────────────────────────────
echo ""
echo "Copying OpenAPI specs..."

mkdir -p "$GENERATED_DIR/openapi"
for proj in "openapi-control-plane" "openapi-data-layer"; do
    src="$MODELS_DIR/$PROJ_DIR/$proj"
    if [ ! -d "$src" ]; then
        echo "✗ OpenAPI projection output not found: $src"
        exit 1
    fi
    cp -r "$src"/* "$GENERATED_DIR/openapi/"
done

echo "✓ OpenAPI specs copied"

# ── Generate Python server models from OpenAPI ───────────────────────────────
# We use openapi-generator to produce Pydantic v2 models with .from_dict()/.to_dict()
# for server-side Lambda request parsing. The OpenAPI spec is the source of truth.
# The pyproject.toml is committed to version control and not regenerated here.
echo ""
echo "Generating Python server models from OpenAPI..."

OPENAPI_GEN_JAR="${OPENAPI_GENERATOR_HOME:-$HOME/.local/share/openapi-generator}/openapi-generator-cli-${OPENAPI_GENERATOR_VERSION}.jar"
if [ ! -f "$OPENAPI_GEN_JAR" ]; then
    echo "Downloading openapi-generator-cli ${OPENAPI_GENERATOR_VERSION}..."
    mkdir -p "$(dirname "$OPENAPI_GEN_JAR")"
    curl -sfL -o "$OPENAPI_GEN_JAR" \
        "https://repo1.maven.org/maven2/org/openapitools/openapi-generator-cli/${OPENAPI_GENERATOR_VERSION}/openapi-generator-cli-${OPENAPI_GENERATOR_VERSION}.jar"
    echo "✓ openapi-generator downloaded"
else
    echo "✓ openapi-generator found at $OPENAPI_GEN_JAR"
fi

# ── Control Plane Python models ──────────────────────────────────────────────
CONTROL_PLANE_SPEC="$GENERATED_DIR/openapi/openapi/ControlPlaneService.openapi.json"
if [ ! -f "$CONTROL_PLANE_SPEC" ]; then
    echo "✗ ControlPlaneService OpenAPI spec not found: $CONTROL_PLANE_SPEC"
    exit 1
fi

java -jar "$OPENAPI_GEN_JAR" generate \
    -i "$CONTROL_PLANE_SPEC" \
    -g python \
    -o "$GENERATED_DIR/control-plane-python-server" \
    --package-name coa_control_plane_server \
    --global-property models,supportingFiles=__init__.py \
    --additional-properties=generateSourceCodeOnly=true

echo "✓ Control Plane Python models generated"

# Fix __init__.py to only export models
CONTROL_PLANE_PKG="$GENERATED_DIR/control-plane-python-server/coa_control_plane_server"
grep '^from coa_control_plane_server\.models\.' "$CONTROL_PLANE_PKG/__init__.py" \
    > "$CONTROL_PLANE_PKG/__init__.py.tmp" || true
echo "# Auto-generated — models only (see smithy-generate.sh)" > "$CONTROL_PLANE_PKG/__init__.py"
cat "$CONTROL_PLANE_PKG/__init__.py.tmp" >> "$CONTROL_PLANE_PKG/__init__.py" 2>/dev/null || true
rm -f "$CONTROL_PLANE_PKG/__init__.py.tmp"

# Regression guard: fail loudly if the models-only __init__ fix-up produced an
# empty package instead of silently shipping zero re-exports.
if ! grep -q '^from coa_control_plane_server\.models\.' "$CONTROL_PLANE_PKG/__init__.py"; then
    echo "✗ control-plane __init__.py re-exports no models — generation is broken"
    exit 1
fi

# Write pyproject.toml for the control-plane Python server models
cat > "$GENERATED_DIR/control-plane-python-server/pyproject.toml" <<'PYPROJECT'
[project]
name = "coa-control-plane-server"
version = "0.0.1"
description = "Control Plane server models (generated from OpenAPI)"
requires-python = ">=3.12"
dependencies = ["pydantic>=2.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["coa_control_plane_server"]
PYPROJECT

# ── Data Layer Python models ─────────────────────────────────────────────────
DATA_LAYER_SPEC="$GENERATED_DIR/openapi/openapi/DataLayerService.openapi.json"
if [ ! -f "$DATA_LAYER_SPEC" ]; then
    echo "✗ DataLayerService OpenAPI spec not found: $DATA_LAYER_SPEC"
    exit 1
fi

java -jar "$OPENAPI_GEN_JAR" generate \
    -i "$DATA_LAYER_SPEC" \
    -g python \
    -o "$GENERATED_DIR/data-layer-python-server" \
    --package-name coa_data_layer_server \
    --global-property models,supportingFiles=__init__.py \
    --additional-properties=generateSourceCodeOnly=true

echo "✓ Data Layer Python models generated"

# Fix data-layer __init__.py to only export models
# The python generator's __init__.py imports api_client, configuration, etc.
# which don't exist in models-only mode. Replace with a models-only __init__.
DATA_LAYER_PKG="$GENERATED_DIR/data-layer-python-server/coa_data_layer_server"
grep '^from coa_data_layer_server\.models\.' "$DATA_LAYER_PKG/__init__.py" \
    > "$DATA_LAYER_PKG/__init__.py.tmp" || true
echo "# Auto-generated — models only (see smithy-generate.sh)" > "$DATA_LAYER_PKG/__init__.py"
cat "$DATA_LAYER_PKG/__init__.py.tmp" >> "$DATA_LAYER_PKG/__init__.py" 2>/dev/null || true
rm -f "$DATA_LAYER_PKG/__init__.py.tmp"

# Regression guard: fail loudly if the models-only __init__ fix-up produced an
# empty package instead of silently shipping zero re-exports.
if ! grep -q '^from coa_data_layer_server\.models\.' "$DATA_LAYER_PKG/__init__.py"; then
    echo "✗ data-layer __init__.py re-exports no models — generation is broken"
    exit 1
fi

# ── Write pyproject.toml for the Data Layer Python server models ─────────────
# The entire smithy-generated/ folder is gitignored, so this file must be
# recreated on each generation. It allows uv to resolve this as a workspace member.
cat > "$GENERATED_DIR/data-layer-python-server/pyproject.toml" <<'PYPROJECT'
[project]
name = "coa-data-layer-server"
version = "0.0.1"
description = "Data Layer server models (generated from OpenAPI)"
requires-python = ">=3.12"
dependencies = ["pydantic>=2.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["coa_data_layer_server"]
PYPROJECT

# ── Record what was generated from ───────────────────────────────────────────
# Last step on purpose: with `set -e`, any failure above exits before this, so a
# partial generation is never stamped as current and the next deploy re-runs it
# (see the Smithy check in preflight-deploy.sh).
echo "$INPUTS_HASH" > "$GENERATED_DIR/.inputs.sha256"

echo ""
echo "=== Code generation complete ==="
echo "Generated artifacts in: $GENERATED_DIR"
echo "  - control-plane-typescript-client/  → TS client (native Smithy)"
echo "  - data-layer-typescript-client/     → TS client (native Smithy)"
echo "  - openapi/                          → OpenAPI specs"
echo "  - control-plane-python-server/      → Python server models (Pydantic v2)"
echo "  - data-layer-python-server/         → Python server models (Pydantic v2)"
