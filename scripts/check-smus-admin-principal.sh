#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Validate the implicit SMUS Admin-role fallback without collapsing AWS
# credential, authorization, and service failures into "role does not exist".
set -euo pipefail

if [ -n "${SCL_SMUS_ADMIN_ARNS:-}" ]; then
  exit 0
fi

AWS_ERROR_FILE="$(mktemp)"
trap 'rm -f "$AWS_ERROR_FILE"' EXIT

aws_error_reason() {
  local error_code
  error_code="$(sed -nE 's/.*error occurred \(([^)]*)\).*/\1/p' "$AWS_ERROR_FILE")"
  error_code="${error_code%%$'\n'*}"
  if [ -n "$error_code" ]; then
    printf '%s\n' "$error_code"
  elif grep -q "Unable to locate credentials" "$AWS_ERROR_FILE"; then
    printf '%s\n' "UnableToLocateCredentials"
  elif grep -Eq "config profile .* could not be found" "$AWS_ERROR_FILE"; then
    printf '%s\n' "ProfileNotFound"
  elif grep -Eq "Could not connect to the endpoint URL|Connect timeout on endpoint URL" "$AWS_ERROR_FILE"; then
    printf '%s\n' "EndpointConnectionError"
  elif grep -q "SSL validation failed" "$AWS_ERROR_FILE"; then
    printf '%s\n' "SSLValidationError"
  else
    # AWS CLI stderr should never be echoed wholesale here: third-party wrappers
    # can add environment diagnostics, including credential material.
    printf '%s\n' "AWS CLI command failed (details suppressed)"
  fi
}

if ! ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>"$AWS_ERROR_FILE")"; then
  echo "ERROR: Could not determine the AWS account with the active credentials." >&2
  echo "AWS reason: $(aws_error_reason)" >&2
  echo "Verify AWS_PROFILE, credential expiration, network access, and sts:GetCallerIdentity, then retry." >&2
  exit 1
fi

if ! [[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "ERROR: STS returned an invalid AWS account ID; refusing to infer an Admin-role ARN." >&2
  exit 1
fi

: >"$AWS_ERROR_FILE"
if ADMIN_ROLE_NAME="$(aws iam get-role --role-name Admin --query "Role.RoleName" --output text 2>"$AWS_ERROR_FILE")"; then
  if [ "$ADMIN_ROLE_NAME" != "Admin" ]; then
    echo "ERROR: IAM returned an unexpected result while verifying the fallback role 'Admin'." >&2
    exit 1
  fi
  echo "No SCL_SMUS_ADMIN_ARNS set — falling back to this account's existing 'Admin' role."
  exit 0
fi

AWS_REASON="$(aws_error_reason)"
if [ "$AWS_REASON" = "NoSuchEntity" ]; then
  echo "ERROR: No SMUS admin principal configured, and IAM confirmed that account $ACCOUNT_ID has no role named 'Admin'." >&2
  echo "Set SCL_SMUS_ADMIN_ARNS to the IAM role/user ARN(s) that human admins federate into," >&2
  echo "e.g. for an IAM Identity Center account, your permission set's federated role:" >&2
  echo "  SCL_SMUS_ADMIN_ARNS=arn:aws:iam::$ACCOUNT_ID:role/aws-reserved/sso.amazonaws.com/<region>/AWSReservedSSO_AdministratorAccess_<suffix> make deploy-dev" >&2
else
  echo "ERROR: No SMUS admin principal configured, and the fallback role 'Admin' could not be verified in account $ACCOUNT_ID." >&2
  echo "AWS reason: $AWS_REASON" >&2
  echo "Grant iam:GetRole for the fallback role, fix the AWS connection, or set SCL_SMUS_ADMIN_ARNS explicitly." >&2
fi
exit 1
