# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The pip SBOM check fails an image build when its python path holds a pip that ships a vendored SBOM."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pip_sbom_check.py"

FakePip = Literal["plain", "sbom", "sbom-leftover"]


def _fake_pip(root: Path, kind: FakePip) -> Path:
    """Write a fake pip under root: a package without an SBOM, with one, or only a leftover _vendor dir."""
    pip = root / "pip"
    pip.mkdir(parents=True)
    if kind != "sbom-leftover":
        (pip / "__init__.py").write_text("", encoding="utf-8")
    if kind != "plain":
        (pip / "_vendor").mkdir()
        (pip / "_vendor" / "bom.cdx.json").write_text("{}", encoding="utf-8")
    return root


def _check(*paths: Path, args: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    """Run the check with site-packages disabled (-S), so only the given paths can hold a pip."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(p) for p in paths)}
    return subprocess.run(
        [sys.executable, "-S", str(_SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("args", [(), ("--absent",)])
def test_passes_when_pip_is_absent(tmp_path, args):
    result = _check(tmp_path, args=args)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_passes_when_pip_has_no_vendored_sbom(tmp_path):
    result = _check(_fake_pip(tmp_path, "plain"))
    assert result.returncode == 0, result.stderr


def test_fails_when_pip_has_a_vendored_sbom(tmp_path):
    result = _check(_fake_pip(tmp_path, "sbom"))
    assert result.returncode == 1
    assert "bom.cdx.json" in result.stderr


def test_fails_on_an_sbom_in_a_pip_shadowed_by_another(tmp_path):
    result = _check(_fake_pip(tmp_path / "first", "plain"), _fake_pip(tmp_path / "second", "sbom"))
    assert result.returncode == 1
    assert str(tmp_path / "second") in result.stderr


def test_fails_on_an_sbom_left_without_a_pip_package(tmp_path):
    result = _check(_fake_pip(tmp_path, "sbom-leftover"), args=("--absent",))
    assert result.returncode == 1
    assert "bom.cdx.json" in result.stderr or "still installed" in result.stderr


def test_absent_mode_fails_on_any_pip(tmp_path):
    result = _check(_fake_pip(tmp_path, "plain"), args=("--absent",))
    assert result.returncode == 1
    assert "pip is still installed" in result.stderr
