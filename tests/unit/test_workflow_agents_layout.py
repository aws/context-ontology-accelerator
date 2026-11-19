# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The workflow-agents package uses the repo's build system only.

Root uv workspace and lock, root ruff and mypy config, Nx targets. This fails if a
second toolchain (own lock, CDK app, deploy scripts, task runner, linter config)
appears in the package.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts import sync_version

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "coa-ai" / "workflow-agents"
PYPROJECT = PACKAGE / "pyproject.toml"


@pytest.mark.parametrize("name", ["uv.lock", "cdk.json", "bin", "infra", "scripts", "Makefile"])
def test_no_separate_toolchain_path(name: str) -> None:
    assert not (PACKAGE / name).exists(), f"coa-ai/workflow-agents/{name} must not exist"


@pytest.mark.parametrize("table", ["poe", "ruff", "mypy"])
def test_pyproject_has_no_own_tool_config(table: str) -> None:
    assert table not in tomllib.loads(PYPROJECT.read_text()).get("tool", {})


def test_package_is_a_root_workspace_member() -> None:
    members = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"]["workspace"]["members"]
    assert "coa-ai/workflow-agents" in members


def test_version_sync_covers_the_package_manifests() -> None:
    manifests = set(sync_version.discover_manifests())
    assert {PYPROJECT, PACKAGE / "src" / "coa_workflow_agents" / "__init__.py"} <= manifests
