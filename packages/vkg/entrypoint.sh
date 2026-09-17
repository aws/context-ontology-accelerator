#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# VKG Translation Service Entrypoint
# Downloads ontology + mappings from S3, starts Ontop in endpoint mode
# with an in-memory H2 (no real DB), then starts the translation API facade.
#
# If ontology files are missing from S3, the service starts in degraded mode
# (returns 503 on /sparql/translate, but container stays alive for ECS health checks).

ONTOLOGY_BUCKET="${ONTOLOGY_BUCKET:?ONTOLOGY_BUCKET is required}"
ONTOLOGY_PREFIX="${ONTOLOGY_PREFIX:-ontologies/}"
ENDPOINT_PORT="${ENDPOINT_PORT:-8080}"
ONTOP_PORT="${ONTOP_PORT:-8081}"
NAMESPACE="${NAMESPACE:?NAMESPACE is required — each VKG instance serves exactly one namespace}"

VERSION="${VERSION:-latest}"

ARTIFACT_DIR="/app/ontology"
S3_PATH="s3://${ONTOLOGY_BUCKET}/${ONTOLOGY_PREFIX}${NAMESPACE}/${VERSION}"

echo "[VKG] Bucket: ${ONTOLOGY_BUCKET}"
echo "[VKG] S3 path: ${S3_PATH}/"
echo "[VKG] API port: ${ENDPOINT_PORT}, Ontop port: ${ONTOP_PORT}"

# Download all artifacts from S3 (with retry — transient S3 failures shouldn't
# leave the container permanently degraded per REL11-BP03).
echo "[VKG] Downloading artifacts..."
S3_DOWNLOAD_OK=false
S3_MAX_ATTEMPTS=3
S3_ATTEMPT=1
S3_DELAY=2

while [ $S3_ATTEMPT -le $S3_MAX_ATTEMPTS ]; do
  if aws s3 cp "${S3_PATH}/" "${ARTIFACT_DIR}/" --recursive 2>/tmp/s3_error.log; then
    S3_DOWNLOAD_OK=true
    break
  fi
  if [ $S3_ATTEMPT -lt $S3_MAX_ATTEMPTS ]; then
    echo "[VKG] WARN: S3 download attempt ${S3_ATTEMPT}/${S3_MAX_ATTEMPTS} failed — retrying in ${S3_DELAY}s"
    cat /tmp/s3_error.log 2>/dev/null
    sleep $S3_DELAY
    S3_DELAY=$((S3_DELAY * 2))
  else
    echo "[VKG] ERROR: S3 download details:"
    cat /tmp/s3_error.log 2>/dev/null
  fi
  S3_ATTEMPT=$((S3_ATTEMPT + 1))
done

if [ "${S3_DOWNLOAD_OK}" != "true" ]; then
  echo "[VKG] WARN: Failed to download from S3 after ${S3_MAX_ATTEMPTS} attempts — starting in degraded mode"
  echo "[VKG] Upload ontology files to ${S3_PATH}/ and restart the task"
  exec python3 /app/translate-server.py --port "${ENDPOINT_PORT}" --ontop-port "${ONTOP_PORT}" --ontology-version "none"
fi

# Detect ontology file by extension (supports .ttl, .owl)
ONTOLOGY_FILE=$(find "${ARTIFACT_DIR}" -maxdepth 1 \( -name "*.ttl" -o -name "*.owl" \) ! -name "*.obda" | head -1)
if [ -z "${ONTOLOGY_FILE}" ]; then
  echo "[VKG] WARN: No ontology file found — starting in degraded mode"
  exec python3 /app/translate-server.py --port "${ENDPOINT_PORT}" --ontop-port "${ONTOP_PORT}" --ontology-version "none"
fi

# Detect mappings file by extension (supports .obda, .r2rml)
MAPPINGS_FILE=$(find "${ARTIFACT_DIR}" -maxdepth 1 \( -name "*.obda" -o -name "*.r2rml" \) | head -1)
if [ -z "${MAPPINGS_FILE}" ]; then
  echo "[VKG] WARN: No mappings file found — starting in degraded mode"
  exec python3 /app/translate-server.py --port "${ENDPOINT_PORT}" --ontop-port "${ONTOP_PORT}" --ontology-version "none"
fi

echo "[VKG] Ontology: ${ONTOLOGY_FILE}"
echo "[VKG] Mappings: ${MAPPINGS_FILE}"

# Detect schema file by extension (optional - for Ontop metadata extraction)
H2_DB_PATH="/app/ontology/vkgdb"
H2_URL="jdbc:h2:${H2_DB_PATH};DB_CLOSE_DELAY=-1"
SCHEMA_FILE=$(find "${ARTIFACT_DIR}" -maxdepth 1 -name "*.sql" | head -1)
if [ -n "${SCHEMA_FILE}" ]; then
  echo "[VKG] Loading schema: ${SCHEMA_FILE}"
  if ! java -cp "/opt/ontop/lib/*:/opt/ontop/jdbc/*" org.h2.tools.RunScript \
    -url "${H2_URL}" -user sa -password "" -script "${SCHEMA_FILE}" 2>/tmp/schema_error.log; then
    echo "[VKG] WARN: Cannot parse schema file ${SCHEMA_FILE} — starting in degraded mode"
    echo "[VKG] Schema error details:"
    cat /tmp/schema_error.log 2>/dev/null
    echo "[VKG] Fix the file in s3://${ONTOLOGY_BUCKET}/${ONTOLOGY_PREFIX}${NAMESPACE}/${VERSION}/ and restart the task"
    exec python3 /app/translate-server.py --port "${ENDPOINT_PORT}" --ontop-port "${ONTOP_PORT}" --ontology-version "none"
  fi
fi

# Generate Ontop properties
cat > "${ARTIFACT_DIR}/ontop.properties" <<EOF
jdbc.url=${H2_URL}
jdbc.driver=org.h2.Driver
jdbc.user=sa
jdbc.password=
EOF

# Extract ontology version
ONTOLOGY_VERSION=$(grep -oP 'owl:versionInfo\s+"?\K[^";\s]+' "${ONTOLOGY_FILE}" 2>/dev/null || echo "${VERSION}")
export ONTOLOGY_VERSION ONTOP_PORT
echo "[VKG] Ontology version: ${ONTOLOGY_VERSION}"

# Start Ontop endpoint (background, port 8081)
# --dev enables /ontop/reformulate endpoint (returns SQL without executing).
# Also enables auto-restart on config file changes — safe because our config
# is immutable after S3 download (no runtime modifications).
#
# Heap sizing: the Ontop launcher reads ONTOP_JAVA_ARGS (NOT JAVA_OPTS). The
# task definition sets ONTOP_JAVA_ARGS; for backward compatibility we fall back
# to a JAVA_OPTS value if only that is present, otherwise let the launcher use
# its built-in default. Without this the configured heap is silently ignored and
# Ontop runs at its ~512m default regardless of task memory (#149 cause C).
export ONTOP_JAVA_ARGS="${ONTOP_JAVA_ARGS:-${JAVA_OPTS:-}}"
echo "[VKG] Ontop heap args (ONTOP_JAVA_ARGS): '${ONTOP_JAVA_ARGS}'"
echo "[VKG] Starting Ontop on port ${ONTOP_PORT}..."
/opt/ontop/ontop endpoint \
  --ontology="${ONTOLOGY_FILE}" \
  --mapping="${MAPPINGS_FILE}" \
  --properties="${ARTIFACT_DIR}/ontop.properties" \
  --port="${ONTOP_PORT}" \
  --lazy \
  --dev &
ONTOP_PID=$!

# Wait for Ontop readiness
ONTOP_READY=false
for i in $(seq 1 90); do
  if ! kill -0 $ONTOP_PID 2>/dev/null; then
    wait $ONTOP_PID
    EXIT_CODE=$?
    echo "[VKG] ERROR: Ontop died with exit code ${EXIT_CODE}"
    exit 1
  fi
  if curl -sf "http://localhost:${ONTOP_PORT}/actuator/health" >/dev/null 2>&1; then
    ONTOP_READY=true
    break
  fi
  sleep 2
done
[ "${ONTOP_READY}" = true ] || { echo "[VKG] ERROR: Ontop failed to start within 180s"; exit 1; }
echo "[VKG] Ontop ready"

# Start translation API facade (foreground, port 8080)
echo "[VKG] Starting translation API on port ${ENDPOINT_PORT}..."
exec python3 /app/translate-server.py \
  --port "${ENDPOINT_PORT}" \
  --ontop-port "${ONTOP_PORT}" \
  --ontology-version "${ONTOLOGY_VERSION}" \
  --mappings-file "${MAPPINGS_FILE}"
