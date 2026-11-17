#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# Load the Ontop H2 validation schema with the least privilege H2 allows.
#
# usage: load-schema.sh SCHEMA_FILE H2_DB_PATH H2_CLASSPATH
#
# H2 hands a script the full SQL surface: CREATE ALIAS compiles and runs Java
# (i.e. OS command execution in this container), FILE_READ reads the task
# filesystem, RUNSCRIPT pulls in more SQL. All of those require admin rights.
# schema.sql is machine-generated from namespace-supplied metadata, so it is
# treated as untrusted input here — this is the second line of defence behind
# coa_ontology.proposals, which refuses to emit an rr:tableName literal that is
# not a SQL identifier path.
#
# Exactly one generated statement shape needs admin rights (CREATE SCHEMA, which
# H2 does not delegate). So admin runs only the lines matching that shape
# literally, and every other line runs as a user holding ALTER ANY SCHEMA —
# enough for CREATE TABLE in any schema, not enough for CREATE ALIAS, FILE_READ
# or RUNSCRIPT. Anything unexpected therefore lands in the unprivileged half and
# fails closed (the caller falls back to degraded mode) instead of running as sa.

SCHEMA_FILE="${1:?SCHEMA_FILE is required}"
H2_DB_PATH="${2:?H2_DB_PATH is required}"
H2_CLASSPATH="${3:?H2_CLASSPATH is required}"

# No DB_CLOSE_DELAY: setting it is admin-only, so the non-admin loader
# connection cannot reuse the URL Ontop is given.
H2_URL="jdbc:h2:${H2_DB_PATH}"

# One complete H2 identifier: a bare name, or a delimited name in which every
# interior quote is doubled. This is deliberately the same grammar as
# coa_common.constants.normalize_sql_ident_path, so a literal the generator
# accepted is a literal this matches.
H2_IDENT='("([^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
SCHEMA_DECL_RE="^CREATE SCHEMA IF NOT EXISTS ${H2_IDENT};\$"

WORK_DIR=$(mktemp -d)
trap 'rm -rf "${WORK_DIR}"' EXIT
ADMIN_SQL="${WORK_DIR}/admin.sql"
LOADER_SQL="${WORK_DIR}/loader.sql"

# Fresh random password per task; nothing outside this script ever needs it.
# Restricted to alphanumerics so it cannot terminate the SQL string literal.
LOADER_PASSWORD=$(head -c 32 /dev/urandom | base64 | tr -cd 'A-Za-z0-9')

{
  echo "CREATE USER IF NOT EXISTS VKG_LOADER PASSWORD '${LOADER_PASSWORD}';"
  echo "ALTER USER VKG_LOADER SET PASSWORD '${LOADER_PASSWORD}';"
  echo "GRANT ALTER ANY SCHEMA TO VKG_LOADER;"
  # grep exits 1 when nothing matches, which `set -e` would treat as fatal; an
  # empty half is normal (no qualified table names, or no tables at all).
  # Exit 1 ONLY: exit 2 means grep itself failed (unreadable file, I/O error,
  # bad regex), and `|| true` would turn that into an empty or truncated half
  # that RunScript then loads happily — a VKG with no tables and no warning.
  grep -E "${SCHEMA_DECL_RE}" "${SCHEMA_FILE}" || [ $? -eq 1 ]
} > "${ADMIN_SQL}"

grep -vE "${SCHEMA_DECL_RE}" "${SCHEMA_FILE}" > "${LOADER_SQL}" || [ $? -eq 1 ]

java -cp "${H2_CLASSPATH}" org.h2.tools.RunScript \
  -url "${H2_URL}" -user sa -password "" -script "${ADMIN_SQL}"

java -cp "${H2_CLASSPATH}" org.h2.tools.RunScript \
  -url "${H2_URL}" -user VKG_LOADER -password "${LOADER_PASSWORD}" -script "${LOADER_SQL}"
