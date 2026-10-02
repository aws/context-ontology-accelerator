#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Deploys the example Athena federation connector (connectors/example) into a COA
# environment, so integration tests have a federated source to query and a real deploy
# proves the sample.
#
# Not part of `make deploy-dev`: the connector stands in for something a CUSTOMER deploys
# in their own account, so a broken sample must not redden a platform deploy.
#
# Must run AFTER COA. The connector's stack grants invoke, spill-read and kms:Decrypt to
# COA's serve and discovery roles, reading their ARNs from SSM parameters the serve and
# sources stacks write.
#
# Usage:
#   ./scripts/deploy-example-connector.sh [env]              # default: dev
#   SCL_PREFIX=scl ./scripts/deploy-example-connector.sh dev # what the pipeline runs
#
# Optional:
#   FUNCTION_NAME_PREFIX   override the stack/Lambda name prefix (default <prefix>-<env>-)
#   EXAMPLE_BULK_ROWS      size the bulk_rows fixture past Athena's 6 MB limit to
#   EXAMPLE_BULK_ROW_BYTES exercise the S3 spill path; both default small
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

# Names the stack and the Lambda, so the pipeline's deployment and a developer's own
# cannot collide in one account, and both read like the rest of the environment:
# scl-dev-example-coa-connector. Permissions never depend on it — COA scopes invoke on
# the coa:connector tag, not on the name.
export FUNCTION_NAME_PREFIX="${FUNCTION_NAME_PREFIX:-${PREFIX}-${ENV}-}"
STACK_NAME="${FUNCTION_NAME_PREFIX}example-coa-connector"

# Checked here, not left to cdk synth: synth runs after `mvn package`, so an unusable name would
# otherwise cost a full fat-jar build first, and its message names the composed prefix rather than
# the argument or the override that produced it. Mirrors connectorFunctionName in the CDK toolkit.
if [[ ! "$STACK_NAME" =~ ^[A-Za-z][A-Za-z0-9-]*$ ]] || [ ${#STACK_NAME} -gt 64 ]; then
  echo "ERROR: '${STACK_NAME}' cannot name both a Lambda and a CloudFormation stack." >&2
  echo "       It must start with a letter, use only letters, digits and hyphens, and stay" >&2
  echo "       within 64 characters. Check the env argument and FUNCTION_NAME_PREFIX." >&2
  exit 1
fi

# Pin the region for the CDK CLI too, so it cannot resolve a different one from the
# active profile and deploy the connector where nothing will query it.
export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"

echo "=== Deploying the example connector ==="
echo "  Environment: ${ENV}"
echo "  Stack:       ${STACK_NAME}"
echo "  Region:      ${REGION}"
echo ""

# ── Resolve COA's two query roles ────────────────────────────────────────────
# Read, never guessed: both role names are CloudFormation-generated. Two roles because
# two COA components reach a connector through Athena — serve runs the queries, and
# discovery runs DESCRIBE, which is how the @pk / @fk tags are read at all. Grant only
# serve and the connector answers SELECT perfectly while no declared key is ever found.
read_param() {
  aws ssm get-parameter --name "$1" --region "$REGION" \
    --query Parameter.Value --output text 2>/dev/null || true
}

# Both paths carry ${ENV}: these two values become the connector's invoke resource policy
# plus s3:GetObject on its spill prefix and kms:Decrypt on its spill key, so an env-less
# name in a shared account would let a sibling environment's platform deploy hand its serve
# role a read on this environment's spilled query results.
SERVE_PARAM="${SSM_PREFIX}/${ENV}/serve/runtime-role-arn"
DISCOVERY_PARAM="${SSM_PREFIX}/${ENV}/sources/db-connector-role-arn"
SERVE_ROLE_ARN="$(read_param "$SERVE_PARAM")"
DISCOVERY_ROLE_ARN="$(read_param "$DISCOVERY_PARAM")"

# "None" is what the CLI prints for an empty --query result, so it means absent too.
missing=""
if [ -z "$SERVE_ROLE_ARN" ] || [ "$SERVE_ROLE_ARN" = "None" ]; then
  missing="${missing} ${SERVE_PARAM}"
fi
if [ -z "$DISCOVERY_ROLE_ARN" ] || [ "$DISCOVERY_ROLE_ARN" = "None" ]; then
  missing="${missing} ${DISCOVERY_PARAM}"
fi

if [ -n "$missing" ]; then
  echo "ERROR: COA does not appear to be deployed in this account/${REGION} under prefix '${PREFIX}'." >&2
  echo "Missing SSM parameter(s):${missing}" >&2
  echo "" >&2
  echo "Deploy COA first (make deploy-dev), or set SCL_PREFIX to the prefix it was deployed with." >&2
  exit 1
fi
export SERVE_ROLE_ARN DISCOVERY_ROLE_ARN

echo "  Serve role:     ${SERVE_ROLE_ARN}"
echo "  Discovery role: ${DISCOVERY_ROLE_ARN}"
echo ""

# ── Build and deploy ─────────────────────────────────────────────────────────
# connectors/ is a workspace of its own (see connectors/pnpm-workspace.yaml), so the
# repository-root install does not reach it and every pnpm command runs from there.
cd "$REPO_ROOT/connectors"

echo "--- Installing connector workspace dependencies ---"
pnpm install --frozen-lockfile

echo ""
echo "--- Building the connector jar ---"
# --fail-if-no-match: without it pnpm prints "No projects matched the filters" and exits 0, so a
# renamed package or a broken workspace glob turns the build AND the deploy below into no-ops
# while this script still reports success.
pnpm --filter coa-example-connector-cdk --fail-if-no-match run package

echo ""
echo "--- Deploying ${STACK_NAME} ---"
# `cdk` directly, not `pnpm run deploy`: extra flags do not survive that npm-script
# boundary, and without --require-approval the IAM diff stops on a confirmation prompt
# that a pipeline runner can never answer ("terminal (TTY) is not attached").
pnpm --filter coa-example-connector-cdk --fail-if-no-match exec \
  cdk deploy --require-approval never

echo ""
echo "=== Example connector deployed to ${ENV} ==="
# The stack deliberately creates no AWS::Athena::DataCatalog — the catalog belongs to
# whichever account runs the queries. The stack's RegisterCatalogCommand output above
# is the one command that makes it queryable.
echo "Not yet queryable: register it as an Athena data catalog using the stack's"
echo "RegisterCatalogCommand output."
