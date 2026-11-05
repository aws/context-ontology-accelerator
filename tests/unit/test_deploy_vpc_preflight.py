# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the deploy preflight's network quota check (``check-vpc-limit.sh``).

A new VPC takes one VPC, one Internet gateway and one Elastic IP (for its NAT
gateway), each a Region quota. The check fails a deploy when the region has no room
for one of them. That is only right when the deploy creates a VPC: an update reuses
the network stack's VPC, and ``SCL_VPC_ID`` imports one. Each test runs the script
against a stub ``aws``.
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
# Counts: AWS_STUB_{VPC,IGW,EIP}_COUNT. Quotas: AWS_STUB_QUOTA_<code>, else the AWS
# default of 5; "fail" makes that call fail.
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
  "ec2 describe-internet-gateways") echo "$AWS_STUB_IGW_COUNT" ;;
  "ec2 describe-addresses") echo "$AWS_STUB_EIP_COUNT" ;;
  "service-quotas get-service-quota")
    code="$(printf '%s\n' "$@" | grep -A1 -x -- --quota-code | tail -n 1)"
    quota_var="AWS_STUB_QUOTA_${code//-/_}"
    quota="${!quota_var:-5.0}"
    [ "$quota" = fail ] && exit 254
    echo "$quota"
    ;;
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
    aws_stub: tuple[Path, Path],
    *,
    vpc_count: int | str = 5,
    igw_count: int | str = 1,
    eip_count: int | str = 0,
    stack: str = "coa-dev-network",
    **extra_env: str,
) -> subprocess.CompletedProcess[str]:
    stub_dir, call_log = aws_stub
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "AWS_STUB_LOG": str(call_log),
        "AWS_STUB_VPC_COUNT": str(vpc_count),
        "AWS_STUB_IGW_COUNT": str(igw_count),
        "AWS_STUB_EIP_COUNT": str(eip_count),
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
    assert "Network quota reached in us-east-1: VPCs 5/5 (L-F678F1CE)." in result.stdout


def test_fresh_deploy_with_headroom_passes(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, vpc_count=4)

    assert result.returncode == 0
    assert (
        result.stdout.strip() == "Network quota headroom in us-east-1: VPCs 4/5, Internet gateways 1/5, Elastic IPs 0/5"
    )


def test_fresh_deploy_at_internet_gateway_quota_fails(aws_stub: tuple[Path, Path]) -> None:
    """Internet gateways can run out before VPCs: detached ones still count."""
    result = _run(aws_stub, vpc_count=2, igw_count=5)

    assert result.returncode == 1
    assert "Network quota reached in us-east-1: Internet gateways 5/5 (L-A4707A72)." in result.stdout


def test_fresh_deploy_at_elastic_ip_quota_fails(aws_stub: tuple[Path, Path]) -> None:
    """The NAT gateway's Elastic IP shares the quota with every other EIP in the region."""
    result = _run(aws_stub, vpc_count=1, eip_count=5)

    assert result.returncode == 1
    assert "Network quota reached in us-east-1: Elastic IPs 5/5 (L-0263D0A3)." in result.stdout
    assert "--service-code ec2 --quota-code L-0263D0A3" in _calls(aws_stub)


def test_every_full_quota_is_named(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, vpc_count=5, igw_count=5, eip_count=6)

    assert result.returncode == 1
    assert "VPCs 5/5 (L-F678F1CE), Internet gateways 5/5 (L-A4707A72), Elastic IPs 6/5 (L-0263D0A3)" in result.stdout


def test_applied_quota_increase_is_honoured(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, vpc_count=1, eip_count=5, AWS_STUB_QUOTA_L_0263D0A3="10.0")

    assert result.returncode == 0
    assert "Elastic IPs 5/10" in result.stdout


@pytest.mark.parametrize("quota", ["fail", "None"])
@pytest.mark.parametrize(
    ("name", "code", "counts", "count"),
    [
        ("VPCs", "L-F678F1CE", {"vpc_count": "5"}, "5"),
        ("Internet gateways", "L-A4707A72", {"vpc_count": "1", "igw_count": "5"}, "5"),
        ("Elastic IPs", "L-0263D0A3", {"vpc_count": "1", "eip_count": "6"}, "6"),
    ],
)
def test_unreadable_quota_warns_instead_of_failing(
    aws_stub: tuple[Path, Path], quota: str, name: str, code: str, counts: dict[str, str], count: str
) -> None:
    """The quota may be raised, so an unreadable one is not guessed at the default of 5."""
    env = {**counts, f"AWS_STUB_QUOTA_{code.replace('-', '_')}": quota}
    result = _run(aws_stub, **env)

    assert result.returncode == 2
    assert f"could not read the {name} quota ({code})" in result.stdout
    assert f"{name} {count}/?" in result.stdout
    assert "Network quota reached" not in result.stdout


def test_nothing_counted_says_so(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, vpc_count="?", igw_count="?", eip_count="?")

    assert result.returncode == 2
    assert "(nothing else counted)" in result.stdout


def test_unreadable_quota_still_reports_a_full_one(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, vpc_count=1, igw_count=5, AWS_STUB_QUOTA_L_0263D0A3="fail")

    assert result.returncode == 1
    assert "Network quota reached in us-east-1: Internet gateways 5/5 (L-A4707A72)." in result.stdout
    # The quota it could not check is still named.
    assert "Not checked: could not read the Elastic IPs quota (L-0263D0A3)." in result.stdout


def test_uncountable_resource_warns_instead_of_failing(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, vpc_count=1, eip_count="?")

    assert result.returncode == 2
    assert "Network quota check incomplete in us-east-1: could not count Elastic IPs" in result.stdout
    assert "VPCs 1/5, Internet gateways 1/5" in result.stdout


def test_full_quota_beats_an_uncountable_one(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, vpc_count="?", igw_count=5)

    assert result.returncode == 1
    assert "Internet gateways 5/5" in result.stdout
    assert "Not checked: could not count VPCs." in result.stdout


@pytest.mark.parametrize(
    "status",
    ["CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE", "IMPORT_ROLLBACK_COMPLETE"],
)
def test_update_at_quota_skips_the_limit(aws_stub: tuple[Path, Path], status: str) -> None:
    result = _run(aws_stub, AWS_STUB_VPC_STATUS=status)

    assert result.returncode == 0
    assert "update reuses the VPC in coa-dev-network" in result.stdout
    assert "ec2 describe-" not in _calls(aws_stub)


@pytest.mark.parametrize(
    "status",
    ["DELETE_COMPLETE", "CREATE_FAILED", "UPDATE_IN_PROGRESS", "UPDATE_FAILED", "UPDATE_ROLLBACK_FAILED", "None"],
)
def test_network_stack_without_a_settled_vpc_is_checked(aws_stub: tuple[Path, Path], status: str) -> None:
    """Only a settled VPC skips the check; deleted, failed and in-progress states are checked."""
    result = _run(aws_stub, AWS_STUB_VPC_STATUS=status)

    assert result.returncode == 1
    assert "Network quota reached" in result.stdout


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
