# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``deploy-serve.sh`` deploys only the serve stack, with the same ``SCL_*`` context as ``deploy.sh``.

Without ``--exclusively`` CDK also deploys the stacks serve depends on (network, storage,
auth, authnz, namespace), and with only a few context keys those stacks synthesize from
defaults. The script runs from a copy with fake ``aws`` and ``pnpm`` first on ``PATH``
and ``SKIP_BUILD=1``; nothing here reaches AWS or builds an image.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]

_FAKE_PNPM = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_PNPM_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
"""

# Answers the aws calls the script relies on (account lookup and the optional
# test-databases stack before the deploy, runtime ARN and status after it) so it runs
# to the end; every other call exits 0. The test-databases stack exists only when
# FAKE_TEST_DB_STACK=1, and then reports a peer VPC id and CIDR.
_FAKE_AWS = """#!/usr/bin/env bash
case "$1 $2" in
  "sts get-caller-identity") echo 123456789012 ;;
  "cloudformation describe-stacks")
    case "$*" in
      *-integ-test-databases*)
        [ "${FAKE_TEST_DB_STACK:-}" = 1 ] || { echo "Stack does not exist" >&2; exit 254; }
        case "$*" in
          *VpcCidr*) echo 10.99.0.0/16 ;;
          *) echo vpc-0peer ;;
        esac ;;
      *) echo arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/rt-1 ;;
    esac ;;
  "bedrock-agentcore-control get-agent-runtime") echo READY ;;
  *) exit 0 ;;
esac
"""


def _deploy_serve(tmp_path: Path, **env_overrides: str) -> list[str]:
    """Run deploy-serve.sh; return the argv of its ``cdk deploy`` call."""
    scripts = tmp_path / "repo" / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy(_ROOT / "scripts" / "deploy-serve.sh", scripts / "deploy-serve.sh")
    for lib in ("cdk-context.sh", "resolve-region.sh"):
        shutil.copy(_ROOT / "scripts" / "lib" / lib, scripts / "lib" / lib)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for path, body in ((bin_dir / "pnpm", _FAKE_PNPM), (bin_dir / "aws", _FAKE_AWS)):
        path.write_text(body)
        path.chmod(0o755)

    pnpm_log = tmp_path / "pnpm.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SCL_", "AWS_", "CDK_"))}
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_PNPM_LOG": str(pnpm_log),
            "SKIP_BUILD": "1",
            **env_overrides,
        }
    )
    result = subprocess.run(
        ["bash", str(scripts / "deploy-serve.sh"), "dev"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    calls = [json.loads(line) for line in pnpm_log.read_text().splitlines()]
    deploys = [argv for argv in calls if "deploy" in argv and "cdk" in argv]
    assert len(deploys) == 1, calls
    return deploys[0]


def _context(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--context"]


def test_deploys_only_the_serve_stack(tmp_path: Path) -> None:
    argv = _deploy_serve(tmp_path)
    assert argv[argv.index("deploy") + 1] == "coa-dev-serve"
    assert "--exclusively" in argv


def test_scl_settings_reach_the_serve_deploy(tmp_path: Path) -> None:
    argv = _deploy_serve(
        tmp_path,
        SCL_PREFIX="acme",
        SCL_VPC_ID="vpc-0abc",
        SCL_VPC_CIDR="10.40.0.0/16",
        SCL_SMUS_ADMIN_ARNS="arn:aws:iam::123456789012:role/Ops",
        SCL_RESOLVE_TIMEOUT_SECONDS="120",
        SCL_DEEP_REASONING_STABILITY_STEPS="3",
        SCL_DEEP_REASONING_ONTOLOGY_TIMEOUT_SECONDS="20",
        SCL_NEPTUNE_TIMEOUT_SECONDS="25",
        SCL_NL2SQL_GRAPH_EXPAND="false",
    )
    assert argv[argv.index("deploy") + 1] == "acme-dev-serve"
    assert "--exclusively" in argv
    context = _context(argv)
    assert "resource_prefix=acme" in context
    assert "vpc_id=vpc-0abc" in context
    assert "vpc_cidr=10.40.0.0/16" in context
    assert "smus_admin_principal_arns=arn:aws:iam::123456789012:role/Ops" in context
    assert "resolve_timeout_s=120" in context
    assert "deep_reasoning_stability_steps=3" in context
    assert "deep_reasoning_ontology_timeout_s=20" in context
    assert "neptune_timeout_s=25" in context
    assert "serve_nl2sql_graph_expand=false" in context


def test_unset_settings_add_no_context_beyond_env_and_image(tmp_path: Path) -> None:
    context = _context(_deploy_serve(tmp_path))
    assert [c.split("=", 1)[0] for c in context] == ["env", "context_manager_image_uri"]


def test_peering_context_from_the_test_databases_stack_reaches_the_serve_deploy(tmp_path: Path) -> None:
    # serve-stack builds cross-VPC JDBC egress rules from jdbc_peer_cidrs, so a serve
    # deploy without it would delete the rules an earlier `make deploy-dev` created.
    context = _context(_deploy_serve(tmp_path, FAKE_TEST_DB_STACK="1"))
    assert "jdbc_peer_vpc_id=vpc-0peer" in context
    assert "jdbc_peer_cidrs=10.99.0.0/16" in context


def test_explicit_peering_variables_win_over_the_stack_lookup(tmp_path: Path) -> None:
    context = _context(_deploy_serve(tmp_path, SCL_JDBC_PEER_VPC_ID="vpc-0mine", SCL_JDBC_PEER_CIDRS="10.50.0.0/16"))
    assert "jdbc_peer_vpc_id=vpc-0mine" in context
    assert "jdbc_peer_cidrs=10.50.0.0/16" in context
