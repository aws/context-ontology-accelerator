# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``check-teardown-pending.sh``, the deploy preflight's check for an unfinished
teardown of the same prefix, run against a stub ``aws``."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check-teardown-pending.sh"

# Network stack status: AWS_STUB_STACK_STATUS, or "stack does not exist" when unset.
# Runtime-deleted-at tag on the runtime security groups: AWS_STUB_DELETED_AT ("None" when unset).
_AWS_STUB = """#!/usr/bin/env bash
set -u
printf '%s\\n' "$*" >>"$AWS_STUB_LOG"
case "$1 $2" in
  "sts get-caller-identity")
    [ -n "${AWS_STUB_NO_CREDS:-}" ] && exit 255
    echo "123456789012" ;;
  "cloudformation describe-stacks")
    [ -n "${AWS_STUB_STACK_ERROR:-}" ] && { echo "An error occurred (Throttling): Rate exceeded" >&2; exit 255; }
    if [ -z "${AWS_STUB_STACK_STATUS:-}" ]; then
      echo "An error occurred (ValidationError): Stack with id coa-dev-network does not exist" >&2
      exit 254
    fi
    echo "$AWS_STUB_STACK_STATUS"
    ;;
  "ec2 describe-security-groups") echo "${AWS_STUB_DELETED_AT:-None}" ;;
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


def _run(aws_stub: tuple[Path, Path], **extra_env: str) -> subprocess.CompletedProcess[str]:
    stub_dir, call_log = aws_stub
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "AWS_STUB_LOG": str(call_log),
    }
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(_SCRIPT), "us-east-1", "coa", "dev"], env=env, capture_output=True, text=True, check=False
    )


def test_no_network_stack_passes(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub)

    assert result.returncode == 0
    assert "No teardown of coa-dev pending" in result.stdout


@pytest.mark.parametrize("status", ["CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"])
def test_deployed_network_stack_passes(aws_stub: tuple[Path, Path], status: str) -> None:
    assert _run(aws_stub, AWS_STUB_STACK_STATUS=status).returncode == 0


@pytest.mark.parametrize("status", ["DELETE_FAILED", "DELETE_IN_PROGRESS"])
def test_unfinished_teardown_fails_and_points_at_another_prefix(aws_stub: tuple[Path, Path], status: str) -> None:
    result = _run(aws_stub, AWS_STUB_STACK_STATUS=status)

    assert result.returncode == 1
    assert f"coa-dev-network is {status}" in result.stdout
    assert "another SCL_PREFIX" in result.stdout
    assert "SCL_PREFIX=coa AWS_REGION=us-east-1 ./scripts/destroy.sh dev" in result.stdout


def test_unfinished_teardown_says_when_the_enis_go(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, AWS_STUB_STACK_STATUS="DELETE_FAILED", AWS_STUB_DELETED_AT="2030-01-01T09:00:00Z")

    assert result.returncode == 1
    # Up to 8 hours after the runtimes were deleted.
    assert "around 2030-01-01 17:00 UTC" in result.stdout


def test_unparseable_deletion_time_is_left_out(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, AWS_STUB_STACK_STATUS="DELETE_FAILED", AWS_STUB_DELETED_AT="not-a-time")

    assert result.returncode == 1
    assert "around" not in result.stdout


def test_without_credentials_the_check_is_skipped(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, AWS_STUB_NO_CREDS="1")

    assert result.returncode == 2
    assert "skipping pending-teardown check" in result.stdout


def test_a_failed_lookup_is_not_an_all_clear(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, AWS_STUB_STACK_ERROR="1")

    assert result.returncode == 2
    assert "Could not check for a pending teardown" in result.stdout
