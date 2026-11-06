# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``deploy.sh`` forwards ``SCL_GRAPH_SOURCES`` as the ``graph_sources`` CDK context.

The retired ``SCL_SERVE_SPARQL_STRATEGY`` is no longer read; setting it gets a warning
naming the replacement, so a deployment that relied on it doesn't silently lose graph
sources. The script runs from a copy whose preflight scripts are stubs, with fake
``aws`` and ``pnpm`` first on ``PATH``; nothing here reaches AWS or builds anything.
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
_FAKE_AWS = """#!/usr/bin/env bash
echo "An error occurred (ValidationError): Stack does not exist" >&2
exit 254
"""


def _deploy(tmp_path: Path, **env_overrides: str) -> tuple[dict[str, list[str]], str]:
    """Run deploy.sh; return the argv of its ``cdk synth`` / ``cdk deploy`` calls and its output."""
    scripts = tmp_path / "repo" / "scripts"
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
        ["bash", str(scripts / "deploy.sh"), "dev"], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in pnpm_log.read_text().splitlines()]
    return {argv[argv.index("cdk") + 1]: argv for argv in calls if "cdk" in argv}, result.stdout + result.stderr


def _graph_context(argv: list[str]) -> list[str]:
    return [
        argv[i + 1]
        for i, a in enumerate(argv)
        if a == "--context" and argv[i + 1].startswith(("graph_", "serve_sparql"))
    ]


def test_unset_passes_no_graph_sources_context(tmp_path: Path) -> None:
    cdk, _ = _deploy(tmp_path)
    for argv in cdk.values():
        assert _graph_context(argv) == []


def test_scl_graph_sources_reaches_synth_and_deploy(tmp_path: Path) -> None:
    cdk, _ = _deploy(tmp_path, SCL_GRAPH_SOURCES="on")
    for argv in cdk.values():
        assert _graph_context(argv) == ["graph_sources=on"]


def test_the_retired_variable_is_not_forwarded_and_warns(tmp_path: Path) -> None:
    cdk, output = _deploy(tmp_path, SCL_SERVE_SPARQL_STRATEGY="true")
    for argv in cdk.values():
        assert _graph_context(argv) == []
    assert "SCL_SERVE_SPARQL_STRATEGY" in output
    assert "SCL_GRAPH_SOURCES=on" in output
