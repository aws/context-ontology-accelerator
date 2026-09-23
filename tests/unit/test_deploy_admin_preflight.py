# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
_SCRIPT = _ROOT / "scripts" / "check-smus-admin-principal.sh"
_ACCOUNT = "123456789012"
_SENSITIVE_MARKER = "AKIA_DO_NOT_ECHO"


@pytest.fixture
def aws_stub(tmp_path: Path) -> tuple[Path, Path]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    call_log = tmp_path / "aws-calls.log"
    stub = stub_dir / "aws"
    stub.write_text(
        f"""#!/usr/bin/env bash
set -u
printf '%s\\n' "$*" >>"$AWS_STUB_LOG"

if [ "$1 $2" = "sts get-caller-identity" ]; then
  case "$AWS_STUB_SCENARIO" in
    sts_failure)
      echo "Unable to locate credentials. You can configure credentials by running aws configure." >&2
      echo "{_SENSITIVE_MARKER}" >&2
      exit 255
      ;;
    sts_expired)
      echo "An error occurred (ExpiredToken) when calling the GetCallerIdentity operation: expired" >&2
      echo "{_SENSITIVE_MARKER}" >&2
      exit 254
      ;;
    *)
      echo "{_ACCOUNT}"
      exit 0
      ;;
  esac
fi

if [ "$1 $2" = "iam get-role" ]; then
  case "$AWS_STUB_SCENARIO" in
    existing)
      echo "Admin"
      exit 0
      ;;
    missing)
      echo "An error occurred (NoSuchEntity) when calling the GetRole operation: Role not found" >&2
      exit 254
      ;;
    denied)
      echo "An error occurred (AccessDenied) when calling the GetRole operation: denied" >&2
      echo "{_SENSITIVE_MARKER}" >&2
      exit 254
      ;;
    network)
      echo "Could not connect to the endpoint URL: https://iam.example.invalid/" >&2
      echo "{_SENSITIVE_MARKER}" >&2
      exit 255
      ;;
  esac
fi

echo "unexpected aws invocation: $*" >&2
exit 2
"""
    )
    stub.chmod(0o755)
    return stub_dir, call_log


def _run(aws_stub: tuple[Path, Path], scenario: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
    stub_dir, call_log = aws_stub
    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "AWS_STUB_LOG": str(call_log),
        "AWS_STUB_SCENARIO": scenario,
    }
    env.pop("SCL_SMUS_ADMIN_ARNS", None)
    env.update(extra_env)
    return subprocess.run([str(_SCRIPT)], cwd=_ROOT, env=env, capture_output=True, text=True, check=False)


def test_existing_admin_role_preserves_fallback(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, "existing")

    assert result.returncode == 0
    assert "falling back to this account's existing 'Admin' role" in result.stdout
    assert "sts get-caller-identity" in aws_stub[1].read_text()
    assert "iam get-role" in aws_stub[1].read_text()


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("sts_failure", "UnableToLocateCredentials"),
        ("sts_expired", "ExpiredToken"),
    ],
)
def test_sts_failure_reports_credentials_and_stops_before_iam(
    aws_stub: tuple[Path, Path],
    scenario: str,
    reason: str,
) -> None:
    result = _run(aws_stub, scenario)
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert "Could not determine the AWS account" in output
    assert reason in output
    assert "has no role named 'Admin'" not in output
    assert "<account>" not in output
    assert _SENSITIVE_MARKER not in output
    assert "iam get-role" not in aws_stub[1].read_text()


def test_confirmed_missing_role_shows_known_account_remediation(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, "missing")
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert f"account {_ACCOUNT} has no role named 'Admin'" in output
    assert f"SCL_SMUS_ADMIN_ARNS=arn:aws:iam::{_ACCOUNT}:role/" in output
    assert "<account>" not in output


def test_access_denied_is_not_reported_as_a_missing_role(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, "denied")
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert "could not be verified" in output
    assert "AccessDenied" in output
    assert "has no role named 'Admin'" not in output
    assert _SENSITIVE_MARKER not in output


def test_network_failure_is_not_reported_as_a_missing_role(aws_stub: tuple[Path, Path]) -> None:
    result = _run(aws_stub, "network")
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert "could not be verified" in output
    assert "EndpointConnectionError" in output
    assert "has no role named 'Admin'" not in output
    assert _SENSITIVE_MARKER not in output


def test_explicit_admin_principal_skips_aws_lookup(aws_stub: tuple[Path, Path]) -> None:
    result = _run(
        aws_stub,
        "existing",
        SCL_SMUS_ADMIN_ARNS=f"arn:aws:iam::{_ACCOUNT}:role/ConfiguredAdmin",
    )

    assert result.returncode == 0
    assert not aws_stub[1].exists()
