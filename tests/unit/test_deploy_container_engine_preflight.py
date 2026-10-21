# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_HELPER = _ROOT / "scripts" / "lib" / "container-engine.sh"
_PREFLIGHT = _ROOT / "scripts" / "preflight-deploy.sh"

# Same ok/warn/err contract as preflight-deploy.sh; prints the outcome the
# preflight summary acts on.
_HARNESS = f"""
ERRORS=0
err() {{ echo "ERROR: $1"; ERRORS=$((ERRORS + 1)); }}
warn() {{ echo "WARN:  $1"; }}
ok() {{ echo "  OK:  $1"; }}
source "{_HELPER}"
coa_check_container_engine
echo "ERRORS=$ERRORS"
echo "CONTAINER_ENGINE=$CONTAINER_ENGINE"
echo "CDK_DOCKER=${{CDK_DOCKER:-}}"
"""


def _stub(bin_dir: Path, name: str, *, daemon_up: bool) -> None:
    stub = bin_dir / name
    stub.write_text(f'#!/bin/bash\n[ "$1" = info ] && exit {0 if daemon_up else 1}\nexit 0\n')
    stub.chmod(0o755)


def _run(tmp_path: Path, engines: dict[str, bool], **extra_env: str) -> subprocess.CompletedProcess[str]:
    """Run the check with ONLY the given engine stubs on PATH (name -> daemon up)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, up in engines.items():
        _stub(bin_dir, name, daemon_up=up)
    env = {k: v for k, v in os.environ.items() if k not in ("CDK_DOCKER", "SCL_ALLOW_NO_CONTAINER_ENGINE")}
    # /usr/bin:/bin supply coreutils; a real docker/finch there would leak into the test.
    env.update(PATH=f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", **extra_env)
    for real in ("docker", "finch"):
        found = shutil.which(real, path="/usr/bin:/bin")
        if found and real not in engines:
            pytest.skip(f"{found} is on the base PATH and would shadow the no-engine case")
    return subprocess.run(["/bin/bash", "-c", _HARNESS], env=env, capture_output=True, text=True, check=False)


def test_no_engine_is_an_error_that_names_the_image_stacks(tmp_path: Path) -> None:
    out = _run(tmp_path, {}).stdout

    assert "ERRORS=1" in out
    assert "ERROR: No container engine running" in out
    assert "container images" in out
    assert "mcp, ontology, serve, sources and vkg" in out
    assert "Start Docker or Finch, or set CDK_DOCKER" in out
    assert "SCL_ALLOW_NO_CONTAINER_ENGINE=1" in out
    assert "local pip bundling" not in out


def test_engine_installed_but_daemon_down_is_the_same_error(tmp_path: Path) -> None:
    out = _run(tmp_path, {"docker": False, "finch": False}).stdout

    assert "ERRORS=1" in out
    assert "ERROR: No container engine running" in out
    assert "CONTAINER_ENGINE=\n" in out


def test_opt_out_downgrades_to_a_warning_that_still_says_images_cannot_build(tmp_path: Path) -> None:
    out = _run(tmp_path, {}, SCL_ALLOW_NO_CONTAINER_ENGINE="1").stdout

    assert "ERRORS=0" in out
    assert "WARN:  No container engine running" in out
    assert "Container images cannot be built" in out


def test_opt_out_requires_exactly_1(tmp_path: Path) -> None:
    out = _run(tmp_path, {}, SCL_ALLOW_NO_CONTAINER_ENGINE="yes").stdout

    assert "ERRORS=1" in out


def test_running_docker_passes(tmp_path: Path) -> None:
    out = _run(tmp_path, {"docker": True}).stdout

    assert "ERRORS=0" in out
    assert "OK:  Docker daemon running" in out
    assert "CONTAINER_ENGINE=docker" in out


def test_running_finch_is_preferred_and_exported(tmp_path: Path) -> None:
    out = _run(tmp_path, {"docker": True, "finch": True}).stdout

    assert "ERRORS=0" in out
    assert "CONTAINER_ENGINE=finch" in out
    assert "CDK_DOCKER=finch" in out


def test_down_finch_falls_back_to_running_docker(tmp_path: Path) -> None:
    out = _run(tmp_path, {"docker": True, "finch": False}).stdout

    assert "ERRORS=0" in out
    assert "CONTAINER_ENGINE=docker" in out


def test_explicit_cdk_docker_unreachable_is_an_error(tmp_path: Path) -> None:
    out = _run(tmp_path, {"docker": True, "podman": False}, CDK_DOCKER="podman").stdout

    assert "ERRORS=1" in out
    assert "CDK_DOCKER=podman set but daemon is not reachable" in out


def test_explicit_cdk_docker_running_is_used(tmp_path: Path) -> None:
    out = _run(tmp_path, {"podman": True}, CDK_DOCKER="podman").stdout

    assert "ERRORS=0" in out
    assert "CONTAINER_ENGINE=podman" in out


def test_preflight_uses_the_helper() -> None:
    preflight = _PREFLIGHT.read_text()

    assert 'source "${REPO_ROOT}/scripts/lib/container-engine.sh"' in preflight
    assert "coa_check_container_engine" in preflight
    assert "local pip bundling must succeed" not in preflight


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_helper_passes_shellcheck() -> None:
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(_HELPER)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stdout + result.stderr
