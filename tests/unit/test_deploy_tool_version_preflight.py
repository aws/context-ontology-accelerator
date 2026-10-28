# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the deploy preflight's Node.js and uv version checks (``check-tool-version.sh``).

The minimums live in package.json ``engines.node`` and pyproject.toml ``required-version``.
An old uv does not stop ``cdk synth`` (the Lambda bundling falls back to Docker), and a
Node.js with the right major but an old patch used to pass, so the preflight is where both
must be refused. Each test runs the script against a stub ``node`` / ``uv`` on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check-tool-version.sh"
_PREFLIGHT = _ROOT / "scripts" / "preflight-deploy.sh"
_DEPLOYING = _ROOT / "external-docs" / "content" / "deploying.md"


def _node_minimum() -> str:
    spec = json.loads((_ROOT / "package.json").read_text())["engines"]["node"]
    match = re.fullmatch(r">=\s*(\d+\.\d+\.\d+)", spec)
    assert match, f"precondition: engines.node is a '>=X.Y.Z' specifier, got {spec!r}"
    return match.group(1)


def _uv_minimum() -> str:
    spec = tomllib.loads((_ROOT / "pyproject.toml").read_text())["tool"]["uv"]["required-version"]
    match = re.fullmatch(r">=\s*(\d+\.\d+\.\d+)", spec)
    assert match, f"precondition: required-version is a '>=X.Y.Z' specifier, got {spec!r}"
    return match.group(1)


def _below(version: str) -> str:
    """The closest version strictly below ``version`` that differs only in its last non-zero part."""
    parts = [int(p) for p in version.split(".")]
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] > 0:
            parts[i] -= 1
            return ".".join(str(p) for p in parts)
    raise AssertionError(f"no version below {version}")


def _stub(tmp_path: Path, name: str, output: str, exit_code: int = 0) -> Path:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / name
    stub.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' '{output}'\nexit {exit_code}\n")
    stub.chmod(0o755)
    return stub_dir


def _bare_path(tmp_path: Path) -> Path:
    """A PATH holding only the utilities the script needs, so no real node/uv is found."""
    bare = tmp_path / "bare"
    bare.mkdir()
    for tool in ("sed", "dirname"):
        found = shutil.which(tool)
        assert found, f"precondition: {tool} on PATH"
        (bare / tool).symlink_to(found)
    return bare


def _run(tool: str, path: str, script: Path = _SCRIPT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script), tool],
        cwd=_ROOT,
        env={**os.environ, "PATH": path},
        capture_output=True,
        text=True,
        check=False,
    )


def _with_system_path(stub_dir: Path) -> str:
    return f"{stub_dir}{os.pathsep}{os.environ['PATH']}"


# ── Node.js ────────────────────────────────────────────────────────────────


def test_node_with_right_major_but_older_patch_is_rejected(tmp_path: Path) -> None:
    minimum = _node_minimum()
    old = _below(minimum)
    result = _run("node", _with_system_path(_stub(tmp_path, "node", f"v{old}")))

    assert result.returncode == 1
    assert f"Node.js {minimum}+ required (found {old})" in result.stdout
    assert f"nvm install {minimum}" in result.stdout


@pytest.mark.parametrize("banner", ["v{min}", "v{min}-nightly20261020", "v99.0.0"])
def test_node_at_or_above_the_minimum_passes(tmp_path: Path, banner: str) -> None:
    minimum = _node_minimum()
    result = _run("node", _with_system_path(_stub(tmp_path, "node", banner.format(min=minimum))))

    assert result.returncode == 0, result.stdout
    assert f"(minimum {minimum})" in result.stdout


def test_node_version_parts_compare_numerically_not_as_text(tmp_path: Path) -> None:
    major, minor, _patch = (int(p) for p in _node_minimum().split("."))
    # Text comparison would rank minor "123" below "23", and patch "99" above "4".
    newer = f"v{major}.{minor + 100}.0"
    older = f"v{major}.{minor - 1 if minor else 0}.99"

    assert _run("node", _with_system_path(_stub(tmp_path / "a", "node", newer))).returncode == 0
    if minor:
        assert _run("node", _with_system_path(_stub(tmp_path / "b", "node", older))).returncode == 1


def test_missing_node_is_reported_as_not_found(tmp_path: Path) -> None:
    result = _run("node", str(_bare_path(tmp_path)))

    assert result.returncode == 1
    assert "Node.js not found" in result.stdout


def test_unparseable_node_version_is_a_parse_failure(tmp_path: Path) -> None:
    result = _run("node", _with_system_path(_stub(tmp_path, "node", "unexpected")))

    assert result.returncode == 1
    assert "Could not parse the node version from 'unexpected'" in result.stdout


# ── uv ─────────────────────────────────────────────────────────────────────


def test_old_uv_is_rejected_with_the_upgrade_command(tmp_path: Path) -> None:
    minimum = _uv_minimum()
    old = _below(minimum)
    result = _run("uv", _with_system_path(_stub(tmp_path, "uv", f"uv {old}")))

    assert result.returncode == 1
    assert f"uv {minimum}+ required (found {old})" in result.stdout
    assert "uv self update" in result.stdout


@pytest.mark.parametrize("banner", ["uv {min}", "uv {min} (0123abcd 2026-10-20)", "uv 99.0.0"])
def test_uv_at_or_above_the_minimum_passes(tmp_path: Path, banner: str) -> None:
    minimum = _uv_minimum()
    result = _run("uv", _with_system_path(_stub(tmp_path, "uv", banner.format(min=minimum))))

    assert result.returncode == 0, result.stdout
    assert f"(minimum {minimum})" in result.stdout


def test_missing_uv_is_reported_as_not_found(tmp_path: Path) -> None:
    result = _run("uv", str(_bare_path(tmp_path)))

    assert result.returncode == 1
    assert "uv not found" in result.stdout


def test_failing_uv_version_command_is_reported(tmp_path: Path) -> None:
    result = _run("uv", _with_system_path(_stub(tmp_path, "uv", "boom", exit_code=3)))

    assert result.returncode == 1
    assert "Could not run 'uv --version'" in result.stdout


# ── Minimum sources ────────────────────────────────────────────────────────


def test_unreadable_minimum_is_reported_not_guessed(tmp_path: Path) -> None:
    # A copy of the script beside a package.json/pyproject.toml without a ">=" minimum.
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / _SCRIPT.name
    shutil.copy(_SCRIPT, script)
    (tmp_path / "package.json").write_text('{"engines": {"node": "^22"}}\n')
    (tmp_path / "pyproject.toml").write_text('[tool.uv]\nrequired-version = "==0.13.4"\n')
    stub_dir = _stub(tmp_path, "node", "v99.0.0")
    _stub(tmp_path, "uv", "uv 99.0.0")

    node = _run("node", _with_system_path(stub_dir), script)
    uv = _run("uv", _with_system_path(stub_dir), script)

    assert node.returncode == 1
    assert "Could not read the minimum Node.js version" in node.stdout
    assert uv.returncode == 1
    assert "Could not read the minimum uv version" in uv.stdout


def test_unknown_tool_is_a_usage_error() -> None:
    result = _run("java", os.environ["PATH"])

    assert result.returncode == 2
    assert "usage:" in result.stdout


def test_preflight_runs_the_node_and_uv_checks() -> None:
    script = _PREFLIGHT.read_text()

    assert "for _tool in node uv; do" in script
    assert '"$REPO_ROOT/scripts/check-tool-version.sh" "$_tool"' in script
    assert "NODE_MAJOR" not in script, "the major-only Node check let an older patch through"


def test_mise_pin_meets_the_node_minimum() -> None:
    mise = _ROOT / ".mise.toml"
    if not mise.exists():
        pytest.skip(".mise.toml is not part of this checkout")
    pinned = tomllib.loads(mise.read_text())["tools"]["node"]
    assert tuple(int(p) for p in pinned.split(".")) >= tuple(int(p) for p in _node_minimum().split("."))


@pytest.mark.parametrize(("row", "minimum"), [("Node.js", _node_minimum), ("uv", _uv_minimum)])
def test_deploying_prerequisites_state_the_enforced_minimum(row: str, minimum: Callable[[], str]) -> None:
    match = re.search(rf"^\| {re.escape(row)} \| ([^|]+?) \|", _DEPLOYING.read_text(), re.MULTILINE)
    assert match, f"deploying.md prerequisites table has no {row} row"
    assert match.group(1) == f"{minimum()}+"


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_tool_version_check_passes_shellcheck() -> None:
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(_SCRIPT)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
