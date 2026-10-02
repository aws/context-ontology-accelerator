#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Deploys the Databricks SQL Warehouse connector (connectors/databricks) as a
# COA-OPERATED connector: one Lambda serving every DATABRICKS_SQL_WAREHOUSE source in
# one COA environment, resolving each source's workspace, warehouse, Unity Catalog
# catalog, schema and credential per request from the Parameter Store parameter for the
# Athena catalog it was invoked under.
#
# Not the same thing as deploy-example-connector.sh, which stands in for something a
# CUSTOMER deploys. This connector is COA's: registration resolves its ARN from SSM and
# fails the create if it is absent, so a release that ships the control plane without
# running this leaves the sub-type advertised and unusable.
#
# The same jar also deploys as a customer-deployed connector, from one pinned endpoint in the
# function's environment. Which shape a deploy gets is DATABRICKS_CONFIG_SOURCE, set below and
# read by one CDK app at synth. The reserved `-managed-` name segment is what keeps the two
# deployments apart in an account.
#
# Must run AFTER COA (make deploy-dev): the connector's stack reads COA's serve and
# discovery role ARNs from SSM to grant them invoke and spill read, and publishes its own
# ARN to a COA path the sources API reads at every source create.
#
# Usage:
#   ./scripts/deploy-managed-databricks-connector.sh [env]              # default: dev
#   SCL_PREFIX=scl ./scripts/deploy-managed-databricks-connector.sh dev # what the pipeline runs
set -euo pipefail

ENV="${1:-dev}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Default MUST match the CDK app's DEFAULT_RESOURCE_PREFIX
# (libs/ts-shared/src/constants.ts, resolved in infra/lib/context.ts), the same way
# deploy.sh and destroy.sh do. A mismatch reads /coa SSM parameters for an
# environment deployed as scl-* and reports COA as not deployed at all.
PREFIX="${SCL_PREFIX:-coa}"
SSM_PREFIX="/${PREFIX}"
# Resolution order matches CDK's own (infra/bin/app.ts treats CDK_DEFAULT_REGION as the
# source of truth), so the region the roles are read from is the region the stack lands in.
REGION="${CDK_DEFAULT_REGION:-${AWS_DEFAULT_REGION:-${AWS_REGION:-us-east-1}}}"

# Both this and PREFIX below end up in a CloudFormation stack name, which allows no underscores
# and must start with a letter — stricter than the other scripts' checks, which is deliberate.
if [[ ! "$ENV" =~ ^[a-zA-Z0-9-]+$ ]]; then
  echo "ERROR: env must be letters, digits and hyphens — it becomes part of a CloudFormation" >&2
  echo "       stack name, which allows no underscores. Got: $ENV" >&2
  exit 1
fi
if [[ ! "$PREFIX" =~ ^[a-zA-Z][a-zA-Z0-9-]*$ ]]; then
  echo "ERROR: SCL_PREFIX must start with a letter and contain only letters, digits and" >&2
  echo "       hyphens (it becomes part of a CloudFormation stack name), got: $PREFIX" >&2
  exit 1
fi

# ── The managed-mode contract ────────────────────────────────────────────────
# Two scalars, and the managed CDK app derives everything else from them: the per-source
# parameter prefix, the resource prefix each namespace's sts:ExternalId comes from, the
# deployment id every parameter is checked against, the function name, the role name and
# both deployment parameter paths. Two rather than one composed token because recovering
# them from a single "coa-dev-2-" is ambiguous between (coa, dev-2) and (coa-dev, 2), and
# this script permits hyphens in the environment name.
#
export COA_PREFIX="$PREFIX"
export COA_ENV_NAME="$ENV"

# The mode, exported rather than left to the npm script so it is visible here, next to the two
# variables only this mode uses. The CDK app reads it at synth to choose the stack, and sets the
# same value on the Lambda, where the jar reads it.
export DATABRICKS_CONFIG_SOURCE="coa-managed"

# ── The name, derived here only to report and check it ───────────────────────
# athena:CreateDataCatalog stores the handler ARN and never re-resolves it, so every
# Athena catalog COA creates embeds this function's ARN. Renaming the function later
# orphans every catalog already created, with no in-place repair — so the name is a pure
# function of the two variables above, derived by the managed app and never overridden.
#
# The `-managed-` segment tells this deployment apart from a customer-deployed connector in
# the same account, and is RESERVED: connectors/databricks/README.md tells a
# customer-deployed connector not to use it.
#
# FUNCTION_NAME_PREFIX is deliberately neither read nor exported. The managed app ignores
# it, so a stale value left in a developer's shell cannot rename COA's connector.
STACK_NAME="${PREFIX}-${ENV}-managed-databricks-coa-connector"

# Checked here, not left to cdk synth: synth runs after `mvn package`, so an unusable name would
# otherwise cost a full fat-jar build first, and its message names the composed prefix rather than
# the argument that produced it. Mirrors connectorFunctionName in the CDK toolkit.
if [[ ! "$STACK_NAME" =~ ^[A-Za-z][A-Za-z0-9-]*$ ]] || [ ${#STACK_NAME} -gt 64 ]; then
  echo "ERROR: '${STACK_NAME}' cannot name both a Lambda and a CloudFormation stack." >&2
  echo "       It must start with a letter, use only letters, digits and hyphens, and stay" >&2
  echo "       within 64 characters. Check the env argument and SCL_PREFIX — the" >&2
  echo "       '-managed-databricks-coa-connector' part is fixed and is 33 characters." >&2
  exit 1
fi

# ── Refuse a single-target variable ──────────────────────────────────────────
# The failure this closes: a developer with a stage-1 `.env` sourced in their shell runs THIS
# script when they meant scripts/deploy-example-connector.sh, and gets a correct managed
# connector rather than the single-endpoint one they were configuring. The managed app reads
# none of these, so they cannot reach the Lambda, and the jar refuses them if they arrive by
# some other route — what this adds is naming the variables before anything has been built,
# in the shell the mistake lives in.
#
# They are worth naming because of what they would mean if they ever DID reach a managed
# function: the connector would ignore the Athena catalog name entirely, so every namespace's
# catalog would resolve the one workspace and credential this stack was given, and the
# per-request catalog check could not see it — nothing would be bound to a catalog to check.
SINGLE_TARGET_VARS=(
  DATABRICKS_WORKSPACE_HOSTNAME
  DATABRICKS_HTTP_PATH
  DATABRICKS_CATALOG
  DATABRICKS_SCHEMA
  CREDENTIAL_SECRET_ARN
  CREDENTIAL_KMS_KEY_ARN
)
present=""
for var in "${SINGLE_TARGET_VARS[@]}"; do
  if [ -n "${!var:-}" ]; then
    present="${present} ${var}"
  fi
done
if [ -n "$present" ]; then
  echo "ERROR: these variables pin a connector to ONE Databricks endpoint and must not be" >&2
  echo "       set when deploying the COA-operated connector:${present}" >&2
  echo "" >&2
  echo "A managed connector resolves the workspace, warehouse, catalog, schema and" >&2
  echo "credential per request, from the parameter for the Athena catalog it was invoked" >&2
  echo "under. Left set, it would serve one workspace and one credential to every" >&2
  echo "namespace's catalog — a cross-namespace read." >&2
  echo "" >&2
  echo "A sourced connectors/.env from a customer-style (stage 1) deployment is the usual" >&2
  echo "cause. Unset them, or run scripts/deploy-example-connector.sh instead if you" >&2
  echo "meant to deploy a single-endpoint connector." >&2
  exit 1
fi

# Pin the region for the CDK CLI too, so it cannot resolve a different one from the
# active profile and deploy the connector where nothing will query it.
export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"

echo "=== Deploying the COA-operated Databricks connector ==="
echo "  Environment:   ${ENV}"
echo "  Stack:         ${STACK_NAME}"
echo "  Region:        ${REGION}"
echo "  Prefix:        ${COA_PREFIX}"
echo ""

# ── Resolve COA's two query roles and its VPC ───────────────────────────────
# Read, never guessed: both role names are CloudFormation-generated. Two roles because
# two COA components reach a connector through Athena — serve runs the queries, and
# discovery runs DESCRIBE, which is how the @pk / @fk tags are read at all. Grant only
# serve and the connector answers SELECT perfectly while no declared key is ever found.
read_param() {
  aws ssm get-parameter --name "$1" --region "$REGION" \
    --query Parameter.Value --output text 2>/dev/null || true
}

# Both paths carry ${ENV}, which is what makes reading them safe in an account where several
# environments share one prefix. These two values become the connector's
# lambda:InvokeFunction resource policy plus s3:GetObject on its spill prefix and kms:Decrypt
# on its spill key, so under an env-less name a sibling environment's platform deploy would
# hand its serve role a read on THIS environment's spilled query results and leave this
# environment's discovery role unable to run a single DESCRIBE.
SERVE_PARAM="${SSM_PREFIX}/${ENV}/serve/runtime-role-arn"
DISCOVERY_PARAM="${SSM_PREFIX}/${ENV}/sources/db-connector-role-arn"
SERVE_ROLE_ARN="$(read_param "$SERVE_PARAM")"
DISCOVERY_ROLE_ARN="$(read_param "$DISCOVERY_PARAM")"

# COA's VPC, which the connector runs in. Read like the roles, from this environment's own path,
# so the connector cannot land in a sibling environment's network. The CDK app refuses to synth
# without both.
VPC_PARAM="${SSM_PREFIX}/${ENV}/network/vpc-id"
SUBNETS_PARAM="${SSM_PREFIX}/${ENV}/network/private-subnet-ids"
CONNECTOR_VPC_ID="$(read_param "$VPC_PARAM")"
CONNECTOR_SUBNET_IDS="$(read_param "$SUBNETS_PARAM")"

# "None" is what the CLI prints for an empty --query result, so it means absent too.
missing=""
if [ -z "$SERVE_ROLE_ARN" ] || [ "$SERVE_ROLE_ARN" = "None" ]; then
  missing="${missing} ${SERVE_PARAM}"
fi
if [ -z "$DISCOVERY_ROLE_ARN" ] || [ "$DISCOVERY_ROLE_ARN" = "None" ]; then
  missing="${missing} ${DISCOVERY_PARAM}"
fi
if [ -z "$CONNECTOR_VPC_ID" ] || [ "$CONNECTOR_VPC_ID" = "None" ]; then
  missing="${missing} ${VPC_PARAM}"
fi
if [ -z "$CONNECTOR_SUBNET_IDS" ] || [ "$CONNECTOR_SUBNET_IDS" = "None" ]; then
  missing="${missing} ${SUBNETS_PARAM}"
fi

if [ -n "$missing" ]; then
  echo "ERROR: COA does not appear to be deployed in this account/${REGION} under prefix '${PREFIX}'." >&2
  echo "Missing SSM parameter(s):${missing}" >&2
  echo "" >&2
  echo "Deploy COA first (make deploy-dev), or set SCL_PREFIX to the prefix it was deployed with." >&2
  exit 1
fi
export SERVE_ROLE_ARN DISCOVERY_ROLE_ARN CONNECTOR_VPC_ID CONNECTOR_SUBNET_IDS

echo "  Serve role:     ${SERVE_ROLE_ARN}"
echo "  Discovery role: ${DISCOVERY_ROLE_ARN}"
echo "  VPC:            ${CONNECTOR_VPC_ID}"
echo "  Subnets:        ${CONNECTOR_SUBNET_IDS}"
echo ""

# ── Build and deploy ─────────────────────────────────────────────────────────
# connectors/ is a workspace of its own (see connectors/pnpm-workspace.yaml), so the
# repository-root install does not reach it and every pnpm command runs from there.
cd "$REPO_ROOT/connectors"

echo "--- Installing connector workspace dependencies ---"
pnpm install --frozen-lockfile

echo ""
echo "--- Building the jar and deploying ${STACK_NAME} ---"
# One `deploy` script for both modes: the mode is DATABRICKS_CONFIG_SOURCE, exported above, and the
# CDK app reads it. Its `predeploy` hook runs the maven package step, so the fat jar is built here
# rather than in a separate step above — which also means a hand-run cannot deploy a stale jar.
#
# --require-approval never: the IAM diff otherwise stops on a confirmation prompt a pipeline
# runner can never answer ("terminal (TTY) is not attached"). pnpm forwards flags after the
# script name, so this reaches `cdk deploy`.
#
# --fail-if-no-match: without it pnpm prints "No projects matched the filters" and exits 0,
# so a renamed package or a broken workspace glob turns the whole build and deploy into a
# no-op while this script still reports success.
pnpm --filter coa-databricks-connector-cdk --fail-if-no-match run deploy \
  --require-approval never

echo ""
echo "=== COA-operated Databricks connector deployed to ${ENV} ==="
# NOT the construct's RegisterCatalogCommand output, which is for a customer-deployed
# connector: for this sub-type COA creates the Athena data catalog itself, one per
# source, at source registration — deriving the name so two sources cannot collide, and
# writing the per-source parameter this connector then reads.
DEPLOYMENT_PREFIX="${SSM_PREFIX}/${ENV}/connectors/databricks/deployment"
FUNCTION_ARN_PARAM="${DEPLOYMENT_PREFIX}/function-arn"
ROLE_ARN_PARAM="${DEPLOYMENT_PREFIX}/role-arn"

echo "The stack published its function ARN to:"
echo "  ${FUNCTION_ARN_PARAM}"
echo ""

# ── The one value a human has to carry out of this deploy ────────────────────
# Every other published fact is read by a machine. This one is copied into an IAM trust
# policy BY HAND, by whoever owns the credential for each Databricks source, and the person
# who just ran this deploy is the person who has to send it to them.
#
# Resolved from SSM rather than from the stack's ConnectorRoleArn output, because the
# parameter is the durable copy anyone can re-read later.
CONNECTOR_ROLE_ARN="$(read_param "$ROLE_ARN_PARAM")"
echo "The connector's execution role — the principal each source's credential-access role"
echo "must TRUST, conditioned on that namespace's sts:ExternalId:"
if [ -n "$CONNECTOR_ROLE_ARN" ] && [ "$CONNECTOR_ROLE_ARN" != "None" ]; then
  echo "  ${CONNECTOR_ROLE_ARN}"
else
  # Not an error: the deploy succeeded, and the value is in the stack's ConnectorRoleArn
  # output and in the parameter either way. A read can still come back empty here — an
  # eventually-consistent GetParameter right after the write, or a deploy role that can
  # write the parameter without being able to read it back.
  echo "  (not readable from here just now — read it from ${ROLE_ARN_PARAM},"
  echo "   or from the ConnectorRoleArn output in the deploy log above)"
fi
echo "  Also at: ${ROLE_ARN_PARAM}"
echo ""
echo "Next: register a DATABRICKS_SQL_WAREHOUSE source through the API"
echo "(POST /namespaces/{namespaceId}/sources). COA resolves the FUNCTION ARN itself, creates"
echo "the Athena catalog and writes the source's parameter — there is no create-data-catalog"
echo "step to run by hand, and nothing further to deploy per source. The ROLE ARN is the one"
echo "value to pass on: each source's credential owner needs it before their role can be"
echo "assumed, and COA cannot supply it on their behalf."
