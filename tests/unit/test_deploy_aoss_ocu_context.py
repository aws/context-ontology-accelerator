# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``deploy.sh`` forwards the OpenSearch Serverless OCU settings as CDK context.

``SCL_AOSS_MIN_OCU`` and ``SCL_AOSS_MAX_OCU`` become ``aoss_min_ocu`` / ``aoss_max_ocu``
on both ``cdk synth`` and ``cdk deploy``. Unset or empty passes nothing, so the storage
stack keeps its defaults: an empty ``aoss_min_ocu`` would read as ``Number("") == 0``,
which is scale-to-zero.

The script runs from a copy whose preflight scripts are stubs, with fake ``aws`` and
``pnpm`` first on ``PATH``; nothing here reaches AWS or builds anything.
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

# Every pnpm call is logged as one JSON argv list, so argument boundaries survive.
_FAKE_PNPM = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_PNPM_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
"""

# No test-databases stack, so deploy.sh skips the JDBC peering context.
_FAKE_AWS = """#!/usr/bin/env bash
echo "An error occurred (ValidationError): Stack does not exist" >&2
exit 254
"""


def _deploy(tmp_path: Path, **env_overrides: str) -> dict[str, list[str]]:
    """Run deploy.sh and return the argv of its ``cdk synth`` and ``cdk deploy`` calls."""
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(_ROOT / "scripts" / "deploy.sh", scripts / "deploy.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for path, body in (
        (scripts / "check-smus-admin-principal.sh", "#!/usr/bin/env bash\nexit 0\n"),
        (scripts / "preflight-deploy.sh", "#!/usr/bin/env bash\nexit 0\n"),
        (bin_dir / "pnpm", _FAKE_PNPM),
        (bin_dir / "aws", _FAKE_AWS),
    ):
        path.write_text(body)
        path.chmod(0o755)

    pnpm_log = tmp_path / "pnpm.log"
    # The caller's own deployment settings must not leak in.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SCL_", "AWS_", "CDK_"))}
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_PNPM_LOG": str(pnpm_log),
            "CDK_DOCKER": "docker",
            **env_overrides,
        }
    )
    result = subprocess.run(
        ["bash", str(scripts / "deploy.sh"), "dev"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    calls = [json.loads(line) for line in pnpm_log.read_text().splitlines()]
    cdk = {argv[argv.index("cdk") + 1]: argv for argv in calls if "cdk" in argv}
    assert set(cdk) == {"synth", "deploy"}, calls
    return cdk


def _context(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--context"]


def _aoss_context(argv: list[str]) -> list[str]:
    return [c for c in _context(argv) if c.startswith("aoss_")]


def test_unset_passes_no_ocu_context(tmp_path: Path) -> None:
    for argv in _deploy(tmp_path).values():
        assert _aoss_context(argv) == []
        assert "env=dev" in _context(argv)


def test_empty_passes_no_ocu_context(tmp_path: Path) -> None:
    for argv in _deploy(tmp_path, SCL_AOSS_MIN_OCU="", SCL_AOSS_MAX_OCU="").values():
        assert _aoss_context(argv) == []


def test_min_and_max_reach_synth_and_deploy(tmp_path: Path) -> None:
    for argv in _deploy(tmp_path, SCL_AOSS_MIN_OCU="4", SCL_AOSS_MAX_OCU="32").values():
        assert _aoss_context(argv) == ["aoss_min_ocu=4", "aoss_max_ocu=32"]


@pytest.mark.parametrize(("var", "key"), [("SCL_AOSS_MIN_OCU", "aoss_min_ocu"), ("SCL_AOSS_MAX_OCU", "aoss_max_ocu")])
def test_each_setting_is_forwarded_alone(tmp_path: Path, var: str, key: str) -> None:
    for argv in _deploy(tmp_path, **{var: "16"}).values():
        assert _aoss_context(argv) == [f"{key}=16"]


def test_zero_floor_is_forwarded(tmp_path: Path) -> None:
    for argv in _deploy(tmp_path, SCL_AOSS_MIN_OCU="0").values():
        assert _aoss_context(argv) == ["aoss_min_ocu=0"]


def test_invalid_value_is_left_for_synth_to_reject(tmp_path: Path) -> None:
    """The storage stack validates at synth; deploy.sh does not duplicate its rules."""
    for argv in _deploy(tmp_path, SCL_AOSS_MIN_OCU="3").values():
        assert _aoss_context(argv) == ["aoss_min_ocu=3"]
