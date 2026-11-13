# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash

# Source this from a deploy script that has already set CONTEXT=(--context "env=...").
# It appends one `--context key=value` pair for every SCL_* setting that is set, so
# deploy.sh and deploy-serve.sh synthesize with the same settings. CDK context is not
# persisted between deploys: a key a script leaves out goes back to its default.
# The SCL_* variables are documented at the top of scripts/deploy.sh.
# It also defines coa_add_jdbc_peering_context, which both scripts call.

[ -n "${SCL_PREFIX:-}" ]        && CONTEXT+=(--context "resource_prefix=$SCL_PREFIX")
[ -n "${SCL_PROJECT_TAG:-}" ]   && CONTEXT+=(--context "project_tag=$SCL_PROJECT_TAG")
[ -n "${SCL_VPC_ID:-}" ]        && CONTEXT+=(--context "vpc_id=$SCL_VPC_ID")
[ -n "${SCL_VPC_CIDR:-}" ]      && CONTEXT+=(--context "vpc_cidr=$SCL_VPC_CIDR")
[ -n "${SCL_AOSS_VPC_ENDPOINT_ID:-}" ] && CONTEXT+=(--context "aoss_vpc_endpoint_id=$SCL_AOSS_VPC_ENDPOINT_ID")
[ -n "${SCL_UI_DOMAIN:-}" ]     && CONTEXT+=(--context "ui_domain=$SCL_UI_DOMAIN")
[ -n "${SCL_UI_CERT_ARN:-}" ]   && CONTEXT+=(--context "ui_cert_arn=$SCL_UI_CERT_ARN")
[ -n "${SCL_API_DOMAIN:-}" ]    && CONTEXT+=(--context "api_domain=$SCL_API_DOMAIN")
[ -n "${SCL_API_CERT_ARN:-}" ]  && CONTEXT+=(--context "api_cert_arn=$SCL_API_CERT_ARN")
[ -n "${SCL_HOSTED_ZONE_ID:-}" ] && CONTEXT+=(--context "hosted_zone_id=$SCL_HOSTED_ZONE_ID")
[ -n "${SCL_DB_SCAN_ENRICHMENT_TIMEOUT_MINUTES:-}" ] && CONTEXT+=(--context "dbScanEnrichmentTimeoutMinutes=$SCL_DB_SCAN_ENRICHMENT_TIMEOUT_MINUTES")
[ -n "${SCL_TIER1_METRIC_TIMEOUT_SECONDS:-}" ] && CONTEXT+=(--context "tier1_metric_timeout_s=$SCL_TIER1_METRIC_TIMEOUT_SECONDS")
[ -n "${SCL_DEEP_REASONING_TIME_BUDGET_SECONDS:-}" ] && CONTEXT+=(--context "deep_reasoning_time_budget_s=$SCL_DEEP_REASONING_TIME_BUDGET_SECONDS")
[ -n "${SCL_DEEP_REASONING_SUFFICIENCY_MIN_CONFIDENCE:-}" ] && CONTEXT+=(--context "deep_reasoning_sufficiency_min_confidence=$SCL_DEEP_REASONING_SUFFICIENCY_MIN_CONFIDENCE")
[ -n "${SCL_DEEP_REASONING_PROGRESS_SIMILARITY_THRESHOLD:-}" ] && CONTEXT+=(--context "deep_reasoning_progress_similarity_threshold=$SCL_DEEP_REASONING_PROGRESS_SIMILARITY_THRESHOLD")
[ -n "${SCL_DEEP_REASONING_STABILITY_STEPS:-}" ] && CONTEXT+=(--context "deep_reasoning_stability_steps=$SCL_DEEP_REASONING_STABILITY_STEPS")
[ -n "${SCL_DEEP_REASONING_STOP_TELEMETRY:-}" ] && CONTEXT+=(--context "deep_reasoning_stop_telemetry=$SCL_DEEP_REASONING_STOP_TELEMETRY")
[ -n "${SCL_DEEP_REASONING_ONTOLOGY_TIMEOUT_SECONDS:-}" ] && CONTEXT+=(--context "deep_reasoning_ontology_timeout_s=$SCL_DEEP_REASONING_ONTOLOGY_TIMEOUT_SECONDS")
[ -n "${SCL_NEPTUNE_TIMEOUT_SECONDS:-}" ] && CONTEXT+=(--context "neptune_timeout_s=$SCL_NEPTUNE_TIMEOUT_SECONDS")
[ -n "${SCL_API_GATEWAY_CLOUDWATCH_ROLE:-}" ] && CONTEXT+=(--context "api_gateway_cloudwatch_role=$SCL_API_GATEWAY_CLOUDWATCH_ROLE")
[ -n "${SCL_RESOLVE_TIMEOUT_SECONDS:-}" ] && CONTEXT+=(--context "resolve_timeout_s=$SCL_RESOLVE_TIMEOUT_SECONDS")
[ -n "${SCL_NEPTUNE_INSTANCE_CLASS:-}" ] && CONTEXT+=(--context "neptune_instance_class=$SCL_NEPTUNE_INSTANCE_CLASS")
[ -n "${SCL_AOSS_MIN_OCU:-}" ] && CONTEXT+=(--context "aoss_min_ocu=$SCL_AOSS_MIN_OCU")
[ -n "${SCL_AOSS_MAX_OCU:-}" ] && CONTEXT+=(--context "aoss_max_ocu=$SCL_AOSS_MAX_OCU")
[ -n "${SCL_LAMBDA_RESERVED_CONCURRENCY:-}" ] && CONTEXT+=(--context "lambda_reserved_concurrency=$SCL_LAMBDA_RESERVED_CONCURRENCY")
[ -n "${SCL_GUARDRAIL_TIER:-}" ] && CONTEXT+=(--context "guardrail_tier=$SCL_GUARDRAIL_TIER")
[ -n "${SCL_GUARDRAIL_PROFILE:-}" ] && CONTEXT+=(--context "guardrail_profile=$SCL_GUARDRAIL_PROFILE")
[ -n "${SCL_NL2SQL_GRAPH_EXPAND:-}" ] && CONTEXT+=(--context "serve_nl2sql_graph_expand=$SCL_NL2SQL_GRAPH_EXPAND")
[ -n "${SCL_NL2SQL_GRAPH_EXPAND_MAX_TABLES:-}" ] && CONTEXT+=(--context "serve_nl2sql_graph_expand_max_tables=$SCL_NL2SQL_GRAPH_EXPAND_MAX_TABLES")
[ -n "${SCL_PREFER_IPV4:-}" ] && CONTEXT+=(--context "prefer_ipv4=$SCL_PREFER_IPV4")
[ -n "${SCL_SMUS_ADMIN_ARNS:-}" ] && CONTEXT+=(--context "smus_admin_principal_arns=$SCL_SMUS_ADMIN_ARNS")
[ -n "${SCL_GRAPH_SOURCES:-}" ] && CONTEXT+=(--context "graph_sources=$SCL_GRAPH_SOURCES")
# SCL_SERVE_SPARQL_STRATEGY was replaced by SCL_GRAPH_SOURCES and is no longer read.
[ -n "${SCL_SERVE_SPARQL_STRATEGY:-}" ] && echo "WARNING: SCL_SERVE_SPARQL_STRATEGY is no longer read; graph sources are now turned on with SCL_GRAPH_SOURCES=on." >&2
# Optional: enables the OoPS! pitfall scan, which sends the serialised ontology to this endpoint.
[ -n "${SCL_OOPS_ENDPOINT:-}" ] && CONTEXT+=(--context "oops_endpoint=$SCL_OOPS_ENDPOINT")

# Appends the JDBC peering context (jdbc_peer_vpc_id, jdbc_peer_cidrs) when a
# test-databases stack exists. Both the network stack and the serve stack read
# these keys, so every deploy script that deploys either one calls this.
coa_add_jdbc_peering_context() {
  # Prefix default matches the CDK app (see DEFAULT_RESOURCE_PREFIX).
  local TEST_STACK_NAME="${SCL_PREFIX:-coa}-integ-test-databases"
  if [ -z "${SCL_JDBC_PEER_VPC_ID:-}" ]; then
    SCL_JDBC_PEER_VPC_ID=$(aws cloudformation describe-stacks \
      --stack-name "$TEST_STACK_NAME" \
      --query "Stacks[0].Outputs[?OutputKey=='VpcId'].OutputValue" \
      --output text 2>/dev/null || echo "")
  fi
  if [ -z "${SCL_JDBC_PEER_CIDRS:-}" ]; then
    SCL_JDBC_PEER_CIDRS=$(aws cloudformation describe-stacks \
      --stack-name "$TEST_STACK_NAME" \
      --query "Stacks[0].Outputs[?OutputKey=='VpcCidr'].OutputValue" \
      --output text 2>/dev/null || echo "")
  fi
  if [ -n "${SCL_JDBC_PEER_VPC_ID:-}" ] && [ "$SCL_JDBC_PEER_VPC_ID" != "None" ] \
    && [ -n "${SCL_JDBC_PEER_CIDRS:-}" ] && [ "$SCL_JDBC_PEER_CIDRS" != "None" ]; then
    echo "Peering into test VPC ${SCL_JDBC_PEER_VPC_ID} (${SCL_JDBC_PEER_CIDRS})"
    CONTEXT+=(--context "jdbc_peer_vpc_id=$SCL_JDBC_PEER_VPC_ID" --context "jdbc_peer_cidrs=$SCL_JDBC_PEER_CIDRS")
  else
    # The stack is optional. Without these keys CloudFormation deletes any cross-VPC egress
    # rules a previous deploy created, hence the WARNING below.
    echo "No JDBC peering context (stack ${TEST_STACK_NAME} not found or has no VPC outputs) — skipping"
    echo "  WARNING: this REMOVES any cross-VPC JDBC egress rules an earlier deploy created."
    echo "           Deploy ${TEST_STACK_NAME} first if this environment runs JDBC integ tests."
  fi
}
