# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Smithy-generate.sh installs the generated TypeScript clients from the workspace's pnpm lockfile.

``smithy-generate.sh`` starts with ``rm -rf smithy-generated``, which also removes the clients' pnpm-linked
``node_modules``. It then ran ``npm install`` inside each client, giving each a private npm install that is not the
workspace's. The gate's node-deps check  then failed after any regeneration on a clean tree, because each
client's node_modules is another install and its versions are npm's, not the lockfile's.

Each test runs the real script in a throwaway repo. Gradle, the OpenAPI generator (``java``), ``npm`` and ``pnpm``
are stubs on PATH that record each call (its arguments, its directory, and whether the clients were already in
place). Nothing is built, downloaded or installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_CLIENTS = ("control-plane-typescript-client", "data-layer-typescript-client")

_GRADLEW = """#!/usr/bin/env bash
set -e
out=build/smithyprojections/models
for proj in control-plane-ts data-layer-ts; do
  mkdir -p "$out/$proj/typescript-client-codegen"
  printf '{"name": "@coa/%s", "scripts": {"build": "concurrently yarn:build:cjs yarn:build:es"}}' "$proj" \\
    > "$out/$proj/typescript-client-codegen/package.json"
done
for proj in openapi-control-plane openapi-data-layer; do mkdir -p "$out/$proj/openapi"; done
echo '{"openapi": "3.0.0"}' > "$out/openapi-control-plane/openapi/ControlPlaneService.openapi.json"
echo '{"openapi": "3.0.0"}' > "$out/openapi-data-layer/openapi/DataLayerService.openapi.json"
"""

_JAVA = """#!/usr/bin/env bash
# The OpenAPI generator: writes the package whose models the script re-exports.
while [ $# -gt 0 ]; do
  case "$1" in -o) out=$2; shift 2 ;; --package-name) pkg=$2; shift 2 ;; *) shift ;; esac
done
mkdir -p "$out/$pkg"
echo "from $pkg.models.thing import Thing" > "$out/$pkg/__init__.py"
"""


def _stub(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _recorder(tool: str, root: Path, log: Path) -> str:
    """A stub that appends one JSON line per call; PLACED says whether both clients were copied by then."""
    checks = " && ".join(f'[ -f "{root}/smithy-generated/{c}/package.json" ]' for c in _CLIENTS)
    return (
        "#!/usr/bin/env bash\n"
        f"if {checks}; then placed=true; else placed=false; fi\n"
        'python3 -c \'import json, os, sys; print(json.dumps({"tool": sys.argv[1], "args": sys.argv[3:], '
        '"cwd": os.getcwd(), "clients_placed": sys.argv[2] == "true"}))\' '
        f'{tool} "$placed" "$@" >> "{log}"\n'
    )


def _run(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path, list[dict]]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    for name in ("smithy-generate.sh", "smithy-fingerprint.sh"):
        shutil.copy(_ROOT / "scripts" / name, root / "scripts" / name)
    (root / "models" / "src" / "main" / "smithy").mkdir(parents=True)
    (root / "models" / "src" / "main" / "smithy" / "x.smithy").write_text("namespace x\n")
    (root / "models" / "smithy-build.json").write_text("{}\n")
    _stub(root / "models" / "gradlew", _GRADLEW)
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.jsonl"
    _stub(bin_dir / "java", _JAVA)
    for tool in ("npm", "pnpm", "npx", "yarn"):
        _stub(bin_dir / tool, _recorder(tool, root, log))
    jar_home = tmp_path / "openapi-generator"
    jar_home.mkdir()
    (jar_home / "openapi-generator-cli-7.12.0.jar").write_text("stub")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "OPENAPI_GENERATOR_HOME": str(jar_home)}
    result = subprocess.run(
        ["/bin/bash", str(root / "scripts" / "smithy-generate.sh")], cwd=root, env=env, capture_output=True, text=True
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, root, calls


def test_the_generated_clients_are_installed_from_the_workspace_lockfile(tmp_path: Path) -> None:
    result, root, calls = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    installs = [c for c in calls if "install" in c["args"] or "i" in c["args"][:1]]
    assert installs == [
        {
            "tool": "pnpm",
            "args": ["install", "--frozen-lockfile"],
            "cwd": str(root),
            "clients_placed": True,
        }
    ], installs


def test_no_client_gets_a_private_npm_install(tmp_path: Path) -> None:
    _, root, calls = _run(tmp_path)
    in_clients = [c for c in calls if c["cwd"] in {str(root / "smithy-generated" / n) for n in _CLIENTS}]
    assert not [c for c in in_clients if c["tool"] == "npm" and c["args"][:1] == ["install"]], in_clients
    assert not list((root / "smithy-generated").glob("*/package-lock.json"))


def test_each_client_is_built_after_the_install(tmp_path: Path) -> None:
    _, root, calls = _run(tmp_path)
    install = next(i for i, c in enumerate(calls) if c["args"][:1] == ["install"])
    builds = {c["cwd"] for c in calls[install + 1 :] if c["args"][:2] == ["run", "build"]}
    assert builds == {str(root / "smithy-generated" / n) for n in _CLIENTS}, calls
    packages = [json.loads((root / "smithy-generated" / n / "package.json").read_text()) for n in _CLIENTS]
    builds_with = [p["scripts"]["build"] for p in packages]
    assert all("yarn:" not in b and not re.search(r"(?<!p)npm run", b) for b in builds_with), builds_with


def test_a_missing_pnpm_fails_first_with_a_clear_message(tmp_path: Path) -> None:
    """Run on its own, without pnpm, the script must say what is missing before the slow Gradle build."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    for name in ("smithy-generate.sh", "smithy-fingerprint.sh"):
        shutil.copy(_ROOT / "scripts" / name, root / "scripts" / name)
    gradle_log = tmp_path / "gradle-ran"
    _stub(root / "models" / "gradlew", f"#!/usr/bin/env bash\ntouch {gradle_log}\n")
    result = subprocess.run(
        ["/bin/bash", str(root / "scripts" / "smithy-generate.sh")],
        cwd=root,
        env={**os.environ, "PATH": str(_tools_only(tmp_path))},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "./scripts/install-pnpm.sh" in result.stdout + result.stderr
    # setup-dev.sh is no remedy on a fresh clone: its `uv sync` needs the members this script generates.
    assert "setup-dev.sh" not in result.stdout + result.stderr
    assert not gradle_log.exists()


def test_the_github_generate_job_enables_pnpm_before_running_the_script() -> None:
    """The script stops without pnpm, and the public mirror's PR workflow runs it on a bare runner."""
    workflow = (_ROOT / ".github" / "workflows" / "pr.yml").read_text()
    job = re.search(r"^  smithy-generate:\n(.*?)(?=^  \S)", workflow, re.S | re.M)
    assert job, "pr.yml has no smithy-generate job"
    steps = job.group(1)
    enables = steps.find('corepack prepare "pnpm@')
    assert 0 <= enables < steps.index("./scripts/smithy-generate.sh")


_MAKE = shutil.which("make")
# What the scripts and stubs run, besides shell builtins. A PATH holding only these hides any pnpm or npm installed on
# the machine, so the pnpm-missing cases run everywhere instead of skipping where pnpm is installed system-wide.
_TOOLS = ("sh", "sed", "head", "dirname", "cat", "chmod", "git")


def _tools_only(tmp_path: Path) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in _TOOLS:
        found = shutil.which(name)
        assert found, f"{name} is not installed"
        (tools / name).symlink_to(found)
    return tools


def _pnpm_pin() -> str:
    pnpm = re.search(r'^pnpm = "([^"]+)"', (_ROOT / ".mise.toml").read_text(), re.M)
    assert pnpm, ".mise.toml pins no pnpm"
    return pnpm.group(1)


def _npm_stub(bin_dir: Path, log: Path, *, puts_pnpm_on_path: bool = True) -> None:
    """`npm install -g pnpm@X` records the call and, like the real global install, puts pnpm on PATH."""
    pnpm_stub = f'#!/bin/sh\necho "pnpm $*" >> "{log}"\n'
    install = f'cat > "{bin_dir}/pnpm" <<\'STUB\'\n{pnpm_stub}STUB\nchmod +x "{bin_dir}/pnpm"\n'
    npm = f'#!/bin/sh\necho "npm $*" >> "{log}"\n'
    if puts_pnpm_on_path:
        npm += f'if [ "$1" = install ]; then\n{install}fi\n'
    _stub(bin_dir / "npm", npm)


def _make_setup(
    tmp_path: Path, *, pnpm_present: bool, mise: str | None = None, npm_puts_pnpm_on_path: bool = True
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the real `make setup` and scripts/install-pnpm.sh, with recording stubs for the other scripts and npm."""
    root, bin_dir, log = tmp_path / "repo", tmp_path / "bin", tmp_path / "calls.log"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(_ROOT / "Makefile", root / "Makefile")
    shutil.copy(_ROOT / "scripts" / "install-pnpm.sh", root / "scripts" / "install-pnpm.sh")
    (root / ".mise.toml").write_text((_ROOT / ".mise.toml").read_text() if mise is None else mise)
    sees_pnpm = 'if command -v pnpm >/dev/null 2>&1; then echo "${0##*/} pnpm=yes"; else echo "${0##*/} pnpm=no"; fi'
    for script in ("smithy-generate.sh", "setup-dev.sh"):
        _stub(root / "scripts" / script, f'#!/bin/sh\n{sees_pnpm} >> "{log}"\n')
    if pnpm_present:
        _stub(bin_dir / "pnpm", f'#!/bin/sh\necho "pnpm $*" >> "{log}"\n')
    _npm_stub(bin_dir, log, puts_pnpm_on_path=npm_puts_pnpm_on_path)
    result = subprocess.run(
        [str(_MAKE), "--no-print-directory", "setup"],
        cwd=root,
        env={**os.environ, "PATH": f"{bin_dir}:{_tools_only(tmp_path)}"},
        capture_output=True,
        text=True,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


@pytest.mark.skipif(not _MAKE, reason="make is not installed")
def test_make_setup_installs_the_pinned_pnpm_before_generating(tmp_path: Path) -> None:
    """smithy-generate.sh stops without pnpm, and setup-dev.sh (which would install it) needs the generated members."""
    result, calls = _make_setup(tmp_path, pnpm_present=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [
        f"npm install -g pnpm@{_pnpm_pin()}",
        "smithy-generate.sh pnpm=yes",
        "setup-dev.sh pnpm=yes",
    ], calls


@pytest.mark.skipif(not _MAKE, reason="make is not installed")
def test_make_setup_keeps_an_installed_pnpm(tmp_path: Path) -> None:
    result, calls = _make_setup(tmp_path, pnpm_present=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == ["pnpm --version", "smithy-generate.sh pnpm=yes", "setup-dev.sh pnpm=yes"], calls


@pytest.mark.skipif(not _MAKE, reason="make is not installed")
def test_make_setup_refuses_to_install_an_unpinned_pnpm(tmp_path: Path) -> None:
    """Without a pin, `npm install -g pnpm@` would install whatever pnpm is latest."""
    result, calls = _make_setup(tmp_path, pnpm_present=False, mise='[tools]\nnode = "24"\n')
    assert result.returncode != 0
    assert ".mise.toml pins no pnpm" in result.stdout + result.stderr
    assert calls == [], calls


@pytest.mark.skipif(not _MAKE, reason="make is not installed")
def test_make_setup_stops_when_the_installed_pnpm_is_not_on_path(tmp_path: Path) -> None:
    """npm's global bin directory may be off PATH; say so, rather than let smithy-generate.sh report pnpm missing."""
    result, calls = _make_setup(tmp_path, pnpm_present=False, npm_puts_pnpm_on_path=False)
    assert result.returncode != 0
    assert "is not on PATH" in result.stderr
    assert f"npm install -g pnpm@{_pnpm_pin()}" in calls
    assert not [c for c in calls if c.startswith(("smithy-generate.sh", "setup-dev.sh"))], calls


def test_setup_dev_installs_the_pinned_pnpm(tmp_path: Path) -> None:
    """Run on its own, setup-dev.sh installs the pnpm .mise.toml pins, not whatever pnpm is latest."""
    root, bin_dir, log = tmp_path / "repo", tmp_path / "bin", tmp_path / "calls.log"
    (root / "scripts").mkdir(parents=True)
    for name in ("setup-dev.sh", "install-pnpm.sh"):
        shutil.copy(_ROOT / "scripts" / name, root / "scripts" / name)
    shutil.copy(_ROOT / ".mise.toml", root / ".mise.toml")
    _stub(root / "scripts" / "check-java-version.sh", "#!/bin/sh\necho 'Java 17'\n")
    for tool in ("uv", "pre-commit"):
        _stub(bin_dir / tool, f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n')
    _npm_stub(bin_dir, log)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{_tools_only(tmp_path)}",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    result = subprocess.run(
        ["/bin/sh", str(root / "scripts" / "setup-dev.sh")], cwd=root, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    install = calls.index(f"npm install -g pnpm@{_pnpm_pin()}")
    assert calls.index("pnpm install --frozen-lockfile") > install, calls
