#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Replace JARs bundled in the Ontop CLI with the pinned, patched versions listed
# in a manifest (see ontop-lib-overrides.txt for the rationale).
#
# usage: ontop-lib-overrides.sh <manifest> <ontop-lib-dir>
#   MAVEN_BASE  repository root to download from (default: Maven Central)
#
# For each manifest entry the pinned JAR is downloaded and its sha256 verified
# before it replaces every existing <artifactId>-<version>.jar; a mismatch fails
# the build and leaves lib/ without the unverified file. The script also refuses
# to replace a bundled JAR that is newer than the pin. JARs that the patched set
# makes obsolete are listed in REMOVE.
set -eu

MANIFEST="$1"
LIB="$2"
MAVEN_BASE="${MAVEN_BASE:-https://repo1.maven.org/maven2}"

# Thymeleaf 3.1 absorbed the java8time extras module; the 3.0-era JAR would
# sit on the classpath against 3.1 core, so it goes.
REMOVE="thymeleaf-extras-java8time"

for artifact in $REMOVE; do
  rm -f "$LIB/$artifact"-[0-9]*.jar
done

grep -Ev '^[[:space:]]*(#|$)' "$MANIFEST" | while read -r path sha256; do
  # <group path>/<artifactId>/<version>/<artifactId>-<version>.jar
  jar="${path##*/}"
  dir="${path%/*}"
  pinned="${dir##*/}"
  dir="${dir%/*}"
  artifact="${dir##*/}"

  # Never downgrade: a newer Ontop release may bundle a newer JAR than the pin.
  # Fail so the stale entry gets dropped instead of silently reverting it.
  for existing in "$LIB/$artifact"-[0-9]*.jar; do
    [ -e "$existing" ] || continue
    bundled="${existing##*/"$artifact"-}"
    bundled="${bundled%.jar}"
    newest="$(printf '%s\n%s\n' "$bundled" "$pinned" | sort -V | tail -1)"
    if [ "$bundled" != "$pinned" ] && [ "$newest" = "$bundled" ]; then
      echo "ontop-lib-overrides: bundled $artifact $bundled is newer than the pinned $pinned; drop its manifest entry" >&2
      exit 1
    fi
  done

  curl -fsSL "$MAVEN_BASE/$path" -o "$LIB/$jar.part"
  if ! echo "$sha256  $LIB/$jar.part" | sha256sum -c - >/dev/null 2>&1; then
    rm -f "$LIB/$jar.part"
    echo "ontop-lib-overrides: sha256 mismatch for $path" >&2
    exit 1
  fi
  # `<artifactId>-[0-9]*` keeps e.g. thymeleaf-3.x from matching thymeleaf-spring5-3.x.
  rm -f "$LIB/$artifact"-[0-9]*.jar
  mv "$LIB/$jar.part" "$LIB/$jar"
  echo "ontop-lib-overrides: $artifact -> $pinned (sha256 OK)"
done
