#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# One-line Maven diagnosis for the scripts that build a connector jar (deploy-all.sh
# and the connector deploy scripts). Prints the Maven version and its JDK on stdout,
# or WARN/ERROR on stderr. Exits 1 only when the build would fail: no mvn, mvn doesn't
# run, Maven too old, or an unsupported JDK.
# Maven uses JAVA_HOME, not the `java` that check-java-version.sh checks (brew's mvn
# pins the newest OpenJDK), so check both.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# connectors/pom.xml's maven-enforcer-plugin (3.5.0) needs Maven 3.6.3 or newer.
MIN_MAVEN="3.6.3"
fail() { echo "ERROR: $*" >&2; exit 1; }
# Run from the repo root: mise shims pick mvn and java by the .mise.toml there, as the build does.
cd "$SCRIPT_DIR/.." || fail "Could not cd to the repo root"

MVN_PATH="$(command -v mvn)" ||
  fail "Maven (mvn) not found — required to build the connector jar. Run: mise install (Maven and a supported JDK from .mise.toml)"
# Not just "mise install": that changes nothing while another mvn comes first on PATH,
# and mise's Maven runs on whatever JAVA_HOME is.
FIX_HINT="Set JAVA_HOME to a JDK 17-24, or use mise's Maven and JDK: mise install, then mise exec -- make <target>"

# First line of `mvn -v` output that matches a sed -E pattern, with \1 kept.
mvn_v_field() { sed -nE "s/$1/\\1/p" <<<"$MVN_VERSION_OUTPUT" | head -n 1; }

# True when dotted version $1 is lower than $2, comparing numeric parts.
version_lt() {
  local i a b
  IFS=. read -ra a <<<"$1"
  IFS=. read -ra b <<<"$2"
  for ((i = 0; i < ${#a[@]} || i < ${#b[@]}; i++)); do
    ((10#${a[i]:-0} < 10#${b[i]:-0})) && return 0
    ((10#${a[i]:-0} > 10#${b[i]:-0})) && return 1
  done
  return 1
}

if ! MVN_VERSION_OUTPUT="$(mvn -v 2>&1)"; then
  # Skip the JVM's "Picked up JAVA_TOOL_OPTIONS: …" banner, which hides the real error.
  first_line="$(grep -v '^Picked up ' <<<"$MVN_VERSION_OUTPUT" | head -n 1)"
  first_line="${first_line%[.,:;]}"
  fail "Could not run '$MVN_PATH -v': ${first_line:-it printed nothing}. $FIX_HINT"
fi

MVN_VERSION="$(mvn_v_field '^Apache Maven ([^ ]+).*')"
# Only a version we can read is compared; an unreadable one warns below.
MVN_RELEASE="$(grep -oE '^[0-9]+(\.[0-9]+)*' <<<"$MVN_VERSION")"
if [ -n "$MVN_RELEASE" ] && version_lt "$MVN_RELEASE" "$MIN_MAVEN"; then
  fail "Maven $MVN_VERSION ($MVN_PATH) is too old: the connector build needs Maven $MIN_MAVEN or newer. Run: mise install (Maven from .mise.toml), then mise exec -- make <target>"
fi
MVN_JAVA_HOME="$(mvn_v_field '^Java version: .*, runtime: (.+)$')"
# The "runtime:" line is in `mvn -v` only since Maven 3.5, so an unknown format
# isn't proof of a broken setup: warn and let the build decide.
if [ -z "$MVN_JAVA_HOME" ] || [ ! -x "$MVN_JAVA_HOME/bin/java" ]; then
  echo "WARN: Maven ${MVN_VERSION:-?} found ($MVN_PATH), but could not tell which JDK it runs on from 'mvn -v' — skipping the JDK check. The connector build needs JDK 17-24." >&2
  exit 0
fi

JAVA_CHECK="$(PATH="$MVN_JAVA_HOME/bin:$PATH" JAVA_FIX_HINT="$FIX_HINT" "$SCRIPT_DIR/check-java-version.sh")" ||
  fail "Maven ${MVN_VERSION:-?} ($MVN_PATH) runs on $MVN_JAVA_HOME: $JAVA_CHECK"

# check-java-version.sh says "Java 21 found (version …)"; reword it as "running on Java 21 (version …)".
echo "Maven ${MVN_VERSION:-?} found, running on ${JAVA_CHECK/ found/}"
