# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The image import check fails when code reachable from an entrypoint imports something that cannot be imported."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "image_import_check.py"


def _check(tmp_path: Path, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
    for name, body in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(body, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "coa_app.main"], env=env, capture_output=True, text=True, check=False
    )


def test_fails_when_a_dependency_imported_inside_a_function_imports_a_missing_module(tmp_path):
    result = _check(
        tmp_path,
        {
            "coa_app/__init__.py": "",
            "coa_app/main.py": "from . import helpers\n",
            "coa_app/helpers.py": "def work():\n    import nlp_lib\n",
            "nlp_lib/__init__.py": "import click_like\n",
        },
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "click_like" in result.stdout


def test_passes_when_every_import_resolves_without_running_first_party_code(tmp_path):
    result = _check(
        tmp_path,
        {
            "coa_app/__init__.py": "",
            "coa_app/main.py": "import os\nimport present_dep\nBUCKET = os.environ['NOT_SET_AT_BUILD_TIME']\n",
            "present_dep/__init__.py": "",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
