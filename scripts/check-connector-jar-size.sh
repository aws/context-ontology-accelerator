#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Fails if the Databricks connector's shaded jar has grown past the CI ceiling, a tripwire
# below Lambda's 250 MB limit on a deployment package's EXTRACTED size. A nested jar is not
# extracted, so the jar's own size binds. Budget: connectors/databricks/pom.xml.
#
# Run from connectors/ after `mvn clean package`: a jar shaded twice is inflated.
set -euo pipefail

CEILING=${CEILING:-209715200}
LAMBDA_LIMIT=262144000
TARGET_DIR=${1:-databricks/target}

# Matched rather than named: the file carries the artifactId and version, so a rename or a
# version bump would otherwise fail as a missing file instead of a size verdict. The shade
# plugin leaves the pre-shade jar as original-*.jar, the one exclusion. Any other second
# jar means this pattern stopped meaning "the shaded artifact", so fail on it.
JARS=$(find "$TARGET_DIR" -maxdepth 1 -name '*.jar' ! -name 'original-*')
COUNT=$(printf '%s\n' "$JARS" | grep -c . || true)
if [ "$COUNT" -ne 1 ]; then
  echo "ERROR: expected exactly one shaded jar in ${TARGET_DIR}, found ${COUNT}:" >&2
  printf '%s\n' "$JARS" >&2
  exit 1
fi

SIZE=$(wc -c < "$JARS" | tr -dc '0-9')
echo "${JARS}: ${SIZE} bytes (ceiling ${CEILING}, Lambda's limit ${LAMBDA_LIMIT})"
if [ "$SIZE" -ge "$CEILING" ]; then
  echo "ERROR: ${JARS} is over the CI ceiling. Trim the jar, or raise CEILING deliberately." >&2
  exit 1
fi
