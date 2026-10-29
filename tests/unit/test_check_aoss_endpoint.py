# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the deploy preflight's OpenSearch Serverless endpoint check.

``check-aoss-endpoint.sh`` stops a deploy into an existing VPC unless
``SCL_AOSS_VPC_ENDPOINT_ID`` is an available ``aoss-data`` endpoint in
``SCL_VPC_ID`` with private DNS enabled. It must fail only when EC2 says the
endpoint is wrong: a call that could not be made (no permission, throttling)
only warns. Each test runs the script against a stub ``aws``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check-aoss-endpoint.sh"

_VPC = "vpc-0abc123"
_ENDPOINT = "vpce-0abc123def4567890"
_GOOD = f"{_VPC}\tcom.amazonaws.us-east-1.aoss-data\tTrue\tavailable"

# describe-vpc-endpoints prints AWS_STUB_ENDPOINT, or fails with AWS_STUB_ERROR
# (exit 254, the CLI's service-error code) when that is set. The endpoint ID the
# stub received is logged so tests can check what the script passed.
_AWS_STUB = """#!/usr/bin/env bash
set -u
printf '%s\\n' "$*" >>"$AWS_STUB_LOG"
case "$1 $2" in
  "sts get-caller-identity") echo "123456789012" ;;
  "ec2 describe-vpc-endpoints")
    if [ -n "${AWS_STUB_ERROR:-}" ]; then
      echo "" >&2
      echo "An error occurred ($AWS_STUB_ERROR) when calling the DescribeVpcEndpoints operation: stub message" >&2
      exit 254
    fi
    printf '%s\\n' "$AWS_STUB_ENDPOINT"
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
    vpc_id: str | None = _VPC,
    endpoint_id: str | None = _ENDPOINT,
    described: str = _GOOD,
    error: str | None = None,
) -> subprocess.CompletedProcess[str]:
    stub_dir, call_log = aws_stub
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "AWS_STUB_LOG": str(call_log),
        "AWS_STUB_ENDPOINT": described,
    }
    for key in ("SCL_VPC_ID", "SCL_AOSS_VPC_ENDPOINT_ID", "AWS_STUB_ERROR"):
        env.pop(key, None)
    if vpc_id is not None:
        env["SCL_VPC_ID"] = vpc_id
    if endpoint_id is not None:
        env["SCL_AOSS_VPC_ENDPOINT_ID"] = endpoint_id
    if error is not None:
        env["AWS_STUB_ERROR"] = error
    return subprocess.run(["bash", str(_SCRIPT), "us-east-1"], env=env, capture_output=True, text=True, check=False)


def _calls(aws_stub: tuple[Path, Path]) -> str:
    call_log = aws_stub[1]
    return call_log.read_text() if call_log.exists() else ""


def test_valid_endpoint_passes(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub)

    assert result.returncode == 0
    assert f"{_ENDPOINT}: aoss-data in {_VPC}" in result.stdout


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("InvalidVpcEndpointId.NotFound", "was not found in us-east-1"),
        ("InvalidVpcEndpointId.Malformed", "is not a valid VPC endpoint ID"),
    ],
)
def test_endpoint_ec2_rejects_fails(aws_stub: tuple[Path, Path], error: str, expected: str) -> None:
    result = _run(aws_stub, error=error)

    assert result.returncode == 1
    assert expected in result.stdout


@pytest.mark.parametrize("error", ["UnauthorizedOperation", "RequestLimitExceeded", "RequestExpired"])
def test_endpoint_that_cannot_be_checked_warns(aws_stub: tuple[Path, Path], error: str) -> None:
    # Says nothing about the endpoint, so it must not block the deploy.
    result = _run(aws_stub, error=error)

    assert result.returncode == 2
    assert result.stdout.startswith(f"Could not check SCL_AOSS_VPC_ENDPOINT_ID {_ENDPOINT}")
    assert "not found" not in result.stdout


@pytest.mark.parametrize(
    ("described", "expected"),
    [
        (
            "vpc-0other99\tcom.amazonaws.us-east-1.aoss-data\tTrue\tavailable",
            f"is in vpc-0other99, not SCL_VPC_ID {_VPC}",
        ),
        (
            f"{_VPC}\tcom.amazonaws.us-east-1.sts\tTrue\tavailable",
            "is for com.amazonaws.us-east-1.sts, not com.amazonaws.us-east-1.aoss-data",
        ),
        (f"{_VPC}\tcom.amazonaws.us-east-1.aoss-data\tFalse\tavailable", "has private DNS disabled"),
        (f"{_VPC}\tcom.amazonaws.us-east-1.aoss-data\tTrue\tpending", "is pending, not available"),
    ],
)
def test_endpoint_that_cannot_serve_the_collection_fails(
    aws_stub: tuple[Path, Path], described: str, expected: str
) -> None:
    result = _run(aws_stub, described=described)

    assert result.returncode == 1
    assert expected in result.stdout


def test_surrounding_whitespace_is_trimmed_like_synth(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, vpc_id=f" {_VPC} ", endpoint_id=f" {_ENDPOINT}\t")

    assert result.returncode == 0
    assert f"--vpc-endpoint-ids {_ENDPOINT} " in _calls(aws_stub)


def test_vpc_without_endpoint_fails_without_calling_aws(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, endpoint_id=None)

    assert result.returncode == 1
    assert "SCL_VPC_ID requires SCL_AOSS_VPC_ENDPOINT_ID" in result.stdout
    assert _calls(aws_stub) == ""


def test_endpoint_without_vpc_fails(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, vpc_id=None)

    assert result.returncode == 1
    assert "only applies to a deploy into an existing VPC" in result.stdout


def test_created_vpc_is_skipped(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, vpc_id=None, endpoint_id=None)

    assert result.returncode == 0
    assert "skipped" in result.stdout
    assert _calls(aws_stub) == ""
