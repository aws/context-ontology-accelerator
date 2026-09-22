#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Print a concise Java toolchain diagnosis and return non-zero when Java 17+
# is unavailable. Parse the actual version banner rather than assuming it is
# the first line: the JVM may prepend "Picked up JAVA_TOOL_OPTIONS".
set -uo pipefail

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

if [ "$JAVA_MAJOR" -lt 17 ]; then
  echo "Java 17+ required (found $JAVA_MAJOR from version $JAVA_VERSION). Run: mise install"
  exit 1
fi

echo "Java $JAVA_MAJOR found (version $JAVA_VERSION)"
