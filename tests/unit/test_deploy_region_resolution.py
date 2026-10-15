# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Region resolution shared by the deploy, preflight and destroy scripts.

The CDK CLI resolves the region from the active AWS profile and passes it to the CDK
app as ``CDK_DEFAULT_REGION``, but only inside the app's process: the calling shell
never sees it. A script that stopped at the environment variables fell back to
us-east-1 while the platform stacks landed in the profile's region, so preflight
checked the wrong region and the connector deploy reported COA as not deployed at all.

``scripts/lib/resolve-region.sh`` adds the profile's region before the us-east-1
default, and every script that resolves a region sources it. A fake ``aws`` first on
``PATH`` stands in for the profile; nothing here reaches AWS, and the connector
scripts stop at their first SSM read, before anything is built.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
_HELPER = _SCRIPTS / "lib" / "resolve-region.sh"
_PROFILE_REGION = "ap-northeast-1"

# Every script that resolves the region the CDK stacks deploy to.
_REGION_SCRIPTS = [
    "deploy-example-connector.sh",
    "deploy-managed-databricks-connector.sh",
    "deploy-serve.sh",
    "destroy.sh",
    "preflight-deploy.sh",
]

# Deploy tooling a connector script could reach past its SSM reads. Each is replaced by
# a stub that fails loudly, so a regression can never start a real build or deploy.
_POISONED_TOOLS = ["cdk", "docker", "finch", "java", "make", "mvn", "node", "npm", "npx", "pnpm"]


def _fake_aws(tmp_path: Path, configure_body: str) -> tuple[Path, Path]:
    """A fake ``aws`` that runs ``configure_body`` for ``configure get region``.

    Every call is logged; any other call fails, the way a missing SSM parameter does.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    call_log = tmp_path / "aws-calls.log"
    stub = stub_dir / "aws"
    stub.write_text(
        f"""#!/bin/bash
printf '%s\\n' "$*" >> '{call_log}'
if [ "$1 $2 $3" = "configure get region" ]; then
{configure_body}
fi
echo "fake aws: no such resource" >&2
exit 254
"""
    )
    stub.chmod(0o755)
    return stub_dir, call_log


def _profile_with_region(tmp_path: Path, region: str = _PROFILE_REGION) -> tuple[Path, Path]:
    return _fake_aws(tmp_path, f"  echo {region}\n  exit 0")


def _resolve(path: str, home: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Source the helper the way the scripts do, under their strictest shell options."""
    return subprocess.run(
        ["/bin/bash", "-c", 'set -euo pipefail; source "$1"; coa_resolve_region', "resolve", str(_HELPER)],
        env={"PATH": path, "HOME": str(home), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_coa_resolve_region_no_env_uses_profile_region(tmp_path: Path) -> None:
    stub_dir, call_log = _profile_with_region(tmp_path)

    result = _resolve(str(stub_dir), tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_PROFILE_REGION}\n"
    assert call_log.read_text() == "configure get region\n"


def test_coa_resolve_region_aws_profile_selects_that_profiles_region(tmp_path: Path) -> None:
    stub_dir, _ = _fake_aws(
        tmp_path,
        '  if [ "${AWS_PROFILE:-}" = "eu-team" ]; then echo eu-west-1; else echo ap-northeast-1; fi\n  exit 0',
    )

    result = _resolve(str(stub_dir), tmp_path, AWS_PROFILE="eu-team")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "eu-west-1\n"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {"CDK_DEFAULT_REGION": "eu-central-1", "AWS_DEFAULT_REGION": "us-west-2", "AWS_REGION": "ca-central-1"},
            "eu-central-1",
        ),
        ({"AWS_DEFAULT_REGION": "us-west-2", "AWS_REGION": "ca-central-1"}, "us-west-2"),
        ({"AWS_REGION": "ca-central-1"}, "ca-central-1"),
    ],
    ids=["cdk_default_region", "aws_default_region", "aws_region"],
)
def test_coa_resolve_region_env_var_wins_over_profile_without_calling_aws(
    tmp_path: Path, env: dict[str, str], expected: str
) -> None:
    stub_dir, call_log = _profile_with_region(tmp_path)

    result = _resolve(str(stub_dir), tmp_path, **env)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected}\n"
    assert not call_log.exists(), "the profile lookup must not run when the environment already names a region"


def test_coa_resolve_region_empty_env_var_falls_through_to_profile(tmp_path: Path) -> None:
    stub_dir, _ = _profile_with_region(tmp_path)

    result = _resolve(str(stub_dir), tmp_path, CDK_DEFAULT_REGION="", AWS_DEFAULT_REGION="", AWS_REGION="")

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_PROFILE_REGION}\n"


def test_coa_resolve_region_profile_without_region_defaults_to_us_east_1(tmp_path: Path) -> None:
    # `aws configure get region` prints nothing and exits 1 when the profile sets no region.
    stub_dir, _ = _fake_aws(tmp_path, "  exit 1")

    result = _resolve(str(stub_dir), tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "us-east-1\n"


def test_coa_resolve_region_failing_aws_cli_defaults_to_us_east_1(tmp_path: Path) -> None:
    # Whatever a failing CLI printed is not a region.
    stub_dir, _ = _fake_aws(tmp_path, '  echo "not-a-region"\n  echo "config file is unreadable" >&2\n  exit 255')

    result = _resolve(str(stub_dir), tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "us-east-1\n"
    assert result.stderr == ""


def test_coa_resolve_region_missing_aws_cli_defaults_to_us_east_1(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    result = _resolve(str(empty_dir), tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "us-east-1\n"
    assert result.stderr == ""


@pytest.mark.parametrize("script", _REGION_SCRIPTS)
def test_region_script_sources_shared_helper(script: str) -> None:
    text = (_SCRIPTS / script).read_text()

    assert 'source "${REPO_ROOT}/scripts/lib/resolve-region.sh"' in text
    assert 'REGION="$(coa_resolve_region)"' in text


def test_region_fallback_chain_lives_only_in_shared_helper() -> None:
    # A private copy of the chain is how four scripts lost the profile lookup that one kept.
    copies = sorted(
        str(path.relative_to(_ROOT))
        for path in _SCRIPTS.rglob("*.sh")
        if path != _HELPER and "${CDK_DEFAULT_REGION:-" in path.read_text()
    )

    assert not copies, f"resolve the region with scripts/lib/resolve-region.sh instead of a private chain: {copies}"


@pytest.mark.parametrize(
    "script",
    ["deploy-managed-databricks-connector.sh", "deploy-example-connector.sh"],
)
def test_connector_deploy_profile_region_only_reads_ssm_in_profile_region(tmp_path: Path, script: str) -> None:
    stub_dir, call_log = _profile_with_region(tmp_path)
    poison_log = tmp_path / "poisoned-calls.log"
    for tool in _POISONED_TOOLS:
        stub = stub_dir / tool
        stub.write_text(f"#!/bin/bash\nprintf '%s\\n' \"{tool} $*\" >> '{poison_log}'\nexit 97\n")
        stub.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(_SCRIPTS / script), "dev"],
        cwd=_ROOT,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    # The fake account has no COA deployment, so the script stops at its SSM check.
    assert result.returncode == 1, result.stdout + result.stderr
    assert not poison_log.exists(), poison_log.read_text()
    assert re.search(rf"Region:\s+{_PROFILE_REGION}$", result.stdout, re.MULTILINE), result.stdout
    assert f"in this account/{_PROFILE_REGION} under prefix" in result.stderr
    ssm_reads = [line for line in call_log.read_text().splitlines() if line.startswith("ssm get-parameter")]
    assert ssm_reads, "the script never read COA's SSM parameters"
    assert all(f"--region {_PROFILE_REGION}" in line for line in ssm_reads), ssm_reads


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_region_helper_passes_shellcheck() -> None:
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(_HELPER)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
