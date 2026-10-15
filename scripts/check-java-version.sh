#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Print a concise Java toolchain diagnosis and return non-zero unless a
# supported Java (17 through MAX_JAVA_MAJOR) is on PATH. Parse the actual
# version banner rather than assuming it is the first line: the JVM may
# prepend "Picked up JAVA_TOOL_OPTIONS".
#
# The upper bound comes from the Databricks connector, which deploy-dev builds
# and unit-tests under Maven: the Arrow/Netty that the Athena Federation SDK
# shades in fails on JDK 25 with a ClassCastException (PooledDirectByteBuf ->
# PooledUnsafeDirectByteBuf). Only the build is affected — the Lambda runs on
# the java21 runtime. Keep in step with requireJavaVersion in connectors/pom.xml.
set -uo pipefail

MIN_JAVA_MAJOR=17
MAX_JAVA_MAJOR=24
RECOMMENDED_JAVA_MAJOR=21
FIX_HINT="Run: mise install, or point JAVA_HOME at JDK $RECOMMENDED_JAVA_MAJOR (macOS: export JAVA_HOME=\$(/usr/libexec/java_home -v $RECOMMENDED_JAVA_MAJOR))"

if ! command -v java >/dev/null 2>&1; then
  echo "Java not found — required for Smithy code generation. Run: mise install"
  exit 1
fi

JAVA_VERSION_OUTPUT="$(java -version 2>&1)"
JAVA_COMMAND_STATUS=$?
if [ "$JAVA_COMMAND_STATUS" -ne 0 ]; then
  echo "Could not run 'java -version' (exit $JAVA_COMMAND_STATUS). Run: mise install"
  exit 1
fi

JAVA_VERSION="$(
  printf '%s\n' "$JAVA_VERSION_OUTPUT" \
    | sed -nE 's/^[[:space:]]*(openjdk|java) version "([^"]+)".*/\2/p' \
    | sed -n '1p'
)"
if [ -z "$JAVA_VERSION" ]; then
  echo "Could not parse the Java version from 'java -version' output. Run: mise install"
  exit 1
fi

case "$JAVA_VERSION" in
  1.*)
    JAVA_MAJOR="${JAVA_VERSION#1.}"
    JAVA_MAJOR="${JAVA_MAJOR%%[._+-]*}"
    ;;
  *)
    JAVA_MAJOR="${JAVA_VERSION%%[._+-]*}"
    ;;
esac

if ! [[ "$JAVA_MAJOR" =~ ^[0-9]+$ ]]; then
  echo "Could not parse a numeric Java major version from '$JAVA_VERSION'. Run: mise install"
  exit 1
fi

if [ "$JAVA_MAJOR" -lt "$MIN_JAVA_MAJOR" ]; then
  echo "Java $MIN_JAVA_MAJOR-$MAX_JAVA_MAJOR required (found $JAVA_MAJOR from version $JAVA_VERSION). $FIX_HINT"
  exit 1
fi

if [ "$JAVA_MAJOR" -gt "$MAX_JAVA_MAJOR" ]; then
  echo "Java $JAVA_MAJOR is not supported (found version $JAVA_VERSION): the Athena Federation SDK bundled in the Databricks connector fails its tests on it. Java $MIN_JAVA_MAJOR-$MAX_JAVA_MAJOR required, $RECOMMENDED_JAVA_MAJOR recommended. $FIX_HINT"
  exit 1
fi

echo "Java $JAVA_MAJOR found (version $JAVA_VERSION)"
