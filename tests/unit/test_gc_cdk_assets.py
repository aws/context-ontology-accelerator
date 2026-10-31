# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``scripts/gc-cdk-assets.sh`` (``make gc-assets``).

The script wraps ``cdk gc`` on the shared CDK bootstrap image repository. It
must never let a deleting run start while a stack is changing or while an ECS
service runs an asset image no CloudFormation template references, since
``cdk gc`` would delete that image. Each test runs the script against a stub
``aws`` and a stub ``cdk`` that records its arguments.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "gc-cdk-assets.sh"

_ACCOUNT = "123456789012"
_REPO = f"cdk-hnb659fds-container-assets-{_ACCOUNT}-us-east-1"
_LIVE = "a" * 64
_STALE = "b" * 64
_CDK_NAME = "cdk-stub"

# Answers come from AWS_STUB_* variables; AWS_STUB_FAIL names one "<service>
# <operation>" pair that fails like a service error.
_AWS_STUB = """#!/usr/bin/env bash
set -u
printf '%s\\n' "$*" >>"$AWS_STUB_LOG"
if [ "$1 $2" = "${AWS_STUB_FAIL:-}" ]; then echo "An error occurred (AccessDenied)" >&2; exit 254; fi
case "$1 $2" in
  "sts get-caller-identity") echo "$AWS_STUB_ACCOUNT" ;;
  "cloudformation describe-stacks") echo "$AWS_STUB_REPO" ;;
  "cloudformation list-stacks") printf '%b\\n' "$AWS_STUB_STACKS" ;;
  "cloudformation get-template") echo "\\"image: $AWS_STUB_TEMPLATE_TAG\\"" ;;
  "ecs list-clusters") echo "${AWS_STUB_CLUSTERS:-}" ;;
  "ecs list-services") echo "${AWS_STUB_SERVICES:-}" ;;
  "ecs describe-services") printf '%b\\n' "${AWS_STUB_DEPLOYED:-}" ;;
  "ecs describe-task-definition") echo "$AWS_STUB_IMAGE" ;;
  *) echo "unexpected aws invocation: $*" >&2; exit 2 ;;
esac
"""

# cdk gc logs its counts at debug level, between noise lines.
_CDK_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$CDK_STUB_LOG"
pwd >>"$CDK_STUB_LOG"
echo "[15:35:29] Toolkit stack: CDKToolkit"
printf '%b' "${CDK_STUB_OUTPUT:-}"
exit "${CDK_STUB_EXIT:-0}"
"""

_GC_COUNTS = "[15:35:44] 100 isolated images\\n[15:35:44] 0 not isolated images\\n[15:35:44] 100 images total\\n"
_GC_COUNTS += "[15:35:44] 0 deletable assets\\n[15:35:44] 100 taggable assets\\n[15:35:44] 0 assets to untag\\n"


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("aws", _AWS_STUB), (_CDK_NAME, _CDK_STUB)):
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    return tmp_path


def _run(
    stubs: Path,
    *,
    action: str | None = None,
    stacks: str = "coa-dev-vkg\\tUPDATE_COMPLETE",
    service_image_tag: str | None = None,
    extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GC_", "AWS_", "CDK_"))}
    env.update(
        {
            "PATH": f"{stubs / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "AWS_REGION": "us-east-1",
            "CDK_BIN": str(stubs / "bin" / _CDK_NAME),
            "AWS_STUB_LOG": str(stubs / "aws.log"),
            "CDK_STUB_LOG": str(stubs / "cdk.log"),
            "AWS_STUB_ACCOUNT": _ACCOUNT,
            "AWS_STUB_REPO": _REPO,
            "AWS_STUB_STACKS": stacks,
            "AWS_STUB_TEMPLATE_TAG": _LIVE,
            "CDK_STUB_OUTPUT": _GC_COUNTS,
        }
    )
    if service_image_tag is not None:
        env.update(
            {
                "AWS_STUB_CLUSTERS": "arn:aws:ecs:us-east-1:123456789012:cluster/coa-dev-vkg",
                "AWS_STUB_SERVICES": "arn:aws:ecs:us-east-1:123456789012:service/coa-dev-vkg/ns-1",
                "AWS_STUB_DEPLOYED": "ns-1\\ttd/vkg:3 td/vkg:2",
                "AWS_STUB_IMAGE": f"{_ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/{_REPO}:{service_image_tag}",
            }
        )
    if action is not None:
        env["GC_ACTION"] = action
    env.update(extra or {})
    return subprocess.run(["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, check=False)


def _cdk_calls(stubs: Path) -> list[str]:
    log = stubs / "cdk.log"
    return log.read_text().splitlines() if log.exists() else []


def test_default_is_a_read_only_ecr_print(stubs: Path) -> None:
    result = _run(stubs)

    assert result.returncode == 0, result.stderr
    args, cwd = _cdk_calls(stubs)
    assert args == (
        f"gc aws://{_ACCOUNT}/us-east-1 --unstable=gc --type=ecr --action=print "
        "--rollback-buffer-days=30 --created-buffer-days=1 --verbose"
    )
    # Run away from infra/cdk.json, which would make cdk gc synthesize the app.
    assert not (Path(cwd) / "cdk.json").exists()
    assert _REPO in result.stdout


def test_print_shows_only_the_counts(stubs: Path) -> None:
    result = _run(stubs)

    assert result.returncode == 0, result.stderr
    assert "  100 taggable assets\n" in result.stdout
    assert "  0 deletable assets\n" in result.stdout
    assert "Toolkit stack" not in result.stdout


def test_print_without_counts_shows_the_full_output(stubs: Path) -> None:
    result = _run(stubs, extra={"CDK_STUB_OUTPUT": ""})

    assert result.returncode == 0, result.stderr
    assert "WARNING: cdk gc printed no counts" in result.stdout
    assert "Toolkit stack: CDKToolkit" in result.stdout


def test_failed_print_shows_the_cdk_output(stubs: Path) -> None:
    result = _run(stubs, extra={"CDK_STUB_OUTPUT": "StackRefreshFailed\\n", "CDK_STUB_EXIT": "1"})

    assert result.returncode == 1
    assert "StackRefreshFailed" in result.stderr


def test_deleting_run_is_not_verbose(stubs: Path) -> None:
    # A deleting run without GC_YES prompts, so its output must stay on the terminal.
    result = _run(stubs, action="full")

    assert result.returncode == 0, result.stderr
    assert "--verbose" not in _cdk_calls(stubs)[0]
    assert "Toolkit stack: CDKToolkit" in result.stdout


def test_gc_yes_skips_the_prompt(stubs: Path) -> None:
    result = _run(stubs, action="full", extra={"GC_YES": "1"})

    assert result.returncode == 0, result.stderr
    assert _cdk_calls(stubs)[0].endswith("--confirm=false")


@pytest.mark.parametrize(
    ("knob", "value"),
    [
        ("GC_ACTION", "delete"),
        ("GC_TYPE", "docker"),
        ("GC_ROLLBACK_BUFFER_DAYS", "-1"),
        ("GC_CREATED_BUFFER_DAYS", "0"),
    ],
)
def test_bad_settings_are_rejected(stubs: Path, knob: str, value: str) -> None:
    result = _run(stubs, extra={knob: value})

    assert result.returncode == 1
    assert knob in result.stderr
    assert _cdk_calls(stubs) == []


@pytest.mark.parametrize("action", ["full", "delete-tagged"])
def test_deleting_refused_while_a_stack_is_changing(stubs: Path, action: str) -> None:
    result = _run(stubs, action=action, stacks="coa-dev-vkg\\tUPDATE_IN_PROGRESS")

    assert result.returncode == 1
    assert "coa-dev-vkg (UPDATE_IN_PROGRESS)" in result.stderr
    assert _cdk_calls(stubs) == []


def test_print_runs_while_a_stack_is_changing(stubs: Path) -> None:
    result = _run(stubs, stacks="coa-dev-vkg\\tUPDATE_IN_PROGRESS")

    assert result.returncode == 0, result.stderr
    assert len(_cdk_calls(stubs)) == 2


def test_change_set_only_stacks_do_not_block(stubs: Path) -> None:
    # The MR dry run leaves stacks in REVIEW_IN_PROGRESS; they are not deploys in flight.
    result = _run(stubs, action="full", stacks="coa-dev-vkg\\tREVIEW_IN_PROGRESS")

    assert result.returncode == 0, result.stderr


def test_deleting_refused_when_a_service_runs_an_unreferenced_image(stubs: Path) -> None:
    result = _run(stubs, action="full", service_image_tag=_STALE)

    assert result.returncode == 1
    assert f"ns-1 uses {_REPO}:{_STALE}" in result.stderr
    assert _cdk_calls(stubs) == []


def test_print_warns_when_a_service_runs_an_unreferenced_image(stubs: Path) -> None:
    result = _run(stubs, service_image_tag=_STALE)

    assert result.returncode == 0, result.stderr
    assert f"ns-1 uses {_REPO}:{_STALE}" in result.stdout
    assert len(_cdk_calls(stubs)) == 2


@pytest.mark.parametrize("status", ["DELETE_FAILED", "CREATE_FAILED", "REVIEW_IN_PROGRESS"])
def test_templates_cdk_gc_ignores_do_not_protect_an_image(stubs: Path, status: str) -> None:
    # cdk gc skips stacks in these states, so an image only they reference is deleted.
    result = _run(stubs, action="full", stacks=f"coa-dev-vkg\\t{status}", service_image_tag=_LIVE)

    assert result.returncode == 1
    assert f"ns-1 uses {_REPO}:{_LIVE}" in result.stderr
    assert _cdk_calls(stubs) == []


def test_service_on_a_referenced_image_does_not_block(stubs: Path) -> None:
    result = _run(stubs, action="full", service_image_tag=_LIVE)

    assert result.returncode == 0, result.stderr
    assert "uses" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "operation",
    ["ecs list-clusters", "ecs describe-services", "ecs describe-task-definition", "cloudformation get-template"],
)
def test_a_failed_check_stops_the_run(stubs: Path, operation: str) -> None:
    result = _run(stubs, action="full", service_image_tag=_LIVE, extra={"AWS_STUB_FAIL": operation})

    assert result.returncode == 1
    assert _cdk_calls(stubs) == []


def test_no_credentials_exits_2(stubs: Path) -> None:
    result = _run(stubs, extra={"AWS_STUB_FAIL": "sts get-caller-identity"})

    assert result.returncode == 2
    assert _cdk_calls(stubs) == []
