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
_SCRIPT = _ROOT / "scripts" / "check-java-version.sh"


def _stub_java(tmp_path: Path, output: str, exit_code: int = 0) -> Path:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "java"
    stub.write_text(
        f"""#!/usr/bin/env bash
cat >&2 <<'EOF'
{output}
EOF
exit {exit_code}
"""
    )
    stub.chmod(0o755)
    return stub_dir


def _run(stub_dir: Path, *, include_system_path: bool = True) -> subprocess.CompletedProcess[str]:
    search_path = str(stub_dir)
    if include_system_path:
        search_path = f"{search_path}{os.pathsep}{os.environ['PATH']}"
    return subprocess.run(
        ["/bin/bash", str(_SCRIPT)],
        cwd=_ROOT,
        env={**os.environ, "PATH": search_path},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("banner", "major"),
    [
        ('openjdk version "17.0.12" 2024-07-16', "17"),
        ('openjdk version "21.0.4" 2024-07-16 LTS', "21"),
        ('java version "21" 2023-09-19 LTS', "21"),
    ],
)
def test_supported_java_versions_pass(tmp_path: Path, banner: str, major: str) -> None:
    result = _run(_stub_java(tmp_path, banner))

    assert result.returncode == 0
    assert f"Java {major} found" in result.stdout


def test_java_tool_options_prefix_does_not_hide_version_banner(tmp_path: Path) -> None:
    result = _run(
        _stub_java(
            tmp_path,
            'Picked up JAVA_TOOL_OPTIONS: -Dfile.encoding=UTF-8\nopenjdk version "17.0.12" 2024-07-16',
        )
    )

    assert result.returncode == 0
    assert "Java 17 found" in result.stdout


def test_legacy_java_8_is_reported_as_too_old(tmp_path: Path) -> None:
    result = _run(_stub_java(tmp_path, 'java version "1.8.0_412"'))

    assert result.returncode == 1
    assert "Java 17+ required" in result.stdout
    assert "found 8" in result.stdout


def test_unrecognized_version_output_reports_parse_failure(tmp_path: Path) -> None:
    result = _run(_stub_java(tmp_path, "unexpected Java launcher output"))

    assert result.returncode == 1
    assert "Could not parse the Java version" in result.stdout
    assert "Java 17+ required" not in result.stdout


def test_failed_java_version_command_is_distinct_from_parse_failure(tmp_path: Path) -> None:
    result = _run(_stub_java(tmp_path, "launcher failed", exit_code=42))

    assert result.returncode == 1
    assert "Could not run 'java -version' (exit 42)" in result.stdout


def test_missing_java_executable_reports_not_found(tmp_path: Path) -> None:
    result = _run(tmp_path, include_system_path=False)

    assert result.returncode == 1
    assert "Java not found" in result.stdout
    assert "Could not parse" not in result.stdout


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_java_check_passes_shellcheck() -> None:
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(_SCRIPT)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
