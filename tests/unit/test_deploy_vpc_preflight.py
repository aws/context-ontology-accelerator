# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the deploy preflight's VPC limit check (``check-vpc-limit.sh``).

The check fails a deploy when the region is at its VPC quota. That is only right
when the deploy creates a VPC: an update reuses the network stack's VPC, and
``SCL_VPC_ID`` imports one. Each test runs the script against a stub ``aws``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check-vpc-limit.sh"

# Network stack VPC status: AWS_STUB_VPC_STATUS, or "stack does not exist" when unset.
# VPC count: AWS_STUB_VPC_COUNT. The quota is the AWS default of 5.
_AWS_STUB = """#!/usr/bin/env bash
set -u
printf '%s\\n' "$*" >>"$AWS_STUB_LOG"
case "$1 $2" in
  "sts get-caller-identity") echo "123456789012" ;;
  "cloudformation list-stack-resources")
    if [ -z "${AWS_STUB_VPC_STATUS:-}" ]; then
      echo "An error occurred (ValidationError) when calling the ListStackResources operation: Stack does not exist" >&2
      exit 254
    fi
    echo "$AWS_STUB_VPC_STATUS"
    ;;
  "ec2 describe-vpcs") echo "$AWS_STUB_VPC_COUNT" ;;
  "service-quotas get-service-quota") echo "5.0" ;;
  *) echo "unexpected aws invocation: $*" >&2; exit 2 ;;
esac
"""


@pytest.fixture
def aws_stub(tmp_path: Path) -> tuple[Path, Path]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "aws"
    stub.write_text(_AWS_STUB)
    stub.chmod(0o755)
    return stub_dir, tmp_path / "aws-calls.log"


def _run(
    aws_stub: tuple[Path, Path], *, vpc_count: int = 5, stack: str = "coa-dev-network", **extra_env: str
) -> subprocess.CompletedProcess[str]:
    stub_dir, call_log = aws_stub
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "AWS_STUB_LOG": str(call_log),
        "AWS_STUB_VPC_COUNT": str(vpc_count),
    }
    env.pop("SCL_VPC_ID", None)
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(_SCRIPT), "us-east-1", stack], env=env, capture_output=True, text=True, check=False
    )


def _calls(aws_stub: tuple[Path, Path]) -> str:
    call_log = aws_stub[1]
    return call_log.read_text() if call_log.exists() else ""


def test_fresh_deploy_at_quota_fails(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub)

    assert result.returncode == 1
    assert "VPC limit reached: 5/5 VPCs in us-east-1" in result.stdout


def test_fresh_deploy_with_headroom_passes(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, vpc_count=4)

    assert result.returncode == 0
    assert "VPC headroom: 4/5 used in us-east-1" in result.stdout


@pytest.mark.parametrize(
    "status",
    ["CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE", "IMPORT_ROLLBACK_COMPLETE"],
)
def test_update_at_quota_skips_the_limit(aws_stub: tuple[Path, Path], status: str) -> None:
    result = _run(aws_stub, AWS_STUB_VPC_STATUS=status)

    assert result.returncode == 0
    assert "update reuses the VPC in coa-dev-network" in result.stdout
    assert "ec2 describe-vpcs" not in _calls(aws_stub)


@pytest.mark.parametrize(
    "status",
    ["DELETE_COMPLETE", "CREATE_FAILED", "UPDATE_IN_PROGRESS", "UPDATE_FAILED", "UPDATE_ROLLBACK_FAILED", "None"],
)
def test_network_stack_without_a_settled_vpc_is_checked(aws_stub: tuple[Path, Path], status: str) -> None:
    """Only a settled VPC skips the check; deleted, failed and in-progress states are checked."""
    result = _run(aws_stub, AWS_STUB_VPC_STATUS=status)

    assert result.returncode == 1
    assert "VPC limit reached" in result.stdout


def test_imported_vpc_skips_the_limit_without_calling_aws(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, SCL_VPC_ID="vpc-0abc")

    assert result.returncode == 0
    assert "SCL_VPC_ID imports vpc-0abc" in result.stdout
    assert _calls(aws_stub) == ""


def test_looks_up_the_given_network_stack(aws_stub: tuple[Path, Path]) -> None:
    _run(aws_stub, stack="acme-prod-network")

    assert "--stack-name acme-prod-network" in _calls(aws_stub)


def test_preflight_names_the_network_stack_from_prefix_and_env() -> None:
    """The stack name must match the CDK app's ``<prefix>-<env>``, with env passed by deploy.sh."""
    preflight = (_ROOT / "scripts" / "preflight-deploy.sh").read_text()
    deploy = (_ROOT / "scripts" / "deploy.sh").read_text()

    assert 'DEPLOY_ENV="${1:-dev}"' in preflight
    assert 'check-vpc-limit.sh" "$REGION" "${SCL_PREFIX:-coa}-${DEPLOY_ENV}-network"' in preflight
    assert './scripts/preflight-deploy.sh "$ENV"' in deploy
