#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Onboards an "Playground" industry demo dataset INTO Context Ontology Accelerator via the API. This is
# distinct from provisioning the source infrastructure (that's the CDK
# deploy-demo-data job); here everything is API-driven and idempotent.
#
# INDUSTRY-GENERIC: all industry specifics (namespace, sources, schema filter,
# metrics, questions, documents) come from a declarative manifest —
# demo/<industry>/manifest.yaml — plus the data files it references. No
# industry-specific logic lives in this script. To onboard a different industry,
# add demo/<industry>/manifest.yaml (see demo/README.md) and pass --industry.
#
# Encapsulates the entire flow so it runs identically in CI (ci/demo.yml, using
# $DEMO_* creds) and locally (against coa-dev). Endpoints/pool/bucket are
# resolved from CloudFormation / SSM — no hardcoded endpoints — and every step
# is idempotent (safe to re-run).
#
# Steps:
#   1. Resolve API endpoint, Cognito pool/client, UI bucket, CloudFront dist.
#   2. Ensure an admin Cognito demo user + get an IdToken.
#   3. Ensure a READ-ONLY (platform-viewer) Cognito demo user so external
#      triers can explore without mutating maintained namespaces.
#   4. Create the namespace (from the manifest) if it doesn't exist.
#   5. Register each source declared in the manifest (from the demo-data-setup
#      stack outputs) if not already present.
#   6. Scan -> approve -> induce -> accept each DATABASE source (rejecting any
#      stale in-flight proposal first). SKIPS induction if the source already
#      has an accepted proposal — override with --force-induce.
#   7. Upload the manifest's documents to the demo docs bucket and register a
#      DOCUMENTS source (auto-ingests via doc-kg-build) for Tier-3 knowledge.
#   8. Onboard the Tier-1 metrics (from the manifest's metrics file).
#   9. Publish questions/{namespaceId}.json (from the manifest's questions file)
#      to the UI bucket + invalidate CloudFront so the playground shows chips.
#
# Safe to run on EVERY deploy: each step checks current state before acting.
#
# Usage (local, coa-dev):
#   ./scripts/onboard-demo.sh --industry insurance --profile coa-dev \
#       --region us-west-2 --prefix scl --env dev
#   Flags: --industry <name>     (default: insurance) selects demo/<name>/manifest.yaml
#          --manifest <path>     (overrides --industry with an explicit manifest)
#          --namespace-name / --display-name (override the manifest's namespace)
#          --skip-induction      (metrics+questions only, fast)
#          --force-induce        (re-induce even if already inducted)
#          --no-viewer           (skip seeding the read-only demo user)
#
# Usage (CI): env provides AWS creds; pass --industry/--prefix/--env/--region and
#   the Cognito demo password via COGNITO_PASSWORD (or --cognito-password).

set -euo pipefail

# Resolve this script's own directory + repo root so manifest/data paths load
# regardless of CWD (CI invokes this via an absolute path).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PROFILE=""
REGION="${AWS_DEFAULT_REGION:-us-west-2}"
# Keep in sync with DEFAULT_RESOURCE_PREFIX in libs/ts-shared/src/constants.ts.
PREFIX="${SCL_PREFIX:-coa}"
ENV_NAME="dev"
INDUSTRY="insurance"
MANIFEST=""              # explicit manifest path (else demo/$INDUSTRY/manifest.yaml)
NAMESPACE_NAME=""        # override manifest namespace.name
DISPLAY_NAME=""          # override manifest namespace.displayName
DEMO_DATA_STACK="demo-data-setup"
COGNITO_USER="demo-induct@scl.internal"
# Read-only demo user granted platform-viewer for external try-out access.
VIEWER_USER="demo-viewer@scl.internal"
COGNITO_PASSWORD="${COGNITO_PASSWORD:-}"
VIEWER_PASSWORD="${VIEWER_PASSWORD:-}"
# Optional explicit overrides (else auto-resolved):
API=""
USER_POOL_ID=""
CLIENT_ID=""
UI_BUCKET=""
DISTRIBUTION_ID=""
SKIP_INDUCTION="false"   # set true to only do metrics + questions
FORCE_INDUCE="false"     # set true to re-induce even if a source is already inducted
SEED_VIEWER="true"       # set false to skip the read-only user

while [[ $# -gt 0 ]]; do
  case $1 in
    --profile) PROFILE="$2"; shift 2;;
    --region) REGION="$2"; shift 2;;
    --prefix) PREFIX="$2"; shift 2;;
    --env) ENV_NAME="$2"; shift 2;;
    --industry) INDUSTRY="$2"; shift 2;;
    --manifest) MANIFEST="$2"; shift 2;;
    --namespace-name) NAMESPACE_NAME="$2"; shift 2;;
    --display-name) DISPLAY_NAME="$2"; shift 2;;
    --demo-data-stack) DEMO_DATA_STACK="$2"; shift 2;;
    --db-stack) DB_STACK="$2"; shift 2;;
    --user) COGNITO_USER="$2"; shift 2;;
    --viewer-user) VIEWER_USER="$2"; shift 2;;
    --cognito-password) COGNITO_PASSWORD="$2"; shift 2;;
    --api) API="${2%/}"; shift 2;;
    --user-pool-id) USER_POOL_ID="$2"; shift 2;;
    --client-id) CLIENT_ID="$2"; shift 2;;
    --ui-bucket) UI_BUCKET="$2"; shift 2;;
    --distribution-id) DISTRIBUTION_ID="$2"; shift 2;;
    --skip-induction) SKIP_INDUCTION="true"; shift;;
    --force-induce) FORCE_INDUCE="true"; shift;;
    --no-viewer) SEED_VIEWER="false"; shift;;
    *) echo "Unknown arg: $1" >&2; exit 1;;
  esac
done

command -v jq >/dev/null      || { echo "ERROR: jq required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 required (manifest parsing)" >&2; exit 1; }

# Convert a YAML file to JSON on stdout. PyYAML is not guaranteed on a bare
# python3 (the CI image is buildpack-deps; PyYAML lives only in the uv venv), so
# try, in order: plain python3, `uv run python` (CI's synced venv has PyYAML via
# metric-service/ontology-engine), then a one-shot `pip install pyyaml`. Fails
# loud if none work.
yaml_to_json() { # $1=yaml path -> JSON on stdout
  local f="$1" prog='import sys,yaml,json; json.dump(yaml.safe_load(open(sys.argv[1])), sys.stdout)'
  if python3 -c 'import yaml' 2>/dev/null; then
    python3 -c "$prog" "$f"; return
  fi
  if command -v uv >/dev/null 2>&1 && uv run --no-project python -c 'import yaml' 2>/dev/null; then
    uv run --no-project python -c "$prog" "$f"; return
  fi
  echo "==> PyYAML not found; installing it (pip install pyyaml)..." >&2
  python3 -m pip install --quiet --disable-pip-version-check pyyaml >&2 \
    || { echo "ERROR: could not install PyYAML for manifest parsing" >&2; return 1; }
  python3 -c "$prog" "$f"
}

# ─── Load + validate the manifest ─────────────────────────────────────────────
[[ -z "$MANIFEST" ]] && MANIFEST="${REPO_ROOT}/demo/${INDUSTRY}/manifest.yaml"
[[ -f "$MANIFEST" ]] || { echo "ERROR: manifest not found: $MANIFEST" >&2; exit 1; }
MANIFEST_DIR="$(cd "$(dirname "$MANIFEST")" && pwd)"

# Convert the YAML manifest to JSON once so the rest of the script uses jq.
MF="$(yaml_to_json "$MANIFEST")" \
  || { echo "ERROR: failed to parse manifest $MANIFEST" >&2; exit 1; }
mf() { echo "$MF" | jq -r "$1"; }        # scalar (empty string if null/missing)

# Resolve manifest-driven config (flags override).
[[ -z "$NAMESPACE_NAME" ]] && NAMESPACE_NAME="$(mf '.namespace.name')"
[[ -z "$DISPLAY_NAME"   ]] && DISPLAY_NAME="$(mf '.namespace.displayName // .namespace.name')"
SCHEMA_FILTER="$(mf '.data.postgres.schemaFilter // ""')"
DOCS_DIR_REL="$(mf '.data.documents.dir // ""')"
DOCS_S3_PREFIX="$(mf '.data.documents.s3Prefix // ""')"
METRICS_FILE="$(mf '.metrics // ""')"
QUESTIONS_FILE="$(mf '.questions // ""')"
[[ -z "$NAMESPACE_NAME" || "$NAMESPACE_NAME" == "null" ]] && { echo "ERROR: manifest namespace.name missing" >&2; exit 1; }

# Resolve a manifest-relative path to an absolute one.
mpath() { echo "${MANIFEST_DIR}/$1"; }

AWS=(aws --region "$REGION")
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")

cf_out()  { "${AWS[@]}" cloudformation describe-stacks --stack-name "$1" --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null; }
ssm()     { "${AWS[@]}" ssm get-parameter --name "$1" --query Parameter.Value --output text 2>/dev/null; }

echo "==> Industry: ${INDUSTRY}  (manifest: ${MANIFEST#"$REPO_ROOT"/})"
echo "==> Resolving environment (prefix=$PREFIX env=$ENV_NAME region=$REGION)..."
# `|| true` so a missing stack/param doesn't trip `set -e` before the friendly
# "could not resolve $k" validation loop below.
[[ -z "$API" ]]          && API=$(cf_out "${PREFIX}-${ENV_NAME}-api" ApiEndpoint || true); API="${API%/}"
[[ -z "$USER_POOL_ID" ]] && USER_POOL_ID=$(ssm "/${PREFIX}/userpool-id" || true)
[[ -z "$CLIENT_ID" ]]    && CLIENT_ID=$(ssm "/${PREFIX}/userpool-client-id" || true)
# The web stack has THREE buckets (UI assets + two CloudFront/UI access-log
# buckets). Select the UI-assets bucket by its logical id (PublicUIUIBucket*) —
# a bare [0] grabs the cf-logs bucket and the questions file lands where the
# playground never reads it.
[[ -z "$UI_BUCKET" ]]    && UI_BUCKET=$("${AWS[@]}" cloudformation describe-stack-resources --stack-name "${PREFIX}-${ENV_NAME}-web" --query "StackResources[?ResourceType=='AWS::S3::Bucket' && starts_with(LogicalResourceId, 'PublicUIUIBucket')]|[0].PhysicalResourceId" --output text 2>/dev/null)
# Fallback: match by name substring if the logical id ever changes.
{ [[ -z "$UI_BUCKET" || "$UI_BUCKET" == "None" ]]; } && UI_BUCKET=$("${AWS[@]}" cloudformation describe-stack-resources --stack-name "${PREFIX}-${ENV_NAME}-web" --query "StackResources[?ResourceType=='AWS::S3::Bucket' && contains(PhysicalResourceId, 'ui-assets')]|[0].PhysicalResourceId" --output text 2>/dev/null)
[[ -z "$DISTRIBUTION_ID" ]] && DISTRIBUTION_ID=$("${AWS[@]}" cloudformation describe-stack-resources --stack-name "${PREFIX}-${ENV_NAME}-web" --query "StackResources[?ResourceType=='AWS::CloudFront::Distribution']|[0].PhysicalResourceId" --output text 2>/dev/null)

for v in API:"$API" USER_POOL_ID:"$USER_POOL_ID" CLIENT_ID:"$CLIENT_ID" UI_BUCKET:"$UI_BUCKET"; do
  k="${v%%:*}"; val="${v#*:}"
  [[ -z "$val" || "$val" == "None" ]] && { echo "ERROR: could not resolve $k (pass it explicitly)" >&2; exit 1; }
done
echo "    API=$API"
echo "    userPool=$USER_POOL_ID client=$CLIENT_ID"
echo "    uiBucket=$UI_BUCKET dist=${DISTRIBUTION_ID:-<none>}"

# ─── Cognito users + token ────────────────────────────────────────────────────
gen_password() { # generate a compliant password
  echo "Demo!$("${AWS[@]}" secretsmanager get-random-password --password-length 16 --exclude-punctuation --query RandomPassword --output text 2>/dev/null || echo "$(date +%s)")Aa1"
}
ensure_user() { # $1=username $2=password [$3=group]
  local u="$1" p="$2" grp="${3:-}"
  "${AWS[@]}" cognito-idp admin-create-user --user-pool-id "$USER_POOL_ID" \
    --username "$u" --message-action SUPPRESS --temporary-password "$p" \
    --user-attributes Name=email,Value="$u" Name=email_verified,Value=true >/dev/null 2>&1 || true
  "${AWS[@]}" cognito-idp admin-set-user-password --user-pool-id "$USER_POOL_ID" \
    --username "$u" --password "$p" --permanent >/dev/null 2>&1 || true
  [[ -n "$grp" ]] && "${AWS[@]}" cognito-idp admin-add-user-to-group --user-pool-id "$USER_POOL_ID" \
    --username "$u" --group-name "$grp" >/dev/null 2>&1 || true
}
get_token() { # $1=username $2=password -> IdToken on stdout
  "${AWS[@]}" cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
    --client-id "$CLIENT_ID" --auth-parameters USERNAME="$1",PASSWORD="$2" \
    --query "AuthenticationResult.IdToken" --output text 2>/tmp/seed-auth.err
}

[[ -z "$COGNITO_PASSWORD" ]] && COGNITO_PASSWORD="$(gen_password)"
echo "==> Ensuring admin Cognito user ${COGNITO_USER}..."
ensure_user "$COGNITO_USER" "$COGNITO_PASSWORD" "Admin"
TOKEN=$(get_token "$COGNITO_USER" "$COGNITO_PASSWORD")
[[ -z "$TOKEN" || "$TOKEN" == "None" ]] && { echo "ERROR: auth failed:" >&2; cat /tmp/seed-auth.err >&2; exit 1; }
AUTH=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json")
echo "    token acquired."

api_get()  { curl -s "${API}$1" "${AUTH[@]}"; }
api_post() { curl -s -o /tmp/seed-resp.json -w "%{http_code}" -X POST "${API}$1" "${AUTH[@]}" -d "$2"; }

# ─── Read-only demo user (platform-viewer) ────────────────────────────────────
# Grants the read-only platform role so external triers can explore every demo
# namespace without being able to mutate the maintained ones. platform-viewer is
# a platform-scoped role bound to a principal via POST /grants (see
# create_platform_grant_handler.py). Idempotent: the grant returns 409 if it
# already exists.
if [[ "$SEED_VIEWER" == "true" ]]; then
  [[ -z "$VIEWER_PASSWORD" ]] && VIEWER_PASSWORD="$(gen_password)"
  echo "==> Ensuring read-only Cognito user ${VIEWER_USER} (platform-viewer)..."
  ensure_user "$VIEWER_USER" "$VIEWER_PASSWORD"
  # principalType is the PrincipalType enum value ("User", not "USER").
  VIEWER_GRANT=$(jq -nc --arg p "$VIEWER_USER" '{principalType:"User",principalId:$p,role:"platform-viewer"}')
  vcode=$(api_post "/grants" "$VIEWER_GRANT")
  case "$vcode" in
    201) echo "    granted platform-viewer to ${VIEWER_USER}.";;
    409) echo "    ${VIEWER_USER} already has platform-viewer.";;
    *)   echo "    [!] platform-viewer grant -> HTTP $vcode: $(head -c 200 /tmp/seed-resp.json)" >&2;;
  esac
fi

# ─── Namespace (create if absent) ─────────────────────────────────────────────
echo "==> Ensuring namespace '${NAMESPACE_NAME}'..."
NS=$(api_get "/namespaces" | jq -r --arg n "$NAMESPACE_NAME" '.namespaces[]? | select(.name==$n) | .namespaceId' | head -1)
if [[ -z "$NS" ]]; then
  body=$(jq -nc --arg n "$NAMESPACE_NAME" --arg o "$COGNITO_USER" --arg d "$DISPLAY_NAME" '{name:$n,owner:$o,displayName:$d}')
  code=$(api_post "/namespaces" "$body")
  [[ "$code" == "201" ]] || { echo "ERROR: create namespace HTTP $code: $(cat /tmp/seed-resp.json)" >&2; exit 1; }
  NS=$(jq -r '.namespace.namespaceId' /tmp/seed-resp.json)
  echo "    created namespace: $NS"
else
  echo "    reusing namespace: $NS"
fi

# ─── Resolve demo-data-setup connection details ───────────────────────────────
# Resolve only the outputs the manifest's sources actually need, so an industry
# with only documents (or only glue) doesn't require a Postgres cluster. Each
# `from` kind validates its own required outputs before it is used.
ACCOUNT=$("${AWS[@]}" sts get-caller-identity --query Account --output text)
manifest_has_from() { echo "$MF" | jq -e --arg f "$1" '[.sources[]? | select(.from==$f)] | length > 0' >/dev/null 2>&1; }

PG_HOST="" PG_DB="" PG_SECRET="" S3_GLUE_DB="" DOCS_BUCKET="" DOCS_BUCKET_ARN=""
# JDBC sources resolve from an explicit --db-stack (defaults to DEMO_DATA_STACK).
# Engine-specific outputs: PgClusterEndpoint/PgSecretArn, MysqlClusterEndpoint/MysqlSecretArn.
JDBC_PG_HOST="" JDBC_PG_SECRET="" JDBC_MYSQL_HOST="" JDBC_MYSQL_SECRET=""
JDBC_DB_STACK="${DB_STACK:-$DEMO_DATA_STACK}"

if manifest_has_from postgres; then
    PG_HOST=$(cf_out "$DEMO_DATA_STACK" PgClusterEndpoint)
    PG_DB=$(cf_out "$DEMO_DATA_STACK" PgDatabaseName)
    PG_SECRET=$(cf_out "$DEMO_DATA_STACK" PgSecretArn)
    [[ -z "$PG_HOST" || "$PG_HOST" == "None" ]] && { echo "ERROR: manifest has a postgres source but '$DEMO_DATA_STACK' PgClusterEndpoint output not found — deploy it first." >&2; exit 1; }
fi
if manifest_has_from jdbc; then
    # Resolve JDBC connection details from the DB stack (supports PG + MySQL)
    JDBC_PG_HOST=$(cf_out "$JDBC_DB_STACK" PgClusterEndpoint || true)
    JDBC_PG_SECRET=$(cf_out "$JDBC_DB_STACK" PgSecretArn || true)
    JDBC_MYSQL_HOST=$(cf_out "$JDBC_DB_STACK" MysqlClusterEndpoint || true)
    JDBC_MYSQL_SECRET=$(cf_out "$JDBC_DB_STACK" MysqlSecretArn || true)
fi
if manifest_has_from glue || manifest_has_from glue-static; then
    S3_GLUE_DB=$(cf_out "$DEMO_DATA_STACK" S3GlueDatabaseName || true)
    # For glue-static, the database name comes from the manifest directly
    [[ -z "$S3_GLUE_DB" || "$S3_GLUE_DB" == "None" ]] && S3_GLUE_DB=""
fi
if manifest_has_from documents; then
    DOCS_BUCKET=$(cf_out "$DEMO_DATA_STACK" DemoDocsBucketName)
    DOCS_BUCKET_ARN=$(cf_out "$DEMO_DATA_STACK" DemoDocsBucketArn)
fi

# ─── Source registration helpers ──────────────────────────────────────────────
sources_json() { api_get "/namespaces/$NS/sources"; }
find_source() { sources_json | jq -r --arg n "$1" '.items[]? | select(.name==$n) | .sourceId' | head -1; }

register_source() { # $1=name $2=body $3=comma-separated accepted codes -> sourceId
  local name="$1" body="$2" expect="$3"
  local sid; sid=$(find_source "$name")
  if [[ -n "$sid" ]]; then echo "$sid"; return; fi
  local code; code=$(api_post "/namespaces/$NS/sources" "$body")
  [[ ",$expect," == *",$code,"* ]] || { echo "ERROR: register source '$name' HTTP $code: $(cat /tmp/seed-resp.json)" >&2; exit 1; }
  jq -r '.sourceId' /tmp/seed-resp.json
}

# Build the registration body for a manifest source entry (by its `from` kind).
build_source_body() { # $1=name $2=from-kind
  local name="$1" from="$2"
  case "$from" in
    postgres)
      jq -nc --arg n "$name" --arg h "$PG_HOST" --arg db "$PG_DB" --arg sec "$PG_SECRET" --arg sf "$SCHEMA_FILTER" \
        '{sourceType:"DATABASE",databaseSource:{name:$n,jdbcConfiguration:({engine:"POSTGRESQL",host:$h,port:5432,databaseName:$db,credentialSecretArn:$sec} + (if $sf != "" then {schemaFilter:$sf} else {} end)),metadataEnrichmentEnabled:true}}';;
    jdbc)
      # Generic JDBC source — resolves host/secret from the DB stack based on engine.
      # Engine and database name come from the manifest source entry or data section.
      local engine; engine=$(echo "$MF" | jq -r --arg n "$name" '[.sources[] | select(.name==$n)][0].engine // "POSTGRESQL"')
      local db_name
      case "$engine" in
        POSTGRESQL)
          db_name=$(echo "$MF" | jq -r --arg n "$name" '[.sources[] | select(.name==$n)][0].database // .data.postgresql.database // "postgres"')
          local pg_sf; pg_sf=$(echo "$MF" | jq -r --arg n "$name" '[.sources[] | select(.name==$n)][0].schemaFilter // ""')
          jq -nc --arg n "$name" --arg h "$JDBC_PG_HOST" --arg db "$db_name" --arg sec "$JDBC_PG_SECRET" --arg sf "$pg_sf" \
            '{sourceType:"DATABASE",databaseSource:{name:$n,jdbcConfiguration:({engine:"POSTGRESQL",host:$h,port:5432,databaseName:$db,credentialSecretArn:$sec} + (if $sf != "" then {schemaFilter:$sf} else {} end)),metadataEnrichmentEnabled:true}}';;
        MYSQL)
          db_name=$(echo "$MF" | jq -r --arg n "$name" '[.sources[] | select(.name==$n)][0].database // .data.mysql.database // "mysql"')
          local mysql_sf; mysql_sf=$(echo "$MF" | jq -r --arg n "$name" '[.sources[] | select(.name==$n)][0].schemaFilter // ""')
          jq -nc --arg n "$name" --arg h "$JDBC_MYSQL_HOST" --arg db "$db_name" --arg sec "$JDBC_MYSQL_SECRET" --arg sf "$mysql_sf" \
            '{sourceType:"DATABASE",databaseSource:{name:$n,jdbcConfiguration:({engine:"MYSQL",host:$h,port:3306,databaseName:$db,credentialSecretArn:$sec} + (if $sf != "" then {schemaFilter:$sf} else {} end)),metadataEnrichmentEnabled:true}}';;
        *) echo "ERROR: jdbc source '$name' has unsupported engine: $engine" >&2; return 1;;
      esac;;
    glue|glue-static)
      # glue-static: databaseName comes from manifest (self-contained, no stack output needed)
      local glue_db
      if [[ "$from" == "glue-static" ]]; then
        glue_db=$(echo "$MF" | jq -r --arg n "$name" '[.sources[] | select(.name==$n)][0] as $s | .data.glue.databaseName // "default"')
      else
        glue_db="$S3_GLUE_DB"
      fi
      jq -nc --arg n "$name" --arg cid "$ACCOUNT" --arg r "$REGION" --arg db "$glue_db" \
        '{sourceType:"DATABASE",databaseSource:{name:$n,glueConfiguration:{catalogId:$cid,region:$r,databaseName:$db},metadataEnrichmentEnabled:true}}';;
    documents)
      jq -nc --arg n "$name" --arg arn "$DOCS_BUCKET_ARN" --arg p "$DOCS_S3_PREFIX" \
        '{sourceType:"DOCUMENTS",documentSource:{name:$n,sourceBucketArn:$arn,s3Prefixes:[$p]}}';;
    *) echo "ERROR: unknown source 'from' kind: $from" >&2; return 1;;
  esac
}

source_status() { api_get "/namespaces/$NS/sources/$1" | jq -r '.status'; }
poll_status() { # $1=sourceId $2=space-separated terminal states $3=timeout_s
  local sid="$1" terminals="$2" timeout="${3:-600}" waited=0 s
  while (( waited < timeout )); do
    s=$(source_status "$sid")
    for t in $terminals; do [[ "$s" == "$t" ]] && { echo "$s"; return; }; done
    sleep 15; waited=$((waited+15))
  done
  echo "TIMEOUT:$s"
}

reject_inflight() {
  local pending
  pending=$(api_get "/namespaces/$NS/proposals" | jq -r '.[]? | select(.status=="pending") | (.proposal_id // .proposalId // .id)')
  [[ -z "$pending" ]] && return
  local ids; ids=$(echo "$pending" | jq -R . | jq -s .)
  api_post "/namespaces/$NS/proposals/reject" "{\"proposal_ids\":$ids,\"reason\":\"onboard-demo: clearing in-flight before induction\"}" >/dev/null
  echo "    rejected stale in-flight proposal(s)."
}

induct_source() { # $1=sourceId $2=uri-suffix
  local sid="$1" suffix="$2"
  reject_inflight
  # Use a shared ontology URI prefix per namespace so all sources contribute to
  # the same ontology graph. The suffix is used only for the label (human-readable).
  local body
  body=$(jq -nc --arg sid "$sid" --arg uri "https://demo.coa/$NS/ontology#" --arg lbl "onboard-demo $suffix" \
    '{datasource_ids:[$sid],ontology_uri_prefix:$uri,label:$lbl,confidence_threshold:0.80,strategy:"table_to_ontology"}')
  local code; code=$(api_post "/namespaces/$NS/induce" "$body")
  [[ "$code" == "202" ]] || { echo "    induce HTTP $code: $(cat /tmp/seed-resp.json)"; return 1; }
  local job; job=$(jq -r '.job_id' /tmp/seed-resp.json)
  local waited=0 st
  while (( waited < 1200 )); do
    st=$(api_get "/namespaces/$NS/induce/jobs/$job" | jq -r '.status')
    case "$st" in completed) break;; failed|error) echo "    induction $job -> $st"; return 1;; esac
    sleep 15; waited=$((waited+15))
  done
  # accept the resulting pending proposal. If none is pending, an accepted
  # proposal already exists for this source (re-run) — treat as done.
  local prop; prop=$(api_get "/namespaces/$NS/proposals" | jq -r '[.[] | select(.status=="pending")][0] | (.proposal_id // .proposalId // .id)')
  [[ -z "$prop" || "$prop" == "null" ]] && { echo "    already inducted (no new proposal) — skipping accept"; return 0; }
  api_post "/namespaces/$NS/proposals/$prop/accept" "{}" >/dev/null
  waited=0
  while (( waited < 1200 )); do
    st=$(api_get "/namespaces/$NS/proposals/$prop" | jq -r '.status')
    case "$st" in accepted) echo "    accepted proposal $prop"; return 0;; failed|error) echo "    accept $prop -> $st"; return 1;; esac
    sleep 15; waited=$((waited+15))
  done
  echo "    accept timed out"; return 1
}

source_already_inducted() { # $1=sourceId — true if an ACCEPTED proposal covers it
  # Re-inducing is the slow + costly step AND re-accepting reverts the manual
  # multi-source VKG latest/ merge. So skip when this source already has
  # an accepted proposal, unless --force-induce is set. The proposals list is a
  # bare array (see reject_inflight); guard for a wrapped shape too.
  local sid="$1"
  api_get "/namespaces/$NS/proposals" \
    | jq -e --arg sid "$sid" '
        (if type == "array" then . else (.proposals // []) end) as $ps
        | [ $ps[]? | select(.status=="accepted")
              | (.metadata.datasourceIds // .metadata.datasource_ids // [])
              | index($sid) ] | any' >/dev/null 2>&1
}

approve_and_induct() { # $1=sourceId $2=uri-suffix
  local sid="$1" suffix="$2" s
  if [[ "$FORCE_INDUCE" != "true" ]] && source_already_inducted "$sid"; then
    echo "    [$suffix] already inducted (accepted proposal exists) — skipping (use --force-induce to re-run)."
    return 0
  fi
  s=$(source_status "$sid")
  if [[ "$s" == "REGISTERED" || "$s" == "SCANNING" || "$s" == "ENRICHING" ]]; then
    echo "    [$suffix] waiting for scan..."
    s=$(poll_status "$sid" "PENDING_REVIEW APPROVED SCAN_FAILED" 1200)
  fi
  [[ "$s" == "SCAN_FAILED"* || "$s" == TIMEOUT* ]] && { echo "    [$suffix] scan not usable: $s (check VPC peering / SG)"; return 1; }
  if [[ "$s" == "PENDING_REVIEW" ]]; then
    api_post "/namespaces/$NS/sources/$sid/approve" "{}" >/dev/null
    s=$(poll_status "$sid" "APPROVED APPROVAL_FAILED" 1200)
  fi
  [[ "$s" == "APPROVED" ]] || { echo "    [$suffix] not approved: $s"; return 1; }
  echo "    [$suffix] approved; inducting..."
  induct_source "$sid" "$suffix"
}

# ─── Register + induce sources declared in the manifest ───────────────────────
# Track the first POSTGRESQL source id — metrics attach to it (dataSourceId).
PG_SID=""
if [[ "$SKIP_INDUCTION" == "false" ]]; then
  echo "==> Registering + inducing sources from manifest..."
  while IFS=$'\t' read -r s_name s_type s_from s_engine s_suffix; do
    [[ -z "$s_name" ]] && continue
    case "$s_type" in
      DATABASE)
        body=$(build_source_body "$s_name" "$s_from")
        sid=$(register_source "$s_name" "$body" "202")
        echo "    [$s_from] source $s_name -> $sid"
        [[ "$s_engine" == "POSTGRESQL" && -z "$PG_SID" ]] && PG_SID="$sid"
        approve_and_induct "$sid" "$s_suffix" || echo "    ($s_name induction incomplete — continuing)"
        ;;
      DOCUMENTS)
        if [[ -z "$DOCS_BUCKET" || "$DOCS_BUCKET" == "None" ]]; then
          echo "==> WARN: DemoDocsBucketName not found on '$DEMO_DATA_STACK' — skipping docs source (Tier-3 unavailable)."
          continue
        fi
        docs_local="$(mpath "$DOCS_DIR_REL")"
        echo "==> Uploading documents from ${docs_local#"$REPO_ROOT"/} to s3://${DOCS_BUCKET}/${DOCS_S3_PREFIX} ..."
        "${AWS[@]}" s3 sync "$docs_local/" "s3://${DOCS_BUCKET}/${DOCS_S3_PREFIX}" --exclude "*" --include "*.md"
        body=$(build_source_body "$s_name" documents)
        sid=$(register_source "$s_name" "$body" "201,202")
        echo "    docs source: $sid"
        doc_st=$(source_status "$sid")
        if [[ "$doc_st" == "COMPLETED" ]]; then
          echo "    [docs] already ingested (COMPLETED) — skipping wait."
        else
          echo "    [docs] waiting for ingestion (doc-kg-build)..."
          doc_st=$(poll_status "$sid" "COMPLETED SCAN_FAILED" 1200)
          case "$doc_st" in
            COMPLETED) echo "    [docs] ingested (COMPLETED).";;
            *) echo "    (docs ingestion incomplete: $doc_st — Tier-3 may be degraded; continuing)";;
          esac
        fi
        ;;
      *) echo "    [!] unknown source type '$s_type' for '$s_name' — skipping" >&2;;
    esac
  done < <(echo "$MF" | jq -r '.sources[]? | [.name, .type, (.from // ""), (.engine // ""), (.inductionSuffix // "")] | @tsv')
else
  echo "==> Skipping induction (--skip-induction); resolving existing POSTGRESQL source for metrics..."
  pg_name=$(mf '[.sources[] | select(.engine=="POSTGRESQL") | .name][0]')
  PG_SID=$(find_source "$pg_name")
  [[ -z "$PG_SID" ]] && { echo "ERROR: no '$pg_name' source and --skip-induction set" >&2; exit 1; }
fi

# ─── Tier-1 metrics (create or update) from the manifest's metrics file ───────
# IMPORTANT: the serve layer passes the metric's SQL straight to the SQL firewall
# (orchestrator._run_tier1 → firewall.evaluate), which parses with read="trino"
# and REQUIRES a top-level SELECT. So each expression must be a FULL Trino SELECT
# statement. TRINO is dialects[0] because the resolver uses dialects[0] and the
# firewall reads Trino; the direct-JDBC path transpiles Trino→Postgres at exec.
if [[ -n "$METRICS_FILE" && "$METRICS_FILE" != "null" && -z "$PG_SID" ]]; then
  # Metrics attach to a POSTGRESQL source (dataSourceId). If the manifest
  # declares metrics but no such source resolved, skip rather than register
  # metrics with an empty dataSourceId (which would be unusable at query time).
  echo "==> WARN: metrics declared but no POSTGRESQL source resolved — skipping metric onboarding." >&2
elif [[ -n "$METRICS_FILE" && "$METRICS_FILE" != "null" ]]; then
  METRICS_PATH="$(mpath "$METRICS_FILE")"
  [[ -f "$METRICS_PATH" ]] || { echo "ERROR: metrics file not found: $METRICS_PATH" >&2; exit 1; }
  echo "==> Onboarding Tier-1 metrics from ${METRICS_PATH#"$REPO_ROOT"/} (dataSource=$PG_SID)..."
  put_metric() { # name desc trino_select table unit synonyms_json
    local body
    body=$(jq -n --arg name "$1" --arg d "$2" --arg tr "$3" --arg ds "$PG_SID" --arg tbl "$4" --arg unit "$5" --argjson syn "$6" \
      '{name:$name,description:$d,expression:{dialects:[{dialect:"TRINO",expression:$tr}]},dataSourceId:$ds,sourceTable:$tbl,unit:$unit,aiContext:{synonyms:$syn,instructions:"Demo metric."}}')
    local code; code=$(api_post "/namespaces/$NS/metrics" "$body")
    if [[ "$code" == "201" ]]; then echo "    [+] $1"
    elif [[ "$code" == "409" ]]; then curl -s -o /dev/null -X PUT "${API}/namespaces/$NS/metrics/$1" "${AUTH[@]}" -d "$body"; echo "    [~] $1 (updated)"
    else echo "    [!] $1 -> HTTP $code: $(head -c 200 /tmp/seed-resp.json)" >&2; fi
  }
  while IFS=$'\t' read -r m_name m_desc m_expr m_table m_unit m_syn; do
    [[ -z "$m_name" ]] && continue
    put_metric "$m_name" "$m_desc" "$m_expr" "$m_table" "$m_unit" "$m_syn"
  done < <(jq -r '.metrics[] | [.name, .description, .trinoExpression, .sourceTable, .unit, (.synonyms | @json)] | @tsv' "$METRICS_PATH")
else
  echo "==> No metrics file in manifest — skipping metric onboarding."
fi

# ─── Publish example questions to the UI bucket ───────────────────────────────
if [[ -n "$QUESTIONS_FILE" && "$QUESTIONS_FILE" != "null" ]]; then
  QUESTIONS_PATH="$(mpath "$QUESTIONS_FILE")"
  [[ -f "$QUESTIONS_PATH" ]] || { echo "ERROR: questions file not found: $QUESTIONS_PATH" >&2; exit 1; }
  KEY="questions/${NS}.json"
  echo "==> Publishing ${QUESTIONS_PATH#"$REPO_ROOT"/} -> s3://${UI_BUCKET}/${KEY} ..."
  jq . "$QUESTIONS_PATH" | "${AWS[@]}" s3 cp - "s3://${UI_BUCKET}/${KEY}" --content-type application/json --cache-control no-cache
  if [[ -n "$DISTRIBUTION_ID" && "$DISTRIBUTION_ID" != "None" ]]; then
    "${AWS[@]}" cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" --paths "/${KEY}" --query "Invalidation.Id" --output text | sed 's/^/    invalidation: /'
  fi
fi

echo ""
echo "Demo seed complete."
echo "  industry  : $INDUSTRY"
echo "  namespace : $NAMESPACE_NAME ($NS)"
echo "  admin user: $COGNITO_USER"
[[ "$SEED_VIEWER" == "true" ]] && echo "  viewer    : $VIEWER_USER (platform-viewer, read-only)"
[[ -n "$METRICS_FILE" && "$METRICS_FILE" != "null" ]] && echo "  metrics   : ${METRICS_FILE}"
[[ -n "$QUESTIONS_FILE" && "$QUESTIONS_FILE" != "null" ]] && echo "  questions : s3://${UI_BUCKET}/questions/${NS}.json"
